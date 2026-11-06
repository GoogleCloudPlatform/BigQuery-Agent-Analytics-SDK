# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression tests for the Grafana efficiency-trial dashboard.

scripts/check_grafana_queries_sync.py checks only bqaa-dashboard.json and the
public demo, so these tests pin the trial's own contract:

1. Filter values are interpolated as JSON arrays (``ARRAY<STRING>${var:json}``)
   that BigQuery reads as data, including apostrophes, quotes, backslashes and
   a value shaped to break out of the array.
2. Token sums read ``content.usage`` and fall back to
   ``attributes.usage_metadata``, the direct logger's shape, counting a row
   that carries both shapes once.
3. Panels 12 and 20 reuse the merged LLM calls query through the Dashboard
   data source, and every BigQuery query stays time-bounded.
4. Every panel and variable query runs, as Grafana would send it, in SQLite
   against the SDK's own view definitions, and the LLM calls query returns the
   expected call count, estimated cost and total tokens for fixture rows.
"""

import contextlib
import datetime
import itertools
import json
import pathlib
import re
import sqlite3
from urllib import parse

import pytest

from bigquery_agent_analytics.seed_events import _EVENT_SCHEMA_FIELDS
from bigquery_agent_analytics.views import ViewManager

ROOT = pathlib.Path(__file__).resolve().parents[1]
TRIAL_PATH = (
    ROOT / "dashboards" / "grafana" / "bqaa-dashboard-efficiency-trial.json"
)
FILTER_VARIABLES = ("agent", "user_id", "event_type", "session_id")
ALL_VALUE = '["___ALL___"]'
DASHBOARD_DATASOURCE_UID = "-- Dashboard --"
# Panels that reuse another panel's result, mapped to their source panel.
DASHBOARD_DATA_SOURCES = {3: 2, 4: 2, 5: 2, 12: 23, 20: 23}
VIEW_USAGE_PANELS = (9, 11, 23)
LOGGER_USAGE = {
    "prompt_tokens": 1_000_000,
    "completion_tokens": 500_000,
    "total_tokens": 1_500_000,
}
PLUGIN_USAGE = {
    "prompt_token_count": 1_000_000,
    "candidates_token_count": 500_000,
    "total_token_count": 1_500_000,
}


def _walk(panels):
  for panel in panels:
    yield panel
    yield from _walk(panel.get("panels", []))


DASHBOARD = json.loads(TRIAL_PATH.read_text(encoding="utf-8"))
PANELS = {panel["id"]: panel for panel in _walk(DASHBOARD["panels"])}
VARIABLES = {v["name"]: v for v in DASHBOARD["templating"]["list"]}
PANEL_SQL = [
    (panel_id, target["rawSql"])
    for panel_id, panel in PANELS.items()
    for target in panel.get("targets", [])
    if target.get("rawSql")
]


def _sql(panel_id):
  (target,) = PANELS[panel_id]["targets"]
  return target["rawSql"]


def _strip_comments(sql):
  return re.sub(r"--[^\n]*", "", sql)


# --------------------------------------------------------------------------- #
# P1-1: filter values are JSON-array data, not SQL text                       #
# --------------------------------------------------------------------------- #

_JSON_FILTER = re.compile(r"\$\{(?:agent|user_id|event_type|session_id):json\}")
_FILTER_REFERENCE = re.compile(
    r"\$\{(agent|user_id|event_type|session_id)(:\w+)?\}"
)
_FILTER_PAIR = re.compile(
    r"'___ALL___' IN UNNEST\(ARRAY<STRING>\$\{(\w+):json\}\)"
    r" OR (?:\w+\.)?(\w+) IN UNNEST\(ARRAY<STRING>\$\{(\w+):json\}\)"
)
_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "?": "?",
    '"': '"',
    "'": "'",
    "`": "`",
}
_FILTER_CASES = {
    "all": None,
    "empty": [],
    "single": ["checkout-agent"],
    "multiple": ["checkout-agent", "returns-agent"],
    "apostrophe": ["O'Brien"],
    "double_quote": ['say "hi"'],
    "backslash": ["C:\\agents\\returns"],
    "breakout": ["x\\'])) OR TRUE --"],
    "control_characters": ["line\nbreak\ttab"],
    "unicode": ["agent-ü-✓"],
}


def _grafana_json(values):
  """Formats a multi-value selection like Grafana's ``json`` formatter."""
  return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _grafana_sqlstring(values):
  """Formats values like Grafana's ``sqlstring`` formatter, the old encoding."""
  return ",".join("'" + value.replace("'", "''") + "'" for value in values)


