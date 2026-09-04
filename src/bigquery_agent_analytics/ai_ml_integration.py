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

"""BigQuery AI/ML Integration for Agent Analytics.

This module provides integration with BigQuery's AI Operator and ML
capabilities:

- AI.GENERATE: Text generation with Gemini for trace analysis
- AI.EMBED: Generate embeddings for semantic search (scalar, no model
  creation needed).  Falls back to legacy ``ML.GENERATE_EMBEDDING``
  when the caller supplies a pre-created BQ ML model reference.
- AI.DETECT_ANOMALIES: Time-series anomaly detection using the
  built-in TimesFM model (no model training needed).  Falls back to
  legacy ``ML.DETECT_ANOMALIES`` with ARIMA_PLUS when
  ``use_legacy_anomaly_model`` is True.
- AI.FORECAST: Time-series forecasting using the built-in TimesFM
    model (no model training needed).  Falls back to legacy
    ``ML.FORECAST`` with ARIMA_PLUS when ``use_legacy_anomaly_model``
    is True.
- ML.DETECT_ANOMALIES: Behavioral anomaly detection via AUTOENCODER
  (requires model training — no AI Operator equivalent).
- ML.DISTANCE: Vector distance computation for pre-computed embeddings.
    AI.SIMILARITY exists for text-to-text comparison but re-embeds per call,
    making it O(N×M) in cross joins vs O(N+M) with AI.EMBED + ML.DISTANCE.
- Batch evaluation using BigQuery's high-throughput ML inference

Example usage:
    ai_client = BigQueryAIClient(
        project_id="my-project",
        dataset_id="agent_analytics",
    )

    # Generate embeddings for traces
    embeddings = await ai_client.generate_embeddings(
        texts=["User asked about weather", "Agent provided forecast"],
    )

    # Detect anomalies in latency
    anomalies = await ai_client.detect_latency_anomalies(
        since_hours=24,
    )
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
import json
import logging
from typing import Any, Optional

from google.cloud import bigquery
from pydantic import BaseModel
from pydantic import Field

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


class AnomalyType(Enum):
  """Types of anomalies that can be detected."""

  LATENCY_SPIKE = "latency_spike"
  ERROR_RATE_SPIKE = "error_rate_spike"
  TOOL_FAILURE_PATTERN = "tool_failure_pattern"
  UNUSUAL_BEHAVIOR = "unusual_behavior"


@dataclass
class Anomaly:
  """Represents a detected anomaly in agent behavior."""

  anomaly_type: AnomalyType
  timestamp: datetime
  severity: float  # 0.0 to 1.0
  description: str
  affected_sessions: list[str] = field(default_factory=list)
  details: dict[str, Any] = field(default_factory=dict)


@dataclass
class LatencyForecast:
  """A single forecasted latency data point.

  The ``status`` field carries the per-row ``ai_forecast_status``
  returned by ``AI.FORECAST``.  An empty string means success;
  any other value describes why the forecast failed for that row
  (e.g. insufficient history).  ``ML.FORECAST`` rows always have
  an empty status.

  Callers that only need successful points can filter::

      ok = [f for f in forecasts if not f.status]
  """

  timestamp: datetime
  forecast_value: float
  lower_bound: float
  upper_bound: float
  status: str = ""


@dataclass
class EmbeddingResult:
  """Result of embedding generation."""

  text: str
  embedding: list[float]
  metadata: dict[str, Any] = field(default_factory=dict)


class BatchEvaluationResult(BaseModel):
  """Result of batch evaluation of sessions."""

  session_id: str = Field(description="The evaluated session ID.")
  task_completion: float = Field(description="Task completion score (0-1).")
  efficiency: float = Field(description="Efficiency score (0-1).")
  tool_usage: float = Field(description="Tool usage quality score (0-1).")
  evaluation_text: Optional[str] = Field(
      default=None,
      description="Raw evaluation text from AI.",
  )
  error: Optional[str] = Field(
      default=None,
      description="Error message if evaluation failed.",
  )


class BigQueryAIClient:
  """Client for BigQuery AI functions.

  Provides wrappers around BigQuery's AI.GENERATE, AI.EMBED, and
  ML functions for agent analytics use cases.
  """

  _DEFAULT_ENDPOINT = "gemini-2.5-flash"

  # SQL for AI.GENERATE text analysis
  _AI_GENERATE_QUERY = """
  SELECT
    result.*
  FROM AI.GENERATE(
    prompt => @prompt,
    endpoint => '{endpoint}',
    model_params => JSON '{{"temperature": {temperature}, "max_output_tokens": {max_tokens}}}'
  ) AS result
  """

  # SQL for embedding generation using AI.EMBED (primary, no model needed)
  _AI_EMBED_QUERY = """
  SELECT
    content,
    AI.EMBED(
      content,
      endpoint => '{endpoint}'
    ).result AS embedding
  FROM UNNEST(@texts) AS content
  """

  # Legacy SQL for embedding generation using ML.GENERATE_EMBEDDING
  # (requires a pre-created BQ ML model)
  _LEGACY_GENERATE_EMBEDDING_QUERY = """
  SELECT
    content,
    ML.GENERATE_EMBEDDING(
      MODEL `{model}`,
      STRUCT(content AS content)
    ).ml_generate_embedding_result as embedding
  FROM UNNEST(@texts) as content
  """

  _DEFAULT_EMBEDDING_ENDPOINT = "text-embedding-005"

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      client: Optional[bigquery.Client] = None,
      text_model: Optional[str] = None,
      embedding_model: Optional[str] = None,
      location: str = "US",
      endpoint: Optional[str] = None,
      connection_id: Optional[str] = None,
      embedding_endpoint: Optional[str] = None,
  ) -> None:
    """Initializes BigQueryAIClient.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        client: Optional BigQuery client.
        text_model: Deprecated alias for *endpoint*. Kept for
            backward compatibility.
        embedding_model: Legacy BQ ML model reference for
            ``ML.GENERATE_EMBEDDING``.  When set to a fully-qualified
            model reference (``project.dataset.model``), the legacy
            path is used.  Otherwise ``AI.EMBED`` with
            *embedding_endpoint* is preferred.
        location: BigQuery location.
        endpoint: AI.GENERATE endpoint (default
            ``gemini-2.5-flash``).
        connection_id: Optional BigQuery connection resource ID.
        embedding_endpoint: Vertex AI endpoint for ``AI.EMBED``
            (default ``text-embedding-005``).
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self._client = client
    self._warned_unlabeled_client = False
    self.location = location
    self.connection_id = connection_id

    # Resolve endpoint: explicit > text_model alias > default
    self.endpoint = endpoint or text_model or self._DEFAULT_ENDPOINT
    # Keep text_model for backward compatibility
    self.text_model = text_model or self.endpoint
    self.embedding_endpoint = (
        embedding_endpoint or self._DEFAULT_EMBEDDING_ENDPOINT
    )
    self.embedding_model = embedding_model

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id, location=self.location)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def generate_text(
      self,
      prompt: str,
      temperature: float = 0.3,
      max_tokens: int = 1024,
      endpoint: Optional[str] = None,
      connection_id: Optional[str] = None,
  ) -> str:
    """Generates text using BigQuery AI.GENERATE.

    Args:
        prompt: The prompt for text generation.
        temperature: Sampling temperature.
        max_tokens: Maximum output tokens.
        endpoint: Override the default endpoint for this call.
        connection_id: Override the default connection for this
            call.

    Returns:
        Generated text.
    """
    ep = endpoint or self.endpoint
    query = self._AI_GENERATE_QUERY.format(
        endpoint=ep,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("prompt", "STRING", prompt),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="ai-ml", ai_function="ai-generate"
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      if results:
        return results[0].get("result", "")
      return ""

    except Exception as e:
      logger.warning("Text generation failed: %s", e)
      return ""

  async def generate_embeddings(
      self,
      texts: list[str],
  ) -> list[EmbeddingResult]:
    """Generates embeddings for texts.

    Uses ``AI.EMBED`` (scalar, no model creation needed) by default.
    Falls back to ``ML.GENERATE_EMBEDDING`` when ``embedding_model``
    is set to a pre-created BQ ML model reference (two or more dots).

    Args:
        texts: List of texts to embed.

    Returns:
        List of EmbeddingResult objects.
    """
    if not texts:
      return []

    # Route: legacy ML.GENERATE_EMBEDDING when a BQ ML model ref is set
    if self.embedding_model and self.embedding_model.count(".") >= 2:
      query = self._LEGACY_GENERATE_EMBEDDING_QUERY.format(
          model=self.embedding_model,
      )
      ai_function = "ml-generate-embedding"
    else:
      query = self._AI_EMBED_QUERY.format(
          endpoint=self.embedding_endpoint,
      )
      ai_function = "ai-embed"

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("texts", "STRING", texts),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="ai-ml", ai_function=ai_function
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      embeddings = []
      for row in results:
        embedding_data = row.get("embedding")
        embedding_values = []
        if embedding_data:
          if isinstance(embedding_data, (list, tuple)):
            embedding_values = list(embedding_data)
          elif hasattr(embedding_data, "values"):
            embedding_values = list(embedding_data.values)

        embeddings.append(
            EmbeddingResult(
                text=row.get("content", ""),
                embedding=embedding_values,
            )
        )

      return embeddings

    except Exception as e:
      logger.warning("Embedding generation failed: %s", e)
      return []

  async def analyze_trace(
      self,
      trace_text: str,
      analysis_prompt: str,
  ) -> dict[str, Any]:
    """Analyzes a trace using AI.GENERATE.

    Args:
        trace_text: The trace text to analyze.
        analysis_prompt: Specific analysis instructions.

    Returns:
        Analysis results as dict.
    """
    full_prompt = f"""
{analysis_prompt}

Trace:
{trace_text}

Provide your analysis as JSON.
"""

    result = await self.generate_text(full_prompt)

    try:
      if "{" in result:
        start = result.index("{")
        end = result.rindex("}") + 1
        return json.loads(result[start:end])
    except (json.JSONDecodeError, ValueError):
      pass

    return {"raw_analysis": result}


class EmbeddingSearchClient:
  """Client for semantic search using BigQuery embeddings.

  Provides vector similarity search over pre-computed embeddings
  stored in BigQuery.
  """

  _VECTOR_SEARCH_QUERY = """
  SELECT
    session_id,
    content,
    timestamp,
    ML.DISTANCE(embedding, @query_embedding, 'COSINE') as distance
  FROM `{project}.{dataset}.{table}`
  WHERE embedding IS NOT NULL
    {filters}
  ORDER BY distance ASC
  LIMIT @top_k
  """

  _CREATE_EMBEDDINGS_TABLE_QUERY = """
  CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{table}` (
    session_id STRING,
    invocation_id STRING,
    event_type STRING,
    content STRING,
    timestamp TIMESTAMP,
    user_id STRING,
    embedding ARRAY<FLOAT64>
  )
  PARTITION BY DATE(timestamp)
  CLUSTER BY user_id, event_type
  """

  # Primary: AI.EMBED (scalar, no model creation needed)
  _AI_EMBED_INDEX_QUERY = """
  CREATE OR REPLACE TABLE `{project}.{dataset}.{table}_indexed` AS
  SELECT
    e.session_id,
    e.invocation_id,
    e.event_type,
    JSON_EXTRACT_SCALAR(e.content, '$.text_summary') as content,
    e.timestamp,
    e.user_id,
    AI.EMBED(
      JSON_EXTRACT_SCALAR(e.content, '$.text_summary'),
      endpoint => '{endpoint}'
    ).result as embedding
  FROM `{project}.{dataset}.{source_table}` e
  WHERE e.event_type IN (
    'USER_MESSAGE_RECEIVED', 'LLM_RESPONSE', 'AGENT_COMPLETED'
  )
    AND COALESCE(
      JSON_EXTRACT_SCALAR(e.content, '$.text_summary'),
      JSON_EXTRACT_SCALAR(e.content, '$.response')
    ) IS NOT NULL
    AND e.timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  """

  # Legacy: ML.GENERATE_EMBEDDING (requires pre-created BQ ML model)
  _LEGACY_INDEX_EMBEDDINGS_QUERY = """
  CREATE OR REPLACE TABLE `{project}.{dataset}.{table}_indexed` AS
  SELECT
    e.session_id,
    e.invocation_id,
    e.event_type,
    JSON_EXTRACT_SCALAR(e.content, '$.text_summary') as content,
    e.timestamp,
    e.user_id,
    ML.GENERATE_EMBEDDING(
      MODEL `{model}`,
      STRUCT(JSON_EXTRACT_SCALAR(e.content, '$.text_summary') AS content)
    ).ml_generate_embedding_result as embedding
  FROM `{project}.{dataset}.{source_table}` e
  WHERE e.event_type IN (
    'USER_MESSAGE_RECEIVED', 'LLM_RESPONSE', 'AGENT_COMPLETED'
  )
    AND COALESCE(
      JSON_EXTRACT_SCALAR(e.content, '$.text_summary'),
      JSON_EXTRACT_SCALAR(e.content, '$.response')
    ) IS NOT NULL
    AND e.timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      embeddings_table: str = "trace_embeddings",
      source_table: str = "agent_events",
      client: Optional[bigquery.Client] = None,
      embedding_model: Optional[str] = None,
      embedding_endpoint: str = "text-embedding-005",
  ) -> None:
    """Initializes EmbeddingSearchClient.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        embeddings_table: Table for embeddings.
        source_table: Source events table.
        client: Optional BigQuery client.
        embedding_model: Legacy BQ ML model reference for
            ``ML.GENERATE_EMBEDDING``.  When set, the legacy path
            is used.  Pass ``None`` (default) to use ``AI.EMBED``.
        embedding_endpoint: Vertex AI endpoint for ``AI.EMBED``
            (default ``text-embedding-005``).
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.embeddings_table = embeddings_table
    self.source_table = source_table
    self._client = client
    self._warned_unlabeled_client = False
    self.embedding_model = embedding_model
    self.embedding_endpoint = embedding_endpoint

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def search(
      self,
      query_embedding: list[float],
      top_k: int = 10,
      user_id: Optional[str] = None,
      since_days: Optional[int] = None,
  ) -> list[dict[str, Any]]:
    """Searches for similar traces using vector similarity.

    Args:
        query_embedding: The query embedding vector.
        top_k: Number of results to return.
        user_id: Optional filter by user.
        since_days: Optional filter by recency.

    Returns:
        List of matching results with similarity scores.
    """
    filters = []
    params = [
        bigquery.ArrayQueryParameter(
            "query_embedding", "FLOAT64", query_embedding
        ),
        bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
    ]

    if user_id:
      filters.append("AND user_id = @user_id")
      params.append(bigquery.ScalarQueryParameter("user_id", "STRING", user_id))

    if since_days:
      filters.append(
          "AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
          "INTERVAL @since_days DAY)"
      )
      params.append(
          bigquery.ScalarQueryParameter("since_days", "INT64", since_days)
      )

    query = self._VECTOR_SEARCH_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.embeddings_table,
        filters=" ".join(filters),
    )

    # ML.DISTANCE is pure vector math over pre-computed embeddings, not
    # an LLM/AI invocation, so no sdk_ai_function dimension is set here.
    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(query_parameters=params), feature="ai-ml"
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      return [
          {
              "session_id": row.get("session_id"),
              "content": row.get("content"),
              "timestamp": row.get("timestamp"),
              "similarity": 1.0 - row.get("distance", 1.0),
          }
          for row in results
      ]

    except Exception as e:
      logger.warning("Vector search failed: %s", e)
      return []

  async def build_embeddings_index(
      self,
      since_days: int = 30,
  ) -> bool:
    """Builds or refreshes the embeddings index.

    Uses ``AI.EMBED`` by default.  Falls back to
    ``ML.GENERATE_EMBEDDING`` when ``embedding_model`` is a legacy
    BQ ML model reference (fully-qualified ``project.dataset.model``).

    Args:
        since_days: Number of days of data to index.

    Returns:
        True if successful, False otherwise.
    """
    if self.embedding_model and self.embedding_model.count(".") >= 2:
      query = self._LEGACY_INDEX_EMBEDDINGS_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          table=self.embeddings_table,
          source_table=self.source_table,
          model=self.embedding_model,
      )
      ai_function = "ml-generate-embedding"
    else:
      query = self._AI_EMBED_INDEX_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          table=self.embeddings_table,
          source_table=self.source_table,
          endpoint=self.embedding_endpoint,
      )
      ai_function = "ai-embed"

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("days", "INT64", since_days),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="ai-ml", ai_function=ai_function
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      await loop.run_in_executor(None, lambda: query_job.result())
      logger.info("Embeddings index built successfully")
      return True

    except Exception as e:
      logger.error("Failed to build embeddings index: %s", e)
      return False


