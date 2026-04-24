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

"""Run evaluation cases against an ADK agent.

Two modes:
  - Default: send traffic through the real agent with BQ logging.
  - --golden: LLM judge mode via EvalRunner (no BQ, pass/fail scoring).

Supports ``--agent-config`` to load any agent's config.json.
Falls back to the demo's company_info_agent when not provided.
"""

import logging
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("google.genai").setLevel(logging.ERROR)

import asyncio
import json
import os
import sys

from google.adk.runners import InMemoryRunner
from google.genai.types import Content
from google.genai.types import Part

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_SCRIPT_DIR)

# Add parent to path so we can import agent_improvement
sys.path.insert(0, _DEMO_DIR)

_DEFAULT_CONFIG = os.path.join(_DEMO_DIR, "config.json")


def load_eval_cases(path: str | None = None) -> list[dict]:
  """Load evaluation cases from JSON file."""
  path = path or os.path.join(_SCRIPT_DIR, "eval_cases.json")
  with open(path) as f:
    data = json.load(f)
  return data["eval_cases"]


async def run_single_case(
    runner: InMemoryRunner, case: dict, user_id: str = "eval_user"
) -> dict:
  """Run a single eval case and return the response."""
  session = await runner.session_service.create_session(
      app_name=runner.app_name,
      user_id=user_id,
  )

  user_message = Content(
      role="user",
      parts=[Part(text=case["question"])],
  )

  response_text = ""
  async for event in runner.run_async(
      user_id=user_id,
      session_id=session.id,
      new_message=user_message,
  ):
    if event.content and event.content.parts:
      for part in event.content.parts:
        if part.text:
          response_text += part.text

  return {
      "case_id": case["id"],
      "question": case["question"],
      "category": case.get("category", ""),
      "response": response_text,
      "session_id": session.id,
  }


async def run_all_cases(
    eval_cases_path: str | None = None,
    config_path: str | None = None,
) -> list[dict]:
  """Run all eval cases with BQ logging (traffic mode)."""
  from agent_improvement import load_agent_module

  mod, cfg = load_agent_module(config_path or _DEFAULT_CONFIG)
  eval_path = eval_cases_path or cfg["eval_cases_path"]

  cases = load_eval_cases(eval_path)
  print(f"\nRunning {len(cases)} cases...\n")

  runner = InMemoryRunner(
      agent=mod.root_agent,
      app_name=cfg["app_name"],
      plugins=[mod.bq_logging_plugin],
  )

  async def _run_one(i: int, case: dict) -> dict:
    try:
      result = await asyncio.wait_for(
          run_single_case(runner, case), timeout=120
      )
      resp_text = result["response"].replace("\n", " ").strip()
      print(f"  [{i}/{len(cases)}] {case['id']}: {case['question']}")
      print(f"           -> {resp_text}")
      return result
    except asyncio.TimeoutError:
      print(f"  [{i}/{len(cases)}] {case['id']}: {case['question']}")
      print(f"           -> TIMEOUT (120s)")
      return {
          "case_id": case["id"],
          "question": case["question"],
          "category": case.get("category", ""),
          "response": "ERROR: Timeout after 120s",
          "session_id": "",
      }
    except Exception as e:
      print(f"  [{i}/{len(cases)}] {case['id']}: {case['question']}")
      print(f"           -> ERROR: {e}")
      return {
          "case_id": case["id"],
          "question": case["question"],
          "category": case.get("category", ""),
          "response": f"ERROR: {e}",
          "session_id": "",
      }

  results = await asyncio.gather(
      *[_run_one(i, case) for i, case in enumerate(cases, 1)]
  )
  results = list(results)

  print(f"\nCompleted {len(results)}/{len(cases)} cases.")
  print("Sessions logged to BigQuery via telemetry plugin.")
  return results


async def run_golden_eval(
    eval_cases_path: str | None = None,
    config_path: str | None = None,
) -> list[dict]:
  """Run eval cases with LLM judge, no BQ logging."""
  from agent_improvement import EvalRunner
  from agent_improvement import load_config

  config = load_config(config_path or _DEFAULT_CONFIG)
  eval_path = eval_cases_path or config.eval_cases_path

  eval_runner = EvalRunner(
      agent_factory=config.agent_factory,
      model_id=config.model_id,
      judge_prompt=config.judge_prompt,
  )

  cases = eval_runner.load_eval_cases(eval_path)
  _, version = config.prompt_adapter.read_prompt()
  print(f"\n  Evaluating {len(cases)} cases with prompt V{version}")
  print("  (LLM judge, no BigQuery logging)\n")

  prompt, _ = config.prompt_adapter.read_prompt()
  all_passed, passed, total, results = await eval_runner.run_golden_eval(
      prompt, eval_path
  )

  rate = round(100 * passed / total) if total else 0
  print(f"\n  Result: {passed}/{total} passed ({rate}%)")
  if all_passed:
    print("  All cases pass.")
  else:
    print(f"  {total - passed} case(s) failed.")

  return results


def main() -> None:
  import argparse

  parser = argparse.ArgumentParser(
      description="Run eval cases against the agent"
  )
  parser.add_argument(
      "--eval-cases",
      type=str,
      default=None,
      help="Path to eval_cases.json (default: from agent config)",
  )
  parser.add_argument(
      "--golden",
      action="store_true",
      help=(
          "LLM judge mode: run cases through a local agent (no BQ"
          " logging) and score each response pass/fail."
      ),
  )
  parser.add_argument(
      "--agent-config",
      type=str,
      default=None,
      help=(
          "Path to the agent's config.json. If not"
          " provided, uses the demo's company_info_agent config."
      ),
  )
  args = parser.parse_args()

  if args.golden:
    results = asyncio.run(run_golden_eval(args.eval_cases, args.agent_config))
    failed = sum(1 for r in results if not r.get("pass", False))
  else:
    results = asyncio.run(run_all_cases(args.eval_cases, args.agent_config))
    failed = 0

  # Write results to a file for reference
  results_path = os.path.join(_DEMO_DIR, "reports", "latest_eval_results.json")
  os.makedirs(os.path.dirname(results_path), exist_ok=True)
  with open(results_path, "w") as f:
    json.dump(results, f, indent=2)
  print(f"Results saved to {results_path}")

  if failed:
    sys.exit(1)


if __name__ == "__main__":
  main()
