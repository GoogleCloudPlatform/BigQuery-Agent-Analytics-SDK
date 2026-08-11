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

"""Tests for the BigQuery-to-LangSmith export connector."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
import uuid

import pytest

from bigquery_agent_analytics.export.langsmith import _build_source_query
from bigquery_agent_analytics.export.langsmith import _canonical_source
from bigquery_agent_analytics.export.langsmith import _create_langsmith_client
from bigquery_agent_analytics.export.langsmith import _DroppedRowCollector
from bigquery_agent_analytics.export.langsmith import _is_retryable
from bigquery_agent_analytics.export.langsmith import _prepare_trace_runs
from bigquery_agent_analytics.export.langsmith import _Watermark
from bigquery_agent_analytics.export.langsmith import export
from bigquery_agent_analytics.export.langsmith import ExportConfig
from bigquery_agent_analytics.export.langsmith import FieldMapping

_T0 = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 8, 10, 12, 0, 1, tzinfo=timezone.utc)


class _QueryJob:

  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self._rows = rows
    self.page_sizes: list[int | None] = []

  def result(self, *, page_size: int | None = None):
    self.page_sizes.append(page_size)
    return list(self._rows)


class _BigQueryClient:

  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self.job = _QueryJob(rows)
    self.queries: list[tuple[str, Any]] = []

  def query(self, sql: str, *, job_config: Any):
    self.queries.append((sql, job_config))
    return self.job


class _IncrementalBigQueryClient(_BigQueryClient):

  def __init__(self, rows: list[dict[str, Any]]) -> None:
    super().__init__(rows)
    self._source_rows = rows

  def query(self, sql: str, *, job_config: Any):
    parameters = {
        parameter.name: parameter.value
        for parameter in job_config.query_parameters
    }
    watermark_time = parameters.get("bqaa_watermark_timestamp")
    if watermark_time is None:
      selected = self._source_rows
    else:
      watermark_trace = parameters["bqaa_watermark_trace_id"]
      watermark_run = parameters["bqaa_watermark_run_id"]

      def follows_watermark(row: dict[str, Any]) -> bool:
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime):
          return False
        if timestamp != watermark_time:
          return timestamp > watermark_time
        trace_id = row.get("trace_id")
        if not isinstance(trace_id, str):
          return False
        if trace_id != watermark_trace:
          return trace_id > watermark_trace
        run_id = row.get("span_id")
        return isinstance(run_id, str) and run_id > watermark_run

      selected = [row for row in self._source_rows if follows_watermark(row)]
    self.job = _QueryJob(selected)
    self.queries.append((sql, job_config))
    return self.job


class _LangSmithClient:

  def __init__(self, failures: int = 0) -> None:
    self.failures = failures
    self.calls: list[dict[str, Any]] = []

  def batch_ingest_runs(self, *, create=None, update=None) -> None:
    if self.failures:
      self.failures -= 1
      raise _RetryableError(429)
    self.calls.append({"create": create, "update": update})


class _FailOnCallLangSmithClient(_LangSmithClient):

  def __init__(self, call_number: int) -> None:
    super().__init__()
    self.call_number = call_number
    self.attempts = 0

  def batch_ingest_runs(self, *, create=None, update=None) -> None:
    self.attempts += 1
    if self.attempts == self.call_number:
      raise _RetryableError(429)
    super().batch_ingest_runs(create=create, update=update)


class _StatefulLangSmithClient(_LangSmithClient):

  def __init__(self) -> None:
    super().__init__()
    self.runs: dict[str, dict[str, Any]] = {}

  def batch_ingest_runs(self, *, create=None, update=None) -> None:
    super().batch_ingest_runs(create=create, update=update)
    for run in create or []:
      self.runs.setdefault(str(run["id"]), dict(run))
    for run in update or []:
      run_id = str(run["id"])
      if run_id not in self.runs:
        raise AssertionError("cannot update a run before it is created")
      self.runs[run_id].update(run)


class _RetryableError(RuntimeError):

  def __init__(self, status_code: int) -> None:
    super().__init__(f"HTTP {status_code}")
    self.status_code = status_code


def _standard_rows() -> list[dict[str, Any]]:
  return [
      {
          "timestamp": _T0,
          "event_type": "AGENT_START",
          "trace_id": "trace-1",
          "span_id": "span-root",
          "parent_span_id": None,
          "content": {"prompt": "opaque input"},
          "attributes": {"model": "not interpreted"},
          "latency_ms": {"total_ms": 1000},
          "status": "OK",
          "error_message": None,
          "custom_record": {"tenant_shape": [1, 2, 3]},
      },
      {
          "timestamp": _T1,
          "event_type": "TOOL_ERROR",
          "trace_id": "trace-1",
          "span_id": "span-child",
          "parent_span_id": "span-root",
          "content": ["opaque", {"payload": True}],
          "attributes": {},
          "latency_ms": {"total_ms": 25},
          "status": "ERROR",
          "error_message": "tool failed",
          "custom_record": {"tenant_shape": [4]},
      },
  ]


def test_default_mapping_builds_one_root_and_preserves_hierarchy() -> None:
  prepared = _prepare_trace_runs(
      _standard_rows(),
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="source-a",
      project_name="langsmith-project",
  )

  assert len(prepared.runs) == 3
  synthetic_root, parent, child = prepared.runs
  assert synthetic_root["id"] == synthetic_root["trace_id"]
  assert synthetic_root["parent_run_id"] is None
  assert parent["parent_run_id"] == synthetic_root["id"]
  assert child["parent_run_id"] == parent["id"]
  assert parent["dotted_order"].startswith(synthetic_root["dotted_order"] + ".")
  assert child["dotted_order"].startswith(parent["dotted_order"] + ".")
  assert parent["inputs"] == {"prompt": "opaque input"}
  assert child["inputs"] == {"value": ["opaque", {"payload": True}]}
  assert child["error"] == "tool failed"
  assert parent["end_time"] == _T1
  assert parent["extra"]["metadata"]["custom_record"] == {
      "tenant_shape": [1, 2, 3]
  }
  assert parent["extra"]["metadata"]["attributes"] == {
      "model": "not interpreted"
  }
  assert "content" not in parent["extra"]["metadata"]
  for run in prepared.runs:
    uuid.UUID(str(run["id"]))
    uuid.UUID(str(run["trace_id"]))
    assert run["session_name"] == "langsmith-project"
    assert run["dotted_order"].endswith(str(run["id"]))


def test_custom_mapping_treats_payload_as_opaque_and_keeps_unknown_columns() -> (
    None
):
  mapping = FieldMapping.from_dict(
      {
          "run_id": "event_key",
          "trace_id": "group.key",
          "parent_run_id": "parent_key",
          "name": "kind",
          "start_time": "occurred_at",
          "inputs": "payload",
          "error": None,
          "latency_ms": None,
      }
  )
  payload = "{not parsed as JSON}"
  row = {
      "event_key": "custom-1",
      "group": {"key": "custom-trace", "other": "covered column"},
      "parent_key": None,
      "kind": "CUSTOM",
      "occurred_at": _T0,
      "payload": payload,
      "unknown_scalar": "survives",
      "unknown_nested": {"arbitrary": [True, None]},
  }

  prepared = _prepare_trace_runs(
      [row], mapping=mapping, source_fingerprint="custom", project_name="p"
  )
  run = prepared.runs[1]

  assert run["inputs"] == {"value": payload}
  assert run["extra"]["metadata"]["unknown_scalar"] == "survives"
  assert run["extra"]["metadata"]["unknown_nested"] == {
      "arbitrary": [True, None]
  }
  # A nested path covers only that value, not the rest of its top-level
  # record. Keeping the record in metadata avoids dropping ``group.other``.
  assert run["extra"]["metadata"]["group"] == {
      "key": "custom-trace",
      "other": "covered column",
  }


def test_custom_mapping_does_not_inherit_undeclared_adk_fields() -> None:
  mapping = FieldMapping.from_dict(
      {"run_id": "event_key", "trace_id": "trace_key"}
  )
  row = {
      "event_key": "custom-1",
      "trace_key": "trace-1",
      "status": "ERROR",
      "error_message": "domain error, not a trace failure",
      "latency_ms": 42,
  }

  run = _prepare_trace_runs(
      [row], mapping=mapping, source_fingerprint="custom", project_name="p"
  ).runs[1]

  assert mapping.status is None
  assert mapping.error is None
  assert mapping.latency_ms is None
  assert "error" not in run
  assert "end_time" not in run
  assert run["extra"]["metadata"]["status"] == "ERROR"
  assert run["extra"]["metadata"]["error_message"] == (
      "domain error, not a trace failure"
  )
  assert run["extra"]["metadata"]["latency_ms"] == 42


def test_default_mapping_decodes_adk_json_string_content_and_latency() -> None:
  row = {
      **_standard_rows()[0],
      "content": json.dumps({"prompt": "decoded ADK payload"}),
      "latency_ms": json.dumps({"total_ms": 1250}),
  }

  run = _prepare_trace_runs(
      [row],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="source",
      project_name="p",
  ).runs[1]

  assert run["inputs"] == {"prompt": "decoded ADK payload"}
  assert run["end_time"] == _T0.replace(microsecond=0) + timedelta(
      milliseconds=1250
  )


@pytest.mark.parametrize(
    ("latency", "expected_end_time"),
    [
        (Decimal("12.5"), _T0 + timedelta(milliseconds=12.5)),
        (True, None),
    ],
)
def test_latency_accepts_decimal_but_not_bool(
    latency: object, expected_end_time: datetime | None
) -> None:
  mapping = FieldMapping.from_dict(
      {
          "run_id": "event_key",
          "trace_id": "trace_key",
          "start_time": "occurred_at",
          "latency_ms": "duration_ms",
      }
  )
  run = _prepare_trace_runs(
      [
          {
              "event_key": "custom-1",
              "trace_key": "trace-1",
              "occurred_at": _T0,
              "duration_ms": latency,
          }
      ],
      mapping=mapping,
      source_fingerprint="custom",
      project_name="p",
  ).runs[1]

  assert run.get("end_time") == expected_end_time


def test_non_json_bigquery_scalars_use_loss_minimizing_metadata_encoding() -> (
    None
):
  row = {
      "event_key": "custom-1",
      "trace_key": "trace-1",
      "payload": b"\x00opaque",
      "amount": Decimal("1.20"),
      "day": date(2026, 8, 10),
  }
  mapping = FieldMapping.from_dict(
      {
          "run_id": "event_key",
          "trace_id": "trace_key",
          "start_time": None,
          "inputs": "payload",
          "latency_ms": None,
          "error": None,
      }
  )

  run = _prepare_trace_runs(
      [row], mapping=mapping, source_fingerprint="custom", project_name="p"
  ).runs[1]

  assert run["inputs"] == {
      "value": {"encoding": "base64", "data": "AG9wYXF1ZQ=="}
  }
  assert run["extra"]["metadata"]["amount"] == "1.20"
  assert run["extra"]["metadata"]["day"] == "2026-08-10"
  json.dumps(run["inputs"])
  json.dumps(run["extra"])


def test_decimal_identity_scalars_are_accepted() -> None:
  mapping = FieldMapping.from_dict(
      {
          "run_id": "event_key",
          "trace_id": "trace_key",
          "parent_run_id": "parent_key",
          "start_time": None,
      }
  )
  prepared = _prepare_trace_runs(
      [
          {
              "event_key": Decimal("1.20"),
              "trace_key": Decimal("7"),
              "parent_key": Decimal("0"),
          }
      ],
      mapping=mapping,
      source_fingerprint="decimal-source",
      project_name="p",
  )

  run = prepared.runs[1]
  assert run["extra"]["bqaa"]["source_run_id"] == "1.20"
  assert run["extra"]["bqaa"]["source_trace_id"] == "7"
  assert run["extra"]["bqaa"]["source_parent_run_id"] == "0"


@pytest.mark.parametrize("error_message", [None, ""])
def test_standard_status_only_error_sets_marker_and_keeps_status_metadata(
    error_message: str | None,
) -> None:
  row = {
      **_standard_rows()[0],
      "status": "ERROR",
      "error_message": error_message,
  }

  run = _prepare_trace_runs(
      [row],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="source",
      project_name="p",
  ).runs[1]

  assert run["error"] == "[SOURCE_STATUS_ERROR]"
  assert run["extra"]["metadata"]["status"] == "ERROR"


def test_run_ids_are_stable_across_replays() -> None:
  first = _prepare_trace_runs(
      _standard_rows(),
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="same-source",
      project_name="p",
  )
  second = _prepare_trace_runs(
      _standard_rows(),
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="same-source",
      project_name="p",
  )

  assert [run["id"] for run in first.runs] == [run["id"] for run in second.runs]
  assert [run["dotted_order"] for run in first.runs] == [
      run["dotted_order"] for run in second.runs
  ]


def test_table_and_sql_sources_are_wrapped_without_rewriting_payloads() -> None:
  table_sql, _ = _build_source_query(
      "project.dataset.agent_events",
      mapping=FieldMapping.standard_adk(),
      since=None,
      until=None,
      where=None,
      watermark=None,
  )
  custom_sql, _ = _build_source_query(
      "SELECT odd, payload FROM custom_source WHERE opaque = true;",
      mapping=FieldMapping.from_dict(
          {
              "run_id": "odd",
              "trace_id": "odd",
              "start_time": None,
              "inputs": "payload",
          }
      ),
      since=None,
      until=None,
      where=None,
      watermark=None,
  )

  assert "FROM `project.dataset.agent_events`" in table_sql
  assert (
      "SELECT odd, payload FROM custom_source WHERE opaque = true" in custom_sql
  )
  assert "ORDER BY" in table_sql
  assert "ORDER BY" in custom_sql
  assert "CAST(bqaa_source.`odd` AS STRING)" in custom_sql


def test_time_filters_cast_string_mapped_timestamps() -> None:
  sql, _ = _build_source_query(
      "project.dataset.events",
      mapping=FieldMapping.from_dict(
          {
              "run_id": "event_key",
              "trace_id": "trace_key",
              "start_time": "occurred_at",
          }
      ),
      since=_T0,
      until=_T1,
      where=None,
      watermark=None,
  )

  timestamp = "SAFE_CAST(bqaa_source.`occurred_at` AS TIMESTAMP)"
  assert f"{timestamp} >= @bqaa_since" in sql
  assert f"{timestamp} < @bqaa_until" in sql
  assert (
      "ORDER BY COALESCE(CAST(bqaa_source.`trace_key` AS STRING), ''), "
      "COALESCE(SAFE_CAST(bqaa_source.`occurred_at` AS TIMESTAMP), "
      "TIMESTAMP('1970-01-01T00:00:00+00:00'))" in sql
  )


def test_watermark_query_coalesces_invalid_cursor_components() -> None:
  mapping = FieldMapping.from_dict(
      {
          "run_id": "event_key",
          "trace_id": "trace_key",
          "start_time": "occurred_at",
      }
  )
  sql, _ = _build_source_query(
      "project.dataset.events",
      mapping=mapping,
      since=None,
      until=None,
      where=None,
      watermark=_Watermark(datetime(1970, 1, 1, tzinfo=timezone.utc), "", ""),
  )

  assert (
      "COALESCE(SAFE_CAST(bqaa_source.`occurred_at` AS TIMESTAMP), "
      "TIMESTAMP('1970-01-01T00:00:00+00:00')) > "
      "@bqaa_watermark_timestamp" in sql
  )
  assert (
      "COALESCE(CAST(bqaa_source.`trace_key` AS STRING), '') > "
      "@bqaa_watermark_trace_id" in sql
  )
  assert (
      "COALESCE(CAST(bqaa_source.`event_key` AS STRING), '') > "
      "@bqaa_watermark_run_id" in sql
  )


def test_table_source_identity_preserves_case() -> None:
  upper = _canonical_source("Project.DataSet.Events", None)
  lower = _canonical_source("project.dataset.events", None)

  assert upper == "table:Project.DataSet.Events"
  assert lower == "table:project.dataset.events"
  assert upper != lower


@pytest.mark.parametrize(
    "table",
    [
        "project.9dataset.7table",
        "project.dataset.table-with-dashes",
    ],
)
def test_valid_digit_leading_and_hyphenated_table_ids_are_recognized(
    table: str,
) -> None:
  sql, _ = _build_source_query(
      table,
      mapping=FieldMapping.standard_adk(),
      since=None,
      until=None,
      where=None,
      watermark=None,
  )

  assert f"FROM `{table}` AS bqaa_source" in sql


@pytest.mark.parametrize(
    "source",
    [
        "project.dataset.`table`",
        "project.dataset.table; DELETE FROM x",
        "project.data-set.table",
    ],
)
def test_unsafe_or_invalid_table_ids_are_not_interpolated_as_identifiers(
    source: str,
) -> None:
  sql, _ = _build_source_query(
      source,
      mapping=FieldMapping.standard_adk(),
      since=None,
      until=None,
      where=None,
      watermark=None,
  )

  assert f"FROM `{source}`" not in sql
  assert "SELECT * FROM (" in sql


def test_export_batches_create_then_update_and_reports_rows() -> None:
  bq = _BigQueryClient(_standard_rows())
  langsmith = _LangSmithClient()
  stats = export(
      "project.dataset.agent_events",
      config=ExportConfig(
          langsmith_project="destination",
          batch_size=100,
          requests_per_second=None,
      ),
      bq_client=bq,
      langsmith_client=langsmith,
  )

  assert stats.rows_read == 2
  assert stats.exported == 2
  assert stats.failed == 0
  assert stats.traces_exported == 1
  assert len(langsmith.calls) == 2
  assert langsmith.calls[0]["create"] is not None
  assert langsmith.calls[0]["update"] is None
  assert langsmith.calls[1]["create"] is None
  assert langsmith.calls[1]["update"] is not None
  query_config = bq.queries[0][1]
  assert query_config.labels["sdk_feature"] == "langsmith-export"


def test_replaying_window_updates_changed_runs_without_duplicates() -> None:
  langsmith = _StatefulLangSmithClient()
  config = ExportConfig(
      langsmith_project="destination", requests_per_second=None
  )

  first_rows = _standard_rows()
  export(
      "project.dataset.agent_events",
      config=config,
      bq_client=_BigQueryClient(first_rows),
      langsmith_client=langsmith,
  )
  replay_rows = _standard_rows()
  replay_rows[1]["error_message"] = "backfilled failure detail"
  replay_rows[1]["content"] = {"backfilled": True}
  export(
      "project.dataset.agent_events",
      config=config,
      bq_client=_BigQueryClient(replay_rows),
      langsmith_client=langsmith,
  )

  assert len(langsmith.calls) == 4
  assert len(langsmith.runs) == 3
  child = next(
      run
      for run in langsmith.runs.values()
      if run.get("extra", {}).get("bqaa", {}).get("source_run_id")
      == "span-child"
  )
  assert child["error"] == "backfilled failure detail"
  assert child["inputs"] == {"backfilled": True}


def test_surfaced_create_conflict_is_not_retried_and_update_still_runs() -> (
    None
):
  class _ConflictOnCreateClient(_LangSmithClient):

    def __init__(self) -> None:
      super().__init__()
      self.create_attempts = 0

    def batch_ingest_runs(self, *, create=None, update=None) -> None:
      if create is not None:
        self.create_attempts += 1
        raise _RetryableError(409)
      super().batch_ingest_runs(create=create, update=update)

  langsmith = _ConflictOnCreateClient()
  stats = export(
      "project.dataset.agent_events",
      config=ExportConfig(requests_per_second=None),
      bq_client=_BigQueryClient(_standard_rows()),
      langsmith_client=langsmith,
  )

  assert langsmith.create_attempts == 1
  assert len(langsmith.calls) == 1
  assert langsmith.calls[0]["create"] is None
  assert langsmith.calls[0]["update"] is not None
  assert stats.exported == 2
  assert stats.failed == 0


def test_same_span_id_in_different_traces_gets_distinct_run_ids() -> None:
  first = _standard_rows()[0]
  second = {**first, "trace_id": "trace-2"}
  run_1 = _prepare_trace_runs(
      [first],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="same-source",
      project_name="p",
  ).runs[1]
  run_2 = _prepare_trace_runs(
      [second],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="same-source",
      project_name="p",
  ).runs[1]

  assert run_1["id"] != run_2["id"]


def test_orphan_child_is_flattened_without_contradictory_dotted_order() -> None:
  _, child = _standard_rows()
  synthetic_root, child_run = _prepare_trace_runs(
      [child],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="same-source",
      project_name="p",
  ).runs

  assert child_run["parent_run_id"] == synthetic_root["id"]
  assert child_run["dotted_order"].startswith(
      synthetic_root["dotted_order"] + "."
  )
  assert child_run["extra"]["bqaa"]["source_parent_run_id"] == "span-root"


def test_synthetic_root_identity_and_order_are_stable_across_windows() -> None:
  parent, child = _standard_rows()
  first = _prepare_trace_runs(
      [parent],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="same-source",
      project_name="p",
  ).runs[0]
  second = _prepare_trace_runs(
      [child],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="same-source",
      project_name="p",
  ).runs[0]

  assert first["id"] == second["id"]
  assert first["dotted_order"] == second["dotted_order"]
  assert first["start_time"] == parent["timestamp"]
  assert first["end_time"] == parent["timestamp"] + timedelta(milliseconds=1000)
  assert second["start_time"] == child["timestamp"]
  assert second["end_time"] == child["timestamp"] + timedelta(milliseconds=25)
  assert first["start_time"].year != 1970
  assert second["start_time"].year != 1970


def test_incremental_export_persists_and_reuses_watermark(
    tmp_path: Path,
) -> None:
  watermark_path = tmp_path / "watermark.json"
  config = ExportConfig(
      langsmith_project="destination",
      incremental=True,
      watermark_path=watermark_path,
      requests_per_second=None,
  )
  first_bq = _BigQueryClient(_standard_rows())
  first = export(
      "project.dataset.agent_events",
      config=config,
      bq_client=first_bq,
      langsmith_client=_LangSmithClient(),
  )

  stored = json.loads(watermark_path.read_text(encoding="utf-8"))
  assert first.watermark == _T1
  assert stored["timestamp"] == _T1.isoformat()
  assert stored["trace_id"] == "trace-1"
  assert stored["run_id"] == "span-child"

  second_bq = _BigQueryClient([])
  second = export(
      "project.dataset.agent_events",
      config=config,
      bq_client=second_bq,
      langsmith_client=_LangSmithClient(),
  )
  sql, job_config = second_bq.queries[0]
  parameters = {p.name: p.value for p in job_config.query_parameters}
  assert "@bqaa_watermark_timestamp" in sql
  assert "@bqaa_watermark_trace_id" in sql
  assert "@bqaa_watermark_run_id" in sql
  assert parameters["bqaa_watermark_timestamp"] == _T1
  assert parameters["bqaa_watermark_trace_id"] == "trace-1"
  assert parameters["bqaa_watermark_run_id"] == "span-child"
  assert second.exported == 0


def test_skipped_row_is_checkpointed_and_does_not_stall_incremental_sync(
    tmp_path: Path,
) -> None:
  valid = _standard_rows()[0]
  invalid = {**valid, "timestamp": _T1, "span_id": None}
  invalid_time = {
      **valid,
      "timestamp": "not-a-timestamp",
      "trace_id": "trace-2",
      "span_id": "bad-time",
  }
  watermark_path = tmp_path / "watermark.json"
  config = ExportConfig(
      incremental=True,
      watermark_path=watermark_path,
      requests_per_second=None,
  )
  bq = _IncrementalBigQueryClient([valid, invalid, invalid_time])

  first = export(
      "project.dataset.agent_events",
      config=config,
      bq_client=bq,
      langsmith_client=_LangSmithClient(),
  )

  assert first.exported == 2
  assert first.skipped == 1
  assert first.traces_exported == 2
  stored = json.loads(watermark_path.read_text(encoding="utf-8"))
  assert stored["timestamp"] == _T1.isoformat()
  assert stored["trace_id"] == "trace-1"
  assert stored["run_id"] == ""

  second = export(
      "project.dataset.agent_events",
      config=config,
      bq_client=bq,
      langsmith_client=_LangSmithClient(),
  )

  assert second.rows_read == 0
  assert second.watermark == _T1
  sql, job_config = bq.queries[1]
  assert "@bqaa_watermark_timestamp" in sql
  parameters = {item.name: item.value for item in job_config.query_parameters}
  assert parameters["bqaa_watermark_timestamp"] == _T1


def test_incremental_mapping_requires_start_time_before_query(
    tmp_path: Path,
) -> None:
  bq = _BigQueryClient([])
  langsmith = _LangSmithClient()
  mapping = FieldMapping.from_dict(
      {"run_id": "event_key", "trace_id": "trace_key"}
  )

  with pytest.raises(
      ValueError, match="incremental export requires a start_time mapping"
  ):
    export(
        "project.dataset.events",
        config=ExportConfig(
            mapping=mapping,
            incremental=True,
            watermark_path=tmp_path / "watermark.json",
            requests_per_second=None,
        ),
        bq_client=bq,
        langsmith_client=langsmith,
    )

  assert bq.queries == []
  assert langsmith.calls == []


def test_watermark_total_order_includes_trace_id(tmp_path: Path) -> None:
  template = _standard_rows()[0]
  rows = [
      {**template, "trace_id": "trace-a", "span_id": "same"},
      {**template, "trace_id": "trace-z", "span_id": "same"},
  ]
  watermark_path = tmp_path / "watermark.json"

  export(
      "project.dataset.agent_events",
      config=ExportConfig(
          incremental=True,
          watermark_path=watermark_path,
          requests_per_second=None,
      ),
      bq_client=_BigQueryClient(rows),
      langsmith_client=_LangSmithClient(),
  )

  stored = json.loads(watermark_path.read_text(encoding="utf-8"))
  assert stored["trace_id"] == "trace-z"
  assert stored["run_id"] == "same"


def test_watermark_rejects_unknown_version_and_destination_change(
    tmp_path: Path,
) -> None:
  watermark_path = tmp_path / "watermark.json"
  base = ExportConfig(
      incremental=True,
      watermark_path=watermark_path,
      langsmith_project="project-a",
      langsmith_endpoint="https://one.example",
      langsmith_workspace_id="workspace-a",
      requests_per_second=None,
  )
  export(
      "project.dataset.agent_events",
      config=base,
      bq_client=_BigQueryClient(_standard_rows()),
      langsmith_client=_LangSmithClient(),
  )

  changed_destination = ExportConfig(
      incremental=True,
      watermark_path=watermark_path,
      langsmith_project="project-b",
      langsmith_endpoint="https://one.example",
      langsmith_workspace_id="workspace-a",
      requests_per_second=None,
  )
  with pytest.raises(ValueError, match="different source.*destination"):
    export(
        "project.dataset.agent_events",
        config=changed_destination,
        bq_client=_BigQueryClient([]),
        langsmith_client=_LangSmithClient(),
    )

  stored = json.loads(watermark_path.read_text(encoding="utf-8"))
  stored["version"] = 999
  watermark_path.write_text(json.dumps(stored), encoding="utf-8")
  with pytest.raises(ValueError, match="unsupported watermark version"):
    export(
        "project.dataset.agent_events",
        config=base,
        bq_client=_BigQueryClient([]),
        langsmith_client=_LangSmithClient(),
    )


def test_incremental_watermark_rejects_a_different_source(
    tmp_path: Path,
) -> None:
  watermark_path = tmp_path / "watermark.json"
  config = ExportConfig(
      langsmith_project="destination",
      incremental=True,
      watermark_path=watermark_path,
      requests_per_second=None,
  )
  export(
      "project.dataset.first",
      config=config,
      bq_client=_BigQueryClient(_standard_rows()),
      langsmith_client=_LangSmithClient(),
  )

  with pytest.raises(ValueError, match="different source"):
    export(
        "project.dataset.second",
        config=config,
        bq_client=_BigQueryClient([]),
        langsmith_client=_LangSmithClient(),
    )


def test_default_client_uses_hyphenated_langsmith_export_surface() -> None:
  bq = _BigQueryClient([])
  with patch(
      "bigquery_agent_analytics.export.langsmith.make_bq_client",
      return_value=bq,
  ) as make_client:
    export(
        "project.dataset.agent_events",
        config=ExportConfig(requests_per_second=None),
        langsmith_client=_LangSmithClient(),
    )

  make_client.assert_called_once_with(
      "project", location=None, sdk_surface="langsmith-export"
  )


def test_batch_size_and_rate_limit_apply_between_api_requests() -> None:
  rows = [_standard_rows()[0]]
  rows.append({**rows[0], "trace_id": "trace-2", "span_id": "span-2"})
  sleeps: list[float] = []
  clock = iter([0.0, 0.0, 0.5, 0.5, 1.0, 1.0, 1.5])
  with (
      patch(
          "bigquery_agent_analytics.export.langsmith.time.sleep",
          side_effect=sleeps.append,
      ),
      patch(
          "bigquery_agent_analytics.export.langsmith.time.monotonic",
          side_effect=lambda: next(clock),
      ),
  ):
    stats = export(
        "project.dataset.agent_events",
        config=ExportConfig(
            batch_size=1,
            requests_per_second=2,
            max_retries=0,
        ),
        bq_client=_BigQueryClient(rows),
        langsmith_client=_LangSmithClient(),
    )

  assert stats.batches == 2
  assert sleeps == [0.5, 0.5, 0.5]


def test_batch_size_is_hard_limit_for_one_oversized_trace() -> None:
  template = _standard_rows()[0]
  rows = [
      {
          **template,
          "timestamp": _T0.replace(microsecond=index),
          "span_id": f"span-{index}",
      }
      for index in range(5)
  ]
  langsmith = _LangSmithClient()

  stats = export(
      "project.dataset.agent_events",
      config=ExportConfig(batch_size=2, requests_per_second=None),
      bq_client=_BigQueryClient(rows),
      langsmith_client=langsmith,
  )

  source_counts = [
      sum("source_run_id" in run["extra"]["bqaa"] for run in call["create"])
      for call in langsmith.calls
      if call["create"] is not None
  ]
  assert source_counts == [2, 2, 1]
  assert stats.exported == 5
  assert stats.failed == 0
  assert stats.batches == 3
  assert stats.traces_exported == 1


def test_partial_oversized_trace_failure_is_not_counted_or_watermarked(
    tmp_path: Path,
) -> None:
  template = _standard_rows()[0]
  rows = [
      {
          **template,
          "timestamp": _T0.replace(microsecond=index),
          "span_id": f"span-{index}",
      }
      for index in range(5)
  ]
  watermark_path = tmp_path / "watermark.json"

  stats = export(
      "project.dataset.agent_events",
      config=ExportConfig(
          incremental=True,
          watermark_path=watermark_path,
          batch_size=2,
          max_retries=0,
          requests_per_second=None,
      ),
      bq_client=_BigQueryClient(rows),
      langsmith_client=_FailOnCallLangSmithClient(call_number=3),
  )

  assert stats.exported == 3
  assert stats.failed == 2
  assert stats.traces_exported == 0
  assert not watermark_path.exists()


def test_cycle_is_broken_below_synthetic_root_without_dropping_rows() -> None:
  first, second = _standard_rows()
  first["parent_span_id"] = "span-child"
  second["parent_span_id"] = "span-root"

  prepared = _prepare_trace_runs(
      [first, second],
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="source",
      project_name="p",
  )

  assert len(prepared.runs) == 3
  assert {
      run["extra"]["bqaa"]["source_run_id"] for run in prepared.runs[1:]
  } == {"span-root", "span-child"}


def test_deep_trace_does_not_depend_on_python_recursion_limit() -> None:
  template = _standard_rows()[0]
  depth = 1100
  rows = [
      {
          **template,
          "timestamp": _T0.replace(microsecond=index),
          "span_id": f"span-{index}",
          "parent_span_id": f"span-{index - 1}" if index else None,
      }
      for index in range(depth)
  ]

  prepared = _prepare_trace_runs(
      rows,
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="source",
      project_name="p",
  )

  assert len(prepared.runs) == depth + 1
  assert prepared.runs[-1]["extra"]["bqaa"]["source_run_id"] == "span-1099"


def test_failed_batch_is_reported_and_does_not_advance_watermark(
    tmp_path: Path,
) -> None:
  sleeps: list[float] = []
  watermark_path = tmp_path / "watermark.json"
  with patch(
      "bigquery_agent_analytics.export.langsmith.time.sleep",
      side_effect=sleeps.append,
  ):
    stats = export(
        "project.dataset.agent_events",
        config=ExportConfig(
            langsmith_project="destination",
            incremental=True,
            watermark_path=watermark_path,
            max_retries=1,
            retry_backoff_seconds=0.25,
            requests_per_second=None,
        ),
        bq_client=_BigQueryClient(_standard_rows()),
        langsmith_client=_LangSmithClient(failures=2),
    )

  assert sleeps == [0.25]
  assert stats.exported == 0
  assert stats.failed == 2
  assert {d.source_run_id for d in stats.dropped_rows} == {
      "span-root",
      "span-child",
  }
  assert not watermark_path.exists()


def test_dropped_row_details_are_capped_without_losing_totals() -> None:
  template = _standard_rows()[0]
  rows = [
      {**template, "span_id": None, "custom_row_number": index}
      for index in range(5)
  ]

  stats = export(
      "project.dataset.agent_events",
      config=ExportConfig(
          max_dropped_rows=2,
          requests_per_second=None,
      ),
      bq_client=_BigQueryClient(rows),
      langsmith_client=_LangSmithClient(),
  )

  assert stats.skipped == 5
  assert stats.failed == 0
  assert len(stats.dropped_rows) == 2
  assert stats.dropped_rows_truncated == 3
  assert stats.to_dict()["dropped_rows_truncated"] == 3


def test_trace_preparation_uses_shared_bounded_dropped_row_collector() -> None:
  template = _standard_rows()[0]
  rows = [{**template, "span_id": None} for _ in range(5)]
  dropped_rows = _DroppedRowCollector(maximum=2)

  prepared = _prepare_trace_runs(
      rows,
      mapping=FieldMapping.standard_adk(),
      source_fingerprint="source",
      project_name="p",
      dropped_rows=dropped_rows,
  )

  assert prepared.skipped_count == 5
  assert dropped_rows.total == 5
  assert len(dropped_rows.rows) == 2
  assert dropped_rows.truncated == 3


def test_invalid_mapping_rejects_missing_identity_fields() -> None:
  with pytest.raises(ValueError, match="run_id"):
    FieldMapping.from_dict({"run_id": None, "trace_id": "trace"})


def test_real_client_adapter_resurfaces_sdk_swallowed_ingest_errors() -> None:
  class _SwallowingClient:

    def __init__(self, **kwargs) -> None:
      self.callback = kwargs["tracing_error_callback"]

    def batch_ingest_runs(self, *, create=None, update=None) -> None:
      del create, update
      self.callback(_RetryableError(429))

  module = SimpleNamespace(Client=_SwallowingClient)
  with patch.dict(sys.modules, {"langsmith": module}):
    client = _create_langsmith_client(ExportConfig(requests_per_second=None))

  with pytest.raises(_RetryableError):
    client.batch_ingest_runs(create=[{"id": "run"}])


def test_real_langsmith_client_serializes_separate_create_and_update_batches(
    monkeypatch,
) -> None:
  langsmith = pytest.importorskip("langsmith")
  client = langsmith.Client(
      api_key="test-key",
      auto_batch_tracing=False,
      info={},
  )
  request_bodies: list[dict[str, Any]] = []

  def capture_batch(_client, body: bytes, **kwargs) -> None:
    del _client, kwargs
    request_bodies.append(json.loads(body))

  monkeypatch.setattr(type(client), "_post_batch_ingest_runs", capture_batch)

  stats = export(
      "project.dataset.agent_events",
      config=ExportConfig(requests_per_second=None),
      bq_client=_BigQueryClient(_standard_rows()),
      langsmith_client=client,
  )

  assert stats.exported == 2
  assert len(request_bodies) == 2
  assert len(request_bodies[0]["post"]) == 3
  assert "patch" not in request_bodies[0]
  assert len(request_bodies[1]["patch"]) == 3
  assert "post" not in request_bodies[1]
  child_update = next(
      run
      for run in request_bodies[1]["patch"]
      if run.get("extra", {}).get("bqaa", {}).get("source_run_id")
      == "span-child"
  )
  assert child_update["error"] == "tool failed"


def test_real_langsmith_retry_rebuilds_mutated_update_payloads(
    monkeypatch,
) -> None:
  langsmith = pytest.importorskip("langsmith")
  client = langsmith.Client(
      api_key="test-key",
      auto_batch_tracing=False,
      info={},
  )
  patch_bodies: list[dict[str, Any]] = []

  def fail_first_patch(_client, body: bytes, **kwargs) -> None:
    del _client, kwargs
    decoded = json.loads(body)
    if "patch" not in decoded:
      return
    patch_bodies.append(decoded)
    if len(patch_bodies) == 1:
      raise _RetryableError(429)

  monkeypatch.setattr(type(client), "_post_batch_ingest_runs", fail_first_patch)

  stats = export(
      "project.dataset.agent_events",
      config=ExportConfig(
          requests_per_second=None,
          max_retries=1,
          retry_backoff_seconds=0,
      ),
      bq_client=_BigQueryClient(_standard_rows()),
      langsmith_client=client,
  )

  assert stats.exported == 2
  assert len(patch_bodies) == 2
  retried_child = next(
      run
      for run in patch_bodies[1]["patch"]
      if run.get("extra", {}).get("bqaa", {}).get("source_run_id")
      == "span-child"
  )
  assert retried_child["inputs"] == {"value": ["opaque", {"payload": True}]}
  assert retried_child["error"] == "tool failed"


def test_real_langsmith_transient_errors_and_groups_are_retryable() -> None:
  langsmith_utils = pytest.importorskip("langsmith.utils")
  transient = [
      langsmith_utils.LangSmithAPIError("server"),
      langsmith_utils.LangSmithConnectionError("connection"),
      langsmith_utils.LangSmithRateLimitError("rate"),
      langsmith_utils.LangSmithRequestTimeout("timeout"),
  ]

  assert all(_is_retryable(exc) for exc in transient)
  assert _is_retryable(
      langsmith_utils.LangSmithExceptionGroup(exceptions=transient)
  )
  assert not _is_retryable(
      langsmith_utils.LangSmithExceptionGroup(
          exceptions=[transient[0], langsmith_utils.LangSmithAuthError("auth")]
      )
  )
  assert not _is_retryable(langsmith_utils.LangSmithConflictError("conflict"))
  assert not _is_retryable(_RetryableError(409))
