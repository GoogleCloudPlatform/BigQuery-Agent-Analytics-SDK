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
import re
import sys

import pytest

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


_ABSENT = object()


def _run(tmp_path, **kw):
  anchor = kw.pop("anchor", None) or _anchor(tmp_path)
  release = kw.pop("release", None)
  if release is None:
    release = _release(tmp_path, anchor)
  pypi = kw.pop("pypi", _ABSENT)
  if pypi is _ABSENT:
    pypi = _pypi(anchor)
  testpypi = kw.pop("testpypi", None)
  release_published = kw.pop("release_published", False)
  return reconcile_release.reconcile(
      version=VERSION,
      anchor_dir=anchor,
      release_dir=release,
      pypi=pypi,
      testpypi=testpypi,
      release_published=release_published,
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


def test_pypi_confirmed_absent_with_exact_draft_is_unpublished(tmp_path):
  # pypi=None is the ONLY absence representation (explicit HTTP 404);
  # an HTTP-200 '{}' must never be treated as absence.
  state, _ = _run(tmp_path, pypi=None)
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
    for state in (
        "unpublished",
        "empty-release",
        "partial",
        "invalid-anchor",
        "invalid-response",
        "missing-release",
        "missing-all",
        "draft-invalid",
        "testpypi-partial",
        "premature-publication",
    ):
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


class TestCompleteSuccessSchema:
  """An HTTP 200 must carry the COMPLETE expected schema: reproduced in
  review — correct filenames/digests with 'yanked' omitted reached
  'complete'."""

  def _reconcile(self, tmp_path, pypi):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    return reconcile_release.reconcile(
        version=VERSION, anchor_dir=anchor, release_dir=release, pypi=pypi
    )

  def test_http_200_empty_object_is_invalid_response(self, tmp_path):
    # '{}' is NOT the absence marker anymore; a 200 without 'urls' is
    # a schema violation.
    state, _ = self._reconcile(tmp_path, {})
    assert state == "invalid-response"

  def test_yanked_omitted_never_reaches_complete(self, tmp_path):
    anchor = _anchor(tmp_path)
    pypi = _pypi(anchor)
    for entry in pypi["urls"]:
      del entry["yanked"]
    state, _ = self._reconcile(tmp_path, pypi)
    assert state == "invalid-response"

  def test_yanked_null_or_string_is_invalid_response(self, tmp_path):
    for bad in (None, "false"):
      anchor = _anchor(tmp_path)
      pypi = _pypi(anchor)
      pypi["urls"][0]["yanked"] = bad
      state, _ = self._reconcile(tmp_path, pypi)
      assert state == "invalid-response", bad

  def test_bad_digests_are_invalid_response(self, tmp_path):
    for bad in ("E" * 64, "g" * 64, "a" * 63, "a" * 65):
      anchor = _anchor(tmp_path)
      pypi = _pypi(anchor)
      pypi["urls"][0]["digests"]["sha256"] = bad
      state, _ = self._reconcile(tmp_path, pypi)
      assert state == "invalid-response", bad


class TestAbsenceFirstClassification:
  """Confirmed PyPI absence is classified BEFORE asset recovery advice:
  yank/version-burn must only ever be emitted when validated PyPI files
  actually exist."""

  def test_no_release_and_no_pypi_is_missing_all(self, tmp_path):
    state, _ = reconcile_release.reconcile(
        version=VERSION,
        anchor_dir=_anchor(tmp_path),
        release_dir=None,
        pypi=None,
    )
    assert state == "missing-all"
    action = reconcile_release.dispatch(state)
    assert "yank" not in action.message
    assert (
        "draft" not in action.message.lower()
        or "no draft" in action.message.lower()
    )

  def test_broken_draft_with_no_pypi_is_draft_invalid_not_partial(
      self, tmp_path
  ):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor, omit=(PLUGIN,))
    state, _ = reconcile_release.reconcile(
        version=VERSION, anchor_dir=anchor, release_dir=release, pypi=None
    )
    assert state == "draft-invalid"
    action = reconcile_release.dispatch(state)
    assert "yank" not in action.message  # nothing was published to yank

  def test_partial_advice_requires_actual_pypi_files(self, tmp_path):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor, omit=(PLUGIN,))
    state, _ = reconcile_release.reconcile(
        version=VERSION,
        anchor_dir=anchor,
        release_dir=release,
        pypi=_pypi(anchor),
    )
    assert state == "partial"
    assert "yank" in reconcile_release.dispatch(state).message


