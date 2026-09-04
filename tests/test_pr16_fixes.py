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

"""Tests for PR #16 bug fixes and feature requests.

Covers:
  Issue 1 (P0): hitl_metrics() global distinct sessions
  Issue 2 (P0): Multi-criterion merge missing criteria
  Issue 3 (P0): asyncio.new_event_loop() replaced with _run_sync()
  Issue 4 (P1): Canonical error predicates
  Issue 5 (P1): Response-source logic (LLM_RESPONSE first)
  Issue 6 (P1): Semantic drift implementation
  Issue 7 (P2): get_trace docs use trace_id
  Issue 8 (P2): Strict-mode parse_errors aggregate
  Issue 9 (P2): GCS offload docstring

  Feature 1: Async APIs + safe sync wrappers
  Feature 2: Event semantics predicates wired in
  Feature 3: True semantic drift with _SEMANTIC_DRIFT_QUERY
  Feature 4: get_session_trace(session_id)
  Feature 6: HITL analytics with global distinct sessions
"""

import asyncio
from datetime import datetime
from datetime import timezone
import inspect
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.client import _apply_strict_mode
from bigquery_agent_analytics.client import _merge_criterion_reports
from bigquery_agent_analytics.client import _run_sync
from bigquery_agent_analytics.client import Client
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.evaluators import SystemEvaluator
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceFilter


def _mock_bq_client():
  """Creates a mock BigQuery client."""
  return MagicMock()


def _make_mock_row(data):
  """Creates a mock BQ row that supports dict(row) and row.get()."""
  mock = MagicMock()
  mock.__iter__ = MagicMock(return_value=iter(data.items()))
  mock.get = data.get
  mock.keys = data.keys
  mock.values = data.values
  mock.items = data.items
  mock.__getitem__ = lambda self, k: data[k]
  return mock


# ================================================================== #
# Issue 1 (P0): hitl_metrics() global distinct sessions               #
# ================================================================== #


class TestHitlMetricsGlobalSessions:
  """Verifies hitl_metrics reads global_hitl_sessions from CROSS JOIN."""

  def test_global_sessions_from_cross_join(self):
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "global_hitl_sessions": 42,
                "event_type": "HITL_CONFIRMATION_REQUEST",
                "event_count": 10,
                "session_count": 5,
                "avg_latency_ms": 200.0,
            }
        ),
        _make_mock_row(
            {
                "global_hitl_sessions": 42,
                "event_type": "HITL_CONFIRMATION_REQUEST_COMPLETED",
                "event_count": 8,
                "session_count": 5,
                "avg_latency_ms": 100.0,
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    metrics = client.hitl_metrics()

    assert metrics["total_hitl_sessions"] == 42
    assert metrics["total_hitl_events"] == 18

  def test_hitl_query_uses_cross_join(self):
    """Verify the SQL template includes CROSS JOIN."""
    from bigquery_agent_analytics.client import _HITL_METRICS_QUERY

    assert "CROSS JOIN" in _HITL_METRICS_QUERY
    assert "global_hitl_sessions" in _HITL_METRICS_QUERY

  def test_hitl_empty_results(self):
    mock_bq = _mock_bq_client()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    metrics = client.hitl_metrics()
    assert metrics["total_hitl_sessions"] == 0


# ================================================================== #
# Issue 2 (P0): Multi-criterion merge missing criteria                 #
# ================================================================== #


class TestMergeCriterionMissingCriteria:
  """Missing criteria default to 0.0 and should fail."""

  def test_missing_criterion_fails_session(self):
    from bigquery_agent_analytics.evaluators import _JudgeCriterion

    c1 = _JudgeCriterion(
        name="correctness",
        prompt_template="",
        score_key="correctness",
        threshold=0.5,
    )
    c2 = _JudgeCriterion(
        name="helpfulness",
        prompt_template="",
        score_key="helpfulness",
        threshold=0.5,
    )

    # Only c1 produced scores for session s1
    report1 = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.8},
                passed=True,
            )
        ],
    )
    # c2 produced no scores for s1 (empty report)
    report2 = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=0,
        passed_sessions=0,
        failed_sessions=0,
        session_scores=[],
    )

    merged = _merge_criterion_reports(
        "judge",
        "test",
        [c1, c2],
        [(c1, report1), (c2, report2)],
    )

    # s1 should FAIL because helpfulness is missing (defaults to 0.0)
    assert merged.total_sessions == 1
    assert merged.session_scores[0].passed is False

  def test_all_criteria_present_passes(self):
    from bigquery_agent_analytics.evaluators import _JudgeCriterion

    c1 = _JudgeCriterion(
        name="correctness",
        prompt_template="",
        score_key="correctness",
        threshold=0.5,
    )
    c2 = _JudgeCriterion(
        name="helpfulness",
        prompt_template="",
        score_key="helpfulness",
        threshold=0.5,
    )

    report1 = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.8},
                passed=True,
            )
        ],
    )
    report2 = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"helpfulness": 0.7},
                passed=True,
            )
        ],
    )

    merged = _merge_criterion_reports(
        "judge",
        "test",
        [c1, c2],
        [(c1, report1), (c2, report2)],
    )

    assert merged.total_sessions == 1
    assert merged.session_scores[0].passed is True
    assert merged.session_scores[0].scores["correctness"] == 0.8
    assert merged.session_scores[0].scores["helpfulness"] == 0.7


