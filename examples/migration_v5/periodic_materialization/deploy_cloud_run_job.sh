#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Deploy the periodic-materialization Cloud Run Job + the Cloud
# Scheduler trigger that fires it.
#
# Usage:
#
#   ./deploy_cloud_run_job.sh \
#     --project PROJECT_ID \
#     --region REGION \
#     --events-dataset EVENTS_DS \
#     --graph-dataset GRAPH_DS \
#     --schedule "0 */6 * * *" \
#     [--location BQ_LOCATION] \
#     [--lookback-hours 6] \
#     [--overlap-minutes 15] \
#     [--max-sessions ""] \
#     [--job-name bqaa-periodic-materialization] \
#     [--smoke]
#
# What this script does (in order):
#
# 1. Pre-creates the **graph dataset** (idempotent ``bq mk``)
#    so the runtime service account doesn't need
#    ``bigquery.datasets.create`` — narrows the runtime IAM
#    surface to dataset-level grants.
#
# 2. Creates the runtime + scheduler-caller service accounts
#    if absent. Default: two SAs — ``bqaa-periodic-runtime-sa``
#    (holds the BigQuery + Vertex AI roles below) and
#    ``bqaa-periodic-scheduler-sa`` (holds only
#    ``roles/run.invoker`` on the job, wired in section 5).
#    Under ``--single-sa``: a single ``bqaa-periodic-sa`` that
#    serves both paths (the pre-#182 default). The runtime SA
#    is granted:
#      * project-level ``roles/bigquery.jobUser`` (jobs.create).
#      * project-level ``roles/aiplatform.user`` (the MAKO
#        demo's extraction path calls ``AI.GENERATE``, which
#        requires Vertex AI access).
#      * dataset-level ``roles/bigquery.dataViewer`` on the
#        events dataset (read-only access — events stay read-
#        only per the README contract).
#      * dataset-level ``roles/bigquery.dataEditor`` on the
#        graph dataset (read + write — entity tables, state
#        table, DDL bootstrap).
#
# 3. Builds a self-contained staging dir containing:
#      * ``run_job.py``, ``Procfile``.
#      * The demo artifacts (``ontology.yaml``, ``binding.yaml``,
#        ``table_ddl.sql``) next to ``run_job.py`` for the
#        flat-container layout.
#      * The local SDK source (``src/bigquery_agent_analytics``
#        + ``src/bigquery_ontology`` + ``pyproject.toml``)
#        inside ``sdk_src/``. The deploy-time
#        ``requirements.txt`` installs from this local path so
#        the deployed image doesn't depend on a published PyPI
#        release containing the in-flight orchestrator (#162).
#
# 4. Deploys the Cloud Run Job via ``gcloud run jobs deploy
#    --source <staging>`` with ``--service-account`` pointing
#    at the runtime SA. Buildpacks autodetects the Python
#    runtime + ``requirements.txt``. Env vars wired through
#    ``--set-env-vars``.
#
# 5. Enables the Cloud Scheduler API if it isn't already, and
#    grants the scheduler-caller SA ``roles/run.invoker`` on
#    the job so the scheduler trigger can actually invoke it.
#    Default: ``bqaa-periodic-scheduler-sa``. Under
#    ``--single-sa``: ``bqaa-periodic-sa`` (same as runtime).
#
# 6. Creates / updates the Cloud Scheduler job pointing at the
#    Cloud Run Jobs ``:run`` endpoint, authenticated as the
#    scheduler-caller SA.
#
# 7. If ``--smoke`` is passed, executes the job once via
#    ``gcloud run jobs execute --wait`` and tails the logs —
#    so "did it deploy correctly?" is one command away.

set -euo pipefail

# ----------------------------------------------------------- #
# Arg parsing                                                  #
# ----------------------------------------------------------- #

PROJECT=""
REGION=""
EVENTS_DATASET=""
GRAPH_DATASET=""
SCHEDULE=""
BQ_LOCATION="US"
LOOKBACK_HOURS="6"
OVERLAP_MINUTES="15"
MAX_SESSIONS=""
JOB_NAME="bqaa-periodic-materialization"
SMOKE=false
EXTRACTION_MODE="ai-fallback"
PROPERTY_GRAPH=""
MAX_SESSION_AGE_HOURS=""
# Production posture by default: split runtime + scheduler-caller
# SAs (issue #182). ``--single-sa`` is the escape hatch for
# customers who explicitly want the simpler combined identity.
SINGLE_SA=false
# Cloud Run Job retry policy (issue #183). The orchestrator's
# session-level idempotency + append-only state table make
# additional retries safe. Default 2 (was hard-coded 1) so
# transient BQ slot pressure / short-lived rate limits don't
# page on-call when a retry would have silently recovered.
MAX_RETRIES="2"