class TestEmptyRelease:
  """{"urls": []} on HTTP 200 is a burned version, not absence."""

  def test_release_present_with_empty_urls_is_empty_release(self, tmp_path):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    state, _ = reconcile_release.reconcile(
        version=VERSION,
        anchor_dir=anchor,
        release_dir=release,
        pypi={"urls": []},
    )
    assert state == "empty-release"

  def test_empty_release_recovery_is_burn_only(self):
    action = reconcile_release.dispatch("empty-release")
    assert not action.publish and action.exit_code != 0
    message = action.message.lower()
    assert "yank" not in message
    assert "rerun" not in message  # same-version rerun would fail
    assert "burn" in message or "bump" in message


def _documented_states(doc):
  """Exact row tokens of the docstring state table (first token of each
  two-space-indented row, before 2+ spaces of column gap)."""
  return {
      m.group(1)
      for m in re.finditer(r"^  ([a-z][a-z-]+)\s{2,}", doc, re.MULTILINE)
  }


class TestStateContract:
  """Produced, dispatched, and documented vocabularies are ONE set,
  asserted with exact bidirectional equality — not substring matching."""

  def test_dispatch_handles_exactly_the_known_states(self):
    for state in reconcile_release.KNOWN_STATES:
      action = reconcile_release.dispatch(state)
      assert action.message, state
    unknown = reconcile_release.dispatch("not-a-state")
    assert not unknown.publish and unknown.exit_code != 0

  def test_constants_dispatch_and_docs_are_exactly_equal(self):
    constants = {
        getattr(reconcile_release, name)
        for name in (
            "COMPLETE",
            "UNPUBLISHED",
            "EMPTY_RELEASE",
            "PARTIAL",
            "MISSING_RELEASE",
            "MISSING_ALL",
            "DRAFT_INVALID",
            "TESTPYPI_PARTIAL",
            "PREMATURE_PUBLICATION",
            "INVALID_ANCHOR",
            "INVALID_RESPONSE",
        )
    }
    documented = _documented_states(reconcile_release.__doc__)
    assert constants == reconcile_release.KNOWN_STATES == documented

  def test_mutated_contracts_are_detected(self):
    # A stale extra documented row and a removed row must BOTH fail the
    # equality — proving the parser is not one-way substring matching.
    doc = reconcile_release.__doc__
    extra = doc + "\n  obsolete-state   never produced anymore\n"
    assert _documented_states(extra) != reconcile_release.KNOWN_STATES
    removed = doc.replace("  empty-release", "  # empty-release")
    assert _documented_states(removed) != reconcile_release.KNOWN_STATES


class TestSchemaValidation:
  """Object-shaped but invalid PyPI schemas are invalid-response, never a
  crash and never a destructive classification."""

  def _reconcile(self, tmp_path, pypi):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    return reconcile_release.reconcile(
        version=VERSION, anchor_dir=anchor, release_dir=release, pypi=pypi
    )

  def test_urls_null_is_invalid_response(self, tmp_path):
    state, _ = self._reconcile(tmp_path, {"urls": None})
    assert state == "invalid-response"

  def test_url_entry_without_filename_is_invalid_response(self, tmp_path):
    state, _ = self._reconcile(tmp_path, {"urls": [{}]})
    assert state == "invalid-response"

  def test_non_dict_url_entry_is_invalid_response(self, tmp_path):
    state, _ = self._reconcile(tmp_path, {"urls": ["x"]})
    assert state == "invalid-response"

  def test_duplicate_filenames_are_invalid_response(self, tmp_path):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    entry = _pypi(anchor)["urls"][0]
    state, detail = reconcile_release.reconcile(
        version=VERSION,
        anchor_dir=anchor,
        release_dir=release,
        pypi={"urls": [entry, dict(entry)]},
    )
    assert state == "invalid-response"

  def test_missing_digest_object_is_invalid_response(self, tmp_path):
    state, _ = self._reconcile(
        tmp_path, {"urls": [{"filename": WHEEL, "yanked": False}]}
    )
    assert state == "invalid-response"


