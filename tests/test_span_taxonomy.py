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
  # "wrong source" IS in the frozen vocabulary — but the three-flag mapper
  # cannot emit it, so a span label carrying it could only be fabricated.
  assert "wrong source" in failure_taxonomy.FROZEN_CATEGORY_NAMES
  assert "wrong source" not in span_taxonomy.SPAN_CATEGORY_NAMES
  with pytest.raises(ValueError, match="G1-frozen taxonomy"):
    dataclasses.replace(label, failure_category="wrong source")
  # Out-of-vocabulary strings stay rejected too.
  with pytest.raises(ValueError, match="G1-frozen taxonomy"):
    dataclasses.replace(label, failure_category="agent starvation")


def test_span_labels_are_restricted_to_the_three_emittable_names() -> None:
  assert span_taxonomy.SPAN_CATEGORY_NAMES == (
      "task/planning",
      "finalization",
      "tool blockers",
  )


def test_a_session_no_flag_tripped_gets_no_span_labels() -> None:
  run = _acceptance_run()
  gold = _acceptance_verdicts(run)[_SESSION_GOLD]
  assert (
      label_failed_session_spans(
          _gold_events_with_spans(), gold, eval_id="ab7535a5"
      )
      == ()
  )


# --- direct ERROR spans and per-category anchoring ------------------------


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


def test_mid_stream_error_keeps_silence_evidence_on_the_last_span() -> None:
  # Regression (#467 P1): the first ERROR span is mid-stream — real native
  # rows follow it. Only tool blockers may anchor there; the end-of-trace
  # claims (finalization, "no subsequent row exists") must anchor to the
  # LAST real span, where they are actually true.
  session = "ee22ff33-4444-4555-8666-777788889999"
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
          _event(session, "LLM_RESPONSE", {"response": "retrying"}, offset=3),
      ],
      "90efbead01efbead12efbead23efbead",
      [
          "0404040404040404",
          "0505050505050505",
          "0606060606060606",
          "0707070707070707",
      ],
  )
  verdict = {
      "process_failed": True,
      "missing_completion": True,
      "score_failed": False,
  }
  labels = label_failed_session_spans(events, verdict, eval_id="ee22ff33")
  by_category = {label.failure_category: label for label in labels}
  blocker = by_category["tool blockers"]
  assert blocker.span_id == "0606060606060606"  # The real ERROR span.
  assert blocker.target_kind == span_taxonomy.TARGET_SPAN
  assert "status ERROR" in blocker.evidence
  assert "goes silent" not in blocker.evidence  # A row follows the ERROR.
  finalization = by_category["finalization"]
  assert finalization.span_id == "0707070707070707"  # The LAST real span.
  assert finalization.target_kind == span_taxonomy.TARGET_GAP_AFTER_SPAN
  assert "LLM_RESPONSE" in finalization.evidence
  assert "0707070707070707" in finalization.evidence
  assert "0606060606060606" not in finalization.evidence


def test_a_started_tool_is_not_reported_as_never_started() -> None:
  # Regression (#467 P1): the session DID start check_inventory before the
  # silence; with the sibling calling the same tool, missing_tools is empty
  # and the evidence must describe the actual gap, not deny the tool call.
  session = "ff33aa44-5555-4666-8777-888899990000"
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
              {"tool": "check_inventory", "result": {"in_stock": 0}},
              offset=2,
          ),
      ],
      "34efbead45efbead56efbead67efbead",
      ["0808080808080808", "0909090909090909", "0a0a0a0a0a0a0a0a"],
  )
  verdict = {
      "process_failed": True,
      "missing_completion": True,
      "score_failed": False,
  }
  for gold_events in ((), _gold_events_with_spans()):
    labels = label_failed_session_spans(
        events, verdict, eval_id="ff33aa44", gold_events=gold_events
    )
    blocker = {label.failure_category: label for label in labels}[
        "tool blockers"
    ]
    assert "no tool was ever started" not in blocker.evidence
    assert "check_inventory was started earlier" in blocker.evidence


def test_no_turn_index_is_published() -> None:
  # #429's turn_index indexes the full reconstructed conversation (user AND
  # agent messages) and has no importable package mapping; rather than fork
  # the coordinate with a user-message ordinal, span labels omit it. The
  # interleaved conversation below is exactly where the two would diverge.
  session = "dd11ee22-3333-4444-8555-666677778888"
  events = _with_spans(
      [
          _event(session, "USER_MESSAGE_RECEIVED", {"text_summary": "one"}),
          _event(session, "LLM_RESPONSE", {"response": "answer one"}, offset=1),
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
  field_names = {field.name for field in dataclasses.fields(label)}
  assert "turn_index" not in field_names
  assert not hasattr(label, "turn_index")


# --- gold evidence scoping (same-prompt sibling only) ----------------------


def test_gold_evidence_is_scoped_to_the_same_prompt_sibling() -> None:
  # Regression (#467 P1): a multi-scenario run. The refund-policy scenario's
  # completed session called lookup_policy; the widget-stock stuck session
  # must NOT be told lookup_policy was "never called" — its gold sibling is
  # the completed session that asked the SAME prompt.
  other = "cafe0000-1111-4222-8333-444455556666"
  other_events = _with_spans(
      [
          _event(
              other,
              "USER_MESSAGE_RECEIVED",
              {"text_summary": "What is the refund policy?"},
          ),
          _event(
              other,
              "TOOL_STARTING",
              {"tool": "lookup_policy", "args": {}},
              offset=1,
          ),
          _event(other, "TOOL_COMPLETED", {"tool": "lookup_policy"}, offset=2),
          _event(other, "AGENT_COMPLETED", "null", offset=3),
      ],
      "abcd0000abcd1111abcd2222abcd3333",
      [
          "1212121212121212",
          "1313131313131313",
          "1414141414141414",
          "1515151515151515",
      ],
  )
  run = NativeAgentEventsRun.from_agent_events(
      _stuck_events_with_spans() + _gold_events_with_spans() + other_events,
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  labels = label_native_run(run, policy=_POLICY)
  assert {label.session_id for label in labels} == {_SESSION_STUCK}
  blocker = {label.failure_category: label for label in labels}["tool blockers"]
  assert "check_inventory was never called" in blocker.evidence
  assert "lookup_policy" not in blocker.evidence


def test_missing_tool_claims_are_omitted_without_a_same_prompt_sibling() -> (
    None
):
  # A failed scenario with no completed same-prompt sibling: the completed
  # sessions of OTHER scenarios must not leak their tools into evidence.
  lonely = "beef0000-2222-4333-8444-555566667777"
  lonely_events = _with_spans(
      [
          _event(
              lonely,
              "USER_MESSAGE_RECEIVED",
              {"text_summary": "Where is my order?"},
          ),
          _event(lonely, "AGENT_STARTING", "You are an agent.", offset=1),
      ],
      "beef1111beef2222beef3333beef4444",
      ["1616161616161616", "1717171717171717"],
  )
  run = NativeAgentEventsRun.from_agent_events(
      lonely_events + _gold_events_with_spans(),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  labels = label_native_run(run, policy=_POLICY)
  assert {label.session_id for label in labels} == {lonely}
  blocker = {label.failure_category: label for label in labels}["tool blockers"]
  assert "never called" not in blocker.evidence
  assert "check_inventory" not in blocker.evidence
  assert "no tool was ever started" in blocker.evidence


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
