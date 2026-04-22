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

  Default expected file format::

      PROMPT_V1 = \"\"\"...\"\"\"

      CURRENT_PROMPT = PROMPT_V1
      CURRENT_VERSION = 1

  For multi-prompt agents (e.g. a supervisor with sub-agent prompts),
  set ``prompt_variable`` to the specific variable name::

      adapter = PythonFilePromptAdapter(
          "prompts.py",
          prompt_variable="CURRENT_SUPERVISOR_INSTRUCTION",
      )

  Each improvement appends a new ``PROMPT_V<n+1>`` block and updates
  the prompt variable and ``CURRENT_VERSION`` references.
  """

  def __init__(
      self,
      path: str,
      prompt_variable: str = "CURRENT_PROMPT",
      version_variable: str = "CURRENT_VERSION",
  ) -> None:
    self._path = path
    self._prompt_variable = prompt_variable
    self._version_variable = version_variable

  @property
  def path(self) -> str:
    return self._path

  @property
  def prompt_variable(self) -> str:
    return self._prompt_variable

  @property
  def version_variable(self) -> str:
    return self._version_variable

  def read_prompt(self) -> tuple[str, int]:
    with open(self._path) as f:
      content = f.read()

    version_match = re.search(rf"{self._version_variable}\s*=\s*(\d+)", content)
    current_version = int(version_match.group(1)) if version_match else 1

    # Match: CURRENT_PROMPT = PROMPT_V<n>
    prompt_ref_match = re.search(
        rf"{re.escape(self._prompt_variable)}\s*=\s*PROMPT_V(\d+)", content
    )
    if prompt_ref_match:
      v = prompt_ref_match.group(1)
      pattern = rf'PROMPT_V{v}\s*=\s*"""(.*?)"""'
      prompt_match = re.search(pattern, content, re.DOTALL)
      if prompt_match:
        return prompt_match.group(1).strip(), current_version

    # Fallback: match CURRENT_PROMPT = """...""" (inline string)
    inline_match = re.search(
        rf'{re.escape(self._prompt_variable)}\s*=\s*"""(.*?)"""',
        content,
        re.DOTALL,
    )
    if inline_match:
      return inline_match.group(1).strip(), current_version

    # Fallback: match CURRENT_PROMPT = "..." or CURRENT_PROMPT = (...)
    # For parenthesized string concatenation, execute the file
    # and read the variable value directly.
    try:
      ns = {}
      exec(compile(content, self._path, "exec"), ns)  # noqa: S102
      value = ns.get(self._prompt_variable, "")
      if isinstance(value, str) and value:
        return value.strip(), current_version
    except Exception:
      pass

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

    # Update the prompt variable reference
    # Handle both CURRENT_PROMPT = PROMPT_V<n> and CURRENT_PROMPT = <IDENTIFIER>
    prompt_var_escaped = re.escape(self._prompt_variable)
    content = re.sub(
        rf"{prompt_var_escaped}\s*=\s*\S+",
        f"{self._prompt_variable} = PROMPT_V{new_version}",
        content,
        count=1,
    )

    # Update version variable
    version_var_escaped = re.escape(self._version_variable)
    content = re.sub(
        rf"{version_var_escaped}\s*=\s*\d+",
        f"{self._version_variable} = {new_version}",
        content,
    )

    # Insert new block before the prompt variable assignment
    prompt_assignment = f"{self._prompt_variable} = PROMPT_V{new_version}"
    content = content.replace(
        prompt_assignment,
        new_block + "\n" + prompt_assignment,
    )

    try:
      compile(content, self._path, "exec")
    except SyntaxError as e:
      raise ValueError(f"Generated prompts.py has syntax error: {e}")

    with open(self._path, "w") as f:
      f.write(content)

    return new_version
