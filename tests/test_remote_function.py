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

"""Tests for the BigQuery Remote Function dispatch logic.

Tests the core dispatch/processing logic in ``dispatch.py`` without
requiring ``functions_framework`` or ``flask``.
"""

from datetime import datetime
from datetime import timezone
import importlib.util
import json
import sys
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.feedback import DriftReport
from bigquery_agent_analytics.insights import AggregatedInsights
from bigquery_agent_analytics.insights import InsightsReport
from bigquery_agent_analytics.insights import SessionMetadata
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace

_NOW = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _mock_trace():
  return Trace(
      trace_id="t1",
      session_id="s1",
      spans=[
          Span(
              event_type="LLM_REQUEST",
              agent="bot",
              timestamp=_NOW,
              content={},
              attributes={},
          )
      ],
      start_time=_NOW,
      end_time=_NOW,
      total_latency_ms=200.0,
  )


def _mock_report(passed, total):
  return EvaluationReport(
      dataset="test",
      evaluator_name="latency",
      total_sessions=total,
      passed_sessions=passed,
      failed_sessions=total - passed,
      created_at=_NOW,
      session_scores=[
          SessionScore(
              session_id=f"s{i}",
              scores={"latency": 0.9 if i < passed else 0.3},
              passed=i < passed,
          )
          for i in range(total)
      ],
  )


