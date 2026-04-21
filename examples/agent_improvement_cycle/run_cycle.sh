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

# ============================================================================
# Agent Improvement Cycle Orchestrator
# ============================================================================
#
# Runs a closed-loop improvement cycle for an ADK agent.  Each cycle:
#
#   Step 1  Generate synthetic traffic  (Gemini produces diverse questions)
#   Step 2  Run traffic through agent   (sessions logged to BigQuery)
#   Step 3  Evaluate session quality    (SDK quality report from BigQuery)
#   Step 4  Improve the prompt          (Gemini rewrites, golden eval gate)
#   Step 5  Measure improvement         (fresh traffic + LLM judge)
#
# Usage:
#   ./run_cycle.sh              # Run one improvement cycle
#   ./run_cycle.sh --cycles 3   # Run 3 consecutive cycles
#   ./run_cycle.sh --eval-only  # Only run Steps 1-3, skip improvement
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPORTS_DIR="$SCRIPT_DIR/reports"

# Load .env from the demo directory so all scripts see the same config
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Defaults
CYCLES=1
EVAL_ONLY=false
APP_NAME="company_info_agent"
TRAFFIC_COUNT=10

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
    --traffic-count)
      TRAFFIC_COUNT="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --cycles N         Run N improvement cycles (default: 1)"
      echo "  --eval-only        Only run evaluation (Steps 1-3), skip improvement"
      echo "  --app-name X       Agent app name for filtering (default: company_info_agent)"
      echo "  --traffic-count N  Number of synthetic questions per cycle (default: 10)"
      echo "  -h, --help         Show this help message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

mkdir -p "$REPORTS_DIR"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Timer: call step_start before a step, step_end after.
step_start() { STEP_START_TIME=$(date +%s); }
step_end() {
  local elapsed=$(( $(date +%s) - STEP_START_TIME ))
  echo "  (${elapsed}s)"
}

separator() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

separator
echo ""
echo "  AGENT IMPROVEMENT CYCLE"
echo ""
echo "  Cycles:     $CYCLES"
echo "  Agent:      $APP_NAME"
echo "  Traffic:    $TRAFFIC_COUNT questions per cycle"
CYCLE_START_TIME=$(date +%s)

# ---------------------------------------------------------------------------
# Pre-flight: verify golden eval passes with current prompt
# ---------------------------------------------------------------------------

separator
echo ""
echo "  PRE-FLIGHT: Verifying golden eval set passes with current prompt"
echo ""
step_start

python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/run_eval.py" --golden

PREFLIGHT_EXIT=$?
step_end

if [[ $PREFLIGHT_EXIT -ne 0 ]]; then
  echo ""
  echo "  ERROR: Pre-flight golden eval failed. Fix the prompt before running the cycle."
  exit 1
fi

for cycle in $(seq 1 "$CYCLES"); do
  TOTAL_STEPS=$( $EVAL_ONLY && echo 3 || echo 5 )

  separator
  echo ""
  echo "  CYCLE $cycle OF $CYCLES"

  # Get current prompt version
  CURRENT_V=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from agent.prompts import CURRENT_VERSION
