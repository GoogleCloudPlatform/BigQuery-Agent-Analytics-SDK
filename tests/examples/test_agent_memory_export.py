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

"""Hermetic tests for examples/agent_memory/export_memory.py.

The export turns the memory layers into the JSON the web view reads. These
tests build it from the committed synthetic fixture through the offline
BigQuery stand-in; expected values are written out from the fixture rows.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime
from datetime import timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from bigquery_agent_analytics import Client

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "agent_memory"
sys.path.insert(0, str(EXAMPLE_DIR))

import export_memory  # noqa: E402
import memory_layers  # noqa: E402
import offline_bigquery  # noqa: E402

TRIP_ENTITY_ARGS = {
    "origin": "LOCATION",
    "destination": "LOCATION",
    "city": "LOCATION",
    "near": "LOCATION",
}
HOTEL_TIMEOUT = (
    "TimeoutError: hotel inventory API did not respond within 5000 ms"
)


@pytest.fixture(scope="module")
def fixture() -> offline_bigquery.Fixture:
  return offline_bigquery.load_fixture()


def _memory(rows, user_id="u-ana") -> memory_layers.UserMemory:
  client = Client(
      project_id="offline-demo",
      dataset_id="agent_analytics",
      verify_schema=False,
      bq_client=offline_bigquery.OfflineBigQueryClient(rows),
  )
  return memory_layers.load_user_memory(
      client, user_id, entity_args=TRIP_ENTITY_ARGS
  )


@pytest.fixture(scope="module")
def ana(fixture) -> dict:
  return export_memory.build_user_export(_memory(fixture.rows))


def test_user_export_lists_sessions_oldest_first_with_messages(ana):
  assert ana["user_id"] == "u-ana"
  assert ana["current_session_id"] == "s-104"
  assert [
      (s["session_id"], s["row_count"], len(s["messages"]))
      for s in ana["sessions"]
  ] == [("s-101", 18, 3), ("s-102", 16, 2), ("s-103", 14, 2), ("s-104", 3, 1)]
  first = ana["sessions"][0]["messages"][0]
  assert first == {
      "role": "user",
      "content": (
          "I'm vegetarian and I prefer window seats. Find me a flight from"
          " SFO to Tokyo on Oct 12."
      ),
      "timestamp": "2026-09-28T17:00:00.100Z",
      "span_id": "sp-101-inv",
      "complete": True,
  }


def test_user_export_keeps_preference_versions_and_entities(ana):
  assert ana["preferences"] == [
      {
          "category": "diet",
          "preference": "vegetarian",
          "valid_from": "2026-09-28T17:00:01.600Z",
          "valid_until": "2026-10-04T18:00:01.445Z",
          "session_id": "s-101",
          "span_id": "sp-101-agent",
      },
      {
          "category": "seat",
          "preference": "window",
          "valid_from": "2026-09-28T17:00:01.600Z",
          "valid_until": None,
          "session_id": "s-101",
          "span_id": "sp-101-agent",
      },
      {
          "category": "diet",
          "preference": "pescatarian",
          "valid_from": "2026-10-04T18:00:01.445Z",
          "valid_until": None,
          "session_id": "s-103",
          "span_id": "sp-103-agent",
      },
  ]
  assert [(e["name"], len(e["mentions"])) for e in ana["entities"]] == [
      ("Kyoto", 3),
      ("Kyoto Station", 2),
      ("SFO", 1),
      ("Tokyo", 1),
  ]
  assert ana["entities"][0]["mentions"][2] == {
      "tool_name": "find_restaurants",
      "argument": "city",
      "session_id": "s-103",
      "span_id": "sp-103-tool-2",
      "timestamp": "2026-10-04T18:00:01.475Z",
      "source": "tool",
  }


def test_trace_timeline_places_model_turns_and_tool_calls(ana):
  trace = next(t for t in ana["traces"] if t["trace_id"] == "inv-102")

  assert [
      (row["kind"], row["label"], row["start_ms"], row["end_ms"], row["status"])
      for row in trace["timeline"]
  ] == [
      ("model", "call: search_hotels", 300.0, 1200.0, "success"),
      ("tool", "search_hotels", 1300.0, 6303.0, "error"),
      ("model", "call: search_hotels", 6403.0, 7103.0, "success"),
      ("tool", "search_hotels", 7203.0, 8023.0, "success"),
      ("model", "answer", 8123.0, 9123.0, "success"),
  ]
  assert trace["timeline"][1]["detail"] == HOTEL_TIMEOUT
  assert trace["timeline"][1]["span_id"] == "sp-102-tool-1"
  assert trace["outcome_status"] == "answered_with_errors"
  assert trace["latency_ms"] == pytest.approx(9323.0)
  assert trace["errors"] == [HOTEL_TIMEOUT]


def test_a_tool_reported_error_is_a_failed_bar_and_a_trace_error(fixture):
  # The timeline, the tool call and the trace use the same failure rule.
  rows = copy.deepcopy(fixture.rows)
  _row(rows, "sp-103-tool-2", "TOOL_COMPLETED")["content"]["result"] = {
      "status": "error",
      "message": "Unrecognized name: cuisine",
  }

  export = export_memory.build_user_export(_memory(rows))

  trace = next(t for t in export["traces"] if t["trace_id"] == "inv-103")
  (bar,) = [r for r in trace["timeline"] if r["span_id"] == "sp-103-tool-2"]
  (call,) = [
      c
      for step in trace["steps"]
      for c in step["tool_calls"]
      if c["span_id"] == "sp-103-tool-2"
  ]
  assert (bar["kind"], bar["status"], bar["detail"]) == (
      "tool",
      "error",
      "Unrecognized name: cuisine",
  )
  assert (call["status"], call["error"]) == (
      "error",
      "Unrecognized name: cuisine",
  )
  assert (trace["outcome_status"], trace["errors"]) == (
      "answered_with_errors",
      ["Unrecognized name: cuisine"],
  )


def test_trace_steps_carry_tool_calls_with_short_results(ana):
  trace = next(t for t in ana["traces"] if t["trace_id"] == "inv-101")

  first, second = trace["steps"]
  assert first["action"] == "call: save_preference, save_preference"
  assert first["tool_calls"][0] == {
      "tool_name": "save_preference",
      "arguments": {"key": "diet", "value": "vegetarian"},
      "result": '{"status": "saved", "key": "diet"}',
      "status": "success",
      "duration_ms": 30,
      "error": None,
      "span_id": "sp-101-tool-1",
      "started_at": "2026-09-28T17:00:01.500Z",
  }
  assert second["tool_calls"][0]["result"].startswith('{"flights": [')


def test_long_tool_results_are_shortened_for_the_page(fixture):
  rows = copy.deepcopy(fixture.rows)
  completion = next(
      r
      for r in rows
      if r["span_id"] == "sp-101-tool-3" and r["event_type"] == "TOOL_COMPLETED"
  )
  completion["content"]["result"] = {"notes": "x" * 1000}

  export = export_memory.build_user_export(_memory(rows))

  trace = next(t for t in export["traces"] if t["trace_id"] == "inv-101")
  result = trace["steps"][1]["tool_calls"][0]["result"]
  assert len(result) == export_memory.RESULT_PREVIEW_CHARS
  assert result.endswith("...")


def test_trace_errors_are_listed_once(fixture):
  rows = copy.deepcopy(fixture.rows)
  error = next(r for r in rows if r["event_type"] == "TOOL_ERROR")
  for event_type in ("AGENT_ERROR", "INVOCATION_ERROR"):
    extra = copy.deepcopy(error)
    extra.update(event_type=event_type, content=None, span_id="sp-102-agent")
    rows.append(extra)

  export = export_memory.build_user_export(_memory(rows))

  trace = next(t for t in export["traces"] if t["trace_id"] == "inv-102")
  assert trace["errors"] == [HOTEL_TIMEOUT]


def test_export_is_plain_json_with_a_context_block(fixture, ana):
  export = export_memory.build_export(
      [ana],
      label="Offline synthetic fixture",
      source="synthetic fixture agent_events.json",
      exported_at="2026-10-06T16:00:00Z",
  )

  assert json.loads(json.dumps(export)) == export
  assert export["schema"] == "bqaa-agent-memory-viz/2"
  assert (export["run"], export["comparisons"]) == (None, [])
  assert ana["context"].startswith("# Memory for user u-ana")
  assert "- diet = pescatarian (since 2026-10-04T18:00:01Z" in ana["context"]


def test_cli_offline_writes_every_requested_user(tmp_path):
  out = tmp_path / "memory_export.json"

  assert (
      export_memory.main(
          ["--user-id", "u-ana", "--user-id", "u-ben", "--out", str(out)]
      )
      == 0
  )

  export = json.loads(out.read_text("utf-8"))
  assert [u["user_id"] for u in export["users"]] == ["u-ana", "u-ben"]
  assert export["label"] == "Offline synthetic fixture"
  ben = export["users"][1]
  assert [p["preference"] for p in ben["preferences"]] == ["vegan"]
  assert "pescatarian" not in json.dumps(ben)


@pytest.mark.parametrize(
    "extra,expected_source",
    [
        ([], "<project>.my_dataset.agent_events"),
        (["--show-project"], "my-project.my_dataset.agent_events"),
    ],
)
def test_cli_live_mode_labels_the_table(
    fixture, tmp_path, extra, expected_source
):
  out = tmp_path / "memory_export.json"
  argv = [
      "--project-id",
      "my-project",
      "--dataset-id",
      "my_dataset",
      "--user-id",
      "u-ana",
      "--now",
      "2026-10-06T16:00:00Z",
      "--out",
      str(out),
      *extra,
  ]

  assert (
      export_memory.main(
          argv, bq_client=offline_bigquery.OfflineBigQueryClient(fixture.rows)
      )
      == 0
  )

  export = json.loads(out.read_text("utf-8"))
  assert export["source"] == expected_source
  assert export["label"] == "BigQuery read"
  assert [s["session_id"] for s in export["users"][0]["sessions"]] == [
      "s-101",
      "s-102",
      "s-103",
      "s-104",
  ]


# ---- streamed model calls ----------------------------------------------------

# google-adk 2.11 writes these only on a terminal (non-partial) LLM_RESPONSE.
TERMINAL_MARKERS = ("cache_type", "finish_reason")


def _row(rows, span_id, event_type):
  return next(
      r
      for r in rows
      if r["span_id"] == span_id and r["event_type"] == event_type
  )


def _fragment(row, text, at):
  """A streaming chunk of ``row``'s model call: same span, no terminal marker."""
  chunk = copy.deepcopy(row)
  chunk["timestamp"] = at
  chunk["content"] = {"response": f"text: '{text}'"}
  for key in TERMINAL_MARKERS:
    chunk["attributes"].pop(key)
  return chunk


