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

# Example: the EvalBench MVP end to end, on one job (#435, #97).
#
# Walks the three CLI steps in order over a single EvalBench job:
#
#   Step 1: bq-agent-sdk evalbench-import
#           source EvalBench BigQuery -> BQAA mirror tables (+ manifest row
#           + the pinned evalbench_failed_sessions view)
#   Step 2: bq-agent-sdk evalbench-failed-sessions
#           the W0.4 failed-session listing of that one published version
#   Step 3: bq-agent-sdk evalbench-score
#           the LLM judge (Client.evaluate + LLMAsJudge) over that same
#           version of the mirror table; details.evalbench names the version
#
# This is the full pipeline walkthrough. `examples/evalbench_score_gate.sh`
# stays the CI gate (step 3 alone, with --exit-code).
#
# Prerequisites (live mode):
#   pip install bigquery-agent-analytics
#   export BQ_AGENT_PROJECT=analytics-project   # project holding the mirror
#   export BQ_AGENT_DATASET=bqaa                # BQAA-owned target dataset
#   export EVALBENCH_PROJECT=benchmark-project  # project holding EvalBench
#   export EVALBENCH_DATASET=evalbench          # EvalBench's own dataset
#   export EVALBENCH_JOB_ID=abc123              # one EvalBench job_id
#
# Optional:
#   export EVALBENCH_IMPORT_VERSION=v1          # pin one version for all
#                                               # three steps; default: the
#                                               # importer mints one and
#                                               # steps 2-3 use the latest
#                                               # successful import
#
# Usage:
#   bash examples/evalbench_mvp_e2e.sh             # live: calls BigQuery
#   bash examples/evalbench_mvp_e2e.sh --fixture   # offline: sample output
#   EVALBENCH_FIXTURE=1 bash examples/evalbench_mvp_e2e.sh
#
# --fixture (or EVALBENCH_FIXTURE=1) never touches BigQuery and never
# invokes the live CLI: it prints the same three step banners followed by
# annotated sample output shaped like each command's real output, and
# exits 0. Use it to record the demo or to run this script in CI.
#
# Exit codes (live mode):
#   0 all three steps ran (step 3 exits 0 even when sessions fail the
#     judge; add --exit-code via the gate script when you want a CI gate)
#   1 a required environment variable is missing
#   2 a step failed (each CLI's own exit code propagates; see docs/evalbench.md)

set -euo pipefail

