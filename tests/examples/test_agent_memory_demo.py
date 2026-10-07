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

"""Hermetic tests for examples/agent_memory (no network, no credentials).

The demo reads every memory layer through the real SDK ``Client.list_traces``
code path. BigQuery itself is replaced by ``OfflineBigQueryClient``, which
serves the committed SYNTHETIC fixture and rejects any statement or
predicate it does not implement. Expected values below are written out by
hand from the fixture rows, not computed with the code under test.
"""

from __future__ import annotations

import copy
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import re
import sys

from google.cloud import bigquery
import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import client as sdk_client
from bigquery_agent_analytics import TraceFilter

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "agent_memory"
sys.path.insert(0, str(EXAMPLE_DIR))

import agent_memory_demo  # noqa: E402
import memory_layers  # noqa: E402
import offline_bigquery  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 6, 16, 0, 0, tzinfo=UTC)
TRIP_ENTITY_ARGS = {
    "origin": "LOCATION",
    "destination": "LOCATION",
    "city": "LOCATION",
    "near": "LOCATION",
}
ANA_S101_TASK = (
    "I'm vegetarian and I prefer window seats. Find me a flight from SFO to"
    " Tokyo on Oct 12."
)
ANA_S103_TASK = (
    "I eat fish now, so update my diet to pescatarian. Find a restaurant in"
    " Kyoto for dinner on Oct 15."
)
ANA_S104_TASK = "Find a restaurant in Osaka for dinner that fits my diet."
HOTEL_TIMEOUT = (
    "TimeoutError: hotel inventory API did not respond within 5000 ms"
)
ANA_S101_ANSWER = (
    "Saved: vegetarian, window seat. DM101 leaves SFO at 11:05 on Oct 12 with"
    " 12 window seats left."
)
# google-adk 2.11 writes these only on a terminal (non-partial) LLM_RESPONSE.
TERMINAL_MARKERS = ("cache_type", "finish_reason")


def _ts(*args) -> datetime:
  return datetime(*args, tzinfo=UTC)


@pytest.fixture(scope="module")
def fixture() -> offline_bigquery.Fixture:
  return offline_bigquery.load_fixture()


def _client(rows) -> Client:
  return Client(
      project_id="offline-demo",
      dataset_id="agent_analytics",
      table_id="agent_events",
      verify_schema=False,
      bq_client=offline_bigquery.OfflineBigQueryClient(rows),
  )


class _RecordingClient:
  """Test double: records each statement and its rows, serves the fixture."""

  def __init__(self, rows):
    self.statements: list[str] = []
    self.configs: list = []
    self.returned: list[list[dict]] = []
    self._inner = offline_bigquery.OfflineBigQueryClient(rows)

  def query(self, sql, job_config=None, **kwargs):
    self.statements.append(sql)
    self.configs.append(job_config)
    job = self._inner.query(sql, job_config=job_config, **kwargs)
    self.returned.append(job.result())
    return job


@pytest.fixture()
def client(fixture) -> Client:
  return _client(fixture.rows)


@pytest.fixture()
def ana(client) -> memory_layers.UserMemory:
  return memory_layers.load_user_memory(
      client,
      "u-ana",
      since=_ts(2026, 9, 6, 16, 0, 0),
      entity_args=TRIP_ENTITY_ARGS,
  )


# ---- the committed fixture --------------------------------------------------

AGENT_EVENTS_COLUMNS = {
    "timestamp",
    "event_type",
    "agent",
    "session_id",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "content",
    "content_parts",
    "attributes",
    "latency_ms",
    "status",
    "error_message",
    "is_truncated",
}


def test_fixture_rows_carry_every_agent_events_column(fixture):
  assert len(fixture.rows) == 65
  assert fixture.now == NOW
  for row in fixture.rows:
    assert set(row) == AGENT_EVENTS_COLUMNS
    assert row["timestamp"].tzinfo is not None


def test_fixture_payloads_use_the_plugin_keys(fixture):
  # Keys the ADK BigQueryAgentAnalyticsPlugin writes for each event type.
  expected_content_keys = {
      "USER_MESSAGE_RECEIVED": {"text_summary"},
      "LLM_RESPONSE": {"response", "usage"},
      "TOOL_STARTING": {"tool", "args", "tool_origin"},
      "TOOL_COMPLETED": {"tool", "result", "tool_origin"},
      "TOOL_ERROR": {"tool", "args", "tool_origin"},
  }
  for row in fixture.rows:
    keys = expected_content_keys.get(row["event_type"])
    if keys is not None:
      assert set(row["content"]) == keys, row["span_id"]
    if row["event_type"] == "STATE_DELTA":
      assert row["content"] is None
      assert isinstance(row["attributes"]["state_delta"], dict)
    if row["event_type"] == "TOOL_ERROR":
      assert row["status"] == "ERROR" and row["error_message"]


# ---- the offline BigQuery stand-in ------------------------------------------


def test_offline_client_scopes_list_traces_to_the_pinned_user(client):
  traces = client.list_traces(TraceFilter(user_id="u-ana"))

  assert sorted(t.session_id for t in traces) == [
      "s-101",
      "s-102",
      "s-103",
      "s-104",
  ]
  assert {t.user_id for t in traces} == {"u-ana"}


