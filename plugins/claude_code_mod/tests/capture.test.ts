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

// What the mod captures: each event becomes the BQAA row the README's
// mapping table promises, with one trace per turn and spans nested under it.

import { type Engine, expect, test } from 'claude-code/testing'

import { ACCESS_TOKEN, DRY_RUN_DIR, OPTIONS, type Row, SESSION_ID, USAGE, type World, byType, deadLetterRows, json, runStep, setUp, startSession } from './helpers.ts'

const HEX32 = /^[0-9a-f]{32}$/
const HEX16 = /^[0-9a-f]{16}$/

test('a turn becomes rows under one trace, with spans, tokens and latency', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.steps.push(
    {
      chunks: [
        { kind: 'text', index: 0, text: 'Let me look.' },
        { kind: 'tool', index: 1, id: 'toolu_01', name: 'Bash' },
        { kind: 'input', index: 1, json: '{"command":"ls"}' },
      ],
      result: { answer: 'Let me look.', toolUses: [{ name: 'Bash', input: { command: 'ls' } }], stopReason: 'tool_use', usage: USAGE },
    },
    {
      chunks: [{ kind: 'text', index: 0, text: 'Two files: a and b.' }],
      result: { answer: 'Two files: a and b.', stopReason: 'end_turn', usage: { ...USAGE, input_tokens: 40, output_tokens: 18 } },
    },
  )
  world.toolReply = () => ({ result: { stdout: 'a\nb', stderr: '', interrupted: false }, text: 'a\nb' })

  await startSession($)
  await $.prompt.submit({ text: 'list the files here', wait: false, origin: { kind: 'composer' } })
  await $.turn.start({ turnId: 'turn-1', text: 'list the files here' })
  const first = await runStep($, { turnId: 'turn-1', index: 0 })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await runStep($, { turnId: 'turn-1', index: 1 })
  await $.turn.complete({
    turnId: 'turn-1',
    answer: 'Two files: a and b.',
    durationMs: 4200,
    isAborted: false,
    reason: 'answer',
    usage: { ...USAGE, input_tokens: 1243, output_tokens: 430 },
  })

  // The stream reached the caller whole and unchanged.
  expect(first.chunks.length).toBe(3)
  expect(first.result.answer).toBe('Let me look.')

  // Nothing was sent from inside a hook: only the background flush sends.
  expect(world.insertCalls().length).toBe(0)
  await world.clock.advance(5000)

  const rows = world.sentRows()
  expect(rows.map(r => r.event_type)).toEqual([
    'STATE_DELTA',
    'USER_MESSAGE_RECEIVED',
    'INVOCATION_STARTING',
    'LLM_REQUEST',
    'LLM_RESPONSE',
    'TOOL_STARTING',
    'TOOL_COMPLETED',
    'LLM_REQUEST',
    'LLM_RESPONSE',
    'AGENT_RESPONSE',
    'INVOCATION_COMPLETED',
  ])
  for (const row of rows) {
    expect(row.session_id).toBe(SESSION_ID)
    expect(row.user_id).toBe('dev')
    expect(row.agent).toBe('coding-agent')
    expect(row.event_id).toMatch(HEX32)
    expect(row.span_id ?? '').toMatch(HEX16)
    expect(row.status).toBe('OK')
    expect(row.is_truncated).toBe(false)
    expect(Number.isNaN(Date.parse(row.timestamp))).toBe(false)
    const writer = json(row.attributes).writer as Record<string, unknown>
    expect(writer).toEqual({ plugin: 'bigquery-agent-analytics-mod', version: '0.1.0', label: 'bigquery-agent-analytics-mod/0.1.0', agent: 'coding-agent', mode: 'insert_all' })
  }
  expect(new Set(rows.map(r => r.event_id)).size).toBe(rows.length)

  const [start] = byType(rows, 'INVOCATION_STARTING')
  const turnRows = rows.filter(r => r.event_type !== 'STATE_DELTA' && r.event_type !== 'USER_MESSAGE_RECEIVED')
  expect(start?.trace_id ?? '').toMatch(HEX32)
  for (const row of turnRows) {
    expect(row.trace_id).toBe(start?.trace_id ?? 'missing')
    expect(row.invocation_id).toBe('turn-1')
  }
  expect(start?.parent_span_id).toBe(null)

  const [req0, req1] = byType(rows, 'LLM_REQUEST')
  const [res0, res1] = byType(rows, 'LLM_RESPONSE')
  expect(req0?.span_id).toBe(res0?.span_id ?? 'missing')
  expect(req1?.span_id).toBe(res1?.span_id ?? 'missing')
  expect(req0?.span_id).not.toBe(req1?.span_id ?? 'missing')
  expect(req0?.parent_span_id).toBe(start?.span_id ?? 'missing')
  expect(json(req0?.content ?? null)).toEqual({ prompt: [{ role: 'user', content: 'list the files here' }] })
  expect(json(req1?.content ?? null)).toEqual({ prompt: [] })
  expect(json(req0?.attributes ?? null)).toMatchObject({ model: 'example-model', step_index: 0, message_count: 1 })

  expect(json(res0?.content ?? null)).toEqual({ response: 'Let me look.', tool_uses: ['Bash'] })
  expect(json(res0?.attributes ?? null)).toMatchObject({
    model: 'example-model',
    finish_reason: 'tool_use',
    usage_metadata: {
      prompt_tokens: 1203 + 18240 + 960,
      completion_tokens: 412,
      total_tokens: 1203 + 18240 + 960 + 412,
      input_tokens: 1203,
      output_tokens: 412,
      cache_read_input_tokens: 18240,
      cache_creation_input_tokens: 960,
    },
  })
  const latency = json(res0?.latency_ms ?? null)
  expect(typeof latency.total_ms).toBe('number')
  expect(typeof latency.time_to_first_token_ms).toBe('number')

  const [toolStart] = byType(rows, 'TOOL_STARTING')
  const [toolDone] = byType(rows, 'TOOL_COMPLETED')
  expect(toolStart?.span_id).toBe(toolDone?.span_id ?? 'missing')
  // The tool's parent is the model request that asked for it.
  expect(toolStart?.parent_span_id).toBe(req0?.span_id ?? 'missing')
  expect(json(toolStart?.content ?? null)).toEqual({ tool: 'Bash', args: { command: 'ls' }, tool_origin: 'LOCAL' })
  expect(json(toolDone?.content ?? null)).toEqual({ tool: 'Bash', result: 'a\nb', tool_origin: 'LOCAL' })
  expect(typeof json(toolDone?.latency_ms ?? null).total_ms).toBe('number')

  const [answer] = byType(rows, 'AGENT_RESPONSE')
  expect(json(answer?.content ?? null)).toEqual({ response: 'Two files: a and b.' })
  const [done] = byType(rows, 'INVOCATION_COMPLETED')
  expect(json(done?.latency_ms ?? null)).toEqual({ total_ms: 4200 })
  expect(json(done?.attributes ?? null)).toMatchObject({ finish_reason: 'answer', step_count: 2, usage_metadata: { completion_tokens: 430 } })

  const [prompt] = byType(rows, 'USER_MESSAGE_RECEIVED')
  expect(json(prompt?.content ?? null)).toEqual({ text_summary: 'list the files here' })
  expect(json(prompt?.attributes ?? null)).toMatchObject({ prompt_origin: 'composer', submitted_during_turn: false })

  const [sessionStart] = byType(rows, 'STATE_DELTA')
  expect(json(sessionStart?.content ?? null)).toEqual({ session_start: true })
  expect(json(sessionStart?.attributes ?? null)).toMatchObject({
    state_delta: { source: 'coding_agent_session_start', start_source: 'startup', model: 'example-model' },
    session_metadata: { project_name: 'my-repo', cwd: '/work/my-repo', host_version: '2.1.296' },
  })

  // One insertAll, to the configured table, with the token from gcloud and
  // each row's event_id as its insertId.
  const [call] = world.insertCalls()
  expect(call?.url).toBe('https://bigquery.googleapis.com/bigquery/v2/projects/my-gcp-project/datasets/agent_analytics/tables/agent_events/insertAll')
  expect(call?.method).toBe('POST')
  expect(call?.headers.authorization).toBe(`Bearer ${ACCESS_TOKEN}`)
  const body = JSON.parse(call?.body ?? '{}') as { skipInvalidRows: boolean; ignoreUnknownValues: boolean; rows: { insertId: string; json: { event_id: string } }[] }
  expect(body.skipInvalidRows).toBe(true)
  expect(body.ignoreUnknownValues).toBe(true)
  for (const r of body.rows) expect(r.insertId).toBe(r.json.event_id)
  expect(world.commands).toEqual([['gcloud', 'auth', 'application-default', 'print-access-token']])
})

