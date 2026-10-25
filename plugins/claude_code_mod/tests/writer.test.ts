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

// The background writer: retries, timeouts, auth refresh, table creation,
// dead-letter files, backfill and the end-of-session spill. No network: the
// insertAll answers are scripted.

import { type Engine, expect, test } from 'claude-code/testing'

import { deadLetterPartition, resolveConfig } from '../hooks/lib/config.ts'
import { type DeadLetterLine, encodeLines, packFiles, parseFile } from '../hooks/lib/deadletter.ts'
import { buildRow } from '../hooks/lib/rows.ts'
import { type Io, Writer } from '../hooks/lib/writer.ts'

import {
  COMMAND,
  DEAD_LETTER_DIR,
  DRY_RUN_DIR,
  GREETED,
  HOST_FILE_BYTES,
  OK_INSERT,
  OPTIONS,
  PARTITION_DIR,
  SESSION_ID,
  TABLE_REF,
  type World,
  deadLetterFiles,
  deadLetterRows,
  json,
  setUp,
  startSession,
  utf8Bytes,
} from './helpers.ts'

const UNAVAILABLE = { status: 503, text: '{"error":{"code":503,"message":"The service is currently unavailable.","status":"UNAVAILABLE"}}' }
const TABLE_MISSING = {
  status: 404,
  text: '{"error":{"code":404,"message":"Not found: Table my-gcp-project:agent_analytics.agent_events","errors":[{"reason":"notFound"}],"status":"NOT_FOUND"}}',
}
const DATASET_MISSING = {
  status: 404,
  text: '{"error":{"code":404,"message":"Not found: Dataset my-gcp-project:agent_analytics","errors":[{"reason":"notFound"}],"status":"NOT_FOUND"}}',
}
const DENIED = {
  status: 403,
  text: '{"error":{"code":403,"message":"Access Denied: Table my-gcp-project:agent_analytics.agent_events: Permission bigquery.tables.updateData denied","errors":[{"reason":"accessDenied"}],"status":"PERMISSION_DENIED"}}',
}
const UNAUTHENTICATED = { status: 401, text: '{"error":{"code":401,"message":"Request had invalid authentication credentials.","status":"UNAUTHENTICATED"}}' }

const statusText = async ($: Engine): Promise<string> => (await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })).text ?? ''

// The mod keeps each dead-letter file to 3 MiB of UTF-8.
const FILE_CAP = 3 * 1024 * 1024

const END = { reason: 'prompt_input_exit', sessionId: SESSION_ID, resume: { id: SESSION_ID } } as const

// A row parked by an earlier session, and its dead-letter line.
const parkedRow = (eventId: string, sessionId: string) => ({
  timestamp: '2026-10-13T20:01:02.003Z',
  event_id: eventId,
  event_type: 'STATE_DELTA',
  agent: 'coding-agent',
  session_id: sessionId,
  invocation_id: null,
  user_id: 'dev',
  trace_id: null,
  span_id: 'bbbbbbbbbbbbbbbb',
  parent_span_id: null,
  content: '{"session_end":true}',
  content_parts: [],
  attributes: '{}',
  latency_ms: null,
  status: 'OK',
  error_message: null,
  is_truncated: false,
})
const parkedLine = (table: string, eventId: string, sessionId: string): string =>
  JSON.stringify({ v: 1, kind: 'spill', reason: 'session ended', attempts: 0, failed_at: '2026-10-13T20:01:02.003Z', table, row: parkedRow(eventId, sessionId) })

// A dead-letter file as a writer that finished leaves it: its lines, then
// the completion record a replay checks for.
const parkedFile = (...lines: string[]): string => packFiles(lines)[0] as string

const OTHER_TABLE = 'other-project.private_dataset.agent_events'
const OTHER_OPTIONS = { project: 'other-project', dataset: 'private_dataset' }

// Parked rows for two tables: each table's files in its own subdirectory, a
// line for the other table inside this table's subdirectory (moved there by
// hand, say), and a file in the dead-letter directory itself.
function parkForTwoTables(world: World): { mine: string; theirs: string; mixed: string; loose: string } {
  const files = {
    mine: `${PARTITION_DIR}/spill-20261013T200102003Z-mine-1.jsonl`,
    theirs: `${DEAD_LETTER_DIR}/${OTHER_TABLE}/spill-20261013T200102003Z-theirs-1.jsonl`,
    mixed: `${PARTITION_DIR}/retry-20261013T200102003Z-mixed-1.jsonl`,
    loose: `${DEAD_LETTER_DIR}/spill-20261013T200102003Z-loose-1.jsonl`,
  }
  world.files.set(files.mine, parkedFile(parkedLine(TABLE_REF, 'a'.repeat(32), 'mine-session')))
  world.files.set(files.theirs, parkedFile(parkedLine(OTHER_TABLE, 'b'.repeat(32), 'their-session')))
  world.files.set(files.mixed, parkedFile(parkedLine(OTHER_TABLE, 'c'.repeat(32), 'their-session'), parkedLine(TABLE_REF, 'd'.repeat(32), 'mine-session')))
  world.files.set(files.loose, parkedFile(parkedLine(OTHER_TABLE, 'e'.repeat(32), 'their-session')))
  return files
}

// Credential-shaped failure text, put together at run time. SECRET must never
// reach a sink: dead-letter files, the status line, the debug log, the
// transcript, or a command's output.
const SECRET = ['canary', 'secret', 'x'.repeat(16)].join('-')
const LEAKY = `${['ya29', SECRET].join('.')} password=${SECRET}`

async function everySink($: Engine, world: World, extra: string[] = []): Promise<string[]> {
  return [...world.files.values(), ...world.statusLines.map(l => l ?? ''), ...world.logs, ...world.toasts, await statusText($), ...extra]
}

test('a failed request is retried with the same insertIds, then succeeds', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAVAILABLE, OK_INSERT]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.insertCalls().length).toBe(1)
  expect(world.acceptedRows().length).toBe(0)
  await world.clock.advance(5000)
  const [first, second] = world.insertCalls()
  const ids = (call: typeof first) => (JSON.parse(call?.body ?? '{}') as { rows: { insertId: string }[] }).rows.map(r => r.insertId)
  expect(ids(second)).toEqual(ids(first))
  expect(world.acceptedRows().length).toBe(3)
  expect(deadLetterFiles(world).length).toBe(0)
  expect(await statusText($)).toMatch(/sent 3, dead-lettered 0/)
})

test('with automatic replay off, the status line for rows parked after retries says /bqaa-backfill sends them', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, { BQAA_AUTO_BACKFILL: 'false' })
  world.replies = [UNAVAILABLE]
  await startSession($)
  for (let t = 0; t < 6; t += 1) await world.clock.advance(5000)
  expect(deadLetterRows(world).length).toBe(1)
  expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: 1 row\(s\) parked after retries \(HTTP 503: .*\); \/bqaa-backfill sends them$/)
})

