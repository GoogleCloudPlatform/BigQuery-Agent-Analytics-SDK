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

"""Categorical evaluation engine for BigQuery Agent Analytics SDK.

Classifies agent sessions into user-defined categories using BigQuery's
native ``AI.GENERATE``, with Gemini API fallback when BigQuery-native
execution is unavailable. Unlike the numeric ``SystemEvaluator`` and
``LLMAsJudge`` report paths, this module returns label-valued results
with strict category validation.

Example usage::

    from bigquery_agent_analytics.categorical_evaluator import (
        CategoricalEvaluationConfig,
        CategoricalMetricCategory,
        CategoricalMetricDefinition,
    )

    config = CategoricalEvaluationConfig(
        metrics=[
            CategoricalMetricDefinition(
                name="tone",
                definition="Overall tone of the conversation.",
                categories=[
                    CategoricalMetricCategory(
                        name="positive",
                        definition="User is satisfied.",
                    ),
                    CategoricalMetricCategory(
                        name="negative",
                        definition="User is frustrated.",
                    ),
                    CategoricalMetricCategory(
                        name="neutral",
                        definition="No strong sentiment.",
                    ),
                ],
            ),
        ],
    )

    report = client.evaluate_categorical(config=config)
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from enum import Enum
import hashlib
import json
import logging
from typing import Any, Optional

from google.cloud import bigquery
from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator

from bigquery_agent_analytics.trace import AmbiguousSessionError
from bigquery_agent_analytics.trace import ResolvedTraceSelector
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceIdentity
from bigquery_agent_analytics.trace import TraceScope
from bigquery_agent_analytics.utils import strip_markdown_fences

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

DEFAULT_ENDPOINT = "gemini-2.5-flash"


class CategoricalContextSource(str, Enum):
  """SDK-defined provenance for trusted categorical judge context."""

  TRUSTED_JUDGE_CONTEXT = "trusted_judge_context"
  GOLDEN_EXPECTED_ANSWER = "golden_expected_answer"


@dataclass(frozen=True, slots=True)
class _CategoricalEvaluationInput:
  """One exact resolved trace and its trusted judge input."""

  selector: ResolvedTraceSelector
  transcript: str
  judge_context: Optional[str] = None
  context_source: Optional[CategoricalContextSource] = None

  @property
  def evaluation_key(self) -> str:
    """Stable internal key spanning intrinsic identity and exact scope."""
    return _resolved_selector_key(self.selector)


def _resolved_selector_key(selector: ResolvedTraceSelector) -> str:
  """Returns an injective internal key for a resolved trace selector."""
  if type(selector) is not ResolvedTraceSelector:
    raise TypeError("selector must be a ResolvedTraceSelector.")
  return json.dumps(
      [
          selector.identity.session_id,
          selector.identity.user_id,
          selector.identity.root_agent_name,
          selector.scope_signature,
      ],
      ensure_ascii=False,
      separators=(",", ":"),
  )


def _trace_content_text(content: Any) -> str:
  """Mirrors the categorical transcript SQL's JSON_VALUE priority."""
  if not isinstance(content, dict):
    # JSON_VALUE returns NULL when asked to traverse a top-level
    # scalar/array. Span is deliberately lenient about content, so
    # mirror SQL here instead of calling .get() on the foreign shape.
    return ""
  candidates: list[Any] = [
      content.get("text_summary"),
      content.get("response"),
  ]
  artifacts = content.get("artifacts")
  if isinstance(artifacts, list) and artifacts:
    first_artifact = artifacts[0]
    if isinstance(first_artifact, dict):
      parts = first_artifact.get("parts")
      if isinstance(parts, list) and parts and isinstance(parts[0], dict):
        candidates.append(parts[0].get("text"))
  candidates.append(content.get("tool"))
  for value in candidates:
    if value is None or isinstance(value, (dict, list)):
      # JSON_VALUE returns NULL for objects and arrays, so COALESCE
      # continues to the next candidate in the SQL transcript.
      continue
    if isinstance(value, bool):
      # BigQuery renders JSON booleans with JSON's lowercase spelling.
      return "true" if value else "false"
    return value if isinstance(value, str) else str(value)
  return ""


def _spans_to_categorical_transcript(spans: list[Span]) -> str:
  """Formats ordered spans like the categorical transcript SQL."""
  lines = []
  for span in spans:
    agent = f" [{span.agent}]" if span.agent is not None else ""
    lines.append(
        f"{span.event_type}{agent}: {_trace_content_text(span.content)}"
    )
  return "\n".join(lines)


def _trace_to_categorical_transcript(trace: Trace) -> str:
  """Formats a materialized trace like the categorical transcript SQL."""
  return _spans_to_categorical_transcript(trace.spans)


