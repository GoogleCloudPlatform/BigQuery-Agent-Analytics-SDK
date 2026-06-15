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

"""Analyze baseline traces and promote an evolved prompt when warranted."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from typing import Any

_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
if _DEMO_DIR not in sys.path:
  sys.path.insert(0, _DEMO_DIR)

from agent.prompt_store import read_state
from agent.prompt_store import write_prompt
from agent.tools import DEMO_TOOLS
from analytics.session_metrics import fetch_session_metrics
from analytics.session_metrics import fetch_tool_counts
from analytics.session_metrics import load_quality_summary
from analytics.session_metrics import load_session_ids
from analytics.session_metrics import require_complete_session_metrics
from analytics.session_metrics import run_sdk_evaluators
from analytics.session_metrics import summarize

DEFAULT_MIN_BROAD_LOOKUP_RATE = 0.5
DEFAULT_MAX_AVG_TOOL_CALLS = 2.0
MIN_GENERATED_PROMPT_CHARS = 120


def _tool_signatures() -> str:
  lines = []
  for tool in DEMO_TOOLS:
    name = getattr(tool, "__name__", "unknown")
    doc = (getattr(tool, "__doc__", "") or "").strip().splitlines()[0]
    lines.append(f"- {name}: {doc}")
  return "\n".join(lines)


def _load_eval_contract(path: str) -> list[dict[str, str]]:
  """Load the deterministic routing contract from run-agent results."""
  with open(path) as f:
    rows = json.load(f)
  if isinstance(rows, dict):
    rows = rows.get("sessions", [])
  contract = []
  for row in rows:
    contract.append(
        {
            "case_id": str(row.get("case_id", "")),
            "question": str(row.get("question", "")),
            "expected_tool": str(row.get("expected_tool", "")),
            "avoid_tool": str(row.get("avoid_tool", "")),
        }
    )
  return contract


def _observations(
    summary: dict[str, Any],
    *,
    token_budget: int,
    min_broad_lookup_rate: float,
    max_avg_tool_calls: float,
) -> list[str]:
  obs = []
  if summary["avg_total_tokens"] > token_budget:
    obs.append("Average total tokens are above the configured session budget.")
  if summary["broad_lookup_session_rate"] >= min_broad_lookup_rate:
    obs.append(
        "Most sessions used the broad basketball reference tool even though each "
        "eval case has a narrow tool path."
    )
  if summary["avg_tool_calls"] > max_avg_tool_calls:
    obs.append(
        "Average tool calls are high for one-question single-turn tasks."
    )
  if not obs:
    obs.append("No clear token or tool-use hotspot was detected.")
  return obs


def _generate_candidate_prompt(
    *,
    current_prompt: str,
    observations: list[str],
    summary: dict[str, Any],
    tool_counts: list[dict[str, Any]],
    quality: dict[str, Any],
    eval_contract: list[dict[str, str]],
    model_id: str,
) -> dict[str, str]:
  """Generate an improved prompt from trace analysis."""
  prompt = f"""\
You are improving an ADK basketball analytics agent prompt from its own trace
analytics. Generate a complete replacement system prompt.

Current prompt:
```
{current_prompt}
```

Available tools:
{_tool_signatures()}

SDK trace summary:
{json.dumps(summary, indent=2)}

Tool counts:
{json.dumps(tool_counts, indent=2)}

Deterministic quality summary:
{json.dumps(quality, indent=2)}

Deterministic routing contract from the eval run:
{json.dumps(eval_contract, indent=2)}

Observed issues:
{json.dumps(observations, indent=2)}

Requirements for the improved prompt:
- Keep the same agent role and basketball analytics task.
- Remove the broad-first behavior that caused lookup_basketball_reference overuse.
- Instruct the agent to choose the narrowest sufficient tool.
- Preserve every expected_tool / avoid_tool pair in the routing contract.
- Treat a named-team strategy, strengths, profile, or late-game offense
  question as a single-team question that calls get_team_profile.
- Treat a named-player scoring, strengths, profile, or quick-read question
  as a single-player question that calls get_player_stats.
