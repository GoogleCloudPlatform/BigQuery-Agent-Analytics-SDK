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

"""Tests for trace-based evaluation harness."""

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.trace_evaluator import BigQueryTraceEvaluator
from bigquery_agent_analytics.trace_evaluator import EvalStatus
from bigquery_agent_analytics.trace_evaluator import MatchType
from bigquery_agent_analytics.trace_evaluator import ReplayContext
from bigquery_agent_analytics.trace_evaluator import SessionTrace
from bigquery_agent_analytics.trace_evaluator import ToolCall
from bigquery_agent_analytics.trace_evaluator import TraceEvent
from bigquery_agent_analytics.trace_evaluator import TraceReplayRunner
from bigquery_agent_analytics.trace_evaluator import TrajectoryMetrics


class TestTraceEvent:
  """Tests for TraceEvent class."""

  def test_from_bigquery_row_basic(self):
    """Test creating TraceEvent from basic BigQuery row."""
    row = {
        "event_type": "TOOL_STARTING",
        "agent": "test_agent",
        "timestamp": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        "content": '{"tool": "search", "args": {"query": "test"}}',
        "attributes": '{"key": "value"}',
        "span_id": "span-123",
        "parent_span_id": "parent-456",
        "status": "OK",
        "error_message": None,
    }

    event = TraceEvent.from_bigquery_row(row)

    assert event.event_type == "TOOL_STARTING"
    assert event.agent == "test_agent"
    assert event.content == {"tool": "search", "args": {"query": "test"}}
    assert event.attributes == {"key": "value"}
    assert event.span_id == "span-123"
    assert event.status == "OK"

  def test_from_bigquery_row_with_latency(self):
    """Test creating TraceEvent with latency data."""
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "test_agent",
        "timestamp": datetime.now(timezone.utc),
        "content": None,
        "attributes": None,
        "latency_ms": '{"total_ms": 500, "time_to_first_token_ms": 100}',
        "status": "OK",
    }

    event = TraceEvent.from_bigquery_row(row)

    assert event.latency_ms == 500

  def test_from_bigquery_row_with_dict_latency(self):
    """Test creating TraceEvent with dict latency data."""
    row = {
        "event_type": "LLM_RESPONSE",
        "agent": "test_agent",
        "timestamp": datetime.now(timezone.utc),
        "content": None,
        "attributes": None,
        "latency_ms": {"total_ms": 300},
        "status": "OK",
    }

    event = TraceEvent.from_bigquery_row(row)

    assert event.latency_ms == 300


class TestSessionTrace:
  """Tests for SessionTrace class."""

  def test_extract_tool_trajectory(self):
    """Test extracting tool trajectory from events."""
    events = [
        TraceEvent(
            event_type="TOOL_STARTING",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"tool": "search", "args": {"query": "weather"}},
            attributes={},
            span_id="span-1",
        ),
        TraceEvent(
            event_type="TOOL_COMPLETED",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"tool": "search", "result": {"data": "sunny"}},
            attributes={},
            span_id="span-1",
        ),
        TraceEvent(
            event_type="TOOL_STARTING",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"tool": "format", "args": {}},
            attributes={},
            span_id="span-2",
        ),
        TraceEvent(
            event_type="TOOL_ERROR",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"tool": "format"},
            attributes={},
            span_id="span-2",
            status="ERROR",
            error_message="Format failed",
        ),
    ]

    trace = SessionTrace(
        session_id="sess-1",
        user_id="user-1",
        events=events,
    )

    tool_calls = trace.extract_tool_trajectory()

    assert len(tool_calls) == 2
    assert tool_calls[0].tool_name == "search"
    assert tool_calls[0].args == {"query": "weather"}
    assert tool_calls[0].status == "OK"
    assert tool_calls[1].tool_name == "format"
    assert tool_calls[1].status == "ERROR"

  def test_extract_final_response_from_agent_completed(self):
    """Test extracting final response from AGENT_COMPLETED event."""
    events = [
        TraceEvent(
            event_type="AGENT_COMPLETED",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"response": "The weather is sunny."},
            attributes={},
        ),
    ]

    trace = SessionTrace(
        session_id="sess-1",
        user_id="user-1",
        events=events,
    )

    response = trace.extract_final_response()

    assert response == "The weather is sunny."

  def test_extract_final_response_fallback_to_llm(self):
    """Test fallback to LLM_RESPONSE for final response."""
    events = [
        TraceEvent(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"response": "LLM says hello."},
            attributes={},
        ),
    ]

    trace = SessionTrace(
        session_id="sess-1",
        user_id="user-1",
        events=events,
    )

    response = trace.extract_final_response()

    assert response == "LLM says hello."