def test_offline_client_anchors_sessions_then_returns_all_their_rows(client):
  # Only s-103's last rows are at or after 18:00:02, but the SDK query
  # anchors the session on them and then fetches every row of it.
  traces = client.list_traces(
      TraceFilter(user_id="u-ana", start_time=_ts(2026, 10, 4, 18, 0, 2))
  )

  assert [t.session_id for t in traces] == ["s-104", "s-103"]
  s103 = traces[1]
  assert len(s103.spans) == 14
  assert s103.spans[1].event_type == "USER_MESSAGE_RECEIVED"


def test_offline_client_keeps_the_newest_sessions_up_to_the_limit(client):
  traces = client.list_traces(TraceFilter(user_id="u-ana", limit=2))

  assert [t.session_id for t in traces] == ["s-104", "s-103"]


def test_offline_client_applies_the_sql_limit_to_sessions(fixture):
  # The SDK also trims to the limit in Python, so check the rows the
  # stand-in itself returned for the statement.
  recorder = _RecordingClient(fixture.rows)
  client = Client(
      project_id="offline-demo",
      dataset_id="agent_analytics",
      verify_schema=False,
      bq_client=recorder,
  )

  client.list_traces(TraceFilter(user_id="u-ana", limit=1))

  assert len(recorder.returned) == 1
  assert {row["session_id"] for row in recorder.returned[0]} == {"s-104"}


def test_offline_client_without_user_pin_returns_every_user(client):
  traces = client.list_traces(TraceFilter(start_time=_ts(2026, 10, 2, 0, 0, 0)))

  assert sorted(t.session_id for t in traces) == ["s-103", "s-104", "s-201"]


@pytest.mark.parametrize(
    "filt",
    [
        TraceFilter(user_id="u-ana", has_error=True),
        TraceFilter(user_id="u-ana", agent_id="trip_planner"),
        TraceFilter(user_id="u-ana", session_ids=["s-101"]),
        TraceFilter(user_id="u-ana", experiment_id="exp-1"),
    ],
)
def test_offline_client_rejects_predicates_it_cannot_honor(client, filt):
  with pytest.raises(NotImplementedError, match="offline fixture"):
    client.list_traces(filt)


def test_offline_client_rejects_other_statements(client):
  with pytest.raises(NotImplementedError, match="Client.list_traces"):
    client.get_trace("t-101")


def test_offline_client_pins_the_sdk_statement():
  # If the SDK changes its list-traces query, the stand-in stops serving it;
  # update the pinned copy and the row semantics in query() together.
  assert (
      offline_bigquery._LIST_TRACES_STATEMENT == sdk_client._LIST_TRACES_QUERY
  )


def _list_traces_call(fixture):
  """The statement and job config the SDK sends for one user's traces."""
  recorder = _RecordingClient(fixture.rows)
  Client(
      project_id="offline-demo",
      dataset_id="agent_analytics",
      verify_schema=False,
      bq_client=recorder,
  ).list_traces(TraceFilter(user_id="u-ana"))
  return recorder.statements[0], recorder.configs[0]


def _replace_once(old, new):
  def mutate(sql):
    assert old in sql
    return sql.replace(old, new, 1)

  return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        lambda sql: sql + "LIMIT 0\n",
        _replace_once(
            "JOIN trace_sessions ts\n  ON e.session_id",
            "JOIN trace_sessions ts\n  ON FALSE AND e.session_id",
        ),
        _replace_once("  e.error_message,\n", ""),
        _replace_once("e.timestamp ASC", "e.timestamp DESC"),
        _replace_once("LIMIT @trace_limit", "LIMIT 1000"),
        _replace_once(".agent_events` e\n", ".other_events` e\n"),
    ],
    ids=[
        "outer-limit-0",
        "join-on-false",
        "dropped-column",
        "order",
        "limit",
        "rows-from-another-table",
    ],
)
def test_offline_client_rejects_any_other_statement_shape(fixture, mutate):
  # Each variant changes which rows a real BigQuery would return, so the
  # stand-in must refuse it rather than answer with the supported shape.
  sql, config = _list_traces_call(fixture)
  stand_in = offline_bigquery.OfflineBigQueryClient(fixture.rows)

  assert len(stand_in.query(sql, job_config=config).result()) == 51
  with pytest.raises(NotImplementedError, match="statement"):
    stand_in.query(mutate(sql), job_config=config)


def test_offline_client_rejects_unexpected_query_parameters(fixture):
  sql, config = _list_traces_call(fixture)
  config.query_parameters = list(config.query_parameters) + [
      bigquery.ScalarQueryParameter("agent_id", "STRING", "trip_planner")
  ]

  with pytest.raises(NotImplementedError, match="parameters"):
    offline_bigquery.OfflineBigQueryClient(fixture.rows).query(
        sql, job_config=config
    )


# ---- short-term memory ------------------------------------------------------


def test_conversation_keeps_user_and_assistant_text_in_order(ana):
  conversation = ana.short_term.get_conversation("s-101")

  assert [(m.role, m.content) for m in conversation] == [
      ("user", ANA_S101_TASK),
      ("assistant", "Noted. Saving your preferences first."),
      (
          "assistant",
          "Saved: vegetarian, window seat. DM101 leaves SFO at 11:05 on Oct"
          " 12 with 12 window seats left.",
      ),
  ]
  assert conversation[0].timestamp == _ts(2026, 9, 28, 17, 0, 0, 100000)
  assert conversation[0].span_id == "sp-101-inv"
  assert conversation[2].span_id == "sp-101-llm-3"