test('rows that exhaust their retries go to a dead-letter file, and are replayed once BigQuery is back', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, OK_INSERT]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  // Four attempts (one plus three retries), then parked on disk.
  expect(world.insertCalls().length).toBe(4)
  const files = deadLetterFiles(world)
  expect(files.length).toBe(1)
  const [path, text] = files[0] as [string, string]
  expect(path.startsWith(`${PARTITION_DIR}/retry-`)).toBe(true)
  // Every line but the last, the file's completion record, holds a row.
  const lines = text
    .trim()
    .split('\n')
    .map(l => JSON.parse(l) as { kind: string; attempts: number; table: string; reason: string; row?: { event_type: string } })
    .filter(l => l.row !== undefined) as { kind: string; attempts: number; table: string; reason: string; row: { event_type: string } }[]
  expect(lines.length).toBe(text.trim().split('\n').length - 1)
  expect(parseFile(text).complete).toBe(true)
  expect(lines.map(l => l.row.event_type)).toEqual(['STATE_DELTA', 'TOOL_STARTING', 'TOOL_COMPLETED'])
  expect(lines[0]).toMatchObject({ kind: 'retry', attempts: 4, table: 'my-gcp-project.agent_analytics.agent_events' })
  expect(lines[0]?.reason ?? '').toMatch(/HTTP 503/)
  expect(world.statusLines.some(l => /^BQAA: 3 row\(s\) parked after retries \(HTTP 503: .*\); they are replayed on their own$/.test(l ?? ''))).toBe(true)

  // BigQuery answers again: new rows go out, then the parked ones follow.
  await $.tool.call({ tool: 'Read', file_path: 'b.ts' })
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  const sent = world.acceptedRows().map(r => r.event_type)
  expect(sent.filter(t => t === 'STATE_DELTA').length).toBe(1)
  expect(sent.filter(t => t === 'TOOL_COMPLETED').length).toBe(2)
  expect(deadLetterFiles(world).length).toBe(0)
  expect(world.files.get(path)).toBe('')
  expect(world.statusLines.at(-1)).toBeUndefined()
})

test('a missing table is created with the BQAA schema, then written', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [TABLE_MISSING, { status: 200, text: '{"kind":"bigquery#table"}' }, OK_INSERT]
  await startSession($)
  await world.clock.advance(5000)
  expect(world.fetches.map(f => f.url.split('/').slice(-2).join('/'))).toEqual(['agent_events/insertAll', 'agent_analytics/tables', 'agent_events/insertAll'])
  const create = JSON.parse(world.fetches[1]?.body ?? '{}') as {
    tableReference: unknown
    schema: { fields: { name: string; type: string; mode?: string }[] }
    timePartitioning: unknown
    clustering: unknown
  }
  expect(create.tableReference).toEqual({ projectId: 'my-gcp-project', datasetId: 'agent_analytics', tableId: 'agent_events' })
  expect(create.schema.fields.map(f => f.name)).toEqual([
    'timestamp',
    'event_id',
    'event_type',
    'agent',
    'session_id',
    'invocation_id',
    'user_id',
    'trace_id',
    'span_id',
    'parent_span_id',
    'content',
    'content_parts',
    'attributes',
    'latency_ms',
    'status',
    'error_message',
    'is_truncated',
  ])
  expect(create.timePartitioning).toEqual({ type: 'DAY', field: 'timestamp' })
  expect(create.clustering).toEqual({ fields: ['event_type', 'agent', 'user_id'] })
  expect(world.acceptedRows().length).toBe(1)
})

// The notice and the replay once it is found: destination.test.ts.
test('a missing dataset is not created: the rows wait as retries, none refused', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await world.clock.advance(5000)
  expect(world.fetches.length).toBe(1)
  expect(deadLetterFiles(world).length).toBe(0)
  const status = await statusText($)
  expect(status).toMatch(/queue: 1 rows/)
  expect(status).toMatch(/\(0 refused\)/)
})

test('a permission error parks rows as refused; /bqaa-backfill sends them once fixed', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DENIED, OK_INSERT]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.insertCalls().length).toBe(1)
  const [file] = deadLetterFiles(world)
  expect(file?.[0].startsWith(`${PARTITION_DIR}/rejected-`)).toBe(true)
  // The status line says what is wrong, and the fix, with the rows waiting.
  expect(world.statusLines.at(-1) ?? '').toBe(
    'BQAA: BigQuery denied the identity access to the dataset. To fix, ask for bigquery.tables.get and bigquery.tables.updateData on the table my-gcp-project.agent_analytics.agent_events (no role that reads rows), then run /bqaa-backfill (3 row(s) waiting)',
  )
  // Refused rows are not replayed automatically.
  await world.clock.advance(60_000)
  expect(world.acceptedRows().length).toBe(0)

  const started = await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  expect(started.text ?? '').toMatch(/backfill started for 1 file/)
  await world.clock.settle()
  expect(world.acceptedRows().map(r => r.event_type)).toEqual(['STATE_DELTA', 'TOOL_STARTING', 'TOOL_COMPLETED'])
  expect(deadLetterFiles(world).length).toBe(0)
  expect(world.logs.some(l => l.includes('bqaa backfill: 1 file(s), 3 row(s) sent, 0 left'))).toBe(true)
  expect(await statusText($)).toMatch(/last backfill \(command, .*\): 1 file\(s\), 3 rows sent, 0 left/)
})

test('the /bqaa-backfill transcript line says why a file was left waiting', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DENIED, OK_INSERT]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(deadLetterFiles(world).length).toBe(1)
  // No mark can be written beside the file: none of it is sent.
  world.refuseWrite = (path, text) => path.includes('/outcome-sending-') && text !== ''
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.settle()
  expect(world.acceptedRows().length).toBe(0)
  expect(world.logs.some(l => /^bqaa backfill: .*; left waiting: could not mark the replay of rejected-\S+ before sending, so none of it is sent/.test(l))).toBe(true)
})

test('a refused token is fetched again', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAUTHENTICATED, OK_INSERT]
  await startSession($)
  await world.clock.advance(5000)
  await world.clock.advance(5000)
  expect(world.commands.length).toBe(2)
  expect(world.acceptedRows().length).toBe(1)
})

test('a failing token command is reported, and rows wait for it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.processReply = { exitCode: 1, stdout: '', stderr: 'ERROR: (gcloud.auth.application-default.print-access-token) Reauthentication failed.\n' }
  await startSession($)
  await world.clock.advance(5000)
  expect(world.fetches.length).toBe(0)
  const status = await statusText($)
  // The command is named by its program only: its arguments may hold secrets.
  expect(status).toMatch(/authentication: token command .gcloud. exited 1: ERROR: \(gcloud/)
  expect(status).toMatch(/queue: 1 rows/)
})

