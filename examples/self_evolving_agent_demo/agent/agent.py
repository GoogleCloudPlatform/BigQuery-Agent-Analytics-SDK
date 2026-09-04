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

"""ADK sample analytics agent used by the self-evolving demo."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
import google.auth
from google.genai import types

from .prompt_store import read_prompt
from .tools import DEMO_TOOLS

_DEMO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_ENV_PATH):
  load_dotenv(dotenv_path=_ENV_PATH)

try:
  _, _auth_project = google.auth.default()
except Exception:
  _auth_project = None

PROJECT_ID = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
if not PROJECT_ID:
  PROJECT_ID = _auth_project
if not PROJECT_ID:
  raise RuntimeError(
      "Could not resolve PROJECT_ID from .env, GOOGLE_CLOUD_PROJECT, or ADC. "
      "Run ./setup.sh or `gcloud config set project YOUR_PROJECT_ID`."
  )

DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
DATASET_ID = os.getenv("SELF_EVOLVING_DATASET_ID", "self_evolving_agent_demo")
TABLE_ID = os.getenv("SELF_EVOLVING_TABLE_ID", "agent_events")
MODEL_ID = os.getenv("SELF_EVOLVING_AGENT_MODEL", "gemini-2.5-flash")
AGENT_LOCATION = os.getenv("SELF_EVOLVING_AGENT_LOCATION", "us-central1")
APP_NAME = "self_evolving_agent"


def _configure_environment() -> None:
  """Configure Vertex AI environment variables required by ADK Gemini."""
  os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
  os.environ["GOOGLE_CLOUD_LOCATION"] = AGENT_LOCATION
  os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"


def create_agent(prompt: str, model_id: str | None = None) -> Agent:
  """Create the sample agent with the supplied system prompt."""
  _configure_environment()
  return Agent(
      name=APP_NAME,
      model=Gemini(
          model=model_id or MODEL_ID,
          retry_options=types.HttpRetryOptions(attempts=3),
      ),
      description=(
          "Basketball analytics assistant with deterministic fixture tools."
      ),
      instruction=prompt,
      tools=DEMO_TOOLS,
  )


_prompt, PROMPT_VERSION = read_prompt()
root_agent = create_agent(_prompt)

bq_logging_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    table_id=TABLE_ID,
    location=DATASET_LOCATION,
    config=BigQueryLoggerConfig(
        enabled=True,
        max_content_length=50 * 1024,
        # Small batches make rows visible quickly for this one-shot demo.
        batch_size=1,
        shutdown_timeout=15.0,
    ),
)

app = root_agent
