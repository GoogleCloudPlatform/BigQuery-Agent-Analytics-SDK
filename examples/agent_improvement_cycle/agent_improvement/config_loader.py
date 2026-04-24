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

"""Load agent improvement config from a JSON file.

The config file lives at ``<agent_root>/config.json``.
All relative paths inside it are resolved against ``<agent_root>``
(the config file's parent directory).

The ``agent_module`` field names a Python module (e.g. ``agent.agent``)
that is imported from ``<agent_root>``.  That module must export:

- ``create_agent(prompt: str) -> Agent``
- ``AGENT_TOOLS: list[Callable]``
- ``root_agent: Agent``
- ``bq_logging_plugin``
"""

from __future__ import annotations

import importlib
import json
import os
import sys

from agent_improvement.config import ImprovementConfig
from agent_improvement.prompt_adapter import PythonFilePromptAdapter
from agent_improvement.prompt_adapter import VertexPromptAdapter


def _resolve_paths(cfg: dict, agent_root: str) -> dict:
  """Resolve relative paths in *cfg* against *agent_root*."""
  resolved = dict(cfg)
  for key in ("prompts_path", "eval_cases_path", "traffic_generator"):
    if key in resolved and not os.path.isabs(resolved[key]):
      resolved[key] = os.path.join(agent_root, resolved[key])
  return resolved


def _import_module(module_name: str, agent_root: str):
  """Import *module_name* after ensuring *agent_root* is on sys.path."""
  if agent_root not in sys.path:
    sys.path.insert(0, agent_root)
  return importlib.import_module(module_name)


def load_agent_module(config_path: str) -> tuple:
  """Load config.json and return ``(agent_module, resolved_config_dict)``.

  Used by traffic mode which needs ``root_agent`` and
  ``bq_logging_plugin`` directly from the agent module.
  """
  config_path = os.path.abspath(config_path)
  agent_root = os.path.dirname(config_path)

  with open(config_path) as f:
    cfg = json.load(f)

  cfg = _resolve_paths(cfg, agent_root)
  mod = _import_module(cfg["agent_module"], agent_root)
  return mod, cfg


def _build_prompt_adapter(cfg: dict):
  """Build the appropriate PromptAdapter from config."""
  prompt_storage = cfg.get("prompt_storage", "python_file")

  if prompt_storage == "vertex":
    # Prefer config value, fall back to environment variable
    prompt_id = cfg.get("vertex_prompt_id") or os.environ.get(
        "VERTEX_PROMPT_ID"
    )
    if not prompt_id:
      raise ValueError(
          "prompt_storage='vertex' requires 'vertex_prompt_id' in config"
          " or VERTEX_PROMPT_ID env var. Run setup_vertex.py first."
      )
    # Also mirror prompt changes to local prompts.py for git tracking
    local_backup = None
    if cfg.get("prompts_path"):
      local_backup = PythonFilePromptAdapter(
          cfg["prompts_path"],
          prompt_variable=cfg.get("prompt_variable", "CURRENT_PROMPT"),
          version_variable=cfg.get("version_variable", "CURRENT_VERSION"),
      )

    return VertexPromptAdapter(
        prompt_id=prompt_id,
        project=cfg.get("vertex_project"),
        location=cfg.get("vertex_location", "us-central1"),
        model=cfg.get("model_id", "gemini-2.5-flash"),
        local_backup=local_backup,
    )

  return PythonFilePromptAdapter(
      cfg["prompts_path"],
      prompt_variable=cfg.get("prompt_variable", "CURRENT_PROMPT"),
      version_variable=cfg.get("version_variable", "CURRENT_VERSION"),
  )


def load_config(config_path: str) -> ImprovementConfig:
  """Load config.json and build an ``ImprovementConfig``.

  Reads the JSON file, resolves paths, imports the agent module,
  and assembles the config object.

  Supports two prompt storage backends via ``prompt_storage``:

  - ``"python_file"`` (default): reads/writes a local ``prompts.py``
  - ``"vertex"``: reads/writes via the Vertex AI Prompt Registry
  """
  mod, cfg = load_agent_module(config_path)
  return ImprovementConfig(
      agent_factory=mod.create_agent,
      agent_name=cfg["app_name"],
      agent_tools=mod.AGENT_TOOLS,
      prompt_adapter=_build_prompt_adapter(cfg),
      eval_cases_path=cfg["eval_cases_path"],
      model_id=cfg.get("model_id", "gemini-2.5-flash"),
      max_attempts=cfg.get("max_attempts", 3),
      judge_prompt=cfg.get("judge_prompt"),
      teacher_model_id=cfg.get("teacher_model_id"),
      use_vertex_optimizer=cfg.get("use_vertex_optimizer", False),
      vertex_location=cfg.get("vertex_location", "us-central1"),
  )
