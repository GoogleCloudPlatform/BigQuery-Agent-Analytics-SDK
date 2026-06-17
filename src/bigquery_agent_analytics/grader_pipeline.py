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

"""Grader composition pipeline for combining multiple evaluators.

Composes ``SystemEvaluator``, ``LLMAsJudge``, and custom graders into a
single verdict using configurable scoring strategies (weighted average,
binary all-pass, or majority vote).

Example usage::

    from bigquery_agent_analytics import (
        SystemEvaluator, GraderPipeline, LLMAsJudge, WeightedStrategy,
    )

    pipeline = (
        GraderPipeline(WeightedStrategy(
            weights={"latency": 0.3, "correctness": 0.7},
        ))
        .add_system_grader(SystemEvaluator.latency(), weight=0.3)
        .add_llm_grader(LLMAsJudge.correctness(), weight=0.7)
    )

    verdict = await pipeline.evaluate(
        session_summary={"session_id": "s1", "avg_latency_ms": 2000},
        trace_text="User: hello\\nAgent: hi",
        final_response="hi",
    )
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Callable

from pydantic import BaseModel
from pydantic import Field

from .evaluators import LLMAsJudge
from .evaluators import SystemEvaluator

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Data Models                                                          #
# ------------------------------------------------------------------ #


class GraderResult(BaseModel):
  """Result from a single grader."""

  grader_name: str = Field(description="Name of the grader.")
  scores: dict[str, float] = Field(
      default_factory=dict,
      description="Metric scores from this grader.",
  )
  passed: bool = Field(
      default=True,
      description="Whether this grader passed.",
  )


class AggregateVerdict(BaseModel):
  """Aggregated verdict from all graders in the pipeline."""

  grader_results: list[GraderResult] = Field(
      default_factory=list,
      description="Individual grader results.",
  )
  final_score: float = Field(
      default=0.0,
      description="Final aggregated score.",
  )
  passed: bool = Field(
      default=False,
      description="Whether the overall evaluation passed.",
  )
  strategy_name: str = Field(
      default="",
      description="Name of the scoring strategy used.",
  )


# ------------------------------------------------------------------ #
# Scoring Strategies                                                   #
# ------------------------------------------------------------------ #


class ScoringStrategy(abc.ABC):
  """Abstract base class for scoring strategies."""

  @abc.abstractmethod
  def aggregate(
      self,
      grader_results: list[GraderResult],
  ) -> AggregateVerdict:
    """Aggregates grader results into a single verdict.

    Args:
        grader_results: List of individual grader results.

    Returns:
        AggregateVerdict with final score and pass/fail.
    """


class WeightedStrategy(ScoringStrategy):
  """Weighted average of grader scores; pass if >= threshold."""

  def __init__(
      self,
      weights: dict[str, float] | None = None,
      threshold: float = 0.5,
  ) -> None:
    """Initializes the weighted strategy.

    Args:
        weights: Mapping of grader name to weight. If None,
            all graders are weighted equally.
        threshold: Minimum weighted score to pass.
    """
    self.weights = weights or {}
    self.threshold = threshold

  def aggregate(
      self,
      grader_results: list[GraderResult],
  ) -> AggregateVerdict:
    if not grader_results:
      return AggregateVerdict(strategy_name="weighted")

    total_weight = 0.0
    weighted_sum = 0.0

    for result in grader_results:
      weight = self.weights.get(result.grader_name, 1.0)
      # Average the grader's metric scores
      if result.scores:
        avg_score = sum(result.scores.values()) / len(result.scores)
      else:
        avg_score = 1.0 if result.passed else 0.0
      weighted_sum += avg_score * weight
      total_weight += weight

    final_score = weighted_sum / total_weight if total_weight > 0 else 0.0

    return AggregateVerdict(
        grader_results=grader_results,
        final_score=final_score,
        passed=final_score >= self.threshold,
        strategy_name="weighted",
    )


class BinaryStrategy(ScoringStrategy):
  """All graders must pass independently."""

  def aggregate(
      self,
      grader_results: list[GraderResult],
  ) -> AggregateVerdict:
    if not grader_results:
      return AggregateVerdict(strategy_name="binary")

    all_passed = all(r.passed for r in grader_results)

    # Average of all scores
    all_scores = []
    for r in grader_results:
      all_scores.extend(r.scores.values())
    final_score = (
        sum(all_scores) / len(all_scores)
        if all_scores
        else (1.0 if all_passed else 0.0)
    )

    return AggregateVerdict(
        grader_results=grader_results,
        final_score=final_score,
        passed=all_passed,
        strategy_name="binary",
    )


class MajorityStrategy(ScoringStrategy):
  """Majority of graders must pass."""

  def aggregate(
      self,
      grader_results: list[GraderResult],
  ) -> AggregateVerdict:
    if not grader_results:
      return AggregateVerdict(strategy_name="majority")

    num_passed = sum(1 for r in grader_results if r.passed)
    majority = num_passed > len(grader_results) / 2

    # Average of all scores
    all_scores = []
    for r in grader_results:
      all_scores.extend(r.scores.values())
    final_score = (
        sum(all_scores) / len(all_scores)
        if all_scores
        else (1.0 if majority else 0.0)
    )

    return AggregateVerdict(
        grader_results=grader_results,
        final_score=final_score,
        passed=majority,
        strategy_name="majority",
    )


# ------------------------------------------------------------------ #
# Grader Pipeline                                                      #
# ------------------------------------------------------------------ #


class _GraderEntry:
  """Internal wrapper for a grader in the pipeline."""

  def __init__(
      self,
      name: str,
      evaluate_fn: Any,
      weight: float = 1.0,
      is_async: bool = False,
  ) -> None:
    self.name = name
    self.evaluate_fn = evaluate_fn
    self.weight = weight
    self.is_async = is_async


class GraderPipeline:
  """Composes multiple graders into a single evaluation pipeline.

  Supports ``SystemEvaluator``, ``LLMAsJudge``, and arbitrary custom
  grader functions combined via a configurable ``ScoringStrategy``.

  Example::

      pipeline = (
          GraderPipeline(WeightedStrategy(threshold=0.6))
          .add_system_grader(SystemEvaluator.latency())
          .add_llm_grader(LLMAsJudge.correctness())
      )
      verdict = await pipeline.evaluate(
          session_summary={...},
          trace_text="...",
          final_response="...",
      )
  """

  def __init__(self, strategy: ScoringStrategy) -> None:
    """Initializes the pipeline with a scoring strategy.

    Args:
        strategy: The strategy used to aggregate grader results.
    """
    self.strategy = strategy
    self._graders: list[_GraderEntry] = []

  def add_system_grader(
      self,
      evaluator: SystemEvaluator,
      weight: float = 1.0,
  ) -> GraderPipeline:
    """Adds a system-based grader to the pipeline.

    Args:
        evaluator: A SystemEvaluator instance.
        weight: Weight for weighted strategies.

    Returns:
        Self for chaining.
    """
    self._graders.append(
        _GraderEntry(
            name=evaluator.name,
            evaluate_fn=evaluator,
            weight=weight,
            is_async=False,
        )
    )
    return self

  def add_code_grader(
      self,
      evaluator: SystemEvaluator,
      weight: float = 1.0,
  ) -> GraderPipeline:
    """Adds a code grader to the pipeline.

    Note this grader is preserved for backwards compatibility, but isn't
    recommended for use.
    """
    return self.add_system_grader(evaluator, weight=weight)

  def add_llm_grader(
      self,
      judge: LLMAsJudge,
      weight: float = 1.0,
  ) -> GraderPipeline:
    """Adds an LLMAsJudge grader to the pipeline.

    Args:
        judge: An LLMAsJudge instance.
        weight: Weight for weighted strategies.

    Returns:
        Self for chaining.
    """
    self._graders.append(
        _GraderEntry(
            name=judge.name,
            evaluate_fn=judge,
            weight=weight,
            is_async=True,
        )
    )
    return self

  def add_custom_grader(
      self,
      name: str,
      fn: Callable[[dict[str, Any]], GraderResult],
      weight: float = 1.0,
  ) -> GraderPipeline:
    """Adds a custom grader function to the pipeline.

    The function receives a dict with ``session_summary``,
    ``trace_text``, and ``final_response`` keys.

    Args:
        name: Name for the grader.
        fn: Function returning a GraderResult.
        weight: Weight for weighted strategies.

    Returns:
        Self for chaining.
    """
    self._graders.append(
        _GraderEntry(
            name=name,
            evaluate_fn=fn,
            weight=weight,
            is_async=False,
        )
    )
    return self

  async def evaluate(
      self,
      session_summary: dict[str, Any] | None = None,
      trace_text: str = "",
      final_response: str = "",
  ) -> AggregateVerdict:
    """Evaluates using all graders and aggregates results.

    Args:
        session_summary: Dict with session metrics (for
            SystemEvaluator graders).
        trace_text: Formatted trace text (for LLMAsJudge
            graders).
        final_response: Final agent response.

    Returns:
        AggregateVerdict with combined results.
    """
    session_summary = session_summary or {}
    grader_results: list[GraderResult] = []

    for entry in self._graders:
      try:
        result = await self._run_grader(
            entry, session_summary, trace_text, final_response
        )
        grader_results.append(result)
      except Exception as e:
        logger.warning("Grader %s failed: %s", entry.name, e)
        grader_results.append(
            GraderResult(
                grader_name=entry.name,
                scores={},
                passed=False,
            )
        )

    return self.strategy.aggregate(grader_results)

  async def _run_grader(
      self,
      entry: _GraderEntry,
      session_summary: dict[str, Any],
      trace_text: str,
      final_response: str,
  ) -> GraderResult:
    """Runs a single grader and returns its result."""
    evaluator = entry.evaluate_fn

    if isinstance(evaluator, SystemEvaluator):
      score = evaluator.evaluate_session(session_summary)
      return GraderResult(
          grader_name=entry.name,
          scores=score.scores,
          passed=score.passed,
      )

    if isinstance(evaluator, LLMAsJudge):
      score = await evaluator.evaluate_session(
          trace_text=trace_text,
          final_response=final_response,
      )
      return GraderResult(
          grader_name=entry.name,
          scores=score.scores,
          passed=score.passed,
      )

    # Custom grader function
    context = {
        "session_summary": session_summary,
        "trace_text": trace_text,
        "final_response": final_response,
    }
    return evaluator(context)
