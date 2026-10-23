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

// The hot path: hooks never wait on BigQuery or a process (session end's
// bounded spill write is the one wait on the disk), the queue stays bounded,
// and the mod never changes what it observes.

import { expect, test } from 'claude-code/testing'

import { COMMAND, OPTIONS, SESSION_ID, byType, deadLetterRows, runStep, setUp, startSession } from './helpers.ts'

test('hooks never call the network or a process themselves, session end included', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.steps.push({ result: { answer: 'ok', stopReason: 'end_turn' } })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await $.classic.SessionStart({ source: 'startup' })
  await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })
  await $.turn.start({ turnId: 't', text: 'hi' })
  await runStep($, { turnId: 't', index: 0 })
  await $.tool.call({ tool: 'Read', file_path: 'README.md' })
  await $.turn.complete({ turnId: 't', answer: 'ok', durationMs: 10, isAborted: false, reason: 'answer' })
  // Every hook above has returned, and nothing reached out.
  expect(world.fetches.length).toBe(0)
  expect(world.commands.length).toBe(0)
  // The token command and the first request run from timers.
  await world.clock.settle()
  expect(world.commands.length).toBe(1)
  await world.clock.advance(5000)
  expect(world.sentRows().length).toBe(9)
  // A token is in hand, BigQuery answers and rows are queued: session end
  // still starts no request and runs no process. The rows go to a spill file.
  await $.tool.call({ tool: 'Read', file_path: 'b.ts' })
  const fetches = world.fetches.length
  const commands = world.commands.length
  await $.session.end({ reason: 'prompt_input_exit', sessionId: SESSION_ID, resume: { id: SESSION_ID } })
  expect(world.fetches.length).toBe(fetches)
  expect(world.commands.length).toBe(commands)
  expect(deadLetterRows(world).map(r => r.event_type)).toEqual(['TOOL_STARTING', 'TOOL_COMPLETED', 'STATE_DELTA'])
})

test('a request that hangs never holds up a hook, and times out on its own', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [{ hang: true }]
  await startSession($)
  await world.clock.advance(5000)
  // The first insertAll is in flight and will not answer.
  expect(world.insertCalls().length).toBe(1)
  for (let i = 0; i < 25; i += 1) {
    const result = await $.tool.call({ tool: 'Read', file_path: `src/file-${i}.ts` })
    expect(result.text).toBe('ok')
  }
  // Past the 10 s client timeout the writer gives up on it and backs off.
  await world.clock.advance(10_000)
  const status = await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })
  expect(status.text ?? '').toMatch(/request timed out after 10000 ms/)
  expect(status.text ?? '').toMatch(/backing off: 1 failure/)
  world.releaseHung(200, '{}')
})

test('the queue is bounded and drops its oldest rows', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, { BQAA_MAX_QUEUE_ROWS: '10' })
  await startSession($)
  for (let i = 0; i < 30; i += 1) await $.tool.call({ tool: 'Read', file_path: `f${i}` })
  const status = await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })
  expect(status.text ?? '').toMatch(/queue: 10 rows/)
  // 1 session row + 60 tool rows captured; 10 kept.
  expect(status.text ?? '').toMatch(/captured 61,/)
  expect(status.text ?? '').toMatch(/dropped 51/)
  await world.clock.advance(5000)
  const rows = world.sentRows()
  expect(rows.length).toBe(10)
  // The newest survive: the last five calls' start and end rows.
  const files = byType(rows, 'TOOL_STARTING').map(r => (JSON.parse(r.content ?? '{}') as { args: { file_path: string } }).args.file_path)
  expect(files).toEqual(['f25', 'f26', 'f27', 'f28', 'f29'])
})

test('disabled: events pass through untouched and nothing is captured', { options: { ...OPTIONS, enabled: false } }, async ($, on) => {
  const world = setUp(on)
  world.toolReply = () => ({ result: { content: 'x' }, text: 'x' })
  await startSession($)
  const result = await $.tool.call({ tool: 'Read', file_path: 'a' })
  expect(result.text).toBe('x')
  expect(result.result).toEqual({ content: 'x' })
  await world.clock.advance(10_000)
  expect(world.fetches.length).toBe(0)
  expect(world.commands.length).toBe(0)
  const status = await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })
  expect(status.text ?? '').toMatch(/disabled/)
})

test('BQAA_TRACE_ENABLED=false turns capture off too', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, { BQAA_TRACE_ENABLED: 'false' })
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a' })
  await world.clock.advance(10_000)
  expect(world.fetches.length).toBe(0)
})

test('not configured: nothing is queued, sent or written, and status says why', async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a' })
  await world.clock.advance(20_000)
  expect(world.fetches.length).toBe(0)
  expect(world.files.size).toBe(0)
  const status = await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })
  expect(status.text ?? '').toMatch(/target not configured/)
  expect(status.text ?? '').toMatch(/problem: project and dataset are not set/)
  expect(status.text ?? '').toMatch(/not captured 3/)
})

test('a model stream passes through chunk for chunk', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const chunks = [
    { kind: 'thinking', index: 0, text: 'Considering.' },
    { kind: 'text', index: 1, text: 'Done' },
    { kind: 'text', index: 1, text: '.' },
  ] as const
  world.steps.push({ chunks: [...chunks], result: { answer: 'Done.', stopReason: 'end_turn' } })
  await startSession($)
  await $.turn.start({ turnId: 't', text: 'go' })
  const { chunks: seen, result } = await runStep($, { turnId: 't', index: 0 })
  expect(seen).toEqual([...chunks])
  expect(result.answer).toBe('Done.')
  expect(result.stopReason).toBe('end_turn')
})

test('closing a model stream early closes the stream beneath', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.steps.push({
    chunks: [
      { kind: 'text', index: 0, text: 'a' },
      { kind: 'text', index: 0, text: 'b' },
    ],
    result: { answer: 'ab' },
  })
  await startSession($)
  await $.turn.start({ turnId: 't', text: 'go' })
  const stream = $.turn.step({ turnId: 't', index: 0, model: 'example-model', messageCount: 1 })
  const first = await stream.next()
  expect(first.done).toBe(false)
  // The person interrupts: the engine closes the stream it was reading.
  await stream.return(undefined as never)
  expect(world.stepsClosed).toBe(1)
  expect(world.stepsFinished).toBe(0)
})

test('an environment variable a policy refuses reads as unset; capture goes on', { options: OPTIONS }, async ($, on) => {
  // Registered ahead of mock.env, so it answers first for this one name.
  on('env.get', { name: 'USERPROFILE' }, () => ({ deny: 'refused by policy' }))
  const world = setUp(on)
  await startSession($)
  await world.clock.advance(5000)
  expect(world.acceptedRows().map(r => r.event_type)).toEqual(['STATE_DELTA'])
})

test('a refused BQAA_ACCESS_TOKEN read leaves the token command to supply the token', { options: OPTIONS }, async ($, on) => {
  on('env.get', { name: 'BQAA_ACCESS_TOKEN' }, () => ({ deny: 'refused by policy' }))
  const world = setUp(on)
  await startSession($)
  await world.clock.advance(5000)
  expect(world.commands.length).toBe(1)
  expect(world.acceptedRows().map(r => r.event_type)).toEqual(['STATE_DELTA'])
})

test('session.start firing again without a reload keeps the queued rows', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await world.clock.advance(5000)
  expect(world.acceptedRows().map(r => r.event_type)).toEqual(['STATE_DELTA', 'TOOL_STARTING', 'TOOL_COMPLETED'])
})
