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

// The welcome card: in the first session after the mod is installed or
// updated, the band above the prompt says that the mod is on, what it
// records and where to, and then that the first rows landed there; a
// transcript line says the same, and a toast says when the rows land. The
// next prompt after they land, or Got it, closes it, and no later session
// shows it. A problem notice has the band first; a run with no one at the
// prompt shows nothing and leaves the store alone; nothing waits on the
// store for the card, session.start included. The greeting record names
// the version greeted and the session; the card's own record, which only
// that card writes, what became of it, written in order, so that a write
// that answers late never undoes a close. The card shows one tick of the
// clock after session.start (the check that this load is still the one the
// host runs), so a test settles the clock before it looks.

import { type Engine, expect, test } from 'claude-code/testing'

import { PLUGIN_VERSION, resolveConfig } from '../hooks/lib/config.ts'
import { packFiles } from '../hooks/lib/deadletter.ts'
import { buildRow } from '../hooks/lib/rows.ts'
import { type Io, Writer } from '../hooks/lib/writer.ts'

import { COMMAND, DRY_RUN_DIR, OK_INSERT, OPTIONS, SESSION_ID, TABLE_REF, type World, deadLetterFiles, setUp, startSession } from './helpers.ts'

const PLUGIN = 'bigquery-agent-analytics-mod'

// What the host passes a hook drawing in the band above the prompt.
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

const TABLE = 'my-gcp-project.agent_analytics.agent_events'
const WHAT = 'sessions, turns, model requests and tool calls'
const RECORDS = `Records ${WHAT} to ${TABLE} (US).`
const WAITING = '◌ Waiting for the first rows, sent every 5 s while you work · /bqaa-status shows the counts'
const landed = (sent: number): string => `✓ First rows landed in BigQuery · ${sent} sent · /bqaa-status shows the counts`
const INSTALLED = ['✓ ', 'BigQuery Agent Analytics mod installed', ` ${PLUGIN_VERSION}`]
const NOT_SET_UP = 'the project and dataset options are set in /config, or BQAA_PROJECT_ID and BQAA_DATASET'
const PROMPT = { text: 'hi', wait: false, origin: { kind: 'composer' } } as const
const CLOSE = 'bqaa-welcome-close'
// Where rows land under OPTIONS, as the record names it.
const HERE = `${TABLE} (US)`

// The band as the terminal would draw it: every Text in order (the card's
// glyph, title, version and lines, then what the mods after this one drew),
// whether the card is boxed, and its Got it button.
async function band($: Engine, props: { hasSurvey?: boolean; maxRows?: number } = {}): Promise<{ texts: string[]; boxed: boolean; button?: string }> {
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND, props: { ...BAND.props, ...props } })
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  const boxed = (await ui.findAll({ type: 'Box' })).some(b => b.props.borderStyle === 'round')
  const button = (await ui.find({ type: 'Button', key: CLOSE }))?.props.label as string | undefined
  await ui.unmount()
  return { texts, boxed, button }
}

const welcomeLines = (lines: string[]): string[] => lines.filter(l => l.startsWith('bqaa: BigQuery Agent Analytics mod'))
// The records: the greeting written last, this version's greeting, and this
// session's card, its landing and its close.
const stored = (world: World): unknown => world.store.get('welcome')
const GREETED_KEY = `welcome:${PLUGIN_VERSION}`
const greeted = (world: World): unknown => world.store.get(GREETED_KEY)
const OWN = `welcome:${PLUGIN_VERSION}:${SESSION_ID}`
const own = (world: World): unknown => world.store.get(OWN)
const CLOSED = `${OWN}:closed`
const closedMark = (world: World): unknown => world.store.get(CLOSED)
const GREETING = { version: PLUGIN_VERSION, previous: null, sessionId: SESSION_ID }
const GREETED_HERE = { sessionId: SESSION_ID, previous: null }
const START = { surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' } as const
const statusText = async ($: Engine): Promise<string> => (await $.command.run({ command: 'bqaa-status', args: '', ...COMMAND })).text ?? ''

test('a fresh install shows the welcome card, then that the first rows landed, and the next prompt closes it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  expect(await band($)).toEqual({ texts: [...INSTALLED, RECORDS, WAITING, 'drawn beneath'], boxed: true, button: 'Got it' })
  expect(welcomeLines(world.transcript)).toEqual([`bqaa: BigQuery Agent Analytics mod installed (${PLUGIN_VERSION}): records ${WHAT} to ${TABLE} (US); /bqaa-status shows what was sent.`])
  // Recorded at once: no later session shows it.
  expect(greeted(world)).toEqual(GREETED_HERE)
  expect(stored(world)).toBeUndefined()
  expect(own(world)).toBeUndefined()
  // The session-start row lands.
  await world.clock.advance(5000)
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, landed(1), 'drawn beneath'])
  expect(world.toasts).toEqual([`BQAA: first rows landed in ${TABLE}`])
  expect(typeof (own(world) as { landedAt: unknown }).landedAt).toBe('number')
  expect(own(world)).toMatchObject({ landedTo: HERE })
  expect(closedMark(world)).toBeUndefined()
  // The count moves with each send; one toast.
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, landed(3), 'drawn beneath'])
  expect(world.toasts.length).toBe(1)
  // The person's next prompt: the card has done its job.
  await $.prompt.submit(PROMPT)
  expect(await band($)).toEqual({ texts: ['drawn beneath'], boxed: false, button: undefined })
  await world.clock.settle()
  expect(closedMark(world)).toBe(true)
  expect(greeted(world)).toEqual(GREETED_HERE)
  await world.clock.advance(5000)
  expect(world.toasts.length).toBe(1)
  expect(welcomeLines(world.transcript).length).toBe(1)
})

