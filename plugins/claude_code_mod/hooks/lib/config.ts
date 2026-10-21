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

// Resolves the mod's configuration from its userConfig options (plugin.json)
// and the BQAA_* environment variables the settings-hook tracing plugin
// already uses. Pure: no `$` calls here, so it can be unit tested directly.

export const PLUGIN_NAME = 'bigquery-agent-analytics-mod'
export const PLUGIN_VERSION = '0.1.0'
export const WRITER_LABEL = `${PLUGIN_NAME}/${PLUGIN_VERSION}`

export const DEFAULT_TABLE = 'agent_events'
export const DEFAULT_LOCATION = 'US'
export const DEFAULT_AGENT_NAME = 'coding-agent'
export const DEFAULT_TOKEN_COMMAND = 'gcloud auth application-default print-access-token'

// Option values as `register(on, options)` receives them.
export type Options = Readonly<Record<string, string | number | boolean | readonly string[]>>

// The environment variables the mod reads, each read by literal name in
// register.ts (the mods API requires literal names for `$.env.get`).
export type EnvSnapshot = Partial<Record<EnvName, string>>
export type EnvName =
  | 'BQAA_PROJECT_ID'
  | 'GCP_PROJECT_ID'
  | 'GOOGLE_CLOUD_PROJECT'
  | 'BQAA_DATASET'
  | 'BQ_DATASET'
  | 'BQAA_TABLE'
  | 'BQ_TABLE'
  | 'BQAA_LOCATION'
  | 'BQAA_TRACE_ENABLED'
  | 'BQAA_DRY_RUN'
  | 'BQAA_AGENT_NAME'
  | 'BQAA_USER_ID'
  | 'USER'
  | 'USERNAME'
  | 'HOME'
  | 'USERPROFILE'
  | 'BQAA_DEAD_LETTER_DIR'
  | 'BQAA_ACCESS_TOKEN_COMMAND'
  | 'BQAA_REQUEST_TIMEOUT_MS'
  | 'BQAA_MAX_QUEUE_ROWS'
  | 'BQAA_EXIT_FLUSH_MS'
  | 'BQAA_AUTO_CREATE_TABLE'
  | 'BQAA_AUTO_BACKFILL'

export type Config = {
  // Whether rows are captured at all.
  enabled: boolean
  // Rows go to local JSONL files instead of BigQuery.
  dryRun: boolean
  project: string
  dataset: string
  table: string
  location: string
  agentName: string
  userId: string
  // Cap, in characters, for each JSON payload column (content, attributes).
  maxPayloadChars: number
  flushIntervalMs: number
  // Client-side bound on one insertAll request; the host also caps a
  // `$.http.fetch` call at 30 seconds.
  requestTimeoutMs: number
  maxQueueRows: number
  // Queue and batch bounds in bytes are UTF-8 bytes.
  maxQueueBytes: number
  maxBatchRows: number
  maxBatchBytes: number
  maxRetries: number
  // How long session.end may wait for its spill file to be written.
  exitFlushMs: number
  deadLetterDir: string
  dryRunDir: string
  accessTokenCommand: string[]
  tokenCommandTimeoutMs: number
  tokenTtlMs: number
  autoCreateTable: boolean
  autoBackfill: boolean
  // Opt-in identity: `user_id` from the credential's verified account, and
  // the machine's hostname in `session_metadata.host_name`. Both are
  // personal data, so both are off unless set.
  userIdFromCredentials: boolean
  recordHostname: boolean
  // Why rows will not reach BigQuery, if anything stops them.
  problems: string[]
}

const clampInt = (value: unknown, fallback: number, min: number, max: number): number => {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return fallback
  return Math.min(max, Math.max(min, Math.round(n)))
}

const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

const firstSet = (...values: (string | undefined)[]): string => {
  for (const value of values) {
    const v = (value ?? '').trim()
    if (v !== '') return v
  }
  return ''
}

const isFalse = (value: string | undefined): boolean =>
  value !== undefined && ['false', '0', 'no', 'off'].includes(value.trim().toLowerCase())

const isTrue = (value: string | undefined): boolean =>
  value !== undefined && ['true', '1', 'yes', 'on'].includes(value.trim().toLowerCase())

// Splits a command line into argv: whitespace separated, with '...' and "..."
// quoting. No shell: `$.process.run` takes argv and runs no shell.
export function splitArgv(line: string): string[] {
  const out: string[] = []
  let current = ''
  let quote: '"' | "'" | null = null
  let hasToken = false
  for (const ch of line) {
    if (quote) {
      if (ch === quote) quote = null
      else current += ch
      continue
    }
    if (ch === '"' || ch === "'") {
      quote = ch
      hasToken = true
      continue
    }
    if (/\s/.test(ch)) {
      if (hasToken) out.push(current)
      current = ''
      hasToken = false
      continue
    }
    current += ch
    hasToken = true
  }
  if (hasToken) out.push(current)
  return out
}

