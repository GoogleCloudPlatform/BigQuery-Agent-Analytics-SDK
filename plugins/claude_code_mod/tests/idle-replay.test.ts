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

// The replay that runs by itself at the paced retries while nothing is
// queued (a session left idle). It goes on until every row waiting for it
// has landed, a replay that lands only some included, and it starts for rows
// parked while a table the mod just made was not visible yet. A file another
// session holds, or one that cannot be read, listed or rewritten, keeps its
// rows due. Rows BigQuery refuses on it move to `rejected` files, as a send's
// refused rows do, and wait for /bqaa-backfill: no replay that runs by itself
// sends them again, and no request or file follows for them.

import { type Engine, expect, test } from 'claude-code/testing'

import { resolveConfig } from '../hooks/lib/config.ts'
import { parseFile, packFiles, sealFile } from '../hooks/lib/deadletter.ts'
import { buildRow } from '../hooks/lib/rows.ts'
import { type Io, Writer } from '../hooks/lib/writer.ts'

import { COMMAND, OK_INSERT, OPTIONS, PARTITION_DIR, TABLE_REF, type World, deadLetterRows, setUp, startSession } from './helpers.ts'

const PLUGIN = 'bigquery-agent-analytics-mod'

const BAND = {
  component: 'AbovePrompt',
  requestId: 'above-prompt',
  viewport: { columns: 120, rows: 40 },
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 115, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const

const DATASET_MISSING = {
  status: 404,
  text: '{"error":{"code":404,"message":"Not found: Dataset my-gcp-project:agent_analytics","errors":[{"reason":"notFound"}],"status":"NOT_FOUND"}}',
}
const TABLE_MISSING = {
  status: 404,
  text: '{"error":{"code":404,"message":"Not found: Table my-gcp-project:agent_analytics.agent_events","errors":[{"reason":"notFound"}],"status":"NOT_FOUND"}}',
}
const TABLE_CREATED = { status: 200, text: '{"kind":"bigquery#table"}' }
// insertAll took every row of the request but the first, which it asks for
// again; and insertAll refused the first row.
const FIRST_AGAIN = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'backendError', message: 'try again' }] }] }) }
const FIRST_REFUSED = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'invalid', message: 'no such field: extra' }] }] }) }
const DENIED = {
  status: 403,
  text: '{"error":{"code":403,"message":"Access Denied: Table my-gcp-project:agent_analytics.agent_events: Permission bigquery.tables.updateData denied","errors":[{"reason":"accessDenied"}],"status":"PERMISSION_DENIED"}}',
}
const INVALID = { status: 400, text: '{"error":{"code":400,"message":"Invalid value for the table schema","errors":[{"reason":"invalid"}],"status":"INVALID_ARGUMENT"}}' }
const UNAUTHENTICATED = { status: 401, text: '{"error":{"code":401,"message":"Request had invalid authentication credentials.","status":"UNAUTHENTICATED"}}' }

const statusText = async ($: Engine): Promise<string> => (await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })).text ?? ''

async function band($: Engine): Promise<{ line?: string; waiting?: string }> {
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  const line = (await ui.find({ type: 'Text', text: /^rows are not being written/ }))?.text
  const waiting = (await ui.find({ type: 'Text', text: /row\(s\) waiting/ }))?.text
  await ui.unmount()
  return { line, waiting }
}

// The event ids parked in this table's files of one kind.
const parkedIn = (world: World, kind: 'spill' | 'retry' | 'rejected'): string[] =>
  [...world.files]
    .filter(([path, text]) => path.startsWith(`${PARTITION_DIR}/${kind}-`) && text.length > 0)
    .flatMap(([, text]) => parseFile(text).lines.map(line => line.row.event_id))

const idsOf = (body: string): string[] => (JSON.parse(body) as { rows: { json: { event_id: string } }[] }).rows.map(r => r.json.event_id)

// Until no row is queued and the rows are parked: a session left idle.
async function leaveIdle($: Engine, world: World): Promise<void> {
  for (let t = 0; t < 30 && !(/queue: 0 rows/.test(await statusText($)) && deadLetterRows(world).length > 0); t += 1) await world.clock.advance(5000)
}

async function idleFor(world: World, minutes: number): Promise<void> {
  for (let t = 0; t < minutes * 12; t += 1) await world.clock.advance(5000)
}