test('BQAA_ACCESS_TOKEN is used as is, with no token command', { options: OPTIONS }, async ($, on) => {
  // Joined at run time, like every credential-shaped test value (helpers.ts).
  const token = ['ya29', 'from-the-environment-0123456789'].join('.')
  const world = setUp(on, { BQAA_ACCESS_TOKEN: token })
  await startSession($)
  await world.clock.advance(5000)
  expect(world.commands.length).toBe(0)
  expect(world.insertCalls()[0]?.headers.authorization).toBe(`Bearer ${token}`)
})

test('rows BigQuery reports as invalid are parked; the rest land', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [
    {
      status: 200,
      text: JSON.stringify({ kind: 'bigquery#tableDataInsertAllResponse', insertErrors: [{ index: 1, errors: [{ reason: 'invalid', message: 'no such field: foo.' }] }] }),
    },
  ]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  const [file] = deadLetterFiles(world)
  // One row, then the file's completion record.
  const [first = '', record = ''] = (file?.[1] ?? '').trim().split('\n')
  const line = JSON.parse(first) as { kind: string; reason: string; row: { event_type: string } }
  expect(JSON.parse(record)).toMatchObject({ complete: true, lines: 1 })
  expect(line.kind).toBe('rejected')
  expect(line.reason).toMatch(/invalid: no such field: foo/)
  expect(line.row.event_type).toBe('TOOL_STARTING')
  expect(await statusText($)).toMatch(/sent 2, dead-lettered 1 \(1 refused\)/)
})

test('session end starts no request: it spills what is queued, and the next session sends it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await world.clock.settle()
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  // A token is in hand and BigQuery answers, yet session end sends nothing.
  await $.session.end(END)
  expect(world.fetches.length).toBe(0)
  const [file] = deadLetterFiles(world)
  expect(file?.[0].startsWith(`${PARTITION_DIR}/spill-`)).toBe(true)
  const spilled = deadLetterRows(world)
  expect(spilled.map(r => r.event_type)).toEqual(['STATE_DELTA', 'TOOL_STARTING', 'TOOL_COMPLETED', 'STATE_DELTA'])
  // The next session replays the file 15 seconds after it starts.
  await startSession($)
  await world.clock.advance(15_000)
  const sent = new Set(world.acceptedRows().map(r => r.event_id))
  expect(spilled.every(r => sent.has(r.event_id))).toBe(true)
  expect(world.files.get(file?.[0] ?? '')).toBe('')
})

test('session end does not wait for a stuck request: its rows are spilled too', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [{ hang: true }]
  await startSession($)
  await world.clock.advance(5000)
  expect(world.insertCalls().length).toBe(1)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  // Returns without the clock moving: nothing waits on the request.
  await $.session.end({ ...END, reason: 'other' })
  // No second request was started behind the stuck one.
  expect(world.insertCalls().length).toBe(1)
  // The stuck batch (it may yet land; event_id dedups) and everything after it.
  expect(deadLetterRows(world).map(r => r.event_type)).toEqual(['STATE_DELTA', 'TOOL_STARTING', 'TOOL_COMPLETED', 'STATE_DELTA'])
  world.releaseHung(200, '{}')
})

test('a spill write that hangs holds session end no longer than its budget', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await world.clock.settle()
  world.hangWrites = true
  let ended = false
  const ending = $.session.end(END).then(() => {
    ended = true
  })
  await world.clock.advance(500)
  expect(ended).toBe(false)
  // BQAA_EXIT_FLUSH_MS defaults to 800 ms.
  await world.clock.advance(400)
  await ending
  expect(ended).toBe(true)
  expect(world.fetches.length).toBe(0)
  world.releaseWrites()
})

// The event types of the rows in this table's files of one kind.
const typesIn = (world: World, kind: 'spill' | 'rejected'): string[] =>
  deadLetterFiles(world)
    .filter(([path, text]) => path.startsWith(`${PARTITION_DIR}/${kind}-`) && text !== '')
    .flatMap(([, text]) => parseFile(text).lines.map(l => l.row.event_type))

// A send whose second row BigQuery refuses, while the file that row goes to
// is still being written when the session ends.
async function refusedWhileEnding($: Engine, on: Parameters<typeof setUp>[0]): Promise<{ world: World; ended: () => boolean; ending: Promise<void> }> {
  const world = setUp(on)
  world.replies = [
    {
      status: 200,
      text: JSON.stringify({ kind: 'bigquery#tableDataInsertAllResponse', insertErrors: [{ index: 1, errors: [{ reason: 'invalid', message: 'no such field: foo.' }] }] }),
    },
  ]
  world.hangWrite = path => path.includes('/rejected-')
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.insertCalls().length).toBe(1)
  let ended = false
  const ending = $.session.end(END).then(() => {
    ended = true
  })
  return { world, ended: () => ended, ending }
}

test('session end waits, inside its budget, for a file of refused rows still being written, and spills its own row apart', { options: OPTIONS }, async ($, on) => {
  const { world, ended, ending } = await refusedWhileEnding($, on)
  await world.clock.advance(500)
  // Its own row is on disk; the refused row is not yet.
  expect([ended(), typesIn(world, 'spill'), typesIn(world, 'rejected')]).toEqual([false, ['STATE_DELTA'], []])
  world.releaseWrites()
  await ending
  expect([typesIn(world, 'spill'), typesIn(world, 'rejected'), world.insertCalls().length]).toEqual([['STATE_DELTA'], ['TOOL_STARTING'], 1])
})

test('a file of refused rows whose write hangs holds session end no longer than its budget', { options: OPTIONS }, async ($, on) => {
  const { world, ended, ending } = await refusedWhileEnding($, on)
  await world.clock.advance(500)
  expect(ended()).toBe(false)
  await world.clock.advance(400)
  await ending
  expect([ended(), typesIn(world, 'spill'), typesIn(world, 'rejected')]).toEqual([true, ['STATE_DELTA'], []])
  world.releaseWrites()
})

test('with BQAA_EXIT_FLUSH_MS=0 session end does not wait for its spill write', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, { BQAA_EXIT_FLUSH_MS: '0' })
  await startSession($)
  await world.clock.settle()
  world.hangWrites = true
  await $.session.end(END)
  expect(world.fetches.length).toBe(0)
  // The write was started all the same.
  world.releaseWrites()
  expect(deadLetterRows(world).map(r => r.event_type)).toEqual(['STATE_DELTA', 'STATE_DELTA'])
})

