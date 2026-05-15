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
# 2. Creates the runtime + scheduler service account
#    (``bqaa-periodic-sa``) if absent, and grants:
#      * project-level ``roles/bigquery.jobUser`` (jobs.create).
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
#    grants the same SA ``roles/run.invoker`` on the job so
#    the scheduler trigger can actually invoke it.
#
# 6. Creates / updates the Cloud Scheduler job pointing at the
#    Cloud Run Jobs ``:run`` endpoint, authenticated as the SA.
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

SCHEDULER_NAME="${JOB_NAME}-cron"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Repo root — used to locate the SDK source for vendoring.
# ``periodic_materialization/`` lives under
# ``examples/migration_v5/``, so the repo root is two dirs up.
REPO_ROOT="$(cd "${ARTIFACTS_DIR}/../.." && pwd)"

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
# 2. Service account (runtime identity + scheduler caller)     #
# ----------------------------------------------------------- #
#
# A single service account is used for both:
#   * The Cloud Run Job runtime (``--service-account`` below).
#     This SA does the actual BigQuery work — reads events,
#     writes entity rows, writes state-table rows.
#   * The Cloud Scheduler caller (OAuth identity on the HTTP
#     trigger). The SA also needs ``roles/run.invoker`` on the
#     job to invoke itself.
#
# Combining the two identities keeps the IAM story simple. For
# production, splitting them (separate SA for scheduler vs job
# runtime) is reasonable hardening; the script is structured so
# swapping in two SAs is a small edit.
#
# Grant order matters: create the SA + grant BigQuery perms
# BEFORE the job deploys, so the job's first invocation has the
# right identity. The job's ``--service-account`` arg refers to
# the SA we just set up.

SA_NAME="bqaa-periodic-sa"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "$SA_EMAIL" \
    --project "$PROJECT" >/dev/null 2>&1; then
  echo "==> creating service account: $SA_EMAIL"
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name "BQAA periodic-materialization runtime + scheduler" \
    --project "$PROJECT"
else
  echo "==> service account exists: $SA_EMAIL"
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
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/bigquery.jobUser \
  --condition=None \
  --quiet >/dev/null

# ``bq add-iam-policy-binding`` defaults to table-shaped
# resource identifiers; without ``--dataset`` it parses
# ``PROJECT:DATASET`` as a malformed table ref and fails. The
# flag is required for dataset-level grants.
echo "==> granting dataset-level roles/bigquery.dataViewer on ${EVENTS_DATASET} to $SA_EMAIL"
bq --project_id="$PROJECT" add-iam-policy-binding \
  --dataset \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataViewer" \
  "${PROJECT}:${EVENTS_DATASET}" >/dev/null

echo "==> granting dataset-level roles/bigquery.dataEditor on ${GRAPH_DATASET} to $SA_EMAIL"
bq --project_id="$PROJECT" add-iam-policy-binding \
  --dataset \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataEditor" \
  "${PROJECT}:${GRAPH_DATASET}" >/dev/null

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

STAGING="$(mktemp -d -t bqaa-cloud-run-job-XXXXXXXX)"
trap 'rm -rf "$STAGING"' EXIT

echo "==> staging at $STAGING"
cp "${SCRIPT_DIR}/run_job.py" "$STAGING/"
cp "${ARTIFACTS_DIR}/ontology.yaml" "$STAGING/"
cp "${ARTIFACTS_DIR}/binding.yaml" "$STAGING/"
cp "${ARTIFACTS_DIR}/table_ddl.sql" "$STAGING/"

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

# Procfile tells Buildpacks how to invoke the entrypoint.
# Cloud Run Jobs ignore ``web:``; we use a custom ``--command``
# below, but a Procfile keeps the staging dir self-documenting.
cat > "$STAGING/Procfile" <<'EOF'
job: python run_job.py
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
)
if [[ -n "${MAX_SESSIONS}" ]]; then
  ENV_VARS+=("BQAA_MAX_SESSIONS=${MAX_SESSIONS}")
fi
# Comma-join for --set-env-vars (no shell-quoting issues since
# all values are simple identifiers / numbers).
ENV_VAR_FLAG="$(IFS=','; echo "${ENV_VARS[*]}")"

gcloud run jobs deploy "$JOB_NAME" \
  --project "$PROJECT" \
  --region "$REGION" \
  --source "$STAGING" \
  --command python \
  --args run_job.py \
  --service-account "$SA_EMAIL" \
  --set-env-vars "$ENV_VAR_FLAG" \
  --task-timeout 30m \
  --max-retries 1

# ----------------------------------------------------------- #
# 5. Enable Cloud Scheduler API + grant invoker on the job     #
# ----------------------------------------------------------- #

echo "==> ensuring Cloud Scheduler API is enabled"
gcloud services enable cloudscheduler.googleapis.com \
  --project "$PROJECT" \
  --quiet

# Grant the SA invoker on the specific Cloud Run Job so the
# scheduler trigger can fire it. (The SA is both the runtime
# identity AND the scheduler caller; ``roles/run.invoker`` on
# the job is the cross-product permission for the scheduler
# side.)
echo "==> granting roles/run.invoker on $JOB_NAME to $SA_EMAIL"
gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
  --project "$PROJECT" \
  --region "$REGION" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/run.invoker \
  --quiet >/dev/null

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
    --oauth-service-account-email "$SA_EMAIL"
else
  echo "==> creating Cloud Scheduler job: $SCHEDULER_NAME"
  gcloud scheduler jobs create http "$SCHEDULER_NAME" \
    --project "$PROJECT" \
    --location "$REGION" \
    --schedule "$SCHEDULE" \
    --uri "$JOB_URI" \
    --http-method POST \
    --oauth-service-account-email "$SA_EMAIL"
fi

echo
echo "Cloud Run Job:       projects/${PROJECT}/locations/${REGION}/jobs/${JOB_NAME}"
echo "Cloud Scheduler:     projects/${PROJECT}/locations/${REGION}/jobs/${SCHEDULER_NAME}"
echo "Schedule:            ${SCHEDULE}"
echo "Service account:     ${SA_EMAIL}"

# ----------------------------------------------------------- #
# 7. Optional smoke run                                        #
# ----------------------------------------------------------- #

if [[ "$SMOKE" == true ]]; then
  echo
  echo "==> running smoke execution (--smoke)"
  EXECUTION_NAME="$(
    gcloud run jobs execute "$JOB_NAME" \
      --project "$PROJECT" \
      --region "$REGION" \
      --wait \
      --format='value(metadata.name)'
  )"
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
fi

echo
echo "Done."
