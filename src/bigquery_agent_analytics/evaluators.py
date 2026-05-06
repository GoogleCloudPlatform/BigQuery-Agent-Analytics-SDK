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

"""Evaluation engine for BigQuery Agent Analytics SDK.

Provides ``CodeEvaluator`` for deterministic, code-based metrics and
``LLMAsJudge`` for semantic evaluation using LLM-as-a-judge. The
``evaluate()`` function orchestrates batch evaluation using BigQuery's
native AI functions for scalable, zero-ETL assessment.

Example usage::

    from bigquery_agent_analytics.evaluators import (
        CodeEvaluator, LLMAsJudge,
    )

    # Deterministic evaluation
    evaluator = CodeEvaluator.latency(threshold_ms=5000)

    # LLM-based semantic evaluation
    judge = LLMAsJudge.correctness()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
import json
import logging
import re
from typing import Any, Callable, Optional

from pydantic import BaseModel
from pydantic import Field

from bigquery_agent_analytics import udf_kernels

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

DEFAULT_ENDPOINT = "gemini-2.5-flash"


# ------------------------------------------------------------------ #
# Evaluation Report                                                    #
# ------------------------------------------------------------------ #


class SessionScore(BaseModel):
  """Scores for a single evaluated session."""

  session_id: str = Field(description="The session ID evaluated.")
  scores: dict[str, float] = Field(
      default_factory=dict,
      description="Metric name to score (0.0 - 1.0).",
  )
  passed: bool = Field(
      default=True,
      description="Whether the session passed all thresholds.",
  )
  details: dict[str, Any] = Field(
      default_factory=dict,
      description="Additional per-session details.",
  )
  llm_feedback: Optional[str] = Field(
      default=None,
      description="LLM judge feedback if applicable.",
  )


class EvaluationReport(BaseModel):
  """Aggregate report from an evaluation run."""

  dataset: str = Field(description="Dataset or filter description.")
  evaluator_name: str = Field(description="Name of evaluator used.")
  total_sessions: int = Field(default=0)
  passed_sessions: int = Field(default=0)
  failed_sessions: int = Field(default=0)
  aggregate_scores: dict[str, float] = Field(
      default_factory=dict,
      description="Average scores across all sessions.",
  )
  details: dict[str, Any] = Field(
      default_factory=dict,
      description=(
          "Operational metadata (parse_errors, fallback_mode, etc.)."
          " Separated from aggregate_scores so downstream consumers"
          " can treat scores as purely normalized metrics."
      ),
  )
  session_scores: list[SessionScore] = Field(
      default_factory=list,
  )
  created_at: datetime = Field(
      default_factory=lambda: datetime.now(timezone.utc),
  )

  @property
  def pass_rate(self) -> float:
    """Fraction of sessions that passed."""
    if self.total_sessions == 0:
      return 0.0
    return self.passed_sessions / self.total_sessions

  def summary(self) -> str:
    """Returns a human-readable summary."""
    lines = [
        f"Evaluation Report: {self.evaluator_name}",
        f"  Dataset: {self.dataset}",
        f"  Sessions: {self.total_sessions}",
        f"  Passed: {self.passed_sessions} ({self.pass_rate:.0%})",
        f"  Failed: {self.failed_sessions}",
    ]
    if self.aggregate_scores:
      lines.append("  Aggregate Scores:")
      for name, score in sorted(self.aggregate_scores.items()):
        lines.append(f"    {name}: {score:.3f}")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Code-Based Evaluator                                                 #
# ------------------------------------------------------------------ #


@dataclass
class _MetricDef:
  """Internal definition of a code metric.

  ``observed_key``, ``observed_fn``, ``detail_fn``, and ``budget`` are optional
  reporting metadata used by the prebuilt evaluators (latency,
  error_rate, turn_count, …) to surface the raw observed value and
  the user-supplied budget in ``SessionScore.details``. They don't
  affect pass/fail computation — that still goes through ``fn`` +
  ``threshold`` — but they let downstream consumers (CLI
  ``--exit-code`` output, dashboards) emit readable failure lines
  without having to re-run the scorer.

  When ``observed_fn`` is set it takes precedence over
  ``observed_key``; use it for metrics whose observed value is
  computed from multiple summary fields (e.g. ``tool_errors /
  tool_calls`` for error rate).

  When ``detail_fn`` is set, its returned key/value pairs are merged
  into the metric's detail payload after the common observed/budget/
  threshold/score/passed fields are populated.
  """

  name: str
  fn: Callable[[dict[str, Any]], float]
  threshold: float = 0.5
  observed_key: Optional[str] = None
  budget: Optional[float] = None
  observed_fn: Optional[Callable[[dict[str, Any]], Any]] = None
  detail_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None


class CodeEvaluator:
  """Deterministic evaluator using code-based metric functions.

  Metrics operate on a session summary dict containing::

      {
        "session_id": str,
        "total_events": int,
        "tool_calls": int,
        "tool_errors": int,
        "llm_calls": int,
        "avg_latency_ms": float,
        "max_latency_ms": float,
        "total_latency_ms": float,
        "turn_count": int,
        "has_error": bool,
      }

  Each metric function returns a score between 0.0 and 1.0.
  """

  def __init__(
      self,
      name: str = "code_evaluator",
      metrics: Optional[list[_MetricDef]] = None,
  ) -> None:
    self.name = name
    self._metrics: list[_MetricDef] = metrics or []

  def add_metric(
      self,
      name: str,
      fn: Callable[[dict[str, Any]], float],
      threshold: float = 0.5,
      observed_key: Optional[str] = None,
      budget: Optional[float] = None,
      observed_fn: Optional[Callable[[dict[str, Any]], Any]] = None,
      detail_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
  ) -> CodeEvaluator:
    """Adds a custom metric function.

    Args:
        name: Metric name.
        fn: Function taking session summary, returning 0-1 score.
            The score is compared to ``threshold``; a session passes
            the metric when ``score >= threshold``.
        threshold: Pass/fail threshold applied to ``fn``'s score.
        observed_key: Optional session-summary key whose value is the
            raw observed metric (e.g. ``"avg_latency_ms"``). When set,
            ``evaluate_session`` stashes the observed value + ``budget``
            under ``SessionScore.details`` for downstream reporting.
        budget: Optional raw-budget value corresponding to the metric
            (e.g. the latency-ms threshold the user supplied). Reported
            alongside ``observed_key``; not used for pass/fail.
        observed_fn: Optional callable that derives the observed value
            from the session summary. Used when the observed metric is
            computed (e.g. ``tool_errors/tool_calls``) rather than
            stored directly. Takes precedence over ``observed_key``.
        detail_fn: Optional callable that derives additional
            JSON-serializable detail fields from the session summary.

    Returns:
        Self for chaining.
    """
    self._metrics.append(
        _MetricDef(
            name=name,
            fn=fn,
            threshold=threshold,
            observed_key=observed_key,
            budget=budget,
            observed_fn=observed_fn,
            detail_fn=detail_fn,
        )
    )
    return self

  def evaluate_session(self, session_summary: dict[str, Any]) -> SessionScore:
    """Evaluates a single session summary.

    Args:
        session_summary: Dict with session metrics.

    Returns:
        SessionScore with computed scores.
    """
    scores: dict[str, float] = {}
    details: dict[str, Any] = {}
    passed = True

    for metric in self._metrics:
      try:
        score = metric.fn(session_summary)
        score = max(0.0, min(1.0, float(score)))
        scores[metric.name] = score
        metric_passed = score >= metric.threshold
        if not metric_passed:
          passed = False
      except Exception as e:
        logger.warning("Metric %s failed: %s", metric.name, e)
        scores[metric.name] = 0.0
        metric_passed = False
        passed = False

      # Stash per-metric reporting detail for *every* metric so the CLI
      # ``--exit-code`` failure output always has a threshold / score /
      # passed triple to emit, even for custom metrics that didn't
      # declare observed_key / observed_fn. Observed / budget are only
      # included when the metric supplied them. Keys are prefixed with
      # ``metric_`` to avoid colliding with other details callers.
      observed_value: Optional[Any] = None
      if metric.observed_fn is not None:
        try:
          observed_value = metric.observed_fn(session_summary)
        except Exception:  # pylint: disable=broad-except
          logger.debug(
              "Metric %s observed_fn failed", metric.name, exc_info=True
          )
          observed_value = None
      elif metric.observed_key is not None:
        observed_value = session_summary.get(metric.observed_key)
      metric_details = {
          "observed": observed_value,
          "budget": metric.budget,
          "threshold": metric.threshold,
          "score": scores[metric.name],
          "passed": metric_passed,
      }
      if metric.detail_fn is not None:
        try:
          metric_details.update(metric.detail_fn(session_summary))
        except Exception:  # pylint: disable=broad-except
          logger.debug("Metric %s detail_fn failed", metric.name, exc_info=True)
      details[f"metric_{metric.name}"] = metric_details

    return SessionScore(
        session_id=session_summary.get("session_id", "unknown"),
        scores=scores,
        passed=passed,
        details=details,
    )

  # ---- Pre-built evaluators ---- #

  # The prebuilt evaluators below use raw-budget gates: they fail iff
  # the observed metric exceeds the user-supplied budget. Historically
  # these ran the normalized ``udf_kernels.score_*`` functions under a
  # 0.5 score cutoff, which caused ``--threshold=5000`` on latency to
  # fail near 2500ms — the gate was at half the budget the user typed.
  # See CHANGELOG and the related blog-post-#2 plan (#77) for context.
  # ``udf_kernels.score_*`` is unchanged; it still powers the SQL-native
  # UDF path in ``udf_sql_templates.py``, which has its own semantics.

  @staticmethod
  def latency(
      threshold_ms: float = 5000.0,
  ) -> CodeEvaluator:
    """Pre-built evaluator that fails when average latency exceeds the budget.

    Pass/fail is a raw comparison: ``avg_latency_ms <= threshold_ms``
    passes, strictly greater fails. The returned evaluator's score for
    a session is ``1.0`` on pass and ``0.0`` on fail.

    Args:
        threshold_ms: Maximum acceptable average latency in ms.

    Returns:
        CodeEvaluator configured for latency checking.
    """

    def _score(s: dict[str, Any]) -> float:
      observed = s.get("avg_latency_ms", 0) or 0
      return 1.0 if observed <= threshold_ms else 0.0

    evaluator = CodeEvaluator(name="latency_evaluator")
    evaluator.add_metric(
        "latency",
        _score,
        threshold=1.0,
        observed_key="avg_latency_ms",
        budget=threshold_ms,
    )
    return evaluator

  @staticmethod
  def turn_count(max_turns: int = 10) -> CodeEvaluator:
    """Pre-built evaluator that fails when turn count exceeds the budget.

    Pass/fail is a raw comparison: ``turn_count <= max_turns`` passes,
    strictly greater fails.

    Args:
        max_turns: Maximum acceptable number of turns.

    Returns:
        CodeEvaluator configured for turn count checking.
    """

    def _score(s: dict[str, Any]) -> float:
      observed = s.get("turn_count", 0) or 0
      return 1.0 if observed <= max_turns else 0.0

    evaluator = CodeEvaluator(name="turn_count_evaluator")
    evaluator.add_metric(
        "turn_count",
        _score,
        threshold=1.0,
        observed_key="turn_count",
        budget=max_turns,
    )
    return evaluator

  @staticmethod
  def error_rate(
      max_error_rate: float = 0.1,
  ) -> CodeEvaluator:
    """Pre-built evaluator that fails when tool error rate exceeds the budget.

    Pass/fail is a raw comparison: ``(tool_errors / tool_calls) <= max_error_rate``
    passes, strictly greater fails. Sessions with zero tool calls pass
    trivially (nothing to fail).

    Args:
        max_error_rate: Maximum acceptable tool error fraction.

    Returns:
        CodeEvaluator configured for error rate checking.
    """

    def _observed(s: dict[str, Any]) -> float:
      calls = s.get("tool_calls", 0) or 0
      errors = s.get("tool_errors", 0) or 0
      if calls <= 0:
        return 0.0
      return errors / calls

    def _score(s: dict[str, Any]) -> float:
      calls = s.get("tool_calls", 0) or 0
      if calls <= 0:
        return 1.0
      return 1.0 if _observed(s) <= max_error_rate else 0.0

    evaluator = CodeEvaluator(name="error_rate_evaluator")
    evaluator.add_metric(
        "error_rate",
        _score,
        threshold=1.0,
        observed_fn=_observed,
        budget=max_error_rate,
    )
    return evaluator

  @staticmethod
  def token_efficiency(
      max_tokens: int = 50000,
  ) -> CodeEvaluator:
    """Pre-built evaluator that fails when total tokens exceed the budget.

    Pass/fail is a raw comparison: ``total_tokens <= max_tokens``
    passes, strictly greater fails.

    Args:
        max_tokens: Maximum acceptable total token count.

    Returns:
        CodeEvaluator configured for token efficiency.
    """

    def _score(s: dict[str, Any]) -> float:
      observed = s.get("total_tokens", 0) or 0
      return 1.0 if observed <= max_tokens else 0.0

    evaluator = CodeEvaluator(name="token_efficiency_evaluator")
    evaluator.add_metric(
        "token_efficiency",
        _score,
        threshold=1.0,
        observed_key="total_tokens",
        budget=max_tokens,
    )
    return evaluator

  @staticmethod
  def ttft(
      threshold_ms: float = 1000.0,
  ) -> CodeEvaluator:
    """Pre-built evaluator that fails when TTFT exceeds the budget.

    Pass/fail is a raw comparison: ``avg_ttft_ms <= threshold_ms``
    passes, strictly greater fails.

    Args:
        threshold_ms: Maximum acceptable average TTFT in ms.

    Returns:
        CodeEvaluator configured for TTFT checking.
    """

    def _score(s: dict[str, Any]) -> float:
      observed = s.get("avg_ttft_ms", 0) or 0
      return 1.0 if observed <= threshold_ms else 0.0

    evaluator = CodeEvaluator(name="ttft_evaluator")
    evaluator.add_metric(
        "ttft",
        _score,
        threshold=1.0,
        observed_key="avg_ttft_ms",
        budget=threshold_ms,
    )
    return evaluator

  @staticmethod
  def cost_per_session(
      max_cost_usd: float = 1.0,
      input_cost_per_1k: float = 0.00025,
      output_cost_per_1k: float = 0.00125,
  ) -> CodeEvaluator:
    """Pre-built evaluator that fails when per-session cost exceeds the budget.

    Pass/fail is a raw comparison: ``estimated_cost_usd <= max_cost_usd``
    passes, strictly greater fails.

    Args:
        max_cost_usd: Maximum acceptable cost in USD.
        input_cost_per_1k: Cost per 1K input tokens.
        output_cost_per_1k: Cost per 1K output tokens.

    Returns:
        CodeEvaluator configured for cost checking.
    """

    def _observed(s: dict[str, Any]) -> float:
      input_tokens = s.get("input_tokens", 0) or 0
      output_tokens = s.get("output_tokens", 0) or 0
      return (input_tokens / 1000.0) * input_cost_per_1k + (
          output_tokens / 1000.0
      ) * output_cost_per_1k

    def _score(s: dict[str, Any]) -> float:
      return 1.0 if _observed(s) <= max_cost_usd else 0.0

    evaluator = CodeEvaluator(name="cost_evaluator")
    evaluator.add_metric(
        "cost",
        _score,
        threshold=1.0,
        observed_fn=_observed,
        budget=max_cost_usd,
    )
    return evaluator

  @staticmethod
  def context_cache_hit_rate(
      min_hit_rate: float = 0.5,
      fail_on_missing_telemetry: bool = False,
      cold_start_rate: float = 0.1,
      warm_rate: float = 0.9,
  ) -> CodeEvaluator:
    """Pre-built evaluator for Gemini context cache prefix hit rate.

    The observed rate is ``cached_tokens / input_tokens``. The session
    summary should include ``input_tokens``, ``cached_tokens``, and
    ideally ``cache_telemetry_events`` from ``SESSION_SUMMARY_QUERY``.
    Missing cache telemetry is reported separately from a true 0-token
    cache hit so older plugin data does not become a false failure by
    default.

    Args:
        min_hit_rate: Minimum acceptable cached-token fraction.
        fail_on_missing_telemetry: If ``True``, sessions with input
            tokens but no cache telemetry fail. If ``False`` (default),
            they pass with ``cache_state='no_cache_telemetry'``.
        cold_start_rate: Rate below which detail marks the session as
            ``"cold_start"``.
        warm_rate: Rate at or above which detail marks the session as
            ``"warm"``.

    Returns:
        CodeEvaluator configured for context cache efficiency.
    """
    try:
      min_hit_rate = float(min_hit_rate)
    except (TypeError, ValueError) as exc:
      raise ValueError(
          f"min_hit_rate must be a number, got {min_hit_rate!r}"
      ) from exc
    if not 0.0 <= min_hit_rate <= 1.0:
      raise ValueError(
          "min_hit_rate must satisfy 0 <= min_hit_rate <= 1, "
          f"got {min_hit_rate}"
      )
    if not 0.0 <= cold_start_rate < warm_rate <= 1.0:
      raise ValueError(
          "cold_start_rate and warm_rate must satisfy "
          "0 <= cold_start_rate < warm_rate <= 1"
      )

    def _number(value: Any, default: float = 0.0) -> float:
      if value is None:
        return default
      try:
        return float(value)
      except (TypeError, ValueError):
        return default

    def _has_cache_telemetry(s: dict[str, Any]) -> bool:
      if "cache_telemetry_events" in s:
        return _number(s.get("cache_telemetry_events")) > 0
      return s.get("cached_tokens") is not None

    def _rate(s: dict[str, Any]) -> Optional[float]:
      input_tokens = _number(s.get("input_tokens"))
      if input_tokens <= 0:
        return 1.0
      if not _has_cache_telemetry(s):
        return None
      cached_tokens = _number(s.get("cached_tokens"))
      return max(0.0, min(1.0, cached_tokens / input_tokens))

    def _score(s: dict[str, Any]) -> float:
      rate = _rate(s)
      if rate is None:
        return 0.0 if fail_on_missing_telemetry else 1.0
      return rate

    def _details(s: dict[str, Any]) -> dict[str, Any]:
      input_tokens = _number(s.get("input_tokens"))
      cached_tokens = _number(s.get("cached_tokens"))
      telemetry_events = int(_number(s.get("cache_telemetry_events")))
      rate = _rate(s)
      if input_tokens <= 0:
        cache_state = "no_llm_input"
      elif rate is None:
        cache_state = "no_cache_telemetry"
      elif rate < cold_start_rate:
        cache_state = "cold_start"
      elif rate >= warm_rate:
        cache_state = "warm"
      else:
        cache_state = "partial"
      return {
          "cached_tokens": int(cached_tokens),
          "input_tokens": int(input_tokens),
          "cache_telemetry_events": telemetry_events,
          "cache_state": cache_state,
          "cold_start_rate": cold_start_rate,
          "warm_rate": warm_rate,
          "fail_on_missing_telemetry": fail_on_missing_telemetry,
      }

    evaluator = CodeEvaluator(name="context_cache_hit_rate_evaluator")
    evaluator.add_metric(
        "context_cache_hit_rate",
        _score,
        threshold=min_hit_rate,
        observed_fn=_rate,
        budget=min_hit_rate,
        detail_fn=_details,
    )
    return evaluator


# ------------------------------------------------------------------ #
# LLM-as-Judge Evaluator                                               #
# ------------------------------------------------------------------ #


_CORRECTNESS_PROMPT = """\
You are evaluating an AI agent's response for correctness.