def _read_string(text, start):
  """Reads the BigQuery quoted string literal that starts at ``text[start]``.

  Returns the decoded value and the index just past the closing quote.
  """
  quote = text[start]
  if quote not in ("'", '"'):
    raise ValueError(f"expected a string literal at {start}")
  chars, index = [], start + 1
  while True:
    char = text[index]
    if char == quote:
      return "".join(chars), index + 1
    if char == "\n":
      raise ValueError("unescaped newline in a string literal")
    if char != "\\":
      chars.append(char)
      index += 1
      continue
    escape = text[index + 1]
    if escape in _SIMPLE_ESCAPES:
      chars.append(_SIMPLE_ESCAPES[escape])
      index += 2
    elif escape in "xX":
      chars.append(chr(int(text[index + 2 : index + 4], 16)))
      index += 4
    elif escape == "u":
      chars.append(chr(int(text[index + 2 : index + 6], 16)))
      index += 6
    elif escape == "U":
      chars.append(chr(int(text[index + 2 : index + 10], 16)))
      index += 10
    elif escape in "01234567":
      chars.append(chr(int(text[index + 1 : index + 4], 8)))
      index += 4
    else:
      raise ValueError(f"invalid escape \\{escape}")


def _read_string_array(text, start):
  """Reads the BigQuery array of string literals at ``text[start]``."""
  if text[start] != "[":
    raise ValueError(f"expected '[' at {start}")
  values, index = [], start + 1
  if text[index] == "]":
    return values, index + 1
  while True:
    value, index = _read_string(text, index)
    values.append(value)
    if text[index] == "]":
      return values, index + 1
    if text[index] != ",":
      raise ValueError(f"unexpected {text[index]!r} after a string literal")
    index += 1


def _interpolate(sql, rendered):
  """Substitutes ``rendered`` for every filter variable, as Grafana would.

  Returns the SQL and the offset at which each substituted literal starts.
  """
  pieces, offsets, cursor, length = [], [], 0, 0
  for match in _JSON_FILTER.finditer(sql):
    prefix = sql[cursor : match.start()]
    pieces.append(prefix)
    length += len(prefix)
    offsets.append(length)
    pieces.append(rendered)
    length += len(rendered)
    cursor = match.end()
  pieces.append(sql[cursor:])
  return "".join(pieces), offsets


def test_no_sqlstring_interpolation_remains():
  assert ":sqlstring" not in TRIAL_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILTER_VARIABLES)
def test_filter_variables_use_a_json_all_value(name):
  variable = VARIABLES[name]
  assert variable["multi"] is True
  assert variable["includeAll"] is True
  assert variable["allValue"] == ALL_VALUE
  assert variable["current"]["value"] == ["$__all"]


def test_every_filter_pairs_the_sentinel_with_a_json_array():
  for panel_id, sql in PANEL_SQL:
    references = _FILTER_REFERENCE.findall(sql)
    assert all(fmt == ":json" for _, fmt in references), panel_id
    pairs = _FILTER_PAIR.findall(sql)
    assert len(pairs) * 2 == len(references), panel_id
    for sentinel_variable, column, value_variable in pairs:
      assert sentinel_variable == column == value_variable, panel_id


@pytest.mark.parametrize("case", sorted(_FILTER_CASES))
def test_filter_values_stay_bigquery_string_data(case):
  values = _FILTER_CASES[case]
  rendered = ALL_VALUE if values is None else _grafana_json(values)
  expected = ["___ALL___"] if values is None else values
  checked = 0
  for panel_id, sql in PANEL_SQL:
    text, offsets = _interpolate(sql, rendered)
    for offset in offsets:
      decoded, end = _read_string_array(text, offset)
      assert decoded == expected, (panel_id, case)
      # The literal ends exactly where the substitution ends, so no part of
      # the value is parsed as SQL.
      assert end == offset + len(rendered), (panel_id, case)
      assert text[end] == ")", (panel_id, case)
      checked += 1
  assert checked == 104


