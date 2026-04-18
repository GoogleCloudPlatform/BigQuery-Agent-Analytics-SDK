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

"""Feedback loop and curation for BigQuery Agent Analytics SDK.

Provides drift detection between golden datasets and production
traces, plus question distribution analysis using semantic clustering.

Example usage::

    client = Client(project_id="p", dataset_id="d")
    report = client.drift_detection(
        dataset="agent_events",
        filters=TraceFilter(start_time=...),
        golden_dataset="golden_questions_v1",
    )
    print(report.coverage_percentage)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
import json
import logging
from typing import Any, Optional

from pydantic import BaseModel
from pydantic import Field

from ._telemetry import with_sdk_labels

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Drift Detection                                                      #
# ------------------------------------------------------------------ #


class DriftReport(BaseModel):
  """Results of drift detection between golden and production data."""

  coverage_percentage: float = Field(
      description="Percentage of unique golden questions covered.",
  )
  total_golden: int = Field(
      description="Unique questions in golden dataset.",
  )
  total_production: int = Field(
      description="Unique questions in production dataset.",
  )
  covered_questions: list[str] = Field(
      default_factory=list,
      description="Golden questions found in production.",
  )
  uncovered_questions: list[str] = Field(
      default_factory=list,
      description="Golden questions NOT found in production.",
  )
  new_questions: list[str] = Field(
      default_factory=list,
      description="Production questions not in golden dataset.",
  )
  details: dict[str, Any] = Field(
      default_factory=dict,
  )

  def summary(self) -> str:
    """Returns a human-readable summary."""
    lines = [
        "Drift Detection Report",
        f"  Coverage: {self.coverage_percentage:.1f}%",
        f"  Golden questions: {self.total_golden}",
        f"  Production questions: {self.total_production}",
        f"  Covered: {len(self.covered_questions)}",
        f"  Uncovered: {len(self.uncovered_questions)}",
        f"  New in production: {len(self.new_questions)}",
    ]
    return "\n".join(lines)


# SQL: extract production questions
_PRODUCTION_QUESTIONS_QUERY = """\
SELECT
  session_id,
  JSON_EXTRACT_SCALAR(content, '$.text_summary') AS question
FROM `{project}.{dataset}.{table}`
WHERE event_type = 'USER_MESSAGE_RECEIVED'
  AND JSON_EXTRACT_SCALAR(content, '$.text_summary') IS NOT NULL
  AND {where}
LIMIT @trace_limit
"""

# SQL: load golden questions
_GOLDEN_QUESTIONS_QUERY = """\
SELECT question
FROM `{project}.{dataset}.{golden_table}`
"""

# SQL: semantic drift using AI.EMBED (primary, no model creation needed)
_AI_EMBED_SEMANTIC_DRIFT_QUERY = """\
WITH golden AS (
  SELECT
    question,
    AI.EMBED(
      question,
      endpoint => '{endpoint}'
    ).result AS embedding
  FROM `{project}.{dataset}.{golden_table}`
),
production AS (
  SELECT
    session_id,
    JSON_EXTRACT_SCALAR(content, '$.text_summary') AS question,
    AI.EMBED(
      JSON_EXTRACT_SCALAR(content, '$.text_summary'),
      endpoint => '{endpoint}'
    ).result AS embedding
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'USER_MESSAGE_RECEIVED'
    AND JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) IS NOT NULL
    AND {where}
  LIMIT @trace_limit
)
SELECT
  g.question AS golden_question,
  p.question AS closest_production,
  ML.DISTANCE(
    g.embedding, p.embedding, 'COSINE'
  ) AS distance
FROM golden g
CROSS JOIN production p
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY g.question ORDER BY distance ASC
) = 1
ORDER BY distance ASC
"""

# Legacy SQL: semantic drift using ML.GENERATE_EMBEDDING
# (requires a pre-created BQ ML embedding model)
_LEGACY_SEMANTIC_DRIFT_QUERY = """\
WITH golden AS (
  SELECT
    question,
    ML.GENERATE_EMBEDDING(
      MODEL `{model}`,
      STRUCT(question AS content)
    ).ml_generate_embedding_result AS embedding
  FROM `{project}.{dataset}.{golden_table}`
),
production AS (
  SELECT
    session_id,
    JSON_EXTRACT_SCALAR(content, '$.text_summary') AS question,
    ML.GENERATE_EMBEDDING(
      MODEL `{model}`,
      STRUCT(
        JSON_EXTRACT_SCALAR(content, '$.text_summary')
        AS content
      )
    ).ml_generate_embedding_result AS embedding
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'USER_MESSAGE_RECEIVED'
    AND JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) IS NOT NULL
    AND {where}
  LIMIT @trace_limit
)
SELECT
  g.question AS golden_question,
  p.question AS closest_production,
  ML.DISTANCE(
    g.embedding, p.embedding, 'COSINE'
  ) AS distance
