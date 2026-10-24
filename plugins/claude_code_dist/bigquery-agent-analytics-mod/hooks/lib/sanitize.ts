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

// Bounded, redacted copies of what the agent, the model and its tools
// produced. Structure is kept only where it is known safe: under a key that is
// not on the allowlist (allowlist.ts) the value goes whole, whatever it holds.
// Text that looks like JSON is parsed and held to the same list, or dropped
// whole when it cannot be parsed within the budget; it is never scrubbed line
// by line. Other text (prompts, answers, tool arguments and results, error
// text) is scanned whole for well-known credential shapes, credential-named
// values and URL credentials; that is best effort, as text has no structure
// to go by.
//
// Runs on the hot path, so its work is bounded by the character budget rather
// than by the size of what the host CLI passed in (a Write of a 2 MB file costs
// the same as one of 20 KB). Text past a budget is dropped and marked, never
// passed on unscanned.

import { isListedKey, nameParts } from './allowlist.ts'

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }

export type SanitizeStats = {
  // Something was cut (BQAA `is_truncated`).
  truncated: boolean
  // How many secrets were replaced; redaction alone does not set `truncated`,
  // as in the BQAA plugin.
  redactions: number
}

export type Limits = {
  // Characters of serialized JSON (quotes, escapes, keys and punctuation
  // counted) the whole copy may take.
  budgetChars: number
  maxStringChars: number
  maxDepth: number
  maxKeys: number
  maxItems: number
}

export const newStats = (): SanitizeStats => ({ truncated: false, redactions: 0 })

// Room left under a column's cap for the truncation markers the walk adds
// (at most one per nesting level once the budget is spent), so a cut copy
// still serializes within `maxPayloadChars` and keeps its shape.
const MARKER_HEADROOM = 512

export const limitsFor = (maxPayloadChars: number): Limits => {
  const budget = Math.max(256, maxPayloadChars - MARKER_HEADROOM)
  return { budgetChars: budget, maxStringChars: budget, maxDepth: 12, maxKeys: 200, maxItems: 200 }
}

const MARKER = '[REDACTED]'

// How far past a cut the text is scanned, so a credential that starts before
// the cut is matched whole; the scanned text past the cut is dropped.
const REDACT_MARGIN = 8192

// ---- credential names in text -------------------------------------------------

// In free text (a header, an assignment, a URL parameter) a name is a
// credential's when one of its parts, split on punctuation and camelCase
// (`db_password`, `X-Api-Key`, `sessionCookie`, `MY_PWD`), is one of these
// words...
const SENSITIVE_WORDS = new Set([
  'pass',
  'passwd',
  'password',
  'passwords',
  'passphrase',
  'pwd',
  'secret',
  'secrets',
  'token',
  'auth',
  'authorization',
  'cookie',
  'cookies',
  'session',
  'key',
  'apikey',
  'credential',
  'credentials',
  'private',
  'bearer',
  'jwt',
  'sig',
  'signature',
])
// ...ends with one of these (`rootpwd`, `accesstoken`, `usersession`)...
const SENSITIVE_ENDINGS = [
  'passwd',
  'password',
  'passphrase',
  'pwd',
  'secret',
  'token',
  'authorization',
  'cookie',
  'session',
  'credential',
  'credentials',
  'bearer',
  'jwt',
  'signature',
  'apikey',
  'accesskey',
  'privatekey',
  'secretkey',
  'signingkey',
]
// ...or contains one of these (`passwordhash`, `privatekeypem`). Short words
// (`pass`, `key`, `auth`, `sig`) match only as a whole part: `bypass`,
// `monkey`, `oauth` and `signal` are not credential names.
const SENSITIVE_STEMS = ['password', 'passwd', 'apikey', 'accesskey', 'privatekey', 'secretkey', 'signingkey', 'credential']

// A part also counts without trailing digits and a plural `s`: `token2`,
// `api_keys`, `access_tokens` and `sessions` are credential names too.
const forms = (part: string): string[] => {
  const bare = part.replace(/\d+$/, '')
  return bare.endsWith('s') ? [part, bare, bare.slice(0, -1)] : [part, bare]
}

const isCredentialWord = (word: string): boolean =>
  word !== '' && (SENSITIVE_WORDS.has(word) || SENSITIVE_ENDINGS.some(end => word.endsWith(end)) || SENSITIVE_STEMS.some(stem => word.includes(stem)))

// Each word, ending and stem above holds one of these, and a name's parts
// are pieces of its lower-case form: a name that holds none is no
// credential's, which is quick to tell.
const CREDENTIAL_PIECE = /pass|pwd|secret|token|auth|cookie|session|key|credential|private|bearer|jwt|sig/
// The same, in either case, for a name a word's reading gives (name
// characters alone).
const PIECE_IN_NAME = new RegExp(CREDENTIAL_PIECE.source, 'i')

// Whether each name that holds such a piece is a credential's, by the name as
// it came: a text holds the same few names again and again (one for each
// option word or list entry), and their parts are slow to tell. Up to 4,096
// names of up to 200 characters are kept.
const SENSITIVE_NAMES = new Map<string, boolean>()

export const isSensitiveKey = (name: string): boolean => {
  const lower = name.toLowerCase()
  // ADK's per-invocation state.
  if (lower.startsWith('temp:')) return true
  if (!CREDENTIAL_PIECE.test(lower)) return false
  const known = SENSITIVE_NAMES.get(name)
  if (known !== undefined) return known
  // Listed names, such as the token counts, are not credentials.
  const sensitive = !isListedKey(name) && nameParts(name).some(part => forms(part).some(isCredentialWord))
  if (name.length <= 200) {
    if (SENSITIVE_NAMES.size >= 4096) SENSITIVE_NAMES.clear()
    SENSITIVE_NAMES.set(name, sensitive)
  }
  return sensitive
}

// ---- free text ---------------------------------------------------------------

// Well-known credential shapes. Every pattern is linear: no unbounded
// quantifier sits next to another that can match the same text.
const PATTERNS: readonly (readonly [RegExp, string])[] = [
  [/-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|$)/g, '[REDACTED:private-key]'],
  [/\bsk-ant-[A-Za-z0-9_-]{16,}/g, '[REDACTED:api-key]'],
  [/\bsk-(?=[A-Za-z0-9_-]{0,200}\d)[A-Za-z0-9_-]{32,}/g, '[REDACTED:api-key]'],
  [/\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g, '[REDACTED:aws-access-key-id]'],
  [/\bAIza[0-9A-Za-z_-]{35}/g, '[REDACTED:google-api-key]'],
  [/\bya29\.[0-9A-Za-z._-]{20,}/g, '[REDACTED:google-oauth-token]'],
  [/\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})/g, '[REDACTED:github-token]'],
  [/\bglpat-[A-Za-z0-9_-]{20,}/g, '[REDACTED:gitlab-token]'],
  [/\bnpm_[A-Za-z0-9]{36,}/g, '[REDACTED:npm-token]'],
  [/\bxox[abposr]-[A-Za-z0-9-]{10,}/g, '[REDACTED:slack-token]'],
  [/\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}/g, '[REDACTED:stripe-key]'],
  [/\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, '[REDACTED:jwt]'],
  [/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}/gi, '$1 [REDACTED]'],
]

// A URL: scheme, `://`, then everything up to a space, a quote, a bracket or
// a backslash (where a URL inside escaped JSON ends).
const URL_IN_TEXT = /\b[a-z][a-z0-9+.-]{1,20}:\/\/[^\s"'<>\\`]+/gi
const URL_TRAILER = /[.,;:!?)\]}]+$/
// Incoming-webhook URLs, whose path is the credential: removed whole.
const WEBHOOK_URLS: readonly RegExp[] = [
  /^https?:\/\/hooks\.slack\.com\/(?:services|workflows|triggers)\//i,
  /^https?:\/\/(?:[a-z0-9-]+\.)?discord(?:app)?\.com\/api\/(?:v\d+\/)?webhooks\//i,
  /^https?:\/\/[a-z0-9-]+\.webhook\.office\.com\//i,
  /^https?:\/\/outlook\.office(?:365)?\.com\/webhook\//i,
]

const decodeName = (name: string): string => {
  try {
    return decodeURIComponent(name.replace(/\+/g, ' '))
  } catch {
    return name
  }
}