- Use lookup_basketball_reference only for league-wide or unsupported ambiguous
  questions where no narrow player, team, or comparison tool fits.
- Remove the fixed five-section scouting-report format from the old prompt.
- Keep final answers to at most four bullets or 120 words.
- Preserve answer quality and tool grounding.
- Keep final answers concise.
- Do not mention trace analytics, BigQuery, SDKs, prompts, or optimization to users.

Return JSON with exactly:
{{
  "improved_prompt": "full replacement system prompt",
  "changes_summary": "one sentence explaining the improvement"
}}
"""
  from google import genai
  from google.genai.types import GenerateContentConfig

  client = genai.Client()
  response = client.models.generate_content(
      model=model_id,
      contents=prompt,
      config=GenerateContentConfig(
          temperature=0.2,
          response_mime_type="application/json",
      ),
  )
  data = json.loads(response.text or "{}")
  improved = str(data.get("improved_prompt", "")).strip()
  changes = str(data.get("changes_summary", "")).strip()
  # A complete system prompt should at least include role and routing guidance.
  if len(improved) < MIN_GENERATED_PROMPT_CHARS:
    raise ValueError("Generated prompt was too short.")
  return {
      "source": "model",
      "improved_prompt": improved,
      "changes_summary": changes or "Generated from SDK trace analysis.",
  }


def _write_prompt_diff(
    *,
    output_dir: str,
    before_prompt: str,
    after_prompt: str,
    observations: list[str],
    changes_summary: str,
) -> str:
  """Write a human-readable V1 -> generated V2 prompt diff."""
  diff_lines = list(
      difflib.unified_diff(
          before_prompt.splitlines(),
          after_prompt.splitlines(),
          fromfile="agent_v1_prompt",
          tofile="generated_agent_v2_prompt",
          lineterm="",
      )
  )
  path = os.path.join(output_dir, "prompt_diff.md")
  with open(path, "w") as f:
    f.write("# Prompt Diff: Agent V1 -> Generated V2\n\n")
    f.write("## Trace Signal\n\n")
    for obs in observations:
      f.write(f"- {obs}\n")
    f.write("\n## Generated Improvement\n\n")
    f.write(f"{changes_summary}\n\n")
    f.write("## Unified Diff\n\n")
    f.write("```diff\n")
    f.write("\n".join(diff_lines))
    f.write("\n```\n")
  return path


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Analyze demo sessions and evolve the active prompt."
  )
  parser.add_argument("--sessions", required=True)
  parser.add_argument(
      "--output-dir", default=os.path.join(_DEMO_DIR, "reports")
  )
  parser.add_argument("--token-budget", type=int, default=12000)
  parser.add_argument("--max-cost-usd", type=float, default=0.05)
  parser.add_argument("--max-turns", type=int, default=4)
  parser.add_argument("--min-quality-pass-rate", type=float, default=1.0)
  parser.add_argument(
      "--min-broad-lookup-rate",
      type=float,
      default=DEFAULT_MIN_BROAD_LOOKUP_RATE,
  )
  parser.add_argument(
      "--max-avg-tool-calls",
      type=float,
      default=DEFAULT_MAX_AVG_TOOL_CALLS,
  )
  parser.add_argument(
      "--generator-model",
      default=os.getenv(
          "SELF_EVOLVING_PROMPT_GENERATOR_MODEL", "gemini-2.5-flash"
      ),
  )
  parser.add_argument("--wait-seconds", type=int, default=15)
  parser.add_argument("--attempts", type=int, default=6)
  args = parser.parse_args()

  os.makedirs(args.output_dir, exist_ok=True)
  session_ids = load_session_ids(args.sessions)
  if not session_ids:
    raise SystemExit(f"No session IDs found in {args.sessions}")

  rows = fetch_session_metrics(
      session_ids,
      attempts=args.attempts,
      wait_seconds=args.wait_seconds,
  )
  try:
    require_complete_session_metrics(rows, session_ids, label="baseline")
  except RuntimeError as exc:
    raise SystemExit(str(exc)) from exc

  summary = summarize(rows)
  tool_counts = fetch_tool_counts(session_ids)
  quality = load_quality_summary(args.sessions)
  eval_contract = _load_eval_contract(args.sessions)
  sdk_reports = run_sdk_evaluators(
      session_ids,
      token_budget=args.token_budget,
      max_cost_usd=args.max_cost_usd,
      max_turns=args.max_turns,
  )
  observations = _observations(
      summary,
      token_budget=args.token_budget,
      min_broad_lookup_rate=args.min_broad_lookup_rate,
      max_avg_tool_calls=args.max_avg_tool_calls,
  )
  current_state = read_state()
  should_promote = (
      current_state["version"] == "v1"
      and quality["pass_rate"] >= args.min_quality_pass_rate
      and (
          summary["broad_lookup_session_rate"] >= args.min_broad_lookup_rate
          or summary["avg_total_tokens"] > args.token_budget
      )
  )

  evolution = {
      "from_version": current_state["version"],
      "to_version": current_state["version"],
      "promoted": False,
      "rationale": "No candidate prompt generated.",
  }
  if should_promote:
    try:
      candidate = _generate_candidate_prompt(
          current_prompt=current_state["prompt"],
          observations=observations,
          summary=summary,
          tool_counts=tool_counts,
          quality=quality,
          eval_contract=eval_contract,
          model_id=args.generator_model,
      )
    except Exception as exc:
      raise SystemExit(
          "Prompt generation failed; no fallback prompt was promoted. "
          f"Original error: {exc}"
      ) from exc
    candidate_path = os.path.join(args.output_dir, "candidate_prompt.json")
    with open(candidate_path, "w") as f:
      json.dump(candidate, f, indent=2)
      f.write("\n")
    prompt_diff_path = _write_prompt_diff(
        output_dir=args.output_dir,
        before_prompt=current_state["prompt"],
        after_prompt=candidate["improved_prompt"],
        observations=observations,
        changes_summary=candidate["changes_summary"],
    )
    rationale = (
        "Generated V2 from SDK trace analysis because baseline quality met "
        "the configured gate and an operational waste signal was detected."
    )
    write_prompt("v2", candidate["improved_prompt"], rationale)
    evolution = {
        "from_version": "v1",
        "to_version": "v2",
        "promoted": True,
        "rationale": rationale,
        "candidate_path": candidate_path,
        "prompt_diff_path": prompt_diff_path,
        "changes_summary": candidate["changes_summary"],
        "candidate_source": candidate.get("source", "model"),
        "generator_model": args.generator_model,
    }

  report = {
      "quality": quality,
      "session_summary": summary,
      "tool_counts": tool_counts,
      "sdk_evaluator_reports": sdk_reports,
      "observations": observations,
      "evolution": evolution,
  }
  output_path = os.path.join(args.output_dir, "self_evolution_analysis.json")
  with open(output_path, "w") as f:
    json.dump(report, f, indent=2)
    f.write("\n")

  print("")
  print("  SDK-backed self-evolution analysis")
  print("  ----------------------------------")
  print(f"  Sessions:              {summary['sessions']}")
  print(f"  Avg total tokens:      {summary['avg_total_tokens']}")
  print(f"  Avg tool calls:        {summary['avg_tool_calls']}")
  print(
      "  Broad lookup sessions: "
      f"{summary['sessions_with_broad_lookup']}/{summary['sessions']}"
  )
  print(f"  Quality pass rate:     {quality['pass_rate']:.0%}")
  print(
      f"  Evolution:             {evolution['from_version']} -> {evolution['to_version']}"
  )
  print(f"  Promoted:              {evolution['promoted']}")
  if evolution.get("candidate_path"):
    print(f"  Candidate prompt:      {evolution['candidate_path']}")
  if evolution.get("prompt_diff_path"):
    print(f"  Prompt diff:           {evolution['prompt_diff_path']}")
  print(f"  Report:                {output_path}")


if __name__ == "__main__":
  main()