test('a band drawn before the session started shows the card at once, before any row lands', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect((await ui.findAll({ type: 'Text' })).map(t => t.text)).toEqual(['drawn beneath'])
  await startSession($)
  await world.clock.settle()
  expect((await ui.findAll({ type: 'Text' })).map(t => t.text)).toEqual([...INSTALLED, RECORDS, WAITING, 'drawn beneath'])
  await ui.unmount()
  expect(world.acceptedRows().length).toBe(0)
})

const SHOWN_BEFORE = [
  ['an earlier session showed the card', { welcome: { version: PLUGIN_VERSION, previous: null, sessionId: 'an-earlier-session' }, [GREETED_KEY]: { sessionId: 'an-earlier-session', previous: null } }],
  ['this session showed it and it was closed', { welcome: GREETING, [GREETED_KEY]: GREETED_HERE, [OWN]: { landedAt: 1, landedTo: HERE }, [CLOSED]: true }],
  ['this session closed it, its greeting never written', { [CLOSED]: true }],
  ['an earlier session showed it, and a late write of an older version came after', { welcome: { version: '0.0.9', previous: null, sessionId: 'an-older-session' }, [GREETED_KEY]: { sessionId: 'an-earlier-session', previous: '0.0.9' } }],
] as const

for (const [name, records] of SHOWN_BEFORE) {
  test(`no card, line or toast when ${name}`, { options: OPTIONS }, async ($, on) => {
    const world = setUp(on, {}, records)
    await startSession($)
    await world.clock.advance(5000)
    expect(world.acceptedRows().length).toBe(1)
    expect((await band($)).texts).toEqual(['drawn beneath'])
    expect(welcomeLines(world.transcript)).toEqual([])
    expect(world.toasts).toEqual([])
    expect(Object.fromEntries([...world.store].filter(([key]) => key.startsWith('welcome')))).toEqual(records)
  })
}

test('a reload in the session that showed the card shows it again as it was, with no second line or toast', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, { welcome: GREETING, [GREETED_KEY]: GREETED_HERE, [OWN]: { landedAt: 1, landedTo: HERE } })
  await startSession($)
  await world.clock.settle()
  // Rebuilt from the store's records alone (no `$.state`), which keep no
  // count: none until rows land again.
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, '✓ First rows landed in BigQuery · /bqaa-status shows the counts', 'drawn beneath'])
  await world.clock.advance(5000)
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, landed(1), 'drawn beneath'])
  expect(welcomeLines(world.transcript)).toEqual([])
  expect(world.toasts).toEqual([])
})

test('an update greets the new version and names the one it replaced', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, { welcome: { version: '0.0.9', previous: null, sessionId: 'an-earlier-session' } })
  await startSession($)
  await world.clock.settle()
  expect((await band($)).texts).toEqual(['✓ ', `BigQuery Agent Analytics mod updated to ${PLUGIN_VERSION}`, ' was 0.0.9', RECORDS, WAITING, 'drawn beneath'])
  expect(welcomeLines(world.transcript)).toEqual([`bqaa: BigQuery Agent Analytics mod updated to ${PLUGIN_VERSION} (from 0.0.9): records ${WHAT} to ${TABLE} (US); /bqaa-status shows what was sent.`])
  expect(greeted(world)).toEqual({ sessionId: SESSION_ID, previous: '0.0.9' })
  // The record an earlier version kept is read, never written.
  expect(stored(world)).toEqual({ version: '0.0.9', previous: null, sessionId: 'an-earlier-session' })
})

test('pre-releases are ordered part by part: rc.10 comes after rc.2', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, { 'welcome:0.1.0-rc.2': { sessionId: 'an-older-session', previous: null }, 'welcome:0.1.0-rc.10': { sessionId: 'an-earlier-session', previous: '0.1.0-rc.2' } })
  await startSession($)
  await world.clock.settle()
  expect((await band($)).texts.slice(1, 3)).toEqual([`BigQuery Agent Analytics mod updated to ${PLUGIN_VERSION}`, ' was 0.1.0-rc.10'])
})

test('the card names, as the version it replaced, the newest one greeted before', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, { 'welcome:0.0.8': { sessionId: 'an-older-session', previous: null }, 'welcome:0.0.9': { sessionId: 'an-earlier-session', previous: '0.0.8' }, [`welcome:0.0.9:an-earlier-session`]: { landedAt: 1, landedTo: HERE } })
  await startSession($)
  await world.clock.settle()
  expect((await band($)).texts.slice(1, 3)).toEqual([`BigQuery Agent Analytics mod updated to ${PLUGIN_VERSION}`, ' was 0.0.9'])
})

