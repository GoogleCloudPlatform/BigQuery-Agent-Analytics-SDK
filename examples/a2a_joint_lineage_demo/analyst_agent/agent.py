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

"""Audit-analyst agent.

Closes the demo loop: the caller and receiver agents produce trace
rows in their `agent_events` tables; the SDK materializes per-org
context graphs; `build_joint_graph.py` stitches a redacted joint
property graph in the auditor dataset; this agent then queries that
joint graph back, answering natural-language audit questions.

The analyst's own traces (its tool calls and reasoning) are logged
to `<ANALYST_DATASET>.agent_events` via the BQ AA Plugin. Operators
can build a separate per-analyst context graph from those rows if
they want audit-of-the-audit lineage.
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
from .tools import ANALYST_TOOLS

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_HERE)
_env_path = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
ANALYST_DATASET_ID = os.getenv("ANALYST_DATASET_ID", "a2a_analyst_demo")
ANALYST_TABLE_ID = os.getenv("ANALYST_TABLE_ID", "agent_events")
MODEL_ID = os.getenv("DEMO_AGENT_MODEL", "gemini-3.1-pro-preview")
# Gemini 3.x preview models are only published at locations/global;
# AGENT_LOCATION below is required when the default MODEL_ID is in
# use. The fallback override to gemini-2.5-pro is documented in
# the demo README and uses DEMO_AGENT_LOCATION=us-central1.
AGENT_LOCATION = os.getenv("DEMO_AGENT_LOCATION", "global")

os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID or ""
os.environ["GOOGLE_CLOUD_LOCATION"] = AGENT_LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

APP_NAME = "a2a_joint_lineage_analyst"


root_agent = Agent(
    name="audit_analyst",
    model=Gemini(
        model=MODEL_ID,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Audit-analyst agent that answers natural-language questions "
        "about the joint A2A context graph by calling four bounded "
        "BigQuery query tools."
    ),
    instruction=SYSTEM_PROMPT,
    tools=ANALYST_TOOLS,
)


_bq_config = BigQueryLoggerConfig(
    enabled=True,
    max_content_length=500 * 1024,
    batch_size=1,
    shutdown_timeout=15.0,
)
bq_logging_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=PROJECT_ID,
    dataset_id=ANALYST_DATASET_ID,
    table_id=ANALYST_TABLE_ID,
    location=DATASET_LOCATION,
    config=_bq_config,
)