@pytest.mark.parametrize("value", ["O'Brien", "x\\'])) OR TRUE --"])
def test_old_sqlstring_encoding_does_not_stay_data(value):
  rendered = "[" + _grafana_sqlstring([value]) + "]"
  text = f"agent IN UNNEST(ARRAY<STRING>{rendered})"
  start = text.index("[")
  try:
    decoded, end = _read_string_array(text, start)
  except ValueError:
    return  # BigQuery rejects the query.
  assert (decoded, end) != ([value], start + len(rendered))


# --------------------------------------------------------------------------- #
# P1-2: token sums read both usage shapes and count each row once             #
# --------------------------------------------------------------------------- #

_VIEW_FALLBACK = re.compile(
    r"COALESCE\(usage_(prompt|completion|total)_tokens,\s*"
    r"SAFE_CAST\(JSON_VALUE\(usage_metadata, '\$\.(\w+)'\) AS INT64\)\)"
)
_RAW_FALLBACK = re.compile(
    r"COALESCE\(SAFE_CAST\(JSON_VALUE\(content, '\$\.usage\.(\w+)'\) AS"
    r" INT64\),\s*SAFE_CAST\(JSON_VALUE\(attributes,"
    r" '\$\.usage_metadata\.(\w+)'\) AS INT64\)\)"
)


@pytest.mark.parametrize("panel_id", VIEW_USAGE_PANELS)
def test_view_token_sums_fall_back_to_usage_metadata(panel_id):
  sql = _strip_comments(_sql(panel_id))
  fallbacks = _VIEW_FALLBACK.findall(sql)
  assert {field for field, _ in fallbacks} == {"prompt", "completion", "total"}
  assert all(key == f"{field}_tokens" for field, key in fallbacks)
  # Every read of a usage column is the first argument of a fallback.
  reads = re.findall(r"\busage_(?:prompt|completion|total)_tokens\b", sql)
  assert len(reads) == len(fallbacks)


def test_session_token_sums_fall_back_to_usage_metadata():
  sql = _strip_comments(_sql(18))
  fallbacks = _RAW_FALLBACK.findall(sql)
  assert sorted(fallbacks) == [
      ("completion", "completion_tokens"),
      ("prompt", "prompt_tokens"),
  ]
  assert len(re.findall(r"'\$\.usage\.", sql)) == len(fallbacks)


# --------------------------------------------------------------------------- #
# Full queries run in SQLite against the SDK's own views                      #
# --------------------------------------------------------------------------- #
#
# Each query is rendered the way Grafana sends it: the constants, the BigQuery
# plugin's $__timeFilter and $__timeGroup expansions, and the filter values as
# BigQuery decodes the JSON arrays. It then runs in SQLite over the
# agent_events columns seed_events creates and the views ViewManager defines,
# so the dashboard's own arithmetic, COALESCE order, filters and HAVING clauses
# are what gets evaluated. The harness only bridges the dialect: the
# _SCALAR_FUNCTIONS and _AGGREGATE_FUNCTIONS stand-ins implement the BigQuery
# functions SQLite lacks, and _REWRITES turns the few constructs that take
# keywords as arguments into calls. A misspelled keyword, column, view or
# function, or a wrong argument count, fails the query.

