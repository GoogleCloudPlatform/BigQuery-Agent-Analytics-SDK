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

// Replay against files other sessions are still writing. `$.fs.write` is
// not atomic: another process can read a file part-written. A replay must
// send nothing from such a file and leave it as it is, so that no unsent row
// is ever lost between a writer and a replayer.

import { type Engine, expect, test } from 'claude-code/testing'

import { resolveConfig } from '../hooks/lib/config.ts'
import { type DeadLetterLine, encodeLines, parseFile } from '../hooks/lib/deadletter.ts'
import { buildRow } from '../hooks/lib/rows.ts'
import { type Io, Writer } from '../hooks/lib/writer.ts'

import { COMMAND, OK_INSERT, OPTIONS, PARTITION_DIR, TABLE_REF, setUp, startSession, utf8Bytes } from './helpers.ts'

const statusText = async ($: Engine): Promise<string> => (await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })).text ?? ''

const row = (eventId: string) =>
  buildRow({
    eventType: 'TOOL_COMPLETED',
    timestampMs: 0,
    eventId,
    agent: 'coding-agent',
    sessionId: 'race-session',
    invocationId: 'turn',
    userId: 'dev',
    traceId: null,
    spanId: null,
    parentSpanId: null,
    content: { result: 'synthetic data' },
    attributes: {},
    maxPayloadChars: 16384,
  }).row

test('a spill file still being written is left to its writer while a replay runs, and every row is sent once it is complete', async () => {
  const config = resolveConfig({ project: 'race-project', dataset: 'events', deadLetterDir: '/race/dead-letter' }, {})
  const dir = `/race/dead-letter/${'race-project.events.agent_events'}`
  const files = new Map<string, string>()
  const sent: string[] = []
  // A file an earlier session parked, complete: the replay works on it while
  // the producer's write finishes.
  const earlier = `${dir}/spill-20261013T200102003Z-earlier-1.jsonl`
  const parked: DeadLetterLine = { v: 1, kind: 'spill', reason: 'session ended', attempts: 0, failed_at: '2026-10-13T20:01:02.003Z', table: 'race-project.events.agent_events', row: row('e'.repeat(32)) }
  files.set(earlier, encodeLines([parked])[0] as string)

  let holdRequest = false
  let requestHeld = (): void => undefined
  let releaseRequest = (): void => undefined
  const requesting = new Promise<void>(resolve => {
    requestHeld = resolve
  })
  const base: Io = {
    now: () => 100_000,
    after: () => ({ cancel: () => undefined }),
    fetch: async (_url, init) => {
      const ids = (JSON.parse(init.body ?? '{}') as { rows: { json: { event_id: string } }[] }).rows.map(r => r.json.event_id)
      if (holdRequest) {
        holdRequest = false
        await new Promise<void>(resolve => {
          releaseRequest = resolve
          requestHeld()
        })
      }
      sent.push(...ids)
      return OK_INSERT
    },
    runCommand: async () => ({ exitCode: 0, stdout: 'a'.repeat(24), stderr: '' }),
    envToken: async () => undefined,
    readFile: async path => files.get(path) ?? '',
    writeFile: async (path, text) => {
      files.set(path, text)
    },
    listDir: async () => [...files].map(([path, text]) => ({ name: path.split('/').pop() as string, kind: 'file', size: utf8Bytes(text) })),
    storeGet: async () => undefined,
    storeSet: async () => undefined,
    storeDelete: async () => undefined,
    status: () => undefined,
    debug: () => undefined,
  }

  // The producer's write shows its first line before the rest, as a write
  // that is not atomic can.
  let source = ''
  let firstLineShown = (): void => undefined
  let finishWrite = (): void => undefined
  const shown = new Promise<void>(resolve => {
    firstLineShown = resolve
  })
  const producer = new Writer(
    {
      ...base,
      writeFile: async (path, text) => {
        source = path
        files.set(path, text.slice(0, text.indexOf('\n') + 1))
        firstLineShown()
        await new Promise<void>(resolve => {
          finishWrite = () => {
            files.set(path, text)
            resolve()
          }
        })
      },
    },
    () => config,
    () => 'producer-session',
  )
  const replayer = new Writer(base, () => config, () => 'replayer-session')

  for (const id of ['a'.repeat(32), 'b'.repeat(32)]) producer.enqueue(row(id), 0)
  const spilling = producer.spillForExit(800)
  await shown
  // The producer's file sorts first: the replay reads it half written, then
  // sends the earlier file, and the producer finishes while that request is
  // out.
  expect(source < earlier).toBe(true)
  holdRequest = true
  const replaying = replayer.backfill(['spill'], 'auto')
  await requesting
  finishWrite()
  await spilling
  releaseRequest()
  const first = await replaying

  expect(sent).toEqual(['e'.repeat(32)])
  expect(files.get(earlier)).toBe('')
  // The producer's file was left alone: both rows are on disk, complete.
  const written = parseFile(files.get(source) ?? '')
  expect(written.complete).toBe(true)
  expect(written.lines.map(line => line.row.event_id)).toEqual(['a'.repeat(32), 'b'.repeat(32)])
  expect(first.incompleteFiles).toEqual([source.split('/').pop()])

  // The next replay sends both and empties the file.
  const second = await replayer.backfill(['spill'], 'auto')
  expect(sent).toEqual(['e'.repeat(32), 'a'.repeat(32), 'b'.repeat(32)])
  expect(files.get(source)).toBe('')
  expect(second.incompleteFiles).toEqual([])
})

test('a dead-letter file with no completion record is kept as it is and reported, never replayed', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // The first line of a file whose writer stopped there.
  const path = `${PARTITION_DIR}/spill-20261013T200102003Z-stopped-1.jsonl`
  const parked: DeadLetterLine = { v: 1, kind: 'spill', reason: 'session ended', attempts: 0, failed_at: '2026-10-13T20:01:02.003Z', table: TABLE_REF, row: row('f'.repeat(32)) }
  const text = `${JSON.stringify(parked)}\n`
  world.files.set(path, text)
  await startSession($)
  await world.clock.advance(15_000)
  expect(world.sentRows().some(r => r.event_id === 'f'.repeat(32))).toBe(false)
  expect(world.files.get(path)).toBe(text)
  expect(await statusText($)).toMatch(/1 file\(s\) without a completion record \(still being written, or left by a writer that stopped\), kept, not replayed yet: spill-20261013T200102003Z-stopped-1\.jsonl/)
})
