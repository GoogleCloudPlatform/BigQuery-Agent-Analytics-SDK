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