# Print usage. Exit code is the caller's choice: ``usage 0``
# for ``--help`` (success), ``usage 1`` for parse / required-arg
# errors. Mixing the two would make ``./deploy_cloud_run_job.sh
# --help`` look like a failure in CI / wrappers that pivot on
# exit codes.
usage() {
  cat <<EOF
Usage: $0 [options]

Required:
  --project PROJECT_ID         GCP project.
  --region REGION              Cloud Run region (e.g. us-central1).
  --events-dataset DATASET     BigQuery dataset with agent_events.
  --graph-dataset DATASET      BigQuery dataset for the graph.
  --schedule "CRON"            Cloud Scheduler cron (e.g. "0 */6 * * *").

Optional:
  --location LOCATION          BigQuery location (default: US).
  --lookback-hours N           Lookback window (default: 6).
  --overlap-minutes N          Overlap window (default: 15).
  --max-sessions N             Cap sessions per run (default: unlimited).
  --job-name NAME              Cloud Run Job name
                               (default: bqaa-periodic-materialization).
  --smoke                      After deploy, run the job once + tail logs.
  --extraction-mode MODE       'ai-fallback' (default) or 'compiled-only'.
                               'ai-fallback' runs structured extractors
                               with AI.GENERATE for any uncovered span.
                               'compiled-only' runs structured extractors
                               only, never calls AI.GENERATE, and surfaces
                               uncovered spans as typed empty_extraction
                               failures with sample diagnostics. In
                               compiled-only mode the deploy skips the
                               roles/aiplatform.user grant and idempotently
                               removes any pre-existing grant from a prior
                               ai-fallback deploy of the same SA.
  --property-graph PATH        Schema-derived mode (#286): derive the
                               ontology + binding from this CREATE PROPERTY
                               GRAPH .sql file plus the table schemas, instead
                               of staging ontology.yaml/binding.yaml. A
                               placeholdered (\${PROJECT_ID}/\${DATASET})
                               table_ddl.sql must sit next to it. Use for
                               rename-free graphs; not compatible with
                               --extraction-mode=compiled-only.
  --max-session-age-hours N    Enable the orphan-session watchdog (issue
                               #180). When set, each cron pass additionally
                               scans for sessions whose first event is
                               older than N hours but which never emitted
                               AGENT_COMPLETED. Each new orphan surfaces
                               as a typed 'session_orphaned' failure in
                               the JSON report; the state table records
                               per-scan + cumulative audit rows. Disabled
                               by default; skipped automatically in any
                               backfill run.
  --single-sa                  Use a single combined service account
                               (bqaa-periodic-sa) for both the Cloud Run
                               Job runtime identity AND the Cloud
                               Scheduler OAuth caller. Default: two SAs
                               (bqaa-periodic-runtime-sa with the BigQuery
                               + Vertex AI roles, bqaa-periodic-scheduler-sa
                               with only roles/run.invoker on the job).
                               The split is the production-posture default
                               per issue #182 — least-privilege: the
                               scheduler-caller never needs the runtime's
                               BigQuery permissions.
  --max-retries N              Cloud Run Job retry count on failure
                               (default: 2). The orchestrator's session-
                               level idempotency + append-only state
                               table make additional retries safe. The
                               value is also wired as BQAA_MAX_RETRIES
                               into the job's env so the run's Cloud
                               Logging payload surfaces it. Per issue
                               #183.
  -h | --help                  Show this help.
EOF
  exit "${1:-1}"
}

# With ``set -u``, a bare ``$2`` reference raises "unbound
# variable" — the wrong shape when a user typo like
# ``--project`` at the very end of the args trailing-edges the
# parser. ``require_arg`` reads ``$2`` defensively via the
# ``${2-}`` default-empty expansion, then either fails with a
# clean usage error or leaves the value on stdout for the
# caller's assignment. Implemented as an inline check (not via
# ``$(require_arg ...)``) so the ``exit`` inside ``usage 1``
# terminates the script — not just a subshell.
require_arg() {
  local flag="$1"
  local value="${2-}"
  if [[ -z "$value" || "$value" == -* ]]; then
    echo "Error: $flag requires a value." >&2
    usage 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)         require_arg "$1" "${2-}"; PROJECT="$2"; shift 2 ;;
    --region)          require_arg "$1" "${2-}"; REGION="$2"; shift 2 ;;
    --events-dataset)  require_arg "$1" "${2-}"; EVENTS_DATASET="$2"; shift 2 ;;
    --graph-dataset)   require_arg "$1" "${2-}"; GRAPH_DATASET="$2"; shift 2 ;;
    --schedule)        require_arg "$1" "${2-}"; SCHEDULE="$2"; shift 2 ;;
    --location)        require_arg "$1" "${2-}"; BQ_LOCATION="$2"; shift 2 ;;
    --lookback-hours)  require_arg "$1" "${2-}"; LOOKBACK_HOURS="$2"; shift 2 ;;
    --overlap-minutes) require_arg "$1" "${2-}"; OVERLAP_MINUTES="$2"; shift 2 ;;
    --max-sessions)    require_arg "$1" "${2-}"; MAX_SESSIONS="$2"; shift 2 ;;
    --job-name)        require_arg "$1" "${2-}"; JOB_NAME="$2"; shift 2 ;;
    --smoke)           SMOKE=true; shift ;;
    --extraction-mode) require_arg "$1" "${2-}"; EXTRACTION_MODE="$2"; shift 2 ;;
    --property-graph)  require_arg "$1" "${2-}"; PROPERTY_GRAPH="$2"; shift 2 ;;
    --max-session-age-hours)
                       require_arg "$1" "${2-}"; MAX_SESSION_AGE_HOURS="$2"; shift 2 ;;
    --single-sa)       SINGLE_SA=true; shift ;;
    --max-retries)     require_arg "$1" "${2-}"; MAX_RETRIES="$2"; shift 2 ;;
    -h|--help)         usage 0 ;;
    *)                 echo "Unknown argument: $1" >&2; usage 1 ;;
  esac
done

# Render ``VAR_NAME`` → ``--var-name`` for the error message.
# Using ``tr`` instead of Bash 4's ``${var,,}`` so this stays
# portable on macOS's stock Bash 3.2 — a customer-facing local
# deploy script should never trip "bad substitution".
for var in PROJECT REGION EVENTS_DATASET GRAPH_DATASET SCHEDULE; do
  if [[ -z "${!var}" ]]; then
    flag=$(printf '%s' "$var" | tr '[:upper:]_' '[:lower:]-')
    echo "Error: --$flag is required (use --help)." >&2
    exit 1
  fi
