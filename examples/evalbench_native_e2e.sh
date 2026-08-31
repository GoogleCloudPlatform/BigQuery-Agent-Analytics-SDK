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

# Team demo: the Week 0 freeze e2e on the NATIVE path (#463, parent #435).
#
# This is a presenter script. Run it in front of the team and read the
# banners aloud: a real customer asked a support agent how many widgets
# are in stock, the agent went silent, and BQAA's native snapshot path
# (bq-agent-sdk evalbench-native-import) finds that session straight from
# production agent_events and names the failure with the frozen G1
# taxonomy -- with no EvalBench source tables anywhere in the path. A
# teammate should understand the failure in 60-90 seconds of reading the
# output, then see how BQAA names it.
#
# Same real session as the merged MVP e2e demo
# (examples/evalbench_mvp_e2e.sh) and the adapter-path freeze demo
# (PR #462): support_agent, session 7e352c34, "How many widgets are in
# stock?", never answered. The Week 0 freeze (partner, D4, G1 v0.1.0 --
# docs/week0_*.md) already landed; this demo leans on it but does not
# re-teach it. What is new here is the entrance: the snapshot starts from
# agent_events, not from EvalBench configs/results/scores.
#
# This is NOT the EXAMPLE Acme pack (examples/evalbench_week0_full_idea.sh),
# which stays illustrative. The six-week clock has NOT started; it starts
# only when the first Week 1 snapshot job is kicked.
#
# This demo is --fixture only: no BigQuery, no --synth, no live CLI, no
# judge calls. Sample identities match the PR #464 native writer tests
# (source test-project-0728-467323.bqaa_e2e_real.agent_events, target
# dataset bqaa, job mvp-e2e-real-traces, import_version v1).
#
# Usage:
#   bash examples/evalbench_native_e2e.sh --fixture
#
# Speaker notes: examples/evalbench_native_e2e.md

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: bash examples/evalbench_native_e2e.sh --fixture

Team demo of the Week 0 freeze e2e on the native path (#463): a live
walkthrough of the widget-stock failed session 7e352c34 -- the customer
asked, the agent went silent, evalbench-native-import snapshots the
production agent_events trace with no EvalBench source tables, and the
frozen G1 taxonomy v0.1.0 names the failure. The --fixture argument is
the only mode: no BigQuery, no --synth, no live CLI. The six-week clock
has not started.

Speaker notes: examples/evalbench_native_e2e.md
USAGE
}

# The --fixture argument is the only way to run this demo; the
# EVALBENCH_FIXTURE environment variable is deliberately not read.
FIXTURE=0
for arg in "$@"; do
  case "${arg}" in
    --fixture) FIXTURE=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "evalbench_native_e2e.sh: unknown argument '${arg}'." \
        "This demo is --fixture only -- no BigQuery, no --synth, no live" \
        "mode; the six-week clock has not started. Run:" \
        "bash examples/evalbench_native_e2e.sh --fixture" >&2
      exit 2
      ;;
  esac
done
if [[ "${FIXTURE}" != "1" ]]; then
  echo "evalbench_native_e2e.sh: this demo is --fixture only" \
    "(no BigQuery, no live mode; the clock has not started). Run:" \
    "bash examples/evalbench_native_e2e.sh --fixture" >&2
  exit 2
fi

banner() {
  echo
  echo "=== $* ==="
}

note() {
  echo "  # $*"
}

# Sample identities only; nothing below is read from or written to BigQuery.
# These match the PR #464 native writer tests; the session facts are real
# and are not renamed by environment variables.
source_table="test-project-0728-467323.bqaa_e2e_real.agent_events"
bq_project="test-project-0728-467323"
bq_dataset="bqaa"
job_id="mvp-e2e-real-traces"
version="v1"
events_table="${bq_project}.${bq_dataset}.evalbench_agent_events"
scores_table="${bq_project}.${bq_dataset}.evalbench_scores_imported"
manifest_table="${bq_project}.${bq_dataset}.evalbench_import_manifest"
view="${bq_project}.${bq_dataset}.evalbench_failed_sessions"
# The protagonist: the same real trace as the merged MVP e2e demo.
scenario_id="7e352c34"
session_id="7e352c34-4c1c-4395-acd5-fb3c8f215346"
import_identity="evalbench-native-import:${job_id}:${version}:${scenario_id}"

echo "EvalBench native freeze e2e (fixture mode): job ${job_id}"
note "Team walkthrough of one real failed session on the native path."
note "No BigQuery call is made in fixture mode. Clock not started."

# ---- Act 1: the customer -------------------------------------------------- #
banner "The customer asked. The agent went silent."
echo "  agent:         support_agent"
echo "  system prompt: \"You are a terse support agent. Use tools when asked"
echo "                 about inventory or tickets. Keep answers to one sentence.\""
echo "  user:          real-user-0"
echo "  asked:         How many widgets are in stock?"
echo "  session_id:    ${session_id}"
echo "  eval_id:       ${scenario_id}   (first 8 chars of session_id)"
note "This is a support ticket that went unanswered. No jargon yet."

# ---- Act 2: the trace ------------------------------------------------------ #
banner "What the trace shows"
echo "  The events live in production agent_events -- the source of truth:"
echo "  ${source_table}"
echo
echo "  USER_MESSAGE_RECEIVED -> INVOCATION_STARTING -> AGENT_STARTING"
echo "  ... then silence. Nothing else. No response ever reached the user."
echo
echo "  What never happened: no check_inventory tool call, no LLM_RESPONSE,"
echo "  no AGENT_COMPLETED."
echo
echo "  Sibling session ab7535a5 asked the same question and answered"
echo "  \"There are 0 widgets in stock.\" -- so the agent CAN do this; this"
echo "  session just never did."
note "This is the human problem: a stock question with no answer."