for (const [previous, word] of [['0.1.0-rc.1', 'updated'], ['nightly', 'changed']] as const) {
  test(`after ${previous}, the card says ${word}: updated only to a version known to be newer`, { options: OPTIONS }, async ($, on) => {
    const world = setUp(on, {}, { welcome: { version: previous, previous: null, sessionId: 'an-earlier-session' } })
    await startSession($)
    await world.clock.settle()
    expect((await band($)).texts.slice(1, 3)).toEqual([`BigQuery Agent Analytics mod ${word} to ${PLUGIN_VERSION}`, ` was ${previous}`])
  })
}

test('a change back to an older version is greeted as a change, not an update', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, { welcome: { version: '99.0.0', previous: PLUGIN_VERSION, sessionId: 'an-earlier-session' } })
  await startSession($)
  await world.clock.settle()
  expect((await band($)).texts.slice(0, 3)).toEqual(['✓ ', `BigQuery Agent Analytics mod changed to ${PLUGIN_VERSION}`, ' was 99.0.0'])
  expect(welcomeLines(world.transcript)).toEqual([`bqaa: BigQuery Agent Analytics mod changed to ${PLUGIN_VERSION} (from 99.0.0): records ${WHAT} to ${TABLE} (US); /bqaa-status shows what was sent.`])
})

test('not set up: the card says what to set, stays after a prompt, and Got it closes it for good', { options: {} }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  const card = ['◇ ', 'BigQuery Agent Analytics mod installed, not set up yet', ` ${PLUGIN_VERSION}`, `Nothing is recorded until ${NOT_SET_UP}.`, "Then make the dataset with the mod's setup.sh; its README shows how."]
  expect(await band($)).toEqual({ texts: [...card, 'drawn beneath'], boxed: true, button: 'Got it' })
  expect(welcomeLines(world.transcript)).toEqual([`bqaa: BigQuery Agent Analytics mod installed (${PLUGIN_VERSION}), not set up yet: nothing is recorded until ${NOT_SET_UP}.`])
  // No rows can land, so a prompt leaves it up.
  await $.prompt.submit(PROMPT)
  await world.clock.advance(5000)
  expect((await band($)).texts).toEqual([...card, 'drawn beneath'])
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  await ui.press({ key: CLOSE })
  expect((await ui.findAll({ type: 'Text' })).map(t => t.text)).toEqual(['drawn beneath'])
  await ui.unmount()
  await world.clock.settle()
  expect(closedMark(world)).toBe(true)
})

test('a missing dataset has the band first; once rows land, the card says so', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  world.replies = [DATASET_MISSING]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  const during = (await band($)).texts
  expect(during.some(t => t.startsWith('rows are not being written: BigQuery cannot find the dataset'))).toBe(true)
  expect(during.some(t => t.startsWith('BigQuery Agent Analytics mod'))).toBe(false)
  // setup.sh has made it, and BigQuery finds it.
  world.replies = [OK_INSERT]
  for (let t = 0; t < 8; t += 1) await world.clock.advance(5000)
  const sent = world.acceptedRows().length
  expect(sent).toBe(3)
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, landed(3), 'drawn beneath'])
  expect(world.toasts.length).toBe(2)
  expect(world.toasts.at(-1)).toBe(`BQAA: first rows landed in ${TABLE}`)
})

test('rows parked while the dataset was missing land by replay in an idle session, and the card says so', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  world.replies = [DATASET_MISSING]
  await startSession($)
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  // Until every row is parked and none is queued.
  for (let t = 0; t < 30 && !/queue: 0 rows/.test(await statusText($)); t += 1) await world.clock.advance(5000)
  expect(deadLetterFiles(world).length > 0).toBe(true)
  // The dataset is made, and nothing new is queued: only the replay sends.
  world.replies = [OK_INSERT]
  for (let t = 0; t < 8; t += 1) await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(3)
  expect(deadLetterFiles(world).length).toBe(0)
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, landed(3), 'drawn beneath'])
  expect(world.toasts.at(-1)).toBe(`BQAA: first rows landed in ${TABLE}`)
})

test('a session whose id is not known when it starts shows no card and greets no one: Got it could keep no close of it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  // The host refuses the id, and no SessionStart named it before.
  world.sessionIdRefused = true
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await world.clock.settle()
  expect(await band($)).toEqual({ texts: ['drawn beneath'], boxed: false, button: undefined })
  expect(welcomeLines(world.transcript)).toEqual([])
  expect(greeted(world)).toBeUndefined()
  expect([...world.store.keys()].filter(key => key.startsWith('welcome'))).toEqual([])
})

test('a run with no one at the prompt shows no card and leaves the store and the session state alone', { options: OPTIONS }, async ($, on) => {
  const written: string[] = []
  on('state.set', ($, e, next) => {
    written.push(e.key)
    return next(e)
  })
  const world = setUp(on, {}, {})
  await $.classic.SessionStart({ source: 'startup', session_id: SESSION_ID, model: 'example-model', cwd: '/work/my-repo' })
  await $.session.start({ surface: null, isInteractive: false, cwd: '/work/my-repo' })
  await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(1)
  expect(welcomeLines(world.transcript)).toEqual([])
  expect(world.toasts).toEqual([])
  expect(stored(world)).toBeUndefined()
  expect(written).toEqual([])
  // The first interactive session after it still has the card.
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await world.clock.settle()
  expect((await band($)).texts.slice(0, 3)).toEqual(INSTALLED)
})

