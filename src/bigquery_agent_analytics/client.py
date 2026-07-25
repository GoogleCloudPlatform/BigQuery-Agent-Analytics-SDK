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
        SystemEvaluator, LLMAsJudge, TraceFilter,
    )
    report = client.evaluate(
        filters=TraceFilter(agent_id="my_agent"),
        evaluator=SystemEvaluator.latency(threshold_ms=3000),
    )
    print(report.summary())
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import concurrent.futures
import copy
from datetime import datetime
from datetime import timezone
import json
import logging
import time
from typing import Any, Optional

from google.cloud import bigquery

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels
from .categorical_evaluator import _build_evaluation_inputs_parameter
from .categorical_evaluator import _CategoricalEvaluationInput
from .categorical_evaluator import _normalize_categorical_evaluation_inputs
from .categorical_evaluator import _spans_to_categorical_transcript
from .categorical_evaluator import _validated_context_mapping
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
from .evaluators import DEFAULT_ENDPOINT
from .evaluators import EvaluationReport
from .evaluators import LLM_JUDGE_BATCH_QUERY
from .evaluators import LLMAsJudge
from .evaluators import render_ai_generate_judge_query
from .evaluators import SESSION_SUMMARY_QUERY
from .evaluators import SessionScore
from .evaluators import split_judge_prompt_template
from .evaluators import SystemEvaluator
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
from .trace import _is_unaddressable_label_key
from .trace import _jsonpath_member_segment
from .trace import AmbiguousSessionError
from .trace import ResolvedTraceSelector
from .trace import Span
from .trace import SQL_NULL
from .trace import Trace
from .trace import TraceFilter
from .trace import TraceIdentity
from .trace import TraceScope
from .trace import TraceSelector
from .trace import UNSET

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

# Issue #359 (U2): candidate sessions are anchored to their complete
# intrinsic identity, and the outer row fetch joins NULL-safely on
# every anchor dimension, so a reused session_id cannot absorb rows
# from another user or root agent even when the filter pins nothing.
# Top-level non-object attributes are intentionally excluded before
# both anchor LIMIT selection and expanded-row materialization.
# {row_where} re-applies caller-selected label/experiment scope to
# the fetched rows (see TraceFilter.row_scope_where).
_LIST_TRACES_QUERY = """\
WITH trace_sessions AS (
  SELECT
    session_id,
    user_id,
    JSON_VALUE(attributes, '$.root_agent_name') AS root_agent_name,
    MAX(timestamp) AS last_event_ts
  FROM `{project}.{dataset}.{table}`
  WHERE COALESCE(JSON_TYPE(attributes), 'null') IN ('object', 'null')
    AND ({where})
  GROUP BY session_id, user_id, root_agent_name
  ORDER BY last_event_ts DESC, session_id, user_id, root_agent_name
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
  e.is_truncated,
  ts.user_id AS anchor_user_id,
  ts.root_agent_name AS anchor_root_agent_name,
  JSON_TYPE(e.attributes) AS attributes_type
FROM `{project}.{dataset}.{table}` e
JOIN trace_sessions ts
  ON e.session_id = ts.session_id
  AND e.user_id IS NOT DISTINCT FROM ts.user_id
  AND JSON_VALUE(e.attributes, '$.root_agent_name')
      IS NOT DISTINCT FROM ts.root_agent_name
WHERE COALESCE(JSON_TYPE(e.attributes), 'null') IN ('object', 'null')
  AND ({row_where})
ORDER BY e.session_id, e.timestamp ASC, e.span_id, e.invocation_id,
  e.event_type
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

# Issue #359 (U2): singular session reads resolve candidates first.
# One row per (identity, tag payload, experiment) combination present
# in the session; Python-side resolution derives scope candidates
# from the distinct payloads (see _resolve_scope_candidates).
# Identity attributes are read as TO_JSON_STRING forms so scalar
# types survive to Python validation — JSON_VALUE would erase the
# difference between "rooty" and a persisted numeric/boolean. The
# NULLIF folds the explicit-JSON-null encoding ('null') into the
# raw-missing encoding (SQL NULL) AT THE SOURCE (PR #371 review
# round 8, P1-1). ``attributes_valid DESC`` first isolates the valid
# subsequence from the malformed tail; WITHIN that valid subsequence,
# canonical encodings make each identity one contiguous run. Python
# removes the invalid tail before deciding truncation or classifying
# scopes, so only the valid subsequence's boundary identity can
# straddle a real cut. The LIMIT bounds SQL and Python work for
# pathological sessions.
_RESOLVE_SESSION_CANDIDATES_QUERY = """\
SELECT
  session_id,
  user_id,
  NULLIF(
      TO_JSON_STRING(JSON_QUERY(attributes, '$.root_agent_name')), 'null'
  ) AS root_agent_name,
  NULLIF(
      TO_JSON_STRING(JSON_QUERY(attributes, '$.experiment_id')), 'null'
  ) AS experiment_id,
  NULLIF(
      TO_JSON_STRING(JSON_QUERY(attributes, '$.custom_tags')), 'null'
  ) AS tag_payload,
  COALESCE(JSON_TYPE(attributes), 'null') IN ('object', 'null')
      AS attributes_valid,
  ARRAY_AGG(
    IF(trace_id = '', NULL, trace_id) IGNORE NULLS
    ORDER BY timestamp, span_id, invocation_id, event_type
    LIMIT 1
  )[SAFE_OFFSET(0)] AS scope_trace_id,
  COUNT(*) AS row_count
FROM `{project}.{dataset}.{table}`
WHERE session_id = @session_id{identity_pins}
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY attributes_valid DESC, session_id, user_id, root_agent_name,
  experiment_id, tag_payload
LIMIT @candidate_limit
"""

# Batched per-identity candidate discovery (PR #371 review round 7,
# P2): one query enumerates a bounded scope page for EVERY ambiguous
# identity via a window partition, replacing sequential per-identity
# scans. The window runs over a plain subquery (not SELECT-list
# aliases of the grouped query) so alias resolution inside QUALIFY
# is never exercised, and it partitions by the CANONICAL identity
# encodings (see the NULLIF note above), so a dual-encoded identity
# gets ONE capped page instead of two — Python's canonical-key
# truncation check then counts exactly what SQL capped (PR #371
# review round 8, P2-4/P3-9).
_RESOLVE_CANDIDATES_BATCH_QUERY = """\
SELECT * FROM (
  SELECT
    session_id,
    user_id,
    NULLIF(
        TO_JSON_STRING(JSON_QUERY(attributes, '$.root_agent_name')), 'null'
    ) AS root_agent_name,
    NULLIF(
        TO_JSON_STRING(JSON_QUERY(attributes, '$.experiment_id')), 'null'
    ) AS experiment_id,
    NULLIF(
        TO_JSON_STRING(JSON_QUERY(attributes, '$.custom_tags')), 'null'
    ) AS tag_payload,
    COALESCE(JSON_TYPE(attributes), 'null') IN ('object', 'null')
        AS attributes_valid,
    COUNT(*) AS row_count
  FROM `{project}.{dataset}.{table}`
  WHERE session_id = @session_id{identity_pins}
    AND ({identity_disjunction})
  GROUP BY 1, 2, 3, 4, 5, 6
)
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY user_id, root_agent_name
  ORDER BY attributes_valid DESC, experiment_id, tag_payload
) <= @per_identity_capped
ORDER BY session_id, user_id, root_agent_name, attributes_valid DESC,
  experiment_id, tag_payload
"""

# Bounded intrinsic-identity discovery for allow_mixed_scope reads:
# identities are row-uniform and few, so this stays tiny even when a
# session carries thousands of scope payloads. DISTINCT runs over the
# canonical root encoding, so a dual-encoded identity yields one row.
_RESOLVE_SESSION_IDENTITIES_QUERY = """\
SELECT DISTINCT
  session_id,
  user_id,
  NULLIF(
      TO_JSON_STRING(JSON_QUERY(attributes, '$.root_agent_name')), 'null'
  ) AS root_agent_name,
  COALESCE(JSON_TYPE(attributes), 'null') IN ('object', 'null')
      AS attributes_valid
FROM `{project}.{dataset}.{table}`
WHERE session_id = @session_id{identity_pins}
ORDER BY attributes_valid DESC, session_id, user_id, root_agent_name
LIMIT @identity_limit
"""

# Anchored singular fetch: rows are pinned to the RESOLVED identity
# NULL-safely, and {row_where} applies the resolved scope so foreign
# passes sharing the session id stay excluded. Like discovery and
# bulk listing, it intentionally excludes top-level non-object
# attributes; completeness covers only object-or-null-attested rows.
_GET_SESSION_TRACE_QUERY = """\
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
  e.is_truncated,
  JSON_TYPE(e.attributes) AS attributes_type
FROM `{project}.{dataset}.{table}` e
WHERE e.session_id = @session_id
  AND e.user_id IS NOT DISTINCT FROM @anchor_user_id
  AND COALESCE(JSON_TYPE(e.attributes), 'null') IN ('object', 'null')
  AND JSON_VALUE(e.attributes, '$.root_agent_name')
      IS NOT DISTINCT FROM @anchor_root_agent_name
  AND {row_where}
  AND {event_where}
ORDER BY e.timestamp ASC, e.span_id, e.invocation_id, e.event_type
"""

# Lightweight companion for event-filtered mixed-scope reads. Scope
# completeness and Python-only selector verification still require every
# row's attributes, but excluded event payloads must not be transferred or
# materialized merely to establish that metadata.
_GET_SESSION_SCOPE_METADATA_QUERY = """\
SELECT
  e.user_id,
  e.attributes,
  JSON_TYPE(e.attributes) AS attributes_type
FROM `{project}.{dataset}.{table}` e
WHERE e.session_id = @session_id
  AND e.user_id IS NOT DISTINCT FROM @anchor_user_id
  AND COALESCE(JSON_TYPE(e.attributes), 'null') IN ('object', 'null')
  AND JSON_VALUE(e.attributes, '$.root_agent_name')
      IS NOT DISTINCT FROM @anchor_root_agent_name
