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

"""Company info agent for the improvement cycle demo.

This agent answers employee questions about company policies. It starts
with intentional flaws in v1 that are progressively fixed by the
improvement cycle.
"""

import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
import google.auth
from google.genai import types

from .prompts import CURRENT_PROMPT
from .tools import get_current_date
from .tools import lookup_company_policy

# Load environment
_env_path = os.path.join(os.path.dirname(__file__), "../.env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, project_id = google.auth.default()

DATASET_ID = os.getenv("DATASET_ID", "agent_logs")
DATASET_LOCATION = os.getenv("BQ_LOCATION", "us-central1")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
MODEL_ID = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")
LOCATION = os.getenv("DEMO_AGENT_LOCATION", "us-central1")

os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# Build the agent
root_agent = Agent(
    name="company_info_agent",
    model=Gemini(
        model=MODEL_ID,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="An agent that answers questions about company policies.",
    instruction=CURRENT_PROMPT,
    tools=[
        lookup_company_policy,
        get_current_date,
    ],
)

# BigQuery telemetry
bq_config = BigQueryLoggerConfig(
    enabled=True,
    max_content_length=500 * 1024,
    batch_size=1,
    shutdown_timeout=10.0,
)
bq_logging_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=project_id,
    dataset_id=DATASET_ID,
    table_id=TABLE_ID,
    location=DATASET_LOCATION,
    config=bq_config,
)

app = root_agent
