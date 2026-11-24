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

# Bootstrap configuration and BigQuery dataset for the Combined CUJ demo.
# Usage:
#   ./setup.sh            # Live BigQuery + Vertex AI setup
#   ./setup.sh --offline  # Write local .env for offline execution only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

OFFLINE_MODE=false
for arg in "$@"; do
  case "$arg" in
    --offline)
      OFFLINE_MODE=true
      ;;
    -h|--help)
      echo "Usage: ./setup.sh [--offline]"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

resolve_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    echo "$PYTHON_BIN"
  elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    echo "$SCRIPT_DIR/.venv/bin/python"
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    echo "$REPO_ROOT/.venv/bin/python"
  else
    echo "python3"
  fi
}

PYTHON_BIN="$(resolve_python)"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
  echo "ERROR: $PYTHON_BIN not found. Set PYTHON_BIN to a Python 3.10+ binary." >&2
  exit 1
fi
if ! "$PYTHON_BIN" - <<'PY' >/dev/null; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  echo "ERROR: Python 3.10+ is required." >&2
  exit 1
fi

DATASET_ID="${DATASET_ID:-combined_cuj_trace_search_demo}"
TABLE_ID="${TABLE_ID:-agent_events}"
EVAL_TURNS_TABLE="${EVAL_TURNS_TABLE:-eval_turns}"
BQ_LOCATION="${BQ_LOCATION:-US}"
AGENT_LOCATION="${AGENT_LOCATION:-us-central1}"
MODEL_NAME="${MODEL_NAME:-gemini-2.5-flash}"
BQML_CONNECTION_ID="${BQML_CONNECTION_ID:-}"
GEMINI_API_KEY="${GEMINI_API_KEY:-}"

if [[ "$OFFLINE_MODE" == "true" ]]; then
  PROJECT_ID="${PROJECT_ID:-demo-project}"
  cat > "$ENV_FILE" <<EOF
# Combined CUJ Trace Search Demo (Offline Mode)
PROJECT_ID=$PROJECT_ID
GOOGLE_CLOUD_QUOTA_PROJECT=$PROJECT_ID
DATASET_ID=$DATASET_ID
TABLE_ID=$TABLE_ID
EVAL_TURNS_TABLE=$EVAL_TURNS_TABLE
BQ_LOCATION=$BQ_LOCATION
AGENT_LOCATION=$AGENT_LOCATION
MODEL_NAME=$MODEL_NAME
DEMO_OFFLINE=true
EOF
  chmod 600 "$ENV_FILE"
  echo "Offline .env written to $ENV_FILE"
  echo "Run: ./run_e2e_demo.sh --offline"
  exit 0
fi

if ! command -v gcloud &>/dev/null; then
  echo "ERROR: gcloud CLI is required for live setup (or pass --offline)." >&2
  exit 1
fi
if ! command -v bq &>/dev/null; then
  echo "ERROR: bq CLI is required for live setup (or pass --offline)." >&2
  exit 1
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: PROJECT_ID is empty. Export PROJECT_ID or run 'gcloud config set project <ID>'." >&2
  exit 1
fi
GOOGLE_CLOUD_QUOTA_PROJECT="${GOOGLE_CLOUD_QUOTA_PROJECT:-$PROJECT_ID}"

if ! gcloud auth application-default print-access-token &>/dev/null 2>&1; then
  echo "Application default credentials not found. Starting login..."
  gcloud auth application-default login
fi

echo "Enabling BigQuery and Vertex AI APIs on $PROJECT_ID..."
gcloud services enable bigquery.googleapis.com aiplatform.googleapis.com --project="$PROJECT_ID" >/dev/null

if ! "$PYTHON_BIN" -c "import bigquery_agent_analytics, google.adk, google.genai" 2>/dev/null; then
  echo "Installing editable SDK + LLM extras..."
  "$PYTHON_BIN" -m pip install -e "$REPO_ROOT[llm]" --quiet
fi

if ! bq show "${PROJECT_ID}:${DATASET_ID}" &>/dev/null 2>&1; then
  echo "Creating BigQuery dataset ${PROJECT_ID}:${DATASET_ID} in ${BQ_LOCATION}..."
  bq mk --dataset --location="$BQ_LOCATION" "${PROJECT_ID}:${DATASET_ID}" >/dev/null
else
  EXISTING_LOC="$(
    bq show --format=prettyjson "${PROJECT_ID}:${DATASET_ID}" \
      | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin).get("location", ""))'
  )"
  if [[ "${EXISTING_LOC,,}" != "${BQ_LOCATION,,}" ]]; then
    echo "ERROR: Existing dataset ${DATASET_ID} is in ${EXISTING_LOC}, expected ${BQ_LOCATION}." >&2
    exit 1
  fi
fi

cat > "$ENV_FILE" <<EOF
# Combined CUJ Trace Search Demo Configuration
PROJECT_ID=$PROJECT_ID
GOOGLE_CLOUD_QUOTA_PROJECT=$GOOGLE_CLOUD_QUOTA_PROJECT
DATASET_ID=$DATASET_ID
TABLE_ID=$TABLE_ID
EVAL_TURNS_TABLE=$EVAL_TURNS_TABLE
BQ_LOCATION=$BQ_LOCATION
AGENT_LOCATION=$AGENT_LOCATION
MODEL_NAME=$MODEL_NAME
BQML_CONNECTION_ID=$BQML_CONNECTION_ID
GEMINI_API_KEY=$GEMINI_API_KEY
GOOGLE_GENAI_USE_VERTEXAI=true
EOF
chmod 600 "$ENV_FILE"

echo "Setup complete ($ENV_FILE)."
echo "Next: ./run_e2e_demo.sh"
