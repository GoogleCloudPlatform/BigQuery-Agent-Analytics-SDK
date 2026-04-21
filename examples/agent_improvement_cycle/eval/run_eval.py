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

from dotenv import load_dotenv
from google import genai
from google.adk.runners import InMemoryRunner
import google.auth
from google.genai.types import Content
from google.genai.types import GenerateContentConfig
from google.genai.types import Part

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_SCRIPT_DIR)

# Load environment and configure Vertex AI
_env_path = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
_project_id = os.getenv("PROJECT_ID") or _auth_project
os.environ["GOOGLE_CLOUD_PROJECT"] = _project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv(
    "DEMO_AGENT_LOCATION", "us-central1"
)
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# Add parent to path so we can import the agent
sys.path.insert(0, _DEMO_DIR)


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
) -> list[dict]:
  """Run all eval cases and print results."""
  cases = load_eval_cases(eval_cases_path)
  print(f"\nRunning {len(cases)} cases...\n")

  from agent.agent import bq_logging_plugin
  from agent.agent import root_agent

  runner = InMemoryRunner(
      agent=root_agent,
      app_name="company_info_agent",
      plugins=[bq_logging_plugin],
  )

  async def _run_one(i: int, case: dict) -> dict:
    try:
      result = await run_single_case(runner, case)
      resp_text = result["response"].replace("\n", " ").strip()
      print(f"  [{i}/{len(cases)}] {case['id']}: {case['question']}")
      print(f"           -> {resp_text}")
      return result
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


GOLDEN_JUDGE_PROMPT = """You are evaluating an AI agent's response to a policy question.

Question: {question}
Response: {response}

Return JSON with exactly these fields:
{{
  "pass": true or false,
  "reason": "one-sentence explanation"
}}

A response PASSES if it provides a specific, substantive answer to the question.
A response FAILS if it says "I don't know", defers to HR, or gives vague/generic information without specifics.
Return ONLY the JSON, no other text.
"""


async def run_golden_eval(eval_cases_path: str | None = None) -> list[dict]:
  """Run eval cases with LLM judge, no BQ logging.

  Creates a throwaway agent with the current prompt and scores each
  response with a lightweight LLM judge.  Returns results with
  pass/fail verdicts.

  Args:
      eval_cases_path: Path to eval cases JSON. If None, defaults to
          eval_cases.json (the golden set). Can point to any file
          with the same schema (e.g. synthetic traffic).
  """
  from agent.tools import get_current_date
  from agent.tools import lookup_company_policy
  from google.adk.agents import Agent

  sys.path.insert(0, _DEMO_DIR)
  from agent.prompts import CURRENT_PROMPT

  cases = load_eval_cases(eval_cases_path)
  model_id = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")

  test_agent = Agent(
      name="golden_eval_agent",
      model=model_id,
      description="An agent that answers questions about company policies.",
      instruction=CURRENT_PROMPT,
      tools=[lookup_company_policy, get_current_date],
  )

  runner = InMemoryRunner(agent=test_agent, app_name="golden_eval")
  client = genai.Client()

  from agent.prompts import CURRENT_VERSION

  print(f"\n  Evaluating {len(cases)} cases with prompt V{CURRENT_VERSION}")
  print("  (LLM judge, no BigQuery logging)\n")

  async def _eval_one(i: int, case: dict) -> dict:
    result = await run_single_case(runner, case, user_id="golden_eval")

    judge_prompt = GOLDEN_JUDGE_PROMPT.format(
        question=case["question"],
        response=result["response"][:500],
    )
    judge_response = client.models.generate_content(
        model=model_id,
        contents=judge_prompt,
        config=GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    verdict = json.loads(judge_response.text)
    result["pass"] = verdict.get("pass", False)
    result["reason"] = verdict.get("reason", "")

    tag = "PASS" if result["pass"] else "FAIL"
    suffix = "" if result["pass"] else f" - {result['reason']}"
    print(f"  [{i}/{len(cases)}] {tag}: {case['id']}{suffix}")
    return result

  results = list(
      await asyncio.gather(
          *[_eval_one(i, case) for i, case in enumerate(cases, 1)]
      )
  )

  passed = sum(1 for r in results if r.get("pass", False))
  total = len(cases)
  rate = round(100 * passed / total) if total else 0
  print(f"\n  Result: {passed}/{total} passed ({rate}%)")
  if passed == total:
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
      help="Path to eval_cases.json (default: eval/eval_cases.json)",
  )
  parser.add_argument(
      "--golden",
      action="store_true",
      help=(
          "LLM judge mode: run cases through a local agent (no BQ"
          " logging) and score each response pass/fail. Uses --eval-cases"
          " if provided, otherwise defaults to eval_cases.json."
      ),
  )
  args = parser.parse_args()

  if args.golden:
    results = asyncio.run(run_golden_eval(args.eval_cases))
    failed = sum(1 for r in results if not r.get("pass", False))
  else:
    results = asyncio.run(run_all_cases(args.eval_cases))
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
