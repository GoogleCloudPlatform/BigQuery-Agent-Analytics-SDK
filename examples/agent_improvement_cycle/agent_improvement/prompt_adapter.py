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
import logging
import re

logger = logging.getLogger(__name__)


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

    # Match: PROMPT_VARIABLE = PROMPT_V<n>
    prompt_ref_match = re.search(
        rf"{re.escape(self._prompt_variable)}\s*=\s*PROMPT_V(\d+)", content
    )
    if prompt_ref_match:
      v = prompt_ref_match.group(1)
      pattern = rf'PROMPT_V{v}\s*=\s*"""(.*?)"""'
      prompt_match = re.search(pattern, content, re.DOTALL)
      if prompt_match:
        return prompt_match.group(1).strip(), current_version

    # Fallback: match inline string assignment
    inline_match = re.search(
        rf'{re.escape(self._prompt_variable)}\s*=\s*"""(.*?)"""',
        content,
        re.DOTALL,
    )
    if inline_match:
      return inline_match.group(1).strip(), current_version

    # Fallback: execute the file and read the variable directly
    # (handles parenthesized string concatenation, etc.)
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


class VertexPromptAdapter(PromptAdapter):
  """Reads/writes prompts via the Vertex AI Prompt Registry API.

  Prompts are stored server-side as Dataset resources with versioning.
  Each ``write_prompt`` call creates a new DatasetVersion via
  ``client.prompts.update()``.

  Usage::

      adapter = VertexPromptAdapter(
          prompt_id="1234567890",
          project="my-project",
          location="us-central1",
      )
      text, version = adapter.read_prompt()
      adapter.write_prompt(new_text, version, "fixed tool usage")

  To create the initial prompt resource, use
  :meth:`create_prompt`.
  """

  def __init__(
      self,
      prompt_id: str,
      project: str | None = None,
      location: str = "us-central1",
      model: str = "gemini-2.5-flash",
  ) -> None:
    from vertexai import Client

    self._prompt_id = prompt_id
    self._model = model
    self._client = Client(project=project, location=location)

  @property
  def prompt_id(self) -> str:
    return self._prompt_id

  @classmethod
  def create_prompt(
      cls,
      text: str,
      display_name: str,
      model: str = "gemini-2.5-flash",
      project: str | None = None,
      location: str = "us-central1",
  ) -> VertexPromptAdapter:
    """Create a new prompt in the Vertex AI Prompt Registry.

    Returns a ``VertexPromptAdapter`` bound to the new prompt ID.
    """
    from google.genai.types import Content
    from google.genai.types import Part
    from vertexai import Client
    from vertexai._genai.types.common import Prompt
    from vertexai._genai.types.common import PromptData

    client = Client(project=project, location=location)
    prompt = client.prompts.create(
        prompt=Prompt(
            prompt_data=PromptData(
                system_instruction=Content(parts=[Part(text=text)]),
                contents=[
                    Content(role="user", parts=[Part(text="{{user_input}}")])
                ],
                model=model,
            )
        ),
        config={"prompt_display_name": display_name},
    )
    logger.info("Created Vertex AI prompt %s", prompt.prompt_id)
    return cls(
        prompt_id=prompt.prompt_id,
        project=project,
        location=location,
        model=model,
    )

  def read_prompt(self) -> tuple[str, int]:
    prompt = self._client.prompts.get(prompt_id=self._prompt_id)
    text = ""
    if (
        prompt.prompt_data
        and prompt.prompt_data.system_instruction
        and prompt.prompt_data.system_instruction.parts
    ):
      text = prompt.prompt_data.system_instruction.parts[0].text or ""

    # create() produces 0 DatasetVersions (= v1), each update() adds one
    versions = list(
        self._client.prompts.list_versions(prompt_id=self._prompt_id)
    )
    version = len(versions) + 1

    return text.strip(), version

  def write_prompt(self, text: str, version: int, summary: str) -> int:
    from google.genai.types import Content
    from google.genai.types import Part
    from vertexai._genai.types.common import Prompt
    from vertexai._genai.types.common import PromptData

    if len(text.strip()) < 50:
      raise ValueError("Improved prompt is too short, likely invalid")

    new_version = version + 1
    self._client.prompts.update(
        prompt_id=self._prompt_id,
        prompt=Prompt(
            prompt_data=PromptData(
                system_instruction=Content(parts=[Part(text=text)]),
                contents=[
                    Content(role="user", parts=[Part(text="{{user_input}}")])
                ],
                model=self._model,
            )
        ),
        config={"version_display_name": f"v{new_version}_{summary[:40]}"},
    )
    logger.info(
        "Wrote prompt v%d to Vertex AI (%s): %s",
        new_version,
        self._prompt_id,
        summary,
    )
    return new_version