usage() {
  # Print the header comment (from "Example:" up to `set -euo pipefail`).
  awk 'NR >= 16 && /^set -euo pipefail/ { exit }
       NR >= 16 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

FIXTURE="${EVALBENCH_FIXTURE:-0}"
for arg in "$@"; do
  case "${arg}" in
    --fixture) FIXTURE=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Error: unknown argument '${arg}'; expected --fixture or --help" >&2
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
# Fixture mode: recordable without BigQuery.
# --------------------------------------------------------------------------- #
if [[ "${FIXTURE}" == "1" ]]; then
  # Sample identities only; nothing below is read from or written to BigQuery.
  bq_project="${BQ_AGENT_PROJECT:-analytics-project}"
  bq_dataset="${BQ_AGENT_DATASET:-bqaa}"
  eb_project="${EVALBENCH_PROJECT:-benchmark-project}"
  eb_dataset="${EVALBENCH_DATASET:-evalbench}"
  job_id="${EVALBENCH_JOB_ID:-gemini-cli-tools-2026-08-30}"
  version="${EVALBENCH_IMPORT_VERSION:-v1}"
  events_table="${bq_project}.${bq_dataset}.evalbench_agent_events"
  scores_table="${bq_project}.${bq_dataset}.evalbench_scores_imported"
  manifest_table="${bq_project}.${bq_dataset}.evalbench_import_manifest"
  view="${bq_project}.${bq_dataset}.evalbench_failed_sessions"
  sid="evalbench-import:${job_id}:${version}"

  echo "EvalBench MVP e2e (fixture mode): job ${job_id}, import_version ${version}"
  note "No BigQuery call is made in fixture mode. Output below is sample"
  note "output shaped like each command's real output; run without"
  note "--fixture against a real job to see live values."

  banner "Step 1: evalbench-import"
  note "source EvalBench BigQuery -> BQAA mirror tables + manifest + view"
  echo "\$ bq-agent-sdk evalbench-import \\"
  echo "    --project-id ${eb_project} --evalbench-dataset ${eb_dataset} \\"
  echo "    --job-id ${job_id} --import-version ${version} \\"
  echo "    --target-project ${bq_project} --target-dataset ${bq_dataset} \\"
  echo "    --min-score goal_completion=0.9 --format json"
  cat <<JSON
{
  "job_id": "${job_id}",
  "import_version": "${version}",
  "status": "imported",
  "events_table": "${events_table}",
  "scores_table": "${scores_table}",
  "manifest_table": "${manifest_table}",
  "event_row_count": 12,
  "score_row_count": 4,
  "manifest": {
    "job_id": "${job_id}",
    "import_version": "${version}",
    "source_project": "${eb_project}",
    "source_dataset": "${eb_dataset}",
    "source_snapshot_at": "2026-08-30T08:00:00+00:00",
    "results_count": 4,
    "scores_count": 4,
    "configs_count": 1,
    "results_fingerprint": "sha256:5d1c…e2a9",
    "scores_fingerprint": "sha256:9b7f…04c1",
    "configs_fingerprint": "sha256:c3a0…77de",
    "events_table": "${events_table}",
    "scores_table": "${scores_table}",
    "event_row_count": 12,
    "score_row_count": 4,
    "imported_at": "2026-08-30T08:05:12+00:00",
    "generation_id": "3f9c2c1e0b7a4d6e9a1b5c8d7e6f4a2b",
    "view_policy": "{\"min_scores\": {\"goal_completion\": 0.9}, \"missing_score_fails\": true}",
    "superseded_generations": []
  },
  "failed_sessions_view": "${view}"
}
JSON
  note "status=imported: 4 scenarios became 12 events + 4 score rows under"
  note "import_version ${version}; the manifest row is the version's contract"
  note "and failed_sessions_view is now pinned to this generation."

  banner "Step 2: evalbench-failed-sessions"
  note "the W0.4 denominator: every session of this one published version,"
  note "failed rows listed (same rules the pinned view renders)"
  echo "\$ bq-agent-sdk evalbench-failed-sessions \\"
  echo "    --project-id ${bq_project} --target-dataset ${bq_dataset} \\"
  echo "    --job-id ${job_id} --import-version ${version} \\"
  echo "    --min-score goal_completion=0.9 --format table"
  fmt="%-*s  %-11s  %-14s  %-18s  %-12s  %s\n"
  width=$(( ${#sid} + 11 ))
  printf "${fmt}" "${width}" session_id scenario_id process_failed \
    missing_completion score_failed failing_scores
  printf "${fmt}" "${width}" "$(printf '%*s' "${width}" '' | tr ' ' -)" \
    ----------- -------------- ------------------ ------------ --------------
  printf "${fmt}" "${width}" "${sid}:read-file" read-file False False True \
    '{"goal_completion": 0.6}'
  printf "${fmt}" "${width}" "${sid}:shell-grep" shell-grep True True False '{}'

  note "session_count=4 failed_count=2 for import_version ${version}."
  note "Each session_id embeds the version, so"
  note "client.get_session_trace(session_id=..., experiment_id=${job_id})"
  note "can only return rows of ${version}."

  banner "Step 3: evalbench-score"
  note "LLM judge over the same version: Client.evaluate + LLMAsJudge,"
  note "narrowed to the pinned session ids of ${version} (never agent_events)"
  echo "\$ bq-agent-sdk evalbench-score \\"
  echo "    --project-id ${bq_project} --dataset-id ${bq_dataset} \\"
  echo "    --job-id ${job_id} --import-version ${version} \\"
  echo "    --evaluator correctness --threshold 0.7 --format json"
  cat <<JSON
{
  "evaluator_name": "llm_judge_correctness",
  "dataset": "${events_table}",
  "total_sessions": 4,
  "passed_sessions": 3,
  "failed_sessions": 1,
  "pass_rate": 0.75,
  "aggregate_scores": {
    "correctness": 0.81
  },
  "details": {
    "evalbench": {
      "job_id": "${job_id}",
      "import_version": "${version}",
      "events_table": "${events_table}",
      "pinned_sessions": 4
    }
  }
}
JSON
  note "details.evalbench ties the scorecard back to job ${job_id},"
  note "import_version ${version}, and the 4 pinned sessions it judged."
  note "For a CI gate on this number, see examples/evalbench_score_gate.sh."

  echo
  echo "Done (fixture mode): import -> failed-sessions -> score for job ${job_id}."
  exit 0
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

run_step() {
  # Propagate the CLI's own exit code as this script's exit 2.
  if ! "$@"; then
    echo "Error: step failed: $*" >&2
    exit 2
  fi
}

echo "EvalBench MVP e2e: job ${EVALBENCH_JOB_ID}" \
  "(${EVALBENCH_PROJECT}.${EVALBENCH_DATASET} -> ${BQ_AGENT_PROJECT}.${BQ_AGENT_DATASET})"

banner "Step 1: evalbench-import"
run_step bq-agent-sdk evalbench-import \
  --project-id "${EVALBENCH_PROJECT}" \
  --evalbench-dataset "${EVALBENCH_DATASET}" \
  --job-id "${EVALBENCH_JOB_ID}" \
  --target-project "${BQ_AGENT_PROJECT}" \
  --target-dataset "${BQ_AGENT_DATASET}" \
  ${version_args[@]+"${version_args[@]}"} \
  --format json

banner "Step 2: evalbench-failed-sessions"
run_step bq-agent-sdk evalbench-failed-sessions \
  --project-id "${BQ_AGENT_PROJECT}" \
  --target-dataset "${BQ_AGENT_DATASET}" \
  --job-id "${EVALBENCH_JOB_ID}" \
  ${version_args[@]+"${version_args[@]}"} \
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