def test_conversation_of_an_unknown_session_raises(ana):
  with pytest.raises(KeyError, match="s-201"):
    ana.short_term.get_conversation("s-201")


def test_list_sessions_is_newest_first_with_message_counts(ana):
  sessions = ana.short_term.list_sessions()

  assert [
      (s.session_id, s.message_count, s.created_at, s.updated_at)
      for s in sessions
  ] == [
      (
          "s-104",
          1,
          _ts(2026, 10, 6, 15, 59, 50),
          _ts(2026, 10, 6, 15, 59, 50, 200000),
      ),
      (
          "s-103",
          2,
          _ts(2026, 10, 4, 18, 0, 0),
          _ts(2026, 10, 4, 18, 0, 3, 285000),
      ),
      (
          "s-102",
          2,
          _ts(2026, 10, 1, 9, 30, 0),
          _ts(2026, 10, 1, 9, 30, 9, 323000),
      ),
      (
          "s-101",
          3,
          _ts(2026, 9, 28, 17, 0, 0),
          _ts(2026, 9, 28, 17, 0, 4, 680000),
      ),
  ]
  assert sessions[0].first_message_preview == ANA_S104_TASK


# ---- long-term memory -------------------------------------------------------


def test_preference_history_versions_only_user_scoped_state(ana):
  history = ana.long_term.get_preference_history()

  assert [
      (
          p.category,
          p.preference,
          p.valid_from,
          p.valid_until,
          p.session_id,
          p.span_id,
      )
      for p in history
  ] == [
      (
          "diet",
          "vegetarian",
          _ts(2026, 9, 28, 17, 0, 1, 600000),
          _ts(2026, 10, 4, 18, 0, 1, 445000),
          "s-101",
          "sp-101-agent",
      ),
      (
          "seat",
          "window",
          _ts(2026, 9, 28, 17, 0, 1, 600000),
          None,
          "s-101",
          "sp-101-agent",
      ),
      (
          "diet",
          "pescatarian",
          _ts(2026, 10, 4, 18, 0, 1, 445000),
          None,
          "s-103",
          "sp-103-agent",
      ),
  ]
  # The session-scoped key from s-102 ("last_hotel_search") is not
  # long-term memory.
  assert {p.category for p in history} == {"diet", "seat"}


@pytest.mark.parametrize(
    "as_of,expected",
    [
        (_ts(2026, 9, 28, 17, 0, 1), {}),
        (
            _ts(2026, 10, 2, 0, 0, 0),
            {"diet": "vegetarian", "seat": "window"},
        ),
        # Boundary: the new version is valid from its own timestamp on.
        (
            _ts(2026, 10, 4, 18, 0, 1, 445000),
            {"diet": "pescatarian", "seat": "window"},
        ),
        (None, {"diet": "pescatarian", "seat": "window"}),
    ],
)
def test_preferences_as_of_return_the_value_valid_then(ana, as_of, expected):
  current = ana.long_term.get_preferences(as_of=as_of)

  assert {k: p.preference for k, p in current.items()} == expected


def test_entities_come_from_declared_tool_arguments(ana):
  entities = ana.long_term.get_entities()

  assert [
      (e.name, e.entity_type, len(e.mentions), e.sessions) for e in entities
  ] == [
      ("Kyoto", "LOCATION", 3, ("s-102", "s-103")),
      ("Kyoto Station", "LOCATION", 2, ("s-102",)),
      ("SFO", "LOCATION", 1, ("s-101",)),
      ("Tokyo", "LOCATION", 1, ("s-101",)),
  ]
  kyoto = entities[0]
  assert [(m.tool_name, m.span_id) for m in kyoto.mentions] == [
      ("search_hotels", "sp-102-tool-1"),
      ("search_hotels", "sp-102-tool-2"),
      ("find_restaurants", "sp-103-tool-2"),
  ]
  assert kyoto.last_seen == _ts(2026, 10, 4, 18, 0, 1, 475000)


def test_entities_need_an_argument_mapping(client):
  memory = memory_layers.load_user_memory(client, "u-ana")

  assert memory.long_term.get_entities() == []


# ---- reasoning memory -------------------------------------------------------


