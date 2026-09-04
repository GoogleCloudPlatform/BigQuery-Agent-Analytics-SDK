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
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! "$PYTHON_BIN" - <<'PY' >/dev/null; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  echo "ERROR: Python 3.10+ is required. Set PYTHON_BIN to a 3.10+ interpreter." >&2
  exit 1
fi

if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
else
  echo "ERROR: .env not found. Run ./setup.sh first." >&2
  exit 1
fi

export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
export TOKEN_BUDGET="${TOKEN_BUDGET:-12000}"
export MAX_COST_USD="${MAX_COST_USD:-0.05}"
export SELF_EVOLVING_PROMPT_GENERATOR_MODEL="${SELF_EVOLVING_PROMPT_GENERATOR_MODEL:-gemini-2.5-flash}"

RUN_ID="$(date +%Y%m%d_%H%M%S)"
REPORTS_DIR="$SCRIPT_DIR/reports/run_${RUN_ID}"
mkdir -p "$REPORTS_DIR"

echo ""
echo "============================================"
echo "  Self-Evolving Agent Demo"
echo "============================================"
echo ""
echo "Reports: $REPORTS_DIR"
echo "Estimated one-run cloud cost: typically well under \$1 with defaults."
echo ""

cd "$SCRIPT_DIR"
"$PYTHON_BIN" -m agent.prompt_store reset >/dev/null

echo "[1/5] Run baseline V1 agent..."
"$PYTHON_BIN" run_agent.py \
  --label baseline \
  --output-dir "$REPORTS_DIR" \
  --allow-failures

echo ""
echo "[2/5] Analyze traces and generate evolved prompt..."
"$PYTHON_BIN" analyze_and_evolve.py \
  --sessions "$REPORTS_DIR/latest_eval_results_baseline.json" \
  --output-dir "$REPORTS_DIR" \
  --token-budget "$TOKEN_BUDGET" \
  --max-cost-usd "$MAX_COST_USD" \
  --generator-model "$SELF_EVOLVING_PROMPT_GENERATOR_MODEL"

echo ""
echo "[3/5] Run evolved agent..."
"$PYTHON_BIN" run_agent.py \
  --label evolved \
  --output-dir "$REPORTS_DIR" \
  --allow-failures

echo ""
echo "[4/5] Compare before and after..."
"$PYTHON_BIN" compare_runs.py \
  --before "$REPORTS_DIR/latest_eval_results_baseline.json" \
  --after "$REPORTS_DIR/latest_eval_results_evolved.json" \
  --output "$REPORTS_DIR/comparison.json"

echo ""
echo "[5/5] Done."
echo ""
echo "Key artifacts:"
echo "  $REPORTS_DIR/latest_eval_results_baseline.json"
echo "  $REPORTS_DIR/candidate_prompt.json"
echo "  $REPORTS_DIR/prompt_diff.md"
echo "  $REPORTS_DIR/self_evolution_analysis.json"
echo "  $REPORTS_DIR/latest_eval_results_evolved.json"
echo "  $REPORTS_DIR/comparison.json"
echo "  $REPORTS_DIR/comparison.md"
echo ""
