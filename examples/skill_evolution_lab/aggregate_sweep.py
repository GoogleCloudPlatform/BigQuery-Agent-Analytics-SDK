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

"""Aggregate a skill-evolution sweep into a mean [min-max] table per model.

Reads a manifest written by run_sweep.sh -- one ``<model>\\t<run_dir>`` line per
run -- and reports V0 baseline + V1 correctness (golden-matched meaningful rate)
and V1 grounding (tool-call share), averaged across that model's runs with the
range, so a single unlucky run can't masquerade as the result.

Usage:
  python aggregate_sweep.py --manifest runs/sweep_<ts>.txt -o runs/SWEEP_<ts>.md
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
import statistics


def _rate(run_dir: str, fname: str) -> float:
  with open(os.path.join(run_dir, fname)) as f:
    return json.load(f)["summary"]["golden_eval_summary"][
        "matched_meaningful_rate"
    ]


def _grounding(run_dir: str, fname: str) -> int:
  with open(os.path.join(run_dir, fname)) as f:
    convs = json.load(f)["conversations"]
  if not convs:
    return 0
  grounded = sum(1 for c in convs if c.get("tool_calls", 0) > 0)
  return round(100 * grounded / len(convs))


def _fmt(values: list) -> str:
  if not values:
    return "-"
  return (
      f"{round(statistics.mean(values))}%"
      f" [{round(min(values))}-{round(max(values))}]"
  )


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--manifest",
      required=True,
      help="file with '<model>\\t<run_dir>' per line",
  )
  parser.add_argument("-o", "--out", default=None, help="Markdown output path")
  args = parser.parse_args()

  with open(args.manifest) as f:
    rows = [line.rstrip("\n") for line in f if line.strip()]

  agg = defaultdict(lambda: {"v0": [], "v1": [], "v1g": []})
  order = []
  for row in rows:
    model, run_dir = row.split("\t", 1)
    if model not in agg:
      order.append(model)
    agg[model]["v0"].append(_rate(run_dir, "v0_test_report.json"))
    agg[model]["v1"].append(_rate(run_dir, "v1_test_report.json"))
    agg[model]["v1g"].append(_grounding(run_dir, "v1_test_traffic.json"))

  lines = [
      "| Model | V0 correctness (mean) | V1 correctness mean [range] |"
      " V1 grounding mean [range] | runs |",
      "| --- | --- | --- | --- | --- |",
  ]
  for model in order:
    a = agg[model]
    lines.append(
        f"| `{model}` | {round(statistics.mean(a['v0']))}% | {_fmt(a['v1'])} |"
        f" {_fmt(a['v1g'])} | {len(a['v1'])} |"
    )
  out = "\n".join(lines)
  print(out)
  if args.out:
    with open(args.out, "w") as f:
      f.write(out + "\n")


if __name__ == "__main__":
  main()
