#!/usr/bin/env python3
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

"""Compare baseline and evolved demo runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
if _DEMO_DIR not in sys.path:
  sys.path.insert(0, _DEMO_DIR)

from analytics.session_metrics import fetch_session_metrics
from analytics.session_metrics import load_quality_summary
from analytics.session_metrics import load_session_ids
from analytics.session_metrics import require_complete_session_metrics
from analytics.session_metrics import summarize


def _load(
    path: str, attempts: int, wait_seconds: int, label: str
) -> tuple[dict, dict]:
  ids = load_session_ids(path)
  rows = fetch_session_metrics(
      ids,
      attempts=attempts,
      wait_seconds=wait_seconds,
  )
  try:
    require_complete_session_metrics(rows, ids, label=label)
  except RuntimeError as exc:
    raise SystemExit(str(exc)) from exc
  return summarize(rows), load_quality_summary(path)


def _pct_delta(before: float, after: float) -> float | None:
  if before == 0:
    return 0.0 if after == 0 else None
  return round((after - before) / before, 4)


def _format_pct_delta(value: float | None) -> str:
  if value is None:
    return "n/a"
  return f"{value:+.1%}"


def _read_candidate_metadata(output_dir: str) -> dict[str, str]:
  path = os.path.join(output_dir, "candidate_prompt.json")
  if not os.path.exists(path):
    return {}
  with open(path) as f:
    data = json.load(f)
  return {
      "changes_summary": str(data.get("changes_summary", "")),
      "source": str(data.get("source", "")),
  }


def _write_markdown_report(
    *,
    output_path: str,
    result: dict[str, Any],
) -> str:
  """Write a concise operator-facing before/after report."""
  output_dir = os.path.dirname(output_path)
  path = os.path.join(output_dir, "comparison.md")
  before_quality = result["before"]["quality"]
  after_quality = result["after"]["quality"]
  before_metrics = result["before"]["metrics"]
  after_metrics = result["after"]["metrics"]
  deltas = result["deltas"]
  candidate_metadata = _read_candidate_metadata(output_dir)
  candidate_summary = candidate_metadata.get("changes_summary", "")
  candidate_source = candidate_metadata.get("source", "")
  prompt_diff_path = os.path.join(output_dir, "prompt_diff.md")
  has_prompt_diff = os.path.exists(prompt_diff_path)

  with open(path, "w") as f:
    f.write("# Agent V1 -> Generated V2 Comparison\n\n")
    f.write("## What Trace Analysis Changed\n\n")
    if candidate_summary:
      f.write(f"{candidate_summary}\n\n")
    else:
      f.write(
          "The generated V2 prompt was created from the baseline trace "
          "summary, tool counts, quality summary, and available tool "
          "signatures.\n\n"
      )
    if candidate_source:
      f.write(f"Candidate source: `{candidate_source}`.\n\n")
    if has_prompt_diff:
      f.write("See `prompt_diff.md` for the exact prompt-level diff.\n\n")

    f.write("## Before / After Metrics\n\n")
    f.write("| Metric | V1 | Generated V2 | Delta |\n")
    f.write("|---|---:|---:|---:|\n")
    f.write(
        "| Quality pass rate | "
        f"{before_quality['pass_rate']:.0%} | "
        f"{after_quality['pass_rate']:.0%} | "
        f"{after_quality['pass_rate'] - before_quality['pass_rate']:+.0%} |\n"
    )
    f.write(
        "| Avg total tokens | "
        f"{before_metrics['avg_total_tokens']} | "
        f"{after_metrics['avg_total_tokens']} | "
        f"{_format_pct_delta(deltas['avg_total_tokens_pct'])} |\n"
    )
    f.write(
        "| Avg tool calls | "
        f"{before_metrics['avg_tool_calls']} | "
        f"{after_metrics['avg_tool_calls']} | "
        f"{_format_pct_delta(deltas['avg_tool_calls_pct'])} |\n"
    )
    f.write(
        "| Broad lookup calls | "
        f"{before_metrics['total_broad_lookup_calls']} | "
        f"{after_metrics['total_broad_lookup_calls']} | "
        f"{deltas['broad_lookup_calls']:+d} |\n"
    )
    f.write(
        "| Tool errors | "
        f"{before_metrics['total_tool_errors']} | "
        f"{after_metrics['total_tool_errors']} | "
        f"{after_metrics['total_tool_errors'] - before_metrics['total_tool_errors']:+d} |\n"
    )

    f.write("\n## Acceptance Gates\n\n")
    for name, passed in result["gates"].items():
      f.write(f"- `{name}`: {passed}\n")

    f.write("\n## Why This Demonstrates Self-Evolution\n\n")
    f.write(
        "The demo does not just compare two static prompts. It uses the "
        "baseline BigQuery traces to identify broad-tool overuse and token "
        "waste, generates a replacement prompt from that evidence, reruns "
        "the agent, then records whether the generated V2 preserved quality "
        "while reducing the measured waste.\n"
    )
  return path


def main() -> None:
  parser = argparse.ArgumentParser(description="Compare two demo runs.")
  parser.add_argument("--before", required=True)
  parser.add_argument("--after", required=True)
  parser.add_argument("--output", default=None)
  parser.add_argument("--min-token-reduction", type=float, default=0.05)
  parser.add_argument("--wait-seconds", type=int, default=15)
  parser.add_argument("--attempts", type=int, default=6)
  parser.add_argument(
      "--fail-on-gate-failure",
      action="store_true",
      help="Exit nonzero when acceptance gates fail.",
  )
  args = parser.parse_args()

  before_summary, before_quality = _load(
      args.before, args.attempts, args.wait_seconds, "baseline"
  )
  after_summary, after_quality = _load(
      args.after, args.attempts, args.wait_seconds, "evolved"
  )
  token_delta = _pct_delta(
      before_summary["avg_total_tokens"],
      after_summary["avg_total_tokens"],
  )
  tool_delta = _pct_delta(
      before_summary["avg_tool_calls"],
      after_summary["avg_tool_calls"],
  )
  broad_delta = (
      after_summary["total_broad_lookup_calls"]
      - before_summary["total_broad_lookup_calls"]
  )
  gates = {
      "quality_not_regressed": (
          after_quality["pass_rate"] >= before_quality["pass_rate"]
      ),
      "tokens_reduced": (
          token_delta is not None and token_delta <= -args.min_token_reduction
      ),
      "broad_lookup_reduced": broad_delta < 0,
      "tool_errors_clear": after_summary["total_tool_errors"] == 0,
  }
  result: dict[str, Any] = {
      "before": {"quality": before_quality, "metrics": before_summary},
      "after": {"quality": after_quality, "metrics": after_summary},
      "deltas": {
          "avg_total_tokens_pct": token_delta,
          "avg_tool_calls_pct": tool_delta,
          "broad_lookup_calls": broad_delta,
      },
      "gates": gates,
      "passed": all(gates.values()),
  }

  if args.output:
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    markdown_path = _write_markdown_report(
        output_path=args.output,
        result=result,
    )
    result["artifacts"] = {
        "markdown_report": markdown_path,
        "prompt_diff": os.path.join(
            os.path.dirname(args.output), "prompt_diff.md"
        ),
    }
    with open(args.output, "w") as f:
      json.dump(result, f, indent=2)
      f.write("\n")

  print("")
  print("  Before/after self-evolution report")
  print("  ----------------------------------")
  print(
      f"  Quality pass rate:  {before_quality['pass_rate']:.0%}"
      f" -> {after_quality['pass_rate']:.0%}"
  )
  print(
      f"  Avg total tokens:   {before_summary['avg_total_tokens']}"
      f" -> {after_summary['avg_total_tokens']}"
      f" ({_format_pct_delta(token_delta)})"
  )
  print(
      f"  Avg tool calls:     {before_summary['avg_tool_calls']}"
      f" -> {after_summary['avg_tool_calls']}"
      f" ({_format_pct_delta(tool_delta)})"
  )
  print(
      "  Broad lookup calls: "
      f"{before_summary['total_broad_lookup_calls']}"
      f" -> {after_summary['total_broad_lookup_calls']}"
  )
  print("  Gates:")
  for name, passed in gates.items():
    print(f"    {name}: {passed}")
  if args.output:
    print(f"  Report: {args.output}")
    print(f"  Markdown: {markdown_path}")

  if args.fail_on_gate_failure and not result["passed"]:
    sys.exit(1)


if __name__ == "__main__":
  main()
