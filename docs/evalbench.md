# EvalBench Import Bridge

`bigquery_agent_analytics.evalbench` reads one EvalBench BigQuery job,
converts its result rows into the BQAA `agent_events` shape, and publishes
that mapping as an immutable, versioned snapshot in BQAA-owned tables.
EvalBench remains the source of truth for benchmark results, and the SDK
never writes into the ADK plugin's production `agent_events` table.

This covers the reader and deterministic row mapping from
[issue #97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
and the MVP snapshot + failed-session denominator from
[issue #435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435):
the versioned writer (slice 1) and the queryable `evalbench_failed_sessions`
view plus version-pinned consumer (slice 2). Localization, failure taxonomy,
and other harnesses are later slices; the sequencing and gates are in
[`docs/agentforensics_mvp_plan.md`](agentforensics_mvp_plan.md).

## Data Flow

```text
EvalBench BigQuery dataset                BQAA-owned target dataset

configs  -- job_id = @job_id --+          evalbench_agent_events
results  -- job_id = @job_id --+--> EvalBenchRun --> evalbench_scores_imported
scores   -- job_id = @job_id --+   (in memory)      evalbench_import_manifest
                                                     ^         |
                              one transaction per (job_id, import_version)
                                                               v
                                          evalbench_failed_sessions (VIEW,
                                          pinned to the job's latest import)
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
# plain read (to_agent_event_rows()) -- the v0.5.1 contract, unchanged
session_id = trace_id = evalbench:{job_id}:{scenario_id}
# published snapshot (materialize / to_agent_event_rows(import_version=...))
session_id = trace_id = evalbench-import:{job_id}:{import_version}:{scenario_id}
agent      = evalbench:{orchestrator}:{generator}
```

The plain-read identity is frozen: `session_id`/`trace_id` are the unescaped
`evalbench:{job_id}:{scenario_id}` and the synthetic `invocation_id`,
`span_id`, and `parent_span_id` values are the same SHA-256-derived ids v0.5.1
produced (the unit tests pin golden vectors), so re-mapping an existing run
after an upgrade keeps its stored trace references.

Published identities live in a separate `evalbench-import:` namespace, are
version-specific, and every synthetic span and invocation id derives from
them with injective framing, so retained import versions of one job never
share a trace or session in the mirror table — and never share one with a
plain read either: a reader that filters only by `trace_id` (such as
`Client.get_trace`) sees exactly one version. Imported score rows use the
same identity, so score joins stay aligned. Published rows also carry
`attributes.evalbench_import_version`.

The published identity encodes the `(job_id, import_version, scenario_id)`
tuple unambiguously: a literal `:` or `\` inside a component is escaped as
`\:` / `\\`, so `(import_version="release:1", scenario_id="case")` and
`(import_version="release", scenario_id="1:case")` get different
`session_id`s (`…:release\:1:case` vs `…:release:1\:case`). Components
without those characters (the common case) render verbatim. A plain read of a
scenario named `v1:case` (`evalbench:job:v1:case`) cannot alias published
version `v1` (`evalbench-import:job:v1:case`) because the prefixes differ.

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
must already exist; the tables are created on first use) and serializes
publishes through a fourth, fixed lock table:

| Table | Default name | Contents |
|---|---|---|
| Events | `evalbench_agent_events` | `agent_events` columns plus `job_id`, `import_version`; partitioned by `timestamp`, clustered by `job_id, import_version, session_id` |
| Scores | `evalbench_scores_imported` | `job_id`, `import_version`, `scenario_id`, `session_id`, `comparator`, `score FLOAT64`, `source_row JSON` (the verbatim EvalBench score row) |
| Manifest | `evalbench_import_manifest` (fixed) | one row per `(job_id, import_version)` — see below |
| Lock | `evalbench_import_lock` (fixed) | one sentinel row (`lock_id = 'evalbench-import'`, `claim_count`, `claimed_at`, `claimed_job_id`, `claimed_import_version`) that every publish transaction updates first |
| Failed sessions | `evalbench_failed_sessions` (view) | `failed_sessions_sql()` pinned to the job's latest successful import — see [Queryable view](#queryable-view) |

The extra event columns are appended after the `agent_events` contract, so
`Client.get_session_trace` and the other explicit-column readers work
unchanged when pointed at the mirror table.

### Atomic publish

Event, score, and manifest rows are loaded into per-import staging tables
(`<table>_staging_<hex>`). The lock sentinel is then seeded by its own
committed DML job (`INSERT … WHERE NOT EXISTS`, a no-op once it exists) so
that it is part of the snapshot of the transaction that follows, and the rows
are published by **one** BigQuery multi-statement transaction:

```sql
DECLARE conflicting_manifest_rows INT64 DEFAULT 0;
BEGIN
  BEGIN TRANSACTION;
  -- Claim the dataset lock: the only statement guaranteed to mutate a row.
  UPDATE `…evalbench_import_lock`
  SET claim_count = claim_count + 1, claimed_at = CURRENT_TIMESTAMP(),
      claimed_job_id = @job_id, claimed_import_version = @import_version
  WHERE lock_id = 'evalbench-import';
  IF @@row_count = 0 THEN RAISE USING MESSAGE = '…sentinel is missing…'; END IF;
  -- Defence in depth: re-check the manifest *inside* the transaction.
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

Why a lock row: BigQuery transactions use snapshot isolation, and
[concurrent transactions are cancelled only when they mutate rows in the
same table](https://cloud.google.com/bigquery/docs/transactions#transaction_concurrency)
— reads and appends run concurrently, and [`INSERT` never conflicts with
other DML](https://cloud.google.com/bigquery/docs/data-manipulation-language#dml_statement_conflicts).
Two first-time imports of one `(job_id, import_version)` that both began
before either committed would therefore both see no manifest row, both run
keyed `DELETE`s that match nothing, and both `INSERT` — two manifests and a
mixed corpus under one supposedly immutable version. The claim `UPDATE`
always mutates the pre-existing sentinel, so at most one of the two
transactions commits; BigQuery cancels the other with a "concurrent update"
error, which `materialize()` reports as `ValueError` (nothing written).
Re-running that importer then reports `unchanged` or the fingerprint error as
usual. The same claim also serializes replace/re-import publishes into the
dataset.

The manifest check runs both before staging (so an unchanged source is a
cheap no-op) and again inside the transaction, after the claim. An importer
whose pre-publish read was stale but whose transaction started after another
commit sees the committed row there and the guard raises (`ValueError` in
Python, nothing written) before any `DELETE` runs.

The unit tests exercise this with a stub that models the snapshot-isolation
rules above (both transactions start from one snapshot, only a mutation of
the sentinel conflicts); they do not run against live BigQuery.

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
| Two importers race on a first-time version with different content | the first to commit wins; the second is cancelled on the lock claim (or, if its transaction started later, refused by the in-transaction guard) and raises `ValueError` |
| Derived version and the source changed | a new `import_version`; earlier versions are retained |

A given `(job_id, import_version)` therefore never accumulates duplicates, and
a published version stays bound to the destination tables in its manifest.
The manifest is the single import registry of the target dataset; its name is
fixed (`evalbench_import_manifest`, `evalbench.MANIFEST_TABLE`) rather than a
`materialize` argument, so every import into the dataset — whatever
`events_table`/`scores_table` it writes — checks the same registry before
deleting published rows. The lock table (`evalbench_import_lock`,
`evalbench.LOCK_TABLE`) is fixed for the same reason. A second manifest cannot be used to re-publish
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
| `imported_at` | publish timestamp (UTC; caller-supplied via `imported_at=`, so two publishes may share it) |
| `generation_id` | opaque id of this committed generation of the row: minted by every publish (a `replace=True` of the same version label included) and by every committed change of `view_policy`; never shared |
| `view_policy` | the score gate the failed-session view renders for this version (canonical JSON of the `EvalScorePolicy`, `NULL` for none) — committed manifest state, never taken from the view |

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

### Queryable view

Every successful `materialize()` call (`imported`, `replaced`, *and*
`unchanged`) also keeps one view in the target dataset — default name
`evalbench_failed_sessions` (`evalbench.DEFAULT_FAILED_SESSIONS_VIEW`),
overridable with `failed_sessions_view=` / `--failed-sessions-view`, or
`None` / `--skip-failed-sessions-view` to manage no view — whose body is
`failed_sessions_sql()` with `@job_id` and `@import_version` rendered as
string literals. Views cannot take query parameters, so the pin lives in
the definition itself, and the view can never scan another version. The
view's query text (its description names the pinned version too) is:

```sql
-- evalbench_failed_sessions pin: {"generation_id": "3f9c2c1e0b7a4d6e9a1b5c8d7e6f4a2b", "import_version": "v2", "job_id": "abc123", "policy": {"min_scores": {"goal_completion": 0.9}, "missing_score_fails": true}}
WITH sessions AS (
  SELECT ...
  FROM `analytics-project.bqaa.evalbench_agent_events`
  WHERE job_id = "abc123" AND import_version = "v2"
  ...
```

The pin records the job, the version, the manifest generation it renders
(`generation_id`, the row's opaque id — a `replace=True` of the same
version label is a new generation, and so is a committed policy change;
`imported_at` is caller-chosen and may repeat, so it is not the
generation), and the score policy rendered into the view (`null` without
one). The view body is a pure function of the latest manifest row: the
gate it renders is the row's committed `view_policy`, never something the
view says about itself. Nothing in the pin proves ownership: the importer
treats a view as its own only when the pin names a version that job
committed to the manifest *and* the body is byte-for-byte what the
importer renders for that manifest row — with the row's own `view_policy`
when the pin names the row's current `generation_id`, so a canonical
rendering of the current generation under any other gate is foreign; or,
for a view a superseded generation left behind, the pinned generation and
policy over the row's own tables, in which case the view is only ever
advanced to the committed rendering, so whatever gate it carries is never
preserved. The comparison is exact — BigQuery returns a view's query text
verbatim, and whitespace inside a rendered literal (`job_id = "job  x"`
versus `"job x"`) changes which rows the view reads, so no whitespace
normalization is applied. A pin copied or forged onto other SQL (however
self-consistent), a contract-shaped body for an unpublished version or
over other tables, or a managed view whose body somebody edited or
reformatted, is a foreign object and is refused. The importer never uses
`CREATE OR REPLACE VIEW`; it creates the view with create-if-absent and
replaces it with an ETag-conditional update, after re-reading both the
view and the latest manifest row immediately before writing.

Pinning rules:

| Situation | View |
|---|---|
| First import of a job | created, pinned to that version |
| New `import_version` of the same job | replaced, pinned to the new version (the job's latest `imported_at` in the manifest) |
| No-op re-import (`unchanged`) | untouched; created only if it does not exist yet (corpora published before views existed) |
| `replace=True` of an older version | re-pinned to it — the replace refreshed `imported_at`, so it *is* the latest successful import |
| `policy=` / `--min-score` given, changed, or dropped | the gate is committed to the manifest row (`view_policy`): a publish records it with the row, and an `unchanged` re-import that names another gate commits it to the row it found — under a new `generation_id`, and only if that row is still the generation it found — before the view is re-rendered from the row (including a later same-version call *without* a policy, which removes the gate); nothing is written when version and policy both match |
| `policy=` on a call whose version is **not** the latest import | the gate is recorded on that version's own row; the view renders the latest version's row, so a view already pinned to the latest generation is left exactly as it is, and a view that is absent or behind is created or advanced carrying the latest row's committed gate (never this call's, and never the gate the stale view happened to carry) |
| `policy=` on a call whose *generation* is not the one the manifest holds (a concurrent `replace=True` of the same version — even with the same `imported_at` — committed a newer `generation_id`) | as above: neither the version label nor the timestamp decides, the committed generation does. Every generation is rendered into the pin, so the newer replacement always rewrites the view (bumping its ETag) even when version, gate and SQL are otherwise identical; a delayed older caller's conditional replace then fails, re-reads, and renders the newer generation's committed gate |
| A view at that name that *is* the importer's rendering of the current generation, but under a gate the manifest row does not record | `ValueError` before anything is written, from every later call (an older version's unchanged re-import included): the view cannot vouch for its own gate, only the manifest can |
| View at that name pinned to **another job** | `ValueError` before anything is written; use one `failed_sessions_view` name per job |
| A table, a view the importer did not create, a copied pin over other SQL, or a managed view whose query was edited, at that name | `ValueError` before anything is written (before the import tables are even created); the importer never replaces objects whose definition it cannot vouch for — drop the object or pick another name |
| Two imports of one job race on the view | the loser of the create race (`409`) or ETag check (`412`) re-reads the view and the latest manifest and re-decides, up to three times, so a delayed writer never overwrites a newer pin — nor re-renders it with its own (older) policy; a race against **another job's** view fails closed with the import already published |

"Latest successful import" is the manifest row with the newest
`imported_at` (ties broken by `import_version`), which is also what the
Python consumer below resolves when no version is pinned, so the two agree.
The view is rewritten only when its rendered definition (pinned version,
policy, or SQL) would change, and is written after the publish transaction
committed; if the write fails (for example a missing
`bigquery.tables.update` permission, or a view that kept changing
underneath the conditional replace) `materialize()` raises a `ValueError`
naming the published version, and simply re-running it retries the view
(`status == "unchanged"`). A specific older version is queried
with the parameterized `failed_sessions_sql()` shown above or with
`failed_sessions(import_version=...)`.

### Version-pinned consumer

`failed_sessions()` lists the failed sessions of exactly one published
version and never mixes two:

```python
from bigquery_agent_analytics.evalbench import EvalScorePolicy, failed_sessions

listing = failed_sessions(
    target_project="analytics-project",
    target_dataset="bqaa",
    job_id="abc123",
    # import_version="v1",   # pin one version; default: latest successful import
    policy=EvalScorePolicy({"goal_completion": 0.5}),
)
print(listing.import_version, listing.failed_count, "of", listing.session_count)
for session in listing.sessions:
    print(session.session_id, session.scenario_id, session.failing_scores)
```

The version is resolved from the manifest *before* any event row is read:
an explicit `import_version` must have a manifest row (otherwise
`ValueError`), and the default is the job's latest successful import. The
query is `failed_sessions_sql()` over the `events_table`/`scores_table`
recorded in that manifest row (a version stays bound to the tables it was
published to), executed with `import_query_parameters(job_id,
import_version)`. `EvalBenchFailedSessions.sessions` holds the failed rows
(`include_passed=True` returns every session); each `EvalBenchSession`
carries `job_id`, `import_version`, the versioned `session_id`/`trace_id`,
`scenario_id`, `started_at`, the four flags, and `failing_scores`.
`EvalBenchSession.verdict()` is the matching `SessionVerdict`, so the
listing is interchangeable with `classify_sessions()` output.

### Drill-down without mixing versions

Published rows use the version-specific identity
`evalbench-import:{job_id}:{import_version}:{scenario_id}` for both
`session_id` and `trace_id`, so a `Client` pointed at the mirror table
resolves one version by construction — there is no other version under
that identity to merge:

```python
from bigquery_agent_analytics import Client

client = Client(
    project_id="analytics-project",
    dataset_id="bqaa",
    table_id="evalbench_agent_events",   # the mirror table, never agent_events
)
for session in listing.sessions:
    trace = client.get_session_trace(**session.trace_selector())
    # equivalently: trace = session.get_trace(client)
```

`trace_selector()` returns `{"session_id": ..., "experiment_id": job_id}`:
the session id already pins the import version, and the `experiment_id`
pin matches `attributes.experiment_id` on every published row, so the
selector is unambiguous for the identity-resolving reader. Extra
`get_session_trace` keyword arguments (for example `event_types=`) pass
through `session.get_trace(client, ...)`.

## Score An Import With The LLM Judge (#97)

`bq-agent-sdk evalbench-score` is a thin wrapper over the existing
`Client.evaluate` + `LLMAsJudge` path: nothing new is computed, the judge
is simply pointed at one published import version of the mirror table.

```python
from bigquery_agent_analytics import Client
from bigquery_agent_analytics.evalbench import import_sessions
from bigquery_agent_analytics.evaluators import LLMAsJudge

pinned = import_sessions(
    target_project="analytics-project",
    target_dataset="bqaa",
    job_id="abc123",
    # import_version="v1",   # pin one version; default: latest successful import
)
client = Client(
    project_id="analytics-project",
    dataset_id="bqaa",
    table_id="evalbench_agent_events",   # must equal pinned.events_table
)
report = client.evaluate(
    evaluator=LLMAsJudge.correctness(threshold=0.7),
    filters=pinned.trace_filter(),
)
print(pinned.import_version, report.pass_rate, report.total_sessions)
```

`import_sessions()` resolves the version from the manifest exactly as
`failed_sessions()` does (an explicit `import_version` must be published;
the default is the job's latest successful import) and then reads that
version's distinct `session_id` values from the `events_table` recorded in
the manifest row. `EvalBenchImportSessions.trace_filter()` returns
`TraceFilter(experiment_id=job_id, session_ids=<those ids>, limit=<count>)`:
`TraceFilter` has no import-version dimension, so the version pin reaches
`Client.evaluate` through the exact versioned session identities, which
retained versions of one job never share. `trace_filter()` refuses an
empty session set because `TraceFilter` treats "no `session_ids`" as
unfiltered, which would silently widen the evaluation to every retained
version of the job. `Client.evaluate` itself is unchanged.

## CLI

```bash
bq-agent-sdk evalbench-import \
  --project-id benchmark-project --evalbench-dataset evalbench --job-id abc123 \
  --target-project analytics-project --target-dataset bqaa \
  [--import-version v1] [--replace] [--snapshot-at 2026-05-01T08:00:00Z] \
  [--events-table evalbench_agent_events] [--scores-table evalbench_scores_imported] \
  [--location US] [--format json|text|table]
```

The command prints the `EvalBenchImportResult` (including the manifest and
the `failed_sessions_view` it left pinned) and exits `0` for `imported`,
`replaced`, or `unchanged`, and `2` on invalid input (including
`--events-table agent_events` or a malformed `--min-score`, both rejected
before any BigQuery call), a changed source under an explicit
`--import-version`, a version already bound to other destination tables, a
failed-session view at that name that belongs to another job, or a BigQuery
error. `--failed-sessions-view NAME` picks the view name,
`--skip-failed-sessions-view` manages none, and repeatable
`--min-score COMPARATOR=MIN_SCORE` (with `--missing-score-passes` to relax
the missing-score rule) renders an `EvalScorePolicy` into the view.

```bash
bq-agent-sdk evalbench-failed-sessions \
  --project-id analytics-project --target-dataset bqaa --job-id abc123 \
  [--import-version v1] [--min-score goal_completion=0.5]... \
  [--missing-score-passes] [--include-passed] \
  [--location US] [--format json|text|table]
```

Prints the `EvalBenchFailedSessions` listing (`--format table` prints one
row per session) for exactly one published version — the job's latest
successful import unless `--import-version` pins one — and exits `0` when
the listing was produced (possibly empty) or `2` on invalid input, a job or
version with no published import, or a BigQuery error.

```bash
bq-agent-sdk evalbench-score \
  --project-id analytics-project --dataset-id bqaa --job-id abc123 \
  [--table-id evalbench_agent_events] [--import-version v1] \
  [--evaluator correctness|hallucination|sentiment] [--threshold 0.7] \
  [--strict] [--exit-code] [--endpoint gemini-2.5-flash] [--connection-id ID] \
  [--location US] [--format json|text|table]
```

Runs the chosen `LLMAsJudge` (default `correctness`, judge default
threshold `0.5`) through `Client.evaluate` over the mirror `--table-id`,
narrowed to one published version of `--job-id` (the latest successful
import unless `--import-version` pins one). The output is the ordinary
`EvaluationReport` (`--format text` prints its summary) with
`details.evalbench` naming the scored `job_id`, `import_version`,
`events_table`, and `pinned_sessions`, so a scorecard can always be traced
back to the version it judged. `--strict`, `--endpoint`, and
`--connection-id` behave as they do for `bq-agent-sdk evaluate`.

Exit codes match `evaluate`: `0` when the scorecard was produced (failing
sessions included), `1` with `--exit-code` when at least one session failed
the threshold (the same `FAIL session=... metric=... feedback="..."` lines
are printed to stderr first), and `2` on invalid input — an unknown
`--evaluator`, the reserved `agent_events` table, a job or version with no
published import, a `--table-id` the version was not published to, or a
version with no sessions — or a BigQuery error. `examples/evalbench_score_gate.sh`
shows the CI gate form.

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
- The target dataset must exist; only the four tables and the
  failed-session view are auto-created.
- One `evalbench_failed_sessions` view is pinned to one job; datasets that
  hold several jobs need one `--failed-sessions-view` name per job (the
  importer refuses to re-point another job's view).
- Concurrent publishes into one dataset serialize on the lock sentinel
  (coarse: one publish at a time per dataset); the loser fails with
  `ValueError` and nothing written rather than corrupting the corpus, and
  can simply be re-run.
- Live BigQuery integration coverage for `materialize()` is not yet gated
  into the test suite; the unit tests use a stubbed client.
