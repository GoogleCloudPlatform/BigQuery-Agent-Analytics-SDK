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

# Setup script for the Agent Improvement Cycle demo.
#
# This script:
#   1. Checks prerequisites (Python, gcloud auth)
#   2. Enables required Google Cloud APIs
#   3. Installs Python dependencies
#   4. Creates the BigQuery dataset if needed
#   5. Writes a .env file with project configuration
#
# Required IAM roles for the authenticated user/service account:
#   - roles/bigquery.dataEditor    (create datasets, write session data)
#   - roles/bigquery.jobUser       (run BigQuery jobs)
#   - roles/aiplatform.user        (call Vertex AI / Gemini models)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

echo ""
echo "============================================"
echo "  Agent Improvement Cycle - Setup"
echo "============================================"
echo ""

# 1. Check Python
echo "[1/5] Checking Python..."
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 is required but not found." >&2
  exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1)
echo "  $PYTHON_VERSION"

# 2. Check gcloud auth
echo ""
echo "[2/5] Checking Google Cloud authentication..."
if ! command -v gcloud &>/dev/null; then
  echo "ERROR: gcloud CLI is required. Install: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: No gcloud project set. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi
echo "  Project: $PROJECT_ID"

# Check application default credentials
if ! gcloud auth application-default print-access-token &>/dev/null 2>&1; then
  echo "  Application default credentials not found. Running login..."
  gcloud auth application-default login
fi
echo "  Credentials: OK"

# 3. Enable required APIs
echo ""
echo "[3/5] Enabling required Google Cloud APIs..."
gcloud services enable bigquery.googleapis.com --project="$PROJECT_ID" 2>/dev/null
echo "  BigQuery API: enabled"
gcloud services enable aiplatform.googleapis.com --project="$PROJECT_ID" 2>/dev/null
echo "  Vertex AI API: enabled"

# 4. Install dependencies
echo ""
echo "[4/5] Installing Python dependencies..."
cd "$REPO_ROOT"
pip install -e ".[all]" --quiet --no-warn-script-location 2>&1 | tail -1 || pip install -e ".[all]"
pip install python-dotenv --quiet --no-warn-script-location 2>&1 | tail -1 || pip install python-dotenv
echo "  Dependencies installed."

# 5. Configure environment
echo ""
echo "[5/5] Configuring environment..."

DATASET_ID="${DATASET_ID:-agent_logs}"
BQ_LOCATION="${BQ_LOCATION:-us-central1}"
TABLE_ID="${TABLE_ID:-agent_events}"

# Create BigQuery dataset if it doesn't exist
if ! bq show "${PROJECT_ID}:${DATASET_ID}" &>/dev/null 2>&1; then
  echo "  Creating BigQuery dataset: ${DATASET_ID} in ${BQ_LOCATION}..."
  bq mk --dataset --location="$BQ_LOCATION" "${PROJECT_ID}:${DATASET_ID}" 2>/dev/null || true
fi

# Write .env file (don't overwrite if it exists)
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
# Agent Improvement Cycle Demo Configuration
PROJECT_ID=$PROJECT_ID
GOOGLE_CLOUD_PROJECT=$PROJECT_ID
DATASET_ID=$DATASET_ID
DATASET_LOCATION=$BQ_LOCATION
TABLE_ID=$TABLE_ID
DEMO_MODEL_ID=gemini-2.5-flash
DEMO_AGENT_LOCATION=us-central1
EOF
  echo "  Created $ENV_FILE"
else
  echo "  $ENV_FILE already exists, skipping."
fi

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "To run a single improvement cycle:"
echo "  cd $SCRIPT_DIR"
echo "  ./run_cycle.sh"
echo ""
echo "To run 3 cycles and watch the score climb:"
echo "  ./run_cycle.sh --cycles 3"
echo ""
