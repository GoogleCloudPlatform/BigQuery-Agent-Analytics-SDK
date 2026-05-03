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

"""Tests for the multi_trial module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.multi_trial import compute_pass_at_k
from bigquery_agent_analytics.multi_trial import compute_pass_pow_k
from bigquery_agent_analytics.multi_trial import MultiTrialReport
from bigquery_agent_analytics.multi_trial import TrialResult
from bigquery_agent_analytics.multi_trial import TrialRunner
from bigquery_agent_analytics.trace_evaluator import BigQueryTraceEvaluator
from bigquery_agent_analytics.trace_evaluator import EvalStatus
from bigquery_agent_analytics.trace_evaluator import EvaluationResult
from bigquery_agent_analytics.trace_evaluator import MatchType

# ------------------------------------------------------------------ #
# Tests for compute_pass_at_k                                          #
# ------------------------------------------------------------------ #


class TestComputePassAtK:
  """Tests for compute_pass_at_k."""

  def test_all_pass(self):
    assert compute_pass_at_k(5, 5) == 1.0

  def test_none_pass(self):
    assert compute_pass_at_k(5, 0) == 0.0

  def test_some_pass(self):
    # P(>=1 pass in 3 trials) with 2 of 3 passing
    result = compute_pass_at_k(3, 2)
    assert 0.0 < result <= 1.0

  def test_one_of_many(self):
    result = compute_pass_at_k(10, 1)
    assert 0.0 < result <= 1.0

  def test_zero_trials(self):
    assert compute_pass_at_k(0, 0) == 0.0

  def test_more_passed_than_trials(self):
    # Edge case: should cap at 1.0
    assert compute_pass_at_k(3, 5) == 1.0


# ------------------------------------------------------------------ #
# Tests for compute_pass_pow_k                                         #
# ------------------------------------------------------------------ #


class TestComputePassPowK:
  """Tests for compute_pass_pow_k."""

  def test_all_pass(self):
    assert compute_pass_pow_k(5, 5) == 1.0

  def test_none_pass(self):
    assert compute_pass_pow_k(5, 0) == 0.0

  def test_some_pass(self):
    # (2/3)^3 ≈ 0.296
    result = compute_pass_pow_k(3, 2)
    assert abs(result - (2 / 3) ** 3) < 1e-9

  def test_half_pass(self):
    # (1/2)^2 = 0.25
    result = compute_pass_pow_k(2, 1)
    assert abs(result - 0.25) < 1e-9

  def test_zero_trials(self):
    assert compute_pass_pow_k(0, 0) == 0.0


# ------------------------------------------------------------------ #
# Tests for TrialResult model                                          #
# ------------------------------------------------------------------ #


class TestTrialResult:
  """Tests for TrialResult data model."""

  def test_basic(self):
    result = TrialResult(
        trial_index=0,
        passed=True,
        scores={"accuracy": 0.9},
    )
    assert result.trial_index == 0
    assert result.passed is True
    assert result.scores["accuracy"] == 0.9

  def test_defaults(self):
    result = TrialResult(trial_index=1, passed=False)
    assert result.scores == {}
    assert result.details == {}


# ------------------------------------------------------------------ #
# Tests for MultiTrialReport model                                     #
# ------------------------------------------------------------------ #


class TestMultiTrialReport:
  """Tests for MultiTrialReport data model."""

  def test_basic(self):
    report = MultiTrialReport(
        session_id="s1",
        num_trials=3,
        pass_at_k=0.9,
        pass_pow_k=0.5,
        per_trial_pass_rate=0.67,
    )
    assert report.session_id == "s1"
    assert report.num_trials == 3

  def test_defaults(self):
    report = MultiTrialReport(
        session_id="s1",
        num_trials=0,
    )
    assert report.trial_results == []
    assert report.pass_at_k == 0.0
    assert report.mean_scores == {}


# ------------------------------------------------------------------ #
# Tests for TrialRunner                                                #
# ------------------------------------------------------------------ #


class TestTrialRunner:
  """Tests for TrialRunner class."""

  def _make_evaluator(self, results):
    """Creates a mock evaluator returning given results."""
    evaluator = MagicMock(spec=BigQueryTraceEvaluator)
    evaluator.evaluate_session = AsyncMock(side_effect=results)
    return evaluator

  @pytest.mark.asyncio
  async def test_run_trials_mixed(self):
    """Test 3 trials with 2 pass, 1 fail."""
    results = [
        EvaluationResult(
            session_id="s1",
            eval_status=EvalStatus.PASSED,
            scores={"accuracy": 0.9},
        ),
        EvaluationResult(
            session_id="s1",
            eval_status=EvalStatus.FAILED,
            scores={"accuracy": 0.3},
        ),
        EvaluationResult(
            session_id="s1",
            eval_status=EvalStatus.PASSED,
            scores={"accuracy": 0.8},
        ),
    ]
    evaluator = self._make_evaluator(results)
    runner = TrialRunner(evaluator, num_trials=3, concurrency=1)

    report = await runner.run_trials(session_id="s1")

    assert report.session_id == "s1"
    assert report.num_trials == 3
    assert len(report.trial_results) == 3
    assert report.per_trial_pass_rate == pytest.approx(2 / 3)
    assert report.pass_at_k > 0.0
    assert report.pass_pow_k > 0.0
    assert "accuracy" in report.mean_scores
    assert "accuracy" in report.score_std_dev

  @pytest.mark.asyncio
  async def test_run_trials_all_pass(self):
    """Test all trials pass."""
    results = [
        EvaluationResult(
            session_id="s1",
            eval_status=EvalStatus.PASSED,
            scores={"metric": 1.0},
        )
        for _ in range(3)
    ]
    evaluator = self._make_evaluator(results)
    runner = TrialRunner(evaluator, num_trials=3, concurrency=3)

    report = await runner.run_trials(session_id="s1")

    assert report.per_trial_pass_rate == 1.0
    assert report.pass_at_k == 1.0
    assert report.pass_pow_k == 1.0

  @pytest.mark.asyncio
  async def test_run_trials_all_fail(self):
    """Test all trials fail."""
    results = [
        EvaluationResult(
            session_id="s1",
            eval_status=EvalStatus.FAILED,
            scores={"metric": 0.1},
        )
        for _ in range(3)
    ]
    evaluator = self._make_evaluator(results)
    runner = TrialRunner(evaluator, num_trials=3, concurrency=3)

    report = await runner.run_trials(session_id="s1")

    assert report.per_trial_pass_rate == 0.0
    assert report.pass_at_k == 0.0
    assert report.pass_pow_k == 0.0

  @pytest.mark.asyncio
  async def test_run_trials_batch(self):
    """Test batch evaluation with 2 tasks."""
    results = [
        # Task 1 trials
        EvaluationResult(
            session_id="s1",
            eval_status=EvalStatus.PASSED,
            scores={"m": 0.9},
        ),
        EvaluationResult(
            session_id="s1",
            eval_status=EvalStatus.PASSED,
            scores={"m": 0.8},
        ),
        # Task 2 trials
        EvaluationResult(
            session_id="s2",
            eval_status=EvalStatus.FAILED,
            scores={"m": 0.2},
        ),
        EvaluationResult(
            session_id="s2",
            eval_status=EvalStatus.FAILED,
            scores={"m": 0.1},
        ),
    ]
    evaluator = self._make_evaluator(results)
    runner = TrialRunner(evaluator, num_trials=2, concurrency=1)

    dataset = [
        {"session_id": "s1"},
        {"session_id": "s2"},
    ]
    reports = await runner.run_trials_batch(dataset)

    assert len(reports) == 2
    assert reports[0].session_id == "s1"
    assert reports[0].per_trial_pass_rate == 1.0
    assert reports[1].session_id == "s2"
    assert reports[1].per_trial_pass_rate == 0.0

  @pytest.mark.asyncio
  async def test_run_trials_zero_results(self):
    """Test edge case with 0 trials."""
    evaluator = MagicMock(spec=BigQueryTraceEvaluator)
    runner = TrialRunner(evaluator, num_trials=0, concurrency=1)

    report = await runner.run_trials(session_id="s1")

    assert report.num_trials == 0
    assert report.trial_results == []
