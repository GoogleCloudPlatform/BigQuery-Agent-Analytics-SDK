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

"""Caller-side media-planning supervisor agent.

Exports:

  * ``root_agent`` — the caller ``Agent`` with four local decision
    tools plus an ``AgentTool`` wrapping a ``RemoteA2aAgent`` that
    points at the receiver's A2A endpoint. The remote tool produces
    the caller-side ``A2A_INTERACTION`` event the auditor join uses.
  * ``bq_logging_plugin`` — a ``BigQueryAgentAnalyticsPlugin``
    instance that writes caller spans to
    ``<CALLER_DATASET_ID>.<CALLER_TABLE_ID>``.
  * ``APP_NAME`` — ADK app name for the caller runner.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.models import Gemini
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
from google.adk.tools.agent_tool import AgentTool
import google.auth
from google.genai import types

from .prompts import SYSTEM_PROMPT
from .tools import CALLER_LOCAL_TOOLS

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_HERE)
_env_path = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
CALLER_DATASET_ID = os.getenv("CALLER_DATASET_ID", "a2a_caller_demo")
CALLER_TABLE_ID = os.getenv("CALLER_TABLE_ID", "agent_events")
# Default to Gemini 3.1 Pro preview. gemini-3-pro-preview was
# discontinued in March 2026; gemini-3.1-pro-preview is the current
# supported 3.x ID on Vertex AI. Verified live: this model is only
# published at locations/global — a regional lookup returns 404. The
# `global` default for AGENT_LOCATION below is required when this
# default is in use. To fall back to gemini-2.5-pro on projects
# without preview access, also override DEMO_AGENT_LOCATION=us-central1.
MODEL_ID = os.getenv("DEMO_AGENT_MODEL", "gemini-3.1-pro-preview")
AGENT_LOCATION = os.getenv("DEMO_AGENT_LOCATION", "global")
RECEIVER_A2A_URL = os.getenv("RECEIVER_A2A_URL", "http://127.0.0.1:8000")
# Standard A2A protocol exposes the agent card at this well-known
# path; ``adk-python``'s ``to_a2a()`` helper serves it from
# ``A2AStarletteApplication``.
RECEIVER_AGENT_CARD_URL = os.getenv(
    "RECEIVER_AGENT_CARD_URL",
    f"{RECEIVER_A2A_URL.rstrip('/')}/.well-known/agent-card.json",
)

# google-adk + google-genai pick these env vars up at construction
# time. Set them so the runner uses Vertex AI for the live LLM calls.
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID or ""
os.environ["GOOGLE_CLOUD_LOCATION"] = AGENT_LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

APP_NAME = "a2a_joint_lineage_caller"


_audience_risk_reviewer = RemoteA2aAgent(
    name="audience_risk_reviewer",
    agent_card=RECEIVER_AGENT_CARD_URL,
    description=(
        "Remote governance agent that reviews three candidate "
        "audiences for policy and brand-risk concerns and returns a "
        "structured selection."
    ),
)

_audience_risk_tool = AgentTool(agent=_audience_risk_reviewer)


root_agent = Agent(
    name="media_planner_supervisor",
    model=Gemini(
        model=MODEL_ID,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Media-planning supervisor that picks goal, budget, channel "
        "mix, and creative theme locally, and delegates audience-risk "
        "review to a remote governance agent over A2A."
    ),
    instruction=SYSTEM_PROMPT,
    tools=[*CALLER_LOCAL_TOOLS, _audience_risk_tool],
)


_bq_config = BigQueryLoggerConfig(
    enabled=True,
    max_content_length=500 * 1024,
    batch_size=1,
    shutdown_timeout=15.0,
)
bq_logging_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=PROJECT_ID,
    dataset_id=CALLER_DATASET_ID,
    table_id=CALLER_TABLE_ID,
    location=DATASET_LOCATION,
    config=_bq_config,
)
