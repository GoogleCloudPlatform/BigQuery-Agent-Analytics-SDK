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
import sys

# Exit code for --gate when V1 loses to V0 (distinct from crash exit codes so
# callers can tell "V1 regressed" apart from "comparison failed").
GATE_EXIT_CODE = 3


def _is_oos(session: dict) -> bool:
  """Out-of-scope question (no golden answer; a clean decline is the win)."""
  return session.get("session_id", "").startswith("oos_")


def _is_correction(session: dict) -> bool:
  sid = session.get("session_id", "")
  return sid.startswith("corr_") or (session.get("user_turns", 1) or 1) > 1


def _is_correct(session: dict) -> bool:
  """In-scope: golden-matched AND meaningful. Out-of-scope: a clean decline."""
  cat = (
      session.get("metrics", {}).get("response_usefulness", {}).get("category")
  )
  if _is_oos(session):
    return cat == "declined"
  ge = session.get("golden_eval", {}) or {}
  return bool(ge.get("matched")) and cat == "meaningful"


def _parroted(session: dict) -> bool:
  for st in session.get("sub_trajectories", []) or []:
    if st.get("outcome") == "parroted":
      return True
  return False


def _summarize(report: dict) -> dict:
  sessions = report.get("sessions", [])
  oos = [s for s in sessions if _is_oos(s)]
  corr = [s for s in sessions if not _is_oos(s) and _is_correction(s)]
  single = [s for s in sessions if not _is_oos(s) and not _is_correction(s)]

  def rate(group):
    if not group:
      return 0.0, 0, 0
    ok = sum(1 for s in group if _is_correct(s))
    return round(ok / len(group) * 100, 1), ok, len(group)

  all_rate, all_ok, all_n = rate(sessions)
  s_rate, s_ok, s_n = rate(single)
  c_rate, c_ok, c_n = rate(corr)
  o_rate, o_ok, o_n = rate(oos)
  return {
      "overall": {"rate": all_rate, "correct": all_ok, "total": all_n},
      "single_turn": {"rate": s_rate, "correct": s_ok, "total": s_n},
      "corrections": {"rate": c_rate, "correct": c_ok, "total": c_n},
      "out_of_scope": {"rate": o_rate, "correct": o_ok, "total": o_n},
      "parroted": sum(1 for s in corr if _parroted(s)),
  }


def _tool_usage(report: dict):
  """Per-tool *selection* counts across sessions.

  Proves the tool-selection story directly (which tool the agent reached for),
  not just an aggregate grounding rate. Needs the structured ``tool_calls_detail``
  that ``run_agent.py`` now records; returns ``None`` for legacy reports scored
  before that field existed, so the behavior table is simply omitted.
  """
  sessions = report.get("sessions", [])
  if not any("tool_calls_detail" in s for s in sessions):
    return None
  by_tool: dict = {}
  any_tool = 0
  for s in sessions:
    names = {
        c.get("name")
        for c in (s.get("tool_calls_detail") or [])
        if c.get("name")
    }
    if names:
      any_tool += 1
    for n in names:
      by_tool[n] = by_tool.get(n, 0) + 1
  return {"any_tool": any_tool, "by_tool": by_tool, "total": len(sessions)}


def _tool_rows(v0_tools, v1_tools, short0="V0", short1="V1") -> list:
  """Behavior table rows: sessions that selected each tool, before vs after."""
  if not v0_tools or not v1_tools:
    return []
  total0, total1 = v0_tools["total"], v1_tools["total"]
  rows = [
      "## Tool selection (sessions that called each tool, held-out set)",
      "",
      f"| Behavior | {short0} | {short1} |",
      "| --- | --- | --- |",
      f"| Called any tool | {v0_tools['any_tool']}/{total0} |"
      f" {v1_tools['any_tool']}/{total1} |",
  ]
  for tool in sorted(set(v0_tools["by_tool"]) | set(v1_tools["by_tool"])):
    a = v0_tools["by_tool"].get(tool, 0)
    b = v1_tools["by_tool"].get(tool, 0)
    rows.append(f"| `{tool}` | {a}/{total0} | {b}/{total1} |")
  rows.append("")
  return rows


def _row(label, v0, v1):
  delta = round(v1["rate"] - v0["rate"], 1)
  sign = "+" if delta >= 0 else ""
  return (
      f"| {label} | {v0['rate']}% ({v0['correct']}/{v0['total']}) | "
      f"{v1['rate']}% ({v1['correct']}/{v1['total']}) | {sign}{delta}pp |"
  )


# Fine-grained quality dimensions (0-2), in report order, with display labels.
_DIM_LABELS = [
    ("correctness", "Correctness"),
    ("tool_usage", "Tool use"),
    ("specificity", "Specificity"),
    ("scope_compliance", "Scope compliance"),
    ("first_time_right", "First-time-right"),
]