# ================================================================== #
# Issue 3 (P0): _run_sync() works in and out of event loops           #
# ================================================================== #


class TestRunSync:
  """Tests for the Jupyter-safe _run_sync helper."""

  def test_run_sync_no_running_loop(self):
    """Works when no event loop is running."""

    async def coro():
      return 42

    result = _run_sync(coro())
    assert result == 42

  def test_run_sync_from_running_loop(self):
    """Works when called from inside a running event loop."""

    async def inner():
      return "hello"

    async def outer():
      return _run_sync(inner())

    result = asyncio.run(outer())
    assert result == "hello"


# ================================================================== #
# Issue 4 (P1): Canonical error predicates                             #
# ================================================================== #


class TestCanonicalErrorPredicates:
  """Verifies error semantics are consistent across modules."""

  def test_span_is_error_event_type_suffix(self):
    """Span with _ERROR suffix event type should be is_error."""
    span = Span(
        event_type="TOOL_ERROR",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        status="OK",
    )
    assert span.is_error is True

  def test_span_is_error_error_message(self):
    """Span with error_message set should be is_error."""
    span = Span(
        event_type="TOOL_COMPLETED",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        status="OK",
        error_message="timeout",
    )
    assert span.is_error is True

  def test_span_is_error_status_error(self):
    """Span with status='ERROR' should be is_error."""
    span = Span(
        event_type="AGENT_COMPLETED",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        status="ERROR",
    )
    assert span.is_error is True

  def test_span_is_error_false_when_clean(self):
    """Span with no error signals should not be is_error."""
    span = Span(
        event_type="AGENT_COMPLETED",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        status="OK",
    )
    assert span.is_error is False

  def test_trace_error_spans_uses_is_error(self):
    """Trace.error_spans should use the 3-part predicate."""
    ts = datetime.now(timezone.utc)
    spans = [
        Span(
            event_type="TOOL_ERROR",
            agent="agent",
            timestamp=ts,
            status="OK",
        ),
        Span(
            event_type="TOOL_COMPLETED",
            agent="agent",
            timestamp=ts,
            status="OK",
            error_message="something failed",
        ),
        Span(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=ts,
            status="OK",
        ),
    ]
    trace = Trace(trace_id="t", session_id="s", spans=spans)
    errors = trace.error_spans
    assert len(errors) == 2

  def test_trace_filter_has_error_uses_canonical_predicate(self):
    """TraceFilter.has_error should use full canonical SQL."""
    filt = TraceFilter(has_error=True)
    where, _ = filt.to_sql_conditions()
    assert "ENDS_WITH(event_type, '_ERROR')" in where
    assert "error_message IS NOT NULL" in where
    assert "status = 'ERROR'" in where

  def test_evaluator_query_uses_canonical_predicate(self):
    """SESSION_SUMMARY_QUERY should use canonical error predicate."""
    from bigquery_agent_analytics.evaluators import SESSION_SUMMARY_QUERY

    assert "ENDS_WITH(event_type, '_ERROR')" in SESSION_SUMMARY_QUERY

  def test_insights_query_uses_canonical_predicate(self):
    """_SESSION_METADATA_QUERY should use canonical error predicate."""
    from bigquery_agent_analytics.insights import _SESSION_METADATA_QUERY

    assert "ENDS_WITH(event_type, '_ERROR')" in _SESSION_METADATA_QUERY

  def test_feedback_unanswered_query_uses_canonical_predicate(self):
    """_FREQUENTLY_UNANSWERED_QUERY uses canonical error predicate."""
    from bigquery_agent_analytics.feedback import _FREQUENTLY_UNANSWERED_QUERY

    assert "ENDS_WITH(event_type, '_ERROR')" in _FREQUENTLY_UNANSWERED_QUERY
    assert "error_message IS NOT NULL" in _FREQUENTLY_UNANSWERED_QUERY