export const joinPath = (dir: string, name: string): string =>
  dir.endsWith('/') || dir.endsWith('\\') ? dir + name : `${dir}/${name}`

const parentDir = (dir: string): string => {
  const trimmed = dir.replace(/[\\/]+$/, '')
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  return cut > 0 ? trimmed.slice(0, cut) : trimmed
}

export function resolveConfig(options: Options, env: EnvSnapshot): Config {
  const problems: string[] = []
  const project = firstSet(text(options.project), env.BQAA_PROJECT_ID, env.GCP_PROJECT_ID, env.GOOGLE_CLOUD_PROJECT)
  const dataset = firstSet(text(options.dataset), env.BQAA_DATASET, env.BQ_DATASET)
  const table = firstSet(text(options.table), env.BQAA_TABLE, env.BQ_TABLE, DEFAULT_TABLE)
  const location = firstSet(text(options.location), env.BQAA_LOCATION, DEFAULT_LOCATION)
  const home = firstSet(env.HOME, env.USERPROFILE)
  const deadLetterDir = firstSet(
    text(options.deadLetterDir),
    env.BQAA_DEAD_LETTER_DIR,
    home ? joinPath(home, '.bqaa-mod/dead-letter') : '',
  )
  // Kill switch and dry-run switch, as the tracing plugin spells them.
  const enabled = options.enabled !== false && !isFalse(env.BQAA_TRACE_ENABLED)
  const dryRun = options.dryRun === true || isTrue(env.BQAA_DRY_RUN)

  if (!dryRun && (project === '' || dataset === '')) {
    problems.push('project and dataset are not set (userConfig project/dataset or BQAA_PROJECT_ID/BQAA_DATASET)')
  }
  if (deadLetterDir === '') {
    problems.push('no dead-letter directory: set userConfig deadLetterDir or BQAA_DEAD_LETTER_DIR (HOME is unset)')
  }
  const tokenLine = firstSet(text(options.accessTokenCommand), env.BQAA_ACCESS_TOKEN_COMMAND, DEFAULT_TOKEN_COMMAND)

  return {
    enabled,
    dryRun,
    project,
    dataset,
    table,
    location,
    agentName: firstSet(text(options.agentName), env.BQAA_AGENT_NAME, DEFAULT_AGENT_NAME),
    userId: firstSet(env.BQAA_USER_ID, env.USER, env.USERNAME, 'local-user'),
    maxPayloadChars: clampInt(options.maxPayloadChars, 16384, 1024, 500_000),
    flushIntervalMs: clampInt(options.flushIntervalMs, 5000, 1000, 300_000),
    requestTimeoutMs: clampInt(env.BQAA_REQUEST_TIMEOUT_MS, 10_000, 1000, 30_000),
    maxQueueRows: clampInt(env.BQAA_MAX_QUEUE_ROWS, 5000, 10, 100_000),
    maxQueueBytes: 8 * 1024 * 1024,
    maxBatchRows: 500,
    // `$.http.fetch` refuses a request body over 4 MiB, counted in
    // characters; UTF-8 bytes are never fewer, so this stays well under it.
    maxBatchBytes: 3 * 1024 * 1024,
    maxRetries: 3,
    exitFlushMs: clampInt(env.BQAA_EXIT_FLUSH_MS, 800, 0, 1200),
    deadLetterDir,
    dryRunDir: deadLetterDir ? joinPath(parentDir(deadLetterDir), 'dry-run') : '',
    accessTokenCommand: splitArgv(tokenLine),
    tokenCommandTimeoutMs: 15_000,
    // Google access tokens last about an hour; refresh well before that.
    tokenTtlMs: 45 * 60 * 1000,
    autoCreateTable: !isFalse(env.BQAA_AUTO_CREATE_TABLE),
    autoBackfill: !isFalse(env.BQAA_AUTO_BACKFILL),
    userIdFromCredentials: options.userIdFromCredentials === true,
    recordHostname: options.recordHostname === true,
    problems,
  }
}

// Whether rows can be sent anywhere: BigQuery needs project and dataset;
// a dry run needs only somewhere to write.
export const canDeliver = (config: Config): boolean =>
  config.enabled && (config.dryRun ? config.dryRunDir !== '' : config.project !== '' && config.dataset !== '')

export const tableRef = (config: Config): string => `${config.project}.${config.dataset}.${config.table}`

// The dead-letter subdirectory for a table: its `project.dataset.table`
// reference percent-encoded, so each table has a safe directory name of its
// own on any file system.
export const destinationDir = (table: string): string =>
  encodeURIComponent(table).replace(/[!'()*~]/g, ch => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`)

// Where this configuration's dead-letter files go, and the only place its
// replays read: rows parked for another table are never sent here.
export const deadLetterPartition = (config: Config): string => joinPath(config.deadLetterDir, destinationDir(tableRef(config)))
