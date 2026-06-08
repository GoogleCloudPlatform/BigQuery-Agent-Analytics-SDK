# Periodic materialization — Cloud Run Job + Cloud Scheduler

Run `bqaa context-graph` on a cron, against your own
BigQuery project, with one local command and one deploy command.

The migration v5 demo (`examples/migration_v5/`) ships the
ontology, binding, and entity-table DDL. The artifact
pipeline that produced them (`ontology_artifacts.py`) is
ontology-agnostic, but **this deploy bundles the checked-in
MAKO snapshots** — running against a different ontology means
regenerating those snapshots for your config first and
re-pointing the deploy at the new files. This directory wraps
the bundled MAKO artifacts in a hands-off scheduled
deployment: a Cloud Run Job that fires every N hours via
Cloud Scheduler, materializes the last N hours of events into
your graph dataset, and emits a structured JSON report to
Cloud Logging.

## Customer playbook (skim this first)

The customer-facing sequence — every section below corresponds
to a numbered step here:

| # | Step | Where |
|---|------|-------|
| 0 | Enable required APIs + grant runtime IAM | [Prerequisites](#prerequisites) |
| 1 | Decide your **events** vs **graph** dataset names | [Datasets](#dataset-roles-events-vs-graph) |
| 2 | Local dry-run against your project (no Cloud Run) | [Local dry-run](#local-dry-run) |
| 3 | Pick a schedule | [Recommended schedules](#recommended-schedules) |
| 4 | Deploy with `--smoke` (verifies in one shot) | [Deploy to Cloud Run + Cloud Scheduler](#deploy-to-cloud-run-cloud-scheduler) |
| 5 | Read the JSON log shape per run | [Expected JSON log shape](#expected-json-log-shape) |
| 6 | Wire the Cloud Monitoring alert on `ok=false` | [Cloud Monitoring alerts](#cloud-monitoring-alerts) |
| 7 | Query the state-table audit log | [Inspecting results](#inspecting-results) |
| 8 | If something looks wrong | [Failure-mode surface](#failure-mode-surface-post-167) + [Troubleshooting](#troubleshooting) |
| 9 | Tear down or redeploy | [Cleanup and redeploy](#cleanup-and-redeploy) |

The rest of the doc has design rationale, the exact IAM matrix,
operational notes, and the captured evidence from the live
deployment verification in PR #166.

## Production shape

```
agent_events            bqaa context-graph         graph entity/
(your events DS)  ────► (Cloud Run Job, every N hrs) ──► relationship tables
                              │                          (your graph DS)
                              ▼
                       _bqaa_materialization_state
                       (checkpoint / state table,
                        co-located with the graph DS)
```

Per run, the orchestrator:

1. Reads the prior checkpoint from `_bqaa_materialization_state`.
2. Scans events in `[checkpoint - overlap_minutes, run_started_at)`,
   capped at `lookback_hours` worth of history.
3. Discovers terminal-event sessions (`event_type =
   'AGENT_COMPLETED'`) and materializes them one at a time.
4. Advances the checkpoint to the latest successful session's
   completion timestamp (never past a failure — partial failure
   leaves a tight high-water mark for the next run).
5. Writes the JSON report to stdout for Cloud Logging.

State-table semantics, overlap-windowed late-arrival handling,
and idempotent retries are all in the SDK's design contract — see
`src/bigquery_agent_analytics/materialize_window.py` for the
full prose.

## Prerequisites

### Required APIs

Enable these in the target project before deploying. The
deploy script enables Cloud Scheduler itself if missing; the
others should already be on or operators need them anyway:

```bash
gcloud services enable \
  bigquery.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  --project=your-project
```

`aiplatform.googleapis.com` is required because the demo's
extraction path calls `AI.GENERATE`. Without it, every session
will fail with `error_code = "empty_extraction"` (surfaced as a
hard `ok=false` since PR #167).

### Required IAM

The deploy script creates **two** service accounts by default
(production posture, per issue #182):

* `bqaa-periodic-runtime-sa@PROJECT.iam.gserviceaccount.com`
  — the Cloud Run Job's runtime identity. Holds the BigQuery +
  (conditionally) Vertex AI roles below.
* `bqaa-periodic-scheduler-sa@PROJECT.iam.gserviceaccount.com`
  — the Cloud Scheduler trigger's OAuth identity. Holds only
  `roles/run.invoker` on the specific Cloud Run Job. No
  BigQuery, no Vertex AI, no project-wide perms.

Pass `--single-sa` to fall back to the pre-#182 combined
identity (`bqaa-periodic-sa@PROJECT.iam.gserviceaccount.com`)
when the two-SA model isn't worth the operational overhead.

| Scope | Role | Granted to | Why |
|---|---|---|---|
| Project | `roles/bigquery.jobUser` | runtime SA | Run BQ jobs (DDL, discovery, state writes) |
| Project | `roles/aiplatform.user` | runtime SA | Call `AI.GENERATE` for entity extraction. Granted in `--extraction-mode=ai-fallback` (default); skipped (and idempotently removed if pre-existing) in `--extraction-mode=compiled-only` |
| Cloud Run Job | `roles/run.invoker` | **scheduler-caller SA** | Cloud Scheduler invokes the job. Granted on the specific job resource, not project-wide. With `--single-sa` this lands on the same SA as the runtime |
| Events DS | `roles/bigquery.dataViewer` | runtime SA | Read `agent_events`; events stay read-only |
| Graph DS | `roles/bigquery.dataEditor` | runtime SA | Write entity/relationship tables + `_bqaa_materialization_state` |

The split keeps the scheduler-caller narrow: it can fire the
job and nothing else. Combining the two paths (`--single-sa`)
gives the scheduler caller broader permissions than it ever
exercises — least-privilege violation that matters in
regulated industries (advertising, financial services,
healthcare). The deploy script handles every grant for both
modes.

### General prerequisites

* GCP project with the BigQuery, Cloud Run, Cloud Scheduler, and
  Cloud Build APIs enabled (per above).
* **Events dataset** (`BQAA_EVENTS_DATASET_ID`) already exists
  with a populated `agent_events` table. The BQ AA plugin writes
  to this; if you've never run an agent against BQAA, seed one
  for this demo via `python examples/migration_v5/run_agent.py
  --project YOUR_PROJECT --dataset YOUR_EVENTS_DS --sessions 3`.
  This dataset is **read-only** for the periodic job — the
  job never writes here.
* **Graph dataset** (`BQAA_GRAPH_DATASET_ID`) — `run_job.py`
  creates this on first invocation if missing (idempotent), so
  you don't have to pre-create it. The entity/relationship
  tables and the state/checkpoint table all live here.
* `gcloud` authenticated with permissions to deploy Cloud Run
  Jobs, create scheduler triggers, and grant IAM bindings.
* `python3` on PATH. The deploy script uses Python to apply
  dataset-level IAM via the BigQuery client's `AccessEntry`
  API (since `bq add-iam-policy-binding` requires project
  allowlisting in some environments). If your `python3` doesn't
  have `google-cloud-bigquery` installed, the script
  transparently creates a one-shot temp venv with it — no
  manual install required. If it does (e.g., you ran
  `pip install -e .` from the repo root), the script reuses
  that directly.

## Dataset roles (events vs graph)

Two distinct datasets, two distinct lifecycles. The deploy
script enforces this at the IAM layer (events READER, graph
EDITOR) so a misconfigured run can't write to the events
dataset.

| Dataset | Holds | Lifecycle | IAM for runtime SA |
|---|---|---|---|
| **Events** (`BQAA_EVENTS_DATASET_ID`) | `agent_events` table — raw event stream written by the BQ AA plugin. | Owned + written by the agent runtime, never the materialization job. Pre-existing. | `roles/bigquery.dataViewer` only (read) |
| **Graph** (`BQAA_GRAPH_DATASET_ID`) | Entity tables (`decision_execution`, `candidate`, …), relationship tables (`evaluates_candidate`, …), and the `_bqaa_materialization_state` audit/checkpoint table. | Created by the deploy script's `bq mk` if missing. Owned by the materialization job. | `roles/bigquery.dataEditor` (read + write) |

The events dataset is **read-only** for the periodic job. The
graph dataset is the only write target. The state table is
co-located with the graph dataset — that decision means a
predicate switch (e.g., swapping `--completion-event-type`)
auto-invalidates the checkpoint because the `state_key` SHA
changes; see the orchestrator design contract for the full
treatment.

If you have multiple agent applications writing to one events
dataset, run one materialization job per application with
different graph datasets. State-keys won't collide; checkpoints
stay isolated.

## Local dry-run

Run the job once on your laptop against a real BigQuery project —
no Cloud Run required. Useful for shaking out the env-var setup
before paying for a deploy:

```bash
# Choose ONE install path (they're mutually exclusive — running
# both leaves you on the published wheel, not your local edits):
#
# (a) Editable from the repo root — picks up in-flight SDK
#     changes you're iterating on locally. The deploy script's
#     vendored-SDK staging uses the same source tree, so a
#     local dry-run and a Cloud Run deploy execute the same
#     code. Use this when you're modifying the SDK and want to
#     test before publishing.
pip install -e .
#
# (b) Pinned from PyPI — once you're on a stable SDK version.
#     0.3.3 adds schema-derived (``--property-graph``) deploy
#     parity on top of the 0.3.2 migration-v5 production track
#     (compiled-only deploy, backfill, orphan watchdog,
#     Terraform module, split SAs, ``--max-retries``). Use this
#     for unmodified production use.
pip install 'bigquery-agent-analytics>=0.3.3'

# Either way, install the example's ancillary deps:
pip install -r examples/migration_v5/periodic_materialization/requirements.txt

BQAA_PROJECT_ID=your-project \
BQAA_EVENTS_DATASET_ID=your_events_dataset \
BQAA_GRAPH_DATASET_ID=your_graph_dataset \
BQAA_LOOKBACK_HOURS=6 \
BQAA_OVERLAP_MINUTES=15 \
python examples/migration_v5/periodic_materialization/run_job.py
```

Output is a single JSON line on stdout (the materialize-window
report) — pipe through `jq` for readability:

```bash
... python run_job.py | jq .
```

Exit codes mirror the SDK CLI:

* `0` — every discovered session materialized cleanly.
* `1` — expected failure: at least one session failed, or
  binding-validate detected schema drift against live BigQuery.
* `2` — unexpected internal error (config missing, code bug).

## Recommended schedules

Pick a schedule + window based on how stale you can tolerate
the graph being. The orchestrator's overlap-windowed re-scan
catches late-arriving events; pair `overlap-minutes` with the
ingestion lag you actually see on your `agent_events` stream
(if your plugin writes are synchronous, 15min is plenty; if
events trickle in over hours, scale up).

| Latency target | Cron | `--lookback-hours` | `--overlap-minutes` | Notes |
|---|---|---|---|---|
| **~1 hour** | `0 * * * *` | `2` | `15` | Tight window + small overlap. Best for low-latency monitoring use cases. Higher BQ cost per day (24 runs). |
| **~6 hours** | `0 */6 * * *` | `8` | `30` | Default in the deploy script. Good balance for dashboarding / reporting use cases. 4 runs per day. |
| **Daily** | `0 2 * * *` | `30` | `60` | Catch-up window covers any late-arriving events from the prior day. 1 run per day. Pair with off-peak `02:00` to avoid contending with daytime BQ slots. |
| **Backfill** | (manual) | depends | depends | For one-shot catch-up: `bqaa context-graph --lookback-hours $N` with N covering the gap. Defer to a future `--backfill --from/--to` mode once it ships. |

Rules of thumb:

* `lookback-hours` is an upper bound on history scanned, not
  the typical scan size. The orchestrator scans
  `[max(checkpoint - overlap, run_started - lookback), run_started)`,
  so steady-state runs scan only the new + overlap window.
* `overlap-minutes` should cover your ingestion's worst-case
  lag. Conservative is fine — the materializer is idempotent
  on the session-id boundary.
* `max-sessions` (optional) caps per-run cost. Useful for the
  first few runs against a large backlog.

## Deploy to Cloud Run + Cloud Scheduler

One command:

```bash
./examples/migration_v5/periodic_materialization/deploy_cloud_run_job.sh \
  --project your-project \
  --region us-central1 \
  --events-dataset your_events_dataset \
  --graph-dataset your_graph_dataset \
  --schedule "0 */6 * * *" \
  --smoke
```

`--smoke` (optional) runs the job once after deploy and tails
the logs, so you find out *now* whether the deploy actually
works — not when the first scheduled fire happens six hours
later.

### Schema-derived deploy (`--property-graph`)

The deploy above bundles the MAKO `ontology.yaml` + `binding.yaml`. For a
**rename-free, codelab-style graph** you can skip those entirely and deploy
from just a property graph — the same one-artifact flow the
[codelab](../../../docs/codelabs/periodic_materialization.md) uses locally:

```bash
./examples/migration_v5/periodic_materialization/deploy_cloud_run_job.sh \
  --project your-project \
  --region us-central1 \
  --events-dataset your_events_dataset \
  --graph-dataset your_graph_dataset \
  --schedule "0 */6 * * *" \
  --property-graph path/to/property_graph.sql
```

`--property-graph` points at a `CREATE PROPERTY GRAPH` `.sql`; a placeholdered
(`${PROJECT_ID}` / `${DATASET}`) `table_ddl.sql` must sit **next to it**. The
deploy stages both (no `ontology.yaml`/`binding.yaml`), sets
`BQAA_PROPERTY_GRAPH=property_graph.sql`, and the runtime derives the ontology +
binding from the property graph + your live table schemas at run time (#286).
Events stay read-only in your events dataset; the graph tables, schema lookup,
and the state table all target the graph dataset.

Requirements / limits:

* Both files must be **placeholdered** (`${PROJECT_ID}` / `${DATASET}`) — the
  deploy refuses hardcoded references so the job can never derive against the
  wrong dataset.
* Not compatible with `--extraction-mode=compiled-only` (no reference
  extractors are staged in derived mode); the deploy rejects that combination.

**Advanced — explicit ontology + binding.** Omit `--property-graph` to keep the
default MAKO path: descriptions for AI prompting, entity inheritance, derived
properties, column renames, and the hand-authored compiled extractor. That is
the right path for migration v5 and any graph that outgrows schema-derived mode.

The script:

1. **Pre-creates the graph dataset** (`bq mk`, idempotent) so
   the runtime SA never needs `bigquery.datasets.create`.
2. **Creates two service accounts** by default (issue #182):
   `bqaa-periodic-runtime-sa@…` (runtime identity — does the
   BigQuery work) and `bqaa-periodic-scheduler-sa@…`
   (scheduler caller — fires the Cloud Run Job's HTTP
   trigger). Pass `--single-sa` to fall back to the combined
   `bqaa-periodic-sa@…` identity for both paths.
3. **Grants narrow IAM** to the runtime SA (the
   scheduler-caller SA only gets `roles/run.invoker` on the
   job in section 5 below):
   * Project-level `roles/bigquery.jobUser` —
     `bigquery.jobs.create` only.
   * Project-level `roles/aiplatform.user` — required in the
     default `--extraction-mode=ai-fallback` mode because the
     demo's extraction path calls BigQuery's `AI.GENERATE`
     function (Gemini-backed entity extraction). Without this
     grant, the AI call returns "user does not have the
     permission to access resources used by AI.GENERATE" and
     the orchestrator silently extracts an empty graph for every
     session. Surfaced by the live verification in PR #166. In
     `--extraction-mode=compiled-only` the SDK never calls
     `AI.GENERATE` (proven by `TestCompiledOnlyMakesZeroLLMCalls`),
     so the deploy script skips this grant **and**
     idempotently removes any pre-existing
     `roles/aiplatform.user` binding from the same SA, so a
     customer who flips an existing deploy from ai-fallback to
     compiled-only ends up with no Vertex AI dependency at all.
   * Dataset-level `roles/bigquery.dataViewer` on
     **events** — read-only access. The events dataset stays
     effectively read-only per the contract above.
   * Dataset-level `roles/bigquery.dataEditor` on
     **graph** — read + write on entity tables, state table,
     DDL bootstrap.
4. **Bundles the deploy** into a self-contained staging dir:
   `run_job.py`, demo artifacts, **the local SDK source**
   under `sdk_src/`. The deploy-time `requirements.txt`
   installs the SDK from `./sdk_src` (not PyPI) so the
   deployed image uses the same code as the local dry-run.
   This keeps the deploy in lockstep with any in-flight SDK
   changes the customer is iterating on locally; once the SDK
   features they depend on have all shipped to PyPI they can
   swap the vendored install for a pinned PyPI version.
5. **Deploys the Cloud Run Job** via `gcloud run jobs deploy
   --source <staging>` (Buildpacks autodetects Python) with
   `--service-account` pointing at the SA. The job's runtime
   identity is the SA, **not** the Compute Engine default
   service account — important, since the default SA may lack
   the dataset-level perms above.
6. **Grants `roles/run.invoker`** on the job to the
   scheduler-caller SA (`bqaa-periodic-scheduler-sa` by default;
   the same SA as the runtime under `--single-sa`).
7. **Creates / updates a Cloud Scheduler HTTP job** that POSTs
   to the Cloud Run Jobs `:run` endpoint with the
   scheduler-caller SA's OAuth identity.

## Inspecting results

**The JSON report (Cloud Logging).** Every run emits a
single-line JSON to stdout, picked up by Cloud Logging as a
structured entry. Filter on `resource.labels.job_name=<job>`:

```bash
gcloud logging read \
  "resource.type=cloud_run_job AND \
   resource.labels.job_name=bqaa-periodic-materialization AND \
   jsonPayload.message=\"materialization complete\"" \
  --project your-project \
  --limit 5 \
  --format='value(jsonPayload)'
```

Each entry includes:

* `run_id`, `state_key`, `window_start`, `window_end`.
* `sessions_discovered` / `sessions_materialized` /
  `sessions_failed`.
* `rows_materialized` — per-entity row counts.
* `table_statuses` — per-table cleanup/insert status. A
  `cleanup_status = "delete_failed"` entry means the BQ
  streaming buffer pinned a table within the ~90-min window —
  expected, not a code error.
* `compiled_outcomes` — C2 (compiled-extractor) telemetry.
* `failures` — list of failed sessions with error codes.
* `ok` — overall success boolean.

### Expected JSON log shape

A successful run looks like this in `jsonPayload`:

```json
{
  "severity": "INFO",
  "message": "materialization complete",
  "run_id": "2d52338e16db",
  "state_key": "3bafe7195e806340bce25b565493d24de073518d2a1c299fb668dc4f86499e5c",
  "window_start": "2026-05-15T17:40:19.542872Z",
  "window_end": "2026-05-16T04:48:45.518518Z",
  "checkpoint_read": "2026-05-15T17:55:19.542872Z",
  "checkpoint_written": "2026-05-15T17:55:19.542872Z",
  "sessions_discovered": 3,
  "sessions_materialized": 3,
  "sessions_failed": 0,
  "rows_materialized": {
    "DecisionExecution": 3,
    "Candidate": 11,
    "...": "..."
  },
  "table_statuses": {
    "project.graph_ds.decision_execution": {
      "rows_attempted": 3,
      "rows_inserted": 3,
      "cleanup_status": "deleted",
      "insert_status": "inserted",
      "idempotent": true
    }
  },
  "compiled_outcomes": {
    "compiled_unchanged": 0,
    "compiled_filtered": 0,
    "fallback_for_event": 0
  },
  "failures": [],
  "ok": true
}
```

A failed run swaps `ok: true` for `ok: false` and populates
`failures[].error_code` with either `empty_extraction` or
`materialization_failed` — see
[Failure-mode surface](#failure-mode-surface-post-167) for the
distinction.

### Cloud Monitoring alerts

Wire a single log-based alert on `jsonPayload.ok=false`. With
PR #167's classifier, this is the only signal needed —
extraction failures and insert failures both surface here.

```bash
# Create a log-based metric that counts failed runs.
gcloud logging metrics create bqaa_periodic_failed_runs \
  --project=your-project \
  --description="Periodic materialization runs that reported ok=false." \
  --log-filter='resource.type="cloud_run_job"
                AND resource.labels.job_name="bqaa-periodic-materialization"
                AND jsonPayload.message="materialization complete"
                AND jsonPayload.ok=false'

# Then alert on the metric > 0 over a 1h window (any failed run
# in the last hour fires the alert). The Cloud Monitoring UI is
# the easier place to set the threshold; gcloud equivalent uses
# the alpha command's ``--condition-filter`` + ``--if`` flags
# (the older ``--threshold-value`` / ``--threshold-comparison``
# pair was removed):
gcloud alpha monitoring policies create \
  --project=your-project \
  --notification-channels=projects/your-project/notificationChannels/CHANNEL_ID \
  --display-name="BQAA periodic materialization failed" \
  --condition-display-name="ok=false runs in the last hour" \
  --condition-filter='metric.type="logging.googleapis.com/user/bqaa_periodic_failed_runs" AND resource.type="cloud_run_job"' \
  --aggregation='{"alignmentPeriod": "3600s", "perSeriesAligner": "ALIGN_SUM"}' \
  --if='> 0' \
  --duration=60s
```

For drill-down on the failure mode, filter on the error code.
``--freshness`` is the portable way to limit the time window
(``date -u -v-1d`` is macOS-only; ``gcloud logging read`` accepts
``--freshness=1d`` directly on every supported platform):

```bash
# All AI / extraction failures in the last 24h.
gcloud logging read \
  'resource.type="cloud_run_job"
   AND jsonPayload.failures.error_code="empty_extraction"' \
  --project=your-project \
  --freshness=1d \
  --limit=50

# All schema / write-perm failures in the last 24h.
gcloud logging read \
  'resource.type="cloud_run_job"
   AND jsonPayload.failures.error_code="materialization_failed"' \
  --project=your-project \
  --freshness=1d \
  --limit=50
```

Two distinct error codes → two distinct on-call runbooks. The
`error_detail` field names the specific failing tables for
`materialization_failed`, so the on-call doesn't have to
correlate with separate logs to find what broke.

**The state table.** Co-located with the graph dataset (NOT
the events dataset — the events dataset stays read-only per
the contract above). A real BQ table at
`<project>.<graph_dataset>._bqaa_materialization_state`, one
append-only row per run. `run_job.py` passes
`state_table="{project}.{graph_dataset}._bqaa_materialization_state"`
explicitly to the orchestrator so the default-dataset fallback
can never point it at the events dataset. Query it for the
audit log:

```sql
SELECT
  run_started_at,
  scan_start,
  scan_end,
  last_completion_at AS checkpoint,
  sessions_discovered,
  sessions_materialized,
  sessions_failed,
  ok,
  error_detail
FROM `your-project.your_graph_dataset._bqaa_materialization_state`
ORDER BY run_started_at DESC
LIMIT 20;
```

The `state_key` column (sha256 of the config) lets you separate
runs from different ontology/binding/predicate combinations — a
predicate switch (e.g. swapping `--completion-event-type`) shows
up as a new key with a fresh bootstrap, not an inherited
checkpoint.

## Configuration reference

All configuration goes through env vars on the Cloud Run Job.
The deploy script wires them via `--set-env-vars`; for local
dry-run, set them in your shell.

| Env var                    | Required | Default | Notes |
|----------------------------|----------|---------|-------|
| `BQAA_PROJECT_ID`          | yes      | —       | GCP project. |
| `BQAA_EVENTS_DATASET_ID`   | yes      | —       | Dataset with `agent_events`. |
| `BQAA_GRAPH_DATASET_ID`    | yes      | —       | Target dataset for entity/relationship tables + the state table. |
| `BQAA_LOCATION`            | no       | `US`    | BigQuery location. Must match both datasets. |
| `BQAA_LOOKBACK_HOURS`      | no       | `6`     | Max history scanned per run. Hard upper bound on scan window. |
| `BQAA_OVERLAP_MINUTES`     | no       | `15`    | Re-scan window for late-arriving events. Bump (e.g. `60`) if ingestion can lag tens of minutes. |
| `BQAA_MAX_SESSIONS`        | no       | unlimited | Per-run cost guardrail. |

## Operational notes

**State-table behavior.** Append-only; never truncate it. Each
run inserts one row. The next run reads
`MAX(last_completion_at) WHERE state_key = <current_config>` as
its starting point. A heartbeat row (empty window) carries
forward the prior checkpoint so the most recent row is self-
documenting.

**Overlap-windowed re-claim.** `BQAA_OVERLAP_MINUTES` re-scans
events slightly older than the prior checkpoint. Default 15 min
is fine for low-latency ingestion; bump higher for slower
sources. The materializer is idempotent per session (delete-then-
insert keyed on `session_id`), so re-scanning is safe.

**Partial failures.** If session 3 of 5 raises during
extraction, the orchestrator stops, advances the checkpoint to
session 2's completion timestamp, writes a state row with
`ok=False`, and exits non-zero. The next scheduled run picks up
from session 2's timestamp and retries session 3 (idempotent
because session-level delete-then-insert).

**Streaming-buffer pinning.** When inserts land in the streaming
buffer (default for `insert_rows_json`), BQ pins those rows for
~30-90 min during which DML `DELETE` returns an error. The
materializer surfaces this as `cleanup_status = "delete_failed"`
in `table_statuses` — operator-visible, not silent. The session-
level delete-then-insert pattern degrades gracefully: if delete
failed, the insert still happens, producing duplicates that the
*next* successful delete cleans up.

**Idempotent retries.** Cloud Run Job retry policy: this script
sets `--max-retries 2` by default (issue #183), tunable via
the `--max-retries N` flag. The orchestrator's checkpoint +
session-level idempotency mean additional retries are safe —
each retry resumes from the last successful session, no
double-counting. For transient BigQuery slot pressure or
short-lived rate-limiting events, two retries silently
absorb the noise; the on-call doesn't get paged for a
condition the next scheduled fire would have recovered from
anyway. For sustained failure (e.g., binding drift) all
retries also fail and the scheduled fire shows up as a failed
execution in Cloud Monitoring.

The retry count surfaces in every run's startup log payload
(`jsonPayload.max_retries`) via the `BQAA_MAX_RETRIES` env
var the deploy script sets — operators correlating alert noise
with retry behaviour see it in Cloud Logging without
`gcloud run jobs describe`. Cost-minimizing deployments can
pass `--max-retries 1` (or `0` to disable); production
deployments running every few hours benefit from the
`2` default.

Set up an alert on `logging.googleapis.com/log_entry_count`
with severity `ERROR` — that fires only after all retries
have exhausted.

## Verified Cloud Run deployment evidence

This section documents an end-to-end live verification of the
deploy path against `test-project-0728-467323` (the
canonical SDK test project). The verification was the work of
PR #166 (follow-up to #165) and surfaced four real issues — all
fixed in `deploy_cloud_run_job.sh` before the evidence below
was captured. See the PR description for the full discovery
log.

**Inputs:**

* Events dataset: `migration_v5_idem_43c51d05` (3 demo
  sessions, 115 events, pre-populated by `run_agent.py` in PR
  #164).
* Graph dataset: `migration_v5_graph_verify_500c9f` (auto-
  created by deploy script).
* Job name: `bqaa-periodic-verify-500c9f`.
* Schedule: `0 */6 * * *`.
* Region: `us-central1`.

**Build + deploy:**

* Cloud Build image:
  `us-central1-docker.pkg.dev/test-project-0728-467323/cloud-run-source-deploy/bqaa-periodic-verify-500c9f@sha256:d1cd008…`.
* Built from the vendored `./sdk_src` (PR #165 contract):
  `Building bigquery-agent-analytics @ file:///workspace/sdk_src` →
  `Built bigquery-agent-analytics @ file:///workspace/sdk_src`.
* Build time: ~4 min (Cloud Build + Buildpacks).

**Cloud Scheduler trigger** (`gcloud scheduler jobs describe`):

```yaml
httpTarget:
  httpMethod: POST
  oauthToken:
    scope: https://www.googleapis.com/auth/cloud-platform
    serviceAccountEmail: bqaa-periodic-sa@test-project-0728-467323.iam.gserviceaccount.com
  uri: https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/test-project-0728-467323/jobs/bqaa-periodic-verify-500c9f:run
schedule: 0 */6 * * *
state: ENABLED
```

**This evidence was captured pre-#182**, before the split-SA
default. The OAuth identity here matches the runtime SA because
the deploy at the time used the single combined
``bqaa-periodic-sa``. New default deploys (post-#182) wire the
scheduler trigger to a separate ``bqaa-periodic-scheduler-sa``
that holds only ``roles/run.invoker`` on the job — the OAuth
identity above would now read ``bqaa-periodic-scheduler-sa@…``
instead. Pass ``--single-sa`` to restore the pre-#182 combined
identity shown here.

**IAM contract** verified post-deploy via the BigQuery client:

```
Events dataset IAM for SA:
  role=READER, entity_type=userByEmail, entity_id=bqaa-periodic-sa@…
  Number of WRITE/OWNER bindings for SA: 0

Graph dataset IAM for SA:
  role=WRITER, entity_type=userByEmail, entity_id=bqaa-periodic-sa@…
```

The SA can read events but cannot write — the "events dataset
read-only" contract holds at the IAM layer.

**Successful execution** (`materialization complete` payload
from Cloud Logging, after the post-deploy `roles/aiplatform.user`
grant the verification added to the deploy script):

```json
{
  "run_id": "2d52338e16db",
  "sessions_discovered": 3,
  "sessions_materialized": 3,
  "sessions_failed": 0,
  "rows_materialized": {
    "DecisionExecution": 3,
    "DecisionPoint": 3,
    "Candidate": 11,
    "SelectionOutcome": 3,
    "ContextSnapshot": 3,
    "evaluatesCandidate": 11,
    "selectedCandidate": 3,
    "rejectedCandidate": 5,
    "atContextSnapshot": 3,
    "executedAtDecisionPoint": 3,
    "hasSelectionOutcome": 3
  },
  "ok": true,
  "failures": []
}
```

All 11 entity/relationship tables populated.
`cleanup_status=deleted, insert_status=inserted,
idempotent=true` across the board.

**Scheduler trigger actually fires.** The cron-scheduled fire
at `2026-05-16T06:00:00Z` produced a third state-table row
(`run_id 4725ebd79060`) with the same shape — proving the
end-to-end path from Cloud Scheduler → Cloud Run Job →
materialization works without manual intervention.

**State table audit log** (`_bqaa_materialization_state` in
the graph dataset):

```
run_id          run_started_at         sessions_disc / mat / failed   ok
ff1e956df8b8    2026-05-16 04:38:59    3 / 3 / 0                       true
2d52338e16db    2026-05-16 04:48:45    3 / 3 / 0                       true
4725ebd79060    2026-05-16 06:02:51    3 / 3 / 0                       true
```

(Row 1 is the deploy script's `--smoke` execution, which ran
BEFORE the verification added `roles/aiplatform.user` to the
deploy. AI.GENERATE failed for every session there. At the time
of #166, the orchestrator reported `ok=true` with empty
`rows_materialized` — the silent-failure mode that PR #167
fixed. Today, the same situation would produce `ok=false` with
`failures[0].error_code = "empty_extraction"`.)

### Failure-mode surface (post-#167)

The orchestrator distinguishes three session-level outcomes
that look identical from `rows_materialized` alone:

* **`empty_extraction`** — extraction (AI.GENERATE or compiled
  bundle) returned an empty graph; no inserts attempted.
  Diagnose by checking the runtime SA's `roles/aiplatform.user`
  grant, AI.GENERATE quotas, or whether the session's events
  legitimately had any content the bound ontology models.

* **`materialization_failed`** — extraction produced rows but
  every insert returned an error. The `failures[].error_detail`
  names the specific tables (e.g.,
  `DecisionExecution: rows_attempted=3, insert_status='insert_failed'`),
  and the aggregate `table_statuses` carries the per-table
  diagnostic at the top level of the report. Diagnose by
  checking the SA's dataset write perm on the graph dataset,
  schema drift the binding-validate pre-flight missed, or
  streaming-buffer pinning.

* **`session_orphaned`** *(only when `--max-session-age-hours`
  is set, issue #180)* — a session's first event landed before
  the configured cutoff but the session never emitted
  `AGENT_COMPLETED`. The orphan-watchdog scan flagged it as
  in-flight-too-long. `failures[].error_detail` names the
  session_id, the cutoff timestamp, and the missing terminal
  event_type, plus a remediation hint pointing at the
  cumulative ledger row (`mode='orphan_ledger'`) in the state
  table. Diagnose by triaging the underlying agent failure or
  by emitting the missing terminal event (the watchdog drops
  the session from the ledger on its next pass once
  `AGENT_COMPLETED` appears).

In all three cases: `ok=false`, CLI exit 1, the cron run shows
up as a failed execution in Cloud Monitoring. Alert directly on
`jsonPayload.ok=false` plus `jsonPayload.failures[].error_code`
for the failure-mode breakdown — no second-line check needed.

The orphan watchdog also persists a structured audit trail in
the state table: `mode='orphan_scan'` for the per-cron-pass
delta (`flagged_session_ids` = sessions newly flagged this
scan, `orphan_watermark` = the scan's cutoff that becomes the
next scan's exclusive `>` boundary) and `mode='orphan_ledger'`
for the cumulative running set after the resolved-orphan
sweep. Operators reading the ledger see "what's currently
unresolved" without having to diff across runs.

## Cleanup and redeploy

### Redeploy (no resource churn)

Re-running the deploy script with the same flags is fully
idempotent — same service account(s)
(`bqaa-periodic-runtime-sa` + `bqaa-periodic-scheduler-sa` by
default; `bqaa-periodic-sa` under `--single-sa`), same job
name, same Scheduler trigger name, same graph dataset. The
existing IAM bindings are detected and skipped (`already
granted (READER)` etc.); only the container image gets rebuilt
to reflect any source changes. Run after any code change to
`run_job.py` or the demo artifacts.

### Tear down a deployment

Three resources to remove. Run in this order so the Scheduler
doesn't try to invoke a deleted job between the two deletes:

```bash
# 1. Stop the cron from firing.
gcloud scheduler jobs delete bqaa-periodic-materialization-cron \
  --project=your-project --location=us-central1 --quiet

# 2. Delete the Cloud Run Job.
gcloud run jobs delete bqaa-periodic-materialization \
  --project=your-project --region=us-central1 --quiet

# 3. (Optional) Drop the graph dataset — destroys ALL
# materialized entity/relationship tables AND the state-table
# audit log. Skip if you want to preserve history.
bq --project_id=your-project rm -r -f your_graph_dataset
```

The events dataset is never modified by the deploy and stays
untouched. The service accounts persist across teardowns —
drop them manually if you're permanently retiring the
deployment (omit either delete if you used `--single-sa`):

```bash
# Default (split-SA) deploys:
gcloud iam service-accounts delete \
  bqaa-periodic-runtime-sa@your-project.iam.gserviceaccount.com \
  --project=your-project --quiet
gcloud iam service-accounts delete \
  bqaa-periodic-scheduler-sa@your-project.iam.gserviceaccount.com \
  --project=your-project --quiet

# Single-SA deploys (--single-sa):
gcloud iam service-accounts delete \
  bqaa-periodic-sa@your-project.iam.gserviceaccount.com \
  --project=your-project --quiet
```

## Infrastructure-as-Code option

* **Terraform module.** [`terraform/`](./terraform/) ships an
  IaC mirror of this bash deploy (resolves #186, defaults
  match the post-#230 surface: split SAs, `max_retries = 2`,
  `extraction_mode = "ai-fallback"`). The module deploys
  *configured* infrastructure — graph dataset, both SAs, IAM
  bindings, Cloud Run Job, Scheduler trigger — and takes the
  published container image URI as input. Container image
  build is intentionally outside the module (the bash deploy's
  Cloud Buildpacks `--source` flow doesn't slot into IaC); the
  expected pipeline is CI builds → Artifact Registry push →
  `terraform apply`. See [`terraform/README.md`](./terraform/README.md)
  for the bash-vs-Terraform comparison and the quickstart.

## Not in scope here

* **Pulumi.** A Pulumi equivalent of the Terraform module would
  be a straightforward port and is open for contribution.
* **Compiled-bundle materialization with fingerprint-stable
  pre-built bundles.** The deploy script supports compiled-only
  extraction via the runtime reference extractor
  (`--extraction-mode=compiled-only`, see the IAM matrix above —
  this drops `roles/aiplatform.user` entirely). For
  fingerprint-stable *compiled bundles* (the `--bundles-root`
  path), see `docs/extractor_compilation/` and PR #152.

## Troubleshooting

**`required env var BQAA_PROJECT_ID is not set`** — the local
dry-run path. Set the three required env vars in your shell.

**`binding-validate failed before extraction`** — the schema
drift contract from #161. Your binding references columns that
don't exist in the live tables. Either fix the binding, fix the
tables, or pass `--no-validate-binding` to bypass (not
recommended in production).

**`Permission denied: bigquery.datasets.create`** — the runtime
SA lacks dataset-create permission. The deploy script grants
project-level `roles/bigquery.user` which includes this; if you
swapped in a custom SA, grant it manually or pre-create the
graph dataset (`bq mk --location=$LOCATION
$PROJECT:$GRAPH_DATASET`) and grant the SA dataEditor on it.

**`insert_failed` across every table on the first run** — the
entity tables don't exist yet. The wrapper bootstraps them via
`CREATE TABLE IF NOT EXISTS`, but if the runtime SA lacks
`bigquery.tables.create`, the bootstrap silently no-ops and
inserts fail. The deploy script grants `roles/bigquery.user` +
`roles/bigquery.dataEditor` to cover this.

**Scheduler fires but the job doesn't run** — IAM. Confirm the
scheduler's service account
(`bqaa-periodic-scheduler-sa@…` by default, or
`bqaa-periodic-sa@…` under `--single-sa`) has
`roles/run.invoker` on the job. The deploy script grants this;
if you renamed the SA or job, regrant manually.
