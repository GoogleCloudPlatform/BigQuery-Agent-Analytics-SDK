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

States:
  complete         every surface matches the anchor exactly → publish
  unpublished      no PyPI files (and no release assets claim) → rerun
  partial          anything missing/extra/tampered/yanked → yank + burn
  missing-release  PyPI has validated files but the GitHub release is
                   gone → cross-surface partial, yank + burn
  invalid-anchor   the anchor itself is inconsistent → investigate CI
  invalid-response the PyPI success body is unparseable or violates the
                   schema → INDETERMINATE, retry the lookup first
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import sys
from typing import Callable


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
  urls = pypi.get("urls", [])
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
    digest = entry.get("digests", {})
    if (
        not isinstance(digest, dict)
        or not isinstance(digest.get("sha256"), str)
        or len(digest["sha256"]) != 64
    ):
      return None, f"{name!r} has no valid sha256 digest object"
    seen[name] = entry
  return seen, ""


def reconcile(
    *,
    version: str,
    anchor_dir: pathlib.Path,
    release_dir: pathlib.Path | None,
    pypi: dict,
) -> tuple[str, str]:
  """Returns (state, detail). ``release_dir=None`` = the GitHub release
  is MISSING; the anchor and the PyPI schema are still fully validated
  before any cross-surface classification."""
  wheel, sdist, plugin = _expected_files(version)
  expected = {wheel, sdist, plugin}

  # --- the anchor itself must be internally exact ---------------------------
  manifest_path = anchor_dir / "SHA256SUMS"
  if not manifest_path.is_file():
    return "invalid-anchor", "anchor has no SHA256SUMS manifest"
  manifest: dict[str, str] = {}
  for line in manifest_path.read_text().splitlines():
    if line.strip():
      digest, _, name = line.partition("  ")
      manifest[name.strip()] = digest.strip()
  if set(manifest) != expected:
    missing = expected - set(manifest)
    extra = set(manifest) - expected
    return (
        "invalid-anchor",
        f"anchor manifest set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})",
    )
  anchor_files = {p.name for p in anchor_dir.iterdir()} - {"SHA256SUMS"}
  if anchor_files != expected:
    return (
        "invalid-anchor",
        f"anchor files set mismatch: {sorted(anchor_files ^ expected)}",
    )
  for name, digest in manifest.items():
    if _sha256(anchor_dir / name) != digest:
      return "invalid-anchor", f"anchor bytes of {name} do not match manifest"

  # --- PyPI schema is validated BEFORE any classification --------------------
  urls, why = _validated_urls(pypi)
  if urls is None:
    return "invalid-response", f"invalid PyPI response schema: {why}"

  # --- GitHub release missing: cross-surface classification ------------------
  if release_dir is None:
    if not urls:
      return "unpublished", "no GitHub release and no PyPI files"
    return (
        "missing-release",
        "PyPI carries validated files but the GitHub release is missing",
    )

  # --- GitHub release assets: exact set + byte identity ---------------------
  release_files = {p.name for p in release_dir.iterdir()}
  expected_release = expected | {"SHA256SUMS"}
  if release_files != expected_release:
    missing = expected_release - release_files
    extra = release_files - expected_release
    return (
        "partial",
        f"release asset set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})",
    )
  for name in expected:
    if _sha256(release_dir / name) != manifest[name]:
      return "partial", f"release asset {name} differs from the build anchor"
  if (release_dir / "SHA256SUMS").read_text() != manifest_path.read_text():
    return "partial", "release SHA256SUMS differs from the build anchor"

  # --- PyPI: exact distribution set + digests + not yanked ------------------
  if not urls:
    return "unpublished", "PyPI has no files for this version"
  expected_pypi = {wheel, sdist}
  if set(urls) != expected_pypi:
    missing = expected_pypi - set(urls)
    extra = set(urls) - expected_pypi
    return (
        "partial",
        f"PyPI file set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})",
    )
  for name in expected_pypi:
    entry = urls[name]
    if entry.get("yanked"):
      return "partial", f"PyPI file {name} is yanked"
    if entry.get("digests", {}).get("sha256") != manifest[name]:
      return "partial", f"PyPI digest of {name} differs from the build anchor"

  return "complete", "byte identity proven against the build anchor"


@dataclasses.dataclass(frozen=True)
class DispatchAction:
  publish: bool
  exit_code: int
  message: str


def dispatch(state: str) -> DispatchAction:
  """Exhaustive, fail-closed state→workflow mapping (#356 round 5:
  invalid-anchor previously fell through a bash case with exit 0)."""
  if state == "complete":
    return DispatchAction(True, 0, "byte identity proven — publish")
  if state == "unpublished":
    return DispatchAction(
        False,
        1,
        "PyPI publication did not happen — keep the draft, fix and rerun"
        " publish-pypi, or burn the version",
    )
  if state == "partial":
    return DispatchAction(
        False,
        1,
        "PARTIAL publication — yank this version on PyPI, burn it"
        " (bump + re-tag), keep the GitHub release draft",
    )
  if state == "invalid-anchor":
    return DispatchAction(
        False,
        1,
        "the build anchor itself is inconsistent — investigate CI before"
        " trusting ANY artifact of this run; do not publish or yank yet",
    )
  if state == "invalid-response":
    return DispatchAction(
        False,
        1,
        "the PyPI lookup returned an unparseable success body — the"
        " publication state is INDETERMINATE; retry the lookup before"
        " taking any recovery action",
    )
  if state == "missing-release":
    return DispatchAction(
        False,
        1,
        "PyPI carries files for this version but the GitHub release is"
        " missing — a cross-surface partial: yank the PyPI files, burn"
        " the version (bump + re-tag)",
    )
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
      required=True,
      help="File holding the PyPI release JSON ('{}' when absent).",
  )
  args = parser.parse_args(argv)
  if (args.release_dir is None) == (not args.release_missing):
    parser.error("exactly one of --release-dir / --release-missing required")
  # Only the workflow's explicit-404 marker ('{}') means absence. A
  # success body that fails to parse (or is not an object) is an
  # INDETERMINATE lookup, never confirmed absence (#356 round 6).
  try:
    pypi = json.loads(args.pypi_json.read_text())
    if not isinstance(pypi, dict):
      raise ValueError(f"expected a JSON object, got {type(pypi).__name__}")
  except ValueError as exc:
    state, detail = "invalid-response", f"unparseable PyPI body: {exc}"
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