class TestTrajectoryMetrics:
  """Tests for TrajectoryMetrics class."""

  def test_exact_match_perfect(self):
    """Test exact match with perfect trajectory."""
    actual = [
        ToolCall(tool_name="search", args={"query": "test"}),
        ToolCall(tool_name="format", args={}),
    ]
    expected = [
        {"tool_name": "search", "args": {"query": "test"}},
        {"tool_name": "format"},
    ]

    score = TrajectoryMetrics.compute_exact_match(actual, expected)

    assert score == 1.0

  def test_exact_match_wrong_order(self):
    """Test exact match with wrong order."""
    actual = [
        ToolCall(tool_name="format", args={}),
        ToolCall(tool_name="search", args={"query": "test"}),
    ]
    expected = [
        {"tool_name": "search"},
        {"tool_name": "format"},
    ]

    score = TrajectoryMetrics.compute_exact_match(actual, expected)

    assert score == 0.0

  def test_exact_match_different_length(self):
    """Test exact match with different lengths."""
    actual = [
        ToolCall(tool_name="search", args={}),
    ]
    expected = [
        {"tool_name": "search"},
        {"tool_name": "format"},
    ]

    score = TrajectoryMetrics.compute_exact_match(actual, expected)

    assert score == 0.0

  def test_exact_match_empty(self):
    """Test exact match with empty trajectories."""
    assert TrajectoryMetrics.compute_exact_match([], []) == 1.0
    assert (
        TrajectoryMetrics.compute_exact_match(
            [ToolCall(tool_name="t", args={})], []
        )
        == 0.0
    )

  def test_in_order_match_with_extras(self):
    """Test in-order match allows extra tools."""
    actual = [
        ToolCall(tool_name="search", args={}),
        ToolCall(tool_name="extra1", args={}),
        ToolCall(tool_name="format", args={}),
        ToolCall(tool_name="extra2", args={}),
        ToolCall(tool_name="send", args={}),
    ]
    expected = [
        {"tool_name": "search"},
        {"tool_name": "format"},
        {"tool_name": "send"},
    ]

    score = TrajectoryMetrics.compute_in_order_match(actual, expected)

    assert score == 1.0

  def test_in_order_match_missing_tool(self):
    """Test in-order match with missing tool."""
    actual = [
        ToolCall(tool_name="search", args={}),
        ToolCall(tool_name="format", args={}),
    ]
    expected = [
        {"tool_name": "search"},
        {"tool_name": "format"},
        {"tool_name": "send"},
    ]

    score = TrajectoryMetrics.compute_in_order_match(actual, expected)

    assert score == pytest.approx(2 / 3)

  def test_any_order_match_all_present(self):
    """Test any-order match with all tools present."""
    actual = [
        ToolCall(tool_name="format", args={}),
        ToolCall(tool_name="send", args={}),
        ToolCall(tool_name="search", args={}),
    ]
    expected = [
        {"tool_name": "search"},
        {"tool_name": "format"},
        {"tool_name": "send"},
    ]

    score = TrajectoryMetrics.compute_any_order_match(actual, expected)

    assert score == 1.0

  def test_any_order_match_missing_tool(self):
    """Test any-order match with missing tool."""
    actual = [
        ToolCall(tool_name="format", args={}),
        ToolCall(tool_name="search", args={}),
    ]
    expected = [
        {"tool_name": "search"},
        {"tool_name": "format"},
        {"tool_name": "send"},
    ]

    score = TrajectoryMetrics.compute_any_order_match(actual, expected)

    assert score == pytest.approx(2 / 3)

  def test_step_efficiency_optimal(self):
    """Test step efficiency when optimal."""
    score = TrajectoryMetrics.compute_step_efficiency(3, 3)
    assert score == 1.0

  def test_step_efficiency_fewer_steps(self):
    """Test step efficiency with fewer steps than optimal."""
    score = TrajectoryMetrics.compute_step_efficiency(2, 3)
    assert score == 1.0

  def test_step_efficiency_more_steps(self):
    """Test step efficiency with more steps than optimal."""
    score = TrajectoryMetrics.compute_step_efficiency(6, 3)
    assert score == 0.5


