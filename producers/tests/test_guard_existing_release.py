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

"""Full-matrix tests for the existing-release guard (#356 rounds 11-14).

Round 11: identical source rebuilt at a different timestamp produces
different wheel/sdist hashes, so a draft whose bytes an index already
accepted must be preserved. Round 13: the guard NEVER deletes
automatically — GitHub has no conditional delete, so a GET/DELETE pair
races publication; a stale draft fails the job with manual
verify-and-delete instructions instead. Round 14: index states are
byte-validated (absent | exact | deviating), keeping the guard's
recoveries CONSISTENT with the reconciler's."""

import hashlib
import itertools
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import guard_existing_release
import reconcile_release


def test_draft_present_and_pypi_exact_full_rerun_preserves_draft():
  # THE round-11 regression: PyPI accepted the original wheel/sdist,
  # only finalize failed, and the operator triggered a full rerun. The
  # draft is the only byte-identical counterpart of the published files
  # and must never be deleted.
  action = guard_existing_release.decide("draft", "exact", "absent")
  assert not action.proceed
  assert action.exit_code != 0
  assert "ORIGINAL workflow attempt" in action.message
  assert "PRESERVED" in action.message


def test_draft_with_exact_testpypi_files_is_preserved_too():
  action = guard_existing_release.decide("draft", "absent", "exact")
  assert not action.proceed
  assert action.exit_code != 0
  assert "TestPyPI" in action.message


def test_deviating_index_rejects_the_rebuilt_attempt_before_burning():
  # A full rerun rebuilds bytes, so its dist DIFFERS from what the
  # index accepted. That REJECTS the rebuilt attempt — the message
  # must FIRST direct an accidental full rerun back to the original
  # workflow run, and assert a burn only for deviation from the
  # ORIGINAL accepted anchor (#353 review).
  for release in ("absent", "draft"):
    action = guard_existing_release.decide(release, "deviating", "absent")
    assert not action.proceed
    assert action.exit_code != 0
    assert "DIFFER" in action.message
    assert "NOT burned" in action.message
    assert "ORIGINAL workflow attempt" in action.message
    assert "ORIGINAL accepted anchor" in action.message
    assert "bump" in action.message  # the true-burn branch stays stated
    # The original-run direction comes before the burn clause.
    assert action.message.index("ORIGINAL workflow attempt") < (
        action.message.index("bump")
    )
  action = guard_existing_release.decide("absent", "absent", "deviating")
  assert not action.proceed
  assert "TestPyPI" in action.message


def test_exact_testpypi_with_missing_release_recreates_the_draft():
  # THE round-14 cross-module regression: reconciler says missing-all →
  # restart github-release from the original attempt; the guard must
  # let that restart through when TestPyPI is byte-exact.
  action = guard_existing_release.decide("absent", "absent", "exact")
  assert action.proceed
  assert action.exit_code == 0
  assert "satisfied" in action.message


def test_exact_pypi_with_missing_release_is_cross_surface_partial():
  # Production files with no release stay authoritative for the
  # reconciler's missing-release (yank/burn) recovery.
  action = guard_existing_release.decide("absent", "exact", "absent")
  assert not action.proceed
  assert "cross-surface" in action.message


def test_stale_draft_requires_manual_deletion_never_automated():
  # THE round-13 regression: a GET-then-DELETE pair cannot be made
  # atomic, so even with both indexes 404 the guard must fail with
  # manual verify-and-delete instructions instead of proceeding.
  action = guard_existing_release.decide("draft", "absent", "absent")
  assert not action.proceed
  assert action.exit_code != 0
  assert "MANUALLY" in action.message
  assert "race" in action.message
  assert "re-run" in action.message


def test_published_release_always_fails():
  for pypi, testpypi in itertools.product(
      ("absent", "exact", "deviating"), repeat=2
  ):
    action = guard_existing_release.decide("published", pypi, testpypi)
    assert not action.proceed
    assert action.exit_code != 0
    assert "burn" in action.message


def test_clean_state_proceeds():
  action = guard_existing_release.decide("absent", "absent", "absent")
  assert action.proceed
  assert action.exit_code == 0


