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

// The local dead-letter files: JSONL, one row per line, each line keeping
// the full insertAll row so a backfill can send it again unchanged, and a
// last line, the completion record, written after every row.
//
// `$.fs` has no append, no rename and no delete, its `write` is not atomic
// (another process can read a file part-written), and it reads and writes at
// most 4 MiB per file, counted in UTF-8 bytes (the file on disk). So every
// failure writes a new, immutable file (no read-modify-write races between
// sessions), files are kept to 3 MiB of UTF-8, a backfill reads only files
// whose completion record matches every line above it, and one that
// delivered a file truncates it to zero bytes. Each table's files live in a
// subdirectory of their own (config.ts `deadLetterPartition`).

import { type BqaaRow, fitRow, utf8Length } from './rows.ts'
import { redactDiagnostic } from './sanitize.ts'

export type DeadLetterKind =
  // Queued rows written out at session end, or under queue pressure while
  // BigQuery was unreachable: never refused, sent by the next auto-backfill.
  | 'spill'
  // Retries ran out on transient errors: sent by the next auto-backfill.
  | 'retry'
  // BigQuery refused them (access denied, an invalid row or request): sent
  // again only by an explicit /bqaa-backfill. Rows for a dataset or table
  // BigQuery cannot find are not refused: they wait as `retry`.
  | 'rejected'

export type DeadLetterLine = {
  v: 1
  kind: DeadLetterKind
  reason: string
  attempts: number
  failed_at: string
  // The table the row was meant for, `project.dataset.table`: a replay sends
  // a line only to that table.
  table: string
  row: BqaaRow
  // For a row with no event_id (from a file another writer made): how the
  // refused and parked counts know it, kept as a replay rewrites the line.
  key?: string
}

// What `$.fs.read` and `$.fs.write` take, and what this mod writes: a file
// a backfill rewrites (with longer reasons, say) still reads back.
export const HOST_FILE_BYTES = 4 * 1024 * 1024
export const MAX_FILE_BYTES = 3 * 1024 * 1024
// Room for the completion record, under MAX_FILE_BYTES.
const RECORD_BYTES = 128
const BODY_BYTES = MAX_FILE_BYTES - RECORD_BYTES
const REASON_CHARS = 1000

export const FILE_PATTERN = /^(spill|retry|rejected)-[A-Za-z0-9._-]+\.jsonl$/

// One line of a dead-letter file: the reason redacted and held to
// REASON_CHARS, and a row too large for a file on its own cut to fit.
export function lineText(line: DeadLetterLine): string {
  const clean: DeadLetterLine = { ...line, reason: redactDiagnostic(line.reason, REASON_CHARS) }
  const text = JSON.stringify(clean)
  if (utf8Length(text) < BODY_BYTES) return text
  const overhead = utf8Length(JSON.stringify({ ...clean, row: null }))
  return JSON.stringify({ ...clean, row: fitRow(clean.row, BODY_BYTES - overhead - 16) })
}

// Groups JSONL lines, each of which fits on its own, into file texts of at
// most `maxBytes` of UTF-8.
export function packLines(lines: readonly string[], maxBytes = MAX_FILE_BYTES): string[] {
  const files: string[] = []
  let current: string[] = []
  let bytes = 0
  for (const line of lines) {
    const size = utf8Length(line) + 1
    if (current.length > 0 && bytes + size > maxBytes) {
      files.push(`${current.join('\n')}\n`)
      current = []
      bytes = 0
    }
    current.push(line)
    bytes += size
  }
  if (current.length > 0) files.push(`${current.join('\n')}\n`)
  return files
}

// FNV-1a, 32 bits, over UTF-16 code units: a checksum, not a secret.
const fnv1a = (text: string): string => {
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(16).padStart(8, '0')
}

const lineCount = (body: string): number => body.split('\n').length - 1