def test_trace_with_steps_records_a_failed_then_retried_tool_call(ana):
  trace = ana.reasoning.get_trace_with_steps("inv-102")

  assert trace.session_id == "s-102"
  assert (
      trace.task == "Book a hotel in Kyoto near Kyoto Station for Oct 14 to 16."
  )
  assert [(s.step_number, s.action) for s in trace.steps] == [
      (1, "call: search_hotels"),
      (2, "call: search_hotels"),
  ]
  failed, retried = trace.steps[0].tool_calls[0], trace.steps[1].tool_calls[0]
  assert (failed.status, failed.duration_ms, failed.error, failed.result) == (
      "error",
      5003,
      HOTEL_TIMEOUT,
      None,
  )
  assert failed.arguments == {
      "city": "Kyoto",
      "near": "Kyoto Station",
      "check_in": "2026-10-14",
      "check_out": "2026-10-16",
  }
  assert (
      trace.steps[0].observation == f"search_hotels -> error: {HOTEL_TIMEOUT}"
  )
  assert (retried.status, retried.duration_ms, retried.span_id) == (
      "success",
      820,
      "sp-102-tool-2",
  )
  assert trace.outcome == (
      "Sakura Station Hotel is 150 m from Kyoto Station at $180 per night."
      " Shall I book it?"
  )
  assert trace.outcome_status == "answered_with_errors"
  assert trace.success is False
  assert trace.metrics == {
      "latency_ms": pytest.approx(9323.0),
      "llm_calls": 3,
      "tool_calls": 2,
      "tool_errors": 1,
      "total_tokens": 1540,
  }
  assert trace.completed_at == _ts(2026, 10, 1, 9, 30, 9, 323000)


def test_trace_groups_one_model_turns_tool_calls_into_one_step(ana):
  trace = ana.reasoning.get_trace_with_steps("inv-101")

  first, second = trace.steps
  assert first.thought == "Noted. Saving your preferences first."
  assert first.action == "call: save_preference, save_preference"
  assert [(c.tool_name, c.arguments, c.status) for c in first.tool_calls] == [
      ("save_preference", {"key": "diet", "value": "vegetarian"}, "success"),
      ("save_preference", {"key": "seat", "value": "window"}, "success"),
  ]
  assert first.observation == (
      'save_preference -> {"status": "saved", "key": "diet"}; '
      'save_preference -> {"status": "saved", "key": "seat"}'
  )
  assert second.thought is None
  assert [c.tool_name for c in second.tool_calls] == ["search_flights"]
  assert trace.outcome_status == "answered"
  assert trace.success is True
  assert trace.metrics["total_tokens"] == 1512


def test_in_progress_invocation_is_unanswered(ana):
  trace = ana.reasoning.get_trace_with_steps("inv-104")

  assert trace.task == ANA_S104_TASK
  assert trace.steps == ()
  assert trace.outcome is None
  assert trace.outcome_status == "unanswered"
  assert trace.completed_at is None


def test_cut_off_invocation_is_unanswered_with_a_pending_call(fixture):
  # Keep s-101 only up to the first TOOL_STARTING: the last model turn has
  # text and tool calls, and the first tool call never completed.
  cutoff = _ts(2026, 9, 28, 17, 0, 1, 500000)
  rows = [
      r
      for r in fixture.rows
      if r["session_id"] != "s-101" or r["timestamp"] <= cutoff
  ]
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  trace = memory.reasoning.get_trace_with_steps("inv-101")

  assert trace.outcome is None
  assert trace.outcome_status == "unanswered"
  (step,) = trace.steps
  assert step.thought == "Noted. Saving your preferences first."
  assert [(c.tool_name, c.status, c.duration_ms) for c in step.tool_calls] == [
      ("save_preference", "pending", None)
  ]
  assert step.observation == "save_preference -> pending"
  # A pending call counts toward total_calls but is neither a success nor a
  # failure (the other save_preference call is s-103's).
  (stats,) = memory.reasoning.get_tool_stats("save_preference")
  assert (
      stats.total_calls,
      stats.successful_calls,
      stats.failed_calls,
      stats.success_rate,
  ) == (2, 1, 0, 0.5)


def _row(rows, span_id, event_type):
  return next(
      r
      for r in rows
      if r["span_id"] == span_id and r["event_type"] == event_type
  )


def _fragment(row, text, at, total):
  """A streaming chunk of ``row``'s model call: same span, no terminal marker."""
  chunk = copy.deepcopy(row)
  chunk["timestamp"] = at
  chunk["content"] = {
      "response": f"text: '{text}'",
      "usage": {"prompt": 540, "completion": 5, "total": total},
  }
  for key in TERMINAL_MARKERS:
    chunk["attributes"].pop(key)
  return chunk


def _moved_to_s104(row, span_id, at):
  moved = copy.deepcopy(row)
  moved.update(
      session_id="s-104",
      invocation_id="inv-104",
      trace_id="t-104",
      span_id=span_id,
      parent_span_id="sp-104-agent",
      timestamp=at,
  )
  return moved


def _interrupted_s104(fixture):
  """s-104 plus a model call that streamed one fragment and stopped."""
  rows = copy.deepcopy(fixture.rows)
  request = _moved_to_s104(
      _row(rows, "sp-103-llm-1", "LLM_REQUEST"),
      "sp-104-llm-1",
      _ts(2026, 10, 6, 15, 59, 50, 300000),
  )
  request["content"] = {"prompt": [{"role": "user", "content": ANA_S104_TASK}]}
  chunk = _fragment(
      _moved_to_s104(
          _row(rows, "sp-103-llm-2", "LLM_RESPONSE"),
          "sp-104-llm-1",
          _ts(2026, 10, 6, 15, 59, 51),
      ),
      "Dotonbori",
      _ts(2026, 10, 6, 15, 59, 51),
      120,
  )
  return rows + [request, chunk]


