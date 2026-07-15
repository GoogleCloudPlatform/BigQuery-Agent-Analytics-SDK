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

"""Snapshot-bound publish of the reconciled release (#356 rounds 12-14).

Publication must be bound to the exact release object that was
reconciled, not to the mutable tag: anything holding ``contents:
write`` can replace draft assets between reconciliation and publish.
This helper therefore operates on the RELEASE ID:

  1. fetch the release by ID; require the same id/tag, a BOOLEAN
     ``draft`` flag, and an asset set exactly equal to the anchor:
     same names, each asset's API ``digest`` equal to the sha256 of
     the anchor file. The release state is classified FIRST (#356
     round 14): an already-public mutable release must get burn
     guidance immediately, never draft-recovery advice.
  2. for a DRAFT only: the repository's immutable-releases setting
     (``GET /repos/{repo}/immutable-releases``) must be ENABLED before
     anything becomes public — GitHub applies immutability only to
     releases published while the setting is on, so enabling it later
     can never retroactively protect an already-published release.
     That endpoint requires repository Administration:read, which the
     workflow ``GITHUB_TOKEN`` can never carry — the caller must
     supply a separate, narrowly scoped credential (a GitHub App
     installation token with ONLY Administration:read) via
     ``--admin-token-env``; every other API call keeps the standard
     job token.
  3. render the canonical body from the ANCHOR wheel and publish via
     ``PATCH /releases/{id}`` with a JSON ``--input`` payload (never
     ``-f field=@file`` — gh raw fields send the literal string)
     reasserting tag, title, body, ``draft=false``,
     ``prerelease=false``, ``make_latest="false"``;
  4. re-fetch by ID and re-verify the snapshot AND the canonical
     editable metadata (title, body, prerelease), assert the object is
     IMMUTABLE, and assert it did not become the repository's Latest —
     tolerating ONLY an explicit no-Latest 404; any other Latest
     lookup failure fails closed.

Idempotent rerun: a published, immutable release with a verified asset
snapshot is checked against the canonical metadata too — immutable
releases still allow title/notes edits, so drift there is REPAIRED
with a metadata-only PATCH (the fields GitHub permits editing);
anything unrepairable fails with exact remediation. A release that was
published while the setting was off is unrepairable by design: it
fails with burn guidance, never with "enable and re-run".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import release_image_tool
import render_release_notes

# (returncode, stdout, stderr) — stderr is part of the contract so the
# explicit no-Latest HTTP 404 can be told apart from auth/rate-limit/
# transport failures (#356 round 14: never fail open on those).
RunGh = Callable[[list[str]], tuple[int, str, str]]


def _run_gh(argv: list[str]) -> tuple[int, str, str]:
  proc = subprocess.run(argv, check=False, capture_output=True, text=True)
  if proc.stderr:
    sys.stderr.write(proc.stderr)
  return proc.returncode, proc.stdout, proc.stderr


def make_env_token_run_gh(token_env: str) -> RunGh:
  """A gh runner that authenticates with the token held in the given
  environment variable — used ONLY for the Administration:read policy
  lookup, keeping the broad-permission credential away from every
  other call."""

  def run(argv: list[str]) -> tuple[int, str, str]:
    token = os.environ.get(token_env, "")
    if not token:
      return (
          1,
          "",
          f"admin token environment variable {token_env!r} is empty or"
          " unset — the immutable-releases policy read needs a GitHub App"
          " installation token with Administration:read",
      )
    env = dict(os.environ, GH_TOKEN=token)
    proc = subprocess.run(
        argv, check=False, capture_output=True, text=True, env=env
    )
    if proc.stderr:
      sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr

  return run


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
  if not isinstance(release.get("draft"), bool):
    # A missing/None/string draft flag must never fall through to the
    # publish edit (#356 round 14) — visibility is part of the
    # fail-closed snapshot contract.
    problems.append(
        f"draft flag is {release.get('draft')!r}, expected a Boolean"
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


def _normalized(text: str) -> str:
  return text.replace("\r\n", "\n").rstrip("\n")


def canonical_problems(
    release: dict, *, expected_name: str, expected_body: str
) -> list[str]:
  """Drift in the metadata GitHub keeps EDITABLE even on immutable
  releases (title, notes) plus the prerelease flag — the snapshot check
  covers assets/tag; this covers everything customers read (#356 round
  13)."""
  problems: list[str] = []
  if release.get("name") != expected_name:
    problems.append(
        f"title drifted: expected {expected_name!r}, got"
        f" {release.get('name')!r}"
    )
  body = release.get("body")
  if not isinstance(body, str) or _normalized(body) != _normalized(
      expected_body
  ):
    problems.append("release body does not match the rendered canonical notes")
  if release.get("prerelease") is not False:
    problems.append(
        f"prerelease flag is {release.get('prerelease')!r}, expected False"
    )
  return problems


def _fetch_json(repo_path: str, run_gh: RunGh) -> tuple[dict | None, str]:
  rc, out, err = run_gh(["gh", "api", repo_path])
  if rc != 0:
    first = err.strip().splitlines()[0] if err.strip() else f"exit {rc}"
    return None, f"fetch of {repo_path} failed: {first}"
  try:
    obj = json.loads(out)
    if not isinstance(obj, dict):
      raise ValueError(f"expected an object, got {type(obj).__name__}")
  except ValueError as exc:
    return None, f"unparseable response from {repo_path}: {exc}"
  return obj, ""


def _latest_problem(repo: str, release_id: int, run_gh: RunGh) -> str | None:
  """The tracing release must never be the repository's Latest (that
  belongs to the SDK's vX.Y.Z releases). ONLY an explicit no-Latest
  HTTP 404 passes; auth/rate-limit/transport/malformed responses fail
  closed (#356 round 14) — `make_latest=false` constrains only our own
  PATCH, it proves nothing about the current Latest pointer."""
  rc, out, err = run_gh(["gh", "api", f"repos/{repo}/releases/latest"])
  if rc != 0:
    if "HTTP 404" in err:
      # The repository has no Latest at all — ours certainly is not it.
      return None
    first = err.strip().splitlines()[0] if err.strip() else f"exit {rc}"
    return (
        f"Latest lookup failed ({first}) — refusing to assume the tracing"
        " release did not become the repository's Latest"
    )
  try:
    latest = json.loads(out)
    if not isinstance(latest, dict):
      raise ValueError(f"expected an object, got {type(latest).__name__}")
  except ValueError as exc:
    return f"Latest lookup returned an unparseable body ({exc})"
  if latest.get("id") == release_id:
    return (
        "this release became the repository's Latest — it must not"
        " displace the SDK's vX.Y.Z Latest; point Latest back at the SDK"
        " release"
    )
  return None


def publish(
    *,
    version: str,
    public_image: str,
    anchor_dir: pathlib.Path,
    tag: str,
    repo: str,
    release_id: int,
    out_dir: pathlib.Path,
    admin_run_gh: RunGh,
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
  # Render the canonical metadata EARLY: it is both the publish payload
  # and the comparison baseline for every fetched snapshot.
  reference = release_image_tool.extract_from_wheel(wheel)
  digest = reference.split("@", 1)[1]
  expected_name = f"Tracing {tag}"
  expected_body = render_release_notes.render(
      version=version, digest=digest, public_image=public_image
  )
  body_path = out_dir / "release_body.md"
  body_path.write_text(expected_body)

  # 1. Classify the EXACT release object FIRST (#356 round 14): a
  # published mutable release needs immediate burn/containment
  # guidance — checking the repository setting first would mask it
  # behind false "still a draft" advice.
  release, why = _fetch_json(f"repos/{repo}/releases/{release_id}", run_gh)
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

  if release["draft"] is False:
    if release.get("immutable") is not True:
      echo(
          "::error::this release was PUBLISHED while the repository was"
          " NOT enforcing immutable releases — immutability cannot be"
          " applied retroactively, so its assets remain permanently"
          " replaceable. Treat the version as burned: delete the release,"
          " yank any index files, bump, re-tag, and re-release with the"
          " setting enabled."
      )
      return 1
    # Published + immutable + snapshot-verified. Title/notes stay
    # EDITABLE on immutable releases, so verify the canonical metadata
    # and repair drift with the only edit GitHub permits.
    drift = canonical_problems(
        release, expected_name=expected_name, expected_body=expected_body
    )
    prerelease_drift = [p for p in drift if p.startswith("prerelease")]
    if prerelease_drift:
      echo(
          "::error::published immutable release has unrepairable metadata"
          f" drift: {'; '.join(prerelease_drift)}"
      )
      return 1
    if drift:
      echo(f"repairing editable metadata drift: {'; '.join(drift)}")
      repair_path = out_dir / "repair_payload.json"
      repair_path.write_text(
          json.dumps({"name": expected_name, "body": expected_body})
      )
      rc, _, _ = run_gh(
          [
              "gh",
              "api",
              "-X",
              "PATCH",
              f"repos/{repo}/releases/{release_id}",
              "--input",
              str(repair_path),
          ]
      )
      if rc != 0:
        return rc
      release, why = _fetch_json(f"repos/{repo}/releases/{release_id}", run_gh)
      if release is None:
        echo(f"::error::post-repair verification failed: {why}")
        return 1
      problems = verify_snapshot(
          release, release_id=release_id, tag=tag, expected=expected
      ) + canonical_problems(
          release, expected_name=expected_name, expected_body=expected_body
      )
      if problems:
        echo(
            f"::error::metadata repair did not converge: {'; '.join(problems)}"
        )
        return 1
    latest_problem = _latest_problem(repo, release_id, run_gh)
    if latest_problem:
      echo(f"::error::{latest_problem}")
      return 1
    echo(
        "release already published, immutable, snapshot-identical, and"
        " canonical — idempotent rerun"
    )
    return 0

  # 2. The release is a DRAFT: immutability is a PREREQUISITE for
  # publication, read with the narrowly scoped Administration:read
  # credential — the workflow GITHUB_TOKEN can never carry that
  # permission (#356 round 14), and it must not be broadened: only
  # this single policy read uses the admin token.
  setting, why = _fetch_json(f"repos/{repo}/immutable-releases", admin_run_gh)
  if setting is None:
    echo(f"::error::immutable-releases policy check failed: {why}")
    return 1
  if setting.get("enabled") is not True:
    echo(
        "::error::the repository's immutable-releases setting is DISABLED"
        " — publishing now would create a permanently mutable release"
        " (enabling the setting later is NOT retroactive). Enable it"
        " (Settings → General → Releases → immutable releases) and re-run"
        " finalize; the release is still an unpublished draft."
    )
    return 1

  # 3. Publish the EXACT object by ID with a JSON payload. Never
  # `-f field=@file`: gh raw fields send the literal string, only a
  # JSON --input body carries the rendered notes.
  payload_path = out_dir / "publish_payload.json"
  payload_path.write_text(
      json.dumps(
          {
              "tag_name": tag,
              "name": expected_name,
              "body": expected_body,
              "draft": False,
              "prerelease": False,
              "make_latest": "false",
          }
      )
  )
  rc, _, _ = run_gh(
      [
          "gh",
          "api",
          "-X",
          "PATCH",
          f"repos/{repo}/releases/{release_id}",
          "--input",
          str(payload_path),
      ]
  )
  if rc != 0:
    return rc

  # 4. Post-publish: same snapshot AND canonical metadata, now
  # published — and immutable (defense in depth behind the step-2
  # prerequisite).
  release, why = _fetch_json(f"repos/{repo}/releases/{release_id}", run_gh)
  if release is None:
    echo(f"::error::post-publish verification failed: {why}")
    return 1
  problems = verify_snapshot(
      release, release_id=release_id, tag=tag, expected=expected
  ) + canonical_problems(
      release, expected_name=expected_name, expected_body=expected_body
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
        "::error::the release published as MUTABLE despite the enabled"
        " setting — treat as burned (immutability is not retroactive):"
        " delete the release, yank any index files, bump and re-tag."
    )
    return 1
  latest_problem = _latest_problem(repo, release_id, run_gh)
  if latest_problem:
    echo(f"::error::{latest_problem}")
    return 1
  echo(
      "release published; snapshot + canonical metadata re-verified;"
      " immutability asserted"
  )
  return 0


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--version", required=True)
  parser.add_argument("--public-image", required=True)
  parser.add_argument("--anchor-dir", type=pathlib.Path, required=True)
  parser.add_argument("--tag", required=True)
  parser.add_argument("--repo", required=True)
  parser.add_argument("--release-id", type=int, required=True)
  parser.add_argument(
      "--admin-token-env",
      required=True,
      help="Name of the environment variable holding a GitHub App"
      " installation token with Administration:read — used ONLY for the"
      " immutable-releases policy lookup.",
  )
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
      admin_run_gh=make_env_token_run_gh(args.admin_token_env),
  )


if __name__ == "__main__":
  raise SystemExit(main())