test('/bqaa-backfill leaves a file another session\'s replay holds and says so in its line; /bqaa-backfill force replays it afresh', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await world.clock.advance(5000)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `${PARTITION_DIR}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry')])[0] as string
  world.files.set(source, text)
  // Another session's mark, its outcome recorded nowhere.
  world.files.set(`${PARTITION_DIR}/outcome-sending-anotherwriter-${name}`, sealFile(`${JSON.stringify({ size: new TextEncoder().encode(text).length, sending: true, by: 'anotherwriter' })}\n`))
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.settle()
  expect([world.sentRows().filter(r => r.event_id === a).length, world.files.get(source)]).toEqual([0, text])
  expect(world.logs.some(l => /^bqaa backfill: .*; left waiting: a replay of retry-20261013T200102003Z-earlier-1\.jsonl sent rows and has recorded no outcome; if that session stopped, \/bqaa-backfill force replays the file afresh/.test(l))).toBe(true)
  await $.command.run({ command: 'bqaa-backfill', args: 'force', ...COMMAND })
  await world.clock.settle()
  expect([world.sentRows().filter(r => r.event_id === a).length, world.files.get(source)]).toEqual([1, ''])
  expect(await statusText($)).toMatch(/last backfill \(force, .*\): 1 file\(s\), 1 rows sent, 0 left/)
})

test('a retry file listed empty, before its write put anything in it, is replayed by the idle session once whole; one left empty long ago keeps nothing going', { options: OPTIONS }, async ($, on) => {
  let lists = 0
  on('fs.list', { path: PARTITION_DIR }, ($, e, next) => {
    lists += 1
    return next(e)
  })
  const world = setUp(on)
  await startSession($)
  await world.clock.advance(5000)
  const old = `${PARTITION_DIR}/retry-20261013T200102002Z-earlier-0.jsonl`
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  const whole = packFiles([line(a, 'retry')])[0] as string
  // One left empty long ago, as a replay leaves a file, and one just made,
  // its write still to put anything in it, at the replay after session.start.
  world.files.set(old, '')
  world.mtimes.set(old, Date.now() - 11 * 60 * 1000)
  world.files.set(source, '')
  world.mtimes.set(source, Date.now())
  await world.clock.advance(15_000)
  expect(world.sentRows().filter(r => r.event_id === a).length).toBe(0)
  world.files.set(source, whole)
  await idleFor(world, 10)
  expect([world.sentRows().filter(r => r.event_id === a).length, world.files.get(source)]).toEqual([1, ''])
  // The session starts again: its replay lists the file, emptied by its
  // replay with its done mark beside it, and the one left empty long ago:
  // neither keeps the table due.
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await world.clock.advance(15_000)
  const before = lists
  await idleFor(world, 10)
  expect(lists - before).toBe(0)
})

test('a listing that gives 0 for a file\'s time gives no time: an empty file is watched from when the writer first saw it, and sent once whole', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await world.clock.advance(5000)
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  // No time set: the listing says 0.
  world.files.set(source, '')
  await world.clock.advance(15_000)
  world.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  await idleFor(world, 10)
  expect([world.sentRows().filter(r => r.event_id === a).length, world.files.get(source)]).toEqual([1, ''])
})

test('a retry file listed with its rows and read empty, no done mark beside it, keeps its table due: the idle session replays it once it is whole again', { options: OPTIONS }, async ($, on) => {
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  // A write that starts the file over is under way at the first read.
  let emptyReads = 1
  on('fs.read', { path: source }, ($, e, next) => (emptyReads-- > 0 ? { value: '' } : next(e)))
  const world = setUp(on)
  await startSession($)
  await world.clock.advance(5000)
  const a = 'a'.repeat(32)
  world.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  await idleFor(world, 10)
  expect([emptyReads < 0, world.sentRows().filter(r => r.event_id === a).length, world.files.get(source)]).toEqual([true, 1, ''])
})

test('where the listing gives no times, an empty file a write may still fill is watched for a lease\'s time from when the writer first saw it: sent once whole, and then nothing keeps the table due', async () => {
  const config = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  const dir = `/dl/${TABLE_REF}`
  const source = `${dir}/retry-20261019T000000000Z-producer-1.jsonl`
  const stuck = `${dir}/retry-20261019T000000000Z-producer-2.jsonl`
  const partial = `${dir}/retry-20261019T000000000Z-producer-3.jsonl`
  const whole = packFiles([line('f'.repeat(32), 'retry')])[0] as string
  const files = new Map<string, string>([
    [source, ''],
    [stuck, ''],
    [partial, whole.slice(0, whole.indexOf('\n') + 1)],
  ])
  const store = new Map<string, unknown>()
  const sent: string[] = []
  const timers: { at: number; fn: () => void; live: boolean }[] = []
  // A day into the clock: no time read as 0 passes for recent.
  let now = 24 * 60 * 60 * 1000
  let lists = 0
  const io: Io = {
    now: () => now,
    after: (ms, fn) => {
      const timer = { at: now + ms, fn, live: true }
      timers.push(timer)
      return { cancel: () => void (timer.live = false) }
    },
    fetch: async (url, init) => {
      sent.push(...idsOf(init.body ?? '{"rows":[]}'))
      return OK_INSERT
    },
    runCommand: async () => ({ exitCode: 0, stdout: `${'a'.repeat(24)}\n`, stderr: '' }),
    envToken: async () => undefined,
    writeFile: async (path, text) => void files.set(path, text),
    readFile: async path => files.get(path) ?? '',
    exists: async path => files.has(path),
    listDir: async d => {
      lists += 1
      return [...files].filter(([path]) => path.startsWith(`${d}/`)).map(([path, text]) => ({ name: path.slice(d.length + 1), kind: 'file', size: new TextEncoder().encode(text).length }))
    },
    storeGet: async key => store.get(key),
    storeSet: async (key, value) => void store.set(key, JSON.parse(JSON.stringify(value)) as unknown),
    storeDelete: async key => void store.delete(key),
    status: () => undefined,
    debug: () => undefined,
  }
  const writer = new Writer(io, () => config, () => 'idle-session')
  const idle = async (minutes: number): Promise<void> => {
    for (let i = 0; i < minutes * 12; i += 1) {
      now += 5000
      for (const timer of timers) {
        if (timer.live && timer.at <= now) {
          timer.live = false
          timer.fn()
        }
      }
      await writer.flush()
    }
  }
  const first = await writer.backfill(['spill', 'retry'], 'auto')
  expect(first.held ?? '').toMatch(/is empty, perhaps still being written; it waits for the next replay/)
  expect([first.skippedFiles, first.incompleteFiles]).toEqual([2, ['retry-20261019T000000000Z-producer-3.jsonl']])
  // The first file's write lands; the second's, and the third's end, never do.
  const a = 'a'.repeat(32)
  files.set(source, packFiles([line(a, 'retry')])[0] as string)
  await idle(5)
  expect([sent.filter(id => id === a).length, files.get(source)]).toEqual([1, ''])
  // A lease's time after the writer first saw them, the empty one and the
  // part-written one keep the table due no longer.
  await idle(10)
  const before = lists
  await idle(10)
  expect(lists - before).toBe(0)
})

test('a retry file read before its completion record was written is replayed by the idle session once it is whole, and one never finished makes no request or file', { options: OPTIONS }, async ($, on) => {
  let lists = 0
  on('fs.list', { path: PARTITION_DIR }, ($, e, next) => {
    lists += 1
    return next(e)
  })
  const world = setUp(on)
  await startSession($)
  await world.clock.advance(5000)
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  const whole = packFiles([line(a, 'retry')])[0] as string
  // Its first line written, not its completion record, at the replay after
  // session.start: nothing is sent.
  world.files.set(source, whole.slice(0, whole.indexOf('\n') + 1))
  await world.clock.advance(15_000)
  expect(world.sentRows().filter(r => r.event_id === a).length).toBe(0)
  // Never finished: the paced replays read it again, with no request and no file.
  const requests = world.insertCalls().length
  const files = world.files.size
  const before = lists
  await idleFor(world, 10)
  expect([world.insertCalls().length, world.files.size]).toEqual([requests, files])
  // Paced: a replay lists the directory, and the file's record check lists
  // it twice more where it finds none, about a dozen replays in ten minutes.
  expect(lists - before > 2 && lists - before < 60).toBe(true)
  // Finished now: the idle session sends it once, and empties it.
  world.files.set(source, whole)
  await idleFor(world, 10)
  expect([world.sentRows().filter(r => r.event_id === a).length, world.files.get(source)]).toEqual([1, ''])
})

test('an idle replay that lands only some rows goes on by itself until the rest land, rewriting no file for a row asked for again', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  expect(parked.length).toBe(3)
  // The dataset is made. BigQuery takes two of the rows, asks for the first
  // again, and goes on asking for it.
  world.replies = [FIRST_AGAIN]
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  expect(parkedIn(world, 'retry')).toEqual([parked[0]])
  // BigQuery found the dataset: no notice, and the row still waits.
  expect((await band($)).line).toBeUndefined()
  const requests = world.insertCalls().length
  const files = world.files.size
  await idleFor(world, 1)
  expect(world.insertCalls().length > requests).toBe(true)
  expect(world.files.size).toBe(files)
  world.replies = [OK_INSERT]
  await idleFor(world, 3)
  expect(deadLetterRows(world).length).toBe(0)
  expect(idsOf(world.insertCalls().at(-1)?.body ?? '{"rows":[]}')).toEqual([parked[0]])
})

test('a row BigQuery refuses on an idle replay moves to a rejected file: no replay by itself sends it again, and no request or file follows', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  expect(parked.length).toBe(1)
  world.replies = [FIRST_REFUSED]
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  expect(parkedIn(world, 'retry')).toEqual([])
  expect(parkedIn(world, 'rejected')).toEqual(parked)
  const requests = world.insertCalls().length
  const files = world.files.size
  await idleFor(world, 10)
  expect(world.insertCalls().length).toBe(requests)
  expect(world.files.size).toBe(files)
  // BigQuery found the dataset: no notice; the refusal is on the status line
  // and in /bqaa-status, as a send's is.
  expect((await band($)).line).toBeUndefined()
  expect(world.statusLines.at(-1)).toBe('BQAA: 1 row(s) refused (invalid: no such field: extra); fix, then /bqaa-backfill')
  expect(await statusText($)).toMatch(/last backfill \(recovery, [^)]*\): 1 file\(s\), 0 rows sent, 1 left, 1 of them refused/)
  // Once the cause is fixed, /bqaa-backfill sends it.
  world.replies = [OK_INSERT]
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.advance(1000)
  expect(idsOf(world.insertCalls().at(-1)?.body ?? '{"rows":[]}')).toEqual(parked)
  expect(deadLetterRows(world).length).toBe(0)
})

test('a request BigQuery refuses whole on an idle replay parks its rows for /bqaa-backfill; access denied is the notice', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  expect(parked.length).toBe(3)
  world.replies = [DENIED]
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  expect(parkedIn(world, 'retry')).toEqual([])
  expect(parkedIn(world, 'rejected').sort()).toEqual([...parked].sort())
  const shown = await band($)
  expect(shown.line).toBe('rows are not being written: BigQuery denied the identity access to the dataset. To fix, ask for roles/bigquery.dataEditor on my-gcp-project.agent_analytics, then run /bqaa-backfill.')
  expect(shown.waiting).toBe(' 3 row(s) waiting.')
  const requests = world.insertCalls().length
  const files = world.files.size
  await idleFor(world, 10)
  expect(world.insertCalls().length).toBe(requests)
  expect(world.files.size).toBe(files)
})

test('a request BigQuery refuses whole as invalid on an idle replay parks its rows for /bqaa-backfill, and nothing follows', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  world.replies = [INVALID]
  for (let t = 0; t < 4; t += 1) await world.clock.advance(5000)
  expect(parkedIn(world, 'rejected')).toEqual(parked)
  expect(world.statusLines.at(-1) ?? '').toMatch(/1 row\(s\) refused \(HTTP 400: Invalid value for the table schema\); fix, then \/bqaa-backfill/)
  const requests = world.insertCalls().length
  await idleFor(world, 10)
  expect(world.insertCalls().length).toBe(requests)
})

test('rows parked while a table the mod just made is not visible yet go out once it is, with nothing new queued', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [TABLE_MISSING, TABLE_CREATED, TABLE_MISSING]
  await startSession($)
  await idleFor(world, 2)
  // Made once; the row's retries ran out while the table was new.
  expect(world.fetches.filter(f => f.url.endsWith('/tables')).length).toBe(1)
  const parked = parkedIn(world, 'retry')
  expect(parked.length).toBe(1)
  world.replies = [OK_INSERT]
  await idleFor(world, 3)
  expect(deadLetterRows(world).length).toBe(0)
  expect(idsOf(world.insertCalls().at(-1)?.body ?? '{"rows":[]}')).toEqual(parked)
})

// A dead-letter line for this table, of `kind`.
const line = (eventId: string, kind: 'spill' | 'retry' | 'rejected'): string =>
  JSON.stringify({
    v: 1,
    kind,
    reason: kind === 'rejected' ? 'invalid: no such field: extra' : 'retries ran out',
    attempts: 4,
    failed_at: '2026-10-13T20:01:02.003Z',
    table: TABLE_REF,
    row: buildRow({ eventType: 'STATE_DELTA', timestampMs: 0, eventId, agent: 'coding-agent', sessionId: 'earlier', invocationId: null, userId: 'dev', traceId: null, spanId: null, parentSpanId: null, content: {}, attributes: {}, maxPayloadChars: 16384 }).row,
  })

test('a refused row in a retry file, as earlier versions rewrote them, is never sent by a replay that runs by itself; /bqaa-backfill sends it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const refused = 'a'.repeat(32)
  const waiting = 'b'.repeat(32)
  world.files.set(`${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`, packFiles([line(refused, 'rejected'), line(waiting, 'retry')])[0] as string)
  await startSession($)
  // The replay 15 s after the start sends the waiting row; the refused one
  // stays, now in a rejected file.
  await world.clock.advance(20_000)
  const sent = () => world.sentRows().map(r => r.event_id)
  expect(sent()).toContain(waiting)
  expect(sent()).not.toContain(refused)
  expect(parkedIn(world, 'rejected')).toEqual([refused])
  expect(parkedIn(world, 'retry')).toEqual([])
  await idleFor(world, 10)
  expect(sent()).not.toContain(refused)
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.advance(1000)
  expect(sent()).toContain(refused)
  expect(deadLetterRows(world).length).toBe(0)
})

test('rows parked for one table stay due while another table is configured, and go out once theirs is again', async () => {
  // A writer built by hand, whose configuration the test changes in place.
  let config = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  let now = 0
  let answer = DATASET_MISSING
  const timers: { at: number; fn: () => void; live: boolean }[] = []
  const files = new Map<string, string>()
  const urls: string[] = []
  const io: Io = {
    now: () => now,
    after: (ms, fn) => {
      const timer = { at: now + ms, fn, live: true }
      timers.push(timer)
      return { cancel: () => (timer.live = false) }
    },
    fetch: async url => {
      urls.push(url)
      return answer
    },
    runCommand: async () => ({ exitCode: 0, stdout: `${'a'.repeat(24)}\n`, stderr: '' }),
    envToken: async () => undefined,
    writeFile: async (path, text) => {
      files.set(path, text)
    },
    readFile: async path => files.get(path) ?? '',
    listDir: async dir => [...files].filter(([path]) => path.startsWith(`${dir}/`)).map(([path, text]) => ({ name: path.slice(dir.length + 1), kind: 'file', size: text.length })),
    storeGet: async () => undefined,
    storeSet: async () => undefined,
    storeDelete: async () => undefined,
    status: () => undefined,
    debug: () => undefined,
  }
  const writer = new Writer(io, () => config, () => 'hand-built-session')
  const tick = async (): Promise<void> => {
    now += 5000
    for (const timer of timers.filter(t => t.live && t.at <= now).sort((a, b) => a.at - b.at)) {
      timer.live = false
      timer.fn()
    }
    await writer.flush()
  }
  const waiting = (): number => [...files.values()].reduce((rows, text) => rows + parseFile(text).lines.length, 0)
  writer.enqueue(buildRow({ eventType: 'STATE_DELTA', timestampMs: 0, eventId: 'c'.repeat(32), agent: 'coding-agent', sessionId: 'hand-built-session', invocationId: null, userId: 'dev', traceId: null, spanId: null, parentSpanId: null, content: {}, attributes: {}, maxPayloadChars: 16384 }).row, 0)
  for (let t = 0; t < 12 && writer.queue.length > 0; t += 1) await tick()
  expect(waiting()).toBe(1)
  // Another table, where nothing is parked: its empty replays leave the row due.
  config = resolveConfig({ project: 'my-gcp-project', dataset: 'another_dataset', deadLetterDir: '/dl' }, {})
  answer = OK_INSERT
  for (let t = 0; t < 24; t += 1) await tick()
  expect(urls.some(u => u.includes('another_dataset'))).toBe(false)
  expect(waiting()).toBe(1)
  config = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  for (let t = 0; t < 24; t += 1) await tick()
  expect(waiting()).toBe(0)
})

test('a file another session holds while an idle replay runs stays due: once that lease ends, an idle replay here sends its rows', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  expect(parked.length).toBe(1)
  const name = ([...world.files.keys()].find(path => path.startsWith(`${PARTITION_DIR}/retry-`)) ?? '').split('/').pop()
  // Another session is replaying the file, and holds its lease.
  const lease = `lease:${TABLE_REF}/${name}`
  world.store.set(lease, { by: 'another-session', until: Date.now() + 600_000 })
  world.replies = [OK_INSERT]
  const requests = world.insertCalls().length
  await idleFor(world, 2)
  expect(world.insertCalls().length).toBe(requests)
  expect(parkedIn(world, 'retry')).toEqual(parked)
  // That session stopped part-way, and its lease ran out (ended here: the
  // test clock leaves Date.now alone).
  world.store.set(lease, { by: 'another-session', until: 0 })
  await idleFor(world, 2)
  expect(deadLetterRows(world).length).toBe(0)
  expect(idsOf(world.insertCalls().at(-1)?.body ?? '{"rows":[]}')).toEqual(parked)
})

test('a replay that cannot empty its file once the rows land sends nothing again; once writes work, a replay empties it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  expect(parked.length).toBe(1)
  world.replies = [OK_INSERT]
  world.refuseWrite = (path, text) => text === '' && path.includes('/retry-')
  await idleFor(world, 2)
  // Sent once; the file could not be emptied, and nothing goes again.
  const landed = () => world.insertCalls().filter(f => f.answered === 200).length
  expect(landed()).toBe(1)
  expect(parkedIn(world, 'retry')).toEqual(parked)
  expect(await statusText($)).toMatch(/last error \([^)]*\): could not empty .* after its replay \(its rows landed or are in newer files\)/)
  world.refuseWrite = null
  await idleFor(world, 2)
  expect(landed()).toBe(1)
  expect(deadLetterRows(world).length).toBe(0)
})

test('a replay that cannot write its mark beside the file sends none of it: the rows wait, and go once writes work', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  expect(parked.length).toBe(1)
  world.replies = [OK_INSERT]
  const asked = world.insertCalls().length
  world.failWrites = true
  await idleFor(world, 2)
  expect(world.insertCalls().length).toBe(asked)
  expect(parkedIn(world, 'retry')).toEqual(parked)
  expect(await statusText($)).toMatch(/could not mark the replay of retry-.* before sending, so none of it is sent/)
  world.failWrites = false
  await idleFor(world, 2)
  expect(world.insertCalls().filter(f => f.answered === 200).length).toBe(1)
  expect(deadLetterRows(world).length).toBe(0)
})

test('a replay that cannot list the dead-letter directory leaves the rows due and says why; the next one sends them', { options: OPTIONS }, async ($, on) => {
  let failLists = false
  on('fs.list', { path: PARTITION_DIR }, ($, e, next) => (failLists ? { deny: `EIO: ${e.path}` } : next(e)))
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  const parked = parkedIn(world, 'retry')
  world.replies = [OK_INSERT]
  failLists = true
  await idleFor(world, 1)
  expect(parkedIn(world, 'retry')).toEqual(parked)
  expect(await statusText($)).toMatch(/last backfill \(recovery, [^)]*\): 0 file\(s\), 0 rows sent, 0 left; stopped: cannot list .*EIO/)
  failLists = false
  await idleFor(world, 2)
  expect(deadLetterRows(world).length).toBe(0)
})

test('rows refused on a send, then on a replay whose answer lands none, all count on the status line', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  expect(parkedIn(world, 'retry').length).toBe(1)
  // The tool call's two rows are refused whole on their send; then the
  // replay's answer refuses the parked row. No row has landed.
  world.replies = [INVALID, FIRST_REFUSED]
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await idleFor(world, 2)
  expect(parkedIn(world, 'retry')).toEqual([])
  expect(parkedIn(world, 'rejected').length).toBe(3)
  expect(await statusText($)).toMatch(/rows: captured 3, sent 0,/)
  expect(world.statusLines.at(-1)).toBe('BQAA: 3 row(s) refused (invalid: no such field: extra); fix, then /bqaa-backfill')
})

test('a replay whose rows BigQuery only asks for again leaves the refused count on the status line, until rows land', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  world.replies = [INVALID, FIRST_AGAIN]
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await idleFor(world, 2)
  expect(parkedIn(world, 'retry').length).toBe(1)
  expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: 2 row\(s\) refused \(HTTP 400: Invalid value for the table schema\); fix, then \/bqaa-backfill$/)
  world.replies = [OK_INSERT]
  await idleFor(world, 2)
  expect(parkedIn(world, 'retry')).toEqual([])
  expect(world.statusLines.at(-1)).toBeUndefined()
})

test('a replay BigQuery answers with a 401, and again with a fresh token, is the credentials notice, as a send is', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  expect((await band($)).line ?? '').toMatch(/BigQuery cannot find the dataset/)
  world.replies = [UNAUTHENTICATED]
  const requests = world.insertCalls().length
  await idleFor(world, 1)
  expect((await band($)).line ?? '').toMatch(/^rows are not being written: no working Google credentials\. /)
  // Each replay tries once more with a fresh token.
  expect((world.insertCalls().length - requests) % 2).toBe(0)
  expect(parkedIn(world, 'retry').length).toBe(1)
})

test('/bqaa-backfill run again while access is denied counts the rows it refuses again once', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DENIED]
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).waiting).toBe(' 1 row(s) waiting.')
  for (let i = 0; i < 2; i += 1) {
    await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
    await world.clock.advance(1000)
  }
  expect(world.insertCalls().length).toBe(3)
  expect((await band($)).waiting).toBe(' 1 row(s) waiting.')
  expect(parkedIn(world, 'rejected').length).toBe(1)
})

test('an idle replay of many parked rows stops between requests for rows queued meanwhile: they go first, then the replay goes on', async () => {
  // A writer built by hand. Its rows are parked after one try, all in one
  // file; the replay then sends one row a request.
  const base = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  let config = { ...base, maxRetries: 0 }
  let now = 0
  let answer = DATASET_MISSING
  let holdNext = false
  let release: (() => void) | null = null
  const timers: { at: number; fn: () => void; live: boolean }[] = []
  const files = new Map<string, string>()
  const sent: string[][] = []
  const io: Io = {
    now: () => now,
    after: (ms, fn) => {
      const timer = { at: now + ms, fn, live: true }
      timers.push(timer)
      return { cancel: () => (timer.live = false) }
    },
    fetch: async (url, init) => {
      if (answer === OK_INSERT) sent.push(idsOf(init.body ?? '{"rows":[]}'))
      if (holdNext) {
        holdNext = false
        await new Promise<void>(resolve => (release = resolve))
      }
      return answer
    },
    runCommand: async () => ({ exitCode: 0, stdout: `${'a'.repeat(24)}\n`, stderr: '' }),
    envToken: async () => undefined,
    writeFile: async (path, text) => {
      files.set(path, text)
    },
    readFile: async path => files.get(path) ?? '',
    listDir: async dir => [...files].filter(([path]) => path.startsWith(`${dir}/`)).map(([path, text]) => ({ name: path.slice(dir.length + 1), kind: 'file', size: text.length })),
    storeGet: async () => undefined,
    storeSet: async () => undefined,
    storeDelete: async () => undefined,
    status: () => undefined,
    debug: () => undefined,
  }
  const writer = new Writer(io, () => config, () => 'hand-built-session')
  const tick = async (): Promise<void> => {
    now += 31_000
    for (const timer of timers.filter(t => t.live && t.at <= now).sort((a, b) => a.at - b.at)) {
      timer.live = false
      timer.fn()
    }
    await writer.flush()
  }
  const row = (n: number) => buildRow({ eventType: 'STATE_DELTA', timestampMs: 0, eventId: String(n).repeat(32), agent: 'coding-agent', sessionId: 'hand-built-session', invocationId: null, userId: 'dev', traceId: null, spanId: null, parentSpanId: null, content: {}, attributes: {}, maxPayloadChars: 16384 }).row
  for (const n of [1, 2, 3]) writer.enqueue(row(n), 0)
  await tick()
  expect(writer.queue.length).toBe(0)
  config = { ...base, maxBatchRows: 1 }
  answer = OK_INSERT
  // The replay's first request takes a while; a row is queued meanwhile.
  holdNext = true
  const flushing = tick()
  for (let i = 0; i < 1000 && release === null; i += 1) await Promise.resolve()
  expect(release === null).toBe(false)
  writer.enqueue(row(4), 0)
  ;(release as (() => void) | null)?.()
  await flushing
  // It stopped for the queued row, which is no failure: no backoff.
  expect(writer.snapshot().consecutiveFailures).toBe(0)
  for (let t = 0; t < 4; t += 1) await tick()
  expect(sent).toEqual([['1'.repeat(32)], ['4'.repeat(32)], ['2'.repeat(32)], ['3'.repeat(32)]])
  expect([...files.values()].reduce((rows, text) => rows + parseFile(text).lines.length, 0)).toBe(0)
})

// insertAll refused the first row of the request and asked for the second again.
const FIRST_REFUSED_SECOND_AGAIN = {
  status: 200,
  text: JSON.stringify({
    insertErrors: [
      { index: 0, errors: [{ reason: 'invalid', message: 'no such field: extra' }] },
      { index: 1, errors: [{ reason: 'backendError', message: 'try again' }] },
    ],
  }),
}

test('a refused row whose file cannot be emptied is sent once: no further request or file, and the file is emptied once it can be', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DATASET_MISSING]
  await startSession($)
  await leaveIdle($, world)
  const source = [...world.files.keys()].find(path => path.startsWith(`${PARTITION_DIR}/retry-`)) ?? ''
  const parked = parkedIn(world, 'retry')
  // The file can be read, but not emptied; new files can be written.
  world.refuseWrite = (path, text) => path === source && text === ''
  world.replies = [FIRST_REFUSED]
  const requests = world.insertCalls().length
  await idleFor(world, 5)
  expect(world.insertCalls().length).toBe(requests + 1)
  expect(parkedIn(world, 'rejected')).toEqual(parked)
  expect([...world.files].filter(([path, text]) => path.includes('/rejected-') && text !== '').length).toBe(1)
  expect(world.files.get(source) !== '').toBe(true)
  // The record that its rows are written out: a new writer (the next
  // session.start) sends none of it either.
  expect(world.store.get(`replayed:${TABLE_REF}/${source.split('/').pop()}`)).toMatchObject({ size: new TextEncoder().encode(world.files.get(source) ?? '').length, fates: 'R', written: [true] })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await idleFor(world, 2)
  expect(world.insertCalls().length).toBe(requests + 1)
  expect([...world.files].filter(([path, text]) => path.includes('/rejected-') && text !== '').length).toBe(1)
  world.refuseWrite = null
  await idleFor(world, 2)
  expect(world.files.get(source)).toBe('')
  expect(world.insertCalls().length).toBe(requests + 1)
  expect(parkedIn(world, 'rejected')).toEqual(parked)
  expect([...world.store.keys()].some(key => key.startsWith('replayed:'))).toBe(false)
})

test('a file whose replay record cannot be read stays due: the replays that run by themselves send none of it, and write out what it left once the record can be read', { options: OPTIONS }, async ($, on) => {
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  let unreadable = false
  on('store.get', { key: `replayed:${TABLE_REF}/${name}` }, ($, e, next) => (unreadable ? { deny: 'the store could not be read' } : next(e)))
  const world = setUp(on)
  const source = `${PARTITION_DIR}/${name}`
  world.files.set(source, packFiles([line('a'.repeat(32), 'retry')])[0] as string)
  // The outcome is recorded in the store; the copy of the refused row cannot
  // be written yet, so the file is not done.
  world.refuseWrite = (path, text) => path.includes('/rejected-') && text !== ''
  world.replies = [FIRST_REFUSED]
  await startSession($)
  await world.clock.advance(20_000)
  const requests = world.insertCalls().length
  const copies = () => parkedIn(world, 'rejected').filter(id => id === 'a'.repeat(32)).length
  expect(copies()).toBe(0)
  unreadable = true
  world.refuseWrite = null
  await idleFor(world, 5)
  expect(world.insertCalls().length).toBe(requests)
  expect(world.files.get(source) !== '').toBe(true)
  expect(copies()).toBe(0)
  unreadable = false
  await idleFor(world, 3)
  expect(world.insertCalls().length).toBe(requests)
  expect(world.files.get(source)).toBe('')
  expect(copies()).toBe(1)
})

test('a file whose rows a replay wrote out but could not empty is emptied by the next, from the mark that it was done, the store unreadable or not', { options: OPTIONS }, async ($, on) => {
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  let unreadable = false
  on('store.get', { key: `replayed:${TABLE_REF}/${name}` }, ($, e, next) => (unreadable ? { deny: 'the store could not be read' } : next(e)))
  const world = setUp(on)
  const source = `${PARTITION_DIR}/${name}`
  world.files.set(source, packFiles([line('a'.repeat(32), 'retry')])[0] as string)
  world.refuseWrite = (path, text) => path === source && text === ''
  world.replies = [FIRST_REFUSED]
  await startSession($)
  await world.clock.advance(20_000)
  const requests = world.insertCalls().length
  const copies = () => parkedIn(world, 'rejected').filter(id => id === 'a'.repeat(32)).length
  expect(copies()).toBe(1)
  expect(world.files.get(source) !== '').toBe(true)
  unreadable = true
  world.refuseWrite = null
  await idleFor(world, 3)
  expect(world.insertCalls().length).toBe(requests)
  expect(world.files.get(source)).toBe('')
  expect(copies()).toBe(1)
})

test('a replay that can write only part of what it left sends nothing again, writes the rest under the same names once it can, then the rows left go on', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  const refused = 'a'.repeat(32)
  const asked = 'b'.repeat(32)
  world.files.set(source, packFiles([line(refused, 'retry'), line(asked, 'retry')])[0] as string)
  // New retry files cannot be written; rejected ones can.
  world.refuseWrite = (path, text) => path !== source && path.startsWith(`${PARTITION_DIR}/retry-`) && text !== ''
  world.replies = [FIRST_REFUSED_SECOND_AGAIN]
  await startSession($)
  await world.clock.advance(20_000)
  const sent = (id: string) => world.sentRows().filter(row => row.event_id === id).length
  expect(sent(refused)).toBe(1)
  expect(parkedIn(world, 'rejected')).toEqual([refused])
  const files = world.files.size
  await idleFor(world, 5)
  expect(sent(refused)).toBe(1)
  expect(sent(asked)).toBe(1)
  expect(world.files.size).toBe(files)
  // Writes work again: the rest is written, the source emptied, and the row
  // asked for again goes on, the refused one never.
  world.refuseWrite = null
  world.replies = [OK_INSERT]
  await idleFor(world, 3)
  expect(world.files.get(source)).toBe('')
  expect(sent(refused)).toBe(1)
  expect(sent(asked)).toBe(2)
  expect(parkedIn(world, 'retry')).toEqual([])
  expect(parkedIn(world, 'rejected')).toEqual([refused])
})

test('a file whose rows a replay of another session already wrote out is not sent again: it is only emptied', { options: OPTIONS }, async ($, on) => {
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  const text = packFiles([line('c'.repeat(32), 'retry')])[0] as string
  const written = packFiles([line('c'.repeat(32), 'rejected')])[0] as string
  const record = { size: new TextEncoder().encode(text).length, fates: 'R', reasons: ['invalid: no such field: extra'], refusals: [0], files: [`${PARTITION_DIR}/rejected-20261013T200103003Z-earlier-2.jsonl`], written: [true], waits: false }
  const world = setUp(on, {}, { [`replayed:${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`]: record })
  world.files.set(source, text)
  world.files.set(`${PARTITION_DIR}/rejected-20261013T200103003Z-earlier-2.jsonl`, written)
  await startSession($)
  await world.clock.advance(20_000)
  expect(world.sentRows().some(row => row.event_id === 'c'.repeat(32))).toBe(false)
  expect(world.files.get(source)).toBe('')
  expect([...world.store.keys()].some(key => key.startsWith('replayed:'))).toBe(false)
  expect(parkedIn(world, 'rejected')).toEqual(['c'.repeat(32)])
})

test("a row an earlier session refused, refused again by /bqaa-backfill while access is denied, counts once in the band", { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.files.set(`${PARTITION_DIR}/rejected-20261013T200102003Z-earlier-1.jsonl`, packFiles([line('d'.repeat(32), 'rejected')])[0] as string)
  await startSession($)
  await world.clock.advance(5000)
  world.replies = [DENIED]
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.advance(1000)
  expect((await band($)).waiting).toBe(' 1 row(s) waiting.')
})

test('a later request of the same replay that lands rows ends the refused count of the requests before it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.files.set(`${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`, packFiles([line('e'.repeat(32), 'retry')])[0] as string)
  world.files.set(`${PARTITION_DIR}/retry-20261013T200102003Z-earlier-2.jsonl`, packFiles([line('f'.repeat(32), 'retry')])[0] as string)
  await startSession($)
  await world.clock.advance(5000)
  world.replies = [FIRST_REFUSED, OK_INSERT]
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.advance(1000)
  expect(parkedIn(world, 'rejected')).toEqual(['e'.repeat(32)])
  expect(world.statusLines.at(-1)).toBeUndefined()
})

test('a row refused again after rows landed counts again: the count is of rows refused since rows last landed', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [INVALID]
  await startSession($)
  await world.clock.advance(5000)
  expect(parkedIn(world, 'rejected').length).toBe(1)
  expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: 1 row\(s\) refused/)
  // Rows land, then /bqaa-backfill sends the refused row, refused again.
  world.replies = [OK_INSERT]
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.statusLines.at(-1)).toBeUndefined()
  world.replies = [INVALID]
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.advance(1000)
  expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: 1 row\(s\) refused/)
})

test('a 400 after the access-denied notice clears it: BigQuery let the identity write, and refused the rows for a reason of their own', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DENIED]
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).line ?? '').toMatch(/denied the identity access/)
  world.replies = [INVALID]
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect((await band($)).line).toBeUndefined()
  expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: \d+ row\(s\) refused/)
})

test('a row denied again under a later access-denied notice counts in that notice', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DENIED]
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).waiting).toBe(' 1 row(s) waiting.')
  // Access comes back (rows land), then goes again.
  world.replies = [OK_INSERT]
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect((await band($)).line).toBeUndefined()
  world.replies = [DENIED]
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.advance(1000)
  expect((await band($)).waiting).toBe(' 1 row(s) waiting.')
})

test('/bqaa-backfill run again with BigQuery still refusing a row counts it once on the status line', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [INVALID]
  await startSession($)
  await world.clock.advance(5000)
  for (let i = 0; i < 2; i += 1) {
    await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
    await world.clock.advance(1000)
  }
  expect(world.insertCalls().length).toBe(3)
  expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: 1 row\(s\) refused/)
})

test('a file written out under a kept outcome is never written again: one /bqaa-backfill sent and emptied meanwhile stays empty', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  const refused = 'a'.repeat(32)
  world.files.set(source, packFiles([line(refused, 'retry'), line('b'.repeat(32), 'retry')])[0] as string)
  world.refuseWrite = (path, text) => path !== source && path.startsWith(`${PARTITION_DIR}/retry-`) && text !== ''
  world.replies = [FIRST_REFUSED_SECOND_AGAIN]
  await startSession($)
  await world.clock.advance(20_000)
  const rejectedFile = [...world.files.keys()].find(path => path.startsWith(`${PARTITION_DIR}/rejected-`)) ?? ''
  expect(parkedIn(world, 'rejected')).toEqual([refused])
  // The refused row's cause is fixed, and /bqaa-backfill sends its file.
  world.replies = [OK_INSERT]
  await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
  await world.clock.advance(1000)
  expect(world.files.get(rejectedFile)).toBe('')
  world.refuseWrite = null
  await idleFor(world, 3)
  expect(world.files.get(rejectedFile)).toBe('')
  expect(world.files.get(source)).toBe('')
  expect(parkedIn(world, 'rejected')).toEqual([])
  expect(world.sentRows().filter(row => row.event_id === refused).length).toBe(2)
})

test('what a replay left, only partly written, is finished by the next session.start\'s writer: the refused row is never sent again, nor copied', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const source = `${PARTITION_DIR}/retry-20261013T200102003Z-earlier-1.jsonl`
  const refused = 'a'.repeat(32)
  const asked = 'b'.repeat(32)
  world.files.set(source, packFiles([line(refused, 'retry'), line(asked, 'retry')])[0] as string)
  world.refuseWrite = (path, text) => path !== source && path.startsWith(`${PARTITION_DIR}/retry-`) && text !== ''
  world.replies = [FIRST_REFUSED_SECOND_AGAIN]
  await startSession($)
  await world.clock.advance(20_000)
  const sent = (id: string) => world.sentRows().filter(row => row.event_id === id).length
  const copies = () => [...world.files].filter(([path, text]) => path.includes('/rejected-') && text !== '').length
  expect(sent(refused)).toBe(1)
  expect(copies()).toBe(1)
  // Three more session.start runs, each with a new writer, while what is
  // left still cannot all be written.
  for (let i = 0; i < 3; i += 1) {
    await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
    await world.clock.advance(20_000)
  }
  expect(sent(refused)).toBe(1)
  expect(copies()).toBe(1)
  world.refuseWrite = null
  world.replies = [OK_INSERT]
  await idleFor(world, 3)
  expect(world.files.get(source)).toBe('')
  expect(sent(refused)).toBe(1)
  expect(copies()).toBe(1)
  expect(parkedIn(world, 'retry')).toEqual([])
  expect(world.sentRows().filter(row => row.event_id === asked).length >= 2).toBe(true)
  expect([...world.store.keys()].some(key => key.startsWith('replayed:'))).toBe(false)
})

// The marks that the file `name` is done, each writer's, in files of their
// own that no record is written to: their paths, and their text by path.
const doneFiles = (files: Map<string, string>, name: string): string[] => [...files.keys()].filter(path => path.startsWith(`/dl/${TABLE_REF}/outcome-done-`) && /^[0-9a-z]+$/.test(path.slice(`/dl/${TABLE_REF}/outcome-done-`.length, -name.length - 1)) && path.endsWith(`-${name}`))
const doneTexts = (files: Map<string, string>, name: string): Record<string, string> => Object.fromEntries(doneFiles(files, name).map(path => [path, files.get(path) ?? '']))
// Whether a mark that the file `name` is done is kept beside it, whole.
const doneMark = (files: Map<string, string>, name: string): boolean =>
  doneFiles(files, name).some(path => {
    const kept = parseFile(files.get(path) ?? '')
    return kept.complete && kept.unreadable.length === 1 && (JSON.parse(kept.unreadable[0] as string) as { retired?: boolean }).retired === true
  })
// Where another writer, `dddddddddd`, keeps its mark that `name` is done.
const doneOf = (name: string): string => `/dl/${TABLE_REF}/outcome-done-${'d'.repeat(10)}-${name}`

// A file beside a source that a replay record is written to: not the done
// mark, nor the mark a replay leaves before it sends.
const recordFile = (path: string): boolean => path.includes('/outcome-') && !path.includes('/outcome-done-') && !path.includes('/outcome-sending-')

// Two sessions that both replay a file: the first's request waits until
// `release`, and the second's mark until that request is out. The second
// sends past the first's mark only as /bqaa-backfill force (which these
// callers make it): any other replay leaves the file to the first.
function bothSend(w: ReturnType<typeof twoWriters>): { release: () => void } {
  let release = (): void => undefined
  let out = (): void => undefined
  const asked = new Promise<void>(resolve => (out = resolve))
  w.hold(session => {
    if (session !== 'first-session') return undefined
    out()
    return new Promise<void>(resolve => (release = resolve))
  })
  w.holdWrites((session, path) => (session === 'second-session' && path.includes('/outcome-sending-') ? asked : undefined))
  return { release: () => release() }
}

// Two writers of their own, as two sessions have, on one dead-letter
// directory and one store, which takes no more than 4 MiB of JSON in all, as
// the host's. BigQuery gives `answer` for the ids of each request.
function twoWriters(answer: (ids: string[]) => { status: number; text: string }) {
  const config = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  const files = new Map<string, string>()
  const store = new Map<string, unknown>()
  const sent: string[] = []
  const bytes = (text: string): number => new TextEncoder().encode(text).length
  let refuseWrite: ((path: string, text: string) => boolean) | null = null
  // Writes cut off part-way: half the text lands (or `tornAt` of it), then
  // the write fails.
  let tearWrite: ((path: string, text: string) => boolean) | null = null
  let tornAt = 0.5
  // Holds a session's requests until it is let go.
  let gate: ((session: string) => Promise<void> | undefined) | null = null
  // Holds a session's writes, and store reads, the same way.
  let writeGate: ((session: string, path: string, text: string) => Promise<void> | undefined) | null = null
  let getGate: ((session: string, key: string) => Promise<void> | undefined) | null = null
  let readGate: ((session: string, path: string) => Promise<void> | undefined) | null = null
  let setGate: ((session: string, key: string) => Promise<void> | undefined) | null = null
  // When files were last written, for the listing (none: not said).
  const mtimes = new Map<string, number>()
  const statuses: (string | undefined)[] = []
  // The status lines the surface does not take (it throws), and whether it
  // takes debug lines.
  let statusFails: ((text: string | undefined) => boolean) | null = null
  let debugFails = false
  // `from`: the store takes that many of a replay's records, then no more.
  // `get` may name the keys whose reads fail.
  let storeFails: { get?: boolean | ((key: string) => boolean); set?: boolean | ((key: string) => boolean); from?: number } = {}
  let records = 0
  // The sessions whose process has ended: a write of theirs not done by then
  // never lands.
  const ended = new Set<string>()
  // The timers set: one goes off only when a test sets it off (`fire`).
  const timers: { ms: number; fn: () => void; done: boolean }[] = []
  const io = (session: string): Io => ({
    now: () => 0,
    after: (ms, fn) => {
      const timer = { ms, fn, done: false }
      timers.push(timer)
      return { cancel: () => void (timer.done = true) }
    },
    fetch: async (url, init) => {
      const ids = idsOf(init.body ?? '{"rows":[]}')
      sent.push(...ids)
      await gate?.(session)
      return answer(ids)
    },
    runCommand: async () => ({ exitCode: 0, stdout: `${'a'.repeat(24)}\n`, stderr: '' }),
    envToken: async () => undefined,
    writeFile: async (path, text) => {
      await writeGate?.(session, path, text)
      if (ended.has(session)) return new Promise<void>(() => undefined)
      if (refuseWrite?.(path, text)) throw new Error(`EACCES: ${path} (${session})`)
      if (tearWrite?.(path, text)) {
        files.set(path, text.slice(0, Math.floor(text.length * tornAt)))
        throw new Error(`EIO: ${path} cut off (${session})`)
      }
      files.set(path, text)
    },
    readFile: async path => {
      await readGate?.(session, path)
      return files.get(path) ?? ''
    },
    exists: async path => files.has(path),
    listDir: async dir => [...files].filter(([path]) => path.startsWith(`${dir}/`)).map(([path, text]) => ({ name: path.slice(dir.length + 1), kind: 'file', size: bytes(text), mtimeMs: mtimes.get(path) })),
    storeGet: async key => {
      await getGate?.(session, key)
      if (key.startsWith('replayed:') && (storeFails.get === true || (typeof storeFails.get === 'function' && storeFails.get(key)))) throw new Error('the store could not be read')
      return store.get(key)
    },
    storeSet: async (key, value) => {
      await setGate?.(session, key)
      if (key.startsWith('replayed:') && ((typeof storeFails.set === 'function' ? storeFails.set(key) : storeFails.set) || (storeFails.from !== undefined && records >= storeFails.from))) throw new Error('the store could not be written')
      if (key.startsWith('replayed:')) records += 1
      const json = JSON.parse(JSON.stringify(value)) as unknown
      if (bytes(JSON.stringify(Object.fromEntries(new Map(store).set(key, json)))) > 4 * 1024 * 1024) throw new Error('the store would pass 4 MiB of JSON')
      store.set(key, json)
    },
    storeDelete: async key => void store.delete(key),
    status: text => {
      if (statusFails?.(text)) throw new Error('no status line here')
      statuses.push(text)
    },
    debug: () => {
      if (debugFails) throw new Error('no debug log here')
    },
  })
  return {
    config,
    files,
    store,
    sent,
    statuses,
    writer: (session: string, c = config) => new Writer(io(session), () => c, () => session),
    // Refuses the writes of what is left to the paths `f` picks.
    refuse: (f: ((path: string) => boolean) | null) => (refuseWrite = f === null ? null : (path, text) => text !== '' && f(path)),
    // Refuses the writes `f` picks, emptying a file included.
    refuseWrites: (f: ((path: string, text: string) => boolean) | null) => (refuseWrite = f),
    failStore: (fails: { get?: boolean | ((key: string) => boolean); set?: boolean | ((key: string) => boolean); from?: number }) => (storeFails = fails),
    recordWrites: () => records,
    tear: (f: ((path: string, text: string) => boolean) | null, at = 0.5) => {
      tearWrite = f
      tornAt = at
    },
    hold: (f: ((session: string) => Promise<void> | undefined) | null) => (gate = f),
    holdWrites: (f: ((session: string, path: string, text: string) => Promise<void> | undefined) | null) => (writeGate = f),
    holdGets: (f: ((session: string, key: string) => Promise<void> | undefined) | null) => (getGate = f),
    holdReads: (f: ((session: string, path: string) => Promise<void> | undefined) | null) => (readGate = f),
    holdSets: (f: ((session: string, key: string) => Promise<void> | undefined) | null) => (setGate = f),
    failStatus: (f: ((text: string | undefined) => boolean) | null) => (statusFails = f),
    failDebug: (fails: boolean) => (debugFails = fails),
    // Ends the session's process, as the host does once session.end returns.
    exit: (session: string) => void ended.add(session),
    // Sets off the timers set for `ms` or less.
    fire: (ms: number) => {
      for (const timer of timers) {
        if (timer.done || timer.ms > ms) continue
        timer.done = true
        timer.fn()
      }
    },
    mtimes,
    sentOf: (id: string) => sent.filter(x => x === id).length,
    copiesOf: (id: string) => [...files].filter(([path, text]) => path.includes('/rejected-') && text !== '').flatMap(([, text]) => parseFile(text).lines).filter(l => l.row.event_id === id).length,
  }
}

test('another session\'s writer resumes what a replay left only partly written: it sends none of it, writes the rest, and empties the file', async () => {
  const w = twoWriters(() => FIRST_REFUSED_SECOND_AGAIN)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  w.files.set(source, packFiles([line('a'.repeat(32), 'retry'), line('b'.repeat(32), 'retry')])[0] as string)
  w.refuse(path => path !== source && path.includes('/retry-'))
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['a'.repeat(32), 'b'.repeat(32)])
  // The other session, with writes working again.
  w.refuse(null)
  const report = await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['a'.repeat(32), 'b'.repeat(32)])
  // Its last-backfill line counts the file it finished, what it left, and
  // how many of those were refused.
  expect(report).toMatchObject({ files: 1, rowsSent: 0, rowsLeft: 2, rowsRefused: 1 })
  expect(w.files.get(source)).toBe('')
  const lines = [...w.files].filter(([, text]) => text !== '').flatMap(([path, text]) => parseFile(text).lines.map(l => `${path.includes('/rejected-') ? 'rejected' : 'retry'}:${l.row.event_id.slice(0, 1)}`))
  expect(lines.sort()).toEqual(['rejected:a', 'retry:b'])
  expect([...w.store.keys()]).toEqual([])
})

test('an outcome recorded before any of what is left was written: the next writer writes all of it from the record, sending nothing', async () => {
  const w = twoWriters(() => FIRST_REFUSED_SECOND_AGAIN)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  w.files.set(source, packFiles([line('a'.repeat(32), 'retry'), line('b'.repeat(32), 'retry')])[0] as string)
  // No file of what is left can be written: the mark and the record are,
  // then the first write fails.
  w.refuse(path => path.includes(`/dl/${TABLE_REF}/`) && path !== source && !path.includes('/outcome-sending-'))
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const record = w.store.get(`replayed:${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`) as { fates: string; written: boolean[] }
  expect(record).toMatchObject({ fates: 'RW', written: [false, false] })
  w.refuse(null)
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent.length).toBe(2)
  expect(w.files.get(source)).toBe('')
  expect([...w.files].filter(([path, text]) => text !== '' && !path.includes('/outcome-')).length).toBe(2)
})

test('past the 10,000 rows a count keeps, a row refused again may count again, and the status line says about', async () => {
  const config = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  const files = new Map<string, string>()
  const statuses: (string | undefined)[] = []
  // Every row of every request refused.
  const refuseAll = (body: string) => ({ status: 200, text: JSON.stringify({ insertErrors: idsOf(body).map((id, index) => ({ index, errors: [{ reason: 'invalid', message: 'no such field: extra' }] })) }) })
  const io: Io = {
    now: () => 0,
    after: () => ({ cancel: () => undefined }),
    fetch: async (url, init) => refuseAll(init.body ?? '{"rows":[]}'),
    runCommand: async () => ({ exitCode: 0, stdout: `${'a'.repeat(24)}\n`, stderr: '' }),
    envToken: async () => undefined,
    writeFile: async (path, text) => void files.set(path, text),
    readFile: async path => files.get(path) ?? '',
    listDir: async dir => [...files].filter(([path]) => path.startsWith(`${dir}/`)).map(([path, text]) => ({ name: path.slice(dir.length + 1), kind: 'file', size: new TextEncoder().encode(text).length })),
    storeGet: async () => undefined,
    storeSet: async () => undefined,
    storeDelete: async () => undefined,
    status: text => void statuses.push(text),
    debug: () => undefined,
  }
  const ids = Array.from({ length: 10_001 }, (_, n) => n.toString(16).padStart(32, '0'))
  packFiles(ids.map(id => line(id, 'rejected'))).forEach((text, n) => files.set(`/dl/${TABLE_REF}/rejected-20261013T200102003Z-earlier-${n + 1}.jsonl`, text))
  const writer = new Writer(io, () => config, () => 'hand-built-session')
  await writer.backfill(['spill', 'retry', 'rejected'], 'command')
  expect(statuses.at(-1) ?? '').toMatch(/^BQAA: about 10001 row\(s\) refused /)
  await writer.backfill(['spill', 'retry', 'rejected'], 'command')
  expect(statuses.at(-1) ?? '').toMatch(/^BQAA: about 10002 row\(s\) refused /)
})

test('a file of what is left that one writer wrote, and /bqaa-backfill sent and emptied, is never written again by the writer that finishes the outcome', async () => {
  let answer = FIRST_REFUSED_SECOND_AGAIN
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  w.files.set(source, packFiles([line('a'.repeat(32), 'retry'), line('b'.repeat(32), 'retry')])[0] as string)
  w.refuse(path => path !== source && path.includes('/retry-'))
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const rejectedFile = [...w.files.keys()].find(path => path.includes('/rejected-')) ?? ''
  // The refused row's cause is fixed: another session's /bqaa-backfill sends
  // the rejected file and empties it (what is left of the source still
  // cannot be written).
  answer = OK_INSERT
  await w.writer('second-session').backfill(['spill', 'retry', 'rejected'], 'command')
  expect(w.files.get(rejectedFile)).toBe('')
  w.refuse(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect(w.files.get(source)).toBe('')
  expect(w.files.get(rejectedFile)).toBe('')
  expect(w.sent.filter(id => id === 'a'.repeat(32)).length).toBe(2)
})

test('a record that does not fit the file (another size) is not trusted: the file is replayed, never emptied unsent', async () => {
  const w = twoWriters(() => OK_INSERT)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  w.files.set(source, packFiles([line('c'.repeat(32), 'retry')])[0] as string)
  w.store.set(`replayed:${TABLE_REF}/${name}`, { size: 1, fates: 'L', reasons: [], refusals: [], files: [], written: [], waits: false })
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['c'.repeat(32)])
  expect(w.files.get(source)).toBe('')
})

test('a writer back after another one finished its outcome, and replayed what it left, writes none of it again: the refused row goes twice in all, and is copied once', async () => {
  let answer = FIRST_REFUSED_SECOND_AGAIN
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // The first writer records the outcome, and can write none of what is left.
  const first = w.writer('first-session')
  w.refuse(path => path !== source && !path.includes('/outcome-sending-'))
  await first.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b), w.recordWrites()]).toEqual([1, 1, 1])
  // Another session writes all of it, but cannot empty the source; its next
  // replay sends the row asked for again, and BigQuery refuses it.
  w.refuseWrites((path, text) => path === source && text === '')
  const second = w.writer('second-session')
  await second.backfill(['spill', 'retry'], 'auto')
  answer = FIRST_REFUSED
  await second.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(b), w.copiesOf(b)]).toEqual([2, 1])
  // The first writer comes back, writes working: the record says all of what
  // is left is written, so it writes none of it again, only empties the file.
  w.refuseWrites(null)
  await first.backfill(['spill', 'retry'], 'auto')
  await first.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b), w.copiesOf(a), w.copiesOf(b)]).toEqual([1, 2, 1, 1])
  expect(w.files.get(source)).toBe('')
  expect([...w.store.keys()]).toEqual([])
})

test('two writers that replay one file at the same time send it at most once, and what it left is written once', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  // Both read the lease before either writes it; each then reads the other's
  // mark beside its own, and neither sends. The next replay sends it.
  await Promise.all([w.writer('first-session').backfill(['spill', 'retry'], 'auto'), w.writer('second-session').backfill(['spill', 'retry'], 'auto')])
  expect(w.sentOf(a) <= 1).toBe(true)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, ''])
  // Each lets its own mark go: the two that sent nothing, and the one that finished the file.
  expect([...w.files].filter(([path, text]) => path.includes('/outcome-sending-') && text !== '').length).toBe(0)
})

test('a record that cannot be read leaves its file waiting: no writer sends any of it, and once it can be read the file is only emptied', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  // Recorded, but the copy of the refused row is not written yet: not done.
  w.refuse(path => path.includes('/rejected-'))
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  w.failStore({ get: true })
  w.refuse(null)
  const second = w.writer('second-session')
  const report = await second.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  expect(report.held ?? '').toMatch(/^cannot read the replay record of retry-20261013T200102003Z-earlier-1\.jsonl: the store could not be read/)
  expect(report.skippedFiles).toBe(1)
  w.failStore({})
  await second.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  expect(w.files.get(source)).toBe('')
})

test('a record the store does not take is kept in a file beside the source: another writer resumes it from there, sending nothing again, and the source is emptied once it can be, its record left as the mark it was done', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites((path, text) => path === source && text === '')
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  expect([...w.store.keys()]).toEqual([])
  expect(w.files.get(`/dl/${TABLE_REF}/outcome-${name}`) ?? '').toMatch(/^\{"fates":"R",/)
  w.failStore({})
  w.refuseWrites(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  expect([w.files.get(source), doneMark(w.files, name)]).toEqual(['', true])
  expect([...w.store.keys()]).toEqual([])
})

test('a record neither the store nor a file takes: nothing of what the replay left is written, and the writer sends none of it again until it can record it', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry')])[0] as string
  w.files.set(source, text)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  const writer = w.writer('a-session')
  const report = await writer.backfill(['spill', 'retry'], 'auto')
  await writer.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  expect(w.files.get(source)).toBe(text)
  expect(report.held ?? '').toMatch(/could not record the replay of .*retry-20261013T200102003Z-earlier-1\.jsonl, so nothing more of what it left is written/)
  w.failStore({})
  w.refuseWrites(null)
  await writer.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  expect(w.files.get(source)).toBe('')
})

test('a record the store took at first and then did not: what the file beside the source has as written counts too, so nothing replayed meanwhile is written again', async () => {
  let answer = FIRST_REFUSED_SECOND_AGAIN
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // The store takes the first record (nothing written yet), then no more: the
  // file beside the source holds the rest. The source cannot be emptied.
  w.failStore({ from: 1 })
  w.refuseWrites((path, text) => path === source && text === '')
  const first = w.writer('first-session')
  await first.backfill(['spill', 'retry'], 'auto')
  expect(w.store.get(`replayed:${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`)).toMatchObject({ written: [false, false] })
  // Its next replay sends the row asked for again, which BigQuery refuses.
  answer = FIRST_REFUSED
  await first.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(b), w.copiesOf(b)]).toEqual([2, 1])
  w.failStore({})
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b), w.copiesOf(a), w.copiesOf(b)]).toEqual([1, 2, 1, 1])
})

test('a record of the file\'s size that names fewer lines than the file holds is not trusted: the file is replayed, and no row is lost', async () => {
  const w = twoWriters(() => OK_INSERT)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const text = packFiles([line('c'.repeat(32), 'retry'), line('d'.repeat(32), 'retry')])[0] as string
  w.files.set(source, text)
  w.store.set(`replayed:${TABLE_REF}/${name}`, { size: new TextEncoder().encode(text).length, fates: 'W', reasons: [], refusals: [], files: [`/dl/${TABLE_REF}/retry-20261013T200103003Z-earlier-2.jsonl`], written: [false], waits: true })
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['c'.repeat(32), 'd'.repeat(32)])
  expect(w.files.get(source)).toBe('')
})

test('a replay record stays small whatever BigQuery says: 4,500 rows refused for as many reasons fit the store with 16 reasons kept, and another writer sends none of them again', async () => {
  const ids = Array.from({ length: 4500 }, (_, n) => n.toString(16).padStart(32, '0'))
  const w = twoWriters(chunk => ({ status: 200, text: JSON.stringify({ insertErrors: chunk.map((id, index) => ({ index, errors: [{ reason: 'invalid', message: `row ${id}: ${'x'.repeat(940)}` }] })) }) }))
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const texts = packFiles(ids.map(id => line(id, 'retry')))
  expect(texts.length).toBe(1)
  w.files.set(source, texts[0] as string)
  w.refuseWrites((path, text) => path === source && text === '')
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent.length).toBe(4500)
  const record = w.store.get(`replayed:${TABLE_REF}/${name}`) as { reasons: string[] }
  expect(record.reasons.length).toBe(16)
  expect(record.reasons[15]).toBe('refused for another reason (a replay keeps 16 reasons a file)')
  expect(new TextEncoder().encode(JSON.stringify(record)).length < 64 * 1024).toBe(true)
  expect(w.files.has(`/dl/${TABLE_REF}/outcome-${name}`)).toBe(false)
  const rejected = [...w.files].filter(([path, text]) => path.includes('/rejected-') && text !== '').flatMap(([, text]) => parseFile(text).lines)
  expect(rejected.length).toBe(4500)
  expect(rejected.find(l => l.row.event_id === ids[0])?.reason).toMatch(new RegExp(`row ${ids[0]}: x`))
  expect(rejected.find(l => l.row.event_id === ids[4499])?.reason).toBe('refused for another reason (a replay keeps 16 reasons a file)')
})

test('a record in the store that no longer fits the file does not hide the one beside it: the file is only emptied, and nothing is sent', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const rejected = `/dl/${TABLE_REF}/rejected-20261013T200103003Z-earlier-2.jsonl`
  const text = packFiles([line('c'.repeat(32), 'retry')])[0] as string
  const size = new TextEncoder().encode(text).length
  w.files.set(source, text)
  w.files.set(rejected, packFiles([line('c'.repeat(32), 'rejected')])[0] as string)
  w.store.set(`replayed:${TABLE_REF}/${name}`, { size, fates: 'WW', reasons: [], refusals: [], files: [`/dl/${TABLE_REF}/retry-20261013T200103003Z-earlier-3.jsonl`], written: [false], waits: true })
  w.files.set(`/dl/${TABLE_REF}/outcome-${name}`, sealFile(`${JSON.stringify({ size, fates: 'R', reasons: ['invalid: no such field: extra'], refusals: [0], files: [rejected], written: [true], waits: false })}\n`))
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual([])
  expect([w.files.get(source), doneMark(w.files, name)]).toEqual(['', true])
  expect(w.copiesOf('c'.repeat(32))).toBe(1)
})

test('a record beside the file that does not fit it is emptied with the replay afresh, so no later replay sends the file again', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const text = packFiles([line('c'.repeat(32), 'retry')])[0] as string
  w.files.set(source, text)
  w.files.set(`/dl/${TABLE_REF}/outcome-${name}`, sealFile(`${JSON.stringify({ size: new TextEncoder().encode(text).length, fates: 'WW', reasons: [], refusals: [], files: [`/dl/${TABLE_REF}/retry-20261013T200103003Z-earlier-3.jsonl`], written: [false], waits: true })}\n`))
  // The source cannot be emptied, so the new outcome's record stays.
  w.refuseWrites((path, text) => path === source && text === '')
  const writer = w.writer('a-session')
  for (let i = 0; i < 3; i += 1) await writer.backfill(['spill', 'retry'], 'auto')
  await w.writer('another-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf('c'.repeat(32)), w.copiesOf('c'.repeat(32))]).toEqual([1, 1])
  expect(w.files.get(`/dl/${TABLE_REF}/outcome-${name}`)).toBe('')
})

test('a record update beside the source cut off part-way leaves the one before it whole: no writer sends the file again, and the next one finishes it', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  // The record's first update, after the rejected file is written, is cut off.
  let records = 0
  w.tear((path, text) => recordFile(path) && text !== '' && ++records === 2)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  for (const session of ['second-session', 'third-session']) await w.writer(session).backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  expect(w.files.get(source)).toBe('')
  // What stays beside it is the mark that it was done.
  expect(doneMark(w.files, 'retry-20261013T200102003Z-earlier-1.jsonl')).toBe(true)
})

test('a first record cut off part-way, with none whole: the writer that holds the outcome writes it again, and sends nothing again', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  let records = 0
  w.tear((path, text) => recordFile(path) && text !== '' && ++records === 1)
  const writer = w.writer('a-session')
  await writer.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  await writer.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
  expect(w.files.get(source)).toBe('')
})

test('a first record cut off part-way, with none whole, its writer gone: replays that run by themselves, and /bqaa-backfill, leave the file waiting, and /bqaa-backfill force replays it afresh', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  let records = 0
  w.tear((path, text) => recordFile(path) && text !== '' && ++records === 1)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  for (const trigger of ['auto', 'command'] as const) {
    const report = await w.writer('second-session').backfill(['spill', 'retry', 'rejected'], trigger)
    expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
    expect(report.held ?? '').toMatch(/cut off part-way; \/bqaa-backfill force replays the file afresh/)
  }
  // The store takes records again: the one cut off goes with the file.
  w.failStore({})
  await w.writer('third-session').backfill(['spill', 'retry', 'rejected'], 'force')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([2, 1])
  expect(w.files.get(source)).toBe('')
  expect(doneMark(w.files, 'retry-20261013T200102003Z-earlier-1.jsonl')).toBe(true)
})

test('a file of what is left, written while its record could not be updated, is not replayed until the record has it: then its rows go once', async () => {
  let answer = FIRST_REFUSED_SECOND_AGAIN
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // The store takes the first two records; once the retry file of what is
  // left is written, no record can be kept anywhere.
  w.failStore({ from: 2 })
  w.refuseWrites(recordFile)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect(w.store.get(`replayed:${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`)).toMatchObject({ written: [true, false] })
  w.failStore({})
  w.refuseWrites(null)
  answer = FIRST_REFUSED
  // The next writer leaves that retry file waiting, finishes the source's
  // outcome (writing it again, unreplayed), then replays it.
  const next = w.writer('second-session')
  await next.backfill(['spill', 'retry'], 'auto')
  expect(w.sentOf(b)).toBe(1)
  await next.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b), w.copiesOf(a), w.copiesOf(b)]).toEqual([1, 2, 1, 1])
})

test('a replay whose answer comes after another session recorded and wrote out its own outcome goes on with that one: what is left is written once', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.refuseWrites((path, text) => path === source && text === '')
  // Both read the lease before either writes it; the first's answer waits.
  const { release } = bothSend(w)
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'force')
  expect(w.copiesOf(a)).toBe(1)
  release()
  await first
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([2, 1])
})

test('a file of what is left is replayed once its parent\'s record names other files, and waits while that record is cut off or cannot be read', async () => {
  const w = twoWriters(() => OK_INSERT)
  const parent = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const child = `/dl/${TABLE_REF}/retry-20261013T200103003Z-earlier-2.jsonl`
  w.files.set(child, packFiles([line('d'.repeat(32), 'retry')], parent)[0] as string)
  // The parent still holds its rows (another session leases it), and its
  // record is cut off part-way, with none whole: the file waits.
  w.files.set(`/dl/${TABLE_REF}/${parent}`, packFiles([line('e'.repeat(32), 'retry')])[0] as string)
  w.store.set(`lease:${TABLE_REF}/${parent}`, { by: 'another-session', until: Number.MAX_SAFE_INTEGER })
  w.files.set(`/dl/${TABLE_REF}/outcome-${parent}`, '{"fates":"W","refusa')
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual([])
  // The parent's record cannot be read: it still waits.
  w.failStore({ get: key => key.endsWith(`/${parent}`) })
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual([])
  // A whole record of another outcome (another replay's): the file goes.
  w.failStore({})
  w.store.set(`replayed:${TABLE_REF}/${parent}`, { size: new TextEncoder().encode(w.files.get(`/dl/${TABLE_REF}/${parent}`) ?? '').length, fates: 'W', reasons: [], refusals: [], files: [`/dl/${TABLE_REF}/retry-20261013T200104003Z-other-3.jsonl`], written: [true], waits: true, seq: 1 })
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['d'.repeat(32)])
  expect(w.files.get(child)).toBe('')
})

test('a replay whose answer comes after another session finished the file and emptied it writes nothing of its own, also once that session\'s mark is gone', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  const { release } = bothSend(w)
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  // The other session writes the rejected file, empties the source and lets
  // its record go, all before the first session's answer comes; the mark it
  // left beside the file is gone too, as a day later.
  await w.writer('second-session').backfill(['spill', 'retry'], 'force')
  expect([w.files.get(source), [...w.store.keys()]]).toEqual(['', []])
  for (const path of [...w.files.keys()].filter(path => path.includes('/outcome-'))) w.files.set(path, '')
  release()
  await first
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([2, 1])
})

test('a replay whose answer comes late, while the record it would adopt cannot be read, keeps its outcome until it can: what is left is still written once', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.refuseWrites((path, text) => path === source && text === '')
  const { release } = bothSend(w)
  const firstWriter = w.writer('first-session')
  const first = firstWriter.backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'force')
  w.failStore({ get: true })
  release()
  await first
  expect(w.copiesOf(a)).toBe(1)
  w.failStore({})
  w.hold(null)
  await firstWriter.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([2, 1])
})

test('while the source cannot be emptied, a file of what is left that its later record has as written is replayed', async () => {
  let answer = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 1, errors: [{ reason: 'backendError', message: 'try again' }] }] }) }
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // Records go to the two files beside the source; it cannot be emptied.
  w.failStore({ set: true })
  w.refuseWrites((path, text) => path === source && text === '')
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b)]).toEqual([1, 1])
  answer = OK_INSERT
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b)]).toEqual([1, 2])
})

test('a file of what is left whose parent was emptied goes, whatever an older record of the parent says; one its parent\'s done mark does not name is emptied unsent', async () => {
  const w = twoWriters(() => OK_INSERT)
  const parent = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const child = `/dl/${TABLE_REF}/retry-20261013T200103003Z-earlier-2.jsonl`
  const orphan = `/dl/${TABLE_REF}/retry-20261013T200104003Z-late-3.jsonl`
  w.files.set(`/dl/${TABLE_REF}/${parent}`, '')
  w.files.set(child, packFiles([line('d'.repeat(32), 'retry')], parent)[0] as string)
  // Left beside the emptied parent: an older record, which has the child
  // still to write (its later records, and the mark, were never kept).
  w.files.set(`/dl/${TABLE_REF}/outcome-b-${parent}`, sealFile(`${JSON.stringify({ size: 900, fates: 'W', reasons: [], refusals: [], files: [child], written: [false], waits: true, seq: 2 })}\n`))
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['d'.repeat(32)])
  // A record of the emptied parent cut off part-way says nothing either.
  const other = `/dl/${TABLE_REF}/retry-20261013T200105003Z-earlier-4.jsonl`
  w.files.set(`/dl/${TABLE_REF}/outcome-b-${parent}`, '{"size":900,"fates":"W","refu')
  w.files.set(other, packFiles([line('g'.repeat(32), 'retry')], parent)[0] as string)
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['d'.repeat(32), 'g'.repeat(32)])
  // The parent's done mark names the children only: a late copy of a file
  // another session's outcome left goes unsent.
  w.files.set(doneOf(parent), sealFile(`${JSON.stringify({ size: 900, fates: '', reasons: [], refusals: [], files: [child, other], written: [true, true], waits: true, seq: 3, retired: true })}\n`))
  w.files.set(orphan, packFiles([line('e'.repeat(32), 'retry')], parent)[0] as string)
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent).toEqual(['d'.repeat(32), 'g'.repeat(32)])
  expect(w.files.get(orphan)).toBe('')
})

test('a record write cut off before its first byte, with none whole, is not taken for none: another writer leaves the file waiting', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.tear((path, text) => recordFile(path) && text !== '', 0)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([...w.files].filter(([path]) => recordFile(path)).map(([, text]) => text)).toEqual([''])
  w.failStore({})
  w.tear(null)
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
})

test('a write of a file of what is left that lands after another session finished the outcome and replayed that file is emptied unsent', async () => {
  let answer = FIRST_REFUSED_SECOND_AGAIN
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // Both read the lease before either writes it. The first session's write
  // of the retry file waits until let go; the second session reads the
  // record once that write has begun, and finishes the outcome.
  let release = (): void => undefined
  let begun = (): void => undefined
  const atWrite = new Promise<void>(resolve => (begun = resolve))
  let waited = false
  w.holdWrites((session, path, text) => (session === 'first-session' && path !== source && path.includes('/retry-') && text !== '' ? (begun(), new Promise<void>(resolve => (release = resolve))) : undefined))
  w.holdGets((session, key) => (session === 'second-session' && key.startsWith('replayed:') && !waited ? ((waited = true), atWrite) : undefined))
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const second = w.writer('second-session')
  await second.backfill(['spill', 'retry'], 'auto')
  expect(w.files.get(source)).toBe('')
  answer = FIRST_REFUSED
  await second.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(b), w.copiesOf(b)]).toEqual([2, 1])
  release()
  await first
  await second.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(b), w.copiesOf(b)]).toEqual([2, 1])
})

test('an outcome whose first record lands after another session finished the file is not written out: it stops at the done mark', async () => {
  const w = twoWriters(() => FIRST_REFUSED_SECOND_AGAIN)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // Both read the lease before either writes it. The first session's first
  // record waits; the second session reads the record once that write has
  // begun (none is there yet), replays the file and finishes it.
  let release = (): void => undefined
  let begun = (): void => undefined
  const atRecord = new Promise<void>(resolve => (begun = resolve))
  let held = false
  let waited = false
  w.failStore({ set: true })
  w.holdWrites((session, path, text) => (session === 'first-session' && !held && path.includes('/outcome-') && text !== '' ? ((held = true), begun(), new Promise<void>(resolve => (release = resolve))) : undefined))
  w.holdGets((session, key) => (session === 'second-session' && key.startsWith('replayed:') && !waited ? ((waited = true), atRecord) : undefined))
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.files.get(source)).toBe('')
  const copies = [w.copiesOf(a), w.copiesOf(b)]
  release()
  await first
  expect([w.copiesOf(a), w.copiesOf(b)]).toEqual(copies)
  expect([...w.files].filter(([path, text]) => path.includes('/retry-') && path !== source && text !== '').length).toBe(1)
})

test('a record the store takes after another session finished the file with an outcome of its own is let go again', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const key = `replayed:${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  // The first session's record waits in the store's write.
  let begin = (): void => undefined
  let release = (): void => undefined
  const begun = new Promise<void>(resolve => (begin = resolve))
  const released = new Promise<void>(resolve => (release = resolve))
  let held = false
  w.holdSets((session, k) => {
    if (session !== 'first-session' || held || !k.startsWith('replayed:')) return undefined
    held = true
    begin()
    return released
  })
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await begun
  // Its lease runs out, and /bqaa-backfill in another session replays the
  // file afresh and finishes it.
  w.store.delete(`lease:${TABLE_REF}/${name}`)
  await w.writer('second-session').backfill(['spill', 'retry', 'rejected'], 'force')
  expect([w.files.get(source), doneMark(w.files, name), w.store.has(key)]).toEqual(['', true, false])
  release()
  await first
  expect([w.store.has(key), w.sentOf(a), w.copiesOf(a)]).toEqual([false, 2, 1])
})

test('where the listing gives no times, the records of a file done more than a day ago are emptied by when its done mark says it was done', async () => {
  const config = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  const dir = `/dl/${TABLE_REF}`
  const source = `${dir}/retry-20261013T200102003Z-earlier-1.jsonl`
  const files = new Map<string, string>([[source, packFiles([line('a'.repeat(32), 'retry')])[0] as string]])
  const store = new Map<string, unknown>()
  let now = 0
  const io: Io = {
    now: () => now,
    after: () => ({ cancel: () => undefined }),
    fetch: async () => OK_INSERT,
    runCommand: async () => ({ exitCode: 0, stdout: `${'a'.repeat(24)}\n`, stderr: '' }),
    envToken: async () => undefined,
    writeFile: async (path, text) => void files.set(path, text),
    readFile: async path => files.get(path) ?? '',
    exists: async path => files.has(path),
    listDir: async d => [...files].filter(([path]) => path.startsWith(`${d}/`)).map(([path, text]) => ({ name: path.slice(d.length + 1), kind: 'file', size: new TextEncoder().encode(text).length })),
    storeGet: async key => store.get(key),
    storeSet: async (key, value) => void store.set(key, JSON.parse(JSON.stringify(value)) as unknown),
    storeDelete: async key => void store.delete(key),
    status: () => undefined,
    debug: () => undefined,
  }
  const writer = new Writer(io, () => config, () => 'a-session')
  const kept = (): string[] => [...files].filter(([path, text]) => path.includes('/outcome-') && text !== '').map(([path]) => path)
  await writer.backfill(['spill', 'retry'], 'auto')
  expect([files.get(source), kept().length]).toEqual(['', 1])
  // Half a day on: kept. Two days on: emptied.
  now = 12 * 60 * 60 * 1000
  await writer.backfill(['spill', 'retry'], 'auto')
  expect(kept().length).toBe(1)
  now = 2 * 24 * 60 * 60 * 1000
  await writer.backfill(['spill', 'retry'], 'auto')
  expect(kept()).toEqual([])
})

test('the records of files emptied more than a day ago are emptied; newer ones, and those of files still there, stay', async () => {
  const w = twoWriters(() => OK_INSERT)
  const dir = `/dl/${TABLE_REF}`
  const old = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const recent = 'retry-20261013T200102003Z-earlier-2.jsonl'
  const live = 'retry-20261013T200102003Z-earlier-3.jsonl'
  const mark = sealFile(`${JSON.stringify({ size: 9, fates: '', reasons: [], refusals: [], files: [], written: [], waits: false, seq: 2, retired: true })}\n`)
  w.files.set(`${dir}/${old}`, '')
  w.files.set(`${dir}/${recent}`, '')
  w.files.set(`${dir}/${live}`, packFiles([line('f'.repeat(32), 'retry')])[0] as string)
  for (const name of [old, recent, live]) w.files.set(`${dir}/outcome-${name}`, mark)
  w.mtimes.set(`${dir}/outcome-${old}`, -25 * 60 * 60 * 1000)
  w.mtimes.set(`${dir}/outcome-${recent}`, -60 * 60 * 1000)
  w.mtimes.set(`${dir}/outcome-${live}`, -25 * 60 * 60 * 1000)
  w.store.set(`lease:${TABLE_REF}/${live}`, { by: 'another-session', until: Number.MAX_SAFE_INTEGER })
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect([w.files.get(`${dir}/outcome-${old}`), w.files.get(`${dir}/outcome-${recent}`), w.files.get(`${dir}/outcome-${live}`)]).toEqual(['', mark, mark])
})

for (const where of ['the store', 'the files beside it']) {
  test(`a writer back from reading the file to resume its outcome, after another writer finished the file and /bqaa-backfill sent what it left, writes nothing (records in ${where}), and leaves the mark that the file was done`, async () => {
    let answer = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'backendError', message: 'try again' }] }, { index: 1, errors: [{ reason: 'invalid', message: 'no such field: extra' }] }] }) }
    const w = twoWriters(() => answer)
    if (where !== 'the store') w.failStore({ set: true })
    const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
    const source = `/dl/${TABLE_REF}/${name}`
    const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
    w.files.set(source, packFiles([line(b, 'retry'), line(a, 'retry')])[0] as string)
    // A first replay writes the retry file of what is left, and cannot write
    // the rejected one.
    w.refuse(path => path.includes('/rejected-'))
    await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
    w.refuse(null)
    answer = OK_INSERT
    // A second writer reads the file to resume that outcome, and waits there;
    // its lease runs out.
    let release = (): void => undefined
    let begun = (): void => undefined
    const atRead = new Promise<void>(resolve => (begun = resolve))
    let heldFirst = false
    w.holdReads((session, path) => (session === 'paused-session' && path === source && !heldFirst ? ((heldFirst = true), begun(), new Promise<void>(resolve => (release = resolve))) : undefined))
    const paused = w.writer('paused-session').backfill(['spill', 'retry'], 'auto')
    await atRead
    w.store.delete(`lease:${TABLE_REF}/${name}`)
    // A third finishes the outcome, and /bqaa-backfill sends what it left.
    await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
    expect(w.files.get(source)).toBe('')
    await w.writer('third-session').backfill(['spill', 'retry', 'rejected'], 'command')
    const leftover = () => [...w.files].filter(([path, text]) => path !== source && /\/(retry|rejected)-/.test(path) && text !== '').length
    expect([w.sentOf(a), w.sentOf(b), leftover()]).toEqual([2, 2, 0])
    release()
    await paused
    await w.writer('third-session').backfill(['spill', 'retry', 'rejected'], 'command')
    expect([w.sentOf(a), w.sentOf(b), leftover()]).toEqual([2, 2, 0])
    expect(doneMark(w.files, name)).toBe(true)
  })
}

