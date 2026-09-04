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

# Example: the EvalBench MVP end to end, told through one failed session
# (#435, #97).
#
# A terse support agent (support_agent) was asked "How many widgets are in
# stock?" and never answered: its trace stops after AGENT_STARTING. This
# script follows that one session -- scenario 7e352c34 of job
# mvp-e2e-real-traces, a real trace from bqaa_e2e_real.agent_events --
# through the three EvalBench CLI steps:
#
#   Step 1: bq-agent-sdk evalbench-import
#           the job's EvalBench tables -> BQAA mirror tables + manifest row
#           + the pinned evalbench_failed_sessions view, so the session can
#           be queried
#   Step 2: bq-agent-sdk evalbench-failed-sessions
#           the W0.4 failed-session listing of that one published version;
#           this session is its one failed row
#   Step 3: bq-agent-sdk evalbench-score
#           the LLM judge (Client.evaluate + LLMAsJudge) over the same
#           version; details.evalbench names the version it judged
#
# `examples/evalbench_score_gate.sh` stays the CI gate (step 3 alone, with
# --exit-code).
#
# Usage:
#   bash examples/evalbench_mvp_e2e.sh --fixture   # offline: the story, recordable
#   bash examples/evalbench_mvp_e2e.sh --synth     # live: build the job from real
#                                                  # traces, then steps 1-3
#   bash examples/evalbench_mvp_e2e.sh             # live: steps 1-3 on an
#                                                  # existing EvalBench job
#
# --fixture (or EVALBENCH_FIXTURE=1) never touches BigQuery and never
# invokes the live CLI: it prints the session, then each step's command
# with sample output shaped like that command's real output, and exits 0.
# Names default to analytics-project.bqaa (mirror), benchmark-project.evalbench
# (source), job mvp-e2e-real-traces, import_version v1; BQ_AGENT_PROJECT,
# BQ_AGENT_DATASET, EVALBENCH_PROJECT, EVALBENCH_DATASET, EVALBENCH_JOB_ID
# and EVALBENCH_IMPORT_VERSION rename them.
#
# --synth (or EVALBENCH_SYNTH=1) is live mode without an EvalBench run:
# step 0 runs examples/evalbench_synth_from_traces.py, which folds a real
# BQAA agent_events table (default: bqaa_e2e_real.agent_events, the table
# this session came from) into EvalBench-shaped configs/results/scores
# tables -- one scenario per trace, real prompt and response text,
# goal_completion 1.0 when the trace reached AGENT_COMPLETED else 0.0 --
# then steps 1-3 run on that job. Defaults (only gcloud is needed):
#
#   BQ_AGENT_PROJECT        gcloud's current project (or set it)
#   EVALBENCH_PROJECT       = BQ_AGENT_PROJECT (synth writes both datasets)
#   EVALBENCH_SOURCE_TABLE  bqaa_e2e_real.agent_events  (the real traces)
#   EVALBENCH_DATASET       bqaa_evalbench_mvp_demo     (built by step 0)
#   BQ_AGENT_DATASET        bqaa_evalbench_mvp_mirror   (import target)
#   EVALBENCH_JOB_ID        mvp-e2e-real-traces
#   EVALBENCH_MIN_SCORE     goal_completion=1  (--min-score for steps 1-2;
#                           set to "" to omit)
#   EVALBENCH_PYTHON        python3 (interpreter with google-cloud-bigquery)
#
# Live mode (no flag) needs BQ_AGENT_PROJECT, BQ_AGENT_DATASET,
# EVALBENCH_PROJECT, EVALBENCH_DATASET and EVALBENCH_JOB_ID. Optional:
# EVALBENCH_IMPORT_VERSION pins one version for all three steps (default:
# step 1 mints one, steps 2-3 use the latest successful import);
# EVALBENCH_JUDGE picks step 3's judge (default correctness).
#
# Exit codes (live mode):
#   0 all steps ran (step 3 exits 0 even when sessions fail the judge;
#     add --exit-code via the gate script when you want a CI gate)
#   1 a required environment variable is missing
#   2 a step failed (each CLI's own exit code propagates; see docs/evalbench.md)

set -euo pipefail

