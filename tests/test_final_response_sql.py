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
"""Runs the final-answer SQL in SQLite and compares it with the Python rules.

The SQL judge queries pick each session's final answer with
``trace._final_response_sql``. These tests run that SQL in SQLite, with
the BigQuery functions it calls registered as Python functions, and check
that it returns what ``Trace.final_response`` returns for the same rows.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import itertools
import json
import random
import re
import sqlite3

import pytest

from bigquery_agent_analytics import evaluators
from bigquery_agent_analytics import insights
from bigquery_agent_analytics.trace import _final_response_sql
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ADK = {"adk": {"schema_version": "1", "app_name": "app"}}
_WRITER = {"writer": {"plugin": "bigquery-agent-analytics-tracing"}}
_COLUMNS = (
    "session_id",
    "timestamp",
    "event_type",
    "invocation_id",
    "content",
    "attributes",
)
# r"..." is a BigQuery raw string literal; SQLite has only '...'.
_RAW_STRING = re.compile(r'(?<=[\s(,])r"([^"]*)"')


def _missing(value):
  return value is _MISSING


_MISSING = object()


def _path(document, path):
  value = json.loads(document) if document is not None else None
  if path == "$":
    return value
  for key in path.removeprefix("$.").split("."):
    if not isinstance(value, dict) or key not in value:
      return _MISSING
    value = value[key]
  return value


def _json_value(document, path):
  value = _path(document, path)
  if _missing(value) or value is None or isinstance(value, (dict, list)):
    return None
  return value if isinstance(value, str) else json.dumps(value)


def _json_query(document, path):
  value = _path(document, path)
  return None if _missing(value) else json.dumps(value, separators=(",", ":"))


def _regexp_contains(value, pattern):
  if value is None:
    return None
  return re.search(pattern, value, flags=0) is not None


def _regexp_replace(value, pattern, replacement):
  # BigQuery's replacement refers to groups as \\1 and drops unmatched ones.
  if value is None:
    return None
  return re.sub(
      pattern,
      lambda match: re.sub(
          r"\\(\d)", lambda ref: match.group(int(ref[1])) or "", replacement
      ),
      value,
  )


def _sqlite_query():
  query = (
      "WITH "
      + _final_response_sql("agent_events", "TRUE")
      + "\nSELECT session_id, final_response FROM final_responses"
  )
  query = _RAW_STRING.sub(
      lambda match: "'" + match[1].replace("'", "''") + "'", query
  )
  assert 'r"' not in query, query
  return query


def _run_sql(rows):
  """``final_response`` per session, from the SQL run in SQLite."""
  with contextlib.closing(sqlite3.connect(":memory:")) as connection:
    connection.create_function("JSON_VALUE", 2, _json_value)
    connection.create_function("JSON_QUERY", 2, _json_query)
    connection.create_function("TO_JSON_STRING", 1, lambda value: value)
    connection.create_function("CHR", 1, chr)
    connection.create_function(
        "LEFT", 2, lambda value, n: None if value is None else value[:n]
    )
    connection.create_function(
        "STARTS_WITH",
        2,
        lambda value, prefix: None
        if value is None
        else value.startswith(prefix),
    )
    # BigQuery's TRIM and RTRIM remove all whitespace, not only spaces.
    connection.create_function(
        "TRIM", 1, lambda value: None if value is None else value.strip()
    )
    connection.create_function(
        "RTRIM", 1, lambda value: None if value is None else value.rstrip()
    )
    connection.create_function("REGEXP_CONTAINS", 2, _regexp_contains)
    connection.create_function("REGEXP_REPLACE", 3, _regexp_replace)
    columns = ", ".join(f"{column} TEXT" for column in _COLUMNS)
    connection.execute(f"CREATE TABLE agent_events ({columns})")
    connection.executemany(
        "INSERT INTO agent_events VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                row["session_id"],
                row["timestamp"].isoformat(),
                row["event_type"],
                row["invocation_id"],
                None if row["content"] is None else json.dumps(row["content"]),
                json.dumps(row["attributes"]),
            )
            for row in rows
        ],
    )
    return dict(connection.execute(_sqlite_query()).fetchall())


def _run_python(rows):
  """``Trace.final_response`` per session, through the row loader."""
  sessions = {}
  for row in rows:
    sessions.setdefault(row["session_id"], []).append(
        Span.from_bigquery_row(
            dict(
                row,
                content=(
                    None
                    if row["content"] is None
                    else json.dumps(row["content"])
                ),
                attributes=json.dumps(row["attributes"]),
            )
        )
    )
  return {
      session_id: Trace(
          trace_id="t", session_id=session_id, spans=spans
      ).final_response
      for session_id, spans in sessions.items()
  }


def _session(session_id, turns):
  """Rows of one session from (event_type, content, invocation, attrs)."""
  return [
      {
          "session_id": session_id,
          "timestamp": _T0 + timedelta(seconds=i),
          "event_type": event_type,
          "invocation_id": invocation,
          "content": content,
          "attributes": attributes,
      }
      for i, (event_type, content, invocation, attributes) in enumerate(turns)
  ]


_CHILD = dict(_ADK, adk={"parent_invocation_id": "inv-2"})
_EVALBENCH = {"experiment_id": "x", "evalbench_scenario_id": "e1"}
_ORPHAN = dict(_ADK, adk={"parent_invocation_id": "e-parent"})


def _user(invocation):
  return ("USER_MESSAGE_RECEIVED", {"text_summary": "q"}, invocation, _ADK)


def _llm(response, invocation="inv-1", attributes=_ADK):
  return ("LLM_RESPONSE", {"response": response}, invocation, attributes)


# Model turns as the ADK plugin formats them, and answers other producers
# store verbatim.
_TURNS = [
    "text: 'The answer is 42.'",
    "text: 'It's done | now'",
    "text: 'Use the call: run_python tool next time.'",
    "text: 'a' | text: 'b'",
    "text: 'a' | other",
    "other | text: 'a'",
    "text: ''",
    "text: 'Answer'  ",
    "None",
    "other",
    "resp: lookup",
    "Executable code (PYTHON): print(1)",
    "call: run_python",
    "call: ",
    "call: list_files | call: preview_file",
    "text: 'Let me check.' | call: lookup",
    "text: 'Let me check.' | call: ",
    "call: search | text: 'Here it is.'",
    "call: run_python | other",
    "call: lookup(id=3)",
    "call: run_python  ",
    "call: mcp.read-file",
    "call: 555-0100",
    "call: 3d_tool",
    "Please call: support",
    "Status | call: support",
    "text: 'x' | call: b is next",
    "text: 'Contact the desk | call: support...[TRUNCATED]",
    "text: 'Checking.' | call: lookup_cus...[TRUNCATED]",
    "text: 'Checking.' | cal...[TRUNCATED]",
    "text: 'First.' | text: 'Sec...[TRUNCATED]",
    "text: 'Pick 'A' | call: lookup | then answer.'",
    "text: 'Use 'x' | otherwise stop'",
    "text: 'If 'closed' | call: the desk...[TRUNCATED]",
    "Executable code (PYTHON): print(a | b) | Code execution result"
    " (OUTCOME_OK): 3 | text: 'The answer is 3.'",
    "Executable code (PYTHON): x = 1 | Code execution result (OUTCOME_OK):"
    "  | text: 'Done.'",
    "call: 1password_lookup",
    "text: 'Run this: echo 'ERROR' | c...[TRUNCATED]",
    "text: 'Press 'Help' | call: supp...[TRUNCATED]",
    "text: 'Checking.' | other | cal...[TRUNCATED]",
    "call: lookup |...[TRUNCATED]",
    "resp: get_...[TRUNCATED]",
    "resp: ",
    "other...[TRUNCATED]",
    "ca...[TRUNCATED]",
    "  ",
]


class TestSqlMatchesPython:

  @pytest.mark.parametrize("turn", _TURNS)
  @pytest.mark.parametrize(
      "attributes", [_ADK, _WRITER, {}], ids=["adk", "writer", "none"]
  )
  def test_one_turn(self, turn, attributes):
    rows = _session(
        "s",
        [
            _llm("text: 'Earlier.'", attributes=attributes),
            _llm(turn, attributes=attributes),
        ],
    )
    assert _run_sql(rows) == _run_python(rows)

  @pytest.mark.parametrize(
      "completion",
      [
          {"response": "Done."},
          {"response": {"answer": "Done."}, "text_summary": "Done."},
          {"response": {"answer": 1}},
          {"response": "call: exit_loop"},
          {"response": "None"},
          {"text_summary": "Summary."},
          None,
      ],
  )
  def test_completion_after_a_tool_call(self, completion):
    rows = _session(
        "s",
        [
            _llm("text: 'Earlier.'"),
            _llm("call: finish"),
            ("AGENT_COMPLETED", completion, "inv-1", _ADK),
        ],
    )
    assert _run_sql(rows) == _run_python(rows)

  def test_invocations(self):
    child = dict(_ADK, adk={"parent_invocation_id": "inv-2"})
    sessions = [
        # The second invocation never answered.
        [
            _user("inv-1"),
            _llm("text: 'One.'"),
            _user("inv-2"),
            _llm("None", "inv-2"),
        ],
        [
            _user("inv-1"),
            _llm("text: 'One.'"),
            _user("inv-2"),
            _llm("call: x", "inv-2"),
        ],
        # A nested invocation belongs to its parent.
        [
            _llm("text: 'One.'"),
            _llm("text: 'Two.'", "inv-2"),
            _llm("None", "inv-2-child", child),
        ],
        # Without usable ids, the last user message bounds the invocation.
        [
            ("USER_MESSAGE_RECEIVED", {"text_summary": "q"}, None, _ADK),
            _llm("text: 'One.'", None),
            ("USER_MESSAGE_RECEIVED", {"text_summary": "q"}, None, _ADK),
            _llm("None", None),
        ],
        # A writer whose ids never repeat, and no ADK envelope.
        [_llm("text: 'One.'", "a", {}), _llm("None", "b", {})],
        # Usage-only and structured model turns.
        [
            _llm("text: 'One.'"),
            ("LLM_RESPONSE", {"usage": {"total": 1}}, "inv-1", _ADK),
        ],
        [_llm({"intent": "return", "needs_photo": True})],
        [("LLM_RESPONSE", "The answer.", "inv-1", _ADK)],
        # One row per invocation, as an event-type filter leaves it.
        [_llm("text: 'One.'", "inv-1"), _llm("None", "inv-2")],
        # BQCA views answer in AGENT_RESPONSE, with NULL model turns.
        [
            ("LLM_RESPONSE", None, "inv-1", _ADK),
            ("AGENT_RESPONSE", {"response": "42 orders."}, "inv-1", _ADK),
        ],
        # A nested invocation's text is not the parent's answer.
        [
            _user("inv-2"),
            _llm("call: research", "inv-2"),
            _llm("text: 'notes'", "inv-c", _CHILD),
            _llm("None", "inv-2"),
        ],
        # EvalBench imports answer verbatim in AGENT_COMPLETED.
        [
            ("USER_MESSAGE_RECEIVED", {"text": "q"}, "e1", _EVALBENCH),
            ("AGENT_COMPLETED", {"response": "other"}, "e1", _EVALBENCH),
        ],
        # Several completions: the latest one with an answer.
        [
            _llm("call: transfer"),
            ("AGENT_COMPLETED", {"response": "Sub done."}, "inv-1", _ADK),
            ("AGENT_COMPLETED", {"response": "Root."}, "inv-1", _ADK),
            ("AGENT_COMPLETED", None, "inv-1", _ADK),
        ],
        # Non-ADK rows after an event-type filter: one row in the last turn.
        [
            _llm("call: lookup", "c1", {}),
            _llm("text: 'Answer one'", "c1", {}),
            _llm("text: 'Answer two'", "c2", {}),
        ],
        # A writer's stray row with a fresh id does not end the turn.
        [
            ("LLM_RESPONSE", {"response": "Fixed."}, "t1", _WRITER),
            ("USER_MESSAGE_RECEIVED", {"text": "q"}, "t1", _WRITER),
            ("TOOL_STARTING", {"tool": "x"}, "x1", _WRITER),
        ],
        # A bool or JSON scalar response, and a mix of ADK and older rows.
        [_llm(True)],
        [_llm("text: 'Old.'", "inv-1", {}), _llm("None", "inv-2")],
        # A row without an id inside the run.
        [_llm("call: f", "inv-1"), _llm("text: 'A'", None)],
        # A nested invocation's completion is not the parent's answer.
        [
            _llm("call: research", "inv-2"),
            ("AGENT_COMPLETED", {"response": "child"}, "inv-c", _CHILD),
        ],
        # Non-ADK rows: the last turn is the id shared by two rows.
        [
            _llm("text: 'One'", "t1", {}),
            ("USER_MESSAGE_RECEIVED", {"text": "q"}, "t2", {}),
            ("TOOL_STARTING", {"tool": "x"}, "t2", {}),
        ],
        # Verbatim rows lose only a text: wrapper.
        [("LLM_RESPONSE", {"response": "text: foo"}, "t1", _WRITER)],
        [_llm(["a"])],
        # An AgentTool session read alone: its parent is not here.
        [
            _llm("text: 'X is 42.'", "e-child", _ORPHAN),
            ("AGENT_COMPLETED", None, "e-child", _ORPHAN),
        ],
        # AGENT_RESPONSE prefers its text_summary.
        [
            (
                "AGENT_RESPONSE",
                {"text_summary": "Paris.", "response": "text: '{}'"},
                "inv-1",
                _ADK,
            )
        ],
        [_llm("call: lookup(query='x')"), _llm("None")],
    ]
    rows = list(
        itertools.chain.from_iterable(
            _session(f"s{i}", turns) for i, turns in enumerate(sessions)
        )
    )
    python = _run_python(rows)
    assert _run_sql(rows) == python
    assert python == {
        "s0": None,
        "s1": None,
        "s2": "Two.",
        "s3": None,
        "s4": "One.",
        "s5": "One.",
        "s6": '{"intent":"return","needs_photo":true}',
        "s7": "The answer.",
        "s8": None,
        "s9": "42 orders.",
        "s10": None,
        "s11": "other",
        "s12": "Root.",
        "s13": "Answer two",
        "s14": "Fixed.",
        "s15": "true",
        "s16": None,
        "s17": "A",
        "s18": None,
        "s19": None,
        "s20": "foo",
        "s21": '["a"]',
        "s22": "X is 42.",
        "s23": "Paris.",
        "s24": None,
    }

  def test_random_sessions(self):
    rng = random.Random(595)
    rows = []
    for n in range(400):
      attributes = rng.choice([_ADK, _ADK, _WRITER, {}])
      turns = []
      for k in range(rng.randint(1, 5)):
        invocation = "inv-2" if k >= 2 and n % 3 == 0 else "inv-1"
        turns.append(_llm(rng.choice(_TURNS), invocation, attributes))
      if rng.random() < 0.3:
        turns.append(
            (
                "AGENT_COMPLETED",
                rng.choice(
                    [{"response": "Done."}, {"response": "call: x"}, None]
                ),
                turns[-1][2],
                attributes,
            )
        )
      rows += _session(f"s{n}", turns)
    assert _run_sql(rows) == _run_python(rows)


def _generated_turn(rng):
  """A model turn as the plugin formats it, possibly cut by its size limit."""
  words = ["The", "answer", "is", "42", "|", "call:", "call: x", "it's"]
  words += ["None", "text:", "other", "\n", "resp: f", "a | b", "..."]
  words += ["'quoted'", "| 'cell' |"]
  parts = []
  for _ in range(rng.randint(1, 3)):
    kind = rng.random()
    if kind < 0.5:
      text = " ".join(rng.choice(words) for _ in range(rng.randint(0, 6)))
      parts.append(f"text: '{text}'")
    elif kind < 0.75:
      name = rng.choice(["run_python", "", "mcp.read-file", "ns:tool", "_x1"])
      parts.append(f"call: {name}")
    elif kind < 0.85:
      parts.append("resp: lookup")
    elif kind < 0.93:
      parts.append("other")
    elif kind < 0.97:
      code = rng.choice(["x = 1", "print(a | b)", "df.to_markdown()\n"])
      parts.append(f"Executable code (PYTHON): {code}")
    else:
      output = rng.choice(["3", "", "| a | b |\n|--:|--:|\n| 1 | 2 |"])
      parts.append(f"Code execution result (OUTCOME_OK): {output}")
  turn = " | ".join(parts)
  if rng.random() < 0.1:
    return "None"
  if rng.random() < 0.15:
    # The limit is large, so the cut lands inside a part's body.
    starts = [
        i
        for i in range(len(turn))
        if turn.startswith(
            ("text: '", "call: ", "Executable code (", "Code execution"), i
        )
    ]
    start = rng.choice(starts) if starts else None
    if start is not None:
      body = (
          start + len("text: '")
          if turn.startswith("text: '", start)
          else turn.index(" ", start) + 1
      )
      end = turn.find(" | ", body)
      end = len(turn) if end == -1 else end
      if end > body + 1:
        turn = turn[: rng.randint(body + 1, end - 1)] + "...[TRUNCATED]"
  return turn


class TestGeneratedTurns:

  @pytest.mark.parametrize("seed", range(6))
  def test_sql_matches_python(self, seed):
    rng = random.Random(seed)
    rows = []
    for n in range(500):
      attributes = rng.choice([_ADK, _ADK, {}])
      turns = [
          _llm(_generated_turn(rng), "inv-1", attributes)
          for _ in range(rng.randint(1, 3))
      ]
      if rng.random() < 0.2:
        turns.append(
            (
                "AGENT_COMPLETED",
                {"response": rng.choice(["Done.", _generated_turn(rng)])},
                "inv-1",
                attributes,
            )
        )
      rows += _session(f"s{n}", turns)
    assert _run_sql(rows) == _run_python(rows)


class TestQueries:

  @pytest.mark.parametrize(
      "template",
      [
          evaluators._AI_GENERATE_JUDGE_BATCH_QUERY_TEMPLATE,
          evaluators._LEGACY_LLM_JUDGE_BATCH_QUERY,
      ],
  )
  def test_judge_queries_read_the_final_answer_rules(self, template):
    assert "LEFT JOIN final_responses USING (session_id)" in template
    assert (
        "JSON_VALUE(content, '$.response')\n      IGNORE NULLS" not in template
    )
    sql = template.format(
        project="p",
        dataset="d",
        table="t",
        where="TRUE",
        endpoint="e",
        connection_arg="",
        model="m",
    )
    assert sql.count("`p.d.t`") == 2

  def test_the_transcript_query_has_no_unused_answer_column(self):
    assert "final_response" not in insights._SESSION_TRANSCRIPT_QUERY