def _at(*args):
  return datetime(*args, tzinfo=timezone.utc)


def test_timeline_has_one_row_per_streamed_model_call(fixture):
  rows = copy.deepcopy(fixture.rows)
  final = _row(rows, "sp-101-llm-3", "LLM_RESPONSE")
  rows += [
      _fragment(
          final, "Saved: vegetarian, ", _at(2026, 9, 28, 17, 0, 3, 900000)
      ),
      _fragment(
          final, "window seat. DM101", _at(2026, 9, 28, 17, 0, 4, 200000)
      ),
  ]

  export = export_memory.build_user_export(_memory(rows))

  trace = next(t for t in export["traces"] if t["trace_id"] == "inv-101")
  assert [
      (row["label"], row["start_ms"], row["end_ms"], row["status"])
      for row in trace["timeline"]
      if row["kind"] == "model"
  ] == [
      ("call: save_preference, save_preference", 300.0, 1400.0, "success"),
      ("call: search_flights", 1680.0, 2480.0, "success"),
      ("answer", 3280.0, 4480.0, "success"),
  ]
  assert trace["llm_calls"] == 3


def test_an_interrupted_stream_exports_as_incomplete(fixture):
  rows = copy.deepcopy(fixture.rows)
  request = copy.deepcopy(_row(rows, "sp-103-llm-1", "LLM_REQUEST"))
  chunk = _fragment(
      _row(rows, "sp-103-llm-2", "LLM_RESPONSE"),
      "Dotonbori",
      _at(2026, 10, 6, 15, 59, 51),
  )
  for row, at in (
      (request, _at(2026, 10, 6, 15, 59, 50, 300000)),
      (chunk, None),
  ):
    row.update(
        session_id="s-104",
        invocation_id="inv-104",
        trace_id="t-104",
        span_id="sp-104-llm-1",
        parent_span_id="sp-104-agent",
    )
    if at is not None:
      row["timestamp"] = at
  rows += [request, chunk]

  export = export_memory.build_user_export(_memory(rows))

  session = next(s for s in export["sessions"] if s["session_id"] == "s-104")
  assert [
      (m["role"], m["content"], m["complete"]) for m in session["messages"]
  ] == [
      (
          "user",
          "Find a restaurant in Osaka for dinner that fits my diet.",
          True,
      ),
      ("assistant", "Dotonbori", False),
  ]
  trace = next(t for t in export["traces"] if t["trace_id"] == "inv-104")
  assert [
      (row["label"], row["start_ms"], row["end_ms"], row["status"])
      for row in trace["timeline"]
  ] == [("reply", 300.0, 1000.0, "incomplete")]
  assert (trace["outcome_status"], trace["outcome_span_id"]) == (
      "unanswered",
      None,
  )