print(CURRENT_VERSION)
")

  # =========================================================================
  # STEP 1: Generate synthetic traffic
  # =========================================================================
  separator
  echo ""
  echo "  STEP 1/$TOTAL_STEPS: GENERATE SYNTHETIC TRAFFIC"
  echo ""
  echo "  Goal:    Produce diverse user questions that differ from the golden eval set"
  echo "  Method:  Gemini generates $TRAFFIC_COUNT realistic employee questions"
  echo "  Output:  eval/synthetic_traffic_cycle_${cycle}.json"
  echo ""
  step_start

  TRAFFIC_JSON="$SCRIPT_DIR/eval/synthetic_traffic_cycle_${cycle}.json"
  python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/generate_traffic.py" \
    --count "$TRAFFIC_COUNT" \
    --output "$TRAFFIC_JSON"

  step_end

  # =========================================================================
  # STEP 2: Run synthetic traffic through the agent
  # =========================================================================
  separator
  echo ""
  echo "  STEP 2/$TOTAL_STEPS: RUN TRAFFIC THROUGH AGENT"
  echo ""
  echo "  Goal:    Send questions to the agent, log every session to BigQuery"
  echo "  Prompt:  V${CURRENT_V} (current)"
  echo "  Input:   eval/synthetic_traffic_cycle_${cycle}.json"
  echo "  Logging: BigQuery via BigQueryAgentAnalyticsPlugin"
  echo ""
  step_start

  python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/run_eval.py" \
    --eval-cases "$TRAFFIC_JSON"

  step_end

  # =========================================================================
  # STEP 3: Evaluate session quality
  # =========================================================================
  separator
  echo ""
  echo "  STEP 3/$TOTAL_STEPS: EVALUATE SESSION QUALITY"
  echo ""
  echo "  Goal:    Score each logged session from BigQuery"
  echo "  Method:  SDK quality_report.py reads sessions, LLM judges each one"
  echo "  Metrics: response_usefulness (meaningful/partial/unhelpful)"
  echo "           task_grounding (grounded/ungrounded)"
  echo "  Output:  reports/quality_report_cycle_${cycle}.json"
  echo ""
  step_start

  REPORT_JSON="$REPORTS_DIR/quality_report_cycle_${cycle}.json"

  # Retry with backoff for BigQuery write propagation
  MAX_RETRIES=6
  for attempt in $(seq 1 "$MAX_RETRIES"); do
    sleep 5
    python3 "$REPO_ROOT/scripts/quality_report.py" \
      --app-name "$APP_NAME" \
      --output-json "$REPORT_JSON" \
      --limit "$TRAFFIC_COUNT" \
      --time-period 24h && break || true

    if [[ $attempt -lt $MAX_RETRIES ]]; then
      echo "  Quality report attempt $attempt/$MAX_RETRIES failed, retrying in 10s..."
      sleep 10
    fi
  done

  if [[ ! -f "$REPORT_JSON" ]]; then
    echo "ERROR: Quality report was not generated after $MAX_RETRIES attempts" >&2
    exit 1
  fi

  # Print quality summary
  python3 -c "
import json
with open('$REPORT_JSON') as f:
    data = json.load(f)
