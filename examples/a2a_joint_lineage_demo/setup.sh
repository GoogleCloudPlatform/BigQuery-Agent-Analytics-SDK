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

# Bootstrap for the A2A Joint Lineage demo.
#
# Steps:
#   1. Verify python3 + gcloud + .venv tooling.
#   2. Enable BigQuery + Vertex AI APIs.
#   3. Install dependencies into ./.venv.
#   4. Create caller, receiver, auditor, and analyst datasets if missing.
#   5. Write a .env file the agents and runners read.
#
# After setup, the demo runs in two terminals:
#
#   Terminal A  (long-lived receiver server)
#     ./.venv/bin/python3 run_receiver_server.py
#
#   Terminal B  (smoke + caller campaigns + dual graph + auditor graph + analyst)
#     ./.venv/bin/python3 smoke_receiver.py
#     ./.venv/bin/python3 run_caller_agent.py
#     ./.venv/bin/python3 build_org_graphs.py
#     ./.venv/bin/python3 build_joint_graph.py
#     ./.venv/bin/python3 run_analyst_agent.py
#
# Required IAM roles for the authenticated principal:
#   - roles/bigquery.dataEditor
#   - roles/bigquery.jobUser
#   - roles/aiplatform.user        (live agent + AI.GENERATE)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

echo ""
echo "============================================"
echo "  A2A Joint Lineage Demo — Setup"
echo "============================================"
echo ""

# 1. Tooling
echo "[1/5] Checking python3 and gcloud..."
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
echo "[2/5] Enabling BigQuery + Vertex AI APIs..."
gcloud services enable bigquery.googleapis.com --project="$PROJECT_ID" 2>/dev/null
gcloud services enable aiplatform.googleapis.com --project="$PROJECT_ID" 2>/dev/null
echo "  BigQuery + Vertex AI APIs enabled."

# 3. Dependencies
echo ""
echo "[3/5] Installing Python dependencies into ./.venv..."
VENV_DIR="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python3"
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip show vertexai 2>/dev/null | grep -q "^Version:" && \
  "$VENV_PY" -m pip uninstall vertexai -y --quiet 2>/dev/null || true
"$VENV_PY" -m pip install \
  "google-cloud-bigquery>=3.13.0" \
  "google-cloud-aiplatform>=1.148.0" \
  "google-adk[a2a]>=1.21.0" \
  "google-genai>=1.0.0" \
  "python-dotenv>=1.0.0" \
  "uvicorn>=0.30.0" \
  "httpx>=0.27.0" \
  --quiet
# google-adk's [a2a] extra pulls a2a-sdk, which the receiver server
# (to_a2a()) and the caller's RemoteA2aAgent both import as `a2a`.
# Without the extra, both fail with ModuleNotFoundError before the
# demo starts.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$VENV_PY" -m pip install -e "$REPO_ROOT" --quiet
echo "  Dependencies installed in $VENV_DIR"

# 4. Datasets
echo ""
echo "[4/5] Configuring datasets and environment..."
DATASET_LOCATION="${DATASET_LOCATION:-${BQ_LOCATION:-us-central1}}"
CALLER_DATASET_ID="${CALLER_DATASET_ID:-a2a_caller_demo}"
CALLER_TABLE_ID="${CALLER_TABLE_ID:-agent_events}"
RECEIVER_DATASET_ID="${RECEIVER_DATASET_ID:-a2a_receiver_demo}"
RECEIVER_TABLE_ID="${RECEIVER_TABLE_ID:-agent_events}"
AUDITOR_DATASET_ID="${AUDITOR_DATASET_ID:-a2a_auditor_demo}"
ANALYST_DATASET_ID="${ANALYST_DATASET_ID:-a2a_analyst_demo}"
ANALYST_TABLE_ID="${ANALYST_TABLE_ID:-agent_events}"
# Gemini 3.x model defaults — verified against live Vertex AI +
# BigQuery AI.GENERATE in May 2026:
#
#   - DEMO_AGENT_LOCATION must be `global` for Gemini 3.x preview
#     models. They are not published at us-central1 (or any other
#     regional location); a regional lookup returns 404. To fall
#     back to gemini-2.5-pro, also override
#     DEMO_AGENT_LOCATION=us-central1.
#
#   - DEMO_AGENT_MODEL: gemini-3.1-pro-preview is the supported 3.x
#     preview ID. gemini-3-pro-preview was discontinued in March
#     2026; gemini-3.1-pro-preview is the current ID.
#
#   - DEMO_AI_ENDPOINT for BigQuery AI.GENERATE must be the full
#     HTTPS endpoint URL. During the Gemini 3 preview the BQML
#     simple-name resolver does NOT recognize "gemini-3-flash" or
#     "gemini-3-flash-preview"; only the full URL works, and only
#     the locations/global publisher path is registered. The model
#     ID is gemini-3-flash-preview (NOT gemini-3-flash). The full
#     URL must be substituted with the demo's PROJECT_ID at .env-
#     write time below; a literal default would not be reachable
#     across projects.
#
#   - To fall back to stable models on a project without Gemini 3
#     preview access, override:
#       DEMO_AGENT_LOCATION=us-central1
#       DEMO_AGENT_MODEL=gemini-2.5-pro
#       DEMO_AI_ENDPOINT=gemini-2.5-flash
DEMO_AGENT_LOCATION="${DEMO_AGENT_LOCATION:-global}"
DEMO_AGENT_MODEL="${DEMO_AGENT_MODEL:-gemini-3.1-pro-preview}"
DEMO_AI_ENDPOINT="${DEMO_AI_ENDPOINT:-https://aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/global/publishers/google/models/gemini-3-flash-preview}"
RECEIVER_A2A_URL="${RECEIVER_A2A_URL:-http://127.0.0.1:8000}"

