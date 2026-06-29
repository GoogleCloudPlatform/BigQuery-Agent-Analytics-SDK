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
# Reproduce the multi-model table in VERIFICATION.md: run the full e2e
# (run_e2e_demo.sh) for each model x seed, then aggregate mean [min-max]
# correctness + grounding per model. Each run restores V0 on exit.
#
# Usage:
#   ./run_sweep.sh
#   MODELS="gemini-3.5-flash gemini-2.5-pro" SEEDS=3 ./run_sweep.sh
#
# This is SLOW: at the default size (~65 held-out questions) each run is
# ~15-20 min, so the default 4 models x 3 seeds = ~3-4 hours. Run setup.sh first.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MODELS="${MODELS:-gemini-3.5-flash gemini-3.1-flash-lite gemini-2.5-pro gemini-3.1-pro-preview}"
SEEDS="${SEEDS:-3}"
TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p runs
MANIFEST="runs/sweep_${TS}.tsv"
: > "$MANIFEST"

echo "== Sweep: models=[$MODELS] seeds=$SEEDS  manifest=$MANIFEST"
for M in $MODELS; do
  for S in $(seq 1 "$SEEDS"); do
    echo "############### $M  seed=$S  ($(date +%H:%M:%S)) ###############"
    AGENT_MODEL="$M" ./run_e2e_demo.sh
    # Record the run dir that run_e2e_demo.sh just created (newest under runs/).
    printf '%s\t%s\n' "$M" "$(ls -dt runs/*/ | head -1)" >> "$MANIFEST"
  done
done

echo ""
echo "================ SWEEP SUMMARY (mean [min-max] per model) ================"
uv run python aggregate_sweep.py --manifest "$MANIFEST" -o "runs/SWEEP_${TS}.md"
echo "Aggregated table: runs/SWEEP_${TS}.md   (per-run artifacts under runs/)"