## Conversation Trace
{trace_text}

## Final Agent Response
{final_response}

## Instructions
Score the response on a scale of 1 to 10 for correctness: Did the \
agent provide an accurate, factual response that addresses the \
user's request?

Respond with ONLY a valid JSON object:
{{"correctness": <score>, "justification": "<brief reason>"}}
"""

_HALLUCINATION_PROMPT = """\
You are evaluating an AI agent's response for hallucination.

## Conversation Trace
{trace_text}

## Final Agent Response
{final_response}

## Instructions
Score the response on a scale of 1 to 10 for faithfulness (where \
10 means NO hallucination). Does the response contain claims not \
supported by the tool results or conversation context?

Respond with ONLY a valid JSON object:
{{"faithfulness": <score>, "justification": "<brief reason>"}}
"""

_SENTIMENT_PROMPT = """\
You are evaluating the sentiment of an AI agent's conversation.

## Conversation Trace
{trace_text}

## Final Agent Response
{final_response}

## Instructions
Score the overall sentiment and helpfulness of the interaction \
on a scale of 1 to 10 (10 = very positive and helpful).

Respond with ONLY a valid JSON object:
{{"sentiment": <score>, "justification": "<brief reason>"}}
"""


@dataclass
class _JudgeCriterion:
  """A single LLM-as-judge criterion."""

  name: str
  prompt_template: str
  score_key: str
  threshold: float = 0.5


class LLMAsJudge:
  """Semantic evaluator using LLM-as-a-judge.

  Uses BigQuery's native ``ML.GENERATE_TEXT`` (or the Gemini API)
  to evaluate agent traces against semantic criteria like
  correctness, hallucination, and sentiment.
  """

  def __init__(
      self,
      name: str = "llm_judge",
      criteria: Optional[list[_JudgeCriterion]] = None,
      model: Optional[str] = None,
  ) -> None:
    self.name = name
    self._criteria: list[_JudgeCriterion] = criteria or []
    self.model = model or "gemini-2.5-flash"

  def add_criterion(
      self,
      name: str,
      prompt_template: str,
      score_key: str,
      threshold: float = 0.5,
  ) -> LLMAsJudge:
    """Adds a custom evaluation criterion.

    Args:
        name: Criterion name.
        prompt_template: Prompt with {trace_text} and
            {final_response} placeholders.
        score_key: JSON key in LLM response containing score.
        threshold: Pass/fail threshold (0-1 scale).

    Returns:
        Self for chaining.
    """
    self._criteria.append(
        _JudgeCriterion(
            name=name,
            prompt_template=prompt_template,
            score_key=score_key,
            threshold=threshold,
        )
    )
    return self

  async def evaluate_session(
      self,
      trace_text: str,
      final_response: str,
  ) -> SessionScore:
    """Evaluates a session using the LLM judge.

    Args:
        trace_text: Formatted trace text.
        final_response: Final agent response.

    Returns:
        SessionScore with LLM-judged scores.
    """
    scores: dict[str, float] = {}
    feedback_parts: list[str] = []
    passed = True

    for criterion in self._criteria:
      score, feedback = await self._judge_criterion(
          criterion,
          trace_text,
          final_response,
      )
      scores[criterion.name] = score
      if feedback:
        feedback_parts.append(f"{criterion.name}: {feedback}")
      if score < criterion.threshold:
        passed = False

    return SessionScore(
        session_id="",
        scores=scores,
        passed=passed,
        llm_feedback="\n".join(feedback_parts) or None,
    )

  async def _judge_criterion(
      self,
      criterion: _JudgeCriterion,
      trace_text: str,
      final_response: str,
  ) -> tuple[float, str]:
    """Evaluates one criterion via LLM call."""
    prompt = criterion.prompt_template.format(
        trace_text=trace_text,
        final_response=final_response or "No response.",
    )

    try:
      from google import genai
      from google.genai import types

      client = genai.Client()
      response = await client.aio.models.generate_content(
          model=self.model,
          contents=prompt,
          config=types.GenerateContentConfig(
              temperature=0.1,
              max_output_tokens=2048,
          ),
      )

      text = response.text.strip()
      result = _parse_json_from_text(text)

      if result and criterion.score_key in result:
        raw = float(result[criterion.score_key])
        score = raw / 10.0  # Normalize 1-10 to 0-1
        justification = result.get("justification", "")
        return score, justification

      return 0.0, text

    except ImportError:
      logger.warning("google-genai not installed, skipping LLM judge.")
      return 0.0, "google-genai not installed"
    except Exception as e:
      logger.warning("LLM judge failed: %s", e)
      return 0.0, str(e)

  # ---- Pre-built evaluators ---- #

  @staticmethod
  def correctness(
      threshold: float = 0.5,
      model: Optional[str] = None,
  ) -> LLMAsJudge:
    """Pre-built correctness evaluator.

    Args:
        threshold: Minimum score to pass (0-1).
        model: LLM model to use for judging.

    Returns:
        LLMAsJudge configured for correctness.
    """
    judge = LLMAsJudge(
        name="correctness_judge",
        model=model,
    )
    judge.add_criterion(
        name="correctness",
        prompt_template=_CORRECTNESS_PROMPT,
        score_key="correctness",
        threshold=threshold,
    )
    return judge

  @staticmethod
  def hallucination(
      threshold: float = 0.5,
      model: Optional[str] = None,
  ) -> LLMAsJudge:
    """Pre-built hallucination (faithfulness) evaluator.

    Args:
        threshold: Minimum faithfulness score to pass (0-1).
        model: LLM model to use for judging.

    Returns:
        LLMAsJudge configured for hallucination detection.
    """
    judge = LLMAsJudge(
        name="hallucination_judge",
        model=model,
    )
    judge.add_criterion(
        name="faithfulness",
        prompt_template=_HALLUCINATION_PROMPT,
        score_key="faithfulness",
        threshold=threshold,
    )
    return judge

  @staticmethod
  def sentiment(
      threshold: float = 0.5,
      model: Optional[str] = None,
  ) -> LLMAsJudge:
    """Pre-built sentiment evaluator.

    Args:
        threshold: Minimum sentiment score to pass (0-1).
        model: LLM model to use for judging.

    Returns:
        LLMAsJudge configured for sentiment analysis.
    """
    judge = LLMAsJudge(
        name="sentiment_judge",
        model=model,
    )
    judge.add_criterion(
        name="sentiment",
        prompt_template=_SENTIMENT_PROMPT,
        score_key="sentiment",
        threshold=threshold,
    )
    return judge


# ------------------------------------------------------------------ #
# SQL Templates for BigQuery-native evaluation                         #
# ------------------------------------------------------------------ #

SESSION_SUMMARY_QUERY = """\
SELECT
  session_id,
  COUNT(*) AS total_events,
  COUNTIF(event_type = 'TOOL_STARTING') AS tool_calls,
  COUNTIF(event_type = 'TOOL_ERROR') AS tool_errors,
  COUNTIF(event_type = 'LLM_REQUEST') AS llm_calls,
  AVG(
    CAST(
      JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64
    )
  ) AS avg_latency_ms,
  MAX(
    CAST(
      JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64
    )
  ) AS max_latency_ms,
  TIMESTAMP_DIFF(
    MAX(timestamp), MIN(timestamp), MILLISECOND
  ) AS total_latency_ms,
  COUNTIF(
    event_type = 'USER_MESSAGE_RECEIVED'
  ) AS turn_count,
  AVG(
    CAST(
      JSON_VALUE(latency_ms, '$.time_to_first_token_ms') AS FLOAT64
    )
  ) AS avg_ttft_ms,
  COUNTIF(event_type LIKE 'HITL_%') AS hitl_events,
  COUNTIF(
    ENDS_WITH(event_type, '_ERROR')
    OR error_message IS NOT NULL
    OR status = 'ERROR'
  ) > 0 AS has_error,
  SUM(COALESCE(
    CAST(JSON_VALUE(
      attributes, '$.usage_metadata.prompt_token_count'
    ) AS INT64),
    CAST(JSON_VALUE(
      content, '$.usage.prompt'
    ) AS INT64),
    CAST(JSON_VALUE(
      attributes, '$.input_tokens'
    ) AS INT64)
  )) AS input_tokens,
  SUM(COALESCE(
    CAST(JSON_VALUE(
      attributes, '$.usage_metadata.candidates_token_count'
    ) AS INT64),
    CAST(JSON_VALUE(
      content, '$.usage.completion'
    ) AS INT64),
    CAST(JSON_VALUE(
      attributes, '$.output_tokens'
    ) AS INT64)
  )) AS output_tokens,
  SUM(COALESCE(
    SAFE_CAST(JSON_VALUE(
      attributes, '$.usage_metadata.cached_content_token_count'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      attributes, '$.context_cache_metadata.cached_content_token_count'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      attributes, '$.context_cache_metadata.cached_tokens'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      attributes, '$.cache_metadata.cached_content_token_count'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      attributes, '$.cache_metadata.cached_tokens'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      attributes, '$.cached_content_token_count'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      attributes, '$.cached_tokens'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      content, '$.usage_metadata.cached_content_token_count'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      content, '$.context_cache_metadata.cached_content_token_count'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      content, '$.context_cache_metadata.cached_tokens'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      content, '$.usage.cached_tokens'
    ) AS INT64),
    SAFE_CAST(JSON_VALUE(
      content, '$.usage.prompt_tokens_details.cached_tokens'
    ) AS INT64),
    0
  )) AS cached_tokens,
  COUNTIF(COALESCE(
    JSON_VALUE(
      attributes, '$.usage_metadata.cached_content_token_count'
    ),
    JSON_VALUE(
      attributes, '$.context_cache_metadata.cached_content_token_count'
    ),
    JSON_VALUE(
      attributes, '$.context_cache_metadata.cached_tokens'
    ),
    JSON_VALUE(
      attributes, '$.cache_metadata.cached_content_token_count'
    ),
    JSON_VALUE(
      attributes, '$.cache_metadata.cached_tokens'
    ),
    JSON_VALUE(
      attributes, '$.cached_content_token_count'
    ),
    JSON_VALUE(
      attributes, '$.cached_tokens'
    ),
    JSON_VALUE(
      content, '$.usage_metadata.cached_content_token_count'
    ),
    JSON_VALUE(
      content, '$.context_cache_metadata.cached_content_token_count'
    ),
    JSON_VALUE(
      content, '$.context_cache_metadata.cached_tokens'
    ),
    JSON_VALUE(
      content, '$.usage.cached_tokens'
    ),
    JSON_VALUE(
      content, '$.usage.prompt_tokens_details.cached_tokens'
    )
  ) IS NOT NULL) AS cache_telemetry_events,
  SUM(COALESCE(
    CAST(JSON_VALUE(
      attributes, '$.usage_metadata.total_token_count'
    ) AS INT64),
    CAST(JSON_VALUE(
      content, '$.usage.total'
    ) AS INT64),
    COALESCE(
      CAST(JSON_VALUE(
        attributes, '$.input_tokens'
      ) AS INT64), 0
    ) + COALESCE(
      CAST(JSON_VALUE(
        attributes, '$.output_tokens'
      ) AS INT64), 0
    )
  )) AS total_tokens
