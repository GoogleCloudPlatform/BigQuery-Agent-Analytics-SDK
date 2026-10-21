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

// A fixture session run through the mod, end to end, whose insertAll rows
// (the session-end row from its spill file) are printed (stderr, one
// `BQAA_SAMPLE_ROW <json>` line each) so the README and
// the demo page can show real output of the mod; the README's "Sample rows"
// section has the command that regenerates examples/sample-rows.jsonl.
//
// The model id, token counts and the turn's duration are fixture inputs. Model and tool
// latencies are what this run measured: nothing really runs, so they are a
// few milliseconds.

import { expect, test } from 'claude-code/testing'

import { OPTIONS, SESSION_ID, byType, deadLetterRows, runStep, setUp, startSession } from './helpers.ts'

// The test runner has a console (its output goes to stderr); the hooks
// environment's declarations do not list one.
declare const console: { log: (...args: unknown[]) => void }

test('fixture session: prompt, four model requests, tools, a subagent, session end', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, { USER: 'dev' })
  const usage = (input: number, output: number, read: number, write: number) => ({
    input_tokens: input,
    output_tokens: output,
    cache_read_input_tokens: read,
    cache_creation_input_tokens: write,
    model: 'example-model',
  })
  const turnId = '3f0e2a71-9c4d-4b8e-a1f6-52d7c0e9b134'
  world.promptEntered = text => $.turn.start({ turnId, text }).then(() => undefined)
  world.toolReply = async e => {
    if (e.tool === 'Read') return { result: { type: 'text' }, text: 'export async function flushOnce(queue: Row[]) {\n  const batch = queue.splice(0, 500)\n  await insertAll(batch)\n}\n' }
    if (e.tool === 'Agent') {
      // The subagent runs while its Agent call is open, as in a session: its
      // run starts with a turn.start that names its loop, then the
      // settings-hook SubagentStart names its type.
      await $.turn.start({ turnId: 'a91c7e2-turn-1', text: 'Where is flushOnce called?', agentId: 'a91c7e2' })
      await $.classic.SubagentStart({ agent_id: 'a91c7e2', agent_type: 'Explore' })
      world.steps.push({
        result: { answer: 'flushOnce is called from two places: the interval timer and /flush.', stopReason: 'end_turn', usage: { ...usage(1_902, 141, 0, 3_377), model: 'example-model' } },
      })
      await runStep($, { turnId: 'a91c7e2-turn-1', index: 0, messageCount: 1, agentId: 'a91c7e2', model: 'example-model' })
      await $.turn.complete({
        turnId: 'a91c7e2-turn-1',
        agentId: 'a91c7e2',
        answer: 'flushOnce is called from two places: the interval timer and /flush.',
        durationMs: 6_480,
        isAborted: false,
        reason: 'answer',
        usage: { ...usage(1_902, 141, 0, 3_377), model: 'example-model' },
      })
      return { result: { status: 'completed' }, text: 'flushOnce is called from two places: the interval timer and /flush.' }
    }
    if (e.tool === 'Edit') return { result: { filePath: 'src/writer.ts' }, text: 'The file src/writer.ts has been updated.' }
    // The person is asked before the shell command runs.
    await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: { command: 'npm test -- writer', description: 'Run the writer tests' } })
    return { result: { stdout: '12 pass\n0 fail' }, text: '12 pass\n0 fail' }
  }

  await startSession($)
  await $.prompt.submit({ text: 'Add retries with backoff to flushOnce in src/writer.ts, then run the writer tests.', wait: false, origin: { kind: 'composer' } })
  world.steps.push({
    chunks: [
      { kind: 'thinking', index: 0, text: 'Read the writer first.' },
      { kind: 'text', index: 1, text: "I'll look at the flush loop first." },
      { kind: 'tool', index: 2, id: 'toolu_01', name: 'Read' },
    ],
    result: {
      answer: "I'll look at the flush loop first.",
      toolUses: [{ name: 'Read', input: { file_path: 'src/writer.ts' } }],
      stopReason: 'tool_use',
      usage: usage(2310, 96, 0, 18_544),
    },
  })
  await runStep($, { turnId, index: 0, messageCount: 1 })
  await $.tool.call({ tool: 'Read', file_path: 'src/writer.ts' })
  world.steps.push({ result: { answer: '', toolUses: [{ name: 'Agent', input: {} }], stopReason: 'tool_use', usage: usage(4, 0, 18_544, 1_210) } })
  await runStep($, { turnId, index: 1, messageCount: 3 })
  await $.tool.call({ tool: 'Agent', description: 'Find flushOnce callers', prompt: 'Where is flushOnce called?', subagent_type: 'Explore' })
  world.steps.push({
    result: { answer: '', toolUses: [{ name: 'Edit', input: {} }, { name: 'Bash', input: {} }], stopReason: 'tool_use', usage: usage(6, 388, 19_754, 2_031) },
  })
  await runStep($, { turnId, index: 2, messageCount: 5 })
  await $.tool.call({ tool: 'Edit', file_path: 'src/writer.ts', old_string: 'await insertAll(batch)', new_string: 'await withBackoff(() => insertAll(batch))' })
  await $.tool.call({ tool: 'Bash', command: 'npm test -- writer', description: 'Run the writer tests' })
  world.steps.push({
    chunks: [{ kind: 'text', index: 0, text: 'Added exponential backoff to flushOnce; the 12 writer tests pass.' }],
    result: { answer: 'Added exponential backoff to flushOnce; the 12 writer tests pass.', stopReason: 'end_turn', usage: usage(5, 61, 21_785, 640) },
  })
  await runStep($, { turnId, index: 3, messageCount: 9 })
  await $.turn.complete({
    turnId,
    answer: 'Added exponential backoff to flushOnce; the 12 writer tests pass.',
    durationMs: 48_211,
    isAborted: false,
    reason: 'answer',
    usage: usage(2_325, 545, 60_083, 22_215),
  })
  await world.clock.advance(5000)
  await $.session.end({ reason: 'prompt_input_exit', sessionId: SESSION_ID, resume: { id: SESSION_ID } })

  // What insertAll took, then the session-end row: session end writes it to
  // a spill file, unchanged, for the next session to send.
  const rows = [...world.acceptedRows(), ...deadLetterRows(world)]
  expect(byType(rows, 'LLM_RESPONSE').length).toBe(5)
  expect(byType(rows, 'TOOL_COMPLETED').length).toBe(4)
  expect(byType(rows, 'AGENT_COMPLETED').length).toBe(1)
  expect(rows.at(-1)?.event_type).toBe('STATE_DELTA')
  for (const row of rows) console.log(`BQAA_SAMPLE_ROW ${JSON.stringify(row)}`)
})
