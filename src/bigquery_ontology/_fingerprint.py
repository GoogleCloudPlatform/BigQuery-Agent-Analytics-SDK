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

"""Canonical fingerprint primitives for concept-index provenance.

Internal module. Both the ontology compiler (``graph_ddl_compiler`` when
it gains ``compile_concept_index``) and the SDK runtime (``OntologyRuntime``
in ``bigquery_agent_analytics``) import from here via absolute import —
the contract has to agree bit-for-bit between the side that writes meta
rows and the side that verifies them, so there is exactly one definition.

The underscore prefix marks this as non-public. Not re-exported from
``bigquery_ontology/__init__.py``. See
``docs/implementation_plan_concept_index_runtime.md`` watchpoints W1/W2.

Three exports, three roles:

- :func:`fingerprint_model` — full SHA-256 over a validated Pydantic
  model, returned as ``"sha256:" + 64 hex chars``. Used by the
  compiler to fingerprint the ``Ontology`` and ``Binding`` inputs,
  and by the runtime to compute cached local fingerprints from the
  same models.
- :func:`compile_fingerprint` — full 64-hex SHA-256 over the concat
  of ``ontology_fingerprint``, ``binding_fingerprint``, and
  ``compiler_version``. **Canonical integrity key.** Used for strict
  verification: main↔meta pair consistency and runtime freshness
  checks.
- :func:`compile_id` — 12-hex display/debug token. Derived as
  ``compile_fingerprint(...)[:_COMPILE_ID_LEN]`` — always a
  structural truncation of the full integrity key, never its own
  hash. Use for operator UX (reports, queue rows, log lines).
  **Never use as the sole freshness check.**

Invariant: ``compile_id == compile_fingerprint[:12]``. Enforced at
the function boundary so a future refactor cannot let the two drift
out of sync.

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
- Hash: SHA-256 over the encoded bytes; output ``"sha256:" + hexdigest``
  for model fingerprints, raw hex for compile fingerprints.

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


def compile_fingerprint(
    ontology_fingerprint: str,
    binding_fingerprint: str,
    compiler_version: str,
) -> str:
  """Return the full 64-hex canonical integrity key for a compile.

  **Canonical integrity key.** Used by strict verification for main
  ↔ meta pair consistency and runtime freshness checks against
  cached local fingerprints. Never truncate this for verification
  purposes. See ``compile_id`` for the short display/debug companion.

  Exact payload contract (pinned):

  .. code-block:: text

      payload = utf8(ontology_fingerprint + "\\x00" +
                     binding_fingerprint + "\\x00" +
                     compiler_version)
      digest  = sha256(payload).hexdigest()  # 64 lowercase hex chars

  The separator is a single NUL byte (``\\x00``). NUL is chosen
  because it cannot appear in any of the three inputs (fingerprints
  are hex; version strings are ASCII), so the delimited encoding is
  unambiguous — no two different input triples can produce the same
  payload. Do not reimplement this elsewhere; import
  :func:`compile_fingerprint` from this module. Regression tests
  pin a golden vector so a silent payload change is caught.

  Args:
      ontology_fingerprint: ``fingerprint_model(ontology)`` output.
      binding_fingerprint: ``fingerprint_model(binding)`` output.
      compiler_version: Version string of the compiler that produced
          the index (e.g. ``"bigquery_ontology 0.2.1"``). Semver bumps
          with behavior changes must flow through this field so the
          fingerprint invalidates old meta.

  Returns:
      64 lowercase hex characters (256 bits).
  """
  payload = "\x00".join(
      (ontology_fingerprint, binding_fingerprint, compiler_version)
  ).encode("utf-8")
  return hashlib.sha256(payload).hexdigest()


def compile_id(
    ontology_fingerprint: str,
    binding_fingerprint: str,
    compiler_version: str,
) -> str:
  """Return the 12-hex-char display token for a compile.

  Derived as ``compile_fingerprint(...)[:_COMPILE_ID_LEN]``. The short
  form is always a structural truncation of the full integrity key —
  never its own hash — so that the two provenance columns in the
  concept index cannot drift out of sync.

  **Display-only.** Use this for operator reports, error messages,
  queue rows, and log lines. Never use ``compile_id`` as the sole
  freshness check; strict verification must use
  :func:`compile_fingerprint` directly.

  Args:
      ontology_fingerprint: ``fingerprint_model(ontology)`` output.
      binding_fingerprint: ``fingerprint_model(binding)`` output.
      compiler_version: Version string of the compiler that produced
          the index.

  Returns:
      12 lowercase hex characters — the first 12 chars of
      ``compile_fingerprint(ontology_fingerprint, binding_fingerprint,
      compiler_version)``.
  """
  return compile_fingerprint(
      ontology_fingerprint, binding_fingerprint, compiler_version
  )[:_COMPILE_ID_LEN]
