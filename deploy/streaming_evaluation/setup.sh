#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

MODE="${1:-}"
PROJECT="${2:-${PROJECT_ID:-}}"
DATASET="${3:-${DATASET_ID:-}}"
SOURCE_TABLE="${4:-${SOURCE_TABLE:-agent_events}}"
RUN_REGION="${5:-${RUN_REGION:-us-central1}}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.streaming_evaluation_state.json}"

SERVICE_NAME="${SERVICE_NAME:-bq-agent-streaming-eval}"
SCHEDULER_JOB_NAME="${SCHEDULER_JOB_NAME:-bq-agent-streaming-eval-5m}"
SCHEDULER_SA_NAME="${SCHEDULER_SA_NAME:-bq-agent-stream-eval-sa}"
RESULT_TABLE="${RESULT_TABLE:-streaming_evaluation_results}"
STATE_TABLE="${STATE_TABLE:-_streaming_eval_state}"
RUNS_TABLE="${RUNS_TABLE:-_streaming_eval_runs}"
POLL_SCHEDULE="${POLL_SCHEDULE:-*/5 * * * *}"
SCHEDULER_TIME_ZONE="${SCHEDULER_TIME_ZONE:-Etc/UTC}"
OVERLAP_MINUTES="${OVERLAP_MINUTES:-15}"
INITIAL_LOOKBACK_MINUTES="${INITIAL_LOOKBACK_MINUTES:-30}"

usage() {
  cat <<EOF
Usage:
  ./setup.sh up PROJECT DATASET [SOURCE_TABLE] [RUN_REGION]
  ./setup.sh down

Required for "up":
  PROJECT or PROJECT_ID
  DATASET or DATASET_ID

Defaults for optional "up" arguments:
  SOURCE_TABLE=${SOURCE_TABLE}
  RUN_REGION=${RUN_REGION}

Environment overrides:
  PROJECT_ID, DATASET_ID, SOURCE_TABLE, RUN_REGION
  SERVICE_NAME, SCHEDULER_JOB_NAME, SCHEDULER_SA_NAME
  RESULT_TABLE, STATE_TABLE, RUNS_TABLE
  POLL_SCHEDULE, SCHEDULER_TIME_ZONE
  OVERLAP_MINUTES, INITIAL_LOOKBACK_MINUTES
EOF
}

require_up_inputs() {
  if [[ -z "$PROJECT" || -z "$DATASET" ]]; then
    cat >&2 <<EOF
PROJECT and DATASET are required for "up".

Examples:
  ./setup.sh up my-project agent_trace agent_events us-central1
  PROJECT_ID=my-project DATASET_ID=agent_trace ./setup.sh up
EOF
    exit 1
  fi
}

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    exit 1
  fi
}

enable_service_if_needed() {
  local service="$1"
  gcloud services enable "$service" \
    --project="$PROJECT" \
    --quiet >/dev/null
}