def test_answered_traces_name_the_span_of_their_answer(ana):
  assert [(t["trace_id"], t["outcome_span_id"]) for t in ana["traces"]] == [
      ("inv-101", "sp-101-llm-3"),
      ("inv-102", "sp-102-llm-3"),
      ("inv-103", "sp-103-llm-2"),
      ("inv-104", None),
  ]


# ---- recalled memory, session state, facts, comparisons and run totals -----


def _with_recall(fixture):
  """s-103's restaurant search, renamed into a recall that cites s-101/2."""
  rows = copy.deepcopy(fixture.rows)
  memory_text = (
      "# Memory for user u-ana\n"
      "- diet = vegetarian [s-101/sp-101-agent]\n"
      "- Kyoto (LOCATION): 2 tool calls [+1 earlier, s-102/sp-102-tool-2]\n"
      "- dinner on 2026/10/15 [unknown/x]"
  )
  for row in rows:
    if row["span_id"] == "sp-103-tool-2":
      row["content"]["tool"] = "recall_memory"
      row["content"]["args"] = {"request": "dinner in Kyoto"}
      if row["event_type"] == "TOOL_COMPLETED":
        row["content"]["result"] = {"memory": memory_text}
  return rows, memory_text


def test_traces_carry_the_memory_their_recall_returned(fixture):
  rows, memory_text = _with_recall(fixture)

  export = export_memory.build_user_export(_memory(rows))

  trace = next(t for t in export["traces"] if t["trace_id"] == "inv-103")
  session = next(s for s in export["sessions"] if s["session_id"] == "s-103")
  assert trace["recall"] == {
      "text": memory_text,
      "sessions": ["s-101", "s-102"],
  }
  assert session["recalled_sessions"] == ["s-101", "s-102"]
  assert all(
      t["recall"] is None
      for t in export["traces"]
      if t["trace_id"] != "inv-103"
  )


