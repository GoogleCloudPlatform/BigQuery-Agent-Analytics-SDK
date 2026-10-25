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

// Unit tests of the mod's plain functions: redaction, truncation, row
// building, configuration, the BigQuery answer classifier and the
// dead-letter file format.

import { expect, test } from 'claude-code/testing'

import { classifyInsert, insertAllBody } from '../hooks/lib/bigquery.ts'
import { destinationDir, resolveConfig, splitArgv } from '../hooks/lib/config.ts'
import { FILE_PATTERN, MAX_FILE_BYTES, encodeLines, fileName, kindOf, parseFile } from '../hooks/lib/deadletter.ts'
import { randomIds } from '../hooks/lib/ids.ts'
import { BoundedQueue } from '../hooks/lib/queue.ts'
import { buildRow, utf8Length } from '../hooks/lib/rows.ts'
import { isSensitiveKey, limitsFor, newStats, redactDiagnostic, redactText, sanitize, toJsonColumn } from '../hooks/lib/sanitize.ts'
import { Tracer } from '../hooks/lib/tracer.ts'

import { utf8Bytes } from './helpers.ts'

const redact = (text: string) => {
  const stats = newStats()
  return { out: redactText(text, stats), count: stats.redactions }
}

// Credential-shaped inputs are joined from pieces at run time, so no file
// holds a literal that a secret scanner would match. Each joined value still
// has its provider's prefix and length, so every pattern has to fire.
const fake = (prefix: readonly string[], filler: string, length: number): string => prefix.join('') + filler.repeat(length).slice(0, length)

test('well-known credential shapes are redacted', () => {
  const pemLabel = ['RSA', 'PRIVATE', 'KEY'].join(' ')
  const cases: [string, RegExp][] = [
    [`key ${fake(['sk-', 'ant-api03-'], 'x', 40)}`, /\[REDACTED:api-key\]/],
    [`PROVIDER_API_KEY=${fake(['sk-', 'proj-'], 'x1', 40)}`, /\[REDACTED/],
    [`aws ${fake(['AK', 'IA'], 'X', 16)} id`, /\[REDACTED:aws-access-key-id\]/],
    [`maps ${fake(['AI', 'za'], 'x', 35)} key`, /\[REDACTED:google-api-key\]/],
    [`tok ${fake(['ya', '29.'], 'x', 30)}`, /\[REDACTED:google-oauth-token\]/],
    [`gh ${fake(['gh', 'p_'], 'x', 36)}`, /\[REDACTED:github-token\]/],
    [`pat ${fake(['github', '_pat_'], 'x', 40)}`, /\[REDACTED:github-token\]/],
    [`gl ${fake(['gl', 'pat-'], 'x', 20)}`, /\[REDACTED:gitlab-token\]/],
    [`npm ${fake(['np', 'm_'], 'x', 36)}`, /\[REDACTED:npm-token\]/],
    [`slack ${fake(['xo', 'xb-'], 'x', 20)}`, /\[REDACTED:slack-token\]/],
    [`stripe ${fake(['sk', '_live_'], 'x', 24)}`, /\[REDACTED:stripe-key\]/],
    [`jwt ${[fake(['ey', 'J'], 'x', 16), fake(['ey', 'J'], 'x', 16), 'x'.repeat(16)].join('.')}`, /\[REDACTED:jwt\]/],
    [`curl -H "Authorization: Bearer ${'x'.repeat(20)}"`, /Authorization: \[REDACTED\]$/],
    [`postgres://admin:${'x'.repeat(14)}@db.internal:5432/app`, /^postgres:\/\/\[REDACTED\]@db\.internal:5432\/app$/],
    [`export DB_PASSWORD=${'x'.repeat(14)}`, /DB_PASSWORD=\[REDACTED\]/],
    [`{"client_secret": "${'x'.repeat(12)}"}`, /^\{"client_secret": \[REDACTED\]$/],
    [`GITHUB_TOKEN=${'x'.repeat(16)}`, /GITHUB_TOKEN=\[REDACTED\]/],
    [[`-----BEGIN ${pemLabel}-----`, 'MIIEow', 'abc', `-----END ${pemLabel}-----`].join('\n'), /^\[REDACTED:private-key\]$/],
  ]
  for (const [input, pattern] of cases) {
    const { out, count } = redact(input)
    expect(out).toMatch(pattern)
    expect(count > 0).toBe(true)
  }
})

test('ordinary text, token counts and hashes are left alone', () => {
  const keep = [
    'max_tokens: 1024',
    '"input_tokens": 512, "output_tokens": 77',
    'scikit-learn and sk-learn are fine',
    'commit 3f2a9c1e5b7d4e8a9c216d0f4b8a7e13a1b2c3d4',
    'session 5c4b1d2e-8f7a-4c3b-9d1e-2a6f0b7c8d9e',
    'The tokenizer splits words.',
    'Read the README and run npm test',
    'https://example.test/search?q=tokens&page=2',
  ]
  for (const input of keep) expect(redact(input)).toEqual({ out: input, count: 0 })
})

test('only listed keys keep their values; under any other key the value goes whole, whatever it holds', () => {
  const stats = newStats()
  const out = sanitize(
    {
      api_keys: 'abc',
      access_tokens: ['a', 'b'],
      signing_keys: { k: 1 },
      sessions: 5,
      token2: true,
      secret1: 'x',
      passphrase2: 'y',
      decryption_keys: 'z',
      headers: { Accept: 'json' },
      author: 'dev',
      note: null,
      command: 'ls -la',
      input_tokens: 5,
      maxTokens: 7,
      model: 'example-model',
      args: { file_path: 'a.txt', target: 'prod' },
    },
    limitsFor(16384),
    stats,
  )
  expect(out).toEqual({
    api_keys: '[REDACTED]',
    access_tokens: '[REDACTED]',
    signing_keys: '[REDACTED]',
    sessions: '[REDACTED]',
    token2: '[REDACTED]',
    secret1: '[REDACTED]',
    passphrase2: '[REDACTED]',
    decryption_keys: '[REDACTED]',
    headers: '[REDACTED]',
    author: '[REDACTED]',
    // Nothing to hide in null.
    note: null,
    command: 'ls -la',
    input_tokens: 5,
    maxTokens: 7,
    model: 'example-model',
    args: { file_path: 'a.txt', target: '[REDACTED]' },
  })
  expect(stats.redactions).toBe(11)
  // Redaction alone is not truncation (as in the BQAA plugin).
  expect(stats.truncated).toBe(false)
})

test('credential names in text: words, endings, stems, plural and numbered forms', () => {
  for (const name of ['pass', 'PASS', 'MY_PWD', 'db_pwd', 'rootpwd', 'password', 'dbPassword', 'passwordHash', 'passwd', 'passphrase', 'secret', 'clientSecret', 'token', 'accessToken', 'X-Amz-Security-Token', 'accesstoken', 'auth', 'Authorization', 'Proxy-Authorization', 'cookie', 'Set-Cookie', 'cookies', 'sessionCookie', 'session', 'session_id', 'key', 'api_key', 'X-Api-Key', 'APIKey', 'apikey', 'privatekey', 'credential', 'credentials', 'private', 'private_key', 'bearer', 'jwt', 'sig', 'signature', 'temp:scratch', 'api_keys', 'API_KEYS', 'access_tokens', 'signing_keys', 'decryption_keys', 'sessions', 'token2', 'secret1', 'passphrase2', 'PASSWORDS2', 'apiKeys']) {
    expect([name, isSensitiveKey(name)]).toEqual([name, true])
  }
  // Listed token counts are not credentials, however they are written.
  for (const name of ['max_tokens', 'MAX_TOKENS', 'maxTokens', 'inputTokens', 'prompt_tokens', 'cache_read_input_tokens', 'total_tokens', 'tokenizer', 'author', 'authors', 'oauth_client_id', 'monkey', 'monkeys', 'keyboard', 'signal', 'file_path', 'command', 'description', 'content', 'role', 'url', 'page']) {
    expect([name, isSensitiveKey(name)]).toEqual([name, false])
  }
})

test('credential names in text match by stem: prefixed names, signatures and signed-link parameters', () => {
  const signed = `https://storage.example.test/b/o?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=${'c'.repeat(24)}&X-Goog-Expires=900&X-Goog-Signature=${'d'.repeat(64)}`
  const shared = `https://acct.blob.example.test/c/b?sv=2024-01-01&sp=r&sig=${'s'.repeat(24)}`
  const presigned = `https://bucket.s3.example.test/k?X-Amz-Credential=${'c'.repeat(20)}&X-Amz-Security-Token=${'t'.repeat(40)}&X-Amz-Signature=${'f'.repeat(64)}`
  expect(redact(`${signed} ${shared} ${presigned}`).out).toBe(
    [
      'https://storage.example.test/b/o?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=[REDACTED]&X-Goog-Expires=900&X-Goog-Signature=[REDACTED]',
      'https://acct.blob.example.test/c/b?sv=2024-01-01&sp=r&sig=[REDACTED]',
      'https://bucket.s3.example.test/k?X-Amz-Credential=[REDACTED]&X-Amz-Security-Token=[REDACTED]&X-Amz-Signature=[REDACTED]',
    ].join(' '),
  )
  const named = ['signature', 'my_api_key', 'openaiApiKey', 'x_amz_security_token', 'sessionCookie', 'db_passphrase'].map(name => `${name}: ${'x'.repeat(16)}`).join('\n')
  expect(redact(named)).toEqual({ out: ['signature', 'my_api_key', 'openaiApiKey', 'x_amz_security_token', 'sessionCookie', 'db_passphrase'].map(name => `${name}: [REDACTED]`).join('\n'), count: 6 })
})

test('diagnostics are redacted before they are cut, so a cut never leaves part of a credential', () => {
  // The token starts four characters before the cut: cut first and redacted
  // second, its first characters would be too short to match, and would show.
  const token = ['ya29', 'x'.repeat(40)].join('.')
  const out = redactDiagnostic(`${'e'.repeat(295)} ${token} and more`, 300)
  expect(out.length <= 300).toBe(true)
  expect(out.includes('ya29')).toBe(false)
  expect(redactDiagnostic(`password=${'p'.repeat(30)}`, 1000)).toBe('password=[REDACTED]')
  // Redacting twice changes nothing.
  expect(redactDiagnostic(redactDiagnostic(`key ${token} password=${'p'.repeat(30)}`, 1000), 1000)).toBe('key [REDACTED:google-oauth-token] password=[REDACTED]')
})

test('UTF-8 length matches the standard encoder', () => {
  for (const text of ['', 'abc', '\u00e9', '\u754c', '\u{1F600}', 'a\u754cb\u{1F600}c', '\uD800', 'x\uDC00y', '\u754c'.repeat(1000)]) {
    expect(utf8Length(text)).toBe(utf8Bytes(text))
  }
})

test('each table gets its own dead-letter subdirectory, with a safe and distinct name', () => {
  expect(destinationDir('my-gcp-project.agent_analytics.agent_events')).toBe('my-gcp-project.agent_analytics.agent_events')
  expect(destinationDir('example.com:proj.ds.t')).toBe('example.com%3Aproj.ds.t')
  const tables = ['p.d.a/b', 'p.d.a%2Fb', 'p.d.t*', 'p.d.t!', 'p.d.t \u754c']
  const dirs = tables.map(destinationDir)
  for (const dir of dirs) expect(/^[A-Za-z0-9._%-]+$/.test(dir)).toBe(true)
  expect(new Set(dirs).size).toBe(tables.length)
})

test('long text is cut, marked, and flagged as truncated, within the column cap', () => {
  const stats = newStats()
  const out = sanitize({ content: 'x'.repeat(50_000) }, limitsFor(4096), stats) as { content: string }
  expect(out.content.startsWith('x'.repeat(3000))).toBe(true)
  expect(out.content).toMatch(/\.\.\.\[TRUNCATED \d+ chars\]$/)
  expect(JSON.stringify(out).length <= 4096).toBe(true)
  expect(stats.truncated).toBe(true)
})

test('escape-heavy text is cut by its serialized length and keeps the row shape', () => {
  const stats = newStats()
  const value = { tool: 'Bash', tool_origin: 'LOCAL', result: '"quoted"\n\t\u0001'.repeat(5000) }
  const out = sanitize(value, limitsFor(2048), stats) as { tool: string; tool_origin: string; result: string }
  const json = toJsonColumn(out, 2048, stats)
  expect(json.length <= 2048).toBe(true)
  const parsed = JSON.parse(json) as { tool: string; tool_origin: string; truncated?: boolean }
  expect(parsed.tool).toBe('Bash')
  expect(parsed.tool_origin).toBe('LOCAL')
  expect(parsed.truncated).toBeUndefined()
})

test('a cut never splits a surrogate pair', () => {
  const stats = newStats()
  const out = sanitize({ text: '\u{1F600}'.repeat(5000) }, limitsFor(1024), stats) as { text: string }
  const body = out.text.replace(/\.\.\.\[TRUNCATED \d+ chars\]$/, '')
  const last = body.charCodeAt(body.length - 1)
  expect(last >= 0xd800 && last <= 0xdbff).toBe(false)
})

test('the character budget bounds the whole copy, not only each string', () => {
  const stats = newStats()
  const out = sanitize({ result: Array.from({ length: 50 }, () => 'y'.repeat(900)) }, limitsFor(4096), stats) as { result: string[] }
  expect(JSON.stringify(out).length < 6000).toBe(true)
  expect(out.result.at(-1)).toMatch(/^\[TRUNCATED \d+ items\]$/)
  expect(stats.truncated).toBe(true)
})

test('deep, wide and circular values stay bounded', () => {
  const stats = newStats()
  let deep: Record<string, unknown> = { text: 'leaf' }
  for (let i = 0; i < 40; i += 1) deep = { result: deep }
  const out = JSON.stringify(sanitize(deep, limitsFor(16384), stats))
  expect(out).toMatch(/TRUNCATED: nested too deep/)
  const list = sanitize(Array.from({ length: 1000 }, (_, i) => i), limitsFor(16384), newStats()) as unknown[]
  expect(list.length).toBe(201)
  expect(list.at(-1)).toBe('[TRUNCATED 800 items]')
  const wide = sanitize(Object.fromEntries(Array.from({ length: 300 }, (_, i) => [`field${i}`, i])), limitsFor(16384), newStats()) as Record<string, unknown>
  expect(wide._truncated_keys).toBe(100)
  const loop: Record<string, unknown> = { text: 'a' }
  loop.result = loop
  expect(sanitize(loop, limitsFor(16384), newStats())).toEqual({ text: 'a', result: '[CIRCULAR]' })
})

test('a multi-megabyte payload costs the hot path a few milliseconds', () => {
  const big = { file_path: 'dump.sql', content: 'INSERT INTO t VALUES (1, \'password=hunter2\');\n'.repeat(60_000) }
  const started = performance.now()
  const built = buildRow({
    eventType: 'TOOL_STARTING',
    timestampMs: Date.now(),
    eventId: 'e'.repeat(32),
    agent: 'coding-agent',
    sessionId: 's',
    invocationId: 'i',
    userId: 'u',
    traceId: 't'.repeat(32),
    spanId: 's'.repeat(16),
    parentSpanId: null,
    content: { tool: 'Write', args: big, tool_origin: 'LOCAL' },
    attributes: {},
    maxPayloadChars: 16384,
  })
  const elapsed = performance.now() - started
  expect(elapsed < 100).toBe(true)
  expect(built.row.is_truncated).toBe(true)
  expect((built.row.content ?? '').length <= 16384).toBe(true)
  expect(built.row.content ?? '').not.toMatch(/hunter2/)
  expect(() => JSON.parse(built.row.content ?? '')).not.toThrow()
})

test('an oversized JSON column becomes a valid object holding a prefix', () => {
  const stats = newStats()
  const text = toJsonColumn({ a: 'q"'.repeat(5000) }, 2048, stats)
  expect(text.length <= 2048).toBe(true)
  const parsed = JSON.parse(text) as { truncated: boolean; original_chars: number; preview: string }
  expect(parsed.truncated).toBe(true)
  expect(parsed.original_chars > 2048).toBe(true)
  expect(stats.truncated).toBe(true)
})

test('rows carry JSON columns as JSON text and cap the error message', () => {
  const { row } = buildRow({
    eventType: 'TOOL_COMPLETED',
    timestampMs: Date.parse('2026-10-13T23:52:17.123Z'),
    eventId: 'a'.repeat(32),
    agent: 'coding-agent',
    sessionId: 's',
    invocationId: 'i',
    userId: 'u',
    traceId: null,
    spanId: 'b'.repeat(16),
    parentSpanId: null,
    content: { tool: 'Bash', result: null, tool_origin: 'LOCAL' },
    attributes: { tool_use_id: 'toolu_1' },
    latencyMs: { total_ms: 12.6, time_to_first_token_ms: undefined },
    status: 'ERROR',
    errorMessage: 'E'.repeat(10_000),
    maxPayloadChars: 16384,
  })
  expect(row.timestamp).toBe('2026-10-13T23:52:17.123Z')
  expect(row.content).toBe('{"tool":"Bash","result":null,"tool_origin":"LOCAL"}')
  expect(row.latency_ms).toBe('{"total_ms":13}')
  expect(row.content_parts).toEqual([])
  expect((row.error_message ?? '').length < 4200).toBe(true)
  expect(row.is_truncated).toBe(true)
})

test('configuration: userConfig wins, then BQAA_* variables, then defaults', () => {
  const fromEnv = resolveConfig({}, { BQAA_PROJECT_ID: 'p1', BQ_DATASET: 'd1', HOME: '/h', USER: 'me', BQAA_TABLE: 't1' })
  expect([fromEnv.project, fromEnv.dataset, fromEnv.table, fromEnv.location, fromEnv.agentName, fromEnv.userId]).toEqual(['p1', 'd1', 't1', 'US', 'coding-agent', 'me'])
  expect(fromEnv.deadLetterDir).toBe('/h/.bqaa-mod/dead-letter')
  expect(fromEnv.dryRunDir).toBe('/h/.bqaa-mod/dry-run')
  expect(fromEnv.problems).toEqual([])
  const fromOptions = resolveConfig({ project: 'p2', dataset: 'd2', table: 'events', location: 'EU', flushIntervalMs: 50, maxPayloadChars: 9e9 }, { BQAA_PROJECT_ID: 'p1', HOME: '/h' })
  expect([fromOptions.project, fromOptions.dataset, fromOptions.table, fromOptions.location]).toEqual(['p2', 'd2', 'events', 'EU'])
  // Numbers are held to their documented bounds.
  expect(fromOptions.flushIntervalMs).toBe(1000)
  expect(fromOptions.maxPayloadChars).toBe(500_000)
  const missing = resolveConfig({}, { HOME: '/h' })
  expect(missing.problems.length).toBe(1)
  expect(resolveConfig({ dryRun: true }, { HOME: '/h' }).problems).toEqual([])
  expect(resolveConfig({}, { BQAA_TRACE_ENABLED: 'false' }).enabled).toBe(false)
  expect(resolveConfig({ enabled: false }, {}).enabled).toBe(false)
  expect(resolveConfig({}, { BQAA_DRY_RUN: 'true' }).dryRun).toBe(true)
  expect(resolveConfig({}, {}).accessTokenCommand).toEqual(['gcloud', 'auth', 'application-default', 'print-access-token'])
})

test('the token command is split into argv without a shell', () => {
  expect(splitArgv('gcloud auth print-access-token --impersonate-service-account=bqaa@p.iam.gserviceaccount.com')).toEqual([
    'gcloud',
    'auth',
    'print-access-token',
    '--impersonate-service-account=bqaa@p.iam.gserviceaccount.com',
  ])
  expect(splitArgv(`"/Applications/Google Cloud SDK/bin/gcloud" auth  print-access-token ''`)).toEqual(['/Applications/Google Cloud SDK/bin/gcloud', 'auth', 'print-access-token', ''])
})

test('insertAll answers are classified for retry, refresh, refusal or success', () => {
  expect(classifyInsert({ status: 200, text: '{}' }, 3).kind).toBe('ok')
  expect(classifyInsert({ status: 503, text: '' }, 3).kind).toBe('retry')
  expect(classifyInsert({ status: 429, text: '' }, 3).kind).toBe('retry')
  expect(classifyInsert({ status: 500, text: 'x' }, 3).kind).toBe('retry')
  expect(classifyInsert({ status: 401, text: '' }, 3).kind).toBe('auth')
  expect(classifyInsert({ status: 404, text: '' }, 3).kind).toBe('not-found')
  expect(classifyInsert({ status: 400, text: '' }, 3).kind).toBe('rejected')
  expect(classifyInsert({ status: 403, text: '{"error":{"errors":[{"reason":"accessDenied"}]}}' }, 3).kind).toBe('rejected')
  expect(classifyInsert({ status: 403, text: '{"error":{"errors":[{"reason":"quotaExceeded"}]}}' }, 3).kind).toBe('retry')
  const partial = classifyInsert(
    { status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'invalid', message: 'bad' }] }, { index: 2, errors: [{ reason: 'backendError' }] }] }) },
    3,
  )
  expect(partial.kind).toBe('partial')
  if (partial.kind === 'partial') {
    expect([...partial.rejected.keys()]).toEqual([0])
    expect([...partial.retry]).toEqual([2])
  }
  const body = JSON.parse(insertAllBody([{ event_id: 'e1' } as never])) as { rows: { insertId: string }[] }
  expect(body.rows[0]?.insertId).toBe('e1')
})

