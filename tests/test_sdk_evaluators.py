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

"""Tests for the SDK evaluators module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.evaluators import _parse_json_from_text
from bigquery_agent_analytics.evaluators import AI_GENERATE_JUDGE_BATCH_QUERY
from bigquery_agent_analytics.evaluators import CodeEvaluator
from bigquery_agent_analytics.evaluators import DEFAULT_ENDPOINT
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import LLM_JUDGE_BATCH_QUERY
from bigquery_agent_analytics.evaluators import LLMAsJudge
from bigquery_agent_analytics.evaluators import SESSION_SUMMARY_QUERY
from bigquery_agent_analytics.evaluators import SessionScore


class TestCodeEvaluator:
  """Tests for CodeEvaluator class."""

  def test_custom_metric(self):
    evaluator = CodeEvaluator(name="test")
    evaluator.add_metric(
        name="custom",
        fn=lambda s: 0.8,
        threshold=0.5,
    )

    summary = {"session_id": "s1", "tool_calls": 5}
    score = evaluator.evaluate_session(summary)

    assert score.session_id == "s1"
    assert score.scores["custom"] == 0.8
    assert score.passed is True

  def test_custom_metric_fail(self):
    evaluator = CodeEvaluator(name="test")
    evaluator.add_metric(
        name="custom",
        fn=lambda s: 0.2,
        threshold=0.5,
    )

    score = evaluator.evaluate_session({"session_id": "s1"})

    assert score.scores["custom"] == 0.2
    assert score.passed is False

  def test_metric_exception_handled(self):
    evaluator = CodeEvaluator(name="test")
    evaluator.add_metric(
        name="broken",
        fn=lambda s: 1 / 0,
        threshold=0.5,
    )

    score = evaluator.evaluate_session({"session_id": "s1"})

    assert score.scores["broken"] == 0.0
    assert score.passed is False

  def test_metric_clamping(self):
    evaluator = CodeEvaluator(name="test")
    evaluator.add_metric(
        name="over",
        fn=lambda s: 1.5,
        threshold=0.5,
    )
    evaluator.add_metric(
        name="under",
        fn=lambda s: -0.5,
        threshold=0.0,
    )

    score = evaluator.evaluate_session({"session_id": "s1"})

    assert score.scores["over"] == 1.0
    assert score.scores["under"] == 0.0

  def test_chaining(self):
    evaluator = (
        CodeEvaluator(name="chain")
        .add_metric("a", lambda s: 0.9)
        .add_metric("b", lambda s: 0.7)
    )
    score = evaluator.evaluate_session({"session_id": "s1"})
    assert "a" in score.scores
    assert "b" in score.scores


class TestCodeEvaluatorPrebuilt:
  """Tests for pre-built CodeEvaluator factories."""

  def test_latency_pass(self):
    evaluator = CodeEvaluator.latency(threshold_ms=5000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "avg_latency_ms": 2000,
        }
    )
    assert score.passed is True
    assert score.scores["latency"] == 1.0

  def test_latency_fail(self):
    evaluator = CodeEvaluator.latency(threshold_ms=1000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "avg_latency_ms": 2000,
        }
    )
    assert score.passed is False
    assert score.scores["latency"] == 0.0

  def test_latency_zero(self):
    evaluator = CodeEvaluator.latency(threshold_ms=5000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "avg_latency_ms": 0,
        }
    )
    assert score.scores["latency"] == 1.0

  def test_turn_count_pass(self):
    evaluator = CodeEvaluator.turn_count(max_turns=10)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "turn_count": 3,
        }
    )
    assert score.passed is True
    assert score.scores["turn_count"] == 1.0

  def test_turn_count_fail(self):
    evaluator = CodeEvaluator.turn_count(max_turns=5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "turn_count": 8,
        }
    )
    assert score.passed is False

  def test_error_rate_pass(self):
    evaluator = CodeEvaluator.error_rate(max_error_rate=0.1)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "tool_calls": 20,
            "tool_errors": 1,
        }
    )
    assert score.passed is True

  def test_error_rate_fail(self):
    evaluator = CodeEvaluator.error_rate(max_error_rate=0.1)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "tool_calls": 10,
            "tool_errors": 5,
        }
    )
    assert score.passed is False

  def test_error_rate_no_calls(self):
    evaluator = CodeEvaluator.error_rate(max_error_rate=0.1)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "tool_calls": 0,
            "tool_errors": 0,
        }
    )
    assert score.scores["error_rate"] == 1.0


class TestPrebuiltRawBudgetBoundaries:
  """Regression tests: prebuilt gates compare raw observed <= budget.

  Prior implementation used normalized scores with a 0.5 pass cutoff,
  which caused every gate to effectively fire at ``budget / 2`` (e.g.
  ``CodeEvaluator.latency(threshold_ms=5000)`` failed at observed >
  2500 ms). These tests lock in the new raw-budget semantics and
  guard against regressions.
  """

  def test_latency_boundary_inclusive(self):
    evaluator = CodeEvaluator.latency(threshold_ms=5000)
    at_budget = evaluator.evaluate_session(
        {"session_id": "s1", "avg_latency_ms": 5000}
    )
    just_over = evaluator.evaluate_session(
        {"session_id": "s2", "avg_latency_ms": 5001}
    )
    # Observed == budget must PASS (regression: old impl failed here at
    # any value strictly above budget/2 = 2500).
    assert at_budget.passed is True
    assert at_budget.scores["latency"] == 1.0
    assert just_over.passed is False
    assert just_over.scores["latency"] == 0.0

  def test_latency_old_midpoint_now_passes(self):
    # The old normalized impl failed at 2501ms with threshold=5000; under
    # the new impl this is nowhere near the budget and must pass.
    evaluator = CodeEvaluator.latency(threshold_ms=5000)
    score = evaluator.evaluate_session(
        {"session_id": "s1", "avg_latency_ms": 2501}
    )
    assert score.passed is True
    assert score.scores["latency"] == 1.0

  def test_turn_count_boundary_inclusive(self):
    evaluator = CodeEvaluator.turn_count(max_turns=10)
    at_budget = evaluator.evaluate_session(
        {"session_id": "s1", "turn_count": 10}
    )
    just_over = evaluator.evaluate_session(
        {"session_id": "s2", "turn_count": 11}
    )
    assert at_budget.passed is True
    assert just_over.passed is False

  def test_turn_count_old_midpoint_now_passes(self):
    evaluator = CodeEvaluator.turn_count(max_turns=10)
    score = evaluator.evaluate_session({"session_id": "s1", "turn_count": 6})
    # Old impl: 1.0 - 6/10 = 0.4 -> fail. New: 6 <= 10 -> pass.
    assert score.passed is True

  def test_error_rate_boundary_inclusive(self):
    evaluator = CodeEvaluator.error_rate(max_error_rate=0.1)
    at_budget = evaluator.evaluate_session(
        {"session_id": "s1", "tool_calls": 10, "tool_errors": 1}
    )
    just_over = evaluator.evaluate_session(
        {"session_id": "s2", "tool_calls": 10, "tool_errors": 2}
    )
    assert at_budget.passed is True
    assert just_over.passed is False

  def test_token_efficiency_boundary_inclusive(self):
    evaluator = CodeEvaluator.token_efficiency(max_tokens=50000)
    at_budget = evaluator.evaluate_session(
        {"session_id": "s1", "total_tokens": 50000}
    )
    just_over = evaluator.evaluate_session(
        {"session_id": "s2", "total_tokens": 50001}
    )
    assert at_budget.passed is True
    assert just_over.passed is False

  def test_ttft_boundary_inclusive(self):
    evaluator = CodeEvaluator.ttft(threshold_ms=1000)
    at_budget = evaluator.evaluate_session(
        {"session_id": "s1", "avg_ttft_ms": 1000}
    )
    just_over = evaluator.evaluate_session(
        {"session_id": "s2", "avg_ttft_ms": 1001}
    )
    assert at_budget.passed is True
    assert just_over.passed is False

  def test_cost_per_session_boundary_inclusive(self):
    evaluator = CodeEvaluator.cost_per_session(
        max_cost_usd=0.01,
        input_cost_per_1k=0.001,
        output_cost_per_1k=0.001,
    )
    # Cost at budget: (5000/1000 + 5000/1000) * 0.001 = 0.01 exactly.
    at_budget = evaluator.evaluate_session(
        {"session_id": "s1", "input_tokens": 5000, "output_tokens": 5000}
    )
    # Cost just over: 10001 input tokens at 0.001/1K -> 0.010001.
    just_over = evaluator.evaluate_session(
        {"session_id": "s2", "input_tokens": 10001, "output_tokens": 0}
    )
    assert at_budget.passed is True
    assert just_over.passed is False

  def test_observed_key_and_budget_in_details(self):
    """Per-metric detail must expose observed/budget for CLI output."""
    evaluator = CodeEvaluator.latency(threshold_ms=5000)
    score = evaluator.evaluate_session(
        {"session_id": "s1", "avg_latency_ms": 6000}
    )
    detail = score.details.get("metric_latency")
    assert detail is not None
    assert detail["observed"] == 6000
    assert detail["budget"] == 5000
    assert detail["passed"] is False

  def test_error_rate_observed_fn_in_details(self):
    """Computed observed (errors/calls) surfaces in details via observed_fn."""
    evaluator = CodeEvaluator.error_rate(max_error_rate=0.1)
    score = evaluator.evaluate_session(
        {"session_id": "s1", "tool_calls": 10, "tool_errors": 5}
    )
    detail = score.details.get("metric_error_rate")
    assert detail is not None
    assert detail["observed"] == pytest.approx(0.5)
    assert detail["budget"] == pytest.approx(0.1)
    assert detail["passed"] is False

  def test_cost_observed_fn_in_details(self):
    """Computed cost surfaces in details via observed_fn."""
    evaluator = CodeEvaluator.cost_per_session(
        max_cost_usd=0.01,
        input_cost_per_1k=0.001,
        output_cost_per_1k=0.001,
    )
    score = evaluator.evaluate_session(
        {"session_id": "s1", "input_tokens": 20000, "output_tokens": 0}
    )
    detail = score.details.get("metric_cost")
    assert detail is not None
    # 20000 / 1000 * 0.001 = 0.02
    assert detail["observed"] == pytest.approx(0.02)
    assert detail["budget"] == pytest.approx(0.01)
    assert detail["passed"] is False


class TestLLMAsJudgePrebuilt:
  """Tests for pre-built LLMAsJudge factories."""

  def test_correctness_factory(self):
    judge = LLMAsJudge.correctness(threshold=0.7)
    assert judge.name == "correctness_judge"
    assert len(judge._criteria) == 1
    assert judge._criteria[0].name == "correctness"
    assert judge._criteria[0].threshold == 0.7

  def test_hallucination_factory(self):
    judge = LLMAsJudge.hallucination()
    assert judge.name == "hallucination_judge"
    assert judge._criteria[0].name == "faithfulness"

  def test_sentiment_factory(self):
    judge = LLMAsJudge.sentiment()
    assert judge.name == "sentiment_judge"
    assert judge._criteria[0].name == "sentiment"

  def test_custom_criterion(self):
    judge = LLMAsJudge(name="custom")
    judge.add_criterion(
        name="helpfulness",
        prompt_template="Rate helpfulness: {trace_text} {final_response}",
        score_key="helpfulness",
        threshold=0.6,
    )
    assert len(judge._criteria) == 1
    assert judge._criteria[0].name == "helpfulness"

  def test_chaining(self):
    judge = (
        LLMAsJudge(name="multi")
        .add_criterion("a", "p1 {trace_text} {final_response}", "a")
        .add_criterion("b", "p2 {trace_text} {final_response}", "b")
    )
    assert len(judge._criteria) == 2


class TestEvaluationReport:
  """Tests for EvaluationReport class."""

  def test_pass_rate(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="test_eval",
        total_sessions=10,
        passed_sessions=7,
        failed_sessions=3,
    )
    assert report.pass_rate == 0.7

  def test_pass_rate_zero(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="test_eval",
        total_sessions=0,
        passed_sessions=0,
        failed_sessions=0,
    )
    assert report.pass_rate == 0.0

  def test_summary(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="latency_evaluator",
        total_sessions=5,
        passed_sessions=3,
        failed_sessions=2,
        aggregate_scores={"latency": 0.65},
    )
    text = report.summary()
    assert "latency_evaluator" in text
    assert "5" in text
    assert "60%" in text
    assert "0.650" in text


class TestParseJson:
  """Tests for JSON parsing helper."""

  def test_plain_json(self):
    result = _parse_json_from_text('{"score": 8, "justification": "good"}')
    assert result["score"] == 8

  def test_json_in_code_block(self):
    result = _parse_json_from_text('```json\n{"score": 7}\n```')
    assert result["score"] == 7

  def test_json_with_surrounding_text(self):
    result = _parse_json_from_text(
        'Here is my analysis: {"score": 9} That is all.'
    )
    assert result["score"] == 9

  def test_empty_input(self):
    assert _parse_json_from_text("") is None
    assert _parse_json_from_text(None) is None

  def test_no_json(self):
    assert _parse_json_from_text("no json here") is None


class TestDefaultEndpoint:
  """Tests for DEFAULT_ENDPOINT constant."""

  def test_default_endpoint_value(self):
    assert DEFAULT_ENDPOINT == "gemini-2.5-flash"


class TestAIGenerateJudgeBatchQuery:
  """Tests for the AI.GENERATE judge batch query template."""

  def test_contains_ai_generate(self):
    assert "AI.GENERATE" in AI_GENERATE_JUDGE_BATCH_QUERY

  def test_contains_output_schema(self):
    assert "output_schema" in AI_GENERATE_JUDGE_BATCH_QUERY

  def test_contains_endpoint_placeholder(self):
    assert "{endpoint}" in AI_GENERATE_JUDGE_BATCH_QUERY

  def test_contains_score_and_justification(self):
    assert "score INT64" in AI_GENERATE_JUDGE_BATCH_QUERY
    assert "justification STRING" in AI_GENERATE_JUDGE_BATCH_QUERY

  def test_does_not_contain_ml_generate_text(self):
    assert "ML.GENERATE_TEXT" not in AI_GENERATE_JUDGE_BATCH_QUERY

  def test_legacy_template_uses_ml_generate_text(self):
    assert "ML.GENERATE_TEXT" in LLM_JUDGE_BATCH_QUERY
    assert "ml_generate_text_result" in LLM_JUDGE_BATCH_QUERY


class TestSessionSummaryQuery:
  """Tests for SESSION_SUMMARY_QUERY token fields."""

  def test_contains_input_tokens(self):
    assert "input_tokens" in SESSION_SUMMARY_QUERY

  def test_contains_output_tokens(self):
    assert "output_tokens" in SESSION_SUMMARY_QUERY

  def test_contains_total_tokens(self):
    assert "total_tokens" in SESSION_SUMMARY_QUERY

  def test_contains_cached_tokens(self):
    assert "cached_tokens" in SESSION_SUMMARY_QUERY
    assert "cached_content_token_count" in SESSION_SUMMARY_QUERY

  def test_contains_cache_telemetry_events(self):
    assert "cache_telemetry_events" in SESSION_SUMMARY_QUERY


class TestTokenEfficiencyPrebuilt:
  """Tests for CodeEvaluator.token_efficiency() preset."""

  def test_zero_tokens(self):
    evaluator = CodeEvaluator.token_efficiency(max_tokens=50000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "total_tokens": 0,
        }
    )
    assert score.scores["token_efficiency"] == 1.0
    assert score.passed is True

  def test_under_budget(self):
    evaluator = CodeEvaluator.token_efficiency(max_tokens=50000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "total_tokens": 10000,
        }
    )
    # Binary gate: observed <= budget -> 1.0
    assert score.scores["token_efficiency"] == 1.0
    assert score.passed is True

  def test_over_budget(self):
    evaluator = CodeEvaluator.token_efficiency(max_tokens=50000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "total_tokens": 60000,
        }
    )
    assert score.scores["token_efficiency"] == 0.0
    assert score.passed is False

  def test_exactly_at_budget(self):
    evaluator = CodeEvaluator.token_efficiency(max_tokens=50000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "total_tokens": 50000,
        }
    )
    # Boundary is inclusive (observed <= budget).
    assert score.scores["token_efficiency"] == 1.0
    assert score.passed is True


class TestContextCacheHitRatePrebuilt:
  """Tests for CodeEvaluator.context_cache_hit_rate() preset."""

  def test_warm_cache_passes(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 950,
            "cache_telemetry_events": 2,
        }
    )
    assert score.scores["context_cache_hit_rate"] == pytest.approx(0.95)
    assert score.passed is True
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] == pytest.approx(0.95)
    assert detail["budget"] == pytest.approx(0.5)
    assert detail["cached_tokens"] == 950
    assert detail["input_tokens"] == 1000
    assert detail["cache_telemetry_events"] == 2
    assert detail["cache_state"] == "warm"

  def test_cold_cache_fails(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 50,
            "cache_telemetry_events": 1,
        }
    )
    assert score.scores["context_cache_hit_rate"] == pytest.approx(0.05)
    assert score.passed is False
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] == pytest.approx(0.05)
    assert detail["cache_state"] == "cold_start"

  def test_partial_cache_at_threshold_passes(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 500,
            "cache_telemetry_events": 1,
        }
    )
    assert score.scores["context_cache_hit_rate"] == pytest.approx(0.5)
    assert score.passed is True
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["cache_state"] == "partial"

  def test_missing_cache_telemetry_passes_by_default(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 0,
            "cache_telemetry_events": 0,
        }
    )
    assert score.scores["context_cache_hit_rate"] == 1.0
    assert score.passed is True
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] is None
    assert detail["cache_state"] == "no_cache_telemetry"

  def test_missing_cache_telemetry_can_fail(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(
        min_hit_rate=0.5,
        fail_on_missing_telemetry=True,
    )
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 0,
            "cache_telemetry_events": 0,
        }
    )
    assert score.scores["context_cache_hit_rate"] == 0.0
    assert score.passed is False
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] is None
    assert detail["cache_state"] == "no_cache_telemetry"

  def test_true_zero_cached_tokens_is_cold_start(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 0,
            "cache_telemetry_events": 1,
        }
    )
    assert score.scores["context_cache_hit_rate"] == 0.0
    assert score.passed is False
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] == 0.0
    assert detail["cache_state"] == "cold_start"

  def test_no_llm_input_passes(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 0,
            "cached_tokens": 0,
            "cache_telemetry_events": 0,
        }
    )
    assert score.scores["context_cache_hit_rate"] == 1.0
    assert score.passed is True
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] == 1.0
    assert detail["cache_state"] == "no_llm_input"

  def test_cached_tokens_clamps_above_input_tokens(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 1200,
            "cache_telemetry_events": 1,
        }
    )
    assert score.scores["context_cache_hit_rate"] == 1.0
    assert score.passed is True
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] == 1.0
    assert detail["cache_state"] == "warm"

  def test_non_numeric_cached_tokens_fall_back_to_zero(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": {"bad": "shape"},
            "cache_telemetry_events": 1,
        }
    )
    assert score.scores["context_cache_hit_rate"] == 0.0
    assert score.passed is False
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] == 0.0
    assert detail["cache_state"] == "cold_start"

  def test_legacy_cached_tokens_without_telemetry_count_is_observed(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate=0.5)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 600,
        }
    )
    assert score.scores["context_cache_hit_rate"] == pytest.approx(0.6)
    assert score.passed is True
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["observed"] == pytest.approx(0.6)
    assert detail["cache_telemetry_events"] == 0

  def test_invalid_cache_state_thresholds_raise(self):
    with pytest.raises(ValueError, match="cold_start_rate"):
      CodeEvaluator.context_cache_hit_rate(
          cold_start_rate=0.9,
          warm_rate=0.1,
      )

  def test_invalid_min_hit_rate_negative_raises(self):
    with pytest.raises(ValueError, match="min_hit_rate"):
      CodeEvaluator.context_cache_hit_rate(min_hit_rate=-0.1)

  def test_invalid_min_hit_rate_above_one_raises(self):
    with pytest.raises(ValueError, match="min_hit_rate"):
      CodeEvaluator.context_cache_hit_rate(min_hit_rate=1.1)

  def test_string_min_hit_rate_is_coerced(self):
    evaluator = CodeEvaluator.context_cache_hit_rate(min_hit_rate="0.5")
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "cached_tokens": 950,
            "cache_telemetry_events": 1,
        }
    )
    assert score.passed is True
    detail = score.details["metric_context_cache_hit_rate"]
    assert detail["budget"] == pytest.approx(0.5)
    assert detail["threshold"] == pytest.approx(0.5)


class TestCostPerSessionPrebuilt:
  """Tests for CodeEvaluator.cost_per_session() preset."""

  def test_zero_tokens(self):
    evaluator = CodeEvaluator.cost_per_session(max_cost_usd=1.0)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 0,
            "output_tokens": 0,
        }
    )
    assert score.scores["cost"] == 1.0
    assert score.passed is True

  def test_under_budget(self):
    evaluator = CodeEvaluator.cost_per_session(
        max_cost_usd=1.0,
        input_cost_per_1k=0.001,
        output_cost_per_1k=0.002,
    )
    # Estimated cost = 0.02 USD, well under 1.0 USD budget -> 1.0
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 10000,
            "output_tokens": 5000,
        }
    )
    assert score.scores["cost"] == 1.0
    assert score.passed is True

  def test_over_budget(self):
    evaluator = CodeEvaluator.cost_per_session(
        max_cost_usd=0.01,
        input_cost_per_1k=1.0,
        output_cost_per_1k=1.0,
    )
    # Cost = (1000/1000)*1.0 + (1000/1000)*1.0 = 2.0
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "input_tokens": 1000,
            "output_tokens": 1000,
        }
    )
    assert score.scores["cost"] == 0.0
    assert score.passed is False

  def test_missing_tokens_defaults_to_zero(self):
    evaluator = CodeEvaluator.cost_per_session(max_cost_usd=1.0)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
        }
    )
    assert score.scores["cost"] == 1.0


class TestTTFTPrebuilt:
  """Tests for CodeEvaluator.ttft() preset."""

  def test_zero_ttft(self):
    evaluator = CodeEvaluator.ttft(threshold_ms=1000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "avg_ttft_ms": 0,
        }
    )
    assert score.scores["ttft"] == 1.0
    assert score.passed is True

  def test_under_threshold(self):
    evaluator = CodeEvaluator.ttft(threshold_ms=1000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "avg_ttft_ms": 400,
        }
    )
    assert score.scores["ttft"] == 1.0
    assert score.passed is True

  def test_over_threshold(self):
    evaluator = CodeEvaluator.ttft(threshold_ms=500)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "avg_ttft_ms": 800,
        }
    )
    assert score.scores["ttft"] == 0.0
    assert score.passed is False

  def test_none_ttft_defaults_to_zero(self):
    evaluator = CodeEvaluator.ttft(threshold_ms=1000)
    score = evaluator.evaluate_session(
        {
            "session_id": "s1",
            "avg_ttft_ms": None,
        }
    )
    assert score.scores["ttft"] == 1.0

  def test_evaluator_name(self):
    evaluator = CodeEvaluator.ttft()
    assert evaluator.name == "ttft_evaluator"


class TestSessionSummaryQueryTTFT:
  """Tests for avg_ttft_ms and hitl_events in SESSION_SUMMARY_QUERY."""

  def test_contains_avg_ttft_ms(self):
    assert "avg_ttft_ms" in SESSION_SUMMARY_QUERY

  def test_contains_hitl_events(self):
    assert "hitl_events" in SESSION_SUMMARY_QUERY

  def test_contains_time_to_first_token(self):
    assert "time_to_first_token_ms" in SESSION_SUMMARY_QUERY
