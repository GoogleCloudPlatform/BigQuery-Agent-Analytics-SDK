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

"""Multi-trial evaluation runner with pass@k / pass^k metrics.

Wraps any ``BigQueryTraceEvaluator`` to run N trials per task and
compute probabilistic pass-rate metrics that account for agent
non-determinism.

Example usage::

    from bigquery_agent_analytics import (
        BigQueryTraceEvaluator, TrialRunner,
    )

    evaluator = BigQueryTraceEvaluator(
        project_id="my-project",
        dataset_id="analytics",
    )
    runner = TrialRunner(evaluator, num_trials=5)

    report = await runner.run_trials(
        session_id="sess-123",
        golden_trajectory=[{"tool_name": "search", "args": {}}],
    )
    print(report.pass_at_k, report.pass_pow_k)
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
from typing import Any, Optional

from pydantic import BaseModel
from pydantic import Field

from .trace_evaluator import BigQueryTraceEvaluator
from .trace_evaluator import EvalStatus
from .trace_evaluator import MatchType

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Data Models                                                          #
# ------------------------------------------------------------------ #


class TrialResult(BaseModel):
  """Result of a single trial."""

  trial_index: int = Field(description="Zero-based trial index.")
  passed: bool = Field(description="Whether this trial passed.")
  scores: dict[str, float] = Field(
      default_factory=dict,
      description="Metric scores for this trial.",
  )
  details: dict[str, Any] = Field(
      default_factory=dict,
      description="Additional trial details.",
  )


class MultiTrialReport(BaseModel):
  """Aggregate report across N trials of one task."""

  session_id: str = Field(description="The session ID evaluated.")
  num_trials: int = Field(description="Number of trials run.")
  trial_results: list[TrialResult] = Field(
      default_factory=list,
      description="Individual trial results.",
  )
  pass_at_k: float = Field(
      default=0.0,
      description="P(>=1 pass in k trials).",
  )
  pass_pow_k: float = Field(
      default=0.0,
      description="P(all k trials pass).",
  )
  per_trial_pass_rate: float = Field(
      default=0.0,
      description="Fraction of trials that passed.",
  )
  mean_scores: dict[str, float] = Field(
      default_factory=dict,
      description="Mean score per metric across trials.",
  )
  score_std_dev: dict[str, float] = Field(
      default_factory=dict,
      description="Standard deviation per metric across trials.",
  )


# ------------------------------------------------------------------ #
# Static Helpers                                                       #
# ------------------------------------------------------------------ #


def compute_pass_at_k(
    num_trials: int,
    num_passed: int,
) -> float:
  """Computes pass@k: P(>=1 pass in k trials).

  Uses the formula: 1 - C(n-c, k) / C(n, k)
  where n = num_trials, c = num_passed, k = num_trials.

  Args:
      num_trials: Total number of trials (k).
      num_passed: Number of trials that passed (c).

  Returns:
      Probability that at least one trial passes.
  """
  if num_trials <= 0:
    return 0.0
  if num_passed <= 0:
    return 0.0
  if num_passed >= num_trials:
    return 1.0

  # 1 - C(n-c, k) / C(n, k)
  n = num_trials
  c = num_passed
  k = num_trials

  # C(n-c, k) / C(n, k) -- if n-c < k then C(n-c,k)=0 => pass@k=1
  if n - c < k:
    return 1.0

  # Use log to avoid overflow for large values
  log_numerator = sum(math.log(n - c - i) for i in range(k))
  log_denominator = sum(math.log(n - i) for i in range(k))

  return 1.0 - math.exp(log_numerator - log_denominator)


def compute_pass_pow_k(
    num_trials: int,
    num_passed: int,
) -> float:
  """Computes pass^k: P(all k trials pass).

  Uses the formula: (num_passed / num_trials) ** num_trials.

  Args:
      num_trials: Total number of trials.
      num_passed: Number of trials that passed.

  Returns:
      Probability that all trials pass.
  """
  if num_trials <= 0:
    return 0.0
  if num_passed <= 0:
    return 0.0
  rate = num_passed / num_trials
  return rate**num_trials


# ------------------------------------------------------------------ #
# TrialRunner                                                          #
# ------------------------------------------------------------------ #


class TrialRunner:
  """Runs multiple evaluation trials and computes aggregate metrics.

  Wraps a ``BigQueryTraceEvaluator`` and runs N trials per task,
  computing pass@k and pass^k metrics that account for agent
  non-determinism (e.g. LLM judges produce different scores each
  call).

  Example::

      runner = TrialRunner(evaluator, num_trials=5, concurrency=3)
      report = await runner.run_trials(
          session_id="sess-123",
          golden_trajectory=[...],
      )
  """

  def __init__(
      self,
      evaluator: BigQueryTraceEvaluator,
      num_trials: int = 5,
      concurrency: int = 3,
  ) -> None:
    """Initializes the TrialRunner.

    Args:
        evaluator: The trace evaluator to wrap.
        num_trials: Number of trials to run per task.
        concurrency: Maximum concurrent evaluations.
    """
    self.evaluator = evaluator
    self.num_trials = num_trials
    self.concurrency = concurrency

  async def run_trials(
      self,
      session_id: str,
      golden_trajectory: Optional[list[dict]] = None,
      golden_response: Optional[str] = None,
      match_type: MatchType = MatchType.EXACT,
      task_description: Optional[str] = None,
      use_llm_judge: bool = False,
      thresholds: Optional[dict[str, float]] = None,
  ) -> MultiTrialReport:
    """Runs N trials of evaluation for a single session.

    Args:
        session_id: The session ID to evaluate.
        golden_trajectory: Expected tool call sequence.
        golden_response: Expected final response.
        match_type: Type of trajectory matching.
        task_description: Task description for LLM judge.
        use_llm_judge: Whether to use LLM-as-judge.
        thresholds: Metric thresholds for pass/fail.

    Returns:
        MultiTrialReport with aggregate metrics.
    """
    semaphore = asyncio.Semaphore(self.concurrency)
    trial_results: list[TrialResult] = []

    async def _run_one(trial_index: int) -> TrialResult:
      async with semaphore:
        result = await self.evaluator.evaluate_session(
            session_id=session_id,
            golden_trajectory=golden_trajectory,
            golden_response=golden_response,
            match_type=match_type,
            task_description=task_description,
            use_llm_judge=use_llm_judge,
            thresholds=thresholds,
        )
        return TrialResult(
            trial_index=trial_index,
            passed=result.eval_status == EvalStatus.PASSED,
            scores=result.scores,
            details=result.details,
        )

    tasks = [_run_one(i) for i in range(self.num_trials)]
    trial_results = list(await asyncio.gather(*tasks))

    return self._build_report(session_id, trial_results)

  async def run_trials_batch(
      self,
      eval_dataset: list[dict[str, Any]],
      match_type: MatchType = MatchType.EXACT,
      use_llm_judge: bool = False,
  ) -> list[MultiTrialReport]:
    """Runs multi-trial evaluation for a batch of tasks.

    Args:
        eval_dataset: List of dicts with session_id,
            expected_trajectory, etc.
        match_type: Type of trajectory matching.
        use_llm_judge: Whether to use LLM-as-judge.

    Returns:
        List of MultiTrialReport, one per task.
    """
    reports = []
    for item in eval_dataset:
      report = await self.run_trials(
          session_id=item["session_id"],
          golden_trajectory=item.get("expected_trajectory"),
          golden_response=item.get("expected_response"),
          match_type=match_type,
          task_description=item.get("task_description"),
          use_llm_judge=use_llm_judge,
          thresholds=item.get("thresholds"),
      )
      reports.append(report)
    return reports

  def _build_report(
      self,
      session_id: str,
      trial_results: list[TrialResult],
  ) -> MultiTrialReport:
    """Builds a MultiTrialReport from trial results."""
    num_trials = len(trial_results)
    if num_trials == 0:
      return MultiTrialReport(
          session_id=session_id,
          num_trials=0,
      )

    num_passed = sum(1 for t in trial_results if t.passed)

    # Aggregate scores
    all_metric_names: set[str] = set()
    for t in trial_results:
      all_metric_names.update(t.scores.keys())

    mean_scores: dict[str, float] = {}
    score_std_dev: dict[str, float] = {}

    for metric in sorted(all_metric_names):
      values = [t.scores.get(metric, 0.0) for t in trial_results]
      mean_scores[metric] = statistics.mean(values)
      if len(values) >= 2:
        score_std_dev[metric] = statistics.stdev(values)
      else:
        score_std_dev[metric] = 0.0

    return MultiTrialReport(
        session_id=session_id,
        num_trials=num_trials,
        trial_results=trial_results,
        pass_at_k=compute_pass_at_k(num_trials, num_passed),
        pass_pow_k=compute_pass_pow_k(num_trials, num_passed),
        per_trial_pass_rate=num_passed / num_trials,
        mean_scores=mean_scores,
        score_std_dev=score_std_dev,
    )
