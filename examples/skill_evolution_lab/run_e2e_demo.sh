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
# Skill Evolution Lab -- End-to-End Demo
# ============================================================================
#
# Runs a closed-loop skill-evolution cycle for ONE policy agent.  The cycle:
#
#   Step 1  V0 baseline       Deploy the flawed SKILL.md; run + score traffic
#                             on the evolve and held-out test sets (golden Q&A).
#   Step 2  Evolve the skill  SDK evolution engine rewrites V0 -> a small,
#                             tool-first V1 (analyst fleet, best-of-N).
#   Step 3  Measure V1        Deploy V1; re-score the held-out test set.
#   Step 4  Compare V0 vs V1  Overall, single-turn, anti-parroting; restore V0.
#   (opt)   Round 2           With --rounds 2 and a winning V1: re-run the
#                             evolve set on V1, evolve V1 -> V2, measure V2 on
#                             the held-out set, and keep V2 only if it beats V1
#                             (otherwise the incumbent V1 stays -- the engine's
#                             v2_selection.txt records the outcome either way).
#   (opt)   Registry mirror   With --with-registry, the WINNING version is
#                             pushed to the Skill Registry ONLY after it beats
#                             the incumbent on the held-out set.
#
# The model, tools, and questions are fixed across versions -- only the skill
# file changes -- so any quality delta is attributable to the skill.
#
# Usage:
#   ./run_e2e_demo.sh                                       # Defaults (one model)
#   ./run_e2e_demo.sh --agent-model gemini-2.5-pro          # Different agent
#   ./run_e2e_demo.sh --rounds 2                            # V0 -> V1 -> V2
#   ./run_e2e_demo.sh --with-registry --skill-id my-skill   # Mirror winner to registry
#   ./run_e2e_demo.sh --help
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$SCRIPT_DIR"

# Each run gets a unique timestamped directory under runs/
RUN_TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

# Suppress noisy Python warnings (authlib, etc.) and INFO-level log spam.
# Belt-and-suspenders: env var for child processes, -W flag for direct calls.
export PYTHONWARNINGS="ignore"
export LOGLEVEL="${LOGLEVEL:-WARNING}"
PY="uv run python -W ignore"

# Capture env-passed overrides BEFORE sourcing .env, so a caller's env var
# (e.g. `AGENT_MODEL=gemini-2.5-pro ./run_e2e_demo.sh`, or run_sweep.sh) wins
# over the value baked into .env. Precedence: CLI flag > env > .env > default.
_ENV_AGENT_MODEL="${AGENT_MODEL:-}"
_ENV_ANALYST_MODEL="${ANALYST_MODEL:-}"
_ENV_JUDGE_MODEL="${JUDGE_MODEL:-}"
_ENV_JUDGE_LOCATION="${JUDGE_LOCATION:-}"
_ENV_CONCURRENCY="${CONCURRENCY:-}"

# Load .env from the demo directory so all scripts see the same config.
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

export GOOGLE_GENAI_USE_VERTEXAI=True
export GOOGLE_CLOUD_PROJECT="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"

# Defaults: env override (captured above) > .env value > built-in default.
# CLI flags below still take precedence over all of these.
# Default agent: gemini-3.1-flash-lite -- it follows the flawed V0's rules most
# literally (lowest V0 baseline), so the default run reproduces the headline
# gain. Override with --agent-model or AGENT_MODEL.
AGENT_MODEL="${_ENV_AGENT_MODEL:-${AGENT_MODEL:-gemini-3.1-flash-lite}}"
ANALYST_MODEL="${_ENV_ANALYST_MODEL:-${ANALYST_MODEL:-gemini-3.1-pro-preview}}"
JUDGE_MODEL="${_ENV_JUDGE_MODEL:-${JUDGE_MODEL:-gemini-2.5-flash}}"
JUDGE_LOCATION="${_ENV_JUDGE_LOCATION:-${JUDGE_LOCATION:-us-central1}}"
CONCURRENCY="${_ENV_CONCURRENCY:-${CONCURRENCY:-3}}"
ROUNDS="${ROUNDS:-1}"
FROM_BIGQUERY="${FROM_BIGQUERY:-0}"
WITH_REGISTRY="${WITH_REGISTRY:-0}"
SKILL_ID="${SKILL_ID:-}"
# Skill Registry region is independent of the judge region (the registry only
# supports us-central1 / europe-west4 / us-east5); default it to REGION (.env).
REGISTRY_LOCATION="${REGISTRY_LOCATION:-${REGION:-us-central1}}"

