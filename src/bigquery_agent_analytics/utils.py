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
  """Strips markdown backticks and fences around a JSON block."""
  if not text:
    return text
  text = text.strip()
  if text.startswith("```"):
    closing_fence = text.find("```", 3)
    if closing_fence != -1:
      text = text[:closing_fence]
    first_newline = text.find("\n")
    if first_newline == -1:
      text = text[3:]
      if text.lower().startswith("json"):
        text = text[4:]
      elif text.lower().startswith("sql"):
        text = text[3:]
      return text.strip()
    else:
      text = text[first_newline:]
  return text.strip()


def _parse_json_from_text(text: Optional[str]) -> Optional[dict[str, Any]]:
  """Parses JSON out of a text block, strip backticks, extract via braces."""
  if not text:
    return None

  # Try raw JSON extraction (brace matching) first to bypass any trailing markdown prose
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
      json_str = "".join(
          char for char in json_str if char >= " " or char in "\n\r\t"
      )
      return json.loads(json_str)
    except (ValueError, IndexError, json.JSONDecodeError):
      pass

  stripped = strip_markdown_fences(text)
  try:
    return json.loads(stripped)
  except (json.JSONDecodeError, TypeError):
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
