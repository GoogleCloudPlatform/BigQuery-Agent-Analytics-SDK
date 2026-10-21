// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// BigQuery REST requests the writer makes through `$.http.fetch`:
// tabledata.insertAll (the legacy streaming API, which takes JSON over
// HTTP/1.1) and tables.insert for the optional create-on-first-use. The
// Storage Write API is gRPC only, which `$.http.fetch` does not speak.

import type { Config } from './config.ts'
import { AGENT_EVENTS_SCHEMA, type BqaaRow } from './rows.ts'
import { redactDiagnostic } from './sanitize.ts'

const API = 'https://bigquery.googleapis.com/bigquery/v2'
const seg = (s: string): string => encodeURIComponent(s)

export const insertAllUrl = (c: Config): string =>
  `${API}/projects/${seg(c.project)}/datasets/${seg(c.dataset)}/tables/${seg(c.table)}/insertAll`

export const tablesUrl = (c: Config): string => `${API}/projects/${seg(c.project)}/datasets/${seg(c.dataset)}/tables`

// Google's token information endpoint: names the account a token was issued
// to. The token goes in a POST body, never in the URL.
export const TOKEN_INFO_URL = 'https://oauth2.googleapis.com/tokeninfo'

// `insertId` is the row's event_id, so BigQuery can drop a retried row on a
// best-effort basis; event_id stays in the row for exact dedup in SQL.
// skipInvalidRows: a bad row is reported alone instead of failing the batch.
// ignoreUnknownValues: a table created before schema v2 (no event_id column)
// still takes the rows.
export function insertAllBody(rows: readonly BqaaRow[]): string {
  return JSON.stringify({
    kind: 'bigquery#tableDataInsertAllRequest',
    skipInvalidRows: true,
    ignoreUnknownValues: true,
    rows: rows.map(row => ({ insertId: row.event_id, json: row })),
  })
}

export function createTableBody(c: Config): string {
  return JSON.stringify({
    tableReference: { projectId: c.project, datasetId: c.dataset, tableId: c.table },
    schema: AGENT_EVENTS_SCHEMA,
    timePartitioning: { type: 'DAY', field: 'timestamp' },
    clustering: { fields: ['event_type', 'agent', 'user_id'] },
    description: 'BigQuery Agent Analytics events written by the bigquery-agent-analytics-mod coding-agent mod.',
  })
}

export type HttpAnswer = { status: number; text: string }

export type InsertOutcome =
  | { kind: 'ok' }
  // Some rows were rejected; `rejected` maps a row index to its reason.
  | { kind: 'partial'; rejected: Map<number, string>; retry: Set<number> }
  // Try again later (network, 408, 429, 5xx, quota).
  | { kind: 'retry'; message: string }
  // The token was refused: refresh it and try again.
  | { kind: 'auth'; message: string }
  // The table (or dataset) does not exist.
  | { kind: 'not-found'; message: string }
  // Needs the person's action (permissions, bad request, billing);
  // `permission` when BigQuery denied the identity access.
  | { kind: 'rejected'; message: string; permission?: true }

const RETRYABLE_REASONS = new Set(['rateLimitExceeded', 'quotaExceeded', 'userRateLimitExceeded', 'backendError', 'internalError', 'timeout', 'stopped'])

type ApiError = { error?: { code?: number; message?: string; status?: string; errors?: { reason?: string; message?: string }[] } }

const parseJson = <T>(text: string): T | null => {
  try {
    return JSON.parse(text) as T
  } catch {
    return null
  }
}

// Error text from an answer, redacted before it is cut: cut first, a
// credential across the cap would leave a piece too short to match.
export function apiErrorMessage(answer: HttpAnswer): string {
  const body = parseJson<ApiError>(answer.text)
  const message = body?.error?.message ?? redactDiagnostic(answer.text, 300)
  return redactDiagnostic(`HTTP ${answer.status}: ${message}`, 1000)
}

// A 2xx answer counts as delivered only when it is an insertAll response: a
// JSON object whose `insertErrors`, if any, each name a row of this batch and
// list its errors. Anything else (a body cut at the response limit, a proxy's
// page) leaves the outcome unknown, so the rows are kept and sent again with
// the same insertIds.
export function classifyInsert(answer: HttpAnswer, rowCount: number): InsertOutcome {
  const { status } = answer
  if (status >= 200 && status < 300) {
    const unknown: InsertOutcome = { kind: 'retry', message: `HTTP ${status} with a response that is not a valid insertAll answer; the rows are kept and sent again` }
    const body = parseJson<unknown>(answer.text)
    if (body === null || typeof body !== 'object' || Array.isArray(body) || 'error' in body) return unknown
    const errors = (body as { insertErrors?: unknown }).insertErrors
    if (errors === undefined) return { kind: 'ok' }
    if (!Array.isArray(errors)) return unknown
    const rejected = new Map<number, string>()
    const retry = new Set<number>()
    for (const entry of errors as unknown[]) {
      const index = (entry as { index?: unknown } | null)?.index
      const reasons = (entry as { errors?: unknown } | null)?.errors
      if (typeof index !== 'number' || !Number.isInteger(index) || index < 0 || index >= rowCount) return unknown
      if (!Array.isArray(reasons) || reasons.some(r => r === null || typeof r !== 'object')) return unknown
      const list = reasons as { reason?: unknown; message?: unknown }[]
      if (list.length > 0 && list.every(r => RETRYABLE_REASONS.has(String(r.reason ?? '')))) retry.add(index)
      else rejected.set(index, redactDiagnostic(list.map(r => `${String(r.reason ?? 'error')}: ${String(r.message ?? '')}`.trim()).join('; '), 1000) || 'rejected')
    }
    if (rejected.size === 0 && retry.size === 0) return { kind: 'ok' }
    return { kind: 'partial', rejected, retry }
  }
  const body = parseJson<ApiError>(answer.text)
  const reason = body?.error?.errors?.[0]?.reason ?? ''
  const message = apiErrorMessage(answer)
  if (status === 401) return { kind: 'auth', message }
  if (status === 404) return { kind: 'not-found', message }
  if (status === 403 && RETRYABLE_REASONS.has(reason)) return { kind: 'retry', message }
  if (status === 408 || status === 429 || status >= 500) return { kind: 'retry', message }
  if (status === 403 && (reason === 'accessDenied' || body?.error?.status === 'PERMISSION_DENIED')) return { kind: 'rejected', message, permission: true }
  if (status === 403 || status === 400) return { kind: 'rejected', message }
  return { kind: 'retry', message }
}

// Whether a 404 names the dataset rather than the table.
export const isDatasetMissing = (message: string): boolean => /not found: dataset/i.test(message)
