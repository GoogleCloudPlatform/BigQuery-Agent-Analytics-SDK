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

// The welcome card across a fresh load of the mod's code (a hot reload):
// what the host keeps for the session (`$.state`) and across sessions
// (`$.store`) is all a new load has; the old load's store writes still
// answer, late, in either order. The host's test kit cannot load the code
// twice, so this loads hooks/register.ts again under node 24 (which runs
// TypeScript as is), with a small stand-in for `$`. No network, no files.
//   node tests/reload.test.mjs
import assert from 'node:assert/strict'
import { AsyncLocalStorage } from 'node:async_hooks'
import { registerHooks } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { PLUGIN_VERSION } from '../hooks/lib/config.ts'

const REGISTER = pathToFileURL(join(dirname(fileURLToPath(import.meta.url)), '..', 'hooks', 'register.ts')).href
// A load of another version of the code (an update in the session): its
// modules load afresh, each under the `version` its importer carries, and
// its config.ts names that version.
const NEXT = '0.2.0'
registerHooks({
  resolve(specifier, context, nextResolve) {
    const resolved = nextResolve(specifier, context)
    const version = context.parentURL ? new URL(context.parentURL).searchParams.get('version') : null
    if (version === null || !resolved.url.startsWith('file:') || !resolved.url.endsWith('.ts')) return resolved
    return { ...resolved, url: `${resolved.url}?version=${version}` }
  },
  load(url, context, nextLoad) {
    const at = new URL(url)
    const version = at.searchParams.get('version')
    if (version === null) return nextLoad(url, context)
    const loaded = nextLoad(url, { ...context, format: 'module-typescript' })
    if (!at.pathname.endsWith('/lib/config.ts')) return loaded
    return { ...loaded, source: String(loaded.source).replace(/PLUGIN_VERSION = '[^']*'/, `PLUGIN_VERSION = '${version}'`) }
  },
})
const SESSION = 'reload-session'
const OWN = `welcome:${PLUGIN_VERSION}:${SESSION}`
const CLOSED = `${OWN}:closed`
const START = { surface: 'terminal', isInteractive: true, cwd: '/work' }
// The event a call of `$` is made under: every `$.state.get` of one reads
// one moment, as the host's does, until a write of its own, landed or
// missed, brings the whole moment up to date.
const dispatch = new AsyncLocalStorage()
const settle = async () => {
  for (let i = 0; i < 200; i += 1) await Promise.resolve()
}

// What outlives a load of the code: the store, the session's state (each
// value with a version, a write with `ifVersion` landing only while the
// value stands at it, as the host's), and the writes still to answer.
// `hold` picks the store keys whose writes wait, `refuseStore` those
// refused; `holdState` the state writes that wait, `refuseState` those
// refused (each given the value and its key); `read` may hold store reads.
function host({ hold = () => false, holdState = () => false, refuseState = () => false, refuseStore = () => false, read, liveReads = false } = {}) {
  const store = new Map()
  const state = new Map()
  const held = []
  const write = (key, value) => {
    if (refuseStore(key)) return Promise.reject(new Error('refused by policy'))
    const land = () => store.set(key, structuredClone(value))
    if (!hold(key)) return Promise.resolve(land())
    return new Promise(resolve => held.push({ key, release: () => resolve(land()) }))
  }
  // A new version for each write that lands, from one sequence for the
  // session's values. The code compares a value's version only with that
  // value's own; the host's test runtime numbers writes of different values
  // alike at times, so nothing here asks more of the numbers than that.
  let stamp = 0
  const stateSet = (ref, value, options) => {
    if (refuseState(value, ref.key)) return Promise.reject(new Error('refused by policy'))
    const land = () => {
      const now = state.get(ref.key) ?? { value: undefined, version: 0 }
      if (options?.ifVersion !== undefined && options.ifVersion !== now.version) return { isSet: false, version: now.version }
      stamp += 1
      state.set(ref.key, { value: structuredClone(value), version: stamp })
      return { isSet: true, version: stamp }
    }
    if (!holdState(value, ref.key)) return Promise.resolve(land())
    return new Promise(resolve => held.push({ key: 'state', value, release: () => resolve(land()) }))
  }
  const card = () => state.get('welcome')?.value
  return { store, state, held, write, stateSet, read, card, liveReads, release: key => held.splice(held.findIndex(h => h.key === key), 1)[0].release() }
}

// Store reads that wait while `holding` says so, until `answer`.
function heldReads(h, holding) {
  const waiting = []
  h.read = key => (holding(key) ? new Promise(resolve => waiting.push(() => resolve(structuredClone(h.store.get(key))))) : Promise.resolve(structuredClone(h.store.get(key))))
  return { answer: () => waiting.splice(0).forEach(go => go()), count: () => waiting.length }
}

// Whether a value of the session's `delivered` records a run's rows that
// `matches` takes.
const recordsRows = (value, matches) => typeof value === 'object' && value !== null && Object.values(value).some(entry => typeof entry?.rows === 'number' && matches(entry.rows))

let loads = 0
// One load of the code against `h`, with `options` over the usual ones. The
// newest load is the one the host runs: a hot reload cancels the waits of the
// load it replaces, so `$.clock.sleep` never answers there once a later load
// is in, or (`h.refuseWaits`) is refused; its other calls may still answer,
// late.
async function load(h, options = {}, version = null) {
  const index = ++loads
  const { register } = await import(`${REGISTER}?load=${index}${version === null ? '' : `&version=${version}`}`)
  h.current = index
  const hooks = new Map()
  const timers = []
  const lines = []
  const toasts = []
  let redraws = 0
  const on = (name, ...args) => {
    hooks.set(name, args.at(-1))
    return { catch: () => undefined }
  }
  register(on, { project: 'my-gcp-project', dataset: 'agent_analytics', dryRun: true, ...options })
  const $ = {
    env: { get: async name => ({ HOME: '/home/dev' })[name] },
    store: { get: key => (h.read ? h.read(key) : Promise.resolve(structuredClone(h.store.get(key)))), set: h.write, delete: async key => void h.store.delete(key), keys: async () => [...h.store.keys()] },
    state: {
      get: async ref => {
        if (h.stateRead) await h.stateRead(ref.key)
        // `liveReads`: a host whose reads see what stands at once.
        const at = h.liveReads ? undefined : dispatch.getStore()
        if (at && !at.moment) at.moment = structuredClone(h.state)
        const now = (at?.moment ?? h.state).get(ref.key) ?? { value: undefined, version: 0 }
        return { value: structuredClone(now.value), version: now.version }
      },
      set: async (ref, value, options) => {
        const result = await h.stateSet(ref, value, options)
        const at = dispatch.getStore()
        if (at?.moment) at.moment = structuredClone(h.state)
        return result
      },
    },
    // `h.noSessionId`: a host that refuses the session's id.
    // `h.sessionId`: the session's id after /clear, /resume or /branch.
    session: { id: async () => (h.noSessionId ? Promise.reject(new Error('refused by policy')) : (h.sessionId ?? SESSION)), version: async () => ({ version: 'test' }) },
    clock: {
      every: (ms, fn) => (timers.push(() => dispatch.run({ moment: null }, fn)), { cancel() {} }),
      after: () => ({ cancel() {} }),
      sleep: () => new Promise((resolve, reject) => queueMicrotask(() => (h.current === index ? resolve() : h.refuseWaits && reject(new Error('cancelled by a reload'))))),
    },
    command: { register: async () => undefined },
    plugin: { root: '/opt/mods/bigquery-agent-analytics-mod' },
    ui: {
      log: text => void lines.push(text),
      toast: text => void toasts.push(text),
      status: () => undefined,
      invalidate: () => void (redraws += 1),
      resolve: () => Object.fromEntries(['Box', 'Text', 'Button'].map(type => [type, props => ({ type, props })])),
    },
    fs: { write: (...args) => (h.writeFile ? h.writeFile(...args) : Promise.resolve()), read: async () => '', list: async () => [], exists: async () => false },
    http: { fetch: async () => Promise.reject(new Error('no network here')) },
    process: { run: async () => Promise.reject(new Error('no processes here')) },
  }
  const fire = (name, e) => dispatch.run({ moment: null }, () => hooks.get(name)($, e, async x => (name === 'ui.render' ? null : x)))
  const walk = n => (n ? [n, ...(Array.isArray(n.props?.children) ? n.props.children.flatMap(walk) : typeof n.props?.children === 'object' ? walk(n.props.children) : [])] : [])
  const band = async () => walk(await fire('ui.render', { component: 'AbovePrompt', props: { hasSurvey: false, maxRows: 10 } }))
  const button = async () => (await band()).find(n => n.type === 'Button')
  return {
    // The stand-in itself, and the event it reads in, for a test of it.
    $,
    // An event raised in this load.
    fire,
    texts: async () => (await band()).filter(n => n.type === 'Text').map(n => n.props.children),
    greetings: () => lines.filter(line => line.startsWith('bqaa: BigQuery Agent Analytics mod')).length,
    toasts: () => toasts.length,
    // The redraws this load asked for.
    redraws: () => redraws,
    start: async () => {
      await fire('session.start', START)
      await settle()
    },
    shown: async () => (await button()) !== undefined,
    // A prompt typed at the terminal.
    prompt: async () => {
      await fire('prompt.submit', { text: 'hi', wait: false, origin: { kind: 'composer' } })
      await settle()
    },
    close: async () => {
      const b = await button()
      assert.ok(b, 'the card is up')
      dispatch.run({ moment: null }, () => b.props.onPress())
      await settle()
    },
    // Rows queued (`count` session-start rows), and the next flush of them.
    queue: async count => {
      for (let i = 0; i < count; i += 1) await fire('classic.SessionStart', { source: 'startup', session_id: SESSION, model: 'example-model', cwd: '/work' })
    },
    flush: async () => {
      timers.at(-1)()
      await settle()
    },
    // The session's first row, written by the next flush (a dry run).
    land: async () => {
      await fire('classic.SessionStart', { source: 'startup', session_id: SESSION, model: 'example-model', cwd: '/work' })
      timers.at(-1)()
      await settle()
    },
  }
}

const tests = {
  async 'a card closed before a reload stays closed while the close is still being written'() {
    const h = host({ hold: () => true })
    const a = await load(h)
    await a.start()
    await a.close()
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
    assert.equal(a.greetings() + b.greetings(), 1)
  },
  async 'a greeting still being written gives one transcript line across loads, and the same card'() {
    const h = host({ hold: () => true })
    const a = await load(h)
    await a.start()
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), true)
    assert.equal(a.greetings() + b.greetings(), 1)
  },
  ...Object.fromEntries(
    ['before', 'after'].map(order => [
      `a landing an earlier load is still writing lands ${order} the close a later load writes: the card stays closed`,
      async () => {
        const h = host({ hold: key => key === OWN })
        const a = await load(h)
        await a.start()
        await a.land()
        assert.equal(h.held.length, 1)
        const b = await load(h)
        await b.start()
        assert.equal(await b.shown(), true)
        if (order === 'before') h.release(OWN)
        await b.close()
        if (order === 'after') h.release(OWN)
        await settle()
        assert.equal(h.store.get(CLOSED), true)
        assert.equal(typeof h.store.get(OWN)?.landedAt, 'number')
        // A later load, and one in a new process with no `$.state`.
        const c = await load(h)
        await c.start()
        assert.equal(await c.shown(), false)
        h.state.clear()
        const d = await load(h)
        await d.start()
        assert.equal(await d.shown(), false)
      },
    ]),
  ),
  async 'a store read an earlier load is still waiting on, answering after a later load showed the card and it was closed, shows nothing, greets no more, and leaves it closed'() {
    let answer
    let first = true
    const h = host({
      read: key => {
        if (first && key === 'welcome') {
          first = false
          return new Promise(resolve => (answer = () => resolve(undefined)))
        }
        return Promise.resolve(structuredClone(h.store.get(key)))
      },
    })
    const a = await load(h)
    await a.start()
    const b = await load(h)
    await b.start()
    await b.close()
    answer()
    await settle()
    const c = await load(h)
    await c.start()
    assert.equal(a.greetings() + b.greetings() + c.greetings(), 1)
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(h.card()?.closed, true)
    assert.equal(await c.shown(), false)
  },
  async 'a dry-run write an earlier load is still finishing, landing after a later load closed the card, leaves it closed'() {
    const writes = []
    const h = host()
    h.writeFile = (path, text) => new Promise(resolve => writes.push(resolve))
    const a = await load(h)
    await a.start()
    await a.land()
    const b = await load(h)
    await b.start()
    await b.close()
    for (const done of writes.splice(0)) done()
    await settle()
    const c = await load(h)
    await c.start()
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(h.card()?.closed, true)
    assert.equal(await c.shown(), false)
  },
  async 'rows an earlier load lands where the card counts them, after a later load took the card over, count on it'() {
    const writes = []
    const h = host()
    h.writeFile = (path, text) => new Promise(resolve => writes.push(resolve))
    const a = await load(h)
    await a.start()
    await a.land()
    const b = await load(h)
    await b.start()
    assert.ok((await b.texts()).some(t => String(t).startsWith('◌ Waiting for the first rows')))
    for (const done of writes.splice(0)) done()
    await settle()
    assert.equal(h.card()?.sent, 1)
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
    // The earlier load only counts: the card is the later load's to speak for.
    assert.equal(a.toasts(), 0)
  },
  async 'a greeting an earlier load never got written is written by the load that reopens the card'() {
    let greetings = 0
    const h = host({ hold: key => key === `welcome:${PLUGIN_VERSION}` && ++greetings === 1 })
    const a = await load(h)
    await a.start()
    assert.equal(h.store.get(`welcome:${PLUGIN_VERSION}`), undefined)
    const b = await load(h)
    await b.start()
    await settle()
    assert.deepEqual(h.store.get(`welcome:${PLUGIN_VERSION}`), { sessionId: SESSION, previous: null })
    assert.equal(a.greetings() + b.greetings(), 1)
  },
  async 'a close the store has, whose session-state write was refused, wins over the open card the state still has'() {
    const h = host({ refuseState: value => value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.close()
    await settle()
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(h.card()?.closed, false)
    // The state still says open (its writes of a close are refused), but a
    // new load reads the close from the store and never draws the card.
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
  },
  async 'a close the store has, whose session-state write was refused once, is written back to the state by the next load'() {
    let refusals = 0
    const h = host({ refuseState: value => value?.closed === true && ++refusals === 1 })
    const a = await load(h)
    await a.start()
    await a.close()
    await settle()
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(h.card()?.closed, false)
    const b = await load(h)
    await b.start()
    assert.equal(h.card()?.closed, true)
    assert.equal(await b.shown(), false)
  },
  async 'a close whose session-state write never answers is in the store at once, and a new load shows no card'() {
    let holding = false
    const h = host({ holdState: () => holding })
    const a = await load(h)
    await a.start()
    holding = true
    await a.close()
    await settle()
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(await a.shown(), false)
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
  },
  async 'session-state writes that answer late, a close and then an earlier landing, leave the card closed'() {
    let holding = false
    const h = host({ holdState: () => holding })
    const a = await load(h)
    await a.start()
    holding = true
    await a.land()
    await a.close()
    holding = false
    // The close's write answers first, then the landing's.
    const close = h.held.findIndex(w => w.key === 'state' && w.value.closed === true)
    h.held.splice(close, 1)[0].release()
    await settle()
    for (const w of h.held.splice(0)) w.release()
    await settle()
    // The landing's late write did not land over the close.
    assert.equal(h.card()?.closed, true)
    const b = await load(h)
    await b.start()
    assert.equal(h.card()?.closed, true)
    assert.equal(await b.shown(), false)
  },
  async 'a greeting whose store write and session-state write are both still in flight gives one transcript line across loads'() {
    let holding = true
    const h = host({ hold: () => true, holdState: () => holding })
    const a = await load(h)
    const starting = a.start()
    const b = await load(h)
    holding = false
    await b.start()
    for (const w of h.held.filter(w => w.key === 'state').splice(0)) h.held.splice(h.held.indexOf(w), 1)[0].release()
    await starting
    await settle()
    assert.equal(a.greetings() + b.greetings(), 1)
  },
  async 'Got it while the greeting is still being written, its session-state close refused, puts the close in the store at once, and a new load shows no card'() {
    const h = host({ hold: key => key === `welcome:${PLUGIN_VERSION}`, refuseState: value => value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.close()
    // The close is written while the greeting still waits.
    assert.equal(h.store.get(CLOSED), true)
    assert.deepEqual(h.held.map(w => w.key), [`welcome:${PLUGIN_VERSION}`])
    assert.equal(h.card()?.closed, false)
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
    h.release(`welcome:${PLUGIN_VERSION}`)
    await settle()
    assert.equal(await b.shown(), false)
  },
  async 'Got it while a landing is still being written, its session-state close refused, puts the close in the store at once, and a new load shows no card'() {
    const h = host({ hold: key => key === OWN, refuseState: value => value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.land()
    assert.deepEqual(h.held.map(w => w.key), [OWN])
    await a.close()
    assert.equal(h.store.get(CLOSED), true)
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
  },
  async 'a new load draws the card only once its reads of the store have answered, and one whose reads never answer never draws it'() {
    const h = host()
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), true)
    let holding = true
    const reads = heldReads(h, () => holding)
    const b = await load(h)
    await b.start()
    assert.ok(reads.count() > 0)
    assert.equal(await b.shown(), false)
    holding = false
    reads.answer()
    await settle()
    assert.equal(await b.shown(), true)
    holding = true
    const c = await load(h)
    await c.start()
    await settle()
    assert.equal(await c.shown(), false)
  },
  async 'a close the store has, its session-state write refused: a new load draws no card while its read of that close waits, nor after'() {
    const h = host({ refuseState: value => value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.close()
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(h.card()?.closed, false)
    const reads = heldReads(h, key => key === CLOSED)
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
    reads.answer()
    await settle()
    assert.equal(await b.shown(), false)
  },
  async 'a load whose reads answer after a later load took the card over for another destination leaves the card to it, and rows it lands count nowhere'() {
    const A = { deadLetterDir: '/home/dev/a/dead-letter' }
    const B = { deadLetterDir: '/home/dev/b/dead-letter' }
    const h = host()
    const a = await load(h, A)
    await a.start()
    let holding = true
    const reads = heldReads(h, () => holding)
    const b = await load(h, A)
    await b.start()
    holding = false
    const c = await load(h, B)
    await c.start()
    assert.equal(h.card()?.destination, 'dry run: /home/dev/b/dry-run')
    reads.answer()
    await settle()
    assert.equal(h.card()?.destination, 'dry run: /home/dev/b/dry-run')
    assert.equal(await b.shown(), false)
    await b.land()
    assert.equal(h.card()?.sent, 0)
    assert.ok((await c.texts()).some(t => String(t).startsWith('◌ Waiting for the first rows')))
    // The first load, still drawing, claims no landing either.
    await c.land()
    assert.equal(h.card()?.sent, 1)
    assert.ok((await c.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
    assert.ok((await a.texts()).some(t => String(t).startsWith('◌ Waiting for the first rows')))
  },
  async 'rows a new load lands while its reads of the store wait count once, across repeated reloads'() {
    const h = host()
    const a = await load(h)
    await a.start()
    await a.land()
    assert.equal(h.card()?.sent, 1)
    let holding = true
    const reads = heldReads(h, () => holding)
    const b = await load(h)
    await b.start()
    await b.land()
    holding = false
    reads.answer()
    await settle()
    assert.equal(h.card()?.sent, 2)
    const c = await load(h)
    await c.start()
    await c.land()
    assert.equal(h.card()?.sent, 3)
    assert.ok((await c.texts()).some(t => String(t).startsWith('✓ First rows written · 3 so far')))
  },
  async 'a new load that cannot read the store shows no card: it cannot tell whether the card was closed'() {
    const h = host()
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), true)
    h.read = () => Promise.reject(new Error('the store could not be read'))
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
  },
  async 'an earlier load whose read of the card answers after a later load showed the card never takes it over'() {
    const A = { deadLetterDir: '/home/dev/a/dead-letter' }
    const B = { deadLetterDir: '/home/dev/b/dead-letter' }
    const h = host()
    const a = await load(h, A)
    // The earlier load's read of the card waits (its rank it has).
    const waiting = []
    h.stateRead = key => (key === 'welcome' ? new Promise(resolve => waiting.push(resolve)) : undefined)
    const starting = a.start()
    await settle()
    h.stateRead = undefined
    const b = await load(h, B)
    await b.start()
    assert.equal(h.card()?.destination, 'dry run: /home/dev/b/dry-run')
    for (const go of waiting.splice(0)) go()
    await starting
    await settle()
    assert.equal(h.card()?.destination, 'dry run: /home/dev/b/dry-run')
    assert.equal(await a.shown(), false)
    assert.equal(await b.shown(), true)
  },
  async 'a card moved elsewhere and back counts from the load that moved it back: rows of a load before it count there no more, those it counted before included'() {
    const A = { deadLetterDir: '/home/dev/a/dead-letter' }
    const B = { deadLetterDir: '/home/dev/b/dead-letter' }
    const h = host()
    const a = await load(h, A)
    await a.start()
    await a.land()
    assert.equal(h.card()?.sent, 1)
    const b = await load(h, B)
    await b.start()
    const c = await load(h, A)
    await c.start()
    assert.equal(h.card()?.sent, 0)
    // The first load's writer lands one more row there: it ran before the
    // card moved back, so none of its rows count.
    await a.land()
    assert.equal(h.card()?.sent, 0)
    await c.land()
    assert.equal(h.card()?.sent, 1)
    assert.ok((await c.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
  },
  async 'a reload whose takeover write answers after the earlier load\'s Got it (its state close refused) shows no card: the close is read again'() {
    let holding = false
    const h = host({ refuseState: value => value?.closed === true, holdState: value => holding && typeof value === 'object' && value !== null && 'owner' in value })
    const a = await load(h)
    await a.start()
    holding = true
    const b = await load(h)
    await b.start()
    assert.equal(h.held.filter(w => w.key === 'state').length, 1)
    await a.close()
    assert.equal(h.store.get(CLOSED), true)
    holding = false
    for (const w of h.held.filter(w => w.key === 'state')) h.held.splice(h.held.indexOf(w), 1)[0].release()
    await settle()
    assert.equal(await b.shown(), false)
  },
  async 'two loads whose first card writes both wait: the earlier one\'s lands first, and the later load takes the card over, with one greeting'() {
    let holding = true
    const h = host({ holdState: value => holding && typeof value === 'object' && value !== null && 'owner' in value })
    const a = await load(h)
    await a.start()
    const b = await load(h)
    await b.start()
    const writes = h.held.filter(w => w.key === 'state')
    assert.equal(writes.length, 2)
    holding = false
    for (const w of writes) {
      h.held.splice(h.held.indexOf(w), 1)[0].release()
      await settle()
    }
    assert.equal(await b.shown(), true)
    assert.equal(a.greetings() + b.greetings(), 1)
  },
  async 'a prompt after rows landed, with session-state closes refused, closes the card here and in the store, and a new load shows none'() {
    const h = host({ refuseState: value => value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.land()
    await a.prompt()
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(await a.shown(), false)
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
  },
  async 'loads in the same millisecond, or with the clock set back, rank by when they started: the earlier one never takes the card from the later'() {
    const realNow = Date.now
    try {
      for (const clock of [() => 1_000_000, (n => () => (n -= 60_000))(10_000_000)]) {
        Date.now = clock
        const A = { deadLetterDir: '/home/dev/a/dead-letter' }
        const B = { deadLetterDir: '/home/dev/b/dead-letter' }
        const h = host()
        const a = await load(h, A)
        await a.start()
        let holding = true
        const reads = heldReads(h, () => holding)
        const b = await load(h, A)
        await b.start()
        holding = false
        const c = await load(h, B)
        await c.start()
        reads.answer()
        await settle()
        assert.equal(h.card()?.destination, 'dry run: /home/dev/b/dry-run')
        assert.equal(await c.shown(), true)
        assert.equal(await b.shown(), false)
      }
    } finally {
      Date.now = realNow
    }
  },
  ...Object.fromEntries(
    ['write', 'read'].map(step => [
      `a load replaced while its rank ${step} is still out takes nothing over: the later load keeps its destination, counts its rows, and a prompt closes the card`,
      async () => {
        const A = { deadLetterDir: '/home/dev/a/dead-letter' }
        const B = { deadLetterDir: '/home/dev/b/dead-letter' }
        // B's cancelled wait never answers, or is refused.
        for (const refuseWaits of [false, true]) {
          let holding = false
          const h = host({ holdState: (value, key) => holding && key === 'loads' })
          h.refuseWaits = refuseWaits
          const a = await load(h, A)
          await a.start()
          const reads = []
          if (step === 'write') holding = true
          else h.stateRead = key => (key === 'loads' ? new Promise(resolve => reads.push(resolve)) : undefined)
          const b = await load(h, A)
          await b.start()
          assert.equal(step === 'write' ? h.held.length : reads.length, 1)
          holding = false
          h.stateRead = undefined
          const c = await load(h, B)
          await c.start()
          // B's rank comes back now, above C's: B is no longer the load the host runs.
          if (step === 'write') h.release('state')
          else reads[0]()
          await settle()
          assert.equal(h.state.get('loads').value.count, 3)
          assert.equal(h.card().destination, 'dry run: /home/dev/b/dry-run')
          assert.equal(await b.shown(), false)
          await c.land()
          assert.equal(h.card().sent, 1)
          assert.equal(h.card().landedTo, 'dry run: /home/dev/b/dry-run')
          assert.ok((await c.texts()).some(text => text.startsWith('✓ First rows written · 1 so far')))
          await c.prompt()
          assert.equal(await c.shown(), false)
          assert.equal(h.store.get(CLOSED), true)
          assert.equal(a.greetings() + b.greetings() + c.greetings(), 1)
        }
      },
    ]),
  ),
  async 'a close an earlier load writes late, its session-state close refused, hides the card a later load took over once it lands, with no reload and no press'() {
    const h = host({ hold: key => key === CLOSED, refuseState: value => value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.close()
    const b = await load(h)
    await b.start()
    // Not in the store yet: nothing says the card was closed.
    assert.equal(await b.shown(), true)
    h.release(CLOSED)
    await settle()
    const asked = b.redraws()
    // A draw reads the close again, and asks for the next, which has no card.
    await b.shown()
    await settle()
    assert.ok(b.redraws() > asked)
    assert.equal(await b.shown(), false)
    assert.equal(h.card().closed, false)
  },
  ...Object.fromEntries(
    ['answers', 'never answers'].map(mode => [
      `a close written while the greeting's write was lost: a resumed session, its state gone, shows no card and greets no more (its read of the close ${mode})`,
      async () => {
        const h = host({ hold: key => key === `welcome:${PLUGIN_VERSION}` })
        const a = await load(h)
        await a.start()
        await a.close()
        assert.equal(h.store.get(CLOSED), true)
        assert.equal(h.store.get(`welcome:${PLUGIN_VERSION}`), undefined)
        h.state.clear()
        heldReads(h, key => mode === 'never answers' && key === CLOSED)
        const b = await load(h)
        await b.start()
        assert.equal(await b.shown(), false)
        await settle()
        assert.equal(await b.shown(), false)
        assert.equal(a.greetings() + b.greetings(), 1)
      },
    ]),
  ),
  async 'a Got it of the version before an update, its session-state close written late, never closes the card of the version after it'() {
    let holding = false
    const h = host({ holdState: value => holding && value?.closed === true })
    const a = await load(h)
    await a.start()
    holding = true
    await a.close()
    assert.equal(h.store.get(CLOSED), true)
    assert.equal(h.held.length, 1)
    holding = false
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.equal(h.card().version, NEXT)
    assert.equal(await b.shown(), true)
    h.release('state')
    await settle()
    assert.equal(h.card().closed, false)
    assert.equal(await b.shown(), true)
    assert.equal(h.store.get(`welcome:${NEXT}:${SESSION}:closed`), undefined)
  },
  async 'a takeover of the version before an update, its reads answering late, never puts its stored close on the card of the version after it'() {
    let refusing = true
    const h = host({ refuseState: value => refusing && value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.close()
    refusing = false
    let holding = true
    const reads = heldReads(h, () => holding)
    const b = await load(h)
    await b.start()
    holding = false
    const c = await load(h, {}, NEXT)
    await c.start()
    assert.equal(await c.shown(), true)
    reads.answer()
    await settle()
    assert.equal(h.card().version, NEXT)
    assert.equal(h.card().closed, false)
    assert.equal(await c.shown(), true)
  },
  ...Object.fromEntries(
    [NEXT, '0.1.0'].map(landsFirst => [
      `of the first card writes of the versions before and after an update, ${landsFirst === NEXT ? 'the later one\'s landing first' : 'the earlier one\'s landing first'}, the later version's card shows, with its own greeting`,
      async () => {
        let holding = true
        const h = host({ holdState: value => holding && typeof value === 'object' && value !== null && 'owner' in value })
        const a = await load(h)
        await a.start()
        const b = await load(h, {}, NEXT)
        await b.start()
        holding = false
        const release = version => {
          const w = h.held.find(x => x.key === 'state' && x.value?.version === version)
          h.held.splice(h.held.indexOf(w), 1)
          w.release()
        }
        release(landsFirst)
        await settle()
        release(landsFirst === NEXT ? '0.1.0' : NEXT)
        await settle()
        assert.equal(h.card().version, NEXT)
        assert.equal(await b.shown(), true)
        assert.equal(b.greetings(), 1)
        assert.equal(h.store.get(`welcome:${NEXT}`)?.sessionId, SESSION)
      },
    ]),
  ),
  async 'a prompt in a load of the version before an update never closes the card of the version after it'() {
    const h = host()
    const a = await load(h)
    await a.start()
    const b = await load(h, {}, NEXT)
    await b.start()
    await b.land()
    assert.equal(typeof h.card().landedAt, 'number')
    await a.prompt()
    assert.equal(h.card().version, NEXT)
    assert.equal(h.card().closed, false)
    assert.equal(await b.shown(), true)
    assert.equal(h.store.get(CLOSED), undefined)
  },
  async 'a run whose read of the session state answers after a later load showed another version\'s card leaves that card alone, also where reads see what stands at once'() {
    const h = host({ liveReads: true })
    let answer
    let first = true
    h.stateRead = key => (key === 'welcome' && first ? ((first = false), new Promise(resolve => (answer = resolve))) : undefined)
    const a = await load(h)
    await a.start()
    h.stateRead = undefined
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.equal(h.card().version, NEXT)
    answer()
    await settle()
    assert.equal(h.card().version, NEXT)
    assert.equal(await b.shown(), true)
    assert.equal(a.greetings() + b.greetings(), 1)
  },
  async 'a read of the close that never answers stops no read of it ten seconds on'() {
    const realNow = Date.now
    try {
      let now = 1_000_000
      Date.now = () => now
      const h = host()
      const a = await load(h)
      await a.start()
      const reads = heldReads(h, key => key === CLOSED)
      assert.equal(await a.shown(), true)
      assert.equal(await a.shown(), true)
      assert.equal(reads.count(), 1)
      now += 11_000
      assert.equal(await a.shown(), true)
      assert.equal(reads.count(), 2)
    } finally {
      Date.now = realNow
    }
  },
  async 'the stand-in reads one moment an event, which a missed conditional write brings up to date whole, as the host\'s'() {
    const h = host()
    const a = await load(h)
    const ref = key => ({ plugin: 'bigquery-agent-analytics-mod', key })
    let go
    const gate = new Promise(resolve => (go = resolve))
    const first = dispatch.run({ moment: null }, async () => {
      const x = await a.$.state.get(ref('loads'))
      await a.$.state.get(ref('welcome'))
      await gate
      const missed = await a.$.state.set(ref('loads'), 9, { ifVersion: x.version })
      return { missed, x: await a.$.state.get(ref('loads')), y: await a.$.state.get(ref('welcome')) }
    })
    const wrote = await dispatch.run({ moment: null }, async () => [await a.$.state.set(ref('loads'), 1), await a.$.state.set(ref('welcome'), { probe: true })])
    go()
    const seen = await first
    assert.equal(seen.missed.isSet, false)
    assert.deepEqual([seen.x.value, seen.x.version, seen.y.value, seen.y.version], [1, wrote[0].version, { probe: true }, wrote[1].version])
  },
  async 'the stand-in brings the whole moment up to date after a conditional write of its own lands too, as the host\'s'() {
    const h = host()
    const a = await load(h)
    const ref = key => ({ plugin: 'bigquery-agent-analytics-mod', key })
    let go
    const gate = new Promise(resolve => (go = resolve))
    const first = dispatch.run({ moment: null }, async () => {
      const x = await a.$.state.get(ref('loads'))
      await a.$.state.get(ref('welcome'))
      await gate
      const kept = await a.$.state.get(ref('welcome'))
      const landed = await a.$.state.set(ref('loads'), 5, { ifVersion: x.version })
      return { kept, landed, y: await a.$.state.get(ref('welcome')) }
    })
    const wrote = await dispatch.run({ moment: null }, async () => a.$.state.set(ref('welcome'), { probe: true }))
    go()
    const seen = await first
    assert.equal(seen.landed.isSet, true)
    assert.deepEqual([seen.kept.value, seen.y.value, seen.y.version], [undefined, { probe: true }, wrote.version])
  },
  async 'rows the version before an update lands after the updated version showed its card never count there: the card counts the updated version\'s rows'() {
    let holding = false
    const writes = []
    const h = host()
    h.writeFile = () => (holding ? new Promise(resolve => writes.push(resolve)) : Promise.resolve())
    const a = await load(h)
    await a.start()
    await a.land()
    assert.equal(h.card()?.sent, 1)
    // Its next write waits; the updated version shows its card meanwhile.
    holding = true
    await a.land()
    holding = false
    assert.equal(writes.length, 1)
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.equal(h.card().version, NEXT)
    assert.equal(h.card().sent, 0)
    writes.splice(0).forEach(go => go())
    await settle()
    assert.deepEqual([h.card().sent, h.card().landedAt], [0, null])
    await b.land()
    assert.equal(h.card().sent, 1)
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
  },
  async 'rows the version before an update lands after its card was closed, before the update or after it, never count on the updated version\'s card'() {
    let holding = false
    const writes = []
    const h = host()
    h.writeFile = () => (holding ? new Promise(resolve => writes.push(resolve)) : Promise.resolve())
    const a = await load(h)
    await a.start()
    await a.land()
    assert.equal(h.card().sent, 1)
    await a.close()
    // A row lands while the card is closed; the next one's write waits
    // across the update.
    await a.land()
    holding = true
    await a.land()
    holding = false
    assert.equal(writes.length, 1)
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.deepEqual([h.card().version, h.card().sent], [NEXT, 0])
    writes.splice(0).forEach(go => go())
    await settle()
    assert.equal(h.card().sent, 0)
    await b.land()
    assert.equal(h.card().sent, 1)
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
  },
  async 'rows landed while the session showed no card (another session greeted this version) never count on the updated version\'s card, nor rows that land after it opened'() {
    let holding = false
    const writes = []
    const h = host()
    h.store.set(`welcome:${PLUGIN_VERSION}`, { version: PLUGIN_VERSION, sessionId: 'another-session', previous: null })
    h.writeFile = () => (holding ? new Promise(resolve => writes.push(resolve)) : Promise.resolve())
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), false)
    await a.land()
    await a.land()
    holding = true
    await a.land()
    holding = false
    assert.equal(writes.length, 1)
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.deepEqual([h.card().version, h.card().sent], [NEXT, 0])
    writes.splice(0).forEach(go => go())
    await settle()
    assert.equal(h.card().sent, 0)
    await b.land()
    assert.equal(h.card().sent, 1)
  },
  async 'a delivery of the version before an update that lands after the updated version\'s card opened never counts on it: the card counts from the run that opened it'() {
    const h = host()
    const a = await load(h)
    await a.start()
    await a.land()
    await a.close()
    const writes = []
    h.writeFile = () => new Promise(resolve => writes.push(resolve))
    await a.land()
    const b = await load(h, {}, NEXT)
    await b.start()
    writes.shift()()
    await settle()
    assert.deepEqual([h.card().sent, h.card().landedAt], [0, null])
    h.writeFile = undefined
    await b.land()
    assert.equal(h.card().sent, 1)
  },
  async 'a delivery that landed before an update, recorded only after the updated version\'s card opened, never counts on it'() {
    let holding = false
    // The write that records the second row's total waits.
    const h = host({ holdState: (value, key) => holding && key === 'delivered' && recordsRows(value, rows => rows >= 2) })
    const a = await load(h)
    await a.start()
    await a.land()
    await a.close()
    holding = true
    await a.land()
    assert.equal(h.held.length, 1)
    holding = false
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.equal(h.card().sent, 0)
    h.release('state')
    await settle()
    assert.deepEqual([h.card().sent, h.card().landedAt], [0, null])
  },
  async 'rows the version before an update lands while the updated version reads the store, and after, never count on its card'() {
    const h = host()
    h.store.set(`welcome:${PLUGIN_VERSION}`, { version: PLUGIN_VERSION, sessionId: 'another-session', previous: null })
    const a = await load(h)
    await a.start()
    await a.land()
    assert.equal(await a.shown(), false)
    // 501 rows queued: the first batch's write waits, then the last row's.
    const writes = []
    h.writeFile = () => new Promise(resolve => writes.push(resolve))
    await a.queue(501)
    await a.flush()
    assert.equal(writes.length, 1)
    let holding = true
    const reads = heldReads(h, () => holding)
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.ok(reads.count() > 0)
    writes.shift()()
    await settle()
    assert.equal(writes.length, 1)
    assert.ok(recordsRows(h.state.get('delivered').value, rows => rows === 501))
    holding = false
    reads.answer()
    await settle()
    assert.equal(h.card().sent, 0)
    writes.shift()()
    await settle()
    assert.ok(recordsRows(h.state.get('delivered').value, rows => rows === 502))
    assert.equal(h.card().sent, 0)
    h.writeFile = undefined
    await b.land()
    assert.equal(h.card().sent, 1)
  },
  async 'an earlier load\'s rows recorded late across an update, a later total of its recorded first, never count on the updated card; the updated version\'s own row counts once'() {
    let holding = false
    // The write that records the 500 rows' total (501 with the first row) waits.
    const h = host({ holdState: (value, key) => holding && key === 'delivered' && recordsRows(value, rows => rows === 501) })
    const a = await load(h)
    await a.start()
    await a.land()
    await a.close()
    // One flush sends 500 rows now, then waits on the last row's write.
    const files = []
    let calls = 0
    h.writeFile = () => (++calls === 1 ? Promise.resolve() : new Promise(resolve => files.push(resolve)))
    holding = true
    await a.queue(501)
    await a.flush()
    holding = false
    assert.deepEqual([h.held.length, files.length], [1, 1])
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.equal(h.card().sent, 0)
    files.shift()()
    await settle()
    assert.ok(recordsRows(h.state.get('delivered').value, rows => rows === 502))
    assert.equal(h.card().sent, 0)
    h.release('state')
    await settle()
    assert.ok(recordsRows(h.state.get('delivered').value, rows => rows === 502))
    assert.deepEqual([h.card().sent, h.card().landedAt], [0, null])
    h.writeFile = undefined
    await b.land()
    assert.equal(h.card().sent, 1)
  },
  async 'a row the updated version lands while its card\'s first write waits counts once that write lands, with one toast; one the version before lands then never counts'() {
    let holding = false
    const h = host({ holdState: (value, key) => holding && key === 'welcome' && value?.version === NEXT })
    const a = await load(h)
    await a.start()
    await a.land()
    await a.close()
    const files = []
    h.writeFile = () => new Promise(resolve => files.push(resolve))
    await a.land()
    holding = true
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.equal(h.held.length, 1)
    // Both land while the card's write waits.
    files.shift()()
    await settle()
    h.writeFile = undefined
    await b.land()
    assert.equal(h.card().version, PLUGIN_VERSION)
    holding = false
    h.release('state')
    await settle()
    assert.deepEqual([h.card().version, h.card().sent], [NEXT, 1])
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
    assert.equal(b.toasts(), 1)
  },
  async 'the updated version\'s rows count once whatever order its records land in: a later total recorded first stands, and the earlier one changes nothing'() {
    let holding = false
    const h = host({ holdState: (value, key) => holding && key === 'delivered' && recordsRows(value, rows => rows === 500) })
    const a = await load(h)
    await a.start()
    await a.land()
    await a.close()
    const b = await load(h, {}, NEXT)
    await b.start()
    // One flush of the updated version: 500 rows, their record held, then the last row.
    holding = true
    await b.queue(501)
    await b.flush()
    holding = false
    assert.equal(h.held.length, 1)
    assert.equal(h.card().sent, 501)
    h.release('state')
    await settle()
    assert.equal(h.card().sent, 501)
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 501 so far')))
  },
  async 'an earlier load whose rank comes back after a later load took one counts none of its rows, also those landing after the later load\'s card opened'() {
    let holding = false
    // The first load's write of the session's count of runs waits.
    const h = host({ holdState: (value, key) => holding && key === 'loads' })
    holding = true
    const a = await load(h)
    await a.start()
    holding = false
    assert.equal(h.held.length, 1)
    const b = await load(h)
    await b.start()
    assert.equal(h.state.get('loads').value.count, 1)
    h.release('state')
    await settle()
    assert.equal(h.state.get('loads').value.count, 2)
    await a.land()
    assert.equal(await a.shown(), false)
    assert.deepEqual([h.card().sent, h.card().landedAt], [0, null])
    await b.land()
    assert.equal(h.card().sent, 1)
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
  },
  async 'a row of the version before an update, landing while the updated version counts its own first row, leaves that landing, and its toast, to the updated version'() {
    const h = host()
    const a = await load(h)
    await a.start()
    await a.land()
    await a.close()
    const files = []
    h.writeFile = () => new Promise(resolve => files.push(resolve))
    await a.land()
    const b = await load(h, {}, NEXT)
    await b.start()
    // The first load's toast for its own card's landing.
    assert.equal(a.toasts(), 1)
    // The updated version's first row lands; its read of the card waits.
    let holding = true
    let release = () => undefined
    const waiting = new Promise(resolve => (release = resolve))
    h.stateRead = key => (holding && key === 'welcome' ? waiting : undefined)
    h.writeFile = undefined
    await b.land()
    holding = false
    // The version before lands its row meanwhile.
    files.shift()()
    await settle()
    release()
    await settle()
    assert.deepEqual([h.card().sent, b.toasts(), a.toasts()], [1, 1, 1])
  },
  async 'rows a reload lands where it sends them while its reads of the store wait count once it takes the card over and moves it there'() {
    const A = { deadLetterDir: '/home/dev/a/dead-letter' }
    const B = { deadLetterDir: '/home/dev/b/dead-letter' }
    const h = host()
    const a = await load(h, A)
    await a.start()
    await a.land()
    assert.equal(h.card()?.sent, 1)
    let holding = true
    const reads = heldReads(h, () => holding)
    const b = await load(h, B)
    await b.start()
    await b.land()
    assert.deepEqual([h.card().destination, h.card().sent], ['dry run: /home/dev/a/dry-run', 1])
    holding = false
    reads.answer()
    await settle()
    assert.deepEqual([h.card().destination, h.card().sent], ['dry run: /home/dev/b/dry-run', 1])
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
  },
  async 'rows a load lands before and after the session\'s state is reset (/clear, /resume or /branch, which fire no session start) never count on the card an update opens after it: the count\'s new lifetime starts over'() {
    const h = host()
    const a = await load(h)
    await a.start()
    await a.land()
    await a.close()
    assert.equal(a.toasts(), 1)
    // The reset: every value of the session's state back to its default. The
    // load goes on sending.
    h.state.clear()
    await a.land()
    await a.land()
    assert.ok(recordsRows(h.state.get('delivered').value, rows => rows === 3))
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.deepEqual([h.card().version, h.card().sent, h.card().landedAt], [NEXT, 0, null])
    assert.equal(h.state.get('loads').value.count, 1)
    await a.land()
    assert.deepEqual([h.card().sent, h.card().landedAt], [0, null])
    await b.land()
    assert.equal(h.card().sent, 1)
    assert.ok((await b.texts()).some(t => String(t).startsWith('✓ First rows written · 1 so far')))
    assert.deepEqual([a.toasts(), b.toasts()], [1, 1])
  },
  ...Object.fromEntries(
    ['after the reset', 'before the reset'].map(when => [
      `a record of a load's rows, its write held from ${when} until after an update's card opened, never counts on that card: it is of the lifetime before the reset`,
      async () => {
        let holding = false
        const h = host({ holdState: (value, key) => holding && key === 'delivered' })
        const a = await load(h)
        await a.start()
        await a.land()
        await a.close()
        if (when === 'before the reset') holding = true
        else h.state.clear()
        await a.land()
        holding = false
        if (when === 'before the reset') {
          assert.equal(h.held.length, 1)
          h.state.clear()
        } else {
          holding = true
          await a.land()
          holding = false
          assert.equal(h.held.length, 1)
        }
        const b = await load(h, {}, NEXT)
        await b.start()
        assert.deepEqual([h.card().version, h.card().sent, h.card().landedAt], [NEXT, 0, null])
        h.release('state')
        await settle()
        assert.ok(recordsRows(h.state.get('delivered').value, rows => rows >= 2))
        assert.deepEqual([h.card().sent, h.card().landedAt], [0, null])
        await b.land()
        assert.equal(h.card().sent, 1)
      },
    ]),
  ),
  ...Object.fromEntries(
    [false, true].flatMap(liveReads => ['removed', 'written back to defaults'].map(reset => [liveReads, reset])).map(([liveReads, reset]) => [
      `a run whose card the session's state was reset under (/clear, values ${reset}${liveReads ? ', reads that see what stands' : ''}) shows none after it, and the next update shows its card`,
      async () => {
        // Every write of the earlier version's card once the state is reset.
        let afterReset = false
        let staleWrites = 0
        const h = host({ liveReads, holdState: (value, key) => (afterReset && key === 'welcome' && value?.version === NEXT && (staleWrites += 1), false) })
        h.store.set(`welcome:${PLUGIN_VERSION}`, { version: PLUGIN_VERSION, sessionId: 'another-session', previous: null })
        const a = await load(h)
        await a.start()
        assert.equal(await a.shown(), false)
        let holding = true
        const reads = heldReads(h, () => holding)
        const b = await load(h, {}, NEXT)
        await b.start()
        assert.ok(reads.count() > 0)
        // The reset, while that run reads the store.
        if (reset === 'removed') h.state.clear()
        else for (const key of ['loads', 'welcome', 'delivered']) await h.stateSet({ key }, undefined)
        afterReset = true
        h.sessionId = 'after-clear'
        await b.fire('classic.SessionStart', { source: 'clear', session_id: h.sessionId, model: 'example-model', cwd: '/work' })
        holding = false
        reads.answer()
        await settle()
        assert.deepEqual([h.card() ?? null, staleWrites], [null, 0])
        assert.equal(await b.shown(), false)
        const c = await load(h, {}, '0.3.0')
        await c.start()
        assert.deepEqual([h.card().version, await c.shown(), c.greetings()], ['0.3.0', true, 1])
      },
    ]),
  ),
  async 'a card a run writes as the session\'s state is reset goes again: the next update shows its own'() {
    let holding = false
    const h = host({ holdState: (value, key) => holding && key === 'welcome' && value?.version === NEXT })
    // No card before: the write lands at the version a reset leaves.
    h.store.set(`welcome:${PLUGIN_VERSION}`, { version: PLUGIN_VERSION, sessionId: 'another-session', previous: null })
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), false)
    holding = true
    const b = await load(h, {}, NEXT)
    await b.start()
    assert.equal(h.held.length, 1)
    holding = false
    h.state.clear()
    h.sessionId = 'after-clear'
    await b.fire('classic.SessionStart', { source: 'clear', session_id: h.sessionId, model: 'example-model', cwd: '/work' })
    h.release('state')
    await settle()
    assert.equal(h.card() ?? null, null)
    assert.equal(b.greetings(), 0)
    const c = await load(h, {}, '0.3.0')
    await c.start()
    assert.deepEqual([h.card().version, await c.shown()], ['0.3.0', true])
  },
  ...Object.fromEntries(
    ['first read', 'read after its write missed'].map(when => [
      `a card of the lifetime before a reset, of a higher rank, found at the ${when}, keeps no update from showing its card`,
      async () => {
        let holding = false
        const h = host({ holdState: (value, key) => holding && key === 'welcome' && value?.version === '0.3.0' })
        const stale = { version: NEXT, previous: PLUGIN_VERSION, sessionId: 'before-clear', landedAt: null, landedTo: null, closed: false, sent: 0, destination: 'dry run: /home/dev/.bqaa-mod/dry-run', owner: 'gone:1', rank: 5, since: 5, lifetime: 'before' }
        if (when === 'first read') h.state.set('welcome', { value: stale, version: 1 })
        else holding = true
        const c = await load(h, {}, '0.3.0')
        await c.start()
        if (when !== 'first read') {
          assert.equal(h.held.length, 1)
          holding = false
          h.state.set('welcome', { value: stale, version: 1000 })
          h.release('state')
          await settle()
        }
        assert.deepEqual([h.card().version, h.card().rank, await c.shown()], ['0.3.0', 1, true])
      },
    ]),
  ),
  async 'this session\'s card of the lifetime before a reset, of a higher rank, is taken over by the next run, which counts from itself'() {
    const h = host()
    h.store.set(`welcome:${PLUGIN_VERSION}`, { version: PLUGIN_VERSION, sessionId: SESSION, previous: null })
    h.state.set('welcome', { value: { version: PLUGIN_VERSION, previous: null, sessionId: SESSION, landedAt: null, landedTo: null, closed: false, sent: 0, destination: 'dry run: /home/dev/.bqaa-mod/dry-run', owner: 'gone:1', rank: 5, since: 5, lifetime: 'before' }, version: 1 })
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), true)
    assert.deepEqual([h.card().rank, h.card().since, h.card().lifetime === h.state.get('loads').value.lifetime], [1, 1, true])
    await a.land()
    assert.equal(h.card().sent, 1)
  },
  async 'a load of the lifetime before a reset, its reads answering after the next update showed its card, leaves that card as it is'() {
    const h = host()
    let holding = true
    const reads = heldReads(h, () => holding)
    const a = await load(h)
    await a.start()
    assert.ok(reads.count() > 0)
    // The reset, which the earlier load never hears of.
    h.state.clear()
    holding = false
    const b = await load(h, {}, NEXT)
    await b.start()
    await b.land()
    assert.deepEqual([h.card().version, h.card().sent], [NEXT, 1])
    reads.answer()
    await settle()
    assert.deepEqual([h.card().version, h.card().sent], [NEXT, 1])
  },
  async 'a load whose reads answer after the session\'s state was reset, which it never heard of, shows no card: one it wrote goes again'() {
    const h = host()
    let holding = true
    const reads = heldReads(h, () => holding)
    const a = await load(h)
    await a.start()
    assert.ok(reads.count() > 0)
    h.state.clear()
    holding = false
    reads.answer()
    await settle()
    assert.deepEqual([await a.shown(), h.card() ?? null, a.greetings()], [false, null, 0])
  },
  async 'a takeover of the lifetime before a reset, its reads answering after the next run showed this session\'s card again, leaves that card to it'() {
    const h = host()
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), true)
    let holding = true
    const reads = heldReads(h, () => holding)
    const b = await load(h)
    await b.start()
    assert.ok(reads.count() > 0)
    // The reset, which the load taking the card over never hears of.
    h.state.clear()
    holding = false
    const c = await load(h)
    await c.start()
    assert.equal(await c.shown(), true)
    const shown = h.card()
    reads.answer()
    await settle()
    assert.deepEqual([h.card().owner, h.card().lifetime, h.card().since], [shown.owner, shown.lifetime, shown.since])
    await c.land()
    assert.equal(h.card().sent, 1)
  },
  ...Object.fromEntries(
    [false, true].flatMap(liveReads => [false, true].map(dryRun => [liveReads, dryRun])).map(([liveReads, dryRun]) => [
      `a card a run wrote as the session's state was reset, its own rows counted on it while it is taken back, goes all the same (${liveReads ? 'reads that see what stands' : 'one moment an event'}, ${dryRun ? 'a dry run' : 'BigQuery'}): the next load counts from itself`,
      async () => {
        let holdCard = true
        let holdBack = false
        const h = host({ liveReads, holdState: (value, key) => key === 'welcome' && ((holdCard && value?.version === PLUGIN_VERSION) || (holdBack && value === null)) })
        const wire = l => {
          l.$.env.get = async name => ({ HOME: '/home/dev', BQAA_ACCESS_TOKEN: 'synthetic-token-for-offline-test' })[name]
          l.$.http.fetch = async () => ({ status: 200, text: '{"kind":"bigquery#tableDataInsertAllResponse"}' })
          return l
        }
        const a = wire(await load(h, { dryRun }))
        await a.start()
        assert.equal(h.held.length, 1)
        // /clear, then /resume of this session: each resets the state, and the load goes on.
        h.state.clear()
        h.sessionId = 'after-clear'
        await a.fire('classic.SessionStart', { source: 'clear', session_id: h.sessionId, model: 'example-model', cwd: '/work' })
        h.state.clear()
        h.sessionId = SESSION
        await a.fire('classic.SessionStart', { source: 'resume', session_id: h.sessionId, model: 'example-model', cwd: '/work' })
        holdCard = false
        holdBack = true
        h.release('state')
        await settle()
        assert.equal(h.held[0]?.value, null)
        // The rows of the clear and the resume land while the card waits to go.
        await a.flush()
        assert.equal(h.card().sent, 2)
        holdBack = false
        h.release('state')
        await settle()
        assert.deepEqual([h.card() ?? null, a.greetings(), await a.shown()], [null, 0, false])
        const b = wire(await load(h, { dryRun }))
        await b.start()
        assert.deepEqual([await b.shown(), h.card().sent, h.card().landedAt], [true, 0, null])
        await b.prompt()
        assert.equal(h.card().closed, false)
      },
    ]),
  ),
  async 'this session\'s card of the lifetime before a reset, landed, is taken over with no count and no landing: the next prompt leaves it until this lifetime lands'() {
    const h = host()
    h.store.set(`welcome:${PLUGIN_VERSION}`, { version: PLUGIN_VERSION, sessionId: SESSION, previous: null })
    h.state.set('welcome', { value: { version: PLUGIN_VERSION, previous: null, sessionId: SESSION, landedAt: 1, landedTo: 'dry run: /home/dev/.bqaa-mod/dry-run', closed: false, sent: 7, destination: 'dry run: /home/dev/.bqaa-mod/dry-run', owner: 'gone:1', rank: 1, since: 1, lifetime: 'before' }, version: 1 })
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), true)
    assert.deepEqual([h.card().sent, h.card().landedAt, h.card().landedTo], [0, null, null])
    await a.prompt()
    assert.equal(h.card().closed, false)
    // The prompt's row and the next one land: this lifetime's own.
    await a.land()
    assert.deepEqual([h.card().sent, typeof h.card().landedAt], [2, 'number'])
  },
  async 'a landing an earlier load never got written is written by the load that takes the card over'() {
    let holding = false
    const h = host({ hold: key => holding && key === OWN })
    const a = await load(h)
    await a.start()
    holding = true
    await a.land()
    holding = false
    assert.deepEqual([h.held.length, h.store.get(OWN)], [1, undefined])
    const b = await load(h)
    await b.start()
    await settle()
    assert.equal(h.store.get(OWN)?.landedAt, h.card().landedAt)
    assert.equal(typeof h.card().landedAt, 'number')
  },
  async 'a store close whose write was refused goes again at the next flush: a reload, its state close refused too, shows no card'() {
    let refusals = 0
    const h = host({ refuseStore: key => key === CLOSED && refusals++ === 0, refuseState: value => value?.closed === true })
    const a = await load(h)
    await a.start()
    await a.close()
    assert.deepEqual([h.store.get(CLOSED), h.card().closed], [undefined, false])
    await a.flush()
    assert.equal(h.store.get(CLOSED), true)
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
  },
  async 'where the session id cannot be read no card shows and no one is greeted, after a reload too: Got it could keep no close'() {
    const h = host({ hold: key => key.startsWith('welcome') })
    h.noSessionId = true
    const a = await load(h)
    await a.start()
    assert.equal(await a.shown(), false)
    const b = await load(h)
    await b.start()
    assert.equal(await b.shown(), false)
    assert.equal(a.greetings() + b.greetings(), 0)
    assert.deepEqual([h.card(), h.held.length, [...h.store.keys()].filter(key => key.startsWith('welcome')).length], [undefined, 0, 0])
  },
  async 'a read of the close that answers after the next one began leaves that one alone: no third read while it waits'() {
    const realNow = Date.now
    try {
      let now = 1_000_000
      Date.now = () => now
      const h = host()
      const a = await load(h)
      await a.start()
      const waiting = []
      h.read = key => (key === CLOSED ? new Promise(resolve => waiting.push(() => resolve(structuredClone(h.store.get(key))))) : Promise.resolve(structuredClone(h.store.get(key))))
      assert.equal(await a.shown(), true)
      now += 11_000
      assert.equal(await a.shown(), true)
      assert.equal(waiting.length, 2)
      waiting[0]()
      await settle()
      assert.equal(await a.shown(), true)
      assert.equal(waiting.length, 2)
    } finally {
      Date.now = realNow
    }
  },
  async 'draws of the card while its read of the close waits start no other read of it'() {
    const h = host()
    const a = await load(h)
    await a.start()
    let holding = true
    const reads = heldReads(h, key => holding && key === CLOSED)
    for (let i = 0; i < 3; i += 1) assert.equal(await a.shown(), true)
    assert.equal(reads.count(), 1)
    holding = false
    reads.answer()
    await settle()
    assert.equal(await a.shown(), true)
  },
}

let failed = 0
for (const [name, run] of Object.entries(tests)) {
  try {
    await run()
    console.log(`ok - ${name}`)
  } catch (err) {
    failed += 1
    console.log(`not ok - ${name}\n  ${err?.message ?? err}`)
  }
}
console.log(`${Object.keys(tests).length - failed} passed, ${failed} failed`)
process.exitCode = failed === 0 ? 0 : 1
