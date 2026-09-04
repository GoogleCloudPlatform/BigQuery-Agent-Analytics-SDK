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

"""Render the customer-first release notes (issue #349 / #356 review).

Checked-in and PR-tested so placeholder drift fails in Producers CI,
not after the release tag is pushed. The Codex compatibility pin comes
from the canonical constant in config_artifacts — never a hand copy.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from bigquery_agent_analytics_tracing.otlp import config_artifacts

# Claude Code version the telemetry contracts were last verified against.
CLAUDE_VERIFIED_VERSION = "2.1.203"

_TEMPLATE_PATH = pathlib.Path(__file__).parent / "release_notes_template.md"
_IMAGE_RE = re.compile(r"[a-z0-9.-]+\.pkg\.dev(/[A-Za-z0-9._-]+){3}")


def render(*, version: str, digest: str, public_image: str) -> str:
  # The coordinate is passed through from the workflow's authoritative
  # env var — the renderer holds no duplicate copy (#356 review).
  if not _IMAGE_RE.fullmatch(public_image):
    raise ValueError(f"malformed public image coordinate {public_image!r}")
  template = _TEMPLATE_PATH.read_text()
  return template.format(
      version=version,
      public_image=public_image,
      digest=digest,
      claude_version=CLAUDE_VERIFIED_VERSION,
      codex_min_version=config_artifacts.CODEX_MIN_VERSION,
  )


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--version", required=True)
  parser.add_argument("--digest", required=True)
  parser.add_argument("--public-image", required=True)
  parser.add_argument("--out", type=pathlib.Path, required=True)
  args = parser.parse_args(argv)
  args.out.write_text(
      render(
          version=args.version,
          digest=args.digest,
          public_image=args.public_image,
      )
  )
  print(f"wrote {args.out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
