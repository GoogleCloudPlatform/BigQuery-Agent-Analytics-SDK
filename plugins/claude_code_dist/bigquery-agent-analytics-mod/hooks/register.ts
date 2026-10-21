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

// BigQuery Agent Analytics mod: captures the host coding-agent CLI's session,
// turn, model request and tool events as BQAA `agent_events` rows and streams
// them to BigQuery from the background.
//
// The rule the capture hooks here follow (the lesson of the settings-hook
// plugin's PR #6, whose foreground hooks could hang the host CLI): a hook
// calls `next` right away, records what it saw into an in-memory queue, and
// returns. No hook waits on the network or a process; the writer does that
// from timers. Two lifecycle hooks do bounded work before passing their event
// on: session.start sets the writer up and registers the commands, and
// session.end writes what is still queued to a spill file, waiting at most
// the exit budget. If the mod's own code throws, the event goes on as if the
// mod were not there (fail open): the observers through their `.catch`, the
// other hooks because their bookkeeping runs inside `safely` or a `try`.
// While rows cannot be written for lack of credentials or permission, the
// band above the prompt says what is wrong; no prompt ever waits on that.
// With nothing wrong, in the first session after an install or an update,
// the band shows a welcome card: that the mod is on, where rows go, and when
// the first ones land.

import type { EngineInterface, Register, SessionStartInput } from 'claude-code'

import type { DeliveredRows, DeliveredState, Run } from '../types'

import { type Config, type EnvSnapshot, type Options, PLUGIN_VERSION, canDeliver, deadLetterPartition, destinationDir, joinPath, resolveConfig, tableRef } from './lib/config.ts'
import { HOST_FILE_BYTES, kindOf } from './lib/deadletter.ts'
import { randomIds } from './lib/ids.ts'
import type { BqaaRow } from './lib/rows.ts'
import { redactDiagnostic } from './lib/sanitize.ts'
import { type DeadLetterSummary, formatStatus } from './lib/status.ts'
import { type StepCtx, type StepResult, type ToolCtx, type ToolOutcome, Tracer, errorText } from './lib/tracer.ts'
import { CardRecords, WELCOME_KEY, type Welcome, cardKey, closedKey, destinationKey, greetedBy, greetedKey, greetedVersions, landedToast, openWelcome, reopenWelcome, welcomeCard, welcomeNotice } from './lib/welcome.ts'
import { COMMAND_LINE_CHARS, type Io, Writer } from './lib/writer.ts'

let options: Options = {}
let config: Config = resolveConfig({}, {})
let writer: Writer | null = null
let flushTimer: { cancel: () => void } | null = null
let internalErrors = 0
// The host's timer, from session.start's `$`, for work that waits briefly
// outside any hook (a finished tool call waiting for its decision).
let clockAfter: ((ms: number, fn: () => void) => { cancel: () => void }) | null = null

// The welcome card (lib/welcome.ts), in the first session after the mod is
// installed or updated, lives in the session's `$.state` alone (`changeCard`):
// every load of this code reads that one copy, and the band draws from it.
// This load of the code, its session.start runs, and the writes of the card
// it showed (`CardRecords`).
const LOAD = Math.random().toString(36).slice(2)
let welcomeStarts = 0
// How many resets of the session's state this load has seen (/clear,
// /resume and /branch, which `classic.SessionStart` reports): a run's
// welcome card begun before one is neither shown nor taken over after it.
let resets = 0
const RESET_SOURCES: ReadonlySet<string> = new Set(['clear', 'resume', 'fork'])
let records: CardRecords | null = null
// Who speaks for the card a session.start run of this load showed (its
// writer counts under the same name).
const ownerOf = (start: number): string => `${LOAD}:${start}`
// The card (`version:session`) this load knows to be closed, whatever the
// session's state says: closed here, or closed in the store, a close whose
// state write was refused included. The band never draws it.
let closedCard: string | null = null
// The card this load has read the store's close for and shown, or taken over
// from the session's state: the band draws no other, so a load whose reads
// have not answered (or never do) never draws a card the store has closed.
let cardReady: string | null = null
const cardId = (version: string, sessionId: string | null): string => `${version}:${sessionId}`
const WELCOME_STATE = { plugin: 'bigquery-agent-analytics-mod', key: 'welcome' } as const
// How many session.start runs, of every load of this code, the session has
// seen: each takes the next number, its rank, so a later load or run ranks
// above every earlier one that shows the card (`startWelcome` stops a load
// replaced before its number came back), whatever the clock says. With the
// lifetime of that count, which starts over with the session's state.
const LOADS_STATE = { plugin: 'bigquery-agent-analytics-mod', key: 'loads' } as const
// Each session.start run's rows landed so far in the session, by run and
// destination (`countKey`), with the run's rank and lifetime, whatever the
// card shows. A card counts the rows of the run it counts from (`since`: the
// one that opened it, or last moved it where rows go now) and of every run
// of its lifetime ranked above it, where it counts rows, and never a run's
// ranked below it, nor a run's of the lifetime before the state was reset
// (`counts`). Ranks go by when each run's count write landed: within one
// load, a run's whose write landed after the next run's ranks above it. A run knows
// its own rank and lifetime with nothing to read, so no read or write that
// answers late, before or after an update, moves a delivery from one card to
// another.
const DELIVERED_STATE = { plugin: 'bigquery-agent-analytics-mod', key: 'delivered' } as const
// A run's rows landed in one destination: the key of `delivered`.
const countKey = (by: string, to: string): string => `${by}@${to}`
// What this load's runs have landed, by `countKey`.
const landedByWriter = new Map<string, number>()
// Whether this session shows a card (a person at the prompt): only then is
// `delivered` kept.
let cardSession = false

// Rows captured before session.start built the writer (an event the engine
// raised first); handed over once it exists.
const early: { row: BqaaRow; redactions: number }[] = []

// Whether session.start has loaded the configuration: the options, the
// environment and the host version. Until then `config` holds the defaults
// register() starts with, so the session-start row, whose classic event the
// host raises while session.start is still reading the environment, waits.
let configured = false
const awaitingConfig: (() => void)[] = []

// `work` now if the configuration is loaded, else once session.start has
// loaded it. Never waits: the hook calling this has called `next` already.
function whenConfigured(work: () => void): void {
  if (configured) safely(work)
  else if (awaitingConfig.length < 10) awaitingConfig.push(work)
}

function configurationLoaded(): void {
  configured = true
  for (const work of awaitingConfig.splice(0)) safely(work)
}

