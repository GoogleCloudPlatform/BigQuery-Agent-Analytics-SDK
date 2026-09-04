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

"""Fail-closed ``context_ref -> publication_id`` resolver (derived/demo).

Reads the ``mapping.json`` written by ``adapter.project``. A later consume
agent calls ``lookup`` instead of echoing whatever ref it was handed:
unknown refs raise ``UnknownContextRefError`` and the CLI exits 2. Results
carry only ``context_ref``, ``publication_id`` and a derived/demo label;
``never_emit_violations`` deep-scans any payload for the keys an observer
must never surface. stdlib only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

NEVER_EMIT = [
    "concept_version_id",
    "bundle_path",
    "source_path",
    "principal",
    "user_id",
    "query_text",
    "sql",
    "parameter_values",
    "destination_table",
]
LABEL = "derived/demo"


class UnknownContextRefError(KeyError):
  """The context_ref is not bound in mapping.json (fail closed)."""


def load_mapping(mapping: str | Path | dict) -> dict:
  if isinstance(mapping, dict):
    doc = mapping
  else:
    doc = json.loads(Path(mapping).read_text("utf-8"))
  table = doc.get("mapping", doc)
  if not isinstance(table, dict):
    raise ValueError("mapping.json has no 'mapping' object")
  return table


def lookup(context_ref: str, mapping: str | Path | dict) -> dict:
  """Resolve a known context_ref; raise UnknownContextRefError otherwise."""
  table = load_mapping(mapping)
  if not isinstance(context_ref, str) or context_ref not in table:
    raise UnknownContextRefError(
        f"context_ref not bound in mapping (fail closed): {context_ref!r}"
    )
  return {
      "context_ref": context_ref,
      "publication_id": table[context_ref],
      "label": LABEL,
  }


def keys_deep(obj: Any, out: set | None = None) -> set:
  out = set() if out is None else out
  if isinstance(obj, list):
    for v in obj:
      keys_deep(v, out)
  elif isinstance(obj, dict):
    for k, v in obj.items():
      out.add(k)
      keys_deep(v, out)
  return out


def never_emit_violations(payload: Any) -> list[str]:
  keys = keys_deep(payload)
  return [k for k in NEVER_EMIT if k in keys]


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("context_ref")
  parser.add_argument("--mapping", required=True, help="path to mapping.json")
  args = parser.parse_args(argv)
  try:
    result = lookup(args.context_ref, args.mapping)
  except UnknownContextRefError as exc:
    print(f"FAIL_CLOSED {exc.args[0]}", file=sys.stderr)
    return 2
  violations = never_emit_violations(result)
  if violations:
    print(f"FAIL_CLOSED never-emit keys present: {violations}", file=sys.stderr)
    return 2
  print(json.dumps(result, indent=2))
  return 0


if __name__ == "__main__":
  sys.exit(main())