def _mock_insights():
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
  return InsightsReport(
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


def _mock_drift():
  return DriftReport(
      coverage_percentage=0.85,
      total_golden=100,
      total_production=200,
  )


def _import_dispatch():
  """Import dispatch.py from deploy/remote_function/."""
  spec = importlib.util.spec_from_file_location(
      "rf_dispatch",
      "deploy/remote_function/dispatch.py",
  )
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


@pytest.fixture(scope="module")
def rf():
  """Module-scoped import of the dispatch module."""
  return _import_dispatch()


# ------------------------------------------------------------------ #
# build_client_from_context                                            #
# ------------------------------------------------------------------ #


class TestBuildClient:

  def test_from_udc(self, rf):
    mock_cls = MagicMock()
    original = rf.Client
    rf.Client = mock_cls
    try:
      rf.build_client_from_context(
          {
              "project_id": "my-proj",
              "dataset_id": "my-ds",
              "table_id": "my_table",
              "location": "us-east1",
          }
      )
      call_kwargs = mock_cls.call_args[1]
      assert call_kwargs["project_id"] == "my-proj"
      assert call_kwargs["dataset_id"] == "my-ds"
      assert call_kwargs["table_id"] == "my_table"
      assert call_kwargs["location"] == "us-east1"
    finally:
      rf.Client = original

  def test_env_var_fallback(self, rf, monkeypatch):
    mock_cls = MagicMock()
    original = rf.Client
    rf.Client = mock_cls
    monkeypatch.setenv("BQ_AGENT_PROJECT", "env-proj")
    monkeypatch.setenv("BQ_AGENT_DATASET", "env-ds")
    try:
      rf.build_client_from_context({})
      call_kwargs = mock_cls.call_args[1]
      assert call_kwargs["project_id"] == "env-proj"
      assert call_kwargs["dataset_id"] == "env-ds"
    finally:
      rf.Client = original

  def test_missing_project_raises(self, rf, monkeypatch):
    monkeypatch.delenv("BQ_AGENT_PROJECT", raising=False)
    monkeypatch.delenv("BQ_AGENT_DATASET", raising=False)
    with pytest.raises(ValueError, match="project_id"):
      rf.build_client_from_context({})

  def test_missing_dataset_raises(self, rf, monkeypatch):
    monkeypatch.setenv("BQ_AGENT_PROJECT", "proj")
    monkeypatch.delenv("BQ_AGENT_DATASET", raising=False)
    with pytest.raises(ValueError, match="dataset_id"):
      rf.build_client_from_context({})


# ------------------------------------------------------------------ #
# analyze operation                                                    #
# ------------------------------------------------------------------ #


class TestAnalyze:

  def test_analyze_basic(self, rf):
    client = MagicMock()
    client.get_session_trace.return_value = _mock_trace()

    result = rf.dispatch(client, "analyze", {"session_id": "s1"})
    assert result["trace_id"] == "t1"
    client.get_session_trace.assert_called_once_with("s1")

  def test_analyze_json_safe(self, rf):
    client = MagicMock()
    client.get_session_trace.return_value = _mock_trace()

    result = rf.dispatch(client, "analyze", {"session_id": "s1"})
    dumped = json.dumps(result)
    parsed = json.loads(dumped)
    assert isinstance(parsed["start_time"], str)
    assert "2026-03-12" in parsed["start_time"]


# ------------------------------------------------------------------ #
# evaluate operation                                                   #
# ------------------------------------------------------------------ #


class TestEvaluate:

  def test_evaluate_basic(self, rf):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)

    result = rf.dispatch(
        client,
        "evaluate",
        {"metric": "latency", "threshold": 5000},
    )
    assert result["total_sessions"] == 10
    json.dumps(result)

  def test_evaluate_default_threshold(self, rf):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(5, 5)

    result = rf.dispatch(
        client,
        "evaluate",
        {"metric": "error_rate"},
    )
    json.dumps(result)

  def test_evaluate_with_filters(self, rf):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(3, 3)

    rf.dispatch(
        client,
        "evaluate",
        {
            "metric": "latency",
            "threshold": 3000,
            "agent_filter": "bot",
            "last": "1h",
            "limit": 50,
        },
    )
    call_kwargs = client.evaluate.call_args[1]
    filters = call_kwargs["filters"]
    assert filters.agent_id == "bot"
    assert filters.limit == 50
    assert filters.start_time is not None

  @pytest.mark.parametrize(
      "metric",
      [
          "latency",
          "error_rate",
          "turn_count",
          "token_efficiency",
          "context_cache_hit_rate",
          "ttft",
          "cost",
      ],
  )
  def test_all_code_evaluators(self, rf, metric):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(5, 5)

    result = rf.dispatch(client, "evaluate", {"metric": metric})
    json.dumps(result)

  def test_context_cache_hit_rate_strict_missing_telemetry_param(self, rf):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(5, 5)

    result = rf.dispatch(
        client,
        "evaluate",
        {
            "metric": "context_cache_hit_rate",
            "fail_on_missing_telemetry": True,
        },
    )
    json.dumps(result)

    evaluator = client.evaluate.call_args[1]["evaluator"]
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 0,
            "cache_telemetry_events": 0,
        }
    )
    assert score.passed is False
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["fail_on_missing_telemetry"] is True

  def test_unknown_metric(self, rf):
    client = MagicMock()
    with pytest.raises(ValueError, match="bogus"):
      rf.dispatch(client, "evaluate", {"metric": "bogus"})


# ------------------------------------------------------------------ #
# judge operation                                                      #
# ------------------------------------------------------------------ #


class TestJudge:

  def test_judge_basic(self, rf):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(8, 10)

    result = rf.dispatch(
        client,
        "judge",
        {"criterion": "correctness", "threshold": 0.7},
    )
    assert result["total_sessions"] == 10
    json.dumps(result)

  @pytest.mark.parametrize(
      "criterion",
      ["correctness", "hallucination", "sentiment"],
  )
  def test_all_judge_criteria(self, rf, criterion):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(5, 5)

    result = rf.dispatch(
        client,
        "judge",
        {"criterion": criterion},
    )
    json.dumps(result)

  def test_unknown_criterion(self, rf):
    client = MagicMock()
    with pytest.raises(ValueError, match="bogus"):
      rf.dispatch(client, "judge", {"criterion": "bogus"})


# ------------------------------------------------------------------ #
# insights operation                                                   #
# ------------------------------------------------------------------ #


