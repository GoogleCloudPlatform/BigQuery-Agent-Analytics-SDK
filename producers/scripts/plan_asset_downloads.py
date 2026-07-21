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

"""Asset-download planner for finalize's trust boundary (#356 round 14).

Release asset metadata is operator/attacker-influencable, so which
bytes get streamed must be decided by PR-TESTED code, not inline shell:

  - metadata must be well-formed: every entry an object with a positive
    integer ``id``, a non-empty ``name`` matching ``[A-Za-z0-9._+-]+``
    (no path separators, no traversal), and a non-negative integer
    ``size``; duplicates are ambiguous — all of these FAIL the plan;
  - ONLY the four expected hardcoded names (wheel, sdist, plugin,
    SHA256SUMS) within the size cap are planned as downloads, by asset
    ID, to their own hardcoded destination names;
  - unexpected or oversized assets are planned as EMPTY placeholders:
    never streamed, but present in the release directory so the tested
    reconciler still sees the exact-set / byte-identity mismatch and
    emits its classification instead of the job dying on disk or
    timeout.

Plan lines on stdout, one per asset, tab-separated:
    download\t<asset_id>\t<name>
    placeholder\t<name>
Exit 0 = valid plan (possibly empty); exit 1 = untrusted metadata, with
a ``detail=`` line explaining why.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Callable

DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # far above any real artifact
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")


def expected_names(version: str) -> tuple[str, str, str, str]:
  return (
      f"bigquery_agent_analytics_tracing-{version}-py3-none-any.whl",
      f"bigquery_agent_analytics_tracing-{version}.tar.gz",
      f"bigquery-agent-analytics-tracing-claude-code-{version}.tar.gz",
      "SHA256SUMS",
  )


def _is_int(value) -> bool:
  return isinstance(value, int) and not isinstance(value, bool)


def plan(
    *, version: str, assets, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[list[tuple[str, ...]] | None, str]:
  """Returns (plan, "") or (None, why). Each plan entry is
  ("download", id, name) or ("placeholder", name)."""
  if not isinstance(assets, list):
    return None, f"assets is {type(assets).__name__}, expected a list"
  allowed = set(expected_names(version))
  seen: set[str] = set()
  entries: list[tuple[str, ...]] = []
  for asset in assets:
    if not isinstance(asset, dict):
      return None, f"asset entry is {type(asset).__name__}, expected an object"
    asset_id = asset.get("id")
    name = asset.get("name")
    size = asset.get("size")
    if not _is_int(asset_id) or asset_id <= 0:
      return None, f"asset id {asset_id!r} is not a positive integer"
    if not isinstance(name, str) or not name:
      return None, "asset entry has no non-empty string name"
    if not _SAFE_NAME.fullmatch(name) or name in (".", ".."):
      return None, f"asset name {name!r} is unsafe — refusing to touch it"
    if name in seen:
      return None, f"duplicate asset name {name!r} — ambiguous release state"
    seen.add(name)
    if not _is_int(size) or size < 0:
      return None, f"asset {name} size {size!r} is not a non-negative integer"
    if name in allowed and size <= max_bytes:
      entries.append(("download", str(asset_id), name))
    else:
      # Unexpected name or oversized expected asset: never streamed;
      # the placeholder keeps the byte-level mismatch visible to the
      # reconciler's tested classification.
      entries.append(("placeholder", name))
  return entries, ""


def main(
    argv: list[str] | None = None, echo: Callable[[str], None] = print
) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--version", required=True)
  parser.add_argument("--assets-json", type=pathlib.Path, required=True)
  parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
  args = parser.parse_args(argv)
  try:
    assets = json.loads(args.assets_json.read_text())
  except ValueError as exc:
    echo(f"detail=unparseable assets metadata: {exc}")
    return 1
  entries, why = plan(
      version=args.version, assets=assets, max_bytes=args.max_bytes
  )
  if entries is None:
    echo(f"detail=untrusted release asset metadata: {why}")
    return 1
  for entry in entries:
    echo("\t".join(entry))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
