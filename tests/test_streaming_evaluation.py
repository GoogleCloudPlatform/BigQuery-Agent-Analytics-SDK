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

"""Tests for the scheduled streaming evaluation deployment path."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics._deploy_runtime import resolve_client_options
from bigquery_agent_analytics._streaming_evaluation import build_streaming_observability_evaluator
from bigquery_agent_analytics._streaming_evaluation import build_trigger_dedupe_key
from bigquery_agent_analytics._streaming_evaluation import classify_trigger_kind
from bigquery_agent_analytics._streaming_evaluation import compute_scan_start
from bigquery_agent_analytics._streaming_evaluation import parse_trigger_payload
from bigquery_agent_analytics._streaming_evaluation import parse_trigger_row
from bigquery_agent_analytics._streaming_evaluation import serialize_streaming_result_row
from bigquery_agent_analytics._streaming_evaluation import STREAMING_EVALUATOR_PROFILE
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import SessionScore

_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 3, 28, 18, 30, 0, tzinfo=timezone.utc)


def _import_module(name: str, relative_path: str):
  spec = importlib.util.spec_from_file_location(
      name,
      _ROOT / relative_path,
  )
  mod = importlib.util.module_from_spec(spec)
  sys.modules[name] = mod
  spec.loader.exec_module(mod)
  return mod


def _normalize_sql(sql_text: str) -> str:
  lines = [
      line.strip()
      for line in sql_text.splitlines()
      if line.strip() and not line.strip().startswith("--")
  ]
  return " ".join(lines).rstrip(";")


def _report() -> EvaluationReport:
  return EvaluationReport(
      dataset="test",
      evaluator_name=STREAMING_EVALUATOR_PROFILE,
      total_sessions=1,
      passed_sessions=1,
      failed_sessions=0,
      aggregate_scores={
          "latency": 0.9,
          "error_rate": 1.0,
          "turn_count": 0.8,
      },
      details={"source": "unit-test"},
      created_at=_NOW,
      session_scores=[
          SessionScore(
              session_id="sess-1",
              scores={
                  "latency": 0.9,
                  "error_rate": 1.0,
                  "turn_count": 0.8,
              },
              passed=True,
              details={"checked": True},
          )
      ],
  )


def _empty_report() -> EvaluationReport:
  return EvaluationReport(
      dataset="test",
      evaluator_name=STREAMING_EVALUATOR_PROFILE,
      total_sessions=0,
      passed_sessions=0,
      failed_sessions=0,
      aggregate_scores={},
      details={"source": "unit-test"},
      created_at=_NOW,
      session_scores=[],
  )


class _FakeSelectJob:

  def __init__(self, rows):
    self._rows = rows

  def result(self):
    return self._rows


class _FakeDmlJob:

  def __init__(self, affected_rows: int | None = 1):
    self.num_dml_affected_rows = affected_rows

  def result(self):
    return None


class _FakeBigQueryClient:

  def __init__(
      self,
      *,
      checkpoint_rows=None,
      trigger_rows=None,
      merge_affected_rows=None,
      error_on: str | None = None,
  ):
    self.checkpoint_rows = checkpoint_rows or []
    self.trigger_rows = trigger_rows or []
    self.merge_affected_rows = list(merge_affected_rows or [])
    self.error_on = error_on
    self.queries = []
    self.job_configs = []

  def query(self, query, job_config=None):
    self.queries.append(query)
    self.job_configs.append(job_config)
    if self.error_on and self.error_on in query:
      raise RuntimeError("transient failure")
    compact = query.lstrip()
    if compact.startswith("SELECT checkpoint_timestamp"):
      return _FakeSelectJob(self.checkpoint_rows)
    if compact.startswith("SELECT\n  session_id,"):
      return _FakeSelectJob(self.trigger_rows)
    if "streaming_evaluation_results" in query:
      affected_rows = (
          self.merge_affected_rows.pop(0) if self.merge_affected_rows else 1
      )
      return _FakeDmlJob(affected_rows)
    if compact.startswith("MERGE") or compact.startswith("INSERT INTO"):
      return _FakeDmlJob(1)
    raise AssertionError(f"Unexpected query: {query}")


@pytest.fixture(scope="module")
def rf_dispatch():
  return _import_module("rf_dispatch", "deploy/remote_function/dispatch.py")


@pytest.fixture(scope="module")
def streaming_worker():
  return _import_module(
      "streaming_worker",
      "deploy/streaming_evaluation/worker.py",
  )


class TestSharedRuntime:

  def test_resolve_client_options_from_env(self, monkeypatch):
    monkeypatch.setenv("BQ_AGENT_PROJECT", "env-project")
    monkeypatch.setenv("BQ_AGENT_DATASET", "env-dataset")
    monkeypatch.setenv("BQ_AGENT_TABLE", "env-table")
    monkeypatch.setenv("BQ_AGENT_LOCATION", "US")

    options = resolve_client_options({})
    assert options["project_id"] == "env-project"
    assert options["dataset_id"] == "env-dataset"
    assert options["table_id"] == "env-table"
    assert options["location"] == "US"
    assert options["verify_schema"] is False

  def test_resolve_client_options_leaves_location_unset_by_default(
      self,
      monkeypatch,
  ):
    monkeypatch.setenv("BQ_AGENT_PROJECT", "env-project")
    monkeypatch.setenv("BQ_AGENT_DATASET", "env-dataset")
    monkeypatch.delenv("BQ_AGENT_LOCATION", raising=False)

    options = resolve_client_options({})

    assert options["location"] is None

  def test_remote_and_streaming_import_same_resolver(
      self,
      rf_dispatch,
      streaming_worker,
  ):
    assert rf_dispatch.resolve_client_options is resolve_client_options
    assert streaming_worker.resolve_client_options is resolve_client_options


class TestHelpers:

  def test_streaming_profile_contains_fixed_metrics(self):
    evaluator = build_streaming_observability_evaluator()
    metric_names = [metric.name for metric in evaluator._metrics]

    assert evaluator.name == STREAMING_EVALUATOR_PROFILE
    assert metric_names == ["latency", "error_rate", "turn_count"]

  def test_dedupe_key_is_stable(self):
    first = build_trigger_dedupe_key(
        session_id="sess-1",
        trace_id="trace-1",
        span_id="span-1",
        event_type="AGENT_COMPLETED",
        trigger_timestamp=_NOW,
    )
    second = build_trigger_dedupe_key(
        session_id="sess-1",
        trace_id="trace-1",
        span_id="span-1",
        event_type="AGENT_COMPLETED",
        trigger_timestamp="2026-03-28T18:30:00Z",
    )

    assert first == second

  def test_classify_trigger_kind_prefers_terminal_then_error(self):
    assert classify_trigger_kind("AGENT_COMPLETED") == "session_terminal"
    assert classify_trigger_kind("TOOL_ERROR") == "error_event"
    assert (
        classify_trigger_kind(
            "AGENT_STEP",
            status="ERROR",
            error_message="boom",
        )
        == "error_event"
    )
    assert classify_trigger_kind("AGENT_STEP", status="ERROR") is None
    assert classify_trigger_kind("AGENT_STEP", error_message="boom") is None
    assert classify_trigger_kind("AGENT_STEP") is None

  def test_parse_trigger_row_classifies_from_overlap_scan_fields(self):
    trigger = parse_trigger_row(
        {
            "session_id": "sess-1",
            "trace_id": "trace-1",
            "span_id": "span-1",
            "event_type": "TOOL_ERROR",
            "status": "ERROR",
            "error_message": "boom",
            "trigger_timestamp": "2026-03-28T18:30:00Z",
        }
    )

    assert trigger.trigger_kind == "error_event"
    assert trigger.is_final is False

  def test_compute_scan_start_uses_checkpoint_overlap(self):
    scan_start = compute_scan_start(
        run_started_at=_NOW,
        checkpoint_timestamp=_NOW,
        overlap=timedelta(minutes=15),
        initial_lookback=timedelta(minutes=30),
    )
    assert scan_start == _NOW - timedelta(minutes=15)

  def test_compute_scan_start_bootstraps_with_initial_lookback(self):
    scan_start = compute_scan_start(
        run_started_at=_NOW,
        checkpoint_timestamp=None,
        overlap=timedelta(minutes=15),
        initial_lookback=timedelta(minutes=30),
    )
    assert scan_start == _NOW - timedelta(minutes=30)

  def test_serialize_result_row_uses_session_scores(self):
    trigger = parse_trigger_payload(
        {
            "session_id": "sess-1",
            "trace_id": "trace-1",
            "span_id": "span-1",
            "event_type": "AGENT_COMPLETED",
            "trigger_kind": "session_terminal",
            "trigger_timestamp": "2026-03-28T18:30:00Z",
        }
    )

    row = serialize_streaming_result_row(trigger, _report(), processed_at=_NOW)

    assert row["session_id"] == "sess-1"
    assert row["trigger_kind"] == "session_terminal"
    assert row["is_final"] is True
    assert row["evaluator_profile"] == STREAMING_EVALUATOR_PROFILE
    assert row["passed"] is True
    assert json.loads(row["aggregate_scores_json"]) == {
        "error_rate": 1.0,
        "latency": 0.9,
        "turn_count": 0.8,
    }
    assert json.loads(row["details_json"]) == {
        "report": {"source": "unit-test"},
        "session": {"checked": True},
    }


class TestWorker:

  def test_valid_run_processes_and_persists(
      self,
      streaming_worker,
      monkeypatch,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
                "trigger_kind": "session_terminal",
            }
        ]
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.return_value = _report()
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 200
    assert response["status"] == "processed"
    assert response["processed_rows"] == 1
    assert response["duplicate_rows"] == 0
    assert response["ignored_rows"] == 0
    client.evaluate.assert_called_once()

    trigger_scan_config = fake_bq.job_configs[1]
    scan_params = {
        param.name: param.value
        for param in trigger_scan_config.query_parameters
    }
    assert scan_params["scan_start"] == _NOW - timedelta(minutes=30)
    assert scan_params["scan_end"] == _NOW

    result_params = None
    for query, job_config in zip(fake_bq.queries, fake_bq.job_configs):
      if "streaming_evaluation_results" in query:
        result_params = {
            param.name: param.value for param in job_config.query_parameters
        }
        break

    assert result_params is not None
    assert result_params["session_id"] == "sess-1"
    assert result_params["trigger_kind"] == "session_terminal"
    assert result_params["is_final"] is True
    assert result_params["evaluator_profile"] == STREAMING_EVALUATOR_PROFILE

  def test_duplicate_trigger_returns_200(self, streaming_worker, monkeypatch):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "TOOL_ERROR",
                "status": "ERROR",
                "error_message": "boom",
                "trigger_timestamp": _NOW,
                "trigger_kind": "error_event",
            }
        ],
        merge_affected_rows=[0],
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.return_value = _report()
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 200
    assert response["status"] == "processed"
    assert response["processed_rows"] == 0
    assert response["duplicate_rows"] == 1

  def test_missing_session_id_is_ignored(
      self,
      streaming_worker,
      monkeypatch,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
                "trigger_kind": "session_terminal",
            }
        ]
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 200
    assert response["ignored_rows"] == 1
    client.evaluate.assert_not_called()

  def test_malformed_row_is_counted_once(
      self,
      streaming_worker,
      monkeypatch,
      caplog,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
            }
        ]
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 200
    assert response["ignored_rows"] == 1
    assert caplog.messages == [
        "Ignoring malformed trigger row: Missing required trigger fields: event_type, trigger_kind"
    ]
    client.evaluate.assert_not_called()

  def test_bigquery_or_evaluation_failure_retries(
      self,
      streaming_worker,
      monkeypatch,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
                "trigger_kind": "session_terminal",
            }
        ]
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.side_effect = RuntimeError("transient failure")
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 500
    assert response["status"] == "retry"
    assert response["reason"] == "internal error"

  def test_zero_session_report_is_skipped_not_retried(
      self,
      streaming_worker,
      monkeypatch,
      caplog,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
                "trigger_kind": "session_terminal",
            }
        ]
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.return_value = _empty_report()
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 200
    assert response["processed_rows"] == 0
    assert response["ignored_rows"] == 1
    assert caplog.messages[-1] == (
        "No events found for session_id=sess-1; skipping trigger"
    )

  def test_checkpoint_does_not_advance_if_success_history_write_fails(
      self,
      streaming_worker,
      monkeypatch,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
                "trigger_kind": "session_terminal",
            }
        ],
        error_on="_streaming_eval_runs",
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.return_value = _report()
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 500
    assert response["status"] == "retry"
    state_queries = [
        query for query in fake_bq.queries if "_streaming_eval_state" in query
    ]
    assert len(state_queries) == 1

  def test_checkpoint_failure_rewrites_success_run_history_to_failed(
      self,
      streaming_worker,
      monkeypatch,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
                "trigger_kind": "session_terminal",
            }
        ],
        error_on="checkpoint_timestamp = S.checkpoint_timestamp",
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.return_value = _report()
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 500
    assert response["reason"] == "internal error"
    runs_writes = []
    for query, job_config in zip(fake_bq.queries, fake_bq.job_configs):
      if "_streaming_eval_runs" in query:
        runs_writes.append(
            {param.name: param.value for param in job_config.query_parameters}
        )

    assert [write["status"] for write in runs_writes] == ["success", "failed"]
    assert runs_writes[-1]["error_message"] == "transient failure"

  def test_missing_dml_stats_are_treated_as_processed(
      self,
      streaming_worker,
      monkeypatch,
      caplog,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW,
                "trigger_kind": "session_terminal",
            }
        ],
        merge_affected_rows=[None],
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.return_value = _report()
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 200
    assert response["processed_rows"] == 1
    assert response["duplicate_rows"] == 0
    assert any(
        "BigQuery DML stats were unavailable" in message
        for message in caplog.messages
    )

  def test_same_session_can_emit_error_and_terminal_rows(
      self,
      streaming_worker,
      monkeypatch,
  ):
    fake_bq = _FakeBigQueryClient(
        trigger_rows=[
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-error",
                "event_type": "TOOL_ERROR",
                "status": "ERROR",
                "error_message": "boom",
                "trigger_timestamp": _NOW,
                "trigger_kind": "error_event",
            },
            {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "span_id": "span-final",
                "event_type": "AGENT_COMPLETED",
                "status": "OK",
                "error_message": None,
                "trigger_timestamp": _NOW + timedelta(minutes=1),
                "trigger_kind": "session_terminal",
            },
        ]
    )
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    client.bq_client = fake_bq
    client.evaluate.return_value = _report()
    monkeypatch.setattr(
        streaming_worker,
        "build_client_from_context",
        MagicMock(return_value=client),
    )

    response, status = streaming_worker.handle_scheduled_run({}, now=_NOW)

    assert status == 200
    assert response["processed_rows"] == 2
    persisted = []
    for query, job_config in zip(fake_bq.queries, fake_bq.job_configs):
      if "streaming_evaluation_results" in query:
        persisted.append(
            {param.name: param.value for param in job_config.query_parameters}
        )
    assert [row["trigger_kind"] for row in persisted] == [
        "error_event",
        "session_terminal",
    ]

  def test_invalid_overlap_env_uses_default(
      self,
      streaming_worker,
      monkeypatch,
      caplog,
  ):
    client = MagicMock()
    client.project_id = "proj"
    client.dataset_id = "agent_trace"
    client.table_id = "agent_events"
    monkeypatch.setenv("BQ_AGENT_OVERLAP_MINUTES", "fifteen")

    config = streaming_worker.load_runtime_config(client)

    assert config.overlap == timedelta(minutes=15)
    assert caplog.messages == [
        "Invalid integer value for BQ_AGENT_OVERLAP_MINUTES='fifteen'; using default 15"
    ]


class TestDeployAssets:

  def test_trigger_query_contains_exact_launch_filters(self):
    sql = (_ROOT / "deploy/streaming_evaluation/trigger_query.sql").read_text()

    assert "event_type = 'AGENT_COMPLETED'" in sql
    assert "event_type = 'TOOL_ERROR'" in sql
    assert "status = 'ERROR'" in sql
    assert "error_message IS NOT NULL" in sql
    assert "OR status = 'ERROR'" not in sql

  def test_trigger_query_matches_worker_scan_query(self, streaming_worker):
    sql = (_ROOT / "deploy/streaming_evaluation/trigger_query.sql").read_text()
    normalized_file = (
        _normalize_sql(sql)
        .replace(
            "`PROJECT.DATASET.SOURCE_TABLE`",
            "`{project}.{dataset}.{table}`",
        )
        .replace(
            "SCAN_START",
            "@scan_start",
        )
        .replace(
            "SCAN_END",
            "@scan_end",
        )
    )

    assert normalized_file == _normalize_sql(
        streaming_worker._TRIGGER_SCAN_QUERY
    )

  def test_trigger_query_contains_expected_scan_fields(self):
    sql = (_ROOT / "deploy/streaming_evaluation/trigger_query.sql").read_text()

    for field in (
        "session_id",
        "trace_id",
        "span_id",
        "event_type",
        "trigger_timestamp",
        "trigger_kind",
    ):
      assert field in sql

  def test_setup_assets_point_to_scheduler_and_state_tables(self):
    setup_sh = (_ROOT / "deploy/streaming_evaluation/setup.sh").read_text()
    readme = (_ROOT / "deploy/streaming_evaluation/README.md").read_text()
    requirements = (
        _ROOT / "deploy/streaming_evaluation/requirements.txt"
    ).read_text()

    assert ".streaming_evaluation_state.json" in setup_sh
    assert "streaming_evaluation_results" in setup_sh
    assert "_streaming_eval_state" in setup_sh
    assert "_streaming_eval_runs" in setup_sh
    assert 'PROJECT and DATASET are required for "up"' in setup_sh
    assert 'cp "$SCRIPT_DIR/requirements.txt" "$staging/"' in setup_sh
    assert "wait_for_service_account" in setup_sh
    assert "scheduler jobs create http" in setup_sh
    assert "cloudscheduler.googleapis.com" in setup_sh
    assert "roles/iam.serviceAccountOpenIdTokenCreator" in setup_sh
    assert "--timeout 300" in setup_sh
    assert "trap 'rm -rf \"$staging\"' RETURN" in setup_sh
    assert "Cloud Scheduler" in readme
    assert "No special BigQuery reservation is required" in readme
    assert "enable the required Cloud Run" in readme
    assert "BigQuery tables were preserved intentionally" in setup_sh
    assert "intentionally preserves the BigQuery tables" in readme
    assert "pyyaml>=6.0" in requirements
    assert "google-adk" not in requirements
    assert setup_sh.index(
        'ensure_scheduler_job "$scheduler_sa_email" "$url"'
    ) < (setup_sh.index('write_state "$bq_location" "$scheduler_sa_email"'))

  def test_streaming_worker_import_does_not_require_google_adk(self):
    script = f"""
import builtins
import importlib.util
import pathlib
import sys

root = pathlib.Path({str(_ROOT)!r})
orig_import = builtins.__import__

def blocked(name, globals=None, locals=None, fromlist=(), level=0):
  if name == "google.adk" or name.startswith("google.adk."):
    raise ImportError("blocked google.adk")
  return orig_import(name, globals, locals, fromlist, level)

builtins.__import__ = blocked
sys.path.insert(0, str(root / "src"))
spec = importlib.util.spec_from_file_location(
    "streaming_worker_no_adk",
    root / "deploy/streaming_evaluation/worker.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules["streaming_worker_no_adk"] = module
spec.loader.exec_module(module)
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        cwd=_ROOT,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