def test_recalled_sessions_keeps_known_earlier_sessions_in_order():
  text = (
      "[s-2/sp-1] [+3 earlier, s-1/sp-9, s-2/sp-4] 10/20 [s-3/sp-2]"
      " [elsewhere/sp-5]"
  )

  assert export_memory.recalled_sessions(
      text, {"s-1", "s-2", "s-3"}, current="s-3"
  ) == ["s-2", "s-1"]


def test_traces_count_their_sql_queries_and_failures(fixture):
  rows = copy.deepcopy(fixture.rows)
  for row in rows:
    if row["span_id"] in ("sp-102-tool-1", "sp-102-tool-2"):
      row["content"]["tool"] = "run_sql"

  export = export_memory.build_user_export(_memory(rows))

  trace = next(t for t in export["traces"] if t["trace_id"] == "inv-102")
  assert (
      trace["tool_call_count"],
      trace["sql_queries"],
      trace["sql_errors"],
  ) == (
      2,
      2,
      1,
  )


def test_sessions_export_their_logged_state_and_the_analyst_name(fixture):
  rows = copy.deepcopy(fixture.rows)
  for row in rows:
    if row["session_id"] == "s-102":
      row["attributes"]["session_metadata"] = {
          "state": {"sim_day": 2, "analyst_name": "Ana Lima", "user:x": "y"}
      }

  export = export_memory.build_user_export(_memory(rows))

  states = {s["session_id"]: s["state"] for s in export["sessions"]}
  assert states["s-102"] == {"sim_day": 2, "analyst_name": "Ana Lima"}
  assert states["s-101"] == {}
  assert export["name"] == "Ana Lima"


