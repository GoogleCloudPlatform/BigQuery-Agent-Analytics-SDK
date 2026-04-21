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

"""Agent improver: reads quality report JSON, improves prompts, extends eval cases.

This script:
1. Reads the quality report JSON (from quality_report.py --output-json)
2. Reads the current prompts.py and the golden eval set
3. Calls Gemini to generate an improved prompt
4. Runs the golden eval set against the candidate prompt (regression gate)
5. Extracts failed synthetic cases and adds them to the golden set
6. Writes the validated prompt to prompts.py
"""

import argparse
import asyncio
import json
import os
import re
import sys

from dotenv import load_dotenv
from google import genai
import google.auth
from google.genai.types import GenerateContentConfig

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_SCRIPT_DIR)

# Load environment and configure Vertex AI
_env_path = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
_project_id = os.getenv("PROJECT_ID") or _auth_project
_location = os.getenv("DEMO_AGENT_LOCATION", "us-central1")
os.environ["GOOGLE_CLOUD_PROJECT"] = _project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = _location
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
_PROMPTS_PATH = os.path.join(_DEMO_DIR, "agent", "prompts.py")
_EVAL_CASES_PATH = os.path.join(_DEMO_DIR, "eval", "eval_cases.json")


IMPROVER_PROMPT = """You are an agent prompt engineer. Your job is to improve an AI agent's system prompt based on quality evaluation results.

## Current Agent Prompt (version {current_version})
```
{current_prompt}
```

## Quality Report Summary
- Total sessions: {total_sessions}
- Meaningful (helpful): {meaningful} ({meaningful_rate}%)
- Partial: {partial}
- Unhelpful: {unhelpful} ({unhelpful_rate}%)

## Unhelpful and Partial Sessions (these need fixing)
{problem_sessions}

## Available Tools
The agent has these tools available:
- lookup_company_policy(topic): Looks up company policy. Topics: pto, sick_leave, remote_work, expenses, benefits, holidays
- get_current_date(): Returns today's date and day of week

## Your Task
Analyze the unhelpful/partial sessions and improve the agent prompt to fix these issues. The agent has tools that can answer these questions, but the prompt doesn't guide the agent to use them properly.

Rules:
1. Keep the prompt concise (under 500 words)
2. Add specific guidance for topics where the agent failed
3. Add instructions to ALWAYS use lookup_company_policy before answering policy questions
4. Add instructions to use get_current_date for any date-related questions
5. Keep all existing correct behavior
6. Do NOT remove information that was working correctly

Return your response as JSON with exactly these fields:
{{
  "improved_prompt": "the full improved prompt text",
  "changes_summary": "brief description of what changed and why"
}}

Return ONLY the JSON, no other text.
"""


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


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_quality_report(path: str) -> dict:
  """Load the JSON quality report."""
  with open(path) as f:
    return json.load(f)


def load_current_prompt() -> tuple[str, int]:
  """Read the current prompt from prompts.py."""
  with open(_PROMPTS_PATH) as f:
    content = f.read()

  version_match = re.search(r"CURRENT_VERSION\s*=\s*(\d+)", content)
  current_version = int(version_match.group(1)) if version_match else 1

  prompt_ref_match = re.search(r"CURRENT_PROMPT\s*=\s*PROMPT_V(\d+)", content)
  if prompt_ref_match:
    v = prompt_ref_match.group(1)
    pattern = rf'PROMPT_V{v}\s*=\s*"""(.*?)"""'
    prompt_match = re.search(pattern, content, re.DOTALL)
    if prompt_match:
      return prompt_match.group(1).strip(), current_version

  return "", current_version


def load_eval_cases() -> dict:
  """Load the golden eval cases."""
  with open(_EVAL_CASES_PATH) as f:
    return json.load(f)


# ---------------------------------------------------------------------------
# Improver
# ---------------------------------------------------------------------------


def format_problem_sessions(report: dict) -> str:
  """Format unhelpful/partial sessions for the improver prompt."""
  lines = []
  for session in report.get("sessions", []):
    metrics = session.get("metrics", {})
    usefulness = metrics.get("response_usefulness", {})
    grounding = metrics.get("task_grounding", {})

    cat = usefulness.get("category", "unknown")
    if cat not in ("unhelpful", "partial"):
      continue

    lines.append(f"### Session: {session.get('session_id', '?')}")
    lines.append(f"- Question: {session.get('question', '?')}")
    resp = session.get("response", "")
    if len(resp) > 300:
      resp = resp[:300] + "..."
    lines.append(f"- Response: {resp}")
    lines.append(f"- Usefulness: {cat} - {usefulness.get('justification', '')}")
    lines.append(
        f"- Grounding: {grounding.get('category', '?')} - "
        f"{grounding.get('justification', '')}"
    )
    lines.append("")

  return "\n".join(lines) if lines else "No problem sessions found."