"""

# NOTE (PR #371 review round 6, P2-11): the persisted schema carries
# no producer/ingest sequence column, so rows that tie on every
# ordered column above have no reconstructable total order. Order-
# sensitive consumers (e.g. final_response) should treat such ties as
# unordered; adding a producer sequence column is a schema follow-up
# outside U2.


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
      sdk_surface: Label value stamped on the ``sdk_surface``
          dimension of every job this Client dispatches. Defaults to
          ``"python"``. The CLI sets ``"cli"`` and the deployed
          remote-function runtime sets ``"remote-function"``. Lets
          operators attribute spend and usage back to the entry-point
          surface in ``INFORMATION_SCHEMA.JOBS_BY_PROJECT``.
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
      sdk_surface: str = "python",
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.location = location
    self.gcs_bucket_name = gcs_bucket_name
    self._bq_client = bq_client
    self._warned_unlabeled_client = False
    self._sdk_surface = sdk_surface
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
    """Lazily initializes the BigQuery client.

    When no ``bq_client`` is passed at construction time, builds a
    ``LabeledBigQueryClient`` via ``make_bq_client`` so every job the
    SDK submits carries the default SDK labels (``sdk``,
    ``sdk_version``, ``sdk_surface``).

    When the caller passes a vanilla ``bigquery.Client``, it is
    honored **as-is** — we do not reconstruct it. Rebuilding from
    ``project`` / ``credentials`` / ``location`` would silently drop
    the caller's ``default_query_job_config`` (think
    ``maximum_bytes_billed``, ``use_legacy_sql``, custom labels, write
    disposition), ``default_load_job_config``, ``client_info``,
    ``client_options``, any custom transport/session, and any subclass
    overrides on ``query`` / ``load_table_from_json``. A one-shot
    ``WARNING`` points callers to ``make_bq_client`` (or
    ``LabeledBigQueryClient`` directly) if they also want SDK
    telemetry labels.

    Non-``bigquery.Client`` objects (e.g. ``MagicMock`` in tests) are
    honored as-is, unchanged.
    """
    if self._bq_client is None:
      self._bq_client = make_bq_client(
          self.project_id,
          location=self.location,
          sdk_surface=self._sdk_surface,
      )
    elif isinstance(self._bq_client, bigquery.Client) and not isinstance(
        self._bq_client, LabeledBigQueryClient
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
      job_config = with_sdk_labels(job_config, feature="trace-read")
      results = list(
          self.bq_client.query(query, job_config=job_config).result()
      )
      columns = {r.get("column_name") for r in results}
      column_types = {r.get("column_name"): r.get("data_type") for r in results}

      missing = _REQUIRED_COLUMNS - columns
      if missing:
        logger.warning(
            "Table %s is missing columns: %s. Some SDK features may not work.",
            self._table_ref,
            missing,
        )
      # Identity/scope queries JSON-navigate these columns; a STRING
      # backing would be silently double-encoded by TO_JSON_STRING
      # and misresolve scopes, so a definite type mismatch is an
      # error, not a warning (PR #371 review round 3, P1-8).
      type_mismatches = {
          name: column_types[name]
          for name in ("attributes", "content", "latency_ms")
          if name in column_types and column_types[name] != "JSON"
      }
      if type_mismatches:
        raise ValueError(
            f"Table {self._table_ref} has incompatible column types"
            f" for JSON navigation: {type_mismatches}. The identity"
            " and scope queries require JSON columns."
        )
    except ValueError:
      raise
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
      job_config = with_sdk_labels(
          bigquery.QueryJobConfig(), feature="trace-read"
      )
      rows = list(self.bq_client.query(query, job_config=job_config).result())
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
      job_config = with_sdk_labels(job_config, feature="trace-read")
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
      ev_config = with_sdk_labels(ev_config, feature="trace-read")
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
    job_config = with_sdk_labels(job_config, feature="trace-read")

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
    job_config = with_sdk_labels(job_config, feature="trace-read")

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

  def get_session_trace(
      self,
      session_id: str,
      *,
      user_id: Any = UNSET,
      root_agent_name: Any = UNSET,
      experiment_id: Any = UNSET,
      custom_labels: Optional[dict] = None,
      scope_signature: Optional[str] = None,
      allow_mixed_scope: bool = False,
      event_types: Optional[list[str]] = None,
  ) -> Trace:
    """Fetches all spans for one resolved session identity.

    Unlike :meth:`get_trace` which queries by ``trace_id``, this
    method resolves a session by identity. ``session_id`` alone is a
    conversation-thread identifier and may be reused across users,
    root agents, and evaluation passes (issue #359): candidates are
    resolved first, and an ambiguous population raises
    :class:`AmbiguousSessionError` carrying retry-ready selectors —
    no implicit newest-wins fallback exists.

    Args:
        session_id: The session ID to retrieve.
        user_id: Optional identity pin. Explicit ``None`` pins a
            NULL user (matched NULL-safely); leave UNSET to let
            resolution decide.
        root_agent_name: Optional identity pin (same three-state
            semantics).
        experiment_id: Optional scope pin.
        custom_labels: Optional subset label pins.
        scope_signature: Optional exact-scope pin (as carried by
            ambiguity retry payloads).
        allow_mixed_scope: KTD4 escape hatch. When ``True``, a
            selector that cannot be narrowed to a single scope
            returns a conversation-complete read of the single
            matching intrinsic identity's object-or-null-attested
            rows instead of raising; top-level non-object attributes
            are intentionally excluded:
            ``scope`` is ``None`` and ``scope_coverage`` names the
            scope signatures merged into the trace. Ambiguity ACROSS
            identities still raises.
        event_types: Optional event types to materialize. Candidate
            resolution stays scope-complete; the resolved span fetch
            applies this restriction in SQL. When the restriction
            matches no rows, the resolved identity/scope is returned
            as a zero-span :class:`Trace`; this does not raise the
            unfiltered not-found error.

    Returns:
        A Trace for the single resolved identity/scope, with
        ``identity`` and ``scope`` attached.

    Raises:
        ValueError: If no events match the session/selector.
        AmbiguousSessionError: If more than one candidate remains
            and ``allow_mixed_scope`` is False (with the flag set,
            only cross-identity ambiguity raises).
    """
    selector = TraceSelector(
        session_id=session_id,
        user_id=user_id,
        root_agent_name=root_agent_name,
        experiment_id=experiment_id,
        custom_labels=custom_labels,
        scope_signature=scope_signature,
    )
    return self.get_trace_by_selector(
        selector,
        allow_mixed_scope=allow_mixed_scope,
        event_types=event_types,
    )

  def get_trace_by_selector(
      self,
      selector: TraceSelector,
      *,
      allow_mixed_scope: bool = False,
      event_types: Optional[list[str]] = None,
  ) -> Trace:
    """Fetches the single trace pinned by a :class:`TraceSelector`.

    This is the one-step retry surface for
    :meth:`AmbiguousSessionError.to_dict` payloads:
    ``get_trace_by_selector(TraceSelector(**candidate["selector"]))``
    returns exactly the chosen candidate.

    Args:
        selector: The identity/scope pins. Identity pins and
            addressable label/experiment pins are pushed into
            candidate discovery SQL, so exact retries resolve even
            for sessions whose total scope population exceeds the
            enumeration bound. An exactly-resolving selector always
            returns its exact scoped candidate — ``allow_mixed_scope``
            never overrides it.
        allow_mixed_scope: Opt-in escape hatch (plan KTD4): when the
            selector remains scope-ambiguous but its selector-aware
            population resolves to ONE intrinsic identity, return the
            conversation-complete object-or-null-attested row set
            for that identity in producer row order. Top-level
            non-object attributes are intentionally excluded. The
            trace carries ``identity``, ``scope=None``, and
            ``scope_coverage`` derived from the scopes actually fetched
            (``None`` when that population was too large to enumerate).
        event_types: Optional event types to materialize. Candidate
            discovery always sees the complete scope population; the
            authoritative span fetch applies this restriction in SQL.
            When the restriction matches no rows, the resolved
            identity/scope is returned as a zero-span :class:`Trace`;
            this does not raise the unfiltered not-found error.

    Raises:
        ValueError: If no events match the selector, if the
            selector-matching population is truncated with no match
            in the enumerated page, or if the fetched rows cannot
            reconstruct the resolved candidate.
        AmbiguousSessionError: If the selector still matches more
            than one resolved candidate.
    """
    if event_types is not None:
      event_types = list(TraceFilter(event_types=event_types).event_types or [])
    pushdown, pin_params = self._selector_pushdown_pins(selector)
    resolve_query = _RESOLVE_SESSION_CANDIDATES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        identity_pins=pushdown,
    )
    resolve_params = [
        bigquery.ScalarQueryParameter(
            "session_id", "STRING", selector.session_id
        ),
        bigquery.ScalarQueryParameter(
            "candidate_limit", "INT64", _MAX_SCOPE_CANDIDATES + 1
        ),
        *pin_params,
    ]
    job_config = bigquery.QueryJobConfig(query_parameters=resolve_params)
    job_config = with_sdk_labels(job_config, feature="trace-read")
    candidate_rows = [
        dict(row)
        for row in self.bq_client.query(
            resolve_query, job_config=job_config
        ).result()
    ]
    if not candidate_rows:
      raise _no_matching_events_error(selector, bool(pushdown))
    candidate_rows = _validated_discovery_rows(candidate_rows)
    truncated = len(candidate_rows) > _MAX_SCOPE_CANDIDATES
    if truncated:
      # Sound boundary handling (PR #371 review rounds 7-9): invalid
      # rows have already been removed from the validity-ordered page.
      # Within the remaining canonical valid subsequence every
      # identity is one contiguous run, so only its boundary identity
      # can straddle the cut. The boundary identity's rows go through
      # the shared partial-page classifier (round 9, P1-2): tagged
      # rows and every COMPLETE experiment group stay classifiable —
      # proven ambiguity must surface as typed retries, not a generic
      # enumeration error — while only the final (possibly cut)
      # experiment group and unprovable empty-scope context are
      # dropped. The canonical-key comparison is kept as defense in
      # depth for callers feeding non-canonical rows.
      boundary = candidate_rows[-1]

      def _canonical_key(row: dict) -> tuple:
        return (
            row.get("session_id"),
            _validated_identity_attr("user_id", row.get("user_id")),
            _parse_identity_attr_json(
                "root_agent_name", row.get("root_agent_name")
            ),
        )

      boundary_key = _canonical_key(boundary)
      boundary_rows = [
          row for row in candidate_rows if _canonical_key(row) == boundary_key
      ]
      sound_ids = {
          id(row) for row in _sound_truncated_identity_rows(boundary_rows)
      }
      candidate_rows = [
          row
          for row in candidate_rows
          if _canonical_key(row) != boundary_key or id(row) in sound_ids
      ]
    candidates = _resolve_scope_candidates(candidate_rows)
    matching = _candidates_matching_selector(candidates, selector)

    # Truncation soundness (PR #371 review round 5, P1-1): a
    # truncated page can prove ambiguity (two matches exist) but can
    # prove neither uniqueness (another match may sort later) nor
    # absence — single-match and no-match truncated pages both fail
    # with the enumeration-bound error.
    if len(matching) == 1 and not truncated:
      return self._fetch_identity_trace(
          matching[0].identity,
          resolved_scope=matching[0],
          scope_trace_id=_resolved_scope_trace_id(candidate_rows, matching[0]),
          event_types=event_types,
      )
    if not truncated and allow_mixed_scope and len(matching) >= 2:
      # A complete candidate page already proves the full
      # selector-matched identity population. Reuse it instead of
      # rediscovering the same identities: one identity proceeds
      # directly to the conversation-complete object-or-null fetch,
      # while multiple identities are already a proven public
      # ambiguity.
      matched_identities = list(
          dict.fromkeys(candidate.identity for candidate in matching)
      )
      if len(matched_identities) >= 2:
        raise AmbiguousSessionError(
            candidates=matching,
            population_truncated=False,
        )
      return self._fetch_mixed_scope_trace(
          selector,
          discovered=(matched_identities, False),
          event_types=event_types,
      )
    if len(matching) == 1 and truncated:
      # Under truncation the singleton needs TWO proofs (rounds 7-10).
      # Identity uniqueness: a fully pinned intrinsic identity is
      # unique by construction; otherwise it is proven through the
      # bounded identity page. Scope uniqueness: only an exact
      # scope_signature pin proves the one visible match IS the
      # requested scope. These proofs make an ambiguity retry
      # executable regardless of allow_mixed_scope; the flag controls
      # only the fallback when exactness cannot be proven.
      if selector.scope_signature is None:
        if allow_mixed_scope:
          return self._fetch_mixed_scope_trace(
              selector, event_types=event_types
          )
      if (
          selector.scope_signature is not None
          and selector.user_id is not UNSET
          and selector.root_agent_name is not UNSET
      ):
        return self._fetch_identity_trace(
            matching[0].identity,
            resolved_scope=matching[0],
            scope_trace_id=_resolved_scope_trace_id(
                candidate_rows, matching[0]
            ),
            event_types=event_types,
        )
      discovered = None
      if selector.scope_signature is not None:
        discovered = self._discover_session_identities(selector)
        identities, identity_page_truncated = discovered
        if (
            not identity_page_truncated
            and len(identities) == 1
            and identities[0] == matching[0].identity
        ):
          return self._fetch_identity_trace(
              matching[0].identity,
              resolved_scope=matching[0],
              scope_trace_id=_resolved_scope_trace_id(
                  candidate_rows, matching[0]
              ),
              event_types=event_types,
          )
      if allow_mixed_scope:
        # Reuse the page just fetched — the mixed path would otherwise
        # rerun the identical discovery query (round 10, P3-1).
        return self._fetch_mixed_scope_trace(
            selector,
            discovered=discovered,
            event_types=event_types,
        )
    if allow_mixed_scope and truncated:
      return self._fetch_mixed_scope_trace(selector, event_types=event_types)
    if len(matching) >= 2:
      # The typed ambiguity surface: real, executable retries. The
      # truncation marker tells callers the set is a lower bound.
      raise AmbiguousSessionError(
          candidates=matching[: _MAX_SCOPE_CANDIDATES + 1],
          population_truncated=(
              truncated or len(matching) > _MAX_SCOPE_CANDIDATES
          ),
      )
    if truncated:
      raise ValueError(
          "The scope-candidate population exceeds the enumeration"
          f" bound ({_MAX_SCOPE_CANDIDATES}); neither uniqueness nor"
          " absence can be proven from the enumerated page. Add"
          " identity, experiment, or label pins (they narrow the SQL"
          " page), or use allow_mixed_scope=True for a"
          " conversation-complete object-or-null-attested read."
      )
    raise ValueError("No candidates match the requested session.")

  def _selector_pushdown_pins(
      self, selector: TraceSelector
  ) -> tuple[str, list]:
    """SQL pushdown fragment + params for the selector's pins.

    Identity pins and ADDRESSABLE scope pins (experiment, JSONPath-
    addressable label keys) narrow discovery in SQL so exact retries
    stay resolvable under the enumeration bound (PR #371 review
    round 4, P1-2). NULL pins use IS NULL; UNSET pins add no
    predicate; unaddressable label keys and scope_signature remain
    Python-side.
    """
    fragment = ""
    params: list = []
    if selector.user_id is not UNSET:
      if selector.user_id is None:
        fragment += "\n  AND user_id IS NULL"
      else:
        fragment += "\n  AND user_id = @pin_user_id"
        params.append(
            bigquery.ScalarQueryParameter(
                "pin_user_id", "STRING", selector.user_id
            )
        )
    if selector.root_agent_name is not UNSET:
      if selector.root_agent_name is None:
        fragment += (
            "\n  AND JSON_VALUE(attributes, '$.root_agent_name') IS NULL"
        )
      else:
        fragment += (
            "\n  AND JSON_VALUE(attributes, '$.root_agent_name')"
            " = @pin_root_agent_name"
        )
        params.append(
            bigquery.ScalarQueryParameter(
                "pin_root_agent_name", "STRING", selector.root_agent_name
            )
        )
    if selector.experiment_id is not UNSET:
      if selector.experiment_id is None:
        # NOT pushed down (PR #371 review round 5, P1-3): filtering
        # to NULL-experiment rows would remove the non-NULL rows that
        # _scope_subgroups needs to classify shared NULL rows, so a
        # nonexistent NULL scope could be manufactured from shared
        # conversation rows. The Python-side matcher applies the pin.
        pass
      else:
        fragment += (
            "\n  AND JSON_VALUE(attributes, '$.experiment_id')"
            " = @pin_experiment_id"
        )
        params.append(
            bigquery.ScalarQueryParameter(
                "pin_experiment_id", "STRING", selector.experiment_id
            )
        )
    if selector.custom_labels:
      index = 0
      for key, value in selector.custom_labels:
        if _is_unaddressable_label_key(key):
          continue  # Python-side matching still applies this pin.
        fragment += (
            f"\n  AND JSON_VALUE(attributes,"
            f" CONCAT('$.custom_tags.', @pin_label_key_{index}))"
            f" = @pin_label_val_{index}"
        )
        params.append(
            bigquery.ScalarQueryParameter(
                f"pin_label_key_{index}",
                "STRING",
                _jsonpath_member_segment(key),
            )
        )
        params.append(
            bigquery.ScalarQueryParameter(
                f"pin_label_val_{index}", "STRING", value
            )
        )
        index += 1
    return fragment, params

  def _discover_session_identities(
      self, selector: TraceSelector
  ) -> tuple[list, bool]:
    """Bounded DISTINCT-identity page for the selector's pushdown.

    Returns the deduplicated identities and whether the page was
    truncated (cap-plus-one sentinel, PR #371 review round 6, P2-7:
    more than ``_MAX_IDENTITIES`` identities means the enumeration is
    a lower bound). Shared by the mixed-scope read and the truncated
    single-match proof in ``get_trace_by_selector`` (PR #371 review
    round 9, P1-1).
    """
    pushdown, pin_params = self._selector_pushdown_pins(selector)
    query = _RESOLVE_SESSION_IDENTITIES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        identity_pins=pushdown,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "session_id", "STRING", selector.session_id
            ),
            bigquery.ScalarQueryParameter(
                "identity_limit", "INT64", _MAX_IDENTITIES + 1
            ),
            *pin_params,
        ]
    )
    job_config = with_sdk_labels(job_config, feature="trace-read")
    identity_rows = [
        dict(row)
        for row in self.bq_client.query(query, job_config=job_config).result()
    ]
    if not identity_rows:
      raise _no_matching_events_error(selector, bool(pushdown))
    identity_rows = _validated_discovery_rows(identity_rows)
    identity_page_truncated = len(identity_rows) > _MAX_IDENTITIES
    identity_rows = identity_rows[:_MAX_IDENTITIES]
    identities: list = []
    for row in identity_rows:
      identity = TraceIdentity(
          session_id=row.get("session_id"),
          user_id=_validated_identity_attr("user_id", row.get("user_id")),
          root_agent_name=_parse_identity_attr_json(
              "root_agent_name", row.get("root_agent_name")
          ),
      )
      if identity not in identities:
        identities.append(identity)
    return identities, identity_page_truncated

  def _fetch_mixed_scope_trace(
      self,
      selector: TraceSelector,
      discovered: Optional[tuple] = None,
      event_types: Optional[list[str]] = None,
  ) -> Trace:
    """Conversation-complete object-or-null-attested read for one identity.

    Top-level non-object attributes are intentionally excluded by the
    same SQL attestation used by discovery and scoped singular fetches;
    they are outside this completeness contract.

    Identity uniqueness is established by a bounded DISTINCT-identity
    query carrying the FULL selector pushdown — identity pins AND
    addressable scope pins — so scope pins that select a single
    identity cannot produce a false cross-identity ambiguity, and a
    selector matching nothing fails with not-found instead of
    returning the whole identity (PR #371 review round 4, P1-1).
    """
    # ``discovered`` carries a caller's just-fetched identity page so
    # pathological sessions do not pay a duplicate discovery query
    # (PR #371 review round 10, P3-1).
    identities, identity_page_truncated = (
        discovered
        if discovered is not None
        else self._discover_session_identities(selector)
    )
    if len(identities) > 1:
      # Python-only pins are applied BEFORE deciding identity
      # ambiguity (PR #371 review round 6, P1-3): the SQL-pushable
      # pins alone may keep several identities whose scopes the full
      # selector actually excludes.
      ambiguous, truncated = self._real_candidates_for_identities(
          selector, identities
      )
      truncated = truncated or identity_page_truncated
      matched_identities = list(
          dict.fromkeys(candidate.identity for candidate in ambiguous)
      )
      if len(matched_identities) >= 2:
        raise AmbiguousSessionError(
            candidates=ambiguous, population_truncated=truncated
        )
      if len(matched_identities) == 1 and not truncated:
        identities = matched_identities
      elif truncated:
        raise ValueError(
            "The identity/scope population exceeds the enumeration"
            " bound; neither uniqueness nor absence can be proven."
            " Add identity, experiment, or label pins to narrow the"
            " lookup."
        )
      else:
        raise ValueError("No candidates match the requested session.")
    identity = identities[0]

    params = [
        bigquery.ScalarQueryParameter(
            "session_id", "STRING", identity.session_id
        ),
        bigquery.ScalarQueryParameter(
            "anchor_user_id", "STRING", identity.user_id
        ),
        bigquery.ScalarQueryParameter(
            "anchor_root_agent_name",
            "STRING",
            identity.root_agent_name,
        ),
    ]
    scope_results = None
    event_where = "TRUE"
    if event_types is not None:
      # Mixed reads need the complete attributes population for coverage and
      # Python-only pin verification. Fetch that lightweight metadata
      # separately so excluded event payloads never cross the wire.
      metadata_query = _GET_SESSION_SCOPE_METADATA_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          table=self.table_id,
      )
      metadata_config = bigquery.QueryJobConfig(query_parameters=params)
      metadata_config = with_sdk_labels(metadata_config, feature="trace-read")
      scope_results = list(
          self.bq_client.query(
              metadata_query, job_config=metadata_config
          ).result()
      )
      if not scope_results:
        raise ValueError(
            f"No events found for session_id={identity.session_id}"
        )
      event_where = "e.event_type IN UNNEST(@selected_event_types)"
      params = [
          *params,
          bigquery.ArrayQueryParameter(
              "selected_event_types", "STRING", event_types
          ),
      ]

    fetch_query = _GET_SESSION_TRACE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        row_where="TRUE",
        event_where=event_where,
    )
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    job_config = with_sdk_labels(job_config, feature="trace-read")
    results = list(
        self.bq_client.query(fetch_query, job_config=job_config).result()
    )
    if not results and event_types is None:
      raise ValueError(f"No events found for session_id={identity.session_id}")
    # Producer row order, one span per row: no cross-scope merging,
    # no span_id-based dedup, no chronology loss. Every fetched row
    # is revalidated against the resolved identity with
    # type-preserving comparison (PR #371 review round 4, P2-8) —
    # JSON_VALUE anchors erase scalar types, so a numeric root agent
    # inserted between discovery and fetch must not slip through.
    spans = []
    fetched_entries = []

    def _validated_components(row):
      row_dict = dict(row)
      # Validated from the RAW cell (PR #371 review rounds 8 P2-2,
      # 9 P1-5, 10 P1-1): a persisted non-object — including a JSON
      # string scalar the BigQuery decoder hands over as str, even
      # one whose text is serialized object syntax — must not
      # silently classify, fabricate an identity, or crash with an
      # unredacted AttributeError. The JSON_TYPE attestation is
      # authoritative when projected.
      attributes = _validated_attributes_object(
          row_dict.get("attributes"),
          sql_type=(
              row_dict.get("attributes_type")
              if "attributes_type" in row_dict
              else _NO_ATTESTATION
          ),
      )
      row_user = _validated_identity_attr("user_id", row_dict.get("user_id"))
      row_root = _validated_identity_attr(
          "root_agent_name", attributes.get("root_agent_name")
      )
      if row_user != identity.user_id or row_root != identity.root_agent_name:
        raise ValueError(
            "Resolution/fetch consistency failure: a fetched row does"
            " not match the resolved identity (identifiers redacted)."
            " The underlying data changed between resolution and"
            " fetch; retry the lookup."
        )
      experiment = _validated_identity_attr(
          "experiment_id", attributes.get("experiment_id")
      )
      payload = _parse_tag_payload(
          attributes.get("custom_tags"), source="attributes"
      )
      row_dict["attributes"] = attributes
      return row_dict, experiment, payload

    for row in scope_results if scope_results is not None else results:
      _, experiment, payload = _validated_components(row)
      fetched_entries.append((experiment, payload, None))

    for row in results:
      row_dict, _, _ = _validated_components(row)
      # Hand the normalized object to Span so it cannot reparse the raw
      # scalar after the authoritative attestation has accepted this row.
      spans.append(Span.from_bigquery_row(row_dict))
    # Coverage from the scopes ACTUALLY fetched, computed WITHOUT the
    # per-row cross-product expansion (PR #371 review round 5, P2-8):
    # only distinct (experiment, payload-signature) sets are tracked.
    fetched_scopes = _fetched_scopes(fetched_entries)
    coverage: Optional[tuple] = None
    if len(fetched_scopes) <= _MAX_SCOPE_CANDIDATES:
      coverage = tuple(
          sorted(scope.scope_signature for scope in fetched_scopes)
      )
    # Python-only selector pins — scope_signature and JSONPath-
    # unaddressable labels — are not SQL-pushable, so the mixed read
    # must verify them against the fetched population instead of
    # silently returning the whole identity (PR #371 review round 5,
    # P1-2).
    self._verify_python_only_pins(selector, identity, fetched_scopes)
    timestamps = [s.timestamp for s in spans if s.timestamp]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    total_ms = (end - start).total_seconds() * 1000 if start and end else None
    trace_id = next((s.trace_id for s in spans if s.trace_id), None)
    return Trace(
        trace_id=trace_id or identity.session_id,
        session_id=identity.session_id,
        spans=spans,
        user_id=identity.user_id,
        start_time=start,
        end_time=end,
        total_latency_ms=total_ms,
        identity=identity,
        scope_coverage=coverage,
    )

  def _real_candidates_for_identities(
      self,
      selector: TraceSelector,
      identities: list,
  ) -> tuple[list, bool]:
    """Selector-constrained candidates covering EVERY ambiguous identity.

    ONE batched query enumerates a bounded scope page per identity
    via a window partition (PR #371 review round 7, P2), so no
    identity is starved by a shared global page and no sequential
    per-identity scans run. Python selector matching is applied so an
    excluded pass is never advertised as an executable retry; a
    truncated per-identity page reports truncation without emitting
    potentially manufactured candidates for that identity.
    """
    per_identity_limit = max(
        2, _MAX_SCOPE_CANDIDATES // max(1, len(identities))
    )
    pushdown, pin_params = self._selector_pushdown_pins(selector)
    disjunction_terms = []
    identity_params = []
    for index, identity in enumerate(identities):
      disjunction_terms.append(
          f"(user_id IS NOT DISTINCT FROM @batch_user_{index}"
          " AND JSON_VALUE(attributes, '$.root_agent_name')"
          f" IS NOT DISTINCT FROM @batch_root_{index})"
      )
      identity_params.append(
          bigquery.ScalarQueryParameter(
              f"batch_user_{index}", "STRING", identity.user_id
          )
      )
      identity_params.append(
          bigquery.ScalarQueryParameter(
              f"batch_root_{index}", "STRING", identity.root_agent_name
          )
      )
    resolve_query = _RESOLVE_CANDIDATES_BATCH_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        identity_pins=pushdown,
        identity_disjunction=" OR ".join(disjunction_terms),
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "session_id", "STRING", selector.session_id
            ),
            bigquery.ScalarQueryParameter(
                "per_identity_capped", "INT64", per_identity_limit + 1
            ),
            *identity_params,
            *pin_params,
        ]
    )
    job_config = with_sdk_labels(job_config, feature="trace-read")
    rows = [
        dict(row)
        for row in self.bq_client.query(
            resolve_query, job_config=job_config
        ).result()
    ]
    rows = _validated_discovery_rows(rows)
    rows_by_identity: dict[tuple, list] = {}
    for row in rows:
      key = (
          row.get("session_id"),
          _validated_identity_attr("user_id", row.get("user_id")),
          _parse_identity_attr_json(
              "root_agent_name", row.get("root_agent_name")
          ),
      )
      rows_by_identity.setdefault(key, []).append(row)
    all_candidates: list = []
    truncated = False
    for key, identity_rows in rows_by_identity.items():
      if len(identity_rows) > per_identity_limit:
        # A truncated per-identity page still classifies its SOUND
        # subset (PR #371 review round 9, P1-2): tagged rows and
        # complete experiment groups are proven candidates and must
        # surface as typed retries; only the final (possibly cut)
        # group and unprovable empty-scope context are dropped.
        # Truncation is still reported so the set reads as a lower
        # bound.
        truncated = True
        identity_rows = _sound_truncated_identity_rows(identity_rows)
        if not identity_rows:
          continue
      candidates = _candidates_matching_selector(
          _resolve_scope_candidates(identity_rows), selector
      )
      all_candidates.extend(candidates[:per_identity_limit])
    return all_candidates, truncated

  @staticmethod
  def _verify_python_only_pins(
      selector: TraceSelector,
      identity: TraceIdentity,
      fetched_scopes: list,
  ) -> None:
    """Verify the COMPLETE selector conjunctively against one scope.

    At least one fetched scope must satisfy EVERY selector pin
    simultaneously — signature, all labels (addressable or not), and
    the experiment pin including ``None`` — or the mixed read fails
    not-found: a signature matching scope A while a label matches
    scope B is no match at all (PR #371 review round 6, P1-2). The
    verification runs over the materialized ``fetched_scopes``
    regardless of whether the additive coverage metadata was omitted
    for size (round 6, P2-10).
    """
    if (
        selector.scope_signature is None
        and not selector.custom_labels
        and selector.experiment_id is UNSET
    ):
      return
    scope_candidates = [
        ResolvedTraceSelector(identity=identity, scope=scope)
        for scope in fetched_scopes
    ]
    if not _candidates_matching_selector(scope_candidates, selector):
      raise ValueError("No candidates match the requested session.")

  def _fetch_identity_trace(
      self,
      identity: TraceIdentity,
      resolved_scope: ResolvedTraceSelector,
      scope_trace_id: Optional[str] = None,
      event_types: Optional[list[str]] = None,
  ) -> Trace:
    """Anchored row fetch for one resolved identity and scope.

    The row-scope predicates and parameters come from the U1
    resolved-selector conversion (``to_selector().to_trace_filter()``)
    so NULL experiments pin ``SQL_NULL`` and JSONPath-unaddressable
    labels are handled through signature attestation instead of
    rejecting the retry.
    """
    params = [
        bigquery.ScalarQueryParameter(
            "session_id", "STRING", identity.session_id
        ),
        bigquery.ScalarQueryParameter(
            "anchor_user_id", "STRING", identity.user_id
        ),
        bigquery.ScalarQueryParameter(
            "anchor_root_agent_name",
            "STRING",
            identity.root_agent_name,
        ),
    ]
    row_filter = resolved_scope.to_selector().to_trace_filter()
    row_where = row_filter.row_scope_where()
    scope_param_names = {"experiment_id"}
    label_count = len(row_filter.custom_labels or {})
    for i in range(label_count):
      scope_param_names.add(f"label_key_{i}")
      scope_param_names.add(f"label_val_{i}")
    _, filter_params = row_filter.to_sql_conditions()
    params.extend(
        param for param in filter_params if param.name in scope_param_names
    )
    event_where = "TRUE"
    if event_types is not None:
      event_where = "e.event_type IN UNNEST(@selected_event_types)"
      params.append(
          bigquery.ArrayQueryParameter(
              "selected_event_types", "STRING", event_types
          )
      )

    fetch_query = _GET_SESSION_TRACE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        row_where=row_where,
        event_where=event_where,
    )
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    job_config = with_sdk_labels(job_config, feature="trace-read")
    results = list(
        self.bq_client.query(fetch_query, job_config=job_config).result()
    )
    if event_types is not None:
      return _materialize_event_filtered_trace(
          results,
          identity=identity,
          resolved_scope=resolved_scope,
          scope_trace_id=scope_trace_id,
      )
    if not results:
      raise ValueError(f"No events found for session_id={identity.session_id}")
    # Materialize ONLY the resolved scope (PR #371 review round 7,
    # P1-4): without the predicate and bound, every sibling scope
    # admitted by the row fetch would be built and its shared rows
    # deep-copied just to be discarded.
    resolved_signature = resolved_scope.scope_signature
    traces = _build_traces_from_rows(
        results,
        max_traces=1,
        scope_predicate=(
            lambda scope: scope is not None
            and scope.scope_signature == resolved_signature
        ),
        population_complete=False,
    )
    for trace in traces:
      if (
          trace.identity == identity
          and trace.scope is not None
          and trace.scope.scope_signature == resolved_signature
      ):
        return trace
    # Fail closed: never substitute a different scope for the one
    # that was resolved.
    raise ValueError(
        "Resolution/fetch consistency failure: the fetched rows no"
        " longer contain the resolved scope (identifiers redacted)."
        " The underlying data changed between resolution and fetch;"
        " retry the lookup."
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
        List of Trace objects, one per resolved identity AND scope
        (issue #359): a session id reused across users, root agents,
        or evaluation passes yields multiple Trace objects, each
        carrying ``identity``/``scope``. Scope-filtered listings may
        over-fetch identity anchors to fill the limit with valid
        scopes; the limit bounds the returned traces.

        Ordering is deterministic (trace ``end_time`` descending with
        stable tie-breakers) WITHIN a result set. Across different
        ``limit`` values, scope-filtered listings are not guaranteed
        prefix-stable: the SQL anchor page ranks identities by the
        recency of their FILTER-MATCHING rows, while returned traces
        rank by ``end_time`` over all scope-admitted rows (which may
        include later untagged enrichment rows), so a larger limit
        can surface an identity that outranks a smaller limit's
        results. Computing anchor recency over scope-admitted rows
        would require an unpruned full-table aggregation per listing;
        callers needing a stable prefix should list once with the
        larger limit and slice.

        Top-level non-object attributes are excluded in SQL before
        anchor selection and row materialization, so they consume no
        limit slots, never appear in results, and produce no quarantine
        warning. Object-typed rows with malformed identity/scope fields
        are quarantined in Python; the warning reports distinct groups
        actually removed, at most once per group for this
        ``list_traces`` call across anchor escalation retries. A
        matching singular selector still raises the typed validation
        error for those object-typed malformed fields.
    """
    # One fully detached snapshot feeds the fragments, parameters,
    # AND the post-query limit, so concurrent filter mutation cannot
    # desynchronize any part of the read (PR #371 review round 4,
    # P1-6).
    filt = (filter_criteria or TraceFilter()).snapshot()
    where, params = filt.to_sql_conditions()
    row_where = filt.row_scope_where()

    return self._fetch_filtered_traces(
        table=self.table_id,
        where=where,
        row_where=row_where,
        params=params,
        limit=filt.limit,
        scope_predicate=_filter_scope_predicate(filt),
        feature="trace-read",
    )

  def _fetch_filtered_traces(
      self,
      *,
      table: str,
      where: str,
      row_where: str,
      params: list,
      limit: Optional[int],
      scope_predicate: Optional[Any],
      feature: str,
      span_predicate: Optional[Any] = None,
  ) -> list[Trace]:
    """Anchor-escalating listing fetch shared by every bulk read path.

    Scope-filtered listings can reject anchored identities whose
    scopes never matched (e.g. an SQL_NULL experiment filter over an
    identity whose NULL rows are shared infrastructure), so a plain
    SQL LIMIT could starve genuine older results (PR #371 review
    round 7, P1-3). The anchor limit escalates until enough valid
    scopes are classified, the anchors are provably exhausted (the
    page returned fewer distinct identities than requested — PR #371
    review round 8, P2-8), or the escalation bound is hit. Both
    ``list_traces`` and the LLM-judge API fallback route through
    here, so evaluation sets cannot silently starve while listings
    escalate (PR #371 review round 8, P1-2).
    """
    query = _LIST_TRACES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
        row_where=row_where,
    )
    # Shared-row trace ids are trusted whenever the ROW fetch is
    # unfiltered: the identity slot census in the builder runs before
    # the slot-level scope predicate, so a Python-side predicate does
    # not blind the census the way SQL row prefiltering does
    # (PR #371 review round 8, P3-10).
    population_complete = row_where == "TRUE"
    sql_limit = limit
    # Escalation runs until the result fills, the anchors are
    # provably exhausted, or the 4096-anchor ceiling has ACTUALLY
    # been queried (PR #371 review round 9, P1-3) — a fixed attempt
    # count silently stopped at 64 anchors for limit=1. Exhaustion is
    # judged on distinct SQL anchors projected from the CTE into the
    # fetched rows (round 9, P1-4): quarantine keys are coarser than
    # anchors, so counting them could collapse two malformed anchors
    # into one and falsely end escalation above valid older results.
    # A None limit (judge path with no filter) keeps the caller's
    # trace_limit parameter untouched and cannot escalate — there is
    # no fill target to escalate toward.
    traces: list[Trace] = []
    reported_quarantined_groups: set = set()
    while True:
      attempt_params = [
          (
              bigquery.ScalarQueryParameter("trace_limit", "INT64", sql_limit)
              if param.name == "trace_limit" and sql_limit is not None
              else param
          )
          for param in params
      ]
      job_config = bigquery.QueryJobConfig(query_parameters=attempt_params)
      job_config = with_sdk_labels(job_config, feature=feature)
      results = list(
          self.bq_client.query(query, job_config=job_config).result()
      )
      traces = _build_traces_from_rows(
          results,
          max_traces=limit,
          scope_predicate=scope_predicate,
          span_predicate=span_predicate,
          population_complete=population_complete,
          on_malformed="quarantine",
          _reported_quarantined_groups=reported_quarantined_groups,
      )
      anchors: set = set()
      for index, row in enumerate(results):
        row_dict = dict(row)
        if "anchor_user_id" in row_dict:
          anchors.add(
              (
                  row_dict.get("session_id"),
                  row_dict.get("anchor_user_id"),
                  row_dict.get("anchor_root_agent_name"),
              )
          )
        else:
          # Rows without the projected anchor columns (foreign row
          # sources) count one anchor per row: an OVERcount can only
          # cost extra escalation attempts, never a false exhaustion.
          anchors.add(("__row__", index))
      # Refill on ANY saturated page that yielded fewer traces than
      # requested — scope-predicate rejection and per-identity
      # quarantine both shrink the yield (PR #371 review round 10,
      # P1-3): a malformed newest anchor must not starve unfiltered
      # listings of valid older traces. Healthy unfiltered pages
      # yield at least one trace per anchor, so they always fill or
      # short-page and never re-query.
      if (
          sql_limit is None
          or len(traces) >= limit
          or not results
          or len(anchors) < sql_limit
          or sql_limit >= 4096
      ):
        break
      sql_limit = min(sql_limit * 8, 4096)
    return _ordered_limited_traces(traces, limit)

  # -------------------------------------------------------------- #
  # Evaluation                                                       #
  # -------------------------------------------------------------- #

  def evaluate(
      self,
      evaluator: SystemEvaluator | LLMAsJudge,
      filters: Optional[TraceFilter] = None,
      dataset: Optional[str] = None,
      strict: bool = False,
  ) -> EvaluationReport:
    """Runs batch evaluation over traces.

    Uses BigQuery native execution for scalable assessment.
    ``SystemEvaluator`` metrics are computed from session
    aggregates. ``LLMAsJudge`` metrics use BQML's
    ``ML.GENERATE_TEXT`` for zero-ETL evaluation.

    Args:
        evaluator: A SystemEvaluator or LLMAsJudge instance.
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
        EvaluationReport with per-session and aggregate scores. When
        the API fallback expands one session into multiple identity/
        scope evaluation units, each ``SessionScore.details`` carries
        authoritative ``user_id``, ``root_agent_name``, and
        ``scope_signature`` attribution. These keys are SDK-reserved
        and strict mode preserves them while adding
        ``parse_error=True``.
    """
    table = dataset or self.table_id
    # One detached snapshot for the whole evaluation: candidate
    # predicates, the fallback row predicate, and the limit all come
    # from the same immutable state (PR #371 review round 4, P1-6).
    filt = (filters or TraceFilter()).snapshot()
    where, params = filt.to_sql_conditions()

    if isinstance(evaluator, SystemEvaluator):
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
      evaluator: SystemEvaluator,
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
    job_config = with_sdk_labels(job_config, feature="eval-code")

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

    Stamps ``report.details["execution_mode"]`` with one of
    ``ai_generate``, ``ml_generate_text``, ``api_fallback`` so the
    caller (and CI gates) can audit which path actually ran.
    When an earlier tier raised before a later tier succeeded,
    ``report.details["fallback_reason"]`` carries the chained
    exception messages in attempt order. (The naming mirrors the
    categorical evaluator's ``execution_mode`` value space for
    consistency.)
    """
    criteria = evaluator._criteria
    if not criteria:
      report = _build_report(
          evaluator_name=evaluator.name,
          dataset=f"{self._table_ref} WHERE {where}",
          session_scores=[],
      )
      report.details["execution_mode"] = "no_op"
      return report

    # Issue #359: the API-fallback trace fetch applies the caller's
    # label/experiment scope to fetched rows, matching list_traces.
    # trace_filter is the detached snapshot captured by evaluate(),
    # so this read cannot desynchronize from the candidate
    # predicates derived there (PR #371 review round 4, P1-6).
    row_where = (
        trace_filter.row_scope_where() if trace_filter is not None else "TRUE"
    )

    fallback_reasons: list[str] = []

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
        merged = _merge_criterion_reports(
            evaluator.name,
            f"{self._table_ref} WHERE {where}",
            criteria,
            criterion_reports,
        )
        merged.details["execution_mode"] = "ai_generate"
        return merged
      except Exception as e:
        logger.debug(
            "AI.GENERATE judge failed, trying legacy: %s",
            e,
        )
        fallback_reasons.append(f"ai_generate: {e}")

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
      merged = _merge_criterion_reports(
          evaluator.name,
          f"{self._table_ref} WHERE {where}",
          criteria,
          criterion_reports,
      )
      merged.details["execution_mode"] = "ml_generate_text"
      if fallback_reasons:
        merged.details["fallback_reason"] = "; ".join(fallback_reasons)
      return merged
    except Exception as e:
      logger.debug(
          "BQML judge failed, falling back to API: %s",
          e,
      )
      fallback_reasons.append(f"ml_generate_text: {e}")

    # Fallback: fetch traces using same table/filter, evaluate via API
    api_report = self._api_judge(
        evaluator,
        table,
        where,
        params,
        row_where=row_where,
        limit=trace_filter.limit if trace_filter is not None else None,
        trace_filter=trace_filter,
    )
    api_report.details["execution_mode"] = "api_fallback"
    if fallback_reasons:
      api_report.details["fallback_reason"] = "; ".join(fallback_reasons)
    return api_report

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

    prefix, middle, suffix = split_judge_prompt_template(
        criterion.prompt_template
    )
    judge_params = list(params) + [
        bq.ScalarQueryParameter("judge_prompt_prefix", "STRING", prefix),
        bq.ScalarQueryParameter("judge_prompt_middle", "STRING", middle),
        bq.ScalarQueryParameter("judge_prompt_suffix", "STRING", suffix),
    ]

    query = render_ai_generate_judge_query(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
        endpoint=self.endpoint,
        connection_id=self.connection_id,
    )
    job_config = bq.QueryJobConfig(
        query_parameters=judge_params,
    )
    job_config = with_sdk_labels(
        job_config, feature="eval-llm-judge", ai_function="ai-generate"
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

    prefix, middle, suffix = split_judge_prompt_template(
        criterion.prompt_template
    )
    judge_params = list(params) + [
        bq.ScalarQueryParameter("judge_prompt_prefix", "STRING", prefix),
        bq.ScalarQueryParameter("judge_prompt_middle", "STRING", middle),
        bq.ScalarQueryParameter("judge_prompt_suffix", "STRING", suffix),
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
    job_config = with_sdk_labels(
        job_config, feature="eval-llm-judge", ai_function="ml-generate-text"
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
      row_where: str = "TRUE",
      limit: Optional[int] = None,
      trace_filter: Optional[TraceFilter] = None,
  ) -> EvaluationReport:
    """Evaluates using the Gemini API (fallback).

    Fetches traces from the same table and filter as the BQ
    evaluation paths, then evaluates each session via the
    Gemini API.
    """
    # Post-expansion scope filter + limit: one identity can expand
    # into several scoped traces (each a paid model call), and
    # sibling scopes that never matched the filter must not be
    # judged (PR #371 review rounds 3-5). Slot-level filtering keeps
    # the limit correct and bounds materialization. The shared
    # escalating fetch keeps the evaluation set from silently
    # starving on scope-filtered anchors (PR #371 review round 8,
    # P1-2).
    judge_predicate = (
        _filter_scope_predicate(trace_filter)
        if trace_filter is not None
        else None
    )
    traces = self._fetch_filtered_traces(
        table=table,
        where=where,
        row_where=row_where,
        params=params,
        limit=limit,
        scope_predicate=judge_predicate,
        feature="eval-llm-judge",
    )

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
      # Per-scope expansion attribution (PR #371 review round 8,
      # P2-7): a session id reused across identities or evaluation
      # passes yields several judged traces; without identity/scope
      # attribution their score rows collide on session_id and
      # cross-criterion merging would overwrite one pass with
      # another. Reserved keys are ASSIGNED from the trace — the
      # authoritative source — never defaulted, so a custom evaluator
      # returning stale or spoofed attribution cannot make distinct
      # score rows collide under the merge key (PR #371 review round
      # 9, P2-1).
      score.details["user_id"] = (
          trace.identity.user_id if trace.identity is not None else None
      )
      score.details["root_agent_name"] = (
          trace.identity.root_agent_name if trace.identity is not None else None
      )
      score.details["scope_signature"] = (
          trace.scope.scope_signature if trace.scope is not None else None
      )
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
      per_session_context: Optional[
          Mapping[ResolvedTraceSelector | str, str]
      ] = None,
  ) -> CategoricalEvaluationReport:
    """Runs categorical evaluation over traces.

    Execution cascade:

    * When ``include_justification=False``:
      AI.CLASSIFY → AI.GENERATE → Gemini API
    * When ``include_justification=True`` (default):
      AI.GENERATE → Gemini API
    * With any non-empty ``per_session_context``:
      identity-safe resolution → AI.GENERATE → Gemini API

    Args:
        config: Categorical evaluation configuration with metric
            definitions and allowed categories.
        filters: Optional trace filters.
        dataset: Optional table name override.
        per_session_context: Optional trusted judge context keyed by an exact
            :class:`ResolvedTraceSelector` or, for an unambiguous evaluated
            population, by legacy session-id string. A non-empty mapping
            bypasses AI.CLASSIFY and binds context to the exact U2
            identity/scope selector through every generative path. Legacy
            string keys are accepted only for one matching transcript-eligible
            resolved trace. Transcript eligibility (length greater than 10)
            is applied before that ambiguity check, so an ineligible colliding
            trace does not make the survivor ambiguous; exact selector keys
            avoid that inference. Keys outside the filtered population are
            ignored. Treat values as trusted evaluator material subject to the
            same data-governance policy as evaluation prompts. Context is sent
            only as a query parameter/model prompt; it is not interpolated into
            SQL, logged, persisted, or placed in job labels. Context calls
            reject ``config.persist_results=True`` until the U5 identity-safe
            persistence migration lands.

    Returns:
        CategoricalEvaluationReport with per-session results and
        category distributions.
    """
    table = dataset or self.table_id
    filt = filters or TraceFilter()
    where, params = filt.to_sql_conditions()

    identity_bound_inputs: Optional[list[_CategoricalEvaluationInput]] = None
    classify_skip_reason = None
    context_snapshot = (
        _validated_context_mapping(per_session_context)
        if per_session_context is not None
        else {}
    )
    if context_snapshot:
      if config.persist_results:
        raise ValueError(
            "Identity-bound judge context requires identity-safe persistence"
            " from issue #358 U5; disable persist_results until that schema,"
            " writer, and view migration is installed."
        )
      # Resolve the same exact identity/scope population used by U2 before
      # any model call. One detached filter snapshot feeds SQL fragments,
      # scope matching, and the returned limit.
      filt = filt.snapshot()
      where, row_where, params = filt.to_query_fragments()
      traces = self._fetch_filtered_traces(
          table=table,
          where=where,
          row_where=row_where,
          params=params,
          limit=filt.limit,
          scope_predicate=_filter_scope_predicate(filt),
          feature="eval-categorical",
          span_predicate=lambda spans: len(
              _spans_to_categorical_transcript(spans)
          )
          > 10,
      )
      identity_bound_inputs = _normalize_categorical_evaluation_inputs(
          traces,
          context_snapshot,
      )
      classify_skip_reason = (
          "AI.CLASSIFY skipped: identity-bound judge context requires"
          " per-trace prompt input, which AI.CLASSIFY cannot accept."
      )

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

    if identity_bound_inputs == []:
      report = build_categorical_report(
          dataset=f"{table_ref} WHERE {where}",
          session_results=[],
          config=config,
      )
      report.details["execution_mode"] = "ai_generate"
      report.details["empty_population"] = True
      report.details["classify_skip_reason"] = classify_skip_reason
      self._persist_categorical_if_configured(report, config, endpoint)
      return report

    # When justification is not needed, try AI.CLASSIFY first.
    if not config.include_justification and identity_bound_inputs is None:
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
          evaluation_inputs=identity_bound_inputs,
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
      if classify_skip_reason:
        report.details["classify_skip_reason"] = classify_skip_reason
      self._persist_categorical_if_configured(report, config, endpoint)
      return report
    except Exception as e:
      if identity_bound_inputs is not None:
        logger.debug(
            "AI.GENERATE categorical failed, falling back to API"
            " (details redacted because trusted judge context was"
            " present; type=%s)",
            type(e).__name__,
        )
        fallback_reason = (
            f"{type(e).__name__}: details redacted because trusted"
            " judge context was present"
        )
      else:
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
          evaluation_inputs=identity_bound_inputs,
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
      if classify_skip_reason:
        report.details["classify_skip_reason"] = classify_skip_reason
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
      if classify_skip_reason:
        report.details["classify_skip_reason"] = classify_skip_reason
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
    job_config = with_sdk_labels(
        job_config,
        feature="eval-categorical",
        ai_function="ai-classify",
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
      evaluation_inputs: Optional[list[_CategoricalEvaluationInput]] = None,
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
        identity_bound=evaluation_inputs is not None,
    )

    source_params = (
        [_build_evaluation_inputs_parameter(evaluation_inputs)]
        if evaluation_inputs is not None
        else list(params)
    )
    query_params = source_params + [
        bigquery.ScalarQueryParameter(
            "categorical_prompt",
            "STRING",
            prompt,
        ),
    ]
    job_config = bigquery.QueryJobConfig(
        query_parameters=query_params,
    )
    job_config = with_sdk_labels(
        job_config,
        feature="eval-categorical",
        ai_function="ai-generate",
    )

    results = list(self.bq_client.query(query, job_config=job_config).result())

    session_results = []
    failed_sessions = {}
    inputs_by_key = (
        {item.evaluation_key: item for item in evaluation_inputs}
        if evaluation_inputs is not None
        else {}
    )
    result_keys = []
    for row in results:
      r = dict(row)
      sid = r.get("session_id", "unknown")
      input_key = (
          r.get("evaluation_key") if evaluation_inputs is not None else sid
      )
      if evaluation_inputs is not None and input_key not in inputs_by_key:
        raise ValueError(
            "Identity-bound categorical result carried an unknown"
            " evaluation key."
        )
      if evaluation_inputs is not None and input_key in result_keys:
        raise ValueError(
            "Identity-bound categorical result duplicated an evaluation key."
        )
      result_keys.append(input_key)
      parsed = parse_categorical_row(sid, r, config)
      input_item = inputs_by_key.get(input_key)
      if input_item is not None:
        parsed.details.update(
            {
                "user_id": input_item.selector.identity.user_id,
                "root_agent_name": (
                    input_item.selector.identity.root_agent_name
                ),
                "scope_signature": input_item.selector.scope_signature,
            }
        )
      has_parse_error = any(m.parse_error for m in parsed.metrics)
      if has_parse_error and r.get("transcript"):
        failed_sessions[input_key] = r.get("transcript", "")
      session_results.append(parsed)

    if evaluation_inputs is not None and set(result_keys) != set(inputs_by_key):
      raise ValueError(
          "Identity-bound categorical result did not return exactly one"
          " row per resolved evaluation input."
      )

    retry_meta = {}
    if failed_sessions:
      logger.warning(
          "AI.GENERATE returned NULL/unparseable for %d session(s); "
          "retrying via Gemini API.",
          len(failed_sessions),
      )
      retry_kwargs = {}
      if evaluation_inputs is not None:
        retry_kwargs["per_session_context"] = {
            key: inputs_by_key[key].judge_context
            for key in failed_sessions
            if inputs_by_key[key].judge_context is not None
        }
        retry_kwargs["resolved_selectors"] = {
            key: inputs_by_key[key].selector for key in failed_sessions
        }
      retried = self._retry_failed_sessions(
          failed_sessions,
          config,
          endpoint,
          max_retries=3,
          **retry_kwargs,
      )
      resolved = 0
      if retried:
        if evaluation_inputs is None:
          retried_map = {r.session_id: r for r in retried}
        else:
          retried_map = {}
          unmatched = list(retried)
          for key in failed_sessions:
            selector = inputs_by_key[key].selector
            for index, retried_result in enumerate(unmatched):
              details = retried_result.details
              if (
                  retried_result.session_id == selector.identity.session_id
                  and details.get("user_id") == selector.identity.user_id
                  and details.get("root_agent_name")
                  == selector.identity.root_agent_name
                  and details.get("scope_signature") == selector.scope_signature
              ):
                retried_map[key] = unmatched.pop(index)
                break
        session_results = [
            retried_map.get(key, sr)
            for key, sr in zip(result_keys, session_results)
        ]
        counted_results = (
            retried_map.values() if evaluation_inputs is not None else retried
        )
        resolved = sum(
            1
            for r in counted_results
            if not any(m.parse_error for m in r.metrics)
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

    if evaluation_inputs is not None:
      by_key = dict(zip(result_keys, session_results))
      session_results = [
          by_key[item.evaluation_key] for item in evaluation_inputs
      ]

    return session_results, retry_meta

  def _retry_failed_sessions(
      self,
      transcripts: dict[str, str],
      config: CategoricalEvaluationConfig,
      endpoint: str,
      max_retries: int = 3,
      per_session_context: Optional[dict[str, str]] = None,
      resolved_selectors: Optional[dict[str, ResolvedTraceSelector]] = None,
  ) -> list:
    """Retries classification for failed sessions via Gemini API.

    Note: This method is synchronous and must not be called from
    an async context with an already-running event loop.

    Args:
        transcripts: Maps session_id to transcript text.
        config: Evaluation config.
        endpoint: Model endpoint.
        max_retries: Maximum number of retry attempts.
        per_session_context: Context keyed like ``transcripts``.
        resolved_selectors: Exact selector metadata keyed like
            ``transcripts``.

    Returns:
        List of CategoricalSessionResult for successfully retried
        sessions.
    """
    original_order = list(transcripts)
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
        api_kwargs = {}
        if per_session_context is not None:
          api_kwargs["per_session_context"] = {
              key: per_session_context[key]
              for key in remaining
              if key in per_session_context
          }
        if resolved_selectors is not None:
          api_kwargs["resolved_selectors"] = {
              key: resolved_selectors[key]
              for key in remaining
              if key in resolved_selectors
          }
        remaining_keys = list(remaining)
        results = _run_sync(
            classify_sessions_via_api(
                remaining,
                config,
                endpoint,
                **api_kwargs,
            )
        )
        still_failed = {}
        for input_key, r in zip(remaining_keys, results):
          has_error = any(m.parse_error for m in r.metrics)
          if has_error:
            still_failed[input_key] = remaining[input_key]
            for m in r.metrics:
              if m.parse_error:
                if (
                    per_session_context is not None
                    and input_key in per_session_context
                ):
                  logger.warning(
                      "Retry attempt %d, session %s, metric %s:"
                      " parse_error=True (raw response redacted because"
                      " trusted judge context was present)",
                      attempt,
                      r.session_id,
                      m.metric_name,
                  )
                else:
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
            all_results[input_key] = r
        # A short API result is unresolved work, never implicit success.
        for input_key in remaining_keys[len(results) :]:
          still_failed[input_key] = remaining[input_key]
        remaining = still_failed
        if remaining:
          logger.warning(
              "Retry attempt %d: %d sessions still unresolved",
              attempt,
              len(remaining),
          )
      except Exception as e:  # Broad catch: retry loop logs + continues
        if per_session_context:
          logger.warning(
              "Gemini API retry attempt %d failed (details redacted"
              " because trusted judge context was present; type=%s)",
              attempt,
              type(e).__name__,
          )
        else:
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

    return [
        all_results[input_key]
        for input_key in original_order
        if input_key in all_results
    ]

  def _categorical_api_fallback(
      self,
      config: CategoricalEvaluationConfig,
      table: str,
      where: str,
      params: list,
      endpoint: str,
      evaluation_inputs: Optional[list[_CategoricalEvaluationInput]] = None,
  ) -> list:
    """Classifies sessions using the Gemini API (fallback).

    Fetches transcripts from BigQuery using the same
    transcript-building CTE as the ``AI.GENERATE`` path,
    then classifies each session via the Gemini API.
    """
    if evaluation_inputs is not None:
      transcripts = {
          item.evaluation_key: item.transcript for item in evaluation_inputs
      }
      contexts = {
          item.evaluation_key: item.judge_context
          for item in evaluation_inputs
          if item.judge_context is not None
      }
      selectors = {
          item.evaluation_key: item.selector for item in evaluation_inputs
      }
      return _run_sync(
          classify_sessions_via_api(
              transcripts,
              config,
              endpoint,
              per_session_context=contexts,
              resolved_selectors=selectors,
          )
      )

    query = CATEGORICAL_TRANSCRIPT_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=table,
        where=where,
    )
    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(query_parameters=list(params)),
        feature="eval-categorical",
    )
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
      ddl_config = with_sdk_labels(
          bigquery.QueryJobConfig(), feature="eval-categorical"
      )
      self.bq_client.query(ddl, job_config=ddl_config).result()

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
    # Apply labels BEFORE executor dispatch so they materialize on the
    # QueryJobConfig in the caller's thread — contextvars do not
    # propagate across run_in_executor's thread boundary.
    job_config = with_sdk_labels(job_config, feature="insights")

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
    job_config = with_sdk_labels(
        job_config, feature="insights", ai_function="ai-generate"
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
    job_config = with_sdk_labels(
        job_config, feature="insights", ai_function="ml-generate-text"
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
    job_config = with_sdk_labels(job_config, feature="insights")

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
      *,
      user_id: Any = UNSET,
      root_agent_name: Any = UNSET,
      experiment_id: Any = UNSET,
      custom_labels: Optional[dict] = None,
      scope_signature: Optional[str] = None,
      allow_mixed_scope: bool = False,
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
        user_id: Optional identity pin, with the same three-state
            semantics as :meth:`get_session_trace`.
        root_agent_name: Optional root-agent identity pin.
        experiment_id: Optional experiment scope pin.
        custom_labels: Optional subset label pins.
        scope_signature: Optional exact-scope signature pin.
        allow_mixed_scope: Opt in to the same mixed-scope escape hatch
            as :meth:`get_session_trace`.

    Returns:
        A Trace object for the resolved identity/scope.
    """
    selector = TraceSelector(
        session_id=session_id,
        user_id=user_id,
        root_agent_name=root_agent_name,
        experiment_id=experiment_id,
        custom_labels=custom_labels,
        scope_signature=scope_signature,
    )
    return self.get_trace_by_selector_gql(
        selector,
        config=config,
        allow_mixed_scope=allow_mixed_scope,
    )

  def get_trace_by_selector_gql(
      self,
      selector: TraceSelector,
      config: Optional[Any] = None,
      *,
      allow_mixed_scope: bool = False,
  ) -> Trace:
    """Reconstructs one selector-resolved trace using GQL edges.

    The shared flat resolver runs exactly once and defines the complete
    allowed span population. Under the Property Graph's unique TechNode
    ``span_id`` key contract, GQL receives those exact IDs so traversal cannot
    broaden the read to another identity or scope that reuses the same
    ``session_id``. Duplicate IDs bypass GQL. The graph result supplies only
    parent relationships; span content and trace metadata remain sourced from
    the authoritative flat trace.

    Args:
        selector: Identity and scope pins for the singular read.
        config: Optional :class:`ContextGraphConfig`.
        allow_mixed_scope: Pass through the U2 mixed-scope escape hatch.

    Returns:
        The resolved flat trace when no graph edges exist, otherwise an
        equivalent trace whose copied spans carry GQL parent links and are
        ordered chronologically.
    """
    flat_trace = self.get_trace_by_selector(
        selector, allow_mixed_scope=allow_mixed_scope
    )
    populated_span_ids = [
        span.span_id for span in flat_trace.spans if span.span_id
    ]
    span_ids = tuple(sorted(set(populated_span_ids)))
    if not span_ids:
      return flat_trace
    if len(span_ids) != len(populated_span_ids):
      # Property Graph TechNode keys require a unique span_id. A resolved
      # mixed-scope trace can legitimately preserve repeated producer rows,
      # but GQL cannot attribute an edge to one of those copies. Keep the
      # authoritative flat relationships rather than applying one graph edge
      # to an arbitrary duplicate.
      logger.warning(
          "Skipping GQL relationship reconstruction because the resolved"
          " trace contains non-unique span IDs."
      )
      return flat_trace

    mgr = self.context_graph(config=config)
    rows = mgr.reconstruct_trace_gql(
        session_id=selector.session_id,
        span_ids=span_ids,
    )

    if not rows:
      logger.info(
          "No GQL edges for session_id=%s (flat/sparse trace); "
          "using flat SQL query.",
          selector.session_id,
      )
      return flat_trace

    # Graph rows are permitted to contribute relationships only. The
    # authoritative flat trace contributes every span and all payload data,
    # so a stale or unexpectedly broad graph can never fabricate an event.
    spans = [copy.deepcopy(span) for span in flat_trace.spans]
    by_id = {span.span_id: span for span in spans if span.span_id}
    for row in rows:
      parent_id = row.get("parent_span_id")
      child_id = row.get("child_span_id")
      if parent_id in by_id and child_id in by_id:
        by_id[child_id].parent_span_id = parent_id

    # Sort by timestamp for deterministic chronological order.
    # Use epoch as fallback (timezone-aware to avoid naive/aware conflicts).
    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    spans.sort(key=lambda s: (s.timestamp or _epoch, s.span_id or ""))

    return Trace(
        trace_id=flat_trace.trace_id,
        session_id=flat_trace.session_id,
        spans=spans,
        user_id=flat_trace.user_id,
        start_time=flat_trace.start_time,
        end_time=flat_trace.end_time,
        total_latency_ms=flat_trace.total_latency_ms,
        identity=flat_trace.identity,
        scope=flat_trace.scope,
        scope_coverage=flat_trace.scope_coverage,
    )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _build_report(
    evaluator_name: str,
    dataset: str,
    session_scores: list[SessionScore],
) -> EvaluationReport:
  """Builds an EvaluationReport from session scores.

  ``total_sessions`` counts EVALUATION UNITS: a session id reused
  across identities or evaluation passes (issue #359) contributes one
  attributed row per judged trace, distinguishable via
  ``SessionScore.details`` (``user_id``, ``root_agent_name``,
  ``scope_signature``).
  """
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
  # Merge on the ATTRIBUTED evaluation unit, not session_id alone
  # (PR #371 review round 8, P2-7): per-scope expanded traces emit
  # several score rows sharing one session id, and a session_id key
  # would silently overwrite one pass's scores with another's.
  session_data: dict[tuple, dict[str, Any]] = {}

  for criterion, report in criterion_reports:
    for ss in report.session_scores:
      key = (
          ss.session_id,
          ss.details.get("user_id"),
          ss.details.get("root_agent_name"),
          ss.details.get("scope_signature"),
      )
      if key not in session_data:
        session_data[key] = {
            "session_id": ss.session_id,
            "scores": {},
            "feedback": [],
            "details": dict(ss.details),
        }
      session_data[key]["scores"].update(ss.scores)
      if ss.llm_feedback:
        session_data[key]["feedback"].append(ss.llm_feedback)

  # Build threshold lookup from criteria
  thresholds = {c.name: c.threshold for c in criteria}

  session_scores = []
  for data in session_data.values():
    scores = data["scores"]
    # Must have at least one score AND all criteria above threshold.
    # Missing criteria default to 0.0 (guaranteed fail).
    passed = bool(scores) and all(
        scores.get(c.name, 0.0) >= thresholds.get(c.name, 0.5) for c in criteria
    )
    session_scores.append(
        SessionScore(
            session_id=data["session_id"],
            scores=scores,
            passed=passed,
            llm_feedback="\n".join(data["feedback"]) or None,
            details=data["details"],
        )
    )

  return _build_report(
      evaluator_name=evaluator_name,
      dataset=dataset,
      session_scores=session_scores,
  )


# Sentinel: the row carries no SQL-side JSON_TYPE attestation (foreign
# row sources or pre-attestation fixtures).
_NO_ATTESTATION = object()


def _validated_discovery_rows(rows: list[dict]) -> list[dict]:
  """Return only SQL-attested object/null rows from a discovery page.

  Discovery SQL orders valid rows ahead of malformed rows before its
  bound, so malformed persisted values neither become public retry
  candidates nor consume sound enumeration capacity. Older/foreign
  row sources without the projected attestation remain compatible.
  A malformed-only page fails with the same redacted validation
  surface used by fetched-row materialization.
  """
  valid = [
      row
      for row in rows
      if "attributes_valid" not in row or row.get("attributes_valid") is True
  ]
  if rows and not valid:
    raise ValueError(
        "Persisted attributes must be a JSON object (contents redacted)."
    )
  return valid


def _validated_attributes_object(
    raw: Any, sql_type: Any = _NO_ATTESTATION
) -> dict:
  """Strictly validated attributes object from the RAW BigQuery cell.

  The BigQuery decoder yields Python objects for JSON columns: an
  object cell arrives as ``dict``, but a schema-valid JSON STRING
  scalar arrives as the decoded Python ``str`` — including one whose
  TEXT is itself serialized object syntax, which a bare reparse would
  accept while every SQL predicate (``JSON_VALUE`` anchors, tag
  paths) still sees a top-level string and returns NULL (PR #371
  review round 10, P1-1). ``sql_type`` is the SQL-side
  ``JSON_TYPE(attributes)`` attestation projected into the row: when
  present it is authoritative — only ``'object'`` (or absent/JSON
  null) attributes classify, so a decoded string scalar can never
  fabricate an identity or scope that SQL anchoring cannot see. Rows
  without the attestation (foreign sources) fall back to accepting a
  ``str`` only if it parses to a JSON object or null (PR #371 review
  round 9, P1-5). Everything else raises the redacted validation
  error, which bulk readers quarantine and singular readers surface.
  """
  if sql_type is not _NO_ATTESTATION:
    if sql_type is None or sql_type == "null":
      return {}
    if sql_type != "object":
      raise ValueError(
          "Persisted attributes must be a JSON object (contents redacted)."
      )
  if raw is None:
    return {}
  if isinstance(raw, dict):
    return raw
  if isinstance(raw, str):
    try:
      parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
      raise ValueError(
          "Persisted attributes must be a JSON object (contents redacted)."
      ) from exc
    if parsed is None:
      return {}
    if isinstance(parsed, dict):
      return parsed
  raise ValueError(
      "Persisted attributes must be a JSON object (contents redacted)."
  )


def _materialize_event_filtered_trace(
    results: list,
    *,
    identity: TraceIdentity,
    resolved_scope: ResolvedTraceSelector,
    scope_trace_id: Optional[str] = None,
) -> Trace:
  """Build one already-resolved scope from a SQL-filtered event subset.

  Candidate discovery has already proven ``resolved_scope``. The span query
  may contain only shared rows after event-type pushdown, so reconstructing
  candidates from that subset would incorrectly lose the proven scope.
  Instead, each returned row is validated and assigned using the same
  exact-payload/shared-row rules as :func:`_build_traces_from_rows`.
  """
  scope = resolved_scope.scope
  expected_labels = scope.labels_dict
  spans = []
  own_trace_ids = []
  for row in results:
    row_dict = dict(row)
    attributes = _validated_attributes_object(
        row_dict.get("attributes"),
        sql_type=(
            row_dict.get("attributes_type")
            if "attributes_type" in row_dict
            else _NO_ATTESTATION
        ),
    )
    row_user = _validated_identity_attr("user_id", row_dict.get("user_id"))
    row_root = _validated_identity_attr(
        "root_agent_name", attributes.get("root_agent_name")
    )
    if row_user != identity.user_id or row_root != identity.root_agent_name:
      raise ValueError(
          "Resolution/fetch consistency failure: a fetched row does not"
          " match the resolved identity (identifiers redacted). The"
          " underlying data changed between resolution and fetch; retry"
          " the lookup."
      )
    experiment = _validated_identity_attr(
        "experiment_id", attributes.get("experiment_id")
    )
    payload = _parse_tag_payload(
        attributes.get("custom_tags"), source="attributes"
    )

    # Untagged NULL-experiment rows are identity-wide shared
    # infrastructure. Otherwise the experiment must match; untagged
    # subgroup-local rows are shared within that experiment, and tagged
    # rows belong only to their exact payload.
    identity_shared = experiment is None and payload is None
    if not identity_shared:
      if experiment != scope.experiment_id:
        continue
      if payload is not None and payload != expected_labels:
        continue
      if payload is not None and row_dict.get("trace_id"):
        own_trace_ids.append(row_dict["trace_id"])

    row_dict["attributes"] = attributes
    spans.append(Span.from_bigquery_row(row_dict))

  timestamps = [span.timestamp for span in spans if span.timestamp]
  start = min(timestamps) if timestamps else None
  end = max(timestamps) if timestamps else None
  total_ms = (end - start).total_seconds() * 1000 if start and end else None
  return Trace(
      trace_id=scope_trace_id
      or (own_trace_ids[0] if own_trace_ids else identity.session_id),
      session_id=identity.session_id,
      spans=spans,
      user_id=identity.user_id,
      start_time=start,
      end_time=end,
      total_latency_ms=total_ms,
      identity=identity,
      scope=scope,
  )


def _sound_truncated_identity_rows(rows: list) -> list:
  """Soundly classifiable subset of ONE identity's cut candidate page.

  ``rows`` are one identity's candidate rows in page order —
  ``(experiment_id, tag_payload)`` with canonical encodings, untagged
  (NULL payload) first within each experiment group — whose tail may
  have been cut by a page boundary. Shared by the boundary-identity
  handling in ``get_trace_by_selector`` and the batched per-identity
  pages (PR #371 review round 9, P1-2).

  Kept: tagged rows (they independently prove their scopes) and
  untagged rows of every experiment group OTHER than the final one —
  the sort order guarantees only the final group can be incomplete,
  so earlier groups are complete and classify exactly as they would
  on a full page.

  Dropped: the final group's untagged row when no payload row of that
  group is visible (its shared-vs-sole-scope meaning depends on
  context the cut may hide), and untagged NULL-experiment rows when
  that drop removed the identity's only visible scope evidence — an
  empty-scope candidate would otherwise be manufactured for an
  identity that provably has a non-NULL experiment.
  """
  if not rows:
    return rows
  final_exp = rows[-1].get("experiment_id")
  final_group_has_payload = any(
      row.get("experiment_id") == final_exp
      and _parse_tag_payload(row.get("tag_payload")) is not None
      for row in rows
  )
  kept = []
  dropped_scope_evidence = False
  for row in rows:
    payload = _parse_tag_payload(row.get("tag_payload"))
    if payload is not None:
      kept.append(row)
      continue
    if row.get("experiment_id") == final_exp and not final_group_has_payload:
      if final_exp is not None:
        dropped_scope_evidence = True
      continue
    kept.append(row)
  if dropped_scope_evidence and not any(
      row.get("experiment_id") is not None
      or _parse_tag_payload(row.get("tag_payload")) is not None
      for row in kept
  ):
    # Every remaining row is untagged NULL-experiment context, but a
    # dropped non-NULL experiment proves the identity has real
    # scopes: the empty-scope classification is unprovable.
    kept = []
  return kept


def _no_matching_events_error(
    selector: TraceSelector, has_pins: bool
) -> ValueError:
  """Typed not-found error distinguishing absence from pin mismatch.

  A discovery query whose selector pins exclude every row must not
  claim the session itself is absent (PR #371 review round 8, P3-12):
  callers distinguishing a missing session from a scope mismatch
  would get the wrong signal.
  """
  if has_pins:
    return ValueError(
        "No events match"
        f" session_id={selector.session_id} under the selector's"
        " pins; the session may exist with rows that do not match."
        " Remove pins to enumerate the session's candidates."
    )
  return ValueError(f"No events found for session_id={selector.session_id}")


def _parse_tag_payload(
    payload: Any, source: str = "resolution"
) -> Optional[dict[str, str]]:
  """Parse one custom-tags payload into canonical labels, fail-closed.

  ``source="resolution"`` accepts the ``TO_JSON_STRING(JSON_QUERY())``
  string encoding used by the candidate queries; ``source=
  "attributes"`` accepts only the already-parsed object form found on
  ``Span.attributes`` — there a str value means the producer
  double-encoded the payload, and decoding it here would advertise a
  scope that SQL matching and candidate resolution both reject for
  the same persisted row (PR #371 review round 8, P3-11). The
  persisted schema is enforced exactly: the payload must be a JSON
  object whose values are strings. Anything else raises instead of
  silently becoming an unscoped or colliding candidate — ``{"run":
  3}`` must not canonicalize into the same scope as ``{"run": "3"}``.
  Returns ``None`` for absent/empty payloads.

  Raises:
      ValueError: If the payload is not a JSON object of strings.
  """
  if payload is None:
    return None
  if isinstance(payload, str):
    if source != "resolution":
      raise ValueError(
          "custom_tags must be a JSON object of string values; got a"
          " double-encoded string (contents redacted)."
      )
    if payload == "null":
      return None
    try:
      payload = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as exc:
      raise ValueError(
          "Malformed custom_tags payload (contents redacted): not"
          " valid JSON."
      ) from exc
  if payload is None:
    return None
  if not isinstance(payload, dict):
    raise ValueError(
        "custom_tags must be a JSON object of string values; got"
        f" {type(payload).__name__}."
    )
  if not payload:
    return None
  labels: dict[str, str] = {}
  for key, value in payload.items():
    if not isinstance(key, str) or not isinstance(value, str):
      raise ValueError(
          "custom_tags must be a JSON object of string values; found"
          f" a {type(value).__name__} entry (contents redacted)."
      )
    labels[key] = value
  return labels


def _parse_identity_attr_json(name: str, value: Any) -> Optional[str]:
  """Parse a TO_JSON_STRING identity attribute, fail-closed on type.

  ``JSON_VALUE`` erases scalar types (a persisted numeric root agent
  would silently become its string form); the candidates query reads
  ``TO_JSON_STRING`` so a non-string scalar is detectable here and
  rejected instead of colliding with a real string identity
  (PR #371 review round 3, P1-8). Values are redacted from errors.
  """
  if value is None:
    return None
  if isinstance(value, str):
    if value == "null":
      return None
    if value.startswith('"'):
      try:
        parsed = json.loads(value)
      except (json.JSONDecodeError, ValueError):
        parsed = None
      if isinstance(parsed, str):
        return parsed
  raise ValueError(
      f"Persisted attribute {name!r} must be a JSON string or null"
      " (value redacted)."
  )


def _validated_identity_attr(name: str, value: Any) -> Optional[str]:
  """Enforce the persisted string schema for identity attributes."""
  if value is None or isinstance(value, str):
    return value
  raise ValueError(
      f"Persisted attribute {name!r} must be a string; got"
      f" {type(value).__name__}."
  )


_MAX_SCOPE_CANDIDATES = 64

# Identity enumeration bound for mixed reads: identities are
# row-uniform and few in live data; the cap-plus-one sentinel marks
# larger populations as truncated.
_MAX_IDENTITIES = 8


def _candidate_sort_key(candidate: ResolvedTraceSelector) -> tuple:
  """Canonical, NULL-safe ordering for resolved candidates."""
  identity = candidate.identity
  return (
      identity.session_id,
      identity.user_id is not None,
      identity.user_id or "",
      identity.root_agent_name is not None,
      identity.root_agent_name or "",
      candidate.scope.experiment_id is not None,
      candidate.scope.experiment_id or "",
      candidate.scope_signature,
  )


def _scope_subgroups(entries: list) -> tuple[dict, list]:
  """Partition one identity's rows into experiment/scope subgroups.

  ``entries`` are ``(experiment_id, payload, item)`` triples,
  mirroring the SQL row-scope semantics:

  * Distinct non-NULL experiments are separate subgroups.
  * A NULL-experiment entry that CARRIES a tag payload keeps its own
    NULL subgroup (a genuine NULL-experiment scope).
  * An untagged NULL-experiment entry is shared conversation
    infrastructure. It is NOT expanded into every subgroup here —
    the identity-level shared pool is returned separately so callers
    attach it only to retained slots (PR #371 review round 6, P1-6:
    eager expansion was O(experiments x shared_rows)).
  * Untagged entries WITH an experiment stay local to that subgroup.

  Returns ``(subgroups, identity_shared)`` where ``identity_shared``
  is the list of untagged NULL-experiment ``(payload, item)`` pairs
  shared by every subgroup of the identity.
  """
  non_null = sorted(
      {experiment for experiment, _, _ in entries if experiment is not None}
  )
  has_null_scope_rows = any(
      experiment is None and payload is not None
      for experiment, payload, _ in entries
  )
  has_null_shared_rows = any(
      experiment is None and payload is None
      for experiment, payload, _ in entries
  )
  keys: list = list(non_null)
  if has_null_scope_rows or not non_null:
    keys.append(None)
  subgroups: dict = {key: {"payloads": {}, "items": []} for key in keys}
  identity_shared: list = []
  for experiment, payload, item in entries:
    if experiment is None and payload is None:
      identity_shared.append((payload, item))
      continue
    target = experiment if experiment is not None else None
    if target not in subgroups:
      continue
    subgroups[target]["items"].append((payload, item))
    if payload is not None:
      signature = tuple(sorted(payload.items()))
      subgroups[target]["payloads"].setdefault(signature, payload)
  # A NULL subgroup that exists only because of shared rows (no
  # non-NULL experiments at all) represents the identity's sole
  # empty scope; mark it so candidate derivation still emits it.
  if (
      None in subgroups
      and not subgroups[None]["items"]
      and (not non_null and has_null_shared_rows)
  ):
    subgroups[None]["items"] = []  # empty own items; shared carries rows
  return subgroups, identity_shared


def _resolve_scope_candidates(rows: list[dict]) -> list[ResolvedTraceSelector]:
  """Derive resolved candidates from per-session aggregation rows.

  Each input row is one distinct ``(user_id, root_agent_name,
  experiment_id, tag_payload)`` combination observed in the session
  (see ``_RESOLVE_SESSION_CANDIDATES_QUERY``). Candidates split by
  intrinsic identity, then by experiment, then by EXACT distinct tag
  payload. The result is canonically sorted so ambiguity payloads
  are deterministic regardless of row order.
  """
  groups: dict[tuple, list] = {}
  for row in rows:
    identity_key = (
        row.get("session_id"),
        _validated_identity_attr("user_id", row.get("user_id")),
        _parse_identity_attr_json(
            "root_agent_name", row.get("root_agent_name")
        ),
    )
    groups.setdefault(identity_key, []).append(
        (
            _parse_identity_attr_json(
                "experiment_id", row.get("experiment_id")
            ),
            _parse_tag_payload(row.get("tag_payload")),
            None,
        )
    )

  candidates: list[ResolvedTraceSelector] = []
  for (session_id, user_id, root_agent_name), entries in groups.items():
    identity = TraceIdentity(
        session_id=session_id,
        user_id=user_id,
        root_agent_name=root_agent_name,
    )
    subgroups, identity_shared = _scope_subgroups(entries)
    for experiment, subgroup in subgroups.items():
      payload_options = list(subgroup["payloads"].values())
      if not payload_options:
        # An empty-scope candidate exists when the subgroup has its
        # own untagged rows, or when the identity consists solely of
        # shared rows (no experiments anywhere).
        if subgroup["items"] or (experiment is None and identity_shared):
          payload_options = [None]
        else:
          continue
      for payload in payload_options:
        candidates.append(
            ResolvedTraceSelector(
                identity=identity,
                scope=TraceScope(
                    experiment_id=experiment, custom_labels=payload
                ),
            )
        )
  candidates = list(dict.fromkeys(candidates))
  candidates.sort(key=_candidate_sort_key)
  return candidates


def _resolved_scope_trace_id(
    rows: list[dict], candidate: ResolvedTraceSelector
) -> Optional[str]:
  """Return the unfiltered candidate page's trace ID for one exact scope.

  Event filtering can remove every tagged row from the payload fetch.
  Candidate discovery is intentionally unfiltered, so its per-payload
  aggregate remains the stable source for the scope's producer trace ID.
  Untagged rows are shared infrastructure and never supply a scope-specific
  ID.
  """
  expected_labels = candidate.scope.labels_dict
  for row in rows:
    identity = TraceIdentity(
        session_id=row.get("session_id"),
        user_id=_validated_identity_attr("user_id", row.get("user_id")),
        root_agent_name=_parse_identity_attr_json(
            "root_agent_name", row.get("root_agent_name")
        ),
    )
    if identity != candidate.identity:
      continue
    experiment = _parse_identity_attr_json(
        "experiment_id", row.get("experiment_id")
    )
    payload = _parse_tag_payload(row.get("tag_payload"))
    if (
        experiment != candidate.scope.experiment_id
        or payload is None
        or payload != expected_labels
    ):
      continue
    trace_id = row.get("scope_trace_id")
    if trace_id is None:
      return None
    if type(trace_id) is not str:
      raise ValueError(
          "Persisted trace_id must be a string (contents redacted)."
      )
    if trace_id == "":
      return None
    return trace_id
  return None


def _candidates_matching_selector(
    candidates: list[ResolvedTraceSelector],
    selector: TraceSelector,
) -> list[ResolvedTraceSelector]:
  """Filter resolved candidates by the caller's selector pins.

  UNSET dimensions are unpinned; explicit ``None`` pins match only
  NULL identity values; label pins are subset requirements on the
  candidate scope; ``scope_signature`` pins the exact scope.
  """
  matching = []
  for candidate in candidates:
    if (
        selector.user_id is not UNSET
        and candidate.identity.user_id != selector.user_id
    ):
      continue
    if (
        selector.root_agent_name is not UNSET
        and candidate.identity.root_agent_name != selector.root_agent_name
    ):
      continue
    if (
        selector.experiment_id is not UNSET
        and candidate.scope.experiment_id != selector.experiment_id
    ):
      continue
    if selector.custom_labels:
      scope_labels = candidate.scope.labels_dict
      if any(
          scope_labels.get(key) != value
          for key, value in selector.custom_labels
      ):
        continue
    if (
        selector.scope_signature is not None
        and candidate.scope_signature != selector.scope_signature
    ):
      continue
    matching.append(candidate)
  return matching


def _isolated_span_copy(span: Span) -> Span:
  """Deep copy of a span shared across sibling traces.

  A shallow copy would leave ``content``/``attributes``/
  ``content_parts`` aliased between traces, and ``_build_tree()``
  mutates ``children`` (PR #371 review round 3, P1-2).
  """
  copied = copy.deepcopy(span)
  copied.children = []
  return copied


def _ordered_limited_traces(
    traces: list[Trace], limit: Optional[int]
) -> list[Trace]:
  """Deterministically order resolved traces and apply the limit.

  The SQL LIMIT bounds intrinsic identity anchors, but scope
  splitting can expand one identity into several traces, so the
  caller-facing limit is re-applied after expansion. NULL and
  empty-string identity values sort distinctly (presence flag before
  value) so ties cannot flip under input reordering.
  """

  def sort_key(trace: Trace) -> tuple:
    end = trace.end_time
    recency = -(end.timestamp()) if end is not None else float("inf")
    identity = trace.identity
    user = identity.user_id if identity else None
    root = identity.root_agent_name if identity else None
    return (
        recency,
        trace.session_id,
        user is not None,
        user or "",
        root is not None,
        root or "",
        trace.scope.scope_signature if trace.scope is not None else "",
    )

  ordered = sorted(traces, key=sort_key)
  if limit is not None:
    ordered = ordered[:limit]
  return ordered


def _fetched_scopes(entries: list) -> list:
  """Distinct TraceScope objects present in fetched rows.

  Computed with set arithmetic only — no per-row cross-product
  expansion (PR #371 review round 5, P2-8). Mirrors _scope_subgroups
  classification: non-NULL experiments are scopes; tagged NULL-
  experiment rows keep a NULL-experiment scope; untagged rows
  contribute an empty scope only when a subgroup has no payloads.
  """
  non_null = sorted(
      {experiment for experiment, _, _ in entries if experiment is not None}
  )
  has_null_scope_rows = any(
      experiment is None and payload is not None
      for experiment, payload, _ in entries
  )
  keys: list = list(non_null)
  if has_null_scope_rows or not non_null:
    keys.append(None)
  payloads_by_key: dict = {key: {} for key in keys}
  for experiment, payload, _ in entries:
    if payload is None:
      continue
    target = experiment if experiment is not None else None
    if target in payloads_by_key:
      payloads_by_key[target].setdefault(
          tuple(sorted(payload.items())), payload
      )
  scopes = []
  for key in keys:
    payloads = list(payloads_by_key[key].values()) or [None]
    for payload in payloads:
      scopes.append(TraceScope(experiment_id=key, custom_labels=payload))
  return scopes


def _slot_sort_key(slot: dict) -> tuple:
  """Ranking for scope slots, mirroring _ordered_limited_traces."""
  end = slot["max_ts"]
  recency = -(end.timestamp()) if end is not None else float("inf")
  return (
      recency,
      slot["session_id"],
      slot["user_id"] is not None,
      slot["user_id"] or "",
      slot["root_agent_name"] is not None,
      slot["root_agent_name"] or "",
      slot["signature"],
  )


def _filter_scope_predicate(filt: "TraceFilter") -> Optional[Any]:
  """Slot-level predicate for the filter's scope pins, or None.

  The session CTE anchors identities and the outer expansion admits
  shared rows, so an identity selected by a label/experiment filter
  can reconstruct sibling scopes that never matched the filter
  (PR #371 review round 5, P1-4). Label pins are subset requirements;
  the experiment pin follows the filter's tri-state semantics.
  Session-level filters (errors, event types, time, latency)
  intentionally return every scope of their matching identities —
  complete-trace semantics per R6.
  """
  label_pins = list((filt.custom_labels or {}).items())
  experiment_pin = filt.experiment_id
  if not label_pins and experiment_pin is None:
    return None

  def predicate(scope: Optional[TraceScope]) -> bool:
    labels = scope.labels_dict if scope is not None else {}
    if label_pins and any(
        labels.get(key) != value for key, value in label_pins
    ):
      return False
    if experiment_pin is not None:
      scope_experiment = scope.experiment_id if scope is not None else None
      if experiment_pin is SQL_NULL:
        return scope_experiment is None
      return scope_experiment == experiment_pin
    return True

  return predicate


def _build_traces_from_rows(
    results: list,
    max_traces: Optional[int] = None,
    scope_predicate: Optional[Any] = None,
    span_predicate: Optional[Any] = None,
    population_complete: bool = True,
    on_malformed: str = "raise",
    _reported_quarantined_groups: Optional[set[tuple]] = None,
) -> list[Trace]:
  """Groups BigQuery result rows into Trace objects.

  Shared by ``list_traces`` and ``_api_judge`` to ensure consistent
  trace construction. Issue #359 (U2): rows are grouped by the FULL
  resolved selector — intrinsic identity (session, user, root agent)
  plus resolved scope (experiment, EXACT tag payload) — instead of
  ``session_id`` alone, so two identities or evaluation passes that
  reuse one session id yield two Trace objects rather than silently
  merging.

  Construction is rank-before-retain with FACTORED shared rows
  (PR #371 review round 5, P1-7): a scope slot stores only the
  indices of its OWN rows; shared rows stay factored per subgroup
  and are attached — and deep-copied where needed — only for the
  retained top ``max_traces`` slots, so the bound limits time and
  memory, not just the returned count.

  ``on_malformed`` bounds the blast radius of malformed persisted
  rows (PR #371 review round 8, P2-6). ``"raise"`` (singular reads)
  keeps the fail-closed typed error. ``"quarantine"`` (bulk listings
  and evaluation fetches) excludes ONLY the identity the malformed
  row belongs to — at the finest granularity its readable fields
  prove — so one poison row cannot brick reads for every session in
  the table; quarantined identities are counted and logged (contents
  redacted), never silently classified.
  """
  identity_groups: dict[tuple, list] = {}
  # Quarantine keys, coarsest-first: a row whose user_id cannot be
  # validated poisons its session. An unreadable attributes object or
  # root agent poisons its exact projected SQL anchor when available,
  # otherwise (session, user). A malformed experiment or tag payload
  # poisons the fully-identified identity.
  poisoned_sessions: set = set()
  poisoned_users: set = set()
  poisoned_identities: set = set()

  for row in results:
    row_dict = dict(row)
    sid = row_dict.get("session_id", "unknown")
    try:
      user_id = _validated_identity_attr("user_id", row_dict.get("user_id"))
    except ValueError:
      if on_malformed != "quarantine":
        raise
      poisoned_sessions.add(sid)
      continue
    try:
      # Validated from the RAW cell, not Span's lenient display
      # normalization (PR #371 review round 9, P1-5), under the
      # SQL-side JSON_TYPE attestation when the row carries it
      # (PR #371 review round 10, P1-1): a decoded string scalar
      # whose text is serialized object syntax must not fabricate an
      # identity or scope the SQL anchors cannot see.
      attributes = _validated_attributes_object(
          row_dict.get("attributes"),
          sql_type=(
              row_dict.get("attributes_type")
              if "attributes_type" in row_dict
              else _NO_ATTESTATION
          ),
      )
      root_agent_name = _validated_identity_attr(
          "root_agent_name", attributes.get("root_agent_name")
      )
    except ValueError:
      if on_malformed != "quarantine":
        raise
      anchor_identity = None
      if "anchor_user_id" in row_dict and "anchor_root_agent_name" in row_dict:
        try:
          anchor_identity = (
              sid,
              _validated_identity_attr(
                  "anchor_user_id", row_dict.get("anchor_user_id")
              ),
              _validated_identity_attr(
                  "anchor_root_agent_name",
                  row_dict.get("anchor_root_agent_name"),
              ),
          )
        except ValueError:
          anchor_identity = None
      if anchor_identity is None:
        poisoned_users.add((sid, user_id))
      else:
        poisoned_identities.add(anchor_identity)
      continue
    identity_key = (sid, user_id, root_agent_name)
    try:
      experiment = _validated_identity_attr(
          "experiment_id", attributes.get("experiment_id")
      )
      payload = _parse_tag_payload(
          attributes.get("custom_tags"), source="attributes"
      )
    except ValueError:
      if on_malformed != "quarantine":
        raise
      poisoned_identities.add(identity_key)
      continue
    # Parse the remaining span fields only after authoritative
    # attributes validation, and pass the normalized object onward so
    # Span never reparses the persisted scalar.
    row_dict["attributes"] = attributes
    span = Span.from_bigquery_row(row_dict)
    identity_groups.setdefault(identity_key, []).append(
        (experiment, payload, span)
    )

  if poisoned_sessions or poisoned_users or poisoned_identities:
    contaminated = [
        key
        for key in identity_groups
        if key[0] in poisoned_sessions
        or (key[0], key[1]) in poisoned_users
        or key in poisoned_identities
    ]
    for key in contaminated:
      del identity_groups[key]
    newly_contaminated = contaminated
    if _reported_quarantined_groups is not None:
      newly_contaminated = [
          key for key in contaminated if key not in _reported_quarantined_groups
      ]
      _reported_quarantined_groups.update(contaminated)
    if newly_contaminated:
      logging.getLogger(__name__).warning(
          "Quarantined %d identity group(s) after Python validation of"
          " materialized attributes (identifiers and contents redacted)."
          " SDK listing SQL excludes attested top-level non-object rows"
          " before this stage, so listing warnings cover object-typed"
          " rows with malformed identity/scope fields. Remaining results"
          " are unaffected; use a matching singular selector for the"
          " typed validation error.",
          len(newly_contaminated),
      )

  # Phase 1: define slots; shared rows stay factored — subgroup-local
  # untagged rows per subgroup, and untagged NULL-experiment rows in
  # ONE identity-level pool (PR #371 review round 6, P1-6).
  slots: list[dict] = []
  for (sid, user_id, root_agent_name), entries in identity_groups.items():
    identity = None
    if isinstance(sid, str):
      try:
        identity = TraceIdentity(
            session_id=sid,
            user_id=user_id,
            root_agent_name=root_agent_name,
        )
      except (TypeError, ValueError):
        identity = None
    indexed_entries = list(enumerate(entries))
    subgroups, identity_shared_raw = _scope_subgroups(
        [
            (exp, payload, (index, span))
            for index, (exp, payload, span) in indexed_entries
        ]
    )
    identity_shared = [item for _, item in identity_shared_raw]
    shared_max_ts = None
    shared_trace_ids = []
    for _, span in identity_shared:
      if span.trace_id:
        shared_trace_ids.append(span.trace_id)
      if span.timestamp is not None and (
          shared_max_ts is None or span.timestamp > shared_max_ts
      ):
        shared_max_ts = span.timestamp
    identity_slots: list[dict] = []
    for experiment, subgroup in subgroups.items():
      items = subgroup["items"]
      local_shared = [item for payload, item in items if payload is None]
      local_shared_max = None
      local_shared_ids = []
      for _, span in local_shared:
        if span.trace_id:
          local_shared_ids.append(span.trace_id)
        if span.timestamp is not None and (
            local_shared_max is None or span.timestamp > local_shared_max
        ):
          local_shared_max = span.timestamp
      payload_items = list(subgroup["payloads"].items())
      scope_options = payload_items
      if not scope_options:
        if items or (experiment is None and identity_shared):
          scope_options = [(None, None)]
        else:
          continue
      slot_index: dict = {}
      for signature_key, payload in scope_options:
        scope = None
        if identity is not None:
          scope = TraceScope(experiment_id=experiment, custom_labels=payload)
        base_ts = shared_max_ts
        if local_shared_max is not None and (
            base_ts is None or local_shared_max > base_ts
        ):
          base_ts = local_shared_max
        slot = {
            "identity": identity,
            "scope": scope,
            "payload": payload,
            "session_id": sid,
            "user_id": user_id if isinstance(user_id, str) else None,
            "root_agent_name": (
                root_agent_name if isinstance(root_agent_name, str) else None
            ),
            "signature": scope.scope_signature if scope is not None else "",
            "own": [],
            "own_trace_ids": [],
            "local_shared": local_shared,
            "local_shared_ids": local_shared_ids,
            "identity_shared": identity_shared,
            "identity_shared_ids": shared_trace_ids,
            "subgroup_slot_total": len(scope_options),
            "max_ts": base_ts,
        }
        slot_index[signature_key] = slot
        identity_slots.append(slot)
        slots.append(slot)
      for payload, item in items:
        if payload is None:
          continue
        slot = slot_index.get(tuple(sorted(payload.items())))
        if slot is None:
          continue
        index, span = item
        slot["own"].append((index, span))
        if span.trace_id:
          slot["own_trace_ids"].append(span.trace_id)
        if span.timestamp is not None and (
            slot["max_ts"] is None or span.timestamp > slot["max_ts"]
        ):
          slot["max_ts"] = span.timestamp
    # Store the census as an integer, not the shared list: a slot
    # holding the list that holds the slot is a reference cycle, so
    # unretained slots and their spans would outlive materialization
    # until cyclic GC (PR #371 review round 9, P3-1).
    for identity_slot in identity_slots:
      identity_slot["identity_slot_total"] = len(identity_slots)

  # Phase 2: scope-filter, rank, and retain BEFORE attaching shared
  # rows — the caller's scope pins drop non-matching slots here so
  # the retained count is both correct and bounded.
  slots = [
      slot
      for slot in slots
      if slot["own"] or slot["local_shared"] or slot["identity_shared"]
  ]
  if scope_predicate is not None:
    slots = [slot for slot in slots if scope_predicate(slot["scope"])]
  slots.sort(key=_slot_sort_key)
  if span_predicate is not None:
    # Apply content eligibility before the public limit while spans are
    # still factored references. This lets the anchor fetch refill after
    # a rejected newest trace without materializing/deep-copying every
    # scope in the page (U4 categorical transcript HAVING parity).
    eligible_slots = []
    for slot in slots:
      ordered_spans = [
          span
          for _, span in sorted(
              slot["own"] + slot["local_shared"] + slot["identity_shared"],
              key=lambda pair: pair[0],
          )
      ]
      if span_predicate(ordered_spans):
        eligible_slots.append(slot)
    slots = eligible_slots
  if max_traces is not None:
    slots = slots[:max_traces]

  # Phase 3: materialize retained slots only; the identity-level
  # shared pool is attached here for the first time, and deep copies
  # happen only where a span lands in more than one RETAINED slot.
  destination_counts: dict[int, int] = {}
  for slot in slots:
    for _, span in slot["own"]:
      destination_counts[id(span)] = destination_counts.get(id(span), 0) + 1
    for _, span in slot["local_shared"]:
      destination_counts[id(span)] = destination_counts.get(id(span), 0) + 1
    for _, span in slot["identity_shared"]:
      destination_counts[id(span)] = destination_counts.get(id(span), 0) + 1
  seen: set[int] = set()
  traces: list[Trace] = []
  for slot in slots:
    ordered = sorted(
        slot["own"] + slot["local_shared"] + slot["identity_shared"],
        key=lambda pair: pair[0],
    )
    spans = []
    for _, span in ordered:
      marker = id(span)
      if destination_counts[marker] > 1:
        if marker in seen:
          spans.append(_isolated_span_copy(span))
        else:
          seen.add(marker)
          spans.append(span)
      else:
        spans.append(span)
    timestamps = [s.timestamp for s in spans if s.timestamp]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    total_ms = None
    if start and end:
      total_ms = (end - start).total_seconds() * 1000
    # Per-scope trace id (PR #371 review round 6, P2-8): the scope's
    # OWN rows first. Shared-row ids are trusted only when the
    # fetched rows are the identity's COMPLETE population (PR #371
    # review round 7, P1-5) AND the sharing pool is unambiguous —
    # judged at the pool's OWN granularity (PR #371 review round 8,
    # P2-3): a subgroup-local untagged row carries its experiment id,
    # so it can only belong to a slot of ITS subgroup and its id is
    # sound whenever that subgroup has a single slot, even when
    # sibling experiments exist. Only the identity-level pool
    # (untagged NULL-experiment rows), which is distributed across
    # every subgroup, needs the identity-level slot census.
    identity_slot_total = slot["identity_slot_total"]
    if slot["own_trace_ids"]:
      trace_id = slot["own_trace_ids"][0]
    elif (
        population_complete
        and slot["subgroup_slot_total"] == 1
        and slot["local_shared_ids"]
    ):
      trace_id = slot["local_shared_ids"][0]
    elif (
        population_complete
        and identity_slot_total == 1
        and slot["identity_shared_ids"]
    ):
      trace_id = slot["identity_shared_ids"][0]
    else:
      trace_id = slot["session_id"]
    identity = slot["identity"]
    traces.append(
        Trace(
            trace_id=trace_id,
            session_id=slot["session_id"],
            spans=spans,
            user_id=identity.user_id if identity else slot["user_id"],
            start_time=start,
            end_time=end,
            total_latency_ms=total_ms,
            identity=identity,
            scope=slot["scope"] if identity is not None else None,
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
              details={**ss.details, "parse_error": True},
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