test('a 2xx answer acknowledges rows only when it is a valid insertAll response', () => {
  const unknown = (text: string) => classifyInsert({ status: 200, text }, 3).kind
  // A body cut off mid-way, the wrong JSON shape, or entries that do not
  // name a row of this batch: the outcome is not known, so the rows are kept.
  for (const text of [
    '{"insertErrors":[{"index":0,"errors":[{"reason":"invalid"',
    '',
    '[]',
    'null',
    '<html>OK</html>',
    '{"insertErrors":{"index":0}}',
    '{"insertErrors":[{"errors":[{"reason":"invalid"}]}]}',
    '{"insertErrors":[{"index":7,"errors":[{"reason":"invalid"}]}]}',
    '{"insertErrors":[{"index":0.5,"errors":[]}]}',
    '{"insertErrors":[{"index":0,"errors":"invalid"}]}',
    '{"insertErrors":[{"index":0,"errors":[null]}]}',
    '{"error":{"code":500,"message":"backend"}}',
  ]) {
    expect(unknown(text)).toBe('retry')
  }
  expect(unknown('{"kind":"bigquery#tableDataInsertAllResponse"}')).toBe('ok')
  expect(unknown('{"insertErrors":[]}')).toBe('ok')
  expect(unknown('{"insertErrors":[{"index":1,"errors":[{"reason":"invalid","message":"bad"}]}]}')).toBe('partial')
})

test('error text is redacted before the answer classifier cuts it', () => {
  const token = ['gh', 'p_', 'x'.repeat(36)].join('')
  const fragment = token.slice(0, 8)
  // The message cap is 1,000 characters, the non-JSON fallback 300: each
  // token below runs past its cap.
  const capped = classifyInsert({ status: 403, text: JSON.stringify({ error: { message: `${'e'.repeat(950)} ${token}` } }) }, 1)
  const page = classifyInsert({ status: 400, text: `<html>${'p'.repeat(280)} ${token}</html>` }, 1)
  const partial = classifyInsert({ status: 200, text: JSON.stringify({ insertErrors: [{ index: 0, errors: [{ reason: 'invalid', message: `${'e'.repeat(970)} ${token}` }] }] }) }, 1)
  expect(capped.kind === 'rejected' && !capped.message.includes(fragment) && capped.message.length <= 1000).toBe(true)
  expect(page.kind === 'rejected' && !page.message.includes(fragment)).toBe(true)
  expect(partial.kind === 'partial' && !(partial.rejected.get(0) ?? fragment).includes(fragment)).toBe(true)
})

test('a credential-named line loses everything after its separator, markers and quotes included', () => {
  const long = `${'x'.repeat(5000)}tail-secret`
  const cases: [string, string][] = [
    ['Cookie: session=abc123; csrf=[REDACTED]', 'Cookie: [REDACTED]'],
    ['Authorization: Token abc123def\nAccept: json', 'Authorization: [REDACTED]\nAccept: json'],
    ['db:\n  password: alpha beta gamma\n  host: db.internal', 'db:\n  password: [REDACTED]\n  host: db.internal'],
    ['export MY_PWD=abc def\nPASS=xyz\nmode=fast', 'export MY_PWD=[REDACTED]\nPASS=[REDACTED]\nmode=fast'],
    ['password: 987654321', 'password: [REDACTED]'],
    [`password: ${long}`, 'password: [REDACTED]'],
    // JSON that is only part of the text, escaped or not: the rest of its line goes.
    ['config {"user": "u", "password": "p", "next": 1}', 'config {"user": "u", "password": [REDACTED]'],
    ['log {\\"password\\": \\"p\\", \\"user\\": \\"u\\"}', 'log {\\"password\\": [REDACTED]'],
    // Code keeps the lines that do not name a credential.
    ['function f(token: string) {\n  return token\n}', 'function f(token: [REDACTED]\n  return token\n}'],
    // Plural and numbered names.
    ['export API_KEYS=abc,def\nSESSIONS=4\ntoken2: xyz\nmode=fast', 'export API_KEYS=[REDACTED]\nSESSIONS=[REDACTED]\ntoken2: [REDACTED]\nmode=fast'],
  ]
  for (const [input, output] of cases) expect(redact(input).out).toBe(output)
  for (const input of ['max_tokens: 1024', 'https://example.test/search?q=tokens&page=2', 'session 5c4b1d2e']) expect(redact(input)).toEqual({ out: input, count: 0 })
})

test('URLs: user-info, credential-named parameters and webhook URLs', () => {
  const secret = 's'.repeat(24)
  const slack = `https://${['hooks', 'slack', 'com'].join('.')}/${['services', 'T0', 'B0', secret].join('/')}`
  const discord = `https://${['discord', 'com'].join('.')}/api/${['webhooks', '42', secret].join('/')}`
  const office = `https://contoso.${['webhook', 'office', 'com'].join('.')}/webhookb2/${secret}`
  const cases: [string, string][] = [
    [`go to https://${['bob', secret].join(':')}@db.example.test/x`, 'go to https://[REDACTED]@db.example.test/x'],
    [`https://h.example.test/cb?code=1&access_token=${secret}&page=2#id_token=${secret}`, 'https://h.example.test/cb?code=1&access_token=[REDACTED]&page=2#id_token=[REDACTED]'],
    [`https://h.example.test/x?sig=${secret}&session=${secret}&key=${secret}`, 'https://h.example.test/x?sig=[REDACTED]&session=[REDACTED]&key=[REDACTED]'],
    [`https://h.example.test/x?api_keys=${secret}&token2=${secret}&max_tokens=5`, 'https://h.example.test/x?api_keys=[REDACTED]&token2=[REDACTED]&max_tokens=5'],
    [`post to ${slack}, ${discord} and ${office}.`, 'post to [REDACTED:webhook-url], [REDACTED:webhook-url] and [REDACTED:webhook-url].'],
  ]
  for (const [input, output] of cases) {
    expect(redact(input).out).toBe(output)
    expect(redact(input).out.includes(secret)).toBe(false)
  }
})

test('JSON in text is parsed and held to the same list, however deeply it is encoded', () => {
  const secret = 'z'.repeat(20)
  const once = JSON.stringify({ command: 'ls', password: secret })
  const twice = JSON.stringify({ text: once })
  const thrice = JSON.stringify(JSON.stringify({ content: twice }))
  const stats = newStats()
  const out = sanitize({ text: once, result: twice, content: thrice, response: JSON.stringify([{ pwd: 1, path: 'a' }, 'x']) }, limitsFor(16384), stats) as Record<string, string>
  expect(out.text).toBe('{"command":"ls","password":"[REDACTED]"}')
  expect(out.result).toBe(JSON.stringify({ text: '{"command":"ls","password":"[REDACTED]"}' }))
  expect(out.content).toBe(JSON.stringify(JSON.stringify({ content: JSON.stringify({ text: '{"command":"ls","password":"[REDACTED]"}' }) })))
  expect(out.response).toBe('[{"pwd":"[REDACTED]","path":"a"},"x"]')
  expect(JSON.stringify(out).includes(secret)).toBe(false)
  expect(stats.truncated).toBe(false)
  // Deeper than the walk goes: dropped and marked, never passed on unscanned.
  let deep = JSON.stringify({ password: secret })
  for (let i = 0; i < 8; i += 1) deep = JSON.stringify({ text: deep })
  const deepStats = newStats()
  const deepOut = sanitize({ text: deep }, limitsFor(16384), deepStats)
  expect(JSON.stringify(deepOut).includes(secret)).toBe(false)
  expect(deepStats.truncated).toBe(true)
})

test('JSON-looking text that cannot be parsed whole is dropped and marked, however it opens', () => {
  const secret = 'z'.repeat(20)
  const pad = 'p'.repeat(30_000)
  const nested = `{"password": [\n  "${secret}"\n], "pad": "${pad}"}`
  const dropped = {
    object: JSON.stringify({ password: [secret], pad }, null, 2),
    zero: `[0, ${nested}]`,
    true: `[true, ${nested}]`,
    null: `[null, ${nested}]`,
    string: `["a", ${nested}]`,
    lines: '[\n  1,\n  {"note": "cut short"',
    encoded: JSON.stringify(JSON.stringify({ password: [secret], pad }, null, 2)),
    invalid: '{not json: password}',
    footnote: '[1] first, [2] second',
  }
  // Text that only starts with a bracket or a quote is not JSON-looking.
  const kept = ['[1/3] Building', '[2026-10-14 12:00:01] started', '[INFO] ready', '[x] done', '"Hi," she said.', 'see [nginx] logs']
  const stats = newStats()
  const out = sanitize({ result: Object.values(dropped), text: kept }, limitsFor(16384), stats) as { result: string[]; text: string[] }
  expect(out.result.map(text => /^\[TRUNCATED: \d+ chars of JSON text that could not be parsed and checked\]$/.test(text))).toEqual(Object.keys(dropped).map(() => true))
  expect(out.result[7]).toBe('[TRUNCATED: 20 chars of JSON text that could not be parsed and checked]')
  expect(out.text).toEqual(kept)
  expect(JSON.stringify(out).includes(secret)).toBe(false)
  expect(stats.truncated).toBe(true)
  // Each drop counts as a redaction too.
  expect(stats.redactions).toBe(Object.keys(dropped).length)
})

test('text past the scanned window is dropped and marked, never passed on unscanned', () => {
  const secret = 'w'.repeat(24)
  const stats = newStats()
  // A credential far past the payload budget, with nothing before it to name it.
  const out = sanitize({ result: `${'a'.repeat(40_000)} ${secret}`, text: `password: ${'b'.repeat(20_000)}${secret}` }, limitsFor(16384), stats)
  expect(JSON.stringify(out).includes(secret)).toBe(false)
  expect(stats.truncated).toBe(true)
})

test('a key name holding a credential becomes a numbered placeholder, never one of its object\'s own names', () => {
  const token = ['ya29', 'u'.repeat(24)].join('.')
  const userinfo = `https://${['demo', 'p'.repeat(16)].join(':')}@example.invalid/report`
  const query = `https://example.invalid/r?sig=${'s'.repeat(16)}&page=2`
  const stats = newStats()
  const out = sanitize(
    {
      result: { [token]: 'unused', [userinfo]: { status: 200 }, [query]: null, plain: 'y', ['k'.repeat(30_000)]: 1 },
      text: JSON.stringify({ [token]: { content: 'kept' } }),
      response: { '[REDACTED_KEY:1]': 'a', [token]: 'b' },
    },
    limitsFor(16384),
    stats,
  )
  expect(out).toEqual({
    // In order: the token, the URLs, a plain name (kept), a name too long to
    // scan.
    result: { '[REDACTED_KEY:1]': '[REDACTED]', '[REDACTED_KEY:2]': '[REDACTED]', '[REDACTED_KEY:3]': null, plain: '[REDACTED]', '[REDACTED_KEY:4]': '[REDACTED]' },
    // In JSON text too.
    text: '{"[REDACTED_KEY:1]":"[REDACTED]"}',
    // A name shaped like a placeholder is not trusted, and numbering skips
    // the object's own names: two entries in, two out.
    response: { '[REDACTED_KEY:2]': '[REDACTED]', '[REDACTED_KEY:3]': '[REDACTED]' },
  })
  const text = JSON.stringify(out)
  expect([text.includes('u'.repeat(24)), text.includes('p'.repeat(16)), text.includes('s'.repeat(16))]).toEqual([false, false, false])
  // Each name replaced counts as a redaction, as each value does: seven
  // names (four in result, one in text, two in response) and seven values
  // (null has nothing to hide).
  expect(stats.redactions).toBe(14)
})

test('the row\'s own fields are listed keys; observed values under other keys are not kept', () => {
  const stats = newStats()
  const content = sanitize({ session_end: true, args: { session_end: true, cmd: 'ls' } }, limitsFor(16384), stats)
  expect(content).toEqual({ session_end: true, args: { session_end: true, cmd: '[REDACTED]' } })
  const attributes = sanitize({ session_metadata: { cwd: `/home/dev/src/x?token=${'t'.repeat(12)}`, host_version: '2.1.299' }, usage_metadata: { prompt_tokens: 3 }, extra: 'x' }, limitsFor(16384), stats)
  // Listed values still go through the text rules.
  expect(attributes).toEqual({ session_metadata: { cwd: '/home/dev/src/x?token=[REDACTED]', host_version: '2.1.299' }, usage_metadata: { prompt_tokens: 3 }, extra: '[REDACTED]' })
})

test('dead-letter files are JSONL, split by UTF-8 bytes under the file limit, and parsed tolerantly', () => {
  const line = (content: string) => ({
    v: 1 as const,
    kind: 'retry' as const,
    reason: 'r',
    attempts: 4,
    failed_at: 'f',
    table: 't',
    row: { event_type: 'TOOL_STARTING', timestamp: '2026-10-13T00:00:00.000Z', content, is_truncated: false } as never,
  })
  // 400,000 three-byte characters a line: 1.2 MB of UTF-8 in 400,000 characters.
  const files = encodeLines(Array.from({ length: 20 }, () => line('\u754c'.repeat(400_000))))
  expect(files.length >= 8).toBe(true)
  for (const text of files) expect(utf8Bytes(text) <= MAX_FILE_BYTES).toBe(true)
  expect(files.flatMap(f => parseFile(f).lines).length).toBe(20)
  // A row too large for a file on its own has its payload cut to fit and is
  // marked truncated, rather than failing the write.
  const [alone] = encodeLines([line('\u754c'.repeat(1_200_000))])
  expect(utf8Bytes(alone ?? '') <= MAX_FILE_BYTES).toBe(true)
  expect((parseFile(alone ?? '').lines[0]?.row as { is_truncated: boolean } | undefined)?.is_truncated).toBe(true)
  // Lines come back with their text as written, so a line can be kept as is.
  const first = files[0]?.split('\n')[0] ?? ''
  const parsed = parseFile(`${first}\n{broken\n\n`)
  expect(parsed.lines.length).toBe(1)
  expect(parsed.raw).toEqual([first])
  expect(parsed.unreadable).toEqual(['{broken'])
  expect(parsed.complete).toBe(false)
  const name = fileName('spill', Date.parse('2026-10-13T23:52:17.123Z'), '5c4b1d2e-8f7a', 3)
  expect(name).toBe('spill-20261013T235217123Z-5c4b1d2e8f7a-3.jsonl')
  expect(FILE_PATTERN.test(name)).toBe(true)
  expect(kindOf(name)).toBe('spill')
  expect(kindOf('notes.txt')).toBe(null)
})

test('a dead-letter file is complete only when its last line is a record matching every line before it', () => {
  const entry = (eventId: string) => ({
    v: 1 as const,
    kind: 'spill' as const,
    reason: 'session ended',
    attempts: 0,
    failed_at: '2026-10-13T20:01:02.003Z',
    table: 't',
    row: { event_type: 'STATE_DELTA', timestamp: '2026-10-13T20:01:02.003Z', event_id: eventId, is_truncated: false } as never,
  })
  const [sealed = ''] = encodeLines([entry('a'.repeat(32)), entry('b'.repeat(32))])
  const whole = parseFile(sealed)
  expect(whole.complete).toBe(true)
  // The record is not a row and not an unreadable line.
  expect(whole.lines.map(line => line.row.event_id)).toEqual(['a'.repeat(32), 'b'.repeat(32)])
  expect(whole.unreadable).toEqual([])
  const lines = sealed.split('\n')
  // Its last line is the record; without it the file is not complete.
  expect(lines.length).toBe(4)
  expect(lines[3]).toBe('')
  expect(parseFile(`${lines[0]}\n${lines[1]}\n`).complete).toBe(false)
  // Part-written: the first line, or all but the record's end.
  expect(parseFile(`${lines[0]}\n`).complete).toBe(false)
  expect(parseFile(sealed.slice(0, -12)).complete).toBe(false)
  // A record that does not match what precedes it: a line dropped, or one
  // character changed.
  expect(parseFile(`${lines[0]}\n${lines[2]}\n`).complete).toBe(false)
  expect(parseFile(sealed.replace('session ended', 'session ender')).complete).toBe(false)
  expect(parseFile('').complete).toBe(false)
})

test('a session-start row written once the configuration is loaded keeps the time its event came', () => {
  let now = 1_000
  const rows: { event_type: string; timestamp: string; attributes: string | null }[] = []
  const tracer = new Tracer({ now: () => now, ids: randomIds, config: () => resolveConfig({ project: 'p', dataset: 'd' }, {}), emit: row => rows.push(row) })
  // The event came at 1,000; its row is written at 1,250, after setup.
  now = 1_250
  tracer.sessionStarted({ source: 'startup' }, 1_000)
  now = 5_000
  tracer.sessionEnded({ reason: 'other' })
  expect(rows.map(row => [row.event_type, row.timestamp])).toEqual([
    ['STATE_DELTA', '1970-01-01T00:00:01.000Z'],
    ['STATE_DELTA', '1970-01-01T00:00:05.000Z'],
  ])
  // The session lasted from the event, not from the write.
  expect((JSON.parse(rows[1]?.attributes ?? '{}') as { state_delta: { duration_ms: number } }).state_delta.duration_ms).toBe(4_000)
})

test('the queue drops its oldest rows past its row or byte bound', () => {
  const queue = new BoundedQueue(3, 1_000_000)
  const row = (n: number) => ({ event_id: String(n), content: null }) as never
  for (let i = 0; i < 5; i += 1) queue.push(row(i))
  expect(queue.length).toBe(3)
  expect(queue.dropped).toBe(2)
  expect(queue.take(10, 1_000_000).map(q => (q.row as { event_id: string }).event_id)).toEqual(['2', '3', '4'])
  const small = new BoundedQueue(100, 500)
  for (let i = 0; i < 10; i += 1) small.push({ event_id: String(i), content: 'x'.repeat(100) } as never)
  expect(small.bytes <= 500).toBe(true)
  expect(small.dropped > 0).toBe(true)
})

test('a huge tool input is cut inside args; tool and tool_origin survive', () => {
  const built = buildRow({
    eventType: 'TOOL_STARTING',
    timestampMs: Date.now(),
    eventId: 'e'.repeat(32),
    agent: 'coding-agent',
    sessionId: 's',
    invocationId: 'i',
    userId: 'u',
    traceId: null,
    spanId: 's'.repeat(16),
    parentSpanId: null,
    content: { tool: 'Write', tool_origin: 'LOCAL', args: { file_path: 'big.txt', content: 'z'.repeat(100_000) } },
    attributes: {},
    maxPayloadChars: 4096,
  })
  const content = JSON.parse(built.row.content ?? '{}') as { tool: string; tool_origin: string; args: { file_path: string } }
  expect(content.tool).toBe('Write')
  expect(content.tool_origin).toBe('LOCAL')
  expect(content.args.file_path).toBe('big.txt')
  expect(built.row.is_truncated).toBe(true)
})

test('credentials in command-line flags are removed, with = or a space before the value', () => {
  const stats = newStats()
  const out = (text: string): string => redactText(text, stats)
  expect(out('docker login -u ci --password=Pr0dRegistryPass! registry.example.com')).toBe('docker login -u ci --password=[REDACTED]')
  expect(out('tool --api-key=abc123def --verbose')).toBe('tool --api-key=[REDACTED]')
  expect(out('gcloud x --client-secret=s3cr3tValue')).toBe('gcloud x --client-secret=[REDACTED]')
  expect(out('java -Djavax.net.ssl.trustStorePassword=changeit -jar app.jar')).toBe('java -Djavax.net.ssl.trustStorePassword=[REDACTED]')
  expect(out('docker login --username ci --password Pr0dRegistryPass! registry.example.com')).toBe('docker login --username ci --password [REDACTED] registry.example.com')
  expect(out('tool --api-key "two words" --verbose')).toBe('tool --api-key [REDACTED] --verbose')
  expect(out("curl --cookie 'id=1; theme=dark' https://example.com/")).toBe('curl --cookie [REDACTED] https://example.com/')
  // Not credentials, or no value after the space: kept as they are.
  for (const kept of ['ls --color=auto -la', 'ls --color auto', 'docker login --password-stdin registry.example.com',
    'tool --token-file /etc/token --verbose', 'cli --max-tokens 1024', 'cli --password --verbose', 'cli --password\nnext line']) {
    expect(out(kept)).toBe(kept)
  }
  expect(out('tool --token [REDACTED:github-token] --verbose')).toBe('tool --token [REDACTED:github-token] --verbose')
})

