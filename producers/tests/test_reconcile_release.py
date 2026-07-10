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

"""State-matrix tests for the release reconciliation (#356 round 4).

The anchor is the IMMUTABLE build artifact, never the mutable draft's own
manifest: exact filename sets everywhere, byte identity against the
anchor, extras rejected.
"""

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import reconcile_release

VERSION = "0.2.0"
WHEEL = f"bigquery_agent_analytics_tracing-{VERSION}-py3-none-any.whl"
SDIST = f"bigquery_agent_analytics_tracing-{VERSION}.tar.gz"
PLUGIN = f"bigquery-agent-analytics-tracing-claude-code-{VERSION}.tar.gz"


def _write(directory, name, data):
  path = directory / name
  path.write_bytes(data)
  return hashlib.sha256(data).hexdigest()


def _anchor(tmp_path, omit=(), extra=()):
  """The immutable build artifact: 3 files + a manifest listing them."""
  anchor = tmp_path / "anchor"
  anchor.mkdir(exist_ok=True)
  lines = []
  for name in (WHEEL, SDIST, PLUGIN):
    if name in omit:
      continue
    digest = _write(anchor, name, f"bytes-of-{name}".encode())
    lines.append(f"{digest}  {name}")
  for name in extra:
    digest = _write(anchor, name, b"extra")
    lines.append(f"{digest}  {name}")
  (anchor / "SHA256SUMS").write_text("\n".join(lines) + "\n")
  return anchor


def _release(tmp_path, anchor, omit=(), extra=(), corrupt=()):
  release = tmp_path / "release"
  release.mkdir(exist_ok=True)
  for path in anchor.iterdir():
    if path.name in omit:
      continue
    data = path.read_bytes()
    if path.name in corrupt:
      data = b"tampered"
    (release / path.name).write_bytes(data)
  for name in extra:
    (release / name).write_bytes(b"stray")
  return release


def _pypi(anchor, omit=(), extra=(), yanked=(), corrupt=()):
  urls = []
  for name in (WHEEL, SDIST):
    if name in omit:
      continue
    data = (anchor / name).read_bytes()
    if name in corrupt:
      data = b"other-bytes"
    urls.append(
        {
            "filename": name,
            "yanked": name in yanked,
            "digests": {"sha256": hashlib.sha256(data).hexdigest()},
        }
    )
  for name in extra:
    urls.append(
        {
            "filename": name,
            "yanked": False,
            "digests": {"sha256": "0" * 64},
        }
    )
  return {"urls": urls}


def _run(tmp_path, **kw):
  anchor = kw.pop("anchor", None) or _anchor(tmp_path)
  release = kw.pop("release", None)
  if release is None:
    release = _release(tmp_path, anchor)
  pypi = kw.pop("pypi", None)
  if pypi is None:
    pypi = _pypi(anchor)
  return reconcile_release.reconcile(
      version=VERSION, anchor_dir=anchor, release_dir=release, pypi=pypi
  )


def test_complete_state_when_everything_matches(tmp_path):
  state, _ = _run(tmp_path)
  assert state == "complete"


def test_anchor_manifest_must_cover_exactly_the_expected_files(tmp_path):
  # The reviewer reproduced this: a present plugin OMITTED from the
  # manifest still passed `sha256sum -c`. Exact sets, not subsets.
  anchor = _anchor(tmp_path)
  manifest = anchor / "SHA256SUMS"
  manifest.write_text(
      "\n".join(l for l in manifest.read_text().splitlines() if PLUGIN not in l)
      + "\n"
  )
  state, detail = _run(tmp_path, anchor=anchor)
  assert state == "invalid-anchor"
  assert PLUGIN in detail


def test_release_missing_an_asset_is_partial(tmp_path):
  anchor = _anchor(tmp_path)
  release = _release(tmp_path, anchor, omit=(PLUGIN,))
  state, detail = _run(tmp_path, anchor=anchor, release=release)
  assert state == "partial" and PLUGIN in detail


def test_release_extra_asset_is_partial(tmp_path):
  anchor = _anchor(tmp_path)
  release = _release(tmp_path, anchor, extra=("stray.bin",))
  state, detail = _run(tmp_path, anchor=anchor, release=release)
  assert state == "partial" and "stray.bin" in detail


