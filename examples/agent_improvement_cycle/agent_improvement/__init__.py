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

"""Reusable agent improvement cycle for ADK agents.

Provides a LoopAgent-based prompt improver that evaluates an agent's
quality and iteratively rewrites its prompt until all golden eval
cases pass.
"""

from agent_improvement.config import ImprovementConfig
from agent_improvement.config_loader import load_agent_module
from agent_improvement.config_loader import load_config
from agent_improvement.eval_runner import EvalRunner
from agent_improvement.improver_agent import create_improver_agent
from agent_improvement.improver_agent import run_improvement
from agent_improvement.prompt_adapter import PromptAdapter
from agent_improvement.prompt_adapter import PythonFilePromptAdapter
from agent_improvement.prompt_adapter import VertexPromptAdapter
from agent_improvement.tool_introspection import extract_tool_signatures

__all__ = [
    "ImprovementConfig",
    "PromptAdapter",
    "PythonFilePromptAdapter",
    "VertexPromptAdapter",
    "EvalRunner",
    "extract_tool_signatures",
    "create_improver_agent",
    "load_agent_module",
    "load_config",
    "run_improvement",
]
