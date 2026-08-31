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

# Post-freeze Week 0 e2e for the AgentForensics MVP (#435).
#
# The Week 0 freeze landed for real: partner (docs/week0_partner.md), D4
# boundary (docs/week0_d4_memo.md), and G1 taxonomy v0.1.0
# (docs/week0_g1_taxonomy.md). This demo replays that freeze as one
# recordable story on the same widget-stock failed session as the merged
# MVP e2e demo (examples/evalbench_mvp_e2e.sh): support_agent, session
# 7e352c34, "How many widgets are in stock?", never answered.
#
# This is NOT the EXAMPLE Acme pack (examples/evalbench_week0_full_idea.sh),
# which stays illustrative. Everything printed here is the REAL frozen
# record. The six-week clock has NOT started; it starts only when the
# first Week 1 snapshot job is kicked.
#
# This demo is --fixture only: no BigQuery, no --synth, no live CLI, no
# judge calls. Sample identities are fixed at the 455 fixture defaults
# (analytics-project.bqaa mirror, benchmark-project.evalbench source,
# job mvp-e2e-real-traces, import_version v1).
#
# Usage:
#   bash examples/evalbench_week0_freeze_e2e.sh --fixture
#
# Narrative companion: examples/evalbench_week0_freeze_e2e.md

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: bash examples/evalbench_week0_freeze_e2e.sh --fixture

Post-freeze Week 0 e2e demo (#435): walks the REAL frozen partner
(Google Cloud BQAA), the fail-closed D4 memo, and the G1 taxonomy v0.1.0
freeze on the widget-stock failed session 7e352c34, then the three
EvalBench CLI steps as sample output. The --fixture argument is the only
mode: no BigQuery, no --synth, no live CLI. The six-week clock has not
started.

Narrative companion: examples/evalbench_week0_freeze_e2e.md
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
# These are the 455 fixture defaults; the frozen facts (partner, D4, G1)
# are fixed and are not renamed by environment variables.
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
note "No BigQuery call is made in fixture mode. Clock has not started."
note "Partner + D4 + G1 are already frozen (#435 Week 0 freeze); this demo"
note "replays that freeze on the widget-stock failed session from the"
note "merged MVP e2e demo."

# ---- 1. Partner ---------------------------------------------------------- #
banner "Partner: Google Cloud BQAA (this SDK)"
echo "The REAL pilot partner is Google Cloud BigQuery Agent Analytics"
echo "(this SDK / BQAA). The pilot is self-hosted: the ADK support_agent"
echo "traces in test-project-0728-467323.bqaa_e2e_real.agent_events,"
echo "imported as EvalBench job ${job_id}."
note "AgentForensics is SANA-adjacent published work -- not a SANA fork and"
note "not a named collaboration with SANA authors. SANA is LakeQA +"
note "KramaBench on Strands; this pilot is ADK+EvalBench on widget-stock"
note "support, so it is not duplicating those two benchmarks."
note "Frozen record: docs/week0_partner.md"

# ---- 2. D4 --------------------------------------------------------------- #
banner "D4: fail-closed memo for this pilot"
echo "The D4 boundary is fail-closed and covers exactly this pilot's data:"
echo "  project:   test-project-0728-467323"
echo "  datasets:  bqaa_e2e_real, bqaa_evalbench_mvp_demo,"
echo "             bqaa_evalbench_mvp_mirror"
echo "  named report consumer: Hai-Yuan Cao (caohy1988 / haiyuan-eng-google)"
note "No other consumer is named; if a consumer is not named in the memo,"
note "access is denied."
note "--fixture validates ingestion, taxonomy mechanics, and stability ONLY"
note "-- it can never produce a Part II funding recommendation."
note "Frozen record: docs/week0_d4_memo.md"

# ---- 3. G1 --------------------------------------------------------------- #
banner "G1 freeze: taxonomy v0.1.0"
echo "Production failure_taxonomy.py is frozen:"
echo "  taxonomy_version: 0.1.0"
echo "  g1_frozen: true"
echo "  clock_started: false"
echo "Mechanical mapping until the labeler study:"
echo "  missing_completion -> finalization"
echo "  process_failed     -> tool blockers"
echo "  score_failed       -> task/planning"
note "Names come back in frozen order (the SANA-neighborhood seven, then"
note "unknown) -- not flag order."
note "Freezing G1 is NOT a clock start: the six-week clock has not started;"
note "it starts only when the first Week 1 snapshot job is kicked."
note "Frozen record: docs/week0_g1_taxonomy.md"

# ---- 4. The session ------------------------------------------------------ #
banner "This agent was asked to check widget stock. Here is the session."
echo "  agent:         support_agent"
echo "  system prompt: \"You are a terse support agent. Use tools when asked"
echo "                 about inventory or tickets. Keep answers to one sentence.\""
echo "  user:          real-user-0"
echo "  session_id:    ${session_id}"
echo "  scenario_id:   ${scenario_id}   (EvalBench eval_id: the session_id's first 8 chars)"
echo "  job:           ${job_id}"
note "The same session as the merged MVP e2e demo -- now read through the"
note "frozen Week 0 record above."

# ---- 5. What happened ---------------------------------------------------- #
banner "What happened"
echo "  user:  How many widgets are in stock?"
echo "  agent: (no response)"
echo
echo "  events, in order:"
echo "    USER_MESSAGE_RECEIVED"
echo "    INVOCATION_STARTING"
echo "    AGENT_STARTING"
echo "    ... then silence"
note "The trace stops after AGENT_STARTING: no check_inventory tool call, no"
note "LLM_RESPONSE, no AGENT_COMPLETED. Sibling session ab7535a5 asked the"
note "same question and answered \"There are 0 widgets in stock.\""

# ---- 6. Import ----------------------------------------------------------- #
banner "Import those traces into EvalBench so we can query this failure"
note "evalbench-import mirrors the job's EvalBench tables into BQAA-owned"
note "tables, records the version in a manifest row, and pins the"
note "evalbench_failed_sessions view to it. After this, failed_sessions can"
note "list session ${scenario_id} -- with its frozen G1 labels."
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

# ---- 7. failed_sessions with the frozen labels --------------------------- #
banner "This session in failed_sessions with frozen G1 labels"
note "1 of 7 sessions failed the W0.4 contract for import_version ${version}."
note "failed_sessions (not the judge) is the W0.4 denominator, and since the"
note "G1 freeze each row carries taxonomy_categories in the frozen names."
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
note "--format json, not table: the table format is not used for this step"
note "in the fixture (it historically omitted taxonomy_categories)."
note "The mechanical flags still trip -- process_failed, missing_completion,"
note "score_failed all true -- and the frozen mapper turns them into the"
note "G1 names, in frozen order: task/planning, finalization, tool blockers."

# ---- 8. Score ------------------------------------------------------------ #
banner "Score this session"
note "evalbench-score runs Client.evaluate + LLMAsJudge over the same"
note "version, narrowed to its 7 pinned session ids."
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
note "Scenario ${scenario_id}: the imported goal_completion is 0.0 (step 2), yet"
note "the correctness judge on this job scored the unanswered session 1.0"
note "with llm_feedback null -- there was no answer to judge. That is why"
note "failed_sessions, not the judge, is the W0.4 denominator."

# ---- 9. Punchline -------------------------------------------------------- #
banner "Punchline"
echo "This widget-stock session failed because the agent never answered; goal_completion=0.0. G1 frozen labels are task/planning, finalization, tool blockers."
exit 0
