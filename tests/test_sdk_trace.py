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

"""Tests for the SDK trace module."""

from datetime import datetime
from datetime import timezone
import json

import pytest

from bigquery_agent_analytics import trace as trace_module
from bigquery_agent_analytics.trace import AmbiguousSessionError
from bigquery_agent_analytics.trace import ContentPart
from bigquery_agent_analytics.trace import ObjectRef
from bigquery_agent_analytics.trace import resolve_singular_candidate
from bigquery_agent_analytics.trace import ResolvedTraceSelector
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import SQL_NULL
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceFilter
from bigquery_agent_analytics.trace import TraceIdentity
from bigquery_agent_analytics.trace import TraceScope
from bigquery_agent_analytics.trace import TraceSelector
from bigquery_agent_analytics.trace import UNSET


class TestSpan:
  """Tests for Span class."""

  def test_from_bigquery_row_basic(self):
    row = {
        "event_type": "TOOL_STARTING",
        "agent": "my_agent",
        "timestamp": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        "content": '{"tool": "search", "args": {"q": "test"}}',
        "attributes": '{"model": "gemini"}',
        "span_id": "span-1",
        "parent_span_id": "parent-1",
        "status": "OK",
        "session_id": "sess-1",
    }

    span = Span.from_bigquery_row(row)

    assert span.event_type == "TOOL_STARTING"
    assert span.agent == "my_agent"
    assert span.content["tool"] == "search"
    assert span.attributes["model"] == "gemini"
    assert span.span_id == "span-1"
    assert span.parent_span_id == "parent-1"
    assert span.status == "OK"
    assert span.session_id == "sess-1"

  def test_from_bigquery_row_json_latency(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": None,
        "attributes": None,
        "latency_ms": '{"total_ms": 450}',
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert span.latency_ms == 450

  def test_from_bigquery_row_dict_latency(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": None,
        "attributes": None,
        "latency_ms": {"total_ms": 200},
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert span.latency_ms == 200

  def test_from_bigquery_row_with_content_parts(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": "{}",
        "attributes": "{}",
        "content_parts": [
            {
                "mime_type": "image/png",
                "uri": "gs://bucket/img.png",
                "text": None,
                "storage_mode": "GCS_REFERENCE",
            }
        ],
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert len(span.content_parts) == 1
    assert span.content_parts[0].mime_type == "image/png"
    assert span.content_parts[0].uri == "gs://bucket/img.png"

  def test_label_tool_event(self):
    span = Span(
        event_type="TOOL_STARTING",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "search_web"},
    )
    assert "TOOL_STARTING" in span.label
    assert "(search_web)" in span.label

  def test_label_error_event(self):
    span = Span(
        event_type="LLM_ERROR",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        status="ERROR",
    )
    assert "ERROR" in span.label

  def test_tool_name_for_tool_event(self):
    span = Span(
        event_type="TOOL_STARTING",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "search_contacts"},
    )
    assert span.tool_name == "search_contacts"

  def test_tool_name_for_hitl_event(self):
    span = Span(
        event_type="HITL_APPROVAL_REQUESTED",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "book_meeting"},
    )
    assert span.tool_name == "book_meeting"

  def test_tool_name_none_for_non_tool_event(self):
    span = Span(
        event_type="USER_MESSAGE_RECEIVED",
        agent=None,
        timestamp=datetime.now(timezone.utc),
        content={"text_summary": "Hello"},
    )
    assert span.tool_name is None

  def test_tool_name_none_when_missing(self):
    span = Span(
        event_type="TOOL_STARTING",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={},
    )
    assert span.tool_name is None

  def test_tool_name_none_when_empty(self):
    span = Span(
        event_type="TOOL_STARTING",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": ""},
    )
    assert span.tool_name is None

  def test_tool_name_does_not_leak_for_non_tool_event_with_tool_key(self):
    """Non-tool events carrying an incidental content['tool'] key must
    NOT surface it as tool_name. The attribute means "this span invoked
    a tool" — arbitrary payloads that happen to use the same key name
    should return None."""
    span = Span(
        event_type="AGENT_THOUGHT",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "looks_like_a_tool_but_isnt", "note": "reasoning"},
    )
    assert span.tool_name is None

  def test_tool_name_does_not_leak_for_llm_response_with_tool_key(self):
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"response": "I'll use a tool", "tool": "mentioned_in_text"},
    )
    assert span.tool_name is None

  def test_summary_with_text(self):
    span = Span(
        event_type="USER_MESSAGE_RECEIVED",
        agent=None,
        timestamp=datetime.now(timezone.utc),
        content={"text_summary": "What is the weather?"},
    )
    assert span.summary == "What is the weather?"

  def test_summary_truncation(self):
    long_text = "x" * 200
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"text_summary": long_text},
    )
    assert len(span.summary) == 120
    assert span.summary.endswith("...")

  def test_summary_from_error_message(self):
    span = Span(
        event_type="TOOL_ERROR",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        error_message="Connection refused",
        status="ERROR",
    )
    assert span.summary == "Connection refused"

  def test_summary_from_content_parts(self):
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={},
        content_parts=[
            ContentPart(
                mime_type="image/png",
                uri="gs://bucket/image.png",
            )
        ],
    )
    assert "image/png" in span.summary
    assert "gs://bucket/image.png" in span.summary

  def test_from_bigquery_row_with_object_ref(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": "{}",
        "attributes": "{}",
        "content_parts": [
            {
                "mime_type": "image/png",
                "uri": None,
                "text": None,
                "storage_mode": "GCS_REFERENCE",
                "object_ref": {
                    "uri": "gs://bucket/ref.png",
                    "version": "v1",
                    "authorizer": "sa@proj.iam",
                    "details": None,
                },
                "part_index": 0,
                "part_attributes": '{"source": "camera"}',
            }
        ],
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert len(span.content_parts) == 1
    part = span.content_parts[0]
    assert part.object_ref is not None
    assert part.object_ref.uri == "gs://bucket/ref.png"
    assert part.object_ref.version == "v1"
    assert part.object_ref.authorizer == "sa@proj.iam"
    assert part.part_index == 0
    assert part.part_attributes == '{"source": "camera"}'

  def test_summary_from_object_ref_uri(self):
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={},
        content_parts=[
            ContentPart(
                mime_type="audio/wav",
                object_ref=ObjectRef(uri="gs://b/audio.wav"),
            )
        ],
    )
    assert "audio/wav" in span.summary
    assert "gs://b/audio.wav" in span.summary

  def test_summary_raw_content_fallback(self):
    """AGENT_STARTING stores raw string content."""
    span = Span(
        event_type="AGENT_STARTING",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"raw": "You are a helpful assistant"},
    )
    assert span.summary == "You are a helpful assistant"

  def test_summary_unwraps_text_single_quoted(self):
    """`text: 'hello'` wrapper should be stripped from response."""
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"response": "text: 'hello world'"},
    )
    assert span.summary == "hello world"

  def test_summary_unwraps_text_double_quoted(self):
    """`text: \"hello\"` wrapper should be stripped too."""
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"response": 'text: "hello world"'},
    )
    assert span.summary == "hello world"

  def test_summary_unwraps_truncated_text_field(self):
    """Truncated `text: 'abc...` (no closing quote) strips opening quote only."""
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"response": "text: 'I found three Priyas"},
    )
    assert span.summary == "I found three Priyas"

  def test_summary_leaves_plain_text_alone(self):
    """A response without the wrapper must be returned unchanged."""
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"response": "Booking confirmed for Priya Patel."},
    )
    assert span.summary == "Booking confirmed for Priya Patel."

  def test_summary_leaves_non_wrapper_text_prefix_alone(self):
    """`text:` without a following space-quoted value is not our wrapper."""
    span = Span(
        event_type="LLM_RESPONSE",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"response": "text:the-tool-id-is-x"},
    )
    # No leading `text: ` (with space), so the unwrapper leaves it alone.
    assert span.summary == "text:the-tool-id-is-x"


class TestTrace:
  """Tests for Trace class."""

  def _make_spans(self):
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return [
        Span(
            event_type="USER_MESSAGE_RECEIVED",
            agent=None,
            timestamp=ts,
            content={"text_summary": "Hello"},
            span_id="s1",
        ),
        Span(
            event_type="AGENT_STARTING",
            agent="my_agent",
            timestamp=ts,
            span_id="s2",
            parent_span_id="s1",
        ),
        Span(
            event_type="TOOL_STARTING",
            agent="my_agent",
            timestamp=ts,
            content={"tool": "search", "args": {"q": "hi"}},
            span_id="s3",
            parent_span_id="s2",
        ),
        Span(
            event_type="TOOL_COMPLETED",
            agent="my_agent",
            timestamp=ts,
            content={"tool": "search", "result": {"data": 1}},
            span_id="s4",
            parent_span_id="s2",
            latency_ms=100,
            status="OK",
        ),
        Span(
            event_type="AGENT_COMPLETED",
            agent="my_agent",
            timestamp=ts,
            content={"response": "Hi there!"},
            span_id="s5",
            parent_span_id="s1",
        ),
    ]

  def test_build_tree(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        spans=self._make_spans(),
    )
    roots = trace._build_tree()
    assert len(roots) == 1
    assert roots[0].span_id == "s1"
    assert len(roots[0].children) >= 1

  def test_render_tree(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        spans=self._make_spans(),
        total_latency_ms=500,
    )
    output = trace.render()
    assert "t1" in output
    assert "sess-1" in output
    assert "USER_MESSAGE_RECEIVED" in output
    assert "TOOL_STARTING" in output

  def test_render_color_default_off(self):
    """Default render() must not emit ANSI escape codes."""
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    spans = [
        Span(
            event_type="TOOL_ERROR",
            agent="agent",
            timestamp=ts,
            content={"tool": "search"},
            error_message="boom",
            status="ERROR",
            span_id="s1",
        ),
    ]
    trace = Trace(trace_id="t1", session_id="sess-1", spans=spans)
    output = trace.render()
    assert "\x1b[" not in output

  def test_render_color_true_wraps_error_icon(self):
    """With color=True, error spans get red ANSI wrap on the cross icon."""
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    spans = [
        Span(
            event_type="TOOL_ERROR",
            agent="agent",
            timestamp=ts,
            content={"tool": "search"},
            error_message="boom",
            status="ERROR",
            span_id="s1",
        ),
    ]
    trace = Trace(trace_id="t1", session_id="sess-1", spans=spans)
    output = trace.render(color=True)
    assert "\x1b[31m\u2717\x1b[0m" in output

  def test_render_color_true_wraps_subtree_warning(self):
    """Parent of an error child gets yellow warning icon wrap."""
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    spans = [
        Span(
            event_type="AGENT_STARTING",
            agent="agent",
            timestamp=ts,
            span_id="s1",
        ),
        Span(
            event_type="TOOL_ERROR",
            agent="agent",
            timestamp=ts,
            content={"tool": "search"},
            error_message="boom",
            status="ERROR",
            span_id="s2",
            parent_span_id="s1",
        ),
    ]
    trace = Trace(trace_id="t1", session_id="sess-1", spans=spans)
    output = trace.render(color=True)
    assert "\x1b[33m\u26a0\x1b[0m" in output
    assert "\x1b[31m\u2717\x1b[0m" in output

  def test_render_color_true_no_wrap_on_success(self):
    """Successful spans stay plain even with color=True."""
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    spans = [
        Span(
            event_type="TOOL_COMPLETED",
            agent="agent",
            timestamp=ts,
            content={"tool": "search", "result": "ok"},
            status="OK",
            span_id="s1",
        ),
    ]
    trace = Trace(trace_id="t1", session_id="sess-1", spans=spans)
    output = trace.render(color=True)
    assert "\x1b[" not in output

  def test_render_handles_unicode_tool_names(self):
    """Non-ASCII tool names must not crash render() and must preserve
    the tree-connector structure on each line. Display-width alignment
    in monospace fonts is a known limitation (tracked separately) —
    this test pins the minimum: no exceptions, connectors emitted."""
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    spans = [
        Span(
            event_type="TOOL_STARTING",
            agent="agent",
            timestamp=ts,
            content={"tool": "搜索联系人"},
            span_id="s1",
        ),
        Span(
            event_type="TOOL_COMPLETED",
            agent="agent",
            timestamp=ts,
            content={"tool": "🔍_fuzzy_search", "result": "ok"},
            span_id="s2",
            parent_span_id="s1",
            latency_ms=50,
        ),
    ]
    trace = Trace(trace_id="t1", session_id="sess-1", spans=spans)
    output = trace.render()
    assert "搜索联系人" in output
    assert "🔍_fuzzy_search" in output
    # tree connectors present on every non-header line
    assert "\u2514\u2500" in output or "\u251c\u2500" in output

  def test_render_flat_no_span_ids(self):
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="USER_MESSAGE_RECEIVED",
            agent=None,
            timestamp=ts,
            content={"text_summary": "Hello"},
        ),
        Span(
            event_type="AGENT_COMPLETED",
            agent="agent",
            timestamp=ts,
            content={"response": "Goodbye"},
        ),
    ]
    trace = Trace(
        trace_id="t2",
        session_id="sess-2",
        spans=spans,
    )
    output = trace.render()
    assert "USER_MESSAGE_RECEIVED" in output
    assert "AGENT_COMPLETED" in output

  def test_tool_calls_extraction(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        spans=self._make_spans(),
    )
    calls = trace.tool_calls
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "search"

  def test_final_response(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        spans=self._make_spans(),
    )
    assert trace.final_response == "Hi there!"

  def test_final_response_prefers_llm_response(self):
    """LLM_RESPONSE is preferred over AGENT_COMPLETED."""
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=ts,
            content={"response": "LLM said this"},
        ),
        Span(
            event_type="AGENT_COMPLETED",
            agent="agent",
            timestamp=ts,
            content={"response": "Agent said this"},
        ),
    ]
    trace = Trace(trace_id="t", session_id="s", spans=spans)
    assert trace.final_response == "LLM said this"

  def test_final_response_unwraps_text_prefix_from_llm_response(self):
    """text: '...' wrapper must be stripped at the trace-level accessor too."""
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=ts,
            content={"response": "text: 'I found three people named Priya.'"},
        ),
    ]
    trace = Trace(trace_id="t", session_id="s", spans=spans)
    assert trace.final_response == "I found three people named Priya."

  def test_final_response_unwraps_text_prefix_from_agent_completed(self):
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="AGENT_COMPLETED",
            agent="agent",
            timestamp=ts,
            content={"response": 'text: "Booking confirmed."'},
        ),
    ]
    trace = Trace(trace_id="t", session_id="s", spans=spans)
    assert trace.final_response == "Booking confirmed."

  def test_final_response_null_agent_completed(self):
    """Handles null AGENT_COMPLETED content (ADK plugin behavior)."""
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=ts,
            content={"response": "From LLM"},
        ),
        Span(
            event_type="AGENT_COMPLETED",
            agent="agent",
            timestamp=ts,
            content={},
        ),
    ]
    trace = Trace(trace_id="t", session_id="s", spans=spans)
    assert trace.final_response == "From LLM"

  def test_tool_calls_includes_tool_origin(self):
    """tool_origin from content is included in tool_calls."""
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="TOOL_STARTING",
            agent="agent",
            timestamp=ts,
            content={
                "tool": "search",
                "args": {},
                "tool_origin": "MCP",
            },
            span_id="t1",
        ),
        Span(
            event_type="TOOL_COMPLETED",
            agent="agent",
            timestamp=ts,
            content={
                "tool": "search",
                "result": {},
                "tool_origin": "MCP",
            },
            span_id="t1",
            status="OK",
        ),
    ]
    trace = Trace(trace_id="t", session_id="s", spans=spans)
    calls = trace.tool_calls
    assert len(calls) == 1
    assert calls[0]["tool_origin"] == "MCP"

  def test_error_spans(self):
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="TOOL_ERROR",
            agent="agent",
            timestamp=ts,
            status="ERROR",
            error_message="Timeout",
        ),
        Span(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=ts,
            status="OK",
        ),
    ]
    trace = Trace(
        trace_id="t",
        session_id="s",
        spans=spans,
    )
    assert len(trace.error_spans) == 1
    assert trace.error_spans[0].event_type == "TOOL_ERROR"