const tracer = new Tracer({
  now: () => Date.now(),
  ids: randomIds,
  config: () => config,
  emit: (row, redactions) => {
    if (writer) writer.enqueue(row, redactions)
    else if (early.length < 1000) early.push({ row, redactions })
  },
  after: (ms, fn) => (clockAfter ? clockAfter(ms, fn) : null),
})

// The mod's bookkeeping never breaks the event it watches.
function safely(work: () => void): void {
  try {
    work()
  } catch {
    internalErrors += 1
  }
}

// `next(e)` for `turn.step` streams the response; this passes every chunk
// (and `return`/`throw`) straight through and notes when the first chunk of
// content (text, thinking or a tool call) arrived.
function timedStream<C extends { kind: string }, R>(inner: AsyncGenerator<C, R, unknown>, onFirstContent: () => void): AsyncGenerator<C, R, unknown> {
  let seen = false
  const stream: AsyncGenerator<C, R, unknown> = {
    async next(...args: [] | [unknown]) {
      const step = await inner.next(...args)
      if (!seen && !step.done && step.value.kind !== 'engine') {
        seen = true
        onFirstContent()
      }
      return step
    },
    return: (value: R | PromiseLike<R>) => inner.return(value),
    throw: (error: unknown) => inner.throw(error),
    [Symbol.asyncIterator]: () => stream,
  }
  return stream
}

// A variable a policy refuses to hand over reads as unset, not as a reason
// to stop capturing.
const none = (): undefined => undefined

// Every line the mod logs or a command prints is redacted on its way out.
const clean = (text: string, maxChars = 2000): string => redactDiagnostic(text, maxChars)
const say = (text: string): { text: string } => ({ text: clean(text, 8000) })

async function readEnv($: EngineInterface): Promise<EnvSnapshot> {
  const [
    BQAA_PROJECT_ID,
    GCP_PROJECT_ID,
    GOOGLE_CLOUD_PROJECT,
    BQAA_DATASET,
    BQ_DATASET,
    BQAA_TABLE,
    BQ_TABLE,
    BQAA_LOCATION,
    BQAA_TRACE_ENABLED,
    BQAA_DRY_RUN,
    BQAA_AGENT_NAME,
    BQAA_USER_ID,
    USER,
    USERNAME,
    HOME,
    USERPROFILE,
    BQAA_DEAD_LETTER_DIR,
    BQAA_ACCESS_TOKEN_COMMAND,
    BQAA_REQUEST_TIMEOUT_MS,
    BQAA_MAX_QUEUE_ROWS,
    BQAA_EXIT_FLUSH_MS,
    BQAA_AUTO_CREATE_TABLE,
    BQAA_AUTO_BACKFILL,
  ] = await Promise.all([
    $.env.get('BQAA_PROJECT_ID').catch(none),
    $.env.get('GCP_PROJECT_ID').catch(none),
    $.env.get('GOOGLE_CLOUD_PROJECT').catch(none),
    $.env.get('BQAA_DATASET').catch(none),
    $.env.get('BQ_DATASET').catch(none),
    $.env.get('BQAA_TABLE').catch(none),
    $.env.get('BQ_TABLE').catch(none),
    $.env.get('BQAA_LOCATION').catch(none),
    $.env.get('BQAA_TRACE_ENABLED').catch(none),
    $.env.get('BQAA_DRY_RUN').catch(none),
    $.env.get('BQAA_AGENT_NAME').catch(none),
    $.env.get('BQAA_USER_ID').catch(none),
    $.env.get('USER').catch(none),
    $.env.get('USERNAME').catch(none),
    $.env.get('HOME').catch(none),
    $.env.get('USERPROFILE').catch(none),
    $.env.get('BQAA_DEAD_LETTER_DIR').catch(none),
    $.env.get('BQAA_ACCESS_TOKEN_COMMAND').catch(none),
    $.env.get('BQAA_REQUEST_TIMEOUT_MS').catch(none),
    $.env.get('BQAA_MAX_QUEUE_ROWS').catch(none),
    $.env.get('BQAA_EXIT_FLUSH_MS').catch(none),
    $.env.get('BQAA_AUTO_CREATE_TABLE').catch(none),
    $.env.get('BQAA_AUTO_BACKFILL').catch(none),
  ])
  return {
    BQAA_PROJECT_ID,
    GCP_PROJECT_ID,
    GOOGLE_CLOUD_PROJECT,
    BQAA_DATASET,
    BQ_DATASET,
    BQAA_TABLE,
    BQ_TABLE,
    BQAA_LOCATION,
    BQAA_TRACE_ENABLED,
    BQAA_DRY_RUN,
    BQAA_AGENT_NAME,
    BQAA_USER_ID,
    USER,
    USERNAME,
    HOME,
    USERPROFILE,
    BQAA_DEAD_LETTER_DIR,
    BQAA_ACCESS_TOKEN_COMMAND,
    BQAA_REQUEST_TIMEOUT_MS,
    BQAA_MAX_QUEUE_ROWS,
    BQAA_EXIT_FLUSH_MS,
    BQAA_AUTO_CREATE_TABLE,
    BQAA_AUTO_BACKFILL,
  }
}

// The session id, read again for each event: after /clear, /resume or
// /branch the process goes on under a new id, and neither session.start nor,
// where a managed policy keeps classic hooks from a user plugin,
// classic.SessionStart says so. A read the host refuses leaves the id the
// tracer has: the last one, or after a session change none. It cannot fail,
// so hooks read it before passing the event on: a tool's own events (its
// permission prompt, a subagent) then find the tool already started.
async function readSessionId($: EngineInterface): Promise<void> {
  try {
    tracer.setSession({ sessionId: await $.session.id() })
  } catch {
    // The tracer keeps what it has.
  }
}

// With recordHostname: the machine's name, from the `hostname` command, run
// from a timer (never in a hook). Rows built after it answers carry it.
async function lookUpHostname($: EngineInterface): Promise<void> {
  try {
    const answer = await $.process.run(['hostname'], { timeoutMs: 5000 })
    const printed = answer.stdout.trim()
    if (answer.exitCode === 0 && /^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$/.test(printed)) tracer.setSession({ hostName: printed })
    else $.ui.log(clean(`bqaa: recordHostname: the hostname command gave no usable name (exit ${answer.exitCode})`), { to: 'debug' })
  } catch (err) {
    $.ui.log(clean(`bqaa: recordHostname: ${errorText(err)}`), { to: 'debug' })
  }
}

