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

"""Media-planner agent + BQ AA Plugin wiring.

This module exports:

  * ``root_agent`` — a Google ADK ``Agent`` configured with the five
    decision-commit tools and a system prompt that requires explicit
    candidate enumeration before each tool call.
  * ``bq_logging_plugin`` — a ``BigQueryAgentAnalyticsPlugin`` instance
    bound to the demo's project / dataset / table. The driver
    (`run_agent.py`) attaches this plugin to ``InMemoryRunner``; the
    plugin writes plugin-shape rows into ``agent_events`` for every
    invocation, agent, LLM, tool, and HITL event.
  * ``APP_NAME`` — the ADK app name used when constructing the
    runner.

Trace rows produced by the plugin are exactly what the SDK's
``ContextGraphManager.build_context_graph`` extraction path expects.
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
from .tools import AGENT_TOOLS

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_HERE)
_env_path = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_ID = os.getenv("DATASET_ID", "decision_lineage_rich_demo")
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
MODEL_ID = os.getenv("DEMO_AGENT_MODEL", "gemini-2.5-pro")
AGENT_LOCATION = os.getenv("DEMO_AGENT_LOCATION", "us-central1")

# google-adk + google-genai pick these env vars up at construction
# time. Set them so the runner uses Vertex AI, the right project, and
# the right region for the live LLM calls.
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID or ""
os.environ["GOOGLE_CLOUD_LOCATION"] = AGENT_LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

APP_NAME = "decision_lineage_demo"


root_agent = Agent(
    name="media_planner",
    model=Gemini(
        model=MODEL_ID,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "A media-planner agent that picks audience, budget, creative, "
        "channel, and schedule for athletic-footwear / apparel "
        "campaigns. Enumerates candidates and rationale at every "
        "decision."
    ),
    instruction=SYSTEM_PROMPT,
    tools=AGENT_TOOLS,
)


_bq_config = BigQueryLoggerConfig(
    enabled=True,
    max_content_length=500 * 1024,
    batch_size=1,
    shutdown_timeout=15.0,
)
bq_logging_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    table_id=TABLE_ID,
    location=DATASET_LOCATION,
    config=_bq_config,
)