class TestTraceFilter:
  """Tests for TraceFilter class."""

  def test_empty_filter(self):
    filt = TraceFilter()
    where, params = filt.to_sql_conditions()
    assert where == "TRUE"
    assert len(params) == 1  # trace_limit

  def test_time_range_filter(self):
    filt = TraceFilter(
        start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2024, 12, 31, tzinfo=timezone.utc),
    )
    where, params = filt.to_sql_conditions()
    assert "timestamp >= @start_time" in where
    assert "timestamp <= @end_time" in where
    assert len(params) == 3  # start, end, limit

  def test_agent_filter(self):
    filt = TraceFilter(agent_id="my_agent")
    where, _ = filt.to_sql_conditions()
    assert "agent = @agent_id" in where

  def test_error_filter(self):
    filt = TraceFilter(has_error=True)
    where, _ = filt.to_sql_conditions()
    assert "status = 'ERROR'" in where

  def test_session_ids_filter(self):
    filt = TraceFilter(session_ids=["s1", "s2"])
    where, _ = filt.to_sql_conditions()
    assert "session_id IN UNNEST(@session_ids)" in where

  def test_latency_filter(self):
    filt = TraceFilter(min_latency_ms=100, max_latency_ms=5000)
    where, _ = filt.to_sql_conditions()
    assert "@min_latency_ms" in where
    assert "@max_latency_ms" in where

  def test_combined_filters(self):
    filt = TraceFilter(
        agent_id="agent",
        has_error=True,
        user_id="user-1",
    )
    where, _ = filt.to_sql_conditions()
    assert " AND " in where
    assert "agent = @agent_id" in where
    assert "status = 'ERROR'" in where
    assert "user_id = @user_id" in where


class TestSpanErrorVisibility:
  """Tests for error propagation on Span."""

  def _make_span(self, status="OK", error_message=None, **kwargs):
    return Span(
        event_type=kwargs.get("event_type", "TOOL_COMPLETED"),
        agent="agent",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        content=kwargs.get("content", {}),
        status=status,
        error_message=error_message,
        children=[],
    )

  def test_is_error_true(self):
    s = self._make_span(status="ERROR", error_message="boom")
    assert s.is_error is True

  def test_is_error_false(self):
    s = self._make_span(status="OK")
    assert s.is_error is False

  def test_subtree_has_error_direct(self):
    s = self._make_span(status="ERROR")
    assert s.subtree_has_error is True

  def test_subtree_has_error_child(self):
    child = self._make_span(status="ERROR", error_message="fail")
    parent = self._make_span(status="OK")
    parent.children = [child]
    assert parent.subtree_has_error is True

  def test_subtree_no_error(self):
    child = self._make_span(status="OK")
    parent = self._make_span(status="OK")
    parent.children = [child]
    assert parent.subtree_has_error is False

  def test_failure_context_with_tool(self):
    s = self._make_span(
        status="ERROR",
        error_message="timeout after 30s",
        event_type="TOOL_ERROR",
        content={"tool": "search_api"},
    )
    ctx = s.failure_context
    assert "TOOL_ERROR" in ctx
    assert "search_api" in ctx
    assert "timeout" in ctx

  def test_failure_context_none_when_ok(self):
    s = self._make_span(status="OK")
    assert s.failure_context is None


class TestTraceErrors:
  """Tests for Trace.errors() and error rendering."""

  def _make_trace(self, spans):
    return Trace(
        trace_id="trace-1",
        session_id="sess-1",
        spans=spans,
    )

  def _make_span(
      self,
      span_id,
      parent=None,
      status="OK",
      error_message=None,
      event_type="AGENT_COMPLETED",
      content=None,
  ):
    return Span(
        event_type=event_type,
        agent="agent",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        span_id=span_id,
        parent_span_id=parent,
        status=status,
        error_message=error_message,
        content=content or {},
    )

  def test_errors_returns_error_spans(self):
    spans = [
        self._make_span("s1", status="OK"),
        self._make_span(
            "s2",
            status="ERROR",
            error_message="fail",
            event_type="TOOL_ERROR",
            content={"tool": "my_tool"},
        ),
    ]
    trace = self._make_trace(spans)
    errors = trace.errors()
    assert len(errors) == 1
    assert errors[0]["error_message"] == "fail"
    assert errors[0]["tool"] == "my_tool"
    assert errors[0]["event_type"] == "TOOL_ERROR"

  def test_errors_empty_when_no_errors(self):
    spans = [self._make_span("s1", status="OK")]
    trace = self._make_trace(spans)
    assert trace.errors() == []

  def test_render_shows_warning_for_parent_of_error(self):
    parent = self._make_span("p1", status="OK", event_type="AGENT_STARTING")
    child = self._make_span(
        "c1",
        parent="p1",
        status="ERROR",
        error_message="broken",
        event_type="TOOL_ERROR",
    )
    trace = self._make_trace([parent, child])
    output = trace.render()
    # Parent should show warning icon (U+26A0)
    assert "\u26a0" in output
    # Child should show error icon (U+2717)
    assert "\u2717" in output

  def test_render_no_warning_when_all_ok(self):
    parent = self._make_span("p1", status="OK", event_type="AGENT_STARTING")
    child = self._make_span(
        "c1", parent="p1", status="OK", event_type="TOOL_COMPLETED"
    )
    trace = self._make_trace([parent, child])
    output = trace.render()
    assert "\u26a0" not in output
    assert "\u2717" not in output


class TestEventTypeEnum:
  """Tests for EventType enum completeness."""

  def test_state_delta_exists(self):
    from bigquery_agent_analytics.trace import EventType

    assert EventType.STATE_DELTA.value == "STATE_DELTA"

  def test_hitl_events_exist(self):
    from bigquery_agent_analytics.trace import EventType

    assert EventType.HITL_CONFIRMATION_REQUEST.value == (
        "HITL_CONFIRMATION_REQUEST"
    )
    assert EventType.HITL_CREDENTIAL_REQUEST.value == (
        "HITL_CREDENTIAL_REQUEST"
    )
    assert EventType.HITL_INPUT_REQUEST.value == ("HITL_INPUT_REQUEST")


class TestSpanNewFields:
  """Tests for new Span fields: trace_id and time_to_first_token_ms."""

  def test_from_bigquery_row_with_trace_id(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": "{}",
        "attributes": "{}",
        "trace_id": "trace-abc-123",
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert span.trace_id == "trace-abc-123"

  def test_from_bigquery_row_time_to_first_token_json_string(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": None,
        "attributes": None,
        "latency_ms": '{"total_ms": 500, "time_to_first_token_ms": 120}',
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert span.latency_ms == 500
    assert span.time_to_first_token_ms == 120

  def test_from_bigquery_row_time_to_first_token_dict(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": None,
        "attributes": None,
        "latency_ms": {"total_ms": 300, "time_to_first_token_ms": 80},
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert span.latency_ms == 300
    assert span.time_to_first_token_ms == 80

  def test_from_bigquery_row_no_ttft(self):
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "agent",
        "timestamp": datetime.now(timezone.utc),
        "content": None,
        "attributes": None,
        "latency_ms": '{"total_ms": 200}',
        "status": "OK",
    }
    span = Span.from_bigquery_row(row)
    assert span.latency_ms == 200
    assert span.time_to_first_token_ms is None


class TestHITLAndStateDeltaLabelSummary:
  """Tests for HITL and STATE_DELTA label and summary."""

  def test_hitl_request_label(self):
    span = Span(
        event_type="HITL_CONFIRMATION_REQUEST",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "approve_payment", "args": {"amount": 100}},
    )
    assert "HITL_CONFIRMATION_REQUEST" in span.label
    assert "(approve_payment)" in span.label

  def test_hitl_completed_label(self):
    span = Span(
        event_type="HITL_CONFIRMATION_REQUEST_COMPLETED",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "approve_payment", "result": "approved"},
    )
    assert "HITL_CONFIRMATION_REQUEST_COMPLETED" in span.label
    assert "(approve_payment)" in span.label

  def test_hitl_request_summary(self):
    span = Span(
        event_type="HITL_INPUT_REQUEST",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "get_user_input", "args": {"prompt": "Enter name"}},
    )
    assert "get_user_input" in span.summary
    assert "prompt" in span.summary

  def test_hitl_completed_summary(self):
    span = Span(
        event_type="HITL_INPUT_REQUEST_COMPLETED",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"tool": "get_user_input", "result": "John Doe"},
    )
    assert "get_user_input" in span.summary
    assert "John Doe" in span.summary

  def test_state_delta_label(self):
    span = Span(
        event_type="STATE_DELTA",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"delta": {"counter": 5, "status": "running"}},
    )
    assert "STATE_DELTA" in span.label

  def test_state_delta_summary_with_delta_key(self):
    span = Span(
        event_type="STATE_DELTA",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"delta": {"counter": 5, "status": "running"}},
    )
    assert "counter" in span.summary
    assert "status" in span.summary

  def test_state_delta_summary_from_attributes(self):
    """Plugin-emitted STATE_DELTA stores delta in attributes.state_delta."""
    span = Span(
        event_type="STATE_DELTA",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={},
        attributes={"state_delta": {"counter": 5, "phase": "running"}},
    )
    assert "counter" in span.summary
    assert "phase" in span.summary

  def test_state_delta_summary_attributes_preferred_over_content(self):
    """attributes.state_delta takes priority over content.delta."""
    span = Span(
        event_type="STATE_DELTA",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"delta": {"old_key": 1}},
        attributes={"state_delta": {"new_key": 2}},
    )
    assert "new_key" in span.summary
    assert "old_key" not in span.summary

  def test_state_delta_summary_fallback_to_content_delta(self):
    """Falls back to content.delta when attributes.state_delta is absent."""
    span = Span(
        event_type="STATE_DELTA",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"delta": {"counter": 5, "status": "running"}},
    )
    assert "counter" in span.summary
    assert "status" in span.summary

  def test_state_delta_summary_fallback_to_flat_content(self):
    """Falls back to flat content when neither attributes nor delta key."""
    span = Span(
        event_type="STATE_DELTA",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        content={"progress": 50, "phase": "analysis"},
    )
    assert "progress" in span.summary
    assert "phase" in span.summary


