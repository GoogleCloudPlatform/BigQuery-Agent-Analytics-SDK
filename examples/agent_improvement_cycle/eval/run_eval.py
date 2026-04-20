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

"""Run evaluation cases against the company info agent.

Sends each question from eval_cases.json to the agent via ADK Runner.
Sessions are logged to BigQuery via the agent's telemetry plugin.
"""

import asyncio
import json
import os
import sys
import uuid

from google.adk.runners import InMemoryRunner
from google.genai.types import Content
from google.genai.types import Part

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_SCRIPT_DIR)

# Add parent to path so we can import the agent
sys.path.insert(0, _DEMO_DIR)


def load_eval_cases(path=None):
  """Load evaluation cases from JSON file."""
  path = path or os.path.join(_SCRIPT_DIR, "eval_cases.json")
  with open(path) as f:
    data = json.load(f)
  return data["eval_cases"]


async def run_single_case(runner, case, user_id="eval_user"):
  """Run a single eval case and return the response."""
  session_id = f"eval_{case['id']}_{uuid.uuid4().hex[:8]}"

  session = await runner.session_service.create_session(
      app_name="company_info_agent",
      user_id=user_id,
      session_id=session_id,
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


async def run_all_cases(eval_cases_path=None):
  """Run all eval cases and print results."""
  cases = load_eval_cases(eval_cases_path)
  print(f"\nRunning {len(cases)} eval cases...\n")

  from agent.agent import bq_logging_plugin
  from agent.agent import root_agent

  runner = InMemoryRunner(
      agent=root_agent,
      app_name="company_info_agent",
      plugins=[bq_logging_plugin],
  )

  results = []
  for i, case in enumerate(cases, 1):
    print(f"  [{i}/{len(cases)}] {case['id']}: {case['question'][:60]}...")
    try:
      result = await run_single_case(runner, case)
      resp_preview = result["response"][:80].replace("\n", " ")
      print(f"           -> {resp_preview}...")
      results.append(result)
    except Exception as e:
      print(f"           -> ERROR: {e}")
      results.append(
          {
              "case_id": case["id"],
              "question": case["question"],
              "category": case.get("category", ""),
              "response": f"ERROR: {e}",
              "session_id": "",
          }
      )

  print(f"\nCompleted {len(results)}/{len(cases)} cases.")
  print("Sessions logged to BigQuery via telemetry plugin.")
  return results


def main():
  import argparse

  parser = argparse.ArgumentParser(
      description="Run eval cases against the agent"
  )
  parser.add_argument(
      "--eval-cases",
      type=str,
      default=None,
      help="Path to eval_cases.json (default: eval/eval_cases.json)",
  )
  args = parser.parse_args()

  results = asyncio.run(run_all_cases(args.eval_cases))

  # Write results to a file for reference
  results_path = os.path.join(_DEMO_DIR, "reports", "latest_eval_results.json")
  os.makedirs(os.path.dirname(results_path), exist_ok=True)
  with open(results_path, "w") as f:
    json.dump(results, f, indent=2)
  print(f"Results saved to {results_path}")


if __name__ == "__main__":
  main()
