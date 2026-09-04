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

"""Tests for the grader_pipeline module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.aggregate_grader import AggregateGrader
from bigquery_agent_analytics.aggregate_grader import AggregateVerdict
from bigquery_agent_analytics.aggregate_grader import BinaryStrategy
from bigquery_agent_analytics.aggregate_grader import GraderPipeline
from bigquery_agent_analytics.aggregate_grader import GraderResult
from bigquery_agent_analytics.aggregate_grader import MajorityStrategy
from bigquery_agent_analytics.aggregate_grader import WeightedStrategy
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.evaluators import SystemEvaluator
from bigquery_agent_analytics.performance_evaluator import EvalStatus
from bigquery_agent_analytics.performance_evaluator import EvaluationResult
from bigquery_agent_analytics.performance_evaluator import PerformanceEvaluator
from bigquery_agent_analytics.performance_evaluator import SessionTrace

# ------------------------------------------------------------------ #
# Tests for WeightedStrategy                                           #
# ------------------------------------------------------------------ #


class TestWeightedStrategy:
  """Tests for WeightedStrategy."""

  def test_equal_weights(self):
    strategy = WeightedStrategy(threshold=0.5)
    results = [
        GraderResult(grader_name="a", scores={"m": 0.8}, passed=True),
        GraderResult(grader_name="b", scores={"m": 0.6}, passed=True),
    ]
    verdict = strategy.aggregate(results)
    # (0.8 + 0.6) / 2 = 0.7
    assert verdict.final_score == pytest.approx(0.7)
    assert verdict.passed is True

  def test_custom_weights(self):
    strategy = WeightedStrategy(
        weights={"a": 3.0, "b": 1.0},
        threshold=0.5,
    )
    results = [
        GraderResult(grader_name="a", scores={"m": 0.9}, passed=True),
        GraderResult(grader_name="b", scores={"m": 0.1}, passed=False),
    ]
    verdict = strategy.aggregate(results)
    # (0.9*3 + 0.1*1) / (3+1) = 2.8/4 = 0.7
    assert verdict.final_score == pytest.approx(0.7)
    assert verdict.passed is True

  def test_below_threshold(self):
    strategy = WeightedStrategy(threshold=0.8)
    results = [
        GraderResult(grader_name="a", scores={"m": 0.5}, passed=True),
    ]
    verdict = strategy.aggregate(results)
    assert verdict.passed is False

  def test_empty_results(self):
    strategy = WeightedStrategy()
    verdict = strategy.aggregate([])
    assert verdict.strategy_name == "weighted"
    assert verdict.grader_results == []


# ------------------------------------------------------------------ #
# Tests for BinaryStrategy                                             #
# ------------------------------------------------------------------ #


class TestBinaryStrategy:
  """Tests for BinaryStrategy."""

  def test_all_pass(self):
    strategy = BinaryStrategy()
    results = [
        GraderResult(grader_name="a", scores={"m": 0.9}, passed=True),
        GraderResult(grader_name="b", scores={"m": 0.8}, passed=True),
    ]
    verdict = strategy.aggregate(results)
    assert verdict.passed is True

  def test_one_fail(self):
    strategy = BinaryStrategy()
    results = [
        GraderResult(grader_name="a", scores={"m": 0.9}, passed=True),
        GraderResult(grader_name="b", scores={"m": 0.2}, passed=False),
    ]
    verdict = strategy.aggregate(results)
    assert verdict.passed is False

  def test_empty_results(self):
    strategy = BinaryStrategy()
    verdict = strategy.aggregate([])
    assert verdict.strategy_name == "binary"


# ------------------------------------------------------------------ #
# Tests for MajorityStrategy                                           #
# ------------------------------------------------------------------ #


class TestMajorityStrategy:
  """Tests for MajorityStrategy."""

  def test_majority_pass(self):
    strategy = MajorityStrategy()
    results = [
        GraderResult(grader_name="a", scores={"m": 0.9}, passed=True),
        GraderResult(grader_name="b", scores={"m": 0.8}, passed=True),
        GraderResult(grader_name="c", scores={"m": 0.2}, passed=False),
    ]
    verdict = strategy.aggregate(results)
    assert verdict.passed is True

  def test_majority_fail(self):
    strategy = MajorityStrategy()
    results = [
        GraderResult(grader_name="a", scores={"m": 0.9}, passed=True),
        GraderResult(grader_name="b", scores={"m": 0.2}, passed=False),
        GraderResult(grader_name="c", scores={"m": 0.1}, passed=False),
    ]
    verdict = strategy.aggregate(results)
    assert verdict.passed is False

  def test_tie(self):
    """With 2 graders, 1 pass 1 fail => not majority."""
    strategy = MajorityStrategy()
    results = [
        GraderResult(grader_name="a", scores={"m": 0.9}, passed=True),
        GraderResult(grader_name="b", scores={"m": 0.2}, passed=False),
    ]
    verdict = strategy.aggregate(results)
    # 1 > 2/2 = 1 is False (not strictly greater)
    assert verdict.passed is False

  def test_empty_results(self):
    strategy = MajorityStrategy()
    verdict = strategy.aggregate([])
    assert verdict.strategy_name == "majority"


class TestAggregateVerdict:
  """Tests for AggregateVerdict data model."""

  def test_verdict_properties(self):
    results = [
        GraderResult(
            grader_name="latency", scores={"latency": 0.9}, passed=True
        ),
        GraderResult(
            grader_name="correctness", scores={"correctness": 0.8}, passed=True
        ),
    ]
    verdict = AggregateVerdict(
        passed=True,
        final_score=0.85,
        grader_results=results,
        strategy_name="weighted",
    )

    assert verdict.passed is True
    assert verdict.final_score == 0.85
    assert len(verdict.grader_results) == 2
    assert verdict.strategy_name == "weighted"
    assert verdict.grader_results[0].grader_name == "latency"
    assert verdict.grader_results[1].grader_name == "correctness"


# ------------------------------------------------------------------ #
# Tests for GraderPipeline                                             #
# ------------------------------------------------------------------ #


class TestAggregateGrader:
  """Tests for AggregateGrader."""

  @pytest.mark.asyncio
  async def test_system_grader(self):
    """Test pipeline with a system grader."""
    pipeline = AggregateGrader(
        WeightedStrategy(threshold=0.5)
    ).add_system_grader(SystemEvaluator.latency(threshold_ms=5000))

    verdict = await pipeline.evaluate(
        session_summary={
            "session_id": "s1",
            "avg_latency_ms": 2000,
        }
    )

    assert verdict.passed is True
    assert len(verdict.grader_results) == 1
    assert verdict.grader_results[0].grader_name == "latency_evaluator"

  @pytest.mark.asyncio
  async def test_performance_evaluator_mocked(self):
    """Test pipeline with a mocked PerformanceEvaluator."""
    evaluator = PerformanceEvaluator(name="mock_evaluator")
    evaluator.evaluate_session = AsyncMock(
        return_value=EvaluationResult(
            session_id="s1",
            scores={"correctness": 0.8},
            eval_status=EvalStatus.PASSED,
        )
    )

    pipeline = AggregateGrader(
        WeightedStrategy(threshold=0.5)
    ).add_performance_grader(evaluator)

    verdict = await pipeline.evaluate(
        session_id="s1",
        trace_text="User: hi",
        final_response="hello",
    )

    assert verdict.passed is True
    assert len(verdict.grader_results) == 1

  @pytest.mark.asyncio
  async def test_custom_grader(self):
    """Test pipeline with a custom grader."""

    def my_grader(ctx):
      return GraderResult(
          grader_name="custom",
          scores={"quality": 0.7},
          passed=True,
      )

    pipeline = AggregateGrader(
        WeightedStrategy(threshold=0.5)
    ).add_custom_grader("custom", my_grader)

    verdict = await pipeline.evaluate()

    assert verdict.passed is True
    assert verdict.grader_results[0].grader_name == "custom"

  @pytest.mark.asyncio
  async def test_mixed_graders(self):
    """Test pipeline with system and performance graders."""
    evaluator = PerformanceEvaluator(name="mock_evaluator")
    evaluator.evaluate_session = AsyncMock(
        return_value=EvaluationResult(
            session_id="s1",
            scores={"correctness": 0.9},
            eval_status=EvalStatus.PASSED,
        )
    )

    pipeline = (
        AggregateGrader(BinaryStrategy())
        .add_system_grader(SystemEvaluator.latency(threshold_ms=5000))
        .add_performance_grader(evaluator)
    )

    verdict = await pipeline.evaluate(
        session_summary={
            "session_id": "s1",
            "avg_latency_ms": 2000,
        },
        trace_text="User: hi",
        final_response="hello",
    )

    assert verdict.passed is True
    assert len(verdict.grader_results) == 2

  @pytest.mark.asyncio
  async def test_performance_evaluator_real_fake(self):
    """Test pipeline with a fake PerformanceEvaluator overriding get_session_trace."""
    from bigquery_agent_analytics.performance_evaluator import ToolCall

    trace = SessionTrace(
        session_id="s1",
        user_id="u1",
        events=[],
        tool_calls=[
            ToolCall(tool_name="search", args={"q": "weather"}),
        ],
        final_response="The weather is sunny.",
    )

    class FakePerformanceEvaluator(PerformanceEvaluator):

      async def get_session_trace(self, session_id: str) -> SessionTrace:
        return trace

    evaluator = FakePerformanceEvaluator(
        project_id="p",
        dataset_id="d",
        name="fake_evaluator",
    )

    pipeline = AggregateGrader(
        WeightedStrategy(threshold=0.5)
    ).add_performance_grader(evaluator)

    verdict = await pipeline.evaluate(
        session_id="s1",
        final_response="The weather is sunny.",
    )

    assert verdict.passed is True
    assert len(verdict.grader_results) == 1
    assert verdict.grader_results[0].passed is True

  @pytest.mark.asyncio
  async def test_chaining_api(self):
    """Test fluent builder chaining."""
    pipeline = (
        AggregateGrader(WeightedStrategy())
        .add_system_grader(SystemEvaluator.latency())
        .add_system_grader(SystemEvaluator.error_rate())
    )
    # Verify chaining works
    assert len(pipeline._graders) == 2

  @pytest.mark.asyncio
  async def test_grader_exception_handled(self):
    """Test that grader exceptions produce a failed result."""

    def bad_grader(ctx):
      raise ValueError("boom")

    pipeline = AggregateGrader(
        WeightedStrategy(threshold=0.5)
    ).add_custom_grader("bad", bad_grader)

    verdict = await pipeline.evaluate()

    assert verdict.passed is False
    assert verdict.grader_results[0].passed is False

  @pytest.mark.asyncio
  async def test_code_grader_legacy_alias(self):
    """Test that the legacy add_code_grader alias still works."""
    pipeline = GraderPipeline(WeightedStrategy(threshold=0.5)).add_code_grader(
        SystemEvaluator.latency(threshold_ms=5000)
    )
    verdict = await pipeline.evaluate(
        session_summary={
            "session_id": "s1",
            "avg_latency_ms": 2000,
        }
    )
    assert verdict.passed is True
    assert len(verdict.grader_results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy", [WeightedStrategy(), BinaryStrategy(), MajorityStrategy()]
)
@pytest.mark.parametrize(
    "scores, status, details",
    [
        ({}, EvalStatus.PASSED, {}),
        ({"quality": float("nan")}, EvalStatus.PASSED, {}),
        ({"quality": float("inf")}, EvalStatus.PASSED, {}),
        ({"quality": 1.0}, EvalStatus.NOT_EVALUATED, {}),
        (
            {"response_match": 1.0},
            EvalStatus.FAILED,
            {"errors": ["judge failed"]},
        ),
    ],
)
async def test_performance_grader_requires_valid_measured_evidence(
    strategy, scores, status, details
):
  evaluator = PerformanceEvaluator()
  evaluator.evaluate_session = AsyncMock(
      return_value=EvaluationResult(
          session_id="s1",
          scores=scores,
          eval_status=status,
          details=details,
      )
  )
  verdict = (
      await AggregateGrader(strategy)
      .add_performance_grader(evaluator)
      .evaluate(session_id="s1", final_response="expected")
  )
  assert verdict.passed is False
  assert verdict.final_score == 0.0
  assert verdict.grader_results[0].passed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actual, expected, passed",
    [("wrong", "right", False), ("right", "right", True)],
)
async def test_performance_grader_uses_real_golden_response_metric(
    actual, expected, passed
):
  evaluator = PerformanceEvaluator()
  evaluator.get_session_trace = AsyncMock(
      return_value=SessionTrace(
          session_id="s1",
          user_id=None,
          events=[],
          final_response=actual,
      )
  )
  verdict = (
      await AggregateGrader(WeightedStrategy())
      .add_performance_grader(evaluator)
      .evaluate(session_id="s1", final_response=expected)
  )
  assert verdict.passed is passed
  assert verdict.final_score == (1.0 if passed else 0.0)
  assert verdict.grader_results[0].scores == {
      "response_match": verdict.final_score
  }


@pytest.mark.asyncio
async def test_configured_performance_name_selects_its_aggregate_weight():
  evaluator = PerformanceEvaluator(name="quality")
  evaluator.get_session_trace = AsyncMock(
      return_value=SessionTrace(
          session_id="s1", user_id=None, events=[], final_response="wrong"
      )
  )
  pipeline = (
      AggregateGrader(
          WeightedStrategy(
              weights={"quality": 9.0, "other": 1.0}, threshold=0.4
          )
      )
      .add_performance_grader(evaluator)
      .add_custom_grader(
          "other",
          lambda _: GraderResult(
              grader_name="other", scores={"other": 1.0}, passed=True
          ),
      )
  )
  verdict = await pipeline.evaluate(session_id="s1", final_response="right")
  assert [r.grader_name for r in verdict.grader_results] == ["quality", "other"]
  assert verdict.grader_results[0].scores == {"response_match": 0.0}
  assert verdict.final_score == pytest.approx(0.1)
  assert verdict.passed is False
  assert PerformanceEvaluator().name == "performance_evaluator"
