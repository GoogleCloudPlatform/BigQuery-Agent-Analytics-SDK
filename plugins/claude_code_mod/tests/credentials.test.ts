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

// When rows cannot be written for lack of credentials or permission: the
// band above the prompt, the toast, the transcript line and the status line,
// each with the fix and the rows waiting; recovery soon after the fix; and
// prompts passed through whatever the credentials do, the retired `block`
// option included.

import { type Engine, expect, test } from 'claude-code/testing'

import { COMMAND, OK_INSERT, OPTIONS, type World, setUp, startSession } from './helpers.ts'

const PLUGIN = 'bigquery-agent-analytics-mod'

// What the host passes a hook drawing in the band above the prompt.
const BAND = {
  component: 'AbovePrompt',
  requestId: 'above-prompt',
  viewport: { columns: 120, rows: 40 },
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 115, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const

// gcloud with no application-default sign-in: a WARNING line first, then
// the ERROR line, then advice.
const NO_SIGN_IN = 'ERROR: (gcloud.auth.application-default.print-access-token) Your default credentials were not found. To set up Application Default Credentials, see https://cloud.google.com/docs/authentication/external/set-up-adc for more information.'
const GCLOUD_FAILS = {
  exitCode: 1,
  stdout: '',
  stderr: `WARNING: The project property is not set; the account's default applies.\n${NO_SIGN_IN}\nTo sign in, use the login command of gcloud auth application-default.\n`,
}
const UNAUTHENTICATED = { status: 401, text: '{"error":{"code":401,"message":"Request had invalid authentication credentials.","status":"UNAUTHENTICATED"}}' }
const DENIED = {
  status: 403,
  text: '{"error":{"code":403,"message":"Access Denied: Table my-gcp-project:agent_analytics.agent_events: Permission bigquery.tables.updateData denied","errors":[{"reason":"accessDenied"}],"status":"PERMISSION_DENIED"}}',
}

const FIX = 'run: gcloud auth application-default login'
const LINE = `rows are not being written: no working Google credentials. To fix, ${FIX}.`
const PROMPT = { text: 'hi', wait: false, origin: { kind: 'composer' } } as const

const statusText = async ($: Engine): Promise<string> => (await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })).text ?? ''

// The band as the terminal would draw it: the mod's line, if any, and the
// text the mods after it drew.
async function band($: Engine, props: { hasSurvey?: boolean } = {}): Promise<{ line?: string; waiting?: string; beneath: boolean }> {
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND, props: { ...BAND.props, ...props } })
  const line = (await ui.find({ type: 'Text', text: /^rows are not being written/ }))?.text
  const waiting = (await ui.find({ type: 'Text', text: /row\(s\) waiting/ }))?.text
  const beneath = (await ui.find({ type: 'Text', text: 'drawn beneath' })) !== undefined
  await ui.unmount()
  return { line, waiting, beneath }
}

const noticeLines = (world: World): string[] => world.transcript.filter(l => l.startsWith('bqaa: rows are not being written'))

test('without working credentials, the band, one toast and one transcript line give the fix and the rows waiting', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.processReply = GCLOUD_FAILS
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(world.fetches.length).toBe(0)
  // The session-start row and the call's two rows wait.
  expect(await band($)).toEqual({ line: LINE, waiting: ' 3 row(s) waiting.', beneath: true })
  expect(world.toasts).toEqual([`BQAA: ${LINE}`])
  expect(world.statusLines.at(-1)).toBe(`BQAA: no working Google credentials. To fix, ${FIX} (3 row(s) waiting)`)
  // The transcript line has the reason: gcloud's ERROR line, not its
  // WARNING line or its instructions.
  const [notice] = noticeLines(world)
  expect(notice).toContain(`exited 1: ${NO_SIGN_IN}`)
  expect(notice).not.toContain('WARNING')
  expect(notice).not.toContain('To sign in, use')
  // More failures, more rows: one toast and one transcript line still; the
  // count follows the rows, parked on disk ones included.
  for (let t = 0; t < 6; t += 1) {
    await $.tool.call({ tool: 'Read', file_path: `b${t}.ts` })
    await world.clock.advance(5000)
  }
  expect(world.toasts.length).toBe(1)
  expect(noticeLines(world).length).toBe(1)
  expect((await band($)).waiting).toBe(' 15 row(s) waiting.')
  expect(await statusText($)).toMatch(/notice \(since .*\): rows are not being written: no working Google credentials; to fix, run: gcloud auth application-default login; 15 row\(s\) waiting; cause: authentication: token command .gcloud. exited 1: ERROR: /)
})

