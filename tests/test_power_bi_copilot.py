# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the browser-only Copilot preview in dashboards/power_bi/copilot."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.test_power_bi_dashboard import _FORBIDDEN_VENDOR_NAMES

ROOT = Path(__file__).resolve().parents[1]
COPILOT = ROOT / "dashboards" / "power_bi" / "copilot"
SMOKE = ROOT / "tests" / "power_bi_copilot_smoke.js"
SOURCES = ("index.html", "styles.css", "app.js")


def _read(name):
  return (COPILOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SOURCES)
def test_sources_carry_the_apache_header(name):
  assert "Licensed under the Apache License, Version 2.0" in _read(name)


@pytest.mark.parametrize("name", SOURCES + ("README.md",))
def test_no_vendor_names(name):
  text = _read(name).lower()
  for vendor in _FORBIDDEN_VENDOR_NAMES():
    assert vendor.lower() not in text, f"{name} contains '{vendor}'"


@pytest.mark.parametrize("name", SOURCES)
def test_loads_nothing_from_the_network(name):
  """Only the license header may contain a URL; nothing is fetched remotely."""
  urls = re.findall(r"https?://[^\s\"'<>)]+", _read(name))
  assert set(urls) <= {"https://www.apache.org/licenses/LICENSE-2.0"}, urls


def test_page_uses_only_local_assets():
  html = _read("index.html")
  assert re.findall(r'<script[^>]*src="([^"]+)"', html) == ["./app.js"]
  assert re.findall(r'<link[^>]*href="([^"]+)"', html) == ["./styles.css"]
  assert "<iframe" not in html.lower()


def test_every_element_id_the_script_reads_exists():
  ids = set(re.findall(r'id="([^"]+)"', _read("index.html")))
  script = _read("app.js")
  wanted = set(re.findall(r"getElementById\('([^']+)'\)", script))
  wanted |= set(re.findall(r"\$\('([^']+)'\)", script))
  assert wanted, "no element lookups found"
  assert wanted <= ids, sorted(wanted - ids)


@pytest.mark.parametrize("name", SOURCES + ("README.md",))
def test_whitespace_hygiene(name):
  text = _read(name)
  assert text.endswith("\n") and not text.endswith("\n\n"), name
  bad = [
      n for n, line in enumerate(text.splitlines(), 1) if line != line.rstrip()
  ]
  assert not bad, f"{name}: trailing whitespace on lines {bad}"


def test_smoke_run_in_node():
  node = shutil.which("node")
  if not node:
    pytest.skip("node is not installed")
  result = subprocess.run(
      [node, str(SMOKE), str(COPILOT / "app.js")],
      capture_output=True,
      text=True,
      timeout=120,
      check=False,
  )
  assert result.returncode == 0, result.stdout + result.stderr
