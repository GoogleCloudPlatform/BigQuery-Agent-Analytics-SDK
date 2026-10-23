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

// Stubs for the parts of the host CLI the mod reaches through `$`, so every
// test runs with no session, no GCP and no network: `$.http.fetch` answers
// from a script the test controls, `$.fs` is a Map held to the host's limits
// (UTF-8 bytes, 4 MiB a file), `$.store` is a Map the test can read,
// `$.process.run` prints a fake token, and `mock.clock` drives the flush
// timer.

import { type Engine, type MockClock, mock } from 'claude-code/testing'
import type { On, TurnStepChunk, TurnStepResult } from 'claude-code'

import { PLUGIN_VERSION } from '../hooks/lib/config.ts'

export type FetchCall = { url: string; method: string; headers: Record<string, string>; body: string; answered: number | 'reject' | 'hang' }
export type FetchReply = { status: number; text: string } | { reject: string } | { hang: true }
export type StepReply = { chunks?: TurnStepChunk[]; result: Partial<TurnStepResult> }
export type ToolReply = { result: unknown; text?: string } | { deny: string } | { isError: true; result: unknown; text?: string }

export type World = {
  clock: MockClock
  fetches: FetchCall[]
  // What `$.http.fetch` answers next; the last entry repeats.
  replies: FetchReply[]
  files: Map<string, string>
  commands: string[][]
  statusLines: (string | undefined)[]
  logs: string[]
  // Of those, the lines meant for the transcript (not the debug log).
  transcript: string[]
  toasts: string[]
  steps: StepReply[]
  // What `$.process.run` (the token command) answers, and after how long
  // on the mock clock.
  processReply: { exitCode: number; stdout: string; stderr: string }
  processDelayMs: number
  // What the `hostname` command prints.
  hostname: string
  // What the token information endpoint answers (with userIdFromCredentials).
  tokenInfo: FetchReply
  // Collector records that reached the stub beneath the mod, as it got them,
  // and whether that stub refuses them.
  records: { event: string; attributes: Record<string, unknown> }[]
  refuseRecords: boolean
  // What `$.session.id()` answers, and whether a policy refuses it.
  sessionId: string
  sessionIdRefused: boolean
  // Makes `$.fs.write` refuse every write, or the writes this picks.
  failWrites: boolean
  // `$.store.keys` refused.
  refuseKeys: boolean
  refuseWrite: ((path: string, text: string) => boolean) | null
  // When files were last written, as the listing says (none: 0).
  mtimes: Map<string, number>
  // Makes `$.fs.write` wait until `releaseWrites` is called: every write,
  // or the writes this picks.
  hangWrites: boolean
  hangWrite: ((path: string, text: string) => boolean) | null
  releaseWrites: () => void
  // Rows from insertAll calls BigQuery answered 200 to, oldest first.
  acceptedRows: () => Row[]
  // How many stubbed model streams ended normally, and how many were closed.
  stepsFinished: number
  stepsClosed: number
  toolReply: (e: { tool: string }) => ToolReply | Promise<ToolReply>
  // Runs inside the prompt.submit stub before it answers, as the engine
  // starts the prompt's turn before `next` resolves.
  promptEntered: ((text: string) => Promise<void>) | null
  // Rows the mod sent with insertAll, oldest first.
  sentRows: () => Row[]
  insertCalls: () => FetchCall[]
  // Lets every `{ hang: true }` request answer now.
  releaseHung: (status: number, text: string) => void
  // The mod's `$.store`, each value read back as JSON, as the host keeps it.
  store: Map<string, unknown>
  // The slash commands the mod registered.
  registered: string[]
}

export type Row = {
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
  content_parts: unknown[]
  attributes: string | null
  latency_ms: string | null
  status: string
  error_message: string | null
  is_truncated: boolean
}