for ds in "$CALLER_DATASET_ID" "$RECEIVER_DATASET_ID" "$AUDITOR_DATASET_ID" "$ANALYST_DATASET_ID"; do
  if bq show "${PROJECT_ID}:${ds}" &>/dev/null; then
    echo "  Dataset ${ds} already exists."
    continue
  fi
  echo "  Creating BigQuery dataset: ${ds} in ${DATASET_LOCATION}..."
  # No `|| true` — let `bq mk` surface its own error and stop the
  # script. Otherwise the .env file gets written and the user
  # discovers the missing dataset at build_joint_graph.py time
  # with a much less actionable error.
  if ! bq mk --dataset --location="$DATASET_LOCATION" \
       "${PROJECT_ID}:${ds}"; then
    echo "ERROR: failed to create dataset ${ds}. Check that the" \
         "authenticated principal has roles/bigquery.dataEditor on" \
         "project ${PROJECT_ID} and that ${DATASET_LOCATION} is a" \
         "valid BigQuery location." >&2
    exit 1
  fi
  # Defensive: confirm the dataset is actually visible after mk.
  if ! bq show "${PROJECT_ID}:${ds}" &>/dev/null; then
    echo "ERROR: bq mk reported success but ${ds} is not visible." \
         "Re-run after addressing." >&2
    exit 1
  fi
done

cat > "$ENV_FILE" <<EOF
# A2A Joint Lineage Demo Configuration
PROJECT_ID=$PROJECT_ID
DATASET_LOCATION=$DATASET_LOCATION

CALLER_DATASET_ID=$CALLER_DATASET_ID
CALLER_TABLE_ID=$CALLER_TABLE_ID

RECEIVER_DATASET_ID=$RECEIVER_DATASET_ID
RECEIVER_TABLE_ID=$RECEIVER_TABLE_ID

AUDITOR_DATASET_ID=$AUDITOR_DATASET_ID

ANALYST_DATASET_ID=$ANALYST_DATASET_ID
ANALYST_TABLE_ID=$ANALYST_TABLE_ID

DEMO_AGENT_LOCATION=$DEMO_AGENT_LOCATION
DEMO_AGENT_MODEL=$DEMO_AGENT_MODEL
DEMO_AI_ENDPOINT=$DEMO_AI_ENDPOINT

RECEIVER_A2A_URL=$RECEIVER_A2A_URL
EOF
echo "  Wrote $ENV_FILE"

# 5. Done
echo ""
echo "[5/5] Done."
echo ""
echo "============================================"
echo "  Setup complete! Next: run the demo."
echo "============================================"
echo ""
echo "Presentation run (starts/stops the receiver server for you):"
echo "  cd $SCRIPT_DIR && ./run_e2e_demo.sh"
echo ""
echo "Two terminals:"
echo ""
echo "  Terminal A (receiver server, leave running):"
echo "    cd $SCRIPT_DIR && ./.venv/bin/python3 run_receiver_server.py"
echo ""
echo "  Terminal B (smoke + caller campaigns + dual graph + auditor graph):"
echo "    cd $SCRIPT_DIR"
echo "    ./.venv/bin/python3 smoke_receiver.py"
echo "    ./.venv/bin/python3 run_caller_agent.py"
echo "    ./.venv/bin/python3 build_org_graphs.py"
echo "    ./.venv/bin/python3 build_joint_graph.py   # runs ./render_queries.sh itself"
echo "    ./.venv/bin/python3 run_analyst_agent.py   # closes the loop"
echo ""
echo "  (Re-run ./render_queries.sh only after editing .env or *.gql.tpl.)"
echo ""
echo "Acceptance gates:"
echo "  - run_caller_agent.py asserts each campaign has ≥1 A2A_INTERACTION row"
echo "    and that ≥1 caller a2a_context_id matches a receiver session_id."
echo "  - build_org_graphs.py asserts the receiver extracted ≥3 decisions"
echo "    and ≥9 candidates from its LLM responses (the prompt-shaped"
echo "    response contract)."
echo ""
echo "Tear down:  ./reset.sh"
echo ""
