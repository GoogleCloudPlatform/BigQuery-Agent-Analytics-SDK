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

"""Bundle manifest for compiled structured extractors.

Saved as ``manifest.json`` next to a compiled bundle's generated
Python module. C2's runtime loader (deferred) reads this to decide
whether a bundle still matches the active ``(ontology, binding,
event_schema, ...)`` tuple before importing the module.

Manifest fields are all primitive / serializable so the file is
inspectable without importing the SDK.
"""

from __future__ import annotations

import dataclasses
import datetime
import json


@dataclasses.dataclass(frozen=True)
class Manifest:
  """Compile-time provenance for one bundle.

  One bundle == one compiled extractor function written to one
  Python module. PR 4b.1 keeps the model minimal — multi-function
  bundles can land later if a real use case appears.
  """

  fingerprint: str
  event_types: tuple[str, ...]
  module_filename: str
  function_name: str
  compiler_package_version: str
  template_version: str
  transcript_builder_version: str
  created_at: str

  def to_json(self) -> str:
    """Serialize to a JSON string with sorted keys.

    ``sort_keys=True`` makes the serialization deterministic for a
    given set of field values — two ``Manifest`` instances with
    identical fields produce byte-identical JSON. The on-disk
    bundle as a whole is byte-stable across consecutive
    ``compile_extractor`` calls because the second call is a cache
    hit and writes nothing — *not* because the manifest's own
    ``created_at`` is preserved across writes (it isn't; each
    write stamps a fresh timestamp).
    """
    payload = dataclasses.asdict(self)
    # asdict preserves the tuple, but json serializes it as a list.
    # Stash it as a list explicitly so from_json's reverse mapping
    # is symmetric with the on-disk form.
    payload["event_types"] = list(self.event_types)
    return json.dumps(payload, indent=2, sort_keys=True)

  @classmethod
  def from_json(cls, text: str) -> "Manifest":
    raw = json.loads(text)
    return cls(
        fingerprint=raw["fingerprint"],
        event_types=tuple(raw["event_types"]),
        module_filename=raw["module_filename"],
        function_name=raw["function_name"],
        compiler_package_version=raw["compiler_package_version"],
        template_version=raw["template_version"],
        transcript_builder_version=raw["transcript_builder_version"],
        created_at=raw["created_at"],
    )


def now_iso_utc() -> str:
  """ISO-8601 UTC timestamp for ``Manifest.created_at``."""
  return datetime.datetime.now(datetime.timezone.utc).isoformat()
