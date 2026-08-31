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

# Team demo: the post-freeze Week 0 e2e as a live walkthrough (#435).
#
# This is a presenter script. Run it in front of the team and read the
# banners aloud: a real customer asked a support agent how many widgets
# are in stock, the agent went silent, and BQAA's EvalBench import path
# finds that session and names the failure with the frozen G1 taxonomy.
# A teammate should understand the failure in 60-90 seconds of reading
# the output, then see how BQAA names it.
#
# Same real session as the merged MVP e2e demo
# (examples/evalbench_mvp_e2e.sh): support_agent, session 7e352c34,
# "How many widgets are in stock?", never answered. The Week 0 freeze
# (partner, D4, G1 v0.1.0 -- docs/week0_*.md) already landed; this demo
# leans on it but does not re-teach it.
#
# This is NOT the EXAMPLE Acme pack (examples/evalbench_week0_full_idea.sh),
# which stays illustrative. The six-week clock has NOT started; it starts
# only when the first Week 1 snapshot job is kicked.
#
# This demo is --fixture only: no BigQuery, no --synth, no live CLI, no
# judge calls. Sample identities are fixed at the 455 fixture defaults
# (analytics-project.bqaa mirror, benchmark-project.evalbench source,
# job mvp-e2e-real-traces, import_version v1).
#
# Usage:
#   bash examples/evalbench_week0_freeze_e2e.sh --fixture
#
# Speaker notes: examples/evalbench_week0_freeze_e2e.md

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: bash examples/evalbench_week0_freeze_e2e.sh --fixture

Team demo of the post-freeze Week 0 e2e (#435): a live walkthrough of
the widget-stock failed session 7e352c34 -- the customer asked, the
agent went silent, evalbench-import + failed_sessions find it, and the
frozen G1 taxonomy v0.1.0 names it. The --fixture argument is the only
mode: no BigQuery, no --synth, no live CLI. The six-week clock has not
started.

Speaker notes: examples/evalbench_week0_freeze_e2e.md
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
      echo "evalbench_week0_freeze_e2e.sh: unknown argument '${arg}'." \
        "This demo is --fixture only -- no BigQuery, no --synth, no live" \
        "mode; the six-week clock has not started. Run:" \
        "bash examples/evalbench_week0_freeze_e2e.sh --fixture" >&2
      exit 2
      ;;
  esac
done
if [[ "${FIXTURE}" != "1" ]]; then
  echo "evalbench_week0_freeze_e2e.sh: this demo is --fixture only" \
    "(no BigQuery, no live mode; the clock has not started). Run:" \
    "bash examples/evalbench_week0_freeze_e2e.sh --fixture" >&2
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
# These are the 455 fixture defaults; the session facts are real and are
# not renamed by environment variables.
bq_project="analytics-project"
bq_dataset="bqaa"
eb_project="benchmark-project"
eb_dataset="evalbench"
job_id="mvp-e2e-real-traces"
version="v1"
events_table="${bq_project}.${bq_dataset}.evalbench_agent_events"
scores_table="${bq_project}.${bq_dataset}.evalbench_scores_imported"
manifest_table="${bq_project}.${bq_dataset}.evalbench_import_manifest"
view="${bq_project}.${bq_dataset}.evalbench_failed_sessions"
# The protagonist: the same real trace as the merged MVP e2e demo.
scenario_id="7e352c34"
session_id="7e352c34-4c1c-4395-acd5-fb3c8f215346"
sid="evalbench-import:${job_id}:${version}:${scenario_id}"

echo "EvalBench Week 0 freeze e2e (fixture mode): job ${job_id}"
note "Team walkthrough of one real failed session and how BQAA names it."
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
note "Real trace source: test-project-0728-467323.bqaa_e2e_real.agent_events"

# ---- Act 2: the trace ------------------------------------------------------ #
banner "What the trace shows"
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

# ---- Act 3: import --------------------------------------------------------- #
banner "Import the real job so we can query that failure"
note "evalbench-import mirrors the job's EvalBench tables into BQAA-owned"
note "tables, records the version in a manifest row, and pins the"
note "evalbench_failed_sessions view to it. After this, we can query the"
note "silent session like any other analytics row."
banner "Step 1: evalbench-import"
echo "\$ bq-agent-sdk evalbench-import \\"
echo "    --project-id ${eb_project} --evalbench-dataset ${eb_dataset} \\"
echo "    --job-id ${job_id} --import-version ${version} \\"
echo "    --target-project ${bq_project} --target-dataset ${bq_dataset} \\"
echo "    --min-score goal_completion=1 --format json"
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
note "status=imported: 7 scenarios became 27 events + 7 score rows under"
note "import_version ${version}. Session ${scenario_id} is 3 of those events and 1 of"
note "those score rows."

# ---- Act 4: failed_sessions ------------------------------------------------ #
banner "failed_sessions finds the one that never answered"
note "1 of 7 sessions failed for import_version ${version} -- ours."
banner "Step 2: evalbench-failed-sessions"
echo "\$ bq-agent-sdk evalbench-failed-sessions \\"
echo "    --project-id ${bq_project} --target-dataset ${bq_dataset} \\"
echo "    --job-id ${job_id} --import-version ${version} \\"
echo "    --min-score goal_completion=1 --format json"
cat <<JSON
{
  "job_id": "${job_id}",
  "import_version": "${version}",
  "session_count": 7,
  "failed_count": 1,
  "sessions": [
    {
      "session_id": "${sid}",
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
note "task/planning  -- never decided to look up stock"
note "tool blockers  -- never called check_inventory"
note "finalization   -- never produced an answer"
note "--format json, not table: the table format omits taxonomy_categories."
note "This is our real BQAA ADK+EvalBench pilot (not SANA/Strands)."
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
      "session_id": "${sid}",
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
exit 0