test('a writer that resumes an outcome its record already has whole records nothing again before it finishes the file', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  w.files.set(source, packFiles([line('a'.repeat(32), 'retry')])[0] as string)
  w.refuseWrites((path, text) => path === source && text === '')
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const writes = w.recordWrites()
  w.refuseWrites(null)
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.files.get(source)).toBe('')
  expect(w.recordWrites()).toBe(writes)
})

test('a writer that takes over from the one before it in the same session holds what that one held: an outcome no record took is not sent again', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  const before = w.writer('a-session')
  await before.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  const after = w.writer('a-session')
  after.adopt(before)
  await after.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  w.failStore({})
  w.refuseWrites(null)
  await after.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
})

for (const read of ['part-written', 'written again, longer', 'changed in place, its seal not matching'] as const) {
  test(`a file whose outcome is recorded, read ${read} after the listing, keeps its record and waits: once it reads whole, the next replay resumes it, sending none of it`, async () => {
    const w = twoWriters(() => OK_INSERT)
    const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
    const source = `/dl/${TABLE_REF}/${name}`
    const a = 'a'.repeat(32)
    const text = packFiles([line(a, 'retry')])[0] as string
    w.files.set(source, text)
    // A replay's outcome: the row refused, its rejected copy not written yet.
    const rejected = `/dl/${TABLE_REF}/rejected-20261013T200103003Z-earlier-2.jsonl`
    w.store.set(`replayed:${TABLE_REF}/${name}`, { size: new TextEncoder().encode(text).length, fates: 'R', reasons: ['invalid: no such field: extra'], refusals: [0], files: [rejected], written: [false], waits: false, seq: 1 })
    // Its first read after the listing finds a write under way over it.
    const other = read === 'part-written' ? text.slice(0, Math.floor(text.length / 2)) : read === 'written again, longer' ? (packFiles([line(a, 'retry'), line('c'.repeat(32), 'retry')])[0] as string) : text.replace(a, 'b'.repeat(32))
    expect(other === text).toBe(false)
    let swapped = false
    w.holdReads((_, path) => {
      if (path !== source || swapped) return undefined
      swapped = true
      w.files.set(source, other)
      return undefined
    })
    await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
    expect([w.sentOf(a), w.store.has(`replayed:${TABLE_REF}/${name}`), w.files.get(rejected) ?? '']).toEqual([0, true, ''])
    w.files.set(source, text)
    await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
    expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([0, 1, ''])
  })
}

