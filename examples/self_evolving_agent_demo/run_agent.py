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

"""Run demo eval questions through the ADK agent with BigQuery logging."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
if _DEMO_DIR not in sys.path:
  sys.path.insert(0, _DEMO_DIR)


def _load_cases(path: str) -> list[dict[str, Any]]:
  with open(path) as f:
    return json.load(f)["eval_cases"]


def _part_text(part: Any) -> str:
  text = getattr(part, "text", None)
  return text or ""


def _part_function_name(part: Any) -> str | None:
  function_call = getattr(part, "function_call", None)
  if not function_call:
    return None
  return getattr(function_call, "name", None)


async def _run_case(
    runner: Any,
    case: dict[str, Any],
    *,
    user_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
  from google.genai.types import Content
  from google.genai.types import Part

  session = await runner.session_service.create_session(
      app_name=runner.app_name,
      user_id=user_id,
  )
  user_message = Content(role="user", parts=[Part(text=case["question"])])
  response_text = ""
  tools_called: list[str] = []

  async def _consume() -> None:
    nonlocal response_text
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=user_message,
    ):
      if not event.content or not event.content.parts:
        continue
      for part in event.content.parts:
        response_text += _part_text(part)
        tool_name = _part_function_name(part)
        if tool_name:
          tools_called.append(tool_name)

  await asyncio.wait_for(_consume(), timeout=timeout_seconds)

  expected_tool = case.get("expected_tool", "")
  avoid_tool = case.get("avoid_tool", "")
  expected_tool_used = expected_tool in tools_called if expected_tool else True
  avoid_tool_used = avoid_tool in tools_called if avoid_tool else False
  # Quality checks answerability; avoid-tool overuse is the separate
  # efficiency signal that drives this demo's evolution.
  quality_passed = bool(response_text.strip()) and expected_tool_used
  return {
      "case_id": case["id"],
      "question": case["question"],
      "expected_tool": expected_tool,
      "avoid_tool": avoid_tool,
      "tools_called": tools_called,
      "expected_tool_used": expected_tool_used,
      "avoid_tool_used": avoid_tool_used,
      "quality_passed": quality_passed,
      "response": response_text.strip(),
      "session_id": session.id,
  }


async def _run_all(args: argparse.Namespace) -> list[dict[str, Any]]:
  from agent.agent import APP_NAME
  from agent.agent import bq_logging_plugin
  from agent.agent import PROMPT_VERSION
  from agent.agent import root_agent
  from google.adk.runners import InMemoryRunner

  cases = _load_cases(args.eval_cases)
  runner = InMemoryRunner(
      agent=root_agent,
      app_name=APP_NAME,
      plugins=[bq_logging_plugin],
  )
  semaphore = asyncio.Semaphore(args.max_concurrency)

  async def _guarded(i: int, case: dict[str, Any]) -> dict[str, Any]:
    async with semaphore:
      print(f"  [{i}/{len(cases)}] {case['id']}: {case['question']}")
      try:
        result = await _run_case(
            runner,
            case,
            user_id=f"{args.label}_user",
            timeout_seconds=args.timeout,
        )
      except Exception as exc:
        result = {
            "case_id": case["id"],
            "question": case["question"],
            "expected_tool": case.get("expected_tool", ""),
            "avoid_tool": case.get("avoid_tool", ""),
            "tools_called": [],
            "expected_tool_used": False,
            "avoid_tool_used": False,
            "quality_passed": False,
            "response": f"ERROR: {exc}",
            "session_id": "",
        }
      result["label"] = args.label
      result["prompt_version"] = PROMPT_VERSION
      answer = result["response"].replace("\n", " ").strip()
      if len(answer) > 180:
        answer = answer[:180] + "..."
      print(f"        tools: {', '.join(result['tools_called']) or 'none'}")
      print(f"        pass:  {result['quality_passed']}")
      print(f"        ans:   {answer}")
      return result

  return list(
      await asyncio.gather(
          *[_guarded(i, case) for i, case in enumerate(cases, 1)]
      )
  )


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Run self-evolving agent demo eval traffic."
  )
  parser.add_argument(
      "--eval-cases",
      default=os.path.join(_DEMO_DIR, "eval", "eval_cases.json"),
  )
  parser.add_argument(
      "--output-dir", default=os.path.join(_DEMO_DIR, "reports")
  )
  parser.add_argument("--label", default="baseline")
  parser.add_argument("--max-concurrency", type=int, default=2)
  parser.add_argument("--timeout", type=int, default=180)
  parser.add_argument(
      "--allow-failures",
      action="store_true",
      help="Write results without exiting nonzero on quality failures.",
  )
  args = parser.parse_args()

  os.makedirs(args.output_dir, exist_ok=True)
  results = asyncio.run(_run_all(args))

  labeled_path = os.path.join(
      args.output_dir, f"latest_eval_results_{args.label}.json"
  )
  latest_path = os.path.join(args.output_dir, "latest_eval_results.json")
  for path in (labeled_path, latest_path):
    with open(path, "w") as f:
      json.dump(results, f, indent=2)
      f.write("\n")
  print("")
  print(f"  Results saved to: {labeled_path}")

  failures = sum(1 for r in results if not r.get("quality_passed"))
  if failures and not args.allow_failures:
    sys.exit(1)


if __name__ == "__main__":
  main()