test('a subagent gets its own agent span under the turn that started it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.steps.push({ result: { answer: 'Found 3 call sites.', stopReason: 'end_turn', usage: { ...USAGE, model: 'example-model' } } })
  await startSession($)
  await $.turn.start({ turnId: 'turn-1', text: 'find the call sites' })
  await $.classic.SubagentStart({ agent_id: 'agent-7', agent_type: 'Explore' })
  await runStep($, { turnId: 'sub-turn-1', index: 0, agentId: 'agent-7', model: 'example-model' })
  await $.tool.call({ tool: 'Grep', pattern: 'flushOnce', agentId: 'agent-7' } as never)
  await $.turn.complete({ turnId: 'sub-turn-1', agentId: 'agent-7', answer: 'Found 3 call sites.', durationMs: 2100, isAborted: false, reason: 'answer', usage: { ...USAGE, model: 'example-model' } })
  await world.clock.advance(5000)

  const rows = world.sentRows()
  const [turn] = byType(rows, 'INVOCATION_STARTING')
  const [agentStart] = byType(rows, 'AGENT_STARTING')
  const [agentDone] = byType(rows, 'AGENT_COMPLETED')
  const [request] = byType(rows, 'LLM_REQUEST')
  const [toolStart] = byType(rows, 'TOOL_STARTING')

  expect(agentStart?.agent).toBe('Explore')
  expect(agentStart?.parent_span_id).toBe(turn?.span_id ?? 'missing')
  expect(agentStart?.trace_id).toBe(turn?.trace_id ?? 'missing')
  expect(agentStart?.invocation_id).toBe('turn-1')
  expect(request?.agent).toBe('Explore')
  expect(request?.parent_span_id).toBe(agentStart?.span_id ?? 'missing')
  expect(toolStart?.agent).toBe('Explore')
  expect(toolStart?.parent_span_id).toBe(request?.span_id ?? 'missing')
  expect(agentDone?.span_id).toBe(agentStart?.span_id ?? 'missing')
  expect(json(agentDone?.latency_ms ?? null)).toEqual({ total_ms: 2100 })
  expect(json(agentDone?.attributes ?? null)).toMatchObject({ agent_id: 'agent-7', model: 'example-model', subagent_turn_id: 'sub-turn-1' })
  // A subagent's answer goes back to its spawner, not to the person.
  expect(byType(rows, 'AGENT_RESPONSE').length).toBe(0)
})

