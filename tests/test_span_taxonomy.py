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
"""Tests for span-level G1 attribution (#466, parent #435).

Everything is offline: the widget-stock silence session ``7e352c34`` is the
in-memory fixture of ``test_native_events_writer`` extended with the native
``trace_id`` / ``span_id`` / ``parent_span_id`` columns the production ADK
``agent_events`` rows carry. Nothing reaches BigQuery, nothing writes
anywhere, and nothing starts the six-week clock.
"""

from __future__ import annotations

import dataclasses

import pytest

from bigquery_agent_analytics import failure_taxonomy
from bigquery_agent_analytics import span_taxonomy
from bigquery_agent_analytics.evalbench import classify_sessions
from bigquery_agent_analytics.native_events import NativeAgentEventsRun
from bigquery_agent_analytics.span_taxonomy import label_failed_session_spans
from bigquery_agent_analytics.span_taxonomy import label_native_run
from bigquery_agent_analytics.span_taxonomy import SpanFailureLabel
from tests.test_native_events_writer import _event
from tests.test_native_events_writer import _gold_events
from tests.test_native_events_writer import _JOB_ID
from tests.test_native_events_writer import _POLICY
from tests.test_native_events_writer import _SESSION_GOLD
from tests.test_native_events_writer import _SESSION_STUCK
from tests.test_native_events_writer import _SOURCE_TABLE
from tests.test_native_events_writer import _stuck_events

# Native trace/span identity of the widget-stock fixture rows, in the OTel
# hex shape the ADK plugin writes. These are fixture stand-ins for the real
# column values (like the gold sibling's uuid suffix); the contract under
# test is that emitted span ids are always drawn FROM the rows, never made
# up by the attribution layer.
_TRACE_STUCK = "6ad3f30c47a2bfd1f1f6f2c1c19f6d2e"
_TRACE_GOLD = "9b41c1de7a0f45c2ae55d9f1c2b3a4d5"
_AGENT_STARTING_SPAN = "b7ad6b7169203331"


def _with_spans(events, trace_id, span_ids):
  rows = []
  for row, span_id in zip(events, span_ids, strict=True):
    row = dict(row)
    row["trace_id"] = trace_id
    row["span_id"] = span_id
    row["parent_span_id"] = None
    rows.append(row)
  return rows


def _stuck_events_with_spans():
  # USER_MESSAGE_RECEIVED -> INVOCATION_STARTING -> AGENT_STARTING, then
  # silence. The last existing span is AGENT_STARTING.
  return _with_spans(
      _stuck_events(),
      _TRACE_STUCK,
      ["5c40f2a1d69e0b17", "8e1b2c3d4f5a6071", _AGENT_STARTING_SPAN],
  )


def _gold_events_with_spans():
  return _with_spans(
      _gold_events(),
      _TRACE_GOLD,
      [
          "1a2b3c4d5e6f7081",
          "2b3c4d5e6f708192",
          "3c4d5e6f708192a3",
          "4d5e6f708192a3b4",
          "5e6f708192a3b4c5",
      ],
  )


def _acceptance_run():
  return NativeAgentEventsRun.from_agent_events(
      _stuck_events_with_spans() + _gold_events_with_spans(),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )


def _acceptance_verdicts(run):
  return {
      verdict.session_id: verdict
      for verdict in classify_sessions(
          run.to_agent_event_rows(import_version="v1"),
          run.to_score_rows(import_version="v1"),
          _POLICY,
      )
  }


# --- acceptance: the AGENT_STARTING -> silence punchline ------------------


def test_session_level_g1_is_unchanged_by_span_labels() -> None:
  run = _acceptance_run()
  verdicts = _acceptance_verdicts(run)
  label_native_run(run, policy=_POLICY)  # Localization has no side effects.
  stuck = verdicts[_SESSION_STUCK]
  assert (
      stuck.process_failed,
      stuck.missing_completion,
      stuck.score_failed,
      stuck.failed,
  ) == (True, True, True, True)
  assert failure_taxonomy.TAXONOMY_VERSION == "0.1.0"
  assert failure_taxonomy.categorize_failed_session(stuck) == (
      "task/planning",
      "finalization",
      "tool blockers",
  )
  assert verdicts[_SESSION_GOLD].failed is False


