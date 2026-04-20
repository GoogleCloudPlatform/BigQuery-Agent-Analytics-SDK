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
2. Reads the current prompts.py
3. Reads the current eval_cases.json
4. Calls Gemini to generate an improved prompt + new eval cases
5. Validates and writes the improvements
"""

import argparse
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
_env_path = os.path.join(_DEMO_DIR, "../../../.env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _project_id = google.auth.default()
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
  "changes_summary": "brief description of what changed and why",
  "new_eval_cases": [
    {{
      "id": "unique_id",
      "question": "a question that tests the fix",
      "category": "the policy category",
      "expected_tool": "lookup_company_policy",
      "notes": "what this tests"
    }}
  ]
}}

Generate 2-4 new eval cases that specifically test the issues you fixed.
Return ONLY the JSON, no other text.
"""


def load_quality_report(path):
  """Load the JSON quality report."""
  with open(path) as f:
    return json.load(f)


def load_current_prompt():
  """Read the current prompt from prompts.py."""
  with open(_PROMPTS_PATH) as f:
    content = f.read()

  # Extract CURRENT_VERSION
  version_match = re.search(r"CURRENT_VERSION\s*=\s*(\d+)", content)
  current_version = int(version_match.group(1)) if version_match else 1

  # Extract CURRENT_PROMPT value by finding the variable it points to
  prompt_ref_match = re.search(r"CURRENT_PROMPT\s*=\s*PROMPT_V(\d+)", content)
  if prompt_ref_match:
    v = prompt_ref_match.group(1)
    # Find that prompt's content
    pattern = rf'PROMPT_V{v}\s*=\s*"""(.*?)"""'
    prompt_match = re.search(pattern, content, re.DOTALL)
    if prompt_match:
      return prompt_match.group(1).strip(), current_version

  return "", current_version


def load_eval_cases():
  """Load current eval cases."""
  with open(_EVAL_CASES_PATH) as f:
    return json.load(f)


def format_problem_sessions(report):
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


def call_improver(current_prompt, current_version, report):
  """Call Gemini to generate improvements."""
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


def write_improved_prompt(improved_prompt, changes_summary, current_version):
  """Append a new prompt version to prompts.py."""
  new_version = current_version + 1

  with open(_PROMPTS_PATH) as f:
    content = f.read()

  # Validate the new prompt is reasonable
  if len(improved_prompt.strip()) < 50:
    raise ValueError("Improved prompt is too short, likely invalid")

  # Sanitize for safe Python embedding
  safe_summary = changes_summary.replace("\n", " ").strip()
  triple_q = '"' * 3
  safe_prompt = improved_prompt.replace(triple_q, '\\"\\"\\"')

  # Build the new version block
  new_block = (
      f"\n\n# --- Version {new_version}: Improvements from cycle"
      f" {current_version} ---\n"
      f"# Changes: {safe_summary}\n"
      f'PROMPT_V{new_version} = """{safe_prompt}\n"""\n'
  )

  # Replace CURRENT_PROMPT and CURRENT_VERSION
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

  # Insert new version block before CURRENT_PROMPT line
  current_prompt_line = f"CURRENT_PROMPT = PROMPT_V{new_version}"
  content = content.replace(
      current_prompt_line,
      new_block + "\n" + current_prompt_line,
  )

  # Validate the result is valid Python
  try:
    compile(content, _PROMPTS_PATH, "exec")
  except SyntaxError as e:
    raise ValueError(f"Generated prompts.py has syntax error: {e}")

  with open(_PROMPTS_PATH, "w") as f:
    f.write(content)

  return new_version


def add_eval_cases(new_cases):
  """Append new eval cases to eval_cases.json."""
  data = load_eval_cases()
  existing_ids = {c["id"] for c in data["eval_cases"]}

  added = 0
  for case in new_cases:
    if case["id"] not in existing_ids:
      data["eval_cases"].append(case)
      existing_ids.add(case["id"])
      added += 1

  # Validate JSON is valid
  json.dumps(data)

  with open(_EVAL_CASES_PATH, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

  return added


def main():
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

  # Check if improvement is needed
  if report["summary"]["meaningful_rate"] >= 95:
    print("  Quality is already high (>=95%). No improvement needed.")
    return

  # Call Gemini to generate improvements (retry on syntax errors)
  for attempt in range(3):
    print(
        f"  Calling Gemini to generate improvements (attempt {attempt + 1})..."
    )
    result = call_improver(current_prompt, current_version, report)
    try:
      new_version = write_improved_prompt(
          result["improved_prompt"],
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

  # Add new eval cases
  new_cases = result.get("new_eval_cases", [])
  if new_cases:
    added = add_eval_cases(new_cases)
    print(f"  Added {added} new eval cases to eval_cases.json")

  print(f"\n  v{current_version} -> v{new_version} complete.")
  print(f"  Re-run evaluation to measure improvement.\n")


if __name__ == "__main__":
  main()