// This session.start run's rank: the next number of the session's count of
// runs (`LOADS_STATE`), taken first thing, and the count's lifetime. /clear,
// /resume and /branch reset every value of the session's state, the count
// included, and fire no session.start, while a run from before goes on
// sending: the first run to count afresh names a new lifetime, so ranks that
// start over are told from the ones before. Null where the session keeps no
// state.
async function nextLoad($: EngineInterface): Promise<Run | null> {
  for (let i = 0; i < 8; i += 1) {
    const held = await $.state.get(LOADS_STATE)
    const now = held.value ?? { count: 0, lifetime: Math.random().toString(36).slice(2) }
    const next = { count: now.count + 1, lifetime: now.lifetime }
    if ((await $.state.set(LOADS_STATE, next, { ifVersion: held.version })).isSet) return { rank: next.count, lifetime: next.lifetime }
  }
  return null
}

// This run's rank (`nextLoad`), once one tick of the host's clock answers
// after it: a hot reload cancels the waits of the load it replaces, so a load
// replaced before its rank came back (its read or write answered late) never
// gets past it, and a run that does took its rank while still the current
// load, before any later load took one: every later run ranks above it. Null
// where the session keeps no state, or the wait is refused. The card waits
// on it, and so does each delivery's count.
async function admit($: EngineInterface, ranked: Promise<Run | null>): Promise<Run | null> {
  const run = await ranked
  if (run === null) return null
  try {
    await $.clock.sleep(0)
  } catch {
    return null
  }
  return run
}

// In a session a person sees (a `-p` run draws no band): the welcome card
// for a version not greeted yet, recorded at once so that no later session
// shows it, or the card this session showed, as a reload left it. Runs in
// the background: nothing waits on the state or the store for it. `start`
// is the session.start run it belongs to, `admitted` its rank. A run whose
// reads another load's change has overtaken (a later reload's, or an
// earlier one's that answered late) shows no card of its own: its writes are
// conditional on the version it read, and it takes over the card shown
// instead, where it ranks above the run that has it.
async function startWelcome($: EngineInterface, e: SessionStartInput, sessionId: string | null, start: number, admitted: Promise<Run | null>): Promise<void> {
  // Whether this run still speaks for the load: no later session.start run,
  // and no reset of the session's state, since it began.
  const resetAt = resets
  const current = (): boolean => start === welcomeStarts && resets === resetAt
  if (!e.isInteractive || e.surface === null) return
  // Nor where the session's id cannot be read: its landing and its close
  // are kept under it, so Got it could not keep the card closed.
  if (sessionId === null) return
  const key = destinationKey(config)
  const me = ownerOf(start)
  const save = (key: string, value: unknown): Promise<void> => $.store.set(key, value)
  // Where the session keeps no state, there is no card: the band draws from
  // it. Nor in a load replaced before its rank came back (`admit`).
  const run = await admitted
  if (run === null) return
  const { rank } = run
  let held: { value: Welcome | null; version: number }
  try {
    const read = await $.state.get(WELCOME_STATE)
    held = { value: read.value ?? null, version: read.version }
  } catch {
    return
  }
  if (!current()) return
  const isOurs = (card: Welcome | null): boolean => card !== null && card.version === PLUGIN_VERSION && sessionId !== null && card.sessionId === sessionId
  if (isOurs(held.value)) return takeOver($, sessionId as string, start, run, current)
  // A card a later load or run showed (another version's, after an update in
  // the session) is left alone; one of the lifetime before a reset is not.
  if (held.value !== null && outranks(held.value, run)) return
  let opened: { welcome: Welcome; isNew: boolean } | null
  try {
    // This session's close is read whether or not its greeting is there: Got
    // it may have been written while the greeting's write was lost, and a
    // resumed session (no state) then shows no card and greets no more.
    const [latest, greeted, closed] = await Promise.all([$.store.get(WELCOME_KEY), $.store.get(greetedKey(PLUGIN_VERSION)), sessionId === null ? undefined : $.store.get(closedKey(PLUGIN_VERSION, sessionId))])
    if (!current() || closed === true) return
    let landing: unknown
    let versions: string[] = []
    if (sessionId !== null && greetedBy(latest, greeted)?.sessionId === sessionId) {
      landing = await $.store.get(cardKey(PLUGIN_VERSION, sessionId))
    } else if (greetedBy(latest, greeted) === null) {
      // A new card: the versions greeted before name the one it replaces (a
      // store that cannot list them cannot tell: no card).
      versions = greetedVersions(await $.store.keys())
    }
    opened = openWelcome({ latest, greeted, landing, closed, versions }, sessionId, key)
  } catch {
    // Unable to tell whether it showed before: never a card each session.
    return
  }
  if (!current() || opened === null) return
  const shown: Welcome = { ...opened.welcome, owner: me, rank, since: rank, lifetime: run.lifetime }
  // Shown only if the state still stands as read: no other load has shown
  // or changed a card since. The one that does shows it, and greets. Where
  // another has: this version's card (an earlier load's whose write answered
  // first, say), which this run takes over from there, with no greeting of
  // its own; a card a later load or run showed, left to it; or an earlier
  // one's of another version (an update in the session), which this run's
  // card replaces, tried again at the state as it stands now. It counts the
  // rows of this run and of every later one (`since`): this run's own rows,
  // landed while the records were read, count at once; an earlier load's,
  // the version before an update's included, never do, whenever they land.
  let version = held.version
  let isSet = false
  const card: Welcome = shown
  for (let i = 0; i < 8 && !isSet; i += 1) {
    let wrote: { isSet: boolean; version: number }
    try {
      wrote = await $.state.set(WELCOME_STATE, card, { ifVersion: version })
    } catch {
      return
    }
    // A run of the lifetime before a reset of the session's state (seen
    // here, or by the count its write, landed or missed, brought up to date)
    // shows no card, and a card it wrote as the reset came goes again,
    // unless a later run wrote since.
    if (resets !== resetAt || !(await inLifetime($, run))) {
      if (wrote.isSet) await takeBack($, me, run, null).catch(() => undefined)
      return
    }
    isSet = wrote.isSet
    if (isSet) break
    if (!current()) return
    let now: { value: Welcome | null; version: number }
    try {
      const read = await $.state.get(WELCOME_STATE)
      now = { value: read.value ?? null, version: read.version }
    } catch {
      return
    }
    if (isOurs(now.value)) return takeOver($, sessionId as string, start, run, current)
    if (now.value !== null && outranks(now.value, run)) return
    version = now.version
  }
  if (!isSet) return
  // The greeting line is this run's, which showed the card, even when a
  // later run takes the card over from here.
  if (opened.isNew) {
    try {
      $.ui.log(clean(welcomeNotice(card, config)))
    } catch {
      // No transcript here: the card says it.
    }
  }
  if (!current()) return
  records = new CardRecords(save, card, opened.isNew)
  cardReady = cardId(card.version, card.sessionId)
  redrawBand($)
  // Rows that landed while the records were read, and while the card was
  // written, recorded before it was: the card says so now, once.
  recount($, run)
}

