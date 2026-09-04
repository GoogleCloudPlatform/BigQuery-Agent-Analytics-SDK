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
# Long-running multi-model sweep: run the full e2e (run_e2e_demo.sh) for each
# model x repetition, then aggregate mean [min-max] correctness + grounding per
# model into the table reported in VERIFICATION.md. Each run restores V0 on exit.
#
# The repetitions are independent re-runs (RUNS, default 3), NOT RNG seeds: no
# seed is threaded into the agent/engine, because the point is to observe the
# run-to-run nondeterministic variance and report it as a range. (SEEDS is
# still accepted as a back-compat alias for RUNS.)
#
# This is SLOW: at the default size (~65 held-out questions) each run is
# ~15-20 min, so the default 4 models x 3 runs = ~3-4 hours. Run setup.sh first.
# The script self-logs to runs/SWEEP_<ts>.log, so it is safe to detach and read
# the results later:
#
#   # foreground (prints progress + final table):
#   ./run_sweep.sh
#
#   # background (survives logout); watch progress, then read the table at the end:
#   nohup ./run_sweep.sh >/dev/null 2>&1 &
#   tail -f runs/SWEEP_*.log          # live progress
#   cat runs/SWEEP_*.md               # final mean [range] table when done
#
#   # subset / fewer repetitions:
#   MODELS="gemini-3.5-flash gemini-2.5-pro" RUNS=2 ./run_sweep.sh
#
# A single failed run (API blip, quota) is logged and skipped; the sweep keeps
# going and still aggregates whatever completed. To re-read a finished sweep
# without re-running, point aggregate_sweep.py at its manifest:
#   uv run python aggregate_sweep.py --manifest runs/sweep_<ts>.tsv
# Full strict mode: the `if ./run_e2e_demo.sh ...` guard below already exempts
# a failed demo run from -e (POSIX: -e is off inside an if condition), so the
# sweep still continues past a single failed run -- while mkdir/cd/manifest
# failures abort instead of being silently tolerated.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MODELS="${MODELS:-gemini-3.5-flash gemini-3.1-flash-lite gemini-2.5-pro gemini-3.1-pro-preview}"
# Independent repetitions per model (RUNS); SEEDS kept as a back-compat alias.
RUNS="${RUNS:-${SEEDS:-3}}"
TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p runs
MANIFEST="runs/sweep_${TS}.tsv"
SUMMARY="runs/SWEEP_${TS}.md"
LOG="runs/SWEEP_${TS}.log"
: > "$MANIFEST"

# Mirror all output to the log so a detached run can be tailed / read later.
exec > >(tee "$LOG") 2>&1

echo "== Sweep start $(date)  models=[$MODELS]  runs=$RUNS"
echo "== log=$LOG  manifest=$MANIFEST  summary=$SUMMARY"

ok=0
fail=0
for M in $MODELS; do
  for S in $(seq 1 "$RUNS"); do
    echo "############### $M  run=$S  ($(date +%H:%M:%S)) ###############"
    if ./run_e2e_demo.sh --agent-model "$M"; then
      # Record the run dir run_e2e_demo.sh just created (newest under runs/).
      printf '%s\t%s\n' "$M" "$(ls -dt runs/*/ | head -1)" >> "$MANIFEST"
      ok=$((ok + 1))
    else
      echo "!!! run FAILED: $M run=$S -- skipping, sweep continues !!!"
      fail=$((fail + 1))
    fi
  done
done

echo ""
echo "== Sweep done $(date):  $ok ok, $fail failed"
if [ "$ok" -gt 0 ]; then
  echo "================ SWEEP SUMMARY (mean [min-max] per model) ================"
  uv run python aggregate_sweep.py --manifest "$MANIFEST" -o "$SUMMARY"
  echo ""
  echo "Aggregated table: $SUMMARY"
  echo "Full log:         $LOG   (per-run artifacts under runs/, git-ignored)"
else
  echo "No runs completed -- nothing to aggregate. See $LOG."
  exit 1
fi