done

# Validate ``--extraction-mode`` at the boundary. The materializer
# rejects unknown values too, but failing here means an operator
# typo (e.g. ``compiled_only`` with an underscore) doesn't spend
# 3 minutes on a Cloud Build before raising.
#
# Both ``ai-fallback`` and ``compiled-only`` are accepted on this
# deploy path. The mode is threaded down via ``BQAA_EXTRACTION_MODE``
# and (in compiled-only) ``BQAA_REFERENCE_EXTRACTORS_MODULE``, and
# controls the conditional ``roles/aiplatform.user`` grant a few
# sections below.
case "$EXTRACTION_MODE" in
  ai-fallback|compiled-only) ;;
  *)
    echo "Error: --extraction-mode must be 'ai-fallback' or 'compiled-only'; got '$EXTRACTION_MODE'." >&2
    exit 1
    ;;
esac

# Spec input mode (#286). ``--property-graph`` selects schema-derived
# mode: the ontology + binding are derived from the property graph + the
# table schemas, so no ``ontology.yaml`` / ``binding.yaml`` is staged.
# Unset = the explicit migration-v5 ontology+binding (compiled-extractor)
# path. Exactly one mode is in effect.
TABLE_DDL_SRC=""
if [[ -n "$PROPERTY_GRAPH" ]]; then
  # Derived mode has no reference extractors staged, so compiled-only
  # would empty_extract at runtime — reject it at the deploy boundary.
  if [[ "$EXTRACTION_MODE" == "compiled-only" ]]; then
    echo "Error: --property-graph (schema-derived mode) does not support --extraction-mode=compiled-only: no reference extractors are staged in derived mode. Use 'ai-fallback', or deploy the explicit ontology/binding path." >&2
    exit 1
  fi
  if [[ ! -f "$PROPERTY_GRAPH" ]]; then
    echo "Error: --property-graph file not found: $PROPERTY_GRAPH" >&2
    exit 1
  fi
  # The derived path also needs the graph tables to exist before the first
  # run (it reads INFORMATION_SCHEMA), so a placeholdered table_ddl.sql must
  # sit next to the property graph.
  TABLE_DDL_SRC="$(dirname "$PROPERTY_GRAPH")/table_ddl.sql"
  if [[ ! -f "$TABLE_DDL_SRC" ]]; then
    echo "Error: schema-derived mode also needs a 'table_ddl.sql' next to the property graph (so the graph tables can be bootstrapped); not found: $TABLE_DDL_SRC" >&2
    exit 1
  fi
  # Enforce the placeholder contract (#286). Both artifacts must use
  # \${PROJECT_ID} / \${DATASET} so the runtime retargets them to the
  # customer's project + graph dataset. A hardcoded graph DDL (e.g. the
  # migration-v5 snapshot pointing at a canonical demo dataset) would derive
  # against the wrong dataset -- reject it here, not after deploy.
  for _pg_artifact in "$PROPERTY_GRAPH" "$TABLE_DDL_SRC"; do
    if ! grep -qF '${PROJECT_ID}' "$_pg_artifact" \
      || ! grep -qF '${DATASET}' "$_pg_artifact"; then
      echo "Error: schema-derived mode requires placeholdered artifacts: $_pg_artifact must contain \${PROJECT_ID} and \${DATASET} so it can be retargeted to your project/graph dataset. Hardcoded graph DDL would derive against the wrong dataset. Use placeholdered, rename-free artifacts, or deploy the explicit --ontology/--binding path." >&2
      exit 1
    fi
  done
fi

# Validate ``--max-retries`` at the boundary (issue #183). A typo
# like ``--max-retries=-1`` would otherwise be forwarded to
# ``gcloud run jobs deploy`` which rejects it with a less obvious
# error after the build. Accept any non-negative integer; gcloud
# itself caps the upper bound.
if ! [[ "$MAX_RETRIES" =~ ^[0-9]+$ ]]; then
  echo "Error: --max-retries must be a non-negative integer; got '$MAX_RETRIES'." >&2
  exit 1
fi

SCHEDULER_NAME="${JOB_NAME}-cron"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Repo root — used to locate the SDK source for vendoring.
# ``periodic_materialization/`` lives under
# ``examples/migration_v5/``, so the repo root is two dirs up.
REPO_ROOT="$(cd "${ARTIFACTS_DIR}/../.." && pwd)"

# Cleanup state — the staging dir is created later in section
# 3, the IAM-venv may be created here if needed. Single trap so
# both get removed on any exit path.
STAGING=""
IAM_VENV=""
_cleanup() {
  [[ -n "$STAGING" ]] && rm -rf "$STAGING"
  [[ -n "$IAM_VENV" ]] && rm -rf "$IAM_VENV"
}
trap _cleanup EXIT

# ----------------------------------------------------------- #
# 0. Python preflight for dataset-level IAM grants             #
# ----------------------------------------------------------- #
#
# The dataset-level IAM grants in section 2 use a Python
# heredoc against ``google.cloud.bigquery`` (legacy
# ``AccessEntry`` API). The ``bq add-iam-policy-binding``
# command is gated on project allowlisting in some projects,
# so we don't rely on it.
#
# If the operator's ``python3`` already has
# ``google-cloud-bigquery`` (e.g., they ran ``pip install -e
# .`` from the repo root for local dry-run), use it directly.
# Otherwise create a one-shot temp venv with just that
# dependency — keeps the deploy a single command from a clean
# shell. Operator only needs ``gcloud`` + ``python3`` (the
# universal baseline).
PY_CMD="python3"
if ! python3 -c "import google.cloud.bigquery" >/dev/null 2>&1; then
  echo "==> creating temp venv with google-cloud-bigquery (for dataset IAM)"
  IAM_VENV="$(mktemp -d -t bqaa-iam-venv-XXXXXXXX)"
  python3 -m venv "$IAM_VENV" >/dev/null
  "$IAM_VENV/bin/pip" install --quiet google-cloud-bigquery
  PY_CMD="$IAM_VENV/bin/python"