class TestInsights:

  def test_insights_basic(self, rf):
    client = MagicMock()
    client.insights.return_value = _mock_insights()

    result = rf.dispatch(client, "insights", {})
    assert "aggregated" in result
    json.dumps(result)


# ------------------------------------------------------------------ #
# drift operation                                                      #
# ------------------------------------------------------------------ #


class TestDrift:

  def test_drift_basic(self, rf):
    client = MagicMock()
    client.drift_detection.return_value = _mock_drift()

    result = rf.dispatch(
        client,
        "drift",
        {"golden_dataset": "golden_set"},
    )
    assert result["coverage_percentage"] == 0.85
    json.dumps(result)

  def test_drift_missing_golden_dataset(self, rf):
    client = MagicMock()
    with pytest.raises(ValueError, match="golden_dataset"):
      rf.dispatch(client, "drift", {})


# ------------------------------------------------------------------ #
# unknown operation                                                    #
# ------------------------------------------------------------------ #


class TestUnknownOperation:

  def test_unknown_operation(self, rf):
    client = MagicMock()
    with pytest.raises(ValueError, match="bogus_op"):
      rf.dispatch(client, "bogus_op", {})


# ------------------------------------------------------------------ #
# process_calls (batching + partial failure)                           #
# ------------------------------------------------------------------ #


class TestProcessCalls:

  def test_batched_calls(self, rf):
    client = MagicMock()
    client.get_session_trace.return_value = _mock_trace()
    client.evaluate.return_value = _mock_report(5, 5)

    replies = rf.process_calls(
        client,
        [
            ["analyze", '{"session_id": "s1"}'],
            ["evaluate", '{"metric": "latency"}'],
        ],
    )
    assert len(replies) == 2
    r0 = replies[0]
    r1 = replies[1]
    assert isinstance(r0, dict)
    assert isinstance(r1, dict)
    assert r0["trace_id"] == "t1"
    assert r0["_version"] == "1.0"
    assert r1["total_sessions"] == 5
    assert r1["_version"] == "1.0"

  def test_partial_failure(self, rf):
    client = MagicMock()
    client.get_session_trace.side_effect = [
        _mock_trace(),
        RuntimeError("not found"),
        _mock_trace(),
    ]

    replies = rf.process_calls(
        client,
        [
            ["analyze", '{"session_id": "s1"}'],
            ["analyze", '{"session_id": "bad"}'],
            ["analyze", '{"session_id": "s3"}'],
        ],
    )
    assert len(replies) == 3

    r0 = replies[0]
    r1 = replies[1]
    r2 = replies[2]

    assert r0["trace_id"] == "t1"
    assert "_error" in r1
    assert r1["_error"]["code"] == "RuntimeError"
    assert r1["_version"] == "1.0"
    assert r2["trace_id"] == "t1"

  def test_params_as_dict(self, rf):
    """params_json can be a dict (already parsed by BQ)."""
    client = MagicMock()
    client.get_session_trace.return_value = _mock_trace()

    replies = rf.process_calls(
        client,
        [["analyze", {"session_id": "s1"}]],
    )
    assert replies[0]["trace_id"] == "t1"

  def test_all_operations_json_safe(self, rf):
    """Every reply from every operation must be JSON-safe."""
    client = MagicMock()
    client.get_session_trace.return_value = _mock_trace()
    client.evaluate.return_value = _mock_report(3, 5)
    client.insights.return_value = _mock_insights()
    client.drift_detection.return_value = _mock_drift()

    replies = rf.process_calls(
        client,
        [
            ["analyze", '{"session_id": "s1"}'],
            ["evaluate", '{"metric": "latency"}'],
            ["insights", "{}"],
            ["drift", '{"golden_dataset": "g"}'],
        ],
    )
    assert len(replies) == 4
    for reply in replies:
      assert isinstance(reply, dict)
      # Must survive json round-trip (no datetime, no non-JSON types)
      json.dumps(reply)
      assert reply["_version"] == "1.0"
