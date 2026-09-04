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

"""Skill Evolution Agent — evolves agent skills from execution trajectories.

The agent runs on a schedule (Cloud Scheduler) or on demand. It reads a
quality report built from the analytics plugin's BigQuery tables, detects
which agent is the bottleneck, runs the evolution pipeline, and opens an
issue/PR with the evolved skill.

The evolution pipeline:
1. Partitions conversations into successes (T+) and failures (T-)
2. Dispatches an analyst fleet in parallel to examine each trajectory
3. Consolidates all patches into an evolved SKILL.md
4. Optionally generates best-of-N candidates and scores them

Importing this module builds ``root_agent``/``app`` (ADK convention) but
touches neither GCP credentials nor the agent registry — project
detection is lazy and skill loading falls back to a built-in prompt.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.planners import BuiltInPlanner
from google.adk.plugins import LoggingPlugin
from google.genai import types

from .skill_loading import load_skill
from .tools import compare_versions
from .tools import count_failures
from .tools import create_evolution_issue
from .tools import create_evolution_pr
from .tools import detect_bottleneck_tool
from .tools import download_from_gcs
from .tools import list_agents
from .tools import parse_quality_issue
from .tools import read_skill
from .tools import restore_skills
from .tools import run_coevolution
from .tools import run_evolution
from .tools import run_quality_report
from .tools import score_candidate
from .tools import snapshot_skills
from .tools import upload_run_to_gcs

logger = logging.getLogger(__name__)

MODEL_ID = os.getenv("SKILL_EVOLUTION_MODEL_ID", "gemini-2.5-pro")
SHOW_THOUGHTS = os.getenv("SHOW_THOUGHTS", "true").lower() in (
    "true",
    "1",
    "yes",
)

_FALLBACK_INSTRUCTION = (
    "You are a Skill Evolution Agent. Use the provided tools to analyze"
    " agent quality and evolve agent skills."
)


def _project() -> str:
  """Project for Vertex calls: env first, ADC only as a fallback.

  Deliberately lazy — importing this module must not require
  credentials, so unit tests and ``--help`` work without ADC.
  """
  explicit = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
  if explicit:
    return explicit
  try:
    import google.auth

    _, project_id = google.auth.default()
    return project_id or ""
  except Exception as exc:  # noqa: BLE001 - ADC is optional at import time
    logger.warning(
        "No project configured and ADC lookup failed (%s); set PROJECT_ID.",
        exc,
    )
    return ""


def _configure_vertex_env() -> None:
  """Point the ADK model client at Vertex without stomping the host env."""
  os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
  project = _project()
  if project:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
  # Model endpoint, not the infra region: gemini-3.x is global-only.
  model_location = os.getenv("MODEL_LOCATION") or "global"
  existing = os.getenv("GOOGLE_CLOUD_LOCATION")
  if existing and os.getenv("MODEL_LOCATION") and existing != model_location:
    logger.warning(
        "GOOGLE_CLOUD_LOCATION=%s is already set; MODEL_LOCATION=%s will not"
        " override it for the orchestrator's own model calls.",
        existing,
        model_location,
    )
  os.environ.setdefault("GOOGLE_CLOUD_LOCATION", model_location)


def _instruction() -> str:
  """Load SKILL.md + references/ as the system prompt."""
  skill_dir = os.path.join(os.path.dirname(__file__), "skill")
  try:
    return load_skill(skill_dir)
  except Exception as exc:  # noqa: BLE001 - never block startup on the skill
    logger.warning("Could not load skill from %s: %s", skill_dir, exc)
    return _FALLBACK_INSTRUCTION


_configure_vertex_env()

root_agent = Agent(
    name="skill_evolution_agent",
    model=Gemini(
        model=MODEL_ID,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Analyzes agent quality reports and evolves agent skills through "
        "trajectory analysis, parallel analyst fleets, and patch "
        "consolidation."
    ),
    instruction=_instruction(),
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=SHOW_THOUGHTS),
    ),
    tools=[
        parse_quality_issue,
        run_quality_report,
        detect_bottleneck_tool,
        run_evolution,
        run_coevolution,
        upload_run_to_gcs,
        download_from_gcs,
        create_evolution_issue,
        create_evolution_pr,
        snapshot_skills,
        restore_skills,
        count_failures,
        score_candidate,
        read_skill,
        list_agents,
        compare_versions,
    ],
)

app = App(
    root_agent=root_agent,
    name="skill_evolution_agent",
    plugins=[LoggingPlugin()],
)
