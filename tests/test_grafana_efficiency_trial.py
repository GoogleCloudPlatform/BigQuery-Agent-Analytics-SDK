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
"""

import json
import pathlib
import re
import sqlite3
from urllib import parse

import pytest

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
_VIEW_ROWS = {
    # (usage_prompt_tokens, usage_completion_tokens, usage_total_tokens,
    #  usage_metadata) as the llm_responses view exposes them.
    "content_usage_only": (1_000_000, 500_000, 1_500_000, None),
    "logger_usage_only": (None, None, None, json.dumps(LOGGER_USAGE)),
    "both_shapes": (1_000_000, 500_000, 1_500_000, json.dumps(LOGGER_USAGE)),
    "plugin_metadata": (
        1_000_000,
        500_000,
        1_500_000,
        json.dumps(PLUGIN_USAGE),
    ),
}
_CONTENT_USAGE = json.dumps(
    {"usage": {"prompt": 1_000_000, "completion": 500_000}}
)
_RAW_ROWS = {
    # (event_type, content, attributes) as agent_events stores them.
    "content_usage_only": ("LLM_RESPONSE", _CONTENT_USAGE, None),
    "logger_usage_only": (
        "LLM_RESPONSE",
        json.dumps({"response": "ok"}),
        json.dumps({"usage_metadata": LOGGER_USAGE}),
    ),
    "both_shapes": (
        "LLM_RESPONSE",
        _CONTENT_USAGE,
        json.dumps({"usage_metadata": LOGGER_USAGE}),
    ),
}


def _to_sqlite(expression):
  """Rewrites the BigQuery JSON reads in ``expression`` for SQLite."""
  translated = re.sub(
      r"SAFE_CAST\(JSON_VALUE\((\w+), ('[^']*')\) AS INT64\)",
      r"CAST(json_extract(\1, \2) AS INTEGER)",
      expression,
  )
  assert "JSON_VALUE" not in translated and "SAFE_CAST" not in translated
  return translated


def _view_totals(rows):
  """Sums the rows with panel 23's own usage expressions."""
  expressions = {
      match.group(1): _to_sqlite(match.group(0))
      for match in _VIEW_FALLBACK.finditer(_sql(23))
  }
  connection = sqlite3.connect(":memory:")
  connection.execute(
      "CREATE TABLE llm_responses (usage_prompt_tokens INTEGER,"
      " usage_completion_tokens INTEGER, usage_total_tokens INTEGER,"
      " usage_metadata TEXT)"
  )
  connection.executemany("INSERT INTO llm_responses VALUES (?, ?, ?, ?)", rows)
  sums = ", ".join(
      f"SUM({expressions[field]})"
      for field in ("prompt", "completion", "total")
  )
  return connection.execute(f"SELECT {sums} FROM llm_responses").fetchone()


def _estimated_cost(prompt_tokens, completion_tokens):
  """Prices tokens with the dashboard's default per-1M-token rates."""
  input_rate = float(VARIABLES["price_per_1m_input_tokens"]["query"])
  output_rate = float(VARIABLES["price_per_1m_output_tokens"]["query"])
  return (
      prompt_tokens / 1e6 * input_rate + completion_tokens / 1e6 * output_rate
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


@pytest.mark.parametrize("case", sorted(_VIEW_ROWS))
def test_headline_counts_one_response_once(case):
  prompt, completion, total = _view_totals([_VIEW_ROWS[case]])
  assert (prompt, completion, total) == (1_000_000, 500_000, 1_500_000)
  assert _estimated_cost(prompt, completion) == pytest.approx(3.75)


def test_headline_adds_up_mixed_producers():
  rows = [_VIEW_ROWS["content_usage_only"], _VIEW_ROWS["logger_usage_only"]]
  assert _view_totals(rows) == (2_000_000, 1_000_000, 3_000_000)


@pytest.mark.parametrize("case", sorted(_RAW_ROWS))
def test_session_rollup_counts_one_response_once(case):
  expressions = {
      match.group(1): _to_sqlite(match.group(0))
      for match in _RAW_FALLBACK.finditer(_strip_comments(_sql(18)))
  }
  connection = sqlite3.connect(":memory:")
  connection.execute(
      "CREATE TABLE agent_events (event_type TEXT, content TEXT,"
      " attributes TEXT)"
  )
  connection.execute(
      "INSERT INTO agent_events VALUES (?, ?, ?)", _RAW_ROWS[case]
  )
  sums = ", ".join(
      f"SUM({expressions[field]})" for field in ("prompt", "completion")
  )
  totals = connection.execute(f"SELECT {sums} FROM agent_events").fetchone()
  assert totals == (1_000_000, 500_000)


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
