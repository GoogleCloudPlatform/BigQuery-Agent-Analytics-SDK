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

"""Release image injection + artifact verification (issue #349).

Three subcommands used by release-tracing.yml:

  inject     write the pinned public image reference into _release.py
             BEFORE wheel/sdist are built (both embed the same constant)
  verify     assert wheel and sdist embed the identical expected reference
             (the mechanical digest-equality gate)
  checksums  write SHA256SUMS for every file in the dist directory
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import pathlib
import re
import sys
import tarfile
import zipfile

_MODULE_RELPATH = "bigquery_agent_analytics_tracing/otlp/_release.py"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TEMPLATE = '''\
"""Released receiver image reference — written by release_image_tool.py.

Pinned by digest per the issue #349 release contract; the version tag is
present for human readability only.
"""

RELEASE_IMAGE = "{reference}"
'''


class ReleaseVerificationError(Exception):
  """A release artifact does not embed the expected image reference."""


def inject(target: pathlib.Path, coordinate: str, digest: str) -> str:
  """Write the pinned reference into ``target``; returns the reference."""
  if not _DIGEST_RE.match(digest):
    raise ValueError(f"malformed digest {digest!r} (want sha256:<64 hex>)")
  tag = coordinate.rsplit("/", 1)[-1].partition(":")[2]
  if not tag:
    raise ValueError(f"coordinate {coordinate!r} has no version tag")
  if tag == "latest":
    raise ValueError("'latest' is never published per the release contract")
  if "@" in coordinate:
    raise ValueError(f"coordinate {coordinate!r} must not already pin a digest")
  reference = f"{coordinate}@{digest}"
  target.write_text(_TEMPLATE.format(reference=reference))
  return reference


def _reference_from_source(text: str, origin: str) -> str | None:
  """Extract the RELEASE_IMAGE constant from module source via AST."""
  for node in ast.parse(text).body:
    if isinstance(node, ast.Assign):
      for name in node.targets:
        if isinstance(name, ast.Name) and name.id == "RELEASE_IMAGE":
          value = ast.literal_eval(node.value)
          return value
  raise ReleaseVerificationError(f"no RELEASE_IMAGE constant in {origin}")


def extract_from_wheel(wheel: pathlib.Path) -> str | None:
  with zipfile.ZipFile(wheel) as zf:
    text = zf.read(_MODULE_RELPATH).decode()
  return _reference_from_source(text, f"wheel {wheel.name}")


def extract_from_sdist(sdist: pathlib.Path) -> str | None:
  with tarfile.open(sdist) as tf:
    for member in tf.getmembers():
      if member.name.endswith(_MODULE_RELPATH):
        text = tf.extractfile(member).read().decode()
        return _reference_from_source(text, f"sdist {sdist.name}")
  raise ReleaseVerificationError(f"no {_MODULE_RELPATH} in sdist {sdist.name}")


def verify_artifacts(
    wheel: pathlib.Path, sdist: pathlib.Path, expected: str
) -> None:
  """The mechanical equality gate: wheel == sdist == expected, no placeholders."""
  from_wheel = extract_from_wheel(wheel)
  from_sdist = extract_from_sdist(sdist)
  if from_wheel is None or from_sdist is None:
    raise ReleaseVerificationError(
        "artifact still carries the dev placeholder (RELEASE_IMAGE = None) — "
        "inject must run before build"
    )
  if from_wheel != from_sdist:
    raise ReleaseVerificationError(
        f"wheel embeds {from_wheel!r} but sdist embeds {from_sdist!r}"
    )
  if from_wheel != expected:
    raise ReleaseVerificationError(
        f"artifacts embed {from_wheel!r} but expected {expected!r}"
    )


def write_checksums(dist_dir: pathlib.Path) -> pathlib.Path:
  out = dist_dir / "SHA256SUMS"
  lines = []
  for path in sorted(dist_dir.iterdir()):
    if path.is_file() and path.name != out.name:
      digest = hashlib.sha256(path.read_bytes()).hexdigest()
      lines.append(f"{digest}  {path.name}")
  out.write_text("\n".join(lines) + "\n")
  return out


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  sub = parser.add_subparsers(dest="command", required=True)

  p_inject = sub.add_parser("inject")
  p_inject.add_argument("--target", type=pathlib.Path, required=True)
  p_inject.add_argument("--coordinate", required=True)
  p_inject.add_argument("--digest", required=True)

  p_verify = sub.add_parser("verify")
  p_verify.add_argument("--wheel", type=pathlib.Path, required=True)
  p_verify.add_argument("--sdist", type=pathlib.Path, required=True)
  p_verify.add_argument("--expected", required=True)

  p_sums = sub.add_parser("checksums")
  p_sums.add_argument("--dist-dir", type=pathlib.Path, required=True)

  args = parser.parse_args(argv)
  try:
    if args.command == "inject":
      print(inject(args.target, args.coordinate, args.digest))
    elif args.command == "verify":
      verify_artifacts(args.wheel, args.sdist, expected=args.expected)
      print(f"digest-equality gate passed: {args.expected}")
    elif args.command == "checksums":
      print(write_checksums(args.dist_dir))
  except (ValueError, ReleaseVerificationError) as exc:
    print(f"error: {exc}", file=sys.stderr)
    return 2
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