fi

# ----------------------------------------------------------- #
# 1. Pre-create the graph dataset (idempotent)                 #
# ----------------------------------------------------------- #
#
# Done here, not at job runtime, so the runtime SA doesn't need
# ``bigquery.datasets.create``. The operator running this script
# already has the broader perms via their own gcloud auth; the
# job's SA can then be scoped to the narrower set below.

echo "==> ensuring graph dataset exists: ${PROJECT}:${GRAPH_DATASET}"
if ! bq --project_id="$PROJECT" show --dataset \
    "${PROJECT}:${GRAPH_DATASET}" >/dev/null 2>&1; then
  bq --project_id="$PROJECT" mk \
    --dataset \
    --location="$BQ_LOCATION" \
    "${PROJECT}:${GRAPH_DATASET}"
else
  echo "==> graph dataset already exists"
fi

# ----------------------------------------------------------- #
# 2. Service accounts (runtime identity + scheduler caller)    #
# ----------------------------------------------------------- #
#
# Per issue #182, the production-posture default is **two**
# service accounts:
#
#   * ``$RUNTIME_SA_EMAIL`` — the Cloud Run Job's runtime
#     identity (``--service-account`` on ``gcloud run jobs
#     deploy``). Holds the BigQuery + (conditionally) Vertex AI
#     roles below. Does the actual BigQuery work: reads events,
#     writes entity rows, writes state-table rows.
#   * ``$SCHEDULER_SA_EMAIL`` — the Cloud Scheduler trigger's
#     OAuth identity (``--oauth-service-account-email`` on
#     ``gcloud scheduler jobs create http``). Holds
#     ``roles/run.invoker`` on the specific Cloud Run Job only.
#     No BigQuery or Vertex AI perms.
#
# The split keeps the scheduler-caller narrow: it can fire the
# job and nothing else. Combining the two paths (the old default,
# now opt-in via ``--single-sa``) gives the scheduler caller
# broader permissions than it ever exercises — least-privilege
# violation that matters in regulated industries.
#
# Grant order matters: create the SA(s) + grant BigQuery perms
# to the runtime SA BEFORE the job deploys, so the job's first
# invocation has the right identity. The job's
# ``--service-account`` arg refers to ``$RUNTIME_SA_EMAIL``.

if [[ "$SINGLE_SA" == "true" ]]; then
  # Combined-identity mode (escape hatch). Mirrors the pre-#182
  # default — one ``bqaa-periodic-sa`` that holds both the
  # BigQuery / Vertex AI roles and ``roles/run.invoker``. Keep
  # the original SA name so existing deploys re-running with
  # ``--single-sa`` keep using their existing identity.
  RUNTIME_SA_NAME="bqaa-periodic-sa"
  SCHEDULER_SA_NAME="bqaa-periodic-sa"
  RUNTIME_SA_DISPLAY="BQAA periodic-materialization runtime + scheduler"
  SCHEDULER_SA_DISPLAY="$RUNTIME_SA_DISPLAY"
else
  # Split-SA mode (default, production posture).
  RUNTIME_SA_NAME="bqaa-periodic-runtime-sa"
  SCHEDULER_SA_NAME="bqaa-periodic-scheduler-sa"
  RUNTIME_SA_DISPLAY="BQAA periodic-materialization runtime"
  SCHEDULER_SA_DISPLAY="BQAA periodic-materialization scheduler caller"