test('the notice clears on the next successful send, and the rows go out', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const working = world.processReply
  world.processReply = GCLOUD_FAILS
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect((await band($)).line).toBe(LINE)
  // Signed in again: the next try sends, and the band, the status line and
  // /bqaa-status no longer show a notice.
  world.processReply = working
  await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(3)
  expect(await band($)).toEqual({ line: undefined, waiting: undefined, beneath: true })
  expect(world.statusLines.at(-1)).toBeUndefined()
  expect(await statusText($)).not.toMatch(/notice/)
})

test('signed in again with nothing new to send, the rows parked meanwhile go out on their own, and the notice clears', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const working = world.processReply
  world.processReply = GCLOUD_FAILS
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  // Until every row is parked and none is queued: a session left idle.
  for (let t = 0; t < 30 && !/queue: 0 rows/.test(await statusText($)); t += 1) await world.clock.advance(5000)
  expect(await statusText($)).toMatch(/queue: 0 rows/)
  expect((await band($)).waiting).toBe(' 3 row(s) waiting.')
  // Signed in from another terminal; no prompt follows.
  world.processReply = working
  for (let t = 0; t < 8; t += 1) await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(3)
  expect((await band($)).line).toBeUndefined()
  expect(world.statusLines.at(-1)).toBeUndefined()
})

test('the band yields to a survey, and keeps what the mods after it draw', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.processReply = GCLOUD_FAILS
  await startSession($)
  await world.clock.advance(5000)
  expect(await band($, { hasSurvey: true })).toEqual({ line: undefined, waiting: undefined, beneath: true })
  expect((await band($)).line).toBe(LINE)
})

test('a 401 gets one immediate retry with a fresh token, with no notice', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAUTHENTICATED, OK_INSERT]
  await startSession($)
  await world.clock.advance(5000)
  // Both requests in the same flush: no backoff between them.
  expect(world.insertCalls().map(f => f.answered)).toEqual([401, 200])
  expect(world.commands.length).toBe(2)
  expect(world.acceptedRows().length).toBe(1)
  expect(world.toasts.length).toBe(0)
  expect(world.statusLines.at(-1)).toBeUndefined()
})

test('BigQuery refusing a fresh token too is a credentials notice, cleared once a token works', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAUTHENTICATED, UNAUTHENTICATED, OK_INSERT]
  await startSession($)
  await world.clock.advance(5000)
  expect(world.insertCalls().length).toBe(2)
  expect((await band($)).line).toBe(LINE)
  expect(noticeLines(world)[0]).toContain('BigQuery refused a fresh token too: HTTP 401: Request had invalid authentication credentials.')
  await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(1)
  expect((await band($)).line).toBeUndefined()
})

test('a missing permission is a notice with the role to ask for; /bqaa-backfill is named once rows flow again', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.replies = [DENIED, OK_INSERT]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  const shown = await band($)
  expect(shown.line).toBe('rows are not being written: BigQuery denied the identity access to the dataset. To fix, ask for roles/bigquery.dataEditor on my-gcp-project.agent_analytics, then run /bqaa-backfill.')
  expect(shown.waiting).toBe(' 3 row(s) waiting.')
  expect(world.toasts.length).toBe(1)
  // Access granted: new rows land, the band clears, and the transcript says
  // the refused ones wait for /bqaa-backfill (they are not replayed alone).
  await $.tool.call({ tool: 'Read', file_path: 'b.ts' })
  await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(2)
  expect((await band($)).line).toBeUndefined()
  expect(world.transcript).toContain('bqaa: rows are being written again; /bqaa-backfill sends the 3 row(s) parked while they could not be.')
})

test('while credentials fail, the backoff before the next retry is capped at 30 s', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  world.processReply = GCLOUD_FAILS
  await startSession($)
  // Fail, with rows to send at every tick, until the backoff would pass a
  // minute; read the wait right after the failure that set it.
  let failures = 0
  let waitSeconds = -1
  for (let t = 0; t < 120 && waitSeconds < 0; t += 1) {
    await $.tool.call({ tool: 'Read', file_path: `f${t}.ts` })
    await world.clock.advance(5000)
    const status = await statusText($)
    const now = Number(/backing off: (\d+) failure/.exec(status)?.[1] ?? 0)
    if (now > failures && now >= 8) waitSeconds = Number(/next try in about (\d+)s/.exec(status)?.[1] ?? -1)
    failures = now
  }
  expect(waitSeconds > 0).toBe(true)
  expect(waitSeconds <= 30).toBe(true)
})