_CONSTANTS = {
    name: VARIABLES[name]["query"]
    for name in (
        "project",
        "dataset",
        "table",
        "view_prefix",
        "price_per_1m_input_tokens",
        "price_per_1m_output_tokens",
    )
}
ALL_QUERIES = {f"panel-{panel_id}": sql for panel_id, sql in PANEL_SQL}
ALL_QUERIES.update(
    (f"variable-{name}", variable["query"]["rawSql"])
    for name, variable in VARIABLES.items()
    if variable.get("type") == "query"
)
# Timestamps are stored and compared as fixed-width RFC 3339 text, which sorts
# in time order, so the plugin's BETWEEN on string bounds works unchanged.
_TIME_FROM = "2026-01-01T00:00:00Z"
_TIME_TO = "2026-01-02T00:00:00Z"
_RFC3339 = "%Y-%m-%dT%H:%M:%SZ"
_EPOCH = datetime.datetime(1970, 1, 1)
_BUCKET_MS = 3_600_000
_TABLE_REFERENCE = (
    "`"
    + re.escape(f"{_CONSTANTS['project']}.{_CONSTANTS['dataset']}.")
    + r"(\w+)`"
)
_REWRITES = (
    # The Grafana BigQuery plugin's macro expansions, for a 1h $__interval.
    (
        r"\$__timeFilter\(([\w.]+)\)",
        rf"\1 BETWEEN '{_TIME_FROM}' AND '{_TIME_TO}'",
    ),
    (
        r"\$__timeGroup\((\w+), \$__interval\)",
        rf"TIMESTAMP_MILLIS(DIV(UNIX_MILLIS(\1), {_BUCKET_MS}) * {_BUCKET_MS})",
    ),
    # Dialect: bare table names, and calls in place of the constructs that
    # take keywords as arguments.
    (_TABLE_REFERENCE, r"\1"),
    (
        r"SAFE_CAST\((JSON_VALUE\(\w+, '[^']*'\)) AS INT64\)",
        r"SAFE_CAST_INT64(\1)",
    ),
    (
        r"APPROX_QUANTILES\((\w+), (\d+)\)\[OFFSET\((\d+)\)\]",
        r"APPROX_QUANTILE(\1, \2, \3)",
    ),
    (
        r"STRING_AGG\(DISTINCT (\w+), ('[^']*') ORDER BY \1\)",
        r"STRING_AGG_DISTINCT(\1, \2)",
    ),
    (r"(TIMESTAMP_DIFF\(.*), SECOND\)", r"\1, 'SECOND')"),
)
_UNTRANSLATED = (
    "${",
    "$__",
    "`",
    "ARRAY<",
    "UNNEST(",
    "SAFE_CAST(",
    "OFFSET(",
    "STRING_AGG(",
    " SECOND)",
)
_BARE_HAVING = "HAVING COUNT(*) > 0"


def _group_bare_having(sql):
  """Gives a HAVING without GROUP BY the explicit group older SQLite needs.

  SQLite before 3.39, such as the 3.37 on Ubuntu 22.04 runners, rejects HAVING
  without GROUP BY. GROUP BY NULL forms one group over a non-empty input and
  none over an empty one, the same rows HAVING COUNT(*) > 0 keeps.
  """
  if "HAVING" not in sql or "GROUP BY" in sql:
    return sql
  assert sql.count("HAVING") == sql.count(_BARE_HAVING) == 1, sql
  return sql.replace(_BARE_HAVING, "GROUP BY NULL " + _BARE_HAVING)


def _sqlite_literal(value):
  return "'" + value.replace("'", "''") + "'"


def _sqlite_query(sql, selections):
  """Renders ``sql`` as Grafana would and maps its dialect onto SQLite."""
  assert set(selections) <= set(FILTER_VARIABLES), selections
  sql = _strip_comments(sql)
  for name, value in _CONSTANTS.items():
    sql = sql.replace(f"${{{name}}}", value)
  for pattern, replacement in _REWRITES:
    sql = re.sub(pattern, replacement, sql)
  sql = _group_bare_having(sql)
  # Filter values go in last, so none of the rewrites above can touch them.
  for name in FILTER_VARIABLES:
    values = selections.get(name)
    rendered = ALL_VALUE if values is None else _grafana_json(values)
    decoded, end = _read_string_array(rendered, 0)  # As BigQuery reads it.
    assert end == len(rendered), rendered
    sql = sql.replace(
        f"UNNEST(ARRAY<STRING>${{{name}:json}})",
        "(" + ", ".join(map(_sqlite_literal, decoded)) + ")",
    )
  for construct in _UNTRANSLATED:
    assert construct not in sql, (construct, sql)
  return sql


def _json_path(document, path):
  value = None if document is None else json.loads(document)
  for key in path.removeprefix("$.").split("."):
    value = value.get(key) if isinstance(value, dict) else None
  return value


def _json_value(document, path):
  """JSON_VALUE: the scalar at ``path`` as a STRING, else NULL."""
  value = _json_path(document, path)
  if value is None or isinstance(value, (dict, list)):
    return None
  return value if isinstance(value, str) else json.dumps(value)


