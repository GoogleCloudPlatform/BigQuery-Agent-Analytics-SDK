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

"""Tests for the bq-agent-sdk CLI."""

from datetime import datetime
from datetime import timezone
import json
import os
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics.cli import app
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import LLMAsJudge
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.feedback import DriftReport
from bigquery_agent_analytics.feedback import QuestionDistribution
from bigquery_agent_analytics.insights import AggregatedInsights
from bigquery_agent_analytics.insights import InsightsReport
from bigquery_agent_analytics.insights import SessionMetadata
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace

runner = CliRunner()

_NOW = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)


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


# ------------------------------------------------------------------ #
# doctor                                                               #
# ------------------------------------------------------------------ #


class TestDoctor:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_doctor_json(self, mock_build):
    client = MagicMock()
    client.doctor.return_value = {
        "status": "OK",
        "event_count": 100,
    }
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "doctor",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["status"] == "OK"
    assert parsed["event_count"] == 100

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_doctor_error_exit_2(self, mock_build):
    mock_build.side_effect = RuntimeError("connection failed")
    result = runner.invoke(
        app,
        [
            "doctor",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# get-trace                                                            #
# ------------------------------------------------------------------ #


class TestGetTrace:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_get_trace_by_session_id(self, mock_build):
    client = MagicMock()
    client.get_session_trace.return_value = _mock_trace()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "get-trace",
            "--project-id=proj",
            "--dataset-id=ds",
            "--session-id=s1",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["trace_id"] == "t1"
    assert parsed["session_id"] == "s1"
    client.get_session_trace.assert_called_once_with("s1")

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_get_trace_by_trace_id(self, mock_build):
    client = MagicMock()
    client.get_trace.return_value = _mock_trace()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "get-trace",
            "--project-id=proj",
            "--dataset-id=ds",
            "--trace-id=t1",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["trace_id"] == "t1"
    client.get_trace.assert_called_once_with("t1")

  def test_get_trace_missing_id_exit_2(self):
    result = runner.invoke(
        app,
        [
            "get-trace",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_get_trace_text_format(self, mock_build):
    client = MagicMock()
    client.get_session_trace.return_value = _mock_trace()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "get-trace",
            "--project-id=proj",
            "--dataset-id=ds",
            "--session-id=s1",
            "--format=text",
        ],
    )
    assert result.exit_code == 0
    # Text format for Trace uses render() which includes trace_id
    assert "t1" in result.output


# ------------------------------------------------------------------ #
# evaluate                                                             #
# ------------------------------------------------------------------ #


class TestEvaluate:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_latency_pass(self, mock_build):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=latency",
            "--threshold=5000",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["total_sessions"] == 10
    assert parsed["passed_sessions"] == 10

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_exit_code_on_failure(self, mock_build):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(7, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=latency",
            "--exit-code",
        ],
    )
    assert result.exit_code == 1

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_exit_code_emits_failure_lines(self, mock_build):
    """--exit-code failure path emits one FAIL line per failing session.

    Regression guard: prior output did not point the reader at which
    threshold regressed and by how much. The new path stashes the raw
    observed value + budget in ``SessionScore.details`` and prints
    them on stderr before raising Exit(code=1).
    """
    report = EvaluationReport(
        dataset="test",
        evaluator_name="latency_evaluator",
        total_sessions=2,
        passed_sessions=1,
        failed_sessions=1,
        created_at=_NOW,
        session_scores=[
            SessionScore(
                session_id="good",
                scores={"latency": 1.0},
                passed=True,
                details={
                    "metric_latency": {
                        "observed": 1200,
                        "budget": 5000,
                        "threshold": 1.0,
                        "score": 1.0,
                        "passed": True,
                    }
                },
            ),
            SessionScore(
                session_id="bad",
                scores={"latency": 0.0},
                passed=False,
                details={
                    "metric_latency": {
                        "observed": 7500,
                        "budget": 5000,
                        "threshold": 1.0,
                        "score": 0.0,
                        "passed": False,
                    }
                },
            ),
        ],
    )
    client = MagicMock()
    client.evaluate.return_value = report
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=latency",
            "--exit-code",
        ],
    )
    assert result.exit_code == 1
    combined = (result.stderr or "") + (result.output or "")
    assert "--exit-code" in combined
    assert "1 session(s) failed" in combined
    assert "FAIL session=bad" in combined
    assert "metric=latency" in combined
    assert "observed=7500" in combined
    assert "budget=5000" in combined
    # Passing sessions must not emit a FAIL line.
    assert "session=good" not in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_exit_code_emits_fallback_for_custom_metric(
      self, mock_build
  ):
    """Custom metric without observed/budget still gets a FAIL line.

    Regression guard: previously the emitter only printed a line when
    the score was exactly 0.0, so a custom ``add_metric(threshold=0.7)``
    or LLM judge scoring 0.6 for a failing session silently produced
    only the summary header. That left CI logs unhelpful.
    """
    report = EvaluationReport(
        dataset="test",
        evaluator_name="custom_eval",
        total_sessions=1,
        passed_sessions=0,
        failed_sessions=1,
        created_at=_NOW,
        session_scores=[
            SessionScore(
                session_id="bad",
                scores={"helpfulness": 0.6},
                passed=False,
                details={
                    "metric_helpfulness": {
                        "observed": None,
                        "budget": None,
                        "threshold": 0.7,
                        "score": 0.6,
                        "passed": False,
                    }
                },
            ),
        ],
    )
    client = MagicMock()
    client.evaluate.return_value = report
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=latency",
            "--exit-code",
        ],
    )
    assert result.exit_code == 1
    combined = (result.stderr or "") + (result.output or "")
    assert "FAIL session=bad" in combined
    assert "metric=helpfulness" in combined
    assert "score=0.6" in combined
    assert "threshold=0.7" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_exit_code_emits_fallback_with_no_details(self, mock_build):
    """Failing session with empty details still emits a FAIL line.

    Safety-net guard: if an upstream evaluator doesn't populate
    per-metric details, we still name the session and metric rather
    than printing only the summary header.
    """
    report = EvaluationReport(
        dataset="test",
        evaluator_name="legacy_eval",
        total_sessions=1,
        passed_sessions=0,
        failed_sessions=1,
        created_at=_NOW,
        session_scores=[
            SessionScore(
                session_id="bad",
                scores={"legacy_metric": 0.3},
                passed=False,
                details={},
            ),
        ],
    )
    client = MagicMock()
    client.evaluate.return_value = report
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=latency",
            "--exit-code",
        ],
    )
    assert result.exit_code == 1
    combined = (result.stderr or "") + (result.output or "")
    assert "FAIL session=bad" in combined
    assert "metric=legacy_metric" in combined
    assert "score=0.3" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_exit_code_on_pass(self, mock_build):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=latency",
            "--exit-code",
        ],
    )
    assert result.exit_code == 0

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_with_filter_args(self, mock_build):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(5, 5)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=error_rate",
            "--threshold=0.1",
            "--agent-id=bot",
            "--last=1h",
            "--limit=50",
        ],
    )
    assert result.exit_code == 0
    # Verify the filter was passed
    call_kwargs = client.evaluate.call_args
    filters = call_kwargs.kwargs.get("filters") or call_kwargs[1].get("filters")
    assert filters.agent_id == "bot"
    assert filters.limit == 50
    assert filters.start_time is not None

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_llm_judge(self, mock_build):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(8, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=llm-judge",
            "--criterion=correctness",
            "--threshold=0.7",
        ],
    )
    assert result.exit_code == 0
    call_args = client.evaluate.call_args
    ev = call_args.kwargs.get("evaluator") or call_args[1].get("evaluator")
    assert isinstance(ev, LLMAsJudge)

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_text_format(self, mock_build):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--format=text",
        ],
    )
    assert result.exit_code == 0
    # Text format uses .summary() which mentions evaluator name
    assert "latency" in result.output

  def test_evaluate_unknown_evaluator(self):
    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=bogus",
        ],
    )
    assert result.exit_code == 2

  def test_evaluate_unknown_criterion(self):
    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=llm-judge",
            "--criterion=bogus",
        ],
    )
    assert result.exit_code == 2

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_strict_flag(self, mock_build):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--strict",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.evaluate.call_args
    assert call_kwargs.kwargs.get("strict") is True

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_evaluate_infra_error_exit_2(self, mock_build):
    client = MagicMock()
    client.evaluate.side_effect = RuntimeError("BQ timeout")
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# env var fallback                                                     #
# ------------------------------------------------------------------ #