fi
RUNTIME_SA_EMAIL="${RUNTIME_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHEDULER_SA_EMAIL="${SCHEDULER_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# ``$SA_EMAIL`` is preserved as an alias for the runtime SA so
# downstream blocks (dataset-level IAM grants, deferred-IAM-remove
# remediation hints, summary echoes) keep their existing
# references. Anything that needs the scheduler caller uses
# ``$SCHEDULER_SA_EMAIL`` explicitly.
SA_NAME="$RUNTIME_SA_NAME"
SA_EMAIL="$RUNTIME_SA_EMAIL"

# Retry an IAM-binding command on the IAM-propagation race.
# ``gcloud iam service-accounts create`` returns success once
# the SA exists in one IAM replica, but
# ``gcloud projects add-iam-policy-binding`` reads from a
# different replica that can lag by several seconds. The
# observed error is ``INVALID_ARGUMENT: Service account ...
# does not exist``. Polling ``describe`` doesn't help (it hits
# the same replica that already returned success). The reliable
# fix is to retry the grant itself.
_retry_iam() {
  local attempts=0
  local max=20
  while [[ $attempts -lt $max ]]; do
    if "$@" >/dev/null 2>/tmp/_iam_err.$$; then
      rm -f /tmp/_iam_err.$$
      return 0
    fi
    if ! grep -qE "(does not exist|Service account)" /tmp/_iam_err.$$; then
      cat /tmp/_iam_err.$$ >&2
      rm -f /tmp/_iam_err.$$
      return 1
    fi
    sleep 3
    attempts=$((attempts + 1))
  done
  echo "Error: IAM grant did not succeed after ${max} retries" >&2
  cat /tmp/_iam_err.$$ >&2
  rm -f /tmp/_iam_err.$$
  return 1
}

_ensure_sa() {
  local sa_name="$1"
  local sa_email="$2"
  local display="$3"
  if ! gcloud iam service-accounts describe "$sa_email" \
      --project "$PROJECT" >/dev/null 2>&1; then
    echo "==> creating service account: $sa_email"
    gcloud iam service-accounts create "$sa_name" \
      --display-name "$display" \
      --project "$PROJECT"
  else
    echo "==> service account exists: $sa_email"
  fi
}
_ensure_sa "$RUNTIME_SA_NAME" "$RUNTIME_SA_EMAIL" "$RUNTIME_SA_DISPLAY"
if [[ "$SINGLE_SA" != "true" ]]; then
  _ensure_sa "$SCHEDULER_SA_NAME" "$SCHEDULER_SA_EMAIL" "$SCHEDULER_SA_DISPLAY"
fi

# IAM grants for the runtime SA — narrowed to dataset-level
# where possible so the events dataset stays effectively read-
# only per the README contract.
#
#   * Project-level ``roles/bigquery.jobUser``
#       → ``bigquery.jobs.create`` (run queries / DML).
#   * Dataset-level ``roles/bigquery.dataViewer`` on events
#       → read-only access to ``agent_events``.
#   * Dataset-level ``roles/bigquery.dataEditor`` on graph
#       → read + write on entity tables, state table, DDL
#         bootstrap (CREATE TABLE IF NOT EXISTS).
#
# The dataset-level grants use ``bq add-iam-policy-binding``,
# which appends to the dataset's IAM policy rather than
# replacing it. Idempotent (re-adds are no-ops).
echo "==> granting project-level roles/bigquery.jobUser to $SA_EMAIL"
_retry_iam gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/bigquery.jobUser \
  --condition=None \
  --quiet

# The MAKO demo's extraction path uses BigQuery's
# ``AI.GENERATE`` function under the hood (Gemini-backed
# entity extraction from ``agent_events`` rows). Calling
# ``AI.GENERATE`` requires Vertex AI access on top of BigQuery
# perms. Without this grant, the AI call returns "user does
# not have the permission to access resources used by
# AI.GENERATE" and the orchestrator silently extracts an empty
# graph for every session (the SDK currently swallows per-event
# AI failures and reports ``sessions_materialized`` without
# checking ``rows_materialized``). The verification round in
# #166 surfaced this — the deploy looks ``ok=true`` but the
# entity tables stay empty.
# ``roles/aiplatform.user`` is conditional on ``--extraction-mode``:
#
# * ``ai-fallback`` (default): grant the role. Without it,
#   ``AI.GENERATE`` returns "user does not have the permission
#   to access resources used by AI.GENERATE" and the
#   orchestrator silently materializes an empty graph
#   (the SDK swallows per-event AI failures and reports
#   ``sessions_materialized`` without checking
#   ``rows_materialized`` — surfaced in #166's verification round).
#
# * ``compiled-only``: skip the grant — ``TestCompiledOnlyMakesZero
#   LLMCalls`` proves the SDK never calls ``AI.GENERATE`` in this
#   mode — AND idempotently remove any pre-existing grant from a
#   prior ai-fallback deploy of the same SA. Without the remove
#   step, a customer who flips an existing deploy from
#   ai-fallback to compiled-only inherits the old role grant and
#   the "compiled-only ⇒ no Vertex AI dependency" story silently
#   breaks.
#
#   The remove is **deferred** until AFTER the new revision has
#   been successfully deployed (and, if ``--smoke`` is set, after
#   the smoke run passes). If we removed the role here and any
#   later step failed (staging, buildpacks, ``gcloud run jobs
#   deploy``, scheduler wiring), the previously-deployed
#   ai-fallback container — still running under the existing
#   schedule — would lose its required Vertex AI role mid-
#   transition. Deferring keeps the existing scheduled deploy
#   functional through every failure-of-the-new-deploy path.
if [[ "$EXTRACTION_MODE" == "ai-fallback" ]]; then
  echo "==> granting project-level roles/aiplatform.user to $SA_EMAIL"
  _retry_iam gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${SA_EMAIL}" \
    --role roles/aiplatform.user \
    --condition=None \
    --quiet
else
  echo "==> compiled-only mode: skipping roles/aiplatform.user grant on $SA_EMAIL"
  echo "==> (any pre-existing roles/aiplatform.user grant will be removed after deploy succeeds)"
fi

# Dataset-level IAM via the BigQuery Python client (the
# ``AccessEntry``-based update API — legacy and fully
# supported, vs ``bq add-iam-policy-binding`` which requires
# project allowlisting in some environments).
#
# ``$PY_CMD`` was set in the preflight section 0: either the
# operator's existing ``python3`` (if it has
# ``google-cloud-bigquery`` installed) or a temp venv (if it
# doesn't). The deploy script doesn't ask the operator to
# install anything beyond ``gcloud`` + ``python3``.
#
# The grant logic is idempotent — it skips the update if the
# binding is already present, so re-running the deploy is safe.
_grant_dataset_iam() {
  local dataset="$1"
  local role="$2"  # legacy role keyword: READER / WRITER / OWNER
  local role_display="$3"  # for the echo message
  echo "==> granting dataset-level ${role_display} on ${dataset} to $SA_EMAIL"
  "$PY_CMD" - <<EOF || { echo "Error: dataset-level IAM grant failed." >&2; exit 1; }
import sys
from google.cloud import bigquery
client = bigquery.Client(project="${PROJECT}")
ds = client.get_dataset("${PROJECT}.${dataset}")
sa = "${SA_EMAIL}"
role = "${role}"
existing = [
    e for e in ds.access_entries
    if e.entity_type == "userByEmail"
    and e.entity_id == sa
    and e.role == role
]
if existing:
    print(f"  already granted ({role})")
    sys.exit(0)
entries = list(ds.access_entries) + [
    bigquery.AccessEntry(
        role=role, entity_type="userByEmail", entity_id=sa
    )
]
ds.access_entries = entries
client.update_dataset(ds, ["access_entries"])
print(f"  granted ({role})")
EOF
}

# READER ≡ roles/bigquery.dataViewer.
_grant_dataset_iam "$EVENTS_DATASET" "READER" "roles/bigquery.dataViewer"
# WRITER ≡ roles/bigquery.dataEditor.
_grant_dataset_iam "$GRAPH_DATASET" "WRITER" "roles/bigquery.dataEditor"

# ----------------------------------------------------------- #
# 3. Build self-contained staging dir                          #
# ----------------------------------------------------------- #
#
# The staging dir vendors:
#   * ``run_job.py``, ``Procfile``.
#   * Demo artifacts (``ontology.yaml``, ``binding.yaml``,
#     ``table_ddl.sql``) next to ``run_job.py``.
#   * The local SDK source under ``sdk_src/``
#     (``src/bigquery_agent_analytics`` +
#     ``src/bigquery_ontology`` + ``pyproject.toml``).
#   * A deploy-time ``requirements.txt`` that installs the SDK
#     from ``./sdk_src`` (NOT from PyPI), so the deployed image
#     uses the same SDK code the local dry-run uses. The
#     committed ``requirements.txt`` only lists the local-dry-
#     run ancillary deps (google-cloud-bigquery + pyyaml); the
#     deploy generates its own requirements next to it in the
#     staging dir.

# ``STAGING`` is declared empty at the top of the script and
# removed via the unified ``_cleanup`` trap, so we just assign
# the mktemp result here — no second trap.
STAGING="$(mktemp -d -t bqaa-cloud-run-job-XXXXXXXX)"

echo "==> staging at $STAGING"
cp "${SCRIPT_DIR}/run_job.py" "$STAGING/"
if [[ -n "$PROPERTY_GRAPH" ]]; then
  # Schema-derived mode: stage only the property graph + its companion
  # table DDL. The runtime derives ontology + binding from them, so no
  # ontology.yaml / binding.yaml / reference_extractor.py is needed.
  cp "$PROPERTY_GRAPH" "$STAGING/property_graph.sql"
  cp "$TABLE_DDL_SRC" "$STAGING/table_ddl.sql"
else
  # Explicit ontology + binding (migration-v5 / compiled-extractor path).
  cp "${ARTIFACTS_DIR}/ontology.yaml" "$STAGING/"
  cp "${ARTIFACTS_DIR}/binding.yaml" "$STAGING/"
  cp "${ARTIFACTS_DIR}/table_ddl.sql" "$STAGING/"
  # Stage the reference extractor module next to ``run_job.py`` so
  # Python can import it via ``BQAA_REFERENCE_EXTRACTORS_MODULE=
  # reference_extractor`` (Buildpacks sets the container working
  # directory to the staging dir's contents, which puts the
  # extractor on ``sys.path``). Shipped in both modes so an
  # operator who flips an existing deploy from ai-fallback to
  # compiled-only doesn't need to also re-stage extractor code.
  cp "${ARTIFACTS_DIR}/reference_extractor.py" "$STAGING/"
fi

# Vendor the local SDK source.
mkdir -p "$STAGING/sdk_src/src"
cp -r "$REPO_ROOT/src/bigquery_agent_analytics" "$STAGING/sdk_src/src/"
cp -r "$REPO_ROOT/src/bigquery_ontology" "$STAGING/sdk_src/src/"
cp "$REPO_ROOT/pyproject.toml" "$STAGING/sdk_src/"
# README.md is referenced by the SDK's ``pyproject.toml``
# (``readme = "README.md"``); ship a stub to keep hatch happy.
echo "# bigquery-agent-analytics (vendored for periodic-materialization deploy)" \
  > "$STAGING/sdk_src/README.md"

# Deploy-time requirements: install SDK from the vendored
# source, plus the wrapper's ancillary deps. Overrides the
# committed file in the staging dir.
cat > "$STAGING/requirements.txt" <<'EOF'
# Auto-generated by deploy_cloud_run_job.sh. Installs the SDK
# from the vendored source bundled into the staging dir, so the
# deployed image uses the same SDK code as the local dry-run.
./sdk_src
google-cloud-bigquery>=3.0.0
pyyaml>=6.0
EOF

# Procfile with a ``web:`` entry — required by Buildpacks at
# build time, and used at runtime as the container's
# entrypoint (Buildpacks wraps it in a venv-activation
# script). Without a Procfile, Buildpacks fails with
# ``provide a main.py or app.py file or set an entrypoint``;
# with a non-``web`` Procfile, it fails with ``web process
# not found in Procfile, found processes: [job]``.
#
# The ``web:`` label is Buildpacks' default process name; it
# does NOT imply the container serves HTTP. Cloud Run Jobs
# just execute the container's default entrypoint, regardless
# of the Procfile label, and this Procfile's entrypoint is
# what actually runs at job execution time.
cat > "$STAGING/Procfile" <<'EOF'
web: python run_job.py
EOF

# ----------------------------------------------------------- #
# 4. Deploy the Cloud Run Job                                  #
# ----------------------------------------------------------- #

echo "==> deploying Cloud Run Job: $JOB_NAME"

ENV_VARS=(
  "BQAA_PROJECT_ID=${PROJECT}"
  "BQAA_EVENTS_DATASET_ID=${EVENTS_DATASET}"
  "BQAA_GRAPH_DATASET_ID=${GRAPH_DATASET}"
  "BQAA_LOCATION=${BQ_LOCATION}"
  "BQAA_LOOKBACK_HOURS=${LOOKBACK_HOURS}"
  "BQAA_OVERLAP_MINUTES=${OVERLAP_MINUTES}"
  "BQAA_EXTRACTION_MODE=${EXTRACTION_MODE}"
)
if [[ -n "${MAX_SESSIONS}" ]]; then
  ENV_VARS+=("BQAA_MAX_SESSIONS=${MAX_SESSIONS}")
fi
# Compiled-only mode reads structured extractors out of the
# staged ``reference_extractor.py`` module. Without this env
# var, ``_build_manager`` would construct a manager with an
# empty extractor registry and ``extract_graph(...,
# on_unhandled_span='fail')`` would mark every session
# ``empty_extraction`` — defeating the point of compiled-only.
# ``BQAA_BUNDLES_ROOT`` stays unset on this default deploy
# path; operators who pre-compile fingerprint-stable bundles
# set it themselves on a custom image.
if [[ "$EXTRACTION_MODE" == "compiled-only" ]]; then
  ENV_VARS+=("BQAA_REFERENCE_EXTRACTORS_MODULE=reference_extractor")
fi
# Schema-derived mode: tell the runtime to derive the spec from the staged
# property graph instead of the explicit ontology/binding pair (#286).
if [[ -n "$PROPERTY_GRAPH" ]]; then
  ENV_VARS+=("BQAA_PROPERTY_GRAPH=property_graph.sql")
fi
# Orphan-session watchdog (issue #180). Only wired when the
# operator explicitly opts in via ``--max-session-age-hours``;
# default deploys ship without the watchdog so they don't pay
# the extra discovery query.
if [[ -n "${MAX_SESSION_AGE_HOURS}" ]]; then
  ENV_VARS+=("BQAA_MAX_SESSION_AGE_HOURS=${MAX_SESSION_AGE_HOURS}")
fi
# Surface the Cloud Run Job's retry count to the runtime so it
# can echo it in the structured startup log — operators
# correlating alert noise with retry behaviour see it in Cloud
# Logging without having to ``gcloud run jobs describe`` per
# alert (issue #183).
ENV_VARS+=("BQAA_MAX_RETRIES=${MAX_RETRIES}")
# Comma-join for --set-env-vars (no shell-quoting issues since
# all values are simple identifiers / numbers).
ENV_VAR_FLAG="$(IFS=','; echo "${ENV_VARS[*]}")"

# NOTE: no ``--command`` / ``--args``. Buildpacks-baked
# containers wrap the entrypoint in a script that activates the
# Python venv (where ``./sdk_src`` is installed). Overriding
# with ``--command python --args run_job.py`` skips that
# wrapper — the container then exec's a bare ``python`` that
# isn't on PATH or can't find the venv packages, and Cloud Run
# reports "Application failed to start: container exited
# abnormally" with no Python output. Letting Buildpacks use the
# Procfile's ``web: python run_job.py`` entrypoint preserves
# the venv activation.
gcloud run jobs deploy "$JOB_NAME" \
  --project "$PROJECT" \
  --region "$REGION" \
  --source "$STAGING" \
  --service-account "$RUNTIME_SA_EMAIL" \
  --set-env-vars "$ENV_VAR_FLAG" \
  --task-timeout 30m \
  --max-retries "$MAX_RETRIES"

# ----------------------------------------------------------- #
# 5. Enable Cloud Scheduler API + grant invoker on the job     #
# ----------------------------------------------------------- #

echo "==> ensuring Cloud Scheduler API is enabled"
gcloud services enable cloudscheduler.googleapis.com \
  --project "$PROJECT" \
  --quiet

# Grant ``roles/run.invoker`` on the specific Cloud Run Job so
# the scheduler trigger can fire it. In split-SA mode this is
# the ONLY role the scheduler-caller SA holds — least-privilege.
# In ``--single-sa`` mode, ``$SCHEDULER_SA_EMAIL == $RUNTIME_SA_EMAIL``
# so the grant lands on the same SA the runtime uses.
#
# Wrapped in ``_retry_iam`` because the same IAM-propagation race
# the project-level grants above defend against also bites here
# on a fresh split-SA deploy: ``bqaa-periodic-scheduler-sa`` was
# created a few seconds earlier in section 2, but
# ``gcloud run jobs add-iam-policy-binding`` can read from a
# different IAM replica that hasn't seen the new SA yet and
# returns ``INVALID_ARGUMENT: Service account ... does not exist``.
# Before this wrapper landed, fresh split-SA deploys could fail
# at this step after the Cloud Run Job was already deployed but
# before the scheduler trigger was wired — leaving the deploy
# half-done with no scheduler invoker grant.
echo "==> granting roles/run.invoker on $JOB_NAME to $SCHEDULER_SA_EMAIL"
_retry_iam gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
  --project "$PROJECT" \
  --region "$REGION" \
  --member "serviceAccount:${SCHEDULER_SA_EMAIL}" \
  --role roles/run.invoker \
  --quiet

# ----------------------------------------------------------- #
# 6. Create / update the Cloud Scheduler trigger               #
# ----------------------------------------------------------- #

JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB_NAME}:run"

