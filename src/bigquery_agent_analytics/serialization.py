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

"""Uniform JSON serialization for CLI and Remote Function boundaries.

Provides a single ``serialize()`` function that converts any SDK return
type into a structure safe for ``json.dumps()``.  Handles three
categories:

* **Pydantic BaseModel** -- uses ``.model_dump(mode="json")`` which
  converts ``datetime`` fields to ISO 8601 strings.
* **dataclass instances** (``Trace``, ``Span``, etc.) -- recursively
  converts to dicts with ``datetime`` → ``isoformat()``.
* **plain dicts / lists / primitives** -- pass through with datetime
  and enum conversion.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from datetime import datetime
from enum import Enum
from typing import Any

# Module-level on purpose: serialize() visits every value recursively,
# and per-call relative imports measurably slow large payloads. trace
# does not import this module, so there is no cycle.
from .trace import _pin_sentinel_name
from .trace import _PIN_WIRE_KEY
from .trace import SQL_NULL
from .trace import UNSET


def serialize(obj: Any) -> Any:
  """Convert any SDK return type to a ``json.dumps()``-safe value.

  Args:
      obj: An SDK return value -- ``Trace``, ``EvaluationReport``,
        ``InsightsReport``, ``DriftReport``, plain ``dict``, etc.

  Returns:
      A JSON-safe Python object (dict, list, str, int, float, bool,
      or None).
  """
  if obj is None:
    return None
  if obj is UNSET or obj is SQL_NULL:
    # Tagged wire encoding (decode with trace.decode_pin): plain JSON
    # null cannot carry SQL_NULL because None already means
    # "unfiltered" on TraceFilter. UNSET only reaches here outside a
    # dataclass field (those are omitted below). Identity comparison,
    # not isinstance: a spoofed __class__ property must not divert a
    # normal dataclass into the sentinel branch.
    return {_PIN_WIRE_KEY: _pin_sentinel_name(obj)}
  if hasattr(obj, "model_dump"):
    return obj.model_dump(mode="json")
  if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
    return _dataclass_to_dict(obj)
  if isinstance(obj, dict):
    return {k: serialize(v) for k, v in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [serialize(item) for item in obj]
  if isinstance(obj, Enum):
    return obj.value
  if isinstance(obj, (datetime, date)):
    return obj.isoformat()
  return obj


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
  """Recursively convert a dataclass instance to a dict.

  Pin wire contract (issue #359): fields holding the
  :data:`~bigquery_agent_analytics.trace.UNSET` pin sentinel are
  omitted entirely — JSON has no third state, and encoding UNSET as
  ``null`` would reconstruct as an explicit pin-to-SQL-NULL. Omission
  round-trips: absent keyword arguments restore the UNSET default.
  On ``TraceSelector``, explicit NULL pins serialize as ``null`` and
  reconstruct unchanged. On ``TraceFilter``, a
  :data:`~bigquery_agent_analytics.trace.SQL_NULL` pin serializes as
  the tagged object ``{"$pin": "SQL_NULL"}`` (``null`` already means
  "unfiltered" there) and must be passed through
  :func:`~bigquery_agent_analytics.trace.decode_pin` before
  reconstruction.
  """
  result: dict[str, Any] = {}
  for f in dataclasses.fields(obj):
    value = getattr(obj, f.name)
    if value is UNSET:
      continue
    result[f.name] = serialize(value)
  return result
