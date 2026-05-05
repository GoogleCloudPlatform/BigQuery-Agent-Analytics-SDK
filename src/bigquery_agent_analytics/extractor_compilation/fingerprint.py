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

"""Compile-input fingerprint for compiled structured extractors.

The fingerprint is the cache key under which a bundle is stored and
the gate that lets a future runtime loader (C2) decide whether a
bundle still matches the active compile inputs. Per #75:

    sha256(ontology, binding, event_schema, event_allowlist,
           transcript_builder_version, content_serialization_rules,
           extraction_rules, template_version,
           compiler_package_version)

Two compile runs on identical inputs must produce byte-identical
fingerprints (and therefore byte-identical bundles). Changing any
of the nine inputs invalidates the fingerprint — stale bundles are
detected at load time, not silently re-used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Canonical separator between named fingerprint inputs. ``\x1f`` is
# ASCII "unit separator" and cannot appear in the JSON / utf-8 byte
# forms produced by ``_canon`` below, so it provides an unambiguous
# field boundary. Without a separator, two distinct input pairs
# could concatenate to the same byte sequence.
_SEP = b"\x1f"


def _canon(value: Any) -> bytes:
  """Produce a canonical byte form for one fingerprint input.

  ``str`` and ``bytes`` are passed through (utf-8 encoded for str).
  Mappings, lists, and tuples are JSON-serialized with
  ``sort_keys=True`` so dict insertion order doesn't change the
  digest.
  """
  if isinstance(value, (bytes, bytearray)):
    return bytes(value)
  if isinstance(value, str):
    return value.encode("utf-8")
  return json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
      "utf-8"
  )


def compute_fingerprint(
    *,
    ontology_text: str,
    binding_text: str,
    event_schema: Any,
    event_allowlist: tuple[str, ...] | list[str],
    transcript_builder_version: str,
    content_serialization_rules: Any,
    extraction_rules: Any,
    template_version: str,
    compiler_package_version: str,
) -> str:
  """Return the hex sha256 of the #75 compile-input tuple.

  Inputs are passed by keyword so the call site documents which
  field is which. ``event_allowlist`` is sorted before hashing so
  caller-side ordering is irrelevant.

  Returns:
      A 64-character lowercase hex string suitable as a directory
      name and as a cache key.
  """
  hasher = hashlib.sha256()
  named_inputs = (
      ("ontology_text", ontology_text),
      ("binding_text", binding_text),
      ("event_schema", event_schema),
      ("event_allowlist", sorted(event_allowlist)),
      ("transcript_builder_version", transcript_builder_version),
      ("content_serialization_rules", content_serialization_rules),
      ("extraction_rules", extraction_rules),
      ("template_version", template_version),
      ("compiler_package_version", compiler_package_version),
  )
  for name, value in named_inputs:
    hasher.update(name.encode("utf-8"))
    hasher.update(_SEP)
    hasher.update(_canon(value))
    hasher.update(_SEP)
  return hasher.hexdigest()