test('a batch the writer before had out when the next writer took over, asked for again, goes with the next writer\'s rows', async () => {
  let calls = 0
  const w = twoWriters(() => (++calls === 1 ? { status: 503, text: 'backend error' } : OK_INSERT))
  const a = 'a'.repeat(32)
  const before = w.writer('a-session')
  before.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  let held = false
  w.hold(() => {
    if (held) return undefined
    held = true
    asked()
    return answered
  })
  const flushing = before.flush()
  await atRequest
  const after = w.writer('a-session')
  after.adopt(before)
  answer()
  await flushing
  expect(w.sentOf(a)).toBe(1)
  await after.flush()
  expect(w.sentOf(a)).toBe(2)
})

test('a batch the first writer had out, through two takeovers, asked for again, goes with the last writer\'s rows', async () => {
  let calls = 0
  const w = twoWriters(() => (++calls === 1 ? { status: 503, text: 'backend error' } : OK_INSERT))
  const a = 'a'.repeat(32)
  const first = w.writer('a-session')
  first.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  let held = false
  w.hold(() => {
    if (held) return undefined
    held = true
    asked()
    return answered
  })
  const flushing = first.flush()
  await atRequest
  const second = w.writer('a-session')
  second.adopt(first)
  const third = w.writer('a-session')
  third.adopt(second)
  answer()
  await flushing
  expect([w.sentOf(a), third.queue.length]).toEqual([1, 1])
  await third.flush()
  expect([w.sentOf(a), third.queue.length]).toEqual([2, 0])
})

