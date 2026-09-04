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

"""Compatibility exports and legacy LLM judge API and SQL helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Any, Optional
import warnings

from .performance_evaluator import BigQueryTraceEvaluator
from .performance_evaluator import PerformanceEvaluator
from .system_evaluator import CodeEvaluator
from .system_evaluator import EvaluationReport
from .system_evaluator import SESSION_SUMMARY_QUERY
from .system_evaluator import SessionScore
from .system_evaluator import SystemEvaluator
from .utils import _parse_json_from_text
from .utils import strip_markdown_fences

DEFAULT_ENDPOINT = "gemini-2.5-flash"

__all__ = [
    "SystemEvaluator",
    "CodeEvaluator",
    "PerformanceEvaluator",
    "BigQueryTraceEvaluator",
    "LLMAsJudge",
    "EvaluationReport",
    "SessionScore",
    "SESSION_SUMMARY_QUERY",
    "DEFAULT_ENDPOINT",
    "strip_markdown_fences",
    "render_ai_generate_judge_query",
    "AI_GENERATE_JUDGE_BATCH_QUERY",
    "LLM_JUDGE_BATCH_QUERY",
    "split_judge_prompt_template",
]

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

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
  """Legacy criterion-based evaluator using the Gemini API per session.

  This standalone adapter preserves the existing factories and custom
  criteria API. New one-sided and side-by-side evaluations can use
  :class:`PerformanceEvaluator`.
  """

  def __init__(
      self,
      name: str = "llm_judge",
      criteria: Optional[list[_JudgeCriterion]] = None,
      model: Optional[str] = None,
  ) -> None:
    self.name = name
    self._criteria: list[_JudgeCriterion] = criteria or []
    self.model = model or DEFAULT_ENDPOINT

  @property
  def llm_judge_model(self) -> str:
    """Model alias shared with the performance evaluator configuration."""
    return self.model

  @llm_judge_model.setter
  def llm_judge_model(self, model: str) -> None:
    self.model = model

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
    passed = bool(self._criteria)

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
        value = result[criterion.score_key]
        if isinstance(value, bool):
          return 0.0, "Invalid judge score: expected a number from 0 to 10"
        raw = float(value)
        if not math.isfinite(raw) or not 0 <= raw <= 10:
          return 0.0, "Invalid judge score: expected a number from 0 to 10"
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

  .. deprecated:: 0.3.0
      Use :class:`PerformanceEvaluator` instead.

  ``AI.GENERATE`` is BigQuery's scalar generative function.
  """
  warnings.warn(
      (
          "render_ai_generate_judge_query is deprecated and will be removed in"
          " a future version. Use PerformanceEvaluator instead."
      ),
      DeprecationWarning,
      stacklevel=2,
  )
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
  warnings.warn(
      "split_judge_prompt_template is deprecated and will be removed in a future version.",
      DeprecationWarning,
      stacklevel=2,
  )
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


def __getattr__(name: str) -> Any:
  if name in ("AI_GENERATE_JUDGE_BATCH_QUERY", "LLM_JUDGE_BATCH_QUERY"):
    warnings.warn(
        (
            f"{name} is deprecated and will be removed in a future version. "
            "Use PerformanceEvaluator instead."
        ),
        DeprecationWarning,
        stacklevel=2,
    )
    if name == "AI_GENERATE_JUDGE_BATCH_QUERY":
      return _AI_GENERATE_JUDGE_BATCH_QUERY_TEMPLATE
    if name == "LLM_JUDGE_BATCH_QUERY":
      return _LEGACY_LLM_JUDGE_BATCH_QUERY
  raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