s = data.get('summary', {})
print()
print(f\"  BASELINE SCORE (V${CURRENT_V}): {s.get('meaningful_rate', '?')}% meaningful\")
print(f\"  ({s.get('meaningful', '?')} meaningful, {s.get('partial', '?')} partial, {s.get('unhelpful', '?')} unhelpful out of {s.get('total_sessions', '?')})\")
"

  step_end

  # =========================================================================
  # STEP 4: Auto-improve the agent prompt
  # =========================================================================
  if [[ "$EVAL_ONLY" == "true" ]]; then
    separator
    echo ""
    echo "  --eval-only: skipping Steps 4-5 (improvement and measurement)."
    echo ""
    echo "  Cycle $cycle complete."
    continue
  fi

  # =========================================================================
  # STEP 4: Auto-improve the agent prompt
  # =========================================================================
  separator
  echo ""
  echo "  STEP 4/$TOTAL_STEPS: IMPROVE PROMPT"
  echo ""
  echo "  Goal:    Fix the prompt to address failed sessions"
  echo "  Method:  1. Gemini analyzes failures and rewrites the prompt"
  echo "           2. Golden eval gate: run all golden cases against the"
  echo "              candidate. If any regresses, reject and retry."
  echo "           3. Extract failed synthetic cases into the golden eval set"
  echo "  Input:   reports/quality_report_cycle_${cycle}.json"
  echo "  Output:  agent/prompts.py (new version)"
  echo "           eval/eval_cases.json (extended with failed cases)"
  echo ""
  step_start

  GOLDEN_BEFORE=$(python3 -c "
import json
with open('$SCRIPT_DIR/eval/eval_cases.json') as f:
    print(len(json.load(f)['eval_cases']))
")

  python3 -W ignore::UserWarning "$SCRIPT_DIR/improver/improve_agent.py" \
    "$REPORT_JSON"

  NEW_V=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import importlib, agent.prompts
importlib.reload(agent.prompts)
print(agent.prompts.CURRENT_VERSION)
")

  GOLDEN_AFTER=$(python3 -c "
import json
with open('$SCRIPT_DIR/eval/eval_cases.json') as f:
    print(len(json.load(f)['eval_cases']))
")

  echo ""
  echo "  Prompt:      V${CURRENT_V} -> V${NEW_V}"
  echo "  Golden set:  $GOLDEN_BEFORE -> $GOLDEN_AFTER cases"

  step_end

  # =========================================================================
  # STEP 5: Measure improvement with fresh traffic
  # =========================================================================
  separator
  echo ""
  echo "  STEP 5/$TOTAL_STEPS: MEASURE IMPROVEMENT"
  echo ""
  echo "  Goal:    Test the improved prompt against fresh, unseen questions"
  echo "  Method:  Generate NEW synthetic traffic (different from Step 1),"
  echo "           run it through the improved V${NEW_V} prompt, and score"
  echo "           each response with an LLM judge."
  echo "  Why:     The Step 1 traffic was used to identify failures --"
  echo "           re-running it would be circular. Fresh questions test"
  echo "           whether the improvement generalizes."
  echo ""
  step_start

  FRESH_TRAFFIC="$SCRIPT_DIR/eval/synthetic_traffic_cycle_${cycle}_fresh.json"
  python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/generate_traffic.py" \
    --count "$TRAFFIC_COUNT" \
    --output "$FRESH_TRAFFIC"

  python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/run_eval.py" \
    --golden \
    --eval-cases "$FRESH_TRAFFIC"

  # Print before/after comparison
  FRESH_RESULTS="$SCRIPT_DIR/reports/latest_eval_results.json"
  python3 -c "
import json
with open('$REPORT_JSON') as f:
    before = json.load(f)
with open('$FRESH_RESULTS') as f:
    after_results = json.load(f)
b = before.get('summary', {})
after_passed = sum(1 for r in after_results if r.get('pass', False))
after_total = len(after_results)
after_rate = round(100 * after_passed / after_total) if after_total else 0
print()
print('  ┌───────────────────────────────────────────────────────┐')
print('  │              CYCLE ${cycle} RESULTS                          │')
print('  ├───────────────────────────────────────────────────────┤')
print(f\"  │  Before (V${CURRENT_V}):  {b.get('meaningful_rate', '?'):>3}% meaningful  ({b.get('meaningful', '?')}/{b.get('total_sessions', '?')} sessions)  │\")
print(f\"  │  After  (V${NEW_V}):  {after_rate:>3}% pass rate    ({after_passed}/{after_total} sessions)  │\")
print('  └───────────────────────────────────────────────────────┘')
" 2>/dev/null || true

  step_end

  echo ""
  echo "  Cycle $cycle complete."
done

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

TOTAL_ELAPSED=$(( $(date +%s) - CYCLE_START_TIME ))
FINAL_V=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import importlib, agent.prompts
importlib.reload(agent.prompts)
print(agent.prompts.CURRENT_VERSION)
")
FINAL_GOLDEN=$(python3 -c "
import json
with open('$SCRIPT_DIR/eval/eval_cases.json') as f:
    print(len(json.load(f)['eval_cases']))
")

separator
echo ""
echo "  DONE  ($CYCLES cycle(s) in ${TOTAL_ELAPSED}s)"
echo ""
echo "  Prompt version:   V${FINAL_V}"
echo "  Golden eval set:  $FINAL_GOLDEN cases"
echo ""
echo "  Artifacts:"
ls -1 "$REPORTS_DIR"/ 2>/dev/null | sed 's/^/    /' || echo "    (none)"
echo ""
echo "  Inspect changes:"
echo "    git diff agent/prompts.py       # prompt evolution"
echo "    git diff eval/eval_cases.json   # new regression cases"
echo "    cat agent/prompts.py            # all prompt versions"
echo ""
echo "  Reset to V1:"
echo "    ./reset.sh"
separator
echo ""
