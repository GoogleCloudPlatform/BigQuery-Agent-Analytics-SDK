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

"""Compare a V0 and V1 quality report and print the skill-evolution result.

Reports the golden-grounded correctness (``matched_meaningful_rate``) before
and after evolution, split into single-turn questions and the multi-turn
anti-parroting cases, plus a count of parroted sub-trajectories (where the
agent caved to the user's wrong "correction" instead of re-verifying).

Usage:
  python compare_runs.py --v0 run/v0_test_report.json \
      --v1 run/v1_test_report.json -o run/RESULT.md
"""

from __future__ import annotations

import argparse
import json


def _is_correction(session: dict) -> bool:
  sid = session.get("session_id", "")
  return sid.startswith("corr_") or (session.get("user_turns", 1) or 1) > 1


def _is_correct(session: dict) -> bool:
  """Golden-matched AND judged meaningful."""
  ge = session.get("golden_eval", {}) or {}
  cat = (
      session.get("metrics", {}).get("response_usefulness", {}).get("category")
  )
  return bool(ge.get("matched")) and cat == "meaningful"


def _parroted(session: dict) -> bool:
  for st in session.get("sub_trajectories", []) or []:
    if st.get("outcome") == "parroted":
      return True
  return False


def _summarize(report: dict) -> dict:
  sessions = report.get("sessions", [])
  single = [s for s in sessions if not _is_correction(s)]
  corr = [s for s in sessions if _is_correction(s)]

  def rate(group):
    if not group:
      return 0.0, 0, 0
    ok = sum(1 for s in group if _is_correct(s))
    return round(ok / len(group) * 100, 1), ok, len(group)

  all_rate, all_ok, all_n = rate(sessions)
  s_rate, s_ok, s_n = rate(single)
  c_rate, c_ok, c_n = rate(corr)
  return {
      "overall": {"rate": all_rate, "correct": all_ok, "total": all_n},
      "single_turn": {"rate": s_rate, "correct": s_ok, "total": s_n},
      "corrections": {"rate": c_rate, "correct": c_ok, "total": c_n},
      "parroted": sum(1 for s in corr if _parroted(s)),
  }


def _row(label, v0, v1):
  delta = round(v1["rate"] - v0["rate"], 1)
  sign = "+" if delta >= 0 else ""
  return (
      f"| {label} | {v0['rate']}% ({v0['correct']}/{v0['total']}) | "
      f"{v1['rate']}% ({v1['correct']}/{v1['total']}) | {sign}{delta}pp |"
  )


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--v0", required=True, help="V0 report JSON")
  parser.add_argument("--v1", required=True, help="V1 report JSON")
  parser.add_argument("-o", "--out", default=None, help="Markdown output path")
  parser.add_argument("--model", default="", help="Model label for the header")
  args = parser.parse_args()

  with open(args.v0) as f:
    v0 = _summarize(json.load(f))
  with open(args.v1) as f:
    v1 = _summarize(json.load(f))

  hdr = f" ({args.model})" if args.model else ""
  lines = [
      f"# Skill Evolution Result{hdr}",
      "",
      "Golden-grounded correctness (matched & meaningful) on the held-out set.",
      "",
      "| Metric | V0 (flawed) | V1 (evolved) | Delta |",
      "| --- | --- | --- | --- |",
      _row("Overall", v0["overall"], v1["overall"]),
      _row("Single-turn", v0["single_turn"], v1["single_turn"]),
      _row("Corrections (anti-parrot)", v0["corrections"], v1["corrections"]),
      "",
      f"Parroted sub-trajectories: V0={v0['parroted']}  V1={v1['parroted']} "
      "(lower is better -- the agent re-verified instead of caving).",
      "",
  ]
  out = "\n".join(lines)
  print(out)
  if args.out:
    with open(args.out, "w") as f:
      f.write(out.rstrip() + "\n")
    json_path = args.out.rsplit(".", 1)[0] + ".json"
    with open(json_path, "w") as f:
      json.dump({"v0": v0, "v1": v1}, f, indent=2)


if __name__ == "__main__":
  main()
