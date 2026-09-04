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

"""Load a SKILL.md (plus references/) into a single prompt string."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def load_skill(skill_dir: str) -> str:
  """Read SKILL.md and reference files into a single prompt string.

  Strips the YAML frontmatter (between ``---`` delimiters), keeps the
  markdown body, and appends any ``.md`` files from the ``references/``
  subdirectory as titled sections.

  Args:
      skill_dir: Path to the skill directory containing SKILL.md.

  Returns:
      Combined skill text suitable for use as an agent system prompt.

  Raises:
      FileNotFoundError: If SKILL.md doesn't exist.
  """
  skill_path = os.path.join(skill_dir, "SKILL.md")
  if not os.path.exists(skill_path):
    raise FileNotFoundError(f"Skill file not found: {skill_path}")

  with open(skill_path) as f:
    content = f.read()

  # Strip YAML frontmatter, keep markdown body.
  if content.startswith("---"):
    parts = content.split("---", 2)
    body = parts[2].strip() if len(parts) >= 3 else content
  else:
    body = content.strip()

  refs_dir = os.path.join(skill_dir, "references")
  if os.path.isdir(refs_dir):
    ref_sections = []
    for fname in sorted(os.listdir(refs_dir)):
      if fname.endswith(".md"):
        with open(os.path.join(refs_dir, fname)) as f:
          ref_content = f.read().strip()
        title = (
            fname.replace(".md", "").replace("_", " ").replace("-", " ").title()
        )
        ref_sections.append(f"## {title}\n\n{ref_content}")
    if ref_sections:
      body += "\n\n---\n\n# Reference Materials\n\n" + "\n\n".join(ref_sections)

  logger.info("Loaded skill from %s: %d chars total", skill_dir, len(body))
  return body
