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

from __future__ import annotations

"""Backward-compatibility module re-exports + legacy LLM-as-judge SQL templates."""

import logging
from typing import Any, Optional
import warnings

from .performance_evaluator import BigQueryTraceEvaluator
from .performance_evaluator import PerformanceEvaluator
from .system_evaluator import CodeEvaluator
from .system_evaluator import EvaluationReport
from .system_evaluator import SessionScore
from .system_evaluator import SystemEvaluator
from .utils import _parse_json_from_text

__all__ = [
    "SystemEvaluator",
    "CodeEvaluator",
    "PerformanceEvaluator",
    "BigQueryTraceEvaluator",
    "LLMAsJudge",
    "EvaluationReport",
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


class _JudgeCriterion:
  """A single LLM-as-judge criterion."""

  def __init__(
      self,
      name: str,
      prompt_template: str,
      score_key: str,
      threshold: float = 0.5,
  ):
    self.name = name
    self.prompt_template = prompt_template
    self.score_key = score_key
    self.threshold = threshold


class LLMAsJudge:
  """Legacy LLMAsJudge subclass preserving pre-built factories for backwards compatibility."""

  def __init__(
      self,
      name: str = "llm_judge",
      model: Optional[str] = None,
      threshold: float = 0.5,
  ):
    self._name = name
    self.llm_judge_model = model or "gemini-2.5-flash"
    self._threshold = threshold
    self._criteria: list[_JudgeCriterion] = []

    # Add default criterion based on name for backward compatibility with factories
    if name == "correctness_judge":
      self.add_criterion(
          "correctness", _CORRECTNESS_PROMPT, "correctness", threshold
      )
    elif name == "hallucination_judge":
      self.add_criterion(
          "faithfulness", _HALLUCINATION_PROMPT, "faithfulness", threshold
      )
    elif name == "sentiment_judge":
      self.add_criterion("sentiment", _SENTIMENT_PROMPT, "sentiment", threshold)

  @property
  def name(self) -> str:
    return self._name

  def add_criterion(
      self,
      name: str,
      prompt_template: str,
      score_key: str,
      threshold: float = 0.5,
  ) -> LLMAsJudge:
    self._criteria.append(
        _JudgeCriterion(name, prompt_template, score_key, threshold)
    )
    return self

  async def evaluate_session(
      self, trace_text: str, final_response: str
  ) -> SessionScore:
    """Evaluates a single session trace text against all criteria."""
    try:
      from google import genai
      from google.genai import types

      client = genai.Client()
    except ImportError:
      logger.warning("google-genai not installed, skipping LLM judge.")
      return SessionScore(
          session_id="unknown",
          scores={},
          passed=False,
          details={"error": "google-genai not installed"},
      )

    scores: dict[str, float] = {}
    feedback_parts = []
    passed = True

    for criterion in self._criteria:
      prompt = criterion.prompt_template.format(
          trace_text=trace_text,
          final_response=final_response or "No response.",
      )
      try:
        response = await client.aio.models.generate_content(
            model=self.llm_judge_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        text = response.text.strip()
        result = _parse_json_from_text(text)
        if result and criterion.score_key in result:
          raw = float(result[criterion.score_key])
          score = raw / 10.0 if raw > 1.0 else raw
          scores[criterion.name] = score
          if score < criterion.threshold:
            passed = False
          justification = result.get("justification", "")
          if justification:
            feedback_parts.append(f"{criterion.name}: {justification}")
        else:
          scores[criterion.name] = 0.0
          passed = False
          feedback_parts.append(
              f"{criterion.name}: Failed to parse score from {text}"
          )
      except Exception as e:
        logger.warning("Criterion %s failed: %s", criterion.name, e)
        scores[criterion.name] = 0.0
        passed = False
        feedback_parts.append(f"{criterion.name}: Error {e}")

    return SessionScore(
        session_id="unknown",
        scores=scores,
        passed=passed,
        llm_feedback="\n".join(feedback_parts) if feedback_parts else None,
    )

  @staticmethod
  def correctness(
      threshold: float = 0.5, model: Optional[str] = None
  ) -> LLMAsJudge:
    return LLMAsJudge(
        name="correctness_judge", model=model, threshold=threshold
    )

  @staticmethod
  def hallucination(
      threshold: float = 0.5, model: Optional[str] = None
  ) -> LLMAsJudge:
    return LLMAsJudge(
        name="hallucination_judge", model=model, threshold=threshold
    )

  @staticmethod
  def sentiment(
      threshold: float = 0.5, model: Optional[str] = None
  ) -> LLMAsJudge:
    return LLMAsJudge(name="sentiment_judge", model=model, threshold=threshold)


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

  .. deprecated:: 0.3.0
  """
  warnings.warn(
      "split_judge_prompt_template is deprecated and will be removed in a future version.",
      DeprecationWarning,
      stacklevel=2,
  )
  has_trace = "{trace_text}" in prompt_template
  has_response = "{final_response}" in prompt_template

  if not has_trace and not has_response:
    return (
        prompt_template + "\nTrace:\n",
        "\nResponse:\n",
        "",
    )

  if not has_trace:
    formatted = prompt_template.format(final_response=_RESPONSE_SENTINEL)
    before_response, _, after_response = formatted.partition(_RESPONSE_SENTINEL)
    return (
        before_response + "\nTrace:\n",
        "\n",
        after_response,
    )

  if not has_response:
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
