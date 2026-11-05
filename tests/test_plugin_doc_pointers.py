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

"""Tests that the plugin pointer docs send readers to the right place.

The `bigquery-agent-analytics-mod` plugin and the `bqaa-tracing` marketplace
catalog live in GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins. After
they moved, pointers in this repository kept describing the old layout:

- the mod's pointer README sent readers of this repository's marketplace to
  a section of the Plugins repository's MIGRATION.md that covers a different
  move. The steps that replace this repository's marketplace are in the
  pointer README itself.
- the tracing README and MARKETPLACE.md did not name the `bqaa-tracing`
  catalog as the canonical install, and MARKETPLACE.md still called
  extracting the release tarball the official path.
- the setup command called the mod a "sibling" plugin, though it no longer
  sits beside the tracing plugin.

The tests read files in this checkout only and never fetch the Plugins
repository, so they cannot see a section renamed or rewritten there. Prose is
compared with runs of whitespace collapsed, so re-wrapping a paragraph
neither hides an old phrase nor breaks a match.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOD_README = ROOT / "plugins" / "claude_code_mod" / "README.md"
TRACING_README = ROOT / "plugins" / "claude_code" / "README.md"
MARKETPLACE_DOC = ROOT / "plugins" / "claude_code" / "MARKETPLACE.md"
SETUP_COMMAND = ROOT / "plugins" / "claude_code" / "commands" / "bqaa-setup.md"

SDK_REPO = "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
PLUGINS_REPO = "GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins"
# What replaces a `bqaa-tracing` marketplace added from this repository, in
# order. The catalog keeps its name, so the old entry has to go first.
SDK_MARKETPLACE_STEPS = (
    "plugin uninstall bigquery-agent-analytics-mod@bqaa-tracing",
    "plugin marketplace remove bqaa-tracing",
    f"plugin marketplace add {PLUGINS_REPO}",
    "plugin install bigquery-agent-analytics-mod@bqaa-tracing",
)

# An inline Markdown link, [label](target).
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


def _flat(text: str) -> str:
  """Collapses each run of whitespace, line breaks included, to one space."""
  return " ".join(text.split())


def _paragraphs(text: str) -> list[str]:
  """Returns the blank-line-separated blocks of `text`, each flattened."""
  return [_flat(block) for block in re.split(r"\n\s*\n", text)]


def _headings(markdown: str) -> list[str]:
  """Returns the heading lines that sit outside fenced code blocks."""
  headings = []
  fenced = False
  for line in markdown.splitlines():
    if line.lstrip().startswith("```"):
      fenced = not fenced
    elif not fenced and re.match(r"#{1,6} ", line):
      headings.append(line)
  return headings


def _anchor(heading: str) -> str:
  """Returns the fragment GitHub links a heading line by."""
  title = heading.lstrip("#").strip().lower()
  return re.sub(r"[^\w\- ]", "", title).replace(" ", "-")


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


def _in_order(text: str, steps: tuple[str, ...]) -> bool:
  """Says whether each step occurs in `text` after the one before it."""
  position = -1
  for step in steps:
    position = text.find(step, position + 1)
    if position == -1:
      return False
  return True


def _readme_anchors() -> set[str]:
  return {
      _anchor(heading)
      for heading in _headings(TRACING_README.read_text(encoding="utf-8"))
  }


def _readme_fragments(paragraph: str) -> list[str]:
  """Returns the fragments of the paragraph's links into the README."""
  return [
      target.split("#", 1)[1]
      for _, target in MARKDOWN_LINK.findall(paragraph)
      if target.startswith("README.md#")
  ]


def _marketplace_install_paragraphs() -> list[str]:
  markdown = MARKETPLACE_DOC.read_text(encoding="utf-8")
  return _paragraphs(_section(markdown, "## Install path (pre-marketplace)"))


def test_mod_readme_links_sdk_marketplace_readers_to_the_local_steps():
  text = MOD_README.read_text(encoding="utf-8")
  sections = {
      _anchor(heading): _section(text, heading) for heading in _headings(text)
  }
  linked = [
      sections.get(target[1:], "")
      for _, target in MARKDOWN_LINK.findall(text)
      if target.startswith("#")
  ]

  assert any(
      SDK_REPO in section and _in_order(section, SDK_MARKETPLACE_STEPS)
      for section in linked
  ), "no in-page link leads to the steps that replace the SDK marketplace"


def test_mod_readme_labels_the_plugins_migration_guide_for_other_moves():
  lines = [
      line
      for line in MOD_README.read_text(encoding="utf-8").splitlines()
      if "/MIGRATION.md" in line
  ]

  assert lines and all(
      "`bigquery-agent-analytics-plugin`" in line for line in lines
  ), lines


def test_tracing_readme_install_section_names_the_catalog_host():
  section = _section(
      TRACING_README.read_text(encoding="utf-8"), "## Installing the plugin"
  )
  canonical = [p for p in _paragraphs(section) if "canonical install path" in p]

  assert "self-hosted catalog" not in _flat(section)
  assert canonical, "the install section no longer names the canonical path"
  for paragraph in canonical:
    assert PLUGINS_REPO in paragraph, paragraph


def test_marketplace_doc_names_the_catalog_as_the_canonical_install():
  anchors = _readme_anchors()
  canonical = [p for p in _marketplace_install_paragraphs() if "canonical" in p]

  assert canonical and all(
      "`bqaa-tracing`" in p
      and PLUGINS_REPO in p
      and "installing-the-plugin" in _readme_fragments(p)
      and set(_readme_fragments(p)) <= anchors
      for p in canonical
  ), canonical


def test_marketplace_doc_labels_tarball_extraction_as_the_fallback():
  anchors = _readme_anchors()
  extraction = [
      p for p in _marketplace_install_paragraphs() if "extract" in p.lower()
  ]

  assert extraction and all(
      "fallback" in p
      and "official path" not in p
      and set(_readme_fragments(p)) <= anchors
      for p in extraction
  ), extraction


def test_marketplace_doc_keeps_the_official_listing_separate_and_pending():
  official = [p for p in _marketplace_install_paragraphs() if "official" in p]

  assert official and all(
      "pending" in p and "/issues/251" in p and PLUGINS_REPO not in p
      for p in official
  ), official


def test_setup_command_names_the_mod_and_the_repository_that_hosts_it():
  text = _flat(SETUP_COMMAND.read_text(encoding="utf-8"))

  assert "sibling `claude_code_mod`" not in text
  assert "`bigquery-agent-analytics-mod`" in text
  assert PLUGINS_REPO in text
