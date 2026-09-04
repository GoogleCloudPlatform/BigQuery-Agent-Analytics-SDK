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

"""Finalize's asset trust boundary, PR-tested (#356 round 14): release
metadata is attacker-influencable, so which bytes are streamed is
decided here — only the four expected hardcoded names within the size
cap are ever downloadable; everything else is a placeholder or a
fail-closed metadata rejection."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import plan_asset_downloads

VERSION = "0.2.0"
WHEEL = f"bigquery_agent_analytics_tracing-{VERSION}-py3-none-any.whl"
SDIST = f"bigquery_agent_analytics_tracing-{VERSION}.tar.gz"
PLUGIN = f"bigquery-agent-analytics-tracing-claude-code-{VERSION}.tar.gz"
EXPECTED = (WHEEL, SDIST, PLUGIN, "SHA256SUMS")


def _asset(name, asset_id=1, size=1000):
  return {"id": asset_id, "name": name, "size": size}


def _plan(assets, **kw):
  return plan_asset_downloads.plan(version=VERSION, assets=assets, **kw)


def test_expected_assets_are_planned_as_downloads_by_id():
  assets = [_asset(name, asset_id=i + 1) for i, name in enumerate(EXPECTED)]
  entries, why = _plan(assets)
  assert why == ""
  assert entries == [
      ("download", str(i + 1), name) for i, name in enumerate(EXPECTED)
  ]


def test_only_the_four_expected_names_can_ever_be_downloaded():
  # The reviewer's core assertion: downloads ⊆ the bounded expected set.
  assets = [_asset(name, asset_id=i + 1) for i, name in enumerate(EXPECTED)]
  assets += [
      _asset("extra.bin", asset_id=50),
      _asset("another.whl", asset_id=51),
  ]
  entries, _ = _plan(assets)
  downloads = [e[2] for e in entries if e[0] == "download"]
  assert set(downloads) == set(EXPECTED)
  placeholders = [e[1] for e in entries if e[0] == "placeholder"]
  assert placeholders == ["extra.bin", "another.whl"]


def test_unexpected_assets_become_placeholders_never_downloads():
  entries, _ = _plan([_asset("gigantic-junk.iso", size=10**12)])
  assert entries == [("placeholder", "gigantic-junk.iso")]


def test_oversized_expected_asset_becomes_a_placeholder():
  entries, _ = _plan(
      [_asset(WHEEL, size=plan_asset_downloads.DEFAULT_MAX_BYTES + 1)]
  )
  assert entries == [("placeholder", WHEEL)]
  entries, _ = _plan([_asset(WHEEL, size=200)], max_bytes=100)
  assert entries == [("placeholder", WHEEL)]


def test_empty_asset_set_is_a_valid_empty_plan():
  entries, why = _plan([])
  assert entries == [] and why == ""


def test_duplicate_names_fail_closed():
  entries, why = _plan([_asset(WHEEL, asset_id=1), _asset(WHEEL, asset_id=2)])
  assert entries is None
  assert "duplicate" in why


def test_unsafe_names_fail_closed():
  for bad in (
      "../escape",
      "a/b",
      "a\\b",
      "",
      ".",
      "..",
      "name with space",
      "a\nb",
  ):
    entries, why = _plan([_asset(bad)])
    assert entries is None, bad


def test_malformed_metadata_fails_closed():
  cases = [
      "not-a-list",
      ["not-a-dict"],
      [{"name": WHEEL, "size": 1}],  # no id
      [{"id": 0, "name": WHEEL, "size": 1}],  # non-positive id
      [{"id": True, "name": WHEEL, "size": 1}],  # boolean id
      [{"id": "7", "name": WHEEL, "size": 1}],  # string id
      [{"id": 1, "name": None, "size": 1}],
      [{"id": 1, "name": WHEEL}],  # no size
      [{"id": 1, "name": WHEEL, "size": -5}],
      [{"id": 1, "name": WHEEL, "size": "big"}],
  ]
  for assets in cases:
    entries, why = _plan(assets)
    assert entries is None, assets
    assert why


class TestCli:

  def _main(self, tmp_path, assets, argv_extra=()):
    path = tmp_path / "assets.json"
    path.write_text(assets if isinstance(assets, str) else json.dumps(assets))
    out = []
    rc = plan_asset_downloads.main(
        ["--version", VERSION, "--assets-json", str(path), *argv_extra],
        echo=out.append,
    )
    return rc, out

  def test_plan_lines_are_tab_separated(self, tmp_path):
    rc, out = self._main(
        tmp_path, [_asset(WHEEL, asset_id=7), _asset("junk.bin", asset_id=8)]
    )
    assert rc == 0
    assert out == [f"download\t7\t{WHEEL}", "placeholder\tjunk.bin"]

  def test_untrusted_metadata_exits_nonzero_with_detail(self, tmp_path):
    rc, out = self._main(tmp_path, [_asset("../evil")])
    assert rc != 0
    assert any(line.startswith("detail=") for line in out)

  def test_unparseable_metadata_exits_nonzero(self, tmp_path):
    rc, out = self._main(tmp_path, '{"not": "closed"')
    assert rc != 0
    assert any("unparseable" in line for line in out)

  def test_max_bytes_flag_is_honored(self, tmp_path):
    rc, out = self._main(
        tmp_path, [_asset(WHEEL, size=500)], argv_extra=("--max-bytes", "100")
    )
    assert rc == 0
    assert out == [f"placeholder\t{WHEEL}"]
