# EvalBench Import Bridge

`bigquery_agent_analytics.evalbench` reads one EvalBench BigQuery job,
converts its result rows into the BQAA `agent_events` shape, and publishes
that mapping as an immutable, versioned snapshot in BQAA-owned tables.
EvalBench remains the source of truth for benchmark results, and the SDK
never writes into the ADK plugin's production `agent_events` table.

This covers the reader and deterministic row mapping from
[issue #97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
and the MVP snapshot + failed-session denominator from
[issue #435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435).
Localization, failure taxonomy, and other harnesses are later slices.

## Data Flow

```text
EvalBench BigQuery dataset                BQAA-owned target dataset

configs  -- job_id = @job_id --+          evalbench_agent_events
results  -- job_id = @job_id --+--> EvalBenchRun --> evalbench_scores_imported
scores   -- job_id = @job_id --+   (in memory)      evalbench_import_manifest
                                                     ^
                              one transaction per (job_id, import_version)
```

All three source queries filter by the parameterized `job_id` in SQL. The
reader loads that single run into memory; `materialize()` stages the mapped
rows and publishes them atomically.

## Read And Map A Run

```python
from bigquery_agent_analytics.evalbench import EvalBenchRun

run = EvalBenchRun.from_bigquery(
    project_id="benchmark-project",
    evalbench_dataset="evalbench",
    job_id="abc123",
    location="US",
)

event_rows = run.to_agent_event_rows()
print(f"Mapped {len(run.results)} scenarios and loaded {len(run.scores)} scores")
```

The reader accepts both major EvalBench result shapes:

| Meaning | NL2SQL fields | Agentic fields |
|---|---|---|
| Scenario | `id` | `eval_id` |
| Prompt | `nl_prompt` | `prompt` or `scenario.starting_prompt` |
| Final output | `generated_sql` | `stdout.response` |
| Tool calls | optional | `stdout.tool_calls` or `accumulated_tools` |

EvalBench's DataFrame writer can store nested objects as JSON or Python-literal
strings. The mapper parses both structured encodings and leaves ordinary text
unchanged.

### Reading a job that may still be running

`from_bigquery` reads `results`, `scores`, and `configs` sequentially. If the
EvalBench job is still writing, the three reads can observe different moments
and mix versions. Either import only after the job has completed, or pass
`snapshot_at` so every read uses BigQuery time travel
(`FOR SYSTEM_TIME AS OF @snapshot_at`) at the same instant:

```python
from datetime import datetime, timezone

run = EvalBenchRun.from_bigquery(
    project_id="benchmark-project",
    evalbench_dataset="evalbench",
    job_id="abc123",
    snapshot_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
)
```

The manifest records `source_snapshot_at` and content fingerprints either
way, so a source that changed after import becomes a new `import_version`
rather than silently reproducing the first one.

## Event Mapping

Each scenario uses this identity:

```text
# plain read (to_agent_event_rows())
session_id = trace_id = evalbench:{job_id}:{scenario_id}
# published snapshot (materialize / to_agent_event_rows(import_version=...))
session_id = trace_id = evalbench:{job_id}:{import_version}:{scenario_id}
agent      = evalbench:{orchestrator}:{generator}
```

Published identities are version-specific, and every synthetic span and
invocation id derives from them, so retained import versions of one job never
share a trace or session in the mirror table: a reader that filters only by
`trace_id` (such as `Client.get_trace`) sees exactly one version. Imported
score rows use the same identity, so score joins stay aligned. Published rows
also carry `attributes.evalbench_import_version`.

The identity encodes the `(job_id, import_version, scenario_id)` tuple
unambiguously: a literal `:` or `\` inside a component is escaped as `\:` /
`\\`, so `(import_version="release:1", scenario_id="case")` and
`(import_version="release", scenario_id="1:case")` get different
`session_id`s (`…:release\:1:case` vs `…:release:1\:case`), and a plain read
of a scenario named `v1:case` never aliases published version `v1`. Components
without those characters (the common case) render verbatim.

`orchestrator`, `generator`, and the run timestamp come from EvalBench's
flattened `configs` rows. If a historical run lacks config metadata, the agent
components become `unknown`; if no timestamp is available, the mapper uses the
Unix epoch and sets `attributes.evalbench_run_time_missing = true` rather than
inventing a current timestamp.

| Source data | Synthetic event | Required content |
|---|---|---|
| Prompt | `USER_MESSAGE_RECEIVED` | `text`, `text_summary` |
| Tool call | `TOOL_STARTING` | `tool`, `args`, `text_summary` |
| Tool result | `TOOL_COMPLETED` or `TOOL_ERROR` | `tool`, `result`, `text_summary` |
| Final response or generated SQL | `AGENT_COMPLETED` | `response`, `text_summary` |

Span identifiers are synthetic (`sha256` of the session identity and role);
they are not EvalBench's original OpenTelemetry spans.

Missing tool data emits no `TOOL_*` rows. Missing final output omits
`AGENT_COMPLETED`. Missing `nl_prompt`/`prompt` is a hard error because a valid
scenario trace cannot be built without the user message. Duplicate scenario IDs
are also rejected because they would otherwise produce colliding trace and span
identifiers.

Every row includes `attributes.experiment_id = job_id` and
`attributes.evalbench_scenario_id = scenario_id`. Agentic token and latency
metadata found in `stdout.stats.models` is normalized onto the terminal row as
`usage_metadata`, `input_tokens`, `output_tokens`, and `latency_ms.total_ms`.
Token counts are summed across model entries. Latency uses the maximum reported
model duration because some EvalBench producers repeat one run-level duration
for every model used by the run.

Source failures surface as `status = 'ERROR'` plus `error_message`: a
non-zero (or non-numeric) `returncode`, `stderr`, and the `*_error` columns
are collected into `attributes.evalbench_error_fields`. Usable `stderr` is a
process failure on its own, independent of `returncode`: a scenario that
exited `0` and produced a final response but also wrote to `stderr` is
published with `status = 'ERROR'` on its terminal row, never as a clean `OK`.

The destination table names are validated before any BigQuery call, and the
ADK plugin's production table name `agent_events` is rejected outright.

## Materialize A Snapshot

```python
result = run.materialize(
    target_project="analytics-project",   # may differ from the source project
    target_dataset="bqaa",
)
print(result.status, result.import_version, result.events_table)
```

`materialize()` writes three BQAA-owned tables in the target dataset (which
must already exist; the tables are created on first use):

| Table | Default name | Contents |
|---|---|---|
| Events | `evalbench_agent_events` | `agent_events` columns plus `job_id`, `import_version`; partitioned by `timestamp`, clustered by `job_id, import_version, session_id` |
| Scores | `evalbench_scores_imported` | `job_id`, `import_version`, `scenario_id`, `session_id`, `comparator`, `score FLOAT64`, `source_row JSON` (the verbatim EvalBench score row) |
| Manifest | `evalbench_import_manifest` (fixed) | one row per `(job_id, import_version)` — see below |

The extra event columns are appended after the `agent_events` contract, so
`Client.get_session_trace` and the other explicit-column readers work
unchanged when pointed at the mirror table.

### Atomic publish

Event, score, and manifest rows are loaded into per-import staging tables
(`<table>_staging_<hex>`) and then published by **one** BigQuery
multi-statement transaction:

```sql
DECLARE conflicting_manifest_rows INT64 DEFAULT 0;
BEGIN
  BEGIN TRANSACTION;
  -- Compare-and-swap: re-check the manifest *inside* the transaction.
  SET conflicting_manifest_rows = (
    SELECT COUNT(*) FROM `…evalbench_import_manifest`
    WHERE job_id = @job_id AND import_version = @import_version
      AND (results_fingerprint != @results_fingerprint OR …   -- omitted with replace=True
           OR events_table != @events_table OR scores_table != @scores_table)
  );
  IF conflicting_manifest_rows > 0 THEN RAISE USING MESSAGE = '…'; END IF;
  DELETE FROM `…evalbench_agent_events`     WHERE job_id = @job_id AND import_version = @import_version;
  DELETE FROM `…evalbench_scores_imported`  WHERE job_id = @job_id AND import_version = @import_version;
  DELETE FROM `…evalbench_import_manifest`  WHERE job_id = @job_id AND import_version = @import_version;
  INSERT INTO `…evalbench_agent_events`    (…) SELECT … FROM `…evalbench_agent_events_staging_…`;
  INSERT INTO `…evalbench_scores_imported` (…) SELECT … FROM `…evalbench_scores_imported_staging_…`;
  INSERT INTO `…evalbench_import_manifest` (…) SELECT … FROM `…evalbench_import_manifest_staging_…`;
  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE;
END;
```

There is no delete-then-append window: a failure before `COMMIT` leaves the
previously published version (or nothing) in place. Mapping errors (missing
prompt, duplicate scenario ids) are raised before any BigQuery write.

The manifest check runs both before staging (so an unchanged source is a
cheap no-op) and again inside the transaction. Two importers that both saw no
manifest row for the same explicit `import_version` therefore cannot both
commit different content: BigQuery serializes conflicting DML on the manifest
table, the later transaction re-reads the earlier row, and the guard raises
(`ValueError` in Python, nothing written). Re-running that importer then
reports `unchanged` or the fingerprint error as usual.

Staging tables are created with a six-hour expiration and dropped after
`COMMIT` on a best-effort basis; a failed drop is logged and never turns a
committed publish into an error.

### Import versions and idempotency

`import_version` defaults to the first 16 hex digits of a SHA-256 over the
order-independent fingerprints of the source `results`, `scores`, and
`configs` rows. The manifest drives idempotency:

| Situation | Outcome |
|---|---|
| No manifest row for `(job_id, import_version)` | rows published, `status = "imported"` |
| Manifest row exists with identical fingerprints | nothing written, `status = "unchanged"` |
| Same as above with `replace=True` | atomically re-published, `status = "replaced"` |
| Explicit `import_version` whose fingerprints changed | `ValueError` (pass a new version, omit it to derive one, or `replace=True`) |
| Manifest row exists but records other `events_table`/`scores_table` | `ValueError`, even with `replace=True`; nothing written |
| Two importers race on a first-time version with different content | the first commit wins; the second raises `ValueError` from the in-transaction guard |
| Derived version and the source changed | a new `import_version`; earlier versions are retained |

A given `(job_id, import_version)` therefore never accumulates duplicates, and
a published version stays bound to the destination tables in its manifest.
The manifest is the single import registry of the target dataset; its name is
fixed (`evalbench_import_manifest`, `evalbench.MANIFEST_TABLE`) rather than a
`materialize` argument, so every import into the dataset — whatever
`events_table`/`scores_table` it writes — checks the same registry before
deleting published rows. A second manifest cannot be used to re-publish
changed source under an existing version around the first manifest row.
An `unchanged` result always refers to the tables that were actually written;
to publish the same version elsewhere, choose a new `import_version` (moving
rows between tables is not supported, so `replace=True` cannot orphan them).

### Manifest

The manifest row binds every published version to its source:

| Column | Meaning |
|---|---|
| `job_id`, `import_version` | the published version |
| `source_project`, `source_dataset`, `source_snapshot_at` | where and (if pinned) when the source was read |
| `results_count`, `scores_count`, `configs_count` | source row counts |
| `results_fingerprint`, `scores_fingerprint`, `configs_fingerprint` | SHA-256 content fingerprints |
| `events_table`, `scores_table` | fully-qualified published tables |
| `event_row_count`, `score_row_count` | rows published |
| `imported_at` | publish timestamp (UTC) |

`EvalBenchImportResult.manifest` returns the same row in memory.

## Failed-Session Contract (W0.4)

`returncode == 0` means the EvalBench process **completed**, not that the
scenario **passed**. A session is *failed* when any of the following holds:

- **process failure** — any event has `status = 'ERROR'` (non-zero
  `returncode`, usable `stderr` regardless of `returncode`, or a source
  `*_error` column);
- **missing completion** — the session has no `AGENT_COMPLETED` event;
- **score failure** — the per-benchmark `EvalScorePolicy` is not met.

`EvalScorePolicy(min_scores={comparator: min_score}, missing_score_fails=True)`
is the policy hook. A session fails the score gate if any listed comparator
scores below its threshold. By default a missing or `NULL` score for a listed
comparator also fails, because a run that completed without being scored is
not evidence of success; set `missing_score_fails=False` to relax that.

`failed_sessions_sql()` returns the denominator query for one pinned import;
it takes `@job_id` and `@import_version` parameters and never reads
`returncode` directly:

```python
from google.cloud import bigquery
from bigquery_agent_analytics.evalbench import (
    EvalScorePolicy,
    failed_sessions_sql,
    import_query_parameters,
)

sql = failed_sessions_sql(
    target_project="analytics-project",
    target_dataset="bqaa",
    policy=EvalScorePolicy({"goal_completion": 0.5}),
)
job_config = bigquery.QueryJobConfig(
    query_parameters=import_query_parameters("abc123", result.import_version)
)
rows = client.query(sql, job_config=job_config).result()
```

Each returned row carries `session_id`, `scenario_id`, `started_at`,
`process_failed`, `missing_completion`, `score_failed`, the combined
`failed` flag, and `failing_scores` (the comparators that missed their
threshold, with the offending score or `NULL`).

`classify_sessions(event_rows, score_rows, policy)` is the in-memory
reference implementation of the same contract over `to_agent_event_rows()`
and `to_score_rows()` output; the unit tests pin the two to each other.

## CLI

```bash
bq-agent-sdk evalbench-import \
  --project-id benchmark-project --evalbench-dataset evalbench --job-id abc123 \
  --target-project analytics-project --target-dataset bqaa \
  [--import-version v1] [--replace] [--snapshot-at 2026-05-01T08:00:00Z] \
  [--events-table evalbench_agent_events] [--scores-table evalbench_scores_imported] \
  [--location US] [--format json|text|table]
```

The command prints the `EvalBenchImportResult` (including the manifest) and
exits `0` for `imported`, `replaced`, or `unchanged`, and `2` on invalid
input (including `--events-table agent_events`, which is rejected before any
BigQuery call), a changed source under an explicit `--import-version`, a
version already bound to other destination tables, or a BigQuery error. There
is no `evalbench-score` command.

## Why These Fields Matter

The mapping follows the SDK queries that consume the mirror table:

- `client.py` projects the complete trace row contract in `_GET_TRACE_QUERY`.
- `trace.py` reads `attributes.experiment_id` for `TraceFilter`.
- `evaluators.py` reads latency and token counters in `SESSION_SUMMARY_QUERY`.
- `evaluators.py` builds judge text from `content.text_summary`, selects
  `content.response`, and drops traces whose assembled text is not longer
  than ten characters.

The last constraint is easy to miss: a row can satisfy the BigQuery schema but
remain invisible to `LLMAsJudge` when `text_summary` is absent. The mapper
therefore populates it on every emitted event.

## Current Boundaries

- Runs are imported one `job_id` at a time and held in memory.
- The target dataset must exist; only the three tables are auto-created.
- Concurrent imports of the same `(job_id, import_version)` serialize on
  BigQuery's transaction locks; a conflicting import fails rather than
  corrupting the corpus.
- Live BigQuery integration coverage for `materialize()` is not yet gated
  into the test suite; the unit tests use a stubbed client.