class TestTraceFilterNewFields:
  """Tests for new TraceFilter fields: tool_origin and root_agent_name."""

  def test_tool_origin_filter(self):
    filt = TraceFilter(tool_origin="MCP")
    where, params = filt.to_sql_conditions()
    assert "tool_origin" in where
    assert "@tool_origin" in where
    param_names = [p.name for p in params]
    assert "tool_origin" in param_names

  def test_root_agent_name_filter(self):
    filt = TraceFilter(root_agent_name="my_root_agent")
    where, params = filt.to_sql_conditions()
    assert "root_agent_name" in where
    assert "@root_agent_name" in where
    param_names = [p.name for p in params]
    assert "root_agent_name" in param_names

  def test_combined_with_existing_filters(self):
    filt = TraceFilter(
        agent_id="agent",
        tool_origin="LOCAL",
        root_agent_name="root",
    )
    where, _ = filt.to_sql_conditions()
    assert "agent = @agent_id" in where
    assert "tool_origin" in where
    assert "root_agent_name" in where


class TestTraceFilterNullSafePins:
  """Three-state identity pins on TraceFilter (issue #359, U1)."""

  def test_sql_null_pin_emits_is_null_predicates(self):
    filt = TraceFilter(
        user_id=SQL_NULL,
        root_agent_name=SQL_NULL,
        experiment_id=SQL_NULL,
    )
    where, params = filt.to_sql_conditions()
    assert "user_id IS NULL" in where
    assert "JSON_VALUE(attributes, '$.root_agent_name') IS NULL" in where
    assert "JSON_VALUE(attributes, '$.experiment_id') IS NULL" in where
    # NULL pins bind no equality parameters.
    param_names = {p.name for p in params}
    assert param_names == {"trace_limit"}

  def test_none_still_means_unfiltered(self):
    where, _ = TraceFilter().to_sql_conditions()
    assert "user_id" not in where
    assert "root_agent_name" not in where
    assert "experiment_id" not in where

  def test_empty_string_values_filter_by_equality(self):
    filt = TraceFilter(user_id="", root_agent_name="", experiment_id="")
    where, params = filt.to_sql_conditions()
    assert "user_id = @user_id" in where
    assert (
        "JSON_VALUE(attributes, '$.root_agent_name') = @root_agent_name"
        in where
    )
    assert "JSON_VALUE(attributes, '$.experiment_id') = @experiment_id" in where
    values = {p.name: p.value for p in params if p.name != "trace_limit"}
    assert values == {"user_id": "", "root_agent_name": "", "experiment_id": ""}

  def test_null_and_empty_string_pins_are_distinct_predicates(self):
    null_where, _ = TraceFilter(user_id=SQL_NULL).to_sql_conditions()
    empty_where, _ = TraceFilter(user_id="").to_sql_conditions()
    assert null_where != empty_where


class TestTraceIdentity:
  """Tests for the intrinsic TraceIdentity value object (issue #359, U1)."""

  def test_equality_and_hash_dedup(self):
    a = TraceIdentity(
        session_id="sess-1", user_id="alice", root_agent_name="root"
    )
    b = TraceIdentity(
        session_id="sess-1", user_id="alice", root_agent_name="root"
    )
    assert a == b
    assert len({a, b}) == 1

  def test_same_session_different_user_distinct(self):
    a = TraceIdentity(session_id="sess-1", user_id="alice")
    b = TraceIdentity(session_id="sess-1", user_id="bob")
    c = TraceIdentity(session_id="sess-1", user_id=None)
    assert len({a, b, c}) == 3

  def test_immutable(self):
    identity = TraceIdentity(session_id="sess-1")
    with pytest.raises(AttributeError):
      identity.session_id = "sess-2"


class TestTraceScope:
  """Tests for the caller-selected TraceScope value object."""

  def test_label_canonicalization_dict_order_irrelevant(self):
    a = TraceScope(
        experiment_id="exp-1", custom_labels={"run": "v1", "slice": "3"}
    )
    b = TraceScope(
        experiment_id="exp-1", custom_labels={"slice": "3", "run": "v1"}
    )
    assert a == b
    assert hash(a) == hash(b)
    assert a.scope_signature == b.scope_signature

  def test_scope_signature_distinguishes_passes(self):
    v0 = TraceScope(custom_labels={"run": "v0"})
    v1 = TraceScope(custom_labels={"run": "v1"})
    assert v0.scope_signature != v1.scope_signature

  def test_empty_scope_stable(self):
    empty = TraceScope()
    assert empty == TraceScope()
    assert isinstance(empty.scope_signature, str)

  def test_labels_dict_round_trip(self):
    scope = TraceScope(custom_labels={"b": "2", "a": "1"})
    assert scope.labels_dict == {"a": "1", "b": "2"}

  def test_empty_labels_normalize_to_no_labels(self):
    assert TraceScope(custom_labels={}) == TraceScope()
    assert TraceScope(custom_labels=()) == TraceScope(custom_labels=None)
    assert (
        TraceScope(custom_labels={}).scope_signature
        == TraceScope().scope_signature
    )

  def test_scope_signature_is_versioned(self):
    assert TraceScope().scope_signature.startswith("v1:")

  def test_scope_signature_delimiter_values_do_not_collide(self):
    # A label value containing the old "k=v;k=v" delimiters must not
    # collide with the scope that spells the same payload as two
    # separate labels.
    packed = TraceScope(custom_labels={"a": "b;c=d"})
    split = TraceScope(custom_labels={"a": "b", "c": "d"})
    assert packed.scope_signature != split.scope_signature

  def test_scope_signature_experiment_id_cannot_smuggle_labels(self):
    packed = TraceScope(experiment_id="x;a=b")
    split = TraceScope(experiment_id="x", custom_labels={"a": "b"})
    assert packed.scope_signature != split.scope_signature

  def test_scope_signature_key_value_boundary_stable(self):
    a = TraceScope(custom_labels={"ab": "c"})
    b = TraceScope(custom_labels={"a": "bc"})
    assert a.scope_signature != b.scope_signature

  def test_scope_signature_unicode_labels_distinct_and_stable(self):
    quoted = TraceScope(custom_labels={"k": 'v"w'})
    escaped = TraceScope(custom_labels={"k": "v\\u0022w"})
    assert quoted.scope_signature != escaped.scope_signature
    accented = TraceScope(custom_labels={"k": "café"})
    assert accented.scope_signature == accented.scope_signature
    assert (
        accented.scope_signature
        != TraceScope(custom_labels={"k": "cafe"}).scope_signature
    )

  def test_non_string_label_keys_or_values_rejected(self):
    with pytest.raises(TypeError, match="must be strings"):
      TraceScope(custom_labels={1: "v"})
    with pytest.raises(TypeError, match="must be strings"):
      TraceScope(custom_labels={"k": 1})

  def test_duplicate_label_keys_rejected(self):
    with pytest.raises(ValueError, match="Duplicate custom label key"):
      TraceScope(custom_labels=(("run", "v0"), ("run", "v1")))


class TestTraceSelector:
  """Tests for caller-pin selectors and their TraceFilter mapping."""

  def test_selector_dedup_keeps_collision_candidates(self):
    a = TraceSelector(session_id="sess-1", user_id="alice")
    a_dup = TraceSelector(session_id="sess-1", user_id="alice")
    b = TraceSelector(session_id="sess-1", user_id="bob")
    c = TraceSelector(
        session_id="sess-1", user_id="alice", custom_labels={"run": "v1"}
    )
    deduped = {a, a_dup, b, c}
    assert len(deduped) == 3

  def test_to_trace_filter_maps_pins(self):
    selector = TraceSelector(
        session_id="sess-1",
        user_id="alice",
        root_agent_name="root",
        experiment_id="exp-1",
        custom_labels={"run": "v1"},
    )
    filt = selector.to_trace_filter(limit=7)
    assert filt.session_ids == ["sess-1"]
    assert filt.user_id == "alice"
    assert filt.root_agent_name == "root"
    assert filt.experiment_id == "exp-1"
    assert filt.custom_labels == {"run": "v1"}
    assert filt.limit == 7

  def test_to_trace_filter_unpinned_fields_absent(self):
    filt = TraceSelector(session_id="sess-1").to_trace_filter()
    assert filt.session_ids == ["sess-1"]
    assert filt.user_id is None
    assert filt.root_agent_name is None
    assert filt.experiment_id is None
    assert filt.custom_labels is None

  def test_duplicate_label_keys_rejected(self):
    # A duplicate key would silently drop one value in the dict-based
    # to_trace_filter() conversion, so it is rejected at construction.
    with pytest.raises(ValueError, match="Duplicate custom label key"):
      TraceSelector(
          session_id="sess-1", custom_labels=(("run", "v0"), ("run", "v1"))
      )

  def test_non_string_label_values_rejected(self):
    with pytest.raises(TypeError, match="must be strings"):
      TraceSelector(session_id="sess-1", custom_labels={"slice": 3})

  def test_empty_labels_equal_unlabeled_selector(self):
    assert TraceSelector(session_id="sess-1", custom_labels={}) == (
        TraceSelector(session_id="sess-1")
    )
    filt = TraceSelector(
        session_id="sess-1", custom_labels={}
    ).to_trace_filter()
    assert filt.custom_labels is None

  def test_unset_and_explicit_null_pins_are_distinct(self):
    unpinned = TraceSelector(session_id="sess-1")
    null_pinned = TraceSelector(session_id="sess-1", user_id=None)
    assert unpinned.user_id is UNSET
    assert null_pinned.user_id is None
    assert unpinned != null_pinned
    assert len({unpinned, null_pinned}) == 2

  def test_to_trace_filter_maps_three_pin_states(self):
    filt = TraceSelector(
        session_id="sess-1",
        user_id=None,
        root_agent_name="root",
    ).to_trace_filter()
    # Explicit NULL pin becomes the NULL-safe filter sentinel.
    assert filt.user_id is SQL_NULL
    # Concrete pins pass through; UNSET dimensions stay unfiltered.
    assert filt.root_agent_name == "root"
    assert filt.experiment_id is None

  def test_empty_string_pin_survives_to_sql(self):
    # An empty-string identity value is a concrete value, not an
    # absent one; it must not collapse into an unpinned dimension.
    filt = TraceSelector(session_id="sess-1", user_id="").to_trace_filter()
    where, params = filt.to_sql_conditions()
    assert "user_id = @user_id" in where
    user_param = next(p for p in params if p.name == "user_id")
    assert user_param.value == ""

  def test_sql_null_sentinel_rejected_as_selector_pin(self):
    # The selector spells a NULL pin as explicit None; accepting the
    # filter-side sentinel too would create two unequal spellings of
    # the same pin.
    with pytest.raises(TypeError, match="pin to SQL"):
      TraceSelector(session_id="sess-1", user_id=SQL_NULL)


class TestResolvedTraceSelector:
  """Tests for resolved identity + scope combinations."""

  def test_exposes_identity_scope_and_signature(self):
    resolved = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
        scope=TraceScope(custom_labels={"run": "v0"}),
    )
    assert resolved.identity.session_id == "sess-1"
    assert resolved.scope_signature == resolved.scope.scope_signature

  def test_same_identity_different_scope_distinct(self):
    identity = TraceIdentity(session_id="sess-1", user_id="alice")
    v0 = ResolvedTraceSelector(
        identity=identity, scope=TraceScope(custom_labels={"run": "v0"})
    )
    v1 = ResolvedTraceSelector(
        identity=identity, scope=TraceScope(custom_labels={"run": "v1"})
    )
    assert len({v0, v1}) == 2

  def test_to_selector_round_trips_all_pins(self):
    resolved = ResolvedTraceSelector(
        identity=TraceIdentity(
            session_id="sess-1", user_id="alice", root_agent_name="root"
        ),
        scope=TraceScope(experiment_id="exp-1", custom_labels={"run": "v0"}),
    )
    selector = resolved.to_selector()
    assert selector == TraceSelector(
        session_id="sess-1",
        user_id="alice",
        root_agent_name="root",
        experiment_id="exp-1",
        custom_labels={"run": "v0"},
        scope_signature=resolved.scope_signature,
    )

  def test_to_selector_pins_null_identity_dimensions(self):
    # AE2: a resolved NULL user/root-agent candidate retries as an
    # explicit NULL pin, not as an unpinned session-only request.
    resolved = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1"),
        scope=TraceScope(),
    )
    selector = resolved.to_selector()
    assert selector.user_id is None
    assert selector.root_agent_name is None
    assert selector.experiment_id is None
    assert selector != TraceSelector(session_id="sess-1")
    filt = selector.to_trace_filter()
    where, params = filt.to_sql_conditions()
    assert "user_id IS NULL" in where
    assert "JSON_VALUE(attributes, '$.root_agent_name') IS NULL" in where
    assert "JSON_VALUE(attributes, '$.experiment_id') IS NULL" in where
    param_names = {p.name for p in params}
    assert "user_id" not in param_names
    assert "root_agent_name" not in param_names
    assert "experiment_id" not in param_names

  def test_to_selector_distinguishes_subset_and_superset_scopes(self):
    # AE3: the retry selector for the {'run': 'v1'} pass must not
    # also select the {'run': 'v1', 'slice': '3'} pass, and an
    # unlabeled candidate must not retry as an unpinned scope.
    identity = TraceIdentity(session_id="sess-1", user_id="alice")
    subset = ResolvedTraceSelector(
        identity=identity, scope=TraceScope(custom_labels={"run": "v1"})
    )
    superset = ResolvedTraceSelector(
        identity=identity,
        scope=TraceScope(custom_labels={"run": "v1", "slice": "3"}),
    )
    unlabeled = ResolvedTraceSelector(identity=identity, scope=TraceScope())
    selectors = {
        subset.to_selector(),
        superset.to_selector(),
        unlabeled.to_selector(),
    }
    assert len(selectors) == 3
    assert subset.to_selector().scope_signature == subset.scope_signature
    assert unlabeled.to_selector().scope_signature == unlabeled.scope_signature