test('a long string never passes on a credential its scan window cut short', () => {
  // A long credential line shrinks the scanned window by more than the scan
  // margin; a token at the window's end, cut there, no longer matches its
  // pattern. The window's last part, scanned only for what comes before it,
  // is never kept.
  const token = 'ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
  const head = `PRIVATE_KEY=${'k'.repeat(11_988)}\n`
  let leaked = 0
  for (let at = 23_900; at <= 24_200; at += 1) {
    const text = head + 'a'.repeat(at - head.length) + ' ' + token + ' tail'
    const out = JSON.stringify(sanitize({ result: text }, limitsFor(16384), newStats()))
    if (out.includes(token.slice(0, 8))) leaked += 1
  }
  expect(leaked).toBe(0)
  const shortHead = `PRIVATE_KEY=${'k'.repeat(8_488)}\n`
  let diagnosticLeaks = 0
  for (let at = 9_150; at <= 9_200; at += 1) {
    const text = shortHead + 'e'.repeat(at - shortHead.length) + ' ' + token + ' and more'
    if (redactDiagnostic(text, 1000).includes(token.slice(0, 8))) diagnosticLeaks += 1
  }
  expect(diagnosticLeaks).toBe(0)
})

test('a credential name right after a non-credential option is still matched', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['kubectl create secret generic db --from-literal=password=S3cretValue1', 'kubectl create secret generic db --from-literal=password=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=DB_PASSWORD=hunter2value', 'gcloud run deploy svc --set-env-vars=DB_PASSWORD=[REDACTED]'],
    ['gcloud run deploy svc --update-env-vars=API_KEY=abc123def --region us', 'gcloud run deploy svc --update-env-vars=API_KEY=[REDACTED]'],
    ['docker run --env=PASSWORD=abc123def image', 'docker run --env=PASSWORD=[REDACTED]'],
    ['docker build --build-arg=NPM_TOKEN=abc123def .', 'docker build --build-arg=NPM_TOKEN=[REDACTED]'],
    ['terraform apply -var=db_password=abc123def', 'terraform apply -var=db_password=[REDACTED]'],
    ['mvn deploy -Dserver=password=abc123def', 'mvn deploy -Dserver=password=[REDACTED]'],
    ['curl --header=X-Api-Key:abc123def', 'curl --header=X-Api-Key:[REDACTED]'],
    ['tool --opt= DB_PASSWORD=abc123def', 'tool --opt= DB_PASSWORD=[REDACTED]'],
    ['env: DB_PASSWORD=abc123def', 'env: DB_PASSWORD=[REDACTED]'],
  ]
  for (const [input, want] of cases) expect(redactText(input, stats)).toBe(want)
})

test('a credential option loses its whole shell argument, whatever it holds', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['docker login --password "prefix\\"SecretSuffix" registry.example.com', 'docker login --password [REDACTED] registry.example.com'],
    ['docker login --password \'prefix\'"SecretSuffix" registry.example.com', 'docker login --password [REDACTED] registry.example.com'],
    ['docker login --password prefix\\ SecretSuffix registry.example.com', 'docker login --password [REDACTED] registry.example.com'],
    ['docker login --password [REDACTED]SecretSuffix registry.example.com', 'docker login --password [REDACTED] registry.example.com'],
    [`docker login --password ghp_${'A1b2'.repeat(9)}!SecretSuffix registry.example.com`, 'docker login --password [REDACTED] registry.example.com'],
    ['curl --pass -SyntheticPassphrase file:///dev/null', 'curl --pass [REDACTED] file:///dev/null'],
    ['tool --password "an unclosed quote\nSecretSuffix on the next line', 'tool --password [REDACTED]'],
  ]
  for (const [input, want] of cases) expect(redactText(input, stats)).toBe(want)
  // Kept: an exact marker, and no value before the next long option.
  expect(redactText('tool --token [REDACTED:github-token] --verbose', stats)).toBe('tool --token [REDACTED:github-token] --verbose')
  expect(redactText('cli --password --verbose', stats)).toBe('cli --password --verbose')
})

test('flag credentials stay out of rows and diagnostics', () => {
  const commands = [
    'kubectl create secret generic db --from-literal=password=SecretSuffix1',
    'docker login --password "prefix\\"SecretSuffix" registry.example.com',
    'docker login --password [REDACTED]SecretSuffix registry.example.com',
    'curl --pass -SecretSuffix file:///dev/null',
  ]
  for (const command of commands) {
    const row = buildRow({ eventType: 'TOOL_STARTING', timestampMs: 0, eventId: 'e1', agent: 'coding-agent', sessionId: 's1',
      invocationId: 'i1', userId: 'dev', traceId: null, spanId: null, parentSpanId: null,
      content: { tool: 'Bash', args: { command } }, attributes: {}, maxPayloadChars: 16384 }).row
    expect(JSON.stringify(row)).not.toContain('SecretSuffix')
    expect(redactDiagnostic(`command failed: ${command}`, 1000)).not.toContain('SecretSuffix')
  }
})

test("a credential option's value is found across line continuations; one an expansion holds, or a continuation splits or glues, runs to the end of the text", () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['docker login --username me --password \\\n    SecretSuffix registry.example.com', 'docker login --username me --password \\\n    [REDACTED] registry.example.com'],
    ['cli --password \\\r\n  SecretSuffix tail', 'cli --password \\\r\n  [REDACTED] tail'],
    // Glued to the name or split by a continuation: to the end of the text.
    ['cli --password\\\nSecretSuffix --next', 'cli --password\\\n[REDACTED]'],
    ['cli --password Secret\\\nSuffix tail', 'cli --password [REDACTED]'],
    // An expansion's end is not read: to the end of the text.
    ["docker login --password $'prefix\\' SecretSuffix' registry.example.com", 'docker login --password [REDACTED]'],
    ['docker login --password $(printf %s SecretSuffix) registry.example.com', 'docker login --password [REDACTED]'],
    ['docker login --password $(printf "%s )" SecretSuffix) registry.example.com', 'docker login --password [REDACTED]'],
    ['docker login --password `printf %s SecretSuffix` registry.example.com', 'docker login --password [REDACTED]'],
    ['docker login --password ${LOGIN:-my SecretSuffix} registry.example.com', 'docker login --password [REDACTED]'],
    ['docker login --password "$(cat "my file") SecretSuffix" registry.example.com', 'docker login --password [REDACTED]'],
    ['tool --password $(echo SecretSuffix', 'tool --password [REDACTED]'],
  ]
  for (const [input, want] of cases) expect(redactText(input, stats)).toBe(want)
  // No value before the next option, across a continuation too.
  expect(redactText('cli --password \\\n--verbose', stats)).toBe('cli --password \\\n--verbose')
})

test('continued and expanded flag credentials stay out of rows and diagnostics', () => {
  // Each command, and whether what follows its value is kept.
  const commands: [string, boolean][] = [
    ['docker login --username me --password \\\n    SecretSuffix registry.example.com', true],
    ["docker login --password $'prefix\\' SecretSuffix' registry.example.com", false],
    ['docker login --password $(printf %s SecretSuffix) registry.example.com', false],
    ['docker login --password `printf %s SecretSuffix` registry.example.com', false],
  ]
  for (const [command, kept] of commands) {
    const row = buildRow({ eventType: 'TOOL_STARTING', timestampMs: 0, eventId: 'e1', agent: 'coding-agent', sessionId: 's1',
      invocationId: 'i1', userId: 'dev', traceId: null, spanId: null, parentSpanId: null,
      content: { tool: 'Bash', args: { command } }, attributes: {}, maxPayloadChars: 16384 }).row
    expect(JSON.stringify(row)).not.toContain('SecretSuffix')
    expect(redactDiagnostic(`command failed: ${command}`, 1000)).not.toContain('SecretSuffix')
    expect(JSON.stringify(row).includes('registry.example.com')).toBe(kept)
  }
})

test('a value a substitution holds runs to the end of the text, whatever shell code is in it', () => {
  const stats = newStats()
  // The substitution's end is not read (its comments, here-documents, arrays
  // and case statements included), so whatever follows the value goes too.
  const inputs = [
    'cli --password $(printf %s SecretSuffix # comment\n) --verbose\necho ordinary',
    'cli --password $(printf %s prefix # )\nprintf %s SecretSuffix) --verbose\necho ordinary',
    'cli --password $(printf %s SecretSuffix # (\n) --verbose',
    'cli --password $(printf %s prefix \\\n# )\nprintf %s SecretSuffix) --verbose',
    'cli --password $(# a comment first\nprintf %s SecretSuffix) --verbose',
    'cli --password <(printf %s x # )\nprintf %s SecretSuffix) --verbose',
    'cli --password $(printf %s abc#SecretSuffix) --verbose',
    'cli --password $(printf %s $((1 << 2)) SecretSuffix) --verbose',
    'cli --password $( (( x = 1 << 2 )); printf %s SecretSuffix) --verbose',
    'cli --password $(printf %s ${#SecretSuffix}) --verbose',
    'cli --password $(printf "%s # )" SecretSuffix) --verbose',
    'cli --password $(printf %s \\\n  SecretSuffix) --verbose',
    'cli --password $(cat <<< SecretSuffix) --verbose',
    'cli --password $(printf %s showcase case SecretSuffix) --verbose',
    'cli --password ${LOGIN:-{a b} SecretSuffix} --verbose',
    'cli --password $(cat <<EOF\nSecretSuffix\nEOF\n) --verbose\necho ordinary',
    'cli --password "$(cat <<EOF\n) " SecretSuffix\nEOF\n)" --verbose',
    "cli --password $(cat <<-'END'\n\t) SecretSuffix\n\tEND\n) --verbose",
    'cli --password $(cat <<A <<B\nx\nA\nSecretSuffix)\nB\n) --verbose',
    'cli --password $(cat <<EOF\nA\nEOF\r\nSecretSuffix)\nEOF \n)\nEOF\n) --verbose',
    'cli --password $(case x in x) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(case y in x) printf %s esac;; y) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(case a in (a) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(case "a b" in c | "a b") printf %s SecretSuffix;& d) printf %s x;; esac) --verbose',
    'cli --password $(case a\nin\n  # a comment\n  a) printf %s SecretSuffix\n  ;;\nesac) --verbose',
    'cli --password $(case a in a) printf %s SecretSuffix esac\nesac) --verbose',
    'cli --password $(case a in a) cat <<EOF;;\nSecretSuffix)\nEOF\nesac) --verbose',
    'cli --password $(ca\\\nse a i\\\nn a) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(cat <<EOF\nEOF) SecretSuffix\nEOF\n) --verbose',
    // An array's elements, with their comments.
    'cli --password $(a=( # )\nx\n); printf %s SecretSuffix) --verbose\necho ordinary',
    'cli --password $(a=(x # )\n); printf %s SecretSuffix) --verbose',
    'cli --password $(a=(y); a+=( # )\nx\n); printf %s SecretSuffix) --verbose',
    'cli --password $(declare -a a=( # )\nx\n); printf %s SecretSuffix) --verbose',
    'cli --password $(a=( [1]=x # )\n); printf %s SecretSuffix) --verbose',
    'cli --password $(a=\\\n( # )\nx\n); printf %s SecretSuffix) --verbose',
    'cli --password $(a=( (x) "#)" ); printf %s SecretSuffix) --verbose',
    // A redirection names a file, never `esac`.
    'cli --password $(case y in (x) <esac;; y) printf %s SecretSuffix;; esac) --verbose\necho ordinary',
    'cli --password $(case y in (x) cat <esac;; y) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(case y in (x) >/dev/null esac;; y) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(case y in (x) >|esac;; y) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(case y in x) 2>&esac;; y) printf %s SecretSuffix;; esac) --verbose',
    'cli --password "$(cat <<EOF\ntext\\\nEOF\n) " SecretSuffix\nEOF\n)" --verbose',
    'cli --password $(printf %s A;#x )\nprintf %s SecretSuffix) --verbose',
    'cli --password $(printf %s x >#)\nprintf %s SecretSuffix) --verbose',
    'cli --password $(printf %s A # x \\\nprintf %s SecretSuffix) --verbose',
    'cli --password $(cat <<EOF) --verbose\nSecretSuffix\nEOF',
    "cli --password $(cat <<''\nSecretSuffix)\n\n) --verbose",
    'cli --password $(cat <<EOF $(printf %s x\n)\nSecretSuffix\nEOF\n) --verbose',
    'cli --password $(case $(uname) in *) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(case a { a) printf %s SecretSuffix;; }) --verbose',
    'cli --password $(case a in @(a|b)) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $((printf %s a) SecretSuffix) --verbose',
    'cli --password $(cat <<EOF\nSecretSuffix with no end',
    'cli --password $(cat <<EOF\nSecretSuffix\nEOF) --verbose',
    'cli --password $(printf %s $((1 # ))\n)) SecretSuffix) --verbose',
    'cli --password $(a=(# )\nx\n); printf %s SecretSuffix) --verbose',
    'cli --password $(a=( x;# )\n); printf %s SecretSuffix) --verbose',
  ]
  for (const input of inputs) expect(redactText(input, stats)).toBe('cli --password [REDACTED]')
})

test('a value a line continuation splits runs to the end of the text, a split $\', ${, $(, << or case included', () => {
  const stats = newStats()
  const inputs = [
    "cli --password $\\\n'prefix\\' SecretSuffix' --verbose",
    'cli --password $\\\n{LOGIN:-my SecretSuffix} --verbose',
    'cli --password $\\\r\n{LOGIN:-my SecretSuffix} --verbose',
    'cli --password $\\\n(printf %s SecretSuffix) --verbose',
    'cli --password <\\\n(printf %s SecretSuffix) --verbose',
    'cli --password $(ca\\\nse y in y) printf %s SecretSuffix;; esac) --verbose',
    'cli --password $(cat <\\\n<EOF\n) SecretSuffix\nEOF\n) --verbose',
    'cli --password $(cat <<\\\n< SecretSuffix) --verbose',
  ]
  for (const input of inputs) expect(redactText(input, stats)).toBe('cli --password [REDACTED]')
})

test('a credential argument ends at an unquoted control or redirection operator', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['cli --password plain;printf done', 'cli --password [REDACTED];printf done'],
    ['cli --password plain|printf done', 'cli --password [REDACTED]|printf done'],
    ['cli --password plain&& printf done', 'cli --password [REDACTED]&& printf done'],
    ['cli --password plain>out.txt', 'cli --password [REDACTED]>out.txt'],
    ['(cli --password plain) && echo ok', '(cli --password [REDACTED]) && echo ok'],
    // A process substitution's end is not read: to the end of the text.
    ['cli --password <(printf %s SecretSuffix) --verbose', 'cli --password [REDACTED]'],
    ['cli --password "a;b|c" --verbose', 'cli --password [REDACTED] --verbose'],
    ['cli --password prefix\vSecret\fSuffix tail', 'cli --password [REDACTED] tail'],
  ]
  for (const [input, want] of cases) expect(redactText(input, stats)).toBe(want)
})

test('a name=value credential runs on across line continuations and an unclosed quote', () => {
  const stats = newStats()
  // A continuation in the value: to the end of the text.
  expect(redactText('cli --password=\\\nSecretSuffix --verbose\nnext line', stats)).toBe('cli --password=[REDACTED]')
  expect(redactText('cli --password="prefix\nSecretSuffix" --verbose\nnext line', stats)).toBe('cli --password=[REDACTED]\nnext line')
  expect(redactText('PASSWORD=abc\\\r\ndefSecretSuffix\r\nnext', stats)).toBe('PASSWORD=[REDACTED]')
  expect(redactText('{"password": "abc", "user": "bob"}\nnext', stats)).toBe('{"password": [REDACTED]\nnext')
})

test('a shell-style name=value runs through its whole shell word, to the end of the line it ends on', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['cli --password=prefix"Secret\nSuffix" --verbose\nnext line', 'cli --password=[REDACTED]\nnext line'],
    ["cli --password=prefix'Secret\nSuffix' --verbose\nnext line", 'cli --password=[REDACTED]\nnext line'],
    // An expansion, a backtick or an escaped quote: to the end of the text.
    ['cli --password=$(printf %s prefix\nprintf %s SecretSuffix) --verbose\nnext line', 'cli --password=[REDACTED]'],
    ['PASSWORD=$(cat <<EOF\nSecretSuffix\nEOF\n) cli --verbose', 'PASSWORD=[REDACTED]'],
    ['cli --from-literal=password=`printf %s prefix\nprintf %s SecretSuffix` --verbose\nnext line', 'cli --from-literal=password=[REDACTED]'],
    ['cli "--password=prefix\nSecretSuffix" --verbose\nnext line', 'cli "--password=[REDACTED]"\nnext line'],
    ['cli `--password=prefix\nSecretSuffix` --verbose\nnext line', 'cli `--password=[REDACTED]'],
    ['log {\\"cmd\\": \\"PASSWORD=abc\\"}\nnext line', 'log {\\"cmd\\": \\"PASSWORD=[REDACTED]'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).toBe(want)
  }
  // A name: value, or name = value with spaces (code, INI), keeps to its line
  // unless the value opens with a quote.
  expect(redactText("password: it's here\nnext line", stats)).toBe('password: [REDACTED]\nnext line')
  expect(redactText('key = (hi << 8) | lo\nnext line', stats)).toBe('key = [REDACTED]\nnext line')
  expect(redactText('token=abc def\nnext line', stats)).toBe('token=[REDACTED]\nnext line')
})

test('a query-style name= value that a quote or an expansion opens runs through its shell word', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['cmd;PASSWORD="a Secret" cli --verbose', 'cmd;PASSWORD=[REDACTED] cli --verbose'],
    ["true&TOKEN='x Secret' cli", 'true&TOKEN=[REDACTED] cli'],
    // An expansion's end is not read: to the end of the text.
    ['cmd;API_KEY=$(printf %s a Secret) cli', 'cmd;API_KEY=[REDACTED]'],
    ['cmd;TOKEN=`printf %s a Secret` cli', 'cmd;TOKEN=[REDACTED]'],
    // Query strings keep their rule.
    ['/path?token=abc&x=1 next', '/path?token=[REDACTED]&x=1 next'],
    ['a=1&password=abc;b=2', 'a=1&password=[REDACTED];b=2'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).toBe(want)
  }
})

test('a quote that closes around a credential name leaves the value outside it', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['cli "--password"="prefix\nSecretSuffix" --verbose\necho ordinary', 'cli "--password"=[REDACTED]\necho ordinary'],
    ['cli --"password"="prefix\nSecretSuffix" --verbose\necho ordinary', 'cli --"password"=[REDACTED]\necho ordinary'],
    ["cli --'password'='prefix\nSecretSuffix' --verbose\necho ordinary", "cli --'password'=[REDACTED]\necho ordinary"],
    ['message "password"="prefix\nSecretSuffix" more\nnext line', 'message "password"=[REDACTED]\nnext line'],
    // A quote opened between the name and `=` holds the value.
    ['cli --password"=prefix\nSecretSuffix" --verbose\nnext line', 'cli --password"=[REDACTED]"\nnext line'],
    // An option whose name is quoted alone, and its value.
    ['cli "--password" "Secret Suffix" --verbose', 'cli "--password" [REDACTED] --verbose'],
    ["cli '--password' Secret\\ Suffix --verbose", "cli '--password' [REDACTED] --verbose"],
    // Empty and other complete quoted segments before the name: the quote
    // just before it opens one.
    [`cli ''"--password=prefix\nSecretSuffix" --verbose\necho ordinary`, `cli ''"--password=[REDACTED]"\necho ordinary`],
    [`cli ""'--password=prefix\nSecretSuffix' --verbose\necho ordinary`, `cli ""'--password=[REDACTED]'\necho ordinary`],
    [`cli ''''"--password=prefix\nSecretSuffix" --verbose\necho ordinary`, `cli ''''"--password=[REDACTED]"\necho ordinary`],
    [`cli ''""'--password=prefix\nSecretSuffix' --verbose\necho ordinary`, `cli ''""'--password=[REDACTED]'\necho ordinary`],
    [`cli 'a'"--password=prefix\nSecretSuffix" --verbose\necho ordinary`, `cli 'a'"--password=[REDACTED]"\necho ordinary`],
    [`cli $'a'"--password=prefix\nSecretSuffix" --verbose\necho ordinary`, `cli $'a'"--password=[REDACTED]"\necho ordinary`],
    // An escaped quote in the word: the quote the value starts in is not
    // known, so it runs to the end of the text.
    [`cli \\'"--password=prefix\nSecretSuffix" --verbose\necho ordinary`, `cli \\'"--password=[REDACTED]`],
    [`cli ''"--password Secret\nSuffix" --verbose\necho ordinary`, `cli ''"--password [REDACTED]" --verbose\necho ordinary`],
    [`echo "a\nb" "--password=prefix\nSecretSuffix" --verbose\necho ordinary`, `echo "a\nb" "--password=[REDACTED]"\necho ordinary`],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).toBe(want)
  }
  // A quote that closes an earlier segment, which the word read alone takes
  // for an opening one: the quote the value starts in is not known, so it
  // runs to the end of the text.
  expect(redactText(`cli "a b"--password=x"y z" --verbose\necho ordinary`, stats)).toBe(`cli "a b"--password=[REDACTED]`)
})