test('the next session replays spill files on its own', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const path = `${PARTITION_DIR}/spill-20261013T200102003Z-previoussess-1.jsonl`
  world.files.set(path, parkedFile(parkedLine(TABLE_REF, 'a'.repeat(32), 'previous-session'), 'not json'))
  await startSession($)
  await world.clock.advance(15_000)
  expect(world.acceptedRows().some(r => r.event_id === 'a'.repeat(32))).toBe(true)
  // The unreadable line is kept for a person to look at, in a new, complete
  // file; the source is emptied once that file is written.
  expect(world.files.get(path)).toBe('')
  const kept = deadLetterFiles(world)
  expect(kept.map(([, text]) => text.split('\n')[0])).toEqual(['not json'])
  expect(kept.map(([, text]) => parseFile(text).complete)).toEqual([true])
})

test('replay sends only rows recorded for the configured table; other tables keep theirs', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const files = parkForTwoTables(world)
  const before = new Map(world.files)
  await startSession($)
  await world.clock.advance(15_000)
  // Every request went to this table, and none carried another table's row.
  expect(world.insertCalls().length > 0).toBe(true)
  for (const call of world.insertCalls()) expect(call.url).toBe('https://bigquery.googleapis.com/bigquery/v2/projects/my-gcp-project/datasets/agent_analytics/tables/agent_events/insertAll')
  const sent = world.acceptedRows()
  expect(sent.some(r => r.session_id === 'their-session')).toBe(false)
  expect(sent.filter(r => r.session_id === 'mine-session').map(r => r.event_id).sort()).toEqual(['a'.repeat(32), 'd'.repeat(32)])
  // The other table's files are as they were, byte for byte; in the mixed
  // file the line for the other table is all that is left, unchanged.
  expect(world.files.get(files.theirs)).toBe(before.get(files.theirs))
  expect(world.files.get(files.loose)).toBe(before.get(files.loose))
  expect(world.files.get(files.mine)).toBe('')
  // The mixed file's line for the other table moved, as written, to a new file
  // in this table's directory before the mixed file was emptied.
  expect(world.files.get(files.mixed)).toBe('')
  const moved = deadLetterFiles(world).filter(([path]) => path.startsWith(`${PARTITION_DIR}/`))
  expect(moved.map(([, text]) => parseFile(text).raw)).toEqual([[parkedLine(OTHER_TABLE, 'c'.repeat(32), 'their-session')]])
  expect(moved.map(([, text]) => parseFile(text).complete)).toEqual([true])
  const status = await statusText($)
  expect(status).toMatch(/2 rows sent, 1 left, 1 of them for another table/)
  expect(status).toMatch(/2 more file\(s\) in .* for other tables or an older layout/)
})

test('configured for the other table, replay sends its rows there and leaves this table\'s alone', { options: OTHER_OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const files = parkForTwoTables(world)
  const before = new Map(world.files)
  await startSession($)
  await world.clock.advance(15_000)
  expect(world.insertCalls().length > 0).toBe(true)
  for (const call of world.insertCalls()) expect(call.url).toBe('https://bigquery.googleapis.com/bigquery/v2/projects/other-project/datasets/private_dataset/tables/agent_events/insertAll')
  const sent = world.acceptedRows()
  expect(sent.some(r => r.session_id === 'mine-session')).toBe(false)
  expect(sent.filter(r => r.session_id === 'their-session').map(r => r.event_id)).toEqual(['b'.repeat(32)])
  for (const path of [files.mine, files.mixed, files.loose]) expect(world.files.get(path)).toBe(before.get(path))
  expect(world.files.get(files.theirs)).toBe('')
})

test('a file another session is replaying is left to it', { options: OPTIONS }, async ($, on) => {
  const name = 'spill-20261013T200102003Z-othersession-1.jsonl'
  const world = setUp(on, {}, { ...GREETED, [`lease:${TABLE_REF}/${name}`]: { by: 'other-session', until: Date.now() + 60_000 } })
  world.files.set(`${PARTITION_DIR}/${name}`, '{"v":1}\n')
  await startSession($)
  await world.clock.advance(15_000)
  expect(world.files.get(`${PARTITION_DIR}/${name}`)).toBe('{"v":1}\n')
  expect(await statusText($)).toMatch(/1 skipped/)
})

test('multibyte rows: dead-letter files stay within the limit in bytes, and every row is replayed', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // 15,000 three-byte characters per tool result: about 45 KB of UTF-8 a row,
  // 5 MB for the 120 calls, though only 1.8 million characters.
  world.toolReply = () => ({ result: { type: 'text' }, text: '界'.repeat(15_000) })
  await startSession($)
  await world.clock.settle()
  for (let i = 0; i < 120; i += 1) await $.tool.call({ tool: 'Read', file_path: `f${i}.ts` })
  await $.session.end(END)
  const files = deadLetterFiles(world)
  expect(files.length > 1).toBe(true)
  for (const [, text] of files) expect(utf8Bytes(text) <= FILE_CAP).toBe(true)
  expect(deadLetterRows(world).filter(r => r.event_type === 'TOOL_COMPLETED').length).toBe(120)
  // The next session reads every file back and sends every row.
  await startSession($)
  await world.clock.advance(15_000)
  expect(world.acceptedRows().filter(r => r.event_type === 'TOOL_COMPLETED').length).toBe(120)
  for (const call of world.insertCalls()) expect(call.body.length <= HOST_FILE_BYTES).toBe(true)
  expect(deadLetterFiles(world).length).toBe(0)
})

test('a dead-letter file over the 4 MiB read limit is reported, not skipped silently', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const name = 'spill-20261013T200102003Z-oversized-1.jsonl'
  const text = '界'.repeat(1_500_000)
  world.files.set(`${PARTITION_DIR}/${name}`, text)
  await startSession($)
  await world.clock.advance(15_000)
  expect(world.files.get(`${PARTITION_DIR}/${name}`)).toBe(text)
  expect(await statusText($)).toMatch(new RegExp(`1 file\\(s\\) over the 4 MiB read limit, not replayed: ${name.replace(/\./g, '\\.')}`))
})

