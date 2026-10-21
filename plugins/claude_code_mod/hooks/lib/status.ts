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

// The text `/bqaa-status` prints. Plain lines: the model reads a command's
// output too, so it carries counts and states, never a payload or a token,
// and the whole text is redacted once more on its way out.

import { type Config, tableRef } from './config.ts'
import { redactDiagnostic } from './sanitize.ts'
import type { BackfillReport, Writer } from './writer.ts'

// This table's dead-letter files (`dir`), the files elsewhere in the
// dead-letter directory (other tables' subdirectories, an older flat
// layout), and files too large to read back.
export type DeadLetterSummary =
  | { dir: string; files: number; bytes: number; byKind: Record<string, number>; oversized: string[]; elsewhere: number }
  | { error: string }

const ago = (now: number, at: number | null): string => {
  if (at === null) return 'never'
  const s = Math.max(0, Math.round((now - at) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

const kb = (bytes: number): string => (bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`)

const names = (list: readonly string[]): string => `${list.slice(0, 5).join(', ')}${list.length > 5 ? ', ...' : ''}`

// A line that names the table, cut for very long names: the setup command
// after it (at most 4,000 characters) then always fits whole in the 8,000
// the command's output keeps.
const short = (line: string): string => redactDiagnostic(line, 1000)

export function formatStatus(args: {
  config: Config
  writer: Writer
  now: number
  sessionId: string | null
  deadLetter: DeadLetterSummary
  backfill: BackfillReport | null
  isBackfilling: boolean
}): string {
  const { config: c, writer, now } = args
  const s = writer.snapshot()
  const k = s.counters
  const target = c.dryRun ? `dry run -> ${c.dryRunDir}` : c.project && c.dataset ? `${tableRef(c)} (${c.location})` : 'not configured'
  const lines = [
    short(`BigQuery Agent Analytics mod: ${c.enabled ? 'enabled' : 'disabled'}, target ${target}`),
    `queue: ${s.queued} rows (${kb(s.queuedBytes)}), ${s.inFlight} in flight; flush every ${c.flushIntervalMs} ms`,
    `rows: captured ${k.captured}, sent ${k.sent}, dead-lettered ${k.failed} (${k.rejected} refused), spilled ${k.spilled}, dropped ${s.dropped}, not captured ${k.skipped}`,
    `requests: ${k.batches} ok, ${k.retries} retried; last success ${ago(now, s.lastSuccessAt)}`,
  ]
  if (s.consecutiveFailures > 0) lines.push(`backing off: ${s.consecutiveFailures} failure(s) in a row, next try in about ${Math.max(0, Math.round((s.pausedUntil - now) / 1000))}s`)
  if (s.problem) {
    const { headline, fix } = writer.problemText()
    // The cause last: where redaction cuts it (a project named like a
    // credential, `auth-…:` in what BigQuery said), the fix and the count stay.
    lines.push(short(`notice (since ${ago(now, s.problem.since)}): rows are not being written: ${headline}; to fix, ${fix}; ${s.waitingText} row(s) waiting; cause: ${s.problem.reason.slice(0, 200)}`))
    const command = writer.setupCommand()
    if (command !== null) lines.push(`setup: ${command}`)
  }
  // No credential word before a colon: the free-text rules would cut the
  // rest of the line.
  const identity = [c.userIdFromCredentials ? `user_id from the token's account (${s.identity})` : '', c.recordHostname ? 'host name recorded' : ''].filter(Boolean).join('; ')
  if (identity) lines.push(`identity: ${identity}`)
  if (s.lastError) lines.push(`last error (${ago(now, s.lastError.at)}): ${s.lastError.message.slice(0, 300)}`)
  if ('error' in args.deadLetter) lines.push(`dead-letter: ${args.deadLetter.error}`)
  else {
    const d = args.deadLetter
    const kinds = Object.entries(d.byKind)
      .map(([kind, n]) => `${n} ${kind}`)
      .join(', ')
    lines.push(`dead-letter: ${d.files} file(s), ${kb(d.bytes)}${kinds ? ` (${kinds})` : ''} in ${d.dir}`)
    if (d.oversized.length > 0) lines.push(`dead-letter: ${d.oversized.length} file(s) over the 4 MiB read limit, not replayed: ${names(d.oversized)}`)
    if (d.elsewhere > 0) lines.push(`dead-letter: ${d.elsewhere} more file(s) in ${c.deadLetterDir} for other tables or an older layout, not replayed under this configuration`)
  }
  if (args.isBackfilling) lines.push('backfill: running')
  else if (args.backfill) {
    const b = args.backfill
    lines.push(
      `last backfill (${b.trigger}, ${ago(now, b.finishedAt)}): ${b.files} file(s), ${b.rowsSent} rows sent, ${b.rowsLeft} left${b.rowsRefused ? `, ${b.rowsRefused} of them refused` : ''}${b.otherTableRows ? `, ${b.otherTableRows} of them for another table` : ''}${b.skippedFiles ? `, ${b.skippedFiles} skipped` : ''}${b.oversizedFiles.length ? `, ${b.oversizedFiles.length} too large to read` : ''}${b.incompleteFiles.length ? `, ${b.incompleteFiles.length} incomplete` : ''}${b.held ? `; left waiting: ${b.held.slice(0, 200)}` : ''}${b.error ? `; stopped: ${b.error.slice(0, 200)}` : ''}`,
    )
    if (b.incompleteFiles.length > 0) {
      lines.push(`dead-letter: ${b.incompleteFiles.length} file(s) without a completion record (still being written, or left by a writer that stopped), kept, not replayed yet: ${names(b.incompleteFiles)}`)
    }
  }
  lines.push(`redactions ${k.redactions}, truncated rows ${k.truncatedRows}; session ${args.sessionId ?? 'unknown'}`)
  for (const problem of c.problems) lines.push(`problem: ${problem}`)
  return redactDiagnostic(lines.join('\n'), 8000)
}
