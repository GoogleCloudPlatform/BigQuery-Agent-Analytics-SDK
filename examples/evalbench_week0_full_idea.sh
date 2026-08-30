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

# EXAMPLE Week 0 scenario pack for the AgentForensics MVP (#435).
#
# Demonstrates all five Week 0 human gates of
# docs/agentforensics_mvp_plan.md as ONE concrete story on the widget-stock
# failed session — as EXAMPLES. Nothing here is a freeze: g1_frozen stays
# false, the six-week clock has NOT started, and no real partner is named
# (the example partner is "Acme Retail Support").
#
# This pack is --fixture only: no BigQuery, no network, no live judge.
#
#   bash examples/evalbench_week0_full_idea.sh --fixture
#
# Narrative companion: examples/evalbench_week0_full_idea.md
# Facts printed under each banner come from examples/fixtures/week0_example_*.json.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE_DIR="${SCRIPT_DIR}/fixtures"

fixture=0
for arg in "$@"; do
  case "$arg" in
    --fixture) fixture=1 ;;
    *)
      echo "evalbench_week0_full_idea.sh: unknown argument '${arg}'." \
        "This EXAMPLE pack is --fixture only — no BigQuery, no --synth, no" \
        "live mode; the six-week clock has not started." >&2
      exit 2
      ;;
  esac
done
if [[ "${EVALBENCH_FIXTURE:-}" == "1" ]]; then
  fixture=1
fi
if (( ! fixture )); then
  echo "evalbench_week0_full_idea.sh: this EXAMPLE pack is --fixture only" \
    "(no BigQuery, no live mode; the clock has not started). Run:" \
    "bash examples/evalbench_week0_full_idea.sh --fixture" >&2
  exit 2
fi

show_json() {
  # Pretty-print one fixture JSON file (also proves it parses).
  python3 -m json.tool "${FIXTURE_DIR}/$1"
}

echo "=== EXAMPLE — Week 0 is not a freeze. Clock has not started. ==="
echo "This is an EXAMPLE scenario pack — every artifact below is"
echo "illustrative. It is not a freeze: not a partner freeze, not a G1"
echo "freeze, not a preregistration freeze."
echo "  g1_frozen: false"
echo "  clock_started: false — the six-week clock has not started."
echo "fixture mode: offline; no BigQuery, no network, no live judge."
echo "It walks the five Week 0 human gates of docs/agentforensics_mvp_plan.md"
echo "as one story on the widget-stock failed session from the #435 e2e demo."
echo

echo "=== EXAMPLE partner + SANA relationship ==="
echo "Example partner: Acme Retail Support (illustrative — no real partner"
echo "is named). AgentForensics here is SANA-adjacent, not a SANA fork:"
echo "SANA is LakeQA + KramaBench on Strands with seven categories; this"
echo "example pilot is ADK+EvalBench on widget-stock support, seeding"
echo "taxonomy v0.1 EXAMPLE from those seven because failure modes overlap"
echo "— and it is not duplicating LakeQA/KramaBench work."
show_json week0_example_partner.json
echo

echo "=== EXAMPLE runtime + route ==="
echo "The pilot traces already exist: support_agent, logged by the ADK"
echo "plugin into bqaa_e2e_real.agent_events and folded into EvalBench job"
echo "mvp-e2e-real-traces. Runtime is ADK plugin -> BQAA agent_events ->"
echo "EvalBench-hosted, so the EvalBench-only MVP route is CORRECT for this"
echo "example and D1 does not need re-decision here."
echo

echo "=== EXAMPLE pilot-benchmark rubric ==="
echo "Selected by the predeclared rubric, not import convenience:"
echo "widget-stock support, session 7e352c34-4c1c-4395-acd5-fb3c8f215346"
echo "(eval_id 7e352c34). All five criteria pass in this example:"
echo "goal_completion 0.0 vs threshold 1; sibling ab7535a5 answered"
echo "\"There are 0 widgets in stock.\"; real ADK events"
echo "USER_MESSAGE_RECEIVED -> INVOCATION_STARTING -> AGENT_STARTING, then"
echo "silence."
show_json week0_example_rubric.json
echo

echo "=== EXAMPLE D4 boundary memo ==="
echo "Fail-closed: no clearance means the pilot runs on pre-redacted"
echo "reference traces (project test-project-0728-467323) or pauses."
echo "A --fixture/synthetic run validates ingestion, taxonomy mechanics,"
echo "and stability ONLY — it can never produce a Part II funding"
echo "recommendation. Report consumers below are examples, not real people;"
echo "the stop/go memo is itself a governed artifact."
show_json week0_example_d4_memo.json
echo

echo "=== EXAMPLE preregistration (not a week-1 freeze) ==="
echo "EXAMPLE copy of the plan's floors and decision rules — not week-1"
echo "freeze; clock not started. Floors: replicate agreement >=80%;"
echo "non-unknown coverage >=80%; kappa point >=0.6 with CI lower >=0.45;"
echo "localization coverage >=70%; hit@1 CI lower >0 and point uplift"
echo ">= +10pp. Plus the reserved revision week, the value-gate rubric, and"
echo "the noisy-small-n localization rule."
show_json week0_example_preregistration.json
echo

echo "=== This agent was asked to check widget stock. Here is the session. ==="
echo "  agent:        support_agent"
echo "  user:         real-user-0"
echo "  prompt:       How many widgets are in stock?"
echo "  session_id:   7e352c34-4c1c-4395-acd5-fb3c8f215346"
echo "  eval_id:      7e352c34"
echo "  job:          mvp-e2e-real-traces"
echo "  source:       bqaa_e2e_real.agent_events (test-project-0728-467323)"
echo "  events:       USER_MESSAGE_RECEIVED -> INVOCATION_STARTING ->"
echo "                AGENT_STARTING, then silence"
echo "  it never called check_inventory; no LLM_RESPONSE; no AGENT_COMPLETED"
echo

echo "=== This session in failed_sessions (mechanical taxonomy_categories) ==="
echo "The slice-9 consumer attaches the mechanical scaffold categories to"
echo "the row — which gates tripped, not why the agent failed:"
echo "  process_failed:      True"
echo "  missing_completion:  True"
echo "  score_failed:        True   (goal_completion 0.0 vs threshold 1)"
echo '  taxonomy_categories: ["process_failed", "missing_completion", "score_failed"]'
echo

echo "=== EXAMPLE mapping of mechanical flags onto SANA-seeded names ==="
echo "EXAMPLE only — not G1; g1_frozen: false. These SANA-seeded names live"
echo "only in this fixture, never in src/. For this session: the agent"
echo "never called check_inventory (tool blockers), never finalized an"
echo "answer (finalization), and the plan to answer never formed"
echo "(task/planning as an overlapping seed)."
show_json week0_example_taxonomy_seed.json
echo

echo "=== Punchline ==="
echo "This widget-stock session failed because the agent never answered; goal_completion=0.0."
