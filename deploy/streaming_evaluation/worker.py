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

"""Scheduled streaming evaluation worker logic.

This module keeps Cloud Scheduler request handling, overlap-window
scanning, SDK execution, and BigQuery persistence independent of Flask
so the Cloud Run entrypoint can stay thin and the behavior can be unit
tested without a live server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
import os
from typing import Any

from google.cloud import bigquery

from bigquery_agent_analytics import TraceFilter
from bigquery_agent_analytics._deploy_runtime import build_client_from_context
from bigquery_agent_analytics._deploy_runtime import resolve_client_options
from bigquery_agent_analytics._streaming_evaluation import build_streaming_observability_evaluator
from bigquery_agent_analytics._streaming_evaluation import compute_scan_start
from bigquery_agent_analytics._streaming_evaluation import DEFAULT_INITIAL_LOOKBACK_MINUTES
from bigquery_agent_analytics._streaming_evaluation import DEFAULT_OVERLAP_MINUTES
from bigquery_agent_analytics._streaming_evaluation import parse_trigger_row
from bigquery_agent_analytics._streaming_evaluation import serialize_streaming_result_row
from bigquery_agent_analytics._streaming_evaluation import STREAMING_PROCESSOR_NAME
from bigquery_agent_analytics._streaming_evaluation import STREAMING_RESULTS_TABLE
from bigquery_agent_analytics._streaming_evaluation import STREAMING_RUNS_TABLE
from bigquery_agent_analytics._streaming_evaluation import STREAMING_STATE_TABLE

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

_TRIGGER_SCAN_QUERY = """\
SELECT
  session_id,
  trace_id,
  span_id,
  event_type,
  status,
  error_message,
  timestamp AS trigger_timestamp,
  CASE
    WHEN event_type = 'AGENT_COMPLETED' THEN 'session_terminal'
    ELSE 'error_event'
  END AS trigger_kind
FROM `{project}.{dataset}.{table}`
WHERE timestamp >= @scan_start
  AND timestamp < @scan_end
  AND session_id IS NOT NULL
  AND (
    event_type = 'AGENT_COMPLETED'
    OR event_type = 'TOOL_ERROR'
    OR (
      status = 'ERROR'
      AND error_message IS NOT NULL
    )
  )
ORDER BY trigger_timestamp ASC
"""

_RESULTS_MERGE_QUERY = """\
MERGE `{project}.{dataset}.{table}` T
USING (
  SELECT
    @dedupe_key AS dedupe_key,
    @session_id AS session_id,
    @trace_id AS trace_id,
    @span_id AS span_id,
    @trigger_kind AS trigger_kind,
    @trigger_event_type AS trigger_event_type,
    @trigger_timestamp AS trigger_timestamp,
    @is_final AS is_final,
    @evaluator_profile AS evaluator_profile,
    @passed AS passed,
    @aggregate_scores_json AS aggregate_scores_json,
    @details_json AS details_json,
    @report_json AS report_json,
    @processed_at AS processed_at
) S
ON T.dedupe_key = S.dedupe_key
WHEN NOT MATCHED THEN
  INSERT (
    dedupe_key,
    session_id,
    trace_id,
    span_id,
    trigger_kind,
    trigger_event_type,
    trigger_timestamp,
    is_final,
    evaluator_profile,
    passed,
    aggregate_scores_json,
    details_json,
    report_json,
    processed_at
  )
  VALUES (
    S.dedupe_key,
    S.session_id,
    S.trace_id,
    S.span_id,
    S.trigger_kind,
    S.trigger_event_type,
    S.trigger_timestamp,
    S.is_final,
    S.evaluator_profile,
    S.passed,
    S.aggregate_scores_json,
    S.details_json,
    S.report_json,
    S.processed_at
  )
"""

_LOAD_STATE_QUERY = """\
SELECT checkpoint_timestamp
FROM `{project}.{dataset}.{table}`
WHERE processor_name = @processor_name
LIMIT 1
"""

_STATE_MERGE_QUERY = """\
MERGE `{project}.{dataset}.{table}` T
USING (
  SELECT
    @processor_name AS processor_name,
    @checkpoint_timestamp AS checkpoint_timestamp,
    @updated_at AS updated_at
) S
ON T.processor_name = S.processor_name
WHEN MATCHED THEN
  UPDATE SET
    checkpoint_timestamp = S.checkpoint_timestamp,
    updated_at = S.updated_at
WHEN NOT MATCHED THEN
  INSERT (
    processor_name,
    checkpoint_timestamp,
    updated_at
  )
  VALUES (
    S.processor_name,
    S.checkpoint_timestamp,
    S.updated_at
  )