class TestEnvVars:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_env_var_project_and_dataset(self, mock_build):
    client = MagicMock()
    client.doctor.return_value = {"status": "OK"}
    mock_build.return_value = client

    result = runner.invoke(
        app,
        ["doctor"],
        env={
            "BQ_AGENT_PROJECT": "env-proj",
            "BQ_AGENT_DATASET": "env-ds",
        },
    )
    assert result.exit_code == 0
    mock_build.assert_called_once()
    call_args = mock_build.call_args
    assert call_args[1].get("project_id") or call_args[0][0] in ("env-proj",)


# ------------------------------------------------------------------ #
# all evaluator types                                                  #
# ------------------------------------------------------------------ #


class TestAllEvaluators:

  @pytest.mark.parametrize(
      "name",
      [
          "latency",
          "error_rate",
          "turn_count",
          "token_efficiency",
          "ttft",
          "cost",
      ],
  )
  @patch("bigquery_agent_analytics.cli._build_client")
  def test_code_evaluator(self, mock_build, name):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--evaluator={name}",
            "--threshold=100",
        ],
    )
    assert result.exit_code == 0

  @pytest.mark.parametrize(
      "criterion",
      ["correctness", "hallucination", "sentiment"],
  )
  @patch("bigquery_agent_analytics.cli._build_client")
  def test_llm_judge_criteria(self, mock_build, criterion):
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=llm-judge",
            f"--criterion={criterion}",
            "--threshold=0.5",
        ],
    )
    assert result.exit_code == 0


