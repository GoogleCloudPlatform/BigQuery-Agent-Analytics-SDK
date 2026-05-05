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
must agree on three contracts:

1. **Property-name resolution.** An extracted property is matched
   against a spec property by either the property's
   ``logical_name`` (ontology-level) or its ``column`` (physical
   column from the binding). When a binding renames a logical name,
   logical-name lookup wins so a renamed property stays routable.

2. **Endpoint-key parsing from node IDs.** Edge endpoint FK columns
   are derived from the edge's ``from_node_id`` / ``to_node_id``
   string by splitting the trailing ``k1=v1,k2=v2`` segment.

3. **Property value normalization.** ``insert_rows_json`` /
   ``load_table_from_json`` only accept JSON-compatible values, but
   the validator legitimately accepts Python ``bytes``, ``date``,
   and tz-aware ``datetime`` for the corresponding SDK types. The
   shared :func:`normalize_property_value` converts those to
   JSON-compatible forms (ISO strings, base64) so a validator-clean
   value never crashes the materializer.

Putting these helpers in one module guarantees the two callers stay
in lockstep — earlier versions had subtle precedence and parsing
divergences that let validator-clean extractions silently corrupt at
INSERT time.

The module is private (leading underscore) because it carries no
public API surface; both caller modules treat it as internal plumbing.
"""

from __future__ import annotations

import base64
import datetime
import re
from typing import Any

# BigQuery JSON-input shape constraints. ``date.fromisoformat`` /
# ``datetime.fromisoformat`` are too permissive for our use: they
# accept compact and week-date forms like ``20260505``,
# ``2026-W19-2``, ``20260505T120000`` that BigQuery rejects in
# JSON inserts. The validator's contract is the BigQuery-accepted
# shape, so we gate on these regexes first and only reach
# ``fromisoformat`` for semantic checks (valid month/day/time).
#
# Fractional seconds are capped at 1-6 digits because BigQuery
# TIMESTAMP is microsecond precision — nanosecond strings like
# ``2026-05-05T12:00:00.123456789Z`` are rejected at INSERT time,
# so the validator's contract must reject them up front.
_BQ_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BQ_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?$"
)


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


def parse_iso_date(value: str) -> bool:
  """Return True if *value* is a BigQuery-acceptable ISO date string.

  Two-stage check: first gate on ``_BQ_DATE_RE`` to reject compact
  (``20260505``) and week-date (``2026-W19-2``) shapes that
  ``fromisoformat`` accepts but BigQuery JSON inserts reject. Then
  call ``date.fromisoformat`` for the semantic check (valid month
  and day) which also catches things like ``"2026-13-99"``.
  """
  if not isinstance(value, str):
    return False
  if not _BQ_DATE_RE.match(value):
    return False
  try:
    datetime.date.fromisoformat(value)
    return True
  except (ValueError, TypeError):
    return False


def parse_iso_datetime(value: str) -> bool:
  """Return True if *value* is a BigQuery-acceptable ISO datetime.

  Two-stage check matching :func:`parse_iso_date`: first gate on
  ``_BQ_DATETIME_RE`` to reject compact forms like
  ``20260505T120000`` that ``fromisoformat`` would otherwise
  accept. Then translate trailing ``Z`` (UTC) to ``+00:00`` —
  Python <3.11 doesn't accept ``Z`` natively — and call
  ``datetime.fromisoformat`` for the semantic check.
  """
  if not isinstance(value, str):
    return False
  if not _BQ_DATETIME_RE.match(value):
    return False
  candidate = value
  if candidate.endswith("Z"):
    candidate = candidate[:-1] + "+00:00"
  try:
    datetime.datetime.fromisoformat(candidate)
    return True
  except (ValueError, TypeError):
    return False


def normalize_property_value(value: Any, sdk_type: str) -> Any:
  """Coerce *value* to a JSON-compatible representation.

  ``insert_rows_json`` / ``load_table_from_json`` cannot serialize
  Python ``bytes`` / ``date`` / ``datetime`` objects. The validator
  legitimately accepts these for the corresponding SDK types, so
  the materializer must normalize before writing. Pass-through for
  everything else (including primitives the validator already
  accepts as-is).

  Conversions:
    * ``bytes`` / ``bytearray``      → base64-encoded ``str``
    * ``datetime.date`` (not datetime) → ``"YYYY-MM-DD"``
    * ``datetime.datetime``          → ISO-8601 ``str``

  Strings are passed through unchanged — if a caller already
  emitted an ISO string for a date/timestamp, the validator
  ensures it parses, and BigQuery handles the rest.
  """
  if isinstance(value, (bytes, bytearray)):
    return base64.b64encode(bytes(value)).decode("ascii")
  if isinstance(value, datetime.datetime):
    return value.isoformat()
  if isinstance(value, datetime.date):
    return value.isoformat()
  return value


def parse_key_segment(node_id: str) -> dict[str, str]:
  """Parse the trailing ``k1=v1,k2=v2`` segment of a node ID.

  Node IDs follow the convention
  ``{session_id}:{entity_name}:{k1=v1,k2=v2}``. Returns a dict of
  the parsed key/value pairs, or an empty dict if the format
  doesn't match (e.g. an index-based fallback ID like ``d1``).

  Splits on ``:`` *at most twice* so primary-key values that
  contain a literal ``:`` survive the parse: a node_id like
  ``sess1:Decision:decision_id=a:b`` parses to
  ``{"decision_id": "a:b"}``. Without the ``maxsplit=2`` cap, the
  trailing ``:b`` would be a separate ``parts`` element and the
  whole segment would fail the ``=`` check.

  The materializer uses this to populate edge FK columns from
  endpoint node-ids; the validator uses it to verify that those
  columns will actually be readable at materialize time.
  """
  parts = node_id.split(":", 2)
  if len(parts) < 3:
    return {}
  key_segment = parts[2]
  if "=" not in key_segment:
    return {}
  result: dict[str, str] = {}
  for pair in key_segment.split(","):
    if "=" in pair:
      k, v = pair.split("=", 1)
      result[k] = v
  return result
