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

from .tools import get_current_date
from .tools import lookup_company_policy

# Agent tools — single list, reused everywhere
AGENT_TOOLS = [lookup_company_policy, get_current_date]

# Load environment
_env_path = os.path.join(os.path.dirname(__file__), "../.env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
project_id = os.getenv("PROJECT_ID") or _auth_project

DATASET_ID = os.getenv("DATASET_ID", "agent_logs")
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
MODEL_ID = os.getenv("DEMO_MODEL_ID", "gemini-2.5-flash")
LOCATION = os.getenv("DEMO_AGENT_LOCATION", "us-central1")

os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


def create_agent(prompt: str, model_id: str | None = None) -> Agent:
  """Create a company_info_agent with the given prompt.

  This is the single agent factory for the demo. Used by eval,
  improvement, and the main agent entry point.

  Args:
      prompt: System instruction for the agent.
      model_id: Optional model override. Defaults to MODEL_ID from env.
  """
  return Agent(
      name="company_info_agent",
      model=Gemini(
          model=model_id or MODEL_ID,
          retry_options=types.HttpRetryOptions(attempts=3),
      ),
      description="An agent that answers questions about company policies.",
      instruction=prompt,
      tools=AGENT_TOOLS,
  )


# Read prompt: Vertex AI Prompt Registry if configured, else local prompts.py
_vertex_prompt_id = os.getenv("VERTEX_PROMPT_ID")
_prompt_text = None

if _vertex_prompt_id:
  try:
    from vertexai import Client as _VertexClient
  except ImportError:
    raise ImportError(
        "VERTEX_PROMPT_ID is set but vertexai.Client is not available. "
        "Install google-cloud-aiplatform>=1.148.0: "
        "pip install 'google-cloud-aiplatform>=1.148.0'"
    )

  _vx_client = _VertexClient(project=project_id, location=LOCATION)
  try:
    _prompt_obj = _vx_client.prompts.get(prompt_id=_vertex_prompt_id)
  except Exception as _e:
    raise RuntimeError(
        f"Failed to load prompt from Vertex AI.\n"
        f"  Prompt ID: {_vertex_prompt_id}\n"
        f"  Project:   {project_id}\n"
        f"  Location:  {LOCATION}\n"
        f"  Error:     {_e}\n"
        f"  The prompt resource may not exist. Run ./setup.sh to create it,\n"
        f"  or ./reset.sh to recreate it from V1."
    ) from _e
  _prompt_text = ""
  if (
      _prompt_obj.prompt_data
      and _prompt_obj.prompt_data.system_instruction
      and _prompt_obj.prompt_data.system_instruction.parts
  ):
    _prompt_text = (
        _prompt_obj.prompt_data.system_instruction.parts[0].text or ""
    )
  print(f"  Loaded prompt from Vertex AI ({_vertex_prompt_id})")
else:
  from .prompts import CURRENT_PROMPT

  _prompt_text = CURRENT_PROMPT

root_agent = create_agent(_prompt_text)

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