// A delegated subagent in the order the host raises it: the Agent call
// starts, the subagent's run begins with a turn.start that names its loop,
// the settings-hook SubagentStart follows, then its steps and tools, its
// turn.complete and the Agent call's end; its answer comes back into the
// running turn as a prompt, and the main loop's next step follows.
async function delegate($: Engine, world: World, agentId: string, agentType: string, runTurnId: string): Promise<void> {
  world.toolReply = async e => {
    if (e.tool !== 'Agent') return { result: 'a.ts:3: flushOnce()', text: 'a.ts:3: flushOnce()' }
    await $.turn.start({ turnId: runTurnId, text: 'Where is flushOnce called?', agentId })
    await $.classic.SubagentStart({ agent_id: agentId, agent_type: agentType })
    world.steps.push({ result: { answer: '', toolUses: [{ name: 'Grep', input: {} }], stopReason: 'tool_use', usage: { ...USAGE, model: 'example-model' } } })
    await runStep($, { turnId: runTurnId, index: 0, agentId })
    await $.tool.call({ tool: 'Grep', pattern: 'flushOnce', agentId } as never)
    world.steps.push({ result: { answer: 'Called from a.ts.', stopReason: 'end_turn', usage: { ...USAGE, model: 'example-model' } } })
    await runStep($, { turnId: runTurnId, index: 1, agentId })
    await $.turn.complete({ turnId: runTurnId, agentId, answer: 'Called from a.ts.', durationMs: 900, isAborted: false, reason: 'answer', usage: { ...USAGE, model: 'example-model' } })
    return { result: { status: 'completed' }, text: 'Called from a.ts.' }
  }
  await $.tool.call({ tool: 'Agent', description: 'Find callers', prompt: 'Where is flushOnce called?', subagent_type: agentType })
  await $.prompt.submit({ text: 'Called from a.ts.', wait: false, origin: { kind: 'peer' }, turnId: 'main-turn' })
}