def test_facts_are_exported_with_the_row_they_came_from(fixture):
  client = Client(
      project_id="offline-demo",
      dataset_id="agent_analytics",
      verify_schema=False,
      bq_client=offline_bigquery.OfflineBigQueryClient(fixture.rows),
  )
  fact = memory_layers.Fact(
      "Ana",
      "PERSON",
      "follows_diet",
      "pescatarian",
      "VALUE",
      "Ana is pescatarian.",
      "s-103",
      "sp-103-inv",
      _at(2026, 10, 4, 18, 0, 0, 100000),
  )
  memory = memory_layers.load_user_memory(client, "u-ana", facts=[fact])

  export = export_memory.build_user_export(memory)

  assert export["facts"] == [
      {
          "subject": "Ana",
          "subject_type": "PERSON",
          "predicate": "follows_diet",
          "object": "pescatarian",
          "object_type": "VALUE",
          "statement": "Ana is pescatarian.",
          "session_id": "s-103",
          "span_id": "sp-103-inv",
          "observed_at": "2026-10-04T18:00:00.100Z",
      }
  ]


RECORD = {
    "label": "Recorded live run: TheLook analyst week",
    "model": "gemini-x",
    "run_tag": "t",
    "days": [{"number": 1, "date": "2026-10-01", "weekday": "Thursday"}],
    "analysts": [
        {"user_id": "u-ana", "name": "Ana Lima", "role": "r"},
        {"user_id": "u-ben", "name": "Ben", "role": "r"},
    ],
    "sessions": [
        {
            "session_id": "s-103",
            "user_id": "u-ana",
            "memory": "on",
            "turns": [{}],
        },
        {
            "session_id": "s-101",
            "user_id": "u-ana",
            "memory": "on",
            "turns": [{}, {}],
        },
        {
            "session_id": "s-201",
            "user_id": "u-ben",
            "memory": "off",
            "turns": [{}],
        },
    ],
    "comparisons": [
        {
            "user_id": "u-ana",
            "day": 1,
            "question": "Dinner that fits my diet?",
            "with_memory": "s-103",
            "without_memory": "s-201",
        }
    ],
    "consolidation": [{"day": 1, "extraction": {"messages": 3, "facts": 2}}],
    "row_counts": [{"row_count": 20}, {"row_count": 5}],
    "usage": [{"model_calls": 7}, {"model_calls": 2}],
}


