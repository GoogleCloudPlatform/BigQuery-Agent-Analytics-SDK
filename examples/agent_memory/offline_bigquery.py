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

"""Offline stand-in for ``google.cloud.bigquery.Client``.

Lets the demo run the real ``bigquery_agent_analytics.Client.list_traces``
code path against the committed fixture, with no network or credentials.
It serves only the list-traces statement and only the ``TraceFilter``
predicates the demo uses (``user_id``, ``start_time`` and ``limit``). Any
other statement or predicate raises ``NotImplementedError`` instead of
silently returning rows a real query would have excluded.

Pass a real ``bigquery.Client`` (or nothing, to use ADC) to query a live
``agent_events`` table instead.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Optional

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "agent_events.json"
)

# The SDK's list-traces statement: a session-anchor CTE filtered by the
# TraceFilter predicates, then every row of the anchored sessions.
_LIST_TRACES_MARKER = "WITH trace_sessions AS"
_ANCHOR_WHERE = re.compile(
    r"JSON_TYPE\(attributes\), 'null'\) IN \('object', 'null'\)"
    r"\s+AND \((?P<where>.*?)\)\s+GROUP BY session_id",
    re.S,
)
_ROW_WHERE = re.compile(
    r"JSON_TYPE\(e\.attributes\), 'null'\) IN \('object', 'null'\)"
    r"\s+AND \((?P<where>.*?)\)\s+ORDER BY e\.session_id",
    re.S,
)
_SUPPORTED_CONDITIONS = frozenset(
    {"user_id = @user_id", "timestamp >= @start_time"}
)


@dataclasses.dataclass(frozen=True)
class Fixture:
  """Rows decoded the way the BigQuery client returns them."""

  rows: list[dict[str, Any]]
  now: datetime
  description: str


def parse_timestamp(value: str) -> datetime:
  """Parses an ISO-8601 UTC timestamp, accepting a trailing ``Z``."""
  if value.endswith("Z"):
    value = value[:-1] + "+00:00"
  return datetime.fromisoformat(value)


def load_fixture(path: Path = DEFAULT_FIXTURE) -> Fixture:
  """Loads fixture rows, turning timestamps into aware datetimes."""
  doc = json.loads(Path(path).read_text(encoding="utf-8"))
  rows = []
  for raw in doc["rows"]:
    row = dict(raw)
    row["timestamp"] = parse_timestamp(row["timestamp"])
    rows.append(row)
  return Fixture(
      rows=rows,
      now=parse_timestamp(doc["now"]),
      description=doc["description"],
  )


class _QueryJob:

  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self._rows = rows

  def result(self) -> list[dict[str, Any]]:
    return self._rows


def _root_agent(row: dict[str, Any]) -> Optional[str]:
  attributes = row.get("attributes")
  if isinstance(attributes, dict):
    return attributes.get("root_agent_name")
  return None


def _nullable_key(value: Any) -> tuple[bool, Any]:
  # BigQuery sorts NULLs first in ascending order.
  return (value is not None, "" if value is None else value)


class OfflineBigQueryClient:
  """Serves ``Client.list_traces`` from in-memory ``agent_events`` rows."""

  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self._rows = rows

  def query(self, sql: str, job_config: Any = None, **kwargs: Any) -> _QueryJob:
    if _LIST_TRACES_MARKER not in sql:
      raise NotImplementedError(
          "The offline fixture client only serves Client.list_traces."
      )
    conditions = self._anchor_conditions(sql)
    params = {}
    for param in getattr(job_config, "query_parameters", None) or []:
      params[param.name] = getattr(param, "value", None)

    # 1. Anchor sessions on rows that match the filter, newest first.
    last_seen: dict[tuple, datetime] = {}
    for row in self._rows:
      if not isinstance(row.get("attributes"), (dict, type(None))):
        continue
      if "user_id = @user_id" in conditions and (
          row["user_id"] != params["user_id"]
      ):
        continue
      if "timestamp >= @start_time" in conditions and (
          row["timestamp"] < params["start_time"]
      ):
        continue
      key = (row["session_id"], row["user_id"], _root_agent(row))
      if key not in last_seen or row["timestamp"] > last_seen[key]:
        last_seen[key] = row["timestamp"]
    anchors = sorted(last_seen, key=lambda k: tuple(map(_nullable_key, k)))
    anchors.sort(key=lambda k: last_seen[k], reverse=True)
    selected = set(anchors[: params["trace_limit"]])

    # 2. Return every row of the anchored sessions, as the SDK query does.
    out = []
    for row in self._rows:
      key = (row["session_id"], row["user_id"], _root_agent(row))
      if key not in selected:
        continue
      fetched = copy.deepcopy(row)
      fetched["anchor_user_id"] = row["user_id"]
      fetched["anchor_root_agent_name"] = _root_agent(row)
      fetched["attributes_type"] = (
          "object" if isinstance(row.get("attributes"), dict) else None
      )
      out.append(fetched)
    out.sort(
        key=lambda r: (
            r["session_id"],
            r["timestamp"],
            _nullable_key(r["span_id"]),
            _nullable_key(r["invocation_id"]),
            r["event_type"],
        )
    )
    return _QueryJob(out)

  @staticmethod
  def _anchor_conditions(sql: str) -> set[str]:
    anchor = _ANCHOR_WHERE.search(sql)
    rows = _ROW_WHERE.search(sql)
    if anchor is None or rows is None:
      raise NotImplementedError(
          "Unrecognized list-traces statement for the offline fixture."
      )
    if rows.group("where").strip() != "TRUE":
      raise NotImplementedError(
          "The offline fixture does not apply row-scope predicates"
          " (experiment, labels, import version)."
      )
    where = anchor.group("where").strip()
    conditions = set() if where == "TRUE" else set(where.split(" AND "))
    unsupported = conditions - _SUPPORTED_CONDITIONS
    if unsupported:
      raise NotImplementedError(
          "The offline fixture only honors TraceFilter(user_id=...,"
          f" start_time=..., limit=...); got {sorted(unsupported)}."
      )
    return conditions
