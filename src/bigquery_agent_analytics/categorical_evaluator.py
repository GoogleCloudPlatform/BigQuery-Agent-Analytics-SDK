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
execution is unavailable. Unlike the numeric ``CodeEvaluator`` and
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

from collections import Counter
from datetime import datetime
from datetime import timezone
import json
import logging
from typing import Any, Optional

from pydantic import BaseModel
from pydantic import Field

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

DEFAULT_ENDPOINT = "gemini-2.5-flash"


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
  metrics: list[CategoricalMetricResult] = Field(default_factory=list)
  details: dict[str, Any] = Field(default_factory=dict)


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
    '\\n' ORDER BY timestamp
  ) AS transcript
FROM `{project}.{dataset}.{table}`
WHERE {where}
GROUP BY session_id
HAVING LENGTH(transcript) > 10
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
      '\\n' ORDER BY timestamp
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(transcript) > 10
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
    model_params => JSON '{{"generationConfig": {{"temperature": {temperature}, "maxOutputTokens": 1024}}}}',
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
      '\\n' ORDER BY timestamp
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(transcript) > 10
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

  Returns:
      Complete SQL query string.
  """
  connection_clause = ""
  if connection_id:
    escaped = _escape_sql_string_literal(connection_id)
    connection_clause = f"\n    connection_id => '{escaped}',"

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
      '\\n' ORDER BY timestamp
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(transcript) > 10
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
    model_params => JSON '{{"generationConfig": {{"temperature": {temperature}, "maxOutputTokens": 1024}}}}',
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

  try:
    parsed = json.loads(raw_json)
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
) -> list[CategoricalSessionResult]:
  """Classifies sessions using the Gemini API (fallback).

  Reuses the same prompt-building and validation logic as the
  BigQuery-native ``AI.GENERATE`` path so that results are
  shape-compatible regardless of execution mode.

  Args:
      transcripts: Maps ``session_id`` to transcript text.
      config: Categorical evaluation configuration.
      endpoint: Model endpoint name.

  Returns:
      One ``CategoricalSessionResult`` per session.
  """
  prompt_prefix = build_categorical_prompt(config)
  results: list[CategoricalSessionResult] = []

  try:
    from google import genai
    from google.genai import types

    client = genai.Client()

    for sid, transcript in transcripts.items():
      text = transcript
      if len(text) > 25000:
        text = text[:25000] + "\n... [truncated]"

      full_prompt = prompt_prefix + "\n\nTranscript:\n" + text

      try:
        response = await client.aio.models.generate_content(
            model=endpoint,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                temperature=config.temperature,
                max_output_tokens=1024,
            ),
        )
        raw_text = response.text.strip()
        metrics = parse_classifications(raw_text, config)
        results.append(
            CategoricalSessionResult(
                session_id=sid,
                metrics=metrics,
            )
        )
      except Exception as e:
        logger.warning(
            "Categorical API classification failed for %s: %s",
            sid,
            e,
        )
        results.append(
            CategoricalSessionResult(
                session_id=sid,
                metrics=[
                    CategoricalMetricResult(
                        metric_name=m.name,
                        parse_error=True,
                        passed_validation=False,
                        raw_response=str(e),
                    )
                    for m in config.metrics
                ],
            )
        )
  except ImportError:
    logger.warning("google-genai not installed; cannot run API fallback.")
    raise

  return results


# ------------------------------------------------------------------ #
# Persistence                                                          #
# ------------------------------------------------------------------ #


def flatten_results_to_rows(
    report: CategoricalEvaluationReport,
    config: CategoricalEvaluationConfig,
    endpoint: str,
) -> list[dict]:
  """Flattens session results to one row per (session_id, metric_name).

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
    for mr in sr.metrics:
      rows.append(
          {
              "session_id": sr.session_id,
              "metric_name": mr.metric_name,
              "category": mr.category,
              "justification": mr.justification,
              "passed_validation": mr.passed_validation,
              "parse_error": mr.parse_error,
              "raw_response": mr.raw_response,
              "endpoint": endpoint,
              "execution_mode": execution_mode,
              "prompt_version": config.prompt_version,
              "created_at": created_at,
          }
      )
  return rows