FROM golden g
CROSS JOIN production p
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY g.question ORDER BY distance ASC
) = 1
ORDER BY distance ASC
"""

# Keep backward-compatible alias.
_SEMANTIC_DRIFT_QUERY = _LEGACY_SEMANTIC_DRIFT_QUERY


# ------------------------------------------------------------------ #
# Question Distribution / Deep Analysis                                #
# ------------------------------------------------------------------ #


class QuestionCategory(BaseModel):
  """A category of questions with examples."""

  name: str = Field(description="Category name.")
  count: int = Field(default=0, description="Number of questions.")
  examples: list[str] = Field(
      default_factory=list,
      description="Example questions in this category.",
  )
  percentage: float = Field(
      default=0.0,
      description="Percentage of total questions.",
  )


class QuestionDistribution(BaseModel):
  """Results of question distribution analysis."""

  total_questions: int = Field(default=0)
  categories: list[QuestionCategory] = Field(
      default_factory=list,
  )
  details: dict[str, Any] = Field(default_factory=dict)

  def summary(self) -> str:
    """Returns a human-readable summary."""
    lines = [
        "Question Distribution Analysis",
        f"  Total questions: {self.total_questions}",
        "  Categories:",
    ]
    for cat in sorted(self.categories, key=lambda c: c.count, reverse=True):
      lines.append(f"    {cat.name}: {cat.count} ({cat.percentage:.1f}%)")
    return "\n".join(lines)


class AnalysisConfig(BaseModel):
  """Configuration for deep analysis."""

  mode: str = Field(
      default="auto_group_using_semantics",
      description=(
          "Analysis mode: 'frequently_asked',"
          " 'frequently_unanswered',"
          " 'auto_group_using_semantics', or 'custom'."
      ),
  )
  custom_categories: Optional[list[str]] = Field(
      default=None,
      description=(
          "Custom category names defined in natural language"
          " (e.g., 'onboarding related', 'PTO related')."
      ),
  )
  top_k: int = Field(
      default=20,
      description="Number of top items per category.",
  )


# SQL: frequently asked questions
_FREQUENTLY_ASKED_QUERY = """\
SELECT
  JSON_EXTRACT_SCALAR(content, '$.text_summary') AS question,
  COUNT(*) AS frequency
FROM `{project}.{dataset}.{table}`
WHERE event_type = 'USER_MESSAGE_RECEIVED'
  AND JSON_EXTRACT_SCALAR(
    content, '$.text_summary'
  ) IS NOT NULL
  AND {where}
GROUP BY question
ORDER BY frequency DESC
LIMIT @top_k
"""

# SQL: frequently unanswered questions
_FREQUENTLY_UNANSWERED_QUERY = """\
WITH user_msgs AS (
  SELECT
    session_id,
    JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) AS question
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'USER_MESSAGE_RECEIVED'
    AND JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) IS NOT NULL
    AND {where}
),
errors AS (
  SELECT DISTINCT session_id
  FROM `{project}.{dataset}.{table}`
  WHERE (
    ENDS_WITH(event_type, '_ERROR')
    OR error_message IS NOT NULL
    OR status = 'ERROR'
  )
    AND {where}
)
SELECT
  u.question,
  COUNT(*) AS frequency
FROM user_msgs u
JOIN errors e ON u.session_id = e.session_id
GROUP BY u.question
ORDER BY frequency DESC
LIMIT @top_k
"""

# SQL: semantic clustering via AI.GENERATE with typed output
_AI_GENERATE_SEMANTIC_GROUPING_QUERY = """\
WITH questions AS (
  SELECT
    JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) AS question
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'USER_MESSAGE_RECEIVED'
    AND JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) IS NOT NULL
    AND {where}
  LIMIT @trace_limit
)
SELECT
  question,
  result.category