class TestReleaseMissingMatrix:
  """Surface combination lives IN the reconciler: anchor and PyPI are
  fully validated before any cross-surface classification."""

  def _run(self, tmp_path, anchor=None, pypi=None):
    anchor = anchor or _anchor(tmp_path)
    return reconcile_release.reconcile(
        version=VERSION, anchor_dir=anchor, release_dir=None, pypi=pypi
    )

  def test_release_missing_and_pypi_absent_is_missing_all(self, tmp_path):
    state, _ = self._run(tmp_path, pypi=None)
    assert state == "missing-all"

  def test_release_missing_and_pypi_empty_urls_is_empty_release(self, tmp_path):
    # HTTP 200 with urls: [] = the release record EXISTS with deleted
    # files; deleted filenames cannot be reused, so the version is
    # burned — never the same-version rerun advice absence would give.
    state, _ = self._run(tmp_path, pypi={"urls": []})
    assert state == "empty-release"

  def test_release_missing_with_valid_pypi_files_is_missing_release(
      self, tmp_path
  ):
    anchor = _anchor(tmp_path)
    state, _ = self._run(tmp_path, anchor=anchor, pypi=_pypi(anchor))
    assert state == "missing-release"

  def test_release_missing_with_malformed_pypi_is_invalid_response(
      self, tmp_path
  ):
    state, _ = self._run(tmp_path, pypi={"urls": None})
    assert state == "invalid-response"

  def test_release_missing_with_invalid_anchor_is_invalid_anchor(
      self, tmp_path
  ):
    anchor = _anchor(tmp_path)
    (anchor / "SHA256SUMS").unlink()
    state, _ = self._run(tmp_path, anchor=anchor, pypi={"urls": []})
    assert state == "invalid-anchor"


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
            "--testpypi-missing",
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

  def test_pypi_missing_flag_is_the_absence_marker(self, tmp_path):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    out = []
    rc = reconcile_release.main(
        [
            "--version",
            VERSION,
            "--anchor-dir",
            str(anchor),
            "--release-dir",
            str(release),
            "--pypi-missing",
            "--testpypi-missing",
        ],
        echo=out.append,
    )
    state = next(l.split("=", 1)[1] for l in out if l.startswith("state="))
    assert state == "unpublished"
    assert rc != 0

  def test_http_200_empty_object_via_cli_is_invalid_response(self, tmp_path):
    rc, state = self._main(tmp_path, b"{}")
    assert state == "invalid-response"
    assert rc != 0


class TestDispatchNewStates:

  def test_invalid_response_fails_with_indeterminate_recovery(self):
    action = reconcile_release.dispatch("invalid-response")
    assert not action.publish and action.exit_code != 0
    assert "indeterminate" in action.message.lower()

  def test_missing_release_is_a_cross_surface_partial(self):
    action = reconcile_release.dispatch("missing-release")
    assert not action.publish and action.exit_code != 0
    assert "yank" in action.message


class TestTestPyPISurface:
  """TestPyPI's absent/complete/partial states are part of the recovery
  matrix (#356 P2): a partial TestPyPI upload burns the version there,
  so production-404 + exact draft must NOT say 'rerun publication'."""

  def test_prod_absent_testpypi_partial_is_burned_not_unpublished(
      self, tmp_path
  ):
    # THE regression from review: the uploader published the wheel and
    # failed on the sdist; a same-version rerun cannot succeed.
    anchor = _anchor(tmp_path)
    state, detail = _run(
        tmp_path,
        anchor=anchor,
        pypi=None,
        testpypi=_pypi(anchor, omit=(SDIST,)),
    )
    assert state == "testpypi-partial"
    assert SDIST in detail
    message = reconcile_release.dispatch(state).message.lower()
    assert "burn" in message or "bump" in message
    assert "yank" not in message
    assert "rerun" not in message

  def test_prod_absent_testpypi_complete_is_unpublished(self, tmp_path):
    anchor = _anchor(tmp_path)
    state, detail = _run(
        tmp_path, anchor=anchor, pypi=None, testpypi=_pypi(anchor)
    )
    assert state == "unpublished"
    assert "TestPyPI: complete" in detail

  def test_prod_absent_testpypi_absent_is_unpublished(self, tmp_path):
    state, detail = _run(tmp_path, pypi=None, testpypi=None)
    assert state == "unpublished"
    assert "TestPyPI: absent" in detail

  def test_testpypi_empty_urls_with_prod_absent_is_burned(self, tmp_path):
    state, _ = _run(tmp_path, pypi=None, testpypi={"urls": []})
    assert state == "testpypi-partial"

  def test_testpypi_yanked_with_prod_absent_is_burned(self, tmp_path):
    anchor = _anchor(tmp_path)
    state, _ = _run(
        tmp_path,
        anchor=anchor,
        pypi=None,
        testpypi=_pypi(anchor, yanked=(WHEEL,)),
    )
    assert state == "testpypi-partial"

  def test_broken_draft_with_testpypi_partial_is_still_burned(self, tmp_path):
    # The burn outranks draft advice: deleting the draft and restarting
    # would die at the TestPyPI upload anyway.
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor, omit=(PLUGIN,))
    state, detail = _run(
        tmp_path,
        anchor=anchor,
        release=release,
        pypi=None,
        testpypi=_pypi(anchor, omit=(SDIST,)),
    )
    assert state == "testpypi-partial"
    assert PLUGIN in detail  # the draft problem still surfaces in the detail

  def test_release_missing_prod_absent_testpypi_partial_is_burned(
      self, tmp_path
  ):
    anchor = _anchor(tmp_path)
    state, _ = reconcile_release.reconcile(
        version=VERSION,
        anchor_dir=anchor,
        release_dir=None,
        pypi=None,
        testpypi=_pypi(anchor, omit=(SDIST,)),
    )
    assert state == "testpypi-partial"

  def test_invalid_testpypi_schema_is_invalid_response(self, tmp_path):
    state, detail = _run(tmp_path, pypi=None, testpypi={"urls": None})
    assert state == "invalid-response"
    assert "TestPyPI" in detail

  def test_testpypi_digest_conflict_with_prod_complete_is_partial(
      self, tmp_path
  ):
    # The full-lifecycle gate installed from TestPyPI: a digest conflict
    # means it exercised different bytes than customers receive.
    anchor = _anchor(tmp_path)
    state, detail = _run(
        tmp_path, anchor=anchor, testpypi=_pypi(anchor, corrupt=(WHEEL,))
    )
    assert state == "partial"
    assert "TestPyPI" in detail

  def test_testpypi_pruned_files_with_prod_complete_stay_complete(
      self, tmp_path
  ):
    # TestPyPI prunes old files routinely — absence there must never
    # block (or yank!) a byte-verified production release.
    anchor = _anchor(tmp_path)
    for testpypi in (None, _pypi(anchor, omit=(SDIST,))):
      state, _ = _run(tmp_path, anchor=anchor, testpypi=testpypi)
      assert state == "complete"


