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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

echo ""
echo "============================================"
echo "  Self-Evolving Agent Demo - Setup"
echo "============================================"
echo ""
echo "Estimated one-run cloud cost: typically well under \$1 for the"
echo "default four-question demo. Setup itself only enables APIs, installs"
echo "local dependencies, and creates a small BigQuery dataset."
echo ""

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
  echo "ERROR: $PYTHON_BIN is required." >&2
  exit 1
fi
if ! "$PYTHON_BIN" - <<'PY' >/dev/null; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  echo "ERROR: Python 3.10+ is required. Set PYTHON_BIN to a 3.10+ interpreter." >&2
  exit 1
fi
if ! command -v gcloud &>/dev/null; then
  echo "ERROR: gcloud CLI is required." >&2
  exit 1
fi
if ! command -v bq &>/dev/null; then
  echo "ERROR: bq CLI is required." >&2
  exit 1
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: No project set. Export PROJECT_ID or run:" >&2
  echo "  gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi
echo "Project: $PROJECT_ID"

if ! gcloud auth application-default print-access-token &>/dev/null 2>&1; then
  echo "Application default credentials not found. Starting login..."
  gcloud auth application-default login
fi

echo ""
echo "Enabling required APIs..."
gcloud services enable bigquery.googleapis.com --project="$PROJECT_ID" >/dev/null
gcloud services enable aiplatform.googleapis.com --project="$PROJECT_ID" >/dev/null
echo "APIs enabled."

echo ""
echo "Installing local package dependencies..."
"$PYTHON_BIN" -m pip install -e "$REPO_ROOT[improvement]" --quiet
echo "Dependencies installed."

DATASET_LOCATION="${DATASET_LOCATION:-${BQ_LOCATION:-us-central1}}"
SELF_EVOLVING_DATASET_ID="${SELF_EVOLVING_DATASET_ID:-self_evolving_agent_demo}"
SELF_EVOLVING_TABLE_ID="${SELF_EVOLVING_TABLE_ID:-agent_events}"
SELF_EVOLVING_AGENT_MODEL="${SELF_EVOLVING_AGENT_MODEL:-gemini-2.5-flash}"
SELF_EVOLVING_PROMPT_GENERATOR_MODEL="${SELF_EVOLVING_PROMPT_GENERATOR_MODEL:-gemini-2.5-flash}"
SELF_EVOLVING_AGENT_LOCATION="${SELF_EVOLVING_AGENT_LOCATION:-us-central1}"
TOKEN_BUDGET="${TOKEN_BUDGET:-12000}"
MAX_COST_USD="${MAX_COST_USD:-0.05}"

if ! bq show "${PROJECT_ID}:${SELF_EVOLVING_DATASET_ID}" &>/dev/null 2>&1; then
  echo ""
  echo "Creating BigQuery dataset: ${SELF_EVOLVING_DATASET_ID} (${DATASET_LOCATION})"
  bq mk --dataset --location="$DATASET_LOCATION" \
    "${PROJECT_ID}:${SELF_EVOLVING_DATASET_ID}" >/dev/null
else
  EXISTING_LOCATION="$(
    bq show --format=prettyjson "${PROJECT_ID}:${SELF_EVOLVING_DATASET_ID}" \
      | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin).get("location", ""))'
  )"
  if [[ "${EXISTING_LOCATION,,}" != "${DATASET_LOCATION,,}" ]]; then
    echo "ERROR: Dataset ${SELF_EVOLVING_DATASET_ID} exists in ${EXISTING_LOCATION}," >&2
    echo "but DATASET_LOCATION is ${DATASET_LOCATION}. Use a matching location or a new dataset ID." >&2
    exit 1
  fi
fi

cat > "$ENV_FILE" <<EOF
# Self-Evolving Agent Demo Configuration
PROJECT_ID=$PROJECT_ID
DATASET_LOCATION=$DATASET_LOCATION
SELF_EVOLVING_DATASET_ID=$SELF_EVOLVING_DATASET_ID
SELF_EVOLVING_TABLE_ID=$SELF_EVOLVING_TABLE_ID
SELF_EVOLVING_AGENT_MODEL=$SELF_EVOLVING_AGENT_MODEL
SELF_EVOLVING_PROMPT_GENERATOR_MODEL=$SELF_EVOLVING_PROMPT_GENERATOR_MODEL
SELF_EVOLVING_AGENT_LOCATION=$SELF_EVOLVING_AGENT_LOCATION
TOKEN_BUDGET=$TOKEN_BUDGET
MAX_COST_USD=$MAX_COST_USD
GOOGLE_GENAI_USE_VERTEXAI=true
EOF

cd "$SCRIPT_DIR"
"$PYTHON_BIN" -m agent.prompt_store reset >/dev/null

echo ""
echo "Setup complete."
echo "Run:"
echo "  cd $SCRIPT_DIR"
echo "  ./run_e2e_demo.sh"
