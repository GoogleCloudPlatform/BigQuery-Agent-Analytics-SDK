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

import pytest

from bigquery_agent_analytics.trace import ContentPart
from bigquery_agent_analytics.trace import ObjectRef
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceFilter


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