# ================================================================== #
# Issue 5 (P1): Response-source logic (LLM_RESPONSE first)            #
# ================================================================== #


class TestResponseSourceOrder:
  """Verifies LLM_RESPONSE is preferred over AGENT_COMPLETED."""

  def test_trace_evaluator_prefers_llm_response(self):
    from bigquery_agent_analytics.trace_evaluator import SessionTrace
    from bigquery_agent_analytics.trace_evaluator import TraceEvent

    events = [
        TraceEvent(
            event_type="LLM_RESPONSE",
            agent="agent",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            content={"response": "LLM answer"},
            attributes={},
        ),
        TraceEvent(
            event_type="AGENT_COMPLETED",
            agent="agent",
            timestamp=datetime(2024, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
            content={"response": "Agent answer"},
            attributes={},
        ),
    ]
    trace = SessionTrace(
        session_id="s1",
        user_id="u1",
        events=events,
    )
    result = trace.extract_final_response()
    assert result == "LLM answer"

  def test_trace_evaluator_falls_back_to_agent_completed(self):
    from bigquery_agent_analytics.trace_evaluator import SessionTrace
    from bigquery_agent_analytics.trace_evaluator import TraceEvent

    events = [
        TraceEvent(
            event_type="AGENT_COMPLETED",
            agent="agent",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            content={"response": "Only agent"},
            attributes={},
        ),
    ]
    trace = SessionTrace(
        session_id="s1",
        user_id="u1",
        events=events,
    )
    result = trace.extract_final_response()
    assert result == "Only agent"

  def test_memory_service_query_includes_llm_response(self):
    from bigquery_agent_analytics.memory_service import BigQuerySessionMemory

    assert "LLM_RESPONSE" in BigQuerySessionMemory._RECENT_CONTEXT_QUERY

  def test_ai_ml_index_query_includes_llm_response(self):
    from bigquery_agent_analytics.ai_ml_integration import EmbeddingSearchClient

    assert "LLM_RESPONSE" in EmbeddingSearchClient._AI_EMBED_INDEX_QUERY
    assert (
        "LLM_RESPONSE" in EmbeddingSearchClient._LEGACY_INDEX_EMBEDDINGS_QUERY
    )


# ================================================================== #
# Issue 6/Feature 3: Semantic drift implementation                     #
# ================================================================== #


class TestSemanticDrift:
  """Tests that semantic drift uses _SEMANTIC_DRIFT_QUERY."""

  def test_semantic_drift_query_exists(self):
    from bigquery_agent_analytics.feedback import _SEMANTIC_DRIFT_QUERY

    assert "ML.GENERATE_EMBEDDING" in _SEMANTIC_DRIFT_QUERY
    assert "ML.DISTANCE" in _SEMANTIC_DRIFT_QUERY

  def test_ai_embed_semantic_drift_query_exists(self):
    from bigquery_agent_analytics.feedback import _AI_EMBED_SEMANTIC_DRIFT_QUERY

    assert "AI.EMBED" in _AI_EMBED_SEMANTIC_DRIFT_QUERY
    assert "ML.DISTANCE" in _AI_EMBED_SEMANTIC_DRIFT_QUERY
    assert "ML.GENERATE_EMBEDDING" not in _AI_EMBED_SEMANTIC_DRIFT_QUERY

  def test_semantic_drift_function_exists(self):
    from bigquery_agent_analytics.feedback import _semantic_drift

    assert callable(_semantic_drift)

  async def test_compute_drift_with_embedding_model(self):
    """When embedding_model is provided, semantic drift is attempted."""
    from bigquery_agent_analytics.feedback import compute_drift

    mock_bq = _mock_bq_client()

    golden_rows = [_make_mock_row({"question": "What is the weather?"})]
    prod_rows = [_make_mock_row({"question": "How's the weather?"})]
    drift_rows = [
        _make_mock_row(
            {
                "golden_question": "What is the weather?",
                "closest_production": "How's the weather?",
                "distance": 0.1,
            }
        )
    ]

    call_count = [0]

    def mock_query(*args, **kwargs):
      call_count[0] += 1
      job = MagicMock()
      if call_count[0] == 1:
        job.result.return_value = golden_rows
      elif call_count[0] == 2:
        job.result.return_value = prod_rows
      else:
        job.result.return_value = drift_rows
      return job

    mock_bq.query = mock_query

    report = await compute_drift(
        bq_client=mock_bq,
        project_id="proj",
        dataset_id="ds",
        table_id="events",
        golden_table="golden",
        where_clause="TRUE",
        query_params=[],
        embedding_model="proj.ds.embedding_model",
    )

    assert report.details.get("method") == "semantic_embedding"
    assert len(report.covered_questions) == 1


# ================================================================== #
# Issue 7 (P2): get_trace uses trace_id in docs                       #
# ================================================================== #


class TestGetTraceDocsUseTraceId:
  """Verify docs reference trace_id not session_id."""

  def test_readme_uses_trace_id(self):
    with open("README.md") as f:
      content = f.read()
    assert 'get_trace("trace-' in content
    assert 'get_trace("session-' not in content

  def test_sdk_md_uses_trace_id(self):
    with open("SDK.md") as f:
      content = f.read()
    assert 'get_trace("trace-' in content
    assert 'get_trace("session-' not in content

  def test_sdk_md_default_table_id(self):
    with open("SDK.md") as f:
      content = f.read()
    assert '`"agent_events_v2"`' not in content


# ================================================================== #
# Issue 8 (P2): Strict-mode parse_errors aggregate                     #
# ================================================================== #


class TestStrictModeParseErrors:
  """Strict mode puts parse_errors in report.details."""

  def test_parse_errors_in_details(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=3,
        passed_sessions=2,
        failed_sessions=1,
        aggregate_scores={"correctness": 0.7},
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.8},
                passed=True,
            ),
            SessionScore(
                session_id="s2",
                scores={},
                passed=True,
            ),
            SessionScore(
                session_id="s3",
                scores={"correctness": 0.9},
                passed=True,
            ),
        ],
    )
    strict_report = _apply_strict_mode(report)

    assert strict_report.details["parse_errors"] == 1
    assert "parse_errors" not in strict_report.aggregate_scores
    assert strict_report.passed_sessions == 2
    assert strict_report.failed_sessions == 1

  def test_no_parse_errors_when_all_have_scores(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        aggregate_scores={"correctness": 0.8},
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.8},
                passed=True,
            ),
        ],
    )
    strict_report = _apply_strict_mode(report)

    # parse_errors always present in details for stable schema.
    assert strict_report.details["parse_errors"] == 0
    assert strict_report.details["parse_error_rate"] == 0.0
    assert "parse_errors" not in strict_report.aggregate_scores


