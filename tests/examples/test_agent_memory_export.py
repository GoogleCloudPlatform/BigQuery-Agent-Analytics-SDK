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
import json
from pathlib import Path
import sys

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
  assert export["schema"] == "bqaa-agent-memory-viz/1"
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