FROM `{project}.{dataset}.{table}`
WHERE {where}
GROUP BY session_id
LIMIT @trace_limit
"""

_AI_GENERATE_JUDGE_BATCH_QUERY_TEMPLATE = """\
WITH session_traces AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type, ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'), ''
        )
      ),
      '\\n' ORDER BY timestamp
    ) AS trace_text,
    ARRAY_AGG(
      JSON_VALUE(content, '$.response')
      IGNORE NULLS
      ORDER BY timestamp DESC
      LIMIT 1
    )[SAFE_OFFSET(0)] AS final_response
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(trace_text) > 10
  LIMIT @trace_limit
)
SELECT
  session_id,
  trace_text,
  final_response,
  gen.score AS score,
  gen.justification AS justification,
  gen.status AS gen_status
FROM (
  SELECT
    session_id,
    trace_text,
    final_response,
    AI.GENERATE(
      -- The Python prompt template is rebuilt at SQL time:
      --   prefix ++ trace_text ++ middle ++ final_response ++ suffix
      -- Each segment is a separate query parameter so AI.GENERATE
      -- sees the exact full Python template (including the
      -- per-criterion output-format spec) the API-fallback path uses.
      prompt => CONCAT(
        @judge_prompt_prefix, trace_text,
        @judge_prompt_middle, COALESCE(final_response, 'N/A'),
        @judge_prompt_suffix
      ),
      endpoint => '{endpoint}',{connection_arg}
      model_params => JSON '{{"generationConfig": {{"temperature": 0.1, "maxOutputTokens": 1024}}}}',
      output_schema => 'score INT64, justification STRING'
    ) AS gen
  FROM session_traces
)
"""


def render_ai_generate_judge_query(
    *,
    project: str,
    dataset: str,
    table: str,
    where: str,
    endpoint: str,
    connection_id: Optional[str] = None,
) -> str:
  """Render the AI.GENERATE judge batch query for a given config.

  ``AI.GENERATE`` is BigQuery's scalar generative function (it returns a
  ``STRUCT<score, justification, full_response, status, ...>`` shaped
  by ``output_schema``). The function call lives inside a regular
  ``SELECT`` — it is *not* a table-valued function, so the surrounding
  ``FROM session_traces, AI.GENERATE(...)`` lateral-join syntax used
  by older SDK versions does not parse against current BigQuery.

  ``connection_id`` is optional. When supplied (e.g.
  ``"us.bqaa_ai_generate"``) the call uses that connection's service
  account; when omitted, AI.GENERATE runs against the end-user
  credentials of whichever account submits the job. Both shapes are
  documented forms of the same function.
  """
  if connection_id:
    connection_arg = f"\n      connection_id => '{connection_id}',"
  else:
    connection_arg = ""
  return _AI_GENERATE_JUDGE_BATCH_QUERY_TEMPLATE.format(
      project=project,
      dataset=dataset,
      table=table,
      where=where,
      endpoint=endpoint,
      connection_arg=connection_arg,
  )


# Public alias kept for downstream code that imports the raw template
# string (e.g. for inspection / docs). Callers building queries should
# use ``render_ai_generate_judge_query`` instead so the optional
# ``connection_id`` arg is wired correctly.
AI_GENERATE_JUDGE_BATCH_QUERY = _AI_GENERATE_JUDGE_BATCH_QUERY_TEMPLATE

# Legacy template kept for backward compatibility with pre-created
# BQ ML models.
_LEGACY_LLM_JUDGE_BATCH_QUERY = """\
WITH session_traces AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type, ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'), ''
        )
      ),
      '\\n' ORDER BY timestamp
    ) AS trace_text,
    ARRAY_AGG(
      JSON_VALUE(content, '$.response')
      IGNORE NULLS
      ORDER BY timestamp DESC
      LIMIT 1
    )[SAFE_OFFSET(0)] AS final_response
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(trace_text) > 10
  LIMIT @trace_limit
)
SELECT
  session_id,
  trace_text,
  final_response,
  ML.GENERATE_TEXT(
    MODEL `{model}`,
    STRUCT(
      -- Same prefix/middle/suffix substitution as the AI.GENERATE
      -- path; preserves the full Python prompt_template.
      CONCAT(
        @judge_prompt_prefix, trace_text,
        @judge_prompt_middle, COALESCE(final_response, 'N/A'),
        @judge_prompt_suffix
      ) AS prompt
    ),
    STRUCT(0.1 AS temperature, 500 AS max_output_tokens)
  ).ml_generate_text_result AS evaluation