class TestAmbiguousSessionError:
  """Tests for the typed ambiguity error (KTD3 redaction contract)."""

  def _candidates(self):
    return [
        ResolvedTraceSelector(
            identity=TraceIdentity(session_id="sess-1", user_id="alice"),
            scope=TraceScope(custom_labels={"run": "v0"}),
        ),
        ResolvedTraceSelector(
            identity=TraceIdentity(session_id="sess-1", user_id="bob"),
            scope=TraceScope(custom_labels={"run": "v1"}),
        ),
    ]

  def test_subclasses_value_error(self):
    err = AmbiguousSessionError(candidates=self._candidates())
    assert isinstance(err, ValueError)

  def test_printable_form_redacts_candidate_values(self):
    err = AmbiguousSessionError(candidates=self._candidates())
    printable = str(err)
    assert "2" in printable
    assert "user_id" in printable
    # Candidate values, session ids, and label values must not leak.
    for leaked in ("alice", "bob", "sess-1", "v0", "v1"):
      assert leaked not in printable

  def test_retry_dimensions_name_differing_fields(self):
    err = AmbiguousSessionError(candidates=self._candidates())
    assert "user_id" in err.retry_dimensions
    assert "custom_labels" in err.retry_dimensions
    assert "root_agent_name" not in err.retry_dimensions

  def test_structured_candidates_full_json_shape(self):
    candidates = self._candidates()
    err = AmbiguousSessionError(candidates=candidates)
    assert err.candidates == tuple(candidates)
    payload = err.to_dict()
    assert payload == {
        "error": "ambiguous_session",
        "candidate_count": 2,
        "retry_dimensions": ["user_id", "custom_labels", "scope_signature"],
        "candidates": [
            {
                "selector": {
                    "session_id": "sess-1",
                    "user_id": "alice",
                    "root_agent_name": None,
                    "experiment_id": None,
                    "custom_labels": {"run": "v0"},
                    "scope_signature": candidates[0].scope_signature,
                },
                "scope_signature": candidates[0].scope_signature,
            },
            {
                "selector": {
                    "session_id": "sess-1",
                    "user_id": "bob",
                    "root_agent_name": None,
                    "experiment_id": None,
                    "custom_labels": {"run": "v1"},
                    "scope_signature": candidates[1].scope_signature,
                },
                "scope_signature": candidates[1].scope_signature,
            },
        ],
    }
    json.dumps(payload)

  def test_candidate_payload_selector_is_retry_ready(self):
    candidates = self._candidates()
    payload = AmbiguousSessionError(candidates=candidates).to_dict()
    for entry, original in zip(payload["candidates"], candidates):
      assert TraceSelector(**entry["selector"]) == original.to_selector()

  def test_null_and_non_null_identity_ambiguity_round_trips(self):
    # AE2: a NULL-user candidate and a non-NULL-user candidate are a
    # real ambiguity, and the NULL candidate's payload retries as an
    # explicit NULL pin — not as the same session-only request that
    # was ambiguous in the first place.
    null_user = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1"),
    )
    named_user = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    err = AmbiguousSessionError(candidates=[null_user, named_user])
    assert err.retry_dimensions == ("user_id",)
    payload = err.to_dict()
    null_selector = payload["candidates"][0]["selector"]
    assert null_selector["user_id"] is None
    assert null_selector["custom_labels"] is None
    retried = TraceSelector(**null_selector)
    assert retried == null_user.to_selector()
    assert retried != TraceSelector(session_id="sess-1")
    where, _ = retried.to_trace_filter().to_sql_conditions()
    assert "user_id IS NULL" in where

  def test_constructor_rejects_non_ambiguous_populations(self):
    single = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    with pytest.raises(ValueError, match="at least two distinct"):
      AmbiguousSessionError(candidates=[single])
    duplicate = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    with pytest.raises(ValueError, match="at least two distinct"):
      AmbiguousSessionError(candidates=[single, duplicate])

  def test_constructor_rejects_cross_session_candidates(self):
    a = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    b = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-2", user_id="alice"),
    )
    with pytest.raises(ValueError, match="share one session_id"):
      AmbiguousSessionError(candidates=[a, b])

  def test_constructor_dedupes_before_counting(self):
    a = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    a_dup = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    b = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="bob"),
    )
    err = AmbiguousSessionError(candidates=[a, a_dup, b])
    assert err.candidates == (a, b)
    assert err.to_dict()["candidate_count"] == 2


class TestResolveSingularCandidate:
  """Tests for legacy session-only key validation (R5/R7 rules)."""

  def _candidate(self, user_id):
    return ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id=user_id),
        scope=TraceScope(),
    )

  def test_single_candidate_accepted(self):
    only = self._candidate("alice")
    assert resolve_singular_candidate([only]) is only

  def test_multiple_candidates_rejected(self):
    with pytest.raises(AmbiguousSessionError):
      resolve_singular_candidate(
          [self._candidate("alice"), self._candidate("bob")]
      )

  def test_zero_candidates_raise_value_error(self):
    with pytest.raises(ValueError, match="No candidates"):
      resolve_singular_candidate([])

  def test_never_picks_newest_implicitly(self):
    # Ordering must not matter: ambiguity is raised regardless of the
    # candidates' order, so no "latest wins" fallback can exist.
    candidates = [self._candidate("bob"), self._candidate("alice")]
    with pytest.raises(AmbiguousSessionError):
      resolve_singular_candidate(list(reversed(candidates)))

  def test_duplicate_rows_of_one_candidate_stay_unambiguous(self):
    # Candidate discovery may return the same resolved selector more
    # than once; duplicates count as one candidate, not an ambiguity.
    only = self._candidate("alice")
    duplicate = self._candidate("alice")
    assert resolve_singular_candidate([only, duplicate]) == only

  def test_duplicates_do_not_mask_distinct_candidates(self):
    with pytest.raises(AmbiguousSessionError) as exc_info:
      resolve_singular_candidate(
          [
              self._candidate("alice"),
              self._candidate("alice"),
              self._candidate("bob"),
          ]
      )
    err = exc_info.value
    assert len(err.candidates) == 2
    assert err.retry_dimensions == ("user_id",)