function redactUrl(url: string, stats: SanitizeStats): string {
  if (WEBHOOK_URLS.some(pattern => pattern.test(url))) {
    stats.redactions += 1
    return '[REDACTED:webhook-url]'
  }
  // `user:password@` (or a bare token) before the host.
  const out = url.replace(/^([a-z][a-z0-9+.-]*:\/\/)[^/?#@]+@/i, (_: string, scheme: string) => {
    stats.redactions += 1
    return `${scheme}${MARKER}@`
  })
  // Query and fragment parameters with a credential's name.
  return out.replace(/([?&#;])([^=&#;?/]+)=([^&#;]*)/g, (whole: string, separator: string, name: string, value: string) => {
    if (value === '' || !isSensitiveKey(decodeName(name))) return whole
    if (value !== MARKER) stats.redactions += 1
    return `${separator}${name}=${MARKER}`
  })
}

function redactUrls(text: string, stats: SanitizeStats): string {
  URL_IN_TEXT.lastIndex = 0
  if (!URL_IN_TEXT.test(text)) return text
  URL_IN_TEXT.lastIndex = 0
  return text.replace(URL_IN_TEXT, (match: string) => {
    const trailer = URL_TRAILER.exec(match)?.[0] ?? ''
    return redactUrl(match.slice(0, match.length - trailer.length), stats) + trailer
  })
}

// A line continuation in a name, as text holds it: a backslash and a line
// break, or that pair as a JSON string encodes it (`\\\n`, `\\\r\n`), as in a
// log line that quotes a command.
const JOIN = String.raw`(?:\\(?:\r?\n|\\\\(?:r\\)?n))`

// The length of the line continuation (JOIN) that starts at `i`; 0 where none
// does.
function joinLength(text: string, i: number): number {
  if (text[i] !== '\\') return 0
  if (text[i + 1] === '\n') return 2
  if (text[i + 1] === '\r') return text[i + 2] === '\n' ? 3 : 0
  if (text[i + 1] !== '\\' || text[i + 2] !== '\\') return 0
  if (text[i + 3] === 'n') return 4
  return text[i + 3] === 'r' && text[i + 4] === '\\' && text[i + 5] === 'n' ? 6 : 0
}

// A character's place in a name: 2 for one a name may start with (a letter
// or `_`), 1 for one it may only hold (a digit, `.` or `-`), else 0.
const NAME_PLACES = Uint8Array.from({ length: 128 }, (_, c) => (/[A-Za-z_]/.test(String.fromCharCode(c)) ? 2 : /[0-9.-]/.test(String.fromCharCode(c)) ? 1 : 0))
const namePlace = (text: string, i: number): number => NAME_PLACES[text.charCodeAt(i)] ?? 0

// A name and a separator: `name: value` and `name=value` (a header, a YAML or
// .env line, a JSON key, an assignment, a command-line flag such as
// `--password=value` or `-Dssl.password=value`), the name quoted or not, its
// quote escaped or not, line continuations (JOIN) in and after it read
// through. That is, after the text's start or a character no name has
// (`boundary`), where this pattern matches, the name taken whole, so a long
// continued name that no separator follows is not tried again shorter:
//   (?:-JOIN*){0,2}([A-Za-z_](?:JOIN*[A-Za-z0-9_.-]){0,99})(?:\\*["'])?(?:[ \t]|JOIN)*([:=])[ \t]*
// As a regular expression it reads a long run of continuations again from
// each place in it where a name may start (the `n` of a JSON-encoded one),
// which is quadratic. Here each run (continuations, a name, backslashes, the
// blanks before a separator) is measured once, from the text's end back (see
// nameRuns), and each place is tried in a few steps.
type NamedValue = { index: number; boundary: string; name: string; separator: number; end: number }

// For each position in a text: where the run of line continuations (JOIN)
// that starts there ends (`joins`); the run of spaces, tabs and continuations
// (`blanks`); the run of backslashes (`slashes`); and, at a name's character,
// the name from there, continuations read through (`names`), and how many
// characters it has, counted up to 101 (`lengths`).
type NameRuns = { joins: Int32Array; blanks: Int32Array; slashes: Int32Array; names: Int32Array; lengths: Uint8Array }

function nameRuns(text: string): NameRuns {
  const n = text.length
  const runs: NameRuns = { joins: new Int32Array(n + 1), blanks: new Int32Array(n + 1), slashes: new Int32Array(n + 1), names: new Int32Array(n + 1), lengths: new Uint8Array(n + 1) }
  const { joins, blanks, slashes, names, lengths } = runs
  joins[n] = blanks[n] = slashes[n] = names[n] = n
  for (let i = n - 1; i >= 0; i -= 1) {
    const c = text[i]
    const join = joinLength(text, i)
    joins[i] = join === 0 ? i : (joins[i + join] as number)
    blanks[i] = c === ' ' || c === '\t' ? (blanks[i + 1] as number) : join === 0 ? i : (blanks[i + join] as number)
    slashes[i] = c === '\\' ? (slashes[i + 1] as number) : i
    if (namePlace(text, i) === 0) continue
    const next = joins[i + 1] as number
    const more = namePlace(text, next) !== 0
    names[i] = more ? (names[next] as number) : i + 1
    lengths[i] = more ? Math.min(101, (lengths[next] as number) + 1) : 1
  }
  return runs
}

// The first name and separator (see NamedValue) at or after `from`.
function namedValue(text: string, from: number, runs: NameRuns): NamedValue | null {
  for (let i = from; i < text.length; i += 1) {
    const found = (i === 0 ? namedAt(text, 0, '', runs) : null) ?? (namePlace(text, i) === 0 ? namedAt(text, i + 1, text[i] as string, runs) : null)
    if (found !== null) return found
  }
  return null
}

// A name and a separator (see NamedValue) right after `boundary`, which ends
// at `p`.
function namedAt(text: string, p: number, boundary: string, runs: NameRuns): NamedValue | null {
  // Up to two dashes, each with the continuations after it.
  let q = p
  for (let k = 0; k < 2 && text[q] === '-'; k += 1) q = runs.joins[q + 1] as number
  if (namePlace(text, q) !== 2 || (runs.lengths[q] as number) > 100) return null
  const end = runs.names[q] as number
  // A quote after the name, escaped or not, then spaces, tabs and
  // continuations.
  const slash = runs.slashes[end] as number
  const separator = runs.blanks[text[slash] === '"' || text[slash] === "'" ? slash + 1 : end] as number
  if (text[separator] !== ':' && text[separator] !== '=') return null
  let after = separator + 1
  while (text[after] === ' ' || text[after] === '\t') after += 1
  return { index: p - boundary.length, boundary, name: text.slice(q, end), separator, end: after }
}

// After a credential name, the value goes: to the end of its line, whatever is
// in it (quotes, brackets and redaction markers are not trusted to end it),
// or, for a query-style `?name=` / `&name=`, to the next `&`, `#`, `;` or
// space (when a quote, `$` or a backtick opens it, as in a shell assignment
// after `;` or `&`, through its shell word if that is longer). A line that
// ends in a continuation (`\`) runs on to the next. After `=` with no space
// around it (a flag or an assignment) the value runs on through the whole
// shell word it starts (see readValue), to the end of the line that word
// ends on; a flag's, to a control operator that ends its word
// (`--password=x;echo ok` keeps `;echo ok`). Otherwise it runs through a
// quote that opens the value, to the end of the line that quote closes on.
// Anything left open runs to the end of the text.

// The end of the line at or after `from`, run on through each line that ends
// in a continuation; `start` is where the value starts.
function lineEnd(text: string, start: number, from: number): number {
  for (;;) {
    const nl = text.indexOf('\n', from)
    if (nl === -1) return text.length
    const stop = text[nl - 1] === '\r' ? nl - 1 : nl
    if (stop <= start || text[stop - 1] !== '\\') return stop
    from = nl + 1
  }
}

// Where a `name:` or `name = ` value ends.
function namedValueEnd(text: string, start: number): number {
  const quote = text[start]
  if (quote !== '"' && quote !== "'") return lineEnd(text, start, start)
  let q = start + 1
  while (q < text.length && text[q] !== quote) q += quote === '"' && text[q] === '\\' ? 2 : 1
  return q >= text.length ? text.length : lineEnd(text, start, q)
}

// Where a `name=` value whose shell word ends at `end` stops: there for a
// flag's whose word a control operator ends, else at the end of that line.
const valueStop = (text: string, start: number, end: number, flag: boolean): number =>
  flag && /[;&|<>)]/.test(text[end] ?? '') ? end : lineEnd(text, start, end)

