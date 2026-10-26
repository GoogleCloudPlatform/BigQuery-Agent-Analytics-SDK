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

// The card the band above the prompt shows in the first session after the
// mod is installed, or updated to a new version: that the mod is on, what it
// records and where to, and once rows land, that they did. Nothing else on
// screen says so while rows are written without trouble. No `$` here, like
// status.ts: register.ts reads the records from `$.state` and `$.store`,
// hands CardRecords its writes, and draws the card.

import type { WelcomeState } from '../../types'
import { type Config, PLUGIN_VERSION, canDeliver, tableRef } from './config.ts'

// The records the card keeps in `$.store`, each written by the session that
// showed it, and only under keys of that version and session, so no write of
// another version or session, however late it answers, lands on them:
// - `welcome:<version>`: that this version was greeted, by which session, and
//   the version before it; written once, when the card first shows, so no
//   later session shows it. The versions greeted, listed, give the next
//   version's card the one it replaced;
// - `welcome:<version>:<session>`: when rows first landed while the card
//   showed, and where (`destinationKey`);
// - `welcome:<version>:<session>:closed`: that it was closed, a key no other
//   write touches, so no write that answers late undoes a close.
// `welcome` alone is read, never written: earlier versions kept the version
// greeted there.
export const WELCOME_KEY = 'welcome'
export const greetedKey = (version: string): string => `${WELCOME_KEY}:${version}`
export const cardKey = (version: string, sessionId: string): string => `${WELCOME_KEY}:${version}:${sessionId}`
export const closedKey = (version: string, sessionId: string): string => `${cardKey(version, sessionId)}:closed`

// The card: the version greeted, the one greeted before it (null on a fresh
// install), the session that showed it, when rows first landed and where,
// whether it was closed, and how many rows landed there while it showed. The
// session keeps it in `$.state`, so a reload (/reload-plugins, an option
// changed in /config) shows it again as it was, unless it was closed,
// waiting again if rows now go elsewhere.
export type Welcome = WelcomeState

export type Tone = 'success' | 'suggestion' | 'warning' | 'inactive'

// What the card says: the title line, after it the version dim, and the
// lines under it.
export type WelcomeCard = {
  tone: Tone
  glyph: string
  title: string
  tag: string
  lines: { text: string; tone?: Tone; dim?: boolean }[]
}

const WHAT = 'sessions, turns, model requests and tool calls'
const NOT_SET_UP = 'the project and dataset options are set in /config, or BQAA_PROJECT_ID and BQAA_DATASET'

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)

// Where rows go under this configuration: the dry-run directory, or the
// table and its location; null while nothing is recorded (capture off, or
// not set up). Rows that landed somewhere else (in a dry run before a
// reload, in another table) say nothing about it.
export const destinationKey = (c: Config): string | null => (!c.enabled || !canDeliver(c) ? null : c.dryRun ? `dry run: ${c.dryRunDir}` : `${tableRef(c)} (${c.location})`)

// The version a greeting record names, if it names one.
export const greetedVersion = (greeted: unknown): string | null => (isObject(greeted) && typeof greeted.version === 'string' ? greeted.version : null)

// The versions greeted, from the store's keys (`welcome:<version>`).
export const greetedVersions = (keys: readonly string[]): string[] =>
  keys.filter(key => key.startsWith(`${WELCOME_KEY}:`) && key.split(':').length === 2).map(key => key.slice(WELCOME_KEY.length + 1))

// Who greeted this version, from its own record (`welcome:<version>`), or
// from `welcome` when that names this version; null when no one did.
export function greetedBy(latest: unknown, greeted: unknown): { sessionId: string | null; previous: string | null } | null {
  const record = isObject(greeted) ? greeted : greetedVersion(latest) === PLUGIN_VERSION && isObject(latest) ? latest : null
  if (record === null) return null
  return { sessionId: typeof record.sessionId === 'string' ? record.sessionId : null, previous: typeof record.previous === 'string' ? record.previous : null }
}

// What `$.store` holds for the card: `welcome` (`latest`), this version's
// greeting, this session's landing and close, and the versions greeted.
export type WelcomeRecords = { latest: unknown; greeted: unknown; landing: unknown; closed: unknown; versions: readonly string[] }

