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

"""Existing-release guard for the github-release job (#356 round 11).

A full rerun rebuilds the wheel/sdist with new timestamps, so the
rebuilt anchor can NEVER byte-match files a package index already
accepted — deleting the draft in that situation destroys the only
byte-identical counterpart of the published artifacts and turns a
transient `finalize` failure into yank/version-burn recovery.

The stale-draft delete is therefore permitted ONLY when BOTH package
indexes (PyPI and TestPyPI) return an explicit 404 for this version.
Once either index holds files, the existing draft is preserved and the
operator must rerun the FAILED jobs from the original workflow attempt
(which re-downloads the original build artifact) instead of a full
rerun. A published release always fails: that version is burned.
"""

from __future__ import annotations

import argparse
import dataclasses
from typing import Callable

RELEASE_STATES = ("absent", "draft", "published")
INDEX_STATES = ("absent", "present")


@dataclasses.dataclass(frozen=True)
class GuardAction:
  delete_draft: bool
  proceed: bool
  exit_code: int
  message: str


def decide(release_state: str, pypi: str, testpypi: str) -> GuardAction:
  """Exhaustive, fail-closed decision over the release × index matrix.

  ``release_state``: absent | draft | published (from the release API).
  ``pypi`` / ``testpypi``: absent (explicit 404) | present (HTTP 200).
  Indeterminate lookups must be rejected by the CALLER — they never
  reach this function, and any unknown token fails closed here."""
  if (
      release_state not in RELEASE_STATES
      or pypi not in INDEX_STATES
      or testpypi not in INDEX_STATES
  ):
    return GuardAction(
        False,
        False,
        1,
        f"unknown guard input (release={release_state!r}, pypi={pypi!r},"
        f" testpypi={testpypi!r}) — failing closed",
    )
  if release_state == "published":
    return GuardAction(
        False,
        False,
        1,
        "release is already PUBLISHED — the version is burned; bump and"
        " cut a new tag",
    )
  indexed = [
      name
      for name, state in (("PyPI", pypi), ("TestPyPI", testpypi))
      if state == "present"
  ]
  if indexed:
    where = " and ".join(indexed)
    if release_state == "draft":
      return GuardAction(
          False,
          False,
          1,
          f"{where} already accepted files for this version — the existing"
          " draft is PRESERVED (a rebuilt anchor can never byte-match"
          " published bytes); rerun the FAILED jobs from the ORIGINAL"
          " workflow attempt instead of a full rerun",
      )
    return GuardAction(
        False,
        False,
        1,
        f"{where} holds files for this version but no GitHub release"
        " exists — a cross-surface partial; do NOT rebuild at this"
        " version: follow the finalize yank/version-burn recovery",
    )
  if release_state == "draft":
    return GuardAction(
        True,
        True,
        0,
        "both indexes confirmed absent (explicit 404) — the stale draft"
        " from an earlier attempt may be deleted and recreated",
    )
  return GuardAction(
      False,
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
  echo(f"delete_draft={int(action.delete_draft)}")
  echo(f"proceed={int(action.proceed)}")
  echo(f"message={action.message}")
  return action.exit_code


if __name__ == "__main__":
  raise SystemExit(main())
