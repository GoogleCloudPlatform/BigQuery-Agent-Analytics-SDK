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

"""Internal routing helpers shared by the validator and materializer.

The validator (``graph_validation.validate_extracted_graph``) and the
materializer (``ontology_materializer._route_node`` / ``_route_edge``)
must agree on two contracts:

1. **Property-name resolution.** An extracted property is matched
   against a spec property by either the property's
   ``logical_name`` (ontology-level) or its ``column`` (physical
   column from the binding). When a binding renames a logical name,
   logical-name lookup wins so a renamed property stays routable.

2. **Endpoint-key parsing from node IDs.** Edge endpoint FK columns
   are derived from the edge's ``from_node_id`` / ``to_node_id``
   string by splitting the trailing ``k1=v1,k2=v2`` segment.

Putting both helpers in one module guarantees the two callers stay in
lockstep — earlier versions had subtle precedence and parsing
divergences that let validator-clean extractions silently corrupt at
INSERT time.

The module is private (leading underscore) because it carries no
public API surface; both caller modules treat it as internal plumbing.
"""

from __future__ import annotations


def build_property_lookup(properties):
  """Return ``{name: ResolvedProperty}`` for both name and column.

  Two-pass insertion: columns first, then logical names. Logical
  names therefore win on collision — a property whose ``column``
  happens to equal another property's ``logical_name`` defers to
  the logical name, matching the natural extractor convention.
  Both validator and materializer use this same precedence.
  """
  out = {}
  for prop in properties:
    out[prop.column] = prop
  for prop in properties:
    out[prop.logical_name] = prop
  return out


def build_name_to_column(properties):
  """Return ``{accepted_name: physical_column}`` for routing.

  Used by the materializer to translate an extracted property's
  ``name`` (which may be a logical name or a column name) into the
  physical column to write to the row dict. Same two-pass
  precedence as :func:`build_property_lookup` so the materializer
  routes whatever the validator accepted.
  """
  out = {}
  for prop in properties:
    out[prop.column] = prop.column
  for prop in properties:
    out[prop.logical_name] = prop.column
  return out


def parse_key_segment(node_id: str) -> dict[str, str]:
  """Parse the trailing ``k1=v1,k2=v2`` segment of a node ID.

  Node IDs follow the convention
  ``{session_id}:{entity_name}:{k1=v1,k2=v2}``. Returns a dict of
  the parsed key/value pairs, or an empty dict if the format
  doesn't match (e.g. an index-based fallback ID like ``d1``).

  The materializer uses this to populate edge FK columns from
  endpoint node-ids; the validator uses it to verify that those
  columns will actually be readable at materialize time.
  """
  parts = node_id.split(":")
  if len(parts) < 3:
    return {}
  key_segment = parts[-1]
  if "=" not in key_segment:
    return {}
  result: dict[str, str] = {}
  for pair in key_segment.split(","):
    if "=" in pair:
      k, v = pair.split("=", 1)
      result[k] = v
  return result
