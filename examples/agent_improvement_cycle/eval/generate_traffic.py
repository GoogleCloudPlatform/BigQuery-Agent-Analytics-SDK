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

"""Generate synthetic user traffic for the agent improvement cycle.

Calls Gemini to produce diverse, realistic employee questions about
company policies.  The generated questions are intentionally different
from the golden eval set so they simulate real-world traffic the agent
has not been specifically tuned for.
"""

import argparse
import json
import logging
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _DEMO_DIR)

import agent_improvement  # noqa: F401 -- configures logging

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from google import genai
import google.auth
from google.genai.types import GenerateContentConfig
from google.genai.types import HttpOptions
from google.genai.types import HttpRetryOptions

# Load environment
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

TRAFFIC_PROMPT = """You are generating realistic employee questions for a company HR policy chatbot.

The chatbot has tools that contain SPECIFIC data on these topics:

- **pto**: 20 days/year, accrued monthly, up to 5 days rollover, 2-week advance notice for >3 day requests
- **sick_leave**: 10 days/year, no rollover, doctor's note required after 3 consecutive days
- **remote_work**: up to 3 days/week with manager approval, core hours 10am-3pm
- **expenses**: meals up to $75/day, receipts required over $25, travel over $500 needs pre-approval, 30-day submission window
- **benefits**: PPO/HMO health plans (80% company-paid), dental (full preventive, 80% major), vision ($200 frames every 2 years), 401k with 4% match (vested after 1 year), parental leave (16 weeks primary, 8 weeks secondary)
- **holidays**: 11 paid holidays per year (New Year's, MLK Day, Presidents' Day, Memorial Day, July 4th, Labor Day, Thanksgiving + day after, Christmas Eve, Christmas, New Year's Eve)

The chatbot also has a tool that returns today's date.

## Rules
1. ONLY ask questions that the data above can answer. Do NOT ask about topics outside this data (e.g., mileage, home office equipment, gym memberships, tuition, sabbaticals).
2. Mix direct factual questions ("What is the meal limit?") with situational ones ("I have a $40 dinner receipt from a client meeting -- do I need to submit it?").
3. Include 1-2 date-related questions that require knowing today's date (e.g., "Is there a holiday coming up this month?", "When is the next company holiday?").
4. Cover all six topics but **weight toward expenses, benefits, and holidays** -- at least 6 out of {count} questions should be about these three topics (2+ each). The remaining questions can cover pto, sick_leave, or remote_work. This weighting ensures the traffic stresses topics that are harder for the agent.
5. Vary the phrasing naturally.

## Existing questions to AVOID duplicating
{existing_questions}

Generate exactly {count} questions.

Return JSON with exactly this structure:
{{
  "traffic_cases": [
    {{
      "id": "traffic_<short_descriptive_id>",
      "question": "the employee's question",
      "category": "one of: pto, sick_leave, remote_work, expenses, benefits, holidays"
    }}
  ]
}}

Return ONLY the JSON, no other text.
"""


def load_existing_questions() -> list[str]:
  """Load questions from the golden eval set to avoid duplicates."""
  eval_path = os.path.join(_SCRIPT_DIR, "eval_cases.json")
  with open(eval_path) as f:
    data = json.load(f)
  return [c["question"] for c in data.get("eval_cases", [])]


def generate_traffic(count: int = 10) -> list[dict]:
  """Call Gemini to generate synthetic traffic cases."""
  existing = load_existing_questions()
  existing_formatted = "\n".join(f"- {q}" for q in existing)

  prompt = TRAFFIC_PROMPT.format(
      existing_questions=existing_formatted,
      count=count,
  )

  model_id = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")
  client = genai.Client(
      http_options=HttpOptions(
          retry_options=HttpRetryOptions(
              attempts=3,
              initial_delay=10.0,
              http_status_codes=[429],
          )
      )
  )
  # Gemini occasionally returns malformed JSON even with
  # response_mime_type="application/json". Retry up to 3 times.
  for attempt in range(3):
    response = client.models.generate_content(
        model=model_id,
        contents=prompt,
        config=GenerateContentConfig(
            temperature=0.8,
            response_mime_type="application/json",
        ),
    )
    try:
      result = json.loads(response.text)
      cases = result.get("traffic_cases", [])

      # Dedup against existing golden eval questions
      existing_lower = {q.lower() for q in existing}
      cases = [
          c
          for c in cases
          if c.get("question", "").lower() not in existing_lower
      ]

      if len(cases) < count // 2:
        if attempt < 2:
          logger.warning(
              "Only got %d cases (wanted %d), retrying (%d/3)...",
              len(cases),
              count,
              attempt + 1,
          )
          continue
      # Gemini may return more cases than requested; truncate to count.
      return cases[:count]
    except json.JSONDecodeError:
      if attempt < 2:
        logger.warning(
            "Gemini returned malformed JSON, retrying (%d/3)...",
            attempt + 1,
        )
      else:
        raise
  return []


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Generate synthetic user traffic for the agent"
  )
  parser.add_argument(
      "--count",
      type=int,
      default=10,
      help="Number of synthetic questions to generate (default: 10)",
  )
  parser.add_argument(
      "--output",
      type=str,
      default=None,
      help="Output file path (default: eval/synthetic_traffic.json)",
  )
  args = parser.parse_args()

  output_path = args.output or os.path.join(
      _SCRIPT_DIR, "synthetic_traffic.json"
  )
  os.makedirs(os.path.dirname(output_path), exist_ok=True)

  logger.info("Generating %d synthetic traffic cases...", args.count)
  cases = generate_traffic(count=args.count)
  logger.info("Generated %d cases.", len(cases))

  # Wrap in the same format as eval_cases.json so run_eval.py can consume it
  output = {
      "eval_set_id": "synthetic_traffic",
      "name": "Synthetic User Traffic",
      "description": "Auto-generated by Gemini to simulate real user questions.",
      "eval_cases": cases,
  }

  with open(output_path, "w") as f:
    json.dump(output, f, indent=2)
    f.write("\n")

  logger.info("Written to %s", output_path)


if __name__ == "__main__":
  main()