class TestBigQueryTraceEvaluator:
  """Tests for BigQueryTraceEvaluator class."""

  @pytest.fixture
  def mock_client(self):
    """Create mock BigQuery client."""
    return MagicMock()

  @pytest.fixture
  def evaluator(self, mock_client):
    """Create evaluator with mock client."""
    return BigQueryTraceEvaluator(
        project_id="test-project",
        dataset_id="test-dataset",
        table_id="test-table",
        client=mock_client,
    )

  @pytest.mark.asyncio
  async def test_get_session_trace(self, evaluator, mock_client):
    """Test retrieving session trace."""
    # Mock query results
    mock_results = [
        {
            "event_type": "USER_MESSAGE_RECEIVED",
            "agent": "agent",
            "timestamp": datetime.now(timezone.utc),
            "content": '{"text_summary": "Hello"}',
            "attributes": "{}",
            "span_id": "span-1",
            "parent_span_id": None,
            "latency_ms": None,
            "status": "OK",
            "error_message": None,
            "user_id": "user-123",
        },
        {
            "event_type": "TOOL_STARTING",
            "agent": "agent",
            "timestamp": datetime.now(timezone.utc),
            "content": '{"tool": "greet", "args": {}}',
            "attributes": "{}",
            "span_id": "span-2",
            "parent_span_id": "span-1",
            "latency_ms": None,
            "status": "OK",
            "error_message": None,
            "user_id": "user-123",
        },
        {
            "event_type": "TOOL_COMPLETED",
            "agent": "agent",
            "timestamp": datetime.now(timezone.utc),
            "content": '{"tool": "greet", "result": "Hi!"}',
            "attributes": "{}",
            "span_id": "span-2",
            "parent_span_id": "span-1",
            "latency_ms": '{"total_ms": 100}',
            "status": "OK",
            "error_message": None,
            "user_id": "user-123",
        },
    ]

    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_results
    mock_client.query.return_value = mock_query_job

    trace = await evaluator.get_session_trace("sess-123")

    assert trace.session_id == "sess-123"
    assert trace.user_id == "user-123"
    assert len(trace.events) == 3
    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].tool_name == "greet"

    # Phase 2a: the query must be labeled so BigQuery Agent Analytics SDK
    # usage shows up in INFORMATION_SCHEMA.JOBS with sdk_feature=trace-read.
    job_config = mock_client.query.call_args.kwargs.get("job_config")
    assert job_config is not None
    assert dict(job_config.labels or {}).get("sdk_feature") == "trace-read"

  @pytest.mark.asyncio
  async def test_evaluate_session_with_trajectory(self, evaluator, mock_client):
    """Test evaluating session with golden trajectory."""
    # Mock trace retrieval
    mock_results = [
        {
            "event_type": "TOOL_STARTING",
            "agent": "agent",
            "timestamp": datetime.now(timezone.utc),
            "content": '{"tool": "search", "args": {"q": "weather"}}',
            "attributes": "{}",
            "span_id": "span-1",
            "status": "OK",
        },
        {
            "event_type": "TOOL_COMPLETED",
            "agent": "agent",
            "timestamp": datetime.now(timezone.utc),
            "content": '{"tool": "search", "result": "sunny"}',
            "attributes": "{}",
            "span_id": "span-1",
            "status": "OK",
        },
        {
            "event_type": "AGENT_COMPLETED",
            "agent": "agent",
            "timestamp": datetime.now(timezone.utc),
            "content": '{"response": "The weather is sunny."}',
            "attributes": "{}",
            "span_id": "span-2",
            "status": "OK",
        },
    ]

    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_results
    mock_client.query.return_value = mock_query_job

    golden_trajectory = [{"tool_name": "search", "args": {"q": "weather"}}]
    golden_response = "The weather is sunny."

    result = await evaluator.evaluate_session(
        session_id="sess-123",
        golden_trajectory=golden_trajectory,
        golden_response=golden_response,
        match_type=MatchType.EXACT,
    )

    assert result.session_id == "sess-123"
    assert result.eval_status == EvalStatus.PASSED
    assert "trajectory_exact_match" in result.scores
    assert result.scores["trajectory_exact_match"] == 1.0
    assert "response_match" in result.scores
    assert result.scores["response_match"] == 1.0

  def test_compute_response_match_exact(self, evaluator):
    """Test exact response matching."""
    score = evaluator._compute_response_match(
        "Hello world",
        "Hello world",
    )
    assert score == 1.0

  def test_compute_response_match_partial(self, evaluator):
    """Test partial response matching."""
    score = evaluator._compute_response_match(
        "Hello world today",
        "Hello world",
    )
    assert score == 1.0  # All expected words present

  def test_compute_response_match_different(self, evaluator):
    """Test different responses."""
    score = evaluator._compute_response_match(
        "Goodbye moon",
        "Hello world",
    )
    assert score == 0.0