def test_comparisons_summarize_the_first_turn_of_both_sessions(fixture):
  memories = {
      "u-ana": _memory(fixture.rows, "u-ana"),
      "u-ben": _memory(fixture.rows, "u-ben"),
  }

  (pair,) = export_memory.build_comparisons(
      RECORD, memories, {"u-ana": "Ana Lima"}
  )

  assert (pair["name"], pair["day"], pair["question"]) == (
      "Ana Lima",
      1,
      "Dinner that fits my diet?",
  )
  assert pair["with_memory"]["session_id"] == "s-103"
  assert pair["with_memory"]["tool_calls"] == [
      "save_preference",
      "find_restaurants",
  ]
  assert pair["with_memory"]["answer"].startswith(
      "Updated your diet to pescatarian."
  )
  assert pair["without_memory"]["session_id"] == "s-201"
  assert pair["without_memory"]["outcome_status"] == "answered"
  assert (
      pair["without_memory"]["sql_queries"],
      pair["with_memory"]["recalled_sessions"],
  ) == (0, [])
  assert pair["without_memory"]["memory_table_reads"] == []
  assert pair["control_read_memory"] is False


def test_comparisons_skip_pairs_whose_memory_was_not_loaded(fixture):
  memories = {"u-ana": _memory(fixture.rows, "u-ana")}

  assert export_memory.build_comparisons(RECORD, memories, {}) == []


def _sql_call(sql, purpose):
  return memory_layers.ToolCall(
      tool_name="run_sql",
      arguments={"purpose": purpose, "sql": sql},
      result=None,
      status="success",
      duration_ms=None,
      error=None,
      session_id="s-ctl",
      span_id=f"span-{purpose}",
      started_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
  )


REFUSED = (
    "Only tables in bigquery-public-data.thelook_ecommerce can be read here,"
    " not p.d.analyst_memory_items."
)


def test_memory_table_reads_are_the_sql_calls_that_name_the_memory_dataset():
  dataset = "bqaa_agent_memory_demo"
  refused = dataclasses.replace(
      _sql_call(f"SELECT * FROM `{dataset}.analyst_memory_items`", "refused"),
      status="error",
      error=REFUSED,
  )
  calls = [
      refused,
      _sql_call(f"SELECT * FROM `{dataset}.analyst_memory_items`", "items"),
      _sql_call(
          f"SELECT * FROM `p.{dataset}.INFORMATION_SCHEMA.TABLES`", "tables"
      ),
      _sql_call(
          "SELECT 1 FROM `bigquery-public-data.thelook_ecommerce.orders`",
          "orders",
      ),
      _sql_call(f"SELECT * FROM `{dataset}_backup.t`", "another dataset"),
      dataclasses.replace(
          _sql_call(dataset, "not sql"), tool_name="describe_table"
      ),
  ]

  reads = export_memory.memory_table_reads(calls, dataset)

  assert [(r["purpose"], r["span_id"]) for r in reads] == [
      ("items", "span-items"),
      ("tables", "span-tables"),
  ]
  assert reads[0]["sql"] == f"SELECT * FROM `{dataset}.analyst_memory_items`"
  assert export_memory.memory_table_reads(calls, None) == []
  # A query the guard refused named the dataset but read nothing.
  assert export_memory.memory_table_attempts(calls, dataset) == [
      {
          "purpose": "refused",
          "sql": f"SELECT * FROM `{dataset}.analyst_memory_items`",
          "span_id": "span-refused",
          "error": REFUSED,
      }
  ]


def _control_queries_memory(rows, result):
  """The control's restaurant search becomes a query of the memory dataset."""
  _row(rows, "sp-201-llm-1", "LLM_RESPONSE")["content"][
      "response"
  ] = "call: save_preference | call: run_sql"
  _row(rows, "sp-201-tool-2", "TOOL_STARTING")["content"] = {
      "tool": "run_sql",
      "args": {
          "purpose": "Look for what is known about this user",
          "sql": "SELECT * FROM `p.d.analyst_memory_items`",
      },
  }
  _row(rows, "sp-201-tool-2", "TOOL_COMPLETED")["content"] = {
      "tool": "run_sql",
      "result": result,
  }