def _normalize_categorical_evaluation_inputs(
    traces: list[Trace],
    per_session_context: Mapping[ResolvedTraceSelector | str, str],
    context_source: CategoricalContextSource = (
        CategoricalContextSource.TRUSTED_JUDGE_CONTEXT
    ),
) -> list[_CategoricalEvaluationInput]:
  """Binds trusted context to a deduplicated exact-selector population.

  Legacy session-id keys are accepted only when that id names one
  transcript-eligible resolved trace in this evaluated population. The
  ``LENGTH(transcript) > 10`` eligibility gate runs before this legacy
  ambiguity check, so an ineligible colliding trace does not make the
  surviving trace ambiguous. Exact selector keys avoid relying on that
  population inference. Keys outside the population are ignored so a
  caller can pass a context superset alongside a narrower
  :class:`TraceFilter`.
  """
  context_snapshot = _validated_context_mapping(per_session_context)

  exact_context: dict[ResolvedTraceSelector, str] = {}
  legacy_context: dict[str, str] = {}
  for key, value in context_snapshot.items():
    if type(key) is ResolvedTraceSelector:
      exact_context[key] = value
    else:
      legacy_context[key] = value

  population: dict[ResolvedTraceSelector, str] = {}
  by_session: dict[str, list[ResolvedTraceSelector]] = {}
  for trace in traces:
    if (
        type(trace.identity) is not TraceIdentity
        or type(trace.scope) is not TraceScope
    ):
      raise TypeError(
          "Identity-bound categorical evaluation requires traces with"
          " resolved TraceIdentity and TraceScope values."
      )
    selector = ResolvedTraceSelector(
        identity=trace.identity,
        scope=trace.scope,
    )
    if selector in population:
      continue
    transcript = _trace_to_categorical_transcript(trace)
    # Preserve the existing transcript SQL's HAVING LENGTH > 10
    # behavior before resolving legacy aliases.
    if len(transcript) <= 10:
      continue
    population[selector] = transcript
    by_session.setdefault(selector.identity.session_id, []).append(selector)

  bound_context: dict[ResolvedTraceSelector, str] = {}

  def _bind(selector: ResolvedTraceSelector, context: str) -> None:
    prior = bound_context.get(selector)
    if prior is not None and prior != context:
      raise ValueError(
          "Multiple mapping keys provide conflicting judge context"
          " for one resolved trace selector."
      )
    bound_context[selector] = context

  for session_id, context in legacy_context.items():
    matches = by_session.get(session_id, [])
    if len(matches) > 1:
      raise AmbiguousSessionError(matches)
    if matches:
      _bind(matches[0], context)

  for selector, context in exact_context.items():
    if selector in population:
      _bind(selector, context)

  return [
      _CategoricalEvaluationInput(
          selector=selector,
          transcript=transcript,
          judge_context=bound_context.get(selector),
          context_source=(
              context_source if selector in bound_context else None
          ),
      )
      for selector, transcript in population.items()
  ]


def _validated_context_mapping(
    per_session_context: Mapping[ResolvedTraceSelector | str, str],
) -> dict[ResolvedTraceSelector | str, str]:
  """Validates and detaches caller-owned judge-context mapping state."""
  if not isinstance(per_session_context, Mapping):
    raise TypeError("per_session_context must be a mapping.")

  snapshot: dict[ResolvedTraceSelector | str, str] = {}
  for key, value in per_session_context.items():
    if type(value) is not str:
      raise TypeError("Judge context values must be strings.")
    if type(key) is ResolvedTraceSelector:
      snapshot[key] = value
    elif type(key) is str:
      snapshot[key] = value
    else:
      raise TypeError(
          "Judge context keys must be exact ResolvedTraceSelector"
          " instances or session-id strings."
      )
  return snapshot


_EVALUATION_INPUT_STRUCT_TYPE = bigquery.StructQueryParameterType(
    bigquery.ScalarQueryParameterType("STRING", name="evaluation_key"),
    bigquery.ScalarQueryParameterType("STRING", name="session_id"),
    bigquery.ScalarQueryParameterType("STRING", name="transcript"),
    bigquery.ScalarQueryParameterType("STRING", name="judge_context"),
)


def _build_evaluation_inputs_parameter(
    inputs: list[_CategoricalEvaluationInput],
) -> bigquery.ArrayQueryParameter:
  """Builds the typed selector/transcript/context array parameter.

  The explicit ``StructQueryParameterType`` is retained for an empty
  input list; BigQuery cannot infer an empty array's element fields.
  """
  values = [
      bigquery.StructQueryParameter(
          None,
          bigquery.ScalarQueryParameter(
              "evaluation_key", "STRING", item.evaluation_key
          ),
          bigquery.ScalarQueryParameter(
              "session_id", "STRING", item.selector.identity.session_id
          ),
          bigquery.ScalarQueryParameter(
              "transcript", "STRING", item.transcript
          ),
          bigquery.ScalarQueryParameter(
              "judge_context", "STRING", item.judge_context
          ),
      )
      for item in inputs
  ]
  return bigquery.ArrayQueryParameter(
      "evaluation_inputs",
      _EVALUATION_INPUT_STRUCT_TYPE,
      values,
  )


# ------------------------------------------------------------------ #
# Configuration Models                                                 #
# ------------------------------------------------------------------ #


class CategoricalMetricCategory(BaseModel):
  """A single allowed category for a categorical metric."""

  name: str = Field(description="Category label.")
  definition: str = Field(description="What this category means.")


class CategoricalMetricDefinition(BaseModel):
  """Definition of one categorical metric to evaluate."""

  name: str = Field(description="Metric name.")
  definition: str = Field(description="What this metric measures.")
  categories: list[CategoricalMetricCategory] = Field(
      description="Allowed categories for this metric.",
  )
  required: bool = Field(
      default=True,
      description="Whether this metric must be classified.",
  )