# ------------------------------------------------------------------ #
# default thresholds                                                   #
# ------------------------------------------------------------------ #


class TestDefaultThresholds:

  @pytest.mark.parametrize(
      "name",
      [
          "latency",
          "error_rate",
          "turn_count",
          "token_efficiency",
          "ttft",
          "cost",
      ],
  )
  @patch("bigquery_agent_analytics.cli._build_client")
  def test_code_evaluator_uses_sdk_default(self, mock_build, name):
    """Omitting --threshold should use the SDK's built-in default."""
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--evaluator={name}",
        ],
    )
    assert result.exit_code == 0

  @pytest.mark.parametrize(
      "criterion",
      ["correctness", "hallucination", "sentiment"],
  )
  @patch("bigquery_agent_analytics.cli._build_client")
  def test_llm_judge_uses_sdk_default(self, mock_build, criterion):
    """Omitting --threshold for llm-judge should use 0.5, not 5000."""
    client = MagicMock()
    client.evaluate.return_value = _mock_report(10, 10)
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=llm-judge",
            f"--criterion={criterion}",
        ],
    )
    assert result.exit_code == 0


# ------------------------------------------------------------------ #
# Helpers for v1.1 commands                                            #
# ------------------------------------------------------------------ #


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


def _mock_distribution():
  return QuestionDistribution(total_questions=50)


# ------------------------------------------------------------------ #
# insights                                                             #
# ------------------------------------------------------------------ #


class TestInsights:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_insights_json(self, mock_build):
    client = MagicMock()
    client.insights.return_value = _mock_insights()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "insights",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "aggregated" in parsed

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_insights_with_filters(self, mock_build):
    client = MagicMock()
    client.insights.return_value = _mock_insights()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "insights",
            "--project-id=proj",
            "--dataset-id=ds",
            "--agent-id=bot",
            "--last=1h",
            "--max-sessions=25",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.insights.call_args[1]
    assert call_kwargs["filters"].agent_id == "bot"
    assert call_kwargs["config"].max_sessions == 25

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_insights_error_exit_2(self, mock_build):
    client = MagicMock()
    client.insights.side_effect = RuntimeError("fail")
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "insights",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# drift                                                                #
# ------------------------------------------------------------------ #


