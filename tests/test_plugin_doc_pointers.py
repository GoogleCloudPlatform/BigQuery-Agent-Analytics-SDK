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

"""Tests that the plugin pointer docs say where the moved plugins now live.

The `bigquery-agent-analytics-mod` plugin and the `bqaa-tracing` marketplace
catalog live in GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins. When
they moved, three pointers in this repository kept describing the old layout:
a migration link whose fragment matched no heading in the destination
MIGRATION.md, a "self-hosted catalog" that read as this repository's own, and
a "sibling" mod that no longer sits beside the tracing plugin.

The tests read files in this checkout only and never fetch the destination
repository, so they cannot see a heading renamed there. Prose is compared
with runs of whitespace collapsed, so re-wrapping a paragraph neither hides
an old phrase nor breaks a match.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOD_README = ROOT / "plugins" / "claude_code_mod" / "README.md"
TRACING_README = ROOT / "plugins" / "claude_code" / "README.md"
SETUP_COMMAND = ROOT / "plugins" / "claude_code" / "commands" / "bqaa-setup.md"

PLUGINS_REPO = "GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins"
# The destination MIGRATION.md section about this repository is headed
# "The earlier location"; GitHub derives the fragment from that heading.
EARLIER_LOCATION_LINK = (
    f"https://github.com/{PLUGINS_REPO}/blob/main/plugins/claude_code_mod/"
    "MIGRATION.md#the-earlier-location"
)

# An inline Markdown link, [label](target).
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


def _flat(text: str) -> str:
  """Collapses each run of whitespace, line breaks included, to one space."""
  return " ".join(text.split())


def _section(markdown: str, heading: str) -> str:
  """Returns the lines under `heading` up to the next heading of its level."""
  lines = markdown.splitlines()
  assert heading in lines, f"missing heading {heading!r}"
  start = lines.index(heading) + 1
  marker = heading.split(" ", 1)[0] + " "
  end = next(
      (i for i in range(start, len(lines)) if lines[i].startswith(marker)),
      len(lines),
  )
  return "\n".join(lines[start:end])


def test_mod_readme_migration_link_targets_the_earlier_location_heading():
  text = MOD_README.read_text(encoding="utf-8")
  label_by_target = {
      target: label for label, target in MARKDOWN_LINK.findall(text)
  }

  assert "#from-the-sdk-repositorys-marketplace" not in text
  assert EARLIER_LOCATION_LINK in label_by_target
  assert "The earlier location" in label_by_target[EARLIER_LOCATION_LINK]


def test_tracing_readme_install_section_names_the_catalog_host():
  section = _section(
      TRACING_README.read_text(encoding="utf-8"), "## Installing the plugin"
  )
  paragraphs = [_flat(p) for p in re.split(r"\n\s*\n", section)]
  canonical = [p for p in paragraphs if "canonical install path" in p]

  assert "self-hosted catalog" not in _flat(section)
  assert canonical, "the install section no longer names the canonical path"
  for paragraph in canonical:
    assert PLUGINS_REPO in paragraph, paragraph


def test_setup_command_names_the_mod_and_the_repository_that_hosts_it():
  text = _flat(SETUP_COMMAND.read_text(encoding="utf-8"))

  assert "sibling `claude_code_mod`" not in text
  assert "`bigquery-agent-analytics-mod`" in text
  assert PLUGINS_REPO in text
