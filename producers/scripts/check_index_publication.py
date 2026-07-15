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

"""Rerun-safe pre-upload gate for the publish jobs (#356 round 12).

The uploader can succeed while the job fails afterward (lost response,
runner death). Because ``skip-existing`` is deliberately disabled — a
silently-skipped burned version would make the lifecycle gate test old
bytes — a plain rerun of the failed upload job could never succeed at
the same version. This gate makes the rerun viable without weakening
the byte-identity contract:

  absent        the index has no record for this version (explicit
                404) → proceed with the upload
  satisfied     the index already carries EXACTLY the wheel + sdist
                about to be uploaded (exact filename set, nothing
                yanked, byte digests identical to the local files) →
                the upload stage is already satisfied; skip it and
                continue the pipeline
  conflict      a VALIDATED deviation (subset, extras, yanked, digest
                mismatch, zero-file release record) → the version is
                burned on this index; bump + re-tag
  indeterminate the response or local inputs prove nothing (malformed
                HTTP-200 body, schema violation, missing local dist
                file, or a local publish set that is not EXACTLY the
                wheel + sdist — the uploader would publish extras
                irreversibly) → refetch / investigate before acting; a
                transient CDN or API glitch is NOT a version burn
                (#356 rounds 13-14)

Exit code is 0 for absent/satisfied and 1 otherwise, so the workflow
step fails closed on any state that cannot lead to a valid release —
but only `conflict` carries burn advice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import reconcile_release

ABSENT = "absent"
SATISFIED = "satisfied"
CONFLICT = "conflict"
INDETERMINATE = "indeterminate"


def check(
    *,
    version: str,
    dist_dir: pathlib.Path,
    index: dict | None,
) -> tuple[str, str]:
  """Returns (status, detail). ``index=None`` = an explicit 404."""
  wheel = f"bigquery_agent_analytics_tracing-{version}-py3-none-any.whl"
  sdist = f"bigquery_agent_analytics_tracing-{version}.tar.gz"
  # The LOCAL publish set must be exactly wheel + sdist (#356 round
  # 14): the uploader publishes every distribution left in the
  # directory, so an unexpected extra local file would be uploaded
  # irreversibly at this version before finalize could ever reject it.
  present = (
      sorted(p.name for p in dist_dir.iterdir()) if dist_dir.is_dir() else []
  )
  extras = [name for name in present if name not in (wheel, sdist)]
  if extras:
    return (
        INDETERMINATE,
        f"local publish set contains unexpected distributions {extras} —"
        " the uploader would publish them irreversibly; investigate the"
        " build artifact and strip step (this is not an index burn)",
    )
  local: dict[str, str] = {}
  for name in (wheel, sdist):
    path = dist_dir / name
    if not path.is_file():
      return (
          INDETERMINATE,
          f"local distribution {name} is missing from {dist_dir} —"
          " investigate the build artifact; this is not an index burn",
      )
    local[name] = hashlib.sha256(path.read_bytes()).hexdigest()

  if index is None:
    return ABSENT, "index returned an explicit 404 for this version"

  urls, why = reconcile_release._validated_urls(index)  # pylint: disable=protected-access
  if urls is None:
    # A malformed success body proves neither absence nor a burn — the
    # operator must refetch, never bump + re-tag off a CDN glitch.
    return (
        INDETERMINATE,
        f"invalid index response schema: {why} — refetch the index and"
        " re-run before taking any recovery action",
    )
  if set(urls) != set(local):
    missing = set(local) - set(urls)
    extra = set(urls) - set(local)
    return (
        CONFLICT,
        f"index file set mismatch (missing: {sorted(missing)},"
        f" extra: {sorted(extra)})",
    )
  for name in sorted(local):
    if urls[name]["yanked"]:
      return CONFLICT, f"index file {name} is yanked"
    if urls[name]["digests"]["sha256"] != local[name]:
      return CONFLICT, f"index digest of {name} differs from the local file"
  return SATISFIED, "index already carries the exact local wheel + sdist"


def main(
    argv: list[str] | None = None, echo: Callable[[str], None] = print
) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--version", required=True)
  parser.add_argument("--dist-dir", type=pathlib.Path, required=True)
  parser.add_argument(
      "--index-json",
      type=pathlib.Path,
      default=None,
      help="File holding the index release JSON (HTTP 200 body).",
  )
  parser.add_argument(
      "--index-missing",
      action="store_true",
      help="The index returned an explicit 404 — the only absence marker.",
  )
  args = parser.parse_args(argv)
  if (args.index_json is None) == (not args.index_missing):
    parser.error("exactly one of --index-json / --index-missing required")
  try:
    if args.index_missing:
      index = None
    else:
      index = json.loads(args.index_json.read_text())
      if not isinstance(index, dict):
        raise ValueError(f"expected a JSON object, got {type(index).__name__}")
  except ValueError as exc:
    status, detail = (
        INDETERMINATE,
        f"unparseable index body: {exc} — refetch the index and re-run"
        " before taking any recovery action",
    )
  else:
    status, detail = check(
        version=args.version, dist_dir=args.dist_dir, index=index
    )
  echo(f"status={status}")
  echo(f"detail={detail}")
  return 0 if status in (ABSENT, SATISFIED) else 1


if __name__ == "__main__":
  raise SystemExit(main())
