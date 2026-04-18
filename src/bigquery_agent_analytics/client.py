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

"""BigQuery Agent Analytics SDK Client.

The ``Client`` class is the primary entry point for the SDK. It
abstracts BigQuery SQL complexity and provides clean Python interfaces
for trace reconstruction, evaluation, and feedback loop curation.

Example usage::

    from bigquery_agent_analytics import Client

    client = Client(
        project_id="my-project",
        dataset_id="agent_analytics",
    )

    # Retrieve and visualize a trace
    trace = client.get_trace("trace-123")
    trace.render()

    # Run evaluation
    from bigquery_agent_analytics import (
        CodeEvaluator, LLMAsJudge, TraceFilter,
    )
    report = client.evaluate(
        filters=TraceFilter(agent_id="my_agent"),
        evaluator=CodeEvaluator.latency(threshold_ms=3000),
    )
    print(report.summary())
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime
from datetime import timezone
import logging
import time
from typing import Any, Optional

from google.cloud import bigquery

from .categorical_evaluator import build_ai_classify_query
from .categorical_evaluator import build_ai_generate_query
from .categorical_evaluator import build_categorical_prompt
from .categorical_evaluator import build_categorical_report
from .categorical_evaluator import CATEGORICAL_AI_GENERATE_QUERY
from .categorical_evaluator import CATEGORICAL_RESULTS_DDL
from .categorical_evaluator import CATEGORICAL_TRANSCRIPT_QUERY
from .categorical_evaluator import CategoricalEvaluationConfig
from .categorical_evaluator import CategoricalEvaluationReport
from .categorical_evaluator import classify_sessions_via_api
from .categorical_evaluator import DEFAULT_RESULTS_TABLE
from .categorical_evaluator import flatten_results_to_rows
from .categorical_evaluator import parse_categorical_row
from .categorical_evaluator import parse_classify_row
from .evaluators import _parse_json_from_text
from .evaluators import AI_GENERATE_JUDGE_BATCH_QUERY
from .evaluators import CodeEvaluator
from .evaluators import DEFAULT_ENDPOINT
from .evaluators import EvaluationReport
from .evaluators import LLM_JUDGE_BATCH_QUERY
from .evaluators import LLMAsJudge
from .evaluators import SESSION_SUMMARY_QUERY
from .evaluators import SessionScore
from .feedback import AnalysisConfig
from .feedback import compute_drift
from .feedback import compute_question_distribution
from .feedback import DriftReport
from .feedback import QuestionDistribution
from .insights import _AI_GENERATE_FACET_EXTRACTION_QUERY
from .insights import _FACET_EXTRACTION_QUERY
from .insights import _SESSION_METADATA_QUERY
from .insights import _SESSION_TRANSCRIPT_QUERY
from .insights import aggregate_facets
from .insights import ANALYSIS_PROMPTS
from .insights import build_analysis_context
from .insights import build_facet_prompt
from .insights import extract_facets_via_api
from .insights import generate_executive_summary
from .insights import InsightsConfig
from .insights import InsightsReport
from .insights import parse_facet_from_ai_generate_row
from .insights import parse_facet_response
from .insights import run_analysis_prompt
from .insights import SessionFacet
from .insights import SessionMetadata
from .trace import Span
from .trace import Trace
from .trace import TraceFilter

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# SQL Templates                                                        #
# ------------------------------------------------------------------ #

_GET_TRACE_QUERY = """\
SELECT
  event_type,
  agent,
  timestamp,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  content,
  content_parts,
  attributes,
  latency_ms,
  status,
  error_message,
  is_truncated
FROM `{project}.{dataset}.{table}`
WHERE trace_id = @trace_id
ORDER BY timestamp ASC
"""

_LIST_TRACES_QUERY = """\
WITH trace_sessions AS (
  SELECT session_id
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  ORDER BY MAX(timestamp) DESC, session_id
  LIMIT @trace_limit
)
SELECT
  e.event_type,
  e.agent,
  e.timestamp,
  e.session_id,
  e.invocation_id,
  e.user_id,
  e.trace_id,
  e.span_id,
  e.parent_span_id,
  e.content,
  e.content_parts,
  e.attributes,
  e.latency_ms,
  e.status,
  e.error_message,
  e.is_truncated
