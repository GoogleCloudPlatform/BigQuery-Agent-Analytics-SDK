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

// The quote open where a match's value starts: one just before the match
// (`"--password=…`, `'--password …`) unless it is escaped or follows a word's
// piece, which it closes (`"--api"-key=…`), then as the quotes in `between`,
// the text between the name and the value, leave it: they can close it
// (`"--password"=…`) or open one (`--password"=…`).
function quoteAtValue(text: string, m: RegExpExecArray, between: string): string {
  let quote = /^["'`]$/.test(m[1] as string) && !/[\\A-Za-z0-9_."'`]/.test(text[m.index - 1] ?? '') ? (m[1] as string) : ''
  for (let k = 0; k < between.length; k += 1) {
    const q = between[k] as string
    if (q === '\\' && quote !== "'") k += 1
    else if (q === '"' || q === "'") quote = quote === '' ? q : quote === q ? '' : quote
  }
  return quote
}

// A name as the shell reads it, its line continuations removed.
const joined = (name: string): string => name.replace(/\\\r?\n/g, '')

function redactNamedValues(text: string, stats: SanitizeStats): string {
  NAMED_VALUE.lastIndex = 0
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
      if (/^["'`$]/.test(text[start] ?? '')) end = Math.max(end, shellArgument(text, start, '').end)
    } else if (m[3] === '=' && /[^ \t]=$/.test(m[0])) {
      const nameEnd = m[0].indexOf(name, (m[1] as string).length) + name.length
      const inside = quoteAtValue(text, m, m[0].slice(nameEnd, separator))
      const word = shellArgument(text, start, inside)
      end = lineEnd(text, start, word.end)
      if (word.closed !== -1) close = inside
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
// continuations (`--password \` and the value on the next line; in the
// option's name too, `--pass\` and `word VALUE`): the whole shell argument.
// The option's name is a credential's and its last part a credential word,
// so `--password-stdin host`, `--token-file path` and `--max-tokens 1024`
// keep theirs. A value that starts with `--` is the next option, not a value.
const FLAG_VALUE = /(^|[^A-Za-z0-9_.-])-(?:\\\r?\n)*-(?:\\\r?\n)*([A-Za-z](?:(?:\\\r?\n)*[A-Za-z0-9_.-]){0,99})(["']?)(?:[ \t]|\\\r?\n)+(?![ \t]|\\\r?\n)(?!--)(?=\S)/g
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
// another line: bash alone ends a body at `EOF)`). -1 when one has none, or a
// line of an unquoted body ends in a backslash: a continuation, which joins
// the next line before it is compared.
function hereDocumentsEnd(text: string, from: number, due: readonly [string, boolean, boolean, number][]): number {
  let at = from
  for (const [delimiter, tabs, quoted] of due) {
    for (;;) {
      if (at >= text.length) return -1
      const nl = text.indexOf('\n', at)
      const line = text.slice(at, nl === -1 ? text.length : nl)
      if (!quoted && line.endsWith('\\')) return -1
      at = nl === -1 ? text.length : nl + 1
      if ((tabs ? line.replace(/^\t+/, '') : line) === delimiter) break
      if (nl === -1) return -1
    }
  }
  return at
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
function shellArgument(text: string, start: number, inside: string): { end: number; closed: number } {
  // What is open, innermost last: ' " a(nsi) ` {, $((…)) as A, ((…)) as B, a
  // parenthesis inside them as P, a glob group as G, an array's elements
  // (`name=(…)`) as R, $( <( >( as S, a subshell as (, and a case
  // statement: c between its arms, p in a pattern, l in an arm's commands.
  const open: string[] = inside === '' ? [] : [inside]
  // Here-documents whose bodies start on the next line: delimiter, tabs
  // stripped, quoted, and the depth of the code that holds them.
  const due: [string, boolean, boolean, number][] = []
  let closed = -1
  // In shell code: what came last (the code's start, a space or line break,
  // an operator or a word), and whether a command can start here.
  let last: 'start' | 'space' | 'op' | 'word' = 'word'
  let command = false
  let i = start
  const toEnd = (): { end: number; closed: number } => ({ end: text.length, closed })
  while (i < text.length) {
    if (closed === -1 && inside !== '' && open.length === 0) closed = i - 1
    const c = text[i] as string
    const top = open[open.length - 1] ?? ''
    if (top === "'") {
      if (c === "'") {
        open.pop()
        last = 'word'
        command = false
      }
      i += 1
      continue
    }
    if (c === '\n' && due.length > 0 && (top === 'S' || top === '(' || top === 'l' || top === 'c')) {
      if (due.some(h => h[3] !== open.length)) return toEnd()
      i = hereDocumentsEnd(text, i + 1, due)
      if (i === -1) return toEnd()
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
        i = commentEnd(text, i)
        if (i === -1) return toEnd()
      } else if (esacAt(text, i)) {
        if (due.length > 0) return toEnd()
        open.pop()
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
      last = 'word'
      command = false
      continue
    }
    if (top === 'a' || top === '`') {
      if (c === (top === 'a' ? "'" : '`')) {
        open.pop()
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
      open.push('a')
      i = n + 1
      continue
    }
    if (c === '$' && d === '(') {
      const e = afterContinuations(text, n + 1)
      if (text[e] === '(') {
        open.push('A')
        i = e + 1
      } else {
        open.push('S')
        i = n + 1
        last = 'start'
        command = true
      }
      continue
    }
    if (c === '$' && d === '{') {
      open.push('{')
      i = n + 1
      // zsh's flags: `${(s:}:)name}`.
      if (text[i] === '(') {
        open.push('G')
        i += 1
      }
      continue
    }
    if (top === '"') {
      if (c === '"') {
        open.pop()
        last = 'word'
        command = false
      } else if (c === '`') {
        open.push('`')
      }
      i += 1
      continue
    }
    if (top === '{') {
      if (c === '}') {
        open.pop()
        last = 'word'
        command = false
      } else if (c === '{' || c === '"' || c === "'" || c === '`') {
        // zsh counts the braces inside `${…}`.
        open.push(c)
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
        open.pop()
        last = top === 'A' ? 'word' : 'op'
        command = false
        i = n + 1
        continue
      }
      if (c === '(') open.push('P')
      else if (c === ')') open.pop()
      else if (c === '"' || c === "'" || c === '`') open.push(c)
      i += 1
      continue
    }
    if (top === 'G') {
      if (c === ')') {
        open.pop()
        last = 'word'
        command = false
      } else if (c === '(') {
        open.push('G')
      } else if (c === '"' || c === "'" || c === '`') {
        open.push(c)
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
        open.pop()
        last = 'word'
        command = false
      } else if (c === ' ' || c === '\t' || c === '\n') {
        if (c === '\n' && due.length > 0) return toEnd()
        last = 'space'
      } else if (c === '#' && last !== 'word') {
        if (last !== 'space') return toEnd()
        i = commentEnd(text, i)
        if (i === -1) return toEnd()
        continue
      } else if (';&|<>'.includes(c)) {
        return toEnd()
      } else {
        // In zsh, a parenthesis here opens a glob group.
        if (c === '(') open.push('G')
        else if (c === '"' || c === "'" || c === '`') open.push(c)
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
        if (c === '"' || c === "'" || c === '`') open.push(c)
        last = 'word'
      }
      i += 1
      continue
    }
    if (top === 'S' || top === '(' || top === 'l') {
      if (c === ' ' || c === '\t' || c === '\n') {
        last = 'space'
        if (c === '\n') command = true
        i += 1
        continue
      }
      if (c === '#' && last !== 'word') {
        // After an operator, bash 3.2 reads no comment; other shells do.
        if (last === 'op') return toEnd()
        i = commentEnd(text, i)
        if (i === -1) return toEnd()
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
        open.push('S')
        i = n + 1
        last = 'start'
        command = true
        continue
      }
      if (c === '(') {
        if (last !== 'word' && d === '(') {
          open.push('B')
          i = n + 1
          continue
        }
        // After `name=`, an array's elements; after another word, a glob
        // group (or a function's `()`); else a subshell.
        const kind = last !== 'word' ? '(' : charBefore(text, i) === '=' ? 'R' : 'G'
        open.push(kind)
        if (kind !== 'G') {
          last = 'start'
          command = kind === '('
        }
        i += 1
        continue
      }
      if (c === ')') {
        if (top === 'l' || due.length > 0) return toEnd()
        open.pop()
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
        open.pop()
        last = 'word'
        command = false
        i += 4
        continue
      }
      if (last !== 'word' && c === 'c') {
        const k = caseStatementAt(text, i)
        if (k === text.length) return toEnd()
        if (k !== -1) {
          open.push('c')
          i = k
          last = 'word'
          command = false
          continue
        }
      }
      if (c === '"' || c === "'" || c === '`') open.push(c)
      last = 'word'
      command = false
      i += 1
      continue
    }
    // The argument itself, outside every quote and expansion.
    if (c === ' ' || c === '\t' || c === '\n' || (c === '\r' && text[i + 1] === '\n')) break
    if ((c === '<' || c === '>') && d === '(') {
      open.push('S')
      i = n + 1
      last = 'start'
      command = true
      continue
    }
    if (c === ';' || c === '|' || c === '&' || c === '<' || c === '>' || c === ')') break
    if (c === '(') open.push('G')
    else if (c === '"' || c === "'" || c === '`') open.push(c)
    i += 1
  }
  if (closed === -1 && inside !== '' && open.length === 0) closed = i - 1
  return { end: Math.min(i, text.length), closed }
}

function redactFlagValues(text: string, stats: SanitizeStats): string {
  FLAG_VALUE.lastIndex = 0
  let out = ''
  let copied = 0
  for (let m = FLAG_VALUE.exec(text); m !== null; m = FLAG_VALUE.exec(text)) {
    const name = joined(m[2] as string)
    const last = nameParts(name).pop() ?? ''
    if (!isSensitiveKey(name) || !forms(last).some(isCredentialWord)) continue
    const start = m.index + m[0].length
    // An option inside a quote still open where its value starts: the value
    // runs through that quote's end, and the quote is put back after the
    // marker.
    const inside = quoteAtValue(text, m, m[3] as string)
    const { end, closed } = shellArgument(text, start, inside)
    if (WHOLE_MARKER.test(text.slice(start, closed === end - 1 ? closed : end))) continue
    out += text.slice(copied, start) + MARKER + (closed === -1 ? '' : inside)
    copied = end
    FLAG_VALUE.lastIndex = end
    stats.redactions += 1
  }
  return copied === 0 ? text : out + text.slice(copied)
}

// A word the shell reads as a long option once its quotes and backslashes are
// removed (`"--pass"word`, `--pass''word`, `--pa\ssword`, `-\-password`,
// `$'--pass'word`), which the rules above see only in pieces: with `=`, its
// value as a flag's or an assignment's; else, if its name's last part is a
// credential word, the next shell argument.
const QUOTED_OPTION = /(^|[\s;&|(])((?=["'\\$-])(?:[-A-Za-z0-9_.]|\\[-A-Za-z0-9_.]|\$?'[-A-Za-z0-9_.]*'|"[-A-Za-z0-9_.]*"|\\\r?\n){2,300})(?=[\s=]|$)/g

function redactQuotedOptions(text: string, stats: SanitizeStats): string {
  QUOTED_OPTION.lastIndex = 0
  let out = ''
  let copied = 0
  for (let m = QUOTED_OPTION.exec(text); m !== null; m = QUOTED_OPTION.exec(text)) {
    const word = joined(m[2] as string)
    if (!/["'\\]/.test(word)) continue
    const name = /^--([A-Za-z][A-Za-z0-9_.-]*)$/.exec(word.replace(/\$'|["']|\\(?=.)/g, ''))?.[1]
    if (name === undefined || !isSensitiveKey(name)) continue
    const after = m.index + m[0].length
    let start = after + 1
    let end: number
    if (text[after] === '=') {
      end = lineEnd(text, start, shellArgument(text, start, '').end)
    } else {
      if (!forms(nameParts(name).pop() ?? '').some(isCredentialWord)) continue
      start = after
      for (;;) {
        if (text[start] === ' ' || text[start] === '\t') start += 1
        else if (afterContinuations(text, start) > start) start = afterContinuations(text, start)
        else break
      }
      if (start === after || start >= text.length || /\s/.test(text[start] as string) || text.startsWith('--', start)) continue
      end = shellArgument(text, start, '').end
      if (WHOLE_MARKER.test(text.slice(start, end))) continue
    }
    const value = text.slice(start, end)
    if (value.trim() === '') continue
    out += text.slice(copied, start) + MARKER
    copied = end
    QUOTED_OPTION.lastIndex = end
    if (value !== MARKER) stats.redactions += 1
  }
  return copied === 0 ? text : out + text.slice(copied)
}


// The free-text rules: credential shapes, URLs, credential-named values, then
// credential-named command-line options, then options whose names are quoted
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
  return redactQuotedOptions(redactFlagValues(redactNamedValues(out, stats), stats), stats)
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