class CategoricalEvaluationConfig(BaseModel):
  """Configuration for a categorical evaluation run."""

  metrics: list[CategoricalMetricDefinition] = Field(
      description="Metrics to evaluate.",
  )
  endpoint: str = Field(
      default=DEFAULT_ENDPOINT,
      description="Model endpoint for classification.",
  )
  temperature: float = Field(
      default=0.0,
      description="Sampling temperature.",
  )
  persist_results: bool = Field(
      default=False,
      description="Write results to BigQuery.",
  )
  results_table: Optional[str] = Field(
      default=None,
      description="Destination table for results.",
  )
  connection_id: Optional[str] = Field(
      default=None,
      description="BQ connection ID for AI.CLASSIFY / AI.GENERATE.",
  )
  include_justification: bool = Field(
      default=True,
      description="Include justification in output.",
  )
  max_output_tokens: int = Field(
      default=8192,
      ge=1,
      le=65536,
      description="Max output tokens for classification response.",
  )
  api_concurrency: int = Field(
      default=5,
      ge=1,
      description=(
          "Max concurrent Gemini API calls when the API fallback path runs "
          "(used by classify_sessions_via_api). Matches the convention in "
          "trace_evaluator.py and multi_trial.py."
      ),
  )
  prompt_version: Optional[str] = Field(
      default=None,
      description="Tracks prompt version for reproducibility.",
  )


# ------------------------------------------------------------------ #
# Result Models                                                        #
# ------------------------------------------------------------------ #


class CategoricalMetricResult(BaseModel):
  """Classification result for a single metric on a single session."""

  metric_name: str
  category: Optional[str] = None
  passed_validation: bool = True
  justification: Optional[str] = None
  raw_response: Optional[str] = None
  parse_error: bool = False


class CategoricalSessionResult(BaseModel):
  """Classification results for all metrics on a single session."""

  session_id: str
  identity: Optional[TraceIdentity] = None
  scope: Optional[TraceScope] = None
  context_applied: bool = False
  context_source: Optional[CategoricalContextSource] = None
  execution_mode: Optional[str] = None
  metrics: list[CategoricalMetricResult] = Field(default_factory=list)
  details: dict[str, Any] = Field(default_factory=dict)

  @model_validator(mode="after")
  def _validate_provenance(self):
    if (
        self.identity is not None
        and self.identity.session_id != self.session_id
    ):
      raise ValueError("identity.session_id must match session_id.")
    if (self.identity is None) != (self.scope is None):
      raise ValueError("identity and scope must be attached together.")
    if self.context_applied != (self.context_source is not None):
      raise ValueError(
          "context_source must be set exactly when context_applied is true."
      )
    return self


class CategoricalEvaluationReport(BaseModel):
  """Aggregate report from a categorical evaluation run."""

  dataset: str = Field(description="Dataset or filter description.")
  evaluator_name: str = "categorical_evaluator"
  total_sessions: int = 0
  category_distributions: dict[str, dict[str, int]] = Field(
      default_factory=dict,
      description="Maps metric_name -> {category -> count}.",
  )
  details: dict[str, Any] = Field(default_factory=dict)
  session_results: list[CategoricalSessionResult] = Field(
      default_factory=list,
  )
  created_at: datetime = Field(
      default_factory=lambda: datetime.now(timezone.utc),
  )

  def summary(self) -> str:
    """Returns a human-readable summary."""
    lines = [
        f"Categorical Evaluation Report: {self.evaluator_name}",
        f"  Dataset: {self.dataset}",
        f"  Sessions: {self.total_sessions}",
    ]
    parse_errors = self.details.get("parse_errors", 0)
    if parse_errors:
      lines.append(
          f"  Parse errors: {parse_errors}"
          f" ({self.details.get('parse_error_rate', 0):.1%})"
      )
    if self.category_distributions:
      lines.append("  Category Distributions:")
      for metric, dist in sorted(self.category_distributions.items()):
        lines.append(f"    {metric}:")
        for cat, count in sorted(dist.items(), key=lambda x: -x[1]):
          lines.append(f"      {cat}: {count}")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# SQL Template                                                         #
# ------------------------------------------------------------------ #

DEFAULT_RESULTS_TABLE = "categorical_results"

CATEGORICAL_RESULTS_DDL = """\
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{results_table}` (
  session_id STRING,
  user_id STRING,
  root_agent_name STRING,
  experiment_id STRING,
  scope_key STRING,
  identity_key STRING,
  context_applied BOOL,
  context_source STRING,
  metric_name STRING,
  category STRING,
  justification STRING,
  passed_validation BOOL,
  parse_error BOOL,
  raw_response STRING,
  endpoint STRING,
  execution_mode STRING,
  prompt_version STRING,
  created_at TIMESTAMP
)
"""

CATEGORICAL_RESULTS_MIGRATIONS = tuple(
    f"""ALTER TABLE `{{project}}.{{dataset}}.{{results_table}}`
ADD COLUMN IF NOT EXISTS {column}"""
    for column in (
        "user_id STRING",
        "root_agent_name STRING",
        "experiment_id STRING",
        "scope_key STRING",
        "identity_key STRING",
        "context_applied BOOL",
        "context_source STRING",
        "execution_mode STRING",
    )
)

