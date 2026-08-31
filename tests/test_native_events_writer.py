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
"""Tests for the native ``agent_events`` snapshot writer (#463, parent #435).

Everything is offline: the acceptance widget-stock session is an in-memory
fixture shaped like production ADK ``agent_events`` rows, and publishing
runs against the fake BigQuery client of ``test_evalbench_importer``.
Nothing here reaches BigQuery, and nothing starts the six-week clock.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path

import pytest

from bigquery_agent_analytics import failure_taxonomy
from bigquery_agent_analytics.evalbench import classify_sessions
from bigquery_agent_analytics.evalbench import EvalScorePolicy
from bigquery_agent_analytics.evalbench import failed_sessions
from bigquery_agent_analytics.native_events import native_next_action
from bigquery_agent_analytics.native_events import NativeAgentEventsRun
from tests.test_evalbench_importer import _FakeQueryJob
from tests.test_evalbench_importer import _FakeWriteClient

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "examples" / "fixtures"

_T0 = datetime(2026, 7, 27, 20, 30, 39, tzinfo=timezone.utc)
_IMPORTED_AT = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

# The acceptance corpus (#463): production project/dataset the widget-stock
# evidence lives in, read-only.
_SOURCE_PROJECT = "test-project-0728-467323"
_SOURCE_TABLE = f"{_SOURCE_PROJECT}.bqaa_e2e_real.agent_events"
_JOB_ID = "mvp-e2e-real-traces"
_SESSION_STUCK = "7e352c34-4c1c-4395-acd5-fb3c8f215346"
# Only the first eight characters of the gold sibling are frozen evidence
# (``examples/fixtures/week0_real_rubric.json``); the suffix is fixture.
_SESSION_GOLD = "ab7535a5-1111-4111-8111-111111111111"
_PROMPT = "How many widgets are in stock?"
_GOLD_ANSWER = "There are 0 widgets in stock."

# The frozen Week 0 rubric gate: goal_completion >= 1.0, missing fails.
_POLICY = EvalScorePolicy({"goal_completion": 1.0}, missing_score_fails=True)


def _event(session_id, event_type, content, *, offset=0, status="OK", **extra):
  row = {
      "timestamp": _T0 + timedelta(seconds=offset),
      "event_type": event_type,
      "agent": "support_agent",
      "session_id": session_id,
      "user_id": "real-user-0",
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


def _stuck_events():
  # USER_MESSAGE_RECEIVED -> INVOCATION_STARTING -> AGENT_STARTING, then
  # silence: no check_inventory, no LLM_RESPONSE, no AGENT_COMPLETED.
  return [
      _event(
          _SESSION_STUCK,
          "USER_MESSAGE_RECEIVED",
          {"text_summary": _PROMPT},
      ),
      _event(_SESSION_STUCK, "INVOCATION_STARTING", None, offset=1),
      _event(
          _SESSION_STUCK,
          "AGENT_STARTING",
          "You are a support agent.",
          offset=2,
      ),
  ]


def _gold_events():
  # The completed sibling: asked the same question and answered it.
  return [
      _event(_SESSION_GOLD, "USER_MESSAGE_RECEIVED", {"text_summary": _PROMPT}),
      _event(
          _SESSION_GOLD,
          "TOOL_STARTING",
          {"tool": "check_inventory", "args": {"item": "widget"}},
          offset=1,
      ),
      _event(
          _SESSION_GOLD,
          "TOOL_COMPLETED",
          {"tool": "check_inventory", "result": {"in_stock": 0}},
          offset=2,
      ),
      _event(
          _SESSION_GOLD,
          "LLM_RESPONSE",
          {"response": _GOLD_ANSWER},
          offset=3,
      ),
      # content as a JSON string, as TO_JSON_STRING(content) returns it.
      _event(_SESSION_GOLD, "AGENT_COMPLETED", "null", offset=4),
  ]


def _acceptance_run(extra_events=()):
  return NativeAgentEventsRun.from_agent_events(
      _stuck_events() + _gold_events() + list(extra_events),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )


def _materialize(run, fake, *, import_version="v1", imported_at=_IMPORTED_AT):
  return run.materialize(
      target_dataset="bqaa_native",
      import_version=import_version,
      imported_at=imported_at,
      policy=_POLICY,
      bq_client=fake,
  )


# --- acceptance: widget-stock silence, no EvalBench tables anywhere -------


def test_widget_stock_session_fails_with_all_three_g1_names() -> None:
  fake = _FakeWriteClient()
  result = _materialize(_acceptance_run(), fake)
  assert result.status == "imported"

  listing = failed_sessions(
      target_project=_SOURCE_PROJECT,
      target_dataset="bqaa_native",
      job_id=_JOB_ID,
      policy=_POLICY,
      bq_client=fake,
  )
  assert listing.import_version == "v1"
  assert listing.session_count == 2
  assert listing.failed_count == 1
  (session,) = listing.sessions
  # The same object with or without the adapter: the real ADK session id
  # and the joinable first-8 identity.
  assert session.session_id == _SESSION_STUCK
  assert session.scenario_id == "7e352c34"
  assert session.process_failed is True
  assert session.missing_completion is True
  assert session.score_failed is True
  assert session.failed is True
  assert session.failing_scores == {"goal_completion": 0.0}
  # G1-frozen names (taxonomy v0.1.0), in frozen order, never flag order.
  assert failure_taxonomy.TAXONOMY_VERSION == "0.1.0"
  assert session.taxonomy_categories == (
      "task/planning",
      "finalization",
      "tool blockers",
  )


def test_punchline_next_action_names_the_missing_answer_and_tool() -> None:
  text = native_next_action(_stuck_events(), gold_events=_gold_events())
  assert "never answered" in text
  assert "never called check_inventory" in text
  # A completed session gets no failure punchline.
  done = native_next_action(_gold_events())
  assert "never answered" not in done
  assert "score policy" in done


def test_classify_sessions_agrees_without_a_publish() -> None:
  # The in-memory reference implementation sees the same three flags the
  # published view reports, straight from the mapped rows.
  run = _acceptance_run()
  verdicts = classify_sessions(
      run.to_agent_event_rows(import_version="v1"),
      run.to_score_rows(import_version="v1"),
      _POLICY,
  )
  by_session = {verdict.session_id: verdict for verdict in verdicts}
  stuck = by_session[_SESSION_STUCK]
  assert (
      stuck.process_failed,
      stuck.missing_completion,
      stuck.score_failed,
      stuck.failed,
  ) == (True, True, True, True)
  assert failure_taxonomy.categorize_failed_session(stuck) == (
      "task/planning",
      "finalization",
      "tool blockers",
  )
  gold = by_session[_SESSION_GOLD]
  assert gold.failed is False
  assert gold.scenario_id == "ab7535a5"


# --- pin identity ---------------------------------------------------------


def test_events_scores_and_manifest_all_carry_the_pin() -> None:
  fake = _FakeWriteClient()
  result = _materialize(_acceptance_run(), fake)
  pin = (_JOB_ID, "v1")
  assert result.event_row_count == len(fake.store.events) == 8
  assert result.score_row_count == len(fake.store.scores) == 2
  for row in fake.store.events + fake.store.scores:
    assert (row["job_id"], row["import_version"]) == pin
  (manifest,) = fake.store.rows
  assert (manifest["job_id"], manifest["import_version"]) == pin
  assert manifest["generation_id"]
  assert manifest["source_project"] == _SOURCE_PROJECT
  assert manifest["source_dataset"] == "bqaa_e2e_real"
  # Real ADK identities, not the adapter's versioned namespace: the pin
  # lives in the (job_id, import_version) columns.
  assert {row["session_id"] for row in fake.store.events} == {
      _SESSION_STUCK,
      _SESSION_GOLD,
  }


def test_view_is_pinned_to_the_latest_successful_native_publication() -> None:
  fake = _FakeWriteClient()
  _materialize(_acceptance_run(), fake)
  view_ref = f"{_SOURCE_PROJECT}.bqaa_native.evalbench_failed_sessions"
  body_v1 = fake.store.views[view_ref]
  assert body_v1.startswith("-- evalbench_failed_sessions pin: ")
  assert f'"{_JOB_ID}"' in body_v1
  assert '"v1"' in body_v1
  assert "@job_id" not in body_v1 and "@import_version" not in body_v1

  # A later successful publication (changed source, new version) moves the
  # view; the pin follows the newest committed manifest row.
  extra = [_event("99999999-aaaa", "USER_MESSAGE_RECEIVED", {"text": "hi"})]
  _materialize(
      _acceptance_run(extra),
      fake,
      import_version="v2",
      imported_at=_IMPORTED_AT + timedelta(hours=1),
  )
  body_v2 = fake.store.views[view_ref]
  assert '"v2"' in body_v2
  assert '"v1"' not in body_v2


def test_view_policy_pin_matches_the_frozen_week0_rubric() -> None:
  rubric = json.loads((_FIXTURE_DIR / "week0_real_rubric.json").read_text())
  assert rubric["clock_started"] is False
  assert rubric["session_id"] == _SESSION_STUCK
  assert rubric["eval_id"] == "7e352c34"
  assert rubric["gold"]["sibling_session"] == "ab7535a5"
  policy = EvalScorePolicy(
      {rubric["score"]["metric"]: float(rubric["score"]["threshold"])},
      missing_score_fails=True,
  )
  assert policy == _POLICY

  fake = _FakeWriteClient()
  _materialize(_acceptance_run(), fake)
  (manifest,) = fake.store.rows
  assert manifest["view_policy"] == json.dumps(
      {"min_scores": {"goal_completion": 1.0}, "missing_score_fails": True},
      sort_keys=True,
  )


# --- deterministic native scores ------------------------------------------


def test_scores_are_deterministic_from_the_session_no_judge() -> None:
  rows = _acceptance_run().to_score_rows(import_version="v1")
  assert [
      (row["session_id"], row["scenario_id"], row["comparator"], row["score"])
      for row in rows
  ] == [
      (_SESSION_STUCK, "7e352c34", "goal_completion", 0.0),
      (_SESSION_GOLD, "ab7535a5", "goal_completion", 1.0),
  ]
  stuck_row, gold_row = rows
  assert stuck_row["source_row"]["completed"] is False
  assert stuck_row["source_row"]["prompt"] == _PROMPT
  assert stuck_row["source_row"]["derived_from"] == _SOURCE_TABLE
  assert "score policy decides passed" in stuck_row["source_row"]["rule"]
  assert gold_row["source_row"]["completed"] is True


def test_never_completed_session_publishes_the_process_failure_marker() -> None:
  rows = _acceptance_run().to_agent_event_rows(import_version="v1")
  stuck_rows = [row for row in rows if row["session_id"] == _SESSION_STUCK]
  assert [row["event_type"] for row in stuck_rows] == [
      "USER_MESSAGE_RECEIVED",
      "INVOCATION_STARTING",
      "AGENT_STARTING",
  ]
  prompt_row = stuck_rows[0]
  # Same marker the adapter publishes for a failed returncode: the prompt
  # row goes out as ERROR, with the original status kept for provenance.
  assert prompt_row["status"] == "ERROR"
  assert "never logged AGENT_COMPLETED" in prompt_row["error_message"]
  assert prompt_row["attributes"]["bqaa_native_source_status"] == "OK"
  assert prompt_row["user_id"] == "real-user-0"
  assert prompt_row["agent"] == "support_agent"
  assert prompt_row["content"]["text_summary"] == _PROMPT
  assert prompt_row["attributes"]["evalbench_scenario_id"] == "7e352c34"
  assert prompt_row["attributes"]["experiment_id"] == _JOB_ID
  assert prompt_row["attributes"]["bqaa_native_source_table"] == _SOURCE_TABLE
  # The rest of the stuck session and the completed sibling stay verbatim.
  for row in stuck_rows[1:]:
    assert row["status"] == "OK"
    assert "bqaa_native_source_status" not in row["attributes"]
  for row in rows:
    if row["session_id"] == _SESSION_GOLD:
      assert row["status"] == "OK"
      assert row["error_message"] is None


def test_sessions_without_a_prompt_are_skipped_not_invented() -> None:
  promptless = [
      _event("dddddddd-0000", "AGENT_STARTING", "You are a support agent."),
      _event("dddddddd-0000", "AGENT_COMPLETED", None, offset=1),
  ]
  run = _acceptance_run(promptless)
  assert run.skipped_session_ids() == ("dddddddd-0000",)
  rows = run.to_agent_event_rows(import_version="v1")
  assert {row["session_id"] for row in rows} == {_SESSION_STUCK, _SESSION_GOLD}
  scores = run.to_score_rows(import_version="v1")
  assert {row["session_id"] for row in scores} == {
      _SESSION_STUCK,
      _SESSION_GOLD,
  }


# --- identity -------------------------------------------------------------


def test_identity_is_first_8_with_collision_fallback_to_full_id() -> None:
  colliding = [
      _event("29ae300e-aaaa", "USER_MESSAGE_RECEIVED", {"text_summary": "a"}),
      _event("29ae300e-bbbb", "USER_MESSAGE_RECEIVED", {"text_summary": "b"}),
  ]
  run = NativeAgentEventsRun.from_agent_events(
      _stuck_events() + colliding,
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  scores = run.to_score_rows(import_version="v1")
  assert {row["session_id"]: row["scenario_id"] for row in scores} == {
      _SESSION_STUCK: "7e352c34",
      "29ae300e-aaaa": "29ae300e-aaaa",
      "29ae300e-bbbb": "29ae300e-bbbb",
  }


# --- no EvalBench source tables on the native path ------------------------


def test_native_run_refuses_evalbench_source_rows() -> None:
  with pytest.raises(ValueError, match="no EvalBench source tables"):
    NativeAgentEventsRun(
        project_id=_SOURCE_PROJECT,
        evalbench_dataset="bqaa_e2e_real",
        job_id=_JOB_ID,
        results=({"eval_id": "x", "prompt": "y"},),
        source_table=_SOURCE_TABLE,
    )


class _FakeReadClient:
  """Read-only fake: records queries, returns the fixture rows once."""

  def __init__(self, rows) -> None:
    self.rows = list(rows)
    self.calls: list[tuple[str, dict]] = []

  def query(self, query: str, **kwargs) -> _FakeQueryJob:
    self.calls.append((query, kwargs))
    return _FakeQueryJob(self.rows)


def test_from_bigquery_reads_only_the_agent_events_table() -> None:
  fake = _FakeReadClient(_stuck_events() + _gold_events())
  run = NativeAgentEventsRun.from_bigquery(
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
      session_ids=[_SESSION_STUCK, _SESSION_GOLD],
      bq_client=fake,
  )
  ((query, kwargs),) = fake.calls
  assert query.startswith("SELECT")
  assert f"`{_SOURCE_TABLE}`" in query
  assert "session_id IN UNNEST(@session_ids)" in query
  for evalbench_source in (".configs`", ".results`", ".scores`"):
    assert evalbench_source not in query
  assert kwargs["job_config"].labels["sdk_feature"] == (
      "evalbench-native-import"
  )
  assert len(run.source_events) == 8
  assert run.results == () and run.scores == () and run.config_rows == ()


def test_publish_never_touches_the_source_dataset() -> None:
  fake = _FakeWriteClient()
  _materialize(_acceptance_run(), fake)
  for query, _ in fake.queries:
    assert "bqaa_e2e_real" not in query
  for destination, _, _ in fake.loads:
    assert "bqaa_e2e_real" not in destination


# --- production agent_events is never written -----------------------------


def test_reserved_agent_events_destination_is_rejected() -> None:
  run = _acceptance_run()
  with pytest.raises(ValueError, match="reserved ADK plugin table"):
    run.materialize(
        target_dataset="bqaa_native",
        events_table="agent_events",
        bq_client=_FakeWriteClient(),
    )


def test_nothing_is_written_into_an_agent_events_table() -> None:
  fake = _FakeWriteClient()
  _materialize(_acceptance_run(), fake)
  written = (
      [ref for ref, _, _ in fake.loads]
      + fake.created
      + [query for query, _ in fake.queries if not query.startswith("SELECT")]
  )
  for target in written:
    assert _SOURCE_TABLE not in target
    for segment in target.replace("`", " ").replace("\n", " ").split():
      assert not segment.endswith(".agent_events")


# --- the six-week clock does not start ------------------------------------


def test_native_publication_does_not_start_the_clock() -> None:
  fake = _FakeWriteClient()
  result = _materialize(_acceptance_run(), fake)
  payload = json.dumps(result.to_dict()).lower()
  assert "clock" not in payload
  rubric = json.loads((_FIXTURE_DIR / "week0_real_rubric.json").read_text())
  assert rubric["clock_started"] is False