// At session start, from the records in `$.store`: a new card for a version
// not greeted yet, the card of the session that showed it after a reload
// that kept no `$.state` (unless it was closed; waiting again if rows now go
// elsewhere, so `key` is this configuration's `destinationKey`), else none.
export function openWelcome(records: WelcomeRecords, sessionId: string | null, key: string | null): { welcome: Welcome; isNew: boolean } | null {
  const by = greetedBy(records.latest, records.greeted)
  if (by === null) {
    // The version it replaces: the newest greeted before (an update's last
    // one; a change back's newer one).
    const before = [...records.versions, greetedVersion(records.latest)].filter((v): v is string => v !== null && v !== PLUGIN_VERSION)
    const previous = before.reduce<string | null>((newest, v) => (newest === null || isNewer(v, newest) ? v : newest), null)
    return { welcome: { version: PLUGIN_VERSION, previous, sessionId, landedAt: null, landedTo: null, closed: false, sent: 0, destination: key, owner: '', rank: 0, since: 0, lifetime: '' }, isNew: true }
  }
  if (sessionId === null || by.sessionId !== sessionId || records.closed === true) return null
  const landing = isObject(records.landing) ? records.landing : {}
  const here = key !== null && typeof landing.landedAt === 'number' && landing.landedTo === key
  return { welcome: { version: PLUGIN_VERSION, previous: by.previous, sessionId, landedAt: here ? (landing.landedAt as number) : null, landedTo: here ? key : null, closed: false, sent: 0, destination: key, owner: '', rank: 0, since: 0, lifetime: '' }, isNew: false }
}

// After a reload in the session that showed the card, from what `$.state`
// kept of it: none once closed; else the card as it was, counting where
// rows go now (`key`), its landing and count kept only if that is where
// they went. A card moved elsewhere counts from the run that moved it
// (register.ts), so no row is counted twice, should rows go back where they
// went.
export function reopenWelcome(kept: Welcome, key: string | null): { welcome: Welcome; isNew: boolean } | null {
  if (kept.closed) return null
  const here = key !== null && kept.landedAt !== null && kept.landedTo === key
  return { welcome: { ...kept, landedAt: here ? kept.landedAt : null, landedTo: here ? key : null, sent: here ? kept.sent : 0, destination: key }, isNew: false }
}

// One card's greeting and landing writes to `$.store` (register.ts passes
// `save`), in the background, one at a time: its greeting first (for a new
// card), then its landing when rows first land, written with the card as it
// is by then. Each module load writes through its own CardRecords; the keys
// keep them apart. Nothing waits on them; one that never answers holds up
// only the writes after it. A close is written on its own, at once
// (register.ts `closeWelcome`), behind nothing.
export class CardRecords {
  card: Welcome
  // Writes one value to `$.store`.
  private readonly save: (key: string, value: unknown) => Promise<void>
  private greetingDue: boolean
  private landingDue = false
  private writing = false

  // `land`: the card's landing goes too, where it has one (a load that took
  // the card over, an earlier load's write of it perhaps lost).
  constructor(save: (key: string, value: unknown) => Promise<void>, card: Welcome, greet: boolean, land = false) {
    this.save = save
    this.card = card
    this.greetingDue = greet
    this.landingDue = land && card.landedAt !== null
    this.write()
  }

  // The card changed (rows landed, or more did): its landing follows, and a
  // write that failed before goes again.
  update(card: Welcome): void {
    const was = this.card
    this.card = card
    if (card.landedAt !== was.landedAt || card.landedTo !== was.landedTo) this.landingDue = true
    this.write()
  }

  private write(): void {
    if (this.writing) return
    this.writing = true
    void this.drain()
  }

  private async drain(): Promise<void> {
    try {
      while (this.greetingDue || this.landingDue) {
        const { version, previous, sessionId, landedAt, landedTo } = this.card
        const greeting = this.greetingDue
        try {
          if (greeting) {
            this.greetingDue = false
            await this.save(greetedKey(version), { sessionId, previous })
          } else {
            this.landingDue = false
            if (sessionId !== null) await this.save(cardKey(version, sessionId), { landedAt, landedTo })
          }
        } catch {
          // Not recorded: it goes again at the card's next change (rows that
          // land later). Until then the next session shows the card again, or
          // a reload with no `$.state` shows it as the store last had it.
          if (greeting) this.greetingDue = true
          else this.landingDue = true
          break
        }
      }
    } finally {
      this.writing = false
    }
  }
}

// A version's parts: its dot-separated numbers and what follows a `-`, or
// null when it is not of that form.
const parts = (version: string): { numbers: number[]; pre: string | null } | null => {
  const match = /^(\d+(?:\.\d+)*)(?:-(.+))?$/.exec(version)
  if (match === null) return null
  return { numbers: (match[1] as string).split('.').map(Number), pre: match[2] ?? null }
}