def test_matrix_invariants():
  # Across the whole matrix the ONLY proceeding states are: a fully
  # clean one (no release, both indexes 404), or the recreate-draft
  # case (no release, production 404, TestPyPI byte-exact). Every other
  # cell fails with a message; nothing is ever deleted.
  for release, pypi, testpypi in itertools.product(
      ("absent", "draft", "published"),
      ("absent", "exact", "deviating"),
      ("absent", "exact", "deviating"),
  ):
    action = guard_existing_release.decide(release, pypi, testpypi)
    expected_proceed = (
        release == "absent"
        and pypi == "absent"
        and testpypi in ("absent", "exact")
    )
    assert action.proceed == expected_proceed, (release, pypi, testpypi)
    assert (action.exit_code == 0) == action.proceed
    assert action.message
    assert not hasattr(action, "delete_draft")  # deletion is gone by design


def test_unknown_inputs_fail_closed():
  for bad in (
      ("indeterminate", "absent", "absent"),
      ("draft", "present", "absent"),  # the pre-round-14 vocabulary
      ("draft", "500", "absent"),
      ("draft", "absent", ""),
  ):
    action = guard_existing_release.decide(*bad)
    assert not action.proceed
    assert action.exit_code != 0
    assert "failing closed" in action.message


def test_main_emits_machine_readable_verdict():
  out = []
  rc = guard_existing_release.main(
      [
          "--release-state",
          "absent",
          "--pypi",
          "absent",
          "--testpypi",
          "absent",
      ],
      echo=out.append,
  )
  assert rc == 0
  assert "proceed=1" in out
  assert not any(line.startswith("delete_draft=") for line in out)

  out = []
  rc = guard_existing_release.main(
      ["--release-state", "draft", "--pypi", "absent", "--testpypi", "absent"],
      echo=out.append,
  )
  assert rc != 0
  assert "proceed=0" in out
  assert any("MANUALLY" in line for line in out)


class TestCrossModuleConsistency:
  """The reconciler's recovery advice and the guard's verdict must
  agree for the same world state (#356 round 14): the reviewer executed
  both paths for release-missing + prod-404 + exact TestPyPI and got
  contradictory messages."""

  VERSION = "0.2.0"
  WHEEL = f"bigquery_agent_analytics_tracing-{VERSION}-py3-none-any.whl"
  SDIST = f"bigquery_agent_analytics_tracing-{VERSION}.tar.gz"
  PLUGIN = f"bigquery-agent-analytics-tracing-claude-code-{VERSION}.tar.gz"

  def _anchor(self, tmp_path):
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    lines = []
    for name in (self.WHEEL, self.SDIST, self.PLUGIN):
      data = f"bytes-of-{name}".encode()
      (anchor / name).write_bytes(data)
      lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    (anchor / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return anchor

  def _exact_index(self, anchor):
    return {
        "urls": [
            {
                "filename": name,
                "yanked": False,
                "digests": {
                    "sha256": hashlib.sha256(
                        (anchor / name).read_bytes()
                    ).hexdigest()
                },
            }
            for name in (self.WHEEL, self.SDIST)
        ]
    }

  def test_missing_release_prod_absent_exact_testpypi_recovery_agrees(
      self, tmp_path
  ):
    anchor = self._anchor(tmp_path)
    state, _ = reconcile_release.reconcile(
        version=self.VERSION,
        anchor_dir=anchor,
        release_dir=None,
        pypi=None,
        testpypi=self._exact_index(anchor),
    )
    assert state == "missing-all"
    # The reconciler tells the operator to restart github-release from
    # the original attempt; the guard must let that restart proceed.
    assert "github-release" in reconcile_release.dispatch(state).message
    action = guard_existing_release.decide("absent", "absent", "exact")
    assert action.proceed

  def test_missing_release_with_exact_prod_recovery_agrees(self, tmp_path):
    anchor = self._anchor(tmp_path)
    state, _ = reconcile_release.reconcile(
        version=self.VERSION,
        anchor_dir=anchor,
        release_dir=None,
        pypi=self._exact_index(anchor),
        testpypi=None,
    )
    assert state == "missing-release"
    assert "yank" in reconcile_release.dispatch(state).message
    action = guard_existing_release.decide("absent", "exact", "absent")
    assert not action.proceed
    assert "yank" in action.message

  def test_deviating_testpypi_recovery_agrees(self, tmp_path):
    anchor = self._anchor(tmp_path)
    deviating = self._exact_index(anchor)
    deviating["urls"][0]["digests"]["sha256"] = "0" * 64
    state, _ = reconcile_release.reconcile(
        version=self.VERSION,
        anchor_dir=anchor,
        release_dir=None,
        pypi=None,
        testpypi=deviating,
    )
    assert state == "testpypi-partial"
    assert "burn" in reconcile_release.dispatch(state).message.lower()
    action = guard_existing_release.decide("absent", "absent", "deviating")
    assert not action.proceed
    assert "bump" in action.message
