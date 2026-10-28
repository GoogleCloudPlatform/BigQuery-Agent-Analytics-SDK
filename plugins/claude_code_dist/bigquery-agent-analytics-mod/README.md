# BigQuery Agent Analytics: Coding-Agent Mod

`bigquery-agent-analytics-mod` is an in-process plugin ([mod](https://code.claude.com/docs/en/plugins/mods/overview)) for Claude Code that streams coding-agent sessions—prompts, model requests, tool calls, subagent runs, token usage, and latency—to BigQuery. Because it writes the same [BigQuery Agent Analytics](https://adk.dev/integrations/bigquery-agent-analytics/) `agent_events` schema (version 2, which adds `event_id`) as ADK's BigQuery Agent Analytics plugin and [`bigquery-agent-analytics-tracing`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/plugins/claude_code), you can query coding-agent activity alongside ADK agents with one set of SQL queries and dashboards.

The mod records events inside the CLI and sends them to BigQuery in batches in the background, so your session never waits on uploads, and if the mod itself fails your session carries on as normal. Start with [QUICKSTART.md](QUICKSTART.md) to connect your own project or join your team's setup.

## Install

**Prerequisites:**
- Claude Code CLI **v2.1.296+** (`claude --version`; tested with `2.1.296`, `2.1.299`, `2.1.302`, and `2.1.304` on macOS and Linux terminals)
- A BigQuery dataset in a Google Cloud project (see [The dataset](#the-dataset))
- Google Cloud credentials (see [Authentication](#authentication))

Install from this repository's `bqaa-tracing` marketplace:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=my-gcp-project --config dataset=agent_analytics
```

Inside an open `claude` session, run `/reload-plugins` (or start a new session) after installing.

To run directly from a local repository checkout for a single session:

```bash
export BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics
claude --plugin-dir ./plugins/claude_code_mod
```

> **Note:** Enable either `bigquery-agent-analytics-mod` **or** `bigquery-agent-analytics-tracing` for a given table, never both—running both writes every event twice. See [MIGRATION.md](MIGRATION.md) when moving from an older install, and [docs/managed-settings.md](docs/managed-settings.md) for organization-wide deployment.

### Updating and removing

- **Update**: Run `claude plugin marketplace update bqaa-tracing`, then `claude plugin update bigquery-agent-analytics-mod@bqaa-tracing`. While prerelease revisions share version `0.1.0`, `plugin update` may report `already at the latest version (0.1.0)`—in that case, uninstall and reinstall with the same `--config` flags, then run `/reload-plugins` or start a new session.
- **Pause or uninstall**: Run `claude plugin disable bigquery-agent-analytics-mod@bqaa-tracing` (or set `--config enabled=false`) to pause capture while keeping settings, or `claude plugin uninstall bigquery-agent-analytics-mod@bqaa-tracing` to remove it. Delete `~/.bqaa-mod` (or your custom `deadLetterDir` and its sibling `dry-run` directory) if you also want to clear local dead-letter and dry-run files.

### The dataset

The BigQuery dataset must exist before the mod can write. [`setup.sh`](setup.sh) uses the `bq` CLI to create everything idempotently:

- **Events dataset and table**: Creates `<dataset>` (in `BQAA_LOCATION`, default `US`) and the day-partitioned, clustered `agent_events` table defined in [`sql/agent_events_ddl.sql`](sql/agent_events_ddl.sql) (schema version 2, including `event_id`). Once the dataset exists, the mod also auto-creates `agent_events` on first `404` unless `BQAA_AUTO_CREATE_TABLE=false`, though only `setup.sh` creates the reporting views.
- **Reporting views**: Creates `<dataset>_reporting` with two daily aggregate views—[`daily_usage_by_model`](sql/views/daily_usage_by_model.sql) and [`daily_usage_by_user`](sql/views/daily_usage_by_user.sql)—filtered to this mod's `LLM_RESPONSE` rows, deduplicated on `(timestamp, event_id)`, and grouped by UTC `day`.

```bash
BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics \
  ./plugins/claude_code_mod/setup.sh
```

Note that standalone `setup.sh` runs read `BQAA_LOCATION` from your shell environment rather than the plugin's `location` setting (pass `BQAA_LOCATION=EU` for non-`US` datasets). Pass `--dry-run` to preview the `bq` commands and SQL without executing them, and see [docs/service-account-auth.md](docs/service-account-auth.md) to configure write-only team credentials and view readers. Example view query:

```bash
bq query --use_legacy_sql=false \
  'SELECT * FROM `my-gcp-project.agent_analytics_reporting.daily_usage_by_user` ORDER BY day DESC LIMIT 14'
```

> **Reusing a table from `bigquery-agent-analytics-tracing`?** That table has no `event_id` column. Inserts still succeed, but BigQuery drops the ID, so retries and backfills can't be deduplicated and the deduplication query under [What it writes](#what-it-writes) fails. Run `setup.sh` once against that dataset (it adds the column), or run:
> ```sql
> ALTER TABLE `YOUR_PROJECT.YOUR_DATASET.agent_events` ADD COLUMN IF NOT EXISTS event_id STRING;
> ```

## Configure

Set options with `--config key=value` at install time, `/plugin configure`, `/config`, or environment variable fallbacks. For boolean environment variables (`BQAA_TRACE_ENABLED`, `BQAA_DRY_RUN`, `BQAA_AUTO_CREATE_TABLE`, `BQAA_AUTO_BACKFILL`), `true`/`1`/`yes`/`on` enables and `false`/`0`/`no`/`off` disables (case-insensitive).

| Option | Environment fallback | Default | Description |
| --- | --- | --- | --- |
| `project` | `BQAA_PROJECT_ID`, `GCP_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT` | *(required)* | GCP project holding the BigQuery dataset. |
| `dataset` | `BQAA_DATASET`, `BQ_DATASET` | *(required)* | BigQuery dataset containing the events table. |
| `table` | `BQAA_TABLE`, `BQ_TABLE` | `agent_events` | Target BigQuery events table. |
| `location` | `BQAA_LOCATION` | `US` | Dataset location shown in `/bqaa-status` and its generated `setup.sh` command (manual `setup.sh` runs read `BQAA_LOCATION` from the environment). |
| `enabled` | `BQAA_TRACE_ENABLED` | `true` | Turn event capture on or off without uninstalling. |
| `dryRun` | `BQAA_DRY_RUN` | `false` | Write JSONL batches locally to `<deadLetterDir>/../dry-run/` instead of BigQuery (a dry-run file can contain the same `event_id` twice if a session ends mid-write). |
| `maxPayloadChars` | — | `16384` (`1024`–`500000`) | Max characters per JSON payload column (`content`, `attributes`); excess sets `is_truncated = true`. |
| `flushIntervalMs` | — | `5000` (`1000`–`300000`) | How often (ms) the background writer flushes queued rows. |
| `deadLetterDir` | `BQAA_DEAD_LETTER_DIR` | `~/.bqaa-mod/dead-letter` | Local directory for unsent JSONL batches, partitioned per `<project.dataset.table>`. |
| `agentName` | `BQAA_AGENT_NAME` | `coding-agent` | Value written to the `agent` column on main-loop events. |
| `accessTokenCommand` | `BQAA_ACCESS_TOKEN_COMMAND` | `gcloud auth application-default print-access-token` | Command (argv, no shell) that prints a Google OAuth access token. |
| `userIdFromCredentials` | — | `false` | Opt-in: set `user_id` to the verified email of the access token's account. Leave off for team setups using service-account impersonation, or every row will record the service account's email. |
| `recordHostname` | — | `false` | Opt-in: include this machine's hostname in `session_metadata.host_name`. |

Advanced tuning (environment variables only):

| Variable | Default | Description |
| --- | --- | --- |
| `BQAA_ACCESS_TOKEN` | *(unset)* | Pre-fetched OAuth2 bearer token (re-read every 5 minutes). When set, takes priority over `BQAA_ACCESS_TOKEN_COMMAND` / `accessTokenCommand` (useful when subprocess execution is unavailable). |
| `BQAA_USER_ID` | `$USER` / `$USERNAME` | Overrides `user_id` (unless `userIdFromCredentials` is enabled and resolved). |
| `BQAA_REQUEST_TIMEOUT_MS` | `10000` (`1000`–`30000`) | Client timeout (ms) per `insertAll` HTTP request. |
| `BQAA_MAX_QUEUE_ROWS` | `5000` (`10`–`100000`) | Max rows buffered in memory (also bounded at `8 MiB` UTF-8). |
| `BQAA_EXIT_FLUSH_MS` | `800` (`0`–`1200`) | Max wait (ms) at `session.end` to write queued rows to a local spill file (`0` = fire-and-forget). |
| `BQAA_AUTO_CREATE_TABLE` | `true` | Create the `agent_events` table automatically when `insertAll` returns `404` for the table. |
| `BQAA_AUTO_BACKFILL` | `true` | Automatically replay local `spill` and `retry` files at session start and after recovery. |

### Authentication

The background writer uploads over HTTPS using a Google OAuth access token obtained from `BQAA_ACCESS_TOKEN` when set, or otherwise by running `accessTokenCommand` directly (no shell; default `gcloud auth application-default print-access-token`).

- **Token lifecycle**: Tokens stay in memory and are sent only to `bigquery.googleapis.com` (plus `oauth2.googleapis.com/tokeninfo` when `userIdFromCredentials` is enabled). Tokens from `accessTokenCommand` are cached for 45 minutes; the writer reruns the command when the cache expires, and once more right away on a `401`. A supplied `BQAA_ACCESS_TOKEN` is re-read every 5 minutes and on `401`, but the mod can't renew it—replace an expired value, or unset it to fall back to `accessTokenCommand`. Service-account JSON key files cannot be passed directly; wrap them in a token-printing command if needed.
- **Sandbox vs. shared team permissions**: For a personal sandbox, `roles/bigquery.dataEditor` on the dataset is convenient and permits automatic table creation. Never share `dataEditor` across a team, because it also grants read and delete access to everyone's rows. For team tables, use service-account impersonation with a custom write-only role containing only `bigquery.tables.get` and `bigquery.tables.updateData` on `agent_events`—see [docs/service-account-auth.md](docs/service-account-auth.md).

## Commands

All slash commands run immediately—even mid-turn—and display only safe operational counters (never prompts, payloads, or tokens):

- `/bqaa-status` — Shows the target table, queue depth, row counters (`captured`, `sent`, `dead-lettered`, `spilled`, `dropped`), request health, active troubleshooting notices (including a ready-to-run `setup.sh` command when the dataset is missing), and local dead-letter file counts (including any `incomplete` files).
- `/bqaa-flush` — Flushes queued rows to BigQuery immediately instead of waiting for the next `flushIntervalMs` tick.
- `/bqaa-backfill` — Replays local dead-letter files (`spill`, `retry`, and `rejected`) for the configured table in the background. Pass `/bqaa-backfill force` to reclaim a file left behind by a terminated session once its 10-minute lease expires; files missing their completion record (listed as `incomplete` in `/bqaa-status`) are skipped so truncated rows are never sent.

For common notice messages and how to fix them, see [QUICKSTART.md — If rows are not written](QUICKSTART.md#if-rows-are-not-written).

## How it works

```text
Host hook ──► sanitize & enqueue (in-memory, <= 5,000 rows / 8 MiB) ──► return without network I/O
                     │
                     ▼  every flushIntervalMs (5 s) or end of turn
              tabledata.insertAll (<= 500 rows / 3 MiB per batch)
                 ├── 200 OK ──► BigQuery agent_events
                 └── error / exit ──► ~/.bqaa-mod/dead-letter/<project.dataset.table>/*.jsonl ──► auto-backfill (spill & retry)
```

- **In-process, fail-open capture**: Hooks never wait on network requests. Turn, model, and tool hooks sanitize and enqueue rows synchronously in memory (`<= 5,000` rows / `8 MiB`), `session.start` and `session.end` perform bounded local setup and spill work, and internal errors are caught so your CLI session is never interrupted.
- **Background batching**: Every `flushIntervalMs` (default `5 s`) and at the end of each main-loop turn, the background writer sends batches of up to `500` rows or `3 MiB` UTF-8 to BigQuery `tabledata.insertAll` with `insertId = event_id`.
- **Retries and queue protection**: Transient errors (`408`, `429`, `5xx`, network timeouts, quota `403`s, and `404`s while a dataset or table propagates) retry up to 3 times with exponential backoff (`1 s`–`60 s`, capped at `30 s` for credential or missing-dataset pauses). If the queue fills up (`5,000` rows or `8 MiB`), the oldest queued rows are dropped; while BigQuery is failing in backoff, each flush tick spills the oldest half to disk once the queue passes 80% capacity so drops stay rare.
- **Local dead-letter and exit spill**: Exhausted batches write to `retry-*.jsonl`, rejected rows (`400`, `403`, invalid row) write to `rejected-*.jsonl`, and rows still queued at process exit write to `spill-*.jsonl` (waiting up to `BQAA_EXIT_FLUSH_MS`) under `~/.bqaa-mod/dead-letter/<project.dataset.table>/`.
- **Automatic backfill**: Fifteen seconds after session start—and whenever BigQuery recovers from an outage—the writer automatically replays complete `spill` and `retry` files belonging to the configured table (`rejected` files wait for `/bqaa-backfill`).

## What it writes

| Host event | BQAA `event_type` | `content` JSON | Notable `attributes` & top-level `latency_ms` |
| --- | --- | --- | --- |
| `classic.SessionStart` | `STATE_DELTA` | `{"session_start": true}` | `attributes.state_delta.{start_source, model}`, `attributes.session_metadata` |
| `prompt.submit` | `USER_MESSAGE_RECEIVED` | `{"text_summary": prompt}` | `attributes.prompt_origin`, `attributes.submitted_during_turn` |
| `turn.start` (main loop) | `INVOCATION_STARTING` | `{}` | `attributes.turn_number`, `attributes.session_metadata` |
| `turn.step` (request) | `LLM_REQUEST` | `{"prompt": [...]}` (turn's first request; `[]` after) | `attributes.model`, `attributes.llm_config.effort`, `attributes.step_index`, `attributes.message_count` |
| `turn.step` (response / error) | `LLM_RESPONSE` / `LLM_ERROR` | `{"tool_uses", "response"}` (`null` on `LLM_ERROR`) | `attributes.model`, `attributes.usage_metadata`, `attributes.finish_reason`; `latency_ms.total_ms`, `latency_ms.time_to_first_token_ms` |
| `tool.call` (before / after) | `TOOL_STARTING` / `TOOL_COMPLETED` | `{"tool", "tool_origin", "args"}` / `{"tool", "tool_origin", "result"}` | `attributes.tool_use_id`, permission fields, `error_message` on error/deny; `latency_ms.total_ms` on `TOOL_COMPLETED` |
| `classic.SubagentStart` | `AGENT_STARTING` | `{"agent_type", "agent_id"}` | Sets top-level `agent` column to the subagent type |
| `turn.complete` (subagent) | `AGENT_COMPLETED` / `AGENT_ERROR` | `{}` | `attributes.usage_metadata` (subagent totals); `latency_ms.total_ms` |
| `turn.complete` (main loop) | `AGENT_RESPONSE` (when answered) + `INVOCATION_COMPLETED` / `INVOCATION_ERROR` | `{"response": answer}` on `AGENT_RESPONSE`; `{}` on `INVOCATION_*` | `attributes.finish_reason`, `attributes.is_aborted`, `attributes.step_count`, `attributes.usage_metadata`; `latency_ms.total_ms` |
| `classic.PermissionRequest` | *(no separate row)* | — | Sets `attributes.permission_requested` on the waiting tool's `TOOL_COMPLETED` row |
| `telemetry.log` (`tool_decision`) | *(no separate row)* | — | Sets `attributes.permission_decision` (`accept`/`reject`) and `attributes.permission_decision_source` on `TOOL_COMPLETED` |
| `session.end` | `STATE_DELTA` | `{"session_end": true}` | `attributes.state_delta.{reason, trace_count, duration_ms}` |

Key correlation, token, and deduplication fields:

- `session_id` — Re-read on every event so rows after `/clear`, `/resume`, or `/branch` carry the updated session ID.
- `invocation_id` & `trace_id` — Turn ID and 32-hex trace ID shared across a main-loop turn and any subagents started during it (`NULL` on session-start and session-end `STATE_DELTA` rows).
- `span_id` & `parent_span_id` — Call hierarchy (`turn` -> `model step` or `subagent` -> `tool call`; `parent_span_id` is `NULL` on top-level turn and session `STATE_DELTA` rows).
- `attributes.usage_metadata` — Carries both BQAA normalized totals (`prompt_tokens` = input + cache read + cache write, `completion_tokens`, `total_tokens`) and raw API counts (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`).
- `attributes.writer.plugin` — Set to `"bigquery-agent-analytics-mod"` so queries can isolate this mod's rows on shared tables.
- `event_id` — Unique 32-hex row ID preserved across retries and backfills (also sent as `insertId`).

> **Reusing a table from `bigquery-agent-analytics-tracing`?** That table has no `event_id` column. Inserts still succeed, but BigQuery drops the ID, so retries and backfills can't be deduplicated. Run `setup.sh` once against that dataset (see [The dataset](#the-dataset)), or run ``ALTER TABLE `YOUR_PROJECT.YOUR_DATASET.agent_events` ADD COLUMN IF NOT EXISTS event_id STRING;`` before relying on the query below.

When querying `agent_events` directly, deduplicate on `event_id` inside a time-bounded CTE before aggregating:

```sql
WITH events AS (
  SELECT *
  FROM `my-gcp-project.agent_analytics.agent_events`
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL
    OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT event_type, COUNT(*) AS row_count
FROM events
GROUP BY event_type
ORDER BY row_count DESC;
```

See [`sql/sample_queries.sql`](sql/sample_queries.sql) for query templates (fill in `{PROJECT}`, `{DATASET}`, and `{TABLE}`), and [`docs/value.md`](docs/value.md) (with [`sql/value_queries.sql`](sql/value_queries.sql)) for team adoption and cost estimates.

### Sample rows

[`examples/sample-rows.jsonl`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/plugins/claude_code_mod/examples/sample-rows.jsonl) contains the complete 26-row fixture sequence emitted by `tests/sample-rows.test.ts` (prompts, model requests, tool calls, a subagent, permission decisions, and session end). Refresh it from a repository checkout with:

```bash
claude plugin test . 2>&1 | sed -n 's/^BQAA_SAMPLE_ROW //p' > examples/sample-rows.jsonl
```

## Privacy and security

Treat the BigQuery dataset with the same access controls as raw coding-agent transcripts:

1. **Captured content**: Rows store prompts, visible model answers, and tool arguments/results up to `maxPayloadChars` per JSON column (`content`, `attributes`), along with token counts, latencies, `cwd`, and `user_id` (your local login name by default).
2. **Strict JSON key allowlist**: Across `content`, `attributes`, and JSON-formatted strings inside text fields, only keys listed in [`hooks/lib/allowlist.ts`](hooks/lib/allowlist.ts) keep their values. Values under unlisted keys (such as `headers`, `env`, `api_keys`, or custom MCP tool arguments) are replaced with `[REDACTED]`, and credential-shaped key names become `[REDACTED_KEY:n]`. Unparseable or oversized JSON-looking strings are replaced whole with `[TRUNCATED: ...]` and marked `is_truncated = true`.
3. **Best-effort free-text redaction**: Non-JSON strings and diagnostic error messages are scanned for common credential flags (`--password=...`), basic-auth URLs (`https://user:secret@host`), and `KEY=VALUE` lists; see [Limitations](#limitations) for known free-text parsing gaps.
4. **Opt-in personal identifiers**: `userIdFromCredentials` (verified account email) and `recordHostname` (machine hostname) are both `false` by default.
5. **In-memory tokens only**: OAuth access tokens stay strictly in memory and are never written to BigQuery rows, local dead-letter files, logs, or command output.
6. **Local files**: Dead-letter and dry-run files under `~/.bqaa-mod/` contain the same redacted rows as the table and are written with your user's default file permissions.

## Testing

Run these checks from a full repository checkout (the marketplace copy in `claude_code_dist` omits `tests/` and `examples/`).

**Offline checks (run in CI without the host CLI or GCP):**

```bash
cd plugins/claude_code_mod
# Needs release tags; on a shallow clone run: git fetch --unshallow --tags
bash build_dist.sh --check
bash tests/build_dist.test.sh
bash tests/setup.test.sh
bash tests/version.test.sh
node tests/reload.test.mjs        # requires Node 24+
python3 -m venv .venv && .venv/bin/pip install duckdb
.venv/bin/python tests/views.test.py
```

**Before a release (requires the host CLI or a test GCP project):**

```bash
claude plugin validate plugins/claude_code_mod
cd plugins/claude_code_mod
claude plugin test
bash tests/state-contract.test.sh
# Start one interactive `claude --plugin-dir .` session first so it generates .claude-plugin/types/:
npx -p typescript tsc -p .
```

The CI suite verifies marketplace sync against release tags, tests `setup.sh` with a stand-in `bq` binary, checks welcome-card behavior across hot reloads in Node 24+, and executes both reporting views in DuckDB. The host CLI suite validates `plugin.json`, exercises hooks/redaction/retries/backfill against stubbed host APIs and mock timers, and verifies host state semantics. Live GCP scripts (`tests/access.live.sh`, `tests/queries.live.py`, and `tests/managed-settings.live.sh`) verify IAM isolation, SQL queries, and managed policies before cutting a release.

## Limitations

- **Early-access mod API**: Host CLI mods are an early-access feature, tested with `2.1.296`, `2.1.299`, `2.1.302`, and `2.1.304` on macOS and Linux terminals; hook events and runtime APIs can change between CLI releases.
- **Eventual deduplication (`tabledata.insertAll`)**: Uploads use HTTP `tabledata.insertAll` (gRPC Storage Write API is unavailable in the mod runtime), whose `insertId` deduplication is best-effort—always deduplicate on `event_id` in queries.
- **Bounded queue**: Rows wait in memory (`5,000` rows or `8 MiB` by default). When the queue is full, the oldest rows are dropped and counted (`dropped` in `/bqaa-status`). While BigQuery is failing, the writer spills rows to disk once the queue passes 80%, so drops are rare. Rows can also be lost if that spill write fails.
- **First-prompt-only `LLM_REQUEST` and empty `content_parts`**: Only a turn's first model request includes the user's prompt; prior conversation history and system prompts are omitted, and `content_parts` is always empty (attachments are listed by type only).
- **Subagent parent span and detection**: A subagent's span attaches directly under the main turn rather than under the `Agent` tool call that launched it, and subagent turns are distinguished from main turns via `turn.start`'s `agentId` (from `2.1.299`).
- **Permission decision window (`2 s`)**: No separate `HITL_*` rows are written; tool permission answers come from the `tool_decision` collector record on `TOOL_COMPLETED`, and rejections arriving more than 2 seconds after the tool returned are not recorded.
- **Exit spill, table-scoped dead letters, and completion records**: `session.end` writes queued rows to a spill file rather than starting network requests. Dead-letter and spill files are replayed only by a session configured for that exact `<project.dataset.table>`, and files without a matching completion record (such as an interrupted write) stay on disk as `incomplete` in `/bqaa-status` for manual inspection.
- **Mid-session reload or crash**: Reloading plugins mid-session (`/reload-plugins` or `--plugin-dir` edits) or a worker crash can lose up to one `flushIntervalMs` window of buffered in-memory rows, plus any batch currently in retry backoff.
- **User-tier classic hooks on managed CLIs**: On organization-managed CLIs, a user-installed plugin does not receive `classic.SessionStart`, `classic.SubagentStart`, or `classic.PermissionRequest` (session-start rows are omitted, subagent starts are inferred, and `allowManagedModsOnly` or web-fetch policies can block user mods; see [docs/managed-settings.md](docs/managed-settings.md)).
- **Best-effort free-text redaction**: Structured JSON keys use a strict allowlist (redacting unlisted keys such as MCP tool arguments), whereas free-text shell and URL scrubbing uses bounded heuristics rather than full shell parsing. Known open edge cases pinned in `tests/units.test.ts` (see [#546](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/546)) include:
  - Backslash-escaped credential names after a quoted space in a flag list (`VERSION="a b",DB_PA\SSWORD=...`), locale-quoted flag names with separate values (`--pass$"word" ...`), or ANSI-C option names containing NUL bytes (`$'--password\0'`).
  - Provider-token-shaped variable names rewritten before `name=value` parsing (`github_pat_...=value`), or values trailing `[REDACTED]` / provider tokens when the shell word end is uncertain.
  - URL user-info containing extra `@` characters (`https://u:p@secret@host`) or URL query parameters containing shell expansions (`?token=$(...)`, `?token=${...}`, `?token=$'...'`).
  - Declared custom delimiters (`^delim^...`) hidden by delimiter-looking earlier values, splitting an allowlisted name (`^.^...max.tokens=...`), spelled with ANSI-C quotes, following a quoted flag name, or `>= 256` characters long.
  - Over-redaction when a credential follows a non-`/` `sed` delimiter after an assignment (`x=1 sed 's#pass"word"=.*#x#' file`) or when allowlisted entries follow a redacted list entry (`DB_PASSWORD=x,max_tokens=1024`).

## Files

```text
QUICKSTART.md                 5-minute setup for sandboxes, team members, and admins
MIGRATION.md                  migrating from older marketplaces or the settings-hook tracer
.claude-plugin/plugin.json    plugin manifest and userConfig schema
hooks/                        in-process hooks, row builder, allowlist, sanitizer, and writer
setup.sh                      idempotent BigQuery dataset, table, and reporting-views setup
build_dist.sh / sync_dist.sh  builds and verifies the marketplace copy in claude_code_dist
docs/                         team service-account auth, managed settings, and analytics guide
sql/                          agent_events DDL, reporting views, and sample/value queries
examples/sample-rows.jsonl    sample BQAA JSONL rows from the test fixture
tests/                        offline unit/reload/DuckDB suites and live GCP checks
types/index.d.ts              TypeScript declarations for session state
```