def call_improver(
    current_prompt: str, current_version: int, report: dict
) -> dict:
  """Call Gemini to generate an improved prompt."""
  summary = report.get("summary", {})
  prompt = IMPROVER_PROMPT.format(
      current_version=current_version,
      current_prompt=current_prompt,
      total_sessions=summary.get("total_sessions", 0),
      meaningful=summary.get("meaningful", 0),
      meaningful_rate=summary.get("meaningful_rate", 0),
      partial=summary.get("partial", 0),
      unhelpful=summary.get("unhelpful", 0),
      unhelpful_rate=summary.get("unhelpful_rate", 0),
      problem_sessions=format_problem_sessions(report),
  )

  model_id = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")
  client = genai.Client()
  response = client.models.generate_content(
      model=model_id,
      contents=prompt,
      config=GenerateContentConfig(
          temperature=0.2,
          response_mime_type="application/json",
      ),
  )

  return json.loads(response.text)


# ---------------------------------------------------------------------------
# Golden eval gate
# ---------------------------------------------------------------------------


def _create_eval_agent(prompt: str):
  """Create a throwaway agent + runner for evaluation (no BQ logging)."""
  from google.adk.agents import Agent
  from google.adk.runners import InMemoryRunner

  sys.path.insert(0, _DEMO_DIR)
  from agent.tools import get_current_date
  from agent.tools import lookup_company_policy

  model_id = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")
  agent = Agent(
      name="eval_agent",
      model=model_id,
      description="An agent that answers questions about company policies.",
      instruction=prompt,
      tools=[lookup_company_policy, get_current_date],
  )
  runner = InMemoryRunner(agent=agent, app_name="eval_agent")
  return runner


async def _judge_cases(
    runner, cases: list[dict], label: str
) -> tuple[int, int, list[dict]]:
  """Run cases through agent and judge each response.

  Returns (passed_count, total, results_list).
  """
  from eval.run_eval import run_single_case

  model_id = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")
  client = genai.Client()
  passed = 0
  results = []

  for case in cases:
    result = await run_single_case(runner, case, user_id="eval")
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
    pass_fail = verdict.get("pass", False)
    if pass_fail:
      passed += 1
      print(f"    PASS: {case['id']}")
    else:
      print(f"    FAIL: {case['id']} - {verdict.get('reason', '')}")
    result["pass"] = pass_fail
    result["reason"] = verdict.get("reason", "")
    results.append(result)

  return passed, len(cases), results


async def run_golden_eval(candidate_prompt: str) -> tuple[bool, int, int]:
  """Run the golden eval set against a candidate prompt.

  Creates a throwaway agent with the candidate prompt (no BQ logging)
  and runs every golden eval case.  Each response is scored by a
  lightweight LLM judge.

  Returns:
      (passed_all, passed_count, total) where passed_all is True only
      if every golden case passes.
  """
  golden_cases = load_eval_cases().get("eval_cases", [])
  runner = _create_eval_agent(candidate_prompt)
  passed, total, _ = await _judge_cases(runner, golden_cases, "golden")
  return passed == total, passed, total


# ---------------------------------------------------------------------------
# Failure extraction
# ---------------------------------------------------------------------------


def extract_failed_cases(report: dict) -> list[dict]:
  """Extract failed synthetic sessions as new golden eval cases.

  Reads the quality report, finds unhelpful/partial sessions, and
  converts them into eval case format for the golden set.
  """
  new_cases = []
  for session in report.get("sessions", []):
    cat = (
        session.get("metrics", {})
        .get("response_usefulness", {})
        .get("category", "")
    )
    if cat not in ("unhelpful", "partial"):
      continue

    question = session.get("question", "")
    if not question:
      continue

    # Build an eval case from the failed session
    case_id = re.sub(r"[^a-z0-9]+", "_", question.lower().strip())[:40]
    case_id = f"extracted_{case_id.strip('_')}"

    new_cases.append(
        {
            "id": case_id,
            "question": question,
            "category": "unknown",
            "expected_tool": "lookup_company_policy",
            "notes": f"Extracted from failed synthetic traffic ({cat})",
        }
    )

  return new_cases


# ---------------------------------------------------------------------------
# Write prompt
# ---------------------------------------------------------------------------