// Whether `version` is known to come after `than`: by its numbers, then a
// release after its own pre-release (`0.2.0` after `0.2.0-rc.1`), then
// pre-releases by their text. False when either is of another form.
const isNewer = (version: string, than: string): boolean => {
  const a = parts(version)
  const b = parts(than)
  if (a === null || b === null) return false
  for (let i = 0; i < Math.max(a.numbers.length, b.numbers.length); i += 1) {
    const step = (a.numbers[i] ?? 0) - (b.numbers[i] ?? 0)
    if (step !== 0) return step > 0
  }
  if (a.pre === null || b.pre === null) return a.pre === null && b.pre !== null
  // Pre-releases part by part: numbers as numbers, before words; more parts
  // after the same ones come later (`rc.10` after `rc.2`, `rc.1.1` after `rc.1`).
  const x = a.pre.split('.')
  const y = b.pre.split('.')
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    const p = x[i]
    const q = y[i]
    if (p === undefined || q === undefined) return q === undefined
    if (p === q) continue
    const pn = /^\d+$/.test(p)
    const qn = /^\d+$/.test(q)
    if (pn && qn) return Number(p) > Number(q)
    if (pn !== qn) return qn
    return p > q
  }
  return false
}

// Installed; updated, to a version known to be newer; else changed.
const arrival = (w: Welcome): string => (w.previous === null ? 'installed' : `${isNewer(w.version, w.previous) ? 'updated' : 'changed'} to ${w.version}`)

// Why nothing is recorded, when the configuration leaves nowhere to write.
const unset = (c: Config): string => (c.dryRun ? (c.problems[0] ?? 'the dry run has nowhere to write') : `nothing is recorded until ${NOT_SET_UP}`)

// The card for this configuration, with `sent` rows landed where it counts
// them (kept across a reload by `$.state`; a card rebuilt from the store's
// records alone starts at none, as the store does not keep the count).
export function welcomeCard(w: Welcome, c: Config, sent: number): WelcomeCard {
  const title = `BigQuery Agent Analytics mod ${arrival(w)}`
  const tag = w.previous === null ? w.version : `was ${w.previous}`
  if (!c.enabled) {
    return { tone: 'inactive', glyph: '○', title: `${title}, capture off`, tag, lines: [{ text: 'Nothing is recorded: the enabled option is off, or BQAA_TRACE_ENABLED=false. Turn it on in /config.' }] }
  }
  if (!canDeliver(c)) {
    const lines = c.dryRun ? [{ text: `Nothing is recorded: ${unset(c)}.` }] : [{ text: `Nothing is recorded until ${NOT_SET_UP}.` }, { text: "Then make the dataset with the mod's setup.sh; its README shows how.", dim: true }]
    return { tone: 'warning', glyph: '◇', title: `${title}, not set up yet`, tag, lines }
  }
  const landed = w.landedAt !== null
  const every = c.flushIntervalMs % 1000 === 0 ? `every ${c.flushIntervalMs / 1000} s` : `every ${c.flushIntervalMs} ms`
  const status = '/bqaa-status shows the counts'
  if (c.dryRun) {
    return {
      tone: landed ? 'success' : 'suggestion',
      glyph: '✓',
      title: `${title}, dry run`,
      tag,
      lines: [
        { text: `Writes ${WHAT} as JSONL files to ${c.dryRunDir}, not to BigQuery.` },
        landed ? { text: `✓ First rows written${sent > 0 ? ` · ${sent} so far` : ''} · ${status}`, tone: 'success' } : { text: `◌ Waiting for the first rows, written ${every} while you work · ${status}`, dim: true },
      ],
    }
  }
  return {
    tone: landed ? 'success' : 'suggestion',
    glyph: '✓',
    title,
    tag,
    lines: [
      { text: `Records ${WHAT} to ${tableRef(c)} (${c.location}).` },
      landed ? { text: `✓ First rows landed in BigQuery${sent > 0 ? ` · ${sent} sent` : ''} · ${status}`, tone: 'success' } : { text: `◌ Waiting for the first rows, sent ${every} while you work · ${status}`, dim: true },
    ],
  }
}

// The transcript line (dim, never sent to the model) when the card first
// shows: what it says, kept in the scrollback.
export function welcomeNotice(w: Welcome, c: Config): string {
  const mod = `bqaa: BigQuery Agent Analytics mod ${w.previous === null ? `installed (${w.version})` : `${arrival(w)} (from ${w.previous})`}`
  if (!c.enabled) return `${mod}; capture is off (the enabled option, or BQAA_TRACE_ENABLED=false).`
  if (!canDeliver(c)) return `${mod}, not set up yet: ${unset(c)}.`
  if (c.dryRun) return `${mod}, dry run: writes ${WHAT} as JSONL files to ${c.dryRunDir}, not to BigQuery.`
  return `${mod}: records ${WHAT} to ${tableRef(c)} (${c.location}); /bqaa-status shows what was sent.`
}

// The toast when the first rows land while the card shows: the band may be
// collapsed.
export const landedToast = (c: Config): string => (c.dryRun ? `BQAA: first rows written to ${c.dryRunDir}` : `BQAA: first rows landed in ${tableRef(c)}`)
