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

// Permission decisions from the host's collector records (`tool_decision`),
// joined to their tool call by tool_use_id. An accept comes while the call
// runs; a rejection can come just after the call returned (seen live with
// host 2.1.299), so once records arrive, a finished call waits up to 2 s for
// its decision. Records pass on unchanged.

import { type Engine, expect, test } from 'claude-code/testing'

import { COMMAND, OPTIONS, SESSION_ID, type ToolReply, type World, byType, deadLetterRows, json, setUp, startSession } from './helpers.ts'

const LOGGED_AT = '2026-10-14T05:00:00.000Z'

const decision = ($: Engine, toolUseId: string, answer: string, source: string, extra: Record<string, string> = {}) =>
  $.telemetry.log({ to: 'collector', event: 'tool_decision', loggedAt: LOGGED_AT, attributes: { tool_use_id: toolUseId, decision: answer, source, tool_name: 'Bash', ...extra } })

// Any other record: the host's stream is live.
const otherRecord = ($: Engine) => $.telemetry.log({ to: 'collector', event: 'user_prompt', loggedAt: LOGGED_AT, attributes: { prompt_length: 2 } })

// A tool call that answers only when the test lets it.
function heldTool(world: World): () => void {
  let release = (): void => undefined
  world.toolReply = () =>
    new Promise<ToolReply>(resolve => {
      release = () => resolve({ result: 'ok', text: 'ok' })
    })
  return () => release()
}

const completed = (world: World) => byType(world.acceptedRows(), 'TOOL_COMPLETED')

test('an accept that comes while the call runs is on its TOOL_COMPLETED row, written at once', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  const release = heldTool(world)
  const call = $.tool.call({ tool: 'Read', file_path: 'a.ts', tool_use_id: 'toolu_accept' })
  await decision($, 'toolu_accept', 'accept', 'config')
  release()
  await call
  // Written with the call's end, not after a wait.
  await world.clock.advance(5000)
  const [row] = completed(world)
  expect(json(row?.attributes ?? null)).toMatchObject({ tool_use_id: 'toolu_accept', permission_decision: 'accept', permission_decision_source: 'config' })
})

test('a rejection that comes just after its call returned is on the row, which keeps the time the call ended', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.toolReply = () => ({ isError: true, result: 'Permission to use Bash has been denied.', text: 'Permission to use Bash has been denied.' })
  await startSession($)
  await otherRecord($)
  const before = Date.now()
  await $.tool.call({ tool: 'Bash', command: 'rm -rf build', tool_use_id: 'toolu_reject' })
  const after = Date.now()
  // The row waits for its decision; real time moves on meanwhile, so a row
  // stamped when written would carry a later time.
  await world.clock.advance(1000)
  const spinUntil = Date.now() + 15
  while (Date.now() < spinUntil) {
    // Waiting out 15 ms of real time.
  }
  await decision($, 'toolu_reject', 'reject', 'user_reject')
  await world.clock.advance(4000)
  const [row] = completed(world)
  expect(row?.status).toBe('ERROR')
  expect(json(row?.attributes ?? null)).toMatchObject({ permission_decision: 'reject', permission_decision_source: 'user_reject' })
  const at = Date.parse(row?.timestamp ?? '')
  expect(at >= before && at <= after).toBe(true)
})

test('a call whose decision never comes is written without one after 2 s', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await otherRecord($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts', tool_use_id: 'toolu_none' })
  await world.clock.advance(1999)
  await $.command.run({ command: 'bqaa-flush', args: '', ...COMMAND })
  await world.clock.settle()
  expect(completed(world).length).toBe(0)
  await world.clock.advance(1)
  await world.clock.advance(5000)
  const [row] = completed(world)
  expect(row).toBeDefined()
  expect(json(row?.attributes ?? null).permission_decision).toBeUndefined()
})

test('with no collector records, calls are written at once, as before', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts', tool_use_id: 'toolu_plain' })
  await $.command.run({ command: 'bqaa-flush', args: '', ...COMMAND })
  await world.clock.settle()
  expect(completed(world).length).toBe(1)
})

test('session end writes the calls still waiting, before its own row', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await otherRecord($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts', tool_use_id: 'toolu_end' })
  await $.session.end({ reason: 'prompt_input_exit', sessionId: SESSION_ID, resume: { id: SESSION_ID } })
  const spilled = deadLetterRows(world).map(r => r.event_type)
  expect(spilled.slice(-2)).toEqual(['TOOL_COMPLETED', 'STATE_DELTA'])
})

test('records pass on unchanged; a record a later hook refuses is not used', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  const release = heldTool(world)
  world.refuseRecords = true
  const call = $.tool.call({ tool: 'Bash', command: 'ls', tool_use_id: 'toolu_refused' })
  await decision($, 'toolu_refused', 'accept', 'config').catch(() => undefined)
  world.refuseRecords = false
  release()
  await call
  await world.clock.advance(5000)
  expect(world.records).toEqual([{ event: 'tool_decision', attributes: { tool_use_id: 'toolu_refused', decision: 'accept', source: 'config', tool_name: 'Bash' } }])
  const [row] = completed(world)
  expect(json(row?.attributes ?? null).permission_decision).toBeUndefined()
})

test('a decision value that is not a short lowercase token is not used; an odd source reads as unknown', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  const release = heldTool(world)
  const first = $.tool.call({ tool: 'Bash', command: 'ls', tool_use_id: 'toolu_odd' })
  await decision($, 'toolu_odd', 'ACCEPT; DROP TABLE', 'config')
  release()
  await first
  const releaseNext = heldTool(world)
  const second = $.tool.call({ tool: 'Bash', command: 'pwd', tool_use_id: 'toolu_source' })
  await decision($, 'toolu_source', 'accept', 'Some Source!')
  releaseNext()
  await second
  await world.clock.advance(5000)
  // The first call waited 2 s for a usable decision, so it is written last.
  const byId = (id: string) => completed(world).find(r => json(r.attributes).tool_use_id === id)
  expect(completed(world).length).toBe(2)
  expect(json(byId('toolu_odd')?.attributes ?? null).permission_decision).toBeUndefined()
  expect(json(byId('toolu_source')?.attributes ?? null)).toMatchObject({ permission_decision: 'accept', permission_decision_source: 'unknown' })
})

test("a subagent's call gets its decision the same way", { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.turn.start({ turnId: 'turn-1', text: 'look around' })
  const release = heldTool(world)
  const call = $.tool.call({ tool: 'Grep', pattern: 'flush', tool_use_id: 'toolu_sub', agentId: 'agent-3' } as never)
  await decision($, 'toolu_sub', 'accept', 'config')
  release()
  await call
  await world.clock.advance(5000)
  const [row] = completed(world)
  expect(json(row?.attributes ?? null)).toMatchObject({ agent_id: 'agent-3', permission_decision: 'accept' })
})
