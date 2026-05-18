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

"""Unit tests for pure helpers in scripts/latency_report.py.

Tests cover format_ms, _span_label, _extract_text, _build_json_output,
and render_summary_table — all functions that can be exercised without
a live BigQuery connection.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import os
import sys

import pytest

# Make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

try:
  from latency_report import _build_json_output  # noqa: E402
  from latency_report import _extract_text
  from latency_report import _span_label
  from latency_report import format_ms
  from latency_report import render_summary_table

  _SKIP = False
except ImportError:
  _SKIP = True

pytestmark = pytest.mark.skipif(
    _SKIP, reason="latency_report requires bigquery_agent_analytics SDK"
)


# ---------------------------------------------------------------------------
# Lightweight stubs
# ---------------------------------------------------------------------------


class _FakeSpan:

  def __init__(
      self,
      event_type,
      content,
      agent=None,
      latency_ms=None,
      span_id=None,
      parent_span_id=None,
      timestamp=None,
      time_to_first_token_ms=None,
      is_error=False,
      error_message=None,
  ):
    self.event_type = event_type
    self.content = content
    self.agent = agent
    self.latency_ms = latency_ms
    self.span_id = span_id
    self.parent_span_id = parent_span_id
    self.timestamp = timestamp or datetime.now(tz=timezone.utc)
    self.time_to_first_token_ms = time_to_first_token_ms
    self.is_error = is_error
    self.error_message = error_message
    self.attributes = {}
    self.children = []


class _FakeTrace:

  def __init__(
      self, spans, total_latency_ms=None, session_id="sess-1", start_time=None
  ):
    self.spans = spans
    self.total_latency_ms = total_latency_ms
    self.session_id = session_id
    self.start_time = start_time or datetime.now(tz=timezone.utc)


# ================================================================== #
# format_ms                                                           #
# ================================================================== #


class TestFormatMs:

  def test_none(self):
    assert format_ms(None) == "?"

  def test_zero(self):
    assert format_ms(0) == "0ms"

  def test_sub_second(self):
    assert format_ms(450) == "450ms"

  def test_seconds(self):
    result = format_ms(3500)
    assert result == "3.5s"

  def test_minutes(self):
    result = format_ms(90000)
    assert result == "1.5min"

  def test_exactly_one_second(self):
    assert format_ms(1000) == "1.0s"

  def test_exactly_one_minute(self):
    assert format_ms(60000) == "1.0min"


# ================================================================== #
# _span_label                                                         #
# ================================================================== #


class TestSpanLabel:

  def test_basic_event(self):
    span = _FakeSpan("LLM_RESPONSE", {}, agent="my_agent")
    label = _span_label(span)
    assert "my_agent" in label
    assert "LLM_RESPONSE" in label

  def test_no_agent(self):
    span = _FakeSpan("LLM_RESPONSE", {})
    label = _span_label(span)
    assert label == "LLM_RESPONSE"

  def test_tool_event(self):
    span = _FakeSpan("TOOL_COMPLETED", {"tool": "search_kb"}, agent="policy")
    label = _span_label(span)
    assert "search_kb" in label
    assert "TOOL_COMPLETED" in label

  def test_a2a_with_remote(self):
    span = _FakeSpan(
        "A2A_INTERACTION",
        {"metadata": {"adk_app_name": "remote_agent"}},
        agent="supervisor",
    )
    label = _span_label(span)
    assert "A2A" in label
    assert "remote_agent" in label

  def test_a2a_without_remote(self):
    span = _FakeSpan("A2A_INTERACTION", {}, agent="supervisor")
    label = _span_label(span)
    assert "supervisor" in label
    assert "A2A_INTERACTION" in label


# ================================================================== #
# _extract_text                                                        #
# ================================================================== #


class TestExtractText:

  def test_user_message(self):
    span = _FakeSpan("USER_MESSAGE_RECEIVED", {"text": "Hello world"})
    assert _extract_text(span) == "Hello world"

  def test_text_summary_preferred(self):
    span = _FakeSpan(
        "USER_MESSAGE_RECEIVED",
        {"text_summary": "Summary", "text": "Full text"},
    )
    assert _extract_text(span) == "Summary"

  def test_llm_response(self):
    span = _FakeSpan("LLM_RESPONSE", {"response": "The answer is 42"})
    assert _extract_text(span) == "The answer is 42"

  def test_call_prefix_skipped(self):
    span = _FakeSpan("LLM_RESPONSE", {"response": "call:tool_name"})
    assert _extract_text(span) is None

  def test_none_content(self):
    span = _FakeSpan("LLM_RESPONSE", None)
    assert _extract_text(span) is None

  def test_string_content(self):
    span = _FakeSpan("USER_MESSAGE_RECEIVED", "plain string")
    text = _extract_text(span)
    assert text is not None
    assert "plain string" in text

  def test_function_call(self):
    span = _FakeSpan(
        "LLM_RESPONSE",
        {"function_call": {"name": "search", "args": {"q": "test"}}},
    )
    text = _extract_text(span)
    assert "search" in text

  def test_artifact_text(self):
    span = _FakeSpan(
        "A2A_INTERACTION",
        {"artifacts": [{"parts": [{"text": "artifact content"}]}]},
    )
    text = _extract_text(span)
    assert text == "artifact content"

  def test_truncation(self):
    long_text = "x" * 300
    span = _FakeSpan("USER_MESSAGE_RECEIVED", {"text": long_text})
    text = _extract_text(span)
    assert len(text) <= 200


# ================================================================== #
# _build_json_output                                                   #
# ================================================================== #


class TestBuildJsonOutput:

  def test_empty_traces(self):
    result = _build_json_output([])
    assert result["summary"]["sessions"] == 0
    assert result["sessions"] == []

  def test_single_trace(self):
    now = datetime.now(tz=timezone.utc)
    trace = _FakeTrace(
        spans=[
            _FakeSpan(
                "LLM_RESPONSE",
                {},
                agent="agent_a",
                latency_ms=500,
            ),
        ],
        total_latency_ms=1200,
        session_id="s1",
        start_time=now,
    )
    result = _build_json_output([trace])
    assert result["summary"]["sessions"] == 1
    assert result["summary"]["avg_ms"] == 1200
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["session_id"] == "s1"

  def test_per_agent_stats(self):
    trace = _FakeTrace(
        spans=[
            _FakeSpan(
                "LLM_RESPONSE",
                {},
                agent="agent_a",
                latency_ms=500,
            ),
            _FakeSpan(
                "TOOL_COMPLETED",
                {"tool": "t"},
                agent="agent_b",
                latency_ms=300,
            ),
        ],
        total_latency_ms=1000,
    )
    result = _build_json_output([trace])
    assert "agent_a" in result["per_agent"]
    assert "agent_b" in result["per_agent"]


# ================================================================== #
# render_summary_table                                                 #
# ================================================================== #


class TestRenderSummaryTable:

  def test_single_trace_returns_empty(self):
    trace = _FakeTrace([], total_latency_ms=1000)
    assert render_summary_table([trace]) == ""

  def test_multiple_traces(self):
    t1 = _FakeTrace([], total_latency_ms=1000, session_id="s1")
    t2 = _FakeTrace([], total_latency_ms=2000, session_id="s2")
    result = render_summary_table([t1, t2])
    assert "Summary" in result
    assert "Avg" in result
    assert "P50" in result

  def test_no_latency_data(self):
    t1 = _FakeTrace([], total_latency_ms=None, session_id="s1")
    t2 = _FakeTrace([], total_latency_ms=None, session_id="s2")
    result = render_summary_table([t1, t2])
    assert "No latency data" in result
