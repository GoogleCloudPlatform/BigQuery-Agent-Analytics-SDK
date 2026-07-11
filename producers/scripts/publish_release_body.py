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

"""Publish-time body re-anchor (#356 rounds 6–7), as one tested path.

Asset reconciliation never reads the draft body, and anything holding
``contents: write`` could have edited it during the approval pause. This
helper composes the full re-anchor: extract the digest from the ANCHOR
wheel → render the notes from the protected checkout → publish
atomically with ``gh release edit --notes-file … --draft=false
--latest=false``. PR-tested with a fake anchor wheel and a stubbed gh.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import release_image_tool
import render_release_notes


def _run_gh(argv: list[str]) -> int:
  return subprocess.run(argv, check=False).returncode


def publish(
    *,
    version: str,
    public_image: str,
    anchor_dir: pathlib.Path,
    tag: str,
    repo: str,
    out_dir: pathlib.Path,
    run_gh: Callable[[list[str]], int] = _run_gh,
) -> int:
  wheel = (
      anchor_dir
      / f"bigquery_agent_analytics_tracing-{version}-py3-none-any.whl"
  )
  if not wheel.is_file():
    raise FileNotFoundError(f"anchor wheel not found: {wheel}")
  reference = release_image_tool.extract_from_wheel(wheel)
  digest = reference.split("@", 1)[1]
  body_path = out_dir / "release_body.md"
  body_path.write_text(
      render_release_notes.render(
          version=version, digest=digest, public_image=public_image
      )
  )
  return run_gh(
      [
          "gh",
          "release",
          "edit",
          tag,
          "--repo",
          repo,
          "--notes-file",
          str(body_path),
          "--draft=false",
          "--latest=false",
      ]
  )


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--version", required=True)
  parser.add_argument("--public-image", required=True)
  parser.add_argument("--anchor-dir", type=pathlib.Path, required=True)
  parser.add_argument("--tag", required=True)
  parser.add_argument("--repo", required=True)
  parser.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path("."))
  args = parser.parse_args(argv)
  return publish(
      version=args.version,
      public_image=args.public_image,
      anchor_dir=args.anchor_dir,
      tag=args.tag,
      repo=args.repo,
      out_dir=args.out_dir,
  )


if __name__ == "__main__":
  raise SystemExit(main())