test('line continuations, quotes and backslashes inside an option name are read through', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['cli --pass\\\nword SecretSuffix --verbose', 'cli --pass\\\nword [REDACTED] --verbose'],
    ['cli --pa\\\nss\\\nword SecretSuffix --verbose', 'cli --pa\\\nss\\\nword [REDACTED] --verbose'],
    ['cli -\\\n-password SecretSuffix --verbose', 'cli -\\\n-password [REDACTED] --verbose'],
    ['cli --\\\npassword SecretSuffix --verbose', 'cli --\\\npassword [REDACTED] --verbose'],
    ['cli --pass\\\r\nword SecretSuffix --verbose', 'cli --pass\\\r\nword [REDACTED] --verbose'],
    // A continuation with no space beside it right after a credential's name
    // glues what follows to it: `word=…` after `--pass`, and `=…` after
    // `--password`, go to the end of the text.
    ['cli --pass\\\nword=SecretSuffix --verbose\nnext', 'cli --pass\\\n[REDACTED]'],
    ['cli --password\\\n=SecretSuffix --verbose\nnext', 'cli --password\\\n[REDACTED]'],
    ['PASS\\\nWORD=SecretSuffix cli\nnext', 'PASS\\\nWORD=[REDACTED]\nnext'],
    // Quotes and backslashes the shell removes from a name.
    ['cli "--pass"word SecretSuffix --verbose', 'cli "--pass"word [REDACTED] --verbose'],
    ['cli --pass""word SecretSuffix --verbose', 'cli --pass""word [REDACTED] --verbose'],
    ['cli --pa\\ssword SecretSuffix --verbose', 'cli --pa\\ssword [REDACTED] --verbose'],
    ['cli -\\-password SecretSuffix --verbose', 'cli -\\-password [REDACTED] --verbose'],
    ['cli "-"-password SecretSuffix --verbose', 'cli "-"-password [REDACTED] --verbose'],
    ["cli $'--pass'word SecretSuffix --verbose", "cli $'--pass'word [REDACTED] --verbose"],
    ["cli --'pass'word=SecretSuffix --verbose\nnext", "cli --'pass'word=[REDACTED]\nnext"],
    ['cli "--api"-key=SecretSuffix --verbose\nnext', 'cli "--api"-key=[REDACTED]\nnext'],
    ['cli "--api"-key SecretSuffix --verbose\nnext', 'cli "--api"-key [REDACTED] --verbose\nnext'],
    // ANSI-C escapes in the name, and an `=` inside a quote.
    ["cli $'--pa\\x73sword' SecretSuffix --verbose", "cli $'--pa\\x73sword' [REDACTED] --verbose"],
    ["cli $'--pa\\163sword' SecretSuffix --verbose", "cli $'--pa\\163sword' [REDACTED] --verbose"],
    ['cli "--pass"word"=prefix\nSecretSuffix" --verbose\nnext', 'cli "--pass"word"=[REDACTED]"\nnext'],
    // So does a value glued by such a continuation (`--password\` and
    // `VALUE`), unless what follows carries the name on.
    ['cli --password\\\nSecretSuffix host', 'cli --password\\\n[REDACTED]'],
    ['cli --pass\\\nword\\\nSecretSuffix host', 'cli --pass\\\nword\\\n[REDACTED]'],
    ['cli "--password"\\\n-stdin host', 'cli "--password"\\\n-stdin host'],
    ['cli --password\\\n-stdin host', 'cli --password\\\n-stdin host'],
    // Not an option: kept.
    ['cli "--pass"word-stdin host', 'cli "--pass"word-stdin host'],
    ['cli "--max"-tokens 1024', 'cli "--max"-tokens 1024'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).toBe(want)
  }
})

test('a value is read in the quote it starts in where the whole-text scan and the word alone agree on it, and otherwise runs to the end of the text', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    // Prose or an odd quote before it: the scan and the word alone disagree
    // on the quote the value starts in, so it runs to the end of the text.
    [`It's set up. Run: cli '--password Secret Suffix' --verbose`, `It's set up. Run: cli '--password [REDACTED]`],
    [`It's set up.\ncli '--password Secret\nSuffix' --verbose\necho ordinary`, `It's set up.\ncli '--password [REDACTED]`],
    [`It's set up. Run: cli $'--password Secret Suffix' --verbose`, `It's set up. Run: cli $'--password [REDACTED]`],
    [`Use a 27" screen; cli "--password Secret Suffix" --verbose`, `Use a 27" screen; cli "--password [REDACTED]`],
    // Read as shell, the text is in a quote from `Don't` on: to its end.
    [`Don't: cli "--password Secret Suffix" --verbose`, `Don't: cli "--password [REDACTED]`],
    // A comment is read on its own.
    [`# example: cli "--password Secret Suffix" --verbose`, `# example: cli "--password [REDACTED]" --verbose`],
    // Comments and words the scan reads as the shells do.
    [`echo \\\n# "\nenv "PASSWORD=prefix\nSecretSuffix" printenv PASSWORD\necho ordinary`, `echo \\\n# "\nenv "PASSWORD=[REDACTED]"\necho ordinary`],
    [`echo \\\n# '\nenv 'PASSWORD=prefix\nSecretSuffix' printenv PASSWORD\necho ordinary`, `echo \\\n# '\nenv 'PASSWORD=[REDACTED]'\necho ordinary`],
    [`cli --verbose \\\n# don't log\ncli '--password Secret Suffix' --verbose`, `cli --verbose \\\n# don't log\ncli '--password [REDACTED]' --verbose`],
    [`x=a; cli \${x}#'q\nr' '--password Secret Suffix' --verbose`, `x=a; cli \${x}#'q\nr' '--password [REDACTED]' --verbose`],
    // Past a here-document's or an arithmetic `<<` the quote is not known: to
    // the end of the text. A case statement is plain text to the scan.
    [`cat <<EOF\nharmless\nEOF\nenv "DB_"PASSWORD="prefix\nSecretSuffix" printenv DB_PASSWORD\necho ordinary`, `cat <<EOF\nharmless\nEOF\nenv "DB_"PASSWORD=[REDACTED]`],
    [`cat <<EOF\nharmless\nEOF\nenv 'DB_'PASSWORD='prefix\nSecretSuffix' printenv DB_PASSWORD\necho ordinary`, `cat <<EOF\nharmless\nEOF\nenv 'DB_'PASSWORD=[REDACTED]`],
    [`case x in x) ;; esac\nenv "DB_"PASSWORD="prefix\nSecretSuffix" printenv DB_PASSWORD\necho ordinary`, `case x in x) ;; esac\nenv "DB_"PASSWORD=[REDACTED]\necho ordinary`],
    [`echo case x in\nenv "DB_"PASSWORD="prefix\nSecretSuffix" printenv DB_PASSWORD\necho ordinary`, `echo case x in\nenv "DB_"PASSWORD=[REDACTED]\necho ordinary`],
    [`echo $((1 << 2))\nenv "DB_"PASSWORD="prefix\nSecretSuffix" printenv DB_PASSWORD\necho ordinary`, `echo $((1 << 2))\nenv "DB_"PASSWORD=[REDACTED]`],
    [`cli -D"javax.net.ssl."trustStorePassword="prefix\nSecretSuffix" --verbose\necho ordinary`, `cli -D"javax.net.ssl."trustStorePassword=[REDACTED]\necho ordinary`],
    [`cli -D'javax.net.ssl.'trustStorePassword='prefix\nSecretSuffix' --verbose\necho ordinary`, `cli -D'javax.net.ssl.'trustStorePassword=[REDACTED]\necho ordinary`],
    // In a command string another shell runs, the scan and the word alone
    // disagree: to the end of the text.
    [`sh -c "printf '<%s>' --batch --passphrase \\"correct horse battery staple\\" -d file"`, `sh -c "printf '<%s>' --batch --passphrase [REDACTED]`],
    [`sh -c "printf '<%s>' --passphrase correct\\\\ horse\\\\ battery"`, `sh -c "printf '<%s>' --passphrase [REDACTED]`],
    [`ssh host "cli --password=\\"Secret\nSuffix\\" --verbose"\necho ordinary`, `ssh host "cli --password=[REDACTED]`],
    [`ssh host "cli --password \\"two words\\" --verbose"`, `ssh host "cli --password [REDACTED]`],
    [`sh -c 'cli --password "two words" --verbose'`, `sh -c 'cli --password [REDACTED]`],
    [`docker exec app sh -c "cli --api-key \\"a b\\" run"`, `docker exec app sh -c "cli --api-key [REDACTED]`],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).not.toContain('Suffix')
  }
  // After a here-document, a quote that closes an earlier segment: past a
  // `<<` the quote is not known, so the value runs to the end of the text.
  expect(redactText(`cat <<EOF\nx\nEOF\ncli "a b"--password=x"y\nSecretSuffix" --verbose\necho ordinary`, stats))
    .toBe(`cat <<EOF\nx\nEOF\ncli "a b"--password=[REDACTED]`)
})

test('where the scan and the word alone disagree on the quote a value starts in, or the scan cannot read the text before it, the value runs to the end of the text', () => {
  const stats = newStats()
  const token = `ghp_${'0'.repeat(36)}`
  const cases: [string, string][] = [
    // Prose with an odd quote before a value quoted the ordinary way: to the
    // end of the text.
    [`It's set up. Run: cli --password 'Secret Suffix' --verbose`, `It's set up. Run: cli --password [REDACTED]`],
    [`Use a 27" screen; cli --password "Secret Suffix" --verbose`, `Use a 27" screen; cli --password [REDACTED]`],
    [`Here's the command:\n\`\`\`\ncli --password 'Secret Suffix'\n\`\`\``, `Here's the command:\n\`\`\`\ncli --password [REDACTED]`],
    [`error: can't connect; retry with --password 'Secret Suffix'`, `error: can't connect; retry with --password [REDACTED]`],
    [`It's set up. Run: cli --passphrase "it's my Suffix phrase" --verbose`, `It's set up. Run: cli --passphrase [REDACTED]`],
    [`It's set up: export DB_PASSWORD='Secret\nSuffix'\necho ordinary`, `It's set up: export DB_PASSWORD=[REDACTED]`],
    [`It's set up: cli --password='Secret\nSuffix' --verbose\necho ordinary`, `It's set up: cli --password=[REDACTED]`],
    [`Use a 27" screen; cli -Dssl.password="Secret\nSuffix" --verbose\necho ordinary`, `Use a 27" screen; cli -Dssl.password=[REDACTED]`],
    [`It's here: cli "--pass"word 'Secret Suffix' --verbose`, `It's here: cli "--pass"word [REDACTED]`],
    [`x=\nprintf '<%s>' "\${x:-it's}"; printf '<%s>' --password 'Secret Suffix' --verbose`, `x=\nprintf '<%s>' "\${x:-it's}"; printf '<%s>' --password [REDACTED] --verbose`],
    // Their mirrors: read as shell, the text is in a quote to its end.
    [`Don't: cli --password "Secret Suffix" --verbose`, `Don't: cli --password [REDACTED]`],
    [`Use a 27" screen; cli --password 'Secret Suffix' --verbose`, `Use a 27" screen; cli --password [REDACTED]`],
    // An option quoted by escaped quotes in a command string.
    [`sh -c "cli \\"--password\\" \\"Secret Suffix\\" --verbose"`, `sh -c "cli \\"--password\\" [REDACTED]`],
    [`ssh host "mysql \\"--password\\" \\"Secret Suffix\\" -e status"`, `ssh host "mysql \\"--password\\" [REDACTED]`],
    [`sh -c "cli \\"--password\\" Secret\\\\ Suffix --verbose"`, `sh -c "cli \\"--password\\" [REDACTED]`],
    [`sh -c "cli \\"--pass\\"word \\"Secret Suffix\\" --verbose"`, `sh -c "cli \\"--pass\\"word [REDACTED]`],
    // After a comment ending in a backslash, or any `<<` but `<<<`: to the end
    // of the text.
    [`# see C:\\path\\\nsh -c "printf \\"<%s>\\" --password \\"a Suffix\\"; echo"`, `# see C:\\path\\\nsh -c "printf \\"<%s>\\" --password [REDACTED]`],
    [`int main() { std::cout << x; }\nsh -c "cli --password \\"a Suffix\\" run"`, `int main() { std::cout << x; }\nsh -c "cli --password [REDACTED]`],
    [`y = 1 << 4\nssh host "cli --api-key \\"a Suffix\\" run"`, `y = 1 << 4\nssh host "cli --api-key [REDACTED]`],
    // A marker followed by a quote does not cut a longer reading short.
    [`case $(printf a) in a) :;; esac\ncli --password ${token}" ' SecretSuffix" --verbose`, `case $(printf a) in a) :;; esac\ncli --password [REDACTED] --verbose`],
    [`case $(printf a) in a) :;; esac\ncli --password ${token}' " SecretSuffix' --verbose`, `case $(printf a) in a) :;; esac\ncli --password [REDACTED] --verbose`],
    [`case $(printf a) in a) :;; esac\ncli --password [REDACTED]" ' SecretSuffix" --verbose`, `case $(printf a) in a) :;; esac\ncli --password [REDACTED] --verbose`],
    // A command string in a here-document's body or a comment.
    [`sh <<'EOF'\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\""\nEOF\necho ordinary`, `sh <<'EOF'\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`sh <<EOF\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\""\nEOF\necho ordinary`, `sh <<EOF\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`# sh -c "cli --password \\"Secret Suffix\\""\necho ordinary`, `# sh -c "cli --password [REDACTED]`],
    // A shell assignment right after `;` or `&`: its whole shell word; one an
    // expansion holds, to the end of the text.
    [`:;PASSWORD=correct\\ horse\\ SecretSuffix printenv PASSWORD`, `:;PASSWORD=[REDACTED] printenv PASSWORD`],
    [`:;PASSWORD=prefix"Secret\nSuffix" printenv PASSWORD`, `:;PASSWORD=[REDACTED] printenv PASSWORD`],
    [`:;PASSWORD=prefix$(printf "Secret\\nSuffix") printenv PASSWORD`, `:;PASSWORD=[REDACTED]`],
    [`true&PASSWORD=correct\\ horse\\ SecretSuffix printenv PASSWORD`, `true&PASSWORD=[REDACTED] printenv PASSWORD`],
    // Prose, Markdown and other code before a quoted value; a marker before
    // the rest of a value; comments, here-document bodies and prose that
    // hold a command string.
    [`I'm stuck. Please run: mysql -u admin --password 'correct horse battery Suffix' -h db.internal app`, `I'm stuck. Please run: mysql -u admin --password [REDACTED]`],
    [`Here's the fix:\n\`\`\`\nPGPASSWORD='correct horse\nbattery Suffix' psql -h db app\n\`\`\``, `Here's the fix:\n\`\`\`\nPGPASSWORD=[REDACTED]`],
    [`It's set up. Run: cli --password $'Secret Suffix' --verbose`, `It's set up. Run: cli --password [REDACTED]`],
    [`It's set up. Run: cli --password "$(printf %s 'Secret Suffix')" --verbose`, `It's set up. Run: cli --password [REDACTED]`],
    [`fn f(x: &'static str) {}\ncli --password 'Secret Suffix' --verbose`, `fn f(x: &'static str) {}\ncli --password [REDACTED]`],
    [`Use the \` key; cli --password 'Secret Suffix' --verbose`, `Use the \` key; cli --password [REDACTED]`],
    [`It's set up. Run: cli $'--pa\\x73sword' 'Secret Suffix' --verbose`, `It's set up. Run: cli $'--pa\\x73sword' [REDACTED]`],
    [`echo "\${x:-it's}"\ncli --password 'Secret Suffix' --verbose`, `echo "\${x:-it's}"\ncli --password [REDACTED] --verbose`],
    [`cli "a b"--password=[REDACTED]"Secret\nSuffix" --verbose`, `cli "a b"--password=[REDACTED]`],
    [`env "DB_\\\n"PASSWORD=[REDACTED]"Secret\nSuffix" printenv DB_PASSWORD\necho ordinary`, `env "DB_\\\n"PASSWORD=[REDACTED]`],
    [`# note \\\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `# note \\\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`cat <<EOF\nline one \\\ncontinued\nEOF\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `cat <<EOF\nline one \\\ncontinued\nEOF\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`echo x # comment\\\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `echo x # comment\\\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`case x in\na) :;; # arm\\\nb) :;;\nesac\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `case x in\na) :;; # arm\\\nb) :;;\nesac\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`cat > deploy.sh <<'EOF'\n#!/bin/sh\nssh prod "mysql --password \\"correct horse battery Suffix\\" -e 'select 1'"\nEOF\nchmod +x deploy.sh`, `cat > deploy.sh <<'EOF'\n#!/bin/sh\nssh prod "mysql --password [REDACTED]`],
    [`# usage: sh -c "cli --passphrase \\"correct horse Suffix\\" -d f"`, `# usage: sh -c "cli --passphrase [REDACTED]`],
    [`See issue #42: ssh host "deploy --password \\"pass Suffix\\" now"`, `See issue #42: ssh host "deploy --password [REDACTED]`],
    [`cat <<EOF\nssh host "deploy --password \\"pass Suffix\\" now"\nEOFX\nmore`, `cat <<EOF\nssh host "deploy --password [REDACTED]`],
    [`# cli --password 'Secret Suffix' \\\necho ordinary`, `# cli --password [REDACTED] \\\necho ordinary`],
    [`cat <<EOF\nmysql --password 'Secret Suffix' -h db \\\nmore\nEOF\necho ordinary`, `cat <<EOF\nmysql --password [REDACTED]`],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).not.toContain('Suffix')
  }
  // Query strings keep their rule.
  expect(redactText('/path?token=abc&x=1 next', stats)).toBe('/path?token=[REDACTED]&x=1 next')
})

test('a value in a command string another shell runs, or after an escape its quoting leaves in, runs to the end of the text', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    [`Use a 27" screen; sh -c "cli \\"--password Secret Suffix\\" run"\necho ordinary`, `Use a 27" screen; sh -c "cli \\"--password [REDACTED]`],
    [`Use a 27" screen; sh -c "cli --password \\"Secret Suffix\\" run"\necho ordinary`, `Use a 27" screen; sh -c "cli --password [REDACTED]`],
    [`Use a 27" screen; sh -c "cli --password \\"Secret Suffix\\" run`, `Use a 27" screen; sh -c "cli --password [REDACTED]`],
    [`Use a 27" screen; sh -c "cli --password pre\\"Secret Suffix\\" run`, `Use a 27" screen; sh -c "cli --password [REDACTED]`],
    [`Use a 27" screen; sh -c "cli --password Secret\\\\ Suffix run"`, `Use a 27" screen; sh -c "cli --password [REDACTED]`],
    [`Use a 27" screen; sh -c "cli \\"--pass\\"word \\"Secret Suffix\\" run"`, `Use a 27" screen; sh -c "cli \\"--pass\\"word [REDACTED]`],
    [`a "b\nsh -c "cli \\"--password=Secret\nSuffix\\" run"\necho ordinary`, `a "b\nsh -c "cli \\"--password=[REDACTED]`],
    [`Use a 27" screen; ssh host "env \\"PASSWORD=Secret Suffix\\" printenv"\necho ordinary`, `Use a 27" screen; ssh host "env \\"PASSWORD=[REDACTED]`],
    [`log {\\"cmd\\": \\"PASSWORD=abc\\"}\nnext line`, `log {\\"cmd\\": \\"PASSWORD=[REDACTED]`],
    [`cli \\"--password abc\\" --verbose`, `cli \\"--password [REDACTED]`],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).not.toContain('Suffix')
  }
})

test('a word is read whole across quotes, backslashes and line continuations', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    // A continuation with no space beside it right after a credential's name
    // glues what follows to it: to the end of the text.
    [`cli "--pass\\\n"word SecretSuffix --verbose\necho ordinary`, `cli "--pass\\\n[REDACTED]`],
    [`cli --pass\\\n"word" SecretSuffix --verbose\necho ordinary`, `cli --pass\\\n[REDACTED]`],
    [`cli --pass\\\n'word' SecretSuffix --verbose\necho ordinary`, `cli --pass\\\n[REDACTED]`],
    [`cli "--pass\\\r\n"word SecretSuffix --verbose\r\necho ordinary`, `cli "--pass\\\r\n[REDACTED]`],
    [`cli --pass\\\r\n"word" SecretSuffix --verbose\r\necho ordinary`, `cli --pass\\\r\n[REDACTED]`],
    [`cli --password\\\n"SecretSuffix" host\necho ordinary`, `cli --password\\\n[REDACTED]`],
    // Unless it carries the name on: the longer name's value.
    [`cli --api\\\n"-key" SecretSuffix --verbose\necho ordinary`, `cli --api\\\n"-key" [REDACTED] --verbose\necho ordinary`],
    [`env "DB_PASS"WORD=SecretSuffix printenv DB_PASSWORD\necho ordinary`, `env "DB_PASS"WORD=[REDACTED]\necho ordinary`],
    [`env DB_PASS"WORD"=SecretSuffix printenv DB_PASSWORD\necho ordinary`, `env DB_PASS"WORD"=[REDACTED]\necho ordinary`],
    [`env 'DB_PASS'WORD='Secret\nSuffix' printenv DB_PASSWORD\necho ordinary`, `env 'DB_PASS'WORD=[REDACTED]\necho ordinary`],
    [`env DB_PA\\SSWORD=SecretSuffix printenv DB_PASSWORD\necho ordinary`, `env DB_PA\\SSWORD=[REDACTED]\necho ordinary`],
    [`env "DB_PASS\\\n"WORD=SecretSuffix printenv DB_PASSWORD\necho ordinary`, `env "DB_PASS\\\n"WORD=[REDACTED]\necho ordinary`],
    [`cli -Dssl.pass"word"=SecretSuffix --verbose\necho ordinary`, `cli -Dssl.pass"word"=[REDACTED]\necho ordinary`],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).not.toContain('Suffix')
  }
  // A longer word that names no credential keeps what follows it.
  for (const text of ['cli --password\\\n"-stdin" host\necho ordinary', "cli --password\\\n'-stdin' host", 'cli --password\\\n"_file" path', 'cli "--password"\\\n-stdin host']) {
    expect(redactText(text, stats)).toBe(text)
  }
})