if gcloud scheduler jobs describe "$SCHEDULER_NAME" \
    --project "$PROJECT" \
    --location "$REGION" >/dev/null 2>&1; then
  echo "==> updating Cloud Scheduler job: $SCHEDULER_NAME"
  gcloud scheduler jobs update http "$SCHEDULER_NAME" \
    --project "$PROJECT" \
    --location "$REGION" \
    --schedule "$SCHEDULE" \
    --uri "$JOB_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA_EMAIL"
else
  echo "==> creating Cloud Scheduler job: $SCHEDULER_NAME"
  gcloud scheduler jobs create http "$SCHEDULER_NAME" \
    --project "$PROJECT" \
    --location "$REGION" \
    --schedule "$SCHEDULE" \
    --uri "$JOB_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA_EMAIL"
fi

echo
echo "Cloud Run Job:       projects/${PROJECT}/locations/${REGION}/jobs/${JOB_NAME}"
echo "Cloud Scheduler:     projects/${PROJECT}/locations/${REGION}/jobs/${SCHEDULER_NAME}"
echo "Schedule:            ${SCHEDULE}"
echo "Max retries:         ${MAX_RETRIES}"
if [[ "$SINGLE_SA" == "true" ]]; then
  echo "Service account:     ${RUNTIME_SA_EMAIL} (single-sa mode)"