test('a prompt while credentials fail retries at once, from a timer: the hook itself waits on nothing', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on)
  const working = world.processReply
  world.processReply = GCLOUD_FAILS
  await startSession($)
  for (let t = 0; t < 6; t += 1) {
    await $.tool.call({ tool: 'Read', file_path: `f${t}.ts` })
    await world.clock.advance(5000)
  }
  // Signed in from the session (`! gcloud auth application-default login`).
  world.processReply = working
  const commands = world.commands.length
  const result = await $.prompt.submit(PROMPT)
  expect(result.text).toBe('hi')
  expect(world.commands.length).toBe(commands)
  expect(world.fetches.length).toBe(0)
  // No time passes: the retry runs as soon as the hook has returned.
  await world.clock.settle()
  expect(world.acceptedRows().some(r => r.event_type === 'USER_MESSAGE_RECEIVED')).toBe(true)
  expect((await band($)).line).toBeUndefined()
})

// A settings file written for an earlier version can still say
// `onAuthFailure: block`. Nothing reads it now: whatever the credentials do,
// prompt.submit passes the prompt on, unchanged, without waiting on the
// network or a process. Each test below runs with that leftover setting.
const LEFTOVER = { ...OPTIONS, onAuthFailure: 'block' }

// Fires a prompt and checks that the hook passed it on and reached nothing
// outside while it ran: no request, no process. The mock clock does not move
// here, so a hook that waited on either would never answer.
async function promptPassesOn($: Engine, world: World): Promise<void> {
  const fetches = world.fetches.length
  const commands = world.commands.length
  const result = await $.prompt.submit(PROMPT)
  expect(result.drop).toBeUndefined()
  expect(result.text).toBe('hi')
  expect(world.fetches.length).toBe(fetches)
  expect(world.commands.length).toBe(commands)
}

test('prompt.submit passes the prompt on, waiting on nothing, when the token command fails', { options: LEFTOVER }, async ($, on) => {
  const world = setUp(on)
  world.processReply = GCLOUD_FAILS
  await startSession($)
  // Before any token was asked for, and once the failure is known.
  await promptPassesOn($, world)
  await world.clock.advance(5000)
  expect((await band($)).line).toBe(LINE)
  await promptPassesOn($, world)
})

test('prompt.submit passes the prompt on, waiting on nothing, when BigQuery refuses the credentials', { options: LEFTOVER }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAUTHENTICATED]
  await startSession($)
  await world.clock.advance(5000)
  // Refused, and refused again with a fresh token: the notice stands.
  expect(world.insertCalls().map(f => f.answered)).toEqual([401, 401])
  expect((await band($)).line).toBe(LINE)
  await promptPassesOn($, world)
})

test('prompt.submit passes the prompt on while the token command and BigQuery never answer', { options: LEFTOVER }, async ($, on) => {
  const world = setUp(on)
  // A day on the mock clock, which never moves here.
  world.processDelayMs = 24 * 60 * 60 * 1000
  world.replies = [{ hang: true }]
  await startSession($)
  await promptPassesOn($, world)
})

test('prompt.submit passes the prompt on while a credentials notice stands and BigQuery hangs', { options: LEFTOVER }, async ($, on) => {
  const world = setUp(on)
  world.replies = [UNAUTHENTICATED, UNAUTHENTICATED, { hang: true }]
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).line).toBe(LINE)
  await promptPassesOn($, world)
})

test('with BQAA_ACCESS_TOKEN refused, the fix is a fresh token or the command', { options: OPTIONS }, async ($, on) => {
  // Joined at run time, like every credential-shaped test value (helpers.ts).
  const world = setUp(on, { BQAA_ACCESS_TOKEN: ['ya29', 'stale-environment-token-0123456789'].join('.') })
  world.replies = [UNAUTHENTICATED]
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).line).toBe('rows are not being written: no working Google credentials. To fix, set a fresh BQAA_ACCESS_TOKEN, or unset it to use the token command.')
  expect(world.commands.length).toBe(0)
})

test("with gcloud's own account as the token command, the fix is gcloud auth login", { options: { ...OPTIONS, accessTokenCommand: 'gcloud auth print-access-token' } }, async ($, on) => {
  const world = setUp(on)
  world.processReply = { exitCode: 1, stdout: '', stderr: 'ERROR: (gcloud.auth.print-access-token) You do not currently have an active account selected.\n' }
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).line).toBe('rows are not being written: no working Google credentials. To fix, run: gcloud auth login.')
})
