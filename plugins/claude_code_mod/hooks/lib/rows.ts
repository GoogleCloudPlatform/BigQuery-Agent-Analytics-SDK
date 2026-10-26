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

// The BigQuery Agent Analytics row, as the `agent_events` table holds it
// (schema version 2, with `event_id`), and the table definition the mod
// creates when the table is missing.

import { type JsonValue, type Limits, type SanitizeStats, cutAt, limitsFor, newStats, sanitize, toJsonColumn } from './sanitize.ts'

export type ContentPart = {
  mime_type: string | null
  uri: string | null
  object_ref: { uri: string | null; version: string | null; authorizer: string | null; details: string | null } | null
  text: string | null
  part_index: number | null
  part_attributes: string | null
  storage_mode: string | null
}

// One row as tabledata.insertAll takes it: JSON columns as JSON text.
export type BqaaRow = {
  timestamp: string
  event_id: string
  event_type: string
  agent: string
  session_id: string | null
  invocation_id: string | null
  user_id: string | null
  trace_id: string | null
  span_id: string | null
  parent_span_id: string | null
  content: string | null
  content_parts: ContentPart[]
  attributes: string | null
  latency_ms: string | null
  status: 'OK' | 'ERROR'
  error_message: string | null
  is_truncated: boolean
}

export type RowInput = {
  eventType: string
  timestampMs: number
  eventId: string
  agent: string
  sessionId: string | null
  invocationId: string | null
  userId: string | null
  traceId: string | null
  spanId: string | null
  parentSpanId: string | null
  content: unknown
  attributes: Record<string, unknown>
  latencyMs?: Record<string, number | undefined>
  status?: 'OK' | 'ERROR'
  errorMessage?: string | null
  maxPayloadChars: number
}

export type BuiltRow = { row: BqaaRow; redactions: number }

const ERROR_MESSAGE_CHARS = 4096

// Builds one row: payloads copied within `maxPayloadChars`, every value under
// a key that is not on the allowlist redacted (allowlist.ts, sanitize.ts),
// `is_truncated` set when anything was cut. `content` holds what the agent,
// the model and the tools produced under the row's own field names;
// `attributes` is the mod's own metadata.
export function buildRow(input: RowInput): BuiltRow {
  const stats: SanitizeStats = newStats()
  const limits: Limits = limitsFor(input.maxPayloadChars)
  const content = input.content === undefined || input.content === null ? null : sanitize(input.content, limits, stats)
  const attributes = sanitize(input.attributes, limits, stats)
  let latency: string | null = null
  if (input.latencyMs) {
    const clean: Record<string, number> = {}
    for (const [key, value] of Object.entries(input.latencyMs)) {
      if (typeof value === 'number' && Number.isFinite(value)) clean[key] = Math.max(0, Math.round(value))
    }
    if (Object.keys(clean).length > 0) latency = JSON.stringify(clean)
  }
  let errorMessage: string | null = null
  if (input.errorMessage) {
    const errLimits: Limits = { ...limits, budgetChars: ERROR_MESSAGE_CHARS, maxStringChars: ERROR_MESSAGE_CHARS }
    errorMessage = sanitize(input.errorMessage, errLimits, stats) as string
  }
  const row: BqaaRow = {
    timestamp: new Date(input.timestampMs).toISOString(),
    event_id: input.eventId,
    event_type: input.eventType,
    agent: input.agent,
    session_id: input.sessionId,
    invocation_id: input.invocationId,
    user_id: input.userId,
    trace_id: input.traceId,
    span_id: input.spanId,
    parent_span_id: input.parentSpanId,
    content: content === null ? null : toJsonColumn(content, input.maxPayloadChars, stats),
    content_parts: [],
    attributes: toJsonColumn(attributes as JsonValue, input.maxPayloadChars, stats),
    latency_ms: latency,
    status: input.status ?? 'OK',
    error_message: errorMessage,
    is_truncated: false,
  }
  row.is_truncated = stats.truncated
  return { row, redactions: stats.redactions }
}

// Bytes `text` takes as UTF-8, the encoding the host writes files in. A lone
// surrogate counts as the three-byte replacement character it is written as.
export function utf8Length(text: string): number {
  let bytes = 0
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i)
    if (code < 0x80) bytes += 1
    else if (code < 0x800) bytes += 2
    else if (code >= 0xd800 && code <= 0xdbff && i + 1 < text.length) {
      const low = text.charCodeAt(i + 1)
      if (low >= 0xdc00 && low <= 0xdfff) {
        bytes += 4
        i += 1
      } else bytes += 3
    } else bytes += 3
  }
  return bytes
}