test('the failing token-command fixture keeps its canaries out of the exercised files and diagnostics', { options: { ...OPTIONS, accessTokenCommand: 'fake-token-tool --profile argcanaryprofile' } }, async ($, on) => {
  const world = setUp(on)
  world.processReply = { exitCode: 1, stdout: '', stderr: `ERROR: refresh failed for ${LEAKY}\n` }
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  // Four attempts, then the rows are parked; the automatic replay at 15 s
  // stops on the same failure, and so does /bqaa-backfill.
  for (let t = 0; t < 6; t += 1) await world.clock.advance(5000)
  const started = await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.settle()
  for (const text of await everySink($, world, [started.text ?? ''])) {
    expect(text.includes(SECRET)).toBe(false)
    expect(text.includes('argcanaryprofile')).toBe(false)
  }
  // The diagnostics did reach those sinks, redacted.
  const parked = deadLetterFiles(world)
  expect(parked.length > 0).toBe(true)
  for (const [, text] of parked) expect(text).toMatch(/"reason":"authentication: token command `fake-token-tool` exited 1: ERROR: refresh failed for \[REDACTED:google-oauth-token\] password=\[REDACTED\]"/)
  // While no credentials work, the status line names the fix, the command by
  // its program only; the toast and the transcript line carry the reason.
  expect(world.statusLines.some(l => (l ?? '').startsWith('BQAA: no working Google credentials. To fix, check the token command `fake-token-tool`'))).toBe(true)
  expect(world.toasts.length).toBe(1)
  expect(world.transcript.some(l => l.includes('exited 1: ERROR: refresh failed for [REDACTED:google-oauth-token] password=[REDACTED]'))).toBe(true)
  expect(await statusText($)).toMatch(/last backfill \(command, .*stopped: authentication: token command .fake-token-tool. exited 1: .*\[REDACTED/)
  expect(world.logs.some(l => l.startsWith('bqaa backfill:') && l.includes('[REDACTED'))).toBe(true)
})

test('the scripted API error fixture keeps its canaries out of the exercised files and diagnostics', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const refusal = { status: 403, text: JSON.stringify({ error: { code: 403, message: `Access Denied: ${LEAKY}`, errors: [{ reason: 'accessDenied' }] } }) }
  const invalid = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'invalid', message: `bad value ${LEAKY}` }] }] }) }
  const busy = { status: 503, text: JSON.stringify({ error: { code: 503, message: `backend busy ${LEAKY}` } }) }
  world.replies = [refusal, invalid, busy]
  await startSession($)
  // Refused: the row is parked with BigQuery's reason, and the status line says so.
  await world.clock.advance(5000)
  // A backfill: insertAll reports the row invalid, so its reason is replaced.
  const first = await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.settle()
  // Another: BigQuery is busy, and the backfill stops on that.
  const second = await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.settle()
  for (const text of await everySink($, world, [first.text ?? '', second.text ?? ''])) expect(text.includes(SECRET)).toBe(false)
  const [file] = deadLetterFiles(world)
  expect(file?.[1] ?? '').toMatch(/"reason":"invalid: bad value \[REDACTED:google-oauth-token\] password=\[REDACTED\]"/)
  expect(world.statusLines.some(l => (l ?? '').startsWith('BQAA: BigQuery denied the identity access to the dataset.'))).toBe(true)
  expect(world.transcript.some(l => l.includes('Access Denied: [REDACTED:google-oauth-token] password=[REDACTED]'))).toBe(true)
  expect(await statusText($)).toMatch(/stopped: HTTP 503: backend busy \[REDACTED:google-oauth-token\]/)
  expect(world.logs.some(l => l.startsWith('bqaa backfill:') && l.includes('stopped: HTTP 503'))).toBe(true)
})

test('under queue pressure while BigQuery is down, rows go to disk instead of being dropped', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, { BQAA_MAX_QUEUE_ROWS: '20' })
  world.replies = [UNAVAILABLE]
  await startSession($)
  await world.clock.advance(5000)
  for (let i = 0; i < 9; i += 1) await $.tool.call({ tool: 'Read', file_path: `f${i}` })
  await world.clock.advance(5000)
  const status = await statusText($)
  expect(status).toMatch(/dropped 0/)
  expect(status).toMatch(/spilled [1-9]/)
  expect(deadLetterFiles(world).some(([p]) => p.startsWith(`${PARTITION_DIR}/spill-`))).toBe(true)
})

test('a dead-letter write that fails is counted as dropped, not thrown', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DENIED]
  world.failWrites = true
  await startSession($)
  await world.clock.advance(5000)
  const status = await statusText($)
  expect(status).toMatch(/dropped 1/)
  expect(status).toMatch(/dead-letter write failed \(1 rows lost\)/)
})

test('refused rows whose dead-letter file cannot be written are said to be lost, and not counted among the rows /bqaa-backfill sends', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [{ status: 400, text: '{"error":{"code":400,"message":"Invalid value for the table schema","errors":[{"reason":"invalid"}],"status":"INVALID_ARGUMENT"}}' }]
  world.failWrites = true
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.statusLines.at(-1)).toBe('BQAA: 3 refused row(s) lost: their dead-letter file could not be written (HTTP 400: Invalid value for the table schema); see /bqaa-status')
  // Writes work again: the next refused rows are the only ones counted.
  world.failWrites = false
  await $.tool.call({ tool: 'Read', file_path: 'b.ts' })
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  expect(world.statusLines.at(-1)).toBe('BQAA: 2 row(s) refused (HTTP 400: Invalid value for the table schema); fix, then /bqaa-backfill')
})

test('session.start fired again clears the status line, and the writer that takes over says the same line again', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [{ status: 400, text: '{"error":{"code":400,"message":"Invalid value for the table schema","errors":[{"reason":"invalid"}],"status":"INVALID_ARGUMENT"}}' }]
  await startSession($)
  await world.clock.advance(5000)
  const said = 'BQAA: 1 row(s) refused (HTTP 400: Invalid value for the table schema); fix, then /bqaa-backfill'
  expect(world.statusLines.at(-1)).toBe(said)
  // The plugin turned off and on: session.start fires again in the same
  // load, and its own row is refused the same way.
  await startSession($)
  expect(world.statusLines.at(-1)).toBeUndefined()
  await world.clock.advance(5000)
  expect(world.statusLines.at(-1)).toBe(said)
})

test('rows out of retries whose dead-letter file cannot be written are said to be lost', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAVAILABLE]
  world.failWrites = true
  await startSession($)
  for (let t = 0; t < 6; t += 1) await world.clock.advance(5000)
  expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: 1 row\(s\) out of retries lost: their dead-letter file could not be written \(HTTP 503: .*\); see \/bqaa-status$/)
})

test('dry run writes the rows locally and sends nothing', { options: { dryRun: true } }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.fetches.length).toBe(0)
  expect(world.commands.length).toBe(0)
  const written = [...world.files.entries()].filter(([p]) => p.startsWith(`${DRY_RUN_DIR}/dry-run-`))
  expect(written.length).toBe(1)
  const rows = (written[0]?.[1] ?? '')
    .trim()
    .split('\n')
    .map(l => JSON.parse(l) as { event_type: string; attributes: string })
  expect(rows.map(r => r.event_type)).toEqual(['STATE_DELTA', 'TOOL_STARTING', 'TOOL_COMPLETED'])
  expect(json(rows[0]?.attributes ?? null)).toMatchObject({ writer: { mode: 'dry_run' } })
})

