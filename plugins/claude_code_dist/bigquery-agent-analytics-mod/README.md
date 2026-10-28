# BigQuery Agent Analytics: Coding-Agent Mod

`bigquery-agent-analytics-mod` is an in-process plugin ([mod](https://code.claude.com/docs/en/plugins/mods/overview)) for Claude Code that streams coding-agent sessions—prompts, model requests, tool calls, subagent runs, token usage, and latency—to BigQuery using the [BigQuery Agent Analytics](https://adk.dev/integrations/bigquery-agent-analytics/) (`agent_events`) schema. Because it writes the exact same table layout as ADK's BigQuery Agent Analytics plugin and [`bigquery-agent-analytics-tracing`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/plugins/claude_code), you can query coding-agent activity alongside ADK agents with one set of SQL queries and dashboards.

Unlike shell-hook tracers that spawn a blocking subprocess on every event, this mod runs inside the host CLI on a strict fail-open, non-blocking path: capture hooks enqueue sanitized rows in memory and return immediately while a background timer batches and sends them to BigQuery. To get up and running in about 5 minutes, start with [QUICKSTART.md](QUICKSTART.md).

## Install

**Prerequisites:**
- Claude Code CLI **v2.1.296+** (`claude --version`)
- A BigQuery dataset in a Google Cloud project
- Google Cloud credentials (see [Authentication](#authentication))

If the target table does not exist yet, the mod creates it automatically on first write unless `BQAA_AUTO_CREATE_TABLE=false`.

Install from this repository's `bqaa-tracing` marketplace:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=my-gcp-project --config dataset=agent_analytics
```

If you already added `bqaa-tracing` earlier, run `claude plugin marketplace update bqaa-tracing` before installing. Inside an open `claude` session, run `/reload-plugins` to load the newly installed mod.

To run directly from a local repository checkout for a single session:

```bash
export BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics
claude --plugin-dir ./plugins/claude_code_mod
```

> **Note:** Enable either `bigquery-agent-analytics-mod` **or** `bigquery-agent-analytics-tracing` for a given table, never both—running both writes every event twice. See [MIGRATION.md](MIGRATION.md) when moving from an older install, and [docs/managed-settings.md](docs/managed-settings.md) for organization-wide deployment.

### The dataset

[`setup.sh`](setup.sh) uses the `bq` CLI to create everything idempotently:

- **Events dataset and table**: Creates `<dataset>` (in `BQAA_LOCATION`, default `US`) and the day-partitioned, clustered `agent_events` table defined in [`sql/agent_events_ddl.sql`](sql/agent_events_ddl.sql).
- **Reporting views**: Creates `<dataset>_reporting` with two daily aggregate views—[`daily_usage_by_model`](sql/views/daily_usage_by_model.sql) and [`daily_usage_by_user`](sql/views/daily_usage_by_user.sql)—filtered to this mod's `LLM_RESPONSE` rows and deduplicated by `(timestamp, event_id)` in UTC.

```bash
BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics \
  ./plugins/claude_code_mod/setup.sh
```

Pass `--dry-run` to print the `bq` commands and SQL without executing them. `setup.sh` does not modify IAM policies; to grant write-only team credentials and view-reader access, see [docs/service-account-auth.md](docs/service-account-auth.md).

## Configure

Set options with `--config key=value` at install time, `/plugin configure`, `/config`, or environment variable fallbacks:

| Option | Environment fallback | Default | Description |
| --- | --- | --- | --- |
| `project` | `BQAA_PROJECT_ID`, `GCP_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT` | *(required)* | GCP project holding the BigQuery dataset. |
| `dataset` | `BQAA_DATASET`, `BQ_DATASET` | *(required)* | BigQuery dataset containing the events table. |
| `table` | `BQAA_TABLE`, `BQ_TABLE` | `agent_events` | Target BigQuery events table. |
| `location` | `BQAA_LOCATION` | `US` | Dataset location shown in `/bqaa-status` and passed to `setup.sh`. |
| `enabled` | `BQAA_TRACE_ENABLED` (`false`/`0`/`no`/`off` disables) | `true` | Turn event capture on or off without uninstalling. |
| `dryRun` | `BQAA_DRY_RUN` (`true`/`1`/`yes`/`on` enables) | `false` | Write JSONL batches locally to `<deadLetterDir>/../dry-run/` instead of BigQuery. |
| `maxPayloadChars` | — | `16384` (`1024`–`500000`) | Max characters per JSON payload column (`content`, `attributes`); excess sets `is_truncated = true`. |
| `flushIntervalMs` | — | `5000` (`1000`–`300000`) | How often (ms) the background writer flushes queued rows. |
| `deadLetterDir` | `BQAA_DEAD_LETTER_DIR` | `~/.bqaa-mod/dead-letter` | Local directory for unsent JSONL batches, partitioned per `<project.dataset.table>`. |
| `agentName` | `BQAA_AGENT_NAME` | `coding-agent` | Value written to the `agent` column on main-loop events. |
| `accessTokenCommand` | `BQAA_ACCESS_TOKEN_COMMAND` | `gcloud auth application-default print-access-token` | Command (argv, no shell) that prints a Google OAuth access token. |
| `userIdFromCredentials` | — | `false` | Opt-in: set `user_id` to the verified email of the access token's account. |
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

Because the mod runtime communicates over HTTPS (`$.http.fetch`), the background writer obtains Google OAuth access tokens from `BQAA_ACCESS_TOKEN` when set, or otherwise by running `accessTokenCommand` via `$.process.run` (default: `gcloud auth application-default print-access-token`).

- **Token lifecycle**: Tokens stay in memory, cache for 45 minutes, refresh automatically with one immediate retry on `401`, and are sent only to `bigquery.googleapis.com` (plus `oauth2.googleapis.com/tokeninfo` when `userIdFromCredentials` is enabled). Service-account JSON key files cannot be passed directly—wrap them in a token-printing command if needed.
- **Sandbox vs. shared team permissions**: For a personal sandbox, `roles/bigquery.dataEditor` on the dataset is convenient and permits automatic table creation. Never share `dataEditor` across a team, because it also grants read and delete access to everyone's rows. For team tables, use service-account impersonation with a custom write-only role containing only `bigquery.tables.get` and `bigquery.tables.updateData` on `agent_events`—see [docs/service-account-auth.md](docs/service-account-auth.md).

## Commands

All slash commands run immediately—even mid-turn—and display only safe operational counters (never prompts, payloads, or tokens):

- `/bqaa-status` — Shows the target table, queue depth, row counters (`captured`, `sent`, `dead-lettered`, `spilled`, `dropped`), request health, active troubleshooting notices (including a ready-to-run `setup.sh` command when the dataset is missing), and local dead-letter file counts.
- `/bqaa-flush` — Flushes queued rows to BigQuery immediately instead of waiting for the next `flushIntervalMs` tick.
- `/bqaa-backfill` — Replays local dead-letter files (`spill`, `retry`, and `rejected`) for the configured table in the background. Pass `/bqaa-backfill force` to reclaim and replay a file left behind by a terminated session once its 10-minute lease expires.

## How it works

```text
Host hook ──► sanitize & enqueue (in-memory, <= 5,000 rows / 8 MiB) ──► return immediately
                     │
                     ▼  every flushIntervalMs (5 s) or end of turn
              tabledata.insertAll (<= 500 rows / 3 MiB per batch)
                 ├── 200 OK ──► BigQuery agent_events
                 └── error / exit ──► ~/.bqaa-mod/dead-letter/<table_id>/*.jsonl ──► auto-backfill
```

- **Non-blocking, fail-open capture**: Every hook calls `next(e)` immediately, sanitizes and enqueues the row synchronously into a bounded in-memory queue (`<= 5,000` rows / `8 MiB`), and catches internal errors so the host CLI never stalls or crashes.
- **Background batching**: Every `flushIntervalMs` (default `5 s`) and at the end of each main-loop turn, the background writer posts batches of up to `500` rows or `3 MiB` UTF-8 to BigQuery `tabledata.insertAll` with `insertId = event_id`.
- **Retries and queue protection**: Transient errors (`408`, `429`, `5xx`, network timeouts, quota `403`s, and `404`s while a dataset or table propagates) retry up to 3 times with exponential backoff (`1 s`–`60 s`, capped at `30 s` for credential or missing-dataset pauses). If the queue crosses 80% capacity while BigQuery is unreachable, the oldest half spills to disk rather than dropping rows.
- **Local dead-letter and exit spill**: Exhausted batches write to `retry-*.jsonl`, rejected rows (`400`, `403`, invalid row) write to `rejected-*.jsonl`, and rows still queued at process exit write to `spill-*.jsonl` (waiting up to `BQAA_EXIT_FLUSH_MS`) under `~/.bqaa-mod/dead-letter/<project.dataset.table>/`.
- **Automatic backfill**: Fifteen seconds after session start—and whenever BigQuery recovers from an outage—the writer automatically replays `spill` and `retry` files belonging to the configured table (`rejected` files wait for `/bqaa-backfill`).

## What it writes

| Host event (mod hook) | BQAA `event_type` | Summary of `content` & `attributes` |
| --- | --- | --- |
| `classic.SessionStart` | `STATE_DELTA` | `{"session_start": true}`, `state_delta.start_source`, `model`, `session_metadata` |
| `prompt.submit` | `USER_MESSAGE_RECEIVED` | `{"text_summary": prompt}`, `prompt_origin`, `submitted_during_turn` |
| `turn.start` (main loop) | `INVOCATION_STARTING` | `turn_number`, `session_metadata` |
| `turn.step` | `LLM_REQUEST` / `LLM_RESPONSE` / `LLM_ERROR` | First-step prompt, `tool_uses`, `response`, `model`, `usage_metadata`, `latency_ms` |
| `tool.call` | `TOOL_STARTING` / `TOOL_COMPLETED` | `tool`, `tool_origin`, `args` / `result`, `tool_use_id`, `latency_ms`, permission fields |
| `classic.SubagentStart` | `AGENT_STARTING` | `agent_type`, `agent_id` (`agent` column set to subagent type) |
| `turn.complete` | `AGENT_COMPLETED` / `AGENT_ERROR` (subagent); `AGENT_RESPONSE` + `INVOCATION_COMPLETED` / `INVOCATION_ERROR` (main) | Final `response`, `finish_reason`, `step_count`, `usage_metadata`, `latency_ms` |
| `classic.PermissionRequest` | *(no separate row)* | Marks `permission_requested` on that tool call's `TOOL_COMPLETED` row |
| `telemetry.log` (`tool_decision`) | *(no separate row)* | Sets `permission_decision` (`accept`/`reject`) and `permission_decision_source` on `TOOL_COMPLETED` |
| `session.end` | `STATE_DELTA` | `{"session_end": true}`, `state_delta.reason`, `trace_count`, `duration_ms` |

Every row carries consistent correlation and deduplication fields:

- `session_id` — Current session identifier (updates across `/clear`, `/resume`, and `/branch`).
- `invocation_id` & `trace_id` — Turn identifier and 32-hex trace ID shared by the main loop and any subagents spawned during that turn.
- `span_id` & `parent_span_id` — Hierarchical call tree (`turn` -> `model step` or `subagent` -> `tool call`).
- `attributes.writer.plugin` — Set to `"bigquery-agent-analytics-mod"` so queries can filter out rows from other writers sharing the table.
- `event_id` — Unique 32-hex row ID preserved across retries and backfills (also passed as `insertId`).

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

See [`sql/sample_queries.sql`](sql/sample_queries.sql) for ready-to-run queries, [`docs/value.md`](docs/value.md) (with [`sql/value_queries.sql`](sql/value_queries.sql)) for team adoption and cost estimates, and [`examples/sample-rows.jsonl`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/plugins/claude_code_mod/examples/sample-rows.jsonl) for complete fixture rows.

## Privacy and security

Treat the BigQuery dataset with the same access controls as raw coding-agent transcripts:

1. **Captured content**: Rows store prompts, visible model answers, and tool arguments/results up to `maxPayloadChars` per JSON column (`content`, `attributes`), along with token counts, latencies, `cwd`, and `user_id` (your local login name by default).
2. **Strict JSON key allowlist**: Across `content`, `attributes`, and JSON-formatted strings inside text fields, only keys listed in [`hooks/lib/allowlist.ts`](hooks/lib/allowlist.ts) keep their values. Values under unlisted keys (such as `headers`, `env`, `api_keys`, or custom MCP tool arguments) are replaced with `[REDACTED]`, and credential-shaped key names become `[REDACTED_KEY:n]`. Unparseable or oversized JSON-looking strings are replaced whole with `[TRUNCATED: ...]` and marked `is_truncated = true`.
3. **Best-effort free-text redaction**: Non-JSON strings and diagnostic error messages are scanned for common credential flags (`--password=...`), basic-auth URLs (`https://user:secret@host`), and simple `KEY=VALUE` lists (see [#546](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/546) for sanitizer hardening details).
4. **Opt-in personal identifiers**: `userIdFromCredentials` (verified account email) and `recordHostname` (machine hostname) are both `false` by default.
5. **In-memory tokens only**: OAuth access tokens stay strictly in memory and are never written to BigQuery rows, local dead-letter files, logs, or command output.
6. **Local files**: Dead-letter and dry-run files under `~/.bqaa-mod/` contain the same redacted rows as the table and are written with your user's default file permissions.

## Testing

```bash
claude plugin validate plugins/claude_code_mod
cd plugins/claude_code_mod
claude plugin test
bash tests/state-contract.test.sh
bash build_dist.sh --check
bash tests/build_dist.test.sh
bash tests/setup.test.sh
bash tests/version.test.sh
node tests/reload.test.mjs        # requires Node 24+
python3 tests/views.test.py       # requires duckdb
npx -p typescript tsc -p .        # requires .claude-plugin/types from one --plugin-dir load
```

The offline test suite validates the manifest, runs unit tests against stubbed host APIs (`$.http.fetch`, `$.process.run`, `$.fs`, `$.store`, and mock timers), checks `$.state` semantics in the host runtime (`tests/state-contract.test.sh`), checks `setup.sh` against a stand-in `bq` binary, verifies welcome-card behavior across hot reloads (`node tests/reload.test.mjs`, requires Node 24+), and executes both reporting views in DuckDB. Live GCP checks (`tests/access.live.sh`, `tests/queries.live.py`, and `tests/managed-settings.live.sh`) run separately against a test project before cutting a release.

To refresh `examples/sample-rows.jsonl` from the fixture test (`tests/sample-rows.test.ts`):

```bash
claude plugin test . 2>&1 | sed -n 's/^BQAA_SAMPLE_ROW //p' > examples/sample-rows.jsonl
```

## Limitations

- **Early-access mod API**: Host CLI mods are an early-access feature (tested on v2.1.296–v2.1.304); hook events and `$` APIs may change across CLI updates.
- **Eventual deduplication (`tabledata.insertAll`)**: Because `$.http.fetch` uses HTTP rather than gRPC Storage Write API, writes go through `tabledata.insertAll`, whose `insertId` deduplication is best-effort—always deduplicate on `event_id` in queries.
- **Hook path and queue overflow**: Hooks queue events in memory and do not wait on BigQuery writes during regular turns; if the local disk cannot be written when the queue overflows, overflow rows are dropped.
- **First-prompt-only `LLM_REQUEST`**: Only the first model request of a turn carries the user's prompt; prior conversation history, system prompts, and multimodal `content_parts` are omitted to keep hooks lightweight.
- **Exit spill waits for the next session**: `session.end` never starts network requests; rows queued at exit are written to a spill file and sent 15 seconds into your next session (or via `/bqaa-backfill`) configured for the same table.
- **Mid-session reload or crash**: Reloading plugins mid-session (`/reload-plugins` or `--plugin-dir` edits) or a worker crash can lose up to one `flushIntervalMs` window of buffered in-memory rows, plus any batch currently in retry backoff.
- **User-tier classic hooks on managed CLIs**: On organization-managed CLIs, a user-installed plugin does not receive `classic.SessionStart`, `classic.SubagentStart`, or `classic.PermissionRequest` (subagent starts are inferred, and `allowManagedModsOnly` requires an admin-deployed folder; see [docs/managed-settings.md](docs/managed-settings.md)).
- **Heuristic free-text scrubbing**: Structured JSON keys enforce a strict allowlist, whereas free-text shell and URL credential redaction is best-effort and cannot parse arbitrary shell quoting, expansions, or custom delimiters (see [#546](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/546)).

## Files

```text
QUICKSTART.md                5-minute setup for sandboxes, team members, and admins
MIGRATION.md                 migrating from older marketplaces or the shell-hook tracer
.claude-plugin/plugin.json   plugin manifest and userConfig schema
hooks/                       in-process hooks, row builder, allowlist, sanitizer, and writer
setup.sh                     idempotent BigQuery dataset, table, and reporting-views setup
build_dist.sh / sync_dist.sh builds and verifies the marketplace copy in claude_code_dist
docs/                        team service-account auth, managed settings, and analytics guide
sql/                         agent_events DDL, reporting views, and sample/value queries
examples/sample-rows.jsonl   sample BQAA JSONL rows from the test fixture
tests/                       offline unit/reload/DuckDB suites and live GCP checks
types/index.d.ts             TypeScript declarations for session state
```