for (const operation of ['get', 'set'] as const) {
  test(`a welcome store ${operation} that never answers holds up nothing: session.start returns, rows land, the commands are there`, { options: OPTIONS }, async ($, on) => {
    // The reads start with `welcome`; the greeting's write goes to its version's key.
    let held = 0
    const hang = (): Promise<never> => {
      held += 1
      return new Promise<never>(() => undefined)
    }
    if (operation === 'get') on('store.get', { key: 'welcome' }, hang)
    else on('store.set', { key: GREETED_KEY }, hang)
    const world = setUp(on, {}, {})
    let started = false
    void startSession($).then(() => {
      started = true
    })
    await world.clock.advance(5000)
    expect(started).toBe(true)
    expect(world.acceptedRows().length).toBe(1)
    expect(world.registered).toEqual(['bqaa-status', 'bqaa-flush', 'bqaa-backfill'])
    // Never read: no card. Read, but never recorded: the card, with its rows.
    expect(held).toBe(1)
    const texts = (await band($)).texts
    if (operation === 'get') expect(texts).toEqual(['drawn beneath'])
    else expect(texts).toEqual([...INSTALLED, RECORDS, landed(1), 'drawn beneath'])
  })
}

test('a store that cannot list the versions greeted before shows no card and greets no one; once it can, the card names the one it replaced', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, { 'welcome:0.0.9': { sessionId: 'an-earlier-session', previous: null } })
  world.refuseKeys = true
  await startSession($)
  await world.clock.settle()
  expect((await band($)).texts).toEqual(['drawn beneath'])
  expect(welcomeLines(world.transcript)).toEqual([])
  expect(greeted(world)).toBeUndefined()
  world.refuseKeys = false
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts.slice(1, 3)).toEqual([`BigQuery Agent Analytics mod updated to ${PLUGIN_VERSION}`, ' was 0.0.9'])
})

test('a landing whose store write is refused is written once more rows land', { options: OPTIONS }, async ($, on) => {
  let refusing = true
  on('store.set', { key: OWN }, ($, e, next) => (refusing ? { deny: 'the store could not be written' } : next(e)))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).texts[4]).toBe(landed(1))
  expect(own(world)).toBeUndefined()
  refusing = false
  await $.tool.call({ tool: 'Read', file_path: 'a.ts' })
  await world.clock.advance(5000)
  expect(own(world)).toMatchObject({ landedTo: HERE })
})

test('a flush interval of no whole number of seconds is said as it is', { options: { ...OPTIONS, flushIntervalMs: 1500 } }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  expect((await band($)).texts[4]).toBe('◌ Waiting for the first rows, sent every 1500 ms while you work · /bqaa-status shows the counts')
})

test('a close read that never answers, the greeting never written, shows no card and greets no one', { options: OPTIONS }, async ($, on) => {
  let reads = 0
  on('store.get', { key: CLOSED }, () => {
    reads += 1
    return new Promise<never>(() => undefined)
  })
  const world = setUp(on, {}, { [CLOSED]: true })
  await startSession($)
  await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(1)
  expect(reads).toBe(1)
  expect((await band($)).texts).toEqual(['drawn beneath'])
  expect(welcomeLines(world.transcript)).toEqual([])
  expect(greeted(world)).toBeUndefined()
})

test('a session left open never writes over a newer version another session greeted meanwhile', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  const newer = { version: '0.2.0', previous: PLUGIN_VERSION, sessionId: 'a-newer-session' }
  world.store.set('welcome', newer)
  world.store.set('welcome:0.2.0', { sessionId: 'a-newer-session', previous: PLUGIN_VERSION })
  // This session's rows land, then its card is closed: both go to its own
  // record, never to another version's.
  await world.clock.advance(5000)
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, landed(1), 'drawn beneath'])
  await $.prompt.submit(PROMPT)
  await world.clock.settle()
  expect(stored(world)).toEqual(newer)
  expect(world.store.get('welcome:0.2.0')).toEqual({ sessionId: 'a-newer-session', previous: PLUGIN_VERSION })
  expect(own(world)).toMatchObject({ landedTo: HERE })
  expect(closedMark(world)).toBe(true)
})

test('rows that land while the store is still being read: the card says so as soon as it shows', { options: OPTIONS }, async ($, on) => {
  // The first read of the record waits until the test answers it; later
  // ones read the store.
  let answer: ((stored: { value: undefined }) => void) | undefined
  on('store.get', { key: 'welcome' }, () => (answer === undefined ? new Promise<{ value: unknown }>(resolve => (answer = resolve)) : { value: world.store.get('welcome') }))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(1)
  answer?.({ value: undefined })
  await world.clock.settle()
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, landed(1), 'drawn beneath'])
  expect(world.toasts).toEqual([`BQAA: first rows landed in ${TABLE}`])
  expect(own(world)).toMatchObject({ landedTo: HERE })
  expect(greeted(world)).toEqual(GREETED_HERE)
})

