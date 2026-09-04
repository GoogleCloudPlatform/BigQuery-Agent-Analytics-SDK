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

from bigquery_agent_analytics.evalbench import classify_sessions
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
      # LLM_RESPONSE (the SDK contract) because the ADK plugin logs
      # AGENT_COMPLETED with no content.
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
          "LLM_RESPONSE",
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
  assert done["source_user_id"] is None
  assert done["source_root_agent_name"] == "support_agent"
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
          "source_user_id": None,
          "source_root_agent_name": "support_agent",
      },
      {
          "job_id": "mvp-e2e-real-traces",
          "id": "7e352c34",
          "eval_id": "7e352c34",
          "comparator": "goal_completion",
          "score": 0.0,
          "run_time": _T0,
          "source_session_id": _SESSION_STUCK,
          "source_user_id": None,
          "source_root_agent_name": "support_agent",
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
  with pytest.raises(ValueError, match="no trace"):
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


def test_llm_response_is_the_final_response_when_completed_has_no_content() -> (
    None
):
  # Codex P1 on PR #455: the ADK plugin emits USER_MESSAGE_RECEIVED ->
  # LLM_RESPONSE -> AGENT_COMPLETED(no content). LLM_RESPONSE is the SDK's
  # response event (event_semantics.RESPONSE_EVENT_TYPES), so its text must
  # become final_response instead of a phantom "completed but never answered".
  events = [
      _event("s1", "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
      _event("s1", "LLM_RESPONSE", {"response": "first draft"}, offset=1),
      _event("s1", "LLM_RESPONSE", {"response": "hello there"}, offset=2),
      _event("s1", "AGENT_COMPLETED", None, offset=3),
  ]
  tables = synth.synthesize(events, job_id="j", source_table="t")
  (row,) = tables.results
  assert row["returncode"] == 0
  assert row["final_response"] == "hello there"
  assert row["stdout"] == "hello there"
  run = EvalBenchRun(
      project_id="p",
      evalbench_dataset="d",
      job_id="j",
      results=tuple(tables.results),
      scores=tuple(tables.scores),
      config_rows=tuple(tables.configs),
  )
  event_types = [
      r["event_type"] for r in run.to_agent_event_rows(import_version="v1")
  ]
  assert event_types == ["USER_MESSAGE_RECEIVED", "AGENT_COMPLETED"]


def test_agent_response_remains_a_compatibility_alias() -> None:
  events = [
      _event("s1", "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
      _event("s1", "AGENT_RESPONSE", {"response": "legacy name"}, offset=1),
  ]
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert tables.results[0]["final_response"] == "legacy name"
  assert tables.results[0]["returncode"] == 1


def test_reused_session_id_across_identities_is_never_spliced() -> None:
  # Codex P1 on PR #455: BQAA trace identity is (session_id, user_id,
  # root_agent_name). Two traces sharing a session_id must become two
  # scenarios, each with its own prompt and response, never one row with
  # user A's prompt and user B's answer.
  def trace(user_id, root_agent_name, prompt, answer, offset):
    attrs = {
        "adk": {"app_name": "bqaa-e2e"},
        "root_agent_name": root_agent_name,
    }
    return [
        _event(
            "reused",
            "USER_MESSAGE_RECEIVED",
            {"text_summary": prompt},
            offset=offset,
            user_id=user_id,
            attributes=attrs,
        ),
        _event(
            "reused",
            "LLM_RESPONSE",
            {"response": answer},
            offset=offset + 1,
            user_id=user_id,
            attributes=attrs,
        ),
        _event(
            "reused",
            "AGENT_COMPLETED",
            None,
            offset=offset + 2,
            user_id=user_id,
            attributes=attrs,
        ),
    ]

  events = (
      trace("user-a", "support_agent", "prompt A", "answer A", 0)
      + trace("user-b", "support_agent", "prompt B", "answer B", 4)
      # Same user, different root agent: still a distinct trace.
      + trace("user-a", "billing_agent", "prompt C", "answer C", 8)
  )
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert tables.skipped_sessions == []
  by_id = {
      r["id"]: (
          r["prompt"],
          r["final_response"],
          r["source_user_id"],
          r["source_root_agent_name"],
      )
      for r in tables.results
  }
  assert by_id == {
      "reused:user-a:support_agent": (
          "prompt A",
          "answer A",
          "user-a",
          "support_agent",
      ),
      "reused:user-b:support_agent": (
          "prompt B",
          "answer B",
          "user-b",
          "support_agent",
      ),
      "reused:user-a:billing_agent": (
          "prompt C",
          "answer C",
          "user-a",
          "billing_agent",
      ),
  }
  assert {r["source_session_id"] for r in tables.results} == {"reused"}
  # Scores carry the same identity as results, one per trace.
  assert sorted(
      (s["id"], s["source_user_id"], s["source_root_agent_name"])
      for s in tables.scores
  ) == sorted(
      (r["id"], r["source_user_id"], r["source_root_agent_name"])
      for r in tables.results
  )
  # The three traces stay distinct through the importer as well.
  run = EvalBenchRun(
      project_id="p",
      evalbench_dataset="d",
      job_id="j",
      results=tuple(tables.results),
      scores=tuple(tables.scores),
      config_rows=tuple(tables.configs),
  )
  sessions = {
      r["session_id"] for r in run.to_agent_event_rows(import_version="v1")
  }
  assert len(sessions) == 3


def test_top_level_root_agent_name_column_wins_over_attributes() -> None:
  # The reader query projects root_agent_name from attributes; rows that
  # carry the column already use it as-is.
  events = [
      _event(
          "s1",
          "USER_MESSAGE_RECEIVED",
          {"text_summary": "a"},
          root_agent_name="from_column",
      ),
  ]
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert tables.results[0]["source_root_agent_name"] == "from_column"


class _NeverQueriedClient:
  """A fake BigQuery client that fails the test if any SQL reaches it."""

  def __init__(self):
    self.queries = []

  def query(self, query, **kwargs):  # pragma: no cover - must not run
    self.queries.append(query)
    raise AssertionError(f"client.query must not be reached: {query!r}")


_HOSTILE_TABLES = [
    "d.t` WHERE FALSE; SELECT 1; --",
    "d.t`; DROP TABLE x; --",
    "d.t -- comment",
    "d.`t`",
    "d.t;",
    "d. t",
    "d.t\n",
    "d.t.u.v",
    "t",
    "",
]


@pytest.mark.parametrize("table", _HOSTILE_TABLES)
def test_hostile_source_table_fails_before_client_query(table) -> None:
  # Codex P1 on PR #455: --source-table is interpolated into SQL between
  # backticks, so every segment must satisfy the repository's identifier
  # policy (^[A-Za-z0-9_-]+$) before any statement is formatted.
  client = _NeverQueriedClient()
  with pytest.raises(ValueError):
    synth.load_events(client, source_table=table, location="US")
  assert client.queries == []
  with pytest.raises(ValueError):
    synth._qualify("p", table)


def test_hostile_identifiers_fail_before_the_client_is_created(
    monkeypatch,
) -> None:
  from google.cloud import bigquery

  def _no_client(*args, **kwargs):  # pragma: no cover - must not run
    raise AssertionError("bigquery.Client must not be created")

  monkeypatch.setattr(bigquery, "Client", _no_client)
  base = ["--project", "p", "--dry-run"]
  hostile = "d.t` WHERE FALSE; SELECT 1; --"
  with pytest.raises(ValueError, match="--source-table table"):
    synth.main(base + ["--source-table", hostile])
  with pytest.raises(ValueError, match="--source-table dataset"):
    synth.main(base + ["--source-table", "bad dataset.t"])
  with pytest.raises(ValueError, match="--source-table project"):
    synth.main(base + ["--source-table", "pro`ject.d.t"])
  with pytest.raises(ValueError, match="--project"):
    synth.main(["--project", "p;", "--dry-run", "--source-table", "d.t"])
  with pytest.raises(ValueError, match="--evalbench-dataset"):
    synth.main(base + ["--source-table", "d.t", "--evalbench-dataset", "x`y"])
  with pytest.raises(ValueError, match="--mirror-dataset"):
    synth.main(base + ["--source-table", "d.t", "--mirror-dataset", "x;y"])


def test_qualify_accepts_plain_identifiers() -> None:
  assert synth._qualify("p", "d.t") == "p.d.t"
  assert synth._qualify("my-proj", "bqaa_e2e_real.agent_events") == (
      "my-proj.bqaa_e2e_real.agent_events"
  )
  assert synth._qualify("p", "other-proj.d.t") == "other-proj.d.t"


def _trace(session_id, user_id, root_agent_name, prompt, answer, offset):
  """One prompt → LLM_RESPONSE → AGENT_COMPLETED trace with exact identity."""
  attrs = {"adk": {"app_name": "bqaa-e2e"}, "root_agent_name": root_agent_name}
  return [
      _event(
          session_id,
          "USER_MESSAGE_RECEIVED",
          {"text_summary": prompt},
          offset=offset,
          user_id=user_id,
          root_agent_name=root_agent_name,
          attributes=attrs,
      ),
      _event(
          session_id,
          "LLM_RESPONSE",
          {"response": answer},
          offset=offset + 1,
          user_id=user_id,
          root_agent_name=root_agent_name,
          attributes=attrs,
      ),
      _event(
          session_id,
          "AGENT_COMPLETED",
          None,
          offset=offset + 2,
          user_id=user_id,
          root_agent_name=root_agent_name,
          attributes=attrs,
      ),
  ]


def _imported_sessions(tables):
  run = EvalBenchRun(
      project_id="p",
      evalbench_dataset="d",
      job_id="j",
      results=tuple(tables.results),
      scores=tuple(tables.scores),
      config_rows=tuple(tables.configs),
  )
  return {r["session_id"] for r in run.to_agent_event_rows(import_version="v1")}


def test_null_and_empty_identity_dimensions_are_distinct_traces() -> None:
  # Codex P1 (round 2) on PR #455: the SDK's TraceIdentity distinguishes
  # SQL NULL from the empty string. user_id=None and user_id="" on the
  # same session/root agent are two traces; neither prompt may be paired
  # with the other's answer.
  events = _trace("shared", None, "support_agent", "prompt N", "answer N", 0)
  events += _trace("shared", "", "support_agent", "prompt E", "answer E", 4)
  events += _trace("shared", "user-a", None, "prompt R", "answer R", 8)
  events += _trace("shared", "user-a", "", "prompt S", "answer S", 12)
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert tables.skipped_sessions == []
  by_id = {
      r["id"]: (
          r["prompt"],
          r["final_response"],
          r["source_user_id"],
          r["source_root_agent_name"],
      )
      for r in tables.results
  }
  assert by_id == {
      "shared:~:support_agent": ("prompt N", "answer N", None, "support_agent"),
      "shared::support_agent": ("prompt E", "answer E", "", "support_agent"),
      "shared:user-a:~": ("prompt R", "answer R", "user-a", None),
      "shared:user-a:": ("prompt S", "answer S", "user-a", ""),
  }
  assert sorted(
      (s["id"], s["source_user_id"], s["source_root_agent_name"])
      for s in tables.scores
  ) == sorted(
      (r["id"], r["source_user_id"], r["source_root_agent_name"])
      for r in tables.results
  )
  assert len(_imported_sessions(tables)) == 4


def test_whitespace_distinct_identities_stay_separate() -> None:
  # Exact strings: " user-a" / "user-a " / "user-a" are three users, and
  # "sess" / "sess " are two sessions. Nothing in the identity is stripped.
  events = _trace("sess", "user-a", "support_agent", "p1", "a1", 0)
  events += _trace("sess", " user-a", "support_agent", "p2", "a2", 4)
  events += _trace("sess", "user-a ", "support_agent", "p3", "a3", 8)
  events += _trace("sess ", "user-a", "support_agent", "p4", "a4", 12)
  events += _trace("sess", "user-a", " support_agent", "p5", "a5", 16)
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert tables.skipped_sessions == []
  assert sorted(
      (r["source_session_id"], r["source_user_id"], r["source_root_agent_name"])
      for r in tables.results
  ) == [
      ("sess", " user-a", "support_agent"),
      ("sess", "user-a", " support_agent"),
      ("sess", "user-a", "support_agent"),
      ("sess", "user-a ", "support_agent"),
      ("sess ", "user-a", "support_agent"),
  ]
  assert {(r["prompt"], r["final_response"]) for r in tables.results} == {
      ("p1", "a1"),
      ("p2", "a2"),
      ("p3", "a3"),
      ("p4", "a4"),
      ("p5", "a5"),
  }
  assert len({r["id"] for r in tables.results}) == 5
  assert len(_imported_sessions(tables)) == 5


def test_scenario_ids_are_injective_for_delimiter_bearing_identities() -> None:
  # Codex P1 (round 2) on PR #455: a raw ":" join mapped ("shared","a:b","c")
  # and ("shared","a","b:c") to the same id and the synthesizer aborted.
  # Components are percent-escaped, so boundaries survive and NULL ("~")
  # differs from "" and from a literal "~".
  events = _trace("shared", "a:b", "c", "p1", "a1", 0)
  events += _trace("shared", "a", "b:c", "p2", "a2", 3)
  events += _trace("shared", "a%3Ab", "c", "p3", "a3", 6)
  events += _trace("shared", "~", "c", "p4", "a4", 9)
  events += _trace("shared", None, "c", "p5", "a5", 12)
  events += _trace("shared:a", "b", "c", "p6", "a6", 15)
  tables = synth.synthesize(events, job_id="j", source_table="t")
  assert tables.skipped_sessions == []
  by_id = {r["id"]: (r["prompt"], r["final_response"]) for r in tables.results}
  assert by_id == {
      "shared:a%3Ab:c": ("p1", "a1"),
      "shared:a:b%3Ac": ("p2", "a2"),
      "shared:a%253Ab:c": ("p3", "a3"),
      "shared:%7E:c": ("p4", "a4"),
      "shared:~:c": ("p5", "a5"),
      "shared%3Aa:b:c": ("p6", "a6"),
  }
  assert len(_imported_sessions(tables)) == 6


def test_scenario_id_encoding_is_deterministic_and_injective() -> None:
  keys = [
      ("s", "a:b", "c"),
      ("s", "a", "b:c"),
      ("s", None, ""),
      ("s", "", None),
      ("s", "~", "%"),
      ("s", "%7E", "%25"),
      ("", None, None),
      (" ", None, None),
  ]
  ids = synth._scenario_ids(keys)
  assert len(set(ids.values())) == len(keys)
  assert ids == synth._scenario_ids(list(reversed(keys)))
  assert ids[("s", None, "")] == "s:~:"
  assert ids[("s", "", None)] == "s::~"
  assert ids[("", None, None)] == ":~:~"
  assert all(value.strip() for value in ids.values())


def test_completed_trace_with_source_error_stays_a_failed_session() -> None:
  # Codex P1 (round 3) on PR #455: a trace can reach AGENT_COMPLETED with
  # status=ERROR / error_message and no TOOL_ERROR at all. The synthesizer
  # used to write that failure only to results.error_message, which the
  # importer does not read, so the session round-tripped as status=OK and
  # the failed-sessions view reported process_failed=false.
  events = [
      _event("s1", "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
      _event("s1", "LLM_RESPONSE", {"response": "partial answer"}, offset=1),
      _event(
          "s1",
          "AGENT_COMPLETED",
          None,
          offset=2,
          status="ERROR",
          error_message="model call failed: 503",
      ),
  ]
  tables = synth.synthesize(events, job_id="j", source_table="t")
  (result,) = tables.results
  # Completion and goal_completion stay tied to AGENT_COMPLETED ...
  assert result["returncode"] == 0
  assert tables.scores[0]["score"] == 1.0
  # ... but the failure is emitted in an importer-recognized field and kept
  # verbatim for provenance.
  assert result["error"] == "model call failed: 503"
  assert result["error_message"] == "model call failed: 503"
  assert "error" in [name for name, _, _ in synth._RESULT_SCHEMA]

  run = EvalBenchRun(
      project_id="p",
      evalbench_dataset="d",
      job_id="j",
      results=tuple(tables.results),
      scores=tuple(tables.scores),
      config_rows=tuple(tables.configs),
  )
  rows = run.to_agent_event_rows(import_version="v1")
  assert [r["event_type"] for r in rows] == [
      "USER_MESSAGE_RECEIVED",
      "AGENT_COMPLETED",
  ]
  terminal = rows[-1]
  assert terminal["status"] == "ERROR"
  assert "model call failed: 503" in terminal["error_message"]
  (verdict,) = classify_sessions(rows, run.to_score_rows(import_version="v1"))
  assert verdict.process_failed is True
  assert verdict.missing_completion is False


def test_tool_error_alone_does_not_mark_the_completion_row_as_error() -> None:
  # A recovered tool error is already a TOOL_ERROR row (status=ERROR); it
  # must not also be promoted to a session-level ``error``.
  tables = synth.synthesize(_events(), job_id="j", source_table="t")
  done = next(
      r for r in tables.results if r["source_session_id"] == _SESSION_DONE
  )
  assert done["error"] is None
  assert done["error_message"] == "inventory backend timed out"


def test_clean_completed_trace_has_no_error_field() -> None:
  events = [
      _event("s1", "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
      _event("s1", "AGENT_COMPLETED", {"text": "done"}, offset=1),
  ]
  (result,) = synth.synthesize(events, job_id="j", source_table="t").results
  assert result["error"] is None
  assert result["error_message"] is None


def test_non_tool_error_status_without_message_is_still_an_error() -> None:
  events = [
      _event("s1", "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
      _event("s1", "LLM_RESPONSE", {"response": "x"}, offset=1, status="ERROR"),
      _event("s1", "AGENT_COMPLETED", None, offset=2),
  ]
  (result,) = synth.synthesize(events, job_id="j", source_table="t").results
  assert result["returncode"] == 0
  assert result["error"] == "LLM_RESPONSE status=ERROR"
  assert result["error_message"] is None