// This session's card as the session's state has it (after a reload, or
// shown first by another load): taken over by this run, of rank `rank`,
// unless a later load or run has it. A close the store has, which the state
// may lack (its write refused, or late), wins; where the store cannot be read
// there is no telling, and no card.
async function takeOver($: EngineInterface, sessionId: string, start: number, run: Run, current: () => boolean): Promise<void> {
  const { rank } = run
  const key = destinationKey(config)
  const me = ownerOf(start)
  let stored: { greeted: boolean; closed: unknown }
  try {
    const [latest, greeted, closed] = await Promise.all([$.store.get(WELCOME_KEY), $.store.get(greetedKey(PLUGIN_VERSION)), $.store.get(closedKey(PLUGIN_VERSION, sessionId))])
    stored = { greeted: greetedBy(latest, greeted) !== null, closed }
  } catch {
    return
  }
  if (stored.closed === true) {
    closedCard = cardId(PLUGIN_VERSION, sessionId)
    redrawBand($)
  }
  if (!current()) return
  const take = (card: Welcome): Welcome | null => {
    // Another version's card (an update in the session), or another
    // session's: not this run's to close or take over.
    if (card.version !== PLUGIN_VERSION || card.sessionId !== sessionId) return null
    if (stored.closed === true) return { ...card, closed: true }
    // A later load, or run, took the card over meanwhile: it shows it.
    if (outranks(card, run)) return null
    const reopened = reopenWelcome(card, key)
    if (reopened === null) return null
    // Moved where rows go now, or of the lifetime before a reset: it counts
    // from this run on, so rows that went there before never count again.
    // Of the lifetime before, its count and landing go too: none is this
    // lifetime's.
    const stale = card.lifetime !== run.lifetime
    const anew = reopened.welcome.destination !== card.destination || stale
    return { ...reopened.welcome, ...(stale ? { sent: 0, landedAt: null, landedTo: null } : {}), owner: me, rank, since: anew ? rank : card.since, lifetime: run.lifetime }
  }
  // As `changeCard`, but a run of the lifetime before a reset of the
  // session's state (as its write, landed or missed, shows) takes over
  // nothing: a card it took as the reset came goes back as it was.
  let taken: Welcome | null = null
  try {
    for (let i = 0; i < 8; i += 1) {
      const held = await $.state.get(WELCOME_STATE)
      const card = held.value ?? null
      const next = card === null ? null : take(card)
      if (card === null || next === null) break
      const written = { ...next, closed: card.closed || next.closed }
      const wrote = await $.state.set(WELCOME_STATE, written, { ifVersion: held.version })
      if (!(await inLifetime($, run))) {
        if (wrote.isSet) await takeBack($, me, run, card).catch(() => undefined)
        return
      }
      if (wrote.isSet) {
        taken = written
        break
      }
    }
  } catch {
    taken = null
  }
  if (taken === null || taken.closed || !current()) return
  // The greeting written again where the store has none (its write was
  // lost with an earlier load): the same record, so no later session greets.
  // Its landing too, the same value, whose write an earlier load may have lost.
  records = new CardRecords((key, value) => $.store.set(key, value), taken, !stored.greeted, true)
  if (!(await stillOpen($, sessionId)) || !current()) return
  cardReady = cardId(PLUGIN_VERSION, sessionId)
  redrawBand($)
  // Rows this run landed while the records were read (where the card counts
  // rows now, if it moved here): counted once.
  recount($, run)
}

// Whether the store has no close for this session's card, read once more
// once this run took the card over: Got it in another load meanwhile, its
// state write refused, closed it. A store that cannot be read tells nothing:
// no card then.
async function stillOpen($: EngineInterface, sessionId: string): Promise<boolean> {
  let closed: unknown
  try {
    closed = await $.store.get(closedKey(PLUGIN_VERSION, sessionId))
  } catch {
    return false
  }
  if (closed !== true) return true
  closedCard = cardId(PLUGIN_VERSION, sessionId)
  redrawBand($)
  return false
}

// The store's close for the card the band draws, read again in the
// background each time it draws it, one read at a time (one that has not
// answered in CLOSE_READ_MS stops no other): Got it in an earlier load, its
// state write refused and its store write late, hides the card here once
// that write lands, with no reload and no press.
let closeRead: Promise<void> | null = null
let closeReadAt = 0
const CLOSE_READ_MS = 10_000
function checkClosed($: EngineInterface, version: string, sessionId: string): void {
  if (closeRead !== null && Date.now() - closeReadAt < CLOSE_READ_MS) return
  closeReadAt = Date.now()
  const read: Promise<void> = $.store
    .get(closedKey(version, sessionId))
    .then(closed => {
      if (closed !== true) return
      closedCard = cardId(version, sessionId)
      redrawBand($)
    })
    .catch(() => {
      internalErrors += 1
    })
    .finally(() => {
      // A read that answers after a later one began leaves that one's guard.
      if (closeRead === read) closeRead = null
    })
  closeRead = read
}

// Changes the card in `$.state`: reads it, applies `change`, and writes it
// only while it still stands at the version read, reading again when it
// does not, so no write of any load, however late, undoes another's. A
// close always stays. Resolves to the card written, or null for none (no
// card, or `change` left it as it was).
async function changeCard($: EngineInterface, change: (card: Welcome) => Welcome | null): Promise<Welcome | null> {
  for (let i = 0; i < 8; i += 1) {
    const held = await $.state.get(WELCOME_STATE)
    const card = held.value ?? null
    if (card === null) return null
    const next = change(card)
    if (next === null) return null
    const written = { ...next, closed: card.closed || next.closed }
    if ((await $.state.set(WELCOME_STATE, written, { ifVersion: held.version })).isSet) return written
  }
  return null
}

// A surface that draws no band may refuse the redraw: nothing to draw then.
function redrawBand($: EngineInterface): void {
  try {
    $.ui.invalidate('ui.render')
  } catch {
    // No band here.
  }
}