def write_improved_prompt(
    improved_prompt: str,
    changes_summary: str,
    current_version: int,
) -> int:
  """Append a new prompt version to prompts.py."""
  new_version = current_version + 1

  with open(_PROMPTS_PATH) as f:
    content = f.read()

  if len(improved_prompt.strip()) < 50:
    raise ValueError("Improved prompt is too short, likely invalid")

  safe_summary = changes_summary.replace("\n", " ").strip()
  triple_q = '"' * 3
  safe_prompt = improved_prompt.replace(triple_q, '\\"\\"\\"')

  new_block = (
      f"\n\n# --- Version {new_version}: Improvements from cycle"
      f" {current_version} ---\n"
      f"# Changes: {safe_summary}\n"
      f'PROMPT_V{new_version} = """{safe_prompt}\n"""\n'
  )

  content = re.sub(
      r"CURRENT_PROMPT\s*=\s*PROMPT_V\d+",
      f"CURRENT_PROMPT = PROMPT_V{new_version}",
      content,
  )
  content = re.sub(
      r"CURRENT_VERSION\s*=\s*\d+",
      f"CURRENT_VERSION = {new_version}",
      content,
  )

  current_prompt_line = f"CURRENT_PROMPT = PROMPT_V{new_version}"
  content = content.replace(
      current_prompt_line,
      new_block + "\n" + current_prompt_line,
  )

  try:
    compile(content, _PROMPTS_PATH, "exec")
  except SyntaxError as e:
    raise ValueError(f"Generated prompts.py has syntax error: {e}")

  with open(_PROMPTS_PATH, "w") as f:
    f.write(content)

  return new_version


# ---------------------------------------------------------------------------
# Add cases to golden set
# ---------------------------------------------------------------------------

_REQUIRED_CASE_KEYS = {"id", "question", "category", "expected_tool"}


def add_eval_cases(new_cases: list[dict]) -> int:
  """Append new eval cases to the golden eval set."""
  data = load_eval_cases()
  existing_ids = {c["id"] for c in data["eval_cases"]}
  existing_questions = {c["question"] for c in data["eval_cases"]}

  added = 0
  for case in new_cases:
    missing = _REQUIRED_CASE_KEYS - set(case.keys())
    if missing:
      print(f"  Skipping invalid eval case (missing {missing}): {case}")
      continue
    # Deduplicate by both ID and question text
    if case["id"] in existing_ids or case["question"] in existing_questions:
      continue
    data["eval_cases"].append(case)
    existing_ids.add(case["id"])
    existing_questions.add(case["question"])
    added += 1

  json.dumps(data)  # Validate JSON

  with open(_EVAL_CASES_PATH, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

  return added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Improve agent prompt based on quality report"
  )
  parser.add_argument(
      "report_json",
      help="Path to the quality report JSON file",
  )
  args = parser.parse_args()

  # Load inputs
  print("\n=== Agent Improver ===\n")
  report = load_quality_report(args.report_json)
  current_prompt, current_version = load_current_prompt()
  print(f"  Current prompt version: v{current_version}")
  print(f"  Quality score: {report['summary']['meaningful_rate']}% meaningful")

  if report["summary"]["meaningful_rate"] >= 95:
    print("  Quality is already high (>=95%). No improvement needed.")
    return

  # Generate improved prompt, validated by golden eval (retry up to 3 times)
  for attempt in range(3):
    print(
        f"  Calling Gemini to generate improvements (attempt"
        f" {attempt + 1})..."
    )
    result = call_improver(current_prompt, current_version, report)
    candidate = result["improved_prompt"]

    if len(candidate.strip()) < 50:
      print("  Warning: candidate prompt too short, retrying...")
      if attempt == 2:
        raise ValueError("All attempts produced invalid prompts")
      continue

    # Golden eval gate: run all golden cases against the candidate
    golden_cases = load_eval_cases().get("eval_cases", [])
    print(f"  Running golden eval ({len(golden_cases)} cases)...")
    passed_all, passed, total = asyncio.run(run_golden_eval(candidate))
    print(f"  Golden eval: {passed}/{total} passed.")

    if not passed_all:
      print("  Golden eval FAILED: candidate breaks existing cases.")
      if attempt == 2:
        raise ValueError("Golden eval failed after 3 attempts")
      print("  Retrying with a new candidate...")
      continue

    print("  Golden eval PASSED: no regressions.")

    # Write the validated prompt
    try:
      new_version = write_improved_prompt(
          candidate,
          result["changes_summary"],
          current_version,
      )
      break
    except ValueError as e:
      print(f"  Warning: {e}")
      if attempt == 2:
        raise
      print("  Retrying...")

  print(f"  Written PROMPT_V{new_version} to prompts.py")
  print(f"  Changes: {result['changes_summary']}")

  # Extract failed synthetic cases and add to golden set
  failed_cases = extract_failed_cases(report)
  if failed_cases:
    added = add_eval_cases(failed_cases)
    print(
        f"  Extracted {len(failed_cases)} failed cases, added {added} new"
        " to golden eval set."
    )

  print(f"\n  v{current_version} -> v{new_version} complete.\n")


if __name__ == "__main__":
  main()
