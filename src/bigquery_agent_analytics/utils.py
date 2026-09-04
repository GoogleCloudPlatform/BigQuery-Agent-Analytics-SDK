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

"""Shared utilities for the BigQuery Agent Analytics SDK."""

import json
import re
from typing import Any, Optional


def strip_markdown_fences(text: Optional[str]) -> Optional[str]:
  """Strip markdown code block fences (``\\`\\`\\`json ... \\`\\`\\```) if present.

  Models frequently wrap JSON output in fenced code blocks. This helper
  removes the opening ``\\`\\`\\`json`` (or plain ``\\`\\`\\```) and closing
  ``\\`\\`\\``` markers so the result can be passed to ``json.loads()``.

  The regex pattern matches the same fences handled server-side by
  ``REGEXP_REPLACE`` in ``ontology_graph.py`` and ``context_graph.py``.
  """
  if not text:
    return text
  text = text.strip()
  if not text.startswith("```"):
    return text
  text = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", text)
  text = re.sub(r"\n?\s*```[\s\S]*$", "", text)
  return text.strip()


def _parse_json_from_text(text: str) -> Optional[dict[str, Any]]:
  """Extracts and parses JSON from LLM response text."""
  if not text:
    return None

  # Strip markdown fences first
  stripped = strip_markdown_fences(text)
  try:
    return json.loads(stripped)
  except (json.JSONDecodeError, TypeError):
    pass

  # Try raw JSON extraction (brace matching)
  if "{" in stripped:
    try:
      start = stripped.index("{")
      brace = 0
      end = start
      for i, ch in enumerate(stripped[start:], start):
        if ch == "{":
          brace += 1
        elif ch == "}":
          brace -= 1
          if brace == 0:
            end = i + 1
            break
      return json.loads(stripped[start:end])
    except (ValueError, json.JSONDecodeError):
      pass

  return None


def _extract_json_from_text(text: str) -> Optional[str]:
  """Extracts raw JSON string out of text block, handles braces and spaces."""
  if not text:
    return None
  text = text.strip()
  if text.startswith("```"):
    json_str = strip_markdown_fences(text)
  else:
    json_str = None
  if not json_str:
    if "{" in text:
      try:
        start = text.index("{")
        brace_count = 0
        end = start
        for i, char in enumerate(text[start:], start):
          if char == "{":
            brace_count += 1
          elif char == "}":
            brace_count -= 1
            if brace_count == 0:
              end = i + 1
              break
        json_str = text[start:end]
      except (ValueError, IndexError):
        pass
  if json_str:
    json_str = json_str.strip()
    json_str = "".join(
        char for char in json_str if char >= " " or char in "\n\r\t"
    )
  return json_str