test('a store read that answers after session.start ran again is not used: one card, one line', { options: OPTIONS }, async ($, on) => {
  const answers: ((stored: { value: undefined }) => void)[] = []
  on('store.get', { key: 'welcome' }, () => new Promise<{ value: undefined }>(resolve => answers.push(resolve)))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await world.clock.settle()
  expect(answers.length).toBe(2)
  // The later start's read answers first; the earlier one's after it.
  answers[1]?.({ value: undefined })
  await world.clock.settle()
  answers[0]?.({ value: undefined })
  await world.clock.settle()
  expect(welcomeLines(world.transcript).length).toBe(1)
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, WAITING, 'drawn beneath'])
})

test('a reload from a dry run to BigQuery waits for rows to land in BigQuery: files written in the dry run are not that', { options: OPTIONS }, async ($, on) => {
  let dryRun = true
  on('env.get', { name: 'BQAA_DRY_RUN' }, () => ({ value: dryRun ? 'true' : undefined }))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.advance(5000)
  expect(world.fetches.length).toBe(0)
  expect(own(world)).toMatchObject({ landedTo: `dry run: ${DRY_RUN_DIR}` })
  // The dry run is turned off, and the mod loads again in the same session.
  dryRun = false
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await world.clock.settle()
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, WAITING, 'drawn beneath'])
  // A prompt leaves it up: nothing has landed in BigQuery yet.
  await $.prompt.submit(PROMPT)
  expect((await band($)).texts.slice(0, 5)).toEqual([...INSTALLED, RECORDS, WAITING])
  // The prompt's rows land, and the card says so, counting only them.
  await world.clock.advance(5000)
  const texts = (await band($)).texts
  expect(world.acceptedRows().length > 0).toBe(true)
  expect(texts[4]).toBe(landed(world.acceptedRows().length))
  expect(world.toasts).toEqual([`BQAA: first rows written to ${DRY_RUN_DIR}`, `BQAA: first rows landed in ${TABLE}`])
  expect(welcomeLines(world.transcript).length).toBe(1)
})

test('a reload to another table waits for rows to land in that table', { options: {} }, async ($, on) => {
  let dataset = 'events_a'
  on('env.get', { name: 'BQAA_DATASET' }, () => ({ value: dataset }))
  const world = setUp(on, { BQAA_PROJECT_ID: 'my-gcp-project' }, {})
  await startSession($)
  await world.clock.advance(5000)
  expect(world.acceptedRows().length).toBe(1)
  dataset = 'events_b'
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/my-repo' })
  await world.clock.settle()
  expect(world.insertCalls().length).toBe(1)
  expect((await band($)).texts).toEqual([...INSTALLED, `Records ${WHAT} to my-gcp-project.events_b.agent_events (US).`, WAITING, 'drawn beneath'])
  await $.prompt.submit(PROMPT)
  expect((await band($)).texts[4]).toBe(WAITING)
})

test('dry run: the card says where rows are written, then that the first were', { options: { dryRun: true } }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  const head = ['✓ ', 'BigQuery Agent Analytics mod installed, dry run', ` ${PLUGIN_VERSION}`, `Writes ${WHAT} as JSONL files to ${DRY_RUN_DIR}, not to BigQuery.`]
  expect((await band($)).texts).toEqual([...head, '◌ Waiting for the first rows, written every 5 s while you work · /bqaa-status shows the counts', 'drawn beneath'])
  expect(welcomeLines(world.transcript)).toEqual([`bqaa: BigQuery Agent Analytics mod installed (${PLUGIN_VERSION}), dry run: writes ${WHAT} as JSONL files to ${DRY_RUN_DIR}, not to BigQuery.`])
  await world.clock.advance(5000)
  expect((await band($)).texts).toEqual([...head, '✓ First rows written · 1 so far · /bqaa-status shows the counts', 'drawn beneath'])
  expect(world.toasts).toEqual([`BQAA: first rows written to ${DRY_RUN_DIR}`])
})

test('capture off: the card says nothing is recorded, and how to turn it on', { options: { ...OPTIONS, enabled: false } }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  expect((await band($)).texts).toEqual([
    '○ ',
    'BigQuery Agent Analytics mod installed, capture off',
    ` ${PLUGIN_VERSION}`,
    'Nothing is recorded: the enabled option is off, or BQAA_TRACE_ENABLED=false. Turn it on in /config.',
    'drawn beneath',
  ])
  expect(welcomeLines(world.transcript)).toEqual([`bqaa: BigQuery Agent Analytics mod installed (${PLUGIN_VERSION}); capture is off (the enabled option, or BQAA_TRACE_ENABLED=false).`])
})

test('the card is boxed where the band has the rows for a border, lines alone where not, and yields to a survey', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  expect(await band($, { maxRows: 5 })).toEqual({ texts: [...INSTALLED, RECORDS, WAITING, 'drawn beneath'], boxed: true, button: 'Got it' })
  expect(await band($, { maxRows: 4 })).toEqual({ texts: [...INSTALLED, RECORDS, WAITING, 'drawn beneath'], boxed: false, button: 'Got it' })
  expect(await band($, { hasSurvey: true })).toEqual({ texts: ['drawn beneath'], boxed: false, button: undefined })
})