# Parse arguments
# need_val <flag> <next-arg-count>: readable usage error instead of a raw
# `set -u` "unbound variable" crash when a flag is missing its value.
need_val() {
  if [[ "$2" -lt 2 ]]; then
    echo "ERROR: $1 requires a value (see --help)" >&2
    exit 1
  fi
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-model)
      need_val "$1" $#
      AGENT_MODEL="$2"
      shift 2
      ;;
    --analyst-model)
      need_val "$1" $#
      ANALYST_MODEL="$2"
      shift 2
      ;;
    --judge-model)
      need_val "$1" $#
      JUDGE_MODEL="$2"
      shift 2
      ;;
    --judge-location)
      need_val "$1" $#
      JUDGE_LOCATION="$2"
      shift 2
      ;;
    --concurrency)
      need_val "$1" $#
      CONCURRENCY="$2"
      shift 2
      ;;
    --rounds)
      need_val "$1" $#
      ROUNDS="$2"
      shift 2
      ;;
    --from-bigquery)
      FROM_BIGQUERY=1
      shift
      ;;
    --with-registry)
      WITH_REGISTRY=1
      shift
      ;;
    --skill-id)
      need_val "$1" $#
      SKILL_ID="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --agent-model M    Agent under test       (default: gemini-3.1-flash-lite)"
      echo "  --analyst-model M  Evolution analyst      (default: gemini-3.1-pro-preview)"
      echo "  --judge-model M    LLM judge for scoring  (default: gemini-2.5-flash)"
      echo "  --judge-location R Vertex region (judge)  (default: us-central1)"
      echo "  --concurrency N    Parallel requests      (default: 3)"
      echo "  --rounds N         Evolution rounds: 1 = V0->V1, 2 = V0->V1->V2"
      echo "                     (round 2 runs only when V1 wins; V2 is kept"
      echo "                     only when it beats V1 on the held-out set)"
      echo "  --from-bigquery    Log every session to a BQAA agent_events table and"
      echo "                     score by reading it back from BigQuery (production"
      echo "                     wiring). Uses DATASET_ID/TABLE_ID from .env"
      echo "                     (defaults: agent_analytics.agent_events)"
      echo "  --with-registry    Mirror the winning version to the Skill Registry"
      echo "                     (only after it beats the incumbent on the held-out set)"
      echo "  --skill-id ID      Skill Registry id (required with --with-registry)"
      echo "  -h, --help         Show this help message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$WITH_REGISTRY" == "1" && -z "$SKILL_ID" ]]; then
  echo "ERROR: --with-registry requires --skill-id" >&2
  exit 1
fi
if [[ "$ROUNDS" != "1" && "$ROUNDS" != "2" ]]; then
  echo "ERROR: --rounds must be 1 or 2 (got: $ROUNDS)" >&2
  exit 1
fi

REPORTS_DIR="$SCRIPT_DIR/runs/${RUN_TIMESTAMP}_${AGENT_MODEL//[^a-zA-Z0-9]/_}"
mkdir -p "$REPORTS_DIR"

# Tee all output: terminal gets colour, log file gets plain text. The $'...'
# quoting makes bash produce the ESC byte itself -- BSD sed (macOS) does not
# interpret \x1b, so an escape inside the sed pattern would not strip anything.
RUN_LOG="$REPORTS_DIR/run.log"
exec > >(tee >(sed $'s/\033\[[0-9;]*m//g' >> "$RUN_LOG")) 2>&1

# ---------------------------------------------------------------------------
# Input files
# ---------------------------------------------------------------------------
SKILL="skills/SKILL.md"
V0="skills/SKILL.v0.md"
SPEC="eval/eval_spec.json"
EVOLVE="eval/questions_evolve.json"
TEST="eval/questions_test.json"
CORR="eval/questions_corrections.json"
CORR_HO="eval/questions_corrections_heldout.json"
OOS="eval/questions_oos.json"
OOS_HO="eval/questions_oos_heldout.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Timestamp prefix for log lines
ts() { date "+%H:%M:%S"; }