test('error text is redacted before /bqaa-status shows it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const token = ['ya29', 'a0AfH6SMBexampleexampleexample0123'].join('.')
  world.processReply = { exitCode: 1, stdout: '', stderr: `ERROR: refresh failed for ${token}\n` }
  await startSession($)
  await world.clock.advance(5000)
  const status = await statusText($)
  expect(status).toMatch(/\[REDACTED:google-oauth-token\]/)
  expect(status).not.toMatch(/a0AfH6SMBexample/)
})

test('/bqaa-flush sends right away instead of at the next interval', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  const reply = await $.command.run({ command: 'bqaa-flush', args: '', ...COMMAND })
  expect(reply.text ?? '').toMatch(/flush requested for 3 queued row/)
  await world.clock.settle()
  expect(world.acceptedRows().length).toBe(3)
})

test('/clear keeps the process sending, under the new session id', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.session.end({ reason: 'clear', sessionId: SESSION_ID, resume: { id: SESSION_ID } })
  world.sessionId = 'after-clear-session'
  await $.classic.SessionStart({ source: 'clear', session_id: 'after-clear-session' })
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  // Nothing was spilled: the process did not end.
  expect(deadLetterFiles(world).length).toBe(0)
  const rows = world.acceptedRows()
  expect(rows.map(r => [r.event_type, r.session_id])).toEqual([
    ['STATE_DELTA', SESSION_ID],
    ['STATE_DELTA', SESSION_ID],
    ['STATE_DELTA', 'after-clear-session'],
    ['TOOL_STARTING', 'after-clear-session'],
    ['TOOL_COMPLETED', 'after-clear-session'],
  ])
})

// A GitHub-style token, built at run time: 40 characters a pattern knows.
const CUT_TOKEN = ['gh', 'p_', 'x'.repeat(36)].join('')
// Its first characters, which must not survive a cut either.
const FRAGMENT = CUT_TOKEN.slice(0, 4)

test('the API-error credential fixture crossing the length cap is removed in the exercised sinks', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // The token ends just past the 1,000-character cap on error messages.
  world.replies = [{ status: 403, text: JSON.stringify({ error: { message: `${'e'.repeat(950)} ${CUT_TOKEN}`, errors: [{ reason: 'accessDenied' }] } }) }]
  await startSession($)
  await world.clock.advance(5000)
  expect(deadLetterFiles(world).length).toBe(1)
  for (const text of await everySink($, world)) expect(text.includes(FRAGMENT)).toBe(false)
  expect(deadLetterFiles(world)[0]?.[1] ?? '').toMatch(/"reason":"HTTP 403: e+ \[REDACTED/)
})

test('a credential across the cap on a non-JSON error body is removed whole', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // A proxy's error page: the token sits across the 300 characters kept.
  world.replies = [{ status: 400, text: `<html>${'p'.repeat(280)} ${CUT_TOKEN}</html>` }]
  await startSession($)
  await world.clock.advance(5000)
  expect(deadLetterFiles(world).length).toBe(1)
  for (const text of await everySink($, world)) expect(text.includes(FRAGMENT)).toBe(false)
})

test('a credential across the cap on a row\'s insert error is removed whole, sent or replayed', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // Sent: the token runs past the 1,000-character cap on a row's reason.
  const sent = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'invalid', message: `${'e'.repeat(955)} ${CUT_TOKEN}` }] }] }) }
  // Replayed: the same for a parked row, whose reason is rewritten in its file.
  const replayed = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'invalid', message: `${'e'.repeat(970)} ${CUT_TOKEN}` }] }] }) }
  const path = `${PARTITION_DIR}/retry-20261013T200102003Z-previoussess-1.jsonl`
  world.files.set(path, parkedFile(parkedLine(TABLE_REF, 'a'.repeat(32), 'previous-session')))
  world.replies = [sent, replayed]
  await startSession($)
  await world.clock.advance(15_000)
  expect(deadLetterFiles(world).filter(([, text]) => text.includes('"kind":"rejected"')).length).toBe(2)
  for (const text of await everySink($, world)) expect(text.includes(FRAGMENT)).toBe(false)
})

test('a tool result sent as text keeps the redaction its keys get as structure', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const secrets = { token: 'x'.repeat(12), sessionCookie: 'y'.repeat(12), password: 'alpha beta gamma', nested: { api_key: 'z'.repeat(12), credentials: { user: 'u', pass: 'w'.repeat(12) } } }
  world.toolReply = () => ({ result: secrets, text: JSON.stringify(secrets) })
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'settings.json' })
  await world.clock.advance(5000)
  const [row] = world.sentRows().filter(r => r.event_type === 'TOOL_COMPLETED')
  const content = row?.content ?? ''
  for (const value of ['x'.repeat(12), 'y'.repeat(12), 'alpha', 'beta gamma', 'z'.repeat(12), 'w'.repeat(12)]) expect(content.includes(value)).toBe(false)
  // Still the text the tool returned, with each value under a key that is
  // not listed replaced whole.
  expect((JSON.parse(content) as { result: string }).result).toBe('{"token":"[REDACTED]","sessionCookie":"[REDACTED]","password":"[REDACTED]","nested":"[REDACTED]"}')
})

test('a 2xx answer that is not a valid insertAll response keeps the rows: retried, then parked', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // Cut off mid-object, as a body past the response limit can be.
  world.replies = [{ status: 200, text: '{"insertErrors":[{"index":0,"errors":[{"reason":"invalid"' }]
  await startSession($)
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  // Four attempts with the same insertIds, none counted as sent.
  const calls = world.insertCalls()
  expect(calls.length).toBe(4)
  const ids = (call: (typeof calls)[number]) => (JSON.parse(call.body) as { rows: { insertId: string }[] }).rows.map(r => r.insertId)
  for (const call of calls) expect(ids(call)).toEqual(ids(calls[0] as (typeof calls)[number]))
  const status = await statusText($)
  expect(status).toMatch(/sent 0, dead-lettered 1/)
  expect(deadLetterRows(world).map(r => r.event_type)).toEqual(['STATE_DELTA'])
  expect(deadLetterFiles(world)[0]?.[0].startsWith(`${PARTITION_DIR}/retry-`)).toBe(true)
})