normalized_scheduler_service_account_name() {
  local raw="$1"
  local normalized
  normalized="$(
    printf '%s' "$raw" \
      | tr '[:upper:]' '[:lower:]' \
      | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
  )"

  if [[ -z "$normalized" ]]; then
    normalized="schedsa"
  fi
  if [[ ! "$normalized" =~ ^[a-z] ]]; then
    normalized="a${normalized}"
  fi
  normalized="${normalized:0:30}"
  normalized="${normalized%-}"
  while [[ ${#normalized} -lt 6 ]]; do
    normalized="${normalized}0"
  done
  echo "$normalized"
}

infer_bq_location() {
  bq show --format=json "${PROJECT}:${DATASET}" | jq -r '.location'
}

create_results_table() {
  local bq_location="$1"
  bq query \
    --project_id="$PROJECT" \
    --location="$bq_location" \
    --use_legacy_sql=false \
    "$(cat <<SQL
CREATE TABLE IF NOT EXISTS \`${PROJECT}.${DATASET}.${RESULT_TABLE}\` (
  dedupe_key STRING NOT NULL,
  session_id STRING NOT NULL,
  trace_id STRING,
  span_id STRING,
  trigger_kind STRING NOT NULL,
  trigger_event_type STRING NOT NULL,
  trigger_timestamp TIMESTAMP NOT NULL,
  is_final BOOL NOT NULL,
  evaluator_profile STRING NOT NULL,
  passed BOOL NOT NULL,
  aggregate_scores_json STRING NOT NULL,
  details_json STRING NOT NULL,
  report_json STRING NOT NULL,
  processed_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(processed_at)
CLUSTER BY trigger_kind, session_id;
SQL
)"
}

create_state_table() {
  local bq_location="$1"
  bq query \
    --project_id="$PROJECT" \
    --location="$bq_location" \
    --use_legacy_sql=false \
    "$(cat <<SQL
CREATE TABLE IF NOT EXISTS \`${PROJECT}.${DATASET}.${STATE_TABLE}\` (
  processor_name STRING NOT NULL,
  checkpoint_timestamp TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
CLUSTER BY processor_name;
SQL
)"
}

create_runs_table() {
  local bq_location="$1"
  bq query \
    --project_id="$PROJECT" \
    --location="$bq_location" \
    --use_legacy_sql=false \
    "$(cat <<SQL
CREATE TABLE IF NOT EXISTS \`${PROJECT}.${DATASET}.${RUNS_TABLE}\` (
  processor_name STRING NOT NULL,
  run_started_at TIMESTAMP NOT NULL,
  run_finished_at TIMESTAMP NOT NULL,
  scan_start TIMESTAMP NOT NULL,
  scan_end TIMESTAMP NOT NULL,
  trigger_rows_found INT64 NOT NULL,
  processed_rows INT64 NOT NULL,
  duplicate_rows INT64 NOT NULL,
  ignored_rows INT64 NOT NULL,
  status STRING NOT NULL,
  error_message STRING
)
PARTITION BY DATE(run_started_at)
CLUSTER BY status, processor_name;
SQL
)"
}

ensure_scheduler_service_account() {
  local account_id
  account_id="$(normalized_scheduler_service_account_name "$SCHEDULER_SA_NAME")"
  local email="${account_id}@${PROJECT}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "$email" \
    --project="$PROJECT" >/dev/null 2>&1; then
    echo "${email}|false"
    return
  fi

  gcloud iam service-accounts create "$account_id" \
    --project="$PROJECT" \
    --display-name="Streaming evaluation scheduler" \
    >/dev/null
  wait_for_service_account "$email"
  echo "${email}|true"
}

wait_for_service_account() {
  local scheduler_sa_email="$1"
  local attempt

  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if gcloud iam service-accounts describe "$scheduler_sa_email" \
      --project="$PROJECT" >/dev/null 2>&1; then
      return
    fi
    sleep 5
  done

  echo "Service account did not become visible in IAM: ${scheduler_sa_email}" >&2
  return 1
}

ensure_scheduler_oidc_binding() {
  local scheduler_sa_email="$1"
  local project_number
  local scheduler_service_agent

  wait_for_service_account "$scheduler_sa_email"
  project_number="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  scheduler_service_agent="service-${project_number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

  gcloud iam service-accounts add-iam-policy-binding "$scheduler_sa_email" \
    --project="$PROJECT" \
    --member="serviceAccount:${scheduler_service_agent}" \
    --role="roles/iam.serviceAccountOpenIdTokenCreator" \
    --quiet >/dev/null
}

deploy_worker() {
  local bq_location="$1"
  local staging
  staging="$(mktemp -d)"
  trap 'rm -rf "$staging"' RETURN

  cp "$SCRIPT_DIR/main.py" "$SCRIPT_DIR/worker.py" "$staging/"
  cp "$SCRIPT_DIR/requirements.txt" "$staging/"
  cp -R "$REPO_ROOT/src" "$staging/"

  cat > "$staging/Procfile" <<'PROCFILE'
web: gunicorn --bind :$PORT --workers 1 --timeout 300 --graceful-timeout 30 main:app
PROCFILE

  gcloud run deploy "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$RUN_REGION" \
    --source="$staging" \
    --quiet \
    --no-allow-unauthenticated \
    --memory=512Mi \
    --timeout=300 \
    --concurrency=1 \
    --min-instances=0 \
    --max-instances=1 \
    --set-env-vars="PYTHONPATH=/workspace/src,BQ_AGENT_PROJECT=${PROJECT},BQ_AGENT_DATASET=${DATASET},BQ_AGENT_TABLE=${SOURCE_TABLE},BQ_AGENT_LOCATION=${bq_location},BQ_AGENT_RESULT_PROJECT=${PROJECT},BQ_AGENT_RESULT_DATASET=${DATASET},BQ_AGENT_RESULT_TABLE=${RESULT_TABLE},BQ_AGENT_STATE_PROJECT=${PROJECT},BQ_AGENT_STATE_DATASET=${DATASET},BQ_AGENT_STATE_TABLE=${STATE_TABLE},BQ_AGENT_RUNS_PROJECT=${PROJECT},BQ_AGENT_RUNS_DATASET=${DATASET},BQ_AGENT_RUNS_TABLE=${RUNS_TABLE},BQ_AGENT_OVERLAP_MINUTES=${OVERLAP_MINUTES},BQ_AGENT_INITIAL_LOOKBACK_MINUTES=${INITIAL_LOOKBACK_MINUTES}" \
    >/dev/null
}

service_url() {
  gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$RUN_REGION" \
    --format='value(status.url)'
}

ensure_scheduler_job() {
  local scheduler_sa_email="$1"
  local url="$2"
  local error_file
  local error_output
  local attempt

  wait_for_service_account "$scheduler_sa_email"
  gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$RUN_REGION" \
    --member="serviceAccount:${scheduler_sa_email}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null

  if gcloud scheduler jobs describe "$SCHEDULER_JOB_NAME" \
    --project="$PROJECT" \
    --location="$RUN_REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs delete "$SCHEDULER_JOB_NAME" \
      --project="$PROJECT" \
      --location="$RUN_REGION" \
      --quiet >/dev/null
  fi

  error_file="$(mktemp)"
  for attempt in 1 2 3 4 5; do
    if gcloud scheduler jobs create http "$SCHEDULER_JOB_NAME" \
      --project="$PROJECT" \
      --location="$RUN_REGION" \
      --schedule="$POLL_SCHEDULE" \
      --time-zone="$SCHEDULER_TIME_ZONE" \
      --uri="${url}/" \
      --http-method=POST \
      --headers=Content-Type=application/json \
      --message-body='{}' \
      --oidc-service-account-email="$scheduler_sa_email" \
      --oidc-token-audience="$url" \
      --attempt-deadline=300s \
      >/dev/null 2>"$error_file"; then
      rm -f "$error_file"
      return
    fi

    error_output="$(cat "$error_file")"
    if [[ "$error_output" != *"NOT_FOUND"* || "$attempt" -eq 5 ]]; then
      rm -f "$error_file"
      echo "$error_output" >&2
      return 1
    fi
    sleep 5
  done
}

write_state() {
  local bq_location="$1"
  local scheduler_sa_email="$2"
  local scheduler_sa_created="$3"

  cat > "$STATE_FILE" <<EOF
{
  "project": "${PROJECT}",
  "dataset": "${DATASET}",
  "source_table": "${SOURCE_TABLE}",
  "run_region": "${RUN_REGION}",
  "bq_location": "${bq_location}",
  "service_name": "${SERVICE_NAME}",
  "scheduler_job_name": "${SCHEDULER_JOB_NAME}",
  "scheduler_service_account": "${scheduler_sa_email}",
  "scheduler_service_account_created": ${scheduler_sa_created},
  "result_table": "${RESULT_TABLE}",
  "state_table": "${STATE_TABLE}",
  "runs_table": "${RUNS_TABLE}"
}
EOF
}

up() {
  require_tool jq
  require_tool bq
  require_tool gcloud
  require_up_inputs

  enable_service_if_needed run.googleapis.com
  enable_service_if_needed cloudbuild.googleapis.com
  enable_service_if_needed artifactregistry.googleapis.com
  enable_service_if_needed cloudscheduler.googleapis.com

  local bq_location
  bq_location="$(infer_bq_location)"

  create_results_table "$bq_location"
  create_state_table "$bq_location"
  create_runs_table "$bq_location"

  deploy_worker "$bq_location"

  local scheduler_sa_result
  scheduler_sa_result="$(ensure_scheduler_service_account)"
  local scheduler_sa_email="${scheduler_sa_result%%|*}"
  local scheduler_sa_created="${scheduler_sa_result##*|}"
  ensure_scheduler_oidc_binding "$scheduler_sa_email"

  local url
  url="$(service_url)"
  ensure_scheduler_job "$scheduler_sa_email" "$url"
  write_state "$bq_location" "$scheduler_sa_email" "$scheduler_sa_created"

  cat <<EOF
Streaming evaluation scheduler deployed.

Cloud Run service:
  ${SERVICE_NAME}

Cloud Scheduler job:
  ${SCHEDULER_JOB_NAME}

Result table:
  ${PROJECT}.${DATASET}.${RESULT_TABLE}

Sample query:
  SELECT *
  FROM \`${PROJECT}.${DATASET}.${RESULT_TABLE}\`
  ORDER BY processed_at DESC
  LIMIT 20;
EOF
}

down() {
  require_tool jq
  require_tool gcloud

  if [[ ! -f "$STATE_FILE" ]]; then
    echo "State file not found: $STATE_FILE" >&2
    exit 1
  fi

  local down_project
  local down_region
  local down_service
  local down_job
  local down_dataset
  local down_result_table
  local down_state_table
  local down_runs_table
  local scheduler_sa_email
  local scheduler_sa_created

  down_project="$(jq -r '.project' "$STATE_FILE")"
  down_dataset="$(jq -r '.dataset' "$STATE_FILE")"
  down_region="$(jq -r '.run_region' "$STATE_FILE")"
  down_service="$(jq -r '.service_name' "$STATE_FILE")"
  down_job="$(jq -r '.scheduler_job_name' "$STATE_FILE")"
  down_result_table="$(jq -r '.result_table' "$STATE_FILE")"
  down_state_table="$(jq -r '.state_table' "$STATE_FILE")"
  down_runs_table="$(jq -r '.runs_table' "$STATE_FILE")"
  scheduler_sa_email="$(jq -r '.scheduler_service_account' "$STATE_FILE")"
  scheduler_sa_created="$(jq -r '.scheduler_service_account_created' "$STATE_FILE")"

  gcloud scheduler jobs delete "$down_job" \
    --project="$down_project" \
    --location="$down_region" \
    --quiet >/dev/null 2>&1 || true

  gcloud run services delete "$down_service" \
    --project="$down_project" \
    --region="$down_region" \
    --quiet >/dev/null 2>&1 || true

  if [[ "$scheduler_sa_created" == "true" ]]; then
    gcloud iam service-accounts delete "$scheduler_sa_email" \
      --project="$down_project" \
      --quiet >/dev/null 2>&1 || true
  fi

  rm -f "$STATE_FILE"
  cat <<EOF
Streaming evaluation scheduler resources removed.

BigQuery tables were preserved intentionally:
  ${down_project}.${down_dataset}.${down_result_table}
  ${down_project}.${down_dataset}.${down_state_table}
  ${down_project}.${down_dataset}.${down_runs_table}

To remove them manually:
  bq rm -t ${down_project}:${down_dataset}.${down_result_table}
  bq rm -t ${down_project}:${down_dataset}.${down_state_table}
  bq rm -t ${down_project}:${down_dataset}.${down_runs_table}
EOF
}

case "$MODE" in
  up)
    up
    ;;
  down)
    down
    ;;
  *)
    usage
    exit 1
    ;;
esac
