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
#   STEP 1: SIMULATE USER TRAFFIC (run_eval.py)
#       Sends test questions from eval_cases.json to the agent.
#       Each question creates a real agent session (tool calls, LLM responses)
#       that gets automatically logged to BigQuery via BigQueryAgentAnalyticsPlugin.
#
#       Think of this as "synthetic user traffic." In production, real users
#       would generate these sessions naturally. For the demo, we use eval
#       cases to simulate that traffic so we have sessions to analyze.
#
#   STEP 2: EVALUATE SESSION QUALITY (quality_report.py from the SDK)
#       Reads the sessions that were just logged to BigQuery and evaluates
#       each one: Was the agent's response actually helpful? Was it grounded
#       in tool output or did the agent hallucinate?
#
#       The SDK's quality_report.py handles this. It uses --app-name to filter
#       to sessions from our agent only, and --output-json to produce a
#       structured report that the improver can consume programmatically.
#
#   STEP 3: AUTO-IMPROVE THE PROMPT (improve_agent.py)
#       Reads the quality report JSON and sends it to Gemini along with the
#       current agent prompt. Gemini analyzes which sessions failed and why,
#       then generates:
#         - An improved prompt that fixes the identified issues
#         - New eval cases that specifically test those fixes
#
#       The improved prompt is written to agent/prompts.py as PROMPT_V{N+1},
#       and CURRENT_PROMPT is updated to point to it. New eval cases are
#       appended to eval/eval_cases.json so the same failures are caught
#       in future cycles.
#
# The hero moment: run 3 cycles and watch quality climb from ~30% to ~90%+.
#
# Usage:
#   ./run_cycle.sh              # Run one improvement cycle
#   ./run_cycle.sh --cycles 3   # Run 3 consecutive cycles
#   ./run_cycle.sh --eval-only  # Only run Steps 1-2, skip improvement
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
      echo "  --eval-only    Only run evaluation (Steps 1-2), skip prompt improvement"
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
  TOTAL_STEPS=$( $EVAL_ONLY && echo 2 || echo 3 )

  echo ""
  echo "--------------------------------------------"
  echo "  Cycle $cycle of $CYCLES"
  echo "--------------------------------------------"

  # -----------------------------------------------------------------------
  # STEP 1: Simulate user traffic
  #
  # Sends each question from eval_cases.json to the agent. The agent
  # processes them as real requests (calling tools, generating responses).
  # Every session is automatically logged to BigQuery via the
  # BigQueryAgentAnalyticsPlugin attached to the agent.
  #
  # In production, real users generate sessions naturally. Here we use
  # eval cases as synthetic traffic so we have data to evaluate.
  # -----------------------------------------------------------------------
  echo ""
  echo "[Step 1/$TOTAL_STEPS] Simulating user traffic (eval_cases.json -> agent -> BigQuery)..."
  echo "  Sending test questions to the agent. Each creates a session logged to BigQuery."
  python3 -W ignore::UserWarning "$SCRIPT_DIR/eval/run_eval.py"

  # -----------------------------------------------------------------------
  # STEP 2: Evaluate session quality
  #
  # Calls the SDK's quality_report.py to read sessions from BigQuery and
  # score each one:
  #   - response_usefulness: meaningful / partial / unhelpful
  #   - task_grounding: grounded / ungrounded / no_tool_needed
  #
  # Flags:
  #   --app-name   Filter to sessions from this agent only (ignores other
  #                agents sharing the same BigQuery dataset)
  #   --output-json  Structured JSON for the improver to consume
  #   --limit 15     Keeps scores focused on this cycle's sessions
  #   --time-period 24h  Only look at recent sessions
  # -----------------------------------------------------------------------
  echo ""
  echo "[Step 2/$TOTAL_STEPS] Evaluating session quality (BigQuery -> SDK quality_report.py)..."
  echo "  Reading logged sessions from BigQuery, scoring each with the SDK."
  sleep 5  # Allow BigQuery writes to propagate

  REPORT_JSON="$REPORTS_DIR/quality_report_cycle_${cycle}.json"
  python3 "$REPO_ROOT/scripts/quality_report.py" \
    --app-name "$APP_NAME" \
    --output-json "$REPORT_JSON" \
    --limit 15 \
    --time-period 24h || true

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

  # -----------------------------------------------------------------------
  # STEP 3: Auto-improve the agent prompt
  #
  # improve_agent.py reads the quality report JSON and calls Gemini to
  # generate a better prompt. Specifically it:
  #
  #   1. Reads the quality report (which sessions failed and why)
  #   2. Reads the current prompt from agent/prompts.py
  #   3. Sends both to Gemini, asking it to fix the identified issues
  #   4. Gemini returns JSON with:
  #      - improved_prompt: the full new prompt text
  #      - changes_summary: what changed and why
  #      - new_eval_cases: test questions for the issues it fixed
  #   5. The script writes PROMPT_V{N+1} to agent/prompts.py and updates
  #      CURRENT_PROMPT to point to it
  #   6. New eval cases are appended to eval/eval_cases.json
  #
  # On the next cycle, the agent uses the improved prompt, and the new
  # eval cases verify the fixes hold.
  # -----------------------------------------------------------------------
  if [[ "$EVAL_ONLY" == "true" ]]; then
    echo ""
    echo "  --eval-only: skipping improvement step."
  else
    echo ""
    echo "[Step 3/3] Auto-improving agent prompt (quality report -> Gemini -> prompts.py)..."
    echo "  Gemini analyzes failures, rewrites the prompt, and adds new eval cases."
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
