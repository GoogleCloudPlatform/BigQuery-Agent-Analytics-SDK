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

"""Release reconciliation anchored to the immutable build artifact.

Issue #349 / #356 round 4: the mutable draft's own manifest is NOT an
identity anchor (a manifest omitting a present file still passes
``sha256sum -c``). The anchor is the CI build artifact; everything else —
the GitHub release assets, the PyPI file set, the TestPyPI file set —
must match it exactly: exact filename sets (no subsets, no extras), byte
identity, nothing yanked. Release VISIBILITY is part of the reconciled
state too (#356 round 11): a draft accidentally published during the
approval pause exposes incomplete artifacts, so "keep the draft" advice
must never be emitted for a release that is already customer-visible.

States (the complete contract — KNOWN_STATES and dispatch() are
generated from one mapping, and tests assert the vocabularies match):
  complete         every surface matches the anchor exactly → publish
  unpublished      draft verified against the anchor; PyPI confirmed
                   absent (explicit 404) → rerun the FAILED publish jobs
                   from the original workflow attempt
  empty-release    PyPI returned HTTP 200 with zero files: the release
                   record exists, files were deleted, and PyPI forbids
                   filename reuse → the version is burned; bump + re-tag
  partial          validated PyPI files exist but something is missing/
                   extra/tampered/yanked → yank + burn
  missing-release  PyPI has validated files but the GitHub release is
                   gone → cross-surface partial, yank + burn
  missing-all      no draft and PyPI confirmed absent → rerun the
                   pipeline from github-release (PyPI needs no cleanup)
  draft-invalid    draft differs from the anchor and PyPI is confirmed
                   absent → delete the draft and rerun (no cleanup)
  testpypi-partial  production PyPI is absent but TestPyPI holds a
                   deviating file set for this version — a failed upload
                   cannot be retried at the same version → burn (bump +
                   re-tag; production needs no cleanup)
  premature-publication  the GitHub release is publicly visible while
                   the underlying state is ANYTHING but complete
                   (indeterminate included: containment first) →
                   re-draft or burn, then the underlying recovery
  invalid-anchor   the anchor itself is inconsistent → investigate CI
  invalid-response  a PyPI/TestPyPI success body is unparseable or
                   violates the schema → INDETERMINATE, retry the
                   lookup first
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import re
import sys
from typing import Callable

# The state vocabulary: defined ONCE — reconcile() returns, dispatch()
# keys, and the documented table are all held to this set by tests.
COMPLETE = "complete"
UNPUBLISHED = "unpublished"
EMPTY_RELEASE = "empty-release"
PARTIAL = "partial"
MISSING_RELEASE = "missing-release"
MISSING_ALL = "missing-all"
DRAFT_INVALID = "draft-invalid"
TESTPYPI_PARTIAL = "testpypi-partial"
PREMATURE_PUBLICATION = "premature-publication"
INVALID_ANCHOR = "invalid-anchor"
INVALID_RESPONSE = "invalid-response"


def _expected_files(version: str) -> tuple[str, str, str]:
  wheel = f"bigquery_agent_analytics_tracing-{version}-py3-none-any.whl"
  sdist = f"bigquery_agent_analytics_tracing-{version}.tar.gz"
  plugin = f"bigquery-agent-analytics-tracing-claude-code-{version}.tar.gz"
  return wheel, sdist, plugin


def _sha256(path: pathlib.Path) -> str:
  return hashlib.sha256(path.read_bytes()).hexdigest()


def _validated_urls(pypi: dict) -> tuple[dict | None, str]:
  """({filename: entry}, "") on a valid schema, else (None, why).

  Object-shaped but invalid success bodies ({"urls": null},
  [{}], duplicates, missing digests) must become 'invalid-response' —
  never a crash and never a destructive classification (#356 round 7).
  """
  if "urls" not in pypi:
    # An HTTP 200 without 'urls' is a schema violation — absence is
    # represented ONLY by the explicit 404 (pypi=None), never by '{}'.
    return None, "success body has no 'urls' key"
  urls = pypi["urls"]
  if urls is None or not isinstance(urls, list):
    return None, f"'urls' is {type(urls).__name__}, expected a list"
  seen: dict[str, dict] = {}
  for entry in urls:
    if not isinstance(entry, dict):
      return None, f"url entry is {type(entry).__name__}, expected an object"
    name = entry.get("filename")
    if not isinstance(name, str) or not name:
      return None, "url entry has no string filename"
    if name in seen:
      return None, f"duplicate filename {name!r}"
    if not isinstance(entry.get("yanked"), bool):
      # Missing/null/string 'yanked' reached 'complete' in review — the
      # field must exist with Boolean type before this response is
      # trusted for ANY classification.
      return None, f"{name!r} has no boolean 'yanked' field"
    digest = entry.get("digests", {})
    if (
        not isinstance(digest, dict)
        or not isinstance(digest.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest["sha256"])
    ):
      return None, f"{name!r} has no valid sha256 digest object"
    seen[name] = entry
  return seen, ""


def _index_problem(
    urls: dict, expected: set[str], manifest: dict[str, str]
) -> str | None:
  """First deviation of a validated index file set from the anchor."""
  if set(urls) != expected:
    missing = expected - set(urls)
    extra = set(urls) - expected
    return (
        f"file set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})"
    )
  for name in sorted(expected):
    if urls[name]["yanked"]:
      return f"file {name} is yanked"
    if urls[name]["digests"]["sha256"] != manifest[name]:
      return f"digest of {name} differs from the build anchor"
  return None


def _classify(
    *,
    version: str,
    anchor_dir: pathlib.Path,
    release_dir: pathlib.Path | None,
    pypi: dict | None,
    testpypi: dict | None,
) -> tuple[str, str]:
  wheel, sdist, plugin = _expected_files(version)
  expected = {wheel, sdist, plugin}

  # --- the anchor itself must be internally exact ---------------------------
  manifest_path = anchor_dir / "SHA256SUMS"
  if not manifest_path.is_file():
    return INVALID_ANCHOR, "anchor has no SHA256SUMS manifest"
  manifest: dict[str, str] = {}
  for line in manifest_path.read_text().splitlines():
    if line.strip():
      digest, _, name = line.partition("  ")
      manifest[name.strip()] = digest.strip()
  if set(manifest) != expected:
    missing = expected - set(manifest)
    extra = set(manifest) - expected
    return (
        INVALID_ANCHOR,
        f"anchor manifest set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})",
    )
  anchor_files = {p.name for p in anchor_dir.iterdir()} - {"SHA256SUMS"}
  if anchor_files != expected:
    return (
        INVALID_ANCHOR,
        f"anchor files set mismatch: {sorted(anchor_files ^ expected)}",
    )
  for name, digest in manifest.items():
    if _sha256(anchor_dir / name) != digest:
      return INVALID_ANCHOR, f"anchor bytes of {name} do not match manifest"

  # --- index schemas are validated BEFORE any classification -----------------
  if pypi is None:
    urls: dict = {}
  else:
    urls, why = _validated_urls(pypi)
    if urls is None:
      return INVALID_RESPONSE, f"invalid PyPI response schema: {why}"
  if testpypi is None:
    tp_urls: dict = {}
  else:
    tp_urls, why = _validated_urls(testpypi)
    if tp_urls is None:
      return INVALID_RESPONSE, f"invalid TestPyPI response schema: {why}"
  # pypi=None (explicit 404) is the SOLE confirmed-absence predicate.
  # An HTTP-200 body with zero files means the release record EXISTS
  # with deleted files — and PyPI forbids reusing deleted filenames, so
  # the version is burned regardless of any other surface state.
  pypi_absent = pypi is None
  if not pypi_absent and not urls:
    return (
        EMPTY_RELEASE,
        "PyPI retains a release record with zero files for this version",
    )

  # --- TestPyPI surface status ------------------------------------------------
  # The uploader can publish one distribution and fail on the next,
  # burning the version there (#356 P2): with production absent, a
  # deviating TestPyPI set means a same-version retry can NEVER succeed,
  # so 'rerun publication' advice would send the operator into a wall.
  expected_pypi = {wheel, sdist}
  if testpypi is None:
    tp_problem = None
    tp_status = "absent"
  else:
    tp_problem = _index_problem(tp_urls, expected_pypi, manifest)
    tp_status = "complete" if tp_problem is None else "partial"

  # --- GitHub release missing: cross-surface classification ------------------
  if release_dir is None:
    if pypi_absent:
      if tp_problem is not None:
        return (
            TESTPYPI_PARTIAL,
            "production PyPI is absent, the GitHub release is missing,"
            f" and TestPyPI {tp_problem}",
        )
      return (
          MISSING_ALL,
          "no GitHub release and nothing on PyPI" f" (TestPyPI: {tp_status})",
      )
    return (
        MISSING_RELEASE,
        "PyPI carries validated files but the GitHub release is missing",
    )

  # --- GitHub release assets: exact set + byte identity ---------------------
  # 'partial' (yank/burn advice) is reserved for states where validated
  # PyPI files exist; a broken draft with NOTHING published is
  # 'draft-invalid' (nothing to yank).
  asset_problem = None
  release_files = {p.name for p in release_dir.iterdir()}
  expected_release = expected | {"SHA256SUMS"}
  if release_files != expected_release:
    missing = expected_release - release_files
    extra = release_files - expected_release
    asset_problem = (
        f"release asset set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})"
    )
  else:
    for name in expected:
      if _sha256(release_dir / name) != manifest[name]:
        asset_problem = f"release asset {name} differs from the build anchor"
        break
    else:
      if (release_dir / "SHA256SUMS").read_text() != manifest_path.read_text():
        asset_problem = "release SHA256SUMS differs from the build anchor"

  if pypi_absent:
    # TestPyPI burn wins over draft advice: whether the draft is exact
    # or broken, a same-version rerun dies at the TestPyPI upload.
    if tp_problem is not None:
      draft_note = (
          f"; additionally {asset_problem}"
          if asset_problem
          else "; the draft matches the anchor"
      )
      return (
          TESTPYPI_PARTIAL,
          f"production PyPI is absent and TestPyPI {tp_problem}{draft_note}",
      )
    if asset_problem:
      return DRAFT_INVALID, f"nothing on PyPI and {asset_problem}"
    return (
        UNPUBLISHED,
        "draft verified against the anchor; PyPI has no files"
        f" (TestPyPI: {tp_status})",
    )
  if asset_problem:
    return PARTIAL, asset_problem
  pypi_problem = _index_problem(urls, expected_pypi, manifest)
  if pypi_problem is not None:
    return PARTIAL, f"PyPI {pypi_problem}"
  # Production is complete. TestPyPI files may have been pruned (the
  # index deletes old files routinely), so MISSING files are tolerated —
  # but every file that IS present must be an expected filename, not
  # yanked, and digest-identical to the anchor: the full-lifecycle gate
  # installed from TestPyPI, and a deviating present file means the gate
  # may have exercised different bytes than customers receive (#356
  # round 12: an extra platform wheel and a yanked expected wheel both
  # reached 'complete' when only the name intersection was checked).
  for name in sorted(tp_urls):
    if name not in expected_pypi:
      return PARTIAL, f"TestPyPI carries unexpected file {name}"
    if tp_urls[name]["yanked"]:
      return PARTIAL, f"TestPyPI file {name} is yanked"
    if tp_urls[name]["digests"]["sha256"] != manifest[name]:
      return PARTIAL, f"TestPyPI digest of {name} differs from the build anchor"

  return COMPLETE, "byte identity proven against the build anchor"


def reconcile(
    *,
    version: str,
    anchor_dir: pathlib.Path,
    release_dir: pathlib.Path | None,
    pypi: dict | None,
    testpypi: dict | None = None,
    release_published: bool = False,
) -> tuple[str, str]:
  """Returns (state, detail).

  ``release_dir=None`` = the GitHub release is MISSING. ``pypi=None`` /
  ``testpypi=None`` = that index returned an explicit 404 (the ONLY
  absence representation — an HTTP-200 body must satisfy the complete
  schema). ``release_published=True`` = the release exists and is NOT a
  draft. Confirmed PyPI absence is classified BEFORE asset recovery
  advice, so yank/burn is only ever emitted when validated PyPI files
  actually exist."""
  state, detail = _classify(
      version=version,
      anchor_dir=anchor_dir,
      release_dir=release_dir,
      pypi=pypi,
      testpypi=testpypi,
  )
  # Visibility is ORTHOGONAL to the underlying classification (#356
  # round 12): EVERY non-complete state — including the indeterminate
  # invalid-anchor / invalid-response — flips to premature-publication
  # when the release is already publicly visible, because containment
  # (get it out of customers' sight) always comes first; the underlying
  # state and its recovery advice are preserved in the detail line. A
  # published release with a malformed index response must never get
  # retry-only advice while it stays public.
  if release_published and release_dir is not None and state != COMPLETE:
    return (
        PREMATURE_PUBLICATION,
        f"the release is publicly visible; underlying state {state}: {detail}",
    )
  return state, detail


@dataclasses.dataclass(frozen=True)
class DispatchAction:
  publish: bool
  exit_code: int
  message: str


_STATE_ACTIONS: dict[str, DispatchAction] = {
    COMPLETE: DispatchAction(True, 0, "byte identity proven — publish"),
    UNPUBLISHED: DispatchAction(
        False,
        1,
        "PyPI publication did not happen — keep the draft, fix and rerun"
        " the FAILED publish jobs from the ORIGINAL workflow attempt (a"
        " full rerun rebuilds the anchor and can never byte-match), or"
        " burn the version",
    ),
    EMPTY_RELEASE: DispatchAction(
        False,
        1,
        "PyPI retains a release record with ZERO files — deleted filenames"
        " cannot be reused, so this version is burned even though there is"
        " nothing to remove; bump the version, rebuild, and cut a new tag",
    ),
    PARTIAL: DispatchAction(
        False,
        1,
        "PARTIAL publication — yank this version on PyPI, burn it"
        " (bump + re-tag), keep the GitHub release draft",
    ),
    MISSING_RELEASE: DispatchAction(
        False,
        1,
        "PyPI carries files for this version but the GitHub release is"
        " missing — a cross-surface partial: yank the PyPI files, burn"
        " the version (bump + re-tag)",
    ),
    MISSING_ALL: DispatchAction(
        False,
        1,
        "nothing exists on either surface — no draft and nothing on PyPI;"
        " restart github-release from the ORIGINAL workflow attempt so the"
        " preserved anchor artifacts are reused (PyPI needs no cleanup)",
    ),
    DRAFT_INVALID: DispatchAction(
        False,
        1,
        "the release draft differs from the build anchor and NOTHING is on"
        " PyPI (no cleanup there) — delete the draft and restart"
        " github-release from the ORIGINAL workflow attempt",
    ),
    TESTPYPI_PARTIAL: DispatchAction(
        False,
        1,
        "TestPyPI already holds a deviating file set for this version —"
        " the failed upload cannot be retried at the same version, so it"
        " is burned even though production PyPI needs no cleanup: bump"
        " the version, rebuild, and cut a new tag",
    ),
    PREMATURE_PUBLICATION: DispatchAction(
        False,
        1,
        "the GitHub release is PUBLISHED while reconciliation is NOT"
        " complete — incomplete artifacts are customer-visible RIGHT NOW:"
        " convert it back to a draft (gh release edit --draft=true)"
        " immediately; if the repository enforces immutable releases it"
        " cannot be re-drafted — treat the version as burned (yank any"
        " published index files, bump, re-tag) and ship a corrected"
        " follow-up release; then follow the recovery for the underlying"
        " state in the detail line",
    ),
    INVALID_ANCHOR: DispatchAction(
        False,
        1,
        "the build anchor itself is inconsistent — investigate CI before"
        " trusting ANY artifact of this run; do not publish yet",
    ),
    INVALID_RESPONSE: DispatchAction(
        False,
        1,
        "the PyPI lookup returned an unparseable success body — the"
        " publication state is INDETERMINATE; retry the lookup before"
        " taking any recovery action",
    ),
}

KNOWN_STATES = frozenset(_STATE_ACTIONS)


def dispatch(state: str) -> DispatchAction:
  """Exhaustive, fail-closed state→workflow mapping. One mapping feeds
  KNOWN_STATES, dispatch(), and the documented contract; anything
  outside it fails closed."""
  action = _STATE_ACTIONS.get(state)
  if action is not None:
    return action
  return DispatchAction(
      False, 1, f"unknown reconciliation state {state!r} — failing closed"
  )


def main(
    argv: list[str] | None = None, echo: Callable[[str], None] = print
) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--version", required=True)
  parser.add_argument("--anchor-dir", type=pathlib.Path, required=True)
  parser.add_argument("--release-dir", type=pathlib.Path, default=None)
  parser.add_argument(
      "--release-missing",
      action="store_true",
      help="The GitHub release does not exist (cross-surface classification).",
  )
  parser.add_argument(
      "--release-published",
      action="store_true",
      help="The GitHub release exists and is NOT a draft (visibility is"
      " part of the reconciled state).",
  )
  parser.add_argument(
      "--pypi-json",
      type=pathlib.Path,
      default=None,
      help="File holding the PyPI release JSON (HTTP 200 body).",
  )
  parser.add_argument(
      "--pypi-missing",
      action="store_true",
      help="PyPI returned an explicit 404 — the only absence marker.",
  )
  parser.add_argument(
      "--testpypi-json",
      type=pathlib.Path,
      default=None,
      help="File holding the TestPyPI release JSON (HTTP 200 body).",
  )
  parser.add_argument(
      "--testpypi-missing",
      action="store_true",
      help="TestPyPI returned an explicit 404 — the only absence marker.",
  )
  args = parser.parse_args(argv)
  if (args.release_dir is None) == (not args.release_missing):
    parser.error("exactly one of --release-dir / --release-missing required")
  if args.release_published and args.release_dir is None:
    parser.error("--release-published requires --release-dir")
  if (args.pypi_json is None) == (not args.pypi_missing):
    parser.error("exactly one of --pypi-json / --pypi-missing required")
  if (args.testpypi_json is None) == (not args.testpypi_missing):
    parser.error("exactly one of --testpypi-json / --testpypi-missing required")

  # --pypi-missing/--testpypi-missing (the explicit HTTP 404) are the
  # sole absence markers. A success body that fails to parse (or is not
  # an object) is an INDETERMINATE lookup, never confirmed absence.
  def _load(path: pathlib.Path, surface: str) -> dict:
    body = json.loads(path.read_text())
    if not isinstance(body, dict):
      raise ValueError(
          f"{surface}: expected a JSON object, got {type(body).__name__}"
      )
    return body

  try:
    pypi = None if args.pypi_missing else _load(args.pypi_json, "PyPI")
    testpypi = (
        None if args.testpypi_missing else _load(args.testpypi_json, "TestPyPI")
    )
  except ValueError as exc:
    state, detail = INVALID_RESPONSE, f"unparseable index body: {exc}"
  else:
    state, detail = reconcile(
        version=args.version,
        anchor_dir=args.anchor_dir,
        release_dir=args.release_dir,
        pypi=pypi,
        testpypi=testpypi,
        release_published=args.release_published,
    )
  echo(f"state={state}")
  echo(f"detail={detail}")
  return dispatch(state).exit_code


if __name__ == "__main__":
  raise SystemExit(main())
