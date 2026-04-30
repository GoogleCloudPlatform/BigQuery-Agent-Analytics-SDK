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

# One-shot bootstrap for the Decision Lineage demo.
#
# Steps:
#   1. Verify python3 + gcloud are available, app-default creds exist.
#   2. Enable BigQuery + Vertex AI APIs.
#   3. Install Python dependencies (SDK installed editable from the
#      repo root so `bigquery_agent_analytics` resolves locally).
#   4. Create the BigQuery dataset if missing.
#   5. Write a .env file the seed/build/render scripts read.
#   6. Run seed_traces.py — INSERTs plugin-shape trace rows.
#   7. Run build_graph.py — calls
#        ContextGraphManager.build_context_graph(use_ai_generate=True,
#                                                include_decisions=True)
#      which invokes AI.GENERATE twice (biz nodes, then decisions),
#      writes BizNode + DecisionPoint + Candidate rows, builds the
#      cross-link / decision edges, and emits CREATE OR REPLACE
#      PROPERTY GRAPH.
#   8. Render bq_studio_queries.gql with project/dataset/session
#      values inlined for copy-paste into BigQuery Studio.
#
# Required IAM roles for the authenticated user/service account:
#   - roles/bigquery.dataEditor
#   - roles/bigquery.jobUser
#   - roles/aiplatform.user        (AI.GENERATE during build_graph.py)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

echo ""
echo "============================================"
echo "  Decision Lineage Demo - Setup"
echo "============================================"
echo ""

# 1. Tooling
echo "[1/8] Checking python3 and gcloud..."
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 is required." >&2
  exit 1
fi
if ! command -v gcloud &>/dev/null; then
  echo "ERROR: gcloud CLI is required. Install: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi
echo "  $(python3 --version)"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: No project. Export PROJECT_ID or 'gcloud config set project ...'" >&2
  exit 1
fi
echo "  Project: $PROJECT_ID"

if ! gcloud auth application-default print-access-token &>/dev/null 2>&1; then
  echo "  Application default credentials not found. Running login..."
  gcloud auth application-default login
fi

# 2. APIs
echo ""
echo "[2/8] Enabling BigQuery + Vertex AI APIs..."
gcloud services enable bigquery.googleapis.com --project="$PROJECT_ID" 2>/dev/null
echo "  BigQuery API: enabled"
gcloud services enable aiplatform.googleapis.com --project="$PROJECT_ID" 2>/dev/null
echo "  Vertex AI API: enabled"

# 3. Dependencies
echo ""
echo "[3/8] Installing Python dependencies..."
pip install "google-cloud-bigquery>=3.13.0" "python-dotenv>=1.0.0" --quiet
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
pip install -e "$REPO_ROOT" --quiet
echo "  Dependencies installed."

# 4. Dataset + .env
echo ""
echo "[4/8] Configuring environment..."
DATASET_ID="${DATASET_ID:-decision_lineage_demo}"
DATASET_LOCATION="${DATASET_LOCATION:-${BQ_LOCATION:-US}}"
TABLE_ID="${TABLE_ID:-agent_events}"
DEMO_SESSION_ID="${DEMO_SESSION_ID:-sess-nike-summer-2026}"
DEMO_AI_ENDPOINT="${DEMO_AI_ENDPOINT:-gemini-2.5-flash}"

if ! bq show "${PROJECT_ID}:${DATASET_ID}" &>/dev/null 2>&1; then
  echo "  Creating BigQuery dataset: ${DATASET_ID} in ${DATASET_LOCATION}..."
  bq mk --dataset --location="$DATASET_LOCATION" \
    "${PROJECT_ID}:${DATASET_ID}" 2>/dev/null || true
fi

cat > "$ENV_FILE" <<EOF
# Decision Lineage Demo Configuration
PROJECT_ID=$PROJECT_ID
DATASET_ID=$DATASET_ID
DATASET_LOCATION=$DATASET_LOCATION
TABLE_ID=$TABLE_ID
DEMO_SESSION_ID=$DEMO_SESSION_ID
DEMO_AI_ENDPOINT=$DEMO_AI_ENDPOINT
EOF
echo "  Wrote $ENV_FILE"

# 5. Seed traces
echo ""
echo "[5/8] Seeding plugin-shape trace rows into agent_events..."
cd "$SCRIPT_DIR"
python3 seed_traces.py

# 6. Build graph (AI.GENERATE)
echo ""
echo "[6/8] Building the property graph via the SDK extraction "
echo "      pipeline (AI.GENERATE — this can take 30-60s)..."
python3 build_graph.py

# 7. Render BQ Studio queries
echo ""
echo "[7/8] Rendering BigQuery Studio query bundle..."
"$SCRIPT_DIR/render_queries.sh"

# 8. Done
echo ""
echo "[8/8] Done."
echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "Next:"
echo "  1. Open BigQuery Studio in your browser:"
echo "     https://console.cloud.google.com/bigquery?project=${PROJECT_ID}"
echo "  2. Navigate to dataset: ${DATASET_ID}"
echo "     (the property graph 'agent_context_graph' shows in the Explorer pane)"
echo "  3. Open ${SCRIPT_DIR}/bq_studio_queries.gql"
echo "     in a text editor and paste each block into BQ Studio."
echo ""
echo "Talk track + timing: see DEMO_NARRATION.md"
echo "Tear down:           ./reset.sh"
echo ""