"""

_RUNS_MERGE_QUERY = """\
MERGE `{project}.{dataset}.{table}` T
USING (
  SELECT
    @processor_name AS processor_name,
    @run_started_at AS run_started_at,
    @run_finished_at AS run_finished_at,
    @scan_start AS scan_start,
    @scan_end AS scan_end,
    @trigger_rows_found AS trigger_rows_found,
    @processed_rows AS processed_rows,
    @duplicate_rows AS duplicate_rows,
    @ignored_rows AS ignored_rows,
    @status AS status,
    @error_message AS error_message
) S
ON T.processor_name = S.processor_name
  AND T.run_started_at = S.run_started_at
WHEN MATCHED THEN
  UPDATE SET
    run_finished_at = S.run_finished_at,
    scan_start = S.scan_start,
    scan_end = S.scan_end,
    trigger_rows_found = S.trigger_rows_found,
    processed_rows = S.processed_rows,
    duplicate_rows = S.duplicate_rows,
    ignored_rows = S.ignored_rows,
    status = S.status,
    error_message = S.error_message
WHEN NOT MATCHED THEN
  INSERT (
    processor_name,
    run_started_at,
    run_finished_at,
    scan_start,
    scan_end,
    trigger_rows_found,
    processed_rows,
    duplicate_rows,
    ignored_rows,
    status,
    error_message
  )
  VALUES (
    S.processor_name,
    S.run_started_at,
    S.run_finished_at,
    S.scan_start,
    S.scan_end,
    S.trigger_rows_found,
    S.processed_rows,
    S.duplicate_rows,
    S.ignored_rows,
    S.status,
    S.error_message
  )