def test_a_completed_stream_is_one_model_call_and_one_answer(fixture):
  rows = copy.deepcopy(fixture.rows)
  final = _row(rows, "sp-101-llm-3", "LLM_RESPONSE")
  rows += [
      _fragment(
          final,
          "Saved: vegetarian, window seat. ",
          _ts(2026, 9, 28, 17, 0, 3, 900000),
          550,
      ),
      _fragment(
          final,
          "DM101 leaves SFO at 11:05 on Oct 12 with 12 window seats left.",
          _ts(2026, 9, 28, 17, 0, 4, 200000),
          560,
      ),
  ]
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  conversation = memory.short_term.get_conversation("s-101")
  trace = memory.reasoning.get_trace_with_steps("inv-101")

  assert [(m.role, m.content, m.complete) for m in conversation[1:]] == [
      ("assistant", "Noted. Saving your preferences first.", True),
      ("assistant", ANA_S101_ANSWER, True),
  ]
  assert (trace.outcome, trace.outcome_status) == (ANA_S101_ANSWER, "answered")
  assert len(trace.steps) == 2
  # One model call per span, and cumulative usage is not added up twice.
  assert (trace.metrics["llm_calls"], trace.metrics["total_tokens"]) == (
      3,
      1512,
  )


def test_an_interrupted_stream_is_not_an_answer(fixture):
  memory = memory_layers.load_user_memory(
      _client(_interrupted_s104(fixture)), "u-ana"
  )

  trace = memory.reasoning.get_trace_with_steps("inv-104")
  conversation = memory.short_term.get_conversation("s-104")

  assert (
      trace.outcome,
      trace.outcome_status,
      trace.success,
      trace.completed_at,
  ) == (None, "unanswered", False, None)
  assert trace.metrics["llm_calls"] == 1
  successful = memory.reasoning.list_traces(success_only=True)
  assert "inv-104" not in [t.trace_id for t in successful]
  assert [(m.role, m.content, m.complete) for m in conversation] == [
      ("user", ANA_S104_TASK, True),
      ("assistant", "Dotonbori", False),
  ]
  context = memory.get_context(ANA_S104_TASK, session_id="s-104")
  assert "- assistant (incomplete): Dotonbori [s-104/sp-104-llm-1]" in context


def test_a_stream_cut_off_by_an_error_is_not_an_answer(fixture):
  rows = _interrupted_s104(fixture)
  error = _moved_to_s104(
      _row(rows, "sp-102-tool-1", "TOOL_ERROR"),
      "sp-104-inv",
      _ts(2026, 10, 6, 15, 59, 51, 500000),
  )
  error.update(
      event_type="INVOCATION_ERROR",
      content=None,
      error_message="stream reset by peer",
  )
  memory = memory_layers.load_user_memory(_client(rows + [error]), "u-ana")

  trace = memory.reasoning.get_trace_with_steps("inv-104")

  assert (trace.outcome_status, trace.errors) == (
      "unanswered",
      ("stream reset by peer",),
  )


def test_rows_without_terminal_markers_use_the_next_row_as_evidence(fixture):
  # Older plugin versions write no terminal marker; a non-error row after
  # the response (a tool call, the next model call, AGENT_COMPLETED) shows
  # that the model call finished.
  rows = copy.deepcopy(fixture.rows)
  for row in rows:
    if row["event_type"] == "LLM_RESPONSE":
      for key in TERMINAL_MARKERS:
        row["attributes"].pop(key)
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  assert sorted(
      (t.trace_id, t.outcome_status) for t in memory.reasoning.list_traces()
  ) == [
      ("inv-101", "answered"),
      ("inv-102", "answered_with_errors"),
      ("inv-103", "answered"),
      ("inv-104", "unanswered"),
  ]


def test_a_terminal_response_answers_before_the_closing_rows_land(fixture):
  # A reader can see the final LLM_RESPONSE before AGENT_COMPLETED and
  # INVOCATION_COMPLETED are written; its terminal marker shows that the
  # model call finished.
  closing = ("AGENT_COMPLETED", "INVOCATION_COMPLETED")
  rows = [
      row
      for row in fixture.rows
      if row["session_id"] != "s-101" or row["event_type"] not in closing
  ]
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  trace = memory.reasoning.get_trace_with_steps("inv-101")

  assert (trace.outcome, trace.outcome_status, trace.completed_at) == (
      ANA_S101_ANSWER,
      "answered",
      None,
  )
  assert memory.short_term.get_conversation("s-101")[-1].complete


@pytest.mark.parametrize(
    "response,expected",
    [
        (
            "text: 'The log format is text: message | call: tool_name.'",
            (["The log format is text: message | call: tool_name."], []),
        ),
        (
            "text: 'I'm here, it's done' | call: save_preference",
            (["I'm here, it's done"], ["save_preference"]),
        ),
        (
            "call: save_preference | call: find_restaurants",
            ([], ["save_preference", "find_restaurants"]),
        ),
        # Two readings fit; with no tool evidence the text stays whole.
        (
            "text: 'a' | call: b | text: 'c'",
            (["a' | call: b | text: 'c"], []),
        ),
        (
            "plain text from another producer",
            (["plain text from another producer"], []),
        ),
        # Text that only starts like a part: "the front desk" is no tool.
        ("call: the front desk", (["call: the front desk"], [])),
        # Over-long payloads as google-adk 2.11 stores them: cut, marked,
        # and left without the closing quote.
        (
            "text: 'Press '1' | call: rebook_flight t...[TRUNCATED]",
            (["Press '1' | call: rebook_flight t...[TRUNCATED]"], []),
        ),
        (
            "call: save_preference | text: 'Saved. Kamo Gr...[TRUNCATED]",
            (["Saved. Kamo Gr...[TRUNCATED]"], ["save_preference"]),
        ),
        ("None", ([], [])),
    ],
)
def test_response_parts_reads_the_plugin_format_structurally(
    response, expected
):
  assert memory_layers.response_parts(response) == expected