test('subagent runs inside a turn open no invocation: the turn keeps one trace and goes on after them', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.turn.start({ turnId: 'main-turn', text: 'find the callers, then the tests' })
  world.steps.push({ result: { answer: '', toolUses: [{ name: 'Agent', input: {} }], stopReason: 'tool_use', usage: { ...USAGE, model: 'example-model' } } })
  await runStep($, { turnId: 'main-turn', index: 0 })
  await delegate($, world, 'agent-7', 'Explore', 'sub-run-7')
  world.steps.push({ result: { answer: '', toolUses: [{ name: 'Agent', input: {} }], stopReason: 'tool_use', usage: { ...USAGE, model: 'example-model' } } })
  await runStep($, { turnId: 'main-turn', index: 1 })
  await delegate($, world, 'agent-8', 'Plan', 'sub-run-8')
  world.steps.push({ result: { answer: 'flushOnce is called from a.ts.', stopReason: 'end_turn', usage: { ...USAGE, model: 'example-model' } } })
  await runStep($, { turnId: 'main-turn', index: 2 })
  await $.turn.complete({ turnId: 'main-turn', answer: 'flushOnce is called from a.ts.', durationMs: 4000, isAborted: false, reason: 'answer', usage: { ...USAGE, model: 'example-model' } })
  await world.clock.advance(5000)
  await $.session.end({ reason: 'prompt_input_exit', sessionId: SESSION_ID, resume: { id: SESSION_ID } })

  const rows = [...world.sentRows(), ...deadLetterRows(world)]
  const turns = byType(rows, 'INVOCATION_STARTING')
  expect(turns.length).toBe(1)
  const turn = turns[0]
  // Every row of the turn, the subagents' included, is in its trace and
  // under its invocation.
  const inTurn = rows.filter(r => r.event_type !== 'STATE_DELTA')
  expect([...new Set(inTurn.map(r => r.trace_id))]).toEqual([turn?.trace_id])
  expect([...new Set(inTurn.map(r => r.invocation_id))]).toEqual(['main-turn'])
  // One agent span per subagent, under the turn's span, named by its type.
  const starts = byType(rows, 'AGENT_STARTING')
  expect(starts.map(r => [r.agent, r.parent_span_id, json(r.attributes).inferred])).toEqual([
    ['Explore', turn?.span_id, undefined],
    ['Plan', turn?.span_id, undefined],
  ])
  for (const start of starts) {
    const requests = byType(rows, 'LLM_REQUEST').filter(r => r.agent === start.agent)
    expect(requests.map(r => r.parent_span_id)).toEqual([start.span_id, start.span_id])
    const grep = byType(rows, 'TOOL_STARTING').filter(r => r.agent === start.agent)
    expect(grep.map(r => r.parent_span_id)).toEqual([requests[0]?.span_id])
    expect(byType(rows, 'AGENT_COMPLETED').filter(r => r.agent === start.agent).map(r => r.span_id)).toEqual([start.span_id])
  }
  // The main loop's three requests stay under the turn's own span.
  expect(byType(rows, 'LLM_REQUEST').filter(r => r.agent === 'coding-agent').map(r => r.parent_span_id)).toEqual([turn?.span_id, turn?.span_id, turn?.span_id])
  // Each answer handed back is recorded in the turn.
  expect(byType(rows, 'USER_MESSAGE_RECEIVED').map(r => [r.trace_id, json(r.attributes).prompt_origin])).toEqual([
    [turn?.trace_id, 'peer'],
    [turn?.trace_id, 'peer'],
  ])
  // The session saw one main-loop turn, and ended outside it.
  const [end] = byType(rows, 'STATE_DELTA').filter(r => json(r.content).session_end === true)
  expect(json(end?.attributes ?? null)).toMatchObject({ state_delta: { trace_count: 1 } })
  expect([end?.invocation_id, end?.trace_id]).toEqual([null, null])
})