test('a credential option inside a quote that opens just before it loses everything to that quote\'s end', () => {
  const stats = newStats()
  const cases: [string, string][] = [
    ['cli "--password Secret\nSuffix" --verbose', 'cli "--password [REDACTED]" --verbose'],
    ["cli '--password Secret Suffix' --verbose", "cli '--password [REDACTED]' --verbose"],
    ['cli "--password Secret" --verbose', 'cli "--password [REDACTED]" --verbose'],
    // Where that quote closes with more of the word joined after it, or an
    // escaped quote opens it, the value runs to the end of the text.
    ['cli "--password Secret"Suffix --verbose', 'cli "--password [REDACTED]'],
    ['cli \\"--password Secret\\" --verbose', 'cli \\"--password [REDACTED]'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, stats)).toBe(want)
    expect(redactText(want, stats)).toBe(want)
  }
})

test('option shapes by credential names: no value is kept (198 inputs)', () => {
  const names = ['password', 'DB_PASSWORD', 'API_KEY', 'apiKey', 'token', 'NPM_TOKEN', 'client_secret', 'secret', 'passphrase', 'auth_token', 'private_key']
  const shapes = ['--opt=@N=@V', '--opt=@N:@V', '--opt= @N=@V', '--opt:@N=@V', '-o=@N=@V', '-Dfoo=@N=@V', '@N=@V', 'export @N=@V', '--@N=@V',
    '-@N=@V', '--@N @V', '@N: @V', '"@N": "@V"', 'https://x.example/?@N=@V&y=1', 'env: @N=@V', '--opt @N=@V', '-e @N=@V', '{"@N":"@V"}']
  const secret = 'Zq9SecretVal'
  const kept: string[] = []
  for (const shape of shapes) {
    for (const name of names) {
      const input = `cmd ${shape.replace('@N', name).replace('@V', secret)} tail`
      if (redactText(input, newStats()).includes(secret)) kept.push(input)
    }
  }
  expect(shapes.length * names.length).toBe(198)
  expect(kept).toEqual([])
})

// The texts a command reaches, in order: its redaction, the row's content
// and error message, the insertAll request, a dead-letter file and the lines
// read back from it, a diagnostic, and a second redaction pass (last).
function pathsOf(command: string): string[] {
  const { row } = buildRow({ eventType: 'TOOL_COMPLETED', timestampMs: 0, eventId: 'e1', agent: 'coding-agent', sessionId: 's1',
    invocationId: 'i1', userId: 'dev', traceId: null, spanId: null, parentSpanId: null,
    content: { tool: 'Bash', args: { command } }, attributes: {}, status: 'ERROR', errorMessage: `command failed: ${command}`,
    maxPayloadChars: 16384 })
  const [file = ''] = encodeLines([{ v: 1, kind: 'retry', reason: 'r', attempts: 1, failed_at: 'f', table: 't', row }])
  const once = redactText(command, newStats())
  return [once, JSON.stringify(row.content), row.error_message ?? '', insertAllBody([row]), file, JSON.stringify(parseFile(file).lines),
    redactDiagnostic(`command failed: ${command}`, 1000), redactText(once, newStats())]
}

test('substitution-held credentials stay out of rows, errors, requests, dead letters and diagnostics', () => {
  // Each command, and the text after its value that is kept.
  const commands: [string, string[]][] = [
    ['cli --password $(printf %s SecretSuffix # comment\n) --verbose\necho ordinary', []],
    ['cli --password $(printf %s prefix \\\n# )\nprintf %s SecretSuffix) --verbose', []],
    ['cli --password $(case y in x) printf %s esac;; y) printf %s SecretSuffix;; esac) --verbose', []],
    ['cli --password $(cat <<EOF\nSecretSuffix\nEOF\n) --verbose\necho ordinary', []],
    ['cli --password "$(cat <<EOF\n) " SecretSuffix\nEOF\n)" --verbose', []],
    ['cli --password $(printf %s abc#SecretSuffix) --verbose', []],
    ['cli --password $(printf %s $((1 << 2)) SecretSuffix) --verbose', []],
    ['cli --password $(ca\\\nse a in a) printf %s SecretSuffix;; esac) --verbose', []],
    ["cli --password $\\\n'prefix\\' SecretSuffix' --verbose", []],
    ['cli --password $\\\n{LOGIN:-my SecretSuffix} --verbose', []],
    ['cli --password $(cat <<< SecretSuffix) --verbose', []],
    ['cli "--password Secret\nSuffix" --verbose', ['--verbose']],
    ['cli "--password" "Secret Suffix" --verbose', ['--verbose']],
    ['cli "--password"="prefix\nSecretSuffix" --verbose\necho ordinary', ['echo ordinary']],
    ['cli --"password"="prefix\nSecretSuffix" --verbose\necho ordinary', ['echo ordinary']],
    ["cli --'password'='prefix\nSecretSuffix' --verbose\necho ordinary", ['echo ordinary']],
    ['message "password"="prefix\nSecretSuffix" more\necho ordinary', ['echo ordinary']],
    ['cli --password=prefix"Secret\nSuffix" --verbose\necho ordinary', ['echo ordinary']],
    ['cli --password=$(printf %s prefix\nprintf %s SecretSuffix) --verbose\necho ordinary', []],
    ['PASSWORD=$(cat <<EOF\nSecretSuffix\nEOF\n) cli --verbose\necho ordinary', []],
    ['cli --password $(a=( # )\nx\n); printf %s SecretSuffix) --verbose\necho ordinary', []],
    ['cli --password=$(a=( # )\nx\n)\nprintf %s SecretSuffix) --verbose\necho ordinary', []],
    ['cli --password prefix$(a=( # )\nx\n); printf %s SecretSuffix)suffix --verbose\necho ordinary', []],
    ['cli --password $(case y in (x) <esac;; y) printf %s SecretSuffix;; esac) --verbose\necho ordinary', []],
    ['cli --pass\\\nword SecretSuffix --verbose\necho ordinary', ['--verbose', 'echo ordinary']],
    ['cli --pass\\\nword=SecretSuffix --verbose\necho ordinary', []],
    ['cli "--pass"word SecretSuffix --verbose\necho ordinary', ['--verbose', 'echo ordinary']],
    ["cli --'pass'word=SecretSuffix --verbose\necho ordinary", ['echo ordinary']],
    [`cli ''"--password=prefix\nSecretSuffix" --verbose\necho ordinary`, ['echo ordinary']],
    [`cli ""'--password=prefix\nSecretSuffix' --verbose\necho ordinary`, ['echo ordinary']],
    [`cli ''''"--password Secret\nSuffix" --verbose\necho ordinary`, ['--verbose', 'echo ordinary']],
    ["cli $'--pa\\x73sword' SecretSuffix --verbose\necho ordinary", ['--verbose', 'echo ordinary']],
    ['cli "--pass"word"=prefix\nSecretSuffix" --verbose\necho ordinary', ['echo ordinary']],
    ['cli --password\\\nSecretSuffix host\necho ordinary', []],
    [`It's set up.\ncli '--password Secret\nSuffix' --verbose\necho ordinary`, []],
    [`echo \\\n# "\nenv "PASSWORD=prefix\nSecretSuffix" printenv PASSWORD\necho ordinary`, ['echo ordinary']],
    [`cat <<EOF\nharmless\nEOF\nenv "DB_"PASSWORD="prefix\nSecretSuffix" printenv DB_PASSWORD\necho ordinary`, []],
    [`sh -c "printf '<%s>' --batch --passphrase \\"correct horse battery Suffix\\" -d file"\necho ordinary`, []],
    [`ssh host "cli --password=\\"Secret\nSuffix\\" --verbose"\necho ordinary`, []],
    [`It's set up. Run: cli --password 'Secret Suffix' --verbose\necho ordinary`, []],
    [`sh -c "cli \\"--password\\" \\"Secret Suffix\\" --verbose"\necho ordinary`, []],
    [`sh <<'EOF'\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\""\nEOF\necho ordinary`, []],
    [`:;PASSWORD=correct\\ horse\\ SecretSuffix printenv PASSWORD\necho ordinary`, ['printenv PASSWORD', 'echo ordinary']],
    [`case $(printf a) in a) :;; esac\ncli --password ghp_${'0'.repeat(36)}" ' SecretSuffix" --verbose`, ['--verbose']],
    [`I'm stuck. Please run: mysql -u admin --password 'correct horse battery Suffix' -h db.internal app`, []],
    [`env "DB_\\\n"PASSWORD=ghp_${'0'.repeat(36)}"Secret\nSuffix" printenv DB_PASSWORD\necho ordinary`, []],
    [`cat <<EOF\nline one \\\ncontinued\nEOF\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, ['line one']],
    [`cat > deploy.sh <<'EOF'\nssh prod "mysql --password \\"correct horse Suffix\\" -e 'select 1'"\nEOF\nchmod +x deploy.sh`, []],
    [`Use a 27" screen; sh -c "cli \\"--password Secret Suffix\\" run"\necho ordinary`, []],
    [`cli "--pass\\\n"word SecretSuffix --verbose\necho ordinary`, []],
    [`env "DB_PASS"WORD=SecretSuffix printenv DB_PASSWORD\necho ordinary`, ['echo ordinary']],
    ['cli --password=\\\nSecretSuffix --verbose', []],
    ['cli --password "$(cat <<EOF\ntext\\\nEOF\n) " SecretSuffix\nEOF\n)" --verbose', []],
    ['cli --password $(printf %s A;#x )\nprintf %s SecretSuffix) --verbose', []],
  ]
  for (const [command, kept] of commands) {
    const paths = pathsOf(command)
    for (const [n, text] of paths.entries()) {
      expect(text).not.toContain('Suffix')
      // The last path is a second pass: it may redact more, never less.
      if (n < paths.length - 1) for (const k of kept) expect(text).toContain(k)
    }
  }
})

test('round 8 and 9 review inputs leave no part of a credential in any path; a value whose end is not certain takes all that follows it', () => {
  const token = `ghp_${'0'.repeat(36)}`
  const cases: [string, string][] = [
    // Round 8, A: prose, an odd quote or a lone backtick before a quoted value.
    [`It's set up. Run: cli --password 'Secret Suffix' --verbose`, `It's set up. Run: cli --password [REDACTED]`],
    [`Use a 27" screen; cli --password "Secret Suffix" --verbose`, `Use a 27" screen; cli --password [REDACTED]`],
    [`Use a 27" screen; cli --password="Secret\nSuffix" --verbose\necho ordinary`, `Use a 27" screen; cli --password=[REDACTED]`],
    [`I'm stuck. Please run: mysql -u admin --password 'correct horse battery Suffix' -h db.internal app`, `I'm stuck. Please run: mysql -u admin --password [REDACTED]`],
    [`Here's the fix:\n\`\`\`\nPGPASSWORD='correct horse\nbattery Suffix' psql -h db app\n\`\`\``, `Here's the fix:\n\`\`\`\nPGPASSWORD=[REDACTED]`],
    [`It's set up. Run: cli --password $'Secret Suffix' --verbose`, `It's set up. Run: cli --password [REDACTED]`],
    [`It's set up. Run: cli --password "$(printf %s 'Secret Suffix')" --verbose`, `It's set up. Run: cli --password [REDACTED]`],
    [`fn f(x: &'static str) {}\ncli --password 'Secret Suffix' --verbose`, `fn f(x: &'static str) {}\ncli --password [REDACTED]`],
    [`Use the \` key; cli --password 'Secret Suffix' --verbose`, `Use the \` key; cli --password [REDACTED]`],
    [`It's set up. Run: cli $'--pa\\x73sword' 'Secret Suffix' --verbose`, `It's set up. Run: cli $'--pa\\x73sword' [REDACTED]`],
    [`echo "\${x:-it's}"\ncli --password 'Secret Suffix' --verbose`, `echo "\${x:-it's}"\ncli --password [REDACTED] --verbose`],
    [`cli "a b"--password=[REDACTED]"Secret\nSuffix" --verbose`, `cli "a b"--password=[REDACTED]`],
    // Round 8, B: a marker, or a token the token rule makes one, before the rest of a value.
    [`env "DB_\\\n"PASSWORD=[REDACTED]"Secret\nSuffix" printenv DB_PASSWORD\necho ordinary`, `env "DB_\\\n"PASSWORD=[REDACTED]`],
    [`env "DB_\\\n"PASSWORD=${token}"Secret\nSuffix" printenv DB_PASSWORD\necho ordinary`, `env "DB_\\\n"PASSWORD=[REDACTED]`],
    // Round 8, C: a comment or a here-document line that ends in a backslash before a command string.
    [`# note \\\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `# note \\\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`cat <<EOF\nline one \\\ncontinued\nEOF\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `cat <<EOF\nline one \\\ncontinued\nEOF\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`echo x # comment\\\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `echo x # comment\\\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    [`case x in\na) :;; # arm\\\nb) :;;\nesac\nsh -c "printf '<%s>\\n' --password \\"Secret Suffix\\" --verbose"\necho ordinary`, `case x in\na) :;; # arm\\\nb) :;;\nesac\nsh -c "printf '<%s>\\n' --password [REDACTED]`],
    // Round 8, D: a command string in a here-document, a comment or prose.
    [`cat > deploy.sh <<'EOF'\n#!/bin/sh\nssh prod "mysql --password \\"correct horse battery Suffix\\" -e 'select 1'"\nEOF\nchmod +x deploy.sh`, `cat > deploy.sh <<'EOF'\n#!/bin/sh\nssh prod "mysql --password [REDACTED]`],
    [`# usage: sh -c "cli --passphrase \\"correct horse Suffix\\" -d f"`, `# usage: sh -c "cli --passphrase [REDACTED]`],
    [`See issue #42: ssh host "deploy --password \\"pass Suffix\\" now"`, `See issue #42: ssh host "deploy --password [REDACTED]`],
    [`cat <<EOF\nssh host "deploy --password \\"pass Suffix\\" now"\nEOFX\nmore`, `cat <<EOF\nssh host "deploy --password [REDACTED]`],
    // Round 8, E: a quoted part a continuation joins to a credential name.
    [`cli "--pass\\\n"word SecretSuffix --verbose\necho ordinary`, `cli "--pass\\\n[REDACTED]`],
    [`cli --pass\\\n"word" SecretSuffix --verbose\necho ordinary`, `cli --pass\\\n[REDACTED]`],
    [`cli --pass\\\n'word' SecretSuffix --verbose\necho ordinary`, `cli --pass\\\n[REDACTED]`],
    [`cli "--pass\\\r\n"word SecretSuffix --verbose\r\necho ordinary`, `cli "--pass\\\r\n[REDACTED]`],
    [`cli --pass\\\r\n"word" SecretSuffix --verbose\r\necho ordinary`, `cli --pass\\\r\n[REDACTED]`],
    // Round 9, A: an option word quoted or escaped in pieces, after prose, in a comment or a here-document.
    [`Use a 27" screen; cli --"password" "Secret Suffix" --verbose`, `Use a 27" screen; cli --"password" [REDACTED]`],
    [`Use a 27" screen; cli --'password' "Secret Suffix" --verbose`, `Use a 27" screen; cli --'password' [REDACTED]`],
    [`Use a 27" screen; cli "--pass"word "Secret Suffix" --verbose`, `Use a 27" screen; cli "--pass"word [REDACTED]`],
    [`Use a 27" screen; cli --pa\\ssword "Secret\nSuffix" --verbose`, `Use a 27" screen; cli --pa\\ssword [REDACTED]`],
    [`Use a 27" screen; cli $'--pa\\x73sword' "Secret Suffix" --verbose`, `Use a 27" screen; cli $'--pa\\x73sword' [REDACTED]`],
    [`Use a 27" screen; cli --"password" [REDACTED]"Secret Suffix" --verbose`, `Use a 27" screen; cli --"password" [REDACTED]`],
    [`# Use a 27" screen; cli --"password" "Secret Suffix" --verbose`, `# Use a 27" screen; cli --"password" [REDACTED]`],
    [`cat <<'EOF'\nUse a 27" screen; cli --"password" "Secret Suffix" --verbose\nEOF\necho ordinary`, `cat <<'EOF'\nUse a 27" screen; cli --"password" [REDACTED]`],
    // Round 9, B: an escaped backtick in a command string.
    [`Use a 27" screen; sh -c "cli --password \\\`printf %s SecretSuffix\\\` --verbose"`, `Use a 27" screen; sh -c "cli --password [REDACTED]`],
    [`# Use a 27" screen; sh -c "cli --password \\\`printf %s SecretSuffix\\\` --verbose"`, `# Use a 27" screen; sh -c "cli --password [REDACTED]`],
    [`cat <<'EOF'\nsh -c "cli --password \\\`printf %s SecretSuffix\\\` --verbose"\nEOF\necho ordinary`, `cat <<'EOF'\nsh -c "cli --password [REDACTED]`],
    // Round 9, C: a command string's escaped or continued option name.
    [`sh -c "cli --pa\\\\ssword \\"Secret Suffix\\" --verbose"`, `sh -c "cli --pa\\\\ssword [REDACTED]`],
    [`sh -c "cli --pass\\\\\nword SecretSuffix --verbose"`, `sh -c "cli --pass\\\\\n[REDACTED]`],
    [`sh -c "cli --pa\\\\ssword=\\"Secret Suffix\\" --verbose"\necho ordinary`, `sh -c "cli --pa\\\\ssword=[REDACTED]`],
    [`sh -c "cli --pass\\\\\nword=SecretSuffix --verbose"\necho ordinary`, `sh -c "cli --pass\\\\\n[REDACTED]`],
    // Round 9, D: a name joined from quoted or escaped parts after another option's `=`.
    [`kubectl create secret generic demo --from-literal=pass"word"=SecretSuffix`, `kubectl create secret generic demo --from-literal=pass"word"=[REDACTED]`],
    [`gcloud run deploy svc --set-env-vars=DB_PASS"WORD"=SecretSuffix --region us\necho ordinary`, `gcloud run deploy svc --set-env-vars=DB_PASS"WORD"=[REDACTED]\necho ordinary`],
    [`terraform apply -var=db_pass"word"=SecretSuffix`, `terraform apply -var=db_pass"word"=[REDACTED]`],
    [`kubectl create secret generic demo --from-literal=pass"word"="Secret\nSuffix" --dry-run=client\necho ordinary`, `kubectl create secret generic demo --from-literal=pass"word"=[REDACTED]\necho ordinary`],
    [`kubectl create secret generic demo --from-literal=pass\\word=SecretSuffix --dry-run=client\necho ordinary`, `kubectl create secret generic demo --from-literal=pass\\word=[REDACTED]\necho ordinary`],
    [`sh -c "kubectl create secret generic demo --from-literal=pass\\"word\\"='Secret Suffix'"\necho ordinary`, `sh -c "kubectl create secret generic demo --from-literal=pass\\"word\\"=[REDACTED]`],
    // Arrays, comments and case statements in a substitution, and a continued name.
    [`cli --password $(a=( # )\nx\n); printf %s SecretSuffix) --verbose\necho ordinary`, `cli --password [REDACTED]`],
    [`cli --password=$(a=( # )\nx\n)\nprintf %s SecretSuffix) --verbose\necho ordinary`, `cli --password=[REDACTED]`],
    [`cli --password prefix$(a=( # )\nx\n); printf %s SecretSuffix)suffix --verbose\necho ordinary`, `cli --password [REDACTED]`],
    [`cli --password $(case y in (x) <esac;; y) printf %s SecretSuffix;; esac) --verbose`, `cli --password [REDACTED]`],
    [`cli --pass\\\nword SecretSuffix --verbose`, `cli --pass\\\nword [REDACTED] --verbose`],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, newStats())).toBe(want)
    for (const text of pathsOf(input)) expect(text).not.toContain('Suffix')
  }
})

test('a generated sweep: in each credential form, a value an expansion, a quote or a continuation opens runs to the end of the text in every path', () => {
  // Values whose end the simple rules cannot tell.
  const values = [
    '$(printf %s pre SecretSuffix)',
    '`printf %s pre SecretSuffix`',
    '${X:-pre SecretSuffix}',
    "$'pre SecretSuffix'",
    '<(printf %s pre SecretSuffix)',
    '"$(printf %s pre) SecretSuffix"',
    '"pre SecretSuffix',
    'pre\\\nSecretSuffix',
  ]
  // Each form, and what is left of it.
  const forms: [(value: string) => string, string][] = [
    [value => `cli --password ${value} --verbose\necho ordinary`, 'cli --password [REDACTED]'],
    [value => `cli --password=${value} --verbose\necho ordinary`, 'cli --password=[REDACTED]'],
    [value => `cli "--password"=${value} --verbose\necho ordinary`, 'cli "--password"=[REDACTED]'],
    [value => `PASSWORD=${value} cli --verbose\necho ordinary`, 'PASSWORD=[REDACTED]'],
  ]
  for (const [form, left] of forms) {
    for (const value of values) {
      const command = form(value)
      expect(redactText(command, newStats())).toBe(left)
      for (const text of pathsOf(command)) {
        expect(text).not.toMatch(/Secret|Suffix/)
        // All that follows the value goes with it.
        expect(text).not.toMatch(/--verbose|echo ordinary/)
      }
    }
  }
})