def test_response_parts_uses_recorded_tool_calls_for_ambiguous_text():
  assert memory_layers.response_parts(
      "text: 'a' | call: b | text: 'c'", executed_tools=["b"]
  ) == (["a", "c"], ["b"])


def test_a_quote_closes_text_only_before_a_separator():
  # After '1' comes " - ", not " | ", so no part can end there, even
  # though the recorded calls would favour splitting.
  assert memory_layers.response_parts(
      "text: 'Press '1' - call: rebook | text: 'ok' | call: rebook",
      executed_tools=["rebook", "rebook"],
  ) == (["Press '1' - call: rebook | text: 'ok"], ["rebook"])


@pytest.mark.parametrize(
    "tail,last_text",
    [
        ("", []),
        (" | text: 'and then...[TRUNCATED]", ["and then...[TRUNCATED]"]),
    ],
    ids=["complete", "truncated"],
)
def test_recorded_tool_calls_split_a_long_response_in_full(tail, last_text):
  # Twelve text parts can be read thousands of ways; the recorded calls
  # still select the reading that splits every part.
  steps = " | ".join(f"text: 'step {i}' | call: tool_{i}" for i in range(12))
  tools = [f"tool_{i}" for i in range(12)]

  assert memory_layers.response_parts(steps + tail, executed_tools=tools) == (
      [f"step {i}" for i in range(12)] + last_text,
      tools,
  )


def test_without_recorded_calls_a_long_ambiguous_text_stays_whole():
  response = "text: 'a' | call: b | " * 12 + "text: 'end'"

  assert memory_layers.response_parts(response) == (
      [response[len("text: '") : -1]],
      [],
  )


def test_crafted_text_cannot_stall_the_reader():
  # Every split of this text dead-ends at the malformed call, so trying
  # them all would take 2**1999 steps. The text comes back whole.
  crafted = "text: 'x' | " * 2000 + "call: not a tool"

  assert memory_layers.response_parts(crafted, executed_tools=[]) == (
      [crafted[len("text: '") :]],
      [],
  )


def test_literal_delimiters_in_an_answer_do_not_invent_a_tool_call(fixture):
  rows = copy.deepcopy(fixture.rows)
  answer = (
      "Log lines look like text: message | call: tool_name. Kamo Grill fits."
  )
  final = _row(rows, "sp-103-llm-2", "LLM_RESPONSE")
  final["content"]["response"] = f"text: '{answer}'"
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  trace = memory.reasoning.get_trace_with_steps("inv-103")

  assert (trace.outcome, trace.outcome_status) == (answer, "answered")
  assert [s.action for s in trace.steps] == [
      "call: save_preference, find_restaurants"
  ]


def test_recorded_tool_calls_resolve_an_ambiguous_model_turn(fixture):
  # This text reads two ways; the TOOL_STARTING rows that follow show that
  # both calls were real.
  rows = copy.deepcopy(fixture.rows)
  _row(rows, "sp-103-llm-1", "LLM_RESPONSE")["content"]["response"] = (
      "text: 'Saving it' | call: save_preference | text: 'then searching'"
      " | call: find_restaurants"
  )
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  (step,) = memory.reasoning.get_trace_with_steps("inv-103").steps

  assert (step.thought, step.action) == (
      "Saving it then searching",
      "call: save_preference, find_restaurants",
  )


def test_each_invocation_of_a_session_is_its_own_trace(fixture):
  # Replay s-103 as a second turn of s-101: same session, new invocation.
  rows = copy.deepcopy(fixture.rows)
  for row in rows:
    if row["session_id"] == "s-103":
      row["session_id"] = "s-101"
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  traces = memory.reasoning.get_session_traces("s-101")

  assert [(t.trace_id, t.task) for t in traces] == [
      ("inv-101", ANA_S101_TASK),
      ("inv-103", ANA_S103_TASK),
  ]
  assert len(memory.short_term.get_conversation("s-101")) == 5


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, ["inv-104", "inv-103", "inv-102", "inv-101"]),
        ({"success_only": True}, ["inv-103", "inv-101"]),
        ({"success_only": False}, ["inv-104", "inv-102"]),
        (
            {"since": _ts(2026, 10, 1, 0, 0, 0)},
            ["inv-104", "inv-103", "inv-102"],
        ),
        ({"until": _ts(2026, 10, 2, 0, 0, 0)}, ["inv-102", "inv-101"]),
    ],
)
def test_list_traces_filters_by_outcome_and_start_time(ana, kwargs, expected):
  traces = ana.reasoning.list_traces(**kwargs)

  assert [t.trace_id for t in traces] == expected