// A store write the test lets land when it calls the returned function.
function holdWrites(): { hold: (value: { key: string; value: unknown }, world: World) => Promise<{ value: undefined }>; release: () => void } {
  const held: (() => void)[] = []
  return {
    hold: (e, world) =>
      new Promise(resolve =>
        held.push(() => {
          world.store.set(e.key, JSON.parse(JSON.stringify(e.value)) as unknown)
          resolve({ value: undefined })
        }),
      ),
    release: () => {
      for (const land of held.splice(0)) land()
    },
  }
}

async function pressGotIt($: Engine): Promise<void> {
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  await ui.press({ key: CLOSE })
  await ui.unmount()
}

test('a close made while the greeting is still being written is written at once, behind nothing: the greeting lands after it, and a reload shows no card', { options: OPTIONS }, async ($, on) => {
  const writes = holdWrites()
  on('store.set', { key: GREETED_KEY }, ($, e) => writes.hold(e, world))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  await pressGotIt($)
  await world.clock.settle()
  // The close is in the store while the greeting is still being written.
  expect(closedMark(world)).toBe(true)
  expect(greeted(world)).toBeUndefined()
  writes.release()
  await world.clock.settle()
  expect(greeted(world)).toEqual(GREETED_HERE)
  expect(closedMark(world)).toBe(true)
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts).toEqual(['drawn beneath'])
})

test('a landing written late never reopens the card a prompt closed after it', { options: OPTIONS }, async ($, on) => {
  const writes = holdWrites()
  let first = true
  on('store.set', { key: OWN }, ($, e) => {
    if (!first) {
      world.store.set(e.key, e.value)
      return { value: undefined }
    }
    first = false
    return writes.hold(e, world)
  })
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).texts[4]).toBe(landed(1))
  // The landing's write has not answered when the prompt closes the card.
  await $.prompt.submit(PROMPT)
  writes.release()
  await world.clock.settle()
  expect(own(world)).toMatchObject({ landedTo: HERE })
  expect(closedMark(world)).toBe(true)
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts).toEqual(['drawn beneath'])
})

for (const order of ['before', 'after'] as const) {
  test(`a landing an earlier load is still writing lands ${order} the close a reload's Got it writes: the card stays closed`, { options: OPTIONS }, async ($, on) => {
    const writes = holdWrites()
    let first = true
    on('store.set', { key: OWN }, ($, e) => {
      if (!first) {
        world.store.set(e.key, e.value)
        return { value: undefined }
      }
      first = false
      return writes.hold(e, world)
    })
    const world = setUp(on, {}, {})
    await startSession($)
    await world.clock.advance(5000)
    // A reload: the card comes back as it was, from `$.state`, its count too.
    await $.session.start(START)
    await world.clock.settle()
    expect((await band($)).texts[4]).toBe(landed(1))
    if (order === 'before') writes.release()
    await pressGotIt($)
    if (order === 'after') writes.release()
    await world.clock.settle()
    expect(own(world)).toMatchObject({ landedTo: HERE })
    expect(closedMark(world)).toBe(true)
    await $.session.start(START)
    await world.clock.settle()
    expect((await band($)).texts).toEqual(['drawn beneath'])
  })
}

test('a newer version another session greets while this card writes its landing keeps its greeting', { options: OPTIONS }, async ($, on) => {
  const writes = holdWrites()
  on('store.set', { key: OWN }, ($, e) => writes.hold(e, world))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.advance(5000)
  const newer = { version: '0.2.0', previous: PLUGIN_VERSION, sessionId: 'a-newer-session' }
  world.store.set('welcome', newer)
  writes.release()
  await world.clock.settle()
  expect(stored(world)).toEqual(newer)
  expect(own(world)).toMatchObject({ landedTo: HERE })
  expect(closedMark(world)).toBeUndefined()
})

test('a reload while the greeting is still being written shows the same card again, with no second line', { options: OPTIONS }, async ($, on) => {
  on('store.set', { key: GREETED_KEY }, () => new Promise<never>(() => undefined))
  const world = setUp(on, {}, {})
  await startSession($)
  // The card is up, and its greeting's write waits.
  await world.clock.settle()
  expect(welcomeLines(world.transcript).length).toBe(1)
  expect(greeted(world)).toBeUndefined()
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts).toEqual([...INSTALLED, RECORDS, WAITING, 'drawn beneath'])
  expect(welcomeLines(world.transcript).length).toBe(1)
})

test('a reload after Got it shows no card while the close is still being written', { options: OPTIONS }, async ($, on) => {
  // The greeting lands; the close's write never answers.
  on('store.set', { key: CLOSED }, () => new Promise<never>(() => undefined))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  expect(greeted(world)).toEqual(GREETED_HERE)
  await pressGotIt($)
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts).toEqual(['drawn beneath'])
})