def test_silence_localizes_to_the_last_real_span_agent_starting() -> None:
  run = _acceptance_run()
  stuck = _acceptance_verdicts(run)[_SESSION_STUCK]
  labels = label_failed_session_spans(
      _stuck_events_with_spans(),
      stuck,
      eval_id="7e352c34",
      gold_events=_gold_events_with_spans(),
  )
  # At least one span is labeled; every label anchors the punchline span.
  assert len(labels) == 3
  assert [label.failure_category for label in labels] == [
      "task/planning",
      "finalization",
      "tool blockers",
  ]
  for label in labels:
    assert label.span_id == _AGENT_STARTING_SPAN
    assert label.trace_id == _TRACE_STUCK
    assert label.session_id == _SESSION_STUCK
    assert label.eval_id == "7e352c34"
    assert label.target_kind == span_taxonomy.TARGET_GAP_AFTER_SPAN
    assert label.turn_index == 0
    assert label.confidence == span_taxonomy.MECHANICAL_CONFIDENCE


def test_evidence_states_no_tool_starting_and_no_check_inventory() -> None:
  run = _acceptance_run()
  stuck = _acceptance_verdicts(run)[_SESSION_STUCK]
  labels = label_failed_session_spans(
      _stuck_events_with_spans(),
      stuck,
      eval_id="7e352c34",
      gold_events=_gold_events_with_spans(),
  )
  by_category = {label.failure_category: label.evidence for label in labels}
  tool_blockers = by_category["tool blockers"]
  assert "no TOOL_STARTING event follows" in tool_blockers
  assert "AGENT_STARTING" in tool_blockers
  assert "check_inventory was never called" in tool_blockers
  finalization = by_category["finalization"]
  assert "no AGENT_COMPLETED event follows" in finalization
  assert "goes silent" in finalization
  planning = by_category["task/planning"]
  assert "score gate failed" in planning
  assert "goal_completion=0.0" in planning


def test_rfc_tuple_shape_is_trace_span_category_evidence_confidence() -> None:
  labels = label_native_run(_acceptance_run(), policy=_POLICY)
  assert labels  # The widget-stock session is localized.
  trace_id, span_id, category, evidence, confidence = labels[0].as_tuple()
  assert trace_id == _TRACE_STUCK
  assert span_id == _AGENT_STARTING_SPAN
  assert category in failure_taxonomy.FROZEN_CATEGORY_NAMES
  assert isinstance(evidence, str) and evidence
  assert confidence == 1.0


def test_label_native_run_labels_only_the_failed_session() -> None:
  labels = label_native_run(_acceptance_run(), policy=_POLICY)
  assert {label.session_id for label in labels} == {_SESSION_STUCK}
  # The completed sibling supplies missing-tool evidence, not labels.
  by_category = {label.failure_category: label for label in labels}
  assert "check_inventory was never called" in (
      by_category["tool blockers"].evidence
  )


# --- identity: joinable to the frozen eval_id rule ------------------------


def test_labels_join_failed_sessions_via_the_frozen_first8_rule() -> None:
  run = _acceptance_run()
  verdicts = _acceptance_verdicts(run)
  labels = label_native_run(run, policy=_POLICY)
  assert {label.eval_id for label in labels} == {"7e352c34"}
  assert verdicts[_SESSION_STUCK].scenario_id == "7e352c34"