// The run `id` names (a load's session.start run and the place its rows land,
// `countKey`) has landed `total` rows in all in `to` (their destination's
// `destinationKey`): once the run is admitted (`admit`), the session's
// `delivered` says so, at its rank, and the card counts it where it counts
// rows from that run on (`recount`), however often or late the run says so.
// A run never admitted (a load replaced before its rank came back) records
// nothing. Rows a run of another configuration sent elsewhere say nothing
// here.
function rowsLanded($: EngineInterface, id: string, total: number, to: string | null, admitted: Promise<Run | null>): void {
  if (to === null || total <= 0) return
  void admitted
    .then(async run => {
      if (run === null) return
      await noteDelivered($, id, { ...run, to, rows: total })
      recount($, run)
    })
    .catch(() => {
      internalErrors += 1
    })
}

// The run and destination `id` has landed `entry.rows` rows in all: the
// session's `delivered` says so, with the run's rank, whatever the card
// shows, in a session that shows cards. Only ever up, at the version read,
// read again on a miss; one that cannot be written leaves the card to count
// what it knows.
async function noteDelivered($: EngineInterface, id: string, entry: DeliveredRows): Promise<void> {
  if (!cardSession) return
  try {
    for (let i = 0; i < 8; i += 1) {
      const held = await $.state.get(DELIVERED_STATE)
      const now = held.value ?? {}
      if ((now[id]?.rows ?? 0) >= entry.rows) return
      if ((await $.state.set(DELIVERED_STATE, { ...now, [id]: entry }, { ifVersion: held.version })).isSet) return
    }
  } catch {
    // No session state here, or refused: no card counts from it.
  }
}

// Whether `run` is of the lifetime the session's count of runs stands in,
// as the dispatch reads it: run right after a write of its own, which
// brings that view up to date, it tells a run from before a reset of the
// session's state. A count that cannot be read tells nothing: no.
async function inLifetime($: EngineInterface, run: Run): Promise<boolean> {
  try {
    return (await $.state.get(LOADS_STATE)).value?.lifetime === run.lifetime
  } catch {
    return false
  }
}

// Puts `value` (null: no card) back over the card the run `owner` names, of
// lifetime `run`, wrote as a reset of the session's state came: while the
// state still holds that card, however often that run's own counts changed
// it since, and never over one a later run wrote.
async function takeBack($: EngineInterface, owner: string, run: Run, value: Welcome | null): Promise<void> {
  for (let i = 0; i < 8; i += 1) {
    const held = await $.state.get(WELCOME_STATE)
    const card = held.value ?? null
    if (card === null || card.owner !== owner || card.lifetime !== run.lifetime) return
    if ((await $.state.set(WELCOME_STATE, value, { ifVersion: held.version })).isSet) return
  }
}

// Whether `card` is a later run's than `run`, of the same lifetime: a card
// of the lifetime before a reset of the session's state outranks no run of
// the one after it (which a run checks of itself, `inLifetime`).
const outranks = (card: Welcome, run: Run): boolean => card.lifetime === run.lifetime && (card.rank ?? 0) > run.rank

// Whether `card` counts the rows of `run`: one of the lifetime it counts in,
// from the run it counts from on.
const counts = (card: Welcome, run: Run): boolean => run.lifetime === card.lifetime && run.rank >= card.since

// The rows `delivered` has landed where `card` counts rows, of the runs it
// counts: each run's total once, so none stands for another's.
function landedFor(card: Welcome, delivered: DeliveredState | undefined): number {
  let rows = 0
  for (const entry of Object.values(delivered ?? {})) {
    if (typeof entry !== 'object' || entry === null || entry.to !== card.destination) continue
    if (typeof entry.rank === 'number' && typeof entry.lifetime === 'string' && counts(card, entry) && typeof entry.rows === 'number' && entry.rows > 0) rows += entry.rows
  }
  return rows
}

// The card's count, read again from `delivered`, as one moment of the
// session's state has both, by the run `run`: only where the card counts
// that run's rows, so the card is left to the runs it counts (never another
// version's card, say, that replaced this run's, nor one opened after the
// state was reset). Written only while
// the card stands as read, read again on a miss, and only ever up; a closed
// card counts no more. The first time rows count, the card says they landed,
// and the load whose run holds the card says so with a toast as well, as the
// band may be collapsed.
function recount($: EngineInterface, run: Run): void {
  let first = false
  void (async (): Promise<Welcome | null> => {
    for (let i = 0; i < 8; i += 1) {
      const held = await $.state.get(WELCOME_STATE)
      const card = held.value ?? null
      if (card === null || card.closed || card.destination === null || !counts(card, run)) return null
      const landed = landedFor(card, (await $.state.get(DELIVERED_STATE)).value)
      if (landed <= card.sent) return null
      first = card.landedAt === null
      const next: Welcome = { ...card, sent: landed, ...(first ? { landedAt: Date.now(), landedTo: card.destination } : {}) }
      if ((await $.state.set(WELCOME_STATE, next, { ifVersion: held.version })).isSet) return next
      first = false
    }
    return null
  })()
    .then(card => {
      if (card === null) return
      // At every change: a landing whose write failed goes again.
      records?.update(card)
      if (!first || card.owner !== ownerOf(welcomeStarts)) return
      try {
        $.ui.toast(clean(landedToast(config), 500), { timeoutMs: 15_000 })
      } catch {
        // No toast here: the card says it.
      }
    })
    .catch(() => {
      internalErrors += 1
    })
}

// Got it was pressed, or (`landedOnly`) a prompt came after rows landed
// where rows go now. Either hides the card here and writes the store's close
// on its own, at once, behind no other write (its key only ever says true),
// whatever becomes of the state's write (refused, late, or never answered)
// and whether or not this load has taken the card over yet. A prompt reads
// the state first: only a card that shows rows landed here closes. Either
// closes only the card of this version and session, however often its write
// is tried again: never one an update put in its place in the session.
// A store close (`closeWelcome`) whose write failed: written again at each
// flush tick until one lands, so a reload, its state close refused too,
// shows the card no more.
let unsavedClose: string | null = null
let closeSaving = false
function saveClose($: EngineInterface): void {
  const key = unsavedClose
  if (key === null || closeSaving) return
  closeSaving = true
  $.store
    .set(key, true)
    .then(() => {
      if (unsavedClose === key) unsavedClose = null
    })
    .catch(() => {
      internalErrors += 1
    })
    .finally(() => {
      closeSaving = false
    })
}

