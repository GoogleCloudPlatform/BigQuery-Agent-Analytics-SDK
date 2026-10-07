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
It implements exactly one statement: the SDK's list-traces query, pinned
below, with only the ``TraceFilter`` predicates the demo uses (``user_id``,
``start_time`` and ``limit``). The whole statement and its parameters must
match; any other statement, shape (join, projection, ordering, limit),
predicate or parameter raises ``NotImplementedError`` instead of silently
answering with rows a real query would not return.

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

# The one statement this stand-in implements: the SDK's list-traces query
# (``bigquery_agent_analytics.client._LIST_TRACES_QUERY`` in SDK 0.5.4) with
# its placeholders. It anchors sessions on rows matching the TraceFilter
# predicates, then returns every row of the anchored sessions.
_LIST_TRACES_STATEMENT = """\
WITH trace_sessions AS (
  SELECT
    session_id,
    user_id,
    JSON_VALUE(attributes, '$.root_agent_name') AS root_agent_name,
    MAX(timestamp) AS last_event_ts
  FROM `{project}.{dataset}.{table}`
  WHERE COALESCE(JSON_TYPE(attributes), 'null') IN ('object', 'null')
    AND ({where})
  GROUP BY session_id, user_id, root_agent_name
  ORDER BY last_event_ts DESC, session_id, user_id, root_agent_name
  LIMIT @trace_limit
)
SELECT
  e.event_type,
  e.agent,
  e.timestamp,
  e.session_id,
  e.invocation_id,
  e.user_id,
  e.trace_id,
  e.span_id,
  e.parent_span_id,
  e.content,
  e.content_parts,
  e.attributes,
  e.latency_ms,
  e.status,
  e.error_message,
  e.is_truncated,
  ts.user_id AS anchor_user_id,
  ts.root_agent_name AS anchor_root_agent_name,
  JSON_TYPE(e.attributes) AS attributes_type
FROM `{project}.{dataset}.{table}` e
JOIN trace_sessions ts
  ON e.session_id = ts.session_id
  AND e.user_id IS NOT DISTINCT FROM ts.user_id
  AND JSON_VALUE(e.attributes, '$.root_agent_name')
      IS NOT DISTINCT FROM ts.root_agent_name
WHERE COALESCE(JSON_TYPE(e.attributes), 'null') IN ('object', 'null')
  AND ({row_where})
ORDER BY e.session_id, e.timestamp ASC, e.span_id, e.invocation_id,
  e.event_type
"""
_SUPPORTED_CONDITIONS = {
    "user_id = @user_id": "user_id",
    "timestamp >= @start_time": "start_time",
}


def _statement_pattern(template: str) -> re.Pattern[str]:
  """A full-match pattern for ``template`` with its placeholders filled in."""
  pattern = []
  table_seen = False
  for piece in re.split(
      r"(\{project\}\.\{dataset\}\.\{table\}|\{where\}|\{row_where\})",
      template,
  ):
    if piece == "{project}.{dataset}.{table}":
      # Both FROM clauses must name the same table.
      pattern.append("(?P=table)" if table_seen else "(?P<table>[^`]+)")
      table_seen = True
    elif piece == "{where}":
      pattern.append("(?P<where>.*?)")
    elif piece == "{row_where}":
      pattern.append("(?P<row_where>.*?)")
    else:
      pattern.append(re.escape(piece))
  return re.compile("".join(pattern), re.S)


_STATEMENT = _statement_pattern(_LIST_TRACES_STATEMENT)


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
    match = _STATEMENT.fullmatch(sql)
    if match is None:
      if not sql.lstrip().startswith("WITH trace_sessions AS"):
        raise NotImplementedError(
            "The offline fixture client only serves the Client.list_traces"
            " statement."
        )
      raise NotImplementedError(
          "This list-traces statement differs from the one the offline"
          " fixture implements (join, projection, ordering or limit), so"
          " its rows cannot be computed here."
      )
    conditions = self._conditions(
        match.group("where"), match.group("row_where")
    )
    params = self._parameters(job_config, conditions)

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
  def _conditions(where: str, row_where: str) -> set[str]:
    if row_where.strip() != "TRUE":
      raise NotImplementedError(
          "The offline fixture does not apply row-scope predicates"
          " (experiment, labels, import version)."
      )
    where = where.strip()
    conditions = set() if where == "TRUE" else set(where.split(" AND "))
    unsupported = conditions - set(_SUPPORTED_CONDITIONS)
    if unsupported:
      raise NotImplementedError(
          "The offline fixture only honors TraceFilter(user_id=...,"
          f" start_time=..., limit=...); got {sorted(unsupported)}."
      )
    return conditions

  @staticmethod
  def _parameters(job_config: Any, conditions: set[str]) -> dict[str, Any]:
    params = {
        param.name: getattr(param, "value", None)
        for param in getattr(job_config, "query_parameters", None) or []
    }
    expected = {"trace_limit"} | {_SUPPORTED_CONDITIONS[c] for c in conditions}
    if set(params) != expected or not isinstance(params["trace_limit"], int):
      raise NotImplementedError(
          f"The offline fixture expects query parameters {sorted(expected)};"
          f" got {sorted(params)}."
      )
    return params