FROM `{project}.{dataset}.{table}` e
JOIN trace_sessions ts ON e.session_id = ts.session_id
ORDER BY e.session_id, e.timestamp ASC
"""

_VERIFY_SCHEMA_QUERY = """\
SELECT column_name, data_type
FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = @table_name
"""

_REQUIRED_COLUMNS = {
    "timestamp",
    "event_type",
    "session_id",
    "content",
    "agent",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "attributes",
    "latency_ms",
    "status",
    "error_message",
    "content_parts",
    "is_truncated",
}

_TABLE_EXISTS_QUERY = """\
SELECT table_name
FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES`
WHERE table_name IN ('agent_events', 'agent_events_v2')
"""

_AUTO_DETECT_TABLES = ["agent_events", "agent_events_v2"]

_HITL_METRICS_QUERY = """\
WITH hitl_global AS (
  SELECT COUNT(DISTINCT session_id) AS global_hitl_sessions
  FROM `{project}.{dataset}.{table}`
  WHERE event_type LIKE 'HITL_%'
    AND {where}
),
hitl_by_type AS (
  SELECT
    event_type,
    COUNT(*) AS event_count,
    COUNT(DISTINCT session_id) AS session_count,
    AVG(
      CAST(
        JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64
      )
    ) AS avg_latency_ms
  FROM `{project}.{dataset}.{table}`
  WHERE event_type LIKE 'HITL_%'
    AND {where}
  GROUP BY event_type
)
SELECT
  g.global_hitl_sessions,
  t.*
FROM hitl_by_type t
CROSS JOIN hitl_global g
ORDER BY t.event_count DESC
"""

_EVENT_COVERAGE_QUERY = """\
SELECT
  event_type,
  COUNT(*) AS event_count
FROM `{project}.{dataset}.{table}`
WHERE {where}
GROUP BY event_type
ORDER BY event_count DESC
"""

_GET_SESSION_TRACE_QUERY = """\
SELECT
  event_type,
  agent,
  timestamp,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  content,
  content_parts,
  attributes,
  latency_ms,
  status,
  error_message,
  is_truncated
FROM `{project}.{dataset}.{table}`
WHERE session_id = @session_id
ORDER BY timestamp ASC
"""


def _run_sync(coro):
  """Runs a coroutine from synchronous code.

  Safe under already-running event loops (e.g. Jupyter notebooks,
  async applications).  Falls back to a thread-pool executor when
  a loop is already active.
  """
  try:
    loop = asyncio.get_running_loop()
  except RuntimeError:
    loop = None

  if loop and loop.is_running():
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
    ) as pool:
      return pool.submit(asyncio.run, coro).result()
  return asyncio.run(coro)


# ------------------------------------------------------------------ #
# Client                                                               #
# ------------------------------------------------------------------ #


class Client:
  """BigQuery Agent Analytics SDK client.

  Provides a high-level Python interface for analyzing agent traces
  stored in BigQuery. Abstracts away SQL complexity, UNNEST
  operations, and BQML mechanics.

  Args:
      project_id: Google Cloud project ID.
      dataset_id: BigQuery dataset containing agent events.
      table_id: Table name for agent events. Pass ``"auto"``
          to auto-detect (tries ``agent_events`` first, then
          ``agent_events_v2``).
      location: BigQuery dataset location. When *None* (default),
          the BigQuery client uses its own default (typically ``US``).
      gcs_bucket_name: Optional GCS bucket name (reserved for future
          GCS-offloaded payload resolution; not yet implemented).
      verify_schema: Whether to verify the table schema on init.
      endpoint: AI.GENERATE endpoint (default gemini-2.5-flash).
          Pass a fully-qualified BQ ML model reference
          (``project.dataset.model``) to use legacy
          ``ML.GENERATE_TEXT`` instead.
      connection_id: Optional BigQuery connection resource ID
          for AI.GENERATE.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      location: Optional[str] = None,
      gcs_bucket_name: Optional[str] = None,
      verify_schema: bool = True,
      bq_client: Optional[bigquery.Client] = None,
      endpoint: Optional[str] = None,
      connection_id: Optional[str] = None,
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.location = location
    self.gcs_bucket_name = gcs_bucket_name
    self._bq_client = bq_client
    self.endpoint = endpoint or DEFAULT_ENDPOINT
    self.connection_id = connection_id

    if table_id == "auto":
      self.table_id = self._detect_table()
    else:
      self.table_id = table_id

    self._table_ref = f"{project_id}.{dataset_id}.{self.table_id}"

    if verify_schema:
      self._verify_schema()

  @property
  def bq_client(self) -> bigquery.Client:
    """Lazily initializes the BigQuery client."""
    if self._bq_client is None:
      kwargs: dict = {"project": self.project_id}
      if self.location:
        kwargs["location"] = self.location
      self._bq_client = bigquery.Client(**kwargs)
    return self._bq_client

  # -------------------------------------------------------------- #
  # Schema Verification                                              #
  # -------------------------------------------------------------- #

  def _verify_schema(self) -> None:
    """Verifies the target table has expected columns."""
    try:
      query = _VERIFY_SCHEMA_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
      )
      job_config = bigquery.QueryJobConfig(
          query_parameters=[
              bigquery.ScalarQueryParameter(
                  "table_name",
                  "STRING",
                  self.table_id,
              ),
          ]
      )
      results = list(
          self.bq_client.query(query, job_config=job_config).result()
      )
      columns = {r.get("column_name") for r in results}

      missing = _REQUIRED_COLUMNS - columns
      if missing:
        logger.warning(
            "Table %s is missing columns: %s. Some SDK features may not work.",
            self._table_ref,
            missing,
        )
    except Exception as e:
      logger.warning(
          "Schema verification failed: %s. Continuing without verification.",
          e,
      )

  def _detect_table(self) -> str:
    """Auto-detects the events table name.

    Checks for ``agent_events`` first (current ADK plugin
    default), then ``agent_events_v2``.

    Returns:
        The detected table name.

    Raises:
        ValueError: If neither table exists.
    """
    try:
      query = _TABLE_EXISTS_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
      )
      rows = list(self.bq_client.query(query).result())
      existing = {r.get("table_name") for r in rows}

      for candidate in _AUTO_DETECT_TABLES:
        if candidate in existing:
          logger.info("Auto-detected events table: %s", candidate)
          return candidate

      raise ValueError(
          f"No events table found in "
          f"{self.project_id}.{self.dataset_id}. "
          f"Expected one of: {_AUTO_DETECT_TABLES}"
      )
    except Exception as e:
      if isinstance(e, ValueError):
        raise
      logger.warning(
          "Table auto-detection failed: %s. " "Falling back to 'agent_events'.",
          e,
      )
      return "agent_events"

  # -------------------------------------------------------------- #
  # Diagnostics                                                      #
  # -------------------------------------------------------------- #

  def doctor(
      self,
      filters: Optional[TraceFilter] = None,
  ) -> dict[str, Any]:
    """Runs diagnostic checks on the SDK configuration.

    Validates table schema, event type coverage, column
    completeness, and AI.GENERATE permissions. Returns a
    structured report with warnings and suggestions.

    Args:
        filters: Optional trace filters to scope the checks.

    Returns:
        Dict with diagnostic results::

            {
              "table": str,
              "schema": {"status": "ok"|"warning"|"error", ...},
              "event_coverage": {event_type: count, ...},
              "warnings": [str, ...],
              "ai_generate": {"status": "ok"|"unavailable", ...},
            }
    """
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()
    report: dict[str, Any] = {
        "table": self._table_ref,
        "warnings": [],
    }

    # 1. Schema check
    try:
      schema_query = _VERIFY_SCHEMA_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
      )
      job_config = bigquery.QueryJobConfig(
          query_parameters=[
              bigquery.ScalarQueryParameter(
                  "table_name",
                  "STRING",
                  self.table_id,
              ),
          ]
      )
      rows = list(
          self.bq_client.query(schema_query, job_config=job_config).result()
      )
      columns = {r.get("column_name") for r in rows}
      missing = _REQUIRED_COLUMNS - columns
      if missing:
        report["schema"] = {
            "status": "warning",
            "present": sorted(columns & _REQUIRED_COLUMNS),
            "missing": sorted(missing),
        }
        report["warnings"].append(f"Missing columns: {sorted(missing)}")
      else:
        report["schema"] = {
            "status": "ok",
            "columns": sorted(columns),
        }
    except Exception as e:
      report["schema"] = {"status": "error", "error": str(e)}
      report["warnings"].append(f"Schema check failed: {e}")

    # 2. Event coverage
    try:
      ev_query = _EVENT_COVERAGE_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          table=self.table_id,
          where=where,
      )
      ev_config = bigquery.QueryJobConfig(
          query_parameters=params,
      )
      ev_rows = list(
          self.bq_client.query(ev_query, job_config=ev_config).result()
      )
      coverage = {r.get("event_type"): r.get("event_count") for r in ev_rows}
      report["event_coverage"] = coverage

      expected = {
          "USER_MESSAGE_RECEIVED",
          "AGENT_STARTING",
          "AGENT_COMPLETED",
          "LLM_REQUEST",
          "LLM_RESPONSE",
          "TOOL_STARTING",
          "TOOL_COMPLETED",
          "INVOCATION_STARTING",
          "INVOCATION_COMPLETED",
      }
      missing_events = expected - set(coverage.keys())
      if missing_events:
        report["warnings"].append(
            f"No events for types: {sorted(missing_events)}"
        )
    except Exception as e:
      report["event_coverage"] = {"error": str(e)}
      report["warnings"].append(f"Event coverage check failed: {e}")

    # 3. AI.GENERATE availability
    report["ai_generate"] = {
        "endpoint": self.endpoint,
        "connection_id": self.connection_id,
        "is_legacy": self._is_legacy_model_ref(self.endpoint),
    }
    if self._is_legacy_model_ref(self.endpoint):
      report["warnings"].append(
          "Using legacy ML.GENERATE_TEXT model reference. "
          "Consider migrating to AI.GENERATE endpoints."
      )

    return report

  # -------------------------------------------------------------- #
  # HITL Analytics                                                   #
  # -------------------------------------------------------------- #

  def hitl_metrics(
      self,
      filters: Optional[TraceFilter] = None,
  ) -> dict[str, Any]:
    """Returns Human-in-the-Loop interaction metrics.

    Summarizes HITL event types: confirmation requests,
    credential requests, and input requests, with completion
    rates and average latency.

    Args:
        filters: Optional trace filters.

    Returns:
        Dict with HITL metrics::

            {
              "total_hitl_events": int,
              "total_hitl_sessions": int,
              "events": [{
                "event_type": str,
                "count": int,
                "sessions": int,
                "avg_latency_ms": float,
              }, ...],
              "completion_rates": {
                "confirmation": float,
                "credential": float,
                "input": float,
              },
            }
    """
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()

    query = _HITL_METRICS_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        where=where,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=params,
    )

    rows = list(self.bq_client.query(query, job_config=job_config).result())

    events = []
    request_counts: dict[str, int] = {}
    completed_counts: dict[str, int] = {}
    total_events = 0
    global_hitl_sessions = 0

    for row in rows:
      r = dict(row)
      et = r.get("event_type", "")
      count = r.get("event_count", 0)
      sessions = r.get("session_count", 0)
      total_events += count
      # Global distinct session count from the CROSS JOIN
      global_hitl_sessions = r.get("global_hitl_sessions", 0)

      events.append(
          {
              "event_type": et,
              "count": count,
              "sessions": sessions,
              "avg_latency_ms": float(r.get("avg_latency_ms") or 0),
          }
      )

      # Track request vs completed for completion rates
      for prefix in ("CONFIRMATION", "CREDENTIAL", "INPUT"):
        if et == f"HITL_{prefix}_REQUEST":
          request_counts[prefix.lower()] = count
        elif et == f"HITL_{prefix}_REQUEST_COMPLETED":
          completed_counts[prefix.lower()] = count

    completion_rates = {}
    for kind in ("confirmation", "credential", "input"):
      req = request_counts.get(kind, 0)
      comp = completed_counts.get(kind, 0)
      completion_rates[kind] = comp / req if req > 0 else 0.0

    return {
        "total_hitl_events": total_events,
        "total_hitl_sessions": global_hitl_sessions,
        "events": events,
        "completion_rates": completion_rates,
    }

  # -------------------------------------------------------------- #
  # Trace Retrieval                                                  #
  # -------------------------------------------------------------- #

  def get_trace(self, trace_id: str) -> Trace:
    """Fetches all spans for a specific trace by ``trace_id``.

    Use :meth:`get_session_trace` to query by ``session_id``
    instead.


    Args:
        trace_id: The trace ID to retrieve.

    Returns:
        A Trace object with all spans.

    Raises:
        ValueError: If no events found for the trace ID.
    """
    query = _GET_TRACE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "trace_id",
                "STRING",
                trace_id,
            ),
        ]
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    if not results:
      raise ValueError(f"No events found for trace_id={trace_id}")

    spans = [Span.from_bigquery_row(dict(row)) for row in results]

    # Determine trace metadata
    user_id = None
    session_id = None
    for row in results:
      if not user_id:
        user_id = row.get("user_id")
      if not session_id:
        session_id = row.get("session_id")

    timestamps = [s.timestamp for s in spans if s.timestamp]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    total_ms = None
    if start and end:
      total_ms = (end - start).total_seconds() * 1000

    return Trace(
        trace_id=trace_id,
        session_id=session_id or "",
        spans=spans,
        user_id=user_id,
        start_time=start,
        end_time=end,
        total_latency_ms=total_ms,
    )

  def get_session_trace(self, session_id: str) -> Trace:
    """Fetches all spans for a specific session by ``session_id``.

    Unlike :meth:`get_trace` which queries by ``trace_id``, this
    method filters by ``session_id``.

    Args:
        session_id: The session ID to retrieve.

    Returns:
        A Trace object with all spans for the session.

    Raises:
        ValueError: If no events found for the session ID.
    """
    query = _GET_SESSION_TRACE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "session_id",
                "STRING",
                session_id,
            ),
        ]
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    if not results:
      raise ValueError(f"No events found for session_id={session_id}")

    spans = [Span.from_bigquery_row(dict(row)) for row in results]

    user_id = None
    trace_id = None
    for row in results:
      if not user_id:
        user_id = row.get("user_id")
      if not trace_id:
        trace_id = row.get("trace_id")

    timestamps = [s.timestamp for s in spans if s.timestamp]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    total_ms = None
    if start and end:
      total_ms = (end - start).total_seconds() * 1000

    return Trace(
        trace_id=trace_id or session_id,
        session_id=session_id,
        spans=spans,
        user_id=user_id,
        start_time=start,
        end_time=end,
        total_latency_ms=total_ms,
    )

  def list_traces(
      self,
      filter_criteria: Optional[TraceFilter] = None,
  ) -> list[Trace]:
    """Lists traces matching the given filter criteria.

    Args:
        filter_criteria: Optional filter. If None, returns
            recent traces (default limit 100).

    Returns:
        List of Trace objects, one per session.
    """
    filt = filter_criteria or TraceFilter()
    where, params = filt.to_sql_conditions()

    query = _LIST_TRACES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        where=where,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=params,
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())
    return _build_traces_from_rows(results)

  # -------------------------------------------------------------- #
  # Evaluation                                                       #
  # -------------------------------------------------------------- #

  def evaluate(
      self,
      evaluator: CodeEvaluator | LLMAsJudge,
      filters: Optional[TraceFilter] = None,
      dataset: Optional[str] = None,
      strict: bool = False,
  ) -> EvaluationReport:
    """Runs batch evaluation over traces.

    Uses BigQuery native execution for scalable assessment.
    ``CodeEvaluator`` metrics are computed from session
    aggregates. ``LLMAsJudge`` metrics use BQML's
    ``ML.GENERATE_TEXT`` for zero-ETL evaluation.

    Args:
        evaluator: A CodeEvaluator or LLMAsJudge instance.
        filters: Optional trace filters.
        dataset: Optional table name override.
        strict: When ``True``, sessions with unparseable or
            empty judge output are marked as failed instead of
            silently passing.  Affected sessions get
            ``parse_error: True`` in their per-session details,
            and report-level ``details`` includes
            ``parse_errors`` (int) and ``parse_error_rate``
            (float) — separate from ``aggregate_scores``.

    Returns:
        EvaluationReport with per-session and aggregate scores.
    """
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()

    if isinstance(evaluator, CodeEvaluator):
      return self._evaluate_code(
          evaluator,
          table,
          where,
          params,
      )
    elif isinstance(evaluator, LLMAsJudge):
      report = self._evaluate_llm_judge(
          evaluator,
          table,
          where,
          params,
          filt,
      )
      if strict:
        report = _apply_strict_mode(report)
      return report
    else:
      raise TypeError(f"Unsupported evaluator type: {type(evaluator)}")

  def _evaluate_code(
      self,
      evaluator: CodeEvaluator,
      table: str,
      where: str,
      params: list,
  ) -> EvaluationReport:
    """Runs code-based evaluation using session summaries."""
    query = SESSION_SUMMARY_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=params,
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    session_scores = []
    for row in results:
      summary = dict(row)
      score = evaluator.evaluate_session(summary)
      session_scores.append(score)

    return _build_report(
        evaluator_name=evaluator.name,
        dataset=f"{self._table_ref} WHERE {where}",
        session_scores=session_scores,
    )

  @staticmethod
  def _is_legacy_model_ref(ref: str) -> bool:
    """Returns True when *ref* looks like a BQ ML model reference.

    Legacy model references have the form
    ``project.dataset.model_name`` (two or more dots).
    """
    return ref.count(".") >= 2

  def _evaluate_llm_judge(
      self,
      evaluator: LLMAsJudge,
      table: str,
      where: str,
      params: list,
      trace_filter: Optional[TraceFilter] = None,
  ) -> EvaluationReport:
    """Runs LLM-as-judge evaluation over ALL criteria.

    Attempts AI.GENERATE first, then legacy ML.GENERATE_TEXT,
    then falls back to the Gemini API.  Each path evaluates
    every criterion in the evaluator and merges the per-session
    scores into a single report.
    """
    criteria = evaluator._criteria
    if not criteria:
      return _build_report(
          evaluator_name=evaluator.name,
          dataset=f"{self._table_ref} WHERE {where}",
          session_scores=[],
      )

    # Try AI.GENERATE (new path) when endpoint is not a legacy ref
    if not self._is_legacy_model_ref(self.endpoint):
      try:
        criterion_reports = []
        for criterion in criteria:
          report = self._ai_generate_judge(
              evaluator,
              criterion,
              table,
              where,
              params,
          )
          criterion_reports.append((criterion, report))
        return _merge_criterion_reports(
            evaluator.name,
            f"{self._table_ref} WHERE {where}",
            criteria,
            criterion_reports,
        )
      except Exception as e:
        logger.debug(
            "AI.GENERATE judge failed, trying legacy: %s",
            e,
        )

    # Try legacy BQML batch evaluation
    text_model = (
        self.endpoint
        if self._is_legacy_model_ref(self.endpoint)
        else (f"{self.project_id}.{self.dataset_id}.gemini_text_model")
    )

    try:
      criterion_reports = []
      for criterion in criteria:
        report = self._bqml_judge(
            evaluator,
            criterion,
            table,
            where,
            params,
            text_model,
        )
        criterion_reports.append((criterion, report))
      return _merge_criterion_reports(
          evaluator.name,
          f"{self._table_ref} WHERE {where}",
          criteria,
          criterion_reports,
      )
    except Exception as e:
      logger.debug(
          "BQML judge failed, falling back to API: %s",
          e,
      )

    # Fallback: fetch traces using same table/filter, evaluate via API
    return self._api_judge(evaluator, table, where, params)

  def _ai_generate_judge(
      self,
      evaluator,
      criterion,
      table,
      where,
      params,
  ) -> EvaluationReport:
    """Evaluates using BigQuery AI.GENERATE with typed output."""
    from google.cloud import bigquery as bq

    judge_params = list(params) + [
        bq.ScalarQueryParameter(
            "judge_prompt",
            "STRING",
            criterion.prompt_template.split("{trace_text}")[0],
        ),
    ]

    query = AI_GENERATE_JUDGE_BATCH_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
        endpoint=self.endpoint,
    )
    job_config = bq.QueryJobConfig(
        query_parameters=judge_params,
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    session_scores = []
    for row in results:
      sid = row.get("session_id", "unknown")
      raw_score = row.get("score")
      justification = row.get("justification", "")

      scores: dict[str, float] = {}
      if raw_score is not None:
        scores[criterion.name] = max(
            0.0,
            min(1.0, float(raw_score) / 10.0),
        )

      passed = bool(scores) and all(
          s >= criterion.threshold for s in scores.values()
      )
      session_scores.append(
          SessionScore(
              session_id=sid,
              scores=scores,
              passed=passed,
              llm_feedback=justification,
          )
      )

    return _build_report(
        evaluator_name=evaluator.name,
        dataset=f"{self._table_ref} WHERE {where}",
        session_scores=session_scores,
    )

  def _bqml_judge(
      self,
      evaluator,
      criterion,
      table,
      where,
      params,
      text_model,
  ) -> EvaluationReport:
    """Evaluates using BigQuery ML.GENERATE_TEXT."""
    from google.cloud import bigquery as bq

    judge_params = list(params) + [
        bq.ScalarQueryParameter(
            "judge_prompt",
            "STRING",
            criterion.prompt_template.split("{trace_text}")[0],
        ),
    ]

    query = LLM_JUDGE_BATCH_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
        model=text_model,
    )
    job_config = bq.QueryJobConfig(
        query_parameters=judge_params,
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    session_scores = []
    for row in results:
      sid = row.get("session_id", "unknown")
      eval_text = row.get("evaluation", "")
      parsed = _parse_json_from_text(eval_text or "")

      scores: dict[str, float] = {}
      if parsed and criterion.score_key in parsed:
        raw = float(parsed[criterion.score_key])
        scores[criterion.name] = raw / 10.0
      elif parsed:
        for k, v in parsed.items():
          if isinstance(v, (int, float)):
            scores[k] = float(v) / 10.0

      passed = bool(scores) and all(
          s >= criterion.threshold for s in scores.values()
      )
      session_scores.append(
          SessionScore(
              session_id=sid,
              scores=scores,
              passed=passed,
              llm_feedback=(
                  parsed.get("justification", "") if parsed else eval_text
              ),
          )
      )

    return _build_report(
        evaluator_name=evaluator.name,
        dataset=f"{self._table_ref} WHERE {where}",
        session_scores=session_scores,
    )

  def _api_judge(
      self,
      evaluator,
      table,
      where,
      params,
  ) -> EvaluationReport:
    """Evaluates using the Gemini API (fallback).

    Fetches traces from the same table and filter as the BQ
    evaluation paths, then evaluates each session via the
    Gemini API.
    """
    query = _LIST_TRACES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
    )
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    results = list(self.bq_client.query(query, job_config=job_config).result())
    traces = _build_traces_from_rows(results)

    session_scores = _run_sync(self._run_api_judge(evaluator, traces))

    return _build_report(
        evaluator_name=evaluator.name,
        dataset=f"{self._table_ref} WHERE {where}",
        session_scores=session_scores,
    )

  async def _run_api_judge(
      self,
      evaluator: LLMAsJudge,
      traces: list[Trace],
  ) -> list[SessionScore]:
    """Runs LLM judge via API for each trace."""
    scores = []
    for trace in traces:
      trace_lines = []
      for span in trace.spans:
        trace_lines.append(f"{span.event_type}: {span.summary}")
      trace_text = "\n".join(trace_lines)
      final = trace.final_response or ""

      score = await evaluator.evaluate_session(
          trace_text,
          final,
      )
      score.session_id = trace.session_id
      scores.append(score)

    return scores

  # -------------------------------------------------------------- #
  # Categorical Evaluation                                            #
  # -------------------------------------------------------------- #

  def evaluate_categorical(
      self,
      config: CategoricalEvaluationConfig,
      filters: Optional[TraceFilter] = None,
      dataset: Optional[str] = None,
  ) -> CategoricalEvaluationReport:
    """Runs categorical evaluation over traces.

    Execution cascade:

    * When ``include_justification=False``:
      AI.CLASSIFY → AI.GENERATE → Gemini API
    * When ``include_justification=True`` (default):
      AI.GENERATE → Gemini API

    Args:
        config: Categorical evaluation configuration with metric
            definitions and allowed categories.
        filters: Optional trace filters.
        dataset: Optional table name override.

    Returns:
        CategoricalEvaluationReport with per-session results and
        category distributions.
    """
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()

    # Endpoint precedence: config.endpoint wins when explicitly set.
    # When config uses the default, fall back to client.endpoint —
    # but guard against legacy BQML model refs which are incompatible
    # with AI.GENERATE.
    _default_ep = CategoricalEvaluationConfig.model_fields["endpoint"].default
    if config.endpoint != _default_ep:
      endpoint = config.endpoint
    elif self._is_legacy_model_ref(self.endpoint):
      endpoint = _default_ep
    else:
      endpoint = self.endpoint

    # Resolve connection_id: config wins over client.
    connection_id = config.connection_id or self.connection_id

    table_ref = f"{self.project_id}.{self.dataset_id}.{table}"
    classify_fallback_reason = None
    fallback_reason = None

    # When justification is not needed, try AI.CLASSIFY first.
    if not config.include_justification:
      try:
        session_results, classify_null_count = self._categorical_ai_classify(
            config,
            table,
            where,
            params,
            endpoint,
            connection_id,
        )
        report = build_categorical_report(
            dataset=f"{table_ref} WHERE {where}",
            session_results=session_results,
            config=config,
        )
        report.details["execution_mode"] = "ai_classify"
        report.details["classify_null_count"] = classify_null_count
        self._persist_categorical_if_configured(report, config, endpoint)
        return report
      except Exception as e:
        logger.debug(
            "AI.CLASSIFY categorical failed, falling back to "
            "AI.GENERATE: %s",
            e,
        )
        classify_fallback_reason = str(e)

    # Try AI.GENERATE.
    try:
      session_results, retry_meta = self._categorical_ai_generate(
          config,
          table,
          where,
          params,
          endpoint,
          connection_id,
      )
      report = build_categorical_report(
          dataset=f"{table_ref} WHERE {where}",
          session_results=session_results,
          config=config,
      )
      report.details["execution_mode"] = "ai_generate"
      if retry_meta:
        report.details["retry"] = retry_meta
      if classify_fallback_reason:
        report.details["classify_fallback_reason"] = classify_fallback_reason
      self._persist_categorical_if_configured(report, config, endpoint)
      return report
    except Exception as e:
      logger.debug(
          "AI.GENERATE categorical failed, falling back to API: %s",
          e,
      )
      fallback_reason = str(e)

    # Fallback: Gemini API.
    try:
      session_results = self._categorical_api_fallback(
          config,
          table,
          where,
          params,
          endpoint,
      )
      report = build_categorical_report(
          dataset=f"{table_ref} WHERE {where}",
          session_results=session_results,
          config=config,
      )
      report.details["execution_mode"] = "api_fallback"
      report.details["fallback_reason"] = fallback_reason
      if classify_fallback_reason:
        report.details["classify_fallback_reason"] = classify_fallback_reason
      self._persist_categorical_if_configured(report, config, endpoint)
      return report
    except ImportError:
      # google-genai not installed — API fallback is unavailable.
      report = build_categorical_report(
          dataset=f"{table_ref} WHERE {where}",
          session_results=[],
          config=config,
      )
      report.details["execution_mode"] = "api_unavailable"
      report.details["fallback_reason"] = fallback_reason
      report.details["api_error"] = "google-genai not installed"
      return report

  def _categorical_ai_classify(
      self,
      config: CategoricalEvaluationConfig,
      table: str,
      where: str,
      params: list,
      endpoint: str,
      connection_id: Optional[str] = None,
  ) -> tuple[list, int]:
    """Classifies sessions using BigQuery AI.CLASSIFY.

    Returns:
        Tuple of (session_results, total_null_count).
    """
    query = build_ai_classify_query(
        config=config,
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
        endpoint=endpoint,
        connection_id=connection_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=list(params),
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    session_results = []
    total_null_count = 0
    for row in results:
      r = dict(row)
      sid = r.get("session_id", "unknown")
      sr, null_count = parse_classify_row(sid, r, config)
      session_results.append(sr)
      total_null_count += null_count
    return session_results, total_null_count

  def _categorical_ai_generate(
      self,
      config: CategoricalEvaluationConfig,
      table: str,
      where: str,
      params: list,
      endpoint: str,
      connection_id: Optional[str] = None,
  ) -> tuple[list, dict]:
    """Classifies sessions using BigQuery AI.GENERATE.

    Sessions where AI.GENERATE returns NULL (e.g. due to rate
    limiting or transient errors) are retried via the Gemini API
    up to 3 times.
    """
    prompt = build_categorical_prompt(config)

    query = build_ai_generate_query(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
        endpoint=endpoint,
        temperature=config.temperature,
        connection_id=connection_id,
        max_output_tokens=config.max_output_tokens,
    )

    query_params = list(params) + [
        bigquery.ScalarQueryParameter(
            "categorical_prompt",
            "STRING",
            prompt,
        ),
    ]
    job_config = bigquery.QueryJobConfig(
        query_parameters=query_params,
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    session_results = []
    failed_sessions = {}
    for row in results:
      r = dict(row)
      sid = r.get("session_id", "unknown")
      parsed = parse_categorical_row(sid, r, config)
      has_parse_error = any(m.parse_error for m in parsed.metrics)
      if has_parse_error and r.get("transcript"):
        failed_sessions[sid] = r.get("transcript", "")
      session_results.append(parsed)

    retry_meta = {}
    if failed_sessions:
      logger.warning(
          "AI.GENERATE returned NULL/unparseable for %d session(s), "
          "retrying via Gemini API: %s",
          len(failed_sessions),
          ", ".join(failed_sessions.keys()),
      )
      retried = self._retry_failed_sessions(
          failed_sessions,
          config,
          endpoint,
          max_retries=3,
      )
      resolved = 0
      if retried:
        retried_map = {r.session_id: r for r in retried}
        session_results = [
            retried_map.get(sr.session_id, sr) for sr in session_results
        ]
        resolved = sum(
            1 for r in retried if not any(m.parse_error for m in r.metrics)
        )
        logger.info(
            "Gemini API retry resolved %d/%d failed sessions",
            resolved,
            len(failed_sessions),
        )
      retry_meta = {
          "failed_count": len(failed_sessions),
          "retry_attempted": True,
          "retry_resolved": resolved,
          "retry_unresolved": len(failed_sessions) - resolved,
      }

    return session_results, retry_meta

  def _retry_failed_sessions(
      self,
      transcripts: dict[str, str],
      config: CategoricalEvaluationConfig,
      endpoint: str,
      max_retries: int = 3,
  ) -> list:
    """Retries classification for failed sessions via Gemini API.

    Note: This method is synchronous and must not be called from
    an async context with an already-running event loop.

    Args:
        transcripts: Maps session_id to transcript text.
        config: Evaluation config.
        endpoint: Model endpoint.
        max_retries: Maximum number of retry attempts.

    Returns:
        List of CategoricalSessionResult for successfully retried
        sessions.
    """
    remaining = dict(transcripts)
    all_results = {}

    for attempt in range(1, max_retries + 1):
      if not remaining:
        break
      if attempt > 1:
        backoff = 2 ** (attempt - 2)
        logger.info(
            "Retry backoff: sleeping %ds before attempt %d", backoff, attempt
        )
        time.sleep(backoff)
      try:
        results = _run_sync(
            classify_sessions_via_api(remaining, config, endpoint)
        )
        still_failed = {}
        for r in results:
          has_error = any(m.parse_error for m in r.metrics)
          if has_error:
            if r.session_id in remaining:
              still_failed[r.session_id] = remaining[r.session_id]
              for m in r.metrics:
                if m.parse_error:
                  logger.warning(
                      "Retry attempt %d, session %s, metric %s: "
                      "parse_error=True, raw_response=%s",
                      attempt,
                      r.session_id,
                      m.metric_name,
                      repr(m.raw_response[:500] if m.raw_response else None),
                  )
                  break
          else:
            all_results[r.session_id] = r
        remaining = still_failed
        if remaining:
          logger.warning(
              "Retry attempt %d: %d sessions still unresolved",
              attempt,
              len(remaining),
          )
      except Exception as e:  # Broad catch: retry loop logs + continues
        logger.warning(
            "Gemini API retry attempt %d failed: %s (type=%s)",
            attempt,
            e,
            type(e).__name__,
        )

    if remaining:
      logger.warning(
          "%d sessions still unresolved after %d retries",
          len(remaining),
          max_retries,
      )

    return list(all_results.values())

  def _categorical_api_fallback(
      self,
      config: CategoricalEvaluationConfig,
      table: str,
      where: str,
      params: list,
      endpoint: str,
  ) -> list:
    """Classifies sessions using the Gemini API (fallback).

    Fetches transcripts from BigQuery using the same
    transcript-building CTE as the ``AI.GENERATE`` path,
    then classifies each session via the Gemini API.
    """
    query = CATEGORICAL_TRANSCRIPT_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
    )
    job_config = bigquery.QueryJobConfig(query_parameters=list(params))
    rows = list(self.bq_client.query(query, job_config=job_config).result())

    transcripts = {}
    for row in rows:
      r = dict(row)
      sid = r.get("session_id", "unknown")
      transcripts[sid] = r.get("transcript", "")

    return _run_sync(classify_sessions_via_api(transcripts, config, endpoint))

  def _persist_categorical_if_configured(
      self,
      report: CategoricalEvaluationReport,
      config: CategoricalEvaluationConfig,
      endpoint: str,
  ) -> None:
    """Persists categorical results to BigQuery when configured.

    Creates the results table if it does not exist, flattens
    session results to one row per ``(session_id, metric_name)``,
    and writes via streaming insert.
    """
    if not config.persist_results:
      return
    if not report.session_results:
      report.details["persisted"] = False
      report.details["persist_note"] = "no sessions to persist"
      return

    results_table = config.results_table or DEFAULT_RESULTS_TABLE

    try:
      ddl = CATEGORICAL_RESULTS_DDL.format(
          project=self.project_id,
          dataset=self.dataset_id,
          results_table=results_table,
      )
      self.bq_client.query(ddl).result()

      rows = flatten_results_to_rows(report, config, endpoint)
      table_ref = f"{self.project_id}.{self.dataset_id}.{results_table}"
      errors = self.bq_client.insert_rows_json(table_ref, rows)
      if errors:
        logger.error(
            "Failed to persist categorical results: %s",
            errors,
        )
        report.details["persisted"] = False
        report.details["persist_error"] = str(errors)
      else:
        logger.info(
            "Persisted %d categorical result rows to %s",
            len(rows),
            table_ref,
        )
        report.details["persisted"] = True
        report.details["persisted_rows"] = len(rows)
        report.details["results_table"] = table_ref
    except Exception as e:
      logger.warning(
          "Failed to persist categorical results: %s",
          e,
      )
      report.details["persisted"] = False
      report.details["persist_error"] = str(e)

  # -------------------------------------------------------------- #
  # Categorical Views                                                #
  # -------------------------------------------------------------- #

  def create_categorical_views(
      self,
      results_table: Optional[str] = None,
      view_prefix: str = "",
  ) -> dict[str, str]:
    """Creates dashboard views over categorical evaluation results.

    Delegates to :class:`CategoricalViewManager` to create a dedup
    base view and aggregated dashboard views.

    Args:
        results_table: Results table name. Defaults to
            ``categorical_results``.
        view_prefix: Optional prefix for view names.

    Returns:
        A dict mapping view name to prefixed view name.
    """
    from .categorical_views import CategoricalViewManager

    vm = CategoricalViewManager(
        project_id=self.project_id,
        dataset_id=self.dataset_id,
        results_table=results_table or DEFAULT_RESULTS_TABLE,
        view_prefix=view_prefix,
        location=self.location,
        bq_client=self.bq_client,
    )
    return vm.create_all_views()

  # -------------------------------------------------------------- #
  # Feedback & Curation                                              #
  # -------------------------------------------------------------- #

  def drift_detection(
      self,
      golden_dataset: str,
      filters: Optional[TraceFilter] = None,
      dataset: Optional[str] = None,
      embedding_model: Optional[str] = None,
  ) -> DriftReport:
    """Detects drift between golden dataset and production.

    Compares golden questions against production traces to
    determine coverage percentage and identify gaps.

    Args:
        golden_dataset: Table name containing golden questions
            (must have a ``question`` column).
        filters: Optional filters for production traces.
        dataset: Optional events table override.
        embedding_model: Optional model for semantic matching.

    Returns:
        DriftReport with coverage metrics.
    """
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()

    return _run_sync(
        compute_drift(
            bq_client=self.bq_client,
            project_id=self.project_id,
            dataset_id=self.dataset_id,
            table_id=table,
            golden_table=golden_dataset,
            where_clause=where,
            query_params=params,
            embedding_model=embedding_model,
        )
    )

  # -------------------------------------------------------------- #
  # Insights                                                         #
  # -------------------------------------------------------------- #

  def insights(
      self,
      filters: Optional[TraceFilter] = None,
      config: Optional[InsightsConfig] = None,
      dataset: Optional[str] = None,
      text_model: Optional[str] = None,
  ) -> InsightsReport:
    """Generates a comprehensive insights report.

    Runs a multi-stage pipeline:
    1. Session filtering and metadata extraction.
    2. Per-session facet extraction via LLM.
    3. Aggregation across sessions.
    4. Multi-prompt analysis.
    5. Executive summary generation.

    Args:
        filters: Optional trace filters.
        config: Insights configuration. Defaults to
            analyzing up to 50 recent sessions.
        dataset: Optional events table override.
        text_model: Optional BQML text model.

    Returns:
        InsightsReport with facets, analysis, and summary.
    """
    return _run_sync(
        self._run_insights(
            filters=filters,
            config=config,
            dataset=dataset,
            text_model=text_model,
        )
    )

  async def _run_insights(
      self,
      filters: Optional[TraceFilter] = None,
      config: Optional[InsightsConfig] = None,
      dataset: Optional[str] = None,
      text_model: Optional[str] = None,
  ) -> InsightsReport:
    """Async implementation of the insights pipeline."""
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    cfg = config or InsightsConfig()
    model = text_model or self.endpoint

    where, params = filt.to_sql_conditions()

    # Step 1: Extract session metadata
    metadata_list = await self._fetch_session_metadata(
        table,
        where,
        params,
        cfg,
    )

    if not metadata_list:
      return InsightsReport(config=cfg)

    session_ids = [m.session_id for m in metadata_list]

    # Step 2: Extract facets
    facets = await self._extract_facets(
        table,
        session_ids,
        model,
    )

    # Step 3: Aggregate
    agg = aggregate_facets(facets, metadata_list)

    # Step 4: Multi-prompt analysis
    context = build_analysis_context(
        agg,
        facets,
        metadata_list,
    )
    prompt_names = cfg.analysis_prompts or list(ANALYSIS_PROMPTS.keys())
    sections = []
    for name in prompt_names:
      section = await run_analysis_prompt(
          name,
          context,
          model="gemini-2.5-flash",
      )
      sections.append(section)

    # Step 5: Executive summary
    report = InsightsReport(
        config=cfg,
        session_facets=facets,
        session_metadata=metadata_list,
        aggregated=agg,
        analysis_sections=sections,
    )
    report.executive_summary = await generate_executive_summary(report)

    return report

  async def _fetch_session_metadata(
      self,
      table: str,
      where: str,
      params: list,
      config: InsightsConfig,
  ) -> list[SessionMetadata]:
    """Fetches session metadata from BigQuery."""
    from google.cloud import bigquery as bq

    loop = asyncio.get_event_loop()

    extra_params = list(params) + [
        bq.ScalarQueryParameter(
            "min_events",
            "INT64",
            config.min_events_per_session,
        ),
        bq.ScalarQueryParameter(
            "min_turns",
            "INT64",
            config.min_turns_per_session,
        ),
        bq.ScalarQueryParameter(
            "max_sessions",
            "INT64",
            config.max_sessions,
        ),
    ]

    query = _SESSION_METADATA_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
    )
    job_config = bq.QueryJobConfig(
        query_parameters=extra_params,
    )

    job = await loop.run_in_executor(
        None,
        lambda: self.bq_client.query(query, job_config=job_config),
    )
    rows = await loop.run_in_executor(
        None,
        lambda: list(job.result()),
    )

    result = []
    for row in rows:
      r = dict(row)
      result.append(
          SessionMetadata(
              session_id=r.get("session_id", ""),
              event_count=r.get("event_count", 0),
              tool_calls=r.get("tool_calls", 0),
              tool_errors=r.get("tool_errors", 0),
              llm_calls=r.get("llm_calls", 0),
              turn_count=r.get("turn_count", 0),
              total_latency_ms=float(r.get("total_latency_ms") or 0),
              avg_latency_ms=float(r.get("avg_latency_ms") or 0),
              agents_used=r.get("agents_used") or [],
              tools_used=r.get("tools_used") or [],
              has_error=bool(r.get("has_error")),
              hitl_events=int(r.get("hitl_events") or 0),
              state_changes=int(r.get("state_changes") or 0),
              start_time=r.get("start_time"),
              end_time=r.get("end_time"),
          )
      )
    return result

  async def _extract_facets(
      self,
      table: str,
      session_ids: list[str],
      text_model: str,
  ) -> list[SessionFacet]:
    """Extracts facets via AI.GENERATE, BQML, or API fallback."""
    # Try AI.GENERATE first (when not a legacy model ref)
    if not self._is_legacy_model_ref(self.endpoint):
      try:
        return await self._extract_facets_ai_generate(
            table,
            session_ids,
        )
      except Exception as e:
        logger.debug(
            "AI.GENERATE facet extraction failed: %s",
            e,
        )

    # Try legacy BQML batch extraction
    try:
      return await self._extract_facets_bqml(
          table,
          session_ids,
          text_model,
      )
    except Exception as e:
      logger.debug(
          "BQML facet extraction failed, falling back to API: %s",
          e,
      )

    # Fallback: fetch transcripts, extract via API
    transcripts = await self._fetch_transcripts(
        table,
        session_ids,
    )
    return await extract_facets_via_api(transcripts)

  async def _extract_facets_ai_generate(
      self,
      table: str,
      session_ids: list[str],
  ) -> list[SessionFacet]:
    """Extracts facets using AI.GENERATE with typed output."""
    from google.cloud import bigquery as bq

    loop = asyncio.get_event_loop()

    facet_prompt = build_facet_prompt()
    query = _AI_GENERATE_FACET_EXTRACTION_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        endpoint=self.endpoint,
    )
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ArrayQueryParameter(
                "session_ids",
                "STRING",
                session_ids,
            ),
            bq.ScalarQueryParameter(
                "facet_prompt",
                "STRING",
                facet_prompt,
            ),
        ],
    )

    job = await loop.run_in_executor(
        None,
        lambda: self.bq_client.query(query, job_config=job_config),
    )
    rows = await loop.run_in_executor(
        None,
        lambda: list(job.result()),
    )

    facets = []
    for row in rows:
      r = dict(row)
      sid = r.get("session_id", "")
      facets.append(parse_facet_from_ai_generate_row(sid, r))
    return facets

  async def _extract_facets_bqml(
      self,
      table: str,
      session_ids: list[str],
      text_model: str,
  ) -> list[SessionFacet]:
    """Extracts facets using legacy ML.GENERATE_TEXT."""
    from google.cloud import bigquery as bq

    loop = asyncio.get_event_loop()

    facet_prompt = build_facet_prompt()
    query = _FACET_EXTRACTION_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        model=text_model,
    )
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ArrayQueryParameter(
                "session_ids",
                "STRING",
                session_ids,
            ),
            bq.ScalarQueryParameter(
                "facet_prompt",
                "STRING",
                facet_prompt,
            ),
        ],
    )

    job = await loop.run_in_executor(
        None,
        lambda: self.bq_client.query(query, job_config=job_config),
    )
    rows = await loop.run_in_executor(
        None,
        lambda: list(job.result()),
    )

    facets = []
    for row in rows:
      r = dict(row)
      sid = r.get("session_id", "")
      raw = r.get("facets_json", "")
      facets.append(parse_facet_response(sid, raw or ""))
    return facets

  async def _fetch_transcripts(
      self,
      table: str,
      session_ids: list[str],
  ) -> dict[str, str]:
    """Fetches session transcripts from BigQuery."""
    from google.cloud import bigquery as bq

    loop = asyncio.get_event_loop()

    query = _SESSION_TRANSCRIPT_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
    )
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ArrayQueryParameter(
                "session_ids",
                "STRING",
                session_ids,
            ),
        ],
    )

    job = await loop.run_in_executor(
        None,
        lambda: self.bq_client.query(query, job_config=job_config),
    )
    rows = await loop.run_in_executor(
        None,
        lambda: list(job.result()),
    )

    return {
        dict(row).get("session_id", ""): dict(row).get("transcript", "")
        for row in rows
    }

  def deep_analysis(
      self,
      filters: Optional[TraceFilter] = None,
      configuration: Optional[AnalysisConfig] = None,
      dataset: Optional[str] = None,
      text_model: Optional[str] = None,
  ) -> QuestionDistribution:
    """Performs deep analysis of question distribution.

    Supports modes: ``frequently_asked``,
    ``frequently_unanswered``,
    ``auto_group_using_semantics``, or custom categories.

    Args:
        filters: Optional filters for production traces.
        configuration: Analysis configuration. Defaults to
            ``auto_group_using_semantics``.
        dataset: Optional events table override.
        text_model: Optional BQML text model for classification.

    Returns:
        QuestionDistribution with categorized results.
    """
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()
    config = configuration or AnalysisConfig()

    model = text_model or self.endpoint

    return _run_sync(
        compute_question_distribution(
            bq_client=self.bq_client,
            project_id=self.project_id,
            dataset_id=self.dataset_id,
            table_id=table,
            where_clause=where,
            query_params=params,
            config=config,
            text_model=model,
        )
    )

  # -------------------------------------------------------------- #
  # Async Public APIs                                                #
  # -------------------------------------------------------------- #

  async def insights_async(
      self,
      filters: Optional[TraceFilter] = None,
      config: Optional[InsightsConfig] = None,
      dataset: Optional[str] = None,
      text_model: Optional[str] = None,
  ) -> InsightsReport:
    """Async version of :meth:`insights`."""
    return await self._run_insights(
        filters=filters,
        config=config,
        dataset=dataset,
        text_model=text_model,
    )

  async def drift_detection_async(
      self,
      golden_dataset: str,
      filters: Optional[TraceFilter] = None,
      dataset: Optional[str] = None,
      embedding_model: Optional[str] = None,
  ) -> DriftReport:
    """Async version of :meth:`drift_detection`."""
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()

    return await compute_drift(
        bq_client=self.bq_client,
        project_id=self.project_id,
        dataset_id=self.dataset_id,
        table_id=table,
        golden_table=golden_dataset,
        where_clause=where,
        query_params=params,
        embedding_model=embedding_model,
    )

  async def deep_analysis_async(
      self,
      filters: Optional[TraceFilter] = None,
      configuration: Optional[AnalysisConfig] = None,
      dataset: Optional[str] = None,
      text_model: Optional[str] = None,
  ) -> QuestionDistribution:
    """Async version of :meth:`deep_analysis`."""
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()
    config = configuration or AnalysisConfig()
    model = text_model or self.endpoint

    return await compute_question_distribution(
        bq_client=self.bq_client,
        project_id=self.project_id,
        dataset_id=self.dataset_id,
        table_id=table,
        where_clause=where,
        query_params=params,
        config=config,
        text_model=model,
    )

  # -------------------------------------------------------------- #
  # Context Graph                                                    #
  # -------------------------------------------------------------- #

  def context_graph(
      self,
      config: Optional[Any] = None,
  ) -> Any:
    """Returns a :class:`ContextGraphManager` bound to this client.

    The manager provides Property Graph DDL generation, business
    entity extraction via ``AI.GENERATE``, GQL traversal, and
    world-change detection.

    Args:
        config: Optional :class:`ContextGraphConfig`. When *None*,
            default settings are used.

    Returns:
        A :class:`ContextGraphManager` instance.
    """
    from .context_graph import ContextGraphConfig
    from .context_graph import ContextGraphManager

    cfg = config or ContextGraphConfig(endpoint=self.endpoint)
    return ContextGraphManager(
        project_id=self.project_id,
        dataset_id=self.dataset_id,
        table_id=self.table_id,
        config=cfg,
        client=self.bq_client,
        location=self.location,
    )

  def get_session_trace_gql(
      self,
      session_id: str,
      config: Optional[Any] = None,
  ) -> Trace:
    """Reconstructs a session trace using GQL graph traversal.

    This is the Property Graph alternative to :meth:`get_session_trace`.
    Instead of a flat SQL query, it walks the ``Caused`` edges in the
    Property Graph to reconstruct the parent→child span tree natively.

    Requires a Property Graph to have been created via
    :meth:`ContextGraphManager.create_property_graph`.  Falls back
    to :meth:`get_session_trace` (flat SQL) when the GQL query
    returns no edges (e.g. sparse/flat traces with no parent→child
    relationships).

    Args:
        session_id: The session ID to reconstruct.
        config: Optional :class:`ContextGraphConfig`.

    Returns:
        A Trace object with all spans for the session.
    """
    mgr = self.context_graph(config=config)
    rows = mgr.reconstruct_trace_gql(session_id=session_id)

    # Always fetch the flat trace to capture isolated events
    flat_trace = self.get_session_trace(session_id)

    if not rows:
      logger.info(
          "No GQL edges for session_id=%s (flat/sparse trace); "
          "using flat SQL query.",
          session_id,
      )
      return flat_trace

    # Build spans from GQL edge pairs.
    # A span may appear as parent_ first (no parent link) then as
    # child_ later — backfill parent_span_id when that happens.
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
      for prefix in ("parent_", "child_"):
        sid = row.get(f"{prefix}span_id")
        if not sid:
          continue
        if sid not in seen:
          seen[sid] = {
              "span_id": sid,
              "event_type": row.get(f"{prefix}event_type", "UNKNOWN"),
              "agent": row.get(f"{prefix}agent"),
              "timestamp": row.get(f"{prefix}timestamp"),
              "session_id": row.get("session_id"),
              "invocation_id": row.get(f"{prefix}invocation_id"),
              "content": row.get(f"{prefix}content") or {},
              "latency_ms": row.get(f"{prefix}latency_ms"),
              "status": row.get(f"{prefix}status", "OK"),
              "error_message": row.get(f"{prefix}error_message"),
              "parent_span_id": (
                  row.get("parent_span_id") if prefix == "child_" else None
              ),
          }
        elif prefix == "child_" and not seen[sid].get("parent_span_id"):
          # Backfill parent link from this child_ edge
          seen[sid]["parent_span_id"] = row.get("parent_span_id")

    gql_spans = [Span.from_bigquery_row(v) for v in seen.values()]

    # Merge: add any flat-trace spans not already covered by GQL
    gql_span_ids = {s.span_id for s in gql_spans if s.span_id}
    for span in flat_trace.spans:
      if span.span_id and span.span_id not in gql_span_ids:
        gql_spans.append(span)

    # Sort by timestamp for deterministic chronological order.
    # Use epoch as fallback (timezone-aware to avoid naive/aware conflicts).
    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    gql_spans.sort(key=lambda s: (s.timestamp or _epoch, s.span_id or ""))

    spans = gql_spans
    user_id = flat_trace.user_id
    trace_id = flat_trace.trace_id

    timestamps = [s.timestamp for s in spans if s.timestamp]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    total_ms = None
    if start and end:
      total_ms = (end - start).total_seconds() * 1000

    return Trace(
        trace_id=trace_id or session_id,
        session_id=session_id,
        spans=spans,
        user_id=user_id,
        start_time=start,
        end_time=end,
        total_latency_ms=total_ms,
    )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _build_report(
    evaluator_name: str,
    dataset: str,
    session_scores: list[SessionScore],
) -> EvaluationReport:
  """Builds an EvaluationReport from session scores."""
  total = len(session_scores)
  passed = sum(1 for s in session_scores if s.passed)
  failed = total - passed

  # Aggregate scores
  agg: dict[str, list[float]] = {}
  for ss in session_scores:
    for name, score in ss.scores.items():
      agg.setdefault(name, []).append(score)

  aggregate = {
      name: sum(vals) / len(vals) for name, vals in agg.items() if vals
  }

  return EvaluationReport(
      dataset=dataset,
      evaluator_name=evaluator_name,
      total_sessions=total,
      passed_sessions=passed,
      failed_sessions=failed,
      aggregate_scores=aggregate,
      session_scores=session_scores,
  )


def _merge_criterion_reports(
    evaluator_name: str,
    dataset: str,
    criteria: list,
    criterion_reports: list[tuple],
) -> EvaluationReport:
  """Merges single-criterion reports into a multi-criterion report.

  Each entry in *criterion_reports* is a ``(criterion, report)``
  pair.  Scores from all criteria are combined per session, and
  ``passed`` is recalculated requiring every criterion to meet
  its threshold.
  """
  session_data: dict[str, dict[str, Any]] = {}

  for criterion, report in criterion_reports:
    for ss in report.session_scores:
      if ss.session_id not in session_data:
        session_data[ss.session_id] = {
            "scores": {},
            "feedback": [],
        }
      session_data[ss.session_id]["scores"].update(ss.scores)
      if ss.llm_feedback:
        session_data[ss.session_id]["feedback"].append(ss.llm_feedback)

  # Build threshold lookup from criteria
  thresholds = {c.name: c.threshold for c in criteria}

  session_scores = []
  for sid, data in session_data.items():
    scores = data["scores"]
    # Must have at least one score AND all criteria above threshold.
    # Missing criteria default to 0.0 (guaranteed fail).
    passed = bool(scores) and all(
        scores.get(c.name, 0.0) >= thresholds.get(c.name, 0.5) for c in criteria
    )
    session_scores.append(
        SessionScore(
            session_id=sid,
            scores=scores,
            passed=passed,
            llm_feedback="\n".join(data["feedback"]) or None,
        )
    )

  return _build_report(
      evaluator_name=evaluator_name,
      dataset=dataset,
      session_scores=session_scores,
  )


def _build_traces_from_rows(results: list) -> list[Trace]:
  """Groups BigQuery result rows into Trace objects.

  Shared by ``list_traces`` and ``_api_judge`` to ensure
  consistent trace construction.
  """
  sessions: dict[str, list[Span]] = {}
  meta: dict[str, dict[str, Any]] = {}

  for row in results:
    row_dict = dict(row)
    sid = row_dict.get("session_id", "unknown")
    span = Span.from_bigquery_row(row_dict)
    sessions.setdefault(sid, []).append(span)
    if sid not in meta:
      meta[sid] = {
          "user_id": row_dict.get("user_id"),
          "trace_id": row_dict.get("trace_id"),
      }

  traces = []
  for sid, spans in sessions.items():
    timestamps = [s.timestamp for s in spans if s.timestamp]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    total_ms = None
    if start and end:
      total_ms = (end - start).total_seconds() * 1000

    traces.append(
        Trace(
            trace_id=meta[sid].get("trace_id") or sid,
            session_id=sid,
            spans=spans,
            user_id=meta[sid].get("user_id"),
            start_time=start,
            end_time=end,
            total_latency_ms=total_ms,
        )
    )

  return traces


def _apply_strict_mode(report: EvaluationReport) -> EvaluationReport:
  """Marks sessions with empty scores as failed (strict mode).

  Returns a new report with updated pass/fail counts.  Each
  affected session gets ``parse_error: True`` in its details.
  Operational counters (``parse_errors``, ``parse_error_rate``)
  are placed in the report-level ``details`` dict — not in
  ``aggregate_scores`` — so downstream consumers can treat
  scores as purely normalized metrics.
  """
  parse_errors = 0
  new_scores = []
  for ss in report.session_scores:
    if not ss.scores:
      parse_errors += 1
      new_scores.append(
          SessionScore(
              session_id=ss.session_id,
              scores=ss.scores,
              passed=False,
              details={"parse_error": True},
              llm_feedback=ss.llm_feedback,
          )
      )
    else:
      new_scores.append(ss)

  passed = sum(1 for s in new_scores if s.passed)
  details = dict(report.details)
  details["parse_errors"] = parse_errors
  details["parse_error_rate"] = (
      parse_errors / report.total_sessions if report.total_sessions else 0.0
  )
  return EvaluationReport(
      dataset=report.dataset,
      evaluator_name=report.evaluator_name,
      total_sessions=report.total_sessions,
      passed_sessions=passed,
      failed_sessions=report.total_sessions - passed,
      aggregate_scores=report.aggregate_scores,
      details=details,
      session_scores=new_scores,
  )
