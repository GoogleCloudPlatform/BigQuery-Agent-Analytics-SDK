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

"""Runs cross-event view SQL in SQLite, and loads the shared workflow fixture.

``execute_cross_event_view()`` runs the SELECT that ``ViewManager`` renders
for a cross-event view. Only BigQuery-specific spellings are rewritten: the
source table, ``SAFE_CAST``, ``SAFE.`` prefixes and raw string literals. The
BigQuery functions the views call become SQLite functions that return NULL
where BigQuery's SAFE forms do. Filtering, grouping, joins and projection
therefore run as SQL, not as a Python copy of the view.

``load_adk2_workflow_fixture()`` returns the representative ADK 2.0 workflow
fixture (#218) that every cross-event view test reads.
"""

import contextlib
import copy
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import ROUND_HALF_UP
import json
import math
import pathlib
import re
import sqlite3

ADK2_WORKFLOW_FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "adk2_workflow_events.json"
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Columns of the SQLite stand-in for the events table. An event dict may
# omit any of them; a missing or None value is stored as SQL NULL, and the
# JSON columns hold JSON text.
_COLUMNS = (
    "timestamp",
    "event_type",
    "agent",
    "user_id",
    "session_id",
    "invocation_id",
    "span_id",
    "content",
    "attributes",
)
_JSON_COLUMNS = frozenset({"content", "attributes"})

_SAFE_CAST = re.compile(r"SAFE_CAST\((.+?) AS (FLOAT64|INT64)\)")
_SAFE_CAST_SHIMS = {"FLOAT64": "SAFE_FLOAT", "INT64": "SAFE_INT"}
# r'...' after whitespace, "(" or ",": a BigQuery raw string literal.
_RAW_STRING = re.compile(r"(?<=[\s(,])r'([^'\\]*)'")


def load_adk2_workflow_fixture():
  """Returns a fresh copy of the parsed workflow fixture."""
  with ADK2_WORKFLOW_FIXTURE.open(encoding="utf-8") as f:
    return json.load(f)


def fixture_events(*row_ids):
  """Returns fixture events, all of them or those with the given row IDs.

  A row ID is the ``row-NN`` prefix of an event's ``_fixture_id``. Selected
  events come back in argument order, each a separate copy that a test may
  modify; a row ID may repeat.
  """
  events = load_adk2_workflow_fixture()["events"]
  if not row_ids:
    return events
  by_id = {event["_fixture_id"][: len("row-NN")]: event for event in events}
  return [copy.deepcopy(by_id[row_id]) for row_id in row_ids]


def _json_value(document, path):
  value = json.loads(document) if document is not None else None
  for key in path.removeprefix("$.").split("."):
    value = value.get(key) if isinstance(value, dict) else None
  if isinstance(value, (dict, list)) or value is None:
    return None
  return value if isinstance(value, str) else json.dumps(value)


def _safe_float(value):
  try:
    return float(value)
  except (ValueError, TypeError, OverflowError):
    return None


def _safe_int(value):
  if value is None or not math.isfinite(value):
    return None
  result = int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))
  return result if -(2**63) <= result < 2**63 else None


def _safe_multiply(x, y):
  return x * y if x is not None and y is not None else None


def _safe_timestamp_micros(value):
  if value is None:
    return None
  try:
    return (_EPOCH + timedelta(microseconds=value)).isoformat()
  except OverflowError:
    return None


def _strpos(value, substring):
  if value is None or substring is None:
    return None
  return value.find(substring) + 1


def _regexp_replace(value, pattern, replacement):
  if value is None or pattern is None or replacement is None:
    return None
  return re.sub(pattern, replacement, value)


def sqlite_query(vm, view_key):
  """Returns the SELECT of a cross-event view, rewritten for SQLite."""
  query = vm.get_view_sql(view_key).split(" AS\n", 1)[1]
  source = f"`{vm.project_id}.{vm.dataset_id}.{vm.table_id}`"
  query = query.replace(source, "agent_events")
  query = _SAFE_CAST.sub(
      lambda match: f"{_SAFE_CAST_SHIMS[match[2]]}({match[1]})", query
  )
  query = query.replace("SAFE.TIMESTAMP_MICROS", "SAFE_TIMESTAMP_MICROS")
  query = _RAW_STRING.sub(r"'\1'", query)
  # Fail loudly on SQL this harness cannot run, rather than test less.
  assert "SAFE_CAST" not in query and "`" not in query, query
  return query


def _row(event):
  values = []
  for column in _COLUMNS:
    value = event.get(column)
    if value is not None and column in _JSON_COLUMNS:
      value = json.dumps(value)
    values.append(value)
  return tuple(values)


def execute_cross_event_view(vm, view_key, events):
  """Runs one cross-event view over ``events``; returns its rows as dicts."""
  query = sqlite_query(vm, view_key)
  column_list = ", ".join(f"{column} TEXT" for column in _COLUMNS)
  placeholders = ", ".join("?" * len(_COLUMNS))
  with contextlib.closing(sqlite3.connect(":memory:")) as connection:
    connection.row_factory = sqlite3.Row
    connection.create_function("JSON_VALUE", 2, _json_value)
    connection.create_function("SAFE_FLOAT", 1, _safe_float)
    connection.create_function("SAFE_INT", 1, _safe_int)
    connection.create_function("SAFE_MULTIPLY", 2, _safe_multiply)
    connection.create_function(
        "SAFE_TIMESTAMP_MICROS", 1, _safe_timestamp_micros
    )
    connection.create_function("STRPOS", 2, _strpos)
    connection.create_function("REGEXP_REPLACE", 3, _regexp_replace)
    connection.execute(f"CREATE TABLE agent_events ({column_list})")
    connection.executemany(
        f"INSERT INTO agent_events VALUES ({placeholders})",
        [_row(event) for event in events],
    )
    return [dict(row) for row in connection.execute(query)]