class TestDrift:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_drift_json(self, mock_build):
    client = MagicMock()
    client.drift_detection.return_value = _mock_drift()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "drift",
            "--project-id=proj",
            "--dataset-id=ds",
            "--golden-dataset=golden",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["coverage_percentage"] == 0.85

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_drift_with_filters(self, mock_build):
    client = MagicMock()
    client.drift_detection.return_value = _mock_drift()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "drift",
            "--project-id=proj",
            "--dataset-id=ds",
            "--golden-dataset=golden",
            "--agent-id=bot",
            "--last=24h",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.drift_detection.call_args[1]
    assert call_kwargs["golden_dataset"] == "golden"
    assert call_kwargs["filters"].agent_id == "bot"

  def test_drift_missing_golden_dataset_exit_2(self):
    result = runner.invoke(
        app,
        [
            "drift",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_drift_error_exit_2(self, mock_build):
    client = MagicMock()
    client.drift_detection.side_effect = RuntimeError("fail")
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "drift",
            "--project-id=proj",
            "--dataset-id=ds",
            "--golden-dataset=golden",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# distribution                                                         #
# ------------------------------------------------------------------ #


class TestDistribution:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_distribution_json(self, mock_build):
    client = MagicMock()
    client.deep_analysis.return_value = _mock_distribution()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "distribution",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["total_questions"] == 50

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_distribution_with_options(self, mock_build):
    client = MagicMock()
    client.deep_analysis.return_value = _mock_distribution()
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "distribution",
            "--project-id=proj",
            "--dataset-id=ds",
            "--mode=frequently_asked",
            "--top-k=10",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.deep_analysis.call_args[1]
    assert call_kwargs["configuration"].mode == "frequently_asked"
    assert call_kwargs["configuration"].top_k == 10

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_distribution_error_exit_2(self, mock_build):
    client = MagicMock()
    client.deep_analysis.side_effect = RuntimeError("fail")
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "distribution",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# hitl-metrics                                                         #
# ------------------------------------------------------------------ #


class TestHitlMetrics:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_hitl_metrics_json(self, mock_build):
    client = MagicMock()
    client.hitl_metrics.return_value = {
        "total_hitl_events": 15,
        "sessions_with_hitl": 3,
    }
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "hitl-metrics",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["total_hitl_events"] == 15

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_hitl_metrics_with_filters(self, mock_build):
    client = MagicMock()
    client.hitl_metrics.return_value = {"total_hitl_events": 5}
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "hitl-metrics",
            "--project-id=proj",
            "--dataset-id=ds",
            "--agent-id=bot",
            "--last=7d",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.hitl_metrics.call_args[1]
    assert call_kwargs["filters"].agent_id == "bot"

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_hitl_metrics_error_exit_2(self, mock_build):
    client = MagicMock()
    client.hitl_metrics.side_effect = RuntimeError("fail")
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "hitl-metrics",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# list-traces                                                          #
# ------------------------------------------------------------------ #


class TestListTraces:

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_list_traces_json(self, mock_build):
    client = MagicMock()
    client.list_traces.return_value = [_mock_trace(), _mock_trace()]
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "list-traces",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert len(parsed) == 2

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_list_traces_with_filters(self, mock_build):
    client = MagicMock()
    client.list_traces.return_value = [_mock_trace()]
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "list-traces",
            "--project-id=proj",
            "--dataset-id=ds",
            "--session-id=s1",
            "--agent-id=bot",
            "--last=1h",
            "--limit=10",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.list_traces.call_args[1]
    filters = call_kwargs["filter_criteria"]
    assert filters.agent_id == "bot"
    assert filters.limit == 10
    assert filters.session_ids == ["s1"]

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_list_traces_error_exit_2(self, mock_build):
    client = MagicMock()
    client.list_traces.side_effect = RuntimeError("fail")
    mock_build.return_value = client

    result = runner.invoke(
        app,
        [
            "list-traces",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# views create-all / views create                                      #
# ------------------------------------------------------------------ #


class TestViews:

  @patch("bigquery_agent_analytics.cli._build_view_manager")
  def test_views_create_all(self, mock_build_vm):
    vm = MagicMock()
    vm.create_all_views.return_value = {
        "LLM_REQUEST": "adk_llm_request",
        "TOOL_CALL": "adk_tool_call",
    }
    mock_build_vm.return_value = vm

    result = runner.invoke(
        app,
        [
            "views",
            "create-all",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["LLM_REQUEST"] == "adk_llm_request"

  @patch("bigquery_agent_analytics.cli._build_view_manager")
  def test_views_create_all_with_prefix(self, mock_build_vm):
    vm = MagicMock()
    vm.create_all_views.return_value = {}
    mock_build_vm.return_value = vm

    result = runner.invoke(
        app,
        [
            "views",
            "create-all",
            "--project-id=proj",
            "--dataset-id=ds",
            "--prefix=custom_",
        ],
    )
    assert result.exit_code == 0
    mock_build_vm.assert_called_once()
    call_args = mock_build_vm.call_args
    assert (
        call_args[0][3] == "custom_" or call_args[1].get("prefix") == "custom_"
    )

  @patch("bigquery_agent_analytics.cli._build_view_manager")
  def test_views_create_single(self, mock_build_vm):
    vm = MagicMock()
    mock_build_vm.return_value = vm

    result = runner.invoke(
        app,
        [
            "views",
            "create",
            "LLM_REQUEST",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    vm.create_view.assert_called_once_with("LLM_REQUEST")
    parsed = json.loads(result.output)
    assert parsed["event_type"] == "LLM_REQUEST"
    assert parsed["status"] == "created"

  @patch("bigquery_agent_analytics.cli._build_view_manager")
  def test_views_create_all_error_exit_2(self, mock_build_vm):
    vm = MagicMock()
    vm.create_all_views.side_effect = RuntimeError("fail")
    mock_build_vm.return_value = vm

    result = runner.invoke(
        app,
        [
            "views",
            "create-all",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2

  @patch("bigquery_agent_analytics.cli._build_view_manager")
  def test_views_create_error_exit_2(self, mock_build_vm):
    vm = MagicMock()
    vm.create_view.side_effect = RuntimeError("fail")
    mock_build_vm.return_value = vm

    result = runner.invoke(
        app,
        [
            "views",
            "create",
            "BOGUS",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


# ------------------------------------------------------------------ #
# categorical-eval                                                     #
# ------------------------------------------------------------------ #

_METRICS_JSON = [
    {
        "name": "tone",
        "definition": "Overall tone of the conversation.",
        "categories": [
            {"name": "positive", "definition": "User is satisfied."},
            {"name": "negative", "definition": "User is frustrated."},
            {"name": "neutral", "definition": "No strong sentiment."},
        ],
    },
]


def _mock_categorical_report():
  from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport
  from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
  from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult

  return CategoricalEvaluationReport(
      dataset="test",
      total_sessions=2,
      session_results=[
          CategoricalSessionResult(
              session_id="s1",
              metrics=[
                  CategoricalMetricResult(
                      metric_name="tone",
                      category="positive",
                      passed_validation=True,
                  ),
              ],
          ),
          CategoricalSessionResult(
              session_id="s2",
              metrics=[
                  CategoricalMetricResult(
                      metric_name="tone",
                      category="negative",
                      passed_validation=True,
                  ),
              ],
          ),
      ],
      category_distributions={"tone": {"positive": 1, "negative": 1}},
      details={"execution_mode": "ai_generate"},
  )


class TestCategoricalEval:

  def _write_metrics(self, tmp_path):
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps(_METRICS_JSON))
    return str(metrics_path)

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_json(self, mock_build, tmp_path):
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["total_sessions"] == 2
    assert "tone" in parsed["category_distributions"]

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_text_format(self, mock_build, tmp_path):
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--format=text",
        ],
    )
    assert result.exit_code == 0
    assert "categorical_evaluator" in result.output

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_with_filters(self, mock_build, tmp_path):
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--agent-id=bot",
            "--last=7d",
            "--limit=50",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.evaluate_categorical.call_args[1]
    assert call_kwargs["filters"].agent_id == "bot"
    assert call_kwargs["filters"].limit == 50

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_persist_flags(self, mock_build, tmp_path):
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--persist",
            "--results-table=my_results",
            "--prompt-version=v1",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.evaluate_categorical.call_args[1]
    config = call_kwargs["config"]
    assert config.persist_results is True
    assert config.results_table == "my_results"
    assert config.prompt_version == "v1"

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_endpoint_override(self, mock_build, tmp_path):
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--endpoint=gemini-2.0-flash",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.evaluate_categorical.call_args[1]
    assert call_kwargs["config"].endpoint == "gemini-2.0-flash"
    # Also passed to _build_client
    build_kwargs = mock_build.call_args
    assert (
        build_kwargs[1].get("endpoint") == "gemini-2.0-flash"
        or build_kwargs[0][4] == "gemini-2.0-flash"
    )

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_no_justification(self, mock_build, tmp_path):
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--no-include-justification",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.evaluate_categorical.call_args[1]
    assert call_kwargs["config"].include_justification is False

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_metrics_dict_format(self, mock_build, tmp_path):
    """Metrics file can be a dict with a 'metrics' key."""
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client

    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps({"metrics": _METRICS_JSON}))

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={str(metrics_path)}",
        ],
    )
    assert result.exit_code == 0

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_error_exit_2(self, mock_build, tmp_path):
    client = MagicMock()
    client.evaluate_categorical.side_effect = RuntimeError("BQ fail")
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
        ],
    )
    assert result.exit_code == 2

  def test_categorical_eval_missing_metrics_file_exit_2(self):
    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            "--metrics-file=/nonexistent/file.json",
        ],
    )
    assert result.exit_code == 2

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_required_false_passthrough(
      self, mock_build, tmp_path
  ):
    """Metrics with required=false in JSON should preserve the field."""
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client

    metrics_with_optional = [
        {
            "name": "tone",
            "definition": "Overall tone.",
            "required": False,
            "categories": [
                {"name": "positive", "definition": "User is satisfied."},
                {"name": "negative", "definition": "User is frustrated."},
            ],
        },
    ]
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps(metrics_with_optional))

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={str(metrics_path)}",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = client.evaluate_categorical.call_args[1]
    config = call_kwargs["config"]
    assert config.metrics[0].required is False

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_empty_metrics_exit_2(self, mock_build, tmp_path):
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text("[]")

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={str(metrics_path)}",
        ],
    )
    assert result.exit_code == 2

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_connection_id_passthrough(
      self, mock_build, tmp_path
  ):
    """--connection-id should be passed to _build_client() and config."""
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--connection-id=proj.us.conn",
        ],
    )
    assert result.exit_code == 0, result.output

    # Verify passed to _build_client.
    build_kwargs = mock_build.call_args
    assert build_kwargs[1].get("connection_id") == "proj.us.conn"

    # Verify passed to config.
    call_kwargs = client.evaluate_categorical.call_args[1]
    assert call_kwargs["config"].connection_id == "proj.us.conn"

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_exit_code_passes(self, mock_build, tmp_path):
    """All sessions match the declared pass category -> exit 0."""
    from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport

    report = CategoricalEvaluationReport(
        dataset="test",
        total_sessions=3,
        category_distributions={"tone": {"positive": 3}},
    )
    client = MagicMock()
    client.evaluate_categorical.return_value = report
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=tone=positive",
        ],
    )
    assert result.exit_code == 0

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_exit_code_fails_below_min(
      self, mock_build, tmp_path
  ):
    """Pass rate below --min-pass-rate -> exit 1 with a FAIL line."""
    from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport

    report = CategoricalEvaluationReport(
        dataset="test",
        total_sessions=4,
        category_distributions={"tone": {"positive": 2, "negative": 2}},
    )
    client = MagicMock()
    client.evaluate_categorical.return_value = report
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=tone=positive",
            "--min-pass-rate=0.9",
        ],
    )
    assert result.exit_code == 1
    combined = (result.stderr or "") + (result.output or "")
    assert "FAIL metric=tone" in combined
    assert "pass_rate=0.5" in combined
    assert "(2/4)" in combined
    assert "min=0.9" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_exit_code_requires_pass_category(
      self, mock_build, tmp_path
  ):
    """--exit-code without any --pass-category exits 2 with guidance."""
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
        ],
    )
    assert result.exit_code == 2
    combined = (result.stderr or "") + (result.output or "")
    assert "--pass-category" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_pass_category_invalid_format(
      self, mock_build, tmp_path
  ):
    """Malformed --pass-category value exits 2 with a readable error."""
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=not_a_key_value_pair",
        ],
    )
    assert result.exit_code == 2
    combined = (result.stderr or "") + (result.output or "")
    assert "--pass-category" in combined
    assert "METRIC=CATEGORY" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_exit_code_missing_metric_warns(
      self, mock_build, tmp_path
  ):
    """--pass-category for an absent metric warns but doesn't fail the run."""
    from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport

    report = CategoricalEvaluationReport(
        dataset="test",
        total_sessions=2,
        category_distributions={"tone": {"positive": 2}},
    )
    client = MagicMock()
    client.evaluate_categorical.return_value = report
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=tone=positive",
            "--pass-category=not_a_metric=whatever",
        ],
    )
    assert result.exit_code == 0
    combined = (result.stderr or "") + (result.output or "")
    assert "WARN" in combined
    assert "not_a_metric" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_exit_code_counts_parse_errors_as_fail(
      self, mock_build, tmp_path
  ):
    """Parse errors and missing classifications count as failing, not unknown.

    Regression guard: if ``build_categorical_report`` drops broken
    classifications from ``category_distributions`` (it does), the
    gate must still treat them as failures for the declared metric.
    Otherwise a totally broken classification run silently passes CI.
    """
    from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport
    from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
    from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult

    report = CategoricalEvaluationReport(
        dataset="test",
        total_sessions=4,
        category_distributions={"tone": {"positive": 1}},
        session_results=[
            CategoricalSessionResult(
                session_id="good",
                metrics=[
                    CategoricalMetricResult(
                        metric_name="tone",
                        category="positive",
                        passed_validation=True,
                    )
                ],
            ),
            CategoricalSessionResult(
                session_id="parse_err",
                metrics=[
                    CategoricalMetricResult(
                        metric_name="tone",
                        category=None,
                        parse_error=True,
                        passed_validation=False,
                    )
                ],
            ),
            CategoricalSessionResult(
                session_id="invalid_cat",
                metrics=[
                    CategoricalMetricResult(
                        metric_name="tone",
                        category="unexpected",
                        passed_validation=False,
                    )
                ],
            ),
            CategoricalSessionResult(
                session_id="no_classification",
                metrics=[],
            ),
        ],
    )
    client = MagicMock()
    client.evaluate_categorical.return_value = report
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=tone=positive",
            "--min-pass-rate=0.9",
        ],
    )
    assert result.exit_code == 1
    combined = (result.stderr or "") + (result.output or "")
    assert "FAIL metric=tone" in combined
    # 1 passing / 4 total, not 1/1 (which would have been a silent pass).
    assert "(1/4)" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_exit_code_all_parse_errors_fails(
      self, mock_build, tmp_path
  ):
    """Every session parse-errored -> gate fails (0/N), not silent pass."""
    from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport
    from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
    from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult

    report = CategoricalEvaluationReport(
        dataset="test",
        total_sessions=3,
        category_distributions={"tone": {}},
        session_results=[
            CategoricalSessionResult(
                session_id=f"s{i}",
                metrics=[
                    CategoricalMetricResult(
                        metric_name="tone",
                        category=None,
                        parse_error=True,
                        passed_validation=False,
                    )
                ],
            )
            for i in range(3)
        ],
    )
    client = MagicMock()
    client.evaluate_categorical.return_value = report
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=tone=positive",
        ],
    )
    assert result.exit_code == 1
    combined = (result.stderr or "") + (result.output or "")
    assert "FAIL metric=tone" in combined
    assert "(0/3)" in combined

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_exit_code_validates_flags_before_run(
      self, mock_build, tmp_path
  ):
    """Missing --pass-category under --exit-code rejects BEFORE BQ work.

    Regression guard: invalid CI configuration should exit 2 without
    spending BigQuery / LLM credits on the classification run.
    """
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
        ],
    )
    assert result.exit_code == 2
    client.evaluate_categorical.assert_not_called()

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_malformed_flag_validates_before_run(
      self, mock_build, tmp_path
  ):
    """Malformed --pass-category under --exit-code rejects BEFORE BQ work."""
    client = MagicMock()
    client.evaluate_categorical.return_value = _mock_categorical_report()
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=not_a_pair",
        ],
    )
    assert result.exit_code == 2
    client.evaluate_categorical.assert_not_called()

  @patch("bigquery_agent_analytics.cli._build_client")
  def test_categorical_eval_multiple_pass_categories_per_metric(
      self, mock_build, tmp_path
  ):
    """Multiple --pass-category flags for one metric OR together."""
    from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport

    report = CategoricalEvaluationReport(
        dataset="test",
        total_sessions=10,
        category_distributions={
            "tone": {"positive": 7, "neutral": 2, "negative": 1}
        },
    )
    client = MagicMock()
    client.evaluate_categorical.return_value = report
    mock_build.return_value = client
    metrics_path = self._write_metrics(tmp_path)

    result = runner.invoke(
        app,
        [
            "categorical-eval",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--metrics-file={metrics_path}",
            "--exit-code",
            "--pass-category=tone=positive",
            "--pass-category=tone=neutral",
            "--min-pass-rate=0.85",
        ],
    )
    # 9/10 pass >= 0.85 -> exit 0
    assert result.exit_code == 0