def test_first8_collision_falls_back_to_the_full_session_id() -> None:
  twin = "7e352c34-ffff-4fff-8fff-ffffffffffff"
  twin_events = _with_spans(
      [
          _event(twin, "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
          _event(twin, "AGENT_STARTING", "You are a support agent.", offset=1),
      ],
      "77aa77aa77aa77aa77aa77aa77aa77aa",
      ["aaaa000011112222", "bbbb111122223333"],
  )
  run = NativeAgentEventsRun.from_agent_events(
      _stuck_events_with_spans() + twin_events + _gold_events_with_spans(),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  labels = label_native_run(run, policy=_POLICY)
  assert {label.eval_id for label in labels} == {_SESSION_STUCK, twin}


# --- no synthetic span identifiers ----------------------------------------


def test_rows_without_span_id_fail_closed_instead_of_inventing_one() -> None:
  run = _acceptance_run()
  stuck = _acceptance_verdicts(run)[_SESSION_STUCK]
  with pytest.raises(ValueError, match="synthetic span identifier"):
    label_failed_session_spans(
        _stuck_events(),  # The bare fixture rows carry no span_id.
        stuck,
        eval_id="7e352c34",
    )


def test_every_emitted_span_id_comes_from_the_input_rows() -> None:
  events = _stuck_events_with_spans()
  run = _acceptance_run()
  stuck = _acceptance_verdicts(run)[_SESSION_STUCK]
  labels = label_failed_session_spans(events, stuck, eval_id="7e352c34")
  real_span_ids = {row["span_id"] for row in events}
  assert {label.span_id for label in labels} <= real_span_ids


# --- taxonomy freeze ------------------------------------------------------


def test_a_fourth_category_string_is_rejected_at_construction() -> None:
  label = label_native_run(_acceptance_run(), policy=_POLICY)[0]
  with pytest.raises(ValueError, match="G1-frozen taxonomy"):
    dataclasses.replace(label, failure_category="agent starvation")


def test_a_session_no_flag_tripped_gets_no_span_labels() -> None:
  run = _acceptance_run()
  gold = _acceptance_verdicts(run)[_SESSION_GOLD]
  assert (
      label_failed_session_spans(
          _gold_events_with_spans(), gold, eval_id="ab7535a5"
      )
      == ()
  )


# --- direct ERROR spans and the #429 turn coordinate ----------------------


def test_a_raw_error_span_is_the_direct_target_with_its_message() -> None:
  session = "cc00dd11-2222-4333-8444-555566667777"
  events = _with_spans(
      [
          _event(session, "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
          _event(
              session,
              "TOOL_STARTING",
              {"tool": "check_inventory", "args": {}},
              offset=1,
          ),
          _event(
              session,
              "TOOL_COMPLETED",
              {"tool": "check_inventory"},
              offset=2,
              status="ERROR",
              error_message="inventory backend timed out",
          ),
      ],
      "10efbead20efbead30efbead40efbead",
      ["0101010101010101", "0202020202020202", "0303030303030303"],
  )
  verdict = {
      "process_failed": True,
      "missing_completion": True,
      "score_failed": False,
  }
  labels = label_failed_session_spans(events, verdict, eval_id="cc00dd11")
  by_category = {label.failure_category: label for label in labels}
  blocker = by_category["tool blockers"]
  assert blocker.span_id == "0303030303030303"
  assert blocker.target_kind == span_taxonomy.TARGET_SPAN
  assert "status ERROR" in blocker.evidence
  assert "inventory backend timed out" in blocker.evidence


def test_turn_index_counts_user_message_received_turns_like_429() -> None:
  session = "dd11ee22-3333-4444-8555-666677778888"
  events = _with_spans(
      [
          _event(session, "USER_MESSAGE_RECEIVED", {"text_summary": "one"}),
          _event(session, "AGENT_STARTING", "You are an agent.", offset=1),
          _event(
              session,
              "USER_MESSAGE_RECEIVED",
              {"text_summary": "two"},
              offset=2,
          ),
          _event(session, "AGENT_STARTING", "Second turn.", offset=3),
      ],
      "50efbead60efbead70efbead80efbead",
      [
          "1111222233334444",
          "2222333344445555",
          "3333444455556666",
          "4444555566667777",
      ],
  )
  verdict = {
      "process_failed": False,
      "missing_completion": True,
      "score_failed": False,
  }
  (label,) = label_failed_session_spans(events, verdict, eval_id="dd11ee22")
  assert label.failure_category == "finalization"
  assert label.turn_index == 1


def test_multi_session_input_is_rejected() -> None:
  verdict = {
      "process_failed": False,
      "missing_completion": True,
      "score_failed": False,
  }
  with pytest.raises(ValueError, match="per-session"):
    label_failed_session_spans(
        _stuck_events_with_spans() + _gold_events_with_spans(),
        verdict,
        eval_id="7e352c34",
    )