class AnomalyDetector:
  """Detects anomalies in agent behavior using BigQuery AI/ML.

  Supports two detection modes:

  - **Time-series latency anomaly detection** — uses
    ``AI.DETECT_ANOMALIES`` (built-in TimesFM, no model training
    needed) by default.  Set ``use_legacy_anomaly_model=True`` to
    use the legacy ``ML.DETECT_ANOMALIES`` path with a pre-trained
    ARIMA_PLUS model.
  - **Behavioral anomaly detection** — uses
    ``ML.DETECT_ANOMALIES`` with a pre-trained AUTOENCODER model
    (no AI Operator equivalent for non-time-series anomaly
    detection).
  """

  # ---- AI.DETECT_ANOMALIES (primary, no model training needed) ---- #

  _AI_DETECT_LATENCY_ANOMALIES_QUERY = """
  SELECT *
  FROM AI.DETECT_ANOMALIES(
    (
      SELECT
        TIMESTAMP_TRUNC(timestamp, HOUR) AS hour,
        AVG(CAST(JSON_EXTRACT_SCALAR(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency
      FROM `{project}.{dataset}.{table}`
      WHERE event_type = 'LLM_RESPONSE'
        AND latency_ms IS NOT NULL
        AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @training_days DAY)
      GROUP BY hour
      HAVING avg_latency IS NOT NULL
    ),
    (
      SELECT
        TIMESTAMP_TRUNC(timestamp, HOUR) AS hour,
        AVG(CAST(JSON_EXTRACT_SCALAR(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency
      FROM `{project}.{dataset}.{table}`
      WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @hours HOUR)
        AND event_type = 'LLM_RESPONSE'
        AND latency_ms IS NOT NULL
      GROUP BY hour
      HAVING avg_latency IS NOT NULL
    ),
    anomaly_prob_threshold => 0.95,
    timestamp_col => 'hour',
    data_col => 'avg_latency'
  )
  WHERE is_anomaly = TRUE
  """

  # ---- AI.FORECAST (primary, no model training needed) ---- #

  _AI_FORECAST_LATENCY_QUERY = """
  SELECT *
  FROM AI.FORECAST(
    (
      SELECT
        TIMESTAMP_TRUNC(timestamp, HOUR) AS hour,
        AVG(CAST(JSON_EXTRACT_SCALAR(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency
      FROM `{project}.{dataset}.{table}`
      WHERE event_type = 'LLM_RESPONSE'
        AND latency_ms IS NOT NULL
        AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @training_days DAY)
      GROUP BY hour
      HAVING avg_latency IS NOT NULL
    ),
    horizon => {horizon},
    confidence_level => {confidence_level},
    timestamp_col => 'hour',
    data_col => 'avg_latency'
  )
  """

  # ---- Legacy ML.DETECT_ANOMALIES (requires model training) ---- #

  _CREATE_LATENCY_MODEL_QUERY = """
  CREATE OR REPLACE MODEL `{project}.{dataset}.latency_anomaly_model`
  OPTIONS(
    model_type = 'ARIMA_PLUS',
    time_series_timestamp_col = 'hour',
    time_series_data_col = 'avg_latency',
    auto_arima = TRUE,
    data_frequency = 'HOURLY'
  ) AS
  SELECT
    TIMESTAMP_TRUNC(timestamp, HOUR) AS hour,
    AVG(CAST(JSON_EXTRACT_SCALAR(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'LLM_RESPONSE'
    AND latency_ms IS NOT NULL
    AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @training_days DAY)
  GROUP BY hour
  HAVING avg_latency IS NOT NULL
  """

  _LEGACY_DETECT_LATENCY_ANOMALIES_QUERY = """
  SELECT *
  FROM ML.DETECT_ANOMALIES(
    MODEL `{project}.{dataset}.latency_anomaly_model`,
    STRUCT(0.95 AS anomaly_prob_threshold),
    (
      SELECT
        TIMESTAMP_TRUNC(timestamp, HOUR) AS hour,
        AVG(CAST(JSON_EXTRACT_SCALAR(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency
      FROM `{project}.{dataset}.{table}`
      WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @hours HOUR)
        AND event_type = 'LLM_RESPONSE'
        AND latency_ms IS NOT NULL
      GROUP BY hour
      HAVING avg_latency IS NOT NULL
    )
  )
  WHERE is_anomaly = TRUE
  """

  # ---- Legacy ML.FORECAST (requires model training) ---- #

  _LEGACY_FORECAST_LATENCY_QUERY = """
  SELECT *
  FROM ML.FORECAST(
    MODEL `{project}.{dataset}.latency_anomaly_model`,
    STRUCT(@horizon AS horizon, @confidence_level AS confidence_level)
  )
  """

  # ---- Autoencoder for behavioral anomaly detection ---- #
  # No AI Operator equivalent for non-time-series anomaly detection.
  # This path requires model training via ML.DETECT_ANOMALIES.

  _CREATE_BEHAVIOR_MODEL_QUERY = """
  CREATE OR REPLACE MODEL `{project}.{dataset}.behavior_anomaly_model`
  OPTIONS(
    model_type = 'AUTOENCODER',
    activation_fn = 'RELU',
    hidden_units = [16, 8, 16],
    l2_reg = 0.0001,
    learn_rate = 0.001
  ) AS
  SELECT
    total_events,
    tool_calls,
    tool_errors,
    llm_calls,
    avg_latency,
    session_duration
  FROM `{project}.{dataset}.session_features`
  WHERE total_events > 0
  """

  _DETECT_BEHAVIOR_ANOMALIES_QUERY = """
  SELECT
    session_id,
    *
  FROM ML.DETECT_ANOMALIES(
    MODEL `{project}.{dataset}.behavior_anomaly_model`,
    STRUCT(0.01 AS contamination),
    (
      SELECT
        session_id,
        COUNT(*) AS total_events,
        COUNTIF(event_type = 'TOOL_STARTING') AS tool_calls,
        COUNTIF(event_type = 'TOOL_ERROR') AS tool_errors,
        COUNTIF(event_type = 'LLM_REQUEST') AS llm_calls,
        AVG(CAST(JSON_EXTRACT_SCALAR(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency,
        TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) AS session_duration
      FROM `{project}.{dataset}.{table}`
      WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @hours HOUR)
      GROUP BY session_id
      HAVING total_events > 0
    )
  )
  WHERE is_anomaly = TRUE
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      client: Optional[bigquery.Client] = None,
      use_legacy_anomaly_model: bool = False,
  ) -> None:
    """Initializes AnomalyDetector.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        table_id: BigQuery table ID.
        client: Optional BigQuery client.
        use_legacy_anomaly_model: If True, uses the legacy
            ``ML.DETECT_ANOMALIES`` path with a pre-trained ARIMA_PLUS
            model for latency detection.  Defaults to False (uses
            ``AI.DETECT_ANOMALIES`` with built-in TimesFM).
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self._client = client
    self._warned_unlabeled_client = False
    self.use_legacy_anomaly_model = use_legacy_anomaly_model

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def train_latency_model(
      self,
      training_days: int = 30,
  ) -> bool:
    """Trains the ARIMA model for latency anomaly detection.

    Only needed when using the legacy ``ML.DETECT_ANOMALIES`` path
    (``use_legacy_anomaly_model=True``).  The ``AI.DETECT_ANOMALIES``
    primary path uses the built-in TimesFM model and requires no
    training.

    Args:
        training_days: Days of historical data to train on.

    Returns:
        True if training successful or not needed.
    """
    if not self.use_legacy_anomaly_model:
      logger.info(
          "AI.DETECT_ANOMALIES uses built-in TimesFM — "
          "no model training needed."
      )
      return True

    query = self._CREATE_LATENCY_MODEL_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    # CREATE MODEL DDL — no AI function is invoked at query time; the
    # ARIMA_PLUS model is consumed later by ML.DETECT_ANOMALIES.
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "training_days", "INT64", training_days
            ),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="ai-ml")

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      await loop.run_in_executor(None, lambda: query_job.result())
      logger.info("Latency anomaly model trained successfully")
      return True

    except Exception as e:
      logger.error("Failed to train latency model: %s", e)
      return False

  async def detect_latency_anomalies(
      self,
      since_hours: int = 24,
      training_days: int = 30,
  ) -> list[Anomaly]:
    """Detects latency anomalies in recent data.

    Uses ``AI.DETECT_ANOMALIES`` (built-in TimesFM) by default.
    Falls back to ``ML.DETECT_ANOMALIES`` with pre-trained ARIMA_PLUS
    when ``use_legacy_anomaly_model`` is True.

    Args:
        since_hours: Hours of recent data to evaluate for anomalies.
        training_days: Days of historical data for baseline (used
            by ``AI.DETECT_ANOMALIES`` as the historical window).

    Returns:
        List of detected anomalies.
    """
    if self.use_legacy_anomaly_model:
      query = self._LEGACY_DETECT_LATENCY_ANOMALIES_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          table=self.table_id,
      )
      ai_function = "ml-detect-anomalies"
    else:
      query = self._AI_DETECT_LATENCY_ANOMALIES_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          table=self.table_id,
      )
      ai_function = "ai-detect-anomalies"

    params = [
        bigquery.ScalarQueryParameter("hours", "INT64", since_hours),
    ]
    if not self.use_legacy_anomaly_model:
      params.append(
          bigquery.ScalarQueryParameter(
              "training_days", "INT64", training_days
          ),
      )

    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(query_parameters=params),
        feature="ai-ml",
        ai_function=ai_function,
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      anomalies = []
      for row in results:
        # AI.DETECT_ANOMALIES returns time_series_timestamp/time_series_data;
        # legacy ML.DETECT_ANOMALIES returns hour/avg_latency.
        hour = row.get("time_series_timestamp") or row.get("hour")
        if isinstance(hour, datetime):
          timestamp = hour
        else:
          timestamp = datetime.now(timezone.utc)

        anomaly_prob = row.get("anomaly_probability", 0.5)
        avg_latency = row.get("time_series_data") or row.get("avg_latency") or 0

        anomalies.append(
            Anomaly(
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                timestamp=timestamp,
                severity=float(anomaly_prob),
                description=(
                    f"Unusual latency detected: {avg_latency:.0f}ms average"
                ),
                details={
                    "avg_latency_ms": avg_latency,
                    "anomaly_probability": anomaly_prob,
                    "lower_bound": row.get("lower_bound"),
                    "upper_bound": row.get("upper_bound"),
                },
            )
        )

      return anomalies

    except Exception as e:
      logger.warning("Latency anomaly detection failed: %s", e)
      return []

  async def forecast_latency(
      self,
      horizon_hours: int = 24,
      training_days: int = 30,
      confidence_level: float = 0.95,
  ) -> list[LatencyForecast]:
    """Forecasts future latency using time-series prediction.

    Uses AI.FORECAST with built-in TimesFM by default.
    Falls back to ML.FORECAST with ARIMA_PLUS when
    ``use_legacy_anomaly_model`` is True (requires prior
    ``train_latency_model()`` call).

    Args:
        horizon_hours: Number of hours to forecast.
        training_days: Historical training window.
        confidence_level: Prediction interval confidence.

    Returns:
        List of LatencyForecast points.
    """
    if self.use_legacy_anomaly_model:
      query = self._LEGACY_FORECAST_LATENCY_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
      )
      params = [
          bigquery.ScalarQueryParameter("horizon", "INT64", horizon_hours),
          bigquery.ScalarQueryParameter(
              "confidence_level", "FLOAT64", confidence_level
          ),
      ]
      ai_function = "ml-forecast"
    else:
      # AI.FORECAST requires literal values for named arguments
      # (horizon, confidence_level), so they are inlined via .format().
      # Only training_days stays as a query parameter (inner SELECT).
      query = self._AI_FORECAST_LATENCY_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          table=self.table_id,
          horizon=int(horizon_hours),
          confidence_level=float(confidence_level),
      )
      params = [
          bigquery.ScalarQueryParameter(
              "training_days", "INT64", training_days
          ),
      ]
      ai_function = "ai-forecast"

    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(query_parameters=params),
        feature="ai-ml",
        ai_function=ai_function,
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      forecasts: list[LatencyForecast] = []
      for row in results:
        # AI.FORECAST returns per-row ai_forecast_status (empty on success).
        status = row.get("ai_forecast_status") or ""
        if status:
          logger.warning("AI.FORECAST row error (status: %s)", status)

        ts = row.get("time_series_timestamp") or row.get("forecast_timestamp")
        if not isinstance(ts, datetime):
          ts = datetime.now(timezone.utc)

        val = row.get("time_series_data") or row.get("forecast_value") or 0
        lower = row.get("prediction_interval_lower_bound") or 0
        upper = row.get("prediction_interval_upper_bound") or 0

        forecasts.append(
            LatencyForecast(
                timestamp=ts,
                forecast_value=float(val),
                lower_bound=float(lower),
                upper_bound=float(upper),
                status=status,
            )
        )

      return forecasts

    except Exception as e:
      logger.warning("Latency forecasting failed: %s", e)
      return []

  async def train_behavior_model(self) -> bool:
    """Trains the autoencoder model for behavioral anomaly detection.

    Returns:
        True if training successful.
    """
    # First, create the session features table
    features_query = f"""
    CREATE OR REPLACE TABLE `{self.project_id}.{self.dataset_id}.session_features` AS
    SELECT
      session_id,
      COUNT(*) AS total_events,
      COUNTIF(event_type = 'TOOL_STARTING') AS tool_calls,
      COUNTIF(event_type = 'TOOL_ERROR') AS tool_errors,
      COUNTIF(event_type = 'LLM_REQUEST') AS llm_calls,
      AVG(CAST(JSON_EXTRACT_SCALAR(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency,
      TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) AS session_duration
    FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
    WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
    GROUP BY session_id
    HAVING total_events > 0 AND avg_latency IS NOT NULL
    """

    # Neither the features-table build nor the autoencoder CREATE MODEL
    # invokes an AI function at query time, so only feature="ai-ml" is
    # set — no sdk_ai_function dimension here.
    features_job_config = with_sdk_labels(
        bigquery.QueryJobConfig(), feature="ai-ml"
    )
    model_job_config = with_sdk_labels(
        bigquery.QueryJobConfig(), feature="ai-ml"
    )

    loop = asyncio.get_event_loop()
    try:
      # Create features table
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(
              features_query, job_config=features_job_config
          ),
      )
      await loop.run_in_executor(None, lambda: query_job.result())

      # Train model
      model_query = self._CREATE_BEHAVIOR_MODEL_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
      )

      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(model_query, job_config=model_job_config),
      )
      await loop.run_in_executor(None, lambda: query_job.result())

      logger.info("Behavior anomaly model trained successfully")
      return True

    except Exception as e:
      logger.error("Failed to train behavior model: %s", e)
      return False

  async def detect_behavior_anomalies(
      self,
      since_hours: int = 24,
  ) -> list[Anomaly]:
    """Detects behavioral anomalies in sessions.

    Args:
        since_hours: Hours of data to analyze.

    Returns:
        List of detected anomalies.
    """
    query = self._DETECT_BEHAVIOR_ANOMALIES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("hours", "INT64", since_hours),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="ai-ml", ai_function="ml-detect-anomalies"
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      anomalies = []
      for row in results:
        session_id = row.get("session_id", "unknown")
        tool_errors = row.get("tool_errors", 0)
        tool_calls = row.get("tool_calls", 0)

        if tool_errors > 0 and tool_calls > 0:
          anomaly_type = AnomalyType.TOOL_FAILURE_PATTERN
          description = (
              f"Session {session_id}: {tool_errors}/{tool_calls} tool failures"
          )
        else:
          anomaly_type = AnomalyType.UNUSUAL_BEHAVIOR
          description = f"Session {session_id}: Unusual behavioral pattern"

        anomalies.append(
            Anomaly(
                anomaly_type=anomaly_type,
                timestamp=datetime.now(timezone.utc),
                severity=0.7,  # Default severity for behavioral anomalies
                description=description,
                affected_sessions=[session_id],
                details={
                    "total_events": row.get("total_events"),
                    "tool_calls": tool_calls,
                    "tool_errors": tool_errors,
                    "llm_calls": row.get("llm_calls"),
                    "avg_latency_ms": row.get("avg_latency"),
                    "session_duration_seconds": row.get("session_duration"),
                },
            )
        )

      return anomalies

    except Exception as e:
      logger.warning("Behavior anomaly detection failed: %s", e)
      return []