test('a replayed file whose answer is malformed stays as it was', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const path = `${PARTITION_DIR}/spill-20261013T200102003Z-previoussess-1.jsonl`
  const text = parkedFile(parkedLine(TABLE_REF, 'a'.repeat(32), 'previous-session'))
  world.files.set(path, text)
  world.replies = [OK_INSERT, { status: 200, text: '{"insertErrors": {"index": 0}}' }]
  await startSession($)
  await world.clock.advance(15_000)
  expect(world.files.get(path)).toBe(text)
  // The replay 15 s in, or the one the paced retries run after it.
  expect(await statusText($)).toMatch(/last backfill \((auto|recovery), .*\): 1 file\(s\), 0 rows sent, 1 left; stopped: HTTP 200 with a response that is not a valid insertAll answer/)
})

// The round-three probes, end to end: each payload reaches the mod as a tool
// result (its text and its structure), BigQuery refuses the batch, so the row
// goes out once in the request and is then parked on disk. The canary, built
// at run time, must be in neither.
const CANARY = ['canary', 'value', 'q'.repeat(12)].join('-')
const SECRET_NUM = Number(['9876', '54321'].join(''))
const SLACK_HOOK = `https://${['hooks', 'slack', 'com'].join('.')}/${['services', 'T0000', 'B0000', CANARY].join('/')}`
const DISCORD_HOOK = `https://${['discord', 'com'].join('.')}/api/${['webhooks', '1234567890', CANARY].join('/')}`
const OFFICE_HOOK = `https://contoso.${['webhook', 'office', 'com'].join('.')}/webhookb2/${CANARY}`
const REFUSED = { status: 403, text: '{"error":{"message":"permission denied","errors":[{"reason":"accessDenied"}]}}' }

const PROBES: [string, string | Record<string, unknown>, string][] = [
  ['a cookie header with a redaction marker inside it', `Cookie: session=${CANARY}; csrf=[REDACTED]`, CANARY],
  ['a credentials list mixing a marker and a secret', { credentials: ['[REDACTED]', CANARY] }, CANARY],
  ['a password longer than 5,000 characters', { password: `${'x'.repeat(5000)}${CANARY}` }, CANARY],
  ['a header value longer than 5,000 characters', `Authorization: ${'x'.repeat(5000)}${CANARY}`, CANARY],
  ['a password in JSON inside JSON', { text: JSON.stringify({ password: CANARY }) }, CANARY],
  ['a password three encodings deep', { a: JSON.stringify({ b: JSON.stringify({ c: JSON.stringify({ pwd: CANARY }) }) }) }, CANARY],
  ['pass and pwd keys', { pass: CANARY, db: { root_pwd: CANARY, MY_PASS: CANARY } }, CANARY],
  ['pass and pwd assignments in text', `export MY_PWD=${CANARY}\nPASS=${CANARY}\nmode=fast`, CANARY],
  ['a numeric secret', { password: SECRET_NUM, pin: 1 }, String(SECRET_NUM)],
  ['a numeric secret in text', `password: ${SECRET_NUM}`, String(SECRET_NUM)],
  ['webhook URLs', `notify ${SLACK_HOOK} and ${DISCORD_HOOK} or ${OFFICE_HOOK}`, CANARY],
  ['a URL with user-info and a signed parameter', `fetch https://${['user', CANARY].join(':')}@db.example.test/x?sig=${CANARY}&page=2`, CANARY],
]

for (const [label, payload, secret] of PROBES) {
  test(`${label}: neither sent nor parked`, { options: OPTIONS }, async ($, on) => {
    const world = setUp(on)
    const text = typeof payload === 'string' ? payload : JSON.stringify(payload)
    world.toolReply = () => ({ result: payload, text })
    world.replies = [REFUSED]
    await startSession($)
    await $.tool.call({ tool: 'Read', file_path: 'synthetic.txt' })
    await world.clock.advance(5000)
    const sent = world.sentRows().filter(r => r.event_type === 'TOOL_COMPLETED')
    const parked = deadLetterRows(world).filter(r => r.event_type === 'TOOL_COMPLETED')
    expect(sent.length).toBe(1)
    expect(parked.length).toBe(1)
    expect(JSON.stringify(sent).includes(secret)).toBe(false)
    expect(JSON.stringify(parked).includes(secret)).toBe(false)
    expect(sent[0]?.content ?? '').toMatch(/REDACTED/)
  })
}

test('the same probes as tool input: every argument not listed goes whole', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [REFUSED]
  await startSession($)
  await $.tool.call({ tool: 'mcp__deploy__run', api_key: SECRET_NUM, env: { DB_PWD: CANARY, cookies: ['[REDACTED]', CANARY] }, note: JSON.stringify({ token: CANARY }), target: 'prod', description: 'deploy the app' })
  await world.clock.advance(5000)
  const [sent] = world.sentRows().filter(r => r.event_type === 'TOOL_STARTING')
  const [parked] = deadLetterRows(world).filter(r => r.event_type === 'TOOL_STARTING')
  for (const row of [sent, parked]) {
    const text = JSON.stringify(row)
    expect(text.includes(CANARY) || text.includes(String(SECRET_NUM))).toBe(false)
    // A listed key keeps its value; one that is not goes, credential or not.
    expect((JSON.parse(row?.content ?? '{}') as { args: { description: string; target: string } }).args).toMatchObject({ description: 'deploy the app', target: '[REDACTED]' })
  }
})

test('a credential in BigQuery error text goes whole even next to a redaction marker', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [{ status: 403, text: JSON.stringify({ error: { message: `denied for Cookie: session=${CANARY}; csrf=[REDACTED]\npassword: ${'x'.repeat(5000)}${CANARY}`, errors: [{ reason: 'accessDenied' }] } }) }]
  await startSession($)
  await world.clock.advance(5000)
  expect(deadLetterFiles(world).length).toBe(1)
  for (const text of await everySink($, world)) expect(text.includes(CANARY)).toBe(false)
})

// Round four. Formatted JSON documents whose credential sits on a later line
// than its name, too long to parse whole: they must not reach delivery or disk.
const credentialDocuments = {
  object: (padding: number) => JSON.stringify({ credentials: { value: CANARY }, padding: 'x'.repeat(padding) }, null, 2),
  list: (padding: number) => JSON.stringify([{ credentials: { value: CANARY } }, 'x'.repeat(padding)], null, 2),
  'list led by a number': (padding: number) => JSON.stringify([1, { credentials: { value: CANARY } }, 'x'.repeat(padding)], null, 2),
}
const credentialDocument = credentialDocuments.object