def _json_query(document, path):
  """JSON_QUERY: the JSON text at ``path``."""
  value = _json_path(document, path)
  return None if value is None else json.dumps(value)


def _safe_cast_int64(value):
  """SAFE_CAST(<STRING> AS INT64): NULL unless an in-range integer literal."""
  if value is None or not re.fullmatch(r"[+-]?\d+", str(value)):
    return None
  number = int(value)
  return number if -(2**63) <= number < 2**63 else None


def _div(dividend, divisor):
  """DIV: integer division, rounding toward zero."""
  if dividend is None or divisor is None:
    return None
  quotient = abs(dividend) // abs(divisor)
  return quotient if (dividend < 0) == (divisor < 0) else -quotient


def _unix_millis(timestamp):
  moment = datetime.datetime.strptime(timestamp, _RFC3339)
  return (moment - _EPOCH) // datetime.timedelta(milliseconds=1)


def _timestamp_millis(millis):
  moment = _EPOCH + datetime.timedelta(milliseconds=millis)
  return moment.strftime(_RFC3339)


def _timestamp_diff(end, start, part):
  assert part == "SECOND", part
  end = datetime.datetime.strptime(end, _RFC3339)
  start = datetime.datetime.strptime(start, _RFC3339)
  return int((end - start).total_seconds())


class _CountIf:
  """COUNTIF."""

  def __init__(self):
    self.count = 0

  def step(self, condition):
    self.count += bool(condition)

  def finalize(self):
    return self.count


class _LogicalOr:
  """LOGICAL_OR: NULL when every input is NULL."""

  def __init__(self):
    self.result = None

  def step(self, condition):
    if condition is not None:
      self.result = bool(self.result or condition)

  def finalize(self):
    return self.result


class _ApproxQuantile:
  """APPROX_QUANTILES(value, buckets)[OFFSET(offset)], computed exactly."""

  def __init__(self):
    self.values = []
    self.fraction = 0

  def step(self, value, buckets, offset):
    self.fraction = offset / buckets
    if value is not None:
      self.values.append(value)

  def finalize(self):
    if not self.values:
      return None
    ordered = sorted(self.values)
    return ordered[round(self.fraction * (len(ordered) - 1))]


class _StringAggDistinct:
  """STRING_AGG(DISTINCT value, separator ORDER BY value)."""

  def __init__(self):
    self.values = set()
    self.separator = ""

  def step(self, value, separator):
    self.separator = separator
    if value is not None:
      self.values.add(value)

  def finalize(self):
    if not self.values:
      return None
    return self.separator.join(sorted(self.values))


_SCALAR_FUNCTIONS = {
    "CONCAT": (-1, lambda *parts: None if None in parts else "".join(parts)),
    "DIV": (2, _div),
    "ENDS_WITH": (
        2,
        lambda text, suffix: (
            None if None in (text, suffix) else text.endswith(suffix)
        ),
    ),
    "IF": (
        3,
        lambda condition, then, otherwise: then if condition else otherwise,
    ),
    "JSON_QUERY": (2, _json_query),
    "JSON_VALUE": (2, _json_value),
    "SAFE_CAST_INT64": (1, _safe_cast_int64),
    "SAFE_DIVIDE": (2, lambda x, y: None if x is None or not y else x / y),
    "TIMESTAMP_DIFF": (3, _timestamp_diff),
    "TIMESTAMP_MILLIS": (1, _timestamp_millis),
    "UNIX_MILLIS": (1, _unix_millis),
}
_AGGREGATE_FUNCTIONS = {
    "APPROX_QUANTILE": (3, _ApproxQuantile),
    "COUNTIF": (1, _CountIf),
    "LOGICAL_OR": (1, _LogicalOr),
    "STRING_AGG_DISTINCT": (2, _StringAggDistinct),
}
_EVENT_COLUMNS = [name for name, _, _ in _EVENT_SCHEMA_FIELDS]
_VIEW_MANAGER = ViewManager(
    project_id=_CONSTANTS["project"],
    dataset_id=_CONSTANTS["dataset"],
    table_id=_CONSTANTS["table"],
    view_prefix=_CONSTANTS["view_prefix"],
)
_EVENT_TYPE_BY_VIEW = {
    _VIEW_MANAGER.get_view_name(event_type): event_type
    for event_type in _VIEW_MANAGER.available_event_types
}
_TRIAL_VIEWS = sorted(
    {
        _CONSTANTS["view_prefix"] + suffix
        for sql in ALL_QUERIES.values()
        for suffix in re.findall(
            r"\$\{view_prefix\}(\w+)", _strip_comments(sql)
        )
    }
)