def test_release_tampered_bytes_are_partial(tmp_path):
  anchor = _anchor(tmp_path)
  release = _release(tmp_path, anchor, corrupt=(WHEEL,))
  state, _ = _run(tmp_path, anchor=anchor, release=release)
  assert state == "partial"


def test_pypi_absent_is_unpublished(tmp_path):
  state, _ = _run(tmp_path, pypi={"urls": []})
  assert state == "unpublished"


def test_pypi_missing_one_distribution_is_partial(tmp_path):
  anchor = _anchor(tmp_path)
  state, _ = _run(tmp_path, anchor=anchor, pypi=_pypi(anchor, omit=(SDIST,)))
  assert state == "partial"


def test_pypi_extra_distribution_is_partial(tmp_path):
  anchor = _anchor(tmp_path)
  state, detail = _run(
      tmp_path, anchor=anchor, pypi=_pypi(anchor, extra=("evil.whl",))
  )
  assert state == "partial" and "evil.whl" in detail


def test_pypi_yanked_file_is_partial(tmp_path):
  anchor = _anchor(tmp_path)
  state, _ = _run(tmp_path, anchor=anchor, pypi=_pypi(anchor, yanked=(WHEEL,)))
  assert state == "partial"


def test_pypi_digest_mismatch_is_partial(tmp_path):
  anchor = _anchor(tmp_path)
  state, _ = _run(tmp_path, anchor=anchor, pypi=_pypi(anchor, corrupt=(WHEEL,)))
  assert state == "partial"


class TestDispatch:
  """The state→workflow mapping must be exhaustive and fail-closed."""

  def test_complete_publishes(self):
    action = reconcile_release.dispatch("complete")
    assert action.publish and action.exit_code == 0

  def test_every_non_complete_state_fails(self):
    for state in ("unpublished", "partial", "invalid-anchor"):
      action = reconcile_release.dispatch(state)
      assert not action.publish
      assert action.exit_code != 0, state
      assert action.message

  def test_unknown_state_fails_closed(self):
    action = reconcile_release.dispatch("something-new")
    assert not action.publish and action.exit_code != 0

  def test_each_state_has_surface_specific_recovery(self):
    assert "rerun" in reconcile_release.dispatch("unpublished").message
    assert "yank" in reconcile_release.dispatch("partial").message
    assert "CI" in reconcile_release.dispatch("invalid-anchor").message


class TestMalformedPypiResponses:
  """An HTTP-200 body that fails to parse is INDETERMINATE, never absence."""

  def _main(self, tmp_path, pypi_bytes):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    pypi_path = tmp_path / "pypi.json"
    pypi_path.write_bytes(pypi_bytes)
    out = []
    rc = reconcile_release.main(
        [
            "--version",
            VERSION,
            "--anchor-dir",
            str(anchor),
            "--release-dir",
            str(release),
            "--pypi-json",
            str(pypi_path),
        ],
        echo=out.append,
    )
    state = next(l.split("=", 1)[1] for l in out if l.startswith("state="))
    return rc, state

  def test_truncated_json_is_invalid_response(self, tmp_path):
    rc, state = self._main(tmp_path, b'{"urls": [{"filename": "x')
    assert state == "invalid-response"
    assert rc != 0

  def test_non_object_json_is_invalid_response(self, tmp_path):
    rc, state = self._main(tmp_path, b'"surprise"')
    assert state == "invalid-response"
    assert rc != 0

  def test_explicit_absence_marker_is_unpublished(self, tmp_path):
    # The workflow writes '{}' ONLY for an explicit HTTP 404.
    rc, state = self._main(tmp_path, b"{}")
    assert state == "unpublished"


class TestDispatchNewStates:

  def test_invalid_response_fails_with_indeterminate_recovery(self):
    action = reconcile_release.dispatch("invalid-response")
    assert not action.publish and action.exit_code != 0
    assert "indeterminate" in action.message.lower()

  def test_missing_release_is_a_cross_surface_partial(self):
    action = reconcile_release.dispatch("missing-release")
    assert not action.publish and action.exit_code != 0
    assert "yank" in action.message
