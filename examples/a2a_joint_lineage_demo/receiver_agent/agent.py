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

"""Receiver-side audience-risk governance agent.

Exports:

  * ``root_agent`` — pure-LLM ``Agent`` (no tools); the prompt
    constrains output shape so the SDK extractor finds three
    candidate options per invocation.
  * ``build_receiver_plugin`` — factory that constructs a
    ``BigQueryAgentAnalyticsPlugin`` writing to
    ``<RECEIVER_DATASET_ID>.<RECEIVER_TABLE_ID>``. The receiver
    server (``run_receiver_server.py``) attaches the returned plugin
    to a custom ``Runner``, since ``to_a2a()``'s default runner
    builder does not accept ``plugins=`` and would otherwise drop
    receiver telemetry on the floor.
  * ``APP_NAME`` — ADK app name for the receiver runner.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
import google.auth
from google.genai import types

from .prompts import SYSTEM_PROMPT

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_HERE)
_env_path = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
RECEIVER_DATASET_ID = os.getenv("RECEIVER_DATASET_ID", "a2a_receiver_demo")
RECEIVER_TABLE_ID = os.getenv("RECEIVER_TABLE_ID", "agent_events")
MODEL_ID = os.getenv("DEMO_AGENT_MODEL", "gemini-2.5-pro")
AGENT_LOCATION = os.getenv("DEMO_AGENT_LOCATION", "us-central1")

os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID or ""
os.environ["GOOGLE_CLOUD_LOCATION"] = AGENT_LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

APP_NAME = "a2a_joint_lineage_receiver"


root_agent = Agent(
    name="audience_risk_reviewer",
    model=Gemini(
        model=MODEL_ID,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Audience-risk governance reviewer. Evaluates three audience "
        "options per request and returns a structured "
        "SELECTED/DROPPED breakdown with explicit rejection "
        "rationale for the SDK extractor."
    ),
    instruction=SYSTEM_PROMPT,
    tools=[],  # pure LLM; no tools in Phase 1
)


def build_receiver_plugin() -> BigQueryAgentAnalyticsPlugin:
  """Constructs the receiver-side BQ AA plugin.

  Returned plugin is intended to be passed via ``Runner(plugins=[...])``
  in ``run_receiver_server.py``. Do not rely on ``to_a2a()``'s default
  runner — it builds its own ``Runner`` with no ``plugins=`` arg, so
  attaching the plugin requires the explicit-runner path.
  """
  return BigQueryAgentAnalyticsPlugin(
      project_id=PROJECT_ID,
      dataset_id=RECEIVER_DATASET_ID,
      table_id=RECEIVER_TABLE_ID,
      location=DATASET_LOCATION,
      config=BigQueryLoggerConfig(
          enabled=True,
          max_content_length=500 * 1024,
          batch_size=1,
          shutdown_timeout=15.0,
      ),
  )
