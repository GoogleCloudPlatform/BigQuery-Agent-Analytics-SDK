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

"""Pin a derived publication and compile its sanctioned SQL.

``load_publication`` reads the copied Acme source bytes, checks them against
the digest pinned in the manifest, extracts exactly one ``sql`` fence and
the declared DATE parameters, and returns a validated publication dict.
``compile_sql`` performs only allowlisted backticked table-identifier
substitutions on the pinned bytes; comments and quoted literals are never
touched, so any other edit produces a different ``compiled_sql_digest``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

import contracts

_FENCE_RE = re.compile(r"```sql\n(.*?)```", re.DOTALL)
_BACKTICK_RE = re.compile(r"`([^`]*)`")
_PARAM_RE = re.compile(
    r"^\s*-\s*\{\s*name:\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*type:\s*([a-z]+)"
    r"\s*,\s*required:\s*(true|false)\s*\}\s*$"
)
_NAMED_PARAM_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
_TABLE_PART_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
# Statement shapes the executor must never submit (spec section 3).
_FORBIDDEN_RE = re.compile(
    r"\b(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|MERGE|CALL|EXECUTE|DECLARE|"
    r"BEGIN|EXPORT|LOAD|GRANT|REVOKE|EXTERNAL_QUERY|ML\.)\b",
    re.IGNORECASE,
)

MANIFEST_REQUIRED = (
    "publication_id",
    "computation_path",
    "computation_sha256",
    "runtime",
    "project",
    "location",
    "dataset",
    "table_map",
    "parameters",
    "output",
    "policy_version",
    "context_ref",
)


def _parse_parameters(frontmatter: str) -> list[dict]:
  params: list[dict] = []
  in_block = False
  for line in frontmatter.splitlines():
    if line.startswith("parameters:"):
      in_block = True
      continue
    if in_block:
      m = _PARAM_RE.match(line)
      if m:
        params.append({
            "name": m.group(1),
            "type": m.group(2).upper(),
            "required": m.group(3) == "true",
        })
        continue
      if line.strip() and not line.startswith(" "):
        in_block = False
  return params


def load_publication(bundle_dir: str, manifest: dict) -> dict:
  """Load and pin a derived publication from trusted local bytes."""
  manifest = contracts.require_keys(manifest, MANIFEST_REQUIRED, "manifest")
  if manifest["runtime"] != "bigquery":
    raise contracts.ContractError("only runtime: bigquery is supported")
  path = Path(bundle_dir) / manifest["computation_path"]
  raw = path.read_bytes()
  actual = hashlib.sha256(raw).hexdigest()
  if actual != manifest["computation_sha256"]:
    raise contracts.ContractError(
        "computation bytes do not match the pinned digest"
    )
  text = raw.decode("utf-8")
  if not text.startswith("---\n"):
    raise contracts.ContractError("computation has no frontmatter")
  end = text.find("\n---\n", 4)
  if end < 0:
    raise contracts.ContractError("unterminated frontmatter")
  frontmatter, body = text[4:end], text[end + 5 :]
  if "type: Attested Computation" not in frontmatter:
    raise contracts.ContractError("not an Attested Computation")
  fences = _FENCE_RE.findall(body)
  if len(fences) != 1:
    raise contracts.ContractError(
        f"expected exactly one sql fence, found {len(fences)}"
    )
  sanctioned_sql = fences[0]

  declared = _parse_parameters(frontmatter)
  manifest_params = manifest["parameters"]
  if not declared or declared != manifest_params:
    raise contracts.ContractError(
        "manifest parameters differ from the computation declaration"
    )
  names = [p["name"] for p in declared]
  if len(set(names)) != len(names):
    raise contracts.ContractError("duplicate parameter names")
  for p in declared:
    if p["type"] != "DATE" or not p["required"]:
      raise contracts.ContractError("only required DATE parameters allowed")
  used = set(_NAMED_PARAM_RE.findall(sanctioned_sql))
  if used != set(names):
    raise contracts.ContractError(
        f"SQL parameters {sorted(used)} != declared {sorted(names)}"
    )

  table_map = manifest["table_map"]
  if not isinstance(table_map, dict) or not table_map:
    raise contracts.ContractError("table_map must be a non-empty mapping")
  referenced = set(_BACKTICK_RE.findall(sanctioned_sql))
  if referenced != set(table_map):
    raise contracts.ContractError(
        "backticked identifiers in SQL must equal the table_map keys"
    )
  for src, dst in table_map.items():
    if not _TABLE_PART_RE.match(dst) or "*" in src:
      raise contracts.ContractError(f"bad table mapping {src!r} -> {dst!r}")
  for part in (manifest["project"], manifest["dataset"]):
    if not _TABLE_PART_RE.match(part):
      raise contracts.ContractError("bad project/dataset identifier")

  output = contracts.require_keys(
      manifest["output"], ("field", "type", "unit", "label"), "output"
  )
  if output["type"] != "NUMERIC":
    raise contracts.ContractError("only a NUMERIC output is supported")
  contracts.validate_claim({"field": output["field"], "value": "0", "unit": output["unit"]})

  pub = {
      "publication_id": manifest["publication_id"],
      "context_ref": manifest["context_ref"],
      "synthetic": bool(manifest.get("synthetic", False)),
      "derived_from": manifest.get("derived_from", ""),
      "computation_path": manifest["computation_path"],
      "computation_digest": contracts.digest_bytes(
          contracts.DOMAIN_COMPUTATION, raw
      ),
      "computation_sha256": actual,
      "sanctioned_sql": sanctioned_sql,
      "project": manifest["project"],
      "location": manifest["location"],
      "dataset": manifest["dataset"],
      "table_map": dict(table_map),
      "parameters": [dict(p) for p in declared],
      "output": {k: output[k] for k in ("field", "type", "unit", "label")},
      "policy_version": manifest["policy_version"],
      "compiler_version": contracts.COMPILER_VERSION,
      "bundle_dir": str(Path(bundle_dir).resolve()),
  }
  pub["dependencies"] = sorted(
      f"{pub['project']}.{pub['dataset']}.{t}" for t in table_map.values()
  )
  pub["compiled_sql"] = compile_sql(pub)
  pub["compiled_sql_digest"] = contracts.digest_bytes(
      contracts.DOMAIN_SQL, pub["compiled_sql"].encode("utf-8")
  )
  pub["output_contract_digest"] = contracts.digest(
      contracts.DOMAIN_OUTPUT, pub["output"]
  )
  pub["publication_digest"] = contracts.digest(
      contracts.DOMAIN_PUBLICATION,
      {
          "publication_id": pub["publication_id"],
          "context_ref": pub["context_ref"],
          "computation_digest": pub["computation_digest"],
          "compiled_sql_digest": pub["compiled_sql_digest"],
          "output_contract_digest": pub["output_contract_digest"],
          "dependencies": pub["dependencies"],
          "parameters": pub["parameters"],
          "table_map": pub["table_map"],
          "project": pub["project"],
          "location": pub["location"],
          "policy_version": pub["policy_version"],
          "compiler_version": pub["compiler_version"],
      },
  )
  return pub


def compile_sql(publication: dict) -> str:
  """Allowlisted table substitution over the pinned sanctioned SQL bytes."""
  sql = publication["sanctioned_sql"]
  table_map = publication["table_map"]
  project, dataset = publication["project"], publication["dataset"]
  if _FORBIDDEN_RE.search(sql) or ";" in sql:
    raise contracts.ContractError("sanctioned SQL is not a single SELECT")

  def _sub(m: re.Match[str]) -> str:
    key = m.group(1)
    if key not in table_map:
      raise contracts.ContractError(f"unmapped identifier `{key}`")
    return f"`{project}.{dataset}.{table_map[key]}`"

  compiled = _BACKTICK_RE.sub(_sub, sql)
  if compiled.lstrip().upper().split()[0] not in ("WITH", "SELECT"):
    raise contracts.ContractError("compiled SQL must start with WITH/SELECT")
  return compiled


def parameter_types(publication: dict) -> dict[str, str]:
  return {p["name"]: p["type"] for p in publication["parameters"]}


def load_manifest(path: str | Path) -> dict:
  import json

  return json.loads(Path(path).read_text(encoding="utf-8"))


def load_fixture_publication(example_dir: str | Path | None = None) -> dict:
  """Load the committed derived fixture publication for this example."""
  here = Path(example_dir) if example_dir else Path(__file__).resolve().parent
  manifest = load_manifest(here / "fixtures" / "publication.json")
  return load_publication(str(here / "fixtures"), manifest)


def attester_artifact_hash(example_dir: str | Path | None = None) -> str:
  """Digest of the trusted verifier/contract code bytes used for a verdict."""
  here = Path(example_dir) if example_dir else Path(__file__).resolve().parent
  parts: dict[str, Any] = {}
  for name in ("contracts.py", "publication.py", "verify.py"):
    parts[name] = hashlib.sha256((here / name).read_bytes()).hexdigest()
  return contracts.digest(contracts.DOMAIN_ATTESTER, parts)