class TestTraceAdditiveIdentity:
  """Trace gains an additive identity without breaking legacy fields."""

  def test_trace_accepts_identity_and_scope(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        user_id="alice",
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
        scope=TraceScope(custom_labels={"run": "v0"}),
    )
    assert trace.session_id == "sess-1"
    assert trace.user_id == "alice"
    assert trace.trace_id == "t1"
    assert trace.identity.user_id == "alice"
    assert trace.scope.labels_dict == {"run": "v0"}

  def test_trace_identity_defaults_to_none(self):
    trace = Trace(trace_id="t1", session_id="sess-1")
    assert trace.identity is None
    assert trace.scope is None

  def test_contradictory_session_id_rejected(self):
    # The identity is the single authority: mirrored legacy scalars
    # must agree with it so downstream code keyed on either surface
    # sees one identity.
    with pytest.raises(ValueError, match="session_id contradicts"):
      Trace(
          trace_id="t1",
          session_id="legacy-session",
          identity=TraceIdentity(session_id="different-session"),
      )

  def test_contradictory_user_id_rejected(self):
    with pytest.raises(ValueError, match="user_id contradicts"):
      Trace(
          trace_id="t1",
          session_id="sess-1",
          user_id="legacy-user",
          identity=TraceIdentity(session_id="sess-1", user_id="other-user"),
      )
    # An identity resolved with a NULL user also contradicts a
    # non-NULL legacy scalar.
    with pytest.raises(ValueError, match="user_id contradicts"):
      Trace(
          trace_id="t1",
          session_id="sess-1",
          user_id="legacy-user",
          identity=TraceIdentity(session_id="sess-1"),
      )

  def test_unset_user_id_backfilled_from_identity(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    assert trace.user_id == "alice"

  def test_mutating_session_id_against_identity_rejected(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    with pytest.raises(ValueError, match="session_id contradicts"):
      trace.session_id = "sess-2"
    # The failed write must not leave a desynchronized object behind.
    assert trace.session_id == "sess-1"

  def test_mutating_user_id_against_identity_rejected(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    with pytest.raises(ValueError, match="user_id contradicts"):
      trace.user_id = "mallory"
    with pytest.raises(ValueError, match="cannot be cleared"):
      trace.user_id = None
    assert trace.user_id == "alice"

  def test_identity_immutable_once_attached(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    # Any replacement is rejected, even when the new identity would
    # be consistent with the current scalar mirrors.
    with pytest.raises(ValueError, match="cannot be replaced"):
      trace.identity = TraceIdentity(session_id="sess-2", user_id="alice")
    with pytest.raises(ValueError, match="cannot be replaced"):
      trace.identity = TraceIdentity(session_id="sess-1", user_id="bob")
    # Detaching would let the mirrors be retagged afterwards.
    with pytest.raises(ValueError, match="cannot be replaced"):
      trace.identity = None
    # Idempotent equal re-assignment stays allowed.
    trace.identity = TraceIdentity(session_id="sess-1", user_id="alice")
    assert trace.user_id == "alice"

  def test_null_user_identity_cannot_be_retagged(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1"),
    )
    # A NULL-user identity must not upgrade to a named user, and a
    # root-agent-only swap must not slip past the scalar mirrors.
    with pytest.raises(ValueError, match="cannot be replaced"):
      trace.identity = TraceIdentity(session_id="sess-1", user_id="mallory")
    assert trace.user_id is None

  def test_root_agent_only_replacement_rejected(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1", root_agent_name="root-a"),
    )
    with pytest.raises(ValueError, match="cannot be replaced"):
      trace.identity = TraceIdentity(
          session_id="sess-1", root_agent_name="root-b"
      )
    assert trace.identity.root_agent_name == "root-a"

  def test_consistent_writes_and_serialization_stay_aligned(self):
    from bigquery_agent_analytics.serialization import serialize

    trace = Trace(trace_id="t1", session_id="sess-1")
    # Legacy traces without identity remain freely mutable.
    trace.user_id = "temp"
    trace.user_id = None
    # Attaching an identity later backfills the mirror.
    trace.identity = TraceIdentity(session_id="sess-1", user_id="alice")
    assert trace.user_id == "alice"
    # Idempotent consistent writes are allowed.
    trace.user_id = "alice"
    trace.session_id = "sess-1"
    result = serialize(trace)
    assert result["session_id"] == result["identity"]["session_id"]
    assert result["user_id"] == result["identity"]["user_id"]


class TestPinSentinelDurability:
  """Sentinels must keep singleton identity through pickle (round 3)."""

  def test_sentinels_survive_pickle(self):
    import pickle

    assert pickle.loads(pickle.dumps(UNSET)) is UNSET
    assert pickle.loads(pickle.dumps(SQL_NULL)) is SQL_NULL

  def test_pickled_selector_keeps_unset_semantics(self):
    import pickle

    restored = pickle.loads(pickle.dumps(TraceSelector(session_id="sess-1")))
    assert restored.user_id is UNSET
    assert restored == TraceSelector(session_id="sess-1")
    filt = restored.to_trace_filter()
    where, _ = filt.to_sql_conditions()
    assert "user_id" not in where

  def test_pickled_filter_keeps_null_pin_semantics(self):
    import pickle

    restored = pickle.loads(pickle.dumps(TraceFilter(user_id=SQL_NULL)))
    where, params = restored.to_sql_conditions()
    assert "user_id IS NULL" in where
    assert all(p.name != "user_id" for p in params)

  def test_sentinels_survive_copy_and_deepcopy(self):
    import copy

    assert copy.copy(UNSET) is UNSET
    assert copy.deepcopy(SQL_NULL) is SQL_NULL
    cloned = copy.deepcopy(TraceSelector(session_id="sess-1"))
    assert cloned.user_id is UNSET


class TestAmbiguousSessionErrorDurability:
  """The error must survive copy/deepcopy/pickle intact (round 3)."""

  def _error(self):
    return AmbiguousSessionError(
        candidates=[
            ResolvedTraceSelector(
                identity=TraceIdentity(session_id="sess-1", user_id="alice"),
            ),
            ResolvedTraceSelector(
                identity=TraceIdentity(session_id="sess-1"),
            ),
        ]
    )

  def test_copy_and_deepcopy_preserve_candidates(self):
    import copy

    err = self._error()
    for clone in (copy.copy(err), copy.deepcopy(err)):
      assert clone.candidates == err.candidates
      assert clone.retry_dimensions == err.retry_dimensions
      assert clone.to_dict() == err.to_dict()

  def test_pickle_round_trip_preserves_payload(self):
    import pickle

    err = self._error()
    restored = pickle.loads(pickle.dumps(err))
    assert restored.candidates == err.candidates
    assert restored.to_dict() == err.to_dict()
    assert str(restored) == str(err)


class TestScopeSignatureRetryGuidance:
  """scope_signature must be hinted when only scope payloads differ."""

  def _colliding(self, labels_a, labels_b):
    identity = TraceIdentity(session_id="sess-1", user_id="alice")
    return AmbiguousSessionError(
        candidates=[
            ResolvedTraceSelector(
                identity=identity, scope=TraceScope(custom_labels=labels_a)
            ),
            ResolvedTraceSelector(
                identity=identity, scope=TraceScope(custom_labels=labels_b)
            ),
        ]
    )

  def test_subset_superset_collision_hints_scope_signature(self):
    err = self._colliding({"run": "v1"}, {"run": "v1", "slice": "3"})
    assert "scope_signature" in err.retry_dimensions
    assert "custom_labels" in err.retry_dimensions
    assert "scope_signature" in str(err)

  def test_unlabeled_labeled_collision_hints_scope_signature(self):
    err = self._colliding(None, {"run": "v1"})
    assert "scope_signature" in err.retry_dimensions
    assert "scope_signature" in str(err)

  def test_identity_only_collision_does_not_hint_scope_signature(self):
    err = AmbiguousSessionError(
        candidates=[
            ResolvedTraceSelector(
                identity=TraceIdentity(session_id="sess-1", user_id="alice"),
            ),
            ResolvedTraceSelector(
                identity=TraceIdentity(session_id="sess-1", user_id="bob"),
            ),
        ]
    )
    assert err.retry_dimensions == ("user_id",)


class TestLabelPairNormalization:
  """Canonical labels must be rebuilt pairs, not caller containers."""

  def test_json_round_trip_normalizes_and_hashes(self):
    from bigquery_agent_analytics.serialization import serialize

    original = TraceScope(custom_labels=(("a", "b"),))
    payload = serialize(original)
    assert payload["custom_labels"] == [["a", "b"]]
    restored = TraceScope(custom_labels=payload["custom_labels"])
    assert restored == original
    assert hash(restored) == hash(original)
    assert restored.scope_signature == original.scope_signature

  def test_string_entry_rejected(self):
    # "ab" would unpack into ("a", "b") but is not a (key, value) pair.
    with pytest.raises(TypeError, match="two-item"):
      TraceScope(custom_labels=["ab"])

  def test_wrong_arity_entries_rejected(self):
    with pytest.raises(TypeError, match="two-item"):
      TraceScope(custom_labels=(("a", "b", "c"),))
    with pytest.raises(TypeError, match="two-item"):
      TraceScope(custom_labels=(("a",),))


class TestConstructorTypeBoundaries:
  """Public models reject values that break value semantics (round 3)."""

  def test_trace_identity_rejects_non_string_fields(self):
    with pytest.raises(TypeError, match="session_id must be a string"):
      TraceIdentity(session_id=1)
    with pytest.raises(TypeError, match="user_id must be a string"):
      TraceIdentity(session_id="sess-1", user_id=True)
    with pytest.raises(TypeError, match="root_agent_name must be a string"):
      TraceIdentity(session_id="sess-1", root_agent_name=3)

  def test_trace_scope_rejects_non_string_experiment_id(self):
    # 1 and True compare equal but sign differently, so both must be
    # rejected instead of silently deduplicating distinct scopes.
    with pytest.raises(TypeError, match="experiment_id must be a string"):
      TraceScope(experiment_id=1)
    with pytest.raises(TypeError, match="experiment_id must be a string"):
      TraceScope(experiment_id=True)

  def test_trace_filter_rejects_unset_and_foreign_values(self):
    with pytest.raises(TypeError, match="TraceFilter.user_id"):
      TraceFilter(user_id=UNSET)
    with pytest.raises(TypeError, match="TraceFilter.root_agent_name"):
      TraceFilter(root_agent_name=UNSET)
    with pytest.raises(TypeError, match="TraceFilter.experiment_id"):
      TraceFilter(experiment_id=1)
    # The supported states still construct.
    TraceFilter(user_id=None, root_agent_name=SQL_NULL, experiment_id="e")

  def test_trace_selector_rejects_non_string_session_and_signature(self):
    with pytest.raises(TypeError, match="session_id must be a string"):
      TraceSelector(session_id=1)
    with pytest.raises(TypeError, match="scope_signature must be a string"):
      TraceSelector(session_id="sess-1", scope_signature=1)


class TestScopeSignatureGolden:
  """Exact golden encoding of the v1 scope signature."""

  def test_full_signature_golden(self):
    scope = TraceScope(
        experiment_id="exp-1", custom_labels={"b": "2", "a": "1"}
    )
    assert scope.scope_signature == (
        'v1:{"custom_labels":[["a","1"],["b","2"]],"experiment_id":"exp-1"}'
    )

  def test_empty_signature_golden(self):
    assert TraceScope().scope_signature == (
        'v1:{"custom_labels":[],"experiment_id":null}'
    )


class TestPackageRootExports:
  """The identity contract is importable from the package root."""

  def test_all_identity_names_exported(self):
    import bigquery_agent_analytics as bqaa

    for name in (
        "TraceIdentity",
        "TraceScope",
        "TraceSelector",
        "ResolvedTraceSelector",
        "AmbiguousSessionError",
        "resolve_singular_candidate",
        "UNSET",
        "SQL_NULL",
    ):
      assert hasattr(bqaa, name), name
      assert name in bqaa.__all__, name
    assert bqaa.UNSET is UNSET
    assert bqaa.SQL_NULL is SQL_NULL


class TestIdentityFieldDeletion:
  """del must not bypass the lifetime identity invariant (round 4)."""

  def test_delete_detach_retag_blocked(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    for name in ("identity", "session_id", "user_id"):
      with pytest.raises(AttributeError, match="cannot be deleted"):
        delattr(trace, name)
    # The object is untouched and still guarded after the attempts.
    assert trace.identity == TraceIdentity(session_id="sess-1", user_id="alice")
    with pytest.raises(ValueError, match="session_id contradicts"):
      trace.session_id = "sess-2"

  def test_delete_blocked_without_identity_too(self):
    trace = Trace(trace_id="t1", session_id="sess-1")
    with pytest.raises(AttributeError, match="cannot be deleted"):
      del trace.session_id

  def test_other_fields_remain_deletable(self):
    trace = Trace(trace_id="t1", session_id="sess-1")
    trace.extra_note = "x"
    del trace.extra_note


class TestTraceComponentTypes:
  """Trace requires the concrete immutable value objects (round 4)."""

  def test_duck_typed_identity_rejected(self):
    from types import SimpleNamespace

    fake = SimpleNamespace(
        session_id="sess-1", user_id="alice", root_agent_name=None
    )
    with pytest.raises(TypeError, match="must be a TraceIdentity"):
      Trace(trace_id="t1", session_id="sess-1", user_id="alice", identity=fake)
    trace = Trace(trace_id="t1", session_id="sess-1")
    with pytest.raises(TypeError, match="must be a TraceIdentity"):
      trace.identity = fake

  def test_duck_typed_scope_rejected(self):
    with pytest.raises(TypeError, match="must be a TraceScope"):
      Trace(trace_id="t1", session_id="sess-1", scope={"run": "v0"})
    trace = Trace(trace_id="t1", session_id="sess-1")
    with pytest.raises(TypeError, match="must be a TraceScope"):
      trace.scope = object()
    trace.scope = TraceScope(custom_labels={"run": "v0"})
    # Once attached, scope follows the attach-once contract
    # (see TestScopeProvenanceInvariant).
    trace.scope = TraceScope(custom_labels={"run": "v0"})


class TestResolvedSelectorComponentTypes:
  """Candidates must be real value objects before dedup (round 4)."""

  def test_foreign_identity_rejected_at_construction(self):
    with pytest.raises(TypeError, match="must be a TraceIdentity"):
      ResolvedTraceSelector(identity=1, scope=TraceScope())
    with pytest.raises(TypeError, match="must be a TraceIdentity"):
      ResolvedTraceSelector(identity=True, scope=TraceScope())
    with pytest.raises(TypeError, match="must be a TraceScope"):
      ResolvedTraceSelector(
          identity=TraceIdentity(session_id="sess-1"), scope="scope"
      )

  def test_resolver_rejects_foreign_candidates_before_dedup(self):
    real = ResolvedTraceSelector(identity=TraceIdentity(session_id="sess-1"))
    with pytest.raises(TypeError, match="ResolvedTraceSelector instances"):
      resolve_singular_candidate([real, "sess-1"])
    with pytest.raises(TypeError, match="ResolvedTraceSelector instances"):
      resolve_singular_candidate([1, True])

  def test_error_rejects_foreign_candidates(self):
    real = ResolvedTraceSelector(identity=TraceIdentity(session_id="sess-1"))
    with pytest.raises(TypeError, match="ResolvedTraceSelector instances"):
      AmbiguousSessionError(candidates=[real, object()])


class TestTraceFilterLifetimeValidation:
  """Tri-state pin validation must survive mutation (round 4)."""

  def test_post_construction_unset_assignment_rejected(self):
    filt = TraceFilter()
    with pytest.raises(TypeError, match="TraceFilter.user_id"):
      filt.user_id = UNSET
    with pytest.raises(TypeError, match="TraceFilter.root_agent_name"):
      filt.root_agent_name = UNSET
    with pytest.raises(TypeError, match="TraceFilter.experiment_id"):
      filt.experiment_id = 1
    # Failed writes leave the filter unchanged and SQL clean.
    where, params = filt.to_sql_conditions()
    assert "user_id" not in where
    assert all(p.name == "trace_limit" for p in params)

  def test_valid_mutations_still_allowed(self):
    filt = TraceFilter()
    filt.user_id = SQL_NULL
    filt.root_agent_name = "root"
    filt.experiment_id = None
    where, _ = filt.to_sql_conditions()
    assert "user_id IS NULL" in where
    assert "@root_agent_name" in where


class TestLabelKeyJsonPathQuoting:
  """Label keys must be quoted JSONPath segments (round 5, P1)."""

  def _key_param(self, labels):
    _, params = TraceFilter(custom_labels=labels).to_sql_conditions()
    return next(p for p in params if p.name == "label_key_0").value

  def test_simple_key_is_quoted(self):
    assert self._key_param({"run": "v1"}) == '"run"'

  def test_dotted_key_stays_one_segment(self):
    # $.custom_tags."a.b" selects the literal member; the unquoted
    # form would traverse into a nested object and return NULL.
    assert self._key_param({"a.b": "x"}) == '"a.b"'

  def test_bracket_key_quoted(self):
    assert self._key_param({"a[0]": "x"}) == '"a[0]"'

  def test_quote_keys_escaped_backslash_keys_literal(self):
    assert self._key_param({'a"b': "x"}) == '"a\\"b"'
    # BigQuery's JSONPath grammar matches backslashes literally inside
    # quoted members; doubling them silently matches nothing (verified
    # against live BigQuery — see test_trace_identity_bigquery_live).
    assert self._key_param({"a\\b": "x"}) == '"a\\b"'

  def test_empty_key_quoted(self):
    assert self._key_param({"": "x"}) == '""'

  def test_value_not_quoted(self):
    _, params = TraceFilter(custom_labels={"a.b": "v.w"}).to_sql_conditions()
    val = next(p for p in params if p.name == "label_val_0").value
    assert val == "v.w"


class TestStrSubclassNormalization:
  """Equality-overriding str subclasses must not drive identity
  decisions (round 5)."""

  class _Collider(str):
    """Compares equal to everything; hashes into one bucket."""

    def __eq__(self, other):
      return True

    def __ne__(self, other):
      return False

    def __hash__(self):
      return 0

  def test_identity_fields_normalized_to_exact_str(self):
    identity = TraceIdentity(
        session_id=self._Collider("sess-1"),
        user_id=self._Collider("alice"),
        root_agent_name=self._Collider("root"),
    )
    for value in (
        identity.session_id,
        identity.user_id,
        identity.root_agent_name,
    ):
      assert type(value) is str
    assert identity.user_id == "alice"

  def test_colliding_user_ids_still_ambiguous(self):
    alice = ResolvedTraceSelector(
        identity=TraceIdentity("sess-1", user_id=self._Collider("alice"))
    )
    bob = ResolvedTraceSelector(
        identity=TraceIdentity("sess-1", user_id=self._Collider("bob"))
    )
    with pytest.raises(AmbiguousSessionError):
      resolve_singular_candidate([alice, bob])

  def test_trace_mirror_check_uses_exact_str(self):
    with pytest.raises(ValueError, match="session_id contradicts"):
      Trace(
          trace_id="t1",
          session_id=self._Collider("legacy"),
          identity=TraceIdentity(session_id="different"),
      )

  def test_scope_and_selector_and_filter_normalized(self):
    scope = TraceScope(
        experiment_id=self._Collider("exp"),
        custom_labels={self._Collider("k"): self._Collider("v")},
    )
    assert type(scope.experiment_id) is str
    key, value = scope.custom_labels[0]
    assert type(key) is str and type(value) is str
    selector = TraceSelector(
        session_id=self._Collider("sess-1"),
        user_id=self._Collider("alice"),
    )
    assert type(selector.session_id) is str
    assert type(selector.user_id) is str
    filt = TraceFilter(user_id=self._Collider("alice"))
    assert type(filt.user_id) is str

  def test_value_object_subclasses_rejected(self):
    class EvilIdentity(TraceIdentity):

      def __eq__(self, other):
        return True

      def __hash__(self):
        return 0

    evil = EvilIdentity(session_id="sess-1")
    with pytest.raises(TypeError, match="must be a TraceIdentity"):
      ResolvedTraceSelector(identity=evil)
    with pytest.raises(TypeError, match="must be a TraceIdentity"):
      Trace(trace_id="t1", session_id="sess-1", identity=evil)


class TestPinSentinelImmutability:
  """Sentinel state must be sealed (round 5)."""

  def test_name_writes_rejected(self):
    with pytest.raises(AttributeError, match="immutable"):
      UNSET._name = "SQL_NULL"
    with pytest.raises(AttributeError, match="immutable"):
      SQL_NULL._name = "UNSET"
    with pytest.raises(AttributeError, match="immutable"):
      del UNSET._name

  def test_pickle_and_wire_names_derived_from_identity(self):
    import pickle

    from bigquery_agent_analytics.serialization import serialize

    # Force the display name out of sync through the object protocol
    # escape hatch; encodings must still follow singleton identity.
    object.__setattr__(UNSET, "_name", "SQL_NULL")
    try:
      assert pickle.loads(pickle.dumps(UNSET)) is UNSET
      assert serialize(SQL_NULL) == {"$pin": "SQL_NULL"}
      restored = pickle.loads(pickle.dumps(TraceSelector(session_id="sess-1")))
      assert restored.user_id is UNSET
    finally:
      object.__setattr__(UNSET, "_name", "UNSET")


class TestExportsIndependentOfClient:
  """U1 contract must import without optional client deps (round 5)."""

  def test_identity_exports_survive_blocked_client_import(self):
    import os
    import pathlib
    import subprocess
    import sys
    import textwrap

    # Pin this checkout's src so the subprocess cannot silently test
    # another installed/checked-out copy of the package.
    repo_src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")

    code = textwrap.dedent(
        """
        import importlib.abc
        import pathlib
        import sys

        class Blocker(importlib.abc.MetaPathFinder):
          def find_spec(self, fullname, path=None, target=None):
            if fullname == "bigquery_agent_analytics.client":
              raise ImportError("blocked for test")
            return None

        sys.meta_path.insert(0, Blocker())
        import bigquery_agent_analytics as bqaa

        expected_src = pathlib.Path(sys.argv[1]).resolve()
        actual = pathlib.Path(bqaa.__file__).resolve()
        assert expected_src in actual.parents, (
            f"imported {actual}, expected under {expected_src}"
        )

        names = [
            "TraceIdentity", "TraceScope", "TraceSelector",
            "ResolvedTraceSelector", "AmbiguousSessionError",
            "resolve_singular_candidate", "UNSET", "SQL_NULL",
            "decode_pin",
        ]
        missing = [
            n for n in names
            if not hasattr(bqaa, n) or n not in bqaa.__all__
        ]
        assert not missing, f"missing: {missing}"
        assert not hasattr(bqaa, "Client")
        print("OK")
    """
    )
    result = subprocess.run(
        [sys.executable, "-c", code, repo_src],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


class TestResolvedSelectorExactType:
  """Subclassed candidates must not reach dedup (round 6, P1)."""

  class _EvilSelector(ResolvedTraceSelector):
    """Bypasses component checks and collapses all comparisons."""

    def __post_init__(self):
      pass

    def __eq__(self, other):
      return True

    def __hash__(self):
      return 0

  def _evil(self, user_id):
    return self._EvilSelector(
        identity=TraceIdentity(session_id="sess-1", user_id=user_id)
    )

  def test_resolver_rejects_subclass_candidates(self):
    with pytest.raises(TypeError, match="exact"):
      resolve_singular_candidate([self._evil("alice"), self._evil("bob")])

  def test_error_rejects_subclass_candidates(self):
    real = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice")
    )
    with pytest.raises(TypeError, match="exact"):
      AmbiguousSessionError(candidates=[real, self._evil("bob")])


class TestTraceMirrorTypeEnforcement:
  """Foreign mirror values must be rejected before comparison
  (round 6, P1)."""

  class _Sneaky:
    """Comparison methods report equality with everything."""

    def __eq__(self, other):
      return True

    def __ne__(self, other):
      return False

    def __hash__(self):
      return 0

  def test_foreign_user_id_rejected(self):
    trace = Trace(
        trace_id="t1",
        session_id="sess-1",
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
    )
    with pytest.raises(TypeError, match="user_id must be a string"):
      trace.user_id = self._Sneaky()
    assert trace.user_id == "alice"

  def test_foreign_session_id_rejected(self):
    trace = Trace(trace_id="t1", session_id="sess-1")
    with pytest.raises(TypeError, match="session_id must be a string"):
      trace.session_id = self._Sneaky()
    with pytest.raises(TypeError, match="session_id must be a string"):
      Trace(trace_id="t1", session_id=self._Sneaky())

  def test_none_session_id_rejected(self):
    with pytest.raises(TypeError, match="session_id must be a string"):
      Trace(trace_id="t1", session_id=None)


class TestScopeProvenanceInvariant:
  """Scope provenance follows the attach-once contract (round 6, P1)."""

  def _scoped_trace(self):
    return Trace(
        trace_id="t1",
        session_id="sess-1",
        scope=TraceScope(custom_labels={"run": "v0"}),
    )

  def test_replacement_rejected(self):
    trace = self._scoped_trace()
    with pytest.raises(ValueError, match="scope cannot be replaced"):
      trace.scope = TraceScope(custom_labels={"run": "v1"})
    assert trace.scope.labels_dict == {"run": "v0"}

  def test_clearing_and_deletion_rejected(self):
    trace = self._scoped_trace()
    with pytest.raises(ValueError, match="scope cannot be replaced"):
      trace.scope = None
    with pytest.raises(AttributeError, match="cannot be deleted"):
      del trace.scope
    assert trace.scope.labels_dict == {"run": "v0"}

  def test_late_attachment_and_idempotent_reassignment_allowed(self):
    trace = Trace(trace_id="t1", session_id="sess-1")
    assert trace.scope is None
    trace.scope = TraceScope(custom_labels={"run": "v0"})
    trace.scope = TraceScope(custom_labels={"run": "v0"})
    assert trace.scope.labels_dict == {"run": "v0"}


class TestSurrogateRejection:
  """Surrogate code units must fail closed (round 6, P2)."""

  # Explicit high+low surrogate code units vs the astral scalar:
  # Python treats them as distinct strings, but JSON escaping maps
  # both to \ud83d\ude00, collapsing them on the wire.
  _SURROGATE = "\ud83d" "\ude00"
  _ASTRAL = "\U0001f600"

  def test_python_distinguishes_the_two_forms(self):
    assert self._SURROGATE != self._ASTRAL
    assert len(self._SURROGATE) == 2
    assert len(self._ASTRAL) == 1

  def test_surrogates_rejected_at_every_boundary(self):
    with pytest.raises(ValueError, match="surrogate"):
      TraceIdentity(session_id="sess-1", user_id=self._SURROGATE)
    with pytest.raises(ValueError, match="surrogate"):
      TraceScope(custom_labels={"k": self._SURROGATE})
    with pytest.raises(ValueError, match="surrogate"):
      TraceScope(experiment_id=self._SURROGATE)
    with pytest.raises(ValueError, match="surrogate"):
      TraceSelector(session_id=self._SURROGATE)
    with pytest.raises(ValueError, match="surrogate"):
      TraceFilter(user_id=self._SURROGATE)
    with pytest.raises(ValueError, match="surrogate"):
      TraceFilter(custom_labels={"k": self._SURROGATE})
    with pytest.raises(ValueError, match="surrogate"):
      Trace(trace_id="t1", session_id=self._SURROGATE)

  def test_astral_scalars_still_accepted(self):
    scope = TraceScope(custom_labels={"k": self._ASTRAL})
    assert scope.labels_dict == {"k": self._ASTRAL}

  def test_ambiguity_payload_json_round_trip_stays_distinct(self):
    import json as json_mod

    identity = TraceIdentity(session_id="sess-1", user_id="alice")
    err = AmbiguousSessionError(
        candidates=[
            ResolvedTraceSelector(
                identity=identity,
                scope=TraceScope(custom_labels={"k": self._ASTRAL}),
            ),
            ResolvedTraceSelector(
                identity=identity,
                scope=TraceScope(custom_labels={"k": "plain"}),
            ),
        ]
    )
    restored = json_mod.loads(json_mod.dumps(err.to_dict()))
    selectors = [TraceSelector(**c["selector"]) for c in restored["candidates"]]
    assert selectors[0] != selectors[1]
    signatures = {c["scope_signature"] for c in restored["candidates"]}
    assert len(signatures) == 2


class TestTraceFilterLabelNormalization:
  """Direct filter labels must not retain caller hooks (round 6)."""

  class _Redirect(str):
    """replace() rewrites content to hijack JSONPath quoting."""

    def replace(self, *args, **kwargs):
      return "hijacked"

  def test_subclass_key_cannot_redirect_jsonpath(self):
    filt = TraceFilter(custom_labels={self._Redirect("k"): "v"})
    key = next(iter(filt.custom_labels))
    assert type(key) is str
    _, params = filt.to_sql_conditions()
    key_param = next(p for p in params if p.name == "label_key_0")
    assert key_param.value == '"k"'

  def test_values_normalized_and_non_strings_rejected(self):
    filt = TraceFilter(custom_labels={"k": self._Redirect("v")})
    assert type(filt.custom_labels["k"]) is str
    with pytest.raises(TypeError, match="keys and values must be strings"):
      TraceFilter(custom_labels={"k": 1})
    with pytest.raises(TypeError, match="must be a dict"):
      TraceFilter(custom_labels=[("k", "v")])

  def test_post_construction_label_assignment_normalized(self):
    filt = TraceFilter()
    filt.custom_labels = {self._Redirect("k"): "v"}
    assert type(next(iter(filt.custom_labels))) is str


class TestAmbiguityStateReadOnly:
  """Validated error state must not be reassignable (round 6)."""

  def _error(self):
    return AmbiguousSessionError(
        candidates=[
            ResolvedTraceSelector(
                identity=TraceIdentity(session_id="sess-1", user_id="alice")
            ),
            ResolvedTraceSelector(
                identity=TraceIdentity(session_id="sess-1", user_id="bob")
            ),
        ]
    )

  def test_candidates_and_retry_dimensions_read_only(self):
    err = self._error()
    with pytest.raises(AttributeError):
      err.candidates = (err.candidates[0],)
    with pytest.raises(AttributeError):
      err.retry_dimensions = ()
    assert err.to_dict()["candidate_count"] == 2

  def test_message_payload_and_pickle_stay_consistent(self):
    import pickle

    err = self._error()
    assert "2" in str(err)
    assert err.to_dict()["candidate_count"] == 2
    restored = pickle.loads(pickle.dumps(err))
    assert restored.to_dict() == err.to_dict()


class TestFilterLabelMutationDurability:
  """In-place label mutation must not bypass validation (round 7)."""

  class _Redirect(str):

    def replace(self, *args, **kwargs):
      return "hijacked"

  def test_dict_style_mutation_keeps_working(self):
    # Base-commit compatibility: ordinary dict mutation is public
    # behavior and must not raise.
    filt = TraceFilter(custom_labels={"k": "v"})
    filt.custom_labels["slice"] = "3"
    filt.custom_labels.update({"run": "v1"})
    filt.custom_labels |= {"extra": "e"}
    assert filt.custom_labels.setdefault("k", "other") == "v"
    filt.custom_labels.pop("extra")
    assert filt.custom_labels == {"k": "v", "slice": "3", "run": "v1"}

  def test_in_place_writes_validated(self):
    filt = TraceFilter(custom_labels={"k": "v"})
    with pytest.raises(TypeError, match="must be strings"):
      filt.custom_labels["n"] = 1
    with pytest.raises(TypeError, match="must be strings"):
      filt.custom_labels.update({1: "v"})
    with pytest.raises(ValueError, match="surrogate"):
      filt.custom_labels["s"] = "\ud800"
    hijack = self._Redirect("x")
    filt.custom_labels[hijack] = self._Redirect("y")
    assert type(list(filt.custom_labels)[-1]) is str
    assert type(filt.custom_labels["x"]) is str
    assert filt.custom_labels == {"k": "v", "x": "y"}

  def test_low_level_injection_cannot_reach_sql(self):
    filt = TraceFilter(custom_labels={"k": "v"})
    # dict.__setitem__ bypasses the subclass mutators; the SQL
    # boundary revalidates a snapshot, so the hijacking key is
    # normalized before JSONPath quoting.
    dict.__setitem__(filt.custom_labels, self._Redirect("x"), "y")
    _, params = filt.to_sql_conditions()
    keys = {p.value for p in params if p.name.startswith("label_key")}
    assert keys == {'"k"', '"x"'}

  def test_low_level_injected_garbage_fails_closed_at_sql(self):
    filt = TraceFilter(custom_labels={"k": "v"})
    dict.__setitem__(filt.custom_labels, "n", 1)
    with pytest.raises(TypeError, match="must be strings"):
      filt.to_sql_conditions()

  def test_labels_pickle_and_copy_keep_validating(self):
    import copy
    import pickle

    filt = TraceFilter(custom_labels={"k": "v"})
    restored = pickle.loads(pickle.dumps(filt))
    assert restored.custom_labels == {"k": "v"}
    with pytest.raises(TypeError, match="must be strings"):
      restored.custom_labels["x"] = 1
    cloned = copy.deepcopy(filt)
    assert cloned.custom_labels == {"k": "v"}
    with pytest.raises(TypeError, match="must be strings"):
      cloned.custom_labels["x"] = 1


class TestFilterLabelDuplicateNormalization:
  """Keys colliding after normalization must fail closed (round 7)."""

  def _distinct_subclass_keys(self):
    class KeyA(str):

      def __hash__(self):
        return 11

      def __eq__(self, other):
        return self is other

    class KeyB(str):

      def __hash__(self):
        return 22

      def __eq__(self, other):
        return self is other

    return KeyA("k"), KeyB("k")

  def test_normalization_collision_raises(self):
    key_a, key_b = self._distinct_subclass_keys()
    source = {key_a: "first", key_b: "second"}
    assert len(source) == 2  # distinct keys before normalization
    with pytest.raises(ValueError, match="Duplicate custom label key"):
      TraceFilter(custom_labels=source)


class TestSessionIdsIdentitySurface:
  """session_ids follows the exact-string contract (round 7)."""

  class _Collider(str):

    def __eq__(self, other):
      return True

    def __hash__(self):
      return 0

  def test_entries_normalized_and_copied_on_assignment(self):
    source = [self._Collider("sess-1")]
    filt = TraceFilter(session_ids=source)
    assert type(filt.session_ids[0]) is str
    # The stored list is a copy: mutating the source has no effect.
    source.append("sess-2")
    assert filt.session_ids == ["sess-1"]

  def test_non_string_and_surrogate_entries_rejected(self):
    with pytest.raises(TypeError, match="entries must be strings"):
      TraceFilter(session_ids=["sess-1", 2])
    with pytest.raises(TypeError, match="list of strings"):
      TraceFilter(session_ids="sess-1")
    surrogate = "\ud800"
    with pytest.raises(ValueError, match="surrogate"):
      TraceFilter(session_ids=[surrogate])

  def test_in_place_mutation_validated_at_write_and_boundary(self):
    filt = TraceFilter(session_ids=["sess-1"])
    filt.session_ids.append(self._Collider("sess-2"))
    assert type(filt.session_ids[1]) is str
    _, params = filt.to_sql_conditions()
    array = next(p for p in params if p.name == "session_ids")
    assert [type(v) is str for v in array.values] == [True, True]
    # Ordinary mutation is validated at write time...
    with pytest.raises(TypeError, match="entries must be strings"):
      filt.session_ids.append(3)
    with pytest.raises(ValueError, match="surrogate"):
      filt.session_ids.extend(["\ud800"])
    with pytest.raises(TypeError, match="entries must be strings"):
      filt.session_ids[0] = 1
    # ...and a low-level bypass is still caught at the SQL boundary.
    list.append(filt.session_ids, 3)
    with pytest.raises(TypeError, match="entries must be strings"):
      filt.to_sql_conditions()


class TestUnaddressableLabelKeys:
  """Keys BigQuery JSONPath cannot encode fail closed (round 9, P1)."""

  # Live-verified parity rule: only ODD-length backslash runs before
  # a quote or at the end of the key are invalid in BigQuery JSONPath.
  UNADDRESSABLE = ["a\\", 'a\\"b', "a\\\\\\", 'a\\\\\\"b']
  ADDRESSABLE = [
      "a\\b",
      "a\\\\b",
      "\\a",
      'a"b',
      "a.b",
      "",
      "a\\\\",  # even trailing run: valid (live-verified)
      'a\\\\"b',  # even run before quote: valid (live-verified)
  ]

  def test_rejected_at_segment_construction(self):
    for key in self.UNADDRESSABLE:
      with pytest.raises(ValueError, match="cannot be addressed"):
        trace_module._jsonpath_member_segment(key)

  def test_rejected_at_filter_label_validation(self):
    for key in self.UNADDRESSABLE:
      with pytest.raises(ValueError, match="cannot be addressed"):
        TraceFilter(custom_labels={key: "x"})
    filt = TraceFilter(custom_labels={"k": "v"})
    with pytest.raises(ValueError, match="cannot be addressed"):
      filt.custom_labels["a\\"] = "x"

  def test_addressable_backslash_shapes_still_accepted(self):
    for key in self.ADDRESSABLE:
      filt = TraceFilter(custom_labels={key: "x"})
      _, params = filt.to_sql_conditions()
      segment = next(p for p in params if p.name == "label_key_0").value
      assert segment == trace_module._jsonpath_member_segment(key)


class TestSessionIdsSelfExtension:
  """Batch writes terminate and are atomic (round 9, P1)."""

  def test_self_extend_terminates_and_doubles(self):
    filt = TraceFilter(session_ids=["a", "b"])
    filt.session_ids.extend(filt.session_ids)
    assert filt.session_ids == ["a", "b", "a", "b"]

  def test_self_iadd_terminates(self):
    filt = TraceFilter(session_ids=["a"])
    filt.session_ids += filt.session_ids
    assert filt.session_ids == ["a", "a"]

  def test_failed_batch_commits_nothing(self):
    filt = TraceFilter(session_ids=["a"])
    with pytest.raises(TypeError, match="entries must be strings"):
      filt.session_ids.extend(["ok", 3])
    assert filt.session_ids == ["a"]


class TestLabelBatchUpdateAtomicity:
  """update()/|= normalize the whole batch first (round 9, P2)."""

  def _colliding_keys(self):
    class KeyA(str):

      def __hash__(self):
        return 11

      def __eq__(self, other):
        return self is other

    class KeyB(str):

      def __hash__(self):
        return 22

      def __eq__(self, other):
        return self is other

    return KeyA("k"), KeyB("k")

  def test_update_detects_normalized_collision(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    key_a, key_b = self._colliding_keys()
    with pytest.raises(ValueError, match="Duplicate custom label key"):
      filt.custom_labels.update({key_a: "first", key_b: "second"})
    assert filt.custom_labels == {"run": "v1"}

  def test_update_failure_commits_nothing(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    with pytest.raises(TypeError, match="must be strings"):
      filt.custom_labels.update([("good", "y"), ("bad", 1)])
    assert "good" not in filt.custom_labels
    assert filt.custom_labels == {"run": "v1"}

  def test_ior_shares_the_atomic_path(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    with pytest.raises(TypeError, match="must be strings"):
      filt.custom_labels |= {"bad": 1}
    assert filt.custom_labels == {"run": "v1"}

  def test_legitimate_batch_update_overwrites_existing(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    filt.custom_labels.update({"run": "v2", "slice": "3"})
    assert filt.custom_labels == {"run": "v2", "slice": "3"}


class TestValidatedContainersStdlibCompat:
  """The containers must behave like dict/list to stdlib (round 9)."""

  def test_dataclasses_asdict_round_trips(self):
    import dataclasses

    filt = TraceFilter(custom_labels={"k": "v"}, session_ids=["s1"])
    plain = dataclasses.asdict(filt)
    assert plain["custom_labels"] == {"k": "v"}
    assert plain["session_ids"] == ["s1"]

  def test_labels_constructor_matches_dict_forms(self):
    from bigquery_agent_analytics.trace import _ValidatedLabels

    assert _ValidatedLabels() == {}
    assert _ValidatedLabels({"a": "1"}) == {"a": "1"}
    assert _ValidatedLabels([("a", "1")]) == {"a": "1"}
    assert _ValidatedLabels((pair for pair in [("a", "1")])) == {"a": "1"}
    assert _ValidatedLabels(a="1") == {"a": "1"}


class TestRemovalOperandNormalization:
  """Equality-overriding operands must not misdirect removals
  (round 9, P2)."""

  class _Evil(str):
    """Claims equality with everything; hash collides broadly."""

    def __eq__(self, other):
      return True

    def __ne__(self, other):
      return False

    def __hash__(self):
      return hash("run")

  def test_mapping_lookup_and_removal_normalized(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    evil = self._Evil("unrelated")
    assert evil not in filt.custom_labels
    assert filt.custom_labels.get(evil) is None
    with pytest.raises(KeyError):
      filt.custom_labels[evil]
    with pytest.raises(KeyError):
      filt.custom_labels.pop(evil)
    with pytest.raises(KeyError):
      del filt.custom_labels[evil]
    assert filt.custom_labels == {"run": "v1"}
    # A well-behaved subclass operand still finds the real entry.
    assert filt.custom_labels.pop(self._Evil("run")) == "v1"

  def test_list_membership_and_removal_normalized(self):
    filt = TraceFilter(session_ids=["sess-1", "sess-2"])
    evil = self._Evil("unrelated")
    assert evil not in filt.session_ids
    assert filt.session_ids.count(evil) == 0
    with pytest.raises(ValueError):
      filt.session_ids.remove(evil)
    with pytest.raises(ValueError):
      filt.session_ids.index(evil)
    assert filt.session_ids == ["sess-1", "sess-2"]
    filt.session_ids.remove(self._Evil("sess-1"))
    assert filt.session_ids == ["sess-2"]


class TestDecodePinExactness:
  """Hostile equality must never mint a sentinel (round 9, P2)."""

  class _FakeTag(str):

    def __eq__(self, other):
      return True

    def __ne__(self, other):
      return False

    def __hash__(self):
      return hash("UNSET")

  def test_fake_tag_passes_through_unchanged(self):
    from bigquery_agent_analytics.trace import decode_pin

    payload = {"$pin": self._FakeTag("not-a-pin")}
    result = decode_pin(payload)
    assert result is payload
    assert result is not UNSET

  def test_fake_key_passes_through_unchanged(self):
    from bigquery_agent_analytics.trace import decode_pin

    payload = {self._FakeTag("not-the-key"): "UNSET"}
    assert decode_pin(payload) is payload

  def test_exact_wire_form_still_resolves(self):
    from bigquery_agent_analytics.trace import decode_pin

    assert decode_pin({"$pin": "UNSET"}) is UNSET
    assert decode_pin({"$pin": "SQL_NULL"}) is SQL_NULL
    assert decode_pin({"$pin": "bogus"}) == {"$pin": "bogus"}


class TestAugmentedAssignmentAliasing:
  """+=/|= must keep aliases attached and avoid rewrap (round 9)."""

  def test_ior_preserves_container_identity(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    alias = filt.custom_labels
    filt.custom_labels |= {"slice": "3"}
    assert filt.custom_labels is alias
    assert alias == {"run": "v1", "slice": "3"}

  def test_iadd_preserves_container_identity(self):
    filt = TraceFilter(session_ids=["a"])
    alias = filt.session_ids
    filt.session_ids += ["b"]
    assert filt.session_ids is alias
    assert alias == ["a", "b"]

  def test_external_assignment_still_copies(self):
    filt_a = TraceFilter(session_ids=["a"])
    filt_b = TraceFilter(session_ids=["b"])
    filt_a.session_ids = filt_b.session_ids
    assert filt_a.session_ids == ["b"]
    assert filt_a.session_ids is not filt_b.session_ids


class TestSetdefaultLookupFirst:
  """setdefault must not validate an unused default (round 9, P3)."""

  def test_existing_key_ignores_default(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    assert filt.custom_labels.setdefault("run") == "v1"
    assert filt.custom_labels.setdefault("run", 123) == "v1"

  def test_missing_key_validates_default(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    with pytest.raises(TypeError, match="must be strings"):
      filt.custom_labels.setdefault("new")
    assert filt.custom_labels.setdefault("new", "x") == "x"
    assert filt.custom_labels["new"] == "x"


class TestRetryableUnaddressableScopeLabels:
  """Resolved candidates with unaddressable label keys must still
  produce an executable one-step retry (round 10, P1)."""

  def _candidate(self):
    # BigQuery JSON can store this member even though its quoted
    # JSONPath form does not exist.
    return ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1", user_id="alice"),
        scope=TraceScope(custom_labels={"a\\": "x", "run": "v1"}),
    )

  def test_retry_payload_round_trip_is_executable(self):
    payload = json.loads(json.dumps(self._candidate().to_retry_payload()))
    selector = TraceSelector(**payload["selector"])
    filt = selector.to_trace_filter()
    where, params = filt.to_sql_conditions()
    # The addressable label still pre-filters; the unaddressable one
    # is excluded from SQL (the signature pin preserves exactness).
    keys = {p.value for p in params if p.name.startswith("label_key")}
    assert keys == {'"run"'}
    assert selector.scope_signature == self._candidate().scope_signature
    assert filt.session_ids == ["sess-1"]
    assert "session_id IN UNNEST(@session_ids)" in where

  def test_signatureless_selector_fails_closed(self):
    selector = TraceSelector(session_id="sess-1", custom_labels={"a\\": "x"})
    with pytest.raises(ValueError, match="scope_signature"):
      selector.to_trace_filter()

  def test_all_labels_unaddressable_drops_predicates_entirely(self):
    resolved = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="sess-1"),
        scope=TraceScope(custom_labels={"a\\": "x"}),
    )
    filt = resolved.to_selector().to_trace_filter()
    _, params = filt.to_sql_conditions()
    assert not [p for p in params if p.name.startswith("label_key")]


class TestNonStringOperandShortCircuit:
  """Hostile non-string operands must never drive comparisons
  (round 10, P2)."""

  class _EvilObject:
    """Not a string; hash collides with 'run', equality matches all."""

    def __eq__(self, other):
      return True

    def __ne__(self, other):
      return False

    def __hash__(self):
      return hash("run")

  def test_mapping_misses_without_comparison(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    evil = self._EvilObject()
    assert evil not in filt.custom_labels
    assert filt.custom_labels.get(evil) is None
    assert filt.custom_labels.get(evil, "d") == "d"
    with pytest.raises(KeyError):
      filt.custom_labels[evil]
    with pytest.raises(KeyError):
      filt.custom_labels.pop(evil)
    assert filt.custom_labels.pop(evil, "d") == "d"
    with pytest.raises(KeyError):
      del filt.custom_labels[evil]
    assert filt.custom_labels == {"run": "v1"}

  def test_list_misses_without_comparison(self):
    filt = TraceFilter(session_ids=["sess-1", "sess-2"])
    evil = self._EvilObject()
    assert evil not in filt.session_ids
    assert filt.session_ids.count(evil) == 0
    with pytest.raises(ValueError):
      filt.session_ids.remove(evil)
    with pytest.raises(ValueError):
      filt.session_ids.index(evil)
    assert filt.session_ids == ["sess-1", "sess-2"]


class TestUpdateRawPairParsing:
  """Hostile equality must not collapse distinct keys before
  normalization (round 10, P2)."""

  class _E(str):
    """Colliding hash, always-true equality — but distinct chars."""

    def __eq__(self, other):
      return True

    def __ne__(self, other):
      return False

    def __hash__(self):
      return 0

  def test_distinct_character_keys_survive_hostile_equality(self):
    filt = TraceFilter(custom_labels={})
    filt.custom_labels.update([(self._E("a"), "1"), (self._E("b"), "2")])
    assert filt.custom_labels == {"a": "1", "b": "2"}

  def test_same_character_hostile_keys_still_fail_closed(self):
    filt = TraceFilter(custom_labels={})
    with pytest.raises(ValueError, match="Duplicate custom label key"):
      filt.custom_labels.update([(self._E("k"), "1"), (self._E("k"), "2")])
    assert filt.custom_labels == {}

  def test_mapping_protocol_and_kwargs_still_work(self):
    filt = TraceFilter(custom_labels={})

    class MappingLike:

      def keys(self):
        return ["m"]

      def __getitem__(self, key):
        return "1"

    filt.custom_labels.update(MappingLike(), extra="2")
    assert filt.custom_labels == {"m": "1", "extra": "2"}
    with pytest.raises(TypeError, match="at most 1 argument"):
      filt.custom_labels.update({}, {})


class TestSignatureAttestedLabelDrop:
  """Only a signature attesting the dropped pins may drop them
  (round 11, P1)."""

  UNADDRESSABLE = {"a\\": "x"}

  def test_mismatched_signature_cannot_erase_pin(self):
    victim = TraceScope(custom_labels={"other": "y"})
    selector = TraceSelector(
        session_id="s",
        custom_labels=self.UNADDRESSABLE,
        scope_signature=victim.scope_signature,
    )
    with pytest.raises(ValueError, match="does not attest"):
      selector.to_trace_filter()

  def test_signature_with_wrong_value_rejected(self):
    wrong_value = TraceScope(custom_labels={"a\\": "DIFFERENT"})
    selector = TraceSelector(
        session_id="s",
        custom_labels=self.UNADDRESSABLE,
        scope_signature=wrong_value.scope_signature,
    )
    with pytest.raises(ValueError, match="does not attest"):
      selector.to_trace_filter()

  def test_malformed_signature_rejected(self):
    for bogus in ("v1:not-json", "v2:{}", 'v1:{"custom_labels":"x"}'):
      selector = TraceSelector(
          session_id="s",
          custom_labels=self.UNADDRESSABLE,
          scope_signature=bogus,
      )
      with pytest.raises(ValueError, match="does not attest"):
        selector.to_trace_filter()

  def test_attesting_signature_still_drops(self):
    scope = TraceScope(custom_labels={"a\\": "x", "run": "v1"})
    resolved = ResolvedTraceSelector(
        identity=TraceIdentity(session_id="s"), scope=scope
    )
    filt = resolved.to_selector().to_trace_filter()
    _, params = filt.to_sql_conditions()
    keys = {p.value for p in params if p.name.startswith("label_key")}
    assert keys == {'"run"'}


class TestMissPathsAvoidCallerHooks:
  """Miss behavior must not run caller __repr__ and must keep native
  arity validation (round 11, P2)."""

  class _LoudRepr:
    """Counts (and could raise in) __repr__; equality matches all."""

    def __init__(self):
      self.repr_calls = 0

    def __repr__(self):
      self.repr_calls += 1
      raise RuntimeError("repr must not be invoked")

    def __eq__(self, other):
      return True

    def __hash__(self):
      return hash("run")

  def test_mapping_misses_never_invoke_repr(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    loud = self._LoudRepr()
    assert loud not in filt.custom_labels
    assert filt.custom_labels.get(loud) is None
    with pytest.raises(KeyError):
      filt.custom_labels[loud]
    with pytest.raises(KeyError):
      filt.custom_labels.pop(loud)
    with pytest.raises(KeyError):
      del filt.custom_labels[loud]
    assert loud.repr_calls == 0
    assert filt.custom_labels == {"run": "v1"}

  def test_list_misses_never_invoke_repr(self):
    filt = TraceFilter(session_ids=["sess-1"])
    loud = self._LoudRepr()
    assert loud not in filt.session_ids
    assert filt.session_ids.count(loud) == 0
    with pytest.raises(ValueError):
      filt.session_ids.remove(loud)
    with pytest.raises(ValueError):
      filt.session_ids.index(loud)
    assert loud.repr_calls == 0

  def test_pop_arity_validated_on_miss_path(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    with pytest.raises(TypeError):
      filt.custom_labels.pop(object(), "d1", "d2")
    assert filt.custom_labels.pop(object(), "d1") == "d1"

  def test_index_bounds_arity_validated_on_miss_path(self):
    filt = TraceFilter(session_ids=["sess-1"])
    with pytest.raises(TypeError):
      filt.session_ids.index(object(), 0, 1, 2)
    # Native start/stop bounds still apply on the miss path.
    with pytest.raises(ValueError):
      filt.session_ids.index(object(), 0, 1)


class TestExactDuplicateLastWriteWins:
  """Ordinary exact-str duplicates keep dict semantics (round 11)."""

  def test_mapping_plus_kwargs_last_write_wins(self):
    filt = TraceFilter(custom_labels={})
    filt.custom_labels.update({"run": "v1"}, run="v2")
    assert filt.custom_labels == {"run": "v2"}

  def test_pair_iterable_last_write_wins(self):
    filt = TraceFilter(custom_labels={})
    filt.custom_labels.update([("run", "v1"), ("run", "v2")])
    assert filt.custom_labels == {"run": "v2"}

  def test_constructor_pairs_last_write_wins(self):
    from bigquery_agent_analytics.trace import _ValidatedLabels

    assert _ValidatedLabels([("run", "v1"), ("run", "v2")]) == {"run": "v2"}

  def test_hostile_subclass_collisions_still_fail_closed(self):
    class KeyA(str):

      def __hash__(self):
        return 11

      def __eq__(self, other):
        return self is other

    filt = TraceFilter(custom_labels={})
    with pytest.raises(ValueError, match="Duplicate custom label key"):
      filt.custom_labels.update([(KeyA("run"), "v1"), ("run", "v2")])
    with pytest.raises(ValueError, match="Duplicate custom label key"):
      filt.custom_labels.update([("run", "v1"), (KeyA("run"), "v2")])
    assert filt.custom_labels == {}


class TestSpoofedClassProperty:
  """A spoofed __class__ must not reach the str copy path
  (round 12, P2)."""

  class _FakeStr:
    """Claims to be a str via __class__; hooks would raise."""

    @property
    def __class__(self):
      return str

    def __eq__(self, other):
      raise RuntimeError("comparison hook must not run")

    def __hash__(self):
      return hash("run")

    def __repr__(self):
      raise RuntimeError("repr hook must not run")

  def test_label_lookups_miss_without_hooks(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    fake = self._FakeStr()
    assert isinstance(fake, str)  # the spoof works on isinstance
    assert fake not in filt.custom_labels
    assert filt.custom_labels.get(fake) is None
    assert filt.custom_labels == {"run": "v1"}

  def test_session_lookups_miss_without_hooks(self):
    filt = TraceFilter(session_ids=["sess-1"])
    fake = self._FakeStr()
    assert fake not in filt.session_ids
    assert filt.session_ids.count(fake) == 0
    assert filt.session_ids == ["sess-1"]


class TestStrictCanonicalSignatureAttestation:
  """Only signatures a real TraceScope generates attest label drops
  (round 12, P2)."""

  UNADDRESSABLE = {"a\\": "x"}

  def _selector(self, signature):
    return TraceSelector(
        session_id="s",
        custom_labels=self.UNADDRESSABLE,
        scope_signature=signature,
    )

  def test_impossible_signatures_rejected(self):
    labels_json = '[["a\\\\","x"]]'
    impossible = [
        # Missing / extra schema fields.
        'v1:{"custom_labels":%s}' % labels_json,
        'v1:{"custom_labels":%s,"experiment_id":null,"extra":1}' % labels_json,
        # Duplicate label keys.
        'v1:{"custom_labels":[["a\\\\","x"],["a\\\\","x"]],'
        '"experiment_id":null}',
        # Non-canonical encoding: unsorted keys / added whitespace.
        'v1:{"experiment_id":null,"custom_labels":%s}' % labels_json,
        'v1:{"custom_labels": %s,"experiment_id":null}' % labels_json,
        # Wrong types.
        'v1:{"custom_labels":%s,"experiment_id":1}' % labels_json,
    ]
    for signature in impossible:
      with pytest.raises(ValueError, match="does not attest"):
        self._selector(signature).to_trace_filter()

  def test_deeply_nested_signature_rejected_not_crashing(self):
    deep = "v1:" + "[" * 200000
    with pytest.raises(ValueError, match="does not attest"):
      self._selector(deep).to_trace_filter()

  def test_genuine_signature_still_attests(self):
    genuine = TraceScope(custom_labels=self.UNADDRESSABLE).scope_signature
    filt = self._selector(genuine).to_trace_filter()
    _, params = filt.to_sql_conditions()
    assert not [p for p in params if p.name.startswith("label_key")]


class TestUnhashableOperandNativeBehavior:
  """Mappings raise TypeError for unhashable operands like a plain
  dict; lists keep native comparison-miss semantics (round 12, P2)."""

  def test_mapping_raises_type_error(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    for operand in ([], {}, set()):
      with pytest.raises(TypeError, match="unhashable"):
        filt.custom_labels[operand]
      with pytest.raises(TypeError, match="unhashable"):
        operand in filt.custom_labels
      with pytest.raises(TypeError, match="unhashable"):
        filt.custom_labels.get(operand)
      with pytest.raises(TypeError, match="unhashable"):
        filt.custom_labels.pop(operand, "default")

  def test_list_keeps_native_miss_semantics(self):
    # Plain lists compare rather than hash: `[] in ['a']` is False.
    filt = TraceFilter(session_ids=["sess-1"])
    assert [] not in filt.session_ids
    assert filt.session_ids.count([]) == 0
    with pytest.raises(ValueError):
      filt.session_ids.remove([])

  def test_hashable_non_string_still_misses(self):
    filt = TraceFilter(custom_labels={"run": "v1"})
    assert 3 not in filt.custom_labels
    with pytest.raises(KeyError):
      filt.custom_labels[3]


class TestHostileMetaclassNameRetrieval:
  """Error formatting must not execute metaclass hooks
  (round 12, P3)."""

  def test_candidate_type_error_avoids_metaclass_name(self):
    class Meta(type):

      @property
      def __name__(cls):
        raise RuntimeError("metaclass hook must not run")

    class Hostile(metaclass=Meta):
      pass

    with pytest.raises(TypeError, match="Hostile"):
      resolve_singular_candidate([Hostile()])