class TestPrematurePublication:
  """A draft accidentally published during the approval pause is
  customer-visible: 'keep the draft' advice would be wrong (#356 P1)."""

  def test_published_release_with_prod_absent_is_premature(self, tmp_path):
    state, detail = _run(tmp_path, pypi=None, release_published=True)
    assert state == "premature-publication"
    assert "unpublished" in detail  # the underlying state survives in detail
    message = reconcile_release.dispatch(state).message.lower()
    assert "draft" in message  # re-draft advice
    assert "customer-visible" in message

  def test_published_release_with_partial_pypi_is_premature(self, tmp_path):
    anchor = _anchor(tmp_path)
    state, detail = _run(
        tmp_path,
        anchor=anchor,
        pypi=_pypi(anchor, omit=(SDIST,)),
        release_published=True,
    )
    assert state == "premature-publication"
    assert "partial" in detail

  def test_published_release_with_complete_state_stays_complete(self, tmp_path):
    # Publication re-runs `gh release edit`, which idempotently
    # reasserts title/prerelease/latest/body on the visible release.
    state, _ = _run(tmp_path, release_published=True)
    assert state == "complete"

  def test_indeterminate_states_are_not_wrapped(self, tmp_path):
    anchor = _anchor(tmp_path)
    (anchor / "SHA256SUMS").unlink()
    state, _ = _run(tmp_path, anchor=anchor, release_published=True)
    assert state == "invalid-anchor"
    state, _ = _run(tmp_path, pypi={"urls": None}, release_published=True)
    assert state == "invalid-response"

  def test_published_flag_needs_release_dir_in_cli(self, tmp_path):
    anchor = _anchor(tmp_path)
    with pytest.raises(SystemExit):
      reconcile_release.main(
          [
              "--version",
              VERSION,
              "--anchor-dir",
              str(anchor),
              "--release-missing",
              "--release-published",
              "--pypi-missing",
              "--testpypi-missing",
          ],
          echo=lambda _: None,
      )

  def test_premature_publication_via_cli(self, tmp_path):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    out = []
    rc = reconcile_release.main(
        [
            "--version",
            VERSION,
            "--anchor-dir",
            str(anchor),
            "--release-dir",
            str(release),
            "--release-published",
            "--pypi-missing",
            "--testpypi-missing",
        ],
        echo=out.append,
    )
    assert rc != 0
    state = next(l.split("=", 1)[1] for l in out if l.startswith("state="))
    assert state == "premature-publication"


class TestMalformedTestPyPIResponses:
  """The TestPyPI success body gets the same fail-closed treatment as
  the production body: unparseable is INDETERMINATE, never absence."""

  def test_truncated_testpypi_json_is_invalid_response(self, tmp_path):
    anchor = _anchor(tmp_path)
    release = _release(tmp_path, anchor)
    bad = tmp_path / "testpypi.json"
    bad.write_bytes(b'{"urls": [{"filename": "x')
    out = []
    rc = reconcile_release.main(
        [
            "--version",
            VERSION,
            "--anchor-dir",
            str(anchor),
            "--release-dir",
            str(release),
            "--pypi-missing",
            "--testpypi-json",
            str(bad),
        ],
        echo=out.append,
    )
    state = next(l.split("=", 1)[1] for l in out if l.startswith("state="))
    assert state == "invalid-response"
    assert rc != 0

  def test_testpypi_args_are_mutually_required(self, tmp_path):
    anchor = _anchor(tmp_path)
    with pytest.raises(SystemExit):
      reconcile_release.main(
          [
              "--version",
              VERSION,
              "--anchor-dir",
              str(anchor),
              "--release-missing",
              "--pypi-missing",
          ],
          echo=lambda _: None,
      )