test('a credential whose end the simple rules tell keeps what follows it, in every path', () => {
  // Each command, what is left of it, and text after its value that is kept.
  const cases: [string, string, string[]][] = [
    ['cli --password plain --verbose', 'cli --password [REDACTED] --verbose', ['--verbose']],
    ["--password 'a b' --verbose", '--password [REDACTED] --verbose', ['--verbose']],
    ['--password=x;echo ok', '--password=[REDACTED];echo ok', ['echo ok']],
    ['cli --password SecretSuffix --verbose', 'cli --password [REDACTED] --verbose', ['--verbose']],
    ["cli --password 'Secret Suffix' --verbose\necho ordinary", 'cli --password [REDACTED] --verbose\necho ordinary', ['--verbose', 'echo ordinary']],
    ['cli --password "Secret Suffix" --verbose', 'cli --password [REDACTED] --verbose', ['--verbose']],
    ['cli --password Secret\\ Suffix --verbose', 'cli --password [REDACTED] --verbose', ['--verbose']],
    [`cli --password 'Secret '"Suffix"x --verbose`, 'cli --password [REDACTED] --verbose', ['--verbose']],
    ['cli --password "pre\\"SecretSuffix" --verbose', 'cli --password [REDACTED] --verbose', ['--verbose']],
    ['cli --password=SecretSuffix;echo ok', 'cli --password=[REDACTED];echo ok', ['echo ok']],
    ['cli --password=SecretSuffix|tee out.log', 'cli --password=[REDACTED]|tee out.log', ['tee out.log']],
    ['cli --password SecretSuffix && echo ok', 'cli --password [REDACTED] && echo ok', ['echo ok']],
    ['(cli --password SecretSuffix) && echo ok', '(cli --password [REDACTED]) && echo ok', ['echo ok']],
    ['cli --password SecretSuffix>out.txt', 'cli --password [REDACTED]>out.txt', ['out.txt']],
    ['cli "--password Secret Suffix" --verbose', 'cli "--password [REDACTED]" --verbose', ['--verbose']],
    ['cmd;PASSWORD="Secret Suffix" cli --verbose', 'cmd;PASSWORD=[REDACTED] cli --verbose', ['cli --verbose']],
    ['env "DB_"PASSWORD="Secret\nSuffix" printenv DB_PASSWORD\necho ordinary', 'env "DB_"PASSWORD=[REDACTED]\necho ordinary', ['echo ordinary']],
    ["mysql -u admin --password 'correct horse battery Suffix' -h db.internal app", 'mysql -u admin --password [REDACTED] -h db.internal app', ['-h db.internal app']],
    ["cli $'--pa\\x73sword' SecretSuffix --verbose", "cli $'--pa\\x73sword' [REDACTED] --verbose", ['--verbose']],
    ['cli --pass\\\nword SecretSuffix --verbose\necho ordinary', 'cli --pass\\\nword [REDACTED] --verbose\necho ordinary', ['--verbose', 'echo ordinary']],
    ['kubectl create secret generic demo --from-literal=pass"word"=SecretSuffix --dry-run=client\necho ordinary', 'kubectl create secret generic demo --from-literal=pass"word"=[REDACTED]\necho ordinary', ['echo ordinary']],
  ]
  for (const [command, want, kept] of cases) {
    expect(redactText(command, newStats())).toBe(want)
    for (const text of pathsOf(command)) {
      expect(text).not.toContain('Suffix')
      for (const k of kept) expect(text).toContain(k)
    }
  }
})

test('text with no credential is left as it is, byte for byte, in every path', () => {
  const commands = [
    'printf %s case',
    'cat <<EOF\nhi\nEOF\necho ordinary',
    'echo abc#def',
    'echo $((1 << 2))',
    'ls --verbose',
    '#!/bin/sh\nset -eu\n# build, then list\nfor f in *.txt; do\n  printf \'%s\\n\' "$f" | tr a-z A-Z > "out/$f"\ndone\ncase "$1" in\n  start) echo start;;\n  *) echo "usage: $0 start" >&2; exit 1;;\nesac\nx=$(date +%s)\ny=`whoami`\necho "${x:-0} $y" 2>&1 | tee log.txt',
    `git log --format='%H %s' -n 3 && echo "it's done"`,
    'docker login --password-stdin registry.example.com',
    'cli --password\\\n-stdin host',
    'cli --password\\\n"-stdin" host\necho ordinary',
    'sh -c "cli --password\\\\\n-stdin host"',
    'tool --token-file /etc/token --verbose',
    'cli --max-tokens 1024',
    'cli --password --verbose',
    // A backslash in a quote the word opens itself is literal: the shell
    // passes `--pa\ssword`, no option.
    "cli '--pa\\ssword' ordinary",
    "'--pa\\ssword' ordinary",
    'cli "--pa\\\\ssword" ordinary',
    "cli '-\\-password' ordinary --verbose",
    "printf '%s\\n' 'It'\"'\"'s done' && echo ordinary",
    // A JSON-encoded continuation that carries the name on.
    'log "cli --password\\\\\\n-stdin host"',
    'log "cli --password\\\\\\r\\n-stdin host"',
    // Lists of names and values with no credential name in them, and an
    // escaped `=` after a name that is no credential's.
    'gcloud run deploy svc --set-env-vars=ORDINARY=x,MODE="a b" --region us',
    'gcloud run deploy svc --set-env-vars ^:^A=1:B=2 --labels=team=x,env=prod',
    'mount -o user=x,uid=1000 //host/share /mnt',
    'helm install demo ./chart --set image.tag=v1,db.host=x',
    "sed 's/password\\=.*/x/' config.ini",
    'cli --opt=a\\=b --verbose',
    `echo "x=1, y='2'" && echo ordinary`,
    // Lists with expansions, command substitutions and long values, and
    // substitutions joined by commas.
    'gcloud run deploy svc --set-env-vars=VERSION=$(git rev-parse HEAD),MODE=prod --region us',
    'gcloud run deploy svc --set-env-vars=ORDINARY=${VALUE},MODE="a b" --region us\necho ordinary',
    'cli --labels=v=`date +%s`,team=x --set-env-vars=A=$X,B=${Y:-z},C=$1',
    `cli --set-env-vars=A=${'a'.repeat(300)},B=c,${'b'.repeat(250)}=d --verbose`,
    'echo "$(date),$(whoami)" && printf \'%s\\n\' "$(ls),x=1"',
    'x=`date`,y=1; echo "$x"',
  ]
  const commandOf = (content: string | undefined): string => (JSON.parse(content ?? '{}') as { args?: { command?: string } }).args?.command ?? ''
  for (const command of commands) {
    const [once, content = 'null', error, request = '{}', , lines = '[]', diagnostic, twice] = pathsOf(command)
    expect([once, twice, error, diagnostic]).toEqual([command, command, `command failed: ${command}`, `command failed: ${command}`])
    expect(commandOf(JSON.parse(content) as string)).toBe(command)
    expect(commandOf((JSON.parse(request) as { rows: { json: { content: string } }[] }).rows[0]?.json.content)).toBe(command)
    expect(commandOf((JSON.parse(lines) as { row: { content: string } }[])[0]?.row.content)).toBe(command)
  }
})

test('round 19 review inputs leave no part of a credential in any path; a value whose end is not certain takes all that follows it', () => {
  const token = `ghp_${'0'.repeat(36)}`
  // A log line that quotes a command as a JSON string.
  const json = (command: string): string => JSON.stringify(command).slice(1, -1)
  const cases: [string, string][] = []
  // P1 1: after an odd `"`, an option word a quote or a backslash splits.
  for (const word of ['--pass"word"', '"--pass"word', "--pass''word", '-\\-password', '--pa\\ssword', '--api"-key"']) {
    cases.push(
      [`Use a 27" screen; cli ${word} "Secret Suffix" --verbose`, `Use a 27" screen; cli ${word} [REDACTED]`],
      [`He measured 13" and wrote:\ncli ${word} "Secret Suffix" --verbose`, `He measured 13" and wrote:\ncli ${word} [REDACTED]`],
      [`Use a 27" screen; mysql ${word} "correct horse Suffix" -h db`, `Use a 27" screen; mysql ${word} [REDACTED]`],
    )
  }
  cases.push(
    [`Use a 27" screen; cli --pass"word" ${token}" ' SecretSuffix" --verbose`, `Use a 27" screen; cli --pass"word" [REDACTED]`],
    // P1 2: an unquoted here-document's body, which its shell reads once more.
    [`cat > run.sh <<EOF\n#!/bin/sh\nexec cli --password \\\\\n  'correct horse battery Suffix' --verbose\nEOF\nsh run.sh`, `cat > run.sh <<EOF\n#!/bin/sh\nexec cli --password [REDACTED]`],
    [`ssh prod bash -s <<EOF\nmysql -u app --password \\\\\n  "correct horse Suffix" -e 'select 1'\nEOF`, `ssh prod bash -s <<EOF\nmysql -u app --password [REDACTED]`],
    [`sh <<EOF\ncli --password \\\`printf %s SecretSuffix\\\` --verbose\nEOF`, `sh <<EOF\ncli --password [REDACTED]`],
    [`cat > run.sh <<-EOF\n\t#!/bin/sh\n\texec cli --password \\\\\n\t  'correct horse battery Suffix' --verbose\n\tEOF\nsh run.sh`, `cat > run.sh <<-EOF\n\t#!/bin/sh\n\texec cli --password [REDACTED]`],
    [`ssh prod bash -s <<-EOF\n\tmysql -u app --password \\\\\n\t  "correct horse Suffix" -e 'select 1'\n\tEOF`, `ssh prod bash -s <<-EOF\n\tmysql -u app --password [REDACTED]`],
    [`sh <<-EOF\n\tcli --password \\\`printf %s SecretSuffix\\\` --verbose\n\tEOF`, `sh <<-EOF\n\tcli --password [REDACTED]`],
    [`sh <<EOF\n:;PASSWORD=correct\\\\ horse\\\\ SecretSuffix printenv PASSWORD\nEOF\necho ordinary`, `sh <<EOF\n:;PASSWORD=[REDACTED]`],
    [`Use a 27" screen; sh <<EOF\ncli --password correct\\\\ horse\\\\ Suffix --verbose\nEOF`, `Use a 27" screen; sh <<EOF\ncli --password [REDACTED]`],
    // P1 3: a single-quoted command string's quoted option, and escapes in a
    // command string after prose.
    [`sh -c 'cli '\\''--password'\\'' '\\''Secret Suffix'\\'' --verbose'`, `sh -c 'cli '\\''--password'\\'' [REDACTED]`],
    [`ssh host 'mysql '\\''--password'\\'' '\\''correct horse Suffix'\\'' -e status'`, `ssh host 'mysql '\\''--password'\\'' [REDACTED]`],
    [`sh -c 'cli '"'"'--password'"'"' '"'"'Secret Suffix'"'"' --verbose'`, `sh -c 'cli '"'"'--password'"'"' [REDACTED]`],
    [`sh -c 'cli '\\''--password'\\'' "Secret Suffix" --verbose'`, `sh -c 'cli '\\''--password'\\'' [REDACTED]`],
    [`Use a 27" screen; sh -c "cli --password \\\`printf %s SecretSuffix\\\` --verbose"`, `Use a 27" screen; sh -c "cli --password [REDACTED]`],
    [`Use a 27" screen; ssh host "cli --password \\\`printf %s SecretSuffix\\\` --verbose"`, `Use a 27" screen; ssh host "cli --password [REDACTED]`],
    [`Use a 27" screen; eval "cli --password \\\`printf %s SecretSuffix\\\` --verbose"`, `Use a 27" screen; eval "cli --password [REDACTED]`],
    [`It's set up. Run: sh -c 'cli --password $'\\''Secret Suffix'\\'' --verbose'`, `It's set up. Run: sh -c 'cli --password [REDACTED]`],
    // The same single-quote join with `=`, in a name, and before an assignment.
    [`sh -c 'cli '"'"'--password'"'"'='"'"'Secret Suffix'"'"' --verbose'`, `sh -c 'cli '"'"'--password'"'"'=[REDACTED]`],
    [`sh -c 'cli '"'"'--pass'"'"'word '"'"'Secret Suffix'"'"' --verbose'`, `sh -c 'cli '"'"'--pass'"'"'word [REDACTED]`],
    [`ssh host 'mysql '"'"'--password'"'"' '"'"'correct horse Suffix'"'"' -e status'`, `ssh host 'mysql '"'"'--password'"'"' [REDACTED]`],
    [`sh -c 'env '"'"'DB_PASS'"'"'WORD=Secret\\ Suffix printenv'`, `sh -c 'env '"'"'DB_PASS'"'"'WORD=[REDACTED]`],
    // After an apostrophe the scan reads such a string's quotes the other way
    // round, and agrees with the word alone.
    [`fn f(x: &'static str) {}\nsh -c 'cli --password \\\n'"'"'Secret"Suffix'"'"'"Secret Suffix" --verbose'\necho ordinary`, `fn f(x: &'static str) {}\nsh -c 'cli --password \\\n[REDACTED]`],
    [`It's set up. Run: sh -c 'cli --password '"'"'Secret Suffix'"'"' --verbose'`, `It's set up. Run: sh -c 'cli --password [REDACTED]`],
    // A backslash in a quote the word opens itself is read again where a
    // command runs its arguments, joined, as shell code.
    [`ssh host cli '--pa\\ssword' 'Secret Suffix' --verbose`, `ssh host cli '--pa\\ssword' [REDACTED]`],
    [`eval cli '--pa\\ssword' 'Secret Suffix' --verbose`, `eval cli '--pa\\ssword' [REDACTED]`],
    [`watch -n 5 cli '--pa\\ssword' 'Secret Suffix' --verbose`, `watch -n 5 cli '--pa\\ssword' [REDACTED]`],
    // P1 4: a command string's option name with an escaped backslash or a
    // continuation in it, the continued part quoted or not.
    [`sh -c "cli --pa\\\\ssword 'Secret Suffix' --verbose"`, `sh -c "cli --pa\\\\ssword [REDACTED]`],
    [`sh -c "cli --pass\\\\\nword 'Secret Suffix' --verbose"`, `sh -c "cli --pass\\\\\n[REDACTED]`],
    [`sh -c "cli --pass\\\\\n'word' 'Secret Suffix' --verbose"`, `sh -c "cli --pass\\\\\n[REDACTED]`],
    [`sh -c "cli --pass\\\\\n\\"word\\" 'Secret Suffix' --verbose"`, `sh -c "cli --pass\\\\\n[REDACTED]`],
  )
  // A continuation in a name as a JSON string encodes it (`\\\n`, `\\\r\n`):
  // in an assignment after another option's `=`, a long option, an
  // assignment, glued to a value, in a command string and in a list.
  for (const eol of ['\n', '\r\n']) {
    const join = json(`\\${eol}`)
    cases.push(
      [`log "${json(`cli --set-env-vars=DB_PASS\\${eol}WORD=SecretSuffix`)}"`, `log "cli --set-env-vars=DB_PASS${join}WORD=[REDACTED]`],
      [`log "${json(`cli --pass\\${eol}word SecretSuffix --verbose`)}"`, `log "cli --pass${join}word [REDACTED]`],
      [`log "${json(`cli --pass\\${eol}word=SecretSuffix --verbose`)}"`, `log "cli --pass${join}[REDACTED]`],
      [`log "${json(`DB_PASS\\${eol}WORD=SecretSuffix printenv DB_PASSWORD`)}"`, `log "DB_PASS${join}WORD=[REDACTED]"`],
      [`log "${json(`cli --password\\${eol}SecretSuffix --verbose`)}"`, `log "cli --password${join}[REDACTED]`],
      [`log "${json(`sh -c "cli --pass\\\\${eol}word SecretSuffix --verbose"`)}"`, `log "sh -c \\"cli --pass${json(`\\\\${eol}`)}[REDACTED]`],
      [`args: ["cli", "${json(`--password\\${eol}SecretSuffix`)}", "--verbose"]`, `args: ["cli", "--password${join}[REDACTED]`],
    )
  }
  for (const [input, want] of cases) {
    expect(redactText(input, newStats())).toBe(want)
    for (const text of pathsOf(input)) expect(text).not.toMatch(/Secret|Suffix|correct|horse|battery|ghp_|0{6}/)
  }
})

test('a generated sweep: after prose that the quote scan misreads, an option word in each spelling loses each value in every path', () => {
  const prefixes = ['', `It's set up. Run: `, `Use a 27" screen; `, `He measured 13" and wrote:\n`, `error: can't connect; `, "Here's the command:\n```\n",
    'Use the ` key; ', "fn f(x: &'static str) {}\n", `He said "use\n`, `x=\nprintf '<%s>' "\${x:-it's}"; `]
  const words = ['--password', '"--pass"word', '--pass"word"', "--pass''word", '-\\-password', "$'--pa\\x73sword'", '--pa\\ssword', '"--password"', "'--password'",
    '--api"-key"', '--passphrase']
  const values = [`"Secret Suffix"`, `'Secret Suffix'`, `Secret\\ Suffix`, `$'Secret Suffix'`, `"Secret"' Suffix'`, `pre"Secret Suffix"`,
    `"$(printf '%s %s' Secret Suffix)"`, `"it's my Suffix"`, `"Secret\nSuffix"`, `'Secret\nSuffix'`]
  let inputs = 0
  for (const prefix of prefixes) {
    for (const word of words) {
      for (const value of values) {
        const input = `${prefix}cli ${word} ${value} --verbose\necho ordinary`
        inputs += 1
        for (const text of pathsOf(input)) expect(text).not.toMatch(/Secret|Suffix/)
      }
    }
  }
  expect(inputs).toBe(1100)
})

// How long each path takes for a command, in milliseconds, at the default
// payload cap or another: its redaction, the row's content and its error
// message, the insertAll request, the dead-letter file and its lines read
// back, a diagnostic, and a second redaction pass.
function pathTimes(command: string, maxPayloadChars = 16384, diagnosticChars = 1000): [string, number][] {
  const times: [string, number][] = []
  const time = <T>(path: string, run: () => T): T => {
    const started = performance.now()
    const out = run()
    times.push([path, performance.now() - started])
    return out
  }
  const row = (content: unknown, errorMessage: string | null) => buildRow({ eventType: 'TOOL_COMPLETED', timestampMs: 0, eventId: 'e1', agent: 'coding-agent', sessionId: 's1',
    invocationId: 'i1', userId: 'dev', traceId: null, spanId: null, parentSpanId: null, content, attributes: {}, status: 'ERROR', errorMessage,
    maxPayloadChars }).row
  const content = { tool: 'Bash', args: { command } }
  const once = time('redaction', () => redactText(command, newStats()))
  time('row content', () => row(content, null))
  time('row error_message', () => row(null, `command failed: ${command}`))
  time('insertAll request', () => insertAllBody([row(content, null)]))
  const [file = ''] = time('dead-letter file', () => encodeLines([{ v: 1, kind: 'retry', reason: 'r', attempts: 1, failed_at: 'f', table: 't', row: row(content, `command failed: ${command}`) }]))
  time('dead-letter lines read back', () => parseFile(file))
  time('diagnostic', () => redactDiagnostic(`command failed: ${command}`, diagnosticChars))
  time('second pass', () => redactText(once, newStats()))
  return times
}

test('a long run of line continuations as a JSON string encodes them, and other 100,000-character runs, cost each path a few milliseconds', () => {
  // The review's input: a log line that quotes, as a JSON string, line
  // continuations as JSON encodes them (`\\\n`, then `\\\r\n`), 100,000
  // characters with no credential; then the same with no log line around it.
  const unit = '\\'.repeat(3) + 'n' + '\\'.repeat(3) + 'r\\n'
  const logged = `log "${unit.repeat(9999)}"    `
  expect(logged.length).toBe(100_000)
  expect(typeof JSON.parse(logged.slice(4).trim())).toBe('string')
  expect(redactText(logged, newStats())).toBe(logged)
  const texts = [
    logged,
    unit.repeat(10_000),
    // A name, then a long run of continuations, or of spaces and
    // continuations, then an `=` or none; and a name after each raw one.
    `a${'\\\\\\n'.repeat(24_999)}=x`,
    `password${' \\\\\\n'.repeat(19_998)}x`,
    `${'a\\\n'.repeat(33_332)}=x`,
    // A list of 25,000 names and values, and 50,000 `(`, each of which
    // starts a word.
    `--set-env-vars=${'A=x,'.repeat(24_990)}DB_PASS"WORD"=SecretSuffix`,
    '$('.repeat(50_000),
  ]
  for (const text of texts) {
    expect(text.length > 99_000).toBe(true)
    for (const [path, ms] of pathTimes(text)) expect([path, ms < 100]).toEqual([path, true])
  }
})

