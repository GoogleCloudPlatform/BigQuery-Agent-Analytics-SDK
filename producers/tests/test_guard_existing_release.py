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

"""Full-matrix tests for the existing-release guard (#356 round 11).

The reviewer reproduced the failure mode: identical source rebuilt at a
different timestamp produces different wheel/sdist hashes, so deleting a
draft whose bytes an index already accepted turns a transient finalize
failure into yank/version-burn recovery. Deletion is allowed ONLY when
both indexes return an explicit 404."""

import itertools
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import guard_existing_release


def test_draft_present_and_pypi_complete_full_rerun_preserves_draft():
  # THE regression from review: PyPI accepted the original wheel/sdist,
  # only finalize failed, and the operator triggered a full rerun. The
  # draft is the only byte-identical counterpart of the published files
  # and must never be auto-deleted.
  action = guard_existing_release.decide("draft", "present", "absent")
  assert not action.delete_draft
  assert not action.proceed
  assert action.exit_code != 0
  assert "ORIGINAL workflow attempt" in action.message
  assert "PRESERVED" in action.message


def test_draft_with_testpypi_files_is_preserved_too():
  # The TestPyPI uploader can accept one distribution before failing:
  # those bytes are just as unreproducible as production ones.
  action = guard_existing_release.decide("draft", "absent", "present")
  assert not action.delete_draft
  assert action.exit_code != 0
  assert "TestPyPI" in action.message


def test_draft_deleted_only_when_both_indexes_404():
  action = guard_existing_release.decide("draft", "absent", "absent")
  assert action.delete_draft
  assert action.proceed
  assert action.exit_code == 0


def test_published_release_always_fails():
  for pypi, testpypi in itertools.product(("absent", "present"), repeat=2):
    action = guard_existing_release.decide("published", pypi, testpypi)
    assert not action.delete_draft
    assert not action.proceed
    assert action.exit_code != 0
    assert "burn" in action.message


def test_no_release_with_indexed_files_is_cross_surface_partial():
  action = guard_existing_release.decide("absent", "present", "absent")
  assert not action.proceed
  assert action.exit_code != 0
  assert "cross-surface" in action.message


def test_clean_state_proceeds_without_deletion():
  action = guard_existing_release.decide("absent", "absent", "absent")
  assert action.proceed
  assert not action.delete_draft
  assert action.exit_code == 0


def test_matrix_invariants():
  # Across the whole matrix: deletion happens ONLY on (draft, 404, 404),
  # and nothing proceeds while any index holds files or the release is
  # published.
  for release, pypi, testpypi in itertools.product(
      ("absent", "draft", "published"),
      ("absent", "present"),
      ("absent", "present"),
  ):
    action = guard_existing_release.decide(release, pypi, testpypi)
    both_absent = pypi == "absent" and testpypi == "absent"
    assert action.delete_draft == (release == "draft" and both_absent)
    assert action.proceed == (release != "published" and both_absent)
    assert (action.exit_code == 0) == action.proceed
    assert action.message


def test_unknown_inputs_fail_closed():
  for bad in (
      ("indeterminate", "absent", "absent"),
      ("draft", "500", "absent"),
      ("draft", "absent", ""),
  ):
    action = guard_existing_release.decide(*bad)
    assert not action.delete_draft
    assert not action.proceed
    assert action.exit_code != 0
    assert "failing closed" in action.message


def test_main_emits_machine_readable_verdict():
  out = []
  rc = guard_existing_release.main(
      ["--release-state", "draft", "--pypi", "absent", "--testpypi", "absent"],
      echo=out.append,
  )
  assert rc == 0
  assert "delete_draft=1" in out
  assert "proceed=1" in out

  out = []
  rc = guard_existing_release.main(
      ["--release-state", "draft", "--pypi", "present", "--testpypi", "absent"],
      echo=out.append,
  )
  assert rc != 0
  assert "delete_draft=0" in out
  assert "proceed=0" in out