FROM session_traces
"""

# Keep backward-compatible alias.
LLM_JUDGE_BATCH_QUERY = _LEGACY_LLM_JUDGE_BATCH_QUERY


_TRACE_SENTINEL = "\x00__BQAA_JUDGE_TRACE__\x00"
_RESPONSE_SENTINEL = "\x00__BQAA_JUDGE_RESPONSE__\x00"


def split_judge_prompt_template(prompt_template: str) -> tuple[str, str, str]:
  """Split a Python judge prompt into ``(prefix, middle, suffix)``.

  The Python ``LLMAsJudge`` prompt template uses ``{trace_text}`` and
  ``{final_response}`` placeholders (in that order) to interpolate
  per-session inputs. The BigQuery-native ``AI.GENERATE`` and
  ``ML.GENERATE_TEXT`` paths can't use Python ``str.format`` — they
  build the prompt at SQL time. This helper returns the three
  literal segments those SQL paths need to ``CONCAT`` together with
  the SQL-side ``trace_text`` and ``final_response`` columns,
  preserving the exact full template (including the per-criterion
  output-format spec that follows the placeholders).

  Internally the helper format()s the template once with sentinel
  values, so any literal ``{{...}}`` braces in the source template
  (e.g. the JSON output spec ``{{"correctness": <score>, ...}}``)
  are correctly un-escaped before splitting. The SQL paths see the
  same string the API-fallback path's ``str.format(...)`` would
  produce.

  Args:
      prompt_template: The Python prompt template, expected to
          contain both ``{trace_text}`` and ``{final_response}``
          placeholders in that order.

  Returns:
      ``(prefix, middle, suffix)`` such that
      ``prefix + trace_text + middle + final_response + suffix``
      reproduces ``prompt_template.format(trace_text=..., final_response=...)``
      for any inputs. When a placeholder is missing, the helper
      synthesizes a labeled section for the missing input and
      places the label *immediately before* the injected value
      (label first, then value), so the model reads
      ``...Trace:\n<TRACE>\nResponse:\n<RESPONSE>...`` rather than
      the value followed by an orphan label.
  """
  has_trace = "{trace_text}" in prompt_template
  has_response = "{final_response}" in prompt_template

  # Reminder for the fallback branches below: the SQL CONCAT runs
  #   prefix ++ trace_text ++ middle ++ final_response ++ suffix
  # so any label we synthesize for an absent placeholder must end
  # up *next to* the value it labels (label first, then value),
  # not on the far side of it. Earlier versions appended labels
  # *after* the values, which produced ``<TRACE>\nTrace:\n...``.

  if not has_trace and not has_response:
    # No placeholders at all. Append a labeled trace + response
    # block after the user's instructions. The labels precede the
    # values so the model reads them in order.
    return (
        prompt_template + "\nTrace:\n",
        "\nResponse:\n",
        "",
    )

  if not has_trace:
    # final_response placeholder only. Honor the user's structure
    # and inject a labeled trace block right before the response,
    # so the trace label sits next to the trace.
    formatted = prompt_template.format(final_response=_RESPONSE_SENTINEL)
    before_response, _, after_response = formatted.partition(_RESPONSE_SENTINEL)
    return (
        before_response + "\nTrace:\n",
        "\n",
        after_response,
    )

  if not has_response:
    # trace_text placeholder only. Append a labeled response block
    # after the original template's tail, so the response label
    # sits next to the response value (not after it).
    formatted = prompt_template.format(trace_text=_TRACE_SENTINEL)
    prefix, _, after_trace = formatted.partition(_TRACE_SENTINEL)
    return (
        prefix,
        after_trace + "\nResponse:\n",
        "",
    )

  formatted = prompt_template.format(
      trace_text=_TRACE_SENTINEL,
      final_response=_RESPONSE_SENTINEL,
  )
  prefix, _, rest = formatted.partition(_TRACE_SENTINEL)
  middle, _, suffix = rest.partition(_RESPONSE_SENTINEL)
  return prefix, middle, suffix


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def strip_markdown_fences(text: Optional[str]) -> Optional[str]:
  """Strip markdown code block fences (``\\`\\`\\`json ... \\`\\`\\```) if present.

  Models frequently wrap JSON output in fenced code blocks. This helper
  removes the opening ``\\`\\`\\`json`` (or plain ``\\`\\`\\```) and closing
  ``\\`\\`\\``` markers so the result can be passed to ``json.loads()``.

  The regex pattern matches the same fences handled server-side by
  ``REGEXP_REPLACE`` in ``ontology_graph.py`` and ``context_graph.py``.
  """
  if not text:
    return text
  text = text.strip()
  if not text.startswith("```"):
    return text
  text = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", text)
  text = re.sub(r"\n?\s*```[\s\S]*$", "", text)
  return text.strip()


def _parse_json_from_text(text: str) -> Optional[dict[str, Any]]:
  """Extracts and parses JSON from LLM response text."""
  if not text:
    return None

  # Strip markdown fences first
  stripped = strip_markdown_fences(text)
  try:
    return json.loads(stripped)
  except (json.JSONDecodeError, TypeError):
    pass

  # Try raw JSON extraction (brace matching)
  if "{" in stripped:
    try:
      start = stripped.index("{")
      brace = 0
      end = start
      for i, ch in enumerate(stripped[start:], start):
        if ch == "{":
          brace += 1
        elif ch == "}":
          brace -= 1
          if brace == 0:
            end = i + 1
            break
      return json.loads(stripped[start:end])
    except (ValueError, json.JSONDecodeError):
      pass

  return None