else
  echo "Runtime SA:          ${RUNTIME_SA_EMAIL}"
  echo "Scheduler-caller SA: ${SCHEDULER_SA_EMAIL}"
fi

# ----------------------------------------------------------- #
# 7. Optional smoke run                                        #
# ----------------------------------------------------------- #

if [[ "$SMOKE" == true ]]; then
  echo
  echo "==> running smoke execution (--smoke)"
  # Capture the exit status so a failed smoke skips the deferred
  # ``compiled-only`` IAM remove below: if the new revision can't
  # even complete one execution, the existing schedule on the
  # previous container is the safer fallback, and that fallback
  # still needs ``roles/aiplatform.user``.
  set +e
  EXECUTION_NAME="$(
    gcloud run jobs execute "$JOB_NAME" \
      --project "$PROJECT" \
      --region "$REGION" \
      --wait \
      --format='value(metadata.name)'
  )"
  SMOKE_RC=$?
  set -e
  echo "==> execution: $EXECUTION_NAME"
  echo "==> tailing logs (last 50 lines):"
  gcloud logging read \
    "resource.type=cloud_run_job \
     AND resource.labels.job_name=${JOB_NAME} \
     AND labels.\"run.googleapis.com/execution_name\"=${EXECUTION_NAME}" \
    --project "$PROJECT" \
    --limit 50 \
    --format='value(textPayload,jsonPayload)' \
    || true
  if [[ $SMOKE_RC -ne 0 ]]; then
    echo "Error: smoke execution failed (exit ${SMOKE_RC}); leaving existing IAM unchanged." >&2
    exit "$SMOKE_RC"
  fi
