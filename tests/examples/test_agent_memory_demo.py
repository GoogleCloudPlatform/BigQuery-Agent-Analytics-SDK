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
from pathlib import Path
import sys

import pytest

from bigquery_agent_analytics import Client
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
    self.returned: list[list[dict]] = []
    self._inner = offline_bigquery.OfflineBigQueryClient(rows)

  def query(self, sql, job_config=None, **kwargs):
    self.statements.append(sql)
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
  assert "- Kyoto (LOCATION): 3 tool calls in s-102, s-103" in context
  assert (
      f'- 0.31 "{ANA_S103_TASK}" -> save_preference, find_restaurants ->'
      ' "Updated your diet to pescatarian. Kamo Grill in Kyoto has'
      ' pescatarian dinner options on Oct 15." [s-103/inv-103]' in context
  )
  assert (
      f"- search_hotels failed 1 of 2 calls; last error: {HOTEL_TIMEOUT}"
      " [s-102/sp-102-tool-1]" in context
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
