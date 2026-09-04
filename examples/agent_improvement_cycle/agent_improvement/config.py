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

"""Configuration for the agent improvement cycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
  from agent_improvement.prompt_adapter import PromptAdapter
  from google.adk.agents import Agent


@dataclass
class ImprovementConfig:
  """Everything needed to run an improvement cycle on any ADK agent.

  Args:
      agent_factory: A callable that takes a prompt string and returns a
          fully configured ADK Agent.  Called each time a candidate
          prompt needs to be evaluated.
      agent_name: Human-readable name for the agent (used in logs).
      agent_tools: The tool functions the target agent uses.  Used by
          :func:`tool_introspection.extract_tool_signatures` to inject
          tool documentation into the improver prompt.
      prompt_adapter: Reads and writes the agent's prompt storage
          (e.g. a ``prompts.py`` file).
      eval_cases_path: Path to the golden eval set JSON file.
      model_id: Gemini model used by the improver and judge LLMs.
      optimizer_max_iterations: Maximum number of prompt optimizer
          iterations per improvement step (Vertex AI Prompt Optimizer
          retry budget).
      quality_threshold: Fraction of golden cases that must pass
          (1.0 = all cases).
      teacher_model_id: Optional Gemini model for the teacher agent that
          generates ground truth. Defaults to *None*, which uses the
          same model as the target agent (``model_id``). Set to a
          stronger model (e.g. ``gemini-2.5-pro``) when failures
          require more reasoning capability than the target model can
          provide with just a better prompt.
      judge_prompt: Custom LLM judge prompt template. Must contain
          ``{question}`` and ``{response}`` placeholders. May also use
          ``{tool_check}`` (objective tool-call evidence) and
          ``{tool_fail_rule}`` (conditional rule when expected tool was
          not called). If *None*, uses the default judge prompt.
  """

  agent_factory: Callable[..., Agent]
  agent_name: str
  agent_tools: list[Callable]
  prompt_adapter: PromptAdapter
  eval_cases_path: str
  model_id: str = "gemini-2.5-flash"
  optimizer_max_iterations: int = 3
  quality_threshold: float = 0.95
  judge_prompt: str | None = None
  teacher_model_id: str | None = None
  use_vertex_optimizer: bool = False
  vertex_location: str = "us-central1"
  max_failure_extract: int | str | None = None