test('a store read from an earlier session.start that answers while a later one is still setting up shows nothing', { options: OPTIONS }, async ($, on) => {
  let release: (() => void) | undefined
  let answer: ((stored: { value: unknown }) => void) | undefined
  let slow = false
  on('env.get', { name: 'BQAA_PROJECT_ID' }, () => (slow ? new Promise(resolve => (release = () => resolve({ value: undefined }))) : { value: undefined }))
  on('store.get', { key: 'welcome' }, () => (answer === undefined ? new Promise<{ value: unknown }>(resolve => (answer = resolve)) : { value: world.store.get('welcome') }))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  slow = true
  let started = false
  void $.session.start(START).then(() => {
    started = true
  })
  await world.clock.settle()
  expect(started).toBe(false)
  answer?.({ value: undefined })
  await world.clock.settle()
  expect(welcomeLines(world.transcript)).toEqual([])
  expect((await band($)).texts).toEqual(['drawn beneath'])
  release?.()
  await world.clock.settle()
  expect(started).toBe(true)
  expect(welcomeLines(world.transcript).length).toBe(1)
})

test('session.start run again while the new card is being written: the run that showed it still writes the greeting line, once', { options: OPTIONS }, async ($, on) => {
  // The first write of the card waits until let go.
  let first = true
  const waiting: (() => void)[] = []
  on('state.set', { plugin: PLUGIN, key: 'welcome' }, ($, e, next) => {
    if (!first) return next(e)
    first = false
    return new Promise(resolve => waiting.push(() => resolve(next(e))))
  })
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  expect(waiting.length).toBe(1)
  // session.start runs again before the write answers; the write lands, and
  // the later run takes the card over from the state.
  const again = $.session.start(START)
  for (const go of waiting.splice(0)) go()
  await again
  await world.clock.settle()
  expect(welcomeLines(world.transcript).length).toBe(1)
  expect((await band($)).button).toBe('Got it')
})

test('a reload to another table, its takeover read still waiting: no landing is claimed there, a prompt meanwhile leaves the card open, and then it waits for rows there', { options: {} }, async ($, on) => {
  let dataset = 'events_a'
  on('env.get', { name: 'BQAA_DATASET' }, () => ({ value: dataset }))
  let holding = false
  const waiting: (() => void)[] = []
  on('store.get', { key: CLOSED }, ($, e, next) => (holding ? new Promise(resolve => waiting.push(() => resolve(next(e)))) : next(e)))
  const world = setUp(on, { BQAA_PROJECT_ID: 'my-gcp-project' }, {})
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).texts[4]).toBe(landed(1))
  dataset = 'events_b'
  holding = true
  await $.session.start(START)
  await world.clock.settle()
  expect(waiting.length).toBe(1)
  expect((await band($)).texts).toEqual(['drawn beneath'])
  await $.prompt.submit(PROMPT)
  await world.clock.settle()
  holding = false
  for (const go of waiting.splice(0)) go()
  await world.clock.settle()
  expect((await band($)).texts).toEqual([...INSTALLED, `Records ${WHAT} to my-gcp-project.events_b.agent_events (US).`, WAITING, 'drawn beneath'])
})

test('rows of a send still in flight across a reload to another table do not land the card there', { options: {} }, async ($, on) => {
  let dataset = 'events_a'
  on('env.get', { name: 'BQAA_DATASET' }, () => ({ value: dataset }))
  const world = setUp(on, { BQAA_PROJECT_ID: 'my-gcp-project' }, {})
  world.replies = [{ hang: true }]
  await startSession($)
  await world.clock.advance(5000)
  expect(world.insertCalls().map(f => f.url.includes('/datasets/events_a/'))).toEqual([true])
  dataset = 'events_b'
  await $.session.start(START)
  await world.clock.settle()
  // The send to events_a lands now.
  world.releaseHung(200, OK_INSERT.text)
  await world.clock.settle()
  expect(world.insertCalls().some(f => f.url.includes('/datasets/events_b/'))).toBe(false)
  expect((await band($)).texts[4]).toBe(WAITING)
  expect(world.toasts).toEqual([])
  await $.prompt.submit(PROMPT)
  expect((await band($)).texts[4]).toBe(WAITING)
  await world.clock.settle()
  expect(own(world)).toBeUndefined()
})

test('rows of a dry-run write still in flight across a reload to BigQuery do not land the card there', { options: OPTIONS }, async ($, on) => {
  let dryRun = true
  on('env.get', { name: 'BQAA_DRY_RUN' }, () => ({ value: dryRun ? 'true' : undefined }))
  const world = setUp(on, {}, {})
  world.hangWrites = true
  await startSession($)
  await world.clock.advance(5000)
  dryRun = false
  await $.session.start(START)
  await world.clock.settle()
  // The dry-run file is written now.
  world.hangWrites = false
  world.releaseWrites()
  await world.clock.settle()
  expect([...world.files.keys()].some(path => path.startsWith(DRY_RUN_DIR))).toBe(true)
  expect(world.insertCalls().length).toBe(0)
  expect((await band($)).texts[4]).toBe(WAITING)
  expect(world.toasts).toEqual([])
})

test('a reload to capture off, after rows landed, keeps the card until Got it', { options: OPTIONS }, async ($, on) => {
  let enabled = true
  on('env.get', { name: 'BQAA_TRACE_ENABLED' }, () => ({ value: enabled ? undefined : 'false' }))
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.advance(5000)
  expect((await band($)).texts[4]).toBe(landed(1))
  enabled = false
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts[1]).toBe('BigQuery Agent Analytics mod installed, capture off')
  await $.prompt.submit(PROMPT)
  expect((await band($)).texts[1]).toBe('BigQuery Agent Analytics mod installed, capture off')
  await pressGotIt($)
  expect((await band($)).texts).toEqual(['drawn beneath'])
})