def _connect():
  """Opens an in-memory agent_events table with the trial's views over it."""
  connection = sqlite3.connect(":memory:")
  # Lets the views call the stand-ins even where SQLite was built with
  # SQLITE_TRUSTED_SCHEMA=0.
  connection.execute("PRAGMA trusted_schema = ON")
  for name, (arity, function) in _SCALAR_FUNCTIONS.items():
    connection.create_function(name, arity, function)
  for name, (arity, aggregate) in _AGGREGATE_FUNCTIONS.items():
    connection.create_aggregate(name, arity, aggregate)
  connection.execute(
      f"CREATE TABLE {_CONSTANTS['table']} ({', '.join(_EVENT_COLUMNS)})"
  )
  for view in _TRIAL_VIEWS:
    ddl = _VIEW_MANAGER.get_view_sql(_EVENT_TYPE_BY_VIEW[view])
    ddl = re.sub(_TABLE_REFERENCE, r"\1", ddl)
    connection.execute(ddl.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))
  return connection


def _run(sql, events=(), **selections):
  """Runs ``sql`` over ``events`` and returns its column names and rows."""
  with contextlib.closing(_connect()) as connection:
    rows = []
    for event in events:
      assert set(event) <= set(_EVENT_COLUMNS), event
      rows.append(
          [
              json.dumps(value) if isinstance(value, dict) else value
              for value in map(event.get, _EVENT_COLUMNS)
          ]
      )
    placeholders = ", ".join("?" * len(_EVENT_COLUMNS))
    connection.executemany(
        f"INSERT INTO {_CONSTANTS['table']} VALUES ({placeholders})", rows
    )
    cursor = connection.execute(_sqlite_query(sql, selections))
    return [column[0] for column in cursor.description], cursor.fetchall()


_SPAN_IDS = itertools.count()
_CONTENT_USAGE = {
    "prompt": 1_000_000,
    "completion": 500_000,
    "total": 1_500_000,
}
_RESPONSES = {
    # BigQueryAgentAnalyticsLogger.log_llm_response writes no content.usage.
    "direct_logger": {
        "content": {"response": "ok"},
        "attributes": {
            "model": "gemini-2.5-flash",
            "usage_metadata": LOGGER_USAGE,
        },
    },
    # The shape the views were written for, as seed_events writes it.
    "plugin": {
        "content": {"response": "ok", "usage": _CONTENT_USAGE},
        "attributes": {
            "model_version": "gemini-2.5-flash",
            "usage_metadata": PLUGIN_USAGE,
        },
    },
    # Both shapes with different numbers: content.usage wins, counted once.
    "both_shapes": {
        "content": {"response": "ok", "usage": _CONTENT_USAGE},
        "attributes": {
            "usage_metadata": {
                "prompt_tokens": 7,
                "completion_tokens": 7,
                "total_tokens": 14,
            },
        },
    },
}
_HEADLINE_COLUMNS = ["llm_calls", "estimated_cost_usd", "total_tokens"]


def _event(event_type="LLM_RESPONSE", **columns):
  """Builds one agent_events row; ``columns`` override the defaults."""
  return {
      "timestamp": "2026-01-01T01:00:00Z",
      "event_type": event_type,
      "agent": "checkout-agent",
      "session_id": "session-1",
      "invocation_id": "invocation-1",
      "user_id": "user-1",
      "trace_id": "trace-1",
      "span_id": f"span-{next(_SPAN_IDS)}",
      "status": "OK",
      **columns,
  }


def _response(shape, **columns):
  return _event(**_RESPONSES[shape], **columns)


# One event of each kind the panels read, so every query has rows to return.
_SAMPLE_EVENTS = [
    _event("LLM_REQUEST", attributes={"model": "gemini-2.5-flash"}),
    _response(
        "plugin",
        latency_ms={"total_ms": 900, "time_to_first_token_ms": 250},
    ),
    _event("TOOL_STARTING", content={"tool": "lookup_order", "args": {}}),
    _event(
        "TOOL_COMPLETED",
        content={"tool": "lookup_order", "result": {}},
        latency_ms={"total_ms": 120},
    ),
    _event(
        "TOOL_ERROR",
        content={"tool": "issue_refund", "args": {}},
        status="ERROR",
        error_message="refund service timed out",
    ),
]