export const SESSION_ID = '5c4b1d2e-8f7a-4c3b-9d1e-2a6f0b7c8d9e'
export const TOKEN_INFO_URL = 'https://oauth2.googleapis.com/tokeninfo'
export const HOME = '/home/dev'
export const DEAD_LETTER_DIR = `${HOME}/.bqaa-mod/dead-letter`
// The table OPTIONS point at, and the dead-letter subdirectory its files go to.
export const TABLE_REF = 'my-gcp-project.agent_analytics.agent_events'
export const PARTITION_DIR = `${DEAD_LETTER_DIR}/${TABLE_REF}`
export const DRY_RUN_DIR = `${HOME}/.bqaa-mod/dry-run`
export const OPTIONS = { project: 'my-gcp-project', dataset: 'agent_analytics' } as const
export const COMMAND = { origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 100 } } as const
export const OK_INSERT = { status: 200, text: '{"kind":"bigquery#tableDataInsertAllResponse"}' }
// The token the stubbed token command prints. Credential-shaped test values
// are joined from pieces at run time, so no file holds a literal that a
// secret scanner would match.
export const ACCESS_TOKEN = ['ya29', 'test-access-token-0123456789'].join('.')

// The host's file limit and how it measures files: UTF-8 bytes. Measured here
// with the standard encoder, independently of the mod's own counting.
export const HOST_FILE_BYTES = 4 * 1024 * 1024
export const utf8Bytes = (text: string): number => new TextEncoder().encode(text).length

export const USAGE = {
  input_tokens: 1203,
  output_tokens: 412,
  cache_read_input_tokens: 18240,
  cache_creation_input_tokens: 960,
  model: 'example-model',
}

// The store once an earlier session greeted this version: the tests start
// from that, but the welcome card's own.
export const GREETED = {
  welcome: { version: PLUGIN_VERSION, previous: null, sessionId: 'an-earlier-session' },
  [`welcome:${PLUGIN_VERSION}`]: { sessionId: 'an-earlier-session', previous: null },
} as const

