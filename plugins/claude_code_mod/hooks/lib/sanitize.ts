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

export const isSensitiveKey = (name: string): boolean => {
  // ADK's per-invocation state.
  if (name.toLowerCase().startsWith('temp:')) return true
  // Listed names, such as the token counts, are not credentials.
  if (isListedKey(name)) return false
  return nameParts(name).some(part => forms(part).some(isCredentialWord))
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

// A name and a separator: `name: value` and `name=value` (a header, a YAML or
// .env line, a JSON key, an assignment, a command-line flag such as
// `--password=value` or `-Dssl.password=value`), the name quoted or not, its
// quote escaped or not, line continuations in and after it read through.
const NAMED_VALUE = /(^|[^A-Za-z0-9_.-])(?:-(?:\\\r?\n)*){0,2}([A-Za-z_](?:(?:\\\r?\n)*[A-Za-z0-9_.-]){0,99})(?:\\*["'])?(?:[ \t]|\\\r?\n)*([:=])[ \t]*/g

// After a credential name, the value goes: to the end of its line, whatever is
// in it (quotes, brackets and redaction markers are not trusted to end it),
// or, for a query-style `?name=` / `&name=`, to the next `&`, `#`, `;` or
// space (when a quote, `$` or a backtick opens it, as in a shell assignment
// after `;` or `&`, through its shell word if that is longer). A line that
// ends in a continuation (`\`) runs on to the next. After `=` with no space
// around it (a flag or an assignment) the value runs on through the whole
// shell word it starts, to the end of the line that word ends on; otherwise
// through a quote that opens the value, to the end of the line that quote
// closes on. Anything left open runs to the end of the text.

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

// The quote codes a whole-text scan records at each position: none, `"`,
// `'`, a backtick, an ANSI-C `$'…'`; then literal (a comment, a
// here-document's body, an escaped character) and unknown (past a construct
// the scan cannot delimit), where a value cannot be read as shell.
const QUOTES = ['', '"', "'", '`', 'a']
const LITERAL = 5
const UNKNOWN = 6

// Each position's quote code in `text`, read as shell code from its start
// with the argument scanner's rules.
function shellQuotes(text: string): Uint8Array {
  const states = new Uint8Array(text.length + 1)
  shellArgument(text, 0, '', states)
  return states
}

// A reader of the quote code at positions of `text` (see shellQuotes), where
// the text of a comment or of a here-document's body reads as shell code on
// its own, so a command string in it has its quotes too; at the third level
// down, literal.
function quoteReader(text: string, depth = 0): (p: number) => number {
  let states: Uint8Array | undefined
  let region: { a: number; b: number; read: (p: number) => number } | undefined
  return (p: number): number => {
    states ??= shellQuotes(text)
    const state = states[p] ?? 0
    if (state !== LITERAL || depth >= 2) return state
    if (region === undefined || p < region.a || p >= region.b) {
      let a = p
      while (a > 0 && states[a - 1] === LITERAL) a -= 1
      let b = p
      while (b < text.length && states[b] === LITERAL) b += 1
      region = { a, b, read: quoteReader(text.slice(a, b), depth + 1) }
    }
    return region.read(p - region.a)
  }
}

// The quote open just before `p` as the word around it alone reads it, from
// the word's start (after the last space, line break or operator): '' for
// none, 'escaped' when a backslash escapes the character at `p`, and null
// when the word's start is more than 4096 characters back.
function wordQuote(text: string, p: number): string | null {
  let k = p
  while (k > 0 && !/[\s;&|<>()]/.test(text[k - 1] as string)) {
    k -= 1
    if (p - k > 4096) return null
  }
  let quote = ''
  while (k < p) {
    const c = text[k] as string
    if (quote === "'") {
      if (c === "'") quote = ''
    } else if (c === '\\') {
      k += 1
    } else if (quote === 'a' || quote === '"') {
      if (c === (quote === 'a' ? "'" : '"')) quote = ''
    } else if (c === '$' && text[k + 1] === "'") {
      quote = 'a'
      k += 1
    } else if (c === "'" || c === '"') {
      quote = c
    }
    k += 1
  }
  return k > p ? 'escaped' : quote
}

// The quotes a credential value that starts at `start` may start in, the
// scan's first: the one the whole-text scan finds open there (any, where it
// cannot tell); and the word alone: no quote, or, for a match with a quote
// just before it, that quote as the word around it reads it (opening one,
// closing one, or text inside another), then as the quotes in `between`
// leave it (`alone`, where the caller read the word itself); and as the
// shell a command string runs reads it (INNER). Reading the value in each
// and keeping the farthest end fails closed where the scan misreads the text
// before it (prose, an apostrophe, an odd quote).
function seedsAt(text: string, start: number, stateAt: (p: number) => number, m?: RegExpExecArray, between = '', alone?: string): string[] {
  const state = stateAt(start)
  const seeds = state === UNKNOWN || state === LITERAL ? ['', '"', "'"] : [QUOTES[state] ?? '']
  let quote = alone ?? ''
  const q = m?.[1] ?? ''
  if (alone === undefined && m !== undefined && (q === '"' || q === "'" || q === '`')) {
    let ansi = q === "'" && text[m.index - 1] === '$'
    let before = wordQuote(text, ansi ? m.index - 1 : m.index)
    if (ansi && before === 'escaped') {
      ansi = false
      before = wordQuote(text, m.index)
    }
    quote = before === null || before === '' ? (ansi ? 'a' : q) : before === 'escaped' || before === q || (before === 'a' && q === "'") ? '' : before
  }
  for (let k = 0; k < between.length; k += 1) {
    const b = between[k] as string
    if (b === '\\' && quote !== "'") k += 1
    else if (b === '"' || b === "'") quote = quote === '' ? b : quote === b || (quote === 'a' && b === "'") ? '' : quote
  }
  if (!seeds.includes(quote)) seeds.push(quote)
  // Read in a double quote, a value runs at least as far as a command
  // string's reading of it can.
  if (!seeds.includes('"')) seeds.push(INNER)
  return seeds
}

// The reading of a value as the shell a command string runs reads it (see
// innerReading).
const INNER = '\\'

// The double quotes in `text` that no backslash escapes, and the backslashes
// of its `\"` and `\\` pairs, for the text last asked about.
let escapes: { text: string; quotes: number[]; pairs: number[] } | undefined
function escapeIndex(text: string): { quotes: number[]; pairs: number[] } {
  if (escapes?.text !== text) {
    const quotes: number[] = []
    const pairs: number[] = []
    for (let k = 0; k < text.length; k += 1) {
      if (text[k] === '\\') {
        if (text[k + 1] === '"' || text[k + 1] === '\\') pairs.push(k)
        k += 1
      } else if (text[k] === '"') {
        quotes.push(k)
      }
    }
    escapes = { text, quotes, pairs }
  }
  return escapes
}

// The first index in the ascending `list` whose value is at least `value`.
function lowerBound(list: readonly number[], value: number): number {
  let lo = 0
  let hi = list.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if ((list[mid] as number) < value) lo = mid + 1
    else hi = mid
  }
  return lo
}

// text[from, to) with a double-quoted string's escapes undone (`\"`, `\\`,
// `\$`, `` \` ``, line continuations): what the shell a command string runs
// reads, and where each of its characters comes from in `text` (with `to`
// last).
function undoEscapes(text: string, from: number, to: number): { inner: string; at: number[] } {
  let inner = ''
  const at: number[] = []
  for (let k = from; k < to; k += 1) {
    const source = k
    if (text[k] === '\\') {
      const after = afterContinuations(text, k)
      if (after > k) {
        k = after - 1
        continue
      }
      if (/["\\$`]/.test(text[k + 1] ?? '')) k += 1
    }
    at.push(source)
    inner += text[k] ?? ''
  }
  at.push(to)
  return { inner, at }
}

// The last string asked about (see innerReading): where it lies in `text`,
// its text with the escapes undone, and that text's quote codes.
let strings: { text: string; from: number; inner: string; at: number[]; states: Uint8Array } | undefined

// A value's first word, up to a space or line break.
const FIRST_WORD = /\S*/y

// A value at `start` as the shell a command string runs reads it, where it
// or the text before it in the double-quoted string that holds it (from the
// last double quote before it that no backslash escapes, or the text's
// start, to the next one or the text's end) escapes a quote or a backslash:
// that string's text with its escapes undone, read as shell from its start,
// from the quote open where the value starts, to that string's end at most.
// Null where nothing is escaped there, or where the value starts in a quote
// that does not close (text written as a string's, a JSON log, may hold no
// such string). The end, and the quote to put back after the marker: the
// escaped one it closes.
function innerReading(text: string, start: number): { end: number; close: string } | null {
  const { quotes, pairs } = escapeIndex(text)
  const q = lowerBound(quotes, start)
  const from = q > 0 ? (quotes[q - 1] as number) + 1 : 0
  const to = q < quotes.length ? (quotes[q] as number) : text.length
  FIRST_WORD.lastIndex = start
  FIRST_WORD.exec(text)
  const pair = pairs[lowerBound(pairs, from)]
  if (pair === undefined || pair + 1 >= Math.min(to, FIRST_WORD.lastIndex)) return null
  if (strings?.text !== text || strings.from !== from) {
    const { inner, at } = undoEscapes(text, from, to)
    strings = { text, from, inner, at, states: shellQuotes(`${inner} `) }
  }
  const { inner, at, states } = strings
  const k = lowerBound(at, start)
  const code = states[k] ?? 0
  const quote = code === LITERAL || code === UNKNOWN ? '' : (QUOTES[code] ?? '')
  const scan = shellArgument(inner, k, quote)
  if (quote !== '' && scan.closed === -1) return null
  const close = quote === '' ? '' : text.slice(at[scan.closed] as number, at[scan.closed + 1] as number)
  return { end: at[scan.end] as number, close }
}

// A credential value read from `start` in each of `seeds`: the farthest end
// (with `toLine`, the end of the line that reading ends on) and the quote to
// put back after the marker, from the reading that set that end (the first
// seed's on a tie); and where the nearest reading in a quote ends. A second
// pass over the result may redact more where the readings disagree, never
// less.
function farthest(text: string, start: number, seeds: readonly string[], toLine: boolean): { end: number; close: string; near: number } {
  let best = { end: -1, close: '' }
  let near = text.length
  for (const seed of seeds) {
    if (seed === INNER) {
      const read = innerReading(text, start)
      if (read === null) continue
      const end = toLine ? lineEnd(text, start, read.end) : read.end
      if (end > best.end) best = { end, close: read.close }
      continue
    }
    const scan = shellArgument(text, start, seed)
    near = Math.min(near, scan.end)
    const end = toLine ? lineEnd(text, start, scan.end) : scan.end
    if (end > best.end) best = { end, close: scan.closed === -1 ? '' : closer(seed) }
  }
  return { ...best, near }
}

// The character that closes a quote open where a value starts.
const closer = (quote: string): string => (quote === 'a' ? "'" : quote)

// A name as the shell reads it, its line continuations removed.
const joined = (name: string): string => name.replace(/\\\r?\n/g, '')

function redactNamedValues(text: string, stats: SanitizeStats): string {
  NAMED_VALUE.lastIndex = 0
  const stateAt = quoteReader(text)
  let out = ''
  let copied = 0
  for (let m = NAMED_VALUE.exec(text); m !== null; m = NAMED_VALUE.exec(text)) {
    const name = m[2] as string
    const separator = m[0].lastIndexOf(m[3] as string)
    if (!isSensitiveKey(joined(name))) {
      // Resume at the separator: it can be the boundary before the next name
      // (`--from-literal=password=…`, `env: NAME=…`).
      NAMED_VALUE.lastIndex = m.index + separator
      continue
    }
    const start = m.index + m[0].length
    let end: number
    // A quote open where the value starts and closed in it is put back after
    // the marker.
    let close = ''
    if (m[3] === '=' && /^[?&#;]$/.test(m[1] as string)) {
      const stop = /[&#;\s]/.exec(text.slice(start))
      end = stop === null ? text.length : start + stop.index
      // After `;` or `&` it may be a shell assignment, and after `?` or `#` a
      // quote, `$` or a backtick may open it: through its shell word, if that
      // is longer (escaped spaces, quotes, expansions).
      if (m[1] === ';' || m[1] === '&' || /^["'`$]/.test(text[start] ?? '')) {
        const read = farthest(text, start, seedsAt(text, start, stateAt), false)
        if (read.end > end) {
          end = read.end
          close = read.close
        }
      }
    } else if (m[3] === '=' && /[^ \t]=$/.test(m[0])) {
      const nameEnd = m[0].indexOf(name, (m[1] as string).length) + name.length
      const read = farthest(text, start, seedsAt(text, start, stateAt, m, m[0].slice(nameEnd, separator)), true)
      end = read.end
      close = read.close
    } else {
      end = namedValueEnd(text, start)
    }
    const value = text.slice(start, end)
    if (value.trim() === '') continue
    out += text.slice(copied, start) + MARKER + close
    copied = end
    NAMED_VALUE.lastIndex = end
    if (value !== MARKER + close) stats.redactions += 1
  }
  return copied === 0 ? text : out + text.slice(copied)
}

// A long command-line option and its value after a space (`--password VALUE`,
// `--api-key "VALUE"`, `--pass -VALUE`, `"--password" VALUE`), or after line
// continuations (`--password \` and the value on the next line): the whole
// shell argument. The option's name is a credential's and its last part a
// credential word, so `--password-stdin host`, `--token-file path` and
// `--max-tokens 1024` keep theirs. A value that starts with `--` is the next
// option, not a value; so is one quoted by escaped quotes in a command
// string (`sh -c "cli \"--password\" …"`). Line continuations in the name are read through
// (`--pass\` and `word VALUE`); one with no space beside it is also taken for
// the gap before a value (`--password\` and `VALUE`), unless what follows
// carries the name on (`"--password"\` and `-stdin`).
const FLAG_VALUE = /(^|[^A-Za-z0-9_.-])-(?:\\\r?\n)*-(?:\\\r?\n)*([A-Za-z](?:(?:\\\r?\n)*[A-Za-z0-9_.-]){0,99})((?:\\*["'])?)((?:[ \t]|\\\r?\n)+)(?![ \t]|\\\r?\n)(?!--)(?=\S)/g
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

// The character before `i` once line continuations are removed (called where
// the text before `i` is a word, so a line break there is a continuation's).
function charBefore(text: string, i: number): string {
  let p = i - 1
  for (;;) {
    if (text[p] === '\n' && text[p - 1] === '\\') p -= 2
    else if (text[p] === '\n' && text[p - 1] === '\r' && text[p - 2] === '\\') p -= 3
    else return text[p] ?? ''
  }
}

// Where the word `word` that starts at `i` ends, line continuations in and
// after it read through; -1 when the text there is not that word alone.
function wordEnd(text: string, i: number, word: string): number {
  let j = i
  for (const letter of word) {
    j = afterContinuations(text, j)
    if (text[j] !== letter) return -1
    j += 1
  }
  j = afterContinuations(text, j)
  return /[A-Za-z0-9_]/.test(text[j] ?? '') ? -1 : j
}

// Whether `esac` at `i` is the word alone. A split `esac` is taken for
// another word, which leaves its case open, so the argument runs to the end.
const esacAt = (text: string, i: number): boolean =>
  text.startsWith('esac', i) && /^(?:[ \t\n;&|)<>]|$)/.test(text[i + 4] ?? '')

// After a `case` at `i`: the index just past its `in`; -1 when no subject and
// `in` follow, so `case` is a word; text.length for a form this does not
// read: a subject with a backslash (other than a line continuation), a
// backtick, `$(`, `${` or `$'`, a comment before `in`, or zsh's `{` form.
function caseStatementAt(text: string, i: number): number {
  let j = wordEnd(text, i, 'case')
  if (j === -1 || (text[j] !== ' ' && text[j] !== '\t')) return -1
  while (text[j] === ' ' || text[j] === '\t') j = afterContinuations(text, j + 1)
  const subject = j
  while (j < text.length && !/[ \t\n;&|<>()]/.test(text[j] as string)) {
    const c = text[j] as string
    if (c === '\\' && afterContinuations(text, j) > j) {
      j = afterContinuations(text, j)
      continue
    }
    if (c === '\\' || c === '`' || (c === '$' && /[('"{]/.test(text[j + 1] ?? ''))) return text.length
    if (c === '"' || c === "'") {
      const e = text.indexOf(c, j + 1)
      if (e === -1 || (c === '"' && /[\\`]|\$[('"{]/.test(text.slice(j + 1, e)))) return text.length
      j = e + 1
    } else {
      j += 1
    }
  }
  if (j === subject) return -1
  for (;;) {
    if (text[j] === ' ' || text[j] === '\t' || text[j] === '\n') j += 1
    else if (afterContinuations(text, j) > j) j = afterContinuations(text, j)
    else break
  }
  if (text[j] === '#' || text[j] === '{') return text.length
  const k = wordEnd(text, j, 'in')
  if (k === -1) return -1
  if (k >= text.length || text[k] === ' ' || text[k] === '\t' || text[k] === '\n') return k
  return text[k] === '\r' ? text.length : -1
}

// Where a comment that starts at `i` ends: at its line break; -1 when it runs
// to the end of the text or ends in a backslash, which bash 3.2 takes to
// continue it on the next line and other shells do not.
function commentEnd(text: string, i: number): number {
  const nl = text.indexOf('\n', i)
  return nl === -1 || text[nl - 1] === '\\' ? -1 : nl
}

// The delimiter of a here-document whose `<<` ends just before `j`: the word
// with its quotes removed, whether any of it was quoted, whether `<<-` strips
// tabs, and where the word ends. Null for an empty delimiter (bash 5 and
// other shells end its body differently) or one with an expansion, a
// backtick or a backslash in double quotes.
function hereDelimiter(text: string, j: number): { delimiter: string; quoted: boolean; tabs: boolean; end: number } | null {
  const tabs = text[j] === '-'
  if (tabs) j = afterContinuations(text, j + 1)
  while (text[j] === ' ' || text[j] === '\t') j = afterContinuations(text, j + 1)
  let delimiter = ''
  let quoted = false
  while (j < text.length && !/[ \t\n;&|<>()]/.test(text[j] as string)) {
    const c = text[j] as string
    if (c === '\\') {
      const after = afterContinuations(text, j)
      if (after > j) {
        j = after
        continue
      }
      delimiter += text[j + 1] ?? ''
      quoted = true
      j += 2
    } else if (c === '"' || c === "'") {
      const e = text.indexOf(c, j + 1)
      if (e === -1 || (c === '"' && /[\\$`]/.test(text.slice(j + 1, e)))) return null
      delimiter += text.slice(j + 1, e)
      quoted = true
      j = e + 1
    } else if (c === '`' || (c === '$' && /[({'"]/.test(text[j + 1] ?? ''))) {
      return null
    } else {
      delimiter += c
      j += 1
    }
  }
  return delimiter === '' ? null : { delimiter, quoted, tabs, end: j }
}

// Where the bodies of the here-documents in `due` end, read from `from`, the
// start of the line after theirs: just past each one's delimiter line, taken
// as it is (a carriage return, a trailing space or a `)` after it makes
// another line: bash alone ends a body at `EOF)`). `end` is -1 when one has
// none, or a line of an unquoted body ends in a backslash: a continuation,
// which joins the next line before it is compared; `known` is then how far
// the text is a body to every shell: to the line after that one, or none of
// it where no line ends a body.
function hereDocumentsEnd(text: string, from: number, due: readonly [string, boolean, boolean, number][]): { end: number; known: number } {
  let at = from
  for (const [delimiter, tabs, quoted] of due) {
    for (;;) {
      if (at >= text.length) return { end: -1, known: from }
      const nl = text.indexOf('\n', at)
      const line = text.slice(at, nl === -1 ? text.length : nl)
      at = nl === -1 ? text.length : nl + 1
      if (!quoted && line.endsWith('\\')) return { end: -1, known: at }
      if ((tabs ? line.replace(/^\t+/, '') : line) === delimiter) break
      if (nl === -1) return { end: -1, known: from }
    }
  }
  return { end: at, known: at }
}

// The shell argument that starts at `start`: it ends at the first space, tab,
// newline, control operator (`;`, `|`, `&`), redirection (`<`, `>`) or `)`
// outside every quote and expansion, and not escaped. It goes through single
// quotes (literal), double quotes and ANSI-C `$'…'` (backslash escapes),
// backticks, `${…}` (with the braces nested in it), `$((…))`, and `$(…)`,
// `(…)`, `<(…)` and `>(…)`, which hold shell code: in them a comment, a
// here-document's body, an array's elements (`name=(…)`) and a `case`
// statement's patterns are read as the shells read them, so a `)` there does
// not end the argument, while a `#` inside a word, an arithmetic `<<`, a
// `case` that is an argument and an `esac` that a redirection names are
// ordinary text. Line continuations are read
// through (`$`, a continuation and `{` is `${`). Where shells read a construct
// differently, or it cannot be delimited, the argument runs to the end of the
// text: a `#` right after an operator or starting a word in arithmetic, a
// comment that ends in a backslash, a here-document delimiter that is empty
// or holds an expansion, a body with no exact delimiter line, a continued
// line in an unquoted body, a body still due when its code ends or at a line
// break inside a nested part, `$((…)` that is not arithmetic, and a `case`
// that the function caseStatementAt does not read, or whose patterns hold a
// parenthesis or a line break. So does anything left open. `inside` is a
// quote the argument starts in; `closed` is where that quote closes (-1: it
// does not).
function shellArgument(text: string, start: number, inside: string, states?: Uint8Array): { end: number; closed: number } {
  // What is open, innermost last: ' " a(nsi) ` {, $((…)) as A, ((…)) as B, a
  // parenthesis inside them as P, a glob group as G, an array's elements
  // (`name=(…)`) as R, $( <( >( as S, a subshell as (, and a case
  // statement: c between its arms, p in a pattern, l in an arm's commands.
  const open: string[] = states !== undefined ? ['W'] : inside === '' ? [] : [inside]
  // The quote each open part is in, for a whole-text scan's record: a quote's
  // own; `${…}`'s and a glob group's, that of what holds them; shell code's,
  // none.
  const quotes: number[] = open.map(frame => Math.max(0, QUOTES.indexOf(frame)))
  const push = (frame: string): void => {
    open.push(frame)
    const code = QUOTES.indexOf(frame)
    quotes.push(code > 0 ? code : frame === '{' || frame === 'G' ? (quotes[quotes.length - 1] ?? 0) : 0)
  }
  const pop = (): void => {
    open.pop()
    quotes.pop()
  }
  // Here-documents whose bodies start on the next line: delimiter, tabs
  // stripped, quoted, and the depth of the code that holds them.
  const due: [string, boolean, boolean, number][] = []
  let closed = -1
  // In shell code: what came last (the code's start, a space or line break,
  // an operator or a word), and whether a command can start here.
  let last: 'start' | 'space' | 'op' | 'word' = states !== undefined ? 'start' : 'word'
  let command = states !== undefined
  let i = start
  // A whole-text scan (`states` given) reads the text as shell code from its
  // start (W) and records each position's quote: positions a step passes over
  // get `fill`, the quote where the step began or, for a comment, a
  // here-document's body or an escaped character, literal.
  let recorded = start
  let fill = 0
  const toEnd = (): { end: number; closed: number } => {
    if (states !== undefined) {
      states.fill(fill, recorded, i)
      states.fill(UNKNOWN, i)
    }
    return { end: text.length, closed }
  }
  // Past a comment at `i`, to its line break; false when the scan stops. A
  // comment to the text's end is all literal to a whole-text scan; after one
  // that ends in a backslash (bash 3.2 continues it on the next line), it
  // cannot tell from that next line on.
  const pastComment = (): boolean => {
    const e = commentEnd(text, i)
    if (e === -1 && text.includes('\n', i)) {
      if (states !== undefined) {
        fill = LITERAL
        i = text.indexOf('\n', i) + 1
      }
      return false
    }
    if (e === -1 && states === undefined) return false
    fill = LITERAL
    i = e === -1 ? text.length : e
    return true
  }
  while (i < text.length) {
    if (states !== undefined) {
      states.fill(fill, recorded, i)
      fill = quotes[quotes.length - 1] ?? 0
      states[i] = fill
      recorded = i + 1
    }
    if (closed === -1 && inside !== '' && open.length === 0) closed = i - 1
    const c = text[i] as string
    const top = open[open.length - 1] ?? ''
    if (top === "'") {
      if (c === "'") {
        pop()
        last = 'word'
        command = false
      }
      i += 1
      continue
    }
    if (c === '\n' && due.length > 0 && (top === 'S' || top === '(' || top === 'l' || top === 'c' || top === 'W')) {
      if (due.some(h => h[3] !== open.length)) return toEnd()
      const body = hereDocumentsEnd(text, i + 1, due)
      if (body.end === -1) {
        // A whole-text scan cannot tell past the part that is a body to all.
        if (states !== undefined && body.known > i + 1) {
          fill = LITERAL
          i = body.known
        }
        return toEnd()
      }
      fill = LITERAL
      i = body.end
      due.length = 0
      last = 'space'
      command = true
      continue
    }
    if (top === 'c') {
      // Between a case statement's arms: `esac`, a comment or a pattern.
      if (c === ' ' || c === '\t' || c === '\n') {
        last = 'space'
        i += 1
      } else if (afterContinuations(text, i) > i) {
        i = afterContinuations(text, i)
      } else if (c === '#') {
        if (last !== 'space') return toEnd()
        if (!pastComment()) return toEnd()
      } else if (esacAt(text, i)) {
        if (due.length > 0) return toEnd()
        pop()
        last = 'word'
        command = false
        i += 4
      } else if (c === ')') {
        return toEnd()
      } else {
        open[open.length - 1] = 'p'
        last = 'op'
        if (c === '(') i += 1
      }
      continue
    }
    if (c === '\\') {
      const after = afterContinuations(text, i)
      if (after > i) {
        // A line continuation leaves no trace.
        i = after
        continue
      }
      i += 2
      fill = LITERAL
      last = 'word'
      command = false
      continue
    }
    if (top === 'a' || top === '`') {
      if (c === (top === 'a' ? "'" : '`')) {
        pop()
        last = 'word'
        command = false
      }
      i += 1
      continue
    }
    // The next character the shell reads, past any line continuations.
    const n = afterContinuations(text, i + 1)
    const d = text[n] ?? ''
    if (c === '$' && d === "'" && top !== '"') {
      push('a')
      i = n + 1
      continue
    }
    if (c === '$' && d === '(') {
      const e = afterContinuations(text, n + 1)
      if (text[e] === '(') {
        push('A')
        i = e + 1
      } else {
        push('S')
        i = n + 1
        last = 'start'
        command = true
      }
      continue
    }
    if (c === '$' && d === '{') {
      push('{')
      i = n + 1
      // zsh's flags: `${(s:}:)name}`.
      if (text[i] === '(') {
        push('G')
        i += 1
      }
      continue
    }
    if (top === '"') {
      if (c === '"') {
        pop()
        last = 'word'
        command = false
      } else if (c === '`') {
        push('`')
      }
      i += 1
      continue
    }
    if (top === '{') {
      if (c === '}') {
        pop()
        last = 'word'
        command = false
      } else if (c === '{' || c === '"' || c === "'" || c === '`') {
        // zsh counts the braces inside `${…}`.
        push(c)
      }
      i += 1
      continue
    }
    if (top === 'A' || top === 'B' || top === 'P') {
      // bash 5 reads a `#` that starts a word here as a comment; other shells
      // as text (`16#ff` is a number to all).
      if (c === '#' && !/[A-Za-z0-9_]/.test(text[i - 1] ?? '')) return toEnd()
      if (c === ')' && top !== 'P') {
        if (d !== ')') return toEnd()
        pop()
        last = top === 'A' ? 'word' : 'op'
        command = false
        i = n + 1
        continue
      }
      if (c === '(') push('P')
      else if (c === ')') pop()
      else if (c === '"' || c === "'" || c === '`') push(c)
      i += 1
      continue
    }
    if (top === 'G') {
      if (c === ')') {
        pop()
        last = 'word'
        command = false
      } else if (c === '(') {
        push('G')
      } else if (c === '"' || c === "'" || c === '`') {
        push(c)
      }
      i += 1
      continue
    }
    if (top === 'R') {
      // An array's elements, up to its `)`: words, quotes, expansions and
      // comments. A `#` right after its `(` (bash 3.2 reads no comment there)
      // or an operator, an operator itself, or a line break while a
      // here-document's body is due is not read.
      if (c === ')') {
        pop()
        last = 'word'
        command = false
      } else if (c === ' ' || c === '\t' || c === '\n') {
        if (c === '\n' && due.length > 0) return toEnd()
        last = 'space'
      } else if (c === '#' && last !== 'word') {
        if (last !== 'space') return toEnd()
        if (!pastComment()) return toEnd()
        continue
      } else if (';&|<>'.includes(c)) {
        return toEnd()
      } else {
        // In zsh, a parenthesis here opens a glob group.
        if (c === '(') push('G')
        else if (c === '"' || c === "'" || c === '`') push(c)
        last = 'word'
      }
      i += 1
      continue
    }
    if (top === 'p') {
      // A case pattern, up to its `)`.
      if (c === ')') {
        open[open.length - 1] = 'l'
        last = 'op'
        command = true
      } else if (c === '|') {
        last = 'op'
      } else if (c === ' ' || c === '\t') {
        last = 'space'
      } else if ('(\n;&<>'.includes(c) || (c === '#' && last !== 'word')) {
        return toEnd()
      } else {
        if (c === '"' || c === "'" || c === '`') push(c)
        last = 'word'
      }
      i += 1
      continue
    }
    if (top === 'S' || top === '(' || top === 'l' || top === 'W') {
      if (c === ' ' || c === '\t' || c === '\n') {
        last = 'space'
        if (c === '\n') command = true
        i += 1
        continue
      }
      if (c === '#' && last !== 'word') {
        // After an operator, bash 3.2 reads no comment; other shells do.
        if (last === 'op') return toEnd()
        if (!pastComment()) return toEnd()
        continue
      }
      if (c === '<' && d === '<') {
        const e = afterContinuations(text, n + 1)
        // A redirection's operand is not where a command starts.
        command = false
        if (text[e] === '<') {
          // A here-string.
          i = e + 1
          last = 'op'
          continue
        }
        const h = hereDelimiter(text, e)
        if (h === null) return toEnd()
        due.push([h.delimiter, h.tabs, h.quoted, open.length])
        i = h.end
        last = 'word'
        continue
      }
      if ((c === '<' || c === '>') && d === '(') {
        push('S')
        i = n + 1
        last = 'start'
        command = true
        continue
      }
      if (c === '(') {
        if (last !== 'word' && d === '(') {
          push('B')
          i = n + 1
          continue
        }
        // After `name=`, an array's elements; after another word, a glob
        // group (or a function's `()`); else a subshell.
        const kind = last !== 'word' ? '(' : charBefore(text, i) === '=' ? 'R' : 'G'
        push(kind)
        if (kind !== 'G') {
          last = 'start'
          command = kind === '('
        }
        i += 1
        continue
      }
      if (c === ')' && top === 'W') {
        // A stray `)` in the text itself.
        last = 'op'
        command = true
        i += 1
        continue
      }
      if (c === ')') {
        if (top === 'l' || due.length > 0) return toEnd()
        pop()
        last = top === '(' ? 'op' : 'word'
        command = false
        i += 1
        continue
      }
      if (c === ';' && top === 'l' && (d === ';' || d === '&' || d === '|')) {
        // An arm ends: `;;`, `;;&`, `;&` or zsh's `;|`.
        i = n + 1
        if (d === ';' && text[afterContinuations(text, i)] === '&') i = afterContinuations(text, i) + 1
        open[open.length - 1] = 'c'
        last = 'op'
        continue
      }
      if (c === ';' || c === '&' || c === '|' || c === '<' || c === '>') {
        last = 'op'
        // A redirection's operand (`<`, `>`, `>|`, `>&`, `<&`) is not where a
        // command starts.
        command = c !== '<' && c !== '>'
        i = !command && (d === '|' || d === '&') ? n + 1 : i + 1
        continue
      }
      if (last !== 'word' && top === 'l' && command && esacAt(text, i)) {
        if (due.length > 0) return toEnd()
        pop()
        last = 'word'
        command = false
        i += 4
        continue
      }
      if (last !== 'word' && c === 'c') {
        const k = caseStatementAt(text, i)
        if (k === text.length) return toEnd()
        if (k !== -1) {
          push('c')
          i = k
          last = 'word'
          command = false
          continue
        }
      }
      if (c === '"' || c === "'" || c === '`') push(c)
      last = 'word'
      command = false
      i += 1
      continue
    }
    // The argument itself, outside every quote and expansion.
    if (c === ' ' || c === '\t' || c === '\n' || (c === '\r' && text[i + 1] === '\n')) break
    if ((c === '<' || c === '>') && d === '(') {
      push('S')
      i = n + 1
      last = 'start'
      command = true
      continue
    }
    if (c === ';' || c === '|' || c === '&' || c === '<' || c === '>' || c === ')') break
    if (c === '(') push('G')
    else if (c === '"' || c === "'" || c === '`') push(c)
    i += 1
  }
  if (states !== undefined) states.fill(fill, recorded)
  if (closed === -1 && inside !== '' && open.length === 0) closed = i - 1
  return { end: Math.min(i, text.length), closed }
}

// Whether a long option's name is a credential's: one with a credential word
// for its last part.
const credentialOption = (name: string): boolean =>
  isSensitiveKey(name) && forms(nameParts(name).pop() ?? '').some(isCredentialWord)

function redactFlagValues(text: string, stats: SanitizeStats): string {
  FLAG_VALUE.lastIndex = 0
  const stateAt = quoteReader(text)
  let out = ''
  let copied = 0
  for (let m = FLAG_VALUE.exec(text); m !== null; m = FLAG_VALUE.exec(text)) {
    const raw = m[2] as string
    let start = m.index + m[0].length
    let between = m[3] as string
    const own = joined(raw)
    const credential = credentialOption(own)
    // A gap of line continuations alone joins what follows to the word: a
    // quoted or escaped part there, read as the shell passes it, may go on
    // with the name. Where that longer word names no credential (`--password\`
    // and `"-stdin"`), nothing goes; where only it names one (`--api\` and
    // `"-key"`), its next argument goes (redactOptionWords); where both do
    // (`"--pass\` and `"word`), the part the continuation joins and the next
    // argument both go.
    let next = false
    if (!/[ \t]/.test(m[4] as string) && /["'\\$]/.test(text[start] ?? '')) {
      const at = optionStart(text, m.index + (m[1] as string).length)
      const word = at === -1 ? undefined : wordName(text, at, stateAt)
      if (word !== undefined && word.name.length > own.length && word.name.startsWith(own)) {
        const whole = word.word.value === -1 ? credentialOption(word.name) : isSensitiveKey(word.name)
        if (!whole && /^[-_.]/.test(word.name.slice(own.length))) continue
        if (whole && !credential) continue
        next = whole && word.word.value === -1
      }
    }
    if (credential) {
      // A gap of line continuations alone joins what follows to the name.
      if (!/[ \t]/.test(m[4] as string) && /^[-_.]/.test(text[start] ?? '')) continue
    } else {
      // With a continuation in the name taken for the gap before a value
      // (`--password\` and `VALUE`), the part before it may be a credential
      // option's: the longest such part, before a letter or digit.
      let cut = -1
      let part = ''
      for (let k = 0; k < raw.length; ) {
        const after = afterContinuations(raw, k)
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
      between = ''
    }
    // A value in a quote still open where it starts runs through that quote's
    // end, and the quote is put back after the marker.
    const value = farthest(text, start, seedsAt(text, start, stateAt, m, between), false)
    let { end, close } = value
    let first = !wholeMarker(text.slice(start, end), close)
    let second: { at: number; end: number; close: string } | undefined
    if (next) {
      // The next argument starts past the joined part as its nearest reading
      // ends; where a farther reading runs into that argument, both go under
      // one marker.
      const at = nextArgument(text, value.near)
      const read = at === -1 ? undefined : farthest(text, at, seedsAt(text, at, stateAt), false)
      if (read !== undefined && !wholeMarker(text.slice(at, read.end), read.close)) {
        if (at >= end) {
          second = { at, end: read.end, close: read.close }
        } else {
          if (read.end > end) {
            end = read.end
            close = read.close
          }
          first = true
        }
      }
    }
    if (first) {
      out += text.slice(copied, start) + MARKER + close
      copied = end
      stats.redactions += 1
    }
    if (second !== undefined) {
      out += text.slice(copied, second.at) + MARKER + second.close
      copied = second.end
      stats.redactions += 1
    }
    if (first || second !== undefined) FLAG_VALUE.lastIndex = copied
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

// Where the argument after an option that ends at `i` starts, past spaces,
// tabs and line continuations; -1 when none follows on that line, or the next
// word is another long option.
function nextArgument(text: string, i: number): number {
  let k = i
  for (;;) {
    if (text[k] === ' ' || text[k] === '\t') k += 1
    else if (afterContinuations(text, k) > k) k = afterContinuations(text, k)
    else break
  }
  return k === i || k >= text.length || /\s/.test(text[k] as string) || text.startsWith('--', k) ? -1 : k
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

// A long option's word from `i` as the shell passes it: its quotes and
// backslashes removed, ANSI-C escapes decoded and line continuations
// dropped, up to its first `=` or its end. Returns that text, where it ends,
// where the value after an `=` starts (-1: no `=`) and the quote open there;
// null when, before then, the word holds an expansion or a character no
// option name has. In a command string another shell runs (`nested`), `\"`
// quotes for that shell, and the value after an `=` it holds is in the
// string's quote.
function optionWord(text: string, i: number, nested = false): OptionWord | null {
  let name = ''
  let quote = ''
  while (name.length <= 200) {
    const c = text[i]
    if (c === undefined) return quote === '' ? { name, end: i, value: -1, inside: '' } : null
    let ch = c
    let step = 1
    if (nested && c === '\\' && text[i + 1] === '"' && (quote === '' || quote === 'n')) {
      // In a command string another shell runs, `\"` quotes for that shell.
      quote = quote === '' ? 'n' : ''
      i += 2
      continue
    }
    if (quote === 'n') {
      if (c === '\\' || c === '$' || c === '`') return null
    } else if (quote === "'" || quote === 'a') {
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
      const after = afterContinuations(text, i)
      if (after > i) {
        i = after
        continue
      }
      if (quote === '"' && !/[\\"$`]/.test(text[i + 1] ?? '')) {
        ch = '\\'
      } else {
        ch = text[i + 1] ?? ''
        step = 2
      }
    } else if (quote === '"') {
      if (c === '"') {
        quote = ''
        i += 1
        continue
      }
      if (c === '$' || c === '`') return null
    } else if (c === "'" || c === '"') {
      quote = c
      i += 1
      continue
    } else if (c === '$' && text[i + 1] === "'") {
      quote = 'a'
      i += 2
      continue
    } else if (/[\s;&|<>()]/.test(c)) {
      return { name, end: i, value: -1, inside: '' }
    }
    if (ch === '=') return { name, end: i, value: i + step, inside: quote === 'n' ? '"' : quote }
    if (!/^[A-Za-z0-9_.-]$/.test(ch)) return null
    name += ch
    i += step
  }
  return null
}

type OptionWord = { name: string; end: number; value: number; inside: string }

// A word that holds `\"` before anything no option's name can hold, from
// where it starts.
const ESCAPED_QUOTE = /(?:[A-Za-z0-9_.=-]|["']|\\[^"]){0,400}\\"/y

// The option or name a word at `at` is, as the shell passes it (see
// optionWord): `--name`, or before an `=` also `-name`, `-Dname` or a shell
// variable's name (`--pass"word"=…`, `"DB_PASS"WORD=…`). It is read as the
// scan finds the text there and, where that gives no credential name, as a
// word of a command string another shell runs (`\"--pass\"word`, after prose
// the scan misreads): `nested` says whether the word was read as one, and
// `scanned` whether the scan found it in such a string.
function wordName(text: string, at: number, stateAt: (p: number) => number): { word: OptionWord; name: string; nested: boolean; scanned: boolean } | undefined {
  const scanned = stateAt(at) === 1
  let found: { word: OptionWord; name: string; nested: boolean; scanned: boolean } | undefined
  for (const nested of scanned ? [true] : [false, true]) {
    if (nested && !scanned) {
      ESCAPED_QUOTE.lastIndex = at
      if (!ESCAPED_QUOTE.test(text)) break
    }
    const word = optionWord(text, at, nested)
    if (word === null) continue
    const m = (word.value === -1 ? /^--([A-Za-z][A-Za-z0-9_.-]*)$/ : /^(?:--?([A-Za-z][A-Za-z0-9_.-]*)|([A-Za-z_][A-Za-z0-9_]*))$/).exec(word.name)
    const name = m?.[1] ?? m?.[2]
    if (name === undefined) continue
    found ??= { word, name, nested, scanned }
    if (isSensitiveKey(name)) return { word, name, nested, scanned }
  }
  return found
}

// Where a word starts that the rules above may see only in pieces: at a
// quote, a backslash, `$'` or a dash, or a name that a quote, a backslash or
// `$'` comes in (`DB_PASS"WORD"`).
const OPTION_WORD = /(?:^|[\s;&|(])(?=["'\\$-]|[A-Za-z_][A-Za-z0-9_.-]*["'\\$])/g

// A word the shell passes as a long option once its quotes, backslashes,
// ANSI-C escapes and line continuations are read (`"--pass"word`,
// `--pass''word`, `--pa\ssword`, `-\-password`, `$'--pa\x73sword'`,
// `"--pass"word"=…"`), or as a name before an `=` (`"DB_PASS"WORD=…`,
// `-Dssl.pass"word"=…`), which the rules above see only in pieces: with `=`,
// its value as a flag's or an assignment's, from inside the quote the `=` is
// in; else, if its name's last part is a credential word, the next shell
// argument.
function redactOptionWords(text: string, stats: SanitizeStats): string {
  OPTION_WORD.lastIndex = 0
  const stateAt = quoteReader(text)
  let out = ''
  let copied = 0
  for (let m = OPTION_WORD.exec(text); m !== null; m = OPTION_WORD.exec(text)) {
    const at = m.index + m[0].length
    OPTION_WORD.lastIndex = at + 1
    // A line continuation's line break starts no word.
    if (m[0] === '\n' && (text[m.index - 1] === '\\' || (text[m.index - 1] === '\r' && text[m.index - 2] === '\\'))) continue
    // The word as the shell passes it, in a command string another shell runs
    // as that string's word (see wordName).
    const option = wordName(text, at, stateAt)
    if (option === undefined || !/["'\\]/.test(joined(text.slice(at, option.word.end))) || !isSensitiveKey(option.name)) continue
    const { word, name, nested, scanned } = option
    // A name the rules above see whole (a quote at most before and after
    // it) is theirs.
    if (word.value !== -1 && !word.name.startsWith('--') && /^\$?["']?-?[A-Za-z_][A-Za-z0-9_.-]*["']?$/.test(text.slice(at, word.value - 1))) continue
    let start: number
    let end: number
    let close = ''
    if (word.value !== -1) {
      start = word.value
      const read = farthest(text, start, seedsAt(text, start, stateAt, undefined, '', nested && !scanned ? '' : word.inside), true)
      end = read.end
      close = read.close
    } else {
      if (!credentialOption(name)) continue
      start = word.end
      for (;;) {
        if (text[start] === ' ' || text[start] === '\t') start += 1
        else if (afterContinuations(text, start) > start) start = afterContinuations(text, start)
        else break
      }
      if (start === word.end || start >= text.length || /\s/.test(text[start] as string) || text.startsWith('--', start)) continue
      // A gap of line continuations alone joins what follows to the name.
      if (!/[ \t]/.test(text.slice(word.end, start)) && /^[-_.]/.test(text[start] as string)) continue
      const read = farthest(text, start, seedsAt(text, start, stateAt, undefined, '', scanned ? '"' : ''), false)
      end = read.end
      close = read.close
      if (wholeMarker(text.slice(start, end), close)) continue
    }
    const value = text.slice(start, end)
    if (value.trim() === '') continue
    out += text.slice(copied, start) + MARKER + close
    copied = end
    OPTION_WORD.lastIndex = end
    if (value !== MARKER + close) stats.redactions += 1
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