def test_similar_traces_rank_the_users_own_successful_tasks(ana):
  similar = ana.reasoning.get_similar_traces(
      ANA_S104_TASK, exclude_session_id="s-104"
  )

  # Tokens after stop words: {find, restaurant, osaka, dinner, fit, diet}
  # vs s-103's 11 tokens; they share {find, restaurant, dinner, diet}.
  assert [(s.trace.trace_id, s.similarity) for s in similar] == [
      ("inv-103", pytest.approx(4 / 13)),
  ]


def test_similar_traces_success_only_drops_traces_with_errors(ana):
  query = "Find a hotel near Kyoto Station"

  assert ana.reasoning.get_similar_traces(query) == []
  similar = ana.reasoning.get_similar_traces(query, success_only=False)
  assert [(s.trace.trace_id, s.similarity) for s in similar] == [
      ("inv-102", pytest.approx(4 / 9)),
  ]


def test_similar_traces_can_exclude_the_current_session(ana):
  similar = ana.reasoning.get_similar_traces(ANA_S103_TASK)

  assert [(s.trace.trace_id, s.similarity) for s in similar] == [
      ("inv-103", 1.0)
  ]
  assert (
      ana.reasoning.get_similar_traces(
          ANA_S103_TASK, exclude_session_id="s-103"
      )
      == []
  )


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("Find restaurants", "find a restaurant", 1.0),
        ("Book a hotel in Kyoto", "Find a restaurant in Osaka", 0.0),
        ("", "Find a restaurant", 0.0),
    ],
)
def test_lexical_similarity_ignores_case_stop_words_and_plurals(
    left, right, expected
):
  assert memory_layers.lexical_similarity(left, right) == expected


def test_tool_stats_count_failures_and_average_latency(ana):
  stats = ana.reasoning.get_tool_stats()

  assert [
      (
          s.name,
          s.total_calls,
          s.successful_calls,
          s.failed_calls,
          s.success_rate,
          s.avg_duration_ms,
          s.last_used_at,
      )
      for s in stats
  ] == [
      (
          "save_preference",
          3,
          3,
          0,
          1.0,
          pytest.approx(85 / 3),
          _ts(2026, 10, 4, 18, 0, 1, 400000),
      ),
      (
          "search_hotels",
          2,
          1,
          1,
          0.5,
          pytest.approx(2911.5),
          _ts(2026, 10, 1, 9, 30, 7, 203000),
      ),
      (
          "find_restaurants",
          1,
          1,
          0,
          1.0,
          pytest.approx(410.0),
          _ts(2026, 10, 4, 18, 0, 1, 475000),
      ),
      (
          "search_flights",
          1,
          1,
          0,
          1.0,
          pytest.approx(640.0),
          _ts(2026, 9, 28, 17, 0, 2, 580000),
      ),
  ]
  assert [s.name for s in ana.reasoning.get_tool_stats("search_hotels")] == [
      "search_hotels"
  ]


# ---- combined context and scoping -------------------------------------------


def test_context_combines_the_three_layers_with_provenance(ana):
  context = ana.get_context(ANA_S104_TASK, session_id="s-104")

  sections = [line for line in context.splitlines() if line.startswith("## ")]
  assert sections == [
      "## Short-term: current conversation (session s-104)",
      "## Long-term: user preferences (ADK user: state)",
      "## Long-term: entities the agent acted on",
      "## Reasoning: similar past tasks that succeeded",
      "## Reasoning: tools that failed before",
  ]
  assert f"- user: {ANA_S104_TASK} [s-104/sp-104-inv]" in context
  assert (
      "- diet = pescatarian (since 2026-10-04T18:00:01Z; replaced"
      " vegetarian) [s-103/sp-103-agent]" in context
  )
  assert (
      "- seat = window (since 2026-09-28T17:00:01Z) [s-101/sp-101-agent]"
      in context
  )
  assert (
      "- Kyoto (LOCATION): 3 tool calls [s-102/sp-102-tool-1,"
      " s-102/sp-102-tool-2, s-103/sp-103-tool-2]" in context
  )
  assert (
      f'- 0.31 trace inv-103: "{ANA_S103_TASK}" -> save_preference,'
      ' find_restaurants -> "Updated your diet to pescatarian. Kamo Grill in'
      ' Kyoto has pescatarian dinner options on Oct 15." [s-103/sp-103-llm-2]'
      in context
  )
  assert (
      f"- search_hotels failed 1 of 2 calls; last error: {HOTEL_TIMEOUT}"
      " [s-102/sp-102-tool-1]" in context
  )


# A session/span pair, optionally preceded by a count of older sources.
_SOURCE_TAG = re.compile(
    r" \[(\+\d+ earlier, )?[^\s/\]]+/[^\s,\]]+(, [^\s/\]]+/[^\s,\]]+)*\]$"
)


def test_every_context_line_names_its_source_rows(ana):
  context = ana.get_context(ANA_S104_TASK, session_id="s-104")

  facts = [
      line
      for line in context.splitlines()
      if line.startswith("- ") and line != "- (none)"
  ]
  # 1 message, 2 preferences, 4 entities, 1 similar task, 1 failed tool.
  assert len(facts) == 9
  assert [line for line in facts if not _SOURCE_TAG.search(line)] == []