# ANSI formatting
BOLD='\033[1m'
DIM='\033[2m'
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
RESET='\033[0m'

# Print a prominent stage banner that stands out from log lines
stage() {
  echo ""
  echo -e "${BOLD}${CYAN}  ▶ $*${RESET}"
  echo ""
}

# Timer: call step_start before a step, step_end after.
step_start() { STEP_START_TIME=$(date +%s); }
step_end() {
  local elapsed=$(( $(date +%s) - STEP_START_TIME ))
  local label="${1:-Step}"
  echo ""
  echo -e "  ${GREEN}✔ ${label} completed in ${elapsed}s.${RESET}"
}

separator() {
  echo ""
  echo -e "${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

# Always leave the LOCAL working copy on V0 (the repo sits at V0). Note: with
# --with-registry this does NOT revert the remote Skill Registry to V0 -- run
# ./reset.sh (WITH_REGISTRY=1) for that.
restore() { cp "$V0" "$SKILL"; }
trap restore EXIT

# Unique per-run label for --from-bigquery: rows carry custom_tags
# {run, slice} so scoring can select exactly this run's sessions from a
# shared, append-only events table.
RUN_LABEL="lab_${RUN_TIMESTAMP}"

# slice <traffic-path> -> "v0_evolve" / "v1_test" / ... (the metric slice name)
slice_of() { local b; b="$(basename "$1")"; echo "${b%_traffic.json}"; }

# run_agent <skill> <out> <qfile...>  -- run questions through the agent.
# With --from-bigquery, every session is also logged to the BQAA events table.
run_agent() {
  local skill="$1" out="$2"; shift 2
  local qargs=() q
  for q in "$@"; do qargs+=(--questions "$q"); done
  local bqargs=()
  if [[ "$FROM_BIGQUERY" == "1" ]]; then
    bqargs=(--log-bigquery --app-name skill-evolution-lab
            --bq-label "run=$RUN_LABEL" --bq-label "slice=$(slice_of "$out")")
  fi
  $PY run_agent.py --skill "$skill" "${qargs[@]}" \
    --model "$AGENT_MODEL" --concurrency "$CONCURRENCY" -o "$out" "${bqargs[@]}"
}

# score <traffic> <report>  -- golden-grounded LLM judge. Full dimensions: the two
# primary metrics (verdict + grounding) plus the five 0-2 quality dimensions.
# Default: score the local conversations file directly. With --from-bigquery:
# read the same sessions back from the BigQuery events table instead (the
# production wiring), selected by this run's {run, slice} labels.
score() {
  if [[ "$FROM_BIGQUERY" == "1" ]]; then
    GOOGLE_CLOUD_LOCATION="$JUDGE_LOCATION" EVAL_MODEL_ID="$JUDGE_MODEL" \
    PROJECT_ID="$GOOGLE_CLOUD_PROJECT" \
    DATASET_ID="${DATASET_ID:-agent_analytics}" \
    TABLE_ID="${TABLE_ID:-agent_events}" \
    DATASET_LOCATION="${DATASET_LOCATION:-${REGION:-us-central1}}" \
      $PY "$REPO_ROOT/scripts/quality_report.py" \
        --label "run=$RUN_LABEL" --label "slice=$(slice_of "$1")" --limit 500 \
        --eval-spec "$SPEC" --dimensions full \
        --tag-turns --output-json "$2"
  else
    GOOGLE_CLOUD_LOCATION="$JUDGE_LOCATION" EVAL_MODEL_ID="$JUDGE_MODEL" \
      $PY "$REPO_ROOT/scripts/quality_report.py" \
        --conversations-file "$1" --eval-spec "$SPEC" --dimensions full \
        --tag-turns --concurrency "$CONCURRENCY" --output-json "$2"
  fi
}

# rate <report>  -> "X% (n/N golden-matched)"
rate() { $PY print_rate.py "$1"; }

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

if [[ -z "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
  echo "ERROR: PROJECT_ID is not set. Run ./setup.sh YOUR_PROJECT_ID REGION" \
       "(or set PROJECT_ID in .env) before running the demo." >&2
  exit 1
fi

separator
echo ""
echo -e "  ${BOLD}${CYAN}SKILL EVOLUTION LAB -- END-TO-END${RESET}"
echo ""
echo "  Project:    $GOOGLE_CLOUD_PROJECT"
echo "  Agent:      $AGENT_MODEL"
echo "  Analyst:    $ANALYST_MODEL"
echo "  Judge:      $JUDGE_MODEL  (@ $JUDGE_LOCATION)"
echo "  Concurrency:$CONCURRENCY"
echo "  Rounds:     $ROUNDS"
if [[ "$FROM_BIGQUERY" == "1" ]]; then
  echo "  Traces:     BigQuery (${DATASET_ID:-agent_analytics}.${TABLE_ID:-agent_events}, run=$RUN_LABEL)"
else
  echo "  Traces:     local JSON (same schema; use --from-bigquery for the BQ path)"
fi
if [[ "$WITH_REGISTRY" == "1" ]]; then
  echo "  Registry:   on  (skill-id=$SKILL_ID, @ $REGISTRY_LOCATION)"
else
  echo "  Registry:   off"
fi
echo "  Output dir: $REPORTS_DIR"
CYCLE_START_TIME=$(date +%s)

separator
echo ""
GOLDEN_COUNT=$(jq '.golden_qa | length' "$SPEC" 2>/dev/null || echo "?")
echo "  GOLDEN EVAL SET ($GOLDEN_COUNT cases)  -- the answer key scoring grades against"
echo ""

# =========================================================================
# STEP 1: V0 baseline
# =========================================================================
separator
stage "STEP 1/4: V0 BASELINE (flawed skill)"
echo "  Goal:    Measure the flawed starting skill before any evolution"
echo "  Method:  Deploy SKILL.v0.md, run traffic, score vs the golden Q&A"
echo "  Sets:    evolve (study) + held-out (test), each with corrections + out-of-scope"
echo ""
step_start

cp "$V0" "$SKILL"
# Save the V0 baseline into the run dir too, so each run is self-contained
# (the flawed starting skill next to the evolved v1_skill.md for diffing).
cp "$V0" "$REPORTS_DIR/v0_skill.md"
echo -e "  ${DIM}[$(ts)] Running + scoring the evolve set...${RESET}"
run_agent "$SKILL" "$REPORTS_DIR/v0_evolve_traffic.json" "$EVOLVE" "$CORR" "$OOS"
score     "$REPORTS_DIR/v0_evolve_traffic.json" "$REPORTS_DIR/v0_evolve_report.json"
echo -e "  ${DIM}[$(ts)] Running + scoring the held-out test set...${RESET}"
run_agent "$SKILL" "$REPORTS_DIR/v0_test_traffic.json" "$TEST" "$CORR_HO" "$OOS_HO"
score     "$REPORTS_DIR/v0_test_traffic.json" "$REPORTS_DIR/v0_test_report.json"

echo ""
echo "  V0 evolve: $(rate "$REPORTS_DIR/v0_evolve_report.json")"
echo -e "  ${BOLD}V0 test:   $(rate "$REPORTS_DIR/v0_test_report.json")${RESET}"

step_end "V0 baseline"

# =========================================================================
# STEP 2: Evolve the skill
# =========================================================================
separator
stage "STEP 2/4: EVOLVE THE SKILL"
echo "  Goal:    Turn the V0 failures into a small, tool-first V1 skill"
echo "  Method:  Analyst fleet reads the scored evolve set; best-of-3 candidates"
echo "  Model:   analyst=$ANALYST_MODEL  (this is the slow step)"
echo ""
step_start

# NOTE: the registry mirror deliberately does NOT happen here -- V1 has not
# been measured yet. It runs after STEP 4, gated on V1 beating V0 (the loop's
# "keep the new skill only when it wins" property).
echo -e "  ${DIM}[$(ts)] Analysts proposing patches, consolidating candidates...${RESET}"
$PY analyze_and_evolve.py \
  --report "$REPORTS_DIR/v0_evolve_report.json" --skill "$SKILL" --eval-spec "$SPEC" \
  -o "$REPORTS_DIR/v1_skill.md" --model "$ANALYST_MODEL" \
  --candidates 3 --max-chars 3500 --write-working-copy

echo ""
echo "  V1 skill: $(wc -c <"$REPORTS_DIR/v1_skill.md")B  (V0 was $(wc -c <"$V0")B)"

step_end "Evolution"

# =========================================================================
# STEP 3: Measure V1 on the held-out test set
# =========================================================================
separator
stage "STEP 3/4: MEASURE V1 (held-out)"
echo "  Goal:    Score the evolved skill on the SAME held-out set V0 was judged on"
echo "  Method:  Deploy V1, run held-out traffic, score vs the golden Q&A"
echo ""
step_start

echo -e "  ${DIM}[$(ts)] Running + scoring the held-out test set with V1...${RESET}"
run_agent "$SKILL" "$REPORTS_DIR/v1_test_traffic.json" "$TEST" "$CORR_HO" "$OOS_HO"
score     "$REPORTS_DIR/v1_test_traffic.json" "$REPORTS_DIR/v1_test_report.json"

echo ""
echo -e "  ${BOLD}V1 test:   $(rate "$REPORTS_DIR/v1_test_report.json")${RESET}"

step_end "V1 measurement"

# =========================================================================
# STEP 4: Compare V0 vs V1
# =========================================================================
separator
stage "STEP 4/4: COMPARE V0 vs V1"
echo "  Goal:    Attribute the delta to the skill (overall / single-turn / parroting)"
echo "  Method:  Diff the two held-out scorecards into RESULT.md"
echo ""
step_start

# --gate makes compare_runs exit 3 when V1 loses to V0 overall; the registry
# mirror below keys off that so a losing V1 is never pushed as a new revision.
V1_WINS=1
GATE_RC=0
$PY compare_runs.py \
  --v0 "$REPORTS_DIR/v0_test_report.json" \
  --v1 "$REPORTS_DIR/v1_test_report.json" \
  --model "$AGENT_MODEL" --gate \
  -o "$REPORTS_DIR/RESULT.md" | tee "$REPORTS_DIR/RESULT.txt" || GATE_RC=$?
if [[ "$GATE_RC" -eq 3 ]]; then
  V1_WINS=0
elif [[ "$GATE_RC" -ne 0 ]]; then
  exit "$GATE_RC"
fi

step_end "Comparison"

# =========================================================================
# ROUND 2 (optional, --rounds 2): evolve the winning V1 again -> V2
# =========================================================================
# FINAL_LABEL tracks which version the working copy holds for the registry
# mirror below: V1 after round 1, V2 only when round 2 ran AND V2 won.
FINAL_LABEL="V1"
if [[ "$ROUNDS" -ge 2 ]]; then
  separator
  if [[ "$V1_WINS" != "1" ]]; then
    stage "ROUND 2 SKIPPED"
    echo "  V1 did not beat V0 on the held-out set, so there is no winning skill"
    echo "  to evolve further. The loop falls back to V0."
  else
    stage "ROUND 2a: RE-RUN THE EVOLVE SET ON V1"
    echo "  Goal:    Fresh learning signal -- what does V1 still get wrong?"
    echo "  Method:  Same evolve traffic, scored the same way; input to round 2"
    echo ""
    step_start
    run_agent "$SKILL" "$REPORTS_DIR/v1_evolve_traffic.json" "$EVOLVE" "$CORR" "$OOS"
    score     "$REPORTS_DIR/v1_evolve_traffic.json" "$REPORTS_DIR/v1_evolve_report.json"
    echo ""
    echo "  V1 evolve: $(rate "$REPORTS_DIR/v1_evolve_report.json")"
    step_end "Round 2 traffic"

    separator
    stage "ROUND 2b: EVOLVE V1 -> V2"
    echo "  Goal:    Merge what round 1 missed into V2 (nothing learned is lost)"
    echo "  Method:  Same engine, --version-label v2 (v2_* artifacts + selection record)"
    echo ""
    step_start
    $PY analyze_and_evolve.py \
      --report "$REPORTS_DIR/v1_evolve_report.json" --skill "$SKILL" --eval-spec "$SPEC" \
      -o "$REPORTS_DIR/v2_skill.md" --model "$ANALYST_MODEL" \
      --candidates 3 --max-chars 3500 --version-label v2 --write-working-copy
    echo ""
    echo "  V2 skill: $(wc -c <"$REPORTS_DIR/v2_skill.md")B  (V1 was $(wc -c <"$REPORTS_DIR/v1_skill.md")B)"
    if [[ -f "$REPORTS_DIR/v2_selection.txt" ]]; then
      echo "  Selection: $(head -1 "$REPORTS_DIR/v2_selection.txt")"
    fi
    step_end "Round 2 evolution"

    separator
    stage "ROUND 2c: MEASURE V2 (held-out) AND GATE V2 vs V1"
    echo "  Goal:    Keep V2 only when it beats the incumbent V1"
    echo "  Method:  Same held-out set, same judge; compare_runs --gate"
    echo ""
    step_start
    run_agent "$SKILL" "$REPORTS_DIR/v2_test_traffic.json" "$TEST" "$CORR_HO" "$OOS_HO"
    score     "$REPORTS_DIR/v2_test_traffic.json" "$REPORTS_DIR/v2_test_report.json"
    echo ""
    echo -e "  ${BOLD}V2 test:   $(rate "$REPORTS_DIR/v2_test_report.json")${RESET}"
    echo ""

    V2_WINS=1
    GATE2_RC=0
    $PY compare_runs.py \
      --v0 "$REPORTS_DIR/v1_test_report.json" \
      --v1 "$REPORTS_DIR/v2_test_report.json" \
      --label0 "V1 (evolved)" --label1 "V2 (round 2)" \
      --model "$AGENT_MODEL" --gate \
      -o "$REPORTS_DIR/RESULT_ROUND2.md" | tee "$REPORTS_DIR/RESULT_ROUND2.txt" || GATE2_RC=$?
    if [[ "$GATE2_RC" -eq 3 ]]; then
      V2_WINS=0
    elif [[ "$GATE2_RC" -ne 0 ]]; then
      exit "$GATE2_RC"
    fi
    if [[ "$V2_WINS" == "1" ]]; then
      FINAL_LABEL="V2"
      echo ""
      echo -e "  ${GREEN}V2 beat V1 on the held-out set -- V2 is the kept version.${RESET}"
    else
      cp "$REPORTS_DIR/v1_skill.md" "$SKILL"
      echo ""
      echo -e "  ${YELLOW}V2 did not beat V1 on the held-out set -- the incumbent V1 stays.${RESET}"
      echo "  This is the safety property doing its job: a round with nothing new"
      echo "  to teach cannot replace a proven skill (see v2_selection.txt)."
    fi
    step_end "Round 2 measurement + gate"
  fi
fi

# =========================================================================
# Registry mirror (optional) -- only a WINNING version becomes a new revision
# =========================================================================
if [[ "$WITH_REGISTRY" == "1" ]]; then
  separator
  stage "REGISTRY MIRROR (gated on the held-out comparison)"
  step_start
  if [[ "$V1_WINS" == "1" ]]; then
    echo -e "  ${DIM}[$(ts)] $FINAL_LABEL beat the incumbent on the held-out set -- mirroring to the registry...${RESET}"
    $PY analyze_and_evolve.py --registry-push-only \
      --skill "$SKILL" --skill-id "$SKILL_ID" --location "$REGISTRY_LOCATION"
  else
    echo -e "  ${YELLOW}V1 did NOT beat V0 on the held-out set -- registry push skipped.${RESET}"
    echo "  The registry keeps its current revision; the local copy is restored to V0."
  fi
  step_end "Registry mirror"
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

TOTAL_ELAPSED=$(( $(date +%s) - CYCLE_START_TIME ))
TOTAL_MIN=$((TOTAL_ELAPSED / 60))
TOTAL_SEC=$((TOTAL_ELAPSED % 60))

separator
echo ""
echo -e "  ${BOLD}${GREEN}DONE${RESET}  (total wall time: ${TOTAL_MIN}m ${TOTAL_SEC}s)"
echo ""
echo "  Result table:   $REPORTS_DIR/RESULT.md"
if [[ -f "$REPORTS_DIR/RESULT_ROUND2.md" ]]; then
  echo "  Round 2 table:  $REPORTS_DIR/RESULT_ROUND2.md  (kept version: $FINAL_LABEL)"
fi
echo ""
echo "  Artifacts (runs/):"
ls -1 "$REPORTS_DIR"/ 2>/dev/null | sed 's/^/    /' || echo "    (none)"
echo ""
echo "  Local skill restored to V0."
echo "  Reproduce the multi-model table: ./run_sweep.sh"
echo ""
separator
echo ""
echo "  Total wall time: ${TOTAL_MIN}m ${TOTAL_SEC}s"
echo ""