// Bytes a row adds to an insertAll body: its JSON as UTF-8 plus the
// `insertId` wrapper. Never fewer than its characters, so a byte budget
// also holds a body to the host's 4 MiB, counted in characters.
export const rowBytes = (row: BqaaRow): number => utf8Length(JSON.stringify(row)) + 64

// A JSON column cut to a `{truncated, original_chars, preview}` object.
const shrinkColumn = (text: string | null | undefined, keep: number): string | null | undefined =>
  typeof text !== 'string' || text.length <= keep ? text : JSON.stringify({ truncated: true, original_chars: text.length, preview: cutAt(text, keep) })

// `row`, or a copy whose JSON fits in `maxBytes` of UTF-8: its payload
// columns cut to valid-JSON prefixes and `is_truncated` set. For a row too
// large for a dead-letter or dry-run file on its own.
export function fitRow(row: BqaaRow, maxBytes: number): BqaaRow {
  if (utf8Length(JSON.stringify(row)) <= maxBytes) return row
  let keep = Math.max(0, ...[row.content, row.attributes, row.error_message].map(v => (typeof v === 'string' ? v.length : 0)))
  for (let i = 0; i < 40 && keep > 0; i += 1) {
    keep = Math.floor(keep / 2)
    const fitted: BqaaRow = {
      ...row,
      content: shrinkColumn(row.content, keep) as string | null,
      attributes: shrinkColumn(row.attributes, keep) as string | null,
      error_message: typeof row.error_message === 'string' ? cutAt(row.error_message, keep) : row.error_message,
      is_truncated: true,
    }
    if (utf8Length(JSON.stringify(fitted)) <= maxBytes) return fitted
  }
  return { ...row, content: null, attributes: null, error_message: null, is_truncated: true }
}

// The BQAA `agent_events` schema (v2) in the REST API's TableSchema form.
export const AGENT_EVENTS_SCHEMA = {
  fields: [
    { name: 'timestamp', type: 'TIMESTAMP', mode: 'REQUIRED', description: 'The UTC time at which the event was logged.' },
    { name: 'event_id', type: 'STRING', description: 'Unique ID assigned before enqueue; also the insertAll insertId.' },
    { name: 'event_type', type: 'STRING', description: "The type of event (e.g. 'LLM_REQUEST', 'TOOL_COMPLETED')." },
    { name: 'agent', type: 'STRING', description: 'The agent (or subagent type) associated with the event.' },
    { name: 'session_id', type: 'STRING', description: 'The coding-agent session id.' },
    { name: 'invocation_id', type: 'STRING', description: 'The turn (invocation) id within the session.' },
    { name: 'user_id', type: 'STRING', description: 'The user associated with the session.' },
    { name: 'trace_id', type: 'STRING', description: '32-char hex trace id, one per main-loop turn.' },
    { name: 'span_id', type: 'STRING', description: '16-char hex span id for this operation.' },
    { name: 'parent_span_id', type: 'STRING', description: '16-char hex span id of the immediate caller.' },
    { name: 'content', type: 'JSON', description: 'The event-specific payload.' },
    {
      name: 'content_parts',
      type: 'RECORD',
      mode: 'REPEATED',
      description: 'Detailed content parts for multi-modal data.',
      fields: [
        { name: 'mime_type', type: 'STRING' },
        { name: 'uri', type: 'STRING' },
        {
          name: 'object_ref',
          type: 'RECORD',
          fields: [
            { name: 'uri', type: 'STRING' },
            { name: 'version', type: 'STRING' },
            { name: 'authorizer', type: 'STRING' },
            { name: 'details', type: 'JSON' },
          ],
        },
        { name: 'text', type: 'STRING' },
        { name: 'part_index', type: 'INTEGER' },
        { name: 'part_attributes', type: 'STRING' },
        { name: 'storage_mode', type: 'STRING' },
      ],
    },
    { name: 'attributes', type: 'JSON', description: 'Metadata: model, token usage, writer, session metadata.' },
    { name: 'latency_ms', type: 'JSON', description: 'Latency measurements (total_ms, time_to_first_token_ms).' },
    { name: 'status', type: 'STRING', description: "'OK' or 'ERROR'." },
    { name: 'error_message', type: 'STRING', description: 'Sanitized error or termination diagnostic.' },
    { name: 'is_truncated', type: 'BOOLEAN', description: 'True when content or metadata was truncated.' },
  ],
} as const