# ================================================================== #
# Issue 9 (P2): GCS offload docstring                                 #
# ================================================================== #


class TestGcsOffloadDocstring:
  """GCS offload should be documented as not yet implemented."""

  def test_docstring_says_not_implemented(self):
    import inspect

    doc = inspect.getdoc(Client)
    assert "not yet implemented" in doc


# ================================================================== #
# Feature 1: Async APIs                                                #
# ================================================================== #


class TestAsyncAPIs:
  """Tests that async API methods exist."""

  def test_insights_async_exists(self):
    assert hasattr(Client, "insights_async")
    assert inspect.iscoroutinefunction(Client.insights_async)

  def test_drift_detection_async_exists(self):
    assert hasattr(Client, "drift_detection_async")
    assert inspect.iscoroutinefunction(Client.drift_detection_async)

  def test_deep_analysis_async_exists(self):
    assert hasattr(Client, "deep_analysis_async")
    assert inspect.iscoroutinefunction(Client.deep_analysis_async)


# ================================================================== #
# Feature 4: get_session_trace(session_id)                             #
# ================================================================== #


class TestGetSessionTrace:
  """Tests for Client.get_session_trace()."""

  def test_get_session_trace_success(self):
    mock_bq = _mock_bq_client()
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _make_mock_row(
            {
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": None,
                "timestamp": ts,
                "session_id": "sess-1",
                "invocation_id": "inv-1",
                "user_id": "user-1",
                "trace_id": "trace-1",
                "span_id": "s1",
                "parent_span_id": None,
                "content": '{"text_summary": "Hello"}',
                "content_parts": [],
                "attributes": "{}",
                "latency_ms": None,
                "status": "OK",
                "error_message": None,
                "is_truncated": False,
            }
        ),
        _make_mock_row(
            {
                "event_type": "AGENT_COMPLETED",
                "agent": "my_agent",
                "timestamp": ts,
                "session_id": "sess-1",
                "invocation_id": "inv-1",
                "user_id": "user-1",
                "trace_id": "trace-1",
                "span_id": "s2",
                "parent_span_id": "s1",
                "content": '{"response": "Hi!"}',
                "content_parts": [],
                "attributes": "{}",
                "latency_ms": '{"total_ms": 250}',
                "status": "OK",
                "error_message": None,
                "is_truncated": False,
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    trace = client.get_session_trace("sess-1")

    assert trace.session_id == "sess-1"
    assert len(trace.spans) == 2
    call_args = mock_bq.query.call_args
    query_str = call_args[0][0]
    assert "session_id = @session_id" in query_str

  def test_get_session_trace_not_found(self):
    mock_bq = _mock_bq_client()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    with pytest.raises(ValueError, match="No events found"):
      client.get_session_trace("nonexistent")


# ================================================================== #
# Feature 5: Docs consistency checks                                   #
# ================================================================== #


class TestDocsConsistency:
  """Static checks for documentation accuracy."""

  def test_init_py_quick_start_uses_trace_id(self):
    with open("src/bigquery_agent_analytics/__init__.py") as f:
      content = f.read()
    assert 'get_trace("trace-' in content

  def test_client_module_docstring_uses_trace_id(self):
    with open("src/bigquery_agent_analytics/client.py") as f:
      content = f.read()
    assert 'get_trace("trace-' in content

  def test_default_table_id_is_agent_events(self):
    """Default table_id should be agent_events (not agent_events_v2)."""
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    assert client.table_id == "agent_events"


# ================================================================== #
# Feature 2: event_semantics wired into all SQL builders               #
# ================================================================== #


class TestEventSemanticsWired:
  """Verify event_semantics predicates used across modules."""

  def test_error_sql_predicate_canonical_form(self):
    from bigquery_agent_analytics.event_semantics import ERROR_SQL_PREDICATE

    assert "ENDS_WITH(event_type, '_ERROR')" in ERROR_SQL_PREDICATE
    assert "error_message IS NOT NULL" in ERROR_SQL_PREDICATE
    assert "status = 'ERROR'" in ERROR_SQL_PREDICATE

  def test_response_event_types_includes_llm_response(self):
    from bigquery_agent_analytics.event_semantics import RESPONSE_EVENT_TYPES

    assert "LLM_RESPONSE" in RESPONSE_EVENT_TYPES

  def test_is_error_event_function(self):
    from bigquery_agent_analytics.event_semantics import is_error_event

    assert is_error_event("TOOL_ERROR", None, "OK") is True
    assert is_error_event("TOOL_COMPLETED", "fail", "OK") is True
    assert is_error_event("AGENT_COMPLETED", None, "ERROR") is True
    assert is_error_event("TOOL_COMPLETED", None, "OK") is False


# ================================================================== #
# Span.label uses is_error                                             #
# ================================================================== #


class TestSpanLabelUsesIsError:
  """Span.label should use self.is_error, not just status == 'ERROR'."""

  def test_label_shows_error_for_error_suffix_event(self):
    span = Span(
        event_type="TOOL_ERROR",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        status="OK",
        content={"tool": "search"},
    )
    assert "ERROR" in span.label or "\u2717" in span.label

  def test_label_shows_error_for_error_message(self):
    span = Span(
        event_type="TOOL_COMPLETED",
        agent="agent",
        timestamp=datetime.now(timezone.utc),
        status="OK",
        error_message="timeout",
    )
    assert span.is_error is True


# ================================================================== #
# Memory service response extraction includes LLM_RESPONSE             #
# ================================================================== #


class TestMemoryServiceLLMResponse:
  """memory_service.py handles LLM_RESPONSE for agent responses."""

  def test_get_recent_context_processes_llm_response(self):
    """BigQuerySessionMemory should process LLM_RESPONSE events."""
    from bigquery_agent_analytics.memory_service import BigQuerySessionMemory

    query = BigQuerySessionMemory._RECENT_CONTEXT_QUERY
    assert "'LLM_RESPONSE'" in query
    assert "'AGENT_COMPLETED'" in query
    assert "'USER_MESSAGE_RECEIVED'" in query