CATEGORICAL_TRANSCRIPT_QUERY = """\
SELECT
  session_id,
  STRING_AGG(
    CONCAT(
      event_type,
      COALESCE(CONCAT(' [', agent, ']'), ''),
      ': ',
      COALESCE(
        JSON_VALUE(content, '$.text_summary'),
        JSON_VALUE(content, '$.response'),
        JSON_VALUE(content, '$.artifacts[0].parts[0].text'),
        JSON_VALUE(content, '$.tool'),
        ''
      )
    ),
    '\\n' ORDER BY timestamp, span_id, invocation_id, event_type
  ) AS transcript
FROM `{project}.{dataset}.{table}`
WHERE {where}
GROUP BY session_id
HAVING LENGTH(transcript) > 10
ORDER BY MAX(timestamp) DESC, session_id
LIMIT @trace_limit
"""

CATEGORICAL_AI_GENERATE_QUERY = """\
WITH session_transcripts AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type,
        COALESCE(CONCAT(' [', agent, ']'), ''),
        ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'),
          JSON_VALUE(content, '$.response'),
          JSON_VALUE(content, '$.artifacts[0].parts[0].text'),
          JSON_VALUE(content, '$.tool'),
          ''
        )
      ),
      '\\n' ORDER BY timestamp, span_id, invocation_id, event_type
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(transcript) > 10
  ORDER BY MAX(timestamp) DESC, session_id
  LIMIT @trace_limit
)
SELECT
  session_id,
  transcript,
  (AI.GENERATE(
    CONCAT(
      @categorical_prompt,
      '\\n\\nTranscript:\\n', transcript
    ),
    endpoint => '{endpoint}',
    model_params => JSON '{{"generationConfig": {{"temperature": {temperature}, "maxOutputTokens": {max_output_tokens}}}}}',
    output_schema => 'classifications STRING'
  )).classifications AS classifications
FROM session_transcripts
"""


# ------------------------------------------------------------------ #
# SQL Escape Helper                                                    #
# ------------------------------------------------------------------ #


def _escape_sql_string_literal(value: str) -> str:
  """Doubles single quotes for safe embedding in SQL string literals."""
  return value.replace("'", "''")


# ------------------------------------------------------------------ #
# AI.CLASSIFY Query Builder                                            #
# ------------------------------------------------------------------ #


def build_classify_categories_literal(
    metric: CategoricalMetricDefinition,
) -> str:
  """Builds a SQL array literal for AI.CLASSIFY categories.

  Returns:
      SQL literal like ``[('label1', 'def1'), ('label2', 'def2')]``.
  """
  pairs = []
  for cat in metric.categories:
    name = _escape_sql_string_literal(cat.name)
    defn = _escape_sql_string_literal(cat.definition)
    pairs.append(f"('{name}', '{defn}')")
  return "[" + ", ".join(pairs) + "]"


