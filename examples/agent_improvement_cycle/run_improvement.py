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

"""Run the improvement cycle for the company_info_agent.

This is the demo entry point that wires the reusable
``agent_improvement`` module to the company_info_agent's shared
agent factory, tools, and eval set.

Usage:
    python run_improvement.py <report.json>
    python run_improvement.py --from-eval-results <eval_results.json>
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
import google.auth

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Load environment
_env_path = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
_project_id = os.getenv("PROJECT_ID") or _auth_project
os.environ["GOOGLE_CLOUD_PROJECT"] = _project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv(
    "DEMO_AGENT_LOCATION", "us-central1"
)
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# Ensure the demo directory is on the path
sys.path.insert(0, _SCRIPT_DIR)

from agent.agent import AGENT_TOOLS
from agent.agent import create_agent
from agent_improvement import ImprovementConfig
from agent_improvement import PythonFilePromptAdapter
from agent_improvement import run_improvement

_MODEL_ID = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")
_PROMPTS_PATH = os.path.join(_SCRIPT_DIR, "agent", "prompts.py")
_EVAL_CASES_PATH = os.path.join(_SCRIPT_DIR, "eval", "eval_cases.json")


def _build_config() -> ImprovementConfig:
  return ImprovementConfig(
      agent_factory=create_agent,
      agent_name="company_info_agent",
      agent_tools=AGENT_TOOLS,
      prompt_adapter=PythonFilePromptAdapter(_PROMPTS_PATH),
      eval_cases_path=_EVAL_CASES_PATH,
      model_id=_MODEL_ID,
      max_attempts=3,
  )


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Improve agent prompt based on quality report"
  )
  parser.add_argument(
      "report_json",
      help="Path to the quality report JSON file",
  )
  parser.add_argument(
      "--from-eval-results",
      action="store_true",
      help=(
          "Treat report_json as golden eval results (from run_eval.py"
          " --golden) instead of a BigQuery quality report."
      ),
  )
  args = parser.parse_args()

  config = _build_config()
  result = asyncio.run(
      run_improvement(
          config,
          report_path=args.report_json,
          from_eval_results=args.from_eval_results,
      )
  )

  if result["new_version"] == result["old_version"]:
    sys.exit(1)


if __name__ == "__main__":
  main()