def test_default_token_rates():
  # The cost expectations below are priced at these defaults.
  assert _CONSTANTS["price_per_1m_input_tokens"] == "1.25"
  assert _CONSTANTS["price_per_1m_output_tokens"] == "5.00"


@pytest.mark.parametrize("shape", sorted(_RESPONSES))
def test_headline_returns_calls_cost_and_tokens(shape):
  # 1M input tokens at $1.25 plus 0.5M output tokens at $5.00 per 1M.
  assert _run(_sql(23), [_response(shape)]) == (
      _HEADLINE_COLUMNS,
      [(1, pytest.approx(3.75), 1_500_000)],
  )


def test_headline_adds_up_mixed_producers():
  events = [_response(shape) for shape in sorted(_RESPONSES)]
  assert _run(_sql(23), events)[1] == [(3, pytest.approx(11.25), 4_500_000)]


def test_headline_counts_calls_per_span():
  # Two chunks of one streamed call share a span. A row without a span key
  # counts as a call of its own.
  events = [
      _response("direct_logger", span_id="span-streamed"),
      _response("direct_logger", span_id="span-streamed"),
      _response("direct_logger", span_id=None),
      _response("direct_logger", trace_id=None),
  ]
  ((calls, _, _),) = _run(_sql(23), events)[1]
  assert calls == 3


@pytest.mark.parametrize(
    "events",
    [
        [],
        [_event("LLM_REQUEST", attributes={"usage_metadata": LOGGER_USAGE})],
        [_response("direct_logger", timestamp="2026-01-02T00:00:01Z")],
        [_response("direct_logger", agent="returns-agent")],
    ],
    ids=["no_events", "request_only", "outside_time_range", "filtered_out"],
)
def test_headline_returns_no_row_without_a_matching_response(events):
  assert _run(_sql(23), events, agent=["checkout-agent"]) == (
      _HEADLINE_COLUMNS,
      [],
  )


def test_headline_filter_matches_a_quoted_agent_name():
  events = [
      _response("direct_logger", agent="O'Brien"),
      _response("direct_logger", agent='O"Brien'),
  ]
  assert _run(_sql(23), events, agent=["O'Brien"])[1] == [
      (1, pytest.approx(3.75), 1_500_000)
  ]


def test_token_usage_over_time_reads_both_usage_shapes():
  events = [_response("direct_logger"), _response("plugin")]
  assert _run(_sql(9), events) == (
      ["time", "prompt_tokens", "completion_tokens", "total_tokens"],
      [("2026-01-01T01:00:00Z", 2_000_000, 1_000_000, 3_000_000)],
  )


def test_tokens_by_model_reads_both_usage_shapes():
  events = [_response("direct_logger"), _response("plugin")]
  columns, rows = _run(_sql(11), events)
  assert columns == [
      "model",
      "prompt_tokens",
      "completion_tokens",
      "total_tokens",
      "responses",
  ]
  # The logger writes attributes.model, which the view does not expose.
  assert sorted(rows) == [
      ("gemini-2.5-flash", 1_000_000, 500_000, 1_500_000, 1),
      ("unknown", 1_000_000, 500_000, 1_500_000, 1),
  ]


def test_session_rollup_counts_each_response_once():
  events = [_response(shape) for shape in sorted(_RESPONSES)]
  # Usage on any other event type is not counted.
  events.append(
      _event("LLM_REQUEST", attributes={"usage_metadata": LOGGER_USAGE})
  )
  columns, (row,) = _run(_sql(18), events)
  session = dict(zip(columns, row))
  assert session["session_events_in_window"] == 4
  assert session["session_input_tokens_in_window"] == 3_000_000
  assert session["session_output_tokens_in_window"] == 1_500_000


@pytest.mark.parametrize("name", sorted(ALL_QUERIES))
def test_every_query_runs_against_the_sdk_views(name):
  # A syntax or binding error raises here. Every query returns no rows for an
  # empty table and some rows for the sample events.
  assert _run(ALL_QUERIES[name])[1] == []
  assert _run(ALL_QUERIES[name], _SAMPLE_EVENTS)[1]


