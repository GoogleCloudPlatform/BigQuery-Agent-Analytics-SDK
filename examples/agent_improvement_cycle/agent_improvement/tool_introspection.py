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

"""Auto-extract tool signatures from ADK agent tool functions."""

from __future__ import annotations

import inspect
from typing import Callable


def extract_tool_signatures(tools: list[Callable]) -> list[dict]:
  """Extract name, signature, and docstring from tool functions.

  Args:
      tools: List of callable tool functions used by the target agent.

  Returns:
      A list of dicts with ``name``, ``signature``, and ``description``
      keys for each tool.
  """
  signatures = []
  for tool in tools:
    sig = inspect.signature(tool)
    # Filter out ToolContext params (injected by ADK, not user-facing)
    params = {k: v for k, v in sig.parameters.items() if k != "tool_context"}
    sig_str = (
        f"{tool.__name__}" f"({', '.join(str(v) for v in params.values())})"
    )
    doc = inspect.getdoc(tool) or ""
    signatures.append(
        {
            "name": tool.__name__,
            "signature": sig_str,
            "description": doc,
        }
    )
  return signatures


def format_tool_signatures(tools: list[Callable]) -> str:
  """Format tool signatures as a human-readable string for prompts.

  Args:
      tools: List of callable tool functions.

  Returns:
      A markdown-formatted string listing each tool.
  """
  sigs = extract_tool_signatures(tools)
  lines = []
  for s in sigs:
    lines.append(f"- {s['signature']}")
    if s["description"]:
      # Include the full docstring, indented
      for doc_line in s["description"].split("\n"):
        lines.append(f"  {doc_line}")
  return "\n".join(lines)
