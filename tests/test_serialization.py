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

"""Tests for the uniform serialization layer."""

from datetime import datetime
from datetime import timezone
import json

import pytest

from bigquery_agent_analytics.context_graph import BizNode
from bigquery_agent_analytics.context_graph import Candidate
from bigquery_agent_analytics.context_graph import DecisionPoint
from bigquery_agent_analytics.context_graph import WorldChangeAlert
from bigquery_agent_analytics.context_graph import WorldChangeReport
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.feedback import DriftReport
from bigquery_agent_analytics.feedback import QuestionDistribution
from bigquery_agent_analytics.insights import AggregatedInsights
from bigquery_agent_analytics.insights import InsightsReport
from bigquery_agent_analytics.insights import SessionFacet
from bigquery_agent_analytics.insights import SessionMetadata
from bigquery_agent_analytics.serialization import serialize
from bigquery_agent_analytics.trace import ContentPart
from bigquery_agent_analytics.trace import EventType
from bigquery_agent_analytics.trace import ObjectRef
from bigquery_agent_analytics.trace import ResolvedTraceSelector
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceIdentity
from bigquery_agent_analytics.trace import TraceScope

_NOW = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ #
# Dataclass returns                                                    #
# ------------------------------------------------------------------ #


class TestSerializeTrace:

  def test_trace_basic(self):
    span = Span(
        event_type="LLM_REQUEST",
        agent=None,
        timestamp=_NOW,
        content={"model": "gemini"},
        attributes={},
    )
    trace = Trace(
        trace_id="t1",
        session_id="s1",
        spans=[span],
        start_time=_NOW,
        end_time=_NOW,
        total_latency_ms=100.0,
    )
    result = serialize(trace)
    assert isinstance(result, dict)
    assert result["trace_id"] == "t1"
    assert result["start_time"] == "2026-03-12T10:00:00+00:00"
    assert result["spans"][0]["timestamp"] == ("2026-03-12T10:00:00+00:00")
    # Must be json-safe
    json.dumps(result)

  def test_span_with_children(self):
    child = Span(
        event_type="TOOL_COMPLETED",
        agent=None,
        timestamp=_NOW,
        content={},
        attributes={},
    )
    parent = Span(
        event_type="AGENT_STARTING",
        agent=None,
        timestamp=_NOW,
        content={},
        attributes={},
        children=[child],
    )
    result = serialize(parent)
    assert len(result["children"]) == 1
    assert result["children"][0]["event_type"] == "TOOL_COMPLETED"
    json.dumps(result)

  def test_span_with_content_parts_and_object_ref(self):
    ref = ObjectRef(uri="gs://bucket/file", version="1")
    part = ContentPart(
        mime_type="text/plain",
        text="hello",
        object_ref=ref,
    )
    span = Span(
        event_type="LLM_RESPONSE",
        agent=None,
        timestamp=_NOW,
        content={},
        attributes={},
        content_parts=[part],
    )
    result = serialize(span)
    cp = result["content_parts"][0]
    assert cp["object_ref"]["uri"] == "gs://bucket/file"
    json.dumps(result)

  def test_biz_node_datetime(self):
    node = BizNode(
        span_id="sp1",
        session_id="s1",
        node_type="Product",
        node_value="Widget",
        evaluated_at=_NOW,
    )
    result = serialize(node)
    assert result["evaluated_at"] == "2026-03-12T10:00:00+00:00"
    json.dumps(result)

  def test_decision_point_datetime(self):
    dp = DecisionPoint(
        decision_id="d1",
        session_id="s1",
        span_id="sp1",
        decision_type="placement_selection",
        timestamp=_NOW,
    )
    result = serialize(dp)
    assert result["timestamp"] == "2026-03-12T10:00:00+00:00"
    json.dumps(result)


# ------------------------------------------------------------------ #
# Pydantic returns                                                     #
# ------------------------------------------------------------------ #


class TestSerializePydantic:

  def test_evaluation_report_created_at(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="latency",
        total_sessions=10,
        passed_sessions=8,
        created_at=_NOW,
    )
    result = serialize(report)
    # model_dump(mode="json") should convert datetime to str
    assert isinstance(result["created_at"], str)
    assert "2026-03-12" in result["created_at"]
    json.dumps(result)

  def test_insights_report_nested_datetimes(self):
    meta = SessionMetadata(
        session_id="s1",
        event_count=10,
        tool_calls=3,
        tool_errors=0,
        llm_calls=5,
        turn_count=2,
        total_latency_ms=1200.0,
        avg_latency_ms=600.0,
        agents_used=["bot"],
        tools_used=["search"],
        has_error=False,
        hitl_events=0,
        state_changes=0,
        start_time=_NOW,
        end_time=_NOW,
    )
    report = InsightsReport(
        created_at=_NOW,
        session_metadata=[meta],
        aggregated=AggregatedInsights(
            total_sessions=1,
            success_rate=1.0,
            avg_effectiveness=0.9,
            avg_latency_ms=1200.0,
            avg_turns=2.0,
            error_rate=0.0,
        ),
        executive_summary="All good.",
    )
    result = serialize(report)
    assert isinstance(result["created_at"], str)
    sm = result["session_metadata"][0]
    assert isinstance(sm["start_time"], str)
    assert isinstance(sm["end_time"], str)
    json.dumps(result)

  def test_drift_report(self):
    report = DriftReport(
        coverage_percentage=0.85,
        total_golden=100,
        total_production=200,
    )
    result = serialize(report)
    assert result["coverage_percentage"] == 0.85
    json.dumps(result)

  def test_question_distribution(self):
    dist = QuestionDistribution(total_questions=50)
    result = serialize(dist)
    assert result["total_questions"] == 50
    json.dumps(result)

  def test_world_change_report_checked_at(self):
    report = WorldChangeReport(
        session_id="s1",
        total_entities_checked=5,
        stale_entities=1,
        checked_at=_NOW,
    )
    result = serialize(report)
    assert isinstance(result["checked_at"], str)
    json.dumps(result)