@pytest.mark.parametrize(
    "result, read",
    [
        ({"status": "ok", "rows": [{"statement": "Ana is vegetarian."}]}, True),
        ({"status": "error", "message": REFUSED}, False),
    ],
    ids=["returned-rows", "refused"],
)
def test_a_control_is_flawed_only_if_its_memory_query_returned_rows(
    fixture, result, read
):
  rows = copy.deepcopy(fixture.rows)
  _control_queries_memory(rows, result)
  memories = {
      "u-ana": _memory(rows, "u-ana"),
      "u-ben": _memory(rows, "u-ben"),
  }

  (pair,) = export_memory.build_comparisons(
      RECORD, memories, {}, memory_dataset="d"
  )

  side = pair["without_memory"]
  assert side["tool_calls"] == ["save_preference", "run_sql"]
  assert pair["control_read_memory"] is read
  assert [q["purpose"] for q in side["memory_table_reads"]] == (
      ["Look for what is known about this user"] if read else []
  )
  assert [q.get("error") for q in side["memory_table_attempts"]] == (
      [] if read else [REFUSED]
  )


def test_hide_project_rewrites_only_the_project_in_memory_table_reads():
  sql = "SELECT p.x FROM `my-proj.d.items` p JOIN `my-proj-2.d.t` USING (x)"
  pairs = [
      {
          "with_memory": None,
          "without_memory": {
              "memory_table_reads": [{"sql": sql}, {"sql": None}]
          },
      }
  ]

  export_memory.hide_project(pairs, "my-proj")

  assert pairs[0]["without_memory"]["memory_table_reads"] == [
      {
          "sql": (
              "SELECT p.x FROM `<project>.d.items` p JOIN `my-proj-2.d.t`"
              " USING (x)"
          )
      },
      {"sql": None},
  ]


def test_run_totals_come_from_the_record_and_the_export(fixture, ana):
  run = export_memory.build_run(RECORD, [ana])

  assert (run["model"], run["days"], run["consolidation"]) == (
      "gemini-x",
      RECORD["days"],
      [{"day": 1, "messages": 3, "facts": 2}],
  )
  assert run["totals"] == {
      "analysts": 2,
      "sessions": 2,
      "control_sessions": 1,
      "turns": 3,
      "rows": 25,
      "model_calls": 9,
      "tool_calls": sum(t["tool_call_count"] for t in ana["traces"]),
      "sql_queries": 0,
      "sql_errors": 0,
      "recalls": 0,
      "facts": 0,
      "entities": len(ana["entities"]),
      "preference_versions": len(ana["preferences"]),
  }


class _LiveFake:
  """list_traces from the fixture; consolidation reads from canned rows."""

  def __init__(self, rows, items):
    self._traces = offline_bigquery.OfflineBigQueryClient(rows)
    self._items = items
    self.item_queries = []

  def query(self, sql, job_config=None, **kwargs):
    if "_memory_items`" in sql:
      self.item_queries.append([p.value for p in job_config.query_parameters])
      return SimpleNamespace(result=lambda: self._items)
    return self._traces.query(sql, job_config=job_config, **kwargs)


def test_cli_live_reads_extracted_items_and_the_run_record(fixture, tmp_path):
  record = tmp_path / "live_run.json"
  record.write_text(json.dumps(RECORD), encoding="utf-8")
  items = [
      {
          "kind": "fact",
          "session_id": "s-101",
          "span_id": "sp-101-inv",
          "observed_at": "2026-09-28T17:00:00Z",
          "subject": "Ana",
          "subject_type": "PERSON",
          "predicate": "follows_diet",
          "object": "vegetarian",
          "object_type": "VALUE",
          "statement": "Ana is vegetarian.",
      }
  ]
  fake = _LiveFake(fixture.rows, items)
  out = tmp_path / "export.json"

  code = export_memory.main(
      [
          "--project-id",
          "p",
          "--dataset-id",
          "d",
          "--memory-tables",
          "analyst_",
          "--run-record",
          str(record),
          "--now",
          "2026-10-06T16:00:00Z",
          "--out",
          str(out),
      ],
      bq_client=fake,
  )

  export = json.loads(out.read_text())
  assert code == 0
  assert export["label"] == "Recorded live run: TheLook analyst week"
  assert [u["user_id"] for u in export["users"]] == ["u-ana", "u-ben"]
  assert [f["statement"] for f in export["users"][0]["facts"]] == [
      "Ana is vegetarian."
  ]
  assert fake.item_queries == [["u-ana"], ["u-ben"], ["u-ben"]]
  assert len(export["comparisons"]) == 1
  assert export["run"]["totals"]["facts"] == 2


