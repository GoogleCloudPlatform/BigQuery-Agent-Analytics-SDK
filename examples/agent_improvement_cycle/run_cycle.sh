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
# Runs a closed-loop improvement cycle for any ADK agent.  Each cycle:
#
#   Step 1  Generate synthetic traffic  (Gemini produces diverse questions)
#   Step 2  Run traffic through agent   (sessions logged to BigQuery)
#   Step 3  Evaluate session quality    (SDK quality report from BigQuery)
#   Step 4  Improve the prompt          (Gemini rewrites, golden eval gate)
#   Step 5  Measure improvement         (fresh traffic + LLM judge)
#
# Usage:
#   ./run_cycle.sh                                          # Demo agent
#   ./run_cycle.sh --agent-config /path/to/config.json      # Any agent
#   ./run_cycle.sh --cycles 3                               # 3 cycles
#   ./run_cycle.sh --eval-only                              # Steps 1-3 only
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPORTS_DIR="$SCRIPT_DIR/reports"

# Suppress noisy Python warnings (authlib, etc.) and INFO-level log spam.
# Belt-and-suspenders: env var for child processes, -W flag for direct calls.
export PYTHONWARNINGS="ignore"
export LOGLEVEL="${LOGLEVEL:-WARNING}"
PY="python3 -W ignore"

# Load .env from the demo directory so all scripts see the same config
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Defaults
CYCLES=1
EVAL_ONLY=false
TRAFFIC_COUNT=10
AGENT_CONFIG=""

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
    --agent-config)
      AGENT_CONFIG="$2"
      shift 2
      ;;
    --app-name)
      # Legacy flag, overrides config's app_name
      APP_NAME_OVERRIDE="$2"
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
      echo "  --agent-config F   Path to agent's config.json"
      echo "                     (default: config.json)"
      echo "  --cycles N         Run N improvement cycles (default: 1)"
      echo "  --eval-only        Only run evaluation (Steps 1-3), skip improvement"
      echo "  --app-name X       Override agent app name for BQ filtering"
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
# Load agent config from JSON
# ---------------------------------------------------------------------------

# Resolve config path
if [[ -z "$AGENT_CONFIG" ]]; then
  AGENT_CONFIG="$SCRIPT_DIR/config.json"
fi
AGENT_CONFIG="$(cd "$(dirname "$AGENT_CONFIG")" && pwd)/$(basename "$AGENT_CONFIG")"

# Agent root = config's parent directory
AGENT_ROOT="$(dirname "$AGENT_CONFIG")"

# Read metadata with jq
APP_NAME="${APP_NAME_OVERRIDE:-$(jq -r '.app_name' "$AGENT_CONFIG")}"
PROMPTS_PATH="$AGENT_ROOT/$(jq -r '.prompts_path' "$AGENT_CONFIG")"
EVAL_CASES_PATH="$AGENT_ROOT/$(jq -r '.eval_cases_path' "$AGENT_CONFIG")"
TRAFFIC_GENERATOR="$AGENT_ROOT/$(jq -r '.traffic_generator' "$AGENT_CONFIG")"
VERSION_VAR=$(jq -r '.version_variable // "CURRENT_VERSION"' "$AGENT_CONFIG")
PROMPT_STORAGE=$(jq -r '.prompt_storage // "python_file"' "$AGENT_CONFIG")
VERTEX_PROMPT_ID=$(jq -r '.vertex_prompt_id // ""' "$AGENT_CONFIG")
VERTEX_LOCATION=$(jq -r '.vertex_location // "us-central1"' "$AGENT_CONFIG")

# Auto-setup: create Vertex AI prompt if not yet configured
if [[ "$PROMPT_STORAGE" == "vertex" && -z "$VERTEX_PROMPT_ID" ]]; then
  echo ""
  echo "  No Vertex AI prompt configured. Running setup..."
  $PY "$SCRIPT_DIR/setup_vertex.py"
  # Re-read the prompt ID after setup
  VERTEX_PROMPT_ID=$(jq -r '.vertex_prompt_id // ""' "$AGENT_CONFIG")
fi

# Build the --agent-config flag for Python scripts
AGENT_CONFIG_FLAG="--agent-config $AGENT_CONFIG"