def _dim_rows(v0_dims: dict, v1_dims: dict) -> list:
  """Build the per-dimension V0-vs-V1 rows (empty if dimensions weren't scored)."""
  rows = []
  for key, label in _DIM_LABELS:
    if key not in v0_dims and key not in v1_dims:
      continue
    a = float(v0_dims.get(key, 0))
    b = float(v1_dims.get(key, 0))
    delta = round(b - a, 2)
    sign = "+" if delta >= 0 else ""
    rows.append(f"| {label} | {a:.2f} | {b:.2f} | {sign}{delta} |")
  return rows


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--v0", required=True, help="Baseline report JSON")
  parser.add_argument("--v1", required=True, help="Candidate report JSON")
  parser.add_argument("-o", "--out", default=None, help="Markdown output path")
  parser.add_argument("--model", default="", help="Model label for the header")
  parser.add_argument(
      "--label0",
      default="V0 (flawed)",
      help="Column label for the baseline report (e.g. 'V1 (evolved)')",
  )
  parser.add_argument(
      "--label1",
      default="V1 (evolved)",
      help="Column label for the candidate report (e.g. 'V2 (round 2)')",
  )
  parser.add_argument(
      "--gate",
      action="store_true",
      help=(
          "Exit with code 3 when V1's overall rate is below V0's -- lets the"
          " demo gate the registry mirror on the held-out comparison"
      ),
  )
  args = parser.parse_args()

  with open(args.v0) as f:
    v0_report = json.load(f)
  with open(args.v1) as f:
    v1_report = json.load(f)
  v0 = _summarize(v0_report)
  v1 = _summarize(v1_report)
  v0_dims = v0_report.get("summary", {}).get("dimension_averages", {}) or {}
  v1_dims = v1_report.get("summary", {}).get("dimension_averages", {}) or {}
  v0_tools = _tool_usage(v0_report)
  v1_tools = _tool_usage(v1_report)

  hdr = f" ({args.model})" if args.model else ""
  # Short labels ("V0", "V1", "V2") for compact rows and the gate message.
  short0 = args.label0.split()[0]
  short1 = args.label1.split()[0]
  lines = [
      f"# Skill Evolution Result{hdr}",
      "",
      "Correctness on the held-out set: in-scope answers matched & meaningful,"
      " out-of-scope questions cleanly declined.",
      "",
      f"| Metric | {args.label0} | {args.label1} | Delta |",
      "| --- | --- | --- | --- |",
      _row("Overall", v0["overall"], v1["overall"]),
      _row("Single-turn", v0["single_turn"], v1["single_turn"]),
      _row("Corrections (anti-parrot)", v0["corrections"], v1["corrections"]),
  ]
  if v0["out_of_scope"]["total"] or v1["out_of_scope"]["total"]:
    lines.append(
        _row("Out-of-scope (declined)", v0["out_of_scope"], v1["out_of_scope"])
    )
  lines += [
      "",
      f"Parroted sub-trajectories: {short0}={v0['parroted']} "
      f" {short1}={v1['parroted']} "
      "(lower is better -- the agent re-verified instead of caving).",
      "",
  ]
  lines += _tool_rows(v0_tools, v1_tools, short0, short1)
  dim_rows = _dim_rows(v0_dims, v1_dims)
  if dim_rows:
    lines += [
        "## Quality dimensions (average 0-2, held-out set)",
        "",
        f"| Dimension | {short0} | {short1} | Delta |",
        "| --- | --- | --- | --- |",
        *dim_rows,
        "",
    ]
  out = "\n".join(lines)
  print(out)
  if args.out:
    with open(args.out, "w") as f:
      f.write(out.rstrip() + "\n")
    json_path = args.out.rsplit(".", 1)[0] + ".json"
    with open(json_path, "w") as f:
      json.dump(
          {
              "v0": v0,
              "v1": v1,
              "v0_dimensions": v0_dims,
              "v1_dimensions": v1_dims,
              "v0_tool_selection": v0_tools,
              "v1_tool_selection": v1_tools,
          },
          f,
          indent=2,
      )

  # Require V1 to *beat* V0 (a tie is not "better"), so a zero-gain V1 never
  # mints a new immutable registry revision. Note: this gates on the OVERALL
  # rate only -- a V1 that gains overall while regressing on a slice still
  # passes (acceptable for the demo; call it out if you productionize).
  if args.gate and v1["overall"]["rate"] <= v0["overall"]["rate"]:
    print(
        f"GATE: {short1} overall {v1['overall']['rate']}% <= {short0}"
        f" {v0['overall']['rate']}% -- {short1} should not be kept.",
        file=sys.stderr,
    )
    sys.exit(GATE_EXIT_CODE)


if __name__ == "__main__":
  main()