test('a prompt that does not come from the terminal leaves a landed card up; the next one typed there closes it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.advance(5000)
  await $.prompt.submit({ ...PROMPT, origin: { kind: 'task-notification' } })
  expect((await band($)).texts[4]).toBe(landed(1))
  await $.prompt.submit(PROMPT)
  expect((await band($)).texts).toEqual(['drawn beneath'])
})

// A dead-letter line for this table.
const parked = (n: number): string =>
  JSON.stringify({
    v: 1,
    kind: 'spill',
    reason: 'session ended before these rows were confirmed sent',
    attempts: 0,
    failed_at: '2026-10-13T20:01:02.003Z',
    table: TABLE_REF,
    row: buildRow({ eventType: 'STATE_DELTA', timestampMs: 0, eventId: String(n).repeat(32), agent: 'coding-agent', sessionId: 'earlier', invocationId: null, userId: 'dev', traceId: null, spanId: null, parentSpanId: null, content: {}, attributes: {}, maxPayloadChars: 16384 }).row,
  })

test('the writer says rows landed, with the configuration they went under: per replay request, before its file is emptied, and for a dry run at session end', async () => {
  const told: [number, string][] = []
  const files = new Map<string, string>()
  let failWrites = false
  const io = (now: () => number): Io => ({
    now,
    after: () => ({ cancel: () => undefined }),
    fetch: async () => OK_INSERT,
    runCommand: async () => ({ exitCode: 0, stdout: `${'a'.repeat(24)}\n`, stderr: '' }),
    envToken: async () => undefined,
    writeFile: async (path, text) => {
      if (failWrites && !path.includes('/outcome-')) throw new Error(`EACCES: ${path}`)
      files.set(path, text)
    },
    readFile: async path => files.get(path) ?? '',
    listDir: async dir => [...files].filter(([path]) => path.startsWith(`${dir}/`)).map(([path, text]) => ({ name: path.slice(dir.length + 1), kind: 'file', size: text.length })),
    storeGet: async () => undefined,
    storeSet: async () => undefined,
    storeDelete: async () => undefined,
    status: () => undefined,
    debug: () => undefined,
    sent: (rows, to) => told.push([rows, to.dryRun ? `dry run: ${to.dryRunDir}` : to.dataset]),
  })
  const config = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl' }, {})
  files.set(`/dl/${TABLE_REF}/spill-20261013T200102003Z-earlier-1.jsonl`, packFiles([parked(1), parked(2), parked(3)])[0] as string)
  // BigQuery takes the three rows; the file then cannot be emptied (the
  // replay's mark and record beside it can be written).
  failWrites = true
  await new Writer(io(() => 0), () => config, () => 'hand-built-session').backfill(['spill'], 'command')
  expect(told).toEqual([[3, 'agent_analytics']])
  failWrites = false
  const dryRun = resolveConfig({ project: 'my-gcp-project', dataset: 'agent_analytics', deadLetterDir: '/dl', dryRun: true }, {})
  const writer = new Writer(io(() => 0), () => dryRun, () => 'hand-built-session')
  writer.enqueue(buildRow({ eventType: 'STATE_DELTA', timestampMs: 0, eventId: '4'.repeat(32), agent: 'coding-agent', sessionId: 'hand-built-session', invocationId: null, userId: 'dev', traceId: null, spanId: null, parentSpanId: null, content: {}, attributes: {}, maxPayloadChars: 16384 }).row, 0)
  await writer.spillForExit(1000)
  expect(told).toEqual([[3, 'agent_analytics'], [1, `dry run: ${dryRun.dryRunDir}`]])
})

test('rows the writer before a reload lands where the card counts them count on the card after it', { options: OPTIONS }, async ($, on) => {
  const world = setUp(on, {}, {})
  world.replies = [{ hang: true }]
  await startSession($)
  await world.clock.advance(5000)
  expect(world.insertCalls().length).toBe(1)
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts[4]).toBe(WAITING)
  world.releaseHung(200, OK_INSERT.text)
  await world.clock.settle()
  expect((await band($)).texts[4]).toBe(landed(1))
})

test('a close whose session-state write is refused hides the card at once, and the store\'s close keeps it hidden after a reload', { options: OPTIONS }, async ($, on) => {
  let refused = 0
  on('state.set', { plugin: PLUGIN, key: 'welcome' }, ($, e, next) => {
    if ((e.value as { closed?: boolean } | null)?.closed !== true) return next(e)
    refused += 1
    return { deny: 'refused by policy' }
  })
  const world = setUp(on, {}, {})
  await startSession($)
  await world.clock.settle()
  await pressGotIt($)
  await world.clock.settle()
  expect(refused > 0).toBe(true)
  expect((await band($)).texts).toEqual(['drawn beneath'])
  expect(closedMark(world)).toBe(true)
  await $.session.start(START)
  await world.clock.settle()
  expect((await band($)).texts).toEqual(['drawn beneath'])
})
