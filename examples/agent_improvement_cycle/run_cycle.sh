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
# This script runs a closed-loop improvement cycle for an ADK agent:
#
#   STEP 1: GENERATE SYNTHETIC TRAFFIC (generate_traffic.py)
#       Calls Gemini to produce diverse, realistic user questions that
#       differ from the golden eval set.  These simulate the kind of
#       questions real users would ask in production.
#
#   STEP 2: RUN SYNTHETIC TRAFFIC (run_eval.py)
#       Sends the generated questions to the agent via ADK InMemoryRunner.
#       Each session is automatically logged to BigQuery via the
#       BigQueryAgentAnalyticsPlugin.
#
#   STEP 3: EVALUATE SESSION QUALITY (quality_report.py from the SDK)
#       Reads the sessions from BigQuery and scores each one on
#       response usefulness and task grounding.
#
#   STEP 4: AUTO-IMPROVE THE PROMPT (improve_agent.py)
#       Reads the quality report and calls Gemini to generate a better
#       prompt.  Before accepting, it runs the golden eval set against
#       the candidate prompt.  If any golden case regresses, the
#       candidate is rejected and retried.  Failed synthetic cases are
#       extracted and added to the golden eval set so the same failures
#       are caught in future cycles.
#
# The hero moment: run 3 cycles and watch quality typically climb from
# ~30% to ~90%+ (results vary due to non-deterministic LLM output).
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
TRAFFIC_COUNT=15

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
      echo "  --eval-only        Only run evaluation (Steps 1-3), skip prompt improvement"
      echo "  --app-name X       Agent app name for filtering (default: company_info_agent)"
      echo "  --traffic-count N  Number of synthetic questions per cycle (default: 15)"
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

echo ""
echo "============================================"
echo "  Agent Improvement Cycle"
echo "============================================"
echo "  Cycles to run:    $CYCLES"
echo "  Agent app:        $APP_NAME"
echo "  Traffic per cycle: $TRAFFIC_COUNT questions"
echo "  Reports dir:      $REPORTS_DIR"
echo "============================================"
echo ""

for cycle in $(seq 1 "$CYCLES"); do
  TOTAL_STEPS=$( $EVAL_ONLY && echo 3 || echo 4 )

  echo ""
  echo "--------------------------------------------"
  echo "  Cycle $cycle of $CYCLES"
  echo "--------------------------------------------"

  # -----------------------------------------------------------------------
  # STEP 1: Generate synthetic traffic
  #
  # Calls Gemini to produce diverse user questions.  The generated
  # questions are intentionally different from the golden eval set so
  # they simulate real-world traffic the agent has not been tuned for.
  # -----------------------------------------------------------------------
  echo ""
  echo "[Step 1/$TOTAL_STEPS] Generating synthetic user traffic..."
  TRAFFIC_JSON="$REPORTS_DIR/synthetic_traffic_cycle_${cycle}.json"
  python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/generate_traffic.py" \
    --count "$TRAFFIC_COUNT" \
    --output "$TRAFFIC_JSON"

  # -----------------------------------------------------------------------
  # STEP 2: Run synthetic traffic through the agent
  #
  # Sends the generated questions to the agent via ADK InMemoryRunner.
  # Each session is logged to BigQuery via BigQueryAgentAnalyticsPlugin.
  # -----------------------------------------------------------------------
  echo ""
  echo "[Step 2/$TOTAL_STEPS] Running synthetic traffic through agent -> BigQuery..."
  python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/run_eval.py" \
    --eval-cases "$TRAFFIC_JSON"

  # -----------------------------------------------------------------------
  # STEP 3: Evaluate session quality
  #
  # The SDK's quality_report.py reads the sessions from BigQuery and
  # scores each one:
  #   - response_usefulness: meaningful / partial / unhelpful
  #   - task_grounding: grounded / ungrounded / no_tool_needed
  # -----------------------------------------------------------------------
  echo ""
  echo "[Step 3/$TOTAL_STEPS] Evaluating session quality..."
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

  # -----------------------------------------------------------------------
  # STEP 4: Auto-improve the agent prompt
  #
  # improve_agent.py:
  #   1. Reads the quality report (which sessions failed and why)
  #   2. Reads the current prompt from agent/prompts.py
  #   3. Calls Gemini to generate an improved prompt
  #   4. Runs the GOLDEN eval set against the candidate prompt
  #      (regression gate -- no BQ logging, throwaway agent).
  #      If any golden case regresses, the candidate is rejected
  #      and a new one is generated (up to 3 attempts).
  #   5. Writes the validated PROMPT_V{N+1} to prompts.py
  #   6. Extracts failed synthetic cases and adds them to the golden
  #      eval set (eval_cases.json) so regressions are caught in
  #      future cycles.
  # -----------------------------------------------------------------------
  if [[ "$EVAL_ONLY" == "true" ]]; then
    echo ""
    echo "  --eval-only: skipping improvement step."
  else
    echo ""
    echo "[Step 4/$TOTAL_STEPS] Auto-improving agent prompt..."
    echo "  Gemini analyzes failures, rewrites the prompt, validates against golden eval."
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
echo "To inspect what changed:"
echo "  git diff $SCRIPT_DIR/agent/prompts.py       # see prompt changes"
echo "  git diff $SCRIPT_DIR/eval/eval_cases.json    # see added eval cases"
echo "  cat $REPORTS_DIR/quality_report_cycle_*.json | python3 -m json.tool | head -20"
echo ""