"""


@dataclass(frozen=True)
class RuntimeConfig:
  """Deployment configuration resolved from env vars."""

  source_project: str
  source_dataset: str
  source_table: str
  result_project: str
  result_dataset: str
  result_table: str
  state_project: str
  state_dataset: str
  state_table: str
  runs_project: str
  runs_dataset: str
  runs_table: str
  processor_name: str
  overlap: timedelta
  initial_lookback: timedelta


@dataclass
class RunStats:
  """Counters for one scheduled worker run."""

  trigger_rows_found: int = 0
  processed_rows: int = 0
  duplicate_rows: int = 0
  ignored_rows: int = 0


def handle_scheduled_run(
    body: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], int]:
  """Process one Cloud Scheduler invocation."""
  del body  # Reserved for future scheduler metadata.

  run_started_at = now or datetime.now(timezone.utc)
  client = None
  config = None
  scan_start = None
  stats = RunStats()
  success_run_finalized = False

  try:
    client = build_client_from_context({})
    config = load_runtime_config(client)
    checkpoint_timestamp = load_checkpoint(client, config)
    scan_start = compute_scan_start(
        run_started_at=run_started_at,
        checkpoint_timestamp=checkpoint_timestamp,
        overlap=config.overlap,
        initial_lookback=config.initial_lookback,
    )
    evaluator = build_streaming_observability_evaluator()

    for trigger in iter_triggers(client, config, scan_start, run_started_at):
      stats.trigger_rows_found += 1
      if isinstance(trigger, _IgnoredTrigger):
        stats.ignored_rows += 1
        continue
      if not trigger.session_id:
        logger.warning("Ignoring trigger row without session_id")
        stats.ignored_rows += 1
        continue

      report = client.evaluate(
          evaluator=evaluator,
          filters=TraceFilter.from_cli_args(
              session_id=trigger.session_id,
              limit=1,
          ),
      )
      if report.total_sessions == 0:
        logger.warning(
            "No events found for session_id=%s; skipping trigger",
            trigger.session_id,
        )
        stats.ignored_rows += 1
        continue

      result_row = serialize_streaming_result_row(trigger, report)
      inserted = persist_result_row(client, config, result_row)
      if inserted:
        stats.processed_rows += 1
      else:
        stats.duplicate_rows += 1

    write_run_history(
        client=client,
        config=config,
        run_started_at=run_started_at,
        run_finished_at=datetime.now(timezone.utc),
        scan_start=scan_start,
        scan_end=run_started_at,
        stats=stats,
        status="success",
        error_message=None,
    )
    update_checkpoint(client, config, run_started_at)
    success_run_finalized = True
  except Exception as exc:  # pragma: no cover - exercised in tests
    logger.exception("Scheduled streaming evaluation run failed")
    if client is not None and config is not None and not success_run_finalized:
      try:
        write_run_history(
            client=client,
            config=config,
            run_started_at=run_started_at,
            run_finished_at=datetime.now(timezone.utc),
            scan_start=scan_start or run_started_at,
            scan_end=run_started_at,
            stats=stats,
            status="failed",
            error_message=str(exc),
        )
      except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to persist streaming run history")
    return {"status": "retry", "reason": "internal error"}, 500

  return {
      "status": "processed",
      "processor_name": config.processor_name,
      "scan_start": scan_start.isoformat(),
      "scan_end": run_started_at.isoformat(),
      "trigger_rows_found": stats.trigger_rows_found,
      "processed_rows": stats.processed_rows,
      "duplicate_rows": stats.duplicate_rows,
      "ignored_rows": stats.ignored_rows,
  }, 200


def load_runtime_config(client) -> RuntimeConfig:
  """Resolve worker configuration from deployment env vars."""
  source_project = client.project_id
  source_dataset = client.dataset_id
  source_table = client.table_id

  result_project = os.environ.get("BQ_AGENT_RESULT_PROJECT", source_project)
  result_dataset = os.environ.get("BQ_AGENT_RESULT_DATASET", source_dataset)
  result_table = os.environ.get(
      "BQ_AGENT_RESULT_TABLE", STREAMING_RESULTS_TABLE
  )

  state_project = os.environ.get("BQ_AGENT_STATE_PROJECT", result_project)
  state_dataset = os.environ.get("BQ_AGENT_STATE_DATASET", result_dataset)
  state_table = os.environ.get("BQ_AGENT_STATE_TABLE", STREAMING_STATE_TABLE)

  runs_project = os.environ.get("BQ_AGENT_RUNS_PROJECT", result_project)
  runs_dataset = os.environ.get("BQ_AGENT_RUNS_DATASET", result_dataset)
  runs_table = os.environ.get("BQ_AGENT_RUNS_TABLE", STREAMING_RUNS_TABLE)

  overlap_minutes = _env_int(
      "BQ_AGENT_OVERLAP_MINUTES",
      DEFAULT_OVERLAP_MINUTES,
  )
  initial_lookback_minutes = _env_int(
      "BQ_AGENT_INITIAL_LOOKBACK_MINUTES",
      DEFAULT_INITIAL_LOOKBACK_MINUTES,
  )

  return RuntimeConfig(
      source_project=source_project,
      source_dataset=source_dataset,
      source_table=source_table,
      result_project=result_project,
      result_dataset=result_dataset,
      result_table=result_table,
      state_project=state_project,
      state_dataset=state_dataset,
      state_table=state_table,
      runs_project=runs_project,
      runs_dataset=runs_dataset,
      runs_table=runs_table,
      processor_name=os.environ.get(
          "BQ_AGENT_PROCESSOR_NAME",
          STREAMING_PROCESSOR_NAME,
      ),
      overlap=timedelta(minutes=overlap_minutes),
      initial_lookback=timedelta(minutes=initial_lookback_minutes),
  )


def iter_triggers(
    client, config: RuntimeConfig, scan_start: datetime, scan_end: datetime
):
  """Yield normalized triggers from the overlap scan query."""
  query = _TRIGGER_SCAN_QUERY.format(
      project=config.source_project,
      dataset=config.source_dataset,
      table=config.source_table,
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("scan_start", "TIMESTAMP", scan_start),
          bigquery.ScalarQueryParameter("scan_end", "TIMESTAMP", scan_end),
      ]
  )

  rows = client.bq_client.query(query, job_config=job_config).result()
  for row in rows:
    row_dict = _row_to_dict(row)
    try:
      yield parse_trigger_row(row_dict)
    except ValueError as exc:
      logger.warning("Ignoring malformed trigger row: %s", exc)
      yield _IgnoredTrigger(row_dict, str(exc))


def load_checkpoint(client, config: RuntimeConfig) -> datetime | None:
  """Load the last successful run checkpoint timestamp."""
  query = _LOAD_STATE_QUERY.format(
      project=config.state_project,
      dataset=config.state_dataset,
      table=config.state_table,
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "processor_name",
              "STRING",
              config.processor_name,
          )
      ]
  )
  rows = list(client.bq_client.query(query, job_config=job_config).result())
  if not rows:
    return None
  return _row_to_dict(rows[0]).get("checkpoint_timestamp")


def persist_result_row(
    client,
    config: RuntimeConfig,
    row: dict[str, Any],
) -> bool:
  """Persist one result row idempotently via BigQuery MERGE."""
  query = _RESULTS_MERGE_QUERY.format(
      project=config.result_project,
      dataset=config.result_dataset,
      table=config.result_table,
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "dedupe_key", "STRING", row["dedupe_key"]
          ),
          bigquery.ScalarQueryParameter(
              "session_id", "STRING", row["session_id"]
          ),
          bigquery.ScalarQueryParameter("trace_id", "STRING", row["trace_id"]),
          bigquery.ScalarQueryParameter("span_id", "STRING", row["span_id"]),
          bigquery.ScalarQueryParameter(
              "trigger_kind",
              "STRING",
              row["trigger_kind"],
          ),
          bigquery.ScalarQueryParameter(
              "trigger_event_type",
              "STRING",
              row["trigger_event_type"],
          ),
          bigquery.ScalarQueryParameter(
              "trigger_timestamp",
              "TIMESTAMP",
              row["trigger_timestamp"],
          ),
          bigquery.ScalarQueryParameter("is_final", "BOOL", row["is_final"]),
          bigquery.ScalarQueryParameter(
              "evaluator_profile",
              "STRING",
              row["evaluator_profile"],
          ),
          bigquery.ScalarQueryParameter("passed", "BOOL", row["passed"]),
          bigquery.ScalarQueryParameter(
              "aggregate_scores_json",
              "STRING",
              row["aggregate_scores_json"],
          ),
          bigquery.ScalarQueryParameter(
              "details_json",
              "STRING",
              row["details_json"],
          ),
          bigquery.ScalarQueryParameter(
              "report_json", "STRING", row["report_json"]
          ),
          bigquery.ScalarQueryParameter(
              "processed_at",
              "TIMESTAMP",
              row["processed_at"],
          ),
      ]
  )

  job = client.bq_client.query(query, job_config=job_config)
  job.result()
  affected_rows = getattr(job, "num_dml_affected_rows", None)
  if affected_rows is None:
    logger.warning(
        "BigQuery DML stats were unavailable for dedupe_key=%s; "
        "treating the row as processed",
        row["dedupe_key"],
    )
    return True
  return bool(affected_rows)


def update_checkpoint(
    client,
    config: RuntimeConfig,
    checkpoint_timestamp: datetime,
) -> None:
  """Advance the hidden processor checkpoint after a successful run."""
  query = _STATE_MERGE_QUERY.format(
      project=config.state_project,
      dataset=config.state_dataset,
      table=config.state_table,
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "processor_name",
              "STRING",
              config.processor_name,
          ),
          bigquery.ScalarQueryParameter(
              "checkpoint_timestamp",
              "TIMESTAMP",
              checkpoint_timestamp,
          ),
          bigquery.ScalarQueryParameter(
              "updated_at",
              "TIMESTAMP",
              datetime.now(timezone.utc),
          ),
      ]
  )
  client.bq_client.query(query, job_config=job_config).result()


def write_run_history(
    client,
    config: RuntimeConfig,
    run_started_at: datetime,
    run_finished_at: datetime,
    scan_start: datetime,
    scan_end: datetime,
    stats: RunStats,
    status: str,
    error_message: str | None,
) -> None:
  """Write hidden run metadata for debugging and recovery analysis."""
  query = _RUNS_MERGE_QUERY.format(
      project=config.runs_project,
      dataset=config.runs_dataset,
      table=config.runs_table,
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "processor_name",
              "STRING",
              config.processor_name,
          ),
          bigquery.ScalarQueryParameter(
              "run_started_at",
              "TIMESTAMP",
              run_started_at,
          ),
          bigquery.ScalarQueryParameter(
              "run_finished_at",
              "TIMESTAMP",
              run_finished_at,
          ),
          bigquery.ScalarQueryParameter("scan_start", "TIMESTAMP", scan_start),
          bigquery.ScalarQueryParameter("scan_end", "TIMESTAMP", scan_end),
          bigquery.ScalarQueryParameter(
              "trigger_rows_found",
              "INT64",
              stats.trigger_rows_found,
          ),
          bigquery.ScalarQueryParameter(
              "processed_rows",
              "INT64",
              stats.processed_rows,
          ),
          bigquery.ScalarQueryParameter(
              "duplicate_rows",
              "INT64",
              stats.duplicate_rows,
          ),
          bigquery.ScalarQueryParameter(
              "ignored_rows", "INT64", stats.ignored_rows
          ),
          bigquery.ScalarQueryParameter("status", "STRING", status),
          bigquery.ScalarQueryParameter(
              "error_message", "STRING", error_message
          ),
      ]
  )
  client.bq_client.query(query, job_config=job_config).result()


class _IgnoredTrigger:
  """Marker object used to keep per-row scan counts accurate."""

  def __init__(self, row: dict[str, Any], reason: str):
    self.row = row
    self.reason = reason
    self.session_id = ""


def _row_to_dict(row: Any) -> dict[str, Any]:
  """Convert BigQuery rows and dict-like values to plain dicts."""
  if isinstance(row, dict):
    return row
  if hasattr(row, "items"):
    return dict(row.items())
  raise TypeError(f"Unsupported row type: {type(row)!r}")


def _env_int(name: str, default: int) -> int:
  """Read an integer env var with a warning fallback."""
  raw = os.environ.get(name)
  if raw is None:
    return default
  try:
    return int(raw)
  except ValueError:
    logger.warning(
        "Invalid integer value for %s=%r; using default %s",
        name,
        raw,
        default,
    )
    return default