class TestReplayContext:
  """Tests for ReplayContext class."""

  def test_inject_llm_response(self):
    """Test injecting LLM responses."""
    ctx = ReplayContext()

    ctx.inject_llm_response("Response 1")
    ctx.inject_llm_response("Response 2")

    assert ctx.get_llm_response(0) == "Response 1"
    assert ctx.get_llm_response(1) == "Response 2"
    assert ctx.get_llm_response(2) is None

  def test_inject_tool_response(self):
    """Test injecting tool responses."""
    ctx = ReplayContext()

    ctx.inject_tool_response("search", {"result": "data"})

    assert ctx.get_tool_response("search") == {"result": "data"}
    assert ctx.get_tool_response("other") is None


class TestTraceReplayRunner:
  """Tests for TraceReplayRunner class."""

  @pytest.fixture
  def mock_evaluator(self):
    """Create mock evaluator."""
    evaluator = MagicMock(spec=BigQueryTraceEvaluator)
    return evaluator

  @pytest.fixture
  def replay_runner(self, mock_evaluator):
    """Create replay runner with mock evaluator."""
    return TraceReplayRunner(mock_evaluator)

  @pytest.mark.asyncio
  async def test_replay_session_full(self, replay_runner, mock_evaluator):
    """Test full session replay."""
    events = [
        TraceEvent(
            event_type="USER_MESSAGE_RECEIVED",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"text": "Hello"},
            attributes={},
        ),
        TraceEvent(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"response": "Hi there!"},
            attributes={},
        ),
        TraceEvent(
            event_type="TOOL_COMPLETED",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"tool": "greet", "result": {"message": "Hello!"}},
            attributes={},
        ),
    ]

    mock_trace = SessionTrace(
        session_id="sess-123",
        user_id="user-1",
        events=events,
    )

    mock_evaluator.get_session_trace = AsyncMock(return_value=mock_trace)

    ctx = await replay_runner.replay_session("sess-123", replay_mode="full")

    assert ctx.get_llm_response(0) == "Hi there!"
    assert ctx.get_tool_response("greet") == {"message": "Hello!"}

  @pytest.mark.asyncio
  async def test_replay_session_tool_only(self, replay_runner, mock_evaluator):
    """Test tool-only replay mode."""
    events = [
        TraceEvent(
            event_type="USER_MESSAGE_RECEIVED",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"text": "Hello"},
            attributes={},
        ),
        TraceEvent(
            event_type="TOOL_COMPLETED",
            agent="agent",
            timestamp=datetime.now(timezone.utc),
            content={"tool": "greet", "result": {"message": "Hello!"}},
            attributes={},
        ),
    ]

    mock_trace = SessionTrace(
        session_id="sess-123",
        user_id="user-1",
        events=events,
    )

    mock_evaluator.get_session_trace = AsyncMock(return_value=mock_trace)

    callback_events = []

    def callback(event, ctx):
      callback_events.append(event.event_type)

    ctx = await replay_runner.replay_session(
        "sess-123",
        replay_mode="tool_only",
        step_callback=callback,
    )

    # Only TOOL_COMPLETED should have triggered callback
    assert callback_events == ["TOOL_COMPLETED"]

  @pytest.mark.asyncio
  async def test_compare_replays(self, replay_runner, mock_evaluator):
    """Test comparing two session replays."""
    trace1 = SessionTrace(
        session_id="sess-1",
        user_id="user-1",
        events=[],
        tool_calls=[
            ToolCall(tool_name="search", args={"q": "test"}),
            ToolCall(tool_name="format", args={}),
        ],
        final_response="Result A",
    )

    trace2 = SessionTrace(
        session_id="sess-2",
        user_id="user-1",
        events=[],
        tool_calls=[
            ToolCall(tool_name="search", args={"q": "different"}),
            ToolCall(tool_name="send", args={}),
        ],
        final_response="Result B",
    )

    mock_evaluator.get_session_trace = AsyncMock(side_effect=[trace1, trace2])

    diff = await replay_runner.compare_replays("sess-1", "sess-2")

    assert diff["event_count_diff"] == 0
    assert diff["tool_count_diff"] == 0
    assert len(diff["tool_differences"]) == 2  # Both tools differ
    assert diff["response_match"] is False