test('rows a writer parks after the next one took over, its batch out of retries, set the next writer\'s paced replays going', async () => {
  let failing = true
  const w = twoWriters(() => (failing ? { status: 503, text: 'backend error' } : OK_INSERT))
  const a = 'a'.repeat(32)
  const before = w.writer('a-session')
  before.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  // Asked for again as often as a row may be: the next answer like it parks it.
  const queued = before.queue.takeAll()
  for (const q of queued) q.attempts = resolveConfig({}, {}).maxRetries
  before.queue.unshift(queued)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(() => {
    w.hold(null)
    asked()
    return answered
  })
  const flushing = before.flush()
  await atRequest
  const after = w.writer('a-session')
  after.adopt(before)
  // Its own scan of what is parked finds nothing yet.
  await after.backfill(['spill', 'retry'], 'auto')
  answer()
  await flushing
  const parkedRows = (): number => [...w.files].filter(([path, text]) => path.includes('/retry-') && text !== '').flatMap(([, text]) => parseFile(text).lines).filter(l => l.row.event_id === a).length
  expect([w.sentOf(a), parkedRows()]).toEqual([1, 1])
  failing = false
  await after.flush()
  expect([w.sentOf(a), parkedRows()]).toEqual([2, 0])
})

test('rows a writer parks while the next writer\'s own replay is under way keep the table due: that replay, ending with nothing left, does not take them for read', async () => {
  let failing = true
  const w = twoWriters(() => (failing ? { status: 503, text: 'backend error' } : OK_INSERT))
  const a = 'a'.repeat(32)
  const s = 's'.repeat(32)
  const before = w.writer('a-session')
  before.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  const queued = before.queue.takeAll()
  for (const q of queued) q.attempts = resolveConfig({}, {}).maxRetries
  before.queue.unshift(queued)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(() => {
    w.hold(null)
    asked()
    return answered
  })
  const flushing = before.flush()
  await atRequest
  const after = w.writer('a-session')
  after.adopt(before)
  // The next writer's replay reads a file parked before; its read waits.
  const spill = `/dl/${TABLE_REF}/spill-20261013T200102003Z-earlier-1.jsonl`
  w.files.set(spill, packFiles([line(s, 'spill')])[0] as string)
  let readReached = (): void => undefined
  let releaseRead = (): void => undefined
  const atRead = new Promise<void>(resolve => (readReached = resolve))
  const read = new Promise<void>(resolve => (releaseRead = resolve))
  w.holdReads((_, path) => {
    if (path !== spill) return undefined
    w.holdReads(null)
    readReached()
    return read
  })
  const replaying = after.backfill(['spill', 'retry'], 'auto')
  await atRead
  // The writer before parks its row meanwhile, its batch out of retries.
  answer()
  await flushing
  const parkedRows = (): number => [...w.files].filter(([path, text]) => path.includes('/retry-') && text !== '').flatMap(([, text]) => parseFile(text).lines).filter(l => l.row.event_id === a).length
  expect(parkedRows()).toBe(1)
  failing = false
  releaseRead()
  await replaying
  expect([w.sentOf(s), w.files.get(spill)]).toEqual([1, ''])
  await after.flush()
  expect([w.sentOf(a), parkedRows()]).toEqual([2, 0])
})

