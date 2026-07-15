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

"""Existing-release guard for the github-release job (#356 rounds 11-13).

A full rerun rebuilds the wheel/sdist with new timestamps, so the
rebuilt anchor can NEVER byte-match files a package index already
accepted — deleting the draft in that situation destroys the only
byte-identical counterpart of the published artifacts and turns a
transient `finalize` failure into yank/version-burn recovery.

The workflow NEVER deletes a release automatically (#356 round 13):
GitHub offers no conditional delete, so a GET/DELETE pair is a TOCTOU
race — the draft can be published between the two requests and the
DELETE would remove a customer-visible release (and, with immutable
releases, permanently retire the tag name). When a stale draft is the
only thing standing in the way (both package indexes return an
explicit 404), the guard FAILS with instructions for the operator to
verify and delete the draft manually, then re-run this job.

Index states are BYTE-VALIDATED (#356 round 14): the caller runs
check_index_publication.py against the job's dist/ artifacts, so each
index is `absent` (explicit 404), `exact` (the index holds exactly the
current wheel+sdist bytes), or `deviating` (anything else — burned).
That makes the guard consistent with the reconciler's recoveries: an
exact TestPyPI publication with the draft missing PROCEEDS to recreate
the draft from the preserved original-attempt artifact, while
production files always make the missing-release / preserved-draft
paths authoritative. A full rerun naturally shows `deviating` because
rebuilt bytes differ — that REJECTS the rebuilt attempt and directs
the operator back to the recoverable original run; a burn is asserted
only for a deviation from the ORIGINAL accepted anchor (which finalize
reports as a burn state), never merely from a rebuilt one. A published
release always fails: that version is burned.
"""

from __future__ import annotations

import argparse
import dataclasses
from typing import Callable

RELEASE_STATES = ("absent", "draft", "published")
INDEX_STATES = ("absent", "exact", "deviating")


@dataclasses.dataclass(frozen=True)
class GuardAction:
  proceed: bool
  exit_code: int
  message: str


def decide(release_state: str, pypi: str, testpypi: str) -> GuardAction:
  """Exhaustive, fail-closed decision over the release × index matrix.

  ``release_state``: absent | draft | published (from the release API).
  ``pypi`` / ``testpypi``: absent (explicit 404) | exact (the index
  holds exactly the current dist bytes, per check_index_publication) |
  deviating (any validated mismatch). Indeterminate lookups must be
  rejected by the CALLER — they never reach this function, and any
  unknown token fails closed here."""
  if (
      release_state not in RELEASE_STATES
      or pypi not in INDEX_STATES
      or testpypi not in INDEX_STATES
  ):
    return GuardAction(
        False,
        1,
        f"unknown guard input (release={release_state!r}, pypi={pypi!r},"
        f" testpypi={testpypi!r}) — failing closed",
    )
  if release_state == "published":
    return GuardAction(
        False,
        1,
        "release is already PUBLISHED — the version is burned; bump and"
        " cut a new tag",
    )
  deviating = [
      name
      for name, state in (("PyPI", pypi), ("TestPyPI", testpypi))
      if state == "deviating"
  ]
  if deviating:
    where = " and ".join(deviating)
    return GuardAction(
        False,
        1,
        f"{where} holds files for this version that DIFFER from THIS"
        " attempt's dist bytes. If this is a full rerun, the rebuilt"
        " attempt is REJECTED but the version is NOT burned: abandon"
        " this run and re-run the FAILED jobs from the ORIGINAL"
        " workflow attempt, whose preserved artifact matches what the"
        " index accepted. The version is burned ONLY if the index also"
        " deviates from the ORIGINAL accepted anchor (finalize reports"
        " that as a burn state) — then bump the version, rebuild, and"
        " cut a new tag. Any existing draft is preserved for forensics",
    )
  if pypi == "exact":
    if release_state == "draft":
      return GuardAction(
          False,
          1,
          "PyPI already accepted exactly these bytes — the existing"
          " draft is PRESERVED; rerun the FAILED jobs from the ORIGINAL"
          " workflow attempt instead of a full rerun",
      )
    return GuardAction(
        False,
        1,
        "PyPI holds validated files for this version but no GitHub"
        " release exists — a cross-surface partial; do NOT rebuild at"
        " this version: follow the finalize yank/version-burn recovery",
    )
  if testpypi == "exact":
    if release_state == "draft":
      return GuardAction(
          False,
          1,
          "TestPyPI already accepted exactly these bytes — the existing"
          " draft is PRESERVED; rerun the FAILED jobs from the ORIGINAL"
          " workflow attempt instead of a full rerun",
      )
    # Production is absent, TestPyPI is byte-exact, and the release is
    # missing: this is the reconciler's missing-all recovery — the
    # draft is recreated from the preserved artifact and the upload
    # stages are already satisfied (#356 round 14 cross-module
    # consistency).
    return GuardAction(
        True,
        0,
        "TestPyPI already holds exactly the current bytes and no release"
        " exists — recreating the draft; the TestPyPI upload stage will"
        " be satisfied without re-upload",
    )
  if release_state == "draft":
    return GuardAction(
        False,
        1,
        "a stale DRAFT from an earlier attempt exists and both indexes"
        " confirmed absent (explicit 404) — automated deletion is a"
        " check/delete race (GitHub has no conditional delete), so:"
        " MANUALLY verify the release is still an unpublished draft,"
        " delete it (gh release delete <tag> or by id), then re-run"
        " this job",
    )
  return GuardAction(
      True,
      0,
      "no existing release and both indexes confirmed absent — proceeding",
  )


def main(
    argv: list[str] | None = None, echo: Callable[[str], None] = print
) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--release-state", required=True)
  parser.add_argument("--pypi", required=True)
  parser.add_argument("--testpypi", required=True)
  args = parser.parse_args(argv)
  action = decide(args.release_state, args.pypi, args.testpypi)
  echo(f"proceed={int(action.proceed)}")
  echo(f"message={action.message}")
  return action.exit_code


if __name__ == "__main__":
  raise SystemExit(main())
