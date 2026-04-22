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

"""Prompt storage adapters for the improvement cycle.

Provides an abstract base class and a concrete implementation for
reading and writing agent prompts from Python files that follow the
``PROMPT_V<n>`` / ``CURRENT_VERSION`` convention.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
import re


class PromptAdapter(ABC):
  """Abstract interface for reading and writing agent prompts."""

  @abstractmethod
  def read_prompt(self) -> tuple[str, int]:
    """Return ``(prompt_text, version_number)``."""

  @abstractmethod
  def write_prompt(self, text: str, version: int, summary: str) -> int:
    """Persist a new prompt version.

    Args:
        text: The full prompt text.
        version: The current version number (the new version will be
            ``version + 1``).
        summary: A short description of what changed.

    Returns:
        The new version number.

    Raises:
        ValueError: If the generated file is syntactically invalid.
    """


class PythonFilePromptAdapter(PromptAdapter):
  """Reads/writes prompts from a ``prompts.py`` file.

  Expected file format::

      PROMPT_V1 = \"\"\"...\"\"\"

      CURRENT_PROMPT = PROMPT_V1
      CURRENT_VERSION = 1

  Each improvement appends a new ``PROMPT_V<n+1>`` block and updates
  the ``CURRENT_PROMPT`` and ``CURRENT_VERSION`` references.
  """

  def __init__(self, path: str) -> None:
    self._path = path

  @property
  def path(self) -> str:
    return self._path

  def read_prompt(self) -> tuple[str, int]:
    with open(self._path) as f:
      content = f.read()

    version_match = re.search(r"CURRENT_VERSION\s*=\s*(\d+)", content)
    current_version = int(version_match.group(1)) if version_match else 1

    prompt_ref_match = re.search(r"CURRENT_PROMPT\s*=\s*PROMPT_V(\d+)", content)
    if prompt_ref_match:
      v = prompt_ref_match.group(1)
      pattern = rf'PROMPT_V{v}\s*=\s*"""(.*?)"""'
      prompt_match = re.search(pattern, content, re.DOTALL)
      if prompt_match:
        return prompt_match.group(1).strip(), current_version

    return "", current_version

  def write_prompt(self, text: str, version: int, summary: str) -> int:
    new_version = version + 1

    with open(self._path) as f:
      content = f.read()

    if len(text.strip()) < 50:
      raise ValueError("Improved prompt is too short, likely invalid")

    safe_summary = summary.replace("\n", " ").strip()
    triple_q = '"' * 3
    safe_prompt = text.replace(triple_q, '\\"\\"\\"')

    new_block = (
        f"\n\n# --- Version {new_version}: Improvements from cycle"
        f" {version} ---\n"
        f"# Changes: {safe_summary}\n"
        f'PROMPT_V{new_version} = """{safe_prompt}\n"""\n'
    )

    content = re.sub(
        r"CURRENT_PROMPT\s*=\s*PROMPT_V\d+",
        f"CURRENT_PROMPT = PROMPT_V{new_version}",
        content,
    )
    content = re.sub(
        r"CURRENT_VERSION\s*=\s*\d+",
        f"CURRENT_VERSION = {new_version}",
        content,
    )

    current_prompt_line = f"CURRENT_PROMPT = PROMPT_V{new_version}"
    content = content.replace(
        current_prompt_line,
        new_block + "\n" + current_prompt_line,
    )

    try:
      compile(content, self._path, "exec")
    except SyntaxError as e:
      raise ValueError(f"Generated prompts.py has syntax error: {e}")

    with open(self._path, "w") as f:
      f.write(content)

    return new_version