test('a writer that takes over holds the queue it takes to its own bounds', async () => {
  const w = twoWriters(() => OK_INSERT)
  const before = w.writer('a-session')
  const ids = Array.from({ length: 12 }, (_, i) => i.toString(16).padStart(32, '0'))
  for (const id of ids) before.enqueue((JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  const after = w.writer('a-session', resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, { BQAA_MAX_QUEUE_ROWS: '10' }))
  after.adopt(before)
  expect(after.queue.length).toBe(10)
  await after.flush()
  expect(ids.map(id => w.sentOf(id))).toEqual([0, 0, ...ids.slice(2).map(() => 1)])
})

test('rows the next writer had queued when it took over go behind the older rows it takes', async () => {
  const w = twoWriters(() => OK_INSERT)
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const before = w.writer('a-session')
  before.enqueue(rowOf(a), 0)
  const after = w.writer('a-session')
  after.enqueue(rowOf(b), 0)
  after.adopt(before)
  await after.flush()
  expect(w.sent.filter(id => id === a || id === b)).toEqual([a, b])
})

test('the exit spill after a takeover takes the batch the writer before still has out, and that writer starts nothing more', async () => {
  const w = twoWriters(() => OK_INSERT)
  const [a, d] = ['a'.repeat(32), 'd'.repeat(32)]
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const before = w.writer('a-session')
  before.enqueue(rowOf(a), 0)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(() => {
    w.hold(null)
    asked()
    return answered
  })
  const flushing = before.flush()
  await atRequest
  const after = w.writer('a-session')
  after.adopt(before)
  expect(await after.spillForExit(1000)).toEqual({ spilled: 1 })
  const spilled = (id: string): number => [...w.files].filter(([path, text]) => path.includes('/spill-') && text !== '').flatMap(([, text]) => parseFile(text).lines).filter(l => l.row.event_id === id).length
  expect(spilled(a)).toBe(1)
  // A row captured after: neither writer sends it.
  after.queue.push(rowOf(d))
  answer()
  await flushing
  expect([w.sentOf(a), w.sentOf(d)]).toEqual([1, 0])
})

// The host ends the process as soon as session.end returns: `exiting` ends
// the session's process once the exit returns, and the yields give an exit
// that does not wait the chance to return before the held write is let go.
const exitOf = (w: ReturnType<typeof twoWriters>, writer: Writer, session = 'a-session') => {
  let returned = false
  const exiting = writer.spillForExit(1000).then(result => {
    returned = true
    w.exit(session)
    return result
  })
  return { exiting, returned: () => returned }
}
const yields = async (): Promise<void> => {
  for (let i = 0; i < 100; i += 1) await Promise.resolve()
}
// Writes to the files `f` picks wait, until `release()`.
const holdFile = (w: ReturnType<typeof twoWriters>, f: (path: string) => boolean) => {
  let reached = (): void => undefined
  let release = (): void => undefined
  const atWrite = new Promise<void>(resolve => (reached = resolve))
  const held = new Promise<void>(resolve => (release = resolve))
  w.holdWrites((_, path, text) => {
    if (!f(path) || text === '') return undefined
    w.holdWrites(null)
    reached()
    return held
  })
  return { atWrite, release }
}
const linesIn = (w: ReturnType<typeof twoWriters>, kind: 'spill' | 'retry' | 'rejected', id: string): number =>
  [...w.files].filter(([path, text]) => path.includes(`/${kind}-`) && text !== '').flatMap(([, text]) => parseFile(text).lines).filter(l => l.row.event_id === id).length

for (const kind of ['refused', 'parked after their retries'] as const) {
  test(`the exit after a takeover, with nothing left to spill, waits for the file the batch out's rows ${kind} are being written to, and makes no second copy of them`, async () => {
    const w = twoWriters(() => (kind === 'refused' ? FIRST_REFUSED : FIRST_AGAIN))
    const a = 'a'.repeat(32)
    const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
    const before = w.writer('a-session')
    before.enqueue(rowOf(a), 0)
    if (kind !== 'refused') {
      const queued = before.queue.takeAll()
      for (const q of queued) q.attempts = resolveConfig({}, {}).maxRetries
      before.queue.unshift(queued)
    }
    // The file the answer sends the row to: its write waits.
    const file = holdFile(w, path => path.includes(kind === 'refused' ? '/rejected-' : '/retry-'))
    void before.flush()
    await file.atWrite
    const after = w.writer('a-session')
    after.adopt(before)
    const exit = exitOf(w, after)
    await yields()
    expect(exit.returned()).toBe(false)
    file.release()
    expect(await exit.exiting).toEqual({ spilled: 0 })
    if (kind === 'refused') expect([linesIn(w, 'rejected', a), linesIn(w, 'spill', a), linesIn(w, 'retry', a)]).toEqual([1, 0, 0])
    else expect([linesIn(w, 'retry', a), linesIn(w, 'spill', a)]).toEqual([1, 0])
  })
}

test('the exit after a takeover waits for a file of refused rows the writer before began after the takeover', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const a = 'a'.repeat(32)
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const before = w.writer('a-session')
  before.enqueue(rowOf(a), 0)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(() => {
    w.hold(null)
    asked()
    return answered
  })
  void before.flush()
  await atRequest
  const after = w.writer('a-session')
  after.adopt(before)
  // The answer comes after the takeover: its refused row's write waits.
  const file = holdFile(w, path => path.includes('/rejected-'))
  answer()
  await file.atWrite
  const exit = exitOf(w, after)
  await yields()
  expect(exit.returned()).toBe(false)
  file.release()
  expect(await exit.exiting).toEqual({ spilled: 0 })
  expect([linesIn(w, 'rejected', a), linesIn(w, 'spill', a)]).toEqual([1, 0])
})

test('the exit waits for a file of refused rows a writer began before it took over', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const b = 'b'.repeat(32)
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const before = w.writer('a-session')
  const after = w.writer('a-session')
  after.enqueue(rowOf(b), 0)
  const file = holdFile(w, path => path.includes('/rejected-'))
  void after.flush()
  await file.atWrite
  after.adopt(before)
  const exit = exitOf(w, after)
  await yields()
  expect(exit.returned()).toBe(false)
  file.release()
  expect(await exit.exiting).toEqual({ spilled: 0 })
  expect([linesIn(w, 'rejected', b), linesIn(w, 'spill', b)]).toEqual([1, 0])
})

test('with nothing to spill, the exit waits for a file of refused rows still being written no longer than its bound', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const a = 'a'.repeat(32)
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const writer = w.writer('a-session')
  writer.enqueue(rowOf(a), 0)
  const file = holdFile(w, path => path.includes('/rejected-'))
  void writer.flush()
  await file.atWrite
  const exit = exitOf(w, writer)
  w.fire(999)
  await yields()
  expect(exit.returned()).toBe(false)
  w.fire(1000)
  expect(await exit.exiting).toEqual({ spilled: 0 })
  // The process has ended: the write lands nowhere.
  file.release()
  await yields()
  expect(linesIn(w, 'rejected', a)).toBe(0)
})

test('the exit while a batch\'s refused rows are still being written spills those its answer asked for again, none it landed or refused, and waits for the refused rows\' file', async () => {
  const [l, r, x] = ['1'.repeat(32), '2'.repeat(32), '3'.repeat(32)]
  const mixed = { status: 200, text: JSON.stringify({ insertErrors: [{ index: 1, errors: [{ reason: 'invalid', message: 'no such field: extra' }] }, { index: 2, errors: [{ reason: 'backendError', message: 'try again' }] }] }) }
  let calls = 0
  const w = twoWriters(() => (++calls === 1 ? mixed : OK_INSERT))
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const writer = w.writer('a-session')
  for (const id of [l, r, x]) writer.enqueue(rowOf(id), 0)
  const file = holdFile(w, path => path.includes('/rejected-'))
  void writer.flush()
  await file.atWrite
  const exit = exitOf(w, writer)
  await yields()
  expect(exit.returned()).toBe(false)
  file.release()
  expect(await exit.exiting).toEqual({ spilled: 1 })
  expect([l, r, x].map(id => [linesIn(w, 'spill', id), linesIn(w, 'rejected', id)])).toEqual([[0, 0], [0, 1], [1, 0]])
  await w.writer('next-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(l), w.sentOf(r), w.sentOf(x), w.copiesOf(r)]).toEqual([1, 1, 2, 1])
})

test('a status line the writer before sets after the next one took over is cleared by the next writer\'s landing', async () => {
  let calls = 0
  const w = twoWriters(() => (++calls === 1 ? DATASET_MISSING : OK_INSERT))
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const before = w.writer('a-session')
  before.enqueue(rowOf(a), 0)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(() => {
    w.hold(null)
    asked()
    return answered
  })
  const flushing = before.flush()
  await atRequest
  const after = w.writer('a-session')
  after.adopt(before)
  after.clearStatus()
  // The writer before learns of the missing dataset now, and says so.
  answer()
  await flushing
  expect(w.statuses.at(-1) ?? '').toMatch(/cannot find the dataset/)
  after.enqueue(rowOf(b), 0)
  await after.flush()
  expect([w.sentOf(b), w.statuses.at(-1)]).toEqual([1, undefined])
})

test('a status line the writer before set, cleared at session.start, is said again by the writer that takes over when the same problem stands', async () => {
  const w = twoWriters(() => DATASET_MISSING)
  const a = 'a'.repeat(32)
  const before = w.writer('a-session')
  before.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  await before.flush()
  const said = w.statuses.at(-1)
  expect(said ?? '').toMatch(/cannot find the dataset/)
  // session.start's writer takes over, then clears the host's line (register.ts).
  const after = w.writer('a-session')
  after.adopt(before)
  after.clearStatus()
  expect(w.statuses.at(-1)).toBeUndefined()
  await after.flush()
  expect([w.sentOf(a), w.statuses.at(-1)]).toEqual([2, said])
})

test('a status line the surface does not take leaves the flush going, and the same line is written at the next try', async () => {
  const w = twoWriters(() => DATASET_MISSING)
  const a = 'a'.repeat(32)
  const writer = w.writer('a-session')
  writer.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  w.failStatus(() => true)
  await writer.flush()
  expect([w.sentOf(a), w.statuses, writer.lastError?.message ?? '']).toEqual([1, [], expect.stringMatching(/cannot find the dataset|Not found: Dataset/)])
  w.failStatus(null)
  writer.retrySoon()
  await writer.flush()
  expect([w.sentOf(a), w.statuses.at(-1) ?? '']).toEqual([2, expect.stringMatching(/cannot find the dataset/)])
})

test('a debug line the surface does not take leaves the flush going: the rows asked for again stay queued', async () => {
  const w = twoWriters(() => DATASET_MISSING)
  const a = 'a'.repeat(32)
  const writer = w.writer('a-session')
  writer.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  w.failDebug(true)
  await writer.flush()
  expect([w.sentOf(a), writer.queue.length, w.statuses.at(-1) ?? '']).toEqual([1, 1, expect.stringMatching(/cannot find the dataset/)])
})

test('a status line clear at session.start that the surface does not take is made by the next landing', async () => {
  const w = twoWriters(() => OK_INSERT)
  const a = 'a'.repeat(32)
  const writer = w.writer('a-session')
  w.failStatus(text => text === undefined)
  writer.clearStatus()
  w.failStatus(null)
  writer.enqueue((JSON.parse(line(a, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row, 0)
  await writer.flush()
  expect([w.sentOf(a), w.statuses]).toEqual([1, [undefined]])
})

test('rows a writer queued before it took over, behind a queue under smaller bounds, are held to its own larger bounds', async () => {
  const w = twoWriters(() => OK_INSERT)
  const rowOf = (id: string) => (JSON.parse(line(id, 'retry')) as { row: Parameters<Writer['enqueue']>[0] }).row
  const small = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, { BQAA_MAX_QUEUE_ROWS: '10' })
  const before = w.writer('a-session', small)
  for (let i = 0; i < 8; i += 1) before.enqueue(rowOf(`b${i}`.padStart(32, '0')), 0)
  const after = w.writer('a-session')
  for (let i = 0; i < 5; i += 1) after.enqueue(rowOf(`a${i}`.padStart(32, '0')), 0)
  after.adopt(before)
  expect(after.queue.length).toBe(13)
})

test('rows with no id alike in two files count as two refused', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const noId = JSON.parse(line('c'.repeat(32), 'retry')) as { row: { event_id?: string } }
  delete noId.row.event_id
  const text = packFiles([JSON.stringify(noId)])[0] as string
  w.files.set(`/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`, text)
  w.files.set(`/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-2.jsonl`, text)
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect(w.statuses.at(-1) ?? '').toMatch(/^BQAA: 2 row\(s\) refused /)
})

test('a replay record too large for the store goes to the file beside the source, which another writer resumes from', async () => {
  // Rows as small as a line can be, each refused for one of many reasons.
  const small = (id: string): string => JSON.stringify({ v: 1, kind: 'retry', reason: '', attempts: 0, failed_at: '2026-10-13T20:01:02.003Z', table: TABLE_REF, row: { timestamp: '2026-10-13T20:01:02.003Z', event_type: 'TEST', event_id: id } })
  const ids = Array.from({ length: 13_000 }, (_, n) => n.toString(16).padStart(8, '0'))
  const w = twoWriters(chunk => ({ status: 200, text: JSON.stringify({ insertErrors: chunk.map((id, index) => ({ index, errors: [{ reason: 'invalid', message: `reason ${Number.parseInt(id, 16) % 1000}: ${'x'.repeat(980)}` }] })) }) }))
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const texts = packFiles(ids.map(small))
  expect(texts.length).toBe(1)
  w.files.set(source, texts[0] as string)
  w.refuseWrites((path, text) => path === source && text === '')
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent.length).toBe(13_000)
  expect(w.store.has(`replayed:${TABLE_REF}/${name}`)).toBe(false)
  const filed = parseFile(w.files.get(`/dl/${TABLE_REF}/outcome-${name}`) ?? '')
  expect(filed.complete).toBe(true)
  expect(new TextEncoder().encode(filed.unreadable[0] ?? '').length > 64 * 1024).toBe(true)
  w.refuseWrites(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sent.length).toBe(13_000)
  expect([w.files.get(source), doneMark(w.files, name)]).toEqual(['', true])
})

test('rows with no id, in a file another writer made, each count once refused, however often /bqaa-backfill sends them', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const noId = (n: number): string => {
    const parsed = JSON.parse(line(String(n).repeat(32), 'rejected')) as { row: { event_id?: string } }
    delete parsed.row.event_id
    return JSON.stringify(parsed)
  }
  world.files.set(`${PARTITION_DIR}/rejected-20261013T200102003Z-earlier-1.jsonl`, packFiles([noId(1), noId(2)])[0] as string)
  await startSession($)
  await world.clock.advance(5000)
  world.replies = [{ status: 200, text: JSON.stringify({ insertErrors: [0, 1].map(index => ({ index, errors: [{ reason: 'invalid', message: 'no such field: extra' }] })) }) }]
  // Run again, BigQuery still refusing them: each still counts once.
  for (let i = 0; i < 2; i += 1) {
    await $.command.run({ command: 'bqaa-backfill', args: '', ...COMMAND })
    await world.clock.advance(1000)
    expect(world.statusLines.at(-1) ?? '').toMatch(/^BQAA: 2 row\(s\) refused /)
  }
})

test('a record another session writes late, beside a file finished meanwhile, never stands over the mark that it was done: none of what it left is written, and nothing goes again', async () => {
  let answer = FIRST_AGAIN
  const w = twoWriters(() => answer)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // The first session's record goes to the file beside the source (the store
  // takes none), and waits there. Then the store takes records again, and
  // the second session, its mark held until then, has row a refused.
  w.failStore({ set: true })
  let letGo = (): void => undefined
  let begun = (): void => undefined
  const atRecord = new Promise<void>(resolve => (begun = resolve))
  let held = false
  w.holdWrites((session, path, text) => {
    if (session === 'second-session' && path.includes('/outcome-sending-')) return atRecord
    if (session !== 'first-session' || held || !recordFile(path) || text === '') return undefined
    held = true
    w.failStore({})
    answer = FIRST_REFUSED
    begun()
    return new Promise<void>(resolve => (letGo = resolve))
  })
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const second = w.writer('second-session')
  // As /bqaa-backfill force, which goes past the first session's mark.
  await second.backfill(['spill', 'retry'], 'force')
  expect([w.files.get(source), doneMark(w.files, name), w.copiesOf(a)]).toEqual(['', true, 1])
  letGo()
  await first
  await second.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b), w.copiesOf(a)]).toEqual([2, 2, 1])
  expect([...w.files].filter(([path, text]) => path.includes('/retry-') && text !== '').length).toBe(0)
  expect(doneMark(w.files, name)).toBe(true)
})

test('a replay whose mark lands after another session finished the file sends none of it', async () => {
  let answer = FIRST_AGAIN
  const w = twoWriters(() => answer)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry'), line('b'.repeat(32), 'retry')])[0] as string)
  let letGo = (): void => undefined
  let begun = (): void => undefined
  const atMark = new Promise<void>(resolve => (begun = resolve))
  let held = false
  w.holdWrites((session, path) => {
    if (session !== 'first-session' || held || !path.includes('/outcome-sending-')) return undefined
    held = true
    answer = FIRST_REFUSED
    begun()
    return new Promise<void>(resolve => (letGo = resolve))
  })
  w.holdGets((session, key) => (session === 'second-session' && key.startsWith('replayed:') ? atMark : undefined))
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const second = w.writer('second-session')
  await second.backfill(['spill', 'retry'], 'auto')
  expect([w.files.get(source), w.sentOf(a), w.copiesOf(a)]).toEqual(['', 1, 1])
  letGo()
  await first
  await second.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
})

test('a replay that sent rows and can record its outcome nowhere leaves its mark: writers of other sessions send none of the file, and the writer that holds the outcome writes it once it can', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  const first = w.writer('first-session')
  await first.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  for (const session of ['second-session', 'third-session']) {
    const report = await w.writer(session).backfill(['spill', 'retry'], 'auto')
    expect(report.held ?? '').toMatch(/^a replay of retry-20261013T200102003Z-earlier-1\.jsonl sent rows and has recorded no outcome; if that session stopped, \/bqaa-backfill force replays the file afresh/)
    expect(report.skippedFiles).toBe(1)
  }
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  w.failStore({})
  w.refuseWrites(null)
  await first.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, ''])
})

test('a file whose replay left its mark and no outcome, its writer gone, waits for /bqaa-backfill too, and /bqaa-backfill force replays it afresh', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  w.failStore({})
  w.refuseWrites(null)
  for (const trigger of ['auto', 'command'] as const) {
    const report = await w.writer('second-session').backfill(['spill', 'retry', 'rejected'], trigger)
    expect([w.sentOf(a), w.copiesOf(a), report.skippedFiles]).toEqual([1, 0, 1])
  }
  await w.writer('second-session').backfill(['spill', 'retry', 'rejected'], 'force')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([2, 1, ''])
})

test('a replay that changes nothing (its rows only asked for again) lets its mark go: another session replays the file by itself', async () => {
  const w = twoWriters(() => FIRST_AGAIN)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry')])[0] as string
  w.files.set(source, text)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.files.get(source), [...w.files].filter(([path, text]) => path.includes('/outcome-sending-') && text !== '').length]).toEqual([1, text, 0])
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sentOf(a)).toBe(2)
})

