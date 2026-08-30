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

# Example: gate a build on the LLM-judge score of one EvalBench import.
#
# Scores exactly one published import version of an EvalBench job that was
# mirrored with `bq-agent-sdk evalbench-import` (#435). The judge runs
# through the ordinary `Client.evaluate` path over the mirror table; the
# ADK plugin's production `agent_events` table is never read.
#
# Prerequisites:
#   pip install bigquery-agent-analytics
#   export BQ_AGENT_PROJECT=analytics-project   # project holding the mirror
#   export BQ_AGENT_DATASET=bqaa                # BQAA-owned target dataset
#   export EVALBENCH_JOB_ID=abc123              # job already imported
#
# Optional:
#   export EVALBENCH_IMPORT_VERSION=v1          # default: latest successful import
#   export EVALBENCH_JUDGE=correctness          # correctness|hallucination|sentiment
#   export EVALBENCH_THRESHOLD=0.7              # judge default 0.5
#
# Usage:
#   bash examples/evalbench_score_gate.sh
#
# Exit codes (same as `bq-agent-sdk evaluate`):
#   0 every session passed the judge threshold
#   1 at least one session failed (FAIL lines are printed to stderr)
#   2 invalid input (unknown judge, unpublished job/version, wrong table)
#     or a BigQuery error

set -euo pipefail

: "${BQ_AGENT_PROJECT:?set BQ_AGENT_PROJECT}"
: "${BQ_AGENT_DATASET:?set BQ_AGENT_DATASET}"
: "${EVALBENCH_JOB_ID:?set EVALBENCH_JOB_ID}"

args=(
  --job-id "${EVALBENCH_JOB_ID}"
  --evaluator "${EVALBENCH_JUDGE:-correctness}"
  --threshold "${EVALBENCH_THRESHOLD:-0.5}"
  --exit-code
  --format text
)
if [[ -n "${EVALBENCH_IMPORT_VERSION:-}" ]]; then
  args+=(--import-version "${EVALBENCH_IMPORT_VERSION}")
fi

echo "=== EvalBench LLM-judge gate: job ${EVALBENCH_JOB_ID} ==="
bq-agent-sdk evalbench-score "${args[@]}"
