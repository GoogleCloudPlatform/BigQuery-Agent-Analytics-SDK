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
"""Tests for ``examples/evalbench_synth_from_traces.py`` (#435 slice 5, #97).

The synthesizer's mapping is exercised on a tiny in-memory event list shaped
like ``agent_events`` rows; nothing here reaches BigQuery. The synthesized
rows are then fed through ``EvalBenchRun.to_agent_event_rows`` /
``to_score_rows`` to prove the importer accepts what the synthesizer emits.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json

import pytest

from bigquery_agent_analytics.evalbench import EvalBenchRun
from examples import evalbench_synth_from_traces as synth

_T0 = datetime(2026, 7, 27, 20, 30, 39, tzinfo=timezone.utc)
_SESSION_DONE = "29ae300e-986c-42b9-83ec-8d38898f6062"
_SESSION_STUCK = "7e352c34-4c1c-4395-acd5-fb3c8f215346"
_SESSION_NO_PROMPT = "ffffffff-0000-0000-0000-000000000000"


def _event(session_id, event_type, content, *, offset=0, status="OK", **extra):
  row = {
      "timestamp": _T0.replace(second=39 + offset),
      "event_type": event_type,
      "agent": "support_agent",
      "session_id": session_id,
      "content": content,
      "attributes": {
          "adk": {"app_name": "bqaa-e2e", "schema_version": "1"},
          "root_agent_name": "support_agent",
      },
      "status": status,
      "error_message": None,
  }
  row.update(extra)
  return row


def _content(value):
  return json.loads(value) if isinstance(value, str) else value


def _events():
  return [
      # A completed session with two tool calls; the final text lives on
      # AGENT_RESPONSE because the plugin logs AGENT_COMPLETED with no content.
      _event(
          _SESSION_DONE,
          "USER_MESSAGE_RECEIVED",
          {"text_summary": "Check inventory for widget and doohickey."},
      ),
      _event(
          _SESSION_DONE, "AGENT_STARTING", "You are a support agent.", offset=1
      ),
      _event(
          _SESSION_DONE,
          "TOOL_STARTING",
          {"tool": "check_inventory", "args": {"item": "widget"}},
          offset=2,
      ),
      _event(
          _SESSION_DONE,
          "TOOL_COMPLETED",
          {"tool": "check_inventory", "result": {"in_stock": 42}},
          offset=2,
      ),
      _event(
          _SESSION_DONE,
          "TOOL_STARTING",
          {"tool": "check_inventory", "args": {"item": "doohickey"}},
          offset=3,
      ),
      _event(
          _SESSION_DONE,
          "TOOL_ERROR",
          {"tool": "check_inventory", "result": None},
          offset=3,
          status="ERROR",
          error_message="inventory backend timed out",
      ),
      _event(
          _SESSION_DONE,
          "AGENT_RESPONSE",
          {"response": "text: 'There are 42 widgets in stock.'"},
          offset=4,
      ),
      # content as a JSON *string*, as TO_JSON_STRING(content) returns it.
      _event(_SESSION_DONE, "AGENT_COMPLETED", "null", offset=4),
      # A session that never completed: only the prompt and AGENT_STARTING.
      _event(
          _SESSION_STUCK,
          "USER_MESSAGE_RECEIVED",
          json.dumps({"text_summary": "How many widgets are in stock?"}),
      ),
      _event(
          _SESSION_STUCK, "AGENT_STARTING", "You are a support agent.", offset=1
      ),
      # A session with no user prompt at all: must be skipped, never invented.
      _event(_SESSION_NO_PROMPT, "AGENT_STARTING", "You are a support agent."),
      _event(_SESSION_NO_PROMPT, "AGENT_COMPLETED", None, offset=1),
  ]


def test_one_result_per_prompted_session_with_real_text() -> None:
  tables = synth.synthesize(
      _events(), job_id="mvp-e2e-real-traces", source_table="p.d.agent_events"
  )
  assert [r["id"] for r in tables.results] == ["29ae300e", "7e352c34"]
  assert tables.skipped_sessions == [_SESSION_NO_PROMPT]

  done, stuck = tables.results
  assert done["eval_id"] == done["id"] == "29ae300e"
  assert done["job_id"] == "mvp-e2e-real-traces"
  assert done["prompt"] == done["nl_prompt"]
  assert done["prompt"] == "Check inventory for widget and doohickey."
  assert done["final_response"] == "text: 'There are 42 widgets in stock.'"
  assert done["stdout"] == done["final_response"]
  assert done["returncode"] == 0
  assert done["run_time"] == _T0
  assert done["source_session_id"] == _SESSION_DONE
  assert done["source_table"] == "p.d.agent_events"
  assert done["error_message"] == "inventory backend timed out"
  tool_calls = json.loads(done["tool_calls"])
  assert [c["tool_name"] for c in tool_calls] == [
      "check_inventory",
      "check_inventory",
  ]
  assert tool_calls[0]["args"] == {"item": "widget"}
  assert tool_calls[0]["result"] == {"in_stock": 42}
  assert tool_calls[0].get("error") is None
  assert tool_calls[1]["args"] == {"item": "doohickey"}
  assert tool_calls[1]["error"] == "inventory backend timed out"

  assert stuck["prompt"] == "How many widgets are in stock?"
  assert stuck["returncode"] == 1
  assert "final_response" not in stuck
  assert "stdout" not in stuck
  assert stuck["tool_calls"] == "[]"


def test_scores_and_configs_follow_completion() -> None:
  tables = synth.synthesize(
      _events(), job_id="mvp-e2e-real-traces", source_table="p.d.agent_events"
  )
  assert tables.scores == [
      {
          "job_id": "mvp-e2e-real-traces",
          "id": "29ae300e",
          "eval_id": "29ae300e",
          "comparator": "goal_completion",
          "score": 1.0,
          "run_time": _T0,
          "source_session_id": _SESSION_DONE,
      },
      {
          "job_id": "mvp-e2e-real-traces",
          "id": "7e352c34",
          "eval_id": "7e352c34",
          "comparator": "goal_completion",
          "score": 0.0,
          "run_time": _T0,
          "source_session_id": _SESSION_STUCK,
      },
  ]
  configs = {row["config"]: row["value"] for row in tables.configs}
  assert configs == {
      "experiment_config.orchestrator": "support_agent",
      "model_config.generator": "bqaa-e2e",
      "bqaa.source_table": "p.d.agent_events",
  }
  assert {row["job_id"] for row in tables.configs} == {"mvp-e2e-real-traces"}
  # The earliest prompt timestamp, not "now": the rows are deterministic so
  # a re-run of the synthesizer reproduces the importer's fingerprints.
  assert {row["run_time"] for row in tables.configs} == {_T0}


def test_synthesized_rows_round_trip_through_the_importer() -> None:
  tables = synth.synthesize(
      _events(), job_id="mvp-e2e-real-traces", source_table="p.d.agent_events"
  )
  run = EvalBenchRun(
      project_id="p",
      evalbench_dataset="bqaa_evalbench_mvp_demo",
      job_id="mvp-e2e-real-traces",
      results=tuple(tables.results),
      scores=tuple(tables.scores),
      config_rows=tuple(tables.configs),
  )
  rows = run.to_agent_event_rows(import_version="v1")
  by_session: dict[str, list[str]] = {}
  for row in rows:
    by_session.setdefault(row["session_id"], []).append(row["event_type"])
  done = "evalbench-import:mvp-e2e-real-traces:v1:29ae300e"
  stuck = "evalbench-import:mvp-e2e-real-traces:v1:7e352c34"
  assert by_session == {
      done: [
          "USER_MESSAGE_RECEIVED",
          "TOOL_STARTING",
          "TOOL_COMPLETED",
          "TOOL_STARTING",
          "TOOL_ERROR",
          "AGENT_COMPLETED",
      ],
      stuck: ["USER_MESSAGE_RECEIVED"],
  }
  prompts = {
      row["session_id"]: _content(row["content"])["text"]
      for row in rows
      if row["event_type"] == "USER_MESSAGE_RECEIVED"
  }
  assert prompts == {
      done: "Check inventory for widget and doohickey.",
      stuck: "How many widgets are in stock?",
  }
  assert {row["agent"] for row in rows} == {"evalbench:support_agent:bqaa-e2e"}

  score_rows = run.to_score_rows(import_version="v1")
  assert [
      (r["session_id"], r["comparator"], r["score"]) for r in score_rows
  ] == [
      (done, "goal_completion", 1.0),
      (stuck, "goal_completion", 0.0),
  ]


def test_short_scenario_ids_fall_back_to_full_session_id_on_collision() -> None:
  events = [
      _event("29ae300e-aaaa", "USER_MESSAGE_RECEIVED", {"text_summary": "a"}),
      _event("29ae300e-bbbb", "USER_MESSAGE_RECEIVED", {"text_summary": "b"}),
  ]
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert [r["id"] for r in tables.results] == ["29ae300e-aaaa", "29ae300e-bbbb"]


def test_prompt_prefers_text_summary_then_text_and_rejects_blank() -> None:
  events = [
      _event("s1", "USER_MESSAGE_RECEIVED", {"text": "from text field"}),
      _event("s2", "USER_MESSAGE_RECEIVED", {"text_summary": "   "}),
      _event("s2", "AGENT_COMPLETED", None, offset=1),
  ]
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert [(r["id"], r["prompt"]) for r in tables.results] == [
      ("s1", "from text field")
  ]
  assert tables.skipped_sessions == ["s2"]


def test_no_prompted_sessions_is_an_error() -> None:
  with pytest.raises(ValueError, match="no session"):
    synth.synthesize(
        [_event("s1", "AGENT_STARTING", "x")], job_id="j", source_table="t"
    )


def test_target_dataset_must_differ_from_source() -> None:
  with pytest.raises(ValueError, match="must not"):
    synth.check_targets(
        source_table="p.bqaa_e2e_real.agent_events",
        evalbench_dataset="bqaa_e2e_real",
        mirror_dataset="bqaa_evalbench_mvp_mirror",
    )
  with pytest.raises(ValueError, match="must not"):
    synth.check_targets(
        source_table="p.bqaa_e2e_real.agent_events",
        evalbench_dataset="bqaa_evalbench_mvp_demo",
        mirror_dataset="agent_analytics",
    )
  with pytest.raises(ValueError, match="must differ"):
    synth.check_targets(
        source_table="p.bqaa_e2e_real.agent_events",
        evalbench_dataset="same",
        mirror_dataset="same",
    )
  synth.check_targets(
      source_table="p.bqaa_e2e_real.agent_events",
      evalbench_dataset="bqaa_evalbench_mvp_demo",
      mirror_dataset="bqaa_evalbench_mvp_mirror",
  )