test('a subagent run whose SubagentStart never comes gets one inferred AGENT_STARTING, inside the turn', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  // A managed policy can keep classic hooks from a plugin a person installed.
  world.toolReply = async e => {
    if (e.tool !== 'Agent') return { result: 'ok', text: 'ok' }
    await $.turn.start({ turnId: 'sub-run-9', text: 'look around', agentId: 'agent-9' })
    await runStep($, { turnId: 'sub-run-9', index: 0, agentId: 'agent-9' })
    await $.turn.complete({ turnId: 'sub-run-9', agentId: 'agent-9', answer: 'done', durationMs: 300, isAborted: false, reason: 'answer' })
    return { result: { status: 'completed' }, text: 'done' }
  }
  await startSession($)
  await $.turn.start({ turnId: 'main-turn', text: 'delegate' })
  await $.tool.call({ tool: 'Agent', description: 'Look around', prompt: 'look around', subagent_type: 'general-purpose' })
  await world.clock.advance(5000)
  const rows = world.sentRows()
  const turns = byType(rows, 'INVOCATION_STARTING')
  expect(turns.length).toBe(1)
  const starts = byType(rows, 'AGENT_STARTING')
  expect(starts.map(r => [r.agent, r.trace_id, r.parent_span_id, r.invocation_id, json(r.attributes).inferred])).toEqual([['subagent', turns[0]?.trace_id, turns[0]?.span_id, 'main-turn', true]])
  expect(byType(rows, 'LLM_REQUEST').map(r => r.parent_span_id)).toEqual([starts[0]?.span_id])
})

test('a SubagentStart that comes after the run\'s first row names the run without opening a second span', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.turn.start({ turnId: 'main-turn', text: 'delegate' })
  await $.turn.start({ turnId: 'sub-run-5', text: 'look', agentId: 'agent-5' })
  await runStep($, { turnId: 'sub-run-5', index: 0, agentId: 'agent-5' })
  await $.classic.SubagentStart({ agent_id: 'agent-5', agent_type: 'Explore' })
  await runStep($, { turnId: 'sub-run-5', index: 1, agentId: 'agent-5' })
  await $.turn.complete({ turnId: 'sub-run-5', agentId: 'agent-5', answer: 'done', durationMs: 300, isAborted: false, reason: 'answer' })
  await world.clock.advance(5000)
  const rows = world.sentRows()
  const starts = byType(rows, 'AGENT_STARTING')
  // One start row, written (inferred) at the first request; the type is
  // known from then on.
  expect(starts.map(r => [r.agent, json(r.attributes).inferred])).toEqual([['subagent', true]])
  const span = starts[0]?.span_id
  expect(byType(rows, 'LLM_REQUEST').map(r => [r.agent, r.parent_span_id])).toEqual([
    ['subagent', span],
    ['Explore', span],
  ])
  expect(byType(rows, 'AGENT_COMPLETED').map(r => [r.agent, r.span_id])).toEqual([['Explore', span]])
})