def build_ai_classify_query(
    config: CategoricalEvaluationConfig,
    project: str,
    dataset: str,
    table: str,
    where: str,
    endpoint: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> str:
  """Builds a BigQuery SQL query using AI.CLASSIFY.

  One AI.CLASSIFY column per metric in a single SELECT.
  Column names ``classify_0``, ``classify_1``, ... map by index
  to ``config.metrics[0]``, ``config.metrics[1]``, ...

  Args:
      config: Categorical evaluation config.
      project: GCP project ID.
      dataset: BigQuery dataset.
      table: Events table name.
      where: SQL WHERE clause.
      endpoint: Optional model endpoint.
      connection_id: Optional BQ connection ID.

  Returns:
      Complete SQL query string.
  """
  optional_params = []
  if connection_id:
    optional_params.append(
        f"    connection_id => '{_escape_sql_string_literal(connection_id)}'"
    )
  if endpoint:
    optional_params.append(
        f"    endpoint => '{_escape_sql_string_literal(endpoint)}'"
    )

  classify_columns = []
  for i, metric in enumerate(config.metrics):
    cats_literal = build_classify_categories_literal(metric)
    parts = [f"    categories => {cats_literal}"]
    parts.extend(optional_params)
    args_str = ",\n".join(parts)
    classify_columns.append(
        f"  AI.CLASSIFY(\n    transcript,\n{args_str}\n  ) AS classify_{i}"
    )

  columns_sql = ",\n".join(classify_columns)

  return f"""\
WITH session_transcripts AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type,
        COALESCE(CONCAT(' [', agent, ']'), ''),
        ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'),
          JSON_VALUE(content, '$.response'),
          JSON_VALUE(content, '$.artifacts[0].parts[0].text'),
          JSON_VALUE(content, '$.tool'),
          ''
        )
      ),
      '\\n' ORDER BY timestamp, span_id, invocation_id, event_type
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(transcript) > 10
  ORDER BY MAX(timestamp) DESC, session_id
  LIMIT @trace_limit
)
SELECT
  session_id,
  transcript,
{columns_sql}
FROM session_transcripts
"""


# ------------------------------------------------------------------ #
# AI.GENERATE Query Builder                                            #
# ------------------------------------------------------------------ #


def build_ai_generate_query(
    project: str,
    dataset: str,
    table: str,
    where: str,
    endpoint: str,
    temperature: float,
    connection_id: Optional[str] = None,
    max_output_tokens: int = 8192,
    identity_bound: bool = False,
) -> str:
  """Builds the AI.GENERATE categorical classification query.

  Same body as ``CATEGORICAL_AI_GENERATE_QUERY`` but conditionally
  includes ``connection_id`` when provided.

  Args:
      project: GCP project ID.
      dataset: BigQuery dataset.
      table: Events table name.
      where: SQL WHERE clause.
      endpoint: Model endpoint.
      temperature: Sampling temperature.
      connection_id: Optional BQ connection ID.
      max_output_tokens: Maximum model output tokens.
      identity_bound: Read pre-resolved selector/transcript/context structs
          from ``@evaluation_inputs`` instead of grouping source rows by
          session id.

  Returns:
      Complete SQL query string.
  """
  connection_clause = ""
  if connection_id:
    escaped = _escape_sql_string_literal(connection_id)
    connection_clause = f"\n    connection_id => '{escaped}',"

  if identity_bound:
    return f"""\
SELECT
  evaluation_key,
  session_id,
  transcript,
  (AI.GENERATE(
    CONCAT(
      @categorical_prompt,
      IF(
        judge_context IS NULL,
        '',
        CONCAT('\\n\\n', judge_context)
      ),
      '\\n\\nTranscript:\\n', transcript
    ),{connection_clause}
    endpoint => '{_escape_sql_string_literal(endpoint)}',
    model_params => JSON '{{"generationConfig": {{"temperature": {temperature}, "maxOutputTokens": {max_output_tokens}}}}}',
    output_schema => 'classifications STRING'
  )).classifications AS classifications
FROM UNNEST(@evaluation_inputs)
"""

  return f"""\
WITH session_transcripts AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type,
        COALESCE(CONCAT(' [', agent, ']'), ''),
        ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'),
          JSON_VALUE(content, '$.response'),
          JSON_VALUE(content, '$.artifacts[0].parts[0].text'),
          JSON_VALUE(content, '$.tool'),
          ''
        )
      ),
      '\\n' ORDER BY timestamp, span_id, invocation_id, event_type
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(transcript) > 10
  ORDER BY MAX(timestamp) DESC, session_id
  LIMIT @trace_limit
)
SELECT
  session_id,
  transcript,
  (AI.GENERATE(
    CONCAT(
      @categorical_prompt,
      '\\n\\nTranscript:\\n', transcript
    ),{connection_clause}
    endpoint => '{_escape_sql_string_literal(endpoint)}',
    model_params => JSON '{{"generationConfig": {{"temperature": {temperature}, "maxOutputTokens": {max_output_tokens}}}}}',
    output_schema => 'classifications STRING'
  )).classifications AS classifications
FROM session_transcripts
"""


# ------------------------------------------------------------------ #
# AI.CLASSIFY Row Parser                                               #
# ------------------------------------------------------------------ #


def parse_classify_row(
    session_id: str,
    row: dict[str, Any],
    config: CategoricalEvaluationConfig,
) -> tuple[CategoricalSessionResult, int]:
  """Parses a BigQuery AI.CLASSIFY result row.

  AI.CLASSIFY returns the exact category label or NULL.
  No JSON parsing or category validation needed.

  Args:
      session_id: The session ID.
      row: Dict from ``dict(bigquery_row)`` with ``classify_N`` columns.
      config: Evaluation config with metric definitions.

  Returns:
      Tuple of (CategoricalSessionResult, null_count) where
      null_count is the number of NULL classify results
      (execution failures, NOT parse errors).
  """
  metrics = []
  null_count = 0

  for i, metric in enumerate(config.metrics):
    col_name = f"classify_{i}"
    value = row.get(col_name)

    if value is not None:
      metrics.append(
          CategoricalMetricResult(
              metric_name=metric.name,
              category=value,
              passed_validation=True,
              parse_error=False,
              raw_response=value,
          )
      )
    else:
      null_count += 1
      metrics.append(
          CategoricalMetricResult(
              metric_name=metric.name,
              category=None,
              passed_validation=False,
              parse_error=False,
              raw_response=None,
          )
      )

  return (
      CategoricalSessionResult(session_id=session_id, metrics=metrics),
      null_count,
  )


# ------------------------------------------------------------------ #
# Prompt Builder                                                       #
# ------------------------------------------------------------------ #


def build_categorical_prompt(
    config: CategoricalEvaluationConfig,
) -> str:
  """Builds the classification prompt from metric definitions.

  Args:
      config: Categorical evaluation configuration.

  Returns:
      Prompt string instructing the model to classify the session.
  """
  lines = [
      "You are classifying an agent conversation session.",
      "For each metric below, choose exactly one category from the"
      " allowed set.",
      "Do not invent categories or return free-form labels.",
      "",
  ]

  for metric in config.metrics:
    lines.append(f"## Metric: {metric.name}")
    lines.append(f"Definition: {metric.definition}")
    lines.append("Allowed categories:")
    for cat in metric.categories:
      lines.append(f"  - {cat.name}: {cat.definition}")
    lines.append("")

  if config.include_justification:
    justification_note = (
        'For each metric, include a brief "justification" string'
        " explaining your choice."
    )
  else:
    justification_note = (
        'Do not include a "justification" field in your response.'
    )

  lines.extend(
      [
          justification_note,
          "",
          "Respond with ONLY a valid JSON array. Each element must have:",
          '  - "metric_name": the metric name exactly as shown above',
          '  - "category": one of the allowed categories exactly as shown above',
      ]
  )
  if config.include_justification:
    lines.append('  - "justification": a brief explanation')

  lines.extend(
      [
          "",
          "Example output format:",
      ]
  )
  example = []
  for metric in config.metrics:
    entry: dict[str, str] = {
        "metric_name": metric.name,
        "category": metric.categories[0].name,
    }
    if config.include_justification:
      entry["justification"] = "..."
    example.append(entry)
  lines.append(json.dumps(example, indent=2))

  return "\n".join(lines)


# ------------------------------------------------------------------ #
# Parsing and Validation                                               #
# ------------------------------------------------------------------ #


def _build_category_lookup(
    config: CategoricalEvaluationConfig,
) -> dict[str, dict[str, str]]:
  """Builds a case-insensitive category lookup from config.

  Returns:
      ``{metric_name: {lower_cat_name: canonical_cat_name, ...}, ...}``
  """
  lookup: dict[str, dict[str, str]] = {}
  for metric in config.metrics:
    lookup[metric.name] = {
        cat.name.lower().strip(): cat.name for cat in metric.categories
    }
  return lookup


def parse_classifications(
    raw_json: Optional[str],
    config: CategoricalEvaluationConfig,
) -> list[CategoricalMetricResult]:
  """Parses the JSON STRING envelope and validates categories.

  Args:
      raw_json: Raw JSON string from the ``classifications`` column.
      config: Evaluation config with metric definitions.

  Returns:
      One ``CategoricalMetricResult`` per configured metric.
  """
  lookup = _build_category_lookup(config)
  required_metrics = {m.name for m in config.metrics if m.required}
  all_metrics = {m.name for m in config.metrics}

  if not raw_json or not raw_json.strip():
    return [
        CategoricalMetricResult(
            metric_name=m.name,
            parse_error=True,
            passed_validation=False,
            raw_response=raw_json,
        )
        for m in config.metrics
    ]

  # Strip markdown code blocks (```json ... ```) that models often wrap
  # around JSON output. Uses the shared helper from evaluators.py.
  text = strip_markdown_fences(raw_json)

  try:
    parsed = json.loads(text)
  except (json.JSONDecodeError, TypeError):
    return [
        CategoricalMetricResult(
            metric_name=m.name,
            parse_error=True,
            passed_validation=False,
            raw_response=raw_json,
        )
        for m in config.metrics
    ]

  if not isinstance(parsed, list):
    parsed = [parsed]

  results_by_metric: dict[str, CategoricalMetricResult] = {}

  for entry in parsed:
    if not isinstance(entry, dict):
      continue

    metric_name = entry.get("metric_name", "")
    if metric_name not in all_metrics:
      continue

    # Duplicate metric entries are malformed — the prompt asks for
    # exactly one category per metric.  Flag as a parse error.
    if metric_name in results_by_metric:
      results_by_metric[metric_name] = CategoricalMetricResult(
          metric_name=metric_name,
          passed_validation=False,
          parse_error=True,
          raw_response=raw_json,
      )
      continue

    raw_category = str(entry.get("category", "")).lower().strip()
    canonical = lookup.get(metric_name, {}).get(raw_category)

    if canonical is not None:
      results_by_metric[metric_name] = CategoricalMetricResult(
          metric_name=metric_name,
          category=canonical,
          passed_validation=True,
          justification=entry.get("justification"),
          raw_response=raw_json,
      )
    else:
      results_by_metric[metric_name] = CategoricalMetricResult(
          metric_name=metric_name,
          category=entry.get("category"),
          passed_validation=False,
          parse_error=True,
          justification=entry.get("justification"),
          raw_response=raw_json,
      )

  # Fill in missing metrics.
  for metric in config.metrics:
    if metric.name not in results_by_metric:
      results_by_metric[metric.name] = CategoricalMetricResult(
          metric_name=metric.name,
          parse_error=metric.name in required_metrics,
          passed_validation=metric.name not in required_metrics,
          raw_response=raw_json,
      )

  return [results_by_metric[m.name] for m in config.metrics]


def parse_categorical_row(
    session_id: str,
    row: dict[str, Any],
    config: CategoricalEvaluationConfig,
) -> CategoricalSessionResult:
  """Parses a BigQuery result row into a CategoricalSessionResult.

  Args:
      session_id: The session ID.
      row: Dict from ``dict(bigquery_row)`` containing at least
          a ``classifications`` STRING column.
      config: Evaluation config with metric definitions.

  Returns:
      CategoricalSessionResult with validated metric results.
  """
  raw = row.get("classifications")
  metrics = parse_classifications(raw, config)
  return CategoricalSessionResult(
      session_id=session_id,
      metrics=metrics,
  )


# ------------------------------------------------------------------ #
# Report Builder                                                       #
# ------------------------------------------------------------------ #


def build_categorical_report(
    dataset: str,
    session_results: list[CategoricalSessionResult],
    config: CategoricalEvaluationConfig,
) -> CategoricalEvaluationReport:
  """Builds an aggregate report from per-session results.

  Args:
      dataset: Dataset description for the report.
      session_results: Per-session classification results.
      config: Evaluation config.

  Returns:
      CategoricalEvaluationReport with distributions and details.
  """
  distributions: dict[str, Counter] = {
      m.name: Counter() for m in config.metrics
  }
  parse_error_count = 0

  for sr in session_results:
    for mr in sr.metrics:
      if mr.parse_error:
        parse_error_count += 1
      if mr.category is not None:
        distributions[mr.metric_name][mr.category] += 1

  total_classifications = len(session_results) * len(config.metrics)
  parse_error_rate = (
      parse_error_count / total_classifications
      if total_classifications > 0
      else 0.0
  )

  return CategoricalEvaluationReport(
      dataset=dataset,
      total_sessions=len(session_results),
      category_distributions={
          name: dict(counter) for name, counter in distributions.items()
      },
      details={
          "parse_errors": parse_error_count,
          "parse_error_rate": parse_error_rate,
      },
      session_results=session_results,
  )


# ------------------------------------------------------------------ #
# Gemini API Fallback                                                  #
# ------------------------------------------------------------------ #


async def classify_sessions_via_api(
    transcripts: dict[str, str],
    config: CategoricalEvaluationConfig,
    endpoint: str = DEFAULT_ENDPOINT,
    per_session_context: dict[str, str] | None = None,
    resolved_selectors: dict[str, ResolvedTraceSelector] | None = None,
    context_source: CategoricalContextSource = (
        CategoricalContextSource.TRUSTED_JUDGE_CONTEXT
    ),
    execution_mode: str = "api_fallback",
) -> list[CategoricalSessionResult]:
  """Classifies sessions using the Gemini API (fallback).

  Reuses the same prompt-building and validation logic as the
  BigQuery-native ``AI.GENERATE`` path so that results are
  shape-compatible regardless of execution mode.

  Per-session work runs concurrently under a semaphore sized by
  ``config.api_concurrency`` (default 5, matching the convention in
  ``trace_evaluator.py`` and ``multi_trial.py``). Output order matches
  ``transcripts.items()`` insertion order.

  Per-session ``Exception`` is caught inside the worker and converted to
  a parse-error ``CategoricalSessionResult`` so one bad session does not
  abort the batch. ``BaseException`` subclasses (``CancelledError``,
  ``KeyboardInterrupt``, ``SystemExit``) deliberately propagate.

  Args:
      transcripts: Maps ``session_id`` to transcript text.
      config: Categorical evaluation configuration.
      endpoint: Model endpoint name.
      per_session_context: Optional per-session context to inject into the
          judge prompt (e.g. matched golden eval expected answers). Values are
          trusted evaluator material and use the same governance boundary as
          evaluation prompts. When present, parse/exception logs redact model
          text that could echo the context.
      resolved_selectors: Optional internal-input-key to exact selector map.
          When present, transcript/context keys remain internal correlation
          keys while returned results carry the selector's public session id
          plus reserved identity/scope attribution.
      context_source: SDK-defined provenance assigned to results that received
          trusted context.
      execution_mode: Per-result execution provenance.

  Returns:
      One ``CategoricalSessionResult`` per session, in input order.
  """
  prompt_prefix = build_categorical_prompt(config)

  if resolved_selectors is not None:
    if set(resolved_selectors) != set(transcripts):
      raise ValueError(
          "resolved_selectors keys must exactly match transcript keys."
      )
    selectors = list(resolved_selectors.values())
    if any(
        type(selector) is not ResolvedTraceSelector for selector in selectors
    ):
      raise TypeError(
          "resolved_selectors values must be exact"
          " ResolvedTraceSelector instances."
      )
    if len(set(selectors)) != len(selectors):
      raise ValueError(
          "resolved_selectors cannot contain duplicate resolved selectors."
      )

  try:
    from google import genai
    from google.genai import types
  except ImportError:
    logger.warning("google-genai not installed; cannot run API fallback.")
    raise

  client = genai.Client()
  semaphore = asyncio.Semaphore(config.api_concurrency)

  async def _classify_one(
      input_key: str, transcript: str
  ) -> CategoricalSessionResult:
    async with semaphore:
      selector = (
          resolved_selectors.get(input_key)
          if resolved_selectors is not None
          else None
      )
      sid = selector.identity.session_id if selector is not None else input_key
      details = (
          {
              "user_id": selector.identity.user_id,
              "root_agent_name": selector.identity.root_agent_name,
              "scope_signature": selector.scope_signature,
          }
          if selector is not None
          else {}
      )
      text = transcript
      if len(text) > 25000:
        text = text[:25000] + "\n... [truncated]"

      session_ctx = ""
      has_judge_context = bool(
          per_session_context and input_key in per_session_context
      )
      if has_judge_context:
        session_ctx = "\n\n" + per_session_context[input_key]
      full_prompt = prompt_prefix + session_ctx + "\n\nTranscript:\n" + text

      try:
        response = await client.aio.models.generate_content(
            model=endpoint,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                temperature=config.temperature,
                max_output_tokens=config.max_output_tokens,
            ),
        )
        raw_text = response.text.strip() if response.text else ""
        metrics = parse_classifications(raw_text, config)
        has_parse_error = any(m.parse_error for m in metrics)
        if has_parse_error:
          finish_reason = None
          if response.candidates:
            finish_reason = response.candidates[0].finish_reason
          if has_judge_context:
            logger.warning(
                "API parse error for session %s: finish_reason=%s,"
                " raw_text_len=%d (raw text redacted because trusted"
                " judge context was present)",
                sid,
                finish_reason,
                len(raw_text),
            )
          else:
            logger.warning(
                "API parse error for session %s: finish_reason=%s, "
                "raw_text_len=%d, raw_text=%s",
                sid,
                finish_reason,
                len(raw_text),
                repr(raw_text[:500]),
            )
        result = CategoricalSessionResult(
            session_id=sid,
            metrics=metrics,
            details=details,
        )
        _apply_categorical_result_provenance(
            result,
            selector=selector,
            context_applied=has_judge_context,
            context_source=context_source if has_judge_context else None,
            execution_mode=execution_mode,
        )
        return result
      except Exception as e:
        if has_judge_context:
          logger.warning(
              "Categorical API classification EXCEPTION for %s"
              " (details redacted because trusted judge context was"
              " present; type=%s)",
              sid,
              type(e).__name__,
          )
        else:
          logger.warning(
              "Categorical API classification EXCEPTION for %s: %s"
              " (type=%s)",
              sid,
              e,
              type(e).__name__,
          )
        result = CategoricalSessionResult(
            session_id=sid,
            metrics=[
                CategoricalMetricResult(
                    metric_name=m.name,
                    parse_error=True,
                    passed_validation=False,
                    raw_response=None if has_judge_context else str(e),
                )
                for m in config.metrics
            ],
            details=details,
        )
        _apply_categorical_result_provenance(
            result,
            selector=selector,
            context_applied=has_judge_context,
            context_source=context_source if has_judge_context else None,
            execution_mode=execution_mode,
        )
        return result

  tasks = [_classify_one(key, text) for key, text in transcripts.items()]
  results = await asyncio.gather(*tasks)
  return list(results)