def test_cli_rejects_memory_tables_offline(capsys):
  with pytest.raises(SystemExit):
    export_memory.main(["--memory-tables", "analyst_"])

  assert "need --project-id" in capsys.readouterr().err


class _Ancestors(HTMLParser):
  """Records the open elements around the element with a given id."""

  VOID = {"meta", "link", "br", "img", "input", "hr", "source", "wbr"}

  def __init__(self, element_id):
    super().__init__()
    self._id = element_id
    self._open = []
    self.ancestors = None

  def handle_starttag(self, tag, attrs):
    attrs = dict(attrs)
    if attrs.get("id") == self._id:
      self.ancestors = list(self._open)
    if tag not in self.VOID:
      self._open.append(attrs)

  def handle_endtag(self, tag):
    if tag not in self.VOID:
      self._open.pop()


def test_the_tooltip_takes_the_theme_and_survives_a_load_error():
  # The theme's colors are custom properties on .viz-root, and the notice
  # for a failed load replaces the children of #app.
  parser = _Ancestors("tooltip")
  parser.feed((EXAMPLE_DIR / "viz" / "index.html").read_text("utf-8"))

  classes = [a.get("class", "") for a in parser.ancestors]
  ids = [a.get("id") for a in parser.ancestors]
  assert "viz-root" in " ".join(classes).split()
  assert "app" not in ids


def test_the_committed_export_matches_the_committed_run_record():
  export = json.loads(
      (EXAMPLE_DIR / "viz" / "data" / "memory_export.json").read_text("utf-8")
  )
  record = json.loads(
      (EXAMPLE_DIR / "recorded_run" / "live_run.json").read_text("utf-8")
  )
  memory_sessions = [s for s in record["sessions"] if s["memory"] == "on"]

  assert export["schema"] == export_memory.SCHEMA
  assert "test-project" not in json.dumps(export)  # shown as <project>
  assert [u["user_id"] for u in export["users"]] == [
      a["user_id"] for a in record["analysts"]
  ]
  assert sorted(
      s["session_id"] for u in export["users"] for s in u["sessions"]
  ) == sorted(s["session_id"] for s in memory_sessions)
  assert [
      (c["with_memory"]["session_id"], c["without_memory"]["session_id"])
      for c in export["comparisons"]
  ] == [(c["with_memory"], c["without_memory"]) for c in record["comparisons"]]
  assert export["run"]["totals"]["sessions"] == len(memory_sessions)
  assert export["run"]["totals"]["rows"] == sum(
      r["row_count"] for r in record["row_counts"]
  )
  # Each tool bar has its call's status, and an answered trace with a
  # failed call is "answered with errors".
  for user in export["users"]:
    for trace in user["traces"]:
      calls = [c for step in trace["steps"] for c in step["tool_calls"]]
      bars = {r["span_id"]: r for r in trace["timeline"] if r["kind"] == "tool"}
      assert {c["span_id"]: c["status"] for c in calls} == {
          span: bar["status"] for span, bar in bars.items()
      }
      failed = [c["error"] for c in calls if c["status"] == "error"]
      assert all(error in trace["errors"] for error in failed)
      if failed and trace["outcome_status"] != "unanswered":
        assert trace["outcome_status"] == "answered_with_errors"
  # Flagged exactly when the run without memory read the memory dataset:
  # a query of it that returned rows. Failed queries are only listed.
  for pair in export["comparisons"]:
    assert pair["control_read_memory"] == bool(
        pair["without_memory"]["memory_table_reads"]
    )
    assert all(
        "error" not in q for q in pair["without_memory"]["memory_table_reads"]
    )
    assert all(
        q.get("error") for q in pair["without_memory"]["memory_table_attempts"]
    )
    assert pair["with_memory"]["memory_table_reads"] == []
    assert pair["with_memory"]["memory_table_attempts"] == []