function closeWelcome($: EngineInterface, landedOnly = false): void {
  const sessionId = tracer.sessionId
  const id = cardId(PLUGIN_VERSION, sessionId)
  const here = destinationKey(config)
  const isThis = (card: Welcome): boolean => card.version === PLUGIN_VERSION && card.sessionId === sessionId
  const landedHere = (card: Welcome): boolean => card.landedAt !== null && card.landedTo === here
  const close = (): void => {
    closedCard = id
    if (sessionId !== null) {
      unsavedClose = closedKey(PLUGIN_VERSION, sessionId)
      saveClose($)
    }
    redrawBand($)
  }
  if (!landedOnly) close()
  void (async () => {
    if (landedOnly) {
      const shown = (await $.state.get(WELCOME_STATE)).value ?? null
      if (shown === null || shown.closed || !isThis(shown) || !landedHere(shown)) return
      close()
    }
    await changeCard($, card => (card.closed || !isThis(card) || (landedOnly && !landedHere(card)) ? null : { ...card, closed: true }))
  })().catch(() => {
    internalErrors += 1
  })
}

// This table's dead-letter files, those elsewhere in the dead-letter
// directory (other tables' subdirectories, at most 50 of them, and files from
// the older flat layout), and files too large to read back.
async function deadLetterSummary($: EngineInterface): Promise<DeadLetterSummary> {
  if (config.deadLetterDir === '') return { error: 'no dead-letter directory configured' }
  const dir = deadLetterPartition(config)
  const summary = { dir, files: 0, bytes: 0, byKind: {} as Record<string, number>, oversized: [] as string[], elsewhere: 0 }
  try {
    if (!(await $.fs.exists(config.deadLetterDir))) return summary
    if (await $.fs.exists(dir)) {
      for (const entry of await $.fs.list(dir)) {
        const kind = kindOf(entry.name)
        if (entry.kind !== 'file' || kind === null || entry.size === 0) continue
        summary.files += 1
        summary.bytes += entry.size
        summary.byKind[kind] = (summary.byKind[kind] ?? 0) + 1
        if (entry.size > HOST_FILE_BYTES) summary.oversized.push(entry.name)
      }
    }
    const mine = destinationDir(tableRef(config))
    let dirs = 0
    for (const entry of await $.fs.list(config.deadLetterDir)) {
      if (entry.kind === 'file' && kindOf(entry.name) !== null && entry.size > 0) summary.elsewhere += 1
      else if (entry.kind === 'dir' && entry.name !== mine && dirs < 50) {
        dirs += 1
        for (const inner of await $.fs.list(joinPath(config.deadLetterDir, entry.name))) {
          if (inner.kind === 'file' && kindOf(inner.name) !== null && inner.size > 0) summary.elsewhere += 1
        }
      }
    }
    return summary
  } catch (err) {
    return { error: clean(`could not list ${config.deadLetterDir}: ${errorText(err)}`, 300) }
  }
}