class BatchEvaluator:
  """Batch evaluation of sessions using BigQuery AI.GENERATE.

  Leverages BigQuery's high-throughput AI.GENERATE for
  evaluating large numbers of sessions efficiently. Uses
  ``output_schema`` for typed structured results.
  """

  _DEFAULT_ENDPOINT = "gemini-2.5-flash"

  _BATCH_EVALUATION_QUERY = """
  WITH session_traces AS (
    SELECT
      session_id,
      STRING_AGG(
        CONCAT(event_type, ': ',
          COALESCE(
            JSON_EXTRACT_SCALAR(
              content, '$.text_summary'
            ), ''
          )
        ),
        '\\n' ORDER BY timestamp
      ) AS trace_text
    FROM `{project}.{dataset}.{table}`
    WHERE timestamp > TIMESTAMP_SUB(
      CURRENT_TIMESTAMP(), INTERVAL @days DAY
    )
      AND event_type IN (
        'USER_MESSAGE_RECEIVED',
        'TOOL_STARTING',
        'TOOL_COMPLETED',
        'AGENT_COMPLETED'
      )
    GROUP BY session_id
    HAVING LENGTH(trace_text) > 10
    LIMIT @limit
  )
  SELECT
    session_id,
    trace_text,
    result.*
  FROM session_traces,
  AI.GENERATE(
    prompt => CONCAT(
      'Evaluate this agent trace on a scale of 1-10 for:\\n',
      '1. Task completion\\n',
      '2. Efficiency\\n',
      '3. Tool usage\\n',
      'Trace:\\n', trace_text
    ),
    endpoint => '{endpoint}',
    model_params => JSON '{{"temperature": 0.1, "max_output_tokens": 500}}',
    output_schema => 'task_completion INT64, efficiency INT64, tool_usage INT64'
  ) AS result
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      client: Optional[bigquery.Client] = None,
      eval_model: Optional[str] = None,
      endpoint: Optional[str] = None,
  ) -> None:
    """Initializes BatchEvaluator.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        table_id: BigQuery table ID.
        client: Optional BigQuery client.
        eval_model: Deprecated alias for *endpoint*. Kept for
            backward compatibility.
        endpoint: AI.GENERATE endpoint (default
            ``gemini-2.5-flash``).
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self._client = client
    self._warned_unlabeled_client = False
    self.endpoint = endpoint or eval_model or self._DEFAULT_ENDPOINT
    # Keep eval_model for backward compatibility
    self.eval_model = eval_model or self.endpoint

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def evaluate_recent_sessions(
      self,
      days: int = 1,
      limit: int = 100,
  ) -> list[BatchEvaluationResult]:
    """Evaluates recent sessions in batch.

    Args:
        days: Days of sessions to evaluate.
        limit: Maximum sessions to evaluate.

    Returns:
        List of BatchEvaluationResult objects.
    """
    query = self._BATCH_EVALUATION_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        endpoint=self.endpoint,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("days", "INT64", days),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="ai-ml", ai_function="ai-generate"
    )

    loop = asyncio.get_event_loop()
    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      evaluations = []
      for row in results:
        session_id = row.get("session_id", "unknown")
        error = None

        # Read typed columns from AI.GENERATE output_schema
        try:
          tc = float(row.get("task_completion", 0))
          eff = float(row.get("efficiency", 0))
          tu = float(row.get("tool_usage", 0))
          task_completion = tc / 10.0
          efficiency = eff / 10.0
          tool_usage = tu / 10.0
        except (TypeError, ValueError) as e:
          task_completion = 0.0
          efficiency = 0.0
          tool_usage = 0.0
          error = f"Failed to parse evaluation: {e}"

        evaluations.append(
            BatchEvaluationResult(
                session_id=session_id,
                task_completion=task_completion,
                efficiency=efficiency,
                tool_usage=tool_usage,
                evaluation_text=None,
                error=error,
            )
        )

      return evaluations

    except Exception as e:
      logger.error("Batch evaluation failed: %s", e)
      return []

  async def store_evaluation_results(
      self,
      results: list[BatchEvaluationResult],
      table_name: str = "session_evaluations",
  ) -> bool:
    """Stores evaluation results to BigQuery.

    Args:
        results: List of evaluation results.
        table_name: Target table name.

    Returns:
        True if successful.
    """
    if not results:
      return True

    table_id = f"{self.project_id}.{self.dataset_id}.{table_name}"

    rows = [
        {
            "session_id": r.session_id,
            "task_completion": r.task_completion,
            "efficiency": r.efficiency,
            "tool_usage": r.tool_usage,
            "evaluation_text": r.evaluation_text,
            "error": r.error,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
        for r in results
    ]

    loop = asyncio.get_event_loop()
    try:
      errors = await loop.run_in_executor(
          None,
          lambda: self.client.insert_rows_json(table_id, rows),
      )

      if errors:
        logger.error("Failed to insert evaluation results: %s", errors)
        return False

      logger.info("Stored %d evaluation results", len(results))
      return True

    except Exception as e:
      logger.error("Failed to store evaluation results: %s", e)
      return False
