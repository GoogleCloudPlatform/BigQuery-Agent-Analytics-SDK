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

"""Snapshot-bound publish of the reconciled release (#356 round 12).

Publication must be bound to the exact release object that was
reconciled, not to the mutable tag: anything holding ``contents:
write`` can replace draft assets between reconciliation and publish.
This helper therefore operates on the RELEASE ID and verifies the full
asset snapshot against the anchor immediately before AND after the
publish edit:

  1. fetch the release by ID; require the same id/tag, ``draft=true``
     (unless it is already published immutably — see below), and an
     asset set exactly equal to the anchor: same names, each asset's
     API ``digest`` equal to the sha256 of the anchor file;
  2. extract the image digest from the ANCHOR wheel, render the
     canonical body, and publish via ``PATCH /releases/{id}``
     reasserting tag, title, body, ``draft=false``,
     ``prerelease=false``, ``make_latest=false``;
  3. re-fetch by ID and re-verify the snapshot on the published object;
  4. assert the published release is IMMUTABLE — the repository setting
     (Settings → General → Releases → immutable releases) is the only
     thing that keeps published assets and SHA256SUMS non-replaceable,
     so a mutable publication fails this job loudly.

Idempotent rerun: a release that is already published, immutable, and
snapshot-identical returns success WITHOUT an edit (an immutable
release cannot be edited, and needs no re-anchor — its body was
verified when it was published).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import release_image_tool
import render_release_notes

RunGh = Callable[[list[str]], tuple[int, str]]


def _run_gh(argv: list[str]) -> tuple[int, str]:
  proc = subprocess.run(argv, check=False, capture_output=True, text=True)
  if proc.stderr:
    sys.stderr.write(proc.stderr)
  return proc.returncode, proc.stdout


def expected_asset_digests(
    version: str, anchor_dir: pathlib.Path
) -> dict[str, str]:
  """name -> sha256 hex of every asset the release must carry, computed
  from the anchor BYTES (the artifact of the producing CI run), never
  from any mutable surface."""
  names = (
      f"bigquery_agent_analytics_tracing-{version}-py3-none-any.whl",
      f"bigquery_agent_analytics_tracing-{version}.tar.gz",
      f"bigquery-agent-analytics-tracing-claude-code-{version}.tar.gz",
      "SHA256SUMS",
  )
  digests: dict[str, str] = {}
  for name in names:
    path = anchor_dir / name
    if not path.is_file():
      raise FileNotFoundError(f"anchor asset not found: {path}")
    digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
  return digests


def verify_snapshot(
    release: dict,
    *,
    release_id: int,
    tag: str,
    expected: dict[str, str],
) -> list[str]:
  """Problems that make this release object unsafe to publish (or to
  accept as published). Empty list = the snapshot matches the anchor."""
  problems: list[str] = []
  if release.get("id") != release_id:
    problems.append(
        f"release id changed: expected {release_id}, got {release.get('id')}"
    )
  if release.get("tag_name") != tag:
    problems.append(
        f"tag changed: expected {tag!r}, got {release.get('tag_name')!r}"
    )
  assets = release.get("assets")
  if not isinstance(assets, list):
    problems.append("release has no asset list")
    return problems
  seen: dict[str, dict] = {}
  for asset in assets:
    name = asset.get("name") if isinstance(asset, dict) else None
    if not isinstance(name, str) or not name:
      problems.append("asset entry has no string name")
      return problems
    if name in seen:
      problems.append(f"duplicate asset name {name!r}")
      return problems
    seen[name] = asset
  if set(seen) != set(expected):
    missing = set(expected) - set(seen)
    extra = set(seen) - set(expected)
    problems.append(
        f"asset set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})"
    )
    return problems
  for name in sorted(expected):
    asset = seen[name]
    if asset.get("state") != "uploaded":
      problems.append(f"asset {name} is in state {asset.get('state')!r}")
    if asset.get("digest") != f"sha256:{expected[name]}":
      problems.append(
          f"asset {name} digest {asset.get('digest')!r} does not match the"
          f" anchor sha256:{expected[name]}"
      )
  return problems


def _fetch(
    repo: str, release_id: int, run_gh: RunGh
) -> tuple[dict | None, str]:
  rc, out = run_gh(["gh", "api", f"repos/{repo}/releases/{release_id}"])
  if rc != 0:
    return None, f"release fetch failed (exit {rc})"
  try:
    release = json.loads(out)
    if not isinstance(release, dict):
      raise ValueError(f"expected an object, got {type(release).__name__}")
  except ValueError as exc:
    return None, f"unparseable release object: {exc}"
  return release, ""


def publish(
    *,
    version: str,
    public_image: str,
    anchor_dir: pathlib.Path,
    tag: str,
    repo: str,
    release_id: int,
    out_dir: pathlib.Path,
    run_gh: RunGh = _run_gh,
    echo: Callable[[str], None] = print,
) -> int:
  wheel = (
      anchor_dir
      / f"bigquery_agent_analytics_tracing-{version}-py3-none-any.whl"
  )
  if not wheel.is_file():
    raise FileNotFoundError(f"anchor wheel not found: {wheel}")
  expected = expected_asset_digests(version, anchor_dir)

  # 1. The pre-publish snapshot: the exact object reconciliation saw.
  release, why = _fetch(repo, release_id, run_gh)
  if release is None:
    echo(f"::error::{why}")
    return 1
  problems = verify_snapshot(
      release, release_id=release_id, tag=tag, expected=expected
  )
  if problems:
    echo(
        "::error::release snapshot changed between reconciliation and"
        f" publish — refusing to publish: {'; '.join(problems)}"
    )
    return 1
  if release.get("draft") is False:
    if release.get("immutable") is True:
      echo(
          "release is already published, immutable, and snapshot-identical"
          " — nothing to edit (idempotent rerun)"
      )
      return 0
    # Published but still mutable: fall through and re-assert the
    # canonical metadata; the immutability assert below will then fail
    # loudly until the repository setting is enabled.

  # 2. Re-anchor the body and publish the EXACT object by ID.
  reference = release_image_tool.extract_from_wheel(wheel)
  digest = reference.split("@", 1)[1]
  body_path = out_dir / "release_body.md"
  body_path.write_text(
      render_release_notes.render(
          version=version, digest=digest, public_image=public_image
      )
  )
  rc, _ = run_gh(
      [
          "gh",
          "api",
          "-X",
          "PATCH",
          f"repos/{repo}/releases/{release_id}",
          "-f",
          f"tag_name={tag}",
          "-f",
          f"name=Tracing {tag}",
          "-f",
          f"body=@{body_path}",
          "-F",
          "draft=false",
          "-F",
          "prerelease=false",
          "-f",
          "make_latest=false",
      ]
  )
  if rc != 0:
    return rc

  # 3 + 4. Post-publish: same snapshot, now published — and immutable.
  release, why = _fetch(repo, release_id, run_gh)
  if release is None:
    echo(f"::error::post-publish verification failed: {why}")
    return 1
  problems = verify_snapshot(
      release, release_id=release_id, tag=tag, expected=expected
  )
  if release.get("draft") is not False:
    problems.append("release is still a draft after the publish edit")
  if problems:
    echo(
        "::error::published release failed post-publish verification:"
        f" {'; '.join(problems)}"
    )
    return 1
  if release.get("immutable") is not True:
    echo(
        "::error::the published release is NOT immutable — its assets and"
        " SHA256SUMS remain replaceable by anything with contents:write."
        " Enable immutable releases in the repository settings (Settings →"
        " General → Releases) and re-run finalize; the published bytes"
        " themselves were verified against the anchor."
    )
    return 1
  echo("release published; snapshot re-verified; immutability asserted")
  return 0


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--version", required=True)
  parser.add_argument("--public-image", required=True)
  parser.add_argument("--anchor-dir", type=pathlib.Path, required=True)
  parser.add_argument("--tag", required=True)
  parser.add_argument("--repo", required=True)
  parser.add_argument("--release-id", type=int, required=True)
  parser.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path("."))
  args = parser.parse_args(argv)
  return publish(
      version=args.version,
      public_image=args.public_image,
      anchor_dir=args.anchor_dir,
      tag=args.tag,
      repo=args.repo,
      release_id=args.release_id,
      out_dir=args.out_dir,
  )


if __name__ == "__main__":
  raise SystemExit(main())