# ------------------------------------------------------------------ #
# Plain dicts, lists, primitives                                       #
# ------------------------------------------------------------------ #


class TestSerializePrimitives:

  def test_dict_with_datetime(self):
    result = serialize({"ts": _NOW, "name": "test"})
    assert result["ts"] == "2026-03-12T10:00:00+00:00"
    assert result["name"] == "test"
    json.dumps(result)

  def test_list_of_traces(self):
    traces = [
        Trace(trace_id="t1", session_id="s1"),
        Trace(trace_id="t2", session_id="s2"),
    ]
    result = serialize(traces)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["trace_id"] == "t1"
    json.dumps(result)

  def test_none(self):
    assert serialize(None) is None

  def test_enum(self):
    assert serialize(EventType.LLM_REQUEST) == "LLM_REQUEST"

  def test_primitive_passthrough(self):
    assert serialize(42) == 42
    assert serialize("hello") == "hello"
    assert serialize(3.14) == 3.14
    assert serialize(True) is True

  def test_tuple_to_list(self):
    result = serialize((("a", 1), ("b", 2)))
    assert result == [["a", 1], ["b", 2]]


# ------------------------------------------------------------------ #
# Round-trip JSON safety                                               #
# ------------------------------------------------------------------ #


class TestSerializeRoundTrip:

  @pytest.mark.parametrize(
      "obj",
      [
          Trace(
              trace_id="t1",
              session_id="s1",
              spans=[
                  Span(
                      event_type="LLM_REQUEST",
                      agent=None,
                      timestamp=_NOW,
                      content={},
                      attributes={},
                  )
              ],
              start_time=_NOW,
          ),
          EvaluationReport(
              dataset="test",
              evaluator_name="latency",
              created_at=_NOW,
              session_scores=[
                  SessionScore(
                      session_id="s1",
                      scores={"latency": 0.8},
                      passed=True,
                  )
              ],
          ),
          DriftReport(
              coverage_percentage=0.9,
              total_golden=50,
              total_production=100,
          ),
          QuestionDistribution(total_questions=25),
          WorldChangeReport(
              session_id="s1",
              total_entities_checked=3,
              stale_entities=0,
              checked_at=_NOW,
          ),
      ],
      ids=[
          "Trace",
          "EvaluationReport",
          "DriftReport",
          "QuestionDistribution",
          "WorldChangeReport",
      ],
  )
  def test_roundtrip(self, obj):
    serialized = serialize(obj)
    dumped = json.dumps(serialized)
    loaded = json.loads(dumped)
    assert isinstance(loaded, dict)


# ------------------------------------------------------------------ #
# Identity contract (issue #359, U1)                                  #
# ------------------------------------------------------------------ #


class TestSerializeIdentity:

  def _trace(self, user_id, run_label):
    return Trace(
        trace_id="t1",
        session_id="sess-1",
        user_id=user_id,
        identity=TraceIdentity(session_id="sess-1", user_id=user_id),
        scope=TraceScope(custom_labels={"run": run_label}),
    )

  def test_trace_with_identity_json_safe_and_legacy_fields_unchanged(self):
    result = serialize(self._trace("alice", "v0"))
    # Legacy scalar fields keep their names and values.
    assert result["trace_id"] == "t1"
    assert result["session_id"] == "sess-1"
    assert result["user_id"] == "alice"
    # The identity is additive and nested.
    assert result["identity"]["session_id"] == "sess-1"
    assert result["identity"]["user_id"] == "alice"
    json.dumps(result)

  def test_identity_null_fields_serialize_stably(self):
    identity = TraceIdentity(session_id="sess-1")
    result = serialize(identity)
    assert result == {
        "session_id": "sess-1",
        "user_id": None,
        "root_agent_name": None,
    }
    json.dumps(result)

  def test_scope_with_null_fields_json_safe(self):
    result = serialize(TraceScope())
    json.dumps(result)
    result = serialize(TraceScope(custom_labels={"b": "2", "a": "1"}))
    json.dumps(result)

  def test_colliding_traces_stay_distinguishable_after_serialization(self):
    a = serialize(self._trace("alice", "v0"))
    b = serialize(self._trace("alice", "v1"))
    assert a["session_id"] == b["session_id"]
    assert a != b

  def test_resolved_selector_serializes(self):
    resolved = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
        scope=TraceScope(experiment_id="exp-1"),
    )
    result = serialize(resolved)
    assert result["identity"]["user_id"] == "alice"
    assert result["scope"]["experiment_id"] == "exp-1"
    json.dumps(result)