# ---- Act 3: native import -------------------------------------------------- #
banner "Snapshot the failure straight from agent_events"
note "evalbench-native-import reads production agent_events (read-only),"
note "derives one deterministic goal_completion score per session (1.0 iff"
note "the session logged AGENT_COMPLETED -- completed, not passed), and"
note "publishes the same pinned (job_id, import_version) snapshot the"
note "adapter produces, with the failed-session view pinned to it."
banner "Step 1: evalbench-native-import"
echo "\$ bq-agent-sdk evalbench-native-import \\"
echo "    --source-table ${source_table} \\"
echo "    --job-id ${job_id} \\"
echo "    --target-dataset ${bq_dataset} \\"
echo "    --session-id ${session_id} \\"
echo "    --location US \\"
echo "    --snapshot-at 2026-08-30T08:00:00Z \\"
echo "    --import-version ${version} \\"
echo "    --min-score goal_completion=1.0"
note "(the six sibling --session-id filters are elided so the command fits"
note "on one slide; the job snapshots all seven pinned sessions)"
cat <<JSON
{
  "job_id": "${job_id}",
  "import_version": "${version}",
  "status": "imported",
  "events_table": "${events_table}",
  "scores_table": "${scores_table}",
  "manifest_table": "${manifest_table}",
  "event_row_count": 27,
  "score_row_count": 7,
  "failed_sessions_view": "${view}"
}
JSON
note "SAY THIS LOUD: the source is production agent_events, read-only;"
note "EvalBench configs/results/scores tables are not read anywhere in"
note "this path. The adapter (#97) stays as an optional on-ramp; this is"
note "the exit ramp."
note "status=imported: 7 sessions became 27 events + 7 score rows under"
note "import_version ${version}. Session ${scenario_id} is 3 of those events and 1 of"
note "those score rows."

# ---- Act 4: failed_sessions ------------------------------------------------ #
banner "failed_sessions finds the one that never answered"
note "1 of 7 sessions failed for import_version ${version} -- ours."
banner "Step 2: evalbench-failed-sessions"
echo "\$ bq-agent-sdk evalbench-failed-sessions \\"
echo "    --project-id ${bq_project} --target-dataset ${bq_dataset} \\"
echo "    --job-id ${job_id} --import-version ${version} \\"
echo "    --min-score goal_completion=1.0 --format json"
cat <<JSON
{
  "job_id": "${job_id}",
  "import_version": "${version}",
  "session_count": 7,
  "failed_count": 1,
  "sessions": [
    {
      "session_id": "${session_id}",
      "scenario_id": "${scenario_id}",
      "process_failed": true,
      "missing_completion": true,
      "score_failed": true,
      "failed": true,
      "failing_scores": {"goal_completion": 0.0},
      "taxonomy_categories": ["task/planning", "finalization", "tool blockers"]
    }
  ]
}
JSON
note "import identity: ${import_identity}"
note "(feature:job_id:import_version:eval_id -- one string a teammate can"
note "paste into a ticket; the published rows keep the REAL ADK session_id,"
note "so the trace above and this row are the same object.)"
note "task/planning  -- never decided to look up stock"
note "tool blockers  -- never called check_inventory"
note "finalization   -- never produced an answer"
note "--format json, not table: the table format omits taxonomy_categories."
note "This is our real BQAA ADK pilot (not SANA/Strands)."
note "Fail-closed D4: Hai-Yuan Cao only; a fixture cannot produce a"
note "funding rec. Clock not started."

# ---- Act 5: the judge ------------------------------------------------------ #
banner "A live judge would miss this"
note "evalbench-score runs Client.evaluate + LLMAsJudge over the same"
note "version, narrowed to its 7 pinned session ids. Watch what it says"
note "about the silent session."
banner "Step 3: evalbench-score"
echo "\$ bq-agent-sdk evalbench-score \\"
echo "    --project-id ${bq_project} --dataset-id ${bq_dataset} \\"
echo "    --job-id ${job_id} --import-version ${version} \\"
echo "    --evaluator correctness --format json"
cat <<JSON
{
  "evaluator_name": "llm_judge_correctness",
  "dataset": "${events_table}",
  "total_sessions": 7,
  "passed_sessions": 7,
  "failed_sessions": 0,
  "pass_rate": 1.0,
  "session_scores": [
    {
      "session_id": "${session_id}",
      "scores": {"correctness": 1.0},
      "passed": true,
      "llm_feedback": null
    }
  ],
  "details": {
    "evalbench": {
      "job_id": "${job_id}",
      "import_version": "${version}",
      "events_table": "${events_table}",
      "pinned_sessions": 7
    }
  }
}
JSON
note "(the six answered sessions are elided from session_scores above)"
note "correctness 1.0, llm_feedback null, pass_rate 1.0 -- because there"
note "was nothing to judge. That is why failed_sessions, not the judge,"
note "is the denominator."

# ---- Act 6: punchline ------------------------------------------------------ #
banner "Punchline"
echo "This widget-stock session failed because the agent never answered (goal_completion=0.0). G1 names it task/planning, tool blockers, and finalization — it never planned the lookup, never called check_inventory, never finished. Next debugging action: inspect why the trace died after AGENT_STARTING before the inventory tool."
echo "We did not need EvalBench tables. Native agent_events was enough."
note "Native writer: issue #463, PR #464. Adapter-path demo: PR #462."
exit 0
