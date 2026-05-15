# Periodic materialization — Cloud Run Job + Cloud Scheduler

Run `bqaa-materialize-window` on a cron, against your own
BigQuery project, with one local command and one deploy command.

The MAKO demo (`examples/migration_v5/`) ships the ontology,
binding, and entity-table DDL. This directory wraps them in a
hands-off scheduled deployment: a Cloud Run Job that fires every
N hours via Cloud Scheduler, materializes the last N hours of
events into your graph dataset, and emits a structured JSON
report to Cloud Logging.

## Production shape

```
agent_events            bqaa-materialize-window         graph entity/
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

* GCP project with the BigQuery, Cloud Run, Cloud Scheduler, and
  Cloud Build APIs enabled.
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

## Local dry-run

Run the job once on your laptop against a real BigQuery project —
no Cloud Run required. Useful for shaking out the env-var setup
before paying for a deploy:

```bash
# From the repo root, install the SDK in editable mode. The
# example uses bigquery_agent_analytics.materialize_window
# (added in PR #162); this isn't in the 0.3.0 PyPI release
# yet, so install from local until 0.4.0 ships.
pip install -e .

# Then install the example's ancillary deps:
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

The script:

1. **Pre-creates the graph dataset** (`bq mk`, idempotent) so
   the runtime SA never needs `bigquery.datasets.create`.
2. **Creates a service account** (`bqaa-periodic-sa@…`) if
   absent. This SA serves two roles: **runtime identity** for
   the Cloud Run Job (does the BigQuery work) and **scheduler
   caller** for the Cloud Scheduler HTTP trigger. For
   production, splitting these into separate SAs is reasonable
   hardening; the script's structure makes that a small edit.
3. **Grants narrow IAM** to the SA:
   * Project-level `roles/bigquery.jobUser` —
     `bigquery.jobs.create` only.
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
   This avoids depending on a PyPI release that may not yet
   contain `materialize_window` (added in PR #162).
5. **Deploys the Cloud Run Job** via `gcloud run jobs deploy
   --source <staging>` (Buildpacks autodetects Python) with
   `--service-account` pointing at the SA. The job's runtime
   identity is the SA, **not** the Compute Engine default
   service account — important, since the default SA may lack
   the dataset-level perms above.
6. **Grants `roles/run.invoker`** on the job to the same SA
   (the scheduler-caller side of the cross-product).
7. **Creates / updates a Cloud Scheduler HTTP job** that POSTs
   to the Cloud Run Jobs `:run` endpoint with the SA's OAuth
   identity.

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
sets `--max-retries 1`. If a transient BQ error fails a run,
Cloud Run retries once; the orchestrator's checkpoint plus
session-level idempotency ensure no double-counting. For
sustained failure (e.g., binding drift), the second retry will
also fail and the scheduled fire will be reported as failed in
Cloud Monitoring. Set up an alert on
`logging.googleapis.com/log_entry_count` with severity `ERROR`.

## Not in scope here

* **Terraform / Pulumi.** A scripted deploy is easier to read
  and easier to copy than IaC. IaC can come once the command
  shape stabilizes.
* **Compiled-bundle materialization.** This example uses the
  plain `from_ontology_binding` extraction path (Gemini-backed).
  For compiled extractors (`--bundles-root`), see
  `docs/extractor_compilation/` and PR #152.
* **Backfill mode.** A separate `--backfill --from / --to`
  CLI mode is on the roadmap (per #161); for now, run the
  CLI manually with a wider `--lookback-hours` to catch up.

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
scheduler's service account (`bqaa-periodic-sa@…`) has
`roles/run.invoker` on the job. The deploy script grants this;
if you renamed the SA or job, regrant manually.
