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
the GitHub release assets, the PyPI file set — must match it exactly:
exact filename sets (no subsets, no extras), byte identity, nothing
yanked.

States (the complete contract — KNOWN_STATES and dispatch() are
generated from one mapping, and tests assert the vocabularies match):
  complete         every surface matches the anchor exactly → publish
  unpublished      draft verified against the anchor; PyPI confirmed
                   absent (explicit 404) → fix and rerun publish-pypi
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
  invalid-anchor   the anchor itself is inconsistent → investigate CI
  invalid-response  the PyPI success body is unparseable or violates
                    the
                   schema → INDETERMINATE, retry the lookup first
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


def reconcile(
    *,
    version: str,
    anchor_dir: pathlib.Path,
    release_dir: pathlib.Path | None,
    pypi: dict | None,
) -> tuple[str, str]:
  """Returns (state, detail).

  ``release_dir=None`` = the GitHub release is MISSING. ``pypi=None`` =
  PyPI returned an explicit 404 (the ONLY absence representation — an
  HTTP-200 body must satisfy the complete schema). Confirmed PyPI
  absence is classified BEFORE asset recovery advice, so yank/burn is
  only ever emitted when validated PyPI files actually exist."""
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

  # --- PyPI schema is validated BEFORE any classification --------------------
  if pypi is None:
    urls: dict = {}
  else:
    urls, why = _validated_urls(pypi)
    if urls is None:
      return INVALID_RESPONSE, f"invalid PyPI response schema: {why}"
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

  # --- GitHub release missing: cross-surface classification ------------------
  if release_dir is None:
    if pypi_absent:
      return MISSING_ALL, "no GitHub release and nothing on PyPI"
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
    if asset_problem:
      return DRAFT_INVALID, f"nothing on PyPI and {asset_problem}"
    return UNPUBLISHED, "draft verified against the anchor; PyPI has no files"
  if asset_problem:
    return PARTIAL, asset_problem
  expected_pypi = {wheel, sdist}
  if set(urls) != expected_pypi:
    missing = expected_pypi - set(urls)
    extra = set(urls) - expected_pypi
    return (
        PARTIAL,
        f"PyPI file set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})",
    )
  for name in expected_pypi:
    entry = urls[name]
    if entry.get("yanked"):
      return PARTIAL, f"PyPI file {name} is yanked"
    if entry.get("digests", {}).get("sha256") != manifest[name]:
      return PARTIAL, f"PyPI digest of {name} differs from the build anchor"

  return COMPLETE, "byte identity proven against the build anchor"


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
        " publish-pypi, or burn the version",
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
        " restart the pipeline from github-release (PyPI needs no cleanup)",
    ),
    DRAFT_INVALID: DispatchAction(
        False,
        1,
        "the release draft differs from the build anchor and NOTHING is on"
        " PyPI (no cleanup there) — delete the draft and restart"
        " github-release",
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
  args = parser.parse_args(argv)
  if (args.release_dir is None) == (not args.release_missing):
    parser.error("exactly one of --release-dir / --release-missing required")
  if (args.pypi_json is None) == (not args.pypi_missing):
    parser.error("exactly one of --pypi-json / --pypi-missing required")
  # --pypi-missing (the explicit HTTP 404) is the sole absence marker.
  # A success body that fails to parse (or is not an object) is an
  # INDETERMINATE lookup, never confirmed absence.
  try:
    if args.pypi_missing:
      pypi = None
    else:
      pypi = json.loads(args.pypi_json.read_text())
      if not isinstance(pypi, dict):
        raise ValueError(f"expected a JSON object, got {type(pypi).__name__}")
  except ValueError as exc:
    state, detail = INVALID_RESPONSE, f"unparseable PyPI body: {exc}"
  else:
    state, detail = reconcile(
        version=args.version,
        anchor_dir=args.anchor_dir,
        release_dir=args.release_dir,
        pypi=pypi,
    )
  echo(f"state={state}")
  echo(f"detail={detail}")
  return dispatch(state).exit_code


if __name__ == "__main__":
  raise SystemExit(main())