// ---- credential values in shell text ----------------------------------------
//
// A credential's value is its shell word, read by a few simple rules
// (simpleEnd) from the quote it starts in, where that quote is known
// (readValue). Wherever the word's end cannot be told with certainty, the
// value runs to the end of the text, and all that follows it is lost with
// it: that over-redaction is the price of never keeping part of a credential.

// The quotes a position can be in: none, double, single, ANSI-C (`$'…'`),
// or unknown.
const NONE = 0
const DOUBLE = 1
const SINGLE = 2
const ANSI = 3
const UNKNOWN = 4

// The quote open at `to`, reading `text` from `from` by the shell's quoting
// rules alone: single quotes, double quotes, `$'…'` and backslashes (a line
// continuation is an escaped line break). Two readings use it. The
// whole-text scan (`states` given, from the text's start) records each
// position's quote and reads all other code as plain text, but a comment
// (from a `#` that starts a word) is read on its own up to its line break;
// it cannot tell past a comment that ends in a backslash (bash 3.2 continues
// it, other shells do not), a `#` right after an operator (bash 3.2 reads no
// comment there), a `<<` that is not `<<<` (a here-document, whose body is
// not code) or a backtick that does not close: UNKNOWN from there on. A word
// read alone (no `states`) cannot be told past a backtick, `$(` or `${`,
// which open code, an escaped quote, backslash, `$` or backtick outside
// quotes (which a command string's quoting can leave in), or where an escape
// runs past `to`.
function readQuotes(text: string, from: number, to: number, states?: Uint8Array): number {
  let q = NONE
  let comment = false
  // Where a backtick outside quotes opened what has not closed.
  let tick = -1
  let i = from
  while (i < to) {
    const c = text[i] as string
    let next = i + 1
    let after = q
    if (comment && c === '\n') {
      after = NONE
      comment = false
    } else if (comment && c === '\\' && afterContinuations(text, i) > i) {
      after = UNKNOWN
    } else if (q === SINGLE) {
      if (c === "'") after = NONE
    } else if (c === '\\') {
      if (states === undefined && q === NONE && /["'\\$`]/.test(text[i + 1] ?? '')) after = UNKNOWN
      next = i + 2
    } else if (q === DOUBLE || q === ANSI) {
      if (c === (q === DOUBLE ? '"' : "'")) after = NONE
    } else if (c === "'" || c === '"') {
      after = c === "'" ? SINGLE : DOUBLE
    } else {
      const n = afterContinuations(text, i + 1)
      const d = text[n] ?? ''
      if (c === '$' && d === "'") {
        after = ANSI
        next = n + 1
      } else if (states === undefined) {
        if (c === '`' || (c === '$' && (d === '(' || d === '{'))) after = UNKNOWN
      } else if (comment) {
        // A comment's text holds no code.
      } else if (c === '`') {
        tick = tick === -1 ? i : -1
      } else if (c === '#' && (i === 0 || /[\s;&|()<>]/.test(text[i - 1] as string))) {
        if (i === 0 || /\s/.test(text[i - 1] as string)) comment = true
        else after = UNKNOWN
      } else if (c === '<' && d === '<') {
        const e = afterContinuations(text, n + 1)
        if (text[e] === '<') next = e + 1
        else after = UNKNOWN
      }
    }
    if (after === UNKNOWN) {
      states?.fill(UNKNOWN, i)
      return UNKNOWN
    }
    states?.fill(q, i, Math.min(next, to))
    q = after
    i = next
  }
  if (states !== undefined) {
    states[to] = q
    if (tick !== -1) states.fill(UNKNOWN, tick)
  }
  return i > to ? UNKNOWN : q
}

// Each position's quote as the whole-text scan reads it (see readQuotes).
function quoteStates(text: string): Uint8Array {
  const states = new Uint8Array(text.length + 1)
  readQuotes(text, 0, text.length, states)
  return states
}

// Where the word that holds `p` starts, as it reads alone: just past the last
// space, tab, line break or operator before `p`; -1 when that is more than
// 4,096 characters back.
function wordStart(text: string, p: number): number {
  let k = p
  while (k > 0 && !/[\s;&|<>()]/.test(text[k - 1] as string)) {
    k -= 1
    if (p - k > 4096) return -1
  }
  return k
}

// Where a credential value that starts at `start` in quote `q` ends, read by
// these rules alone: plain and backslash-escaped characters, single-quoted
// parts and double-quoted parts with no expansion, joined, up to the first
// space, tab, line break, `;`, `|`, `&`, `<`, `>` or `)` outside them. -1
// where they cannot tell: where the value holds `$(`, `${`, `$[`, `$'`, `$"`,
// a backtick, `(`, `<(`, `>(`, a line continuation, a quote that does not
// close, a single quote written `"'"` (as a single-quoted command string
// writes its own, `'"'"'`: the value may be in such a string, its quotes
// misread), or, outside quotes, an escaped quote, backslash, `$` or backtick
// (which a command string's quoting can leave in); where the quote it starts
// in closes with more of its word joined after it (`"PASSWORD="Secret`, where
// that quote may as well be an outer string's, such as JSON's); and where a
// redirection (`<`, `>` or `&>`, after nothing or a file descriptor's
// number) ends it, as the real value can come after a redirection.
function simpleEnd(text: string, start: number, q: number): number {
  // Whether the value is still in the quote it starts in.
  let first = q !== NONE
  let i = start
  for (; i < text.length; i += 1) {
    const c = text[i] as string
    const d = text[i + 1] ?? ''
    if (c === '"' && d === "'" && text[i + 2] === '"') {
      return -1
    } else if (q === SINGLE ? c === "'" : q === DOUBLE && c === '"') {
      q = NONE
      if (first && !/^(?:[ \t\n;&|<>)]|\r\n|$)/.test(text.slice(i + 1, i + 3))) return -1
      first = false
    } else if (q === SINGLE) {
      // Single-quoted text is literal.
    } else if (c === '\\') {
      if (d === '' || d === '\n' || d === '\r' || (q === NONE && /["'\\$`]/.test(d))) return -1
      i += 1
    } else if (c === '`' || (c === '$' && /[({['"]/.test(d))) {
      return -1
    } else if (q === DOUBLE) {
      // Double-quoted text with no expansion is literal.
    } else if (c === "'" || c === '"') {
      q = c === "'" ? SINGLE : DOUBLE
    } else if (c === ' ' || c === '\t' || c === '\n' || (c === '\r' && d === '\n') || c === ';' || c === '|' || c === ')' || (c === '&' && d !== '>')) {
      break
    } else if (c === '<' || c === '>' || c === '&') {
      return text[afterContinuations(text, i + 1)] === '(' || /^\d*$/.test(text.slice(start, i)) ? -1 : i
    } else if (c === '(') {
      return -1
    }
  }
  return q === NONE ? i : -1
}

// A credential value that starts at `start`, in the word that starts at
// `from`: where its shell word ends (see simpleEnd), and the quote it starts
// in, put back after the marker. Null where that end cannot be told with
// certainty: where the quote it starts in is not known, because the
// whole-text scan (`states`) and the word read alone from `from` disagree on
// it or either cannot tell (as after an apostrophe in prose, in a command
// string such as `sh -c "… --password …"`, past a here-document); where it
// starts in `$'…'`; or where simpleEnd cannot tell. `alone`, where given, is
// the quote the word read alone is in at `start`, already read.
function readValue(text: string, start: number, from: number, states: Uint8Array, alone?: number): { end: number; close: string } | null {
  const q = states[start] ?? UNKNOWN
  if (from === -1 || q === UNKNOWN || q === ANSI || (alone ?? readQuotes(text, from, start)) !== q) return null
  const end = simpleEnd(text, start, q)
  return end === -1 ? null : { end, close: q === DOUBLE ? '"' : q === SINGLE ? "'" : '' }
}

const JOINS = new RegExp(JOIN, 'g')
const JOIN_AT = new RegExp(JOIN, 'y')

// A name as the shell reads it, its line continuations (JOIN) removed.
const joined = (name: string): string => (name.includes('\\') ? name.replace(JOINS, '') : name)

// The first index at or after `i` that no line continuation (JOIN) covers.
function afterJoins(text: string, i: number): number {
  for (JOIN_AT.lastIndex = i; JOIN_AT.test(text); ) i = JOIN_AT.lastIndex
  return i
}

function redactNamedValues(text: string, stats: SanitizeStats): string {
  if (!/[:=]/.test(text)) return text
  const runs = nameRuns(text)
  let states: Uint8Array | undefined
  let out = ''
  let copied = 0
  let next = 0
  for (let m = namedValue(text, 0, runs); m !== null; m = namedValue(text, next, runs)) {
    next = m.end
    if (!isSensitiveKey(joined(m.name))) {
      // Resume at the separator: it can be the boundary before the next name
      // (`--from-literal=password=…`, `env: NAME=…`).
      next = m.separator
      continue
    }
    const start = m.end
    const from = wordStart(text, m.index + m.boundary.length)
    const equals = text[m.separator] === '='
    let end: number
    // A quote open where the value starts and closed in it is put back after
    // the marker.
    let close = ''
    if (equals && /^[?&#;]$/.test(m.boundary)) {
      const stop = /[&#;\s]/.exec(text.slice(start))
      end = stop === null ? text.length : start + stop.index
      // After `;` or `&` it may be a shell assignment, and after `?` or `#` a
      // quote, `$` or a backtick may open it: through its shell word, if that
      // is longer (escaped spaces, quotes, expansions).
      if (m.boundary === ';' || m.boundary === '&' || /^["'`$]/.test(text[start] ?? '')) {
        const read = readValue(text, start, from, (states ??= quoteStates(text)))
        if (read === null) {
          end = text.length
        } else if (read.end > end) {
          end = read.end
          close = read.close
        }
      }
    } else if (equals && start === m.separator + 1 && !/[ \t]/.test(text[m.separator - 1] as string)) {
      const read = readValue(text, start, from, (states ??= quoteStates(text)))
      end = read === null ? text.length : valueStop(text, start, read.end, /^\$?["']*-/.test(text.slice(from, start)))
      close = read?.close ?? ''
    } else {
      end = namedValueEnd(text, start)
    }
    const value = text.slice(start, end)
    if (value.trim() === '') continue
    out += text.slice(copied, start) + MARKER + close
    copied = end
    next = end
    if (value !== MARKER + close) stats.redactions += 1
  }
  return copied === 0 ? text : out + text.slice(copied)
}

// A long command-line option and its value after a space (`--password VALUE`,
// `--api-key "VALUE"`, `--pass -VALUE`, `"--password" VALUE`), or after line
// continuations (`--password \` and the value on the next line): the whole
// shell argument (see readValue). The option's name is a credential's and
// its last part a credential word, so `--password-stdin host`,
// `--token-file path` and `--max-tokens 1024` keep theirs. A value that
// starts with `--` is the next option, not a value. Line continuations (JOIN)
// in the name are read through (`--pass\` and `word VALUE`). A continuation
// with no space beside it right after a credential name glues what follows
// to it (`--password\` and `VALUE`, `--pass\` and `"word" VALUE`): everything
// from there to the end of the text goes, unless what follows carries the
// name on into another option (`--password\` and `-stdin`, `"--password"\`
// and `"-stdin"`).
const FLAG_VALUE = new RegExp(String.raw`(^|[^A-Za-z0-9_.-])-${JOIN}*-${JOIN}*([A-Za-z](?:${JOIN}*[A-Za-z0-9_.-]){0,99})((?:\\*["'])?)((?:[ \t]|${JOIN})+)(?![ \t]|${JOIN})(?!--)(?=\S)`, 'g')
// A value that is one redaction marker and nothing else.
const WHOLE_MARKER = /^\[REDACTED(?::[a-z-]+)?\]$/

// The first index at or after `i` that a line continuation (a backslash and
// a newline, which the shell removes) does not cover.
function afterContinuations(text: string, i: number): number {
  for (;;) {
    if (text[i] === '\\' && text[i + 1] === '\n') i += 2
    else if (text[i] === '\\' && text[i + 1] === '\r' && text[i + 2] === '\n') i += 3
    else return i
  }
}

// Whether a long option's name is a credential's: one with a credential word
// for its last part.
const credentialOption = (name: string): boolean =>
  isSensitiveKey(name) && forms(nameParts(name).pop() ?? '').some(isCredentialWord)

function redactFlagValues(text: string, stats: SanitizeStats): string {
  FLAG_VALUE.lastIndex = 0
  let states: Uint8Array | undefined
  let out = ''
  let copied = 0
  for (let m = FLAG_VALUE.exec(text); m !== null; m = FLAG_VALUE.exec(text)) {
    const raw = m[2] as string
    const own = joined(raw)
    const dashes = m.index + (m[1] as string).length
    let start = m.index + m[0].length
    let end = text.length
    let close = ''
    if (!/[ \t]/.test(m[4] as string)) {
      // Line continuations alone after the name glue what follows to its
      // word, as the shell passes it.
      const at = optionStart(text, dashes)
      const word = at === -1 ? null : optionWord(text, at)
      const name = word !== null && /^--[A-Za-z]/.test(word.name) ? word.name.slice(2) : own
      // Carried on into another option: its value, if it is a credential's,
      // is redactOptionWords'.
      if (name.length > own.length && name.startsWith(own) && /^[-_.]/.test(name.slice(own.length))) continue
      if (!credentialOption(own) && !credentialOption(name)) continue
    } else if (credentialOption(own)) {
      const read = readValue(text, start, wordStart(text, dashes), (states ??= quoteStates(text)))
      if (read !== null) {
        // No value before an operator (`--password ;`).
        if (read.end === start) continue
        end = read.end
        close = read.close
      }
    } else {
      // With a continuation in the name taken for a glued value (`--password\`
      // and `VALUE`), the part before it may be a credential option's: the
      // longest such part, before a letter or digit. Everything after its
      // continuation goes.
      let cut = -1
      let part = ''
      for (let k = 0; k < raw.length; ) {
        const after = afterJoins(raw, k)
        if (after === k) {
          part += raw[k]
          k += 1
          continue
        }
        if (/[A-Za-z0-9]/.test(raw[after] ?? '') && credentialOption(part)) cut = after
        k = after
      }
      if (cut === -1) continue
      start = m.index + m[0].indexOf(raw, (m[1] as string).length) + cut
    }
    if (wholeMarker(text.slice(start, end), close)) continue
    out += text.slice(copied, start) + MARKER + close
    copied = end
    stats.redactions += 1
    FLAG_VALUE.lastIndex = end
  }
  return copied === 0 ? text : out + text.slice(copied)
}

// Whether a value is one redaction marker and the quote put back after it.
const wholeMarker = (value: string, close: string): boolean =>
  WHOLE_MARKER.test(close !== '' && value.endsWith(close) ? value.slice(0, -close.length) : value)

// The start of the word whose first dash is at `p`, past the quotes before
// it; -1 when other text comes first (the word is no option).
function optionStart(text: string, p: number): number {
  let k = p
  while (k > 0 && (text[k - 1] === '"' || text[k - 1] === "'" || (text[k - 1] === '$' && text[k] === "'"))) k -= 1
  return k === 0 || /[\s;&|(]/.test(text[k - 1] as string) ? k : -1
}

// An ANSI-C escape at `i` (a backslash inside `$'…'`): the character it
// stands for and its length. A control character or an escape it does not
// decode gives a character no option name has.
function ansiEscape(text: string, i: number): { char: string; length: number } {
  const ahead = text.slice(i + 1, i + 10)
  const hex = /^x([0-9A-Fa-f]{1,2})/.exec(ahead)
  if (hex !== null) return { char: String.fromCharCode(parseInt(hex[1] as string, 16)), length: 1 + hex[0].length }
  const octal = /^[0-7]{1,3}/.exec(ahead)
  if (octal !== null) return { char: String.fromCharCode(parseInt(octal[0], 8) & 0xff), length: 1 + octal[0].length }
  const unicode = /^(?:u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8})/.exec(ahead)
  if (unicode !== null) {
    const code = parseInt(unicode[0].slice(1), 16)
    return { char: code <= 0x10ffff ? String.fromCodePoint(code) : '\0', length: 1 + unicode[0].length }
  }
  const e = ahead[0] ?? ''
  return { char: /^[\\'"?]$/.test(e) ? e : '\0', length: 2 }
}

// A word's name, where it starts and ends, where the value after an `=`
// starts (-1: none), and the state its reader is in there or at the word's
// end (see optionWord and looseWord), from which a list reading goes on.
type OptionWord = { name: string; start: number; end: number; value: number; state: number }

// What a list reading (see optionWord and looseWord) comes to from each place
// in a text where a name starts again, by the state it is in there: each
// list is read once, whichever word's reading comes to it.
type ListReadings = Map<number, OptionWord | null>

// optionWord's quotes, as readQuotes numbers them, and back.
const QUOTE_OF: Readonly<Record<string, number>> = { '': NONE, '"': DOUBLE, "'": SINGLE, a: ANSI }
const QUOTES = ['', '"', "'", 'a']

// What a list reading came to, kept for each place `passed` where it started
// a name again.
function settle(readings: ListReadings | undefined, passed: number[] | undefined, word: OptionWord | null): OptionWord | null {
  if (readings !== undefined && passed !== undefined) for (const key of passed) readings.set(key, word)
  return word
}

// Whether a character is one a name may hold (see NAME_PLACES), white space
// (as `\s` reads it) or a control operator's (`;&|<>()`), told by a table
// for ASCII, as the readers ask of each character.
const isNameChar = (ch: string): boolean => ch.length === 1 && (NAME_PLACES[ch.charCodeAt(0)] ?? 0) !== 0
const ASCII_KINDS = Uint8Array.from({ length: 128 }, (_, c) => (/\s/.test(String.fromCharCode(c)) ? 1 : /[;&|<>()]/.test(String.fromCharCode(c)) ? 2 : 0))
const isSpace = (c: string): boolean => (c.charCodeAt(0) < 128 ? ASCII_KINDS[c.charCodeAt(0)] === 1 : /\s/.test(c))
const isOperator = (c: string): boolean => ASCII_KINDS[c.charCodeAt(0)] === 2

// A long option's word from `i` as the shell passes it: its quotes and
// backslashes removed, ANSI-C escapes decoded and line continuations
// dropped, up to its first `=` or its end. Returns that text, where it starts
// and ends and where the value after an `=` starts (-1: no `=`), and the
// state this reading is in there (see wordState); null when, before then,
// the word holds an expansion, a character no option name has, or a name
// longer than 200 characters. With `accept`, a name before an `=` that it
// does not take is passed by, and the rest of the word is read as a list of
// names and values (`--set-env-vars=ORDINARY=x,DB_PASS"WORD"=…`,
// `-o user=x:pass"word"=…`): a name starts again after each `=`, each
// expansion (`$VALUE`, `${VALUE}`, whatever it holds) and each other
// character no name has but a quote or a backslash the word leaves in
// (those are dropped), up to a name before an `=` that `accept` takes. A name
// of more than 200 characters is none, but does not end the list: it may be
// a long value. Null where code whose end is not read (a backtick, `$(`)
// comes first, or the word ends, unless nothing but name characters came
// after its last `=` (that name is returned, as without `accept`). Given
// `after`, the state a reading was in after a word's `=` whose value starts
// at `i`, it reads on from there as such a list. What a list comes to is kept
// in `readings` (see ListReadings).
function optionWord(text: string, i: number, accept?: (word: OptionWord) => boolean, readings?: ListReadings, after?: number): OptionWord | null {
  let name = ''
  let start = i
  let quote = after === undefined ? '' : (QUOTES[after >> 1] as string)
  // Whether a name before an `=` was passed by, and since the last `=` a
  // character no name has came.
  let listed = after !== undefined
  let apart = false
  // Whether the word read alone from where this reading started (see
  // readQuotes) still tells the quote it is in: not past `${`, `$'` across a
  // line continuation, or, outside quotes, an escaped quote, backslash, `$`
  // or backtick.
  let known = after === undefined || (after & 1) === 1
  // Where this reading started a name again in a list, with its state there.
  let passed: number[] | undefined
  while (listed || name.length <= 200) {
    if (readings !== undefined && listed && start === i) {
      const key = ((i * 4 + (QUOTE_OF[quote] as number)) * 2 + (known ? 1 : 0)) * 2 + (apart ? 1 : 0)
      const read = readings.get(key)
      if (read !== undefined) return settle(readings, passed, read)
      ;(passed ??= []).push(key)
    }
    const c = text[i]
    if (c === undefined) return settle(readings, passed, quote === '' && !apart && name.length <= 200 ? { name, start, end: i, value: -1, state: known ? 1 : 0 } : null)
    let ch = c
    let step = 1
    if (quote === "'" || quote === 'a') {
      if (c === "'") {
        quote = ''
        i += 1
        continue
      }
      if (quote === 'a' && c === '\\') {
        const e = ansiEscape(text, i)
        ch = e.char
        step = e.length
      }
    } else if (c === '\\') {
      const next = afterContinuations(text, i)
      if (next > i) {
        i = next
        continue
      }
      if (quote === '"' && !/[\\"$`]/.test(text[i + 1] ?? '')) {
        ch = '\\'
      } else {
        if (quote === '' && /["'\\$`]/.test(text[i + 1] ?? '')) known = false
        ch = text[i + 1] ?? ''
        step = 2
      }
    } else if (quote === '"' && c === '"') {
      quote = ''
      i += 1
      continue
    } else if (quote === '' && c === '$' && text[i + 1] === "'") {
      quote = 'a'
      i += 2
      continue
    } else if (c === '$' || c === '`') {
      // An expansion, outside quotes or in double quotes: one more character
      // no name has, in a list.
      const d = text[afterContinuations(text, i + 1)]
      if (!listed || c === '`' || d === '(') return settle(readings, passed, null)
      if (quote === '' && (d === '{' || d === "'")) known = false
    } else if (quote === '' && (c === "'" || c === '"')) {
      quote = c
      i += 1
      continue
    } else if (quote === '' && /[\s;&|<>()]/.test(c)) {
      return settle(readings, passed, apart || name.length > 200 ? null : { name, start, end: i, value: -1, state: known ? 1 : 0 })
    }
    if (ch === '=') {
      const word = { name, start, end: i, value: i + step, state: (QUOTE_OF[quote] as number) * 2 + (known ? 1 : 0) }
      if (accept === undefined || (name.length <= 200 && accept(word))) return settle(readings, passed, word)
      listed = true
      apart = false
      name = ''
      start = i + step
    } else if (isNameChar(ch)) {
      if (name.length <= 200) name += ch
    } else if (listed && /^["'\\]$/.test(ch)) {
      // A quote or backslash the word leaves in is dropped, as the shell of a
      // command string the word may be in reads it (`DB_PASS'"'"'WORD'"'"'=`).
    } else if (listed) {
      apart = true
      name = ''
      start = i + step
    } else {
      return settle(readings, passed, null)
    }
    i += step
  }
  return settle(readings, passed, null)
}

// The quote the word read alone is in where optionWord's reading from the
// word's start left it in `state` (see readQuotes): UNKNOWN where it cannot
// tell.
const wordState = (state: number): number => ((state & 1) === 1 ? state >> 1 : UNKNOWN)

// A line break as a JSON string encodes it, after the backslashes that end
// before it: `n`, or `r`, backslashes and `n`.
const JSON_BREAK = /(?:r\\+)?n/y

// The word at `i` read loosely, as a command string's word that another
// shell runs, however deeply quoted, may pass it: every quote, backslash and
// line continuation (a run of backslashes and a line break, or one as a JSON
// string encodes it, `\\\n` or `\\\r\n` at any depth) dropped, the escapes in
// `$'…'` decoded, up to its first `=` or its end (a space, tab, line break
// or operator). A continuation right after a credential's name glues what
// follows to it as its value (as after an `=`), unless that carries the name
// on (`-`, `_`, `.`). Null where it holds a character no name has. With
// `accept`, read on as a list past a name before an `=` that it does not
// take, as optionWord reads one, from `after` and with `readings` as it does
// too; its state is whether `$'` came before.
function looseWord(text: string, i: number, accept?: (word: OptionWord) => boolean, readings?: ListReadings, after?: number): OptionWord | null {
  let name = ''
  let start = i
  let ansi = after === 1
  // As in optionWord.
  let listed = after !== undefined
  let apart = false
  let passed: number[] | undefined
  while (listed || name.length <= 200) {
    if (readings !== undefined && listed && start === i) {
      const key = (i * 2 + (ansi ? 1 : 0)) * 2 + (apart ? 1 : 0)
      const read = readings.get(key)
      if (read !== undefined) return settle(readings, passed, read)
      ;(passed ??= []).push(key)
    }
    const c = text[i]
    // In a list, an operator, which the string's own quotes may hold, is one
    // more character no name has.
    if (c === undefined || isSpace(c) || (!listed && isOperator(c))) return settle(readings, passed, apart || name.length > 200 ? null : { name, start, end: i, value: -1, state: ansi ? 1 : 0 })
    if (c === '\\') {
      let j = i
      while (text[j] === '\\') j += 1
      let next = text[j] === '\n' ? j + 1 : text[j] === '\r' && text[j + 1] === '\n' ? j + 2 : -1
      JSON_BREAK.lastIndex = j
      if (next === -1 && j - i >= 3 && (j - i) % 2 === 1 && JSON_BREAK.test(text)) next = JSON_BREAK.lastIndex
      if (next !== -1 && name !== '' && name.length <= 200 && !/[-_.]/.test(text[next] ?? '') && isSensitiveKey(name.replace(/^-+/, ''))) {
        const word = { name, start, end: next, value: next, state: ansi ? 1 : 0 }
        if (accept === undefined || accept(word)) return settle(readings, passed, word)
      }
      if (next !== -1) {
        i = next
        continue
      }
      if (ansi && /[0-7xuU]/.test(text[j] ?? '')) {
        const e = ansiEscape(text, j - 1)
        i = j - 1 + e.length
        if (isNameChar(e.char)) {
          if (name.length <= 200) name += e.char
        } else if (listed) {
          // After a character no name has, a name starts again in a list.
          apart = true
          name = ''
          start = i
        } else {
          return settle(readings, passed, null)
        }
      } else {
        i = j
      }
      continue
    }
    const ansiQuote = c === '$' && text[i + 1] === "'"
    if (ansiQuote) {
      ansi = true
    } else if (c === '=') {
      const word = { name, start, end: i, value: i + 1, state: ansi ? 1 : 0 }
      if (accept === undefined || (name.length <= 200 && accept(word))) return settle(readings, passed, word)
      listed = true
      apart = false
      name = ''
      start = i + 1
    } else if (isNameChar(c)) {
      if (name.length <= 200) name += c
    } else if (c === '"' || c === "'") {
      // A quote is dropped.
    } else if (listed && c !== '`' && !(c === '$' && text[i + 1] === '(')) {
      // After a character no name has, an expansion among them, a name
      // starts again in a list.
      apart = true
      name = ''
      start = i + 1
    } else {
      // Else the word is no name's; code whose end is not read (a backtick,
      // `$(`) ends a list too.
      return settle(readings, passed, null)
    }
    i += ansiQuote ? 2 : 1
  }
  return settle(readings, passed, null)
}

// The option or name a word is: `--name`, or before an `=` also `-name`,
// `-Dname` or a shell variable's name; in a list (`listed`), any name as the
// rules above read one (`db.password`, `x-api-key`).
function nameOf(word: OptionWord, listed = false): string | undefined {
  const m = (word.value === -1 ? /^--([A-Za-z][A-Za-z0-9_.-]*)$/ : listed ? /^(?:--?([A-Za-z][A-Za-z0-9_.-]*)|([A-Za-z_][A-Za-z0-9_.-]*))$/ : /^(?:--?([A-Za-z][A-Za-z0-9_.-]*)|([A-Za-z_][A-Za-z0-9_]*))$/).exec(word.name)
  return m?.[1] ?? m?.[2]
}

// A command that runs its arguments, joined, as shell code (`ssh host cmd
// …`, `eval cmd …`, `watch cmd …`): that shell reads their quotes again.
const JOINER = /(?:^|[\s(`/])(?:ssh|eval|watch)(?=[ \t])/g

// For each position in `text`, 1 where such a command is named before it in
// its command (after the last line break, `;`, `&` or `|`).
function joinedArguments(text: string): Uint8Array {
  const marks = new Uint8Array(text.length + 1)
  JOINER.lastIndex = 0
  let m = JOINER.exec(text)
  let on = 0
  for (let i = 0; i <= text.length; i += 1) {
    for (; m !== null && m.index + m[0].length <= i; m = JOINER.exec(text)) on = 1
    marks[i] = on
    if (/[\n;&|]/.test(text[i] ?? '')) on = 0
  }
  return marks
}

// The whole-text readings a word's rules use, made once per text: each
// position's quote (see quoteStates) and the first position after it in
// another quote (see quoteChanges), whether a shell reads it again as a
// joined argument (see joinedArguments), where commandWord's marks are (see
// commandMarks), and what the lists of names and values read as the shell
// passes them and loosely come to (see ListReadings).
type Scan = { states: () => Uint8Array; changes: () => Int32Array; rerun: () => Uint8Array; marks: () => CommandMarks; lists: () => ListReadings; looseLists: () => ListReadings }

// For each position, the first position after it whose quote (`states`)
// differs.
function quoteChanges(states: Uint8Array): Int32Array {
  const changes = new Int32Array(states.length)
  changes[states.length - 1] = states.length
  for (let i = states.length - 2; i >= 0; i -= 1) changes[i] = states[i + 1] !== states[i] ? i + 1 : (changes[i + 1] as number)
  return changes
}

// For each position in a text: the first backslash or `"'"` at or after it
// (`marks`) and the first space, tab, line break or other white space
// (`spaces`), so that commandWord, run at each of a long word's dashes,
// quotes or backslashes, does not read the word again each time.
type CommandMarks = { marks: Int32Array; spaces: Int32Array }

function commandMarks(text: string): CommandMarks {
  const n = text.length
  const marks = new Int32Array(n + 1)
  const spaces = new Int32Array(n + 1)
  marks[n] = spaces[n] = n
  for (let i = n - 1; i >= 0; i -= 1) {
    marks[i] = text[i] === '\\' || text.startsWith(`"'"`, i) ? i : (marks[i + 1] as number)
    spaces[i] = isSpace(text[i] as string) ? i : (spaces[i + 1] as number)
  }
  return { marks, spaces }
}

// Whether the word at `at` may be a command string's, quoted for the shell
// that runs it: where it holds a backslash that the whole-text scan does not
// read inside a quote the word opens itself (in `cli '--pa\ssword' x` the
// backslash is literal), or does but the word is a joined argument (`ssh
// host cli '--pa\ssword' x`); or a single quote written `"'"`, as a
// single-quoted command string writes its own (`'"'"'`). Only the first 400
// characters of the word are read.
function commandWord(text: string, at: number, scan: Scan): boolean {
  const { marks, spaces } = scan.marks()
  const end = Math.min(spaces[at] as number, at + 400)
  for (let k = marks[at] as number; k < end; k = marks[k + 1] as number) {
    if (text.startsWith(`"'"`, k)) return true
    const states = scan.states()
    if (states[k] !== SINGLE && states[k] !== DOUBLE) return true
    // In a quote: one the word opens itself, unless the quote open at `at`
    // lasts to it.
    if ((scan.changes()[at] as number) > k || scan.rerun()[at] === 1) return true
  }
  return false
}

// Whether a word's name, or its name and `=`, holds a quote or a backslash
// that is not a line continuation's (`"DB_PASS"WORD`, `--password\=`): one
// spelled plainly is the rules above's.
const inPieces = (text: string, word: OptionWord): boolean =>
  /["'\\]/.test(joined(text.slice(word.start, word.value === -1 ? word.end : word.value)))

// The credential name the word at `at` holds, as the shell passes it (see
// optionWord), else, where the word may be a command string's (see
// commandWord), read loosely (see looseWord): the word's own, or, after the
// `=` of a name that is not a credential's, the next one's
// (`--from-literal=pass"word"=…`, each part after an `=` read afresh); and
// where it starts. Where neither reading finds one so, the word is read on
// as a list of names and values (`--set-env-vars=ORDINARY=x,DB_PASS"WORD"=…`),
// both ways, each list once for the whole text (see ListReadings), and of the
// names spelled in pieces (see inPieces) the one whose value starts first is
// taken; read as the shell passes it, with the quote the word read alone is
// in there. Undefined where none is.
function credentialWord(text: string, at: number, scan: Scan): { word: OptionWord; name: string; nested: boolean; alone?: number | undefined } | undefined {
  // The words the readings that pass an `=` read first (only those can go on
  // as a list), and what they read next.
  const heads: [OptionWord, boolean, OptionWord | null][] = []
  for (const nested of commandWord(text, at, scan) ? [false, true] : [false]) {
    let head: OptionWord | undefined
    for (let from = at, k = 0; k < 8; k += 1) {
      const word = nested ? looseWord(text, from) : optionWord(text, from)
      const name = word === null || !PIECE_IN_NAME.test(word.name) ? undefined : nameOf(word)
      if (word !== null && name !== undefined && isSensitiveKey(name)) return { word, name, nested }
      if (k === 1 && head !== undefined) heads.push([head, nested, word])
      if (word === null || word.value === -1) break
      if (k === 0) head = word
      from = word.value
    }
  }
  // A list's credential name, spelled in pieces.
  const listed = (word: OptionWord): boolean => {
    const name = PIECE_IN_NAME.test(word.name) ? nameOf(word, true) : undefined
    return name !== undefined && isSensitiveKey(name) && inPieces(text, word)
  }
  let found: { word: OptionWord; name: string; nested: boolean; alone?: number | undefined } | undefined
  const starts = (word: OptionWord): number => (word.value === -1 ? word.end : word.value)
  for (const [head, nested, next] of heads) {
    // The list goes on after the first word's `=`, in the state its reading
    // was in there; after a loose word glued at a continuation, it reads that
    // name on. Where the next reading, from that `=` outside quotes, ended the
    // word (loosely, at a space or the text's end) with a name that is no
    // credential's, the list ends there too: it reads what that reading did.
    const glued = head.value === head.end
    const ended = !glued && next !== null && next.value === -1 && (nested ? head.state === 0 && (next.end === text.length || /\s/.test(text[next.end] as string)) : head.state >> 1 === NONE)
    const word = listed(head) ? head : ended ? null : glued ? looseWord(text, at, listed, scan.looseLists()) : nested ? looseWord(text, head.value, listed, scan.looseLists(), head.state) : optionWord(text, head.value, listed, scan.lists(), head.state)
    // A word with a value is one `listed` took.
    if (word === null || (word.value === -1 && !listed(word))) continue
    if (found === undefined || starts(word) < starts(found.word)) found = { word, name: nameOf(word, true) as string, nested, alone: nested ? undefined : wordState(word.state) }
  }
  return found
}

// Where a word starts that the rules above may see only in pieces: at a
// quote, a backslash, `$'` or a dash, or a name that a quote, a backslash or
// `$'` comes in (`DB_PASS"WORD"`), or an `=` follows (a list of names and
// values, `ORDINARY=x,DB_PASS"WORD"=…`). Also where a list goes on after a
// command substitution, whose end its reading does not read: after a `)` or
// a backtick and a list's separator (`VERSION=$(git rev-parse HEAD),…`).
const OPTION_WORD = /(?:^|[\s;&|(]|[)`][,:@#/+%!~^])(?=["'\\$-]|[A-Za-z_][A-Za-z0-9_.-]*["'\\$=])/g

// A word the shell passes as a long option once its quotes, backslashes,
// ANSI-C escapes and line continuations are read (`"--pass"word`,
// `--pass''word`, `--pa\ssword`, `-\-password`, `$'--pa\x73sword'`,
// `"--pass"word"=…"`), or as a name before an `=` (`"DB_PASS"WORD=…`,
// `-Dssl.pass"word"=…`, also after another option's `=`), which the rules
// above see only in pieces; also such a word of a command string another
// shell runs (`\"--pass\"word`, `--pa\\ssword`, `'"'"'--password'"'"'`).
// With `=`, its value as a flag's or an assignment's; else, if its name's
// last part is a credential word, the next shell argument. A command
// string's value runs to the end of the text.
function redactOptionWords(text: string, stats: SanitizeStats): string {
  OPTION_WORD.lastIndex = 0
  let states: Uint8Array | undefined
  let changes: Int32Array | undefined
  let rerun: Uint8Array | undefined
  let marks: CommandMarks | undefined
  let lists: ListReadings | undefined
  let looseLists: ListReadings | undefined
  const scan: Scan = {
    states: () => (states ??= quoteStates(text)),
    changes: () => (changes ??= quoteChanges(scan.states())),
    rerun: () => (rerun ??= joinedArguments(text)),
    marks: () => (marks ??= commandMarks(text)),
    lists: () => (lists ??= new Map()),
    looseLists: () => (looseLists ??= new Map()),
  }
  let out = ''
  let copied = 0
  for (let m = OPTION_WORD.exec(text); m !== null; m = OPTION_WORD.exec(text)) {
    const at = m.index + m[0].length
    OPTION_WORD.lastIndex = at + 1
    // A line continuation's line break starts no word.
    if (m[0] === '\n' && (text[m.index - 1] === '\\' || (text[m.index - 1] === '\r' && text[m.index - 2] === '\\'))) continue
    const option = credentialWord(text, at, scan)
    if (option === undefined || !inPieces(text, option.word)) continue
    const { word, name, nested } = option
    const from = word.start
    // A name the rules above see whole (a quote at most before and after
    // it) is theirs.
    if (!nested && word.value !== -1 && !word.name.startsWith('--') && /^\$?["']?-?[A-Za-z_][A-Za-z0-9_.-]*["']?$/.test(text.slice(from, word.value - 1))) continue
    let start: number
    let end = text.length
    let close = ''
    if (word.value !== -1) {
      start = word.value
      const read = nested ? null : readValue(text, start, at, scan.states(), option.alone)
      if (read !== null) {
        end = valueStop(text, start, read.end, /^\$?["']*-/.test(text.slice(at, start)))
        close = read.close
      }
    } else {
      if (!credentialOption(name)) continue
      start = word.end
      for (;;) {
        if (text[start] === ' ' || text[start] === '\t') start += 1
        else if (afterContinuations(text, start) > start) start = afterContinuations(text, start)
        else break
      }
      if (start === word.end || start >= text.length || /\s/.test(text[start] as string) || text.startsWith('--', start)) continue
      const read = nested ? null : readValue(text, start, at, scan.states(), option.alone)
      if (read !== null) {
        if (read.end === start) continue
        end = read.end
        close = read.close
      }
    }
    const value = text.slice(start, end)
    if (value.trim() === '' || wholeMarker(value, close)) continue
    out += text.slice(copied, start) + MARKER + close
    copied = end
    OPTION_WORD.lastIndex = end
    stats.redactions += 1
  }
  return copied === 0 ? text : out + text.slice(copied)
}

// The free-text rules: credential shapes, URLs, credential-named values, then
// credential-named command-line options, then option words the shell reads
// in pieces.
export function redactText(input: string, stats: SanitizeStats): string {
  let out = input
  for (const [pattern, replacement] of PATTERNS) {
    pattern.lastIndex = 0
    if (!pattern.test(out)) continue
    pattern.lastIndex = 0
    out = out.replace(pattern, (...args: unknown[]) => {
      stats.redactions += 1
      const groups = args.slice(1, -2) as (string | undefined)[]
      return replacement.replace(/\$(\d)/g, (_, n: string) => groups[Number(n) - 1] ?? '')
    })
  }
  out = redactUrls(out, stats)
  return redactOptionWords(redactFlagValues(redactNamedValues(out, stats), stats), stats)
}

// Error text, dead-letter reasons and status lines: redacted first, then held
// to `maxChars`, so a cut can never leave the start of a credential that, cut
// short, no longer matches its pattern. Of a window cut from a longer text,
// the last REDACT_MARGIN characters are never kept: they were scanned only
// for what comes before them, and may end in a credential the window cut
// short (redactions before it can shrink the window under `maxChars`).
export function redactDiagnostic(text: string, maxChars: number): string {
  const clipped = text.length > maxChars + REDACT_MARGIN
  const redacted = redactText(clipped ? text.slice(0, maxChars + REDACT_MARGIN) : text, newStats())
  return cutAt(redacted, clipped ? Math.min(maxChars, Math.max(0, redacted.length - REDACT_MARGIN)) : maxChars)
}

// ---- bounded copies ------------------------------------------------------------

type WalkState = { remaining: number; seen: WeakSet<object> }

// Length of the marker a cut string ends with.
const MARKER_RESERVE = 40

const jsonLength = (text: string): number => JSON.stringify(text).length

// Never end a cut between the two halves of a surrogate pair.
export const cutAt = (text: string, end: number): string => {
  let cut = text.slice(0, Math.max(0, end))
  const last = cut.charCodeAt(cut.length - 1)
  if (last >= 0xd800 && last <= 0xdbff) cut = cut.slice(0, -1)
  return cut
}

// Already-redacted `text`, cut to `maxJson` characters of serialized JSON and
// marked.
function cutToFit(text: string, originalLength: number, maxJson: number, stats: SanitizeStats): { text: string; cost: number } {
  stats.truncated = true
  const room = Math.max(0, maxJson - MARKER_RESERVE)
  let keep = Math.min(text.length, room)
  let cut = cutAt(text, keep)
  let cost = jsonLength(cut)
  // Escapes (quotes, newlines, control characters) make JSON longer than
  // the text: shrink until the serialized form fits.
  for (let i = 0; i < 8 && cost > room && keep > 0; i += 1) {
    keep = Math.max(0, Math.floor((keep * room) / cost) - 1)
    cut = cutAt(text, keep)
    cost = jsonLength(cut)
  }
  const out = `${cut}...[TRUNCATED ${Math.max(0, originalLength - cut.length)} chars]`
  return { text: out, cost: jsonLength(out) }
}

// `input` redacted by the text rules and, past `maxJson` characters of
// serialized JSON, cut and marked. Only a window reaching REDACT_MARGIN past
// the cut is scanned; the rest of a longer input is dropped, not passed on.
// Of that window, the last REDACT_MARGIN characters are never kept: they were
// scanned only for what comes before them, and may end in a credential the
// window cut short (redactions before it can shrink the window under the cut).
function clipString(input: string, maxJson: number, stats: SanitizeStats): { text: string; cost: number } {
  if (input.length > maxJson + REDACT_MARGIN) {
    const redacted = redactText(input.slice(0, maxJson + REDACT_MARGIN), stats)
    return cutToFit(cutAt(redacted, Math.max(0, redacted.length - REDACT_MARGIN)), input.length, maxJson, stats)
  }
  const redacted = redactText(input, stats)
  const cost = jsonLength(redacted)
  if (cost <= maxJson) return { text: redacted, cost }
  return cutToFit(redacted, input.length, maxJson, stats)
}

// Text that looks like JSON: an object; a list that opens with a value (an
// object, a list, a string, a number, true, false or null) or is empty; or a
// JSON-encoded string whose text opens an object or a list. `[1/3] Building`,
// `[INFO] ready` and `"Hi," she said.` do not.
const JSON_LOOKING = /^\s*(?:\{|\[\s*(?:[{["\]]|(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)\s*[,\]])|"(?:\s|\\[nrt"\\/])*[{[])/

// JSON text short enough to be scanned whole, parsed.
function parseJsonText(text: string, limits: Limits): { value: unknown } | null {
  if (text.length > limits.budgetChars + REDACT_MARGIN) return null
  try {
    return { value: JSON.parse(text) as unknown }
  } catch {
    return null
  }
}

// JSON-looking text that cannot be parsed whole (longer than the scan budget,
// or not valid JSON, such as a document cut short) cannot be checked key by
// key, and scrubbing it line by line would pass a credential nested below
// its name's line: it is dropped and marked instead.
function dropJsonText(text: string, stats: SanitizeStats, state: WalkState): string {
  stats.truncated = true
  stats.redactions += 1
  const marker = `[TRUNCATED: ${text.length} chars of JSON text that could not be parsed and checked]`
  state.remaining -= jsonLength(marker)
  return marker
}

// A key name that is not on the list is copied only when the free-text
// rules find nothing in it. One holding a credential shape, a URL with
// user-info, a credential-named parameter or a credential-named value, or
// one too long to scan within the budget, gives way to `[REDACTED_KEY:n]`:
// numbered in order within its object, never one of that object's own
// names, so no entry overwrites another.
function placeholderNeeded(key: string, limits: Limits): boolean {
  return key.length > limits.budgetChars + REDACT_MARGIN || redactText(key, newStats()) !== key
}

// JSON found in a string: walked under the same allowlist, then encoded
// again. Past the depth budget it is dropped, not passed on unscanned.
function walkJsonText(parsed: unknown, depth: number, allowed: number, limits: Limits, stats: SanitizeStats, state: WalkState): string {
  if (depth >= limits.maxDepth) {
    stats.truncated = true
    const text = '[TRUNCATED: nested JSON too deep]'
    state.remaining -= jsonLength(text)
    return text
  }
  // Encoding the result again escapes it: give the walk half the room.
  const inner = walk(parsed, depth + 1, limits, stats, { remaining: Math.floor(allowed / 2), seen: state.seen })
  const encoded = JSON.stringify(inner === undefined ? null : inner)
  const fitted = jsonLength(encoded) <= allowed ? { text: encoded, cost: jsonLength(encoded) } : cutToFit(encoded, encoded.length, allowed, stats)
  state.remaining -= fitted.cost
  return fitted.text
}

function walk(value: unknown, depth: number, limits: Limits, stats: SanitizeStats, state: WalkState): JsonValue | undefined {
  if (value === null) {
    state.remaining -= 4
    return null
  }
  switch (typeof value) {
    case 'string': {
      const allowed = Math.max(0, Math.min(limits.maxStringChars, state.remaining))
      if (JSON_LOOKING.test(value)) {
        const parsed = parseJsonText(value, limits)
        return parsed === null ? dropJsonText(value, stats, state) : walkJsonText(parsed.value, depth, allowed, limits, stats, state)
      }
      const { text, cost } = clipString(value, allowed, stats)
      state.remaining -= cost
      return text
    }
    case 'number': {
      const out = Number.isFinite(value) ? value : null
      state.remaining -= String(out).length
      return out
    }
    case 'boolean':
      state.remaining -= 5
      return value
    case 'bigint': {
      const out = value.toString()
      state.remaining -= out.length + 2
      return out
    }
    case 'undefined':
    case 'function':
    case 'symbol':
      return undefined
  }
  const obj = value as object
  if (state.seen.has(obj)) return '[CIRCULAR]'
  if (depth >= limits.maxDepth) {
    stats.truncated = true
    return '[TRUNCATED: nested too deep]'
  }
  if (ArrayBuffer.isView(obj)) return `[binary ${obj.byteLength} bytes]`
  if (obj instanceof ArrayBuffer) return `[binary ${obj.byteLength} bytes]`
  if (obj instanceof Date) return Number.isNaN(obj.getTime()) ? null : obj.toISOString()
  state.seen.add(obj)
  state.remaining -= 2
  try {
    if (Array.isArray(obj)) {
      const out: JsonValue[] = []
      for (let i = 0; i < obj.length; i += 1) {
        if (i >= limits.maxItems || state.remaining <= 0) {
          stats.truncated = true
          out.push(`[TRUNCATED ${obj.length - i} items]`)
          break
        }
        state.remaining -= 1
        const item = walk(obj[i], depth + 1, limits, stats, state)
        out.push(item === undefined ? null : item)
      }
      return out
    }
    const out: { [key: string]: JsonValue } = {}
    const keys = Object.keys(obj)
    const own = new Set(keys)
    let placeholders = 0
    for (let i = 0; i < keys.length; i += 1) {
      const key = keys[i] as string
      if (i >= limits.maxKeys || state.remaining <= 0) {
        stats.truncated = true
        out['_truncated_keys'] = keys.length - i
        break
      }
      const raw = (obj as Record<string, unknown>)[key]
      if (raw === undefined || typeof raw === 'function' || typeof raw === 'symbol') continue
      // Decided by the name as it came; a listed name is made of listed
      // words alone and is copied as it is.
      const listed = isListedKey(key)
      let name = key
      if (!listed && placeholderNeeded(key, limits)) {
        do {
          placeholders += 1
          name = `[REDACTED_KEY:${placeholders}]`
        } while (own.has(name))
        stats.redactions += 1
      }
      state.remaining -= jsonLength(name) + 2
      // Under a key that is not listed the whole value goes, whatever its
      // type; null has nothing to hide.
      if (!listed) {
        if (raw === null) {
          state.remaining -= 4
          out[name] = null
        } else {
          stats.redactions += 1
          state.remaining -= 12
          out[name] = MARKER
        }
        continue
      }
      const item = walk(raw, depth + 1, limits, stats, state)
      if (item !== undefined) out[name] = item
    }
    return out
  } finally {
    state.seen.delete(obj)
  }
}

// A JSON-safe, bounded, redacted copy of a row's `content`, its
// `attributes` or its error text: every key is held to the allowlist, every
// string to the text rules.
export function sanitize(value: unknown, limits: Limits, stats: SanitizeStats): JsonValue {
  const out = walk(value, 0, limits, stats, { remaining: limits.budgetChars, seen: new WeakSet() })
  return out === undefined ? null : out
}

// Serializes a sanitized value as the text of a BigQuery JSON column, held to
// `maxChars`. Past it the column becomes a valid JSON object holding a prefix
// of the serialized text, and `truncated` is set.
export function toJsonColumn(value: JsonValue, maxChars: number, stats: SanitizeStats): string {
  const json = JSON.stringify(value)
  if (json.length <= maxChars) return json
  stats.truncated = true
  let keep = Math.max(0, Math.floor((maxChars - 120) / 2))
  for (;;) {
    const wrapped = JSON.stringify({ truncated: true, original_chars: json.length, preview: json.slice(0, keep) })
    if (wrapped.length <= maxChars || keep === 0) return wrapped
    keep = Math.floor(keep / 2)
  }
}
