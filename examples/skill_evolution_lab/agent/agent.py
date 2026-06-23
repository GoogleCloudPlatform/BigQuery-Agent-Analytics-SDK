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

"""Agent factory for the skill-evolution lab.

The agent is deliberately minimal so the demo is legible: a single Gemini
model whose *system instruction is the SKILL.md body* plus two Python tools
(automatic function calling). Swapping the skill file is the only thing that
changes between V0 and V1 -- the model, tools, and questions stay fixed, so any
quality delta is attributable to the skill.

Gemini 3.x models are served from the Vertex AI ``global`` endpoint; 2.5
models are regional. ``make_client`` routes automatically based on the model
name.
"""

from __future__ import annotations

import os
import re

from google import genai
from google.genai import types

from .tools import AGENT_TOOLS

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def skill_instruction(skill_text: str) -> str:
  """Return the SKILL.md body (YAML frontmatter stripped) for use as the
  system instruction."""
  return _FRONTMATTER_RE.sub("", skill_text, count=1).strip()


def model_location(model: str) -> str:
  """Vertex location for a model.

  Gemini 3.x and gemini-2.5-pro are served from (and have the most capacity on)
  the 'global' endpoint; other models fall back to the regional location.
  """
  if model.startswith("gemini-3") or model == "gemini-2.5-pro":
    return "global"
  return os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")


def make_client(model: str, project: str | None = None) -> genai.Client:
  """Build a Vertex AI google-genai client routed to the right endpoint."""
  project = (
      project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID")
  )
  return genai.Client(
      vertexai=True, project=project, location=model_location(model)
  )


def build_config(skill_text: str) -> types.GenerateContentConfig:
  """Build the generation config: skill as system instruction + tools.

  Temperature 0 keeps the demo deterministic. Automatic function calling is
  left enabled (the default) so the SDK executes the Python tools and loops.
  """
  return types.GenerateContentConfig(
      system_instruction=skill_instruction(skill_text),
      tools=list(AGENT_TOOLS),
      temperature=0.0,
  )