FROM questions,
AI.GENERATE(
  prompt => CONCAT(
    'Classify this question into exactly one category.\\n',
    'Categories: {categories}\\n',
    'Question: ', question, '\\n',
    'Respond with ONLY the category name.'
  ),
  endpoint => '{endpoint}',
  model_params => JSON '{{"temperature": 0.0, "max_output_tokens": 50}}',
  output_schema => 'category STRING'
) AS result
"""

# Legacy template kept for backward compatibility with pre-created
# BQ ML models.
_LEGACY_SEMANTIC_GROUPING_QUERY = """\
WITH questions AS (
  SELECT
    JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) AS question
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'USER_MESSAGE_RECEIVED'
    AND JSON_EXTRACT_SCALAR(
      content, '$.text_summary'
    ) IS NOT NULL
    AND {where}
  LIMIT @trace_limit
)
SELECT
  question,
  ML.GENERATE_TEXT(
    MODEL `{model}`,
    STRUCT(
      CONCAT(
        'Classify this question into exactly one category.\\n',
        'Categories: {categories}\\n',
        'Question: ', question, '\\n',
        'Respond with ONLY the category name.'
      ) AS prompt
    ),
    STRUCT(0.0 AS temperature, 50 AS max_output_tokens)
  ).ml_generate_text_result AS category
