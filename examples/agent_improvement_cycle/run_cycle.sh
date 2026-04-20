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

# Agent Improvement Cycle Orchestrator
#
# Runs the full cycle:
#   1. Run eval cases against the agent (sessions logged to BigQuery)
#   2. Generate quality report with LLM-as-a-judge evaluation
#   3. Run the improver to fix the prompt and extend eval cases
#   4. Print summary
#
# Usage:
#   ./run_cycle.sh              # Run one improvement cycle
#   ./run_cycle.sh --cycles 3   # Run 3 consecutive cycles
#   ./run_cycle.sh --eval-only  # Only run eval, skip improvement

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
REPORTS_DIR="$SCRIPT_DIR/reports"

# Defaults
CYCLES=1
EVAL_ONLY=false
APP_NAME="company_info_agent"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cycles)
      CYCLES="$2"
      shift 2
      ;;
    --eval-only)
      EVAL_ONLY=true
      shift
      ;;
    --app-name)
      APP_NAME="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --cycles N     Run N improvement cycles (default: 1)"
      echo "  --eval-only    Only run evaluation, skip improvement step"
      echo "  --app-name X   Agent app name for filtering (default: company_info_agent)"
      echo "  -h, --help     Show this help message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

mkdir -p "$REPORTS_DIR"

echo ""
echo "============================================"
echo "  Agent Improvement Cycle"
echo "============================================"
echo "  Cycles to run: $CYCLES"
echo "  Agent app:     $APP_NAME"
echo "  Reports dir:   $REPORTS_DIR"
echo "============================================"
echo ""

for cycle in $(seq 1 "$CYCLES"); do
  echo ""
  echo "--------------------------------------------"
  echo "  Cycle $cycle of $CYCLES"
  echo "--------------------------------------------"

  # Step 1: Run eval cases against the agent
  echo ""
  echo "[Step 1/$( $EVAL_ONLY && echo 2 || echo 3 )] Running eval cases..."
  python3 "$SCRIPT_DIR/eval/run_eval.py"

  # Step 2: Wait briefly for BigQuery writes to propagate, then run quality report
  echo ""
  echo "[Step 2/$( $EVAL_ONLY && echo 2 || echo 3 )] Running quality evaluation..."
  sleep 5  # Allow BigQuery writes to settle

  REPORT_JSON="$REPORTS_DIR/quality_report_cycle_${cycle}.json"
  python3 "$REPO_ROOT/src/scripts/quality_report.py" \
    --app-name "$APP_NAME" \
    --output-json "$REPORT_JSON" \
    --report \
    --limit 50 \
    --time-period 24h

  if [[ ! -f "$REPORT_JSON" ]]; then
    echo "ERROR: Quality report was not generated at $REPORT_JSON" >&2
    exit 1
  fi

  # Print quality summary
  echo ""
  echo "  Quality report saved: $REPORT_JSON"
  if command -v python3 &>/dev/null; then
    python3 -c "
import json, sys
with open('$REPORT_JSON') as f:
    data = json.load(f)
s = data.get('summary', {})
print(f\"  Score: {s.get('meaningful_rate', '?')}% meaningful \")
print(f\"  ({s.get('meaningful', '?')} meaningful, {s.get('partial', '?')} partial, {s.get('unhelpful', '?')} unhelpful)\")
"
  fi

  # Step 3: Run the improver (unless --eval-only)
  if [[ "$EVAL_ONLY" == "true" ]]; then
    echo ""
    echo "  --eval-only: skipping improvement step."
  else
    echo ""
    echo "[Step 3/3] Running agent improver..."
    python3 "$SCRIPT_DIR/improver/improve_agent.py" "$REPORT_JSON"
  fi

  echo ""
  echo "  Cycle $cycle complete."
done

echo ""
echo "============================================"
echo "  All $CYCLES cycle(s) finished."
echo "============================================"
echo ""
echo "Artifacts in $REPORTS_DIR:"
ls -1 "$REPORTS_DIR"/ 2>/dev/null || echo "  (none)"
echo ""
echo "Current prompt version:"
python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from agent.prompts import CURRENT_VERSION
print(f'  v{CURRENT_VERSION}')
"
echo ""