export function setUp(on: On, env: Record<string, string> = {}, store: Record<string, unknown> = GREETED): World {
  const clock = mock.clock(on, { now: 0 })
  mock.env(on, { HOME, USER: 'dev', ...env })
  const asJson = (value: unknown): unknown => JSON.parse(JSON.stringify(value)) as unknown
  const hung: ((answer: { value: { status: number; ok: boolean; headers: Record<string, string>; text: string } }) => void)[] = []
  const heldWrites: { path: string; text: string; resolve: (answer: { value: undefined }) => void }[] = []
  const world: World = {
    clock,
    fetches: [],
    replies: [OK_INSERT],
    files: new Map(),
    commands: [],
    statusLines: [],
    logs: [],
    transcript: [],
    toasts: [],
    steps: [],
    processReply: { exitCode: 0, stdout: `${ACCESS_TOKEN}\n`, stderr: '' },
    processDelayMs: 0,
    hostname: 'devbox-7',
    tokenInfo: { status: 400, text: '{"error":"invalid_token"}' },
    records: [],
    refuseRecords: false,
    sessionId: SESSION_ID,
    sessionIdRefused: false,
    failWrites: false,
    refuseKeys: false,
    refuseWrite: null,
    mtimes: new Map(),
    hangWrites: false,
    hangWrite: null,
    releaseWrites: () => {
      for (const held of heldWrites.splice(0)) {
        world.files.set(held.path, held.text)
        held.resolve({ value: undefined })
      }
    },
    acceptedRows: () =>
      world
        .insertCalls()
        .filter(f => f.answered === 200)
        .flatMap(f => (JSON.parse(f.body) as { rows: { json: Row }[] }).rows.map(r => r.json)),
    stepsFinished: 0,
    stepsClosed: 0,
    toolReply: () => ({ result: 'ok', text: 'ok' }),
    promptEntered: null,
    insertCalls: () => world.fetches.filter(f => f.url.endsWith('/insertAll')),
    sentRows: () => world.insertCalls().flatMap(f => (JSON.parse(f.body) as { rows: { json: Row }[] }).rows.map(r => r.json)),
    releaseHung: (status, text) => {
      for (const resolve of hung.splice(0)) resolve({ value: { status, ok: status >= 200 && status < 300, headers: {}, text } })
    },
    store: new Map(Object.entries(store).map(([key, value]) => [key, asJson(value)])),
    registered: [],
  }

  on('store.get', ($, e) => ({ value: world.store.get(e.key) }))
  // As the host, a store of no more than 4 MiB of JSON in all.
  on('store.set', ($, e) => {
    const value = asJson(e.value)
    if (utf8Bytes(JSON.stringify(Object.fromEntries(new Map(world.store).set(e.key, value)))) > HOST_FILE_BYTES) return { deny: 'the store would pass 4 MiB of JSON' }
    world.store.set(e.key, value)
    return { value: undefined }
  })
  on('store.delete', ($, e) => {
    world.store.delete(e.key)
    return { value: undefined }
  })
  on('store.keys', () => (world.refuseKeys ? { deny: 'the store could not be listed' } : { value: [...world.store.keys()] }))
  on('session.start', () => ({ cwd: '/work/my-repo' }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('session.id', () => (world.sessionIdRefused ? { deny: 'refused by policy' } : { value: world.sessionId }))
  on('session.version', () => ({ value: { version: '2.1.296', base: '2.1.296' } }))
  on('command.register', ($, e) => {
    world.registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.status', ($, e) => {
    world.statusLines.push(e.text)
    return { value: undefined }
  })
  on('ui.log', ($, e) => {
    world.logs.push(e.text)
    if (e.to !== 'debug') world.transcript.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    world.toasts.push(e.text)
    return { value: undefined }
  })
  // What the mods after this one draw in a site this one draws in too.
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn beneath'] }))
  on('process.run', async ($, e) => {
    world.commands.push([...e.argv])
    const isHostname = e.argv[0] === 'hostname'
    const printed = isHostname ? { exitCode: 0, stdout: `${world.hostname}\n`, stderr: '' } : world.processReply
    if (!isHostname && world.processDelayMs > 0) await clock.sleep(world.processDelayMs)
    return { value: { ...printed, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // The next scripted BigQuery answer; the last one repeats.
  const nextReply = (): FetchReply => (world.replies.length > 1 ? world.replies.shift() : world.replies[0]) as FetchReply
  on('http.fetch', ($, e) => {
    const headers = { ...((e.init?.headers ?? {}) as Record<string, string>) }
    const reply = e.url === TOKEN_INFO_URL ? world.tokenInfo : nextReply()
    const answered = 'reject' in reply ? 'reject' : 'hang' in reply ? 'hang' : reply.status
    world.fetches.push({ url: e.url, method: e.init?.method ?? 'GET', headers, body: String(e.init?.body ?? ''), answered })
    if ('reject' in reply) return { deny: reply.reject }
    if ('hang' in reply) return new Promise(resolve => hung.push(resolve))
    return { value: { status: reply.status, ok: reply.status >= 200 && reply.status < 300, headers: {}, text: reply.text } }
  })
  on('fs.write', ($, e) => {
    if (world.failWrites || world.refuseWrite?.(e.path, e.text)) return { deny: `EACCES: ${e.path}` }
    if (utf8Bytes(e.text) > HOST_FILE_BYTES) return { deny: `${e.path}: over the 4 MiB file limit` }
    if (world.hangWrites || world.hangWrite?.(e.path, e.text)) return new Promise(resolve => heldWrites.push({ path: e.path, text: e.text, resolve }))
    world.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.read', ($, e) => {
    const text = world.files.get(e.path)
    if (text === undefined) return { deny: `ENOENT: ${e.path}` }
    if (utf8Bytes(text) > HOST_FILE_BYTES) return { deny: `${e.path}: over the 4 MiB file limit` }
    return { value: text }
  })
  on('fs.exists', ($, e) => ({ value: [...world.files.keys()].some(p => p === e.path || p.startsWith(`${e.path}/`)) }))
  // Files with their size in bytes, and subdirectories, as the host lists them.
  on('fs.list', ($, e) => {
    const prefix = `${e.path.replace(/\/$/, '')}/`
    const entries = new Map<string, { name: string; kind: 'file' | 'dir'; size: number; mtimeMs: number; isLink: boolean }>()
    for (const [p, text] of world.files) {
      if (!p.startsWith(prefix)) continue
      const rest = p.slice(prefix.length)
      const slash = rest.indexOf('/')
      if (slash === -1) entries.set(rest, { name: rest, kind: 'file', size: utf8Bytes(text), mtimeMs: world.mtimes.get(p) ?? 0, isLink: false })
      else entries.set(rest.slice(0, slash), { name: rest.slice(0, slash), kind: 'dir', size: 0, mtimeMs: 0, isLink: false })
    }
    return { value: [...entries.values()] }
  })
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('prompt.submit', async ($, e) => {
    if (world.promptEntered) await world.promptEntered(e.text)
    return { text: e.text }
  })
  on('telemetry.log', { to: 'collector' }, ($, e) => {
    world.records.push({ event: e.event, attributes: { ...e.attributes } })
    return world.refuseRecords ? { deny: 'not exported' } : { value: undefined }
  })
  on('classic.SessionStart', () => ({}))
  on('classic.SubagentStart', () => ({}))
  on('classic.PermissionRequest', () => ({}))
  on('tool.call', async ($, e) => (await world.toolReply(e as { tool: string })) as never)
  on('turn.step', async function* ($, e) {
    const reply = world.steps.shift() ?? { result: {} }
    let finished = false
    try {
      for (const chunk of reply.chunks ?? []) yield chunk
      const result: TurnStepResult = {
        turnId: e.turnId,
        index: e.index,
        answer: '',
        toolUses: [],
        stopReason: 'end_turn',
        usage: null,
        ...reply.result,
      }
      finished = true
      world.stepsFinished += 1
      return result
    } finally {
      if (!finished) world.stepsClosed += 1
    }
  })
  return world
}

// Reads a turn.step stream to its end, as the engine does, and returns the
// result the plugins passed up.
export async function runStep($: Engine, e: { turnId: string; index: number; model?: string; messageCount?: number; agentId?: string }): Promise<{ chunks: unknown[]; result: TurnStepResult }> {
  const stream = $.turn.step({ model: 'example-model', messageCount: 1, ...e })
  const chunks: unknown[] = []
  let step = await stream.next()
  while (step.done !== true) {
    chunks.push(step.value)
    step = await stream.next()
  }
  return { chunks, result: step.value }
}

// In the order the host raises them: the settings-hook SessionStart comes
// before session.start has read the environment and the host version.
export async function startSession($: Engine): Promise<void> {
  await $.classic.SessionStart({ source: 'startup', session_id: SESSION_ID, model: 'example-model', cwd: '/work/my-repo' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
}

export const json = (text: string | null): Record<string, unknown> => (text === null ? {} : (JSON.parse(text) as Record<string, unknown>))

export const byType = (rows: Row[], type: string): Row[] => rows.filter(r => r.event_type === type)

// The dead-letter files that hold something (not the replay records kept
// beside them, `outcome-…`).
export function deadLetterFiles(world: World): [string, string][] {
  return [...world.files.entries()].filter(([p, text]) => p.startsWith(`${DEAD_LETTER_DIR}/`) && text.length > 0 && /^(spill|retry|rejected)-/.test(p.split('/').pop() ?? ''))
}

// The rows parked in dead-letter files, file by file in name (time) order.
// A file's last line is its completion record, which holds no row.
export function deadLetterRows(world: World): Row[] {
  return deadLetterFiles(world)
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .flatMap(([, text]) =>
      text
        .trim()
        .split('\n')
        .map(l => (JSON.parse(l) as { row?: Row }).row)
        .filter((row): row is Row => row !== undefined),
    )
}