# --------------------------------------------------------------------------- #
# Structure                                                                   #
# --------------------------------------------------------------------------- #


def test_panel_ids_are_unique():
  ids = [panel["id"] for panel in _walk(DASHBOARD["panels"])]
  assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "consumer,source", sorted(DASHBOARD_DATA_SOURCES.items())
)
def test_dashboard_data_panels_read_their_source(consumer, source):
  panel = PANELS[consumer]
  assert panel["datasource"]["uid"] == DASHBOARD_DATASOURCE_UID
  (target,) = panel["targets"]
  assert target["panelId"] == source
  fields = panel["options"]["reduceOptions"]["fields"]
  column = re.fullmatch(r"/\^(\w+)\$/", fields).group(1)
  assert re.search(rf"\bAS {column}\b", _sql(source)), (consumer, column)


def test_every_bigquery_query_is_time_bounded():
  queries = [sql for _, sql in PANEL_SQL]
  queries += [
      variable["query"]["rawSql"]
      for variable in VARIABLES.values()
      if variable.get("type") == "query"
  ]
  for sql in queries:
    code = _strip_comments(sql)
    tables = re.findall(r"`\$\{project\}\.\$\{dataset\}\.[^`]+`", code)
    bounds = re.findall(r"\$__timeFilter\((?:\w+\.)?timestamp\)", code)
    assert tables and len(bounds) >= len(tables), code[:80]


def test_description_points_at_the_embedded_sql():
  description = DASHBOARD["description"]
  assert "Efficiency trial" in description
  assert "not synced" in description
  assert "source of truth" not in description


# --------------------------------------------------------------------------- #
# Review presentation fixes                                                   #
# --------------------------------------------------------------------------- #

_SESSION_IDS = [
    "plain-session",
    "has space",
    "a&var-agent=evil",
    "O'Brien's run",
    "path/with/slashes",
    "100% + #hash",
    "sessión-✓",
]


def _session_link():
  (override,) = PANELS[18]["fieldConfig"]["overrides"]
  assert override["matcher"] == {"id": "byName", "options": "session_id"}
  (link,) = override["properties"][0]["value"]
  return link


def _render_session_link(session_id):
  """Interpolates the link like Grafana, ``percentencode`` included."""
  url = _session_link()["url"]
  replacements = {
      "${__url.path}": "/d/bqaa-dashboard-efficiency-trial/trial",
      "${__url_time_range}": "from=now-24h&to=now",
      "${datasource:queryparam}": "var-datasource=bqaa-bigquery",
      # Grafana's percentencode is encodeURIComponent plus !'()*.
      "${__data.fields.session_id:percentencode}": parse.quote(
          session_id, safe="-_.~"
      ),
  }
  for placeholder, value in replacements.items():
    assert url.count(placeholder) == 1, placeholder
    url = url.replace(placeholder, value)
  assert "${" not in url
  return url


def test_session_link_opens_the_whole_trace():
  link = _session_link()
  assert link["title"] == "Open session trace (all agents and event types)"
  assert link["targetBlank"] is False


@pytest.mark.parametrize("session_id", _SESSION_IDS)
def test_session_link_round_trips_the_session_id(session_id):
  url = parse.urlsplit(_render_session_link(session_id))
  query = parse.parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
  assert url.path == "/d/bqaa-dashboard-efficiency-trial/trial"
  assert query == {
      "from": ["now-24h"],
      "to": ["now"],
      "var-datasource": ["bqaa-bigquery"],
      "var-agent": ["$__all"],
      "var-user_id": ["$__all"],
      "var-event_type": ["$__all"],
      "var-session_id": [session_id],
      "viewPanel": ["19"],
  }


def test_latency_legend_names_real_query_columns():
  overrides = PANELS[10]["fieldConfig"]["overrides"]
  named = {override["matcher"]["options"] for override in overrides}
  aliases = set(re.findall(r"\bAS (\w+)", _strip_comments(_sql(10))))
  assert named == {"p50_total_ms", "p95_total_ms", "p50_ttft_ms"}
  assert named <= aliases
