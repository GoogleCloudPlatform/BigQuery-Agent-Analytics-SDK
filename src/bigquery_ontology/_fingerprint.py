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

"""Canonical model fingerprint + compile_id for concept-index provenance.

Internal module. Both the ontology compiler (``graph_ddl_compiler`` when
it gains ``compile_concept_index``) and the SDK runtime (``OntologyRuntime``
in ``bigquery_agent_analytics``) import from here via absolute import —
the contract has to agree bit-for-bit between the side that writes meta
rows and the side that verifies them, so there is exactly one definition.

The underscore prefix marks this as non-public. Not re-exported from
``bigquery_ontology/__init__.py``. See
``docs/implementation_plan_concept_index_runtime.md`` watchpoint W1.

Serialization contract (pinned):

- Input: ``BaseModel.model_dump(mode="json", by_alias=False, exclude_none=False)``.
  ``mode="json"`` normalizes enums, datetimes, and Pydantic types to
  JSON-safe primitives so the hash is stable across Python versions.
  ``exclude_none=False`` keeps optional-but-declared fields in the
  output so adding a default value later doesn't silently collide
  with the pre-default fingerprint.
- Encoding: ``json.dumps(..., sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)``. Sorted keys at every nesting level, no
  whitespace, UTF-8.
- Hash: SHA-256 over the encoded bytes; output ``"sha256:" + hexdigest``.

Never fingerprint over ``yaml.dump(model)``, ``str(model)``, or
``model.model_dump()`` without ``mode="json"``. Each of those breaks
cross-version or cross-type stability.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

_FINGERPRINT_PREFIX = "sha256:"
_COMPILE_ID_LEN = 12  # 48 bits of entropy, per issue #58.


def fingerprint_model(model: BaseModel) -> str:
  """Return the canonical SHA-256 fingerprint of a validated Pydantic model.

  Args:
      model: Any validated Pydantic model (Ontology, Binding, or similar).

  Returns:
      ``"sha256:" + 64 hex chars`` — a stable content hash over the
      model's semantic fields, invariant to source-file whitespace,
      key ordering, comments, and Pydantic-default materialization.
  """
  dumped = model.model_dump(
      mode="json",
      by_alias=False,
      exclude_none=False,
  )
  canonical = json.dumps(
      dumped,
      sort_keys=True,
      separators=(",", ":"),
      ensure_ascii=False,
  ).encode("utf-8")
  return _FINGERPRINT_PREFIX + hashlib.sha256(canonical).hexdigest()


def compile_id(
    ontology_fingerprint: str,
    binding_fingerprint: str,
    compiler_version: str,
) -> str:
  """Return the 12-hex-char pair-consistency tag used by the concept index.

  Derived deterministically from the three compile inputs so that the
  main concept-index table and its ``__meta`` sibling can be tied
  together without requiring DDL-level transactions. 48 bits is
  sufficient for pair consistency within a single ``output_table``;
  full-fingerprint freshness is checked separately against the meta
  row.

  Args:
      ontology_fingerprint: ``fingerprint_model(ontology)`` output.
      binding_fingerprint: ``fingerprint_model(binding)`` output.
      compiler_version: Version string of the compiler that produced
          the index (e.g. ``"bigquery_ontology 0.2.1"``). Semver bumps
          with behavior changes must flow through this field so the
          tag invalidates old meta.

  Returns:
      12 lowercase hex characters.
  """
  payload = "\x00".join(
      (ontology_fingerprint, binding_fingerprint, compiler_version)
  ).encode("utf-8")
  return hashlib.sha256(payload).hexdigest()[:_COMPILE_ID_LEN]