test('an escaped `=` after a credential name, and a credential name spelled in pieces in a list of names and values, leave no part of the credential in any path', () => {
  const json = (command: string): string => JSON.stringify(command).slice(1, -1)
  const cases: [string, string][] = [
    // An escaped `=` after an option's name, or after a name after another
    // option's `=`: the shell passes `--password=…` and `PASSWORD=…`.
    ['cli --password\\=SecretSuffix --verbose', 'cli --password\\=[REDACTED]'],
    ['cli --from-literal=PASSWORD\\=SecretSuffix', 'cli --from-literal=PASSWORD\\=[REDACTED]'],
    ['cli --password\\="Secret Suffix" --verbose\necho ordinary', 'cli --password\\=[REDACTED]\necho ordinary'],
    ['kubectl create secret generic demo --from-literal=PASSWORD\\=SecretSuffix --dry-run=client\necho ordinary', 'kubectl create secret generic demo --from-literal=PASSWORD\\=[REDACTED]\necho ordinary'],
    ['env DB_PASSWORD\\=SecretSuffix printenv DB_PASSWORD\necho ordinary', 'env DB_PASSWORD\\=[REDACTED]\necho ordinary'],
    ['cli --set-env-vars=ORDINARY=x,DB_PASSWORD\\=SecretSuffix --region us\necho ordinary', 'cli --set-env-vars=ORDINARY=x,DB_PASSWORD\\=[REDACTED]\necho ordinary'],
    // A name joined from quoted parts after a comma in a list of names and
    // values: the shell passes `…,DB_PASSWORD=…`.
    ['cli --set-env-vars=ORDINARY=x,DB_PASS"WORD"=SecretSuffix', 'cli --set-env-vars=ORDINARY=x,DB_PASS"WORD"=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=ORDINARY=x,DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=ORDINARY=x,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    // The list after a space, in quotes with `;` between, with gcloud's own
    // delimiter, after `-o` and `--set`, and past eight `=`.
    ['gcloud run deploy svc --set-env-vars ORDINARY=x,DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars ORDINARY=x,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars="ORDINARY=x;DB_PASS"WORD"=Secret Suffix" --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars="ORDINARY=x;DB_PASS"WORD"=[REDACTED]"\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^:^ORDINARY=x:DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^:^ORDINARY=x:DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['mount -o user=x,pass"word"=SecretSuffix //host/share /mnt\necho ordinary', 'mount -o user=x,pass"word"=[REDACTED]\necho ordinary'],
    ['helm install demo ./chart --set image.tag=v1,db.pass"word"=SecretSuffix\necho ordinary', 'helm install demo ./chart --set image.tag=v1,db.pass"word"=[REDACTED]\necho ordinary'],
    ['cli --opt=a=b=c=d=e=f=g=h=pass"word"=SecretSuffix\necho ordinary', 'cli --opt=a=b=c=d=e=f=g=h=pass"word"=[REDACTED]\necho ordinary'],
    // In a command string, and in a log line that quotes it as a JSON string;
    // where the inner shell's quotes hold `<` and a continuation joins the
    // name.
    [`sh -c "gcloud run deploy svc --set-env-vars=ORDINARY=x,DB_PASS\\"WORD\\"=SecretSuffix --region us"\necho ordinary`, 'sh -c "gcloud run deploy svc --set-env-vars=ORDINARY=x,DB_PASS\\"WORD\\"=[REDACTED]'],
    [`log "${json('gcloud run deploy svc --set-env-vars=ORDINARY=x,DB_PASS"WORD"=SecretSuffix --region us')}"`, 'log "gcloud run deploy svc --set-env-vars=ORDINARY=x,DB_PASS\\"WORD\\"=[REDACTED]'],
    [`sh -c "cli --set-env-vars=\\"ORDINARY=x<DB_PASS\\\\\nWORD=Secret Suffix\\" --verbose"\necho ordinary`, 'sh -c "cli --set-env-vars=\\"ORDINARY=x<DB_PASS\\\\\n[REDACTED]'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, newStats())).toBe(want)
    for (const text of pathsOf(input)) expect(text).not.toMatch(/Secret|Suffix/)
  }
})

test('a generated sweep: in a list of names and values, after each separator and each option that takes a list, a credential name spelled in pieces loses its value in every path', () => {
  const inputs: string[] = []
  // Unquoted, after each separator: each spelling the shell joins into a
  // credential's name, the `=` escaped too.
  const names = ['DB_PASS"WORD"', "DB_PASS'WORD'", '"DB_PASS"WORD', 'DB_PA\\SSWORD', 'DB_PASS""WORD', "DB_PASS''WORD", 'DB_PASS\\\nWORD', 'db.pass"word"',
    'API"_KEY"', 'x-api"-key"', "$'DB_PASS\\x57ORD'", 'DB_PASSWORD\\']
  for (const separator of [',', ':', '@', '#', '/', '+', '%', '!', '~', '^', '=']) {
    for (const name of names) inputs.push(`cli --set-env-vars=ORDINARY=x${separator}${name}=SecretSuffix --verbose\necho ordinary`)
  }
  // In double and single quotes, where operators and blanks separate too.
  for (const separator of [',', ';', '|', '&', ' ', '\t', '<', '>', '(', ')', '\n']) {
    for (const name of ['DB_PASS"WORD"', 'DB_PASS""WORD', 'DB_PASS\\\nWORD', 'db.pass"word"', 'API"_KEY"']) inputs.push(`cli --set-env-vars="ORDINARY=x${separator}${name}=Secret Suffix" --verbose\necho ordinary`)
    for (const name of ["DB_PASS'WORD'", "DB_PASS''WORD", "db.pass'word'"]) inputs.push(`cli --set-env-vars='ORDINARY=x${separator}${name}=Secret Suffix' --verbose\necho ordinary`)
  }
  // Each option that takes a list, after `=` or a space, with each value.
  for (const option of ['--set-env-vars ', '--update-env-vars=', '--build-env-vars=', '--labels=', '-o ', '--set ', '-e ', 'env ']) {
    for (const name of ['DB_PASS"WORD"', 'db.pass"word"', "DB_PASS''WORD", 'DB_PASSWORD\\']) {
      for (const value of ['SecretSuffix', '"Secret Suffix"', "'Secret Suffix'", 'Secret\\ Suffix']) inputs.push(`cli ${option}ORDINARY=x,${name}=${value} --verbose\necho ordinary`)
    }
  }
  // gcloud's own delimiter, and a list of twelve before the credential.
  for (const name of names) {
    inputs.push(`cli --set-env-vars=^:^A=1:B=x,y:${name}=SecretSuffix --verbose`, `cli --set-env-vars=${Array.from({ length: 12 }, (_, i) => `K${i}=v${i}`).join(',')},${name}=SecretSuffix`)
  }
  // An escaped `=` after an option, a name, or a name in a list.
  for (const name of ['--password', 'PASSWORD', 'DB_PASSWORD', 'api_key', '--api-key', 'db.password']) {
    for (const value of ['SecretSuffix', '"Secret Suffix"', "'Secret Suffix'", 'Secret\\ Suffix']) {
      inputs.push(`cli ${name}\\=${value} --verbose`, `cli --from-literal=${name}\\=${value}`, `cli --set-env-vars=ORDINARY=x,${name}\\=${value}`)
    }
  }
  // Each, also in a command string another shell runs and in a log line that
  // quotes it as a JSON string.
  for (const command of inputs.splice(0)) {
    inputs.push(command, `sh -c "${command.replace(/[\\"$`]/g, c => `\\${c}`)}"`, `log "${JSON.stringify(command).slice(1, -1)}"`)
  }
  expect(inputs.length).toBe(1332)
  for (const input of inputs) for (const text of pathsOf(input)) expect(text).not.toMatch(/Secret|Suffix/)
})

test('a list of names and values that each `;` starts again, a backslash in it, is read once: 100,000 characters cost each path a few milliseconds, at the payload cap and whole', () => {
  // The review's inputs: `A=x;\A=x;` repeated (one literal backslash), cut
  // to 100,000 characters, and 1,111 of them then spaces to 100,000, with no
  // credential; and a printf of 10,002 characters of it in double quotes.
  // Each `;` starts another word that may be a list of names and values.
  const unit = 'A=x;\\A=x;'
  const texts = [unit.repeat(Math.ceil(100_000 / unit.length)).slice(0, 100_000), unit.repeat(1111).padEnd(100_000), `printf %s "${unit.repeat(1112).slice(0, 10_002)}"`]
  expect(texts.map(text => text.length)).toEqual([100_000, 100_000, 10_014])
  for (const text of texts) {
    expect(redactText(text, newStats())).toBe(text)
    for (const [cap, diagnostic] of [[16384, 1000], [220_000, 110_000]] as const) {
      for (const [path, ms] of pathTimes(text, cap, diagnostic)) expect([cap, path, ms < 100]).toEqual([cap, path, true])
    }
  }
})