fi

# ----------------------------------------------------------- #
# 8. Compiled-only: deferred IAM remove                        #
# ----------------------------------------------------------- #
#
# Runs ONLY after every earlier step succeeded — the new
# revision is deployed, scheduler is wired, and (if ``--smoke``)
# the new revision proved it can complete an execution. Any
# failure before this point exited non-zero (``set -e`` at the
# top of the script + the explicit ``exit "$SMOKE_RC"`` above),
# so the existing ai-fallback deploy keeps its
# ``roles/aiplatform.user`` and its schedule keeps working
# during a botched transition.
#
# ``remove-iam-policy-binding`` is naturally non-idempotent:
# gcloud returns non-zero with "Policy binding not found" if the
# binding doesn't exist. We treat *that specific error* as
# success (the common first-deploy case where the SA was never
# granted the role), but surface every other failure
# (``PERMISSION_DENIED``, org-policy reject, transient gcloud
# errors, malformed condition) so the script's "Done." doesn't
# claim victory while the role silently stays attached. The
# previous ``2>/dev/null || true`` swallowed all of those.
if [[ "$EXTRACTION_MODE" == "compiled-only" ]]; then
  echo
  echo "==> compiled-only deploy succeeded; idempotently removing any"
  echo "    pre-existing roles/aiplatform.user grant from $SA_EMAIL"
  REMOVE_STDERR="$(mktemp -t bqaa-iam-remove-stderr-XXXXXXXX)"
  set +e
  gcloud projects remove-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${SA_EMAIL}" \
    --role roles/aiplatform.user \
    --condition=None \
    --quiet 2>"$REMOVE_STDERR"
  REMOVE_RC=$?
  set -e
  if [[ $REMOVE_RC -ne 0 ]]; then
    # gcloud's "binding doesn't exist" error wording is stable
    # across the current release channels: "Policy binding not
    # found" (most cases). We match that exact substring so a
    # future gcloud wording change becomes a loud failure, not a
    # silent regression. Other failures (permission, network,
    # org policy) carry different wording and fall through to
    # the propagate-and-exit branch.
    if grep -q "Policy binding not found" "$REMOVE_STDERR"; then
      echo "    (no pre-existing roles/aiplatform.user grant — nothing to remove)"
    else
      echo "Error: failed to remove roles/aiplatform.user from $SA_EMAIL" >&2
      echo "  gcloud exit code: $REMOVE_RC" >&2
      echo "  gcloud stderr:" >&2
      sed 's/^/    /' "$REMOVE_STDERR" >&2
      echo "" >&2
      echo "The new compiled-only revision is deployed and scheduled," >&2
      echo "but $SA_EMAIL may still hold roles/aiplatform.user, which" >&2
      echo "contradicts the 'no Vertex AI IAM' guarantee for" >&2
      echo "--extraction-mode=compiled-only. Resolve the cause above," >&2
      echo "then re-run this script or remove the role manually:" >&2
      echo "  gcloud projects remove-iam-policy-binding $PROJECT \\" >&2
      echo "    --member 'serviceAccount:$SA_EMAIL' \\" >&2
      echo "    --role roles/aiplatform.user --condition=None" >&2
      rm -f "$REMOVE_STDERR"
      exit "$REMOVE_RC"
    fi
  fi
  rm -f "$REMOVE_STDERR"
fi

echo
echo "Done."