export const register: Register = (on, registered) => {
  options = registered
  config = resolveConfig(registered, {})
  configured = false
  awaitingConfig.length = 0

  on('session.start', async ($, e, next) => {
    // A read for the card by an earlier run of this load is not used from
    // here on.
    const start = ++welcomeStarts
    cardReady = null
    // Its rank, in a session a person sees (a `-p` run draws no card).
    cardSession = e.isInteractive && e.surface !== null
    const ranked = cardSession ? nextLoad($).catch(() => null) : Promise.resolve(null)
    const admitted = admit($, ranked).catch(() => null)
    try {
      config = resolveConfig(options, await readEnv($))
      clockAfter = (ms, fn) => $.clock.after(ms, fn)
      const io: Io = {
        now: () => Date.now(),
        after: (ms, fn) => $.clock.after(ms, fn),
        fetch: async (url, init) => {
          const answer = await $.http.fetch(url, init)
          return { status: answer.status, text: answer.text }
        },
        runCommand: async (argv, timeoutMs) => {
          const run = await $.process.run(argv, { timeoutMs })
          return { exitCode: run.exitCode, stdout: run.stdout, stderr: run.stderr }
        },
        // Refused by a policy, it reads as unset: the token command is next.
        envToken: () => $.env.get('BQAA_ACCESS_TOKEN').catch(none),
        writeFile: (path, text) => $.fs.write(path, text),
        readFile: async path => {
          const read = await $.fs.read(path)
          return typeof read === 'string' ? read : ''
        },
        exists: path => $.fs.exists(path),
        listDir: async path => (await $.fs.list(path)).map(entry => ({ name: entry.name, kind: entry.kind, size: entry.size, mtimeMs: entry.mtimeMs })),
        storeGet: key => $.store.get(key),
        storeSet: (key, value) => $.store.set(key, value),
        storeDelete: key => $.store.delete(key),
        status: text => $.ui.status(text === undefined ? undefined : clean(text, 300)),
        debug: text => $.ui.log(clean(text), { to: 'debug' }),
        toast: text => $.ui.toast(clean(text, 500), { timeoutMs: 15_000 }),
        notice: text => $.ui.log(clean(text)),
        redraw: () => $.ui.invalidate('ui.render'),
        // Where setup.sh is, for the fix when BigQuery cannot find the dataset.
        pluginRoot: $.plugin.root,
        sent: (rows, to) => {
          const key = destinationKey(to)
          if (key === null || rows <= 0) return
          const id = countKey(ownerOf(start), key)
          const total = (landedByWriter.get(id) ?? 0) + rows
          landedByWriter.set(id, total)
          rowsLanded($, id, total, key, admitted)
        },
      }
      const previous = writer
      const own: Writer = new Writer(io, () => config, () => tracer.sessionId)
      writer = own
      // session.start can fire again without a reload (the plugin was turned
      // off and on): keep what the previous writer still held.
      if (previous) writer.adopt(previous)
      // The host keeps one status line per plugin until it is replaced, a
      // reload included: this writer starts with none, as it holds no notice.
      writer.clearStatus()
      for (const held of early.splice(0)) writer.enqueue(held.row, held.redactions)
      const [sessionId, version] = await Promise.all([$.session.id().catch(() => null), $.session.version().catch(() => null)])
      tracer.setSession({ sessionId, cwd: e.cwd, engineVersion: version?.version ?? null })
      flushTimer?.cancel()
      flushTimer = $.clock.every(config.flushIntervalMs, () => {
        void writer?.flush()
        saveClose($)
      })
      if (config.enabled && config.recordHostname) $.clock.after(0, () => void lookUpHostname($))
      if (canDeliver(config) && !config.dryRun) {
        // Off this dispatch: the token command and the replay run later.
        $.clock.after(0, () => writer?.prefetchToken())
        if (config.autoBackfill) {
          $.clock.after(15_000, () => {
            void writer?.backfill(['spill', 'retry'], 'auto')
          })
        }
      }
      for (const problem of config.problems) $.ui.log(clean(`bqaa: ${problem}`), { to: 'debug' })
    } catch (err) {
      internalErrors += 1
      $.ui.log(clean(`bqaa: setup failed, rows are not sent: ${errorText(err)}`), { to: 'debug' })
    }
    // A session-start row that waited for the configuration is written now.
    configurationLoaded()
    // Each in its own try: a refused name must not keep the others out.
    try {
      await $.command.register({ name: 'bqaa-status', description: 'BigQuery Agent Analytics: queue, sent, failed and dead-letter counts', immediate: true })
    } catch {
      internalErrors += 1
    }
    try {
      await $.command.register({ name: 'bqaa-flush', description: 'BigQuery Agent Analytics: send the queued rows now', immediate: true })
    } catch {
      internalErrors += 1
    }
    try {
      await $.command.register({ name: 'bqaa-backfill', description: 'BigQuery Agent Analytics: resend rows parked in the dead-letter directory', argumentHint: '[force]', immediate: true })
    } catch {
      internalErrors += 1
    }
    // In the background: neither the first prompt nor anything above waits
    // on the store for the welcome card.
    void startWelcome($, e, tracer.sessionId, start, admitted).catch(() => {
      internalErrors += 1
    })
    return next(e)
  })

  // Observers: each calls `next` before it records anything, after reading
  // the session id where it needs it. Their `.catch` spells out what a
  // failure means here: the event goes on as if the mod were not there.
  on('classic.SessionStart', ($, e, next) => {
    // /clear, /resume and /branch reset the session's state: a card a run
    // began before stays the session's before (`startWelcome`).
    if (RESET_SOURCES.has(String(e.source))) resets += 1
    const pending = next(e)
    // Its row carries what session.start loads (the user, the agent name,
    // the writer mode, the host version): written now if that is done, else
    // once it is, at the time the event came.
    const at = Date.now()
    whenConfigured(() => tracer.sessionStarted(e, at))
    return pending
  }).catch(($, e, next) => next(e))

  on('classic.SubagentStart', async ($, e, next) => {
    await readSessionId($)
    const pending = next(e)
    safely(() => tracer.subagentStarted(e))
    return pending
  }).catch(($, e, next) => next(e))

  on('classic.PermissionRequest', ($, e, next) => {
    const pending = next(e)
    safely(() => tracer.permissionRequested(e))
    return pending
  }).catch(($, e, next) => next(e))

  // Every prompt is passed on first, as it came; the hook then waits on
  // nothing but the session id (no network, no process). A prompt while rows
  // cannot be written also starts a retry, from a timer: the person may have
  // just signed in.
  on('prompt.submit', async ($, e, next) => {
    const submittedAt = Date.now()
    const entered = await next(e)
    if (entered.drop === undefined) {
      await readSessionId($)
      safely(() =>
        tracer.promptSubmitted({
          text: entered.text,
          submittedAt,
          originKind: e.origin.kind,
          duringTurnId: e.turnId,
          wait: e.wait,
          attachments: e.attachments,
        }),
      )
      safely(() => writer?.retrySoon())
      // Rows landed before this prompt, typed at the terminal: the welcome
      // card has said what it is for.
      if (e.origin.kind === 'composer') safely(() => closeWelcome($, true))
    }
    return entered
  }).catch(($, e, next) => next(e))

  // The band above the prompt while rows cannot be written for lack of
  // credentials or permission, or because BigQuery cannot find the dataset:
  // what is wrong, the fix (for a missing dataset, the setup.sh command on a
  // line of its own), and how many rows wait. Otherwise, in the session that
  // greets an install or an update, the welcome card. What the mods after
  // this one draw there stays, under these lines; a survey has the band to
  // itself.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e)
    const current = writer
    if (current === null || e.props.hasSurvey) return theirs
    if (current.problem === null) {
      // The card as the session's state has it: this read redraws the band
      // whenever any load changes it.
      let shown: Welcome | null = null
      try {
        shown = (await $.state.get(WELCOME_STATE)).value ?? null
      } catch {
        shown = null
      }
      if (shown === null || shown.closed || shown.version !== PLUGIN_VERSION || shown.sessionId !== tracer.sessionId) return theirs
      const id = cardId(shown.version, shown.sessionId)
      if (id === closedCard || id !== cardReady) return theirs
      // A close another load wrote late hides it at a later draw.
      const { version, sessionId } = shown
      if (sessionId !== null) safely(() => checkClosed($, version, sessionId))
      // Rows that landed elsewhere than this configuration sends them say
      // nothing here: the card waits.
      const here = shown.destination === destinationKey(config)
      const card = welcomeCard(here ? shown : { ...shown, landedAt: null, landedTo: null }, config, here ? shown.sent : 0)
      const { Box, Button, Text } = $.ui.resolve(e)
      const title = Box({
        flexDirection: 'row',
        justifyContent: 'space-between',
        children: [
          Box({
            flexDirection: 'row',
            flexWrap: 'wrap',
            flexShrink: 1,
            children: [
              Text({ color: card.tone, bold: true, children: `${card.glyph} ` }),
              Text({ bold: true, children: clean(card.title, 200) }),
              Text({ dimColor: true, children: ` ${clean(card.tag, 100)}` }),
            ],
          }),
          Button({ key: 'bqaa-welcome-close', label: 'Got it', role: 'dismiss', onPress: () => closeWelcome($) }),
        ],
      })
      const lines = card.lines.map(line => Text({ ...(line.tone ? { color: line.tone } : {}), ...(line.dim ? { dimColor: true } : {}), children: clean(line.text, 500) }))
      // In a box where the band has the rows for its border; lines alone where not.
      const frame = e.props.maxRows >= lines.length + 3 ? { borderStyle: 'round', borderColor: card.tone, paddingX: 1 } : {}
      return Box({ flexDirection: 'column', children: [Box({ flexDirection: 'column', ...frame, children: [title, ...lines] }), theirs] })
    }
    const { headline, fix } = current.problemText()
    const run = current.commandLine()
    const { Box, Text } = $.ui.resolve(e)
    const line = Box({
      flexDirection: 'row',
      flexWrap: 'wrap',
      children: [
        Text({ color: 'warning', bold: true, children: '⚠ BQAA: ' }),
        Text({ children: clean(`rows are not being written: ${headline}. To fix, ${fix}.`, 500) }),
        Text({ dimColor: true, children: ` ${current.waitingText()} row(s) waiting.` }),
      ],
    })
    const lines = run === null ? [line] : [line, Text({ dimColor: true, children: clean(run, COMMAND_LINE_CHARS) })]
    return Box({ flexDirection: 'column', children: [...lines, theirs] })
  })

  // Permission decisions, from the host's own collector records: each record
  // goes on first, unchanged, and one a later hook refused is not used.
  on('telemetry.log', { to: 'collector' }, async ($, e, next) => {
    const logged = await next(e)
    if (logged.deny === undefined) safely(() => tracer.recordSeen(e.event, e.attributes))
    return logged
  }).catch(($, e, next) => next(e))

  on('turn.start', async ($, e, next) => {
    await readSessionId($)
    const pending = next(e)
    safely(() => tracer.turnStarted(e))
    return pending
  })

  on('turn.step', async function* ($, e, next) {
    await readSessionId($)
    const stream = next(e)
    let ctx: StepCtx | undefined
    safely(() => {
      ctx = tracer.stepStarted(e)
    })
    let firstContentAt: number | undefined
    let result
    try {
      result = yield* timedStream(stream, () => {
        firstContentAt = Date.now()
      })
    } catch (err) {
      const failed = ctx
      if (failed) safely(() => tracer.stepFailed(failed, err))
      throw err
    }
    const done = ctx
    const response = result
    if (done) safely(() => tracer.stepFinished(done, response as StepResult, firstContentAt))
    return result
  })

  on('tool.call', async ($, e, next) => {
    await readSessionId($)
    const pending = next(e)
    let ctx: ToolCtx | undefined
    safely(() => {
      ctx = tracer.toolStarted(e as unknown as Record<string, unknown>)
    })
    let result
    try {
      result = await pending
    } catch (err) {
      const failed = ctx
      if (failed) safely(() => tracer.toolFailed(failed, err))
      throw err
    }
    const done = ctx
    const outcome = result
    if (done) safely(() => tracer.toolFinished(done, outcome as ToolOutcome))
    return result
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    await readSessionId($)
    const pending = next(e)
    safely(() => {
      tracer.turnCompleted({
        turnId: e.turnId,
        agentId: e.agentId,
        answer: e.answer,
        durationMs: e.durationMs,
        isAborted: e.isAborted,
        reason: e.reason,
        refusal: e.reason === 'refusal' ? e.refusal : undefined,
        usage: e.usage,
      })
      // A finished main-loop turn is a natural moment to send.
      if (!e.agentId) writer?.requestFlush(0)
    })
    return pending
  })

  on('session.end', async ($, e, next) => {
    safely(() => tracer.sessionEnded(e))
    if (writer) {
      if (e.reason === 'clear' || e.reason === 'resume') {
        // The process goes on under another session: send as usual.
        safely(() => writer?.requestFlush(0))
      } else {
        // The process is ending. No request starts here: what is still
        // queued goes to a spill file the next session sends, and the write
        // may take at most the exit budget, inside the engine's
        // end-of-session bound.
        const budget = Math.max(0, Math.min(config.exitFlushMs, next.budget.remainingMs - 300))
        try {
          await writer.spillForExit(budget)
        } catch {
          internalErrors += 1
        }
      }
    }
    return next(e)
  })

  on('command.run', { command: 'bqaa-status' }, async ($, e) => {
    if (!writer) return say('BigQuery Agent Analytics mod: not started (session.start has not run).')
    const summary = await deadLetterSummary($)
    const text = formatStatus({
      config,
      writer,
      now: Date.now(),
      sessionId: tracer.sessionId,
      deadLetter: summary,
      backfill: writer.backfillReport,
      isBackfilling: writer.isBackfilling,
    })
    return say(internalErrors > 0 ? `${text}\ninternal errors (skipped bookkeeping): ${internalErrors}` : text)
  })

  on('command.run', { command: 'bqaa-flush' }, async () => {
    if (!writer) return say('BigQuery Agent Analytics mod: not started.')
    if (!canDeliver(config)) return say(`BigQuery Agent Analytics mod: nothing to flush to (${config.problems.join('; ') || 'disabled'}).`)
    const queued = writer.queue.length
    writer.requestFlush(0)
    return say(`BigQuery Agent Analytics mod: flush requested for ${queued} queued row(s); /bqaa-status shows the result.`)
  })

  on('command.run', { command: 'bqaa-backfill' }, async ($, e) => {
    const current = writer
    if (!current) return say('BigQuery Agent Analytics mod: not started.')
    if (current.isBackfilling) return say('BigQuery Agent Analytics mod: a backfill is already running; /bqaa-status shows progress.')
    const summary = await deadLetterSummary($)
    if ('error' in summary) return say(`BigQuery Agent Analytics mod: ${summary.error}`)
    if (summary.files === 0) {
      const elsewhere = summary.elsewhere > 0 ? ` ${summary.elsewhere} file(s) elsewhere in ${config.deadLetterDir} are for other tables or an older layout; they are replayed only under a configuration for their table.` : ''
      return say(`BigQuery Agent Analytics mod: no dead-letter files for ${tableRef(config)} in ${summary.dir}.${elsewhere}`)
    }
    // `force` replays afresh, too, a file another session's replay holds (its
    // mark, or its record cut off part-way): for a session that stopped
    // mid-replay. Runs in the background: the command returns now. Its
    // result is a transcript line.
    const force = (e.args ?? '').trim() === 'force'
    void current
      .backfill(['spill', 'retry', 'rejected'], force ? 'force' : 'command')
      .then(report => {
        const refused = report.rowsRefused ? `, ${report.rowsRefused} of them refused` : ''
        const other = report.otherTableRows ? ` (${report.otherTableRows} for another table)` : ''
        const oversized = report.oversizedFiles.length ? `, ${report.oversizedFiles.length} file(s) too large to read` : ''
        const incomplete = report.incompleteFiles.length ? `, ${report.incompleteFiles.length} file(s) without a completion record kept` : ''
        $.ui.log(clean(`bqaa backfill: ${report.files} file(s), ${report.rowsSent} row(s) sent, ${report.rowsLeft} left${refused}${other}${oversized}${incomplete}${report.held ? `; left waiting: ${report.held}` : ''}${report.error ? `; stopped: ${report.error}` : ''}`, 500))
      })
      .catch(() => {
        internalErrors += 1
      })
    return say(`BigQuery Agent Analytics mod: backfill started for ${summary.files} file(s) in ${summary.dir}; /bqaa-status shows progress.`)
  })
}