// A file's text: its lines (`body`, each ending in a newline), then the
// completion record, how many lines come before it and their checksum, and,
// for a file of what a replay left, the file it came from (`parent`).
export const sealFile = (body: string, parent?: string): string =>
  `${body}${JSON.stringify({ v: 1, complete: true, lines: lineCount(body), fnv1a: fnv1a(body), ...(parent === undefined ? {} : { parent }) })}\n`

// Dead-letter file texts for JSONL lines: packed under MAX_FILE_BYTES with
// room for each file's completion record, then sealed.
export const packFiles = (lines: readonly string[], parent?: string): string[] => packLines(lines, BODY_BYTES).map(body => sealFile(body, parent))

// Several files when the lines pass MAX_FILE_BYTES.
export const encodeLines = (lines: readonly DeadLetterLine[]): string[] => packFiles(lines.map(lineText))

// The bodies `packFiles` seals, for a writer that counts each file's lines.
export const packBodies = (lines: readonly string[]): string[] => packLines(lines, BODY_BYTES)

export const fileName = (kind: DeadLetterKind, atMs: number, session: string | null, seq: number, writer?: string): string => {
  const stamp = new Date(atMs).toISOString().replace(/[-:]/g, '').replace('.', '')
  const who = (session ?? 'nosession').replace(/[^A-Za-z0-9]/g, '').slice(0, 12) || 'nosession'
  // The writer's own name, where given: two writers of one session (one
  // taking over from the other) never make the same name.
  return `${kind}-${stamp}-${who}-${writer === undefined ? '' : `${writer}-`}${seq}.jsonl`
}

export const kindOf = (name: string): DeadLetterKind | null => {
  const m = FILE_PATTERN.exec(name)
  return m ? (m[1] as DeadLetterKind) : null
}

// Whether a line's own `kind` names one.
export const isDeadLetterKind = (value: unknown): value is DeadLetterKind => value === 'spill' || value === 'retry' || value === 'rejected'

// `raw[i]` is the text `lines[i]` was read from, so a line can be kept
// exactly as written. `complete`: the file ends with a completion record that
// matches every line above it. A file still being written, or left by a
// writer that stopped part-way, is not complete. `parent`: the file a
// replay wrote this one from, as its completion record names it.
export type ParsedFile = { complete: boolean; lines: DeadLetterLine[]; raw: string[]; unreadable: string[]; parent: string | null }

const readRecord = (line: string): { lines: number; fnv1a: string; parent: string | null } | null => {
  try {
    const parsed = JSON.parse(line) as { complete?: unknown; lines?: unknown; fnv1a?: unknown; row?: unknown; parent?: unknown } | null
    if (parsed === null || typeof parsed !== 'object' || parsed.complete !== true || parsed.row !== undefined) return null
    const parent = typeof parsed.parent === 'string' ? parsed.parent : null
    return typeof parsed.lines === 'number' && typeof parsed.fnv1a === 'string' ? { lines: parsed.lines, fnv1a: parsed.fnv1a, parent } : null
  } catch {
    return null
  }
}

export function parseFile(text: string): ParsedFile {
  // The completion record is the last line, and the text ends with it.
  let body = text
  let complete = false
  let parent: string | null = null
  if (text.endsWith('\n')) {
    const start = text.lastIndexOf('\n', text.length - 2) + 1
    const record = readRecord(text.slice(start, -1))
    if (record !== null) {
      body = text.slice(0, start)
      complete = record.lines === lineCount(body) && record.fnv1a === fnv1a(body)
      parent = record.parent
    }
  }
  const lines: DeadLetterLine[] = []
  const raw: string[] = []
  const unreadable: string[] = []
  for (const line of body.split('\n')) {
    if (line.trim() === '') continue
    try {
      const parsed = JSON.parse(line) as DeadLetterLine
      if (parsed && typeof parsed === 'object' && parsed.row && typeof parsed.row.event_type === 'string' && typeof parsed.row.timestamp === 'string') {
        lines.push(parsed)
        raw.push(line)
      } else {
        unreadable.push(line)
      }
    } catch {
      unreadable.push(line)
    }
  }
  return { complete, lines, raw, unreadable, parent }
}