# Helper: read current prompt version
_read_version() {
  if [[ "$PROMPT_STORAGE" == "vertex" && -n "$VERTEX_PROMPT_ID" ]]; then
    python3 -c "
from vertexai import Client
c = Client(location='$VERTEX_LOCATION')
vs = list(c.prompts.list_versions(prompt_id='$VERTEX_PROMPT_ID'))
print(len(vs) + 1)
"
  else
    grep -oP "${VERSION_VAR}\s*=\s*\K\d+" "$PROMPTS_PATH"
  fi
}

# Helper: display the current prompt text and version
_show_prompt() {
  local label="${1:-Current prompt}"
  echo ""
  echo "  ${label}:"
  echo ""
  if [[ "$PROMPT_STORAGE" == "vertex" && -n "$VERTEX_PROMPT_ID" ]]; then
    "$SCRIPT_DIR/show_prompt.sh" "$VERTEX_PROMPT_ID"
  else
    local v
    v=$(grep -oP "${VERSION_VAR}\s*=\s*\K\d+" "$PROMPTS_PATH")
    echo "  Version: v${v}"
    echo "  File: $PROMPTS_PATH"
    echo ""
  fi
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Timer: call step_start before a step, step_end after.
step_start() { STEP_START_TIME=$(date +%s); }
step_end() {
  local elapsed=$(( $(date +%s) - STEP_START_TIME ))
  local label="${1:-Step}"
  echo ""
  echo "  Done. ${label} completed in ${elapsed}s."
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
echo "  Config:     $AGENT_CONFIG"
echo "  Storage:    $PROMPT_STORAGE"
if [[ "$PROMPT_STORAGE" == "vertex" && -n "$VERTEX_PROMPT_ID" ]]; then
  echo "  Prompt ID:  $VERTEX_PROMPT_ID"
fi
echo "  Traffic:    $TRAFFIC_COUNT questions per cycle"
CYCLE_START_TIME=$(date +%s)

separator
echo ""
_show_prompt "STARTING PROMPT"

# ---------------------------------------------------------------------------
# Pre-flight: verify golden eval passes with current prompt
# ---------------------------------------------------------------------------

separator
echo ""
echo "  PRE-FLIGHT: Verifying golden eval set passes with current prompt"
echo ""
step_start

set +e
$PY "$SCRIPT_DIR/eval/run_eval.py" --golden $AGENT_CONFIG_FLAG
PREFLIGHT_EXIT=$?
set -e
step_end "Pre-flight check"

if [[ $PREFLIGHT_EXIT -ne 0 ]]; then
  FAILING_V=$(_read_version)
  echo ""
  echo "  PRE-FLIGHT FAILED: Prompt V${FAILING_V} does not pass all golden cases."
  echo ""

  # Surface exactly which cases failed
  EVAL_RESULTS="$REPORTS_DIR/latest_eval_results.json"
  if [[ -f "$EVAL_RESULTS" ]]; then
    echo "  Failing cases:"
    python3 -c "
import json, sys
with open('$EVAL_RESULTS') as f:
    for r in json.load(f):
        if not r.get('pass', False):
            print(f\"    FAIL: {r.get('case_id','?')} - {r.get('reason','no reason')}\")
" 2>/dev/null || true
    echo ""
  fi

  echo "  Auto-improving prompt to pass golden eval set..."
  echo ""

  $PY "$SCRIPT_DIR/run_improvement.py" \
    $AGENT_CONFIG_FLAG \
    --from-eval-results "$EVAL_RESULTS"

  FIXED_V=$(_read_version)
  echo ""
  echo "  Pre-flight fix: V${FAILING_V} -> V${FIXED_V}"
  # The improvement step's test_candidate already validated all golden
  # cases pass before writing the prompt.
fi

for cycle in $(seq 1 "$CYCLES"); do
  TOTAL_STEPS=$( $EVAL_ONLY && echo 3 || echo 5 )

  separator
  echo ""
  echo "  CYCLE $cycle OF $CYCLES"

  # Get current prompt version
  CURRENT_V=$(_read_version)

  # =========================================================================
  # STEP 1: Generate synthetic traffic
  # =========================================================================
  separator
  echo ""
  echo "  STEP 1/$TOTAL_STEPS: GENERATE SYNTHETIC TRAFFIC"
  echo ""
  echo "  Goal:    Produce diverse user questions that differ from the golden eval set"
  echo "  Method:  Gemini generates $TRAFFIC_COUNT questions"
  echo ""
  step_start

  TRAFFIC_JSON="$REPORTS_DIR/synthetic_traffic_cycle_${cycle}.json"
  $PY "$TRAFFIC_GENERATOR" \
    --count "$TRAFFIC_COUNT" \
    --output "$TRAFFIC_JSON"

  # Count actual cases (may be fewer than requested after dedup)
  ACTUAL_TRAFFIC_COUNT=$(jq '.eval_cases | length' "$TRAFFIC_JSON")

  echo ""
  echo "  Generated questions saved to: $TRAFFIC_JSON"
  echo "  Requested: $TRAFFIC_COUNT, Generated: $ACTUAL_TRAFFIC_COUNT (after dedup)"
  echo "  Sample questions:"
  jq -r '.eval_cases[:3][] | "    - \(.question)"' "$TRAFFIC_JSON" 2>/dev/null || true

  step_end "Traffic generation"

  # =========================================================================
  # STEP 2: Run synthetic traffic through the agent
  # =========================================================================
  separator
  echo ""
  echo "  STEP 2/$TOTAL_STEPS: RUN TRAFFIC THROUGH AGENT"
  echo ""
  echo "  Goal:    Send questions to the agent, log every session to BigQuery"
  echo "  Prompt:  V${CURRENT_V} (current)"
  echo "  Logging: BigQuery via BigQueryAgentAnalyticsPlugin"
  echo ""
  step_start

  $PY "$SCRIPT_DIR/eval/run_eval.py" \
    $AGENT_CONFIG_FLAG \
    --eval-cases "$TRAFFIC_JSON"

  step_end "Agent execution"

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
  echo ""
  step_start

  REPORT_JSON="$REPORTS_DIR/quality_report_cycle_${cycle}.json"
  rm -f "$REPORT_JSON"

  # Retry with backoff for BigQuery streaming buffer propagation.
  echo "  Waiting 15s for BigQuery streaming buffer to flush..."
  echo ""
  echo "  While we wait, here are the questions that were sent to the agent:"
  jq -r '.eval_cases[] | "    [\(.id)] \(.question)"' "$TRAFFIC_JSON" 2>/dev/null || true
  echo ""
  MAX_RETRIES=6
  for attempt in $(seq 1 "$MAX_RETRIES"); do
    sleep 15
    echo "  Querying BigQuery and scoring sessions with LLM judge..."
    $PY "$REPO_ROOT/scripts/quality_report.py" \
      --app-name "$APP_NAME" \
      --output-json "$REPORT_JSON" \
      --limit "$ACTUAL_TRAFFIC_COUNT" \
      --time-period 24h || { rm -f "$REPORT_JSON"; true; }

    if [[ -f "$REPORT_JSON" ]]; then
      SESSION_COUNT=$(jq -r '.summary.total_sessions // 0' "$REPORT_JSON" 2>/dev/null || echo "0")
      if [[ "$SESSION_COUNT" -gt 0 ]]; then
        break
      fi
      echo "  No sessions found yet (attempt $attempt/$MAX_RETRIES), retrying in 15s..."
      rm -f "$REPORT_JSON"
    fi

    if [[ $attempt -lt $MAX_RETRIES ]]; then
      sleep 15
    fi
  done

  if [[ ! -f "$REPORT_JSON" ]]; then
    echo "ERROR: Quality report was not generated after $MAX_RETRIES attempts" >&2
    exit 1
  fi

  # Print quality summary
  echo ""
  echo "  BASELINE SCORE (V${CURRENT_V}): $(jq -r '.summary.meaningful_rate' "$REPORT_JSON")% meaningful"
  echo "  ($(jq -r '.summary.meaningful' "$REPORT_JSON") meaningful, $(jq -r '.summary.partial' "$REPORT_JSON") partial, $(jq -r '.summary.unhelpful' "$REPORT_JSON") unhelpful out of $(jq -r '.summary.total_sessions' "$REPORT_JSON"))"
  echo ""
  echo "  Report saved to: $REPORT_JSON"

  step_end "Quality evaluation"

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

  separator
  echo ""
  echo "  STEP 4/$TOTAL_STEPS: IMPROVE PROMPT"
  echo ""
  echo "  Goal:    Fix the prompt to address failed sessions"
  echo "  Method:  1. Extract failed cases into golden eval set"
  echo "           2. Gemini generates improved prompt"
  echo "           3. Regression gate: candidate must pass ALL golden"
  echo "              cases (original + extracted). Retry if any fail."
  echo ""
  step_start

  GOLDEN_BEFORE=$(jq '.eval_cases | length' "$EVAL_CASES_PATH")

  set +e
  $PY "$SCRIPT_DIR/run_improvement.py" \
    $AGENT_CONFIG_FLAG \
    "$REPORT_JSON"
  IMPROVE_EXIT=$?
  set -e

  if [[ $IMPROVE_EXIT -ne 0 ]]; then
    echo ""
    echo "  WARNING: Improvement step did not produce a new prompt version."
    echo "  Continuing with current prompt."
  fi

  NEW_V=$(_read_version)
  GOLDEN_AFTER=$(jq '.eval_cases | length' "$EVAL_CASES_PATH")

  echo ""
  echo "  Prompt:      V${CURRENT_V} -> V${NEW_V}"
  echo "  Golden set:  $GOLDEN_BEFORE -> $GOLDEN_AFTER cases"

  step_end "Prompt improvement"

  # =========================================================================
  # STEP 5: Measure improvement with fresh traffic
  # =========================================================================
  separator
  echo ""
  echo "  STEP 5/$TOTAL_STEPS: MEASURE IMPROVEMENT"
  echo ""
  echo "  Goal:    Measure quality on fresh, unseen traffic via BigQuery"
  echo "  Method:  1. Generate fresh synthetic traffic (different from Step 1)"
  echo "           2. Run through agent with BigQuery logging"
  echo "           3. Score sessions from BigQuery (same as Step 3)"
  echo ""
  step_start

  # 5a: Generate fresh synthetic traffic
  echo "  --- Fresh traffic ---"
  FRESH_TRAFFIC="$REPORTS_DIR/synthetic_traffic_cycle_${cycle}_fresh.json"
  $PY "$TRAFFIC_GENERATOR" \
    --count "$TRAFFIC_COUNT" \
    --output "$FRESH_TRAFFIC"
  ACTUAL_FRESH_COUNT=$(jq '.eval_cases | length' "$FRESH_TRAFFIC")

  # 5c: Run fresh traffic through the improved agent (WITH BQ logging)
  $PY "$SCRIPT_DIR/eval/run_eval.py" \
    $AGENT_CONFIG_FLAG \
    --eval-cases "$FRESH_TRAFFIC"

  # 5d: Score from BigQuery
  echo ""
  echo "  --- Quality report from BigQuery ---"
  FRESH_REPORT="$REPORTS_DIR/quality_report_cycle_${cycle}_after.json"
  rm -f "$FRESH_REPORT"

  echo ""
  echo "  Waiting 30s for BigQuery streaming buffer to flush..."
  echo ""
  echo "  While we wait, here is the current golden eval set ($GOLDEN_AFTER cases):"
  jq -r '.eval_cases[] | "    [\(.id)] \(.question) (\(.category // "general"))"' "$EVAL_CASES_PATH" 2>/dev/null || true
  echo ""

  MAX_RETRIES=6
  for attempt in $(seq 1 "$MAX_RETRIES"); do
    sleep 30
    echo "  Querying BigQuery and scoring sessions with LLM judge..."
    $PY "$REPO_ROOT/scripts/quality_report.py" \
      --app-name "$APP_NAME" \
      --output-json "$FRESH_REPORT" \
      --limit "$ACTUAL_FRESH_COUNT" \
      --time-period 24h || { rm -f "$FRESH_REPORT"; true; }

    # Guard: ensure NO old session IDs from Step 3 appear in the
    # fresh report. A simple != check allows mixed old/new populations.
    if [[ -f "$FRESH_REPORT" ]]; then
      OVERLAP_COUNT=$(python3 -c "
import json
with open('$REPORT_JSON') as f:
    old_ids = set(s['session_id'] for s in json.load(f).get('sessions', []))
with open('$FRESH_REPORT') as f:
    new_sessions = json.load(f).get('sessions', [])
new_ids = set(s['session_id'] for s in new_sessions)
print(len(old_ids & new_ids))
" 2>/dev/null || echo "999")
      NEW_COUNT=$(jq -r '.summary.total_sessions // 0' "$FRESH_REPORT" 2>/dev/null || echo "0")
      IS_FRESH=$( [[ "$NEW_COUNT" -gt 0 && "$OVERLAP_COUNT" == "0" ]] && echo "yes" || echo "no" )
      if [[ "$IS_FRESH" == "yes" ]]; then
        break
      fi
      echo "  Sessions not yet propagated or overlap detected (attempt $attempt/$MAX_RETRIES)..."
      rm -f "$FRESH_REPORT"
    fi

    if [[ $attempt -lt $MAX_RETRIES ]]; then
      sleep 10
    fi
  done

  if [[ ! -f "$FRESH_REPORT" ]]; then
    echo "ERROR: Fresh quality report was not generated after $MAX_RETRIES attempts" >&2
    exit 1
  fi

  # 5e: Print before/after comparison
  B_MR=$(jq -r '.summary.meaningful_rate // 0' "$REPORT_JSON")
  B_M=$(jq -r '.summary.meaningful // "?"' "$REPORT_JSON")
  B_T=$(jq -r '.summary.total_sessions // "?"' "$REPORT_JSON")
  A_MR=$(jq -r '.summary.meaningful_rate // 0' "$FRESH_REPORT")
  A_M=$(jq -r '.summary.meaningful // "?"' "$FRESH_REPORT")
  A_T=$(jq -r '.summary.total_sessions // "?"' "$FRESH_REPORT")

  BEFORE_LINE="Before (V${CURRENT_V}):  ${B_MR}% meaningful  (${B_M}/${B_T} sessions)"
  AFTER_LINE="After  (V${NEW_V}):  ${A_MR}% meaningful  (${A_M}/${A_T} sessions)"
  TITLE="CYCLE ${cycle} RESULTS"
  # Width = longest line + 4 padding
  W=${#BEFORE_LINE}; [[ ${#AFTER_LINE} -gt $W ]] && W=${#AFTER_LINE}; [[ ${#TITLE} -gt $W ]] && W=${#TITLE}; W=$((W + 4))
  HR=$(printf '─%.0s' $(seq 1 "$W"))
  echo ""
  printf "  ┌%s┐\n" "$HR"
  printf "  │%*s%s%*s│\n" $(( (W - ${#TITLE}) / 2 )) "" "$TITLE" $(( (W - ${#TITLE} + 1) / 2 )) ""
  printf "  ├%s┤\n" "$HR"
  printf "  │  %-$((W - 2))s│\n" "$BEFORE_LINE"
  printf "  │  %-$((W - 2))s│\n" "$AFTER_LINE"
  printf "  └%s┘\n" "$HR"

  step_end "Measurement"

  echo ""
  echo "  Cycle $cycle complete."
done

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

TOTAL_ELAPSED=$(( $(date +%s) - CYCLE_START_TIME ))
TOTAL_MIN=$((TOTAL_ELAPSED / 60))
TOTAL_SEC=$((TOTAL_ELAPSED % 60))
FINAL_V=$(_read_version)
FINAL_GOLDEN=$(jq '.eval_cases | length' "$EVAL_CASES_PATH")

separator
echo ""
_show_prompt "FINAL PROMPT"
separator
echo ""

separator
echo ""
echo "  DONE  ($CYCLES cycle(s), total wall time: ${TOTAL_MIN}m ${TOTAL_SEC}s)"
echo ""
echo "  Prompt version:   V${FINAL_V}"
echo "  Golden eval set:  $FINAL_GOLDEN cases"
echo ""
echo "  Artifacts (reports/):"
ls -1 "$REPORTS_DIR"/ 2>/dev/null | sed 's/^/    /' || echo "    (none)"
echo ""
echo "  Inspect changes:"
if [[ "$PROMPT_STORAGE" == "vertex" ]]; then
  echo "    git diff $(basename "$PROMPTS_PATH")   # prompt evolution (mirrored locally)"
else
  echo "    git diff $(basename "$PROMPTS_PATH")   # prompt evolution"
fi
echo "    git diff $(basename "$EVAL_CASES_PATH")   # new regression cases"