test('a mark its writer could not let go is that writer\'s own: it replays the file again; another session, and the writer that takes over, wait', async () => {
  const w = twoWriters(() => FIRST_AGAIN)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.refuseWrites((path, text) => path.includes('/outcome-sending-') && text === '')
  const first = w.writer('first-session')
  await first.backfill(['spill', 'retry'], 'auto')
  await first.backfill(['spill', 'retry'], 'auto')
  expect(w.sentOf(a)).toBe(2)
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sentOf(a)).toBe(2)
  // The writer that takes over in the same session holds no outcome of the
  // mark's: it waits as another session does, and /bqaa-backfill sends it.
  const after = w.writer('first-session')
  after.adopt(first)
  const report = await after.backfill(['spill', 'retry'], 'auto')
  expect(report.held ?? '').toMatch(/sent rows and has recorded no outcome/)
  expect(w.sentOf(a)).toBe(2)
  await after.backfill(['spill', 'retry'], 'force')
  expect(w.sentOf(a)).toBe(3)
})

test('the writer that takes over while a replay is under way holds the outcome that replay comes to: it sends none of the file again, and writes the outcome out', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  // Its record can be written nowhere: the outcome stays in the writer.
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  let begin = (): void => undefined
  let release = (): void => undefined
  const begun = new Promise<void>(resolve => (begin = resolve))
  const released = new Promise<void>(resolve => (release = resolve))
  let held = false
  w.hold(() => {
    if (held) return undefined
    held = true
    begin()
    return released
  })
  const previous = w.writer('a-session')
  const under = previous.backfill(['spill', 'retry'], 'auto')
  await begun
  const after = w.writer('a-session')
  after.adopt(previous)
  release()
  await under
  // Nothing of what it left is written before its record is.
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source) !== '']).toEqual([1, 0, true])
  await after.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  w.failStore({})
  w.refuseWrites(null)
  await after.backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, ''])
})

test('a file listed part-written and read whole waits for the next replay: its record and marks go by the size the listing gave', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry')])[0] as string
  w.files.set(source, text.slice(0, 100))
  // The write finishes after the listing, before the read.
  let grown = false
  w.holdReads((_, path) => {
    if (path === source && !grown) {
      grown = true
      w.files.set(source, text)
    }
    return undefined
  })
  w.refuseWrites((path, value) => path === source && value === '')
  const report = await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect(report.held ?? '').toMatch(new RegExp(`^${name.replace(/\./g, '\\.')} changed while it was read; it waits for the next replay`))
  expect([report.skippedFiles, report.error, w.sentOf(a), w.store.get(`replayed:${TABLE_REF}/${name}`)]).toEqual([1, null, 0, undefined])
  // Listed whole: sent once, and its record fits it, the file not emptied.
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, text])
  w.refuseWrites(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, ''])
})

test('a late copy of a finished file is emptied with its done mark left as it was, the emptying failing or not', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry')])[0] as string
  w.files.set(source, text)
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const marks = doneTexts(w.files, name)
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source), doneMark(w.files, name), w.store.size, Object.keys(marks).length]).toEqual([1, 1, '', true, 0, 1])
  // The late copy, and every write of a done mark cut off part-way.
  w.files.set(source, text)
  w.tear(path => path.includes('/outcome-done-'))
  w.refuseWrites((path, value) => path === source && value === '')
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), doneTexts(w.files, name), w.files.get(source)]).toEqual([1, marks, text])
  w.tear(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), doneTexts(w.files, name)]).toEqual([1, 1, marks])
  w.refuseWrites(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source), doneTexts(w.files, name)]).toEqual([1, 1, '', marks])
})

test('a done mark another session writes while this writer writes the same outcome out stands as it was: this writer never writes it again', async () => {
  const w = twoWriters(() => FIRST_REFUSED_SECOND_AGAIN)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  let begin = (): void => undefined
  let release = (): void => undefined
  const begun = new Promise<void>(resolve => (begin = resolve))
  const released = new Promise<void>(resolve => (release = resolve))
  let held = false
  // The first session's first write of what is left waits.
  w.holdWrites((session, path, text) => {
    if (session !== 'first-session' || held || path === source || text === '' || !/\/(retry|rejected)-/.test(path)) return undefined
    held = true
    begin()
    return released
  })
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await begun
  // Its lease runs out, and another session finishes the outcome meanwhile.
  w.store.delete(`lease:${TABLE_REF}/${name}`)
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  const marks = doneTexts(w.files, name)
  expect([w.files.get(source), doneMark(w.files, name), w.sentOf(a), w.sentOf(b)]).toEqual(['', true, 1, 1])
  // Every write of a done mark is cut off from here.
  w.tear(path => path.includes('/outcome-done-'))
  release()
  await first
  expect([doneTexts(w.files, name), w.sentOf(a), w.sentOf(b), w.copiesOf(a)]).toEqual([marks, 1, 1, 1])
})

test('/bqaa-backfill sends none of a file another session\'s replay is sending: one outcome, and a row it saw refused is sent no more', async () => {
  let answer = FIRST_AGAIN
  const w = twoWriters(() => answer)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(source, packFiles([line(a, 'retry'), line(b, 'retry')])[0] as string)
  // The first session's first record waits in the store's write, and the
  // second session marks the file once that write has begun.
  let atRecord = (): void => undefined
  let releaseRecord = (): void => undefined
  const recordStarted = new Promise<void>(resolve => (atRecord = resolve))
  const recordReleased = new Promise<void>(resolve => (releaseRecord = resolve))
  let heldRecord = false
  w.holdSets((session, key) => {
    if (session !== 'first-session' || !key.startsWith('replayed:') || heldRecord) return undefined
    heldRecord = true
    answer = FIRST_REFUSED
    atRecord()
    return recordReleased
  })
  w.holdWrites((session, path) => (session === 'second-session' && path.includes('/outcome-sending-') ? recordStarted : undefined))
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const report = await w.writer('second-session').backfill(['spill', 'retry', 'rejected'], 'command')
  expect(report.held ?? '').toMatch(/^another session is replaying retry-20261013T200102003Z-earlier-1\.jsonl; if it stopped, \/bqaa-backfill force replays the file afresh/)
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 0])
  releaseRecord()
  await first
  // What the first left goes on: the row asked for again is sent, refused
  // now, and copied once.
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.sentOf(b)]).toEqual([2, 1, 1])
})

test('a done mark write still out when another writer finished the file, landing cut off, takes nothing from that writer\'s mark', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  let begin = (): void => undefined
  let release = (): void => undefined
  const begun = new Promise<void>(resolve => (begin = resolve))
  const released = new Promise<void>(resolve => (release = resolve))
  w.holdWrites((session, path, text) => {
    if (session !== 'first-session' || !path.includes('/outcome-done-') || text === '') return undefined
    begin()
    return released
  })
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await begun
  // Its lease runs out, and another session finishes the outcome.
  w.store.delete(`lease:${TABLE_REF}/${name}`)
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  const finished = doneTexts(w.files, name)
  expect([w.files.get(source), Object.keys(finished).length, doneMark(w.files, name)]).toEqual(['', 1, true])
  // The first session's write lands, cut off part-way, in a file of its own.
  w.tear(path => path.includes('/outcome-done-'))
  release()
  await first
  expect(doneMark(w.files, name)).toBe(true)
  for (const [path, text] of Object.entries(finished)) expect(w.files.get(path)).toBe(text)
  // A late copy of the file is emptied unsent.
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.tear(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, ''])
})

test('a file another session\'s lease holds, and one of what is left whose parent that lease holds, say why they wait', async () => {
  const w = twoWriters(() => OK_INSERT)
  const parent = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const child = `/dl/${TABLE_REF}/retry-20261013T200103003Z-earlier-2.jsonl`
  w.files.set(`/dl/${TABLE_REF}/${parent}`, packFiles([line('e'.repeat(32), 'retry')])[0] as string)
  w.store.set(`lease:${TABLE_REF}/${parent}`, { by: 'another-session', until: Number.MAX_SAFE_INTEGER })
  const alone = await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect([alone.held, alone.skippedFiles, w.sent]).toEqual([`another session's lease holds ${parent} (ten minutes at most); it waits for the next replay`, 1, []])
  // Its record, in the store, has the file it left not yet written.
  w.store.set(`replayed:${TABLE_REF}/${parent}`, { size: new TextEncoder().encode(w.files.get(`/dl/${TABLE_REF}/${parent}`) ?? '').length, fates: 'W', reasons: [], refusals: [], files: [child], written: [false], waits: true, seq: 1 })
  w.files.set(child, packFiles([line('d'.repeat(32), 'retry')], parent)[0] as string)
  const both = await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect([both.held, both.skippedFiles, w.sent]).toEqual(['retry-20261013T200103003Z-earlier-2.jsonl waits until the replay of the file it came from has it as written', 2, []])
})

test('of two whole done marks for one file, every writer goes by the later: what the earlier names, and the later does not, is emptied unsent', async () => {
  const w = twoWriters(() => OK_INSERT)
  const parent = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const [named, unnamed] = [`/dl/${TABLE_REF}/retry-20261013T200103003Z-later-2.jsonl`, `/dl/${TABLE_REF}/retry-20261013T200103004Z-earlier-3.jsonl`]
  const [d, e] = ['d'.repeat(32), 'e'.repeat(32)]
  w.files.set(`/dl/${TABLE_REF}/${parent}`, '')
  // Listed first, and first by name, the earlier mark (number 3); then the
  // later one (number 5).
  const mark = (files: string[], seq: number): string => sealFile(`${JSON.stringify({ size: 900, fates: 'W', reasons: [], refusals: [], files, written: files.map(() => true), waits: true, seq, retired: true })}\n`)
  w.files.set(`/dl/${TABLE_REF}/outcome-done-aaaaaaaaaa-${parent}`, mark([unnamed], 3))
  w.files.set(`/dl/${TABLE_REF}/outcome-done-zzzzzzzzzz-${parent}`, mark([named], 5))
  w.files.set(named, packFiles([line(d, 'retry')], parent)[0] as string)
  w.files.set(unnamed, packFiles([line(e, 'retry')], parent)[0] as string)
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(d), w.sentOf(e), w.files.get(named), w.files.get(unnamed)]).toEqual([1, 0, '', ''])
})

test('a writer whose read of the record answers after another writer finished the file, which a late write then puts back, sends none of it: the done marks are read again before it goes by no record', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const parentName = 'retry-20261013T200102002Z-parent-1.jsonl'
  const parent = `/dl/${TABLE_REF}/${parentName}`
  const name = 'retry-20261013T200102003Z-child-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry'), line('c'.repeat(32), 'retry')])[0] as string
  w.files.set(parent, text)
  // The parent's outcome is recorded: its first row waits, in the child, and
  // its second landed.
  w.store.set(`replayed:${TABLE_REF}/${parentName}`, { size: new TextEncoder().encode(text).length, fates: 'WL', reasons: [], refusals: [], files: [source], written: [false], waits: true, seq: 1 })
  // A writer's write of the child waits; its lease runs out, and another
  // writer finishes the parent.
  let outputReached = (): void => undefined
  let releaseOutput = (): void => undefined
  const atOutput = new Promise<void>(resolve => (outputReached = resolve))
  const output = new Promise<void>(resolve => (releaseOutput = resolve))
  let outputHeld = false
  w.holdWrites((session, path, value) => {
    if (session !== 'delayed-parent' || path !== source || value === '' || outputHeld) return undefined
    outputHeld = true
    outputReached()
    return output
  })
  const delayed = w.writer('delayed-parent').backfill(['spill', 'retry'], 'auto')
  await atOutput
  w.store.delete(`lease:${TABLE_REF}/${parentName}`)
  await w.writer('finishing-parent').backfill(['spill', 'retry'], 'auto')
  expect([w.files.get(parent), parseFile(w.files.get(source) ?? '').lines.length]).toEqual(['', 1])
  // A checker's read of the child waits; its lease runs out, and a sender
  // sends the child, its answer held.
  let readReached = (): void => undefined
  let releaseRead = (): void => undefined
  const atRead = new Promise<void>(resolve => (readReached = resolve))
  const read = new Promise<void>(resolve => (releaseRead = resolve))
  let readHeld = false
  w.holdReads((session, path) => {
    if (session !== 'checking-child' || path !== source || readHeld) return undefined
    readHeld = true
    readReached()
    return read
  })
  const checking = w.writer('checking-child').backfill(['spill', 'retry'], 'auto')
  await atRead
  w.store.delete(`lease:${TABLE_REF}/${name}`)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(session => {
    if (session !== 'sending-child') return undefined
    asked()
    return answered
  })
  const sending = w.writer('sending-child').backfill(['spill', 'retry'], 'auto')
  await atRequest
  // The checker goes on to its mark; its read of the child's record waits.
  let getReached = (): void => undefined
  let releaseGet = (): void => undefined
  const atGet = new Promise<void>(resolve => (getReached = resolve))
  const got = new Promise<void>(resolve => (releaseGet = resolve))
  let getHeld = false
  w.holdGets((session, key) => {
    if (session !== 'checking-child' || key !== `replayed:${TABLE_REF}/${name}` || getHeld) return undefined
    getHeld = true
    getReached()
    return got
  })
  releaseRead()
  await atGet
  // The sender's row is refused: it records, copies, marks the child done,
  // empties it and lets its record go; the late write puts the child back.
  answer()
  await sending
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source), doneMark(w.files, name)]).toEqual([1, 1, '', true])
  releaseOutput()
  await delayed
  expect(parseFile(w.files.get(source) ?? '').lines.length).toBe(1)
  releaseGet()
  await checking
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
})

test('a writer whose read of the marks answers after another writer finished the file, which a late write then puts back, sends none of it: the done marks are read after the marks', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const parentName = 'retry-20261013T200102002Z-parent-1.jsonl'
  const parent = `/dl/${TABLE_REF}/${parentName}`
  const name = 'retry-20261013T200102003Z-child-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry'), line('c'.repeat(32), 'retry')])[0] as string
  w.files.set(parent, text)
  // The parent's outcome is recorded: its first row waits, in the child, and
  // its second landed.
  w.store.set(`replayed:${TABLE_REF}/${parentName}`, { size: new TextEncoder().encode(text).length, fates: 'WL', reasons: [], refusals: [], files: [source], written: [false], waits: true, seq: 1 })
  // A writer's write of the child waits; its lease runs out, and another
  // writer finishes the parent.
  let outputReached = (): void => undefined
  let releaseOutput = (): void => undefined
  const atOutput = new Promise<void>(resolve => (outputReached = resolve))
  const output = new Promise<void>(resolve => (releaseOutput = resolve))
  let outputHeld = false
  w.holdWrites((session, path, value) => {
    if (session !== 'delayed-parent' || path !== source || value === '' || outputHeld) return undefined
    outputHeld = true
    outputReached()
    return output
  })
  const delayed = w.writer('delayed-parent').backfill(['spill', 'retry'], 'auto')
  await atOutput
  w.store.delete(`lease:${TABLE_REF}/${parentName}`)
  await w.writer('finishing-parent').backfill(['spill', 'retry'], 'auto')
  expect([w.files.get(parent), parseFile(w.files.get(source) ?? '').lines.length]).toEqual(['', 1])
  // A checker's read of the child waits; its lease runs out, and a sender
  // sends the child, its answer held.
  let readReached = (): void => undefined
  let releaseRead = (): void => undefined
  const atRead = new Promise<void>(resolve => (readReached = resolve))
  const read = new Promise<void>(resolve => (releaseRead = resolve))
  let readHeld = false
  w.holdReads((session, path) => {
    if (session !== 'checking-child' || path !== source || readHeld) return undefined
    readHeld = true
    readReached()
    return read
  })
  const checking = w.writer('checking-child').backfill(['spill', 'retry'], 'auto')
  await atRead
  w.store.delete(`lease:${TABLE_REF}/${name}`)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(session => {
    if (session !== 'sending-child') return undefined
    asked()
    return answered
  })
  const sending = w.writer('sending-child').backfill(['spill', 'retry'], 'auto')
  await atRequest
  // The checker goes on to its mark; its read of a mark beside the child
  // waits.
  let getReached = (): void => undefined
  let releaseGet = (): void => undefined
  const atGet = new Promise<void>(resolve => (getReached = resolve))
  const got = new Promise<void>(resolve => (releaseGet = resolve))
  let getHeld = false
  w.holdReads((session, path) => {
    if (session !== 'checking-child' || !path.includes('/outcome-sending-') || !path.endsWith(name) || getHeld) return undefined
    getHeld = true
    getReached()
    return got
  })
  releaseRead()
  await atGet
  // The sender's row is refused: it records, copies, marks the child done,
  // empties it and lets its record go; the late write puts the child back.
  answer()
  await sending
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source), doneMark(w.files, name)]).toEqual([1, 1, '', true])
  releaseOutput()
  await delayed
  expect(parseFile(w.files.get(source) ?? '').lines.length).toBe(1)
  releaseGet()
  await checking
  expect([w.sentOf(a), w.copiesOf(a)]).toEqual([1, 1])
})

test('a writer that finds another writer\'s mark standing as it reads the record leaves the file: no later read of the marks, after that writer finished, lets it send', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const parentName = 'retry-20261013T200102002Z-parent-1.jsonl'
  const parent = `/dl/${TABLE_REF}/${parentName}`
  const name = 'retry-20261013T200102003Z-child-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry'), line('c'.repeat(32), 'retry')])[0] as string
  w.files.set(parent, text)
  w.store.set(`replayed:${TABLE_REF}/${parentName}`, { size: new TextEncoder().encode(text).length, fates: 'WL', reasons: [], refusals: [], files: [source], written: [false], waits: true, seq: 1 })
  // A late write of the child waits, and another writer finishes the parent.
  let outputReached = (): void => undefined
  let releaseOutput = (): void => undefined
  const atOutput = new Promise<void>(resolve => (outputReached = resolve))
  const output = new Promise<void>(resolve => (releaseOutput = resolve))
  let outputHeld = false
  w.holdWrites((session, path, value) => {
    if (session !== 'delayed-parent' || path !== source || value === '' || outputHeld) return undefined
    outputHeld = true
    outputReached()
    return output
  })
  const delayed = w.writer('delayed-parent').backfill(['spill', 'retry'], 'auto')
  await atOutput
  w.store.delete(`lease:${TABLE_REF}/${parentName}`)
  await w.writer('finishing-parent').backfill(['spill', 'retry'], 'auto')
  // A checker's read of the child waits; a sender sends the child, its answer held.
  let readReached = (): void => undefined
  let releaseRead = (): void => undefined
  const atRead = new Promise<void>(resolve => (readReached = resolve))
  const read = new Promise<void>(resolve => (releaseRead = resolve))
  let readHeld = false
  w.holdReads((session, path) => {
    if (session !== 'checking-child' || path !== source || readHeld) return undefined
    readHeld = true
    readReached()
    return read
  })
  const checking = w.writer('checking-child').backfill(['spill', 'retry'], 'auto')
  await atRead
  w.store.delete(`lease:${TABLE_REF}/${name}`)
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(session => {
    if (session !== 'sending-child') return undefined
    asked()
    return answered
  })
  const sending = w.writer('sending-child').backfill(['spill', 'retry'], 'auto')
  await atRequest
  // The sender's mark, the only one standing now.
  const marks = [...w.files].filter(([path, value]) => path.includes('/outcome-sending-') && path.endsWith(`-${name}`) && value !== '').map(([path]) => path)
  expect(marks.length).toBe(1)
  // The checker's first read of the sender's mark finds it standing; a second
  // read of it, if any, waits.
  let secondReached = (): void => undefined
  let releaseSecond = (): void => undefined
  const atSecond = new Promise<void>(resolve => (secondReached = resolve))
  const second = new Promise<void>(resolve => (releaseSecond = resolve))
  let reads = 0
  w.holdReads((session, path) => {
    if (session !== 'checking-child' || path !== marks[0]) return undefined
    reads += 1
    if (reads !== 2) return undefined
    secondReached()
    return second
  })
  releaseRead()
  const first = await Promise.race([atSecond.then(() => 'read again'), checking.then(() => 'done')])
  // The sender finishes; the late write puts the child back.
  answer()
  await sending
  releaseOutput()
  await delayed
  releaseSecond()
  await checking
  expect([first, w.sentOf(a), w.copiesOf(a)]).toEqual(['done', 1, 1])
})

