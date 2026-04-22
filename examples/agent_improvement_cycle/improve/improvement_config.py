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

"""Improvement config for the company_info_agent demo.

This is the reference template for plugging any ADK agent into the
improvement cycle. To use this with your own agent:

1. Copy this file into your agent's ``improve/`` directory.
2. Update the imports, paths, and factory function for your agent.
3. Run: ``./run_cycle.sh --agent-config /path/to/your/improvement_config.py``

Required exports:
    build_config()     -> ImprovementConfig (used by run_improvement.py)
    get_root_agent()   -> Agent             (used by run_eval.py traffic mode)
    get_bq_plugin()    -> plugin            (used by run_eval.py traffic mode)
    APP_NAME           : str                (used by run_cycle.sh)
    PROMPTS_PATH       : str                (used by run_cycle.sh)
    EVAL_CASES_PATH    : str                (used by run_cycle.sh)
    TRAFFIC_GENERATOR  : str                (used by run_cycle.sh)
"""

import os
import sys

from dotenv import load_dotenv
import google.auth

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_CONFIG_DIR)

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

# Add paths so we can import the agent and the improvement module
sys.path.insert(0, _DEMO_DIR)

from agent.agent import AGENT_TOOLS
from agent.agent import bq_logging_plugin
from agent.agent import create_agent
from agent.agent import root_agent
from agent_improvement import ImprovementConfig
from agent_improvement import PythonFilePromptAdapter

# ---------------------------------------------------------------------------
# Metadata (read by run_cycle.sh)
# ---------------------------------------------------------------------------

APP_NAME = "company_info_agent"
PROMPTS_PATH = os.path.join(_DEMO_DIR, "agent", "prompts.py")
EVAL_CASES_PATH = os.path.join(_DEMO_DIR, "eval", "eval_cases.json")
TRAFFIC_GENERATOR = os.path.join(_DEMO_DIR, "eval", "generate_traffic.py")

_MODEL_ID = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Config builder (used by run_improvement.py)
# ---------------------------------------------------------------------------


def build_config() -> ImprovementConfig:
  """Build the ImprovementConfig for this agent."""
  return ImprovementConfig(
      agent_factory=create_agent,
      agent_name=APP_NAME,
      agent_tools=AGENT_TOOLS,
      prompt_adapter=PythonFilePromptAdapter(PROMPTS_PATH),
      eval_cases_path=EVAL_CASES_PATH,
      model_id=_MODEL_ID,
      max_attempts=3,
  )


# ---------------------------------------------------------------------------
# Agent access (used by run_eval.py for traffic mode with BQ logging)
# ---------------------------------------------------------------------------


def get_root_agent():
  """Return the root agent instance."""
  return root_agent


def get_bq_plugin():
  """Return the BigQuery logging plugin."""
  return bq_logging_plugin