for (const [shape, document] of Object.entries(credentialDocuments)) {
  for (const sink of ['sent', 'parked'] as const) {
    test(`a formatted JSON ${shape} past the parse budget is dropped and marked, not scrubbed line by line (${sink})`, { options: OPTIONS }, async ($, on) => {
      const world = setUp(on)
      if (sink === 'parked') world.replies = [REFUSED]
      world.toolReply = () => ({ result: { type: 'text' }, text: document(30_000) })
      await startSession($)
      await $.tool.call({ tool: 'Read', file_path: 'large-config.json' })
      await world.clock.advance(5000)
      const rows = (sink === 'sent' ? world.sentRows() : deadLetterRows(world)).filter(r => r.event_type === 'TOOL_COMPLETED')
      expect(rows.length).toBe(1)
      expect(JSON.stringify(rows).includes(CANARY)).toBe(false)
      expect(rows[0]?.is_truncated).toBe(true)
      expect((JSON.parse(rows[0]?.content ?? '{}') as { result: string }).result).toMatch(/^\[TRUNCATED: \d+ chars of JSON text that could not be parsed and checked\]$/)
    })
  }
}

test('formatted JSON within the budget is still redacted by its structure', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.toolReply = () => ({ result: { type: 'text' }, text: credentialDocument(100) })
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'small-config.json' })
  await world.clock.advance(5000)
  const [row] = world.sentRows().filter(r => r.event_type === 'TOOL_COMPLETED')
  expect(JSON.stringify(row).includes(CANARY)).toBe(false)
  expect(JSON.parse((JSON.parse(row?.content ?? '{}') as { result: string }).result)).toEqual({ credentials: '[REDACTED]', padding: '[REDACTED]' })
})

test('JSON text that does not parse is dropped, not scrubbed line by line', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // Cut off before its end, as a tool's own output limit can leave it.
  world.toolReply = () => ({ result: { type: 'text' }, text: credentialDocument(100).slice(0, -20) })
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'cut-config.json' })
  await world.clock.advance(5000)
  const [row] = world.sentRows().filter(r => r.event_type === 'TOOL_COMPLETED')
  expect(JSON.stringify(row).includes(CANARY)).toBe(false)
  expect(row?.is_truncated).toBe(true)
})

test('after /clear the next rows carry the new session id, with no classic hook to say so', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // No classic.SessionStart: a managed policy can keep classic hooks from a
  // user plugin, and session.start does not fire after /clear.
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await $.session.end({ reason: 'clear', sessionId: SESSION_ID, resume: { id: SESSION_ID } })
  world.sessionId = 'session-after-clear'
  await $.turn.start({ turnId: 'turn-after-clear', text: 'after clear' })
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.sentRows().map(r => [r.event_type, r.session_id])).toEqual([
    ['STATE_DELTA', SESSION_ID],
    ['INVOCATION_STARTING', 'session-after-clear'],
    ['TOOL_STARTING', 'session-after-clear'],
    ['TOOL_COMPLETED', 'session-after-clear'],
  ])
})

test('after a session change, a refused id read leaves rows with no session id, never the old one', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await $.session.end({ reason: 'resume', sessionId: SESSION_ID, resume: { id: SESSION_ID } })
  world.sessionIdRefused = true
  await $.turn.start({ turnId: 'turn-after-resume', text: 'after resume' })
  await world.clock.advance(5000)
  const [started] = world.sentRows().filter(r => r.event_type === 'INVOCATION_STARTING')
  expect(started?.session_id).toBe(null)
})

// A replay whose leftovers outgrow one file (every row refused with a long
// reason), run against a file system that can fail every write but the
// source's own.
async function splitReplay(failNewFiles: boolean) {
  const config = resolveConfig({ project: 'replay-project', dataset: 'events', deadLetterDir: '/replay/dead-letter' }, {})
  const source = `${deadLetterPartition(config)}/spill-20261013T200102003Z-replay-1.jsonl`
  const lines: DeadLetterLine[] = Array.from({ length: 1000 }, (_, i) => ({
    v: 1,
    kind: 'spill',
    reason: 'session ended',
    attempts: 0,
    failed_at: '2026-10-13T20:01:02.003Z',
    table: 'replay-project.events.agent_events',
    row: buildRow({ eventType: 'TOOL_COMPLETED', timestampMs: 0, eventId: String(i).padStart(32, '0'), agent: 'coding-agent', sessionId: 's', invocationId: 't', userId: 'u', traceId: null, spanId: null, parentSpanId: null, content: { result: 'r'.repeat(2200) }, attributes: {}, maxPayloadChars: 16384 }).row,
  }))
  const encoded = encodeLines(lines)
  expect(encoded.length).toBe(1)
  const files = new Map<string, string>([[source, encoded[0] as string]])
  const refusal = (count: number) => JSON.stringify({ insertErrors: Array.from({ length: count }, (_, index) => ({ index, errors: [{ reason: 'invalid', message: 'm'.repeat(900) }] })) })
  const io: Io = {
    now: () => 0,
    after: () => ({ cancel: () => undefined }),
    fetch: async (_url, init) => ({ status: 200, text: refusal((JSON.parse(init.body ?? '{}') as { rows: unknown[] }).rows.length) }),
    runCommand: async () => ({ exitCode: 0, stdout: 'a'.repeat(24), stderr: '' }),
    envToken: async () => undefined,
    readFile: async path => files.get(path) ?? '',
    writeFile: async (path, text) => {
      // The replay's mark and record files beside the source are written.
      if (failNewFiles && path !== source && !path.includes('/outcome-')) throw new Error('disk full')
      files.set(path, text)
    },
    listDir: async () => [{ name: source.split('/').pop() as string, kind: 'file', size: utf8Bytes(encoded[0] as string) }],
    storeGet: async () => undefined,
    storeSet: async () => undefined,
    storeDelete: async () => undefined,
    status: () => undefined,
    debug: () => undefined,
  }
  const writer = new Writer(io, () => config, () => 'replay-session')
  const report = await writer.backfill(['spill'], 'command')
  const ids = [...files.values()].flatMap(text => parseFile(text).lines.map(line => line.row.event_id))
  const incomplete = [...files].filter(([path, text]) => path !== source && !path.includes('/outcome-') && !parseFile(text).complete).map(([path]) => path)
  return { files, source, report, ids, incomplete }
}

test('a replay that splits what is left writes every part before emptying its source', async () => {
  const { files, source, report, ids, incomplete } = await splitReplay(false)
  expect(report.error).toBe(null)
  // Each new file ends with its completion record.
  expect(incomplete).toEqual([])
  // Lengths, not contents: a failure message holding megabytes of rows
  // would not reach the test report.
  expect(files.get(source)?.length).toBe(0)
  // At least two new files, holding every refused row exactly once.
  expect(files.size >= 3).toBe(true)
  expect(ids.length).toBe(1000)
  expect(new Set(ids).size).toBe(1000)
})

test('a write that fails while a replay splits leaves the source whole', async () => {
  const { files, source, report, ids } = await splitReplay(true)
  expect(report.held ?? report.error ?? '').toMatch(/disk full/)
  expect(parseFile(files.get(source) ?? '').lines.length).toBe(1000)
  expect(new Set(ids).size).toBe(1000)
})