test('in a list of names and values, a credential name spelled in pieces after an expansion, a command substitution or a long value loses its value in every path', () => {
  const json = (command: string): string => JSON.stringify(command).slice(1, -1)
  const long = 'a'.repeat(210)
  const cases: [string, string][] = [
    // The review's inputs: the shell passes `…,DB_PASSWORD=SecretSuffix`
    // whatever the expansion holds, and after a value of 210 characters. A
    // value after `${` runs to the end of the text: where the word goes on
    // past `${…}` cannot be told.
    ['cli --set-env-vars=ORDINARY=${VALUE},DB_PASS"WORD"=SecretSuffix', 'cli --set-env-vars=ORDINARY=${VALUE},DB_PASS"WORD"=[REDACTED]'],
    ['cli --set-env-vars=ORDINARY=$VALUE,DB_PASS"WORD"=SecretSuffix', 'cli --set-env-vars=ORDINARY=$VALUE,DB_PASS"WORD"=[REDACTED]'],
    [`cli --set-env-vars=ORDINARY=${long},DB_PASS"WORD"=SecretSuffix`, `cli --set-env-vars=ORDINARY=${long},DB_PASS"WORD"=[REDACTED]`],
    // With more of the command after it.
    ['gcloud run deploy svc --set-env-vars=ORDINARY=${VALUE},DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=ORDINARY=${VALUE},DB_PASS"WORD"=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=ORDINARY=$VALUE,DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=ORDINARY=$VALUE,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    [`gcloud run deploy svc --set-env-vars=ORDINARY=${long},DB_PASS"WORD"=SecretSuffix --region us\necho ordinary`, `gcloud run deploy svc --set-env-vars=ORDINARY=${long},DB_PASS"WORD"=[REDACTED]\necho ordinary`],
    // Other expansions, in quotes too, and other separators and options.
    ['cli --set-env-vars=ORDINARY=$1,DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=ORDINARY=$1,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['cli --set-env-vars=ORDINARY=$@,DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=ORDINARY=$@,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['cli --set-env-vars=ORDINARY=${VALUE:-x},DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=ORDINARY=${VALUE:-x},DB_PASS"WORD"=[REDACTED]'],
    ['cli --set-env-vars=ORDINARY="$VALUE",DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=ORDINARY="$VALUE",DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['cli --set-env-vars="ORDINARY=$VALUE;DB_PASS"WORD"=Secret Suffix" --verbose\necho ordinary', 'cli --set-env-vars="ORDINARY=$VALUE;DB_PASS"WORD"=[REDACTED]"\necho ordinary'],
    ['cli --set-env-vars=ORDINARY=$A$B,MODE=${C}x,DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=ORDINARY=$A$B,MODE=${C}x,DB_PASS"WORD"=[REDACTED]'],
    ['cli --set-env-vars=^:^ORDINARY=$VALUE:DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=^:^ORDINARY=$VALUE:DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['mount -o user=$USER,pass"word"=SecretSuffix //host/share /mnt\necho ordinary', 'mount -o user=$USER,pass"word"=[REDACTED]\necho ordinary'],
    // After a command substitution, whose end the list reading does not
    // read: the entry after it starts a word of its own.
    ['gcloud run deploy svc --set-env-vars=VERSION=$(git rev-parse HEAD),DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=VERSION=$(git rev-parse HEAD),DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=VERSION=`git rev-parse HEAD`,DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=VERSION=`git rev-parse HEAD`,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['cli --set-env-vars=VERSION=$(date),MODE=x,DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=VERSION=$(date),MODE=x,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['cli --set-env-vars=^:^VERSION=$((1 + 2)):DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary', 'cli --set-env-vars=^:^VERSION=$((1 + 2)):DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    // Long values: quoted, with dots and dashes, and a long name that is no
    // credential's before the credential's.
    [`cli --set-env-vars=ORDINARY="${long}",DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary`, `cli --set-env-vars=ORDINARY="${long}",DB_PASS"WORD"=[REDACTED]\necho ordinary`],
    [`cli --set-env-vars=ORDINARY=${'a.b-c'.repeat(200)},DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary`, `cli --set-env-vars=ORDINARY=${'a.b-c'.repeat(200)},DB_PASS"WORD"=[REDACTED]\necho ordinary`],
    [`cli --set-env-vars=ORDINARY=x,${long}=y,DB_PASS"WORD"=SecretSuffix --verbose\necho ordinary`, `cli --set-env-vars=ORDINARY=x,${long}=y,DB_PASS"WORD"=[REDACTED]\necho ordinary`],
    // In a command string, and in a log line that quotes it as a JSON string.
    ['sh -c "cli --set-env-vars=ORDINARY=\\${VALUE},DB_PASS\\"WORD\\"=SecretSuffix --verbose"\necho ordinary', 'sh -c "cli --set-env-vars=ORDINARY=\\${VALUE},DB_PASS\\"WORD\\"=[REDACTED]'],
    [`sh -c "cli --set-env-vars=ORDINARY=${long},DB_PASS\\"WORD\\"=SecretSuffix --verbose"\necho ordinary`, `sh -c "cli --set-env-vars=ORDINARY=${long},DB_PASS\\"WORD\\"=[REDACTED]`],
    [`log "${json('cli --set-env-vars=ORDINARY=$VALUE,DB_PASS"WORD"=SecretSuffix --verbose')}"`, 'log "cli --set-env-vars=ORDINARY=$VALUE,DB_PASS\\"WORD\\"=[REDACTED]'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, newStats())).toBe(want)
    for (const text of pathsOf(input)) expect(text).not.toMatch(/Secret|Suffix/)
  }
})

test('a generated sweep: in a list of names and values, after each expansion, command substitution and long value, a credential name spelled in pieces loses its value in every path', () => {
  const inputs: string[] = []
  // What comes before the credential's entry: expansions, alone or among
  // other text, in quotes or not, command substitutions, and values and
  // names of 201 or more characters.
  const before = ['$VALUE', '${VALUE}', '${VALUE:-x}', '${#VALUE}', '$1', '$@', '$?', '"$VALUE"', '"${VALUE}"', 'pre$VALUE', '${VALUE}post', '$A$B', 'x${A}y$B',
    '$(date)', '`date`', '$(git rev-parse HEAD)', 'a'.repeat(201), 'a'.repeat(1000), `"${'a'.repeat(300)}"`, `'${'a'.repeat(300)}'`, 'a-b.c_'.repeat(50)]
  const names = ['DB_PASS"WORD"', "DB_PASS'WORD'", 'DB_PA\\SSWORD', 'db.pass"word"', 'API"_KEY"', 'DB_PASSWORD\\']
  for (const value of before) {
    for (const name of names) {
      for (const separator of [',', ':', '@']) inputs.push(`cli --set-env-vars=ORDINARY=${value}${separator}${name}=SecretSuffix --verbose\necho ordinary`)
      inputs.push(`cli --set-env-vars ORDINARY=${value},${name}="Secret Suffix" --verbose`, `cli -o user=${value},${name}=Secret\\ Suffix`)
    }
  }
  // Each, also in a command string another shell runs and in a log line that
  // quotes it as a JSON string.
  for (const command of inputs.splice(0)) {
    inputs.push(command, `sh -c "${command.replace(/[\\"$`]/g, c => `\\${c}`)}"`, `log "${JSON.stringify(command).slice(1, -1)}"`)
  }
  expect(inputs.length).toBe(1890)
  for (const input of inputs) for (const text of pathsOf(input)) expect(text).not.toMatch(/Secret|Suffix/)
})

test('in a list of names and values, after any delimiter (`#` or `?`, a quote, a backslash, a dot or digits, gcloud\'s own of any characters) and after a command substitution or an expansion, a credential name loses its value in every path', () => {
  const long = 'a'.repeat(201)
  const cases: [string, string][] = [
    // After `#` or `?` (gcloud's `^#^`, `^?^`), a value that `&` or `;`
    // leads or holds, in quotes or escaped: its whole shell word (to the end
    // of the text where a space in a substitution hides the word's start).
    ['gcloud run deploy svc --set-env-vars="^#^VERSION=$(printf v)#DB_PASSWORD=&SecretSuffix" --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars="^#^VERSION=$(printf v)#DB_PASSWORD=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars="^#^VERSION=$(printf v)#DB_PASSWORD=first&SecretSuffix" --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars="^#^VERSION=$(printf v)#DB_PASSWORD=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=^#^VERSION=v#DB_PASSWORD=first\\&SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^#^VERSION=v#DB_PASSWORD=[REDACTED] --region us\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^#^VERSION=v#DB_PASS\\\nWORD=first\\&SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^#^VERSION=v#DB_PASS\\\nWORD=[REDACTED] --region us\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars="^?^VERSION=v?DB_PASSWORD=&SecretSuffix" --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars="^?^VERSION=v?DB_PASSWORD=[REDACTED]" --region us\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars="^#^VERSION=v#DB_PASSWORD=;SecretSuffix" --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars="^#^VERSION=v#DB_PASSWORD=[REDACTED]" --region us\necho ordinary'],
    // A backslash or a quote for the delimiter, also after a value of 201
    // characters: the shell passes `…123\DB_PASSWORD=…`, `…123'DB_PASSWORD=…`.
    ['gcloud run deploy svc --set-env-vars=^\\\\^VERSION=123\\\\DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^\\\\^VERSION=123\\\\DB_PASS"WORD"=[REDACTED]'],
    [`gcloud run deploy svc --set-env-vars=^\\\\^VERSION=${long}\\\\DB_PASS"WORD"=SecretSuffix --region us\necho ordinary`, `gcloud run deploy svc --set-env-vars=^\\\\^VERSION=${long}\\\\DB_PASS"WORD"=[REDACTED]`],
    [`gcloud run deploy svc --set-env-vars="^'^VERSION=123'DB_PASS"WORD"=Secret Suffix" --region us\necho ordinary`, `gcloud run deploy svc --set-env-vars="^'^VERSION=123'DB_PASS"WORD"=[REDACTED]"\necho ordinary`],
    [`gcloud run deploy svc --set-env-vars="^'^VERSION=${long}'DB_PASS"WORD"=Secret Suffix" --region us\necho ordinary`, `gcloud run deploy svc --set-env-vars="^'^VERSION=${long}'DB_PASS"WORD"=[REDACTED]"\necho ordinary`],
    [`gcloud run deploy svc --set-env-vars='^"^VERSION=123"DB_PASS'WORD=SecretSuffix --region us\necho ordinary`, `gcloud run deploy svc --set-env-vars='^"^VERSION=123"DB_PASS'WORD=[REDACTED]\necho ordinary`],
    // After a command substitution or backticks, whatever comes between it
    // and the name: two characters, an escaped one, another one, a quote
    // that closes on it, more of the value, a later entry. The space in the
    // substitution hides where the word starts: its value runs to the end of
    // the line, or of the text where the quotes read from the space and from
    // the text's start disagree.
    ['gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=VERSION=$(printf v)\\,DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=VERSION=$(printf v)\\,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^]^VERSION=$(printf v)]DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^]^VERSION=$(printf v)]DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^::^VERSION=`printf v`::DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^::^VERSION=`printf v`::DB_PASS"WORD"=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=VERSION="$(printf v)",DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=VERSION="$(printf v)",DB_PASS"WORD"=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=VERSION=$(printf v)-dev,DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=VERSION=$(printf v)-dev,DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=VERSION=`printf v`-dev,DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=VERSION=`printf v`-dev,DB_PASS"WORD"=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::MODE=x::DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::MODE=x::DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ["gcloud run deploy svc --set-env-vars=VERSION=$(printf v)','DB_PASS'WORD'=SecretSuffix --region us\necho ordinary", "gcloud run deploy svc --set-env-vars=VERSION=$(printf v)','DB_PASS'WORD'=[REDACTED]\necho ordinary"],
    ['gcloud run deploy svc --set-env-vars="^)^VERSION=$(printf v))DB_PASS"WORD"=Secret Suffix" --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars="^)^VERSION=$(printf v))DB_PASS"WORD"=[REDACTED]'],
    // A name after a dot or digits that end the delimiter, written whole
    // or in pieces, after a value, an expansion or a substitution.
    ['gcloud run deploy svc --set-env-vars=^.^VERSION=$(printf v).DB_PASSWORD=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^.^VERSION=$(printf v).DB_PASSWORD=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^.^VERSION=${VALUE}.DB_PASSWORD=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^.^VERSION=${VALUE}.DB_PASSWORD=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=^,.^ORDINARY=x,.DB_PASSWORD=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^,.^ORDINARY=x,.DB_PASSWORD=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^:.^ORDINARY=x:.DB_PASSWORD=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^:.^ORDINARY=x:.DB_PASSWORD=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^.^VERSION=$(printf v).DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^.^VERSION=$(printf v).DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^1^VERSION=$(printf v)1DB_PASSWORD=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^1^VERSION=$(printf v)1DB_PASSWORD=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^1^VERSION=$(printf v)1DB_PASS"WORD"=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^1^VERSION=$(printf v)1DB_PASS"WORD"=[REDACTED]\necho ordinary'],
    // gcloud's own delimiter of letters or digits: a name after it, even a
    // short credential word glued to the value before it.
    ['gcloud run deploy svc --set-env-vars=^1^VERSION=v1pass=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^1^VERSION=v1pass=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^x^VERSION=vxpass=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^x^VERSION=vxpass=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^ab^VERSION=vabpass=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^ab^VERSION=vabpass=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars "^xy^A=1xyB=2xyapi_key=Secret Suffix" --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars "^xy^A=1xyB=2xyapi_key=[REDACTED]"\necho ordinary'],
    [`gcloud run deploy svc --set-env-vars=^${'x'.repeat(40)}q^VERSION=1${'x'.repeat(40)}qpass=SecretSuffix --region us\necho ordinary`, `gcloud run deploy svc --set-env-vars=^${'x'.repeat(40)}q^VERSION=1${'x'.repeat(40)}qpass=[REDACTED]\necho ordinary`],
    // An `=` escaped outside quotes, after a substitution and such a delimiter.
    ['gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASS"WORD"\\=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASS"WORD"\\=[REDACTED]\necho ordinary'],
    ['gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASSWORD\\=SecretSuffix --region us\necho ordinary', 'gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASSWORD\\=[REDACTED]\necho ordinary'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, newStats())).toBe(want)
    for (const text of pathsOf(input)) expect(text).not.toMatch(/Secret|Suffix/)
  }
})

test('a query-style value after `#` or `?` runs through its shell word in a list that gcloud\'s `^#^` or `^?^` sets apart; elsewhere, and in a URL, the parameter rule stays', () => {
  const cases: [string, string][] = [
    ['gcloud run deploy svc --set-env-vars="^#^A=1#token=Secret&x=1" && echo ordinary', 'gcloud run deploy svc --set-env-vars="^#^A=1#token=[REDACTED]" && echo ordinary'],
    ['gcloud run deploy svc --set-env-vars=^?^A=1?token=Secret\\&x=1 && echo ordinary', 'gcloud run deploy svc --set-env-vars=^?^A=1?token=[REDACTED] && echo ordinary'],
    ['gcloud run deploy svc --set-env-vars="^?^A=1?token=&Secret" && echo ordinary', 'gcloud run deploy svc --set-env-vars="^?^A=1?token=[REDACTED]" && echo ordinary'],
    // Query strings and URLs keep their rule, in quotes too.
    ['/path?token=abc&x=1 next', '/path?token=[REDACTED]&x=1 next'],
    ['cli "/path?token=Secret&x=1" && echo ordinary', 'cli "/path?token=[REDACTED]&x=1" && echo ordinary'],
    ['curl "https://h.example.test/cb?access_token=Secret&page=2" && echo ordinary', 'curl "https://h.example.test/cb?access_token=[REDACTED]&page=2" && echo ordinary'],
    ['curl https://h.example.test/cb#access_token=Secret&state=x && echo ordinary', 'curl https://h.example.test/cb#access_token=[REDACTED]&state=x && echo ordinary'],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, newStats())).toBe(want)
    expect(redactText(want, newStats())).toBe(want)
  }
})

// Every list of names and values that a delimiter, a value before the
// credential's entry, a spelling of its name and a value make: the full cross
// product, as gcloud would take it. How each delimiter is written so that the
// shell passes it, gcloud's `^…^` before the list (as the shell passes it,
// a quote or a space in it too) where it is not the comma, and the quote
// around the list; what comes before the entry (a delimiter-looking value
// too); each spelling the shell joins into a credential's name by that quote,
// an escaped `=` too where nothing quotes it; and each value.
function listSweep(): string[] {
  const delimiters: [string, string, string][] = [
    [',', '', ''], [':', '^:^', ''], ['::', '^::^', ''], [']', '^]^', ''], ['#', '^#^', ''], ['?', '^?^', ''], ['.', '^.^', ''], [',.', '^,.^', ''],
    ['\\\\', '^\\\\^', ''], ['\\,', '', ''], ['1', '^1^', ''], ['q', '^q^', ''], ['ab', '^ab^', ''], ['--', '^--^', ''], ['q', '^"q"^', ''],
    ["'", "^'^", '"'], [';', '^;^', '"'], [' ', '^ ^', '"'], ['#', '^#^', '"'], [' q', '^ q^', '"'], ['"', '^"^', "'"], [',', '', "'"],
  ]
  const befores = ['v', '$VALUE', '${VALUE}', '$(printf v)', '`printf v`', '$(printf v)-dev', '$((1 + 2))', 'a'.repeat(201), '^x^']
  const spellings: Record<string, string[]> = {
    '': ['DB_PASSWORD', 'DB_PASS"WORD"', "DB_PASS'WORD'", 'DB_PA\\SSWORD', '"DB_PASS"WORD', 'DB_PASS\\\nWORD', 'db.pass"word"', 'API"_KEY"', 'pass', "$'DB_PASS'WORD",
      'DB_PASSWORD\\', 'DB_PASS"WORD"\\'],
    '"': ['DB_PASSWORD', 'DB_PASS"WORD"', 'DB_PASS""WORD', 'DB_PASS\\\nWORD', 'db.pass"word"', 'API"_KEY"', 'pass'],
    "'": ['DB_PASSWORD', "DB_PASS''WORD", "db.pass''word", 'pass'],
  }
  // An unquoted `&` ends the command: escaped, it is the value's.
  const values: Record<string, string[]> = { '': ['SecretSuffix', '"Secret Suffix"', "'Secret Suffix'", 'Secret\\ Suffix', '\\&SecretSuffix'], '"': ['Secret Suffix', '&Secret Suffix', 'x&Secret;Suffix'], "'": ['Secret Suffix', '&Secret Suffix'] }
  const inputs: string[] = []
  for (const [delimiter, prefix, quote] of delimiters) {
    // The delimiter as gcloud gets it: a value that holds it is split.
    const passed = delimiter.replace(/^\\(.)/, '$1')
    for (const before of befores) {
      // A variable takes the name characters after it, and a delimiter
      // splits a name or a value that holds it: none gives a credential's
      // entry.
      if (before === '$VALUE' && /^[A-Za-z0-9_]/.test(delimiter)) continue
      for (const name of spellings[quote] as string[]) {
        // A short credential word is a name of its own only after a
        // delimiter (or, glued to the value before it, gcloud's of letters
        // or digits).
        if (name === 'pass' && !/[A-Za-z0-9]$/.test(delimiter) && delimiter !== ',') continue
        if (name.includes('.') && passed.includes('.')) continue
        for (const value of values[quote] as string[]) {
          if (value.includes(passed)) continue
          inputs.push(`gcloud run deploy svc --set-env-vars=${quote}${prefix}VERSION=${before}${delimiter}${name}=${value}${quote} --region us\necho ordinary`)
        }
      }
    }
  }
  return inputs
}

const LIST_SWEEP = listSweep()

for (const [label, wrap] of [
  ['as a command line', (c: string): string => c],
  ["in a single-quoted command string another shell runs (sh -c '…')", (c: string): string => `sh -c '${c.replace(/'/g, `'\\''`)}'`],
  ['in a double-quoted command string another shell runs (sh -c "…")', (c: string): string => `sh -c "${c.replace(/[\\"$`]/g, ch => `\\${ch}`)}"`],
  ['in a log line that quotes it as a JSON string', (c: string): string => `log "${JSON.stringify(c).slice(1, -1)}"`],
] as const) {
  // In four parts, each well within a test's time.
  for (const part of [0, 1, 2, 3]) {
    test(`a generated sweep, ${label}, part ${part + 1} of 4: every delimiter × value before the entry × spelling × value in a list of names and values loses the credential in every path`, () => {
      expect(LIST_SWEEP.length).toBe(7923)
      const size = Math.ceil(LIST_SWEEP.length / 4)
      for (const command of LIST_SWEEP.slice(part * size, (part + 1) * size)) {
        const input = wrap(command)
        for (const text of pathsOf(input)) expect([input, /Secret|Suffix/.test(text)]).toEqual([input, false])
      }
    })
  }
}

test('lists of names and values with these delimiters and no credential name in them are left as they are, byte for byte, in every path', () => {
  const commands = [
    'gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::MODE=prod --region us',
    'gcloud run deploy svc --set-env-vars=^.^VERSION=v.MODE=prod --region us',
    'gcloud run deploy svc --set-env-vars=^1^VERSION=v1MODE=prod --region us',
    'gcloud run deploy svc --set-env-vars=^x^VERSION=vxMODE=prod --region us',
    'gcloud run deploy svc --set-env-vars=^ab^VERSION=vabMODE=prod --region us',
    `gcloud run deploy svc --set-env-vars="^'^VERSION=1'MODE=prod" --region us`,
    'gcloud run deploy svc --set-env-vars=^\\\\^VERSION=1\\\\MODE=prod --region us',
    'gcloud run deploy svc --set-env-vars="^#^VERSION=v#MODE=&prod" --region us',
    'curl "https://h.example.test/x?q=tokens&page=2#frag" && echo ordinary',
    'echo aGVsbG8gd29ybGQ= dGhpcyBpcyBhIHRlc3Q= && x509=1 v1=2',
    'make TARGET=x86_64 OS=linux KEYBOARD=us',
    'printf "%s\\n" "a=1" "b=2"',
  ]
  const commandOf = (content: string | undefined): string => (JSON.parse(content ?? '{}') as { args?: { command?: string } }).args?.command ?? ''
  for (const command of commands) {
    const [once, content = 'null', error, request = '{}', file = '', lines = '[]', diagnostic, twice] = pathsOf(command)
    expect([once, twice, error, diagnostic]).toEqual([command, command, `command failed: ${command}`, `command failed: ${command}`])
    expect(commandOf(JSON.parse(content) as string)).toBe(command)
    expect(commandOf((JSON.parse(request) as { rows: { json: { content: string } }[] }).rows[0]?.json.content)).toBe(command)
    expect(commandOf((parseFile(file).lines[0] as { row: { content: string } } | undefined)?.row.content)).toBe(command)
    expect(commandOf((JSON.parse(lines) as { row: { content: string } }[])[0]?.row.content)).toBe(command)
  }
})

test('names in lists after any delimiter are read once per text: 100,000-character lists cost each path a few milliseconds, at the payload cap and whole', () => {
  const texts = [
    // Long runs of quoted pieces with a credential piece in them before each
    // `=`, a gcloud list with a letter for its delimiter, a run of digits
    // and letters, and quotes and backslashes before an `=`.
    `${'"key'.repeat(100)}=x,`.repeat(248),
    `--set-env-vars=^x^${'AxB=1x'.repeat(16_600)}`,
    `${'1a'.repeat(49_998)}=x`,
    `${'\\"'.repeat(49_998)}=x`,
    `"^'^${"A=1'".repeat(24_999)}"`,
    // 2,500 lines, each with a credential that only this rule finds; and
    // after a `^` that opens no list, 11,000 `#name=` and 25,000 `name=`
    // (gcloud's lists are looked for once).
    `--set-env-vars="^'^A=1'DB_PASS"WORD"=y"\n`.repeat(2_500),
    `^ ${'#token=x '.repeat(11_000)}`,
    `^ ${'a=1 '.repeat(25_000)}`,
    // gcloud's delimiter read as the shell passes it: quoted, with spaces,
    // and a `^…^` in each value; and escaped `=` in a command string.
    `--o='^"q"^A=1qpass=x' `.repeat(4_546),
    `--o="^ a b^xa b" `.repeat(5_883),
    'a=^x^'.repeat(20_000),
    `sh -c '${'DB_PASSWORD\\=x '.repeat(6_667)}'`,
  ]
  for (const text of texts) {
    expect(text.length > 99_000).toBe(true)
    for (const [cap, diagnostic] of [[16384, 1000], [220_000, 110_000]] as const) {
      for (const [path, ms] of pathTimes(text, cap, diagnostic)) expect([cap, path, ms < 100]).toEqual([cap, path, true])
    }
  }
})

// A command as a command line, in a single- and a double-quoted command
// string another shell runs, and in a log line that quotes it as a JSON
// string.
const wrappersOf = (command: string): string[] => [
  command,
  `sh -c '${command.replace(/'/g, `'\\''`)}'`,
  `sh -c "${command.replace(/[\\"$`]/g, c => `\\${c}`)}"`,
  `log "${JSON.stringify(command).slice(1, -1)}"`,
]

test('round 3 review inputs: a value a marker or a provider token starts, an escaped `=` in a command string, gcloud\'s delimiter as the shell passes it, a delimiter-looking value and a long quoted name lose all of the credential in every path and wrapper', () => {
  const token = `ghp_${'a1B2'.repeat(9)}`
  const cases: [string, string][] = [
    // P1-1: a value that a marker or a provider token's redaction starts.
    ['gcloud run deploy svc --set-env-vars="^q^VERSION=vqpass=[REDACTED]SecretSuffix"', 'gcloud run deploy svc --set-env-vars="^q^VERSION=vqpass=[REDACTED]"'],
    [`gcloud run deploy svc --set-env-vars=^q^VERSION=vqpass=${token}:SecretSuffix`, 'gcloud run deploy svc --set-env-vars=^q^VERSION=vqpass=[REDACTED]'],
    [`gcloud run deploy svc --set-env-vars="^q^VERSION=vqpass=${token}:SecretSuffix" --region us\necho ordinary`, 'gcloud run deploy svc --set-env-vars="^q^VERSION=vqpass=[REDACTED]"\necho ordinary'],
    // P1-2: an `=` escaped outside quotes, then in command strings below.
    ['gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASSWORD\\=SecretSuffix', 'gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASSWORD\\=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASS"WORD"\\=SecretSuffix', 'gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::DB_PASS"WORD"\\=[REDACTED]'],
    // P1-3: gcloud's delimiter as the shell passes it: with a space in it,
    // in quotes.
    // (A space in quotes hides where the word starts: to the end of the text.)
    ['gcloud run deploy svc --set-env-vars="^ q^VERSION=v qpass=SecretSuffix"', 'gcloud run deploy svc --set-env-vars="^ q^VERSION=v qpass=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars=^"q"^VERSION=vqpass=SecretSuffix', 'gcloud run deploy svc --set-env-vars=^"q"^VERSION=vqpass=[REDACTED]'],
    ['gcloud run deploy svc --set-env-vars="^ #^VERSION=v #DB_PASSWORD=first&SecretSuffix"', 'gcloud run deploy svc --set-env-vars="^ #^VERSION=v #DB_PASSWORD=[REDACTED]'],
    // P1-4: a value that looks like gcloud's delimiter does not set the
    // list's apart.
    ['gcloud run deploy svc --set-env-vars="^#^VERSION=^x^#DB_PASSWORD=first&SecretSuffix"', 'gcloud run deploy svc --set-env-vars="^#^VERSION=^x^#DB_PASSWORD=[REDACTED]"'],
    // P2-1: a name's pieces past any length.
    [`gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::pa${'""'.repeat(520)}ss=SecretSuffix`, `gcloud run deploy svc --set-env-vars=^::^VERSION=$(printf v)::pa${'""'.repeat(520)}ss=[REDACTED]`],
  ]
  for (const [input, want] of cases) {
    expect(redactText(input, newStats())).toBe(want)
    for (const wrapped of wrappersOf(input)) for (const text of pathsOf(wrapped)) expect([wrapped, /Secret|Suffix/.test(text)]).toEqual([wrapped, false])
  }
})

test('an allowlisted name the shell joins from quoted parts, and a backslash its own quote keeps, leave the text as it is in every path', () => {
  const commandOf = (content: string | undefined): string => (JSON.parse(content ?? '{}') as { args?: { command?: string } }).args?.command ?? ''
  const commands = [
    ...['cli --max_"to"kens=1024', 'cli --set input_"to"kens=512 --verbose', 'cli --total_"to"kens=5', 'cli --cache_creation_input_"to"kens=3',
      'cli --set prompt_"tok"ens=9', "cli --output_'to'kens=7"].flatMap(wrappersOf),
    // The literal backslash of a word's own quote, in each wrapper.
    ...wrappersOf("sed 's/password\\=.*/x/' config.ini"),
  ]
  for (const command of commands) {
    const [once, content = 'null', error, request = '{}', file = '', lines = '[]', diagnostic, twice] = pathsOf(command)
    expect([once, twice, error, diagnostic]).toEqual([command, command, `command failed: ${command}`, `command failed: ${command}`])
    expect(commandOf(JSON.parse(content) as string)).toBe(command)
    expect(commandOf((JSON.parse(request) as { rows: { json: { content: string } }[] }).rows[0]?.json.content)).toBe(command)
    expect(commandOf((parseFile(file).lines[0] as { row: { content: string } } | undefined)?.row.content)).toBe(command)
    expect(commandOf((JSON.parse(lines) as { row: { content: string } }[])[0]?.row.content)).toBe(command)
  }
})

// Review rounds r5, r6 and r7: every reported list/URL shape, in the four
// wrappers and eight output paths. The cases this build redacts must keep
// no part of the canary anywhere; the cases still open (tracked in the
// README's Limitations and in #546) are pinned here so a later fix trips
// this test and both places get updated together.
test('r5/r6/r7 review shapes: fixed cases redact in every path and wrapper; open cases are pinned', () => {
  const G = 'gcloud run deploy svc --set-env-vars='
  const token = 'gh' + 'p_' + 'Z'.repeat(36)
  const long = 'a'.repeat(400)
  const fixed = [
    // r7 P1-1: a quoted or continued separator after a substitution.
    `${G}VERSION=$(printf v)","DB_PASS"WORD"=CanaryValue`,
    `${G}^:^VERSION=$(printf v)":"DB_PASS"WORD"=CanaryValue`,
    // r7 P1-3: an expansion that holds a blank, and $[1 + 2].
    `${G}VERSION=\${VALUE:-a b},DB_PASS"WORD"=CanaryValue`,
    `${G}VERSION=$[1 + 2],DB_PASS"WORD"=CanaryValue`,
    // r7 P1-4: an escaped literal backtick in the list.
    `${G}VERSION=a\\\`b,DB_PASS"WORD"=CanaryValue`,
    // r6: a space in an earlier value; the broad delimiter/substitution set.
    `${G}VERSION="a b",DB_PASS"WORD"=CanaryValue`,
    `${G}VERSION=$(git rev-parse HEAD),DB_PASS"WORD"=CanaryValue`,
    `${G}^::^VERSION=v::DB_PASS"WORD"=CanaryValue`,
    // the query escaped-opener, now read through its shell word.
    'sh -c "see q?token=\\"abc def\\""',
  ]
  for (const command of fixed) for (const wrapped of wrappersOf(command)) for (const text of pathsOf(wrapped)) expect([wrapped, /CanaryValue|abc|def/.test(text)]).toEqual([wrapped, false])

  const open = [
    // r5 P1-1: a marker, or a provider token the earlier pass marked, then more text.
    `${G}"^q^VERSION=$(printf v)qpass=[REDACTED]&CanaryValue"`,
    `${G}"^q^VERSION=$(printf v)qpass=${token}&CanaryValue"`,
    // r5 P1-2: a delimiter-looking value after a quoted space or in a substitution.
    `${G}"^#^VERSION=hello ^x^#DB_PASSWORD=first&CanaryValue"`,
    // r5 P1-3: the allowlist before a punctuation delimiter splits the entry.
    `${G}^.^VERSION=max.tokens=CanaryValue`,
    `${G}^-^VERSION=output-tokens=CanaryValue`,
    // r5 P1-4: a shell-spelled delimiter, a quoted option name, a 256-char delimiter.
    `${G}^$'\\x71'^VERSION=vqpass=CanaryValue`,
    `gcloud run deploy svc --set-env-"vars"=^q^VERSION=vqpass=CanaryValue`,
    `${G}^${'q'.repeat(256)}^VERSION=v${'q'.repeat(256)}pass=CanaryValue`,
    // r6 P1-2: a $'…' name past about the first 400 characters of its word.
    `${G}VERSION=${long},$'DB_PASS\\x57ORD'=CanaryValue`,
    // r6 P1-3: a $ opener in a URL query value.
    'curl https://example.test/?token=$(printf %s CanaryValue)',
    // r7 P1-2: a locale-quoted $"…" name fragment.
    `${G}VERSION=v,DB_PASS$"WORD"=CanaryValue`,
    'cli --pass$"word" CanaryValue',
  ]
  // Pins current behavior: each still keeps the canary in some output
  // path of some wrapper. When one is fixed, flip it to the `fixed` list
  // above and drop its Limitations bullet.
  for (const command of open) expect([command, wrappersOf(command).some(w => pathsOf(w).some(t => t.includes('CanaryValue')))]).toEqual([command, true])
})