usage() {
  # Print the header comment (from "Example:" up to `set -euo pipefail`).
  awk 'NR >= 16 && /^set -euo pipefail/ { exit }
       NR >= 16 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

FIXTURE="${EVALBENCH_FIXTURE:-0}"
SYNTH="${EVALBENCH_SYNTH:-0}"
for arg in "$@"; do
  case "${arg}" in
    --fixture) FIXTURE=1 ;;
    --synth) SYNTH=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Error: unknown argument '${arg}'; expected --fixture, --synth or --help" >&2
      exit 2
      ;;
  esac
done

banner() {
  echo
  echo "=== $* ==="
}

note() {
  echo "  # $*"
}

# --------------------------------------------------------------------------- #
# Fixture mode: the story of one failed session, recordable without BigQuery.
# --------------------------------------------------------------------------- #
if [[ "${FIXTURE}" == "1" ]]; then
  # Sample identities only; nothing below is read from or written to BigQuery.
  bq_project="${BQ_AGENT_PROJECT:-analytics-project}"
  bq_dataset="${BQ_AGENT_DATASET:-bqaa}"
  eb_project="${EVALBENCH_PROJECT:-benchmark-project}"
  eb_dataset="${EVALBENCH_DATASET:-evalbench}"
  job_id="${EVALBENCH_JOB_ID:-mvp-e2e-real-traces}"
  version="${EVALBENCH_IMPORT_VERSION:-v1}"
  events_table="${bq_project}.${bq_dataset}.evalbench_agent_events"
  scores_table="${bq_project}.${bq_dataset}.evalbench_scores_imported"
  manifest_table="${bq_project}.${bq_dataset}.evalbench_import_manifest"
  view="${bq_project}.${bq_dataset}.evalbench_failed_sessions"
  # The protagonist: a real trace from bqaa_e2e_real.agent_events.
  scenario_id="7e352c34"
  session_id="7e352c34-4c1c-4395-acd5-fb3c8f215346"
  sid="evalbench-import:${job_id}:${version}:${scenario_id}"

  echo "EvalBench MVP e2e (fixture mode): job ${job_id}, import_version ${version}"
  note "No BigQuery call is made in fixture mode. The session below is a real"
  note "trace; each command's output is sample output shaped like the real"
  note "thing. Run with --synth to replay it live from the traces."

  # ---- 1. Setup -------------------------------------------------------- #
  banner "This agent was asked to check widget stock. Here is the session."
  echo "  agent:         support_agent"
  echo "  system prompt: \"You are a terse support agent. Use tools when asked"
  echo "                 about inventory or tickets. Keep answers to one sentence.\""
  echo "  user:          real-user-0"
  echo "  session_id:    ${session_id}"
  echo "  scenario_id:   ${scenario_id}   (EvalBench eval_id: the session_id's first 8 chars)"
  note "support_agent was asked to answer an inventory question."

  # ---- 2. What happened ------------------------------------------------ #
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
  note "LLM_RESPONSE, no AGENT_COMPLETED. final_response=null, tool_calls=[],"
  note "error_message=null, returncode=1. Sibling session ab7535a5 asked the"
  note "same question and answered \"There are 0 widgets in stock.\""

  # ---- 3. Import ------------------------------------------------------- #
  banner "Import those traces into EvalBench so we can query this failure"
  note "evalbench-import mirrors the job's EvalBench tables into BQAA-owned"
  note "tables, records the version in a manifest row, and pins the"
  note "evalbench_failed_sessions view to it. After this, failed_sessions can"
  note "list session ${scenario_id}."
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
  "manifest": {
    "job_id": "${job_id}",
    "import_version": "${version}",
    "source_project": "${eb_project}",
    "source_dataset": "${eb_dataset}",
    "source_snapshot_at": "2026-08-30T08:00:00+00:00",
    "results_count": 7,
    "scores_count": 7,
    "configs_count": 1,
    "results_fingerprint": "sha256:5d1c…e2a9",
    "scores_fingerprint": "sha256:9b7f…04c1",
    "configs_fingerprint": "sha256:c3a0…77de",
    "events_table": "${events_table}",
    "scores_table": "${scores_table}",
    "event_row_count": 27,
    "score_row_count": 7,
    "imported_at": "2026-08-30T08:05:12+00:00",
    "generation_id": "3f9c2c1e0b7a4d6e9a1b5c8d7e6f4a2b",
    "view_policy": "{\"min_scores\": {\"goal_completion\": 1.0}, \"missing_score_fails\": true}",
    "superseded_generations": []
  },
  "failed_sessions_view": "${view}"
}
JSON
  note "status=imported: 7 scenarios became 27 events + 7 score rows under"
  note "import_version ${version}. Session ${scenario_id} is 3 of those events and 1 of"
  note "those score rows. The manifest row is the version's contract, and"
  note "failed_sessions_view is pinned to this generation, so the next step"
  note "can only see rows of ${version}."

  # ---- 4. This session in failed_sessions ------------------------------ #
  banner "This session in failed_sessions"
  note "1 of 7 sessions failed the W0.4 contract for import_version ${version};"
  note "this is the row."
  banner "Step 2: evalbench-failed-sessions"
  echo "\$ bq-agent-sdk evalbench-failed-sessions \\"
  echo "    --project-id ${bq_project} --target-dataset ${bq_dataset} \\"
  echo "    --job-id ${job_id} --import-version ${version} \\"
  echo "    --min-score goal_completion=1 --format table"
  fmt="%-*s  %-11s  %-14s  %-18s  %-12s  %s\n"
  width=${#sid}
  printf "${fmt}" "${width}" session_id scenario_id process_failed \
    missing_completion score_failed failing_scores
  printf "${fmt}" "${width}" "$(printf '%*s' "${width}" '' | tr ' ' -)" \
    ----------- -------------- ------------------ ------------ --------------
  printf "${fmt}" "${width}" "${sid}" "${scenario_id}" True True True \
    '[{"comparator": "goal_completion", "score": 0.0}]'
  echo
  note "process_failed=True      returncode 1: the run did not finish cleanly"
  note "missing_completion=True  no AGENT_COMPLETED event in the trace"
  note "score_failed=True        goal_completion 0.0 misses --min-score goal_completion=1"
  note "Use --format json to include the frozen G1 v0.1.0 categories on this row:"
  echo '  {"taxonomy_categories": ["task/planning", "finalization", "tool blockers"]}'
  note "These map the observed flags to categories; they do not establish why the process stopped."
  note "session_count=7 failed_count=1. The session_id embeds the version, so"
  note "client.get_session_trace(session_id=\"${sid}\","
  note "experiment_id=\"${job_id}\") returns only ${version}'s rows for it."

  # ---- 5. Score this session ------------------------------------------- #
  banner "Score this session"
  note "evalbench-score runs Client.evaluate + LLMAsJudge over the same"
  note "version, narrowed to its 7 pinned session ids (never agent_events)."
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
  "aggregate_scores": {
    "correctness": 1.0
  },
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
  note "details.evalbench ties the scorecard to job ${job_id},"
  note "import_version ${version}, and the 7 pinned sessions it judged."

  # ---- 6. Punchline ---------------------------------------------------- #
  banner "Punchline"
  echo "This widget-stock session failed because the agent never answered; goal_completion=0.0."
  exit 0
fi


# --------------------------------------------------------------------------- #
# Synth mode defaults: the demo's own names, on gcloud's current project.
# --------------------------------------------------------------------------- #
if [[ "${SYNTH}" == "1" ]]; then
  if [[ -z "${BQ_AGENT_PROJECT:-}" ]] && command -v gcloud >/dev/null 2>&1; then
    BQ_AGENT_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  : "${BQ_AGENT_PROJECT:?set BQ_AGENT_PROJECT (or gcloud config set project ...)}"
  EVALBENCH_PROJECT="${EVALBENCH_PROJECT:-${BQ_AGENT_PROJECT}}"
  if [[ "${EVALBENCH_PROJECT}" != "${BQ_AGENT_PROJECT}" ]]; then
    echo "Error: --synth builds the EvalBench dataset and the mirror dataset in" \
      "one project; EVALBENCH_PROJECT=${EVALBENCH_PROJECT} differs from" \
      "BQ_AGENT_PROJECT=${BQ_AGENT_PROJECT}" >&2
    exit 1
  fi
  EVALBENCH_SOURCE_TABLE="${EVALBENCH_SOURCE_TABLE:-bqaa_e2e_real.agent_events}"
  EVALBENCH_DATASET="${EVALBENCH_DATASET:-bqaa_evalbench_mvp_demo}"
  BQ_AGENT_DATASET="${BQ_AGENT_DATASET:-bqaa_evalbench_mvp_mirror}"
  EVALBENCH_JOB_ID="${EVALBENCH_JOB_ID:-mvp-e2e-real-traces}"
  EVALBENCH_MIN_SCORE="${EVALBENCH_MIN_SCORE-goal_completion=1}"
fi

# --------------------------------------------------------------------------- #
# Live mode: calls BigQuery through the three CLIs.
# --------------------------------------------------------------------------- #
: "${BQ_AGENT_PROJECT:?set BQ_AGENT_PROJECT}"
: "${BQ_AGENT_DATASET:?set BQ_AGENT_DATASET}"
: "${EVALBENCH_PROJECT:?set EVALBENCH_PROJECT}"
: "${EVALBENCH_DATASET:?set EVALBENCH_DATASET}"
: "${EVALBENCH_JOB_ID:?set EVALBENCH_JOB_ID}"

version_args=()
if [[ -n "${EVALBENCH_IMPORT_VERSION:-}" ]]; then
  version_args+=(--import-version "${EVALBENCH_IMPORT_VERSION}")
fi
min_score_args=()
if [[ -n "${EVALBENCH_MIN_SCORE:-}" ]]; then
  min_score_args+=(--min-score "${EVALBENCH_MIN_SCORE}")
fi

run_step() {
  # Propagate the CLI's own exit code as this script's exit 2.
  if ! "$@"; then
    echo "Error: step failed: $*" >&2
    exit 2
  fi
}

echo "EvalBench MVP e2e: job ${EVALBENCH_JOB_ID}" \
  "(${EVALBENCH_PROJECT}.${EVALBENCH_DATASET} -> ${BQ_AGENT_PROJECT}.${BQ_AGENT_DATASET})"

if [[ "${SYNTH}" == "1" ]]; then
  banner "Step 0: synthesize EvalBench tables from real traces"
  note "${EVALBENCH_SOURCE_TABLE} -> ${EVALBENCH_PROJECT}.${EVALBENCH_DATASET}.{configs,results,scores}"
  note "one scenario per session; prompts and responses are the real trace text"
  run_step "${EVALBENCH_PYTHON:-python3}" \
    "$(dirname "${BASH_SOURCE[0]}")/evalbench_synth_from_traces.py" \
    --project "${EVALBENCH_PROJECT}" \
    --source-table "${EVALBENCH_SOURCE_TABLE}" \
    --evalbench-dataset "${EVALBENCH_DATASET}" \
    --mirror-dataset "${BQ_AGENT_DATASET}" \
    --job-id "${EVALBENCH_JOB_ID}"
fi

banner "Step 1: evalbench-import"
run_step bq-agent-sdk evalbench-import \
  --project-id "${EVALBENCH_PROJECT}" \
  --evalbench-dataset "${EVALBENCH_DATASET}" \
  --job-id "${EVALBENCH_JOB_ID}" \
  --target-project "${BQ_AGENT_PROJECT}" \
  --target-dataset "${BQ_AGENT_DATASET}" \
  ${version_args[@]+"${version_args[@]}"} \
  ${min_score_args[@]+"${min_score_args[@]}"} \
  --format json

banner "Step 2: evalbench-failed-sessions"
run_step bq-agent-sdk evalbench-failed-sessions \
  --project-id "${BQ_AGENT_PROJECT}" \
  --target-dataset "${BQ_AGENT_DATASET}" \
  --job-id "${EVALBENCH_JOB_ID}" \
  ${version_args[@]+"${version_args[@]}"} \
  ${min_score_args[@]+"${min_score_args[@]}"} \
  --format table

banner "Step 3: evalbench-score"
run_step bq-agent-sdk evalbench-score \
  --project-id "${BQ_AGENT_PROJECT}" \
  --dataset-id "${BQ_AGENT_DATASET}" \
  --job-id "${EVALBENCH_JOB_ID}" \
  ${version_args[@]+"${version_args[@]}"} \
  --evaluator "${EVALBENCH_JUDGE:-correctness}" \
  --format json

echo
echo "Done: import -> failed-sessions -> score for job ${EVALBENCH_JOB_ID}."
