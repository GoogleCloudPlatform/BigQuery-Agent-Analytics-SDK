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

# End-to-end Combined CUJ demo runner.
# Usage:
#   ./run_e2e_demo.sh [--offline] [--with-vector-index-scale] [--no-rerank]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

OFFLINE_MODE=false
WITH_SCALE=false
NO_RERANK=false

for arg in "$@"; do
  case "$arg" in
    --offline)
      OFFLINE_MODE=true
      ;;
    --with-vector-index-scale)
      WITH_SCALE=true
      ;;
    --no-rerank)
      NO_RERANK=true
      ;;
    -h|--help)
      echo "Usage: ./run_e2e_demo.sh [--offline] [--with-vector-index-scale] [--no-rerank]"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown flag: $arg" >&2
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
  echo "ERROR: $PYTHON_BIN not found. Set PYTHON_BIN to a Python 3.10+ interpreter." >&2
  exit 1
fi
if ! "$PYTHON_BIN" - <<'PY' >/dev/null; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  echo "ERROR: Python 3.10+ is required." >&2
  exit 1
fi

if [[ "$OFFLINE_MODE" != "true" ]]; then
  if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
  else
    echo "ERROR: .env not found. Run ./setup.sh first or pass --offline." >&2
    exit 1
  fi
fi

export PYTHONDONTWRITEBYTECODE=1
export PYTHONSAFEPATH=1
export PYTHONPATH="$SCRIPT_DIR:${REPO_ROOT}/src:${PYTHONPATH:-}"

PROJECT_ID="${PROJECT_ID:-demo-project}"
DATASET_ID="${DATASET_ID:-combined_cuj_trace_search_demo}"
TABLE_ID="${TABLE_ID:-agent_events}"
EVAL_TURNS_TABLE="${EVAL_TURNS_TABLE:-eval_turns}"
BQ_LOCATION="${BQ_LOCATION:-US}"
AGENT_LOCATION="${AGENT_LOCATION:-us-central1}"
MODEL_NAME="${MODEL_NAME:-gemini-2.5-flash}"

RUN_ID="$(date -u +%Y%m%d_%H%M%S)"
REPORTS_DIR="${REPORTS_DIR:-$SCRIPT_DIR/reports/run_${RUN_ID}}"
mkdir -p "$REPORTS_DIR"

LIVE_JSON="$REPORTS_DIR/live_demo_results.json"
SEARCH_JSON="$REPORTS_DIR/search_results.json"
PATTERN_JSON="$REPORTS_DIR/pattern_eval_report.json"
EVAL_SUITE_JSON="$REPORTS_DIR/eval_dataset_generated.json"
SHOWCASE_HTML="$REPORTS_DIR/showcase.html"

echo ""
echo "========================================================="
echo "  Combined CUJ Trace Search & Cost-Aware Eval Demo"
echo "========================================================="
echo "Mode: $( [[ "$OFFLINE_MODE" == "true" ]] && echo 'Offline (Mock-Live)' || echo 'Live BigQuery + ADK' )"
echo "Reports: $REPORTS_DIR"
echo ""

cd "$SCRIPT_DIR"

echo "[1/4] Running ADK agent scenarios & S1/S2/S3 extraction ablation..."
STAGE1_ARGS=(
  --project-id "$PROJECT_ID"
  --dataset-id "$DATASET_ID"
  --table-id "$TABLE_ID"
  --eval-turns-table "$EVAL_TURNS_TABLE"
  --location "$BQ_LOCATION"
  --agent-location "$AGENT_LOCATION"
  --model "$MODEL_NAME"
  --output-json "$LIVE_JSON"
)
if [[ "$OFFLINE_MODE" == "true" ]]; then
  STAGE1_ARGS+=(--mock-live)
fi
"$PYTHON_BIN" -m agent.run_live_demo "${STAGE1_ARGS[@]}"

echo ""
echo "[2/4] Setting up dual indexes & running 4-layer search benchmark..."
STAGE2_ARGS=(
  --project-id "$PROJECT_ID"
  --dataset-id "$DATASET_ID"
  --events-table "$TABLE_ID"
  --eval-turns-table "$EVAL_TURNS_TABLE"
  --location "$BQ_LOCATION"
  --vertex-location "$AGENT_LOCATION"
  --input-json "$LIVE_JSON"
  --output-json "$SEARCH_JSON"
)
if [[ "$OFFLINE_MODE" == "true" ]]; then
  STAGE2_ARGS+=(--offline)
else
  STAGE2_ARGS+=(--embed-wait-seconds 75)
fi
if [[ "$WITH_SCALE" == "true" ]]; then
  STAGE2_ARGS+=(--with-vector-index-scale)
fi
if [[ "$NO_RERANK" == "true" ]]; then
  STAGE2_ARGS+=(--no-rerank)
fi
if [[ -n "${BQML_CONNECTION_ID:-}" ]]; then
  STAGE2_ARGS+=(--connection-id "$BQML_CONNECTION_ID")
fi
"$PYTHON_BIN" -m analytics.index_and_search "${STAGE2_ARGS[@]}"

echo ""
echo "[3/4] Running pattern discovery -> EvalSuite export & 8-lever cascade..."
STAGE3_ARGS=(
  --project-id "$PROJECT_ID"
  --dataset-id "$DATASET_ID"
  --eval-turns-table "$EVAL_TURNS_TABLE"
  --location "$BQ_LOCATION"
  --input-json "$LIVE_JSON"
  --search-json "$SEARCH_JSON"
  --output-json "$PATTERN_JSON"
  --eval-suite-output "$EVAL_SUITE_JSON"
)
if [[ "$OFFLINE_MODE" == "true" ]]; then
  STAGE3_ARGS+=(--offline)
fi
"$PYTHON_BIN" -m analytics.pattern_to_eval "${STAGE3_ARGS[@]}"

echo ""
echo "[4/4] Building self-contained Adaptive Workbench HTML showcase..."
"$PYTHON_BIN" -m report.build_showcase_html \
  --live-json "$LIVE_JSON" \
  --search-json "$SEARCH_JSON" \
  --pattern-json "$PATTERN_JSON" \
  --eval-suite-json "$EVAL_SUITE_JSON" \
  --output "$SHOWCASE_HTML"

echo ""
echo "Demo complete! Artifacts written to:"
echo "  $LIVE_JSON"
echo "  $SEARCH_JSON"
echo "  $PATTERN_JSON"
echo "  $EVAL_SUITE_JSON"
echo "  $SHOWCASE_HTML"