# ------------------------------------------------------------------ #
# categorical-views                                                    #
# ------------------------------------------------------------------ #


class TestCategoricalViews:

  @patch("bigquery_agent_analytics.categorical_views.CategoricalViewManager")
  def test_categorical_views_json(self, mock_cls):
    vm = MagicMock()
    vm.create_all_views.return_value = {
        "categorical_results_latest": "categorical_results_latest",
        "categorical_daily_counts": "categorical_daily_counts",
    }
    mock_cls.return_value = vm

    result = runner.invoke(
        app,
        [
            "categorical-views",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "categorical_results_latest" in parsed

  @patch("bigquery_agent_analytics.categorical_views.CategoricalViewManager")
  def test_categorical_views_with_prefix(self, mock_cls):
    vm = MagicMock()
    vm.create_all_views.return_value = {
        "categorical_results_latest": "adk_categorical_results_latest",
    }
    mock_cls.return_value = vm

    result = runner.invoke(
        app,
        [
            "categorical-views",
            "--project-id=proj",
            "--dataset-id=ds",
            "--prefix=adk_",
        ],
    )
    assert result.exit_code == 0
    mock_cls.assert_called_once()
    call_kwargs = mock_cls.call_args[1]
    assert call_kwargs["view_prefix"] == "adk_"
    assert call_kwargs["location"] is None

  @patch("bigquery_agent_analytics.categorical_views.CategoricalViewManager")
  def test_categorical_views_custom_table(self, mock_cls):
    vm = MagicMock()
    vm.create_all_views.return_value = {}
    mock_cls.return_value = vm

    result = runner.invoke(
        app,
        [
            "categorical-views",
            "--project-id=proj",
            "--dataset-id=ds",
            "--results-table=my_results",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = mock_cls.call_args[1]
    assert call_kwargs["results_table"] == "my_results"

  @patch("bigquery_agent_analytics.categorical_views.CategoricalViewManager")
  def test_categorical_views_error_exit_2(self, mock_cls):
    vm = MagicMock()
    vm.create_all_views.side_effect = RuntimeError("BQ fail")
    mock_cls.return_value = vm

    result = runner.invoke(
        app,
        [
            "categorical-views",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
    assert result.exit_code == 2


class TestOntologyPropertyGraph:

  _SPEC_PATH = os.path.join(
      os.path.dirname(__file__),
      "..",
      "examples",
      "ymgo_graph_spec.yaml",
  )

  def test_sql_output(self):
    result = runner.invoke(
        app,
        [
            "ontology-property-graph",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--env=p.d",
        ],
    )
    assert result.exit_code == 0
    assert "CREATE OR REPLACE PROPERTY GRAPH" in result.output
    assert "NODE TABLES" in result.output
    assert "EDGE TABLES" in result.output

  def test_custom_graph_name(self):
    result = runner.invoke(
        app,
        [
            "ontology-property-graph",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--env=p.d",
            "--graph-name=my_custom_graph",
        ],
    )
    assert result.exit_code == 0
    assert "my_custom_graph" in result.output

  def test_json_output(self):
    result = runner.invoke(
        app,
        [
            "ontology-property-graph",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--env=p.d",
            "--format=json",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "ddl" in parsed
    assert "graph_name" in parsed

  @patch(
      "bigquery_agent_analytics.ontology_property_graph"
      ".OntologyPropertyGraphCompiler.create_property_graph"
  )
  def test_execute_flag(self, mock_create):
    mock_create.return_value = True

    result = runner.invoke(
        app,
        [
            "ontology-property-graph",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--env=p.d",
            "--execute",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["success"] is True
    mock_create.assert_called_once()

  def test_bad_spec_path_exit_2(self):
    result = runner.invoke(
        app,
        [
            "ontology-property-graph",
            "--project-id=proj",
            "--dataset-id=ds",
            "--spec-path=/nonexistent/path.yaml",
        ],
    )
    assert result.exit_code == 2


class TestOntologyShowcaseGql:

  _SPEC_PATH = os.path.join(
      os.path.dirname(__file__),
      "..",
      "examples",
      "ymgo_graph_spec.yaml",
  )

  def test_sql_output(self):
    result = runner.invoke(
        app,
        [
            "ontology-showcase-gql",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--env=p.d",
        ],
    )
    assert result.exit_code == 0
    assert "GRAPH" in result.output
    assert "MATCH" in result.output
    assert "mako_DecisionPoint" in result.output

  def test_json_output(self):
    result = runner.invoke(
        app,
        [
            "ontology-showcase-gql",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--env=p.d",
            "--format=json",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "gql" in parsed
    assert "graph_name" in parsed

  def test_specific_relationship(self):
    result = runner.invoke(
        app,
        [
            "ontology-showcase-gql",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--env=p.d",
            "--relationship=ForCandidate",
        ],
    )
    assert result.exit_code == 0
    assert "ForCandidate" in result.output

  def test_bad_spec_path_exit_2(self):
    result = runner.invoke(
        app,
        [
            "ontology-showcase-gql",
            "--project-id=proj",
            "--dataset-id=ds",
            "--spec-path=/nonexistent/path.yaml",
        ],
    )
    assert result.exit_code == 2


class TestOntologyBuild:

  _SPEC_PATH = os.path.join(
      os.path.dirname(__file__),
      "..",
      "examples",
      "ymgo_graph_spec.yaml",
  )

  @patch("bigquery_agent_analytics.ontology_orchestrator.build_ontology_graph")
  def test_build_command(self, mock_build):
    from bigquery_agent_analytics.ontology_models import ExtractedGraph

    mock_build.return_value = {
        "graph_name": "YMGO_Context_Graph_V3",
        "graph_ref": "proj.ds.YMGO_Context_Graph_V3",
        "graph": ExtractedGraph(name="test"),
        "tables_created": {"mako_DecisionPoint": "p.d.decision_points"},
        "rows_materialized": {"mako_DecisionPoint": 2},
        "property_graph_created": True,
        "spec": MagicMock(),
    }

    result = runner.invoke(
        app,
        [
            "ontology-build",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--session-ids=sess1,sess2",
            "--env=p.d",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["graph_name"] == "YMGO_Context_Graph_V3"
    assert parsed["property_graph_created"] is True
    mock_build.assert_called_once()

  @patch("bigquery_agent_analytics.ontology_orchestrator.build_ontology_graph")
  def test_property_graph_failure_exit_1(self, mock_build):
    from bigquery_agent_analytics.ontology_models import ExtractedGraph

    mock_build.return_value = {
        "graph_name": "g",
        "graph_ref": "proj.ds.g",
        "graph": ExtractedGraph(name="test"),
        "tables_created": {},
        "rows_materialized": {},
        "property_graph_created": False,
        "spec": MagicMock(),
    }

    result = runner.invoke(
        app,
        [
            "ontology-build",
            "--project-id=proj",
            "--dataset-id=ds",
            f"--spec-path={self._SPEC_PATH}",
            "--session-ids=sess1",
            "--env=p.d",
        ],
    )
    assert result.exit_code == 1

  def test_bad_spec_path_exit_2(self):
    result = runner.invoke(
        app,
        [
            "ontology-build",
            "--project-id=proj",
            "--dataset-id=ds",
            "--spec-path=/nonexistent/path.yaml",
            "--session-ids=sess1",
        ],
    )
    assert result.exit_code == 2