test('a later run of the same loop nests under the turn it runs in, with a span of its own', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.turn.start({ turnId: 'turn-a', text: 'first' })
  await $.turn.start({ turnId: 'mate-run-1', text: 'check', agentId: 'mate-1' })
  await $.classic.SubagentStart({ agent_id: 'mate-1', agent_type: 'reviewer' })
  await runStep($, { turnId: 'mate-run-1', index: 0, agentId: 'mate-1' })
  await $.turn.complete({ turnId: 'mate-run-1', agentId: 'mate-1', answer: 'ok', durationMs: 100, isAborted: false, reason: 'answer' })
  await $.turn.complete({ turnId: 'turn-a', answer: 'first done', durationMs: 500, isAborted: false, reason: 'answer' })
  await $.turn.start({ turnId: 'turn-b', text: 'second' })
  await $.turn.start({ turnId: 'mate-run-2', text: 'check again', agentId: 'mate-1' })
  await runStep($, { turnId: 'mate-run-2', index: 0, agentId: 'mate-1' })
  await $.turn.complete({ turnId: 'mate-run-2', agentId: 'mate-1', answer: 'ok', durationMs: 100, isAborted: false, reason: 'answer' })
  await $.turn.complete({ turnId: 'turn-b', answer: 'second done', durationMs: 500, isAborted: false, reason: 'answer' })
  await world.clock.advance(5000)
  const rows = world.sentRows()
  const [a, b, ...more] = byType(rows, 'INVOCATION_STARTING')
  expect(more.length).toBe(0)
  const starts = byType(rows, 'AGENT_STARTING')
  expect(starts.map(r => [r.agent, r.trace_id, r.parent_span_id, r.invocation_id])).toEqual([
    ['reviewer', a?.trace_id, a?.span_id, 'turn-a'],
    ['reviewer', b?.trace_id, b?.span_id, 'turn-b'],
  ])
  expect(starts[0]?.span_id === starts[1]?.span_id).toBe(false)
  expect(byType(rows, 'LLM_REQUEST').map(r => r.parent_span_id)).toEqual([starts[0]?.span_id, starts[1]?.span_id])
})

// The settings-hook SessionStart can arrive before session.start has read
// the environment and the host version. Its row must carry the loaded
// configuration: the user, the agent name, the writer mode, the version.
const CONFIGURED = { BQAA_USER_ID: 'analyst-7', BQAA_AGENT_NAME: 'repo-agent', BQAA_DRY_RUN: 'true' }

async function sessionStartRow(world: World): Promise<Row | undefined> {
  await world.clock.advance(5000)
  const lines = [...world.files.entries()].filter(([path]) => path.startsWith(`${DRY_RUN_DIR}/dry-run-`)).flatMap(([, text]) => text.trim().split('\n'))
  return lines.map(line => JSON.parse(line) as Row).find(row => row.event_type === 'STATE_DELTA' && json(row.content).session_start === true)
}

const expectConfigured = (row: Row | undefined): void => {
  expect([row?.user_id, row?.agent]).toEqual(['analyst-7', 'repo-agent'])
  expect(json(row?.attributes ?? null)).toMatchObject({ root_agent_name: 'repo-agent', writer: { agent: 'repo-agent', mode: 'dry_run' }, session_metadata: { host_version: '2.1.296' } })
}

test('a SessionStart raised before session.start is written with the loaded configuration', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, CONFIGURED)
  await $.classic.SessionStart({ source: 'startup', session_id: SESSION_ID, model: 'example-model', cwd: '/work/my-repo' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  expectConfigured(await sessionStartRow(world))
})

test('a SessionStart raised while session.start is still setting up waits for it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, CONFIGURED)
  const starting = $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await $.classic.SessionStart({ source: 'startup', session_id: SESSION_ID, model: 'example-model', cwd: '/work/my-repo' })
  await starting
  expectConfigured(await sessionStartRow(world))
})

test('tool errors, hook denials and failed model requests are ERROR rows', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.steps.push({ result: { answer: '', toolUses: [], stopReason: null, usage: null } })
  world.toolReply = e =>
    e.tool === 'Bash' ? { isError: true, result: 'Exit code 1', text: 'Exit code 1\nnpm ERR! missing script: lint' } : { deny: 'Writes under /etc are blocked by policy.' }
  await startSession($)
  await $.turn.start({ turnId: 'turn-9', text: 'run lint' })
  await runStep($, { turnId: 'turn-9', index: 0 })
  const failed = await $.tool.call({ tool: 'Bash', command: 'npm run lint' })
  const denied = await $.tool.call({ tool: 'Write', file_path: '/etc/hosts', content: 'x' })
  await $.turn.complete({ turnId: 'turn-9', answer: '', durationMs: 900, isAborted: false, reason: 'error' })
  await world.clock.advance(5000)

  // The mod hands every result back exactly as it came.
  expect(failed.isError).toBe(true)
  expect(failed.text).toBe('Exit code 1\nnpm ERR! missing script: lint')
  expect(denied.deny).toBe('Writes under /etc are blocked by policy.')

  const rows = world.sentRows()
  const [llmError] = byType(rows, 'LLM_ERROR')
  expect(llmError?.status).toBe('ERROR')
  expect(llmError?.error_message ?? '').toMatch(/no response/)
  const [bash, write] = byType(rows, 'TOOL_COMPLETED')
  expect(bash?.status).toBe('ERROR')
  expect(bash?.error_message).toBe('Exit code 1\nnpm ERR! missing script: lint')
  expect(write?.status).toBe('ERROR')
  expect(write?.error_message).toBe('Writes under /etc are blocked by policy.')
  expect(json(write?.attributes ?? null)).toMatchObject({ denied: true })
  const [turnError] = byType(rows, 'INVOCATION_ERROR')
  expect(turnError?.status).toBe('ERROR')
  expect(turnError?.error_message).toBe('the turn ended on an API error')
  expect(byType(rows, 'AGENT_RESPONSE').length).toBe(0)
})