FROM questions
"""


def _is_legacy_model_ref(ref: str) -> bool:
  """Returns True when *ref* looks like a BQ ML model reference.

  Legacy model references have the form
  ``project.dataset.model_name`` (two or more dots).
  """
  return ref.count(".") >= 2


def _sanitize_categories(categories_str: str) -> str:
  """Escapes category text for safe embedding in SQL string literals.

  Doubles single quotes and strips backslashes to prevent SQL
  injection or query breakage when custom category labels are
  formatted into ``CONCAT(...)`` prompt text.
  """
  return categories_str.replace("\\", "\\\\").replace("'", "''")


async def compute_drift(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_id: str,
    golden_table: str,
    where_clause: str,
    query_params: list,
    embedding_model: Optional[str] = None,
) -> DriftReport:
  """Computes drift between golden dataset and production.

  Args:
      bq_client: BigQuery client instance.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset.
      table_id: Events table.
      golden_table: Golden questions table.
      where_clause: SQL WHERE clause for filtering.
      query_params: BigQuery query parameters.
      embedding_model: Optional model for semantic comparison.

  Returns:
      DriftReport with coverage metrics.
  """
  from google.cloud import bigquery

  loop = asyncio.get_event_loop()

  # Load golden questions
  golden_q = _GOLDEN_QUESTIONS_QUERY.format(
      project=project_id,
      dataset=dataset_id,
      golden_table=golden_table,
  )
  golden_config = with_sdk_labels(bigquery.QueryJobConfig(), feature="drift")
  golden_job = await loop.run_in_executor(
      None,
      lambda: bq_client.query(golden_q, job_config=golden_config),
  )
  golden_rows = await loop.run_in_executor(
      None,
      lambda: list(golden_job.result()),
  )
  golden_questions = [
      r.get("question", "") for r in golden_rows if r.get("question")
  ]

  # Load production questions
  prod_q = _PRODUCTION_QUESTIONS_QUERY.format(
      project=project_id,
      dataset=dataset_id,
      table=table_id,
      where=where_clause,
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=query_params,
  )
  job_config = with_sdk_labels(job_config, feature="drift")
  prod_job = await loop.run_in_executor(
      None,
      lambda: bq_client.query(prod_q, job_config=job_config),
  )
  prod_rows = await loop.run_in_executor(
      None,
      lambda: list(prod_job.result()),
  )
  prod_questions = [
      r.get("question", "") for r in prod_rows if r.get("question")
  ]

  # Use semantic drift if embedding model is provided
  if embedding_model:
    try:
      return await _semantic_drift(
          bq_client=bq_client,
          project_id=project_id,
          dataset_id=dataset_id,
          table_id=table_id,
          golden_table=golden_table,
          where_clause=where_clause,
          query_params=query_params,
          embedding_model=embedding_model,
          golden_questions=golden_questions,
          prod_questions=prod_questions,
      )
    except Exception as e:
      logger.warning(
          "Semantic drift failed, falling back to keyword matching: %s",
          e,
      )

  # Simple keyword overlap matching — compare lowercased but
  # return original-cased questions for fidelity.
  golden_by_key = {q.lower().strip(): q for q in golden_questions}
  prod_by_key = {q.lower().strip(): q for q in prod_questions}

  golden_keys = set(golden_by_key)
  prod_keys = set(prod_by_key)

  covered_keys = golden_keys & prod_keys
  uncovered_keys = golden_keys - prod_keys
  new_keys = prod_keys - golden_keys

  coverage = (
      (len(covered_keys) / len(golden_keys) * 100) if golden_keys else 0.0
  )

  return DriftReport(
      coverage_percentage=coverage,
      total_golden=len(golden_keys),
      total_production=len(prod_keys),
      covered_questions=sorted(golden_by_key[k] for k in covered_keys),
      uncovered_questions=sorted(golden_by_key[k] for k in uncovered_keys),
      new_questions=sorted([prod_by_key[k] for k in new_keys])[:100],
      details={
          "method": "keyword_overlap",
          "raw_golden_count": len(golden_questions),
          "raw_production_count": len(prod_questions),
          "unique_golden_count": len(golden_keys),
          "unique_production_count": len(prod_keys),
      },
  )


async def _semantic_drift(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_id: str,
    golden_table: str,
    where_clause: str,
    query_params: list,
    embedding_model: str,
    golden_questions: list[str],
    prod_questions: list[str],
    similarity_threshold: float = 0.3,
) -> DriftReport:
  """Computes semantic drift using embedding cosine distance.

  Uses ``AI.EMBED`` (scalar, no model creation needed) when the
  *embedding_model* is not a legacy BQ ML model reference.  Falls
  back to ``ML.GENERATE_EMBEDDING`` for legacy model references
  (two or more dots, e.g. ``project.dataset.model``).

  Both paths use ``ML.DISTANCE`` for the final cosine computation
  (no AI Operator equivalent for vector distance).

  Args:
      bq_client: BigQuery client instance.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset.
      table_id: Events table.
      golden_table: Golden questions table.
      where_clause: SQL WHERE clause for filtering.
      query_params: BigQuery query parameters.
      embedding_model: Vertex AI endpoint (e.g. ``text-embedding-005``)
          or legacy BQ ML model reference (e.g.
          ``project.dataset.embedding_model``).
      golden_questions: Pre-loaded golden questions.
      prod_questions: Pre-loaded production questions.
      similarity_threshold: Cosine distance threshold below which a
          golden question is considered "covered". Default 0.3.

  Returns:
      DriftReport with semantic coverage metrics.
  """
  from google.cloud import bigquery as bq_mod

  loop = asyncio.get_event_loop()

  if _is_legacy_model_ref(embedding_model):
    query = _LEGACY_SEMANTIC_DRIFT_QUERY.format(
        project=project_id,
        dataset=dataset_id,
        table=table_id,
        golden_table=golden_table,
        model=embedding_model,
        where=where_clause,
    )
    ai_function = "ml-generate-embedding"
  else:
    query = _AI_EMBED_SEMANTIC_DRIFT_QUERY.format(
        project=project_id,
        dataset=dataset_id,
        table=table_id,
        golden_table=golden_table,
        endpoint=embedding_model,
        where=where_clause,
    )
    ai_function = "ai-embed"

  job_config = bq_mod.QueryJobConfig(query_parameters=query_params)
  job_config = with_sdk_labels(
      job_config, feature="drift", ai_function=ai_function
  )
  job = await loop.run_in_executor(
      None,
      lambda: bq_client.query(query, job_config=job_config),
  )
  rows = await loop.run_in_executor(
      None,
      lambda: list(job.result()),
  )

  covered: list[str] = []
  uncovered: list[str] = []
  details: dict[str, Any] = {"semantic_matches": []}

  seen_golden = set()
  for row in rows:
    g_q = row.get("golden_question", "")
    p_q = row.get("closest_production", "")
    distance = row.get("distance", 1.0)

    seen_golden.add(g_q)
    match_info = {
        "golden": g_q,
        "closest_production": p_q,
        "cosine_distance": distance,
    }
    details["semantic_matches"].append(match_info)

    if distance <= similarity_threshold:
      covered.append(g_q)
    else:
      uncovered.append(g_q)

  # Any golden questions not in the result set are uncovered
  for gq in golden_questions:
    if gq not in seen_golden:
      uncovered.append(gq)

  # Identify new production questions (no close golden match) —
  # compare lowercased but return original-cased questions.
  golden_by_key = {q.lower().strip(): q for q in golden_questions}
  prod_by_key = {q.lower().strip(): q for q in prod_questions}
  new_keys = set(prod_by_key) - set(golden_by_key)
  new_in_prod = sorted([prod_by_key[k] for k in new_keys])[:100]

  # Use deduped golden count so coverage % aligns with total_golden.
  unique_golden = len(golden_by_key)
  coverage = (len(covered) / unique_golden * 100) if unique_golden else 0.0

  details["similarity_threshold"] = similarity_threshold
  details["method"] = "semantic_embedding"
  details["raw_golden_count"] = len(golden_questions)
  details["raw_production_count"] = len(prod_questions)
  details["unique_golden_count"] = unique_golden
  details["unique_production_count"] = len(prod_by_key)

  return DriftReport(
      coverage_percentage=coverage,
      total_golden=unique_golden,
      total_production=len(prod_by_key),
      covered_questions=sorted(covered),
      uncovered_questions=sorted(uncovered),
      new_questions=new_in_prod,
      details=details,
  )


async def compute_question_distribution(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_id: str,
    where_clause: str,
    query_params: list,
    config: AnalysisConfig,
    text_model: Optional[str] = None,
) -> QuestionDistribution:
  """Computes question distribution analysis.

  Args:
      bq_client: BigQuery client instance.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset.
      table_id: Events table.
      where_clause: SQL WHERE clause.
      query_params: BigQuery query parameters.
      config: Analysis configuration.
      text_model: Optional model for semantic grouping.

  Returns:
      QuestionDistribution with categorized results.
  """
  from google.cloud import bigquery

  loop = asyncio.get_event_loop()

  if config.mode == "frequently_asked":
    return await _frequently_asked(
        bq_client,
        project_id,
        dataset_id,
        table_id,
        where_clause,
        query_params,
        config.top_k,
        loop,
    )
  elif config.mode == "frequently_unanswered":
    return await _frequently_unanswered(
        bq_client,
        project_id,
        dataset_id,
        table_id,
        where_clause,
        query_params,
        config.top_k,
        loop,
    )
  else:
    # auto_group or custom
    return await _semantic_grouping(
        bq_client,
        project_id,
        dataset_id,
        table_id,
        where_clause,
        query_params,
        config,
        text_model,
        loop,
    )


async def _frequently_asked(
    bq_client,
    project_id,
    dataset_id,
    table_id,
    where_clause,
    query_params,
    top_k,
    loop,
) -> QuestionDistribution:
  """Computes frequently asked questions."""
  from google.cloud import bigquery

  params = list(query_params) + [
      bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
  ]
  query = _FREQUENTLY_ASKED_QUERY.format(
      project=project_id,
      dataset=dataset_id,
      table=table_id,
      where=where_clause,
  )
  job_config = bigquery.QueryJobConfig(query_parameters=params)
  job_config = with_sdk_labels(job_config, feature="feedback")
  job = await loop.run_in_executor(
      None,
      lambda: bq_client.query(query, job_config=job_config),
  )
  rows = await loop.run_in_executor(
      None,
      lambda: list(job.result()),
  )

  total = sum(r.get("frequency", 0) for r in rows)
  categories = []
  for r in rows:
    q = r.get("question", "")
    freq = r.get("frequency", 0)
    pct = (freq / total * 100) if total else 0.0
    categories.append(
        QuestionCategory(
            name=q,
            count=freq,
            percentage=pct,
            examples=[q],
        )
    )

  return QuestionDistribution(
      total_questions=total,
      categories=categories,
  )


async def _frequently_unanswered(
    bq_client,
    project_id,
    dataset_id,
    table_id,
    where_clause,
    query_params,
    top_k,
    loop,
) -> QuestionDistribution:
  """Computes frequently unanswered questions."""
  from google.cloud import bigquery

  params = list(query_params) + [
      bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
  ]
  query = _FREQUENTLY_UNANSWERED_QUERY.format(
      project=project_id,
      dataset=dataset_id,
      table=table_id,
      where=where_clause,
  )
  job_config = bigquery.QueryJobConfig(query_parameters=params)
  job_config = with_sdk_labels(job_config, feature="feedback")
  job = await loop.run_in_executor(
      None,
      lambda: bq_client.query(query, job_config=job_config),
  )
  rows = await loop.run_in_executor(
      None,
      lambda: list(job.result()),
  )

  total = sum(r.get("frequency", 0) for r in rows)
  categories = []
  for r in rows:
    q = r.get("question", "")
    freq = r.get("frequency", 0)
    pct = (freq / total * 100) if total else 0.0
    categories.append(
        QuestionCategory(
            name=q,
            count=freq,
            percentage=pct,
            examples=[q],
        )
    )

  return QuestionDistribution(
      total_questions=total,
      categories=categories,
  )


async def _semantic_grouping(
    bq_client,
    project_id,
    dataset_id,
    table_id,
    where_clause,
    query_params,
    config,
    text_model,
    loop,
) -> QuestionDistribution:
  """Groups questions semantically using LLM classification.

  Uses the same endpoint-routing strategy as the evaluators
  and insights modules:

  1. AI.GENERATE — when the endpoint is not a legacy BQ ML
     model reference (default path).
  2. Legacy ML.GENERATE_TEXT — when the endpoint has 2+ dots
     (``project.dataset.model``).
  3. Fallback — ``frequently_asked`` when neither path
     succeeds.

  The ``details`` dict of the returned distribution includes a
  ``grouping_mode`` key reporting which path was used.
  """
  from google.cloud import bigquery

  # Use LLM to classify if model available
  if text_model and (
      config.custom_categories or config.mode == "auto_group_using_semantics"
  ):
    raw_categories = ", ".join(
        config.custom_categories
        or [
            "General Inquiry",
            "Technical Support",
            "Account Management",
            "Feature Request",
            "Bug Report",
            "Other",
        ]
    )
    categories_str = _sanitize_categories(raw_categories)

    # Try AI.GENERATE first when the endpoint is not a legacy ref
    if not _is_legacy_model_ref(text_model):
      try:
        query = _AI_GENERATE_SEMANTIC_GROUPING_QUERY.format(
            project=project_id,
            dataset=dataset_id,
            table=table_id,
            where=where_clause,
            endpoint=text_model,
            categories=categories_str,
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=query_params,
        )
        job_config = with_sdk_labels(
            job_config, feature="feedback", ai_function="ai-generate"
        )
        result = await _run_grouping_query(
            bq_client,
            query,
            job_config,
            config,
            loop,
        )
        result.details["grouping_mode"] = "ai_generate"
        return result
      except Exception as e:
        logger.debug(
            "AI.GENERATE semantic grouping failed, trying legacy: %s",
            e,
        )

    # Legacy ML.GENERATE_TEXT path
    legacy_model = (
        text_model
        if _is_legacy_model_ref(text_model)
        else f"{project_id}.{dataset_id}.gemini_text_model"
    )
    try:
      query = _LEGACY_SEMANTIC_GROUPING_QUERY.format(
          project=project_id,
          dataset=dataset_id,
          table=table_id,
          where=where_clause,
          model=legacy_model,
          categories=categories_str,
      )
      job_config = bigquery.QueryJobConfig(
          query_parameters=query_params,
      )
      job_config = with_sdk_labels(
          job_config, feature="feedback", ai_function="ml-generate-text"
      )
      result = await _run_grouping_query(
          bq_client,
          query,
          job_config,
          config,
          loop,
      )
      result.details["grouping_mode"] = "legacy_ml_generate_text"
      return result
    except Exception as e:
      logger.warning("Semantic grouping failed: %s", e)

  # Fallback: return frequently asked
  result = await _frequently_asked(
      bq_client,
      project_id,
      dataset_id,
      table_id,
      where_clause,
      query_params,
      config.top_k,
      loop,
  )
  result.details["grouping_mode"] = "frequently_asked_fallback"
  return result


async def _run_grouping_query(
    bq_client,
    query,
    job_config,
    config,
    loop,
) -> QuestionDistribution:
  """Executes a semantic grouping query and aggregates results."""
  job = await loop.run_in_executor(
      None,
      lambda: bq_client.query(query, job_config=job_config),
  )
  rows = await loop.run_in_executor(
      None,
      lambda: list(job.result()),
  )

  # Aggregate by category
  cat_data: dict[str, list[str]] = {}
  for r in rows:
    cat = (r.get("category") or "Other").strip()
    q = r.get("question", "")
    cat_data.setdefault(cat, []).append(q)

  total = sum(len(v) for v in cat_data.values())
  result_cats = []
  for name, examples in cat_data.items():
    pct = (len(examples) / total * 100) if total else 0.0
    result_cats.append(
        QuestionCategory(
            name=name,
            count=len(examples),
            percentage=pct,
            examples=examples[: config.top_k],
        )
    )

  return QuestionDistribution(
      total_questions=total,
      categories=result_cats,
  )