def test_entity_context_keeps_the_latest_sources_within_max_items(ana):
  context = ana.get_context(ANA_S104_TASK, session_id="s-104", max_items=2)

  assert (
      "- Kyoto (LOCATION): 3 tool calls [+1 earlier, s-102/sp-102-tool-2,"
      " s-103/sp-103-tool-2]" in context
  )


def test_memory_never_includes_another_users_rows(ana):
  # Ben's session is the closest lexical match for Ana's task, and his diet
  # is stored under the same state key. Neither may leak into Ana's memory.
  context = ana.get_context(ANA_S104_TASK, session_id="s-104")

  assert {t.session_id for t in ana.traces} == {
      "s-101",
      "s-102",
      "s-103",
      "s-104",
  }
  assert "vegan" not in context
  assert "s-201" not in context
  assert [e.name for e in ana.long_term.get_entities()] == [
      "Kyoto",
      "Kyoto Station",
      "SFO",
      "Tokyo",
  ]


def test_a_session_id_shared_by_two_root_agents_fails_closed(fixture):
  rows = copy.deepcopy(fixture.rows)
  extra = copy.deepcopy(next(r for r in rows if r["span_id"] == "sp-101-inv"))
  extra["attributes"]["root_agent_name"] = "another_root_agent"
  rows.append(extra)
  memory = memory_layers.load_user_memory(_client(rows), "u-ana")

  with pytest.raises(ValueError, match="s-101"):
    memory.short_term.get_conversation("s-101")


# ---- the CLI ------------------------------------------------------------------


def test_cli_offline_prints_all_three_layers(capsys):
  assert agent_memory_demo.main([]) == 0
  out = capsys.readouterr().out

  for heading in (
      "== 1. Short-term memory ==",
      "== 2. Long-term memory ==",
      "== 3. Reasoning memory ==",
      "== 4. get_context() for the next model call ==",
  ):
    assert heading in out
  assert "offline fixture (synthetic rows)" in out
  # The demo inspects the most recent past trace that recorded a tool error.
  assert "Trace inv-102 (session s-102): answered_with_errors" in out
  assert f"search_hotels  error  5003 ms  {HOTEL_TIMEOUT}" in out
  assert "diet = pescatarian" in out
  assert "s-201" not in out


def test_cli_offline_other_user_sees_only_their_memory(capsys):
  argv = ["--user-id", "u-ben", "--session-id", "s-201"]

  assert agent_memory_demo.main(argv) == 0
  out = capsys.readouterr().out

  assert "diet = vegan" in out
  assert "pescatarian" not in out


def test_cli_live_mode_reads_the_named_table_once(fixture, capsys):
  recorder = _RecordingClient(fixture.rows)
  argv = [
      "--project-id",
      "my-project",
      "--dataset-id",
      "my_dataset",
      "--table-id",
      "my_events",
      "--user-id",
      "u-ana",
      "--session-id",
      "s-104",
      "--now",
      "2026-10-06T16:00:00Z",
  ]

  assert agent_memory_demo.main(argv, bq_client=recorder) == 0
  out = capsys.readouterr().out

  assert len(recorder.statements) == 1
  assert "`my-project.my_dataset.my_events`" in recorder.statements[0]
  assert "source : BigQuery my-project.my_dataset.my_events" in out
  assert "offline fixture" not in out


def test_cli_rejects_a_project_without_a_dataset(capsys):
  with pytest.raises(SystemExit):
    agent_memory_demo.main(["--project-id", "my-project"])


def test_cli_prints_a_zero_millisecond_average(fixture, tmp_path, capsys):
  # Live in-process tools often finish in 0 ms; that is a value, not "-".
  rows = copy.deepcopy(fixture.rows)
  for row in rows:
    if row["span_id"] == "sp-101-tool-3" and row["latency_ms"]:
      row["latency_ms"] = {"total_ms": 0}
  doc = {
      "description": "zero-latency variant",
      "now": "2026-10-06T16:00:00.000Z",
      "rows": [
          dict(row, timestamp=row["timestamp"].isoformat()) for row in rows
      ],
  }
  path = tmp_path / "agent_events.json"
  path.write_text(json.dumps(doc), encoding="utf-8")

  assert agent_memory_demo.main(["--fixture", str(path)]) == 0
  out = capsys.readouterr().out

  (line,) = [l for l in out.splitlines() if l.startswith("  search_flights ")]
  assert line.split()[-1] == "0.0"


def test_cli_marks_an_incomplete_reply(fixture, tmp_path, capsys):
  rows = _interrupted_s104(fixture)
  doc = {
      "description": "interrupted-stream variant",
      "now": "2026-10-06T16:00:00.000Z",
      "rows": [
          dict(row, timestamp=row["timestamp"].isoformat()) for row in rows
      ],
  }
  path = tmp_path / "agent_events.json"
  path.write_text(json.dumps(doc), encoding="utf-8")

  assert agent_memory_demo.main(["--fixture", str(path)]) == 0
  out = capsys.readouterr().out

  assert "  [assistant, incomplete] Dotonbori" in out
  assert "- assistant (incomplete): Dotonbori [s-104/sp-104-llm-1]" in out