test('a permission prompt is flagged on the tool row it held up', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  let ask: Promise<unknown> | null = null
  world.toolReply = e => {
    if (e.tool === 'Bash') ask = $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: { command: 'rm -rf build' } })
    return { result: '', text: '' }
  }
  await startSession($)
  await $.turn.start({ turnId: 'turn-2', text: 'clean' })
  await $.tool.call({ tool: 'Bash', command: 'rm -rf build' })
  await ask
  await $.tool.call({ tool: 'Read', file_path: 'README.md' })
  await world.clock.advance(5000)
  const [bash, read] = byType(world.sentRows(), 'TOOL_COMPLETED')
  expect(json(bash?.attributes ?? null).permission_requested).toBe(true)
  expect(json(read?.attributes ?? null).permission_requested).toBeUndefined()
})

test('a prompt typed during a turn is recorded against that turn', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.turn.start({ turnId: 'turn-3', text: 'refactor the writer' })
  await $.prompt.submit({ text: 'also keep the tests green', wait: false, origin: { kind: 'composer' }, turnId: 'turn-3' })
  await world.clock.advance(5000)
  const [prompt] = byType(world.sentRows(), 'USER_MESSAGE_RECEIVED')
  const [turn] = byType(world.sentRows(), 'INVOCATION_STARTING')
  expect(prompt?.invocation_id).toBe('turn-3')
  expect(prompt?.trace_id).toBe(turn?.trace_id ?? 'missing')
  expect(json(prompt?.attributes ?? null)).toMatchObject({ submitted_during_turn: true })
})

test('session end is recorded with its reason, turn count and duration', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  await startSession($)
  await $.turn.start({ turnId: 'turn-1', text: 'hi' })
  await $.turn.complete({ turnId: 'turn-1', answer: 'Hello.', durationMs: 800, isAborted: false, reason: 'answer' })
  await world.clock.advance(5000)
  // Session end starts no request: its row goes to a spill file the next
  // session sends.
  await $.session.end({ reason: 'prompt_input_exit', sessionId: SESSION_ID, resume: { id: SESSION_ID } })
  const ends = byType(deadLetterRows(world), 'STATE_DELTA').filter(r => json(r.content).session_end === true)
  expect(ends.length).toBe(1)
  expect(json(ends[0]?.attributes ?? null)).toMatchObject({ state_delta: { source: 'coding_agent_session_end', reason: 'prompt_input_exit', trace_count: 1 } })
})

test('a model request from a turn the mod never saw start does not take over the current turn', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.steps.push({ result: { answer: 'side', stopReason: 'end_turn' } })
  await startSession($)
  await $.turn.start({ turnId: 'turn-a', text: 'first' })
  await runStep($, { turnId: 'turn-unknown', index: 0 })
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  const rows = world.sentRows()
  const [turn] = byType(rows, 'INVOCATION_STARTING')
  const [stranger] = byType(rows, 'LLM_REQUEST')
  const [tool] = byType(rows, 'TOOL_STARTING')
  expect(stranger?.invocation_id).toBe('turn-unknown')
  expect(stranger?.trace_id).not.toBe(turn?.trace_id ?? 'missing')
  // The tool still belongs to the turn that started.
  expect(tool?.invocation_id).toBe('turn-a')
  expect(tool?.parent_span_id).toBe(turn?.span_id ?? 'missing')
})