def _apply_categorical_result_provenance(
    result: CategoricalSessionResult,
    *,
    selector: Optional[ResolvedTraceSelector],
    context_applied: bool,
    context_source: Optional[CategoricalContextSource],
    execution_mode: str,
) -> None:
  """Assigns SDK-owned identity and execution provenance to one result."""
  result.identity = selector.identity if selector is not None else None
  result.scope = selector.scope if selector is not None else None
  result.context_applied = context_applied
  result.context_source = context_source if context_applied else None
  result.execution_mode = execution_mode
  if selector is not None:
    result.details.update(
        {
            "user_id": selector.identity.user_id,
            "root_agent_name": selector.identity.root_agent_name,
            "scope_signature": selector.scope_signature,
        }
    )


# ------------------------------------------------------------------ #
# Persistence                                                          #
# ------------------------------------------------------------------ #


def flatten_results_to_rows(
    report: CategoricalEvaluationReport,
    config: CategoricalEvaluationConfig,
    endpoint: str,
) -> list[dict]:
  """Flattens session results to persistence rows with identity provenance.

  Identity/scope fields are nullable for historical-compatible rows. Contextual
  rows retain only SDK-owned provenance; their justification and raw response
  are omitted so trusted judge/golden-answer context, including model echoes,
  cannot be persisted.

  Args:
      report: The evaluation report to flatten.
      config: Evaluation config (for prompt_version).
      endpoint: Endpoint used for classification.

  Returns:
      List of dicts suitable for ``insert_rows_json``.
  """
  execution_mode = report.details.get("execution_mode")
  created_at = report.created_at.isoformat()
  rows = []
  for sr in report.session_results:
    sr._validate_provenance()
    identity = sr.identity
    scope = sr.scope
    identity_key = (
        _categorical_identity_key(identity, scope)
        if identity is not None
        else None
    )
    result_execution_mode = sr.execution_mode or execution_mode
    for mr in sr.metrics:
      rows.append(
          {
              "session_id": sr.session_id,
              "user_id": identity.user_id if identity is not None else None,
              "root_agent_name": (
                  identity.root_agent_name if identity is not None else None
              ),
              "experiment_id": (
                  scope.experiment_id if scope is not None else None
              ),
              "scope_key": (
                  scope.scope_signature if scope is not None else None
              ),
              "identity_key": identity_key,
              "context_applied": sr.context_applied,
              "context_source": (
                  sr.context_source.value
                  if sr.context_source is not None
                  else None
              ),
              "metric_name": mr.metric_name,
              "category": mr.category,
              "justification": (
                  None if sr.context_applied else mr.justification
              ),
              "passed_validation": mr.passed_validation,
              "parse_error": mr.parse_error,
              # A model may echo its trusted judge input. Persisting the raw
              # envelope for a contextual evaluation could therefore copy a
              # golden answer even though the SDK never writes the input
              # directly.
              "raw_response": (None if sr.context_applied else mr.raw_response),
              "endpoint": endpoint,
              "execution_mode": result_execution_mode,
              "prompt_version": config.prompt_version,
              "created_at": created_at,
          }
      )
  return rows


def _categorical_identity_key(
    identity: TraceIdentity,
    scope: Optional[TraceScope],
) -> str:
  """Returns a versioned stable key for one resolved trace identity/scope."""
  canonical = json.dumps(
      [
          identity.session_id,
          identity.user_id,
          identity.root_agent_name,
          scope.scope_signature if scope is not None else None,
      ],
      ensure_ascii=False,
      separators=(",", ":"),
  ).encode("utf-8")
  return "v1:" + hashlib.sha256(canonical).hexdigest()