test('a writer replaying a file afresh past a done mark cut off part-way, whose read of the marks answers after another writer finished the file, sends none of it: the done marks are read after the marks', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry'), line('c'.repeat(32), 'retry')])[0] as string
  w.files.set(source, text)
  const size = new TextEncoder().encode(text).length
  const whole = sealFile(`${JSON.stringify({ fates: 'RL', reasons: [], refusals: [], size, waits: false, seq: 1, files: [], written: [], retired: true })}\n`)
  w.files.set(doneOf(name), whole.slice(0, Math.floor(whole.length / 2)))
  // A writer replays the file afresh, its answer held; it will hold its
  // emptying of the file too.
  let asked = (): void => undefined
  let answer = (): void => undefined
  const atRequest = new Promise<void>(resolve => (asked = resolve))
  const answered = new Promise<void>(resolve => (answer = resolve))
  w.hold(session => {
    if (session !== 'finishing-session') return undefined
    asked()
    return answered
  })
  let emptyReached = (): void => undefined
  let releaseEmpty = (): void => undefined
  const atEmpty = new Promise<void>(resolve => (emptyReached = resolve))
  const emptying = new Promise<void>(resolve => (releaseEmpty = resolve))
  w.holdWrites((session, path, value) => {
    if (session !== 'finishing-session' || path !== source || value !== '') return undefined
    emptyReached()
    return emptying
  })
  const finishing = w.writer('finishing-session').backfill(['spill', 'retry'], 'force')
  await atRequest
  w.store.delete(`lease:${TABLE_REF}/${name}`)
  const theirs = [...w.files].filter(([path, value]) => path.includes('/outcome-sending-') && path.endsWith(`-${name}`) && value !== '').map(([path]) => path)
  expect(theirs.length).toBe(1)
  // Another writer replays it afresh too: its second read of its own mark,
  // after the record read cut off, waits.
  let getReached = (): void => undefined
  let releaseGet = (): void => undefined
  const atGet = new Promise<void>(resolve => (getReached = resolve))
  const got = new Promise<void>(resolve => (releaseGet = resolve))
  let own = 0
  w.holdReads((session, path) => {
    if (session !== 'checking-session' || !path.includes('/outcome-sending-') || !path.endsWith(`-${name}`) || path === theirs[0]) return undefined
    own += 1
    if (own !== 2) return undefined
    getReached()
    return got
  })
  const checking = w.writer('checking-session').backfill(['spill', 'retry'], 'force')
  await atGet
  // The first writer's row is refused: it records, copies and marks the file
  // done; its emptying of the file waits.
  answer()
  await atEmpty
  expect([w.sentOf(a), w.copiesOf(a), doneMark(w.files, name), w.files.get(source)]).toEqual([1, 1, true, text])
  releaseGet()
  await checking
  releaseEmpty()
  await finishing
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, ''])
})

test('of two whole done marks of one number, every writer goes by the later name', async () => {
  const w = twoWriters(() => OK_INSERT)
  const parent = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const [first, second] = [`/dl/${TABLE_REF}/retry-20261013T200103003Z-child-1.jsonl`, `/dl/${TABLE_REF}/retry-20261013T200103004Z-child-2.jsonl`]
  const [d, e] = ['d'.repeat(32), 'e'.repeat(32)]
  w.files.set(`/dl/${TABLE_REF}/${parent}`, '')
  const mark = (files: string[]): string => sealFile(`${JSON.stringify({ size: 900, fates: 'W', reasons: [], refusals: [], files, written: [true], waits: true, seq: 5, retired: true })}\n`)
  w.files.set(`/dl/${TABLE_REF}/outcome-done-aaaaaaaaaa-${parent}`, mark([first]))
  w.files.set(`/dl/${TABLE_REF}/outcome-done-zzzzzzzzzz-${parent}`, mark([second]))
  w.files.set(first, packFiles([line(d, 'retry')], parent)[0] as string)
  w.files.set(second, packFiles([line(e, 'retry')], parent)[0] as string)
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(d), w.sentOf(e)]).toEqual([0, 1])
})

test('two writers of one session, one taking over from the other, never give two files one name: neither writes over what the other left', async () => {
  const w = twoWriters(() => FIRST_AGAIN)
  const [one, two] = [`/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`, `/dl/${TABLE_REF}/retry-20261013T200102004Z-earlier-2.jsonl`]
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  // Each file's first row is asked for again, its second lands: each replay
  // leaves a retry file, the first each writer names, in the same millisecond.
  w.files.set(one, packFiles([line(a, 'retry'), line('c'.repeat(32), 'retry')])[0] as string)
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  w.files.set(two, packFiles([line(b, 'retry'), line('d'.repeat(32), 'retry')])[0] as string)
  await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  const waiting = [...w.files].filter(([path, text]) => path !== one && path !== two && path.includes('/retry-') && text !== '').flatMap(([, text]) => parseFile(text).lines.map(l => l.row.event_id))
  expect(waiting.sort()).toEqual([a, b])
})

test('a done mark cut off part-way, with no other record, leaves the file waiting: none of it goes by itself or by /bqaa-backfill, and /bqaa-backfill force replays it afresh', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  const text = packFiles([line(a, 'retry')])[0] as string
  w.files.set(source, text)
  const size = new TextEncoder().encode(text).length
  const whole = sealFile(`${JSON.stringify({ fates: 'R', reasons: [], refusals: [], size, waits: false, seq: 1, files: [], written: [], retired: true })}\n`)
  w.files.set(doneOf(name), whole.slice(0, Math.floor(whole.length / 2)))
  for (const trigger of ['auto', 'command'] as const) {
    const report = await w.writer('first-session').backfill(['spill', 'retry'], trigger)
    expect(report.held ?? '').toMatch(/is cut off part-way; \/bqaa-backfill force replays the file afresh/)
    expect([report.skippedFiles, w.sentOf(a), w.files.get(source)]).toEqual([1, 0, text])
  }
  await w.writer('first-session').backfill(['spill', 'retry'], 'force')
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source), doneMark(w.files, name)]).toEqual([1, 1, '', true])
})

for (const why of ['cut off', 'unreadable', 'marked by another replay', 'not markable'] as const) {
  test(`a file a replay that runs by itself leaves waiting (its record ${why}) holds up none of the files after it`, async () => {
    const w = twoWriters(() => OK_INSERT)
    const first = 'retry-20261013T200102003Z-earlier-1.jsonl'
    const later = `/dl/${TABLE_REF}/retry-20261013T200102004Z-earlier-2.jsonl`
    const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
    w.files.set(`/dl/${TABLE_REF}/${first}`, packFiles([line(a, 'retry')])[0] as string)
    w.files.set(later, packFiles([line(b, 'retry')])[0] as string)
    if (why === 'cut off') w.files.set(`/dl/${TABLE_REF}/outcome-${first}`, '{"size":900,"fates":"W","refu')
    if (why === 'unreadable') w.failStore({ get: key => key.endsWith(`/${first}`) })
    if (why === 'not markable') w.refuseWrites((path, text) => path.includes('/outcome-sending-') && path.endsWith(`-${first}`) && text !== '')
    if (why === 'marked by another replay') w.files.set(`/dl/${TABLE_REF}/outcome-sending-anotherwriter-${first}`, sealFile(`${JSON.stringify({ size: new TextEncoder().encode(w.files.get(`/dl/${TABLE_REF}/${first}`) as string).length, sending: true, by: 'another-writer' })}\n`))
    const report = await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
    expect([w.sentOf(a), w.sentOf(b), w.files.get(later)]).toEqual([0, 1, ''])
    expect(report.error).toBe(null)
    expect(report.held ?? '').toMatch(new RegExp(first.replace(/\./g, '\\.')))
  })
}

test('a file of what is left that the record of its emptied parent does not name is emptied unsent, the done mark there or not', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  // The done mark cannot be written: the record stays in the store, every
  // file it names written, and the source is emptied.
  w.refuseWrites(path => path.includes('/outcome-done-'))
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect([w.files.get(source), doneMark(w.files, name), w.sentOf(a), w.copiesOf(a)]).toEqual(['', false, 1, 1])
  expect(w.store.get(`replayed:${TABLE_REF}/${name}`)).toMatchObject({ written: [true] })
  // A late copy of what another session's outcome left, naming the parent.
  const late = `/dl/${TABLE_REF}/retry-20261013T200103003Z-late-1.jsonl`
  w.files.set(late, packFiles([line(a, 'retry')], name)[0] as string)
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.files.get(late), w.sentOf(a), w.copiesOf(a)]).toEqual(['', 1, 1])
})

test('two sessions whose marks both stand when each reads them back send none of the file, and let their own go: the next replay sends it once', async () => {
  const w = twoWriters(() => OK_INSERT)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  let marked = (): void => undefined
  const secondMarked = new Promise<void>(resolve => (marked = resolve))
  let held = false
  w.holdReads((session, path) => {
    if (!path.includes('/outcome-sending-')) return undefined
    if (session === 'second-session') {
      marked()
      return undefined
    }
    if (held) return undefined
    held = true
    return secondMarked
  })
  await Promise.all([w.writer('first-session').backfill(['spill', 'retry'], 'auto'), w.writer('second-session').backfill(['spill', 'retry'], 'auto')])
  expect(w.sentOf(a)).toBe(0)
  expect([...w.files].filter(([path, text]) => path.includes('/outcome-sending-') && text !== '').length).toBe(0)
  w.holdReads(null)
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.files.get(source)]).toEqual([1, ''])
})

test('a session whose mark lands after another session recorded its outcome for the file sends none of it', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  let recorded = (): void => undefined
  const atCopy = new Promise<void>(resolve => (recorded = resolve))
  let letGo = (): void => undefined
  w.holdWrites((session, path, text) => {
    if (session === 'first-session' && path.includes('/rejected-') && text !== '') {
      recorded()
      return new Promise<void>(resolve => (letGo = resolve))
    }
    return session === 'second-session' && path.includes('/outcome-sending-') ? atCopy : undefined
  })
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  letGo()
  await first
  expect([w.sentOf(a), w.copiesOf(a), w.files.get(source)]).toEqual([1, 1, ''])
})

test('a replay with nothing left to write never writes its done mark over the one another session wrote meanwhile', async () => {
  let answer = OK_INSERT
  const w = twoWriters(() => answer)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  let letGo = (): void => undefined
  let begun = (): void => undefined
  const atRecord = new Promise<void>(resolve => (begun = resolve))
  let held = false
  w.holdWrites((session, path, text) => {
    if (session === 'second-session' && path.includes('/outcome-sending-')) return atRecord
    if (session !== 'first-session' || held || !recordFile(path) || text === '') return undefined
    held = true
    w.failStore({})
    answer = FIRST_REFUSED
    begun()
    return new Promise<void>(resolve => (letGo = resolve))
  })
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  // As /bqaa-backfill force, which goes past the first session's mark.
  await w.writer('second-session').backfill(['spill', 'retry'], 'force')
  const marks = doneTexts(w.files, name)
  expect([w.files.get(source), doneMark(w.files, name), w.copiesOf(a)]).toEqual(['', true, 1])
  letGo()
  await first
  expect(doneTexts(w.files, name)).toEqual(marks)
})

test('a replay that changed nothing lets go only its own mark: another session\'s, whose outcome no record took, still keeps a third from sending the file', async () => {
  let answer = FIRST_REFUSED
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  const { release } = bothSend(w)
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  await w.writer('second-session').backfill(['spill', 'retry'], 'force')
  answer = FIRST_AGAIN
  release()
  await first
  const report = await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sentOf(a)).toBe(2)
  expect(report.held ?? '').toMatch(/sent rows and has recorded no outcome/)
})

test('a session whose mark is written late, after another session sent the file and could record its outcome nowhere, sends none of it', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  let release = (): void => undefined
  const heldMark = new Promise<void>(resolve => (release = resolve))
  w.holdWrites((session, path) => (session === 'second-session' && path.includes('/outcome-sending-') ? heldMark : undefined))
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const second = w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  await first
  expect(w.sentOf(a)).toBe(1)
  release()
  await second
  expect(w.sentOf(a)).toBe(1)
})

test('a replay that changed nothing lets go of its own mark only: another session\'s, written meanwhile, its outcome recorded nowhere, keeps a third from sending the file, however late the letting go lands', async () => {
  let answer = FIRST_AGAIN
  const w = twoWriters(() => answer)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  let clearing = (): void => undefined
  let release = (): void => undefined
  const atClear = new Promise<void>(resolve => (clearing = resolve))
  const heldClear = new Promise<void>(resolve => (release = resolve))
  w.holdWrites((session, path, text) => {
    if (!path.includes('/outcome-sending-')) return undefined
    if (session === 'second-session' && text !== '') return atClear
    if (session === 'first-session' && text === '') {
      answer = FIRST_REFUSED
      clearing()
      return heldClear
    }
    return undefined
  })
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  // As /bqaa-backfill, which goes past the first session's mark: a replay
  // that runs by itself would leave the file to the first.
  await w.writer('second-session').backfill(['spill', 'retry'], 'force')
  expect(w.sentOf(a)).toBe(2)
  release()
  await first
  await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sentOf(a)).toBe(2)
})

test('a mark another session writes late and cut off part-way stands, and leaves the first session\'s mark standing too: a third session sends none of the file', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const source = `/dl/${TABLE_REF}/retry-20261013T200102003Z-earlier-1.jsonl`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.failStore({ set: true })
  w.refuseWrites(recordFile)
  let release = (): void => undefined
  const heldMark = new Promise<void>(resolve => (release = resolve))
  w.holdWrites((session, path) => (session === 'second-session' && path.includes('/outcome-sending-') ? heldMark : undefined))
  const first = w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  const second = w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  await first
  expect(w.sentOf(a)).toBe(1)
  w.tear(path => path.includes('/outcome-sending-'))
  release()
  await second
  expect(w.sentOf(a)).toBe(1)
  w.tear(null)
  const report = await w.writer('third-session').backfill(['spill', 'retry'], 'auto')
  expect(w.sentOf(a)).toBe(1)
  expect(report.held ?? '').toMatch(/sent rows and has recorded no outcome/)
})

test('a file whose outcome can be recorded nowhere leaves only itself waiting: the files after it are replayed in the same pass', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const first = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const later = `/dl/${TABLE_REF}/retry-20261013T200102004Z-earlier-2.jsonl`
  const [a, b] = ['a'.repeat(32), 'b'.repeat(32)]
  w.files.set(`/dl/${TABLE_REF}/${first}`, packFiles([line(a, 'retry')])[0] as string)
  w.files.set(later, packFiles([line(b, 'retry')])[0] as string)
  w.failStore({ set: key => key.endsWith(`/${first}`) })
  w.refuseWrites((path, text) => recordFile(path) && path.endsWith(first) && text !== '')
  const report = await w.writer('a-session').backfill(['spill', 'retry'], 'auto')
  expect([w.sentOf(a), w.sentOf(b), w.files.get(later), w.copiesOf(b)]).toEqual([1, 1, '', 1])
  expect(report.error).toBe(null)
  expect(report.held ?? '').toMatch(/could not record the replay of .*earlier-1\.jsonl/)
})

test('a file emptied from its done mark is reported with its outcome: its rows left and refused', async () => {
  const w = twoWriters(() => FIRST_REFUSED)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  w.files.set(source, packFiles([line('a'.repeat(32), 'retry')])[0] as string)
  w.refuseWrites((path, text) => path === source && text === '')
  await w.writer('first-session').backfill(['spill', 'retry'], 'auto')
  expect(doneMark(w.files, name)).toBe(true)
  w.refuseWrites(null)
  const report = await w.writer('second-session').backfill(['spill', 'retry'], 'auto')
  expect([w.files.get(source), report.files, report.rowsLeft, report.rowsRefused]).toEqual(['', 1, 1, 1])
})

test('another writer\'s mark cut off part-way, with no outcome recorded, is taken as standing: no replay sends any of the file but /bqaa-backfill force, which replays it afresh', async () => {
  const w = twoWriters(() => OK_INSERT)
  const name = 'retry-20261013T200102003Z-earlier-1.jsonl'
  const source = `/dl/${TABLE_REF}/${name}`
  const a = 'a'.repeat(32)
  w.files.set(source, packFiles([line(a, 'retry')])[0] as string)
  w.files.set(`/dl/${TABLE_REF}/outcome-sending-anotherwriter-${name}`, '{"size":900,"sending":tr')
  for (const trigger of ['auto', 'command'] as const) {
    const report = await w.writer('a-session').backfill(['spill', 'retry', 'rejected'], trigger)
    expect([w.sentOf(a), report.skippedFiles]).toEqual([0, 1])
    expect(report.held ?? '').toMatch(/sent rows and has recorded no outcome; if that session stopped, \/bqaa-backfill force replays the file afresh/)
  }
  await w.writer('a-session').backfill(['spill', 'retry', 'rejected'], 'force')
  expect([w.sentOf(a), w.files.get(source)]).toEqual([1, ''])
})
