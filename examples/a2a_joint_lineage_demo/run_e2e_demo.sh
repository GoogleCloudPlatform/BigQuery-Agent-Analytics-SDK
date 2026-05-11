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

# One-command runner for the A2A Joint Lineage demo.
#
# Prerequisite:
#   ./setup.sh
#
# This script starts the receiver A2A server in the background, runs the
# receiver smoke gate, runs the caller campaigns, builds both per-org SDK
# context graphs, builds the auditor joint graph, then stops the receiver.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
VENV_PY="$SCRIPT_DIR/.venv/bin/python3"
SERVER_LOG="$SCRIPT_DIR/receiver_server.log"
SERVER_PID=""

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" &>/dev/null; then
    echo ""
    echo "Stopping receiver server (pid $SERVER_PID)..."
    kill "$SERVER_PID" &>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ ! -f "$ENV_FILE" || ! -x "$VENV_PY" ]]; then
  echo "ERROR: run ./setup.sh first so .env and ./.venv exist." >&2
  exit 2
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

PROJECT_ID="${PROJECT_ID:?missing PROJECT_ID in .env}"
RECEIVER_A2A_URL="${RECEIVER_A2A_URL:-http://127.0.0.1:8000}"
RECEIVER_START_TIMEOUT_S="${DEMO_RECEIVER_START_TIMEOUT_S:-60}"
RECEIVER_AGENT_CARD="${RECEIVER_A2A_URL%/}/.well-known/agent-card.json"

echo ""
echo "============================================"
echo "  A2A Joint Lineage Demo — E2E Runner"
echo "============================================"
echo ""
echo "Project:      $PROJECT_ID"
echo "Receiver URL: $RECEIVER_A2A_URL"
echo "Server log:   $SERVER_LOG"
echo ""

echo "[1/5] Starting receiver A2A server..."
: > "$SERVER_LOG"
"$VENV_PY" "$SCRIPT_DIR/run_receiver_server.py" > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "  Waiting for receiver agent card..."
deadline=$((SECONDS + RECEIVER_START_TIMEOUT_S))
until "$VENV_PY" -c \
  'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2).read()' \
  "$RECEIVER_AGENT_CARD" &>/dev/null; do
  if ! kill -0 "$SERVER_PID" &>/dev/null; then
    echo "ERROR: receiver server exited before becoming ready." >&2
    echo "Last 80 log lines:" >&2
    tail -80 "$SERVER_LOG" >&2 || true
    exit 1
  fi
  if (( SECONDS >= deadline )); then
    echo "ERROR: receiver did not become ready within ${RECEIVER_START_TIMEOUT_S}s." >&2
    echo "Last 80 log lines:" >&2
    tail -80 "$SERVER_LOG" >&2 || true
    exit 1
  fi
  sleep 2
done
echo "  Receiver ready: $RECEIVER_AGENT_CARD"

echo ""
echo "[2/5] Smoke-testing receiver plugin writes..."
"$VENV_PY" "$SCRIPT_DIR/smoke_receiver.py"

echo ""
echo "[3/5] Running caller campaigns through the remote A2A receiver..."
"$VENV_PY" "$SCRIPT_DIR/run_caller_agent.py"

echo ""
echo "[4/5] Building caller and receiver SDK context graphs..."
"$VENV_PY" "$SCRIPT_DIR/build_org_graphs.py"

echo ""
echo "[5/5] Building auditor projections and joint property graph..."
"$VENV_PY" "$SCRIPT_DIR/build_joint_graph.py"

echo ""
echo "============================================"
echo "  E2E demo ready"
echo "============================================"
echo ""
echo "Open BigQuery Studio:"
echo "  https://console.cloud.google.com/bigquery?project=$PROJECT_ID"
echo ""
echo "Use:"
echo "  $SCRIPT_DIR/BQ_STUDIO_WALKTHROUGH.md"
echo "  $SCRIPT_DIR/bq_studio_queries.gql"
echo "  $SCRIPT_DIR/DEMO_NARRATION.md"
echo ""
