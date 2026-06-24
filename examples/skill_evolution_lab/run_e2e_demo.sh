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
#
# End-to-end skill-evolution demo on ONE policy agent:
#   1. V0: deploy a deliberately flawed SKILL.md (baked facts + "answer only
#      from the above, else contact HR"); generate traffic on the evolve and
#      held-out test sets; score against the golden Q&A (ground truth).
#   2. Evolve: run the SDK evolution engine on the V0 evolve-set failures ->
#      a small, tool-first V1 skill (also learns to re-verify user "corrections"
#      instead of parroting them).
#   3. V1: deploy the evolved skill; re-score the held-out test set.
#   4. Compare V0 vs V1 (overall, single-turn, anti-parroting); restore the
#      local V0 working copy (the Skill Registry, if used, is reverted by
#      ./reset.sh, not here).
#
# The model, tools, and questions are fixed across V0 and V1 -- only the skill
# file changes -- so any quality delta is attributable to the skill.
#
# Env overrides: AGENT_MODEL, ANALYST_MODEL, JUDGE_MODEL, JUDGE_LOCATION,
# CONCURRENCY. Set WITH_REGISTRY=1 (and SKILL_ID) to also mirror V1 to the
# Skill Registry as a new revision.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# CLI/env overrides take precedence over .env (which only supplies defaults),
# so `AGENT_MODEL=gemini-3.1-pro-preview ./run_e2e_demo.sh` actually wins.
_cli_AGENT_MODEL="${AGENT_MODEL:-}"
_cli_ANALYST_MODEL="${ANALYST_MODEL:-}"
_cli_JUDGE_MODEL="${JUDGE_MODEL:-}"
_cli_JUDGE_LOCATION="${JUDGE_LOCATION:-}"
_cli_CONCURRENCY="${CONCURRENCY:-}"
[ -f .env ] && source .env

export GOOGLE_GENAI_USE_VERTEXAI=True
export GOOGLE_CLOUD_PROJECT="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"

AGENT_MODEL="${_cli_AGENT_MODEL:-${AGENT_MODEL:-gemini-3.5-flash}}"
ANALYST_MODEL="${_cli_ANALYST_MODEL:-${ANALYST_MODEL:-gemini-3.1-pro-preview}}"
JUDGE_MODEL="${_cli_JUDGE_MODEL:-${JUDGE_MODEL:-gemini-2.5-flash}}"
JUDGE_LOCATION="${_cli_JUDGE_LOCATION:-${JUDGE_LOCATION:-us-central1}}"
# Skill Registry region is independent of the judge region (the registry only
# supports us-central1 / europe-west4 / us-east5); default it to REGION (.env).
REGISTRY_LOCATION="${REGISTRY_LOCATION:-${REGION:-us-central1}}"
CONC="${_cli_CONCURRENCY:-${CONCURRENCY:-3}}"

SKILL=skills/SKILL.md
V0=skills/SKILL.v0.md
SPEC=eval/eval_spec.json
EVOLVE=eval/questions_evolve.json
TEST=eval/questions_test.json
CORR=eval/questions_corrections.json
CORR_HO=eval/questions_corrections_heldout.json

TS="$(date +%Y%m%d_%H%M%S)"
RUN="runs/${TS}_${AGENT_MODEL//[^a-zA-Z0-9]/_}"
mkdir -p "$RUN"
echo "== Skill lab E2E  agent=$AGENT_MODEL  analyst=$ANALYST_MODEL  judge=$JUDGE_MODEL"
echo "   run=$RUN   log=$RUN/run.log"

# Always leave the LOCAL working copy on V0 (repo sits at V0). Note: with
# WITH_REGISTRY=1 this does NOT revert the remote Skill Registry to V0 -- run
# `./reset.sh` (WITH_REGISTRY=1) for that.
restore() { cp "$V0" "$SKILL"; }
trap restore EXIT

run_agent() {  # run_agent <skill> <out> <qfile...>
  local skill="$1" out="$2"; shift 2
  local qargs=(); local q
  for q in "$@"; do qargs+=(--questions "$q"); done
  uv run python run_agent.py --skill "$skill" "${qargs[@]}" \
    --model "$AGENT_MODEL" --concurrency "$CONC" -o "$out" >>"$RUN/run.log" 2>&1
}

score() {  # score <traffic> <report>   (golden-grounded; primary dims = cheaper)
  GOOGLE_CLOUD_LOCATION="$JUDGE_LOCATION" EVAL_MODEL_ID="$JUDGE_MODEL" \
    uv run python ../../scripts/quality_report.py \
      --conversations-file "$1" --eval-spec "$SPEC" --dimensions primary \
      --tag-turns --concurrency "$CONC" --output-json "$2" \
      >>"$RUN/run.log" 2>&1
}

rate() {  # rate <report> -> "X% (n/N golden-matched)"
  uv run python print_rate.py "$1"
}

# --- V0: flawed skill -> traffic -> score (evolve + held-out test) ---
cp "$V0" "$SKILL"
echo "[V0] traffic + score ..."
run_agent "$SKILL" "$RUN/v0_evolve_traffic.json" "$EVOLVE" "$CORR"
score    "$RUN/v0_evolve_traffic.json" "$RUN/v0_evolve_report.json"
run_agent "$SKILL" "$RUN/v0_test_traffic.json" "$TEST" "$CORR_HO"
score    "$RUN/v0_test_traffic.json" "$RUN/v0_test_report.json"
echo "     V0 evolve: $(rate "$RUN/v0_evolve_report.json")"
echo "     V0 test:   $(rate "$RUN/v0_test_report.json")"

# --- Evolve: extract a tool-first V1 skill from the V0 failures ---
echo "[evolve] analyst=$ANALYST_MODEL (this is the slow step) ..."
REG_ARGS=()
if [ "${WITH_REGISTRY:-0}" = "1" ]; then
  REG_ARGS=(--registry-update --skill-id "${SKILL_ID:?set SKILL_ID with WITH_REGISTRY=1}" --location "$REGISTRY_LOCATION")
fi
uv run python analyze_and_evolve.py \
  --report "$RUN/v0_evolve_report.json" --skill "$SKILL" \
  -o "$RUN/v1_skill.md" --model "$ANALYST_MODEL" \
  --candidates 3 --max-chars 3500 --write-working-copy "${REG_ARGS[@]}" \
  >>"$RUN/run.log" 2>&1
echo "     V1 skill: $(wc -c <"$RUN/v1_skill.md")B (V0 was $(wc -c <"$V0")B)"

# --- V1: evolved skill -> re-score held-out test ---
echo "[V1] traffic + score ..."
run_agent "$SKILL" "$RUN/v1_test_traffic.json" "$TEST" "$CORR_HO"
score    "$RUN/v1_test_traffic.json" "$RUN/v1_test_report.json"
echo "     V1 test:   $(rate "$RUN/v1_test_report.json")"

# --- Compare ---
echo ""
uv run python compare_runs.py --v0 "$RUN/v0_test_report.json" \
  --v1 "$RUN/v1_test_report.json" --model "$AGENT_MODEL" \
  -o "$RUN/RESULT.md" | tee "$RUN/RESULT.txt"
echo "Artifacts: $RUN"
