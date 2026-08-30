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
"""Tests for the EvalBench reader/mapper (#97) and snapshot importer (#435)."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from datetime import timezone
import json
import math
import re

import pytest

from bigquery_agent_analytics import evalbench
from bigquery_agent_analytics.evalbench import classify_sessions
from bigquery_agent_analytics.evalbench import EvalBenchRun
from bigquery_agent_analytics.evalbench import EvalScorePolicy
from bigquery_agent_analytics.evalbench import failed_sessions_sql

_RUN_TIME = datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc)
_EXPECTED_EVENT_COLUMNS = {
    "session_id",
    "event_type",
    "timestamp",
    "agent",
    "invocation_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "user_id",
    "content",
    "content_parts",
    "attributes",
    "latency_ms",
    "status",
    "error_message",
    "is_truncated",
}


class _FakeQueryJob:

  def __init__(self, rows: list[dict]) -> None:
    self._rows = rows

  def result(self) -> list[dict]:
    return self._rows


class _FakeBigQueryClient:

  def __init__(self, tables: dict[str, list[dict]]) -> None:
    self.tables = tables
    self.calls: list[tuple[str, dict]] = []

  def query(self, query: str, **kwargs) -> _FakeQueryJob:
    self.calls.append((query, kwargs))
    for table_name, rows in self.tables.items():
      if f".{table_name}`" in query:
        return _FakeQueryJob(rows)
    raise AssertionError(f"unexpected query: {query}")


def _run(*results: dict, config_rows: tuple[dict, ...] | None = None):
  if config_rows is None:
    config_rows = (
        {
            "config": "experiment_config.orchestrator",
            "value": "geminicli",
            "run_time": _RUN_TIME,
        },
        {
            "config": "model_config.generator",
            "value": "gemini_cli",
            "run_time": _RUN_TIME,
        },
    )
  return EvalBenchRun(
      project_id="source-project",
      evalbench_dataset="evalbench",
      job_id="job-123",
      location="US",
      results=tuple(results),
      config_rows=config_rows,
  )


def test_from_bigquery_filters_every_source_query_by_job_id() -> None:
  fake = _FakeBigQueryClient(
      {
          "results": [{"id": "scenario-1", "job_id": "job-123"}],
          "scores": [
              {
                  "id": "scenario-1",
                  "job_id": "job-123",
                  "comparator": "goal_completion",
                  "score": 1,
              }
          ],
          "configs": [
              {
                  "job_id": "job-123",
                  "config": "experiment_config.orchestrator",
                  "value": "geminicli",
              }
          ],
      }
  )

  run = EvalBenchRun.from_bigquery(
      project_id="source-project",
      evalbench_dataset="evalbench",
      job_id="job-123",
      location="EU",
      bq_client=fake,
  )

  assert len(run.results) == 1
  assert len(run.scores) == 1
  assert len(run.config_rows) == 1
  assert len(fake.calls) == 3
  for query, kwargs in fake.calls:
    assert "WHERE job_id = @job_id" in query
    assert "job-123" not in query
    assert kwargs["location"] == "EU"
    job_config = kwargs["job_config"]
    assert job_config.labels["sdk_feature"] == "evalbench-import"
    parameter = job_config.query_parameters[0]
    assert parameter.name == "job_id"
    assert parameter.value == "job-123"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "source-project`; DROP TABLE x; --"),
        ("evalbench_dataset", "evalbench.bad"),
    ],
)
def test_from_bigquery_rejects_unsafe_source_identifiers(
    field: str, value: str
) -> None:
  kwargs = {
      "project_id": "source-project",
      "evalbench_dataset": "evalbench",
      "job_id": "job-123",
      "bq_client": _FakeBigQueryClient({}),
  }
  kwargs[field] = value
  with pytest.raises(ValueError, match=field):
    EvalBenchRun.from_bigquery(**kwargs)


def test_current_agentic_row_maps_prompt_tools_response_and_identity() -> None:
  stdout = json.dumps(
      {
          "response": "The customer qualifies for a refund.",
          "tool_calls": [
              {
                  "tool_name": "orders__lookup",
                  "parameters": {"order_id": "A-1"},
                  "response": {"status": "delivered"},
                  "status": "success",
                  "timestamp": "2026-04-29T12:30:00Z",
                  "result_timestamp": "2026-04-29T12:30:00.125Z",
              }
          ],
      }
  )
  rows = _run(
      {
          "eval_id": "refund-1",
          "prompt": "Can order A-1 be refunded?",
          "stdout": stdout,
          "returncode": 0,
      }
  ).to_agent_event_rows()

  assert [row["event_type"] for row in rows] == [
      "USER_MESSAGE_RECEIVED",
      "TOOL_STARTING",
      "TOOL_COMPLETED",
      "AGENT_COMPLETED",
  ]
  assert all(set(row) == _EXPECTED_EVENT_COLUMNS for row in rows)
  assert all(row["session_id"] == "evalbench:job-123:refund-1" for row in rows)
  assert all(row["trace_id"] == row["session_id"] for row in rows)
  assert all(row["agent"] == "evalbench:geminicli:gemini_cli" for row in rows)
  assert rows[0]["content"] == {
      "text": "Can order A-1 be refunded?",
      "text_summary": "Can order A-1 be refunded?",
  }
  assert rows[1]["content"]["args"] == {"order_id": "A-1"}
  assert rows[2]["content"]["result"] == {"status": "delivered"}
  assert rows[2]["latency_ms"] == {"total_ms": 125}
  assert rows[-1]["content"]["response"] == (
      "The customer qualifies for a refund."
  )
  assert "evalbench_error_fields" not in rows[0]["attributes"]


def test_failed_tool_emits_tool_error_for_session_summary() -> None:
  stdout = json.dumps(
      {
          "response": "The lookup failed.",
          "tool_calls": [
              {
                  "tool_name": "orders__lookup",
                  "parameters": {"order_id": "missing"},
                  "error": "order not found",
              }
          ],
      }
  )

  rows = _run(
      {"eval_id": "tool-error-1", "prompt": "Find the order", "stdout": stdout}
  ).to_agent_event_rows()

  assert [row["event_type"] for row in rows] == [
      "USER_MESSAGE_RECEIVED",
      "TOOL_STARTING",
      "TOOL_ERROR",
      "AGENT_COMPLETED",
  ]
  tool_error = rows[2]
  assert tool_error["status"] == "ERROR"
  assert tool_error["error_message"] == "order not found"


def test_synthetic_rows_satisfy_judge_text_and_response_contracts() -> None:
  rows = _run(
      {
          "id": "sql-1",
          "nl_prompt": "List the five newest orders.",
          "generated_sql": "SELECT * FROM orders ORDER BY created_at DESC LIMIT 5",
          "run_time": _RUN_TIME,
      }
  ).to_agent_event_rows()

  trace_text = "\n".join(
      f"{row['event_type']}: {row['content'].get('text_summary', '')}"
      for row in rows
  )
  assert len(trace_text) > 10
  assert all(row["content"]["text_summary"] for row in rows)
  completed = [row for row in rows if row["event_type"] == "AGENT_COMPLETED"]
  assert len(completed) == 1
  assert completed[0]["content"]["response"].startswith("SELECT")
  assert completed[0]["attributes"]["experiment_id"] == "job-123"
  assert completed[0]["attributes"]["evalbench_scenario_id"] == "sql-1"


def test_missing_tools_and_final_response_are_non_fatal() -> None:
  rows = _run(
      {
          "id": "sql-without-output",
          "nl_prompt": "A valid prompt remains importable.",
          "generated_sql": "skipped",
          "input_tokens": 25,
          "output_tokens": 5,
      },
      config_rows=(),
  ).to_agent_event_rows()

  assert len(rows) == 1
  assert rows[0]["event_type"] == "USER_MESSAGE_RECEIVED"
  assert rows[0]["timestamp"] == "1970-01-01T00:00:00+00:00"
  assert rows[0]["attributes"]["evalbench_run_time_missing"] is True
  assert rows[0]["attributes"]["usage_metadata"]["total_token_count"] == 30


def test_missing_nl_prompt_is_a_hard_failure() -> None:
  run = _run({"id": "missing-prompt", "generated_sql": "SELECT 1"})
  with pytest.raises(ValueError, match="missing nl_prompt/prompt"):
    run.to_agent_event_rows()


def test_missing_scenario_id_is_a_hard_failure() -> None:
  run = _run({"nl_prompt": "No identifier", "generated_sql": "SELECT 1"})
  with pytest.raises(ValueError, match="missing id/eval_id"):
    run.to_agent_event_rows()


def test_agentic_multimodel_tokens_sum_without_multiplying_latency() -> None:
  stdout = json.dumps(
      {
          "response": "Done",
          "stats": {
              "models": {
                  "gemini-2.5-flash": {
                      "api": {"totalLatencyMs": 850},
                      "tokens": {
                          "input": 120,
                          "candidates": 30,
                          "total": 150,
                          "cached": 20,
                      },
                  },
                  "gemini-2.5-pro": {
                      "api": {"totalLatencyMs": 850},
                      "tokens": {
                          "input": 20,
                          "candidates": 10,
                          "total": 30,
                          "cached": 5,
                      },
                  },
              }
          },
      }
  )
  rows = _run(
      {"eval_id": "token-1", "prompt": "Run the task", "stdout": stdout}
  ).to_agent_event_rows()
  completed = rows[-1]

  assert completed["latency_ms"] == {"total_ms": 850}
  assert completed["attributes"]["input_tokens"] == 140
  assert completed["attributes"]["output_tokens"] == 40
  assert completed["attributes"]["usage_metadata"] == {
      "prompt_token_count": 140,
      "candidates_token_count": 40,
      "total_token_count": 180,
      "cached_content_token_count": 25,
  }


def test_error_fields_are_preserved_without_inventing_a_final_response() -> (
    None
):
  rows = _run(
      {
          "eval_id": "failed-1",
          "prompt": "Run a failing command",
          "stdout": "",
          "stderr": "command failed",
          "returncode": 2,
      }
  ).to_agent_event_rows()

  assert len(rows) == 1
  assert rows[0]["status"] == "ERROR"
  assert "returncode: 2" in rows[0]["error_message"]
  assert "stderr: command failed" in rows[0]["error_message"]
  assert rows[0]["attributes"]["evalbench_error_fields"] == {
      "stderr": "command failed",
      "returncode": 2,
  }


def test_python_literal_nested_result_shape_is_supported() -> None:
  rows = _run(
      {
          "eval_results": repr(
              {
                  "eval_id": "legacy-agent-1",
                  "prompt": "Inspect the repository",
                  "stdout": json.dumps({"response": "Inspection complete"}),
                  "accumulated_tools": ["read_file"],
              }
          )
      }
  ).to_agent_event_rows()

  assert rows[0]["session_id"] == "evalbench:job-123:legacy-agent-1"
  assert [row["event_type"] for row in rows] == [
      "USER_MESSAGE_RECEIVED",
      "TOOL_STARTING",
      "TOOL_COMPLETED",
      "AGENT_COMPLETED",
  ]
  assert rows[-1]["content"]["response"] == "Inspection complete"


def test_mapping_is_deterministic_and_sorts_unique_scenarios() -> None:
  run = _run(
      {"id": "b", "nl_prompt": "Prompt B", "generated_sql": "SELECT 2"},
      {"id": "a", "nl_prompt": "Prompt A", "generated_sql": "SELECT 1"},
  )
  first = run.to_agent_event_rows()
  second = run.to_agent_event_rows()

  assert first == second
  assert first[0]["session_id"] == "evalbench:job-123:a"
  assert first[2]["session_id"] == "evalbench:job-123:b"


def test_duplicate_scenario_ids_are_rejected() -> None:
  run = _run(
      {"id": "duplicate", "nl_prompt": "First", "generated_sql": "SELECT 1"},
      {"id": "duplicate", "nl_prompt": "Retry", "generated_sql": "SELECT 2"},
  )

  with pytest.raises(ValueError, match="duplicate scenario id 'duplicate'"):
    run.to_agent_event_rows()


# ---------------------------------------------------------------------------
# materialize(): atomic, idempotent snapshot into BQAA-owned tables (#435)
# ---------------------------------------------------------------------------


class _FakeJob:

  def __init__(self, rows=(), error: Exception | None = None) -> None:
    self._rows = list(rows)
    self._error = error

  def result(self):
    if self._error is not None:
      raise self._error
    return self._rows


class _FakeManifestStore:
  """Committed manifest rows shared by several fake clients (one BigQuery)."""

  def __init__(self, rows: list[dict] | None = None) -> None:
    self.rows: list[dict] = list(rows or [])


_RAISE_MESSAGE = re.compile(r"RAISE USING MESSAGE = '([^']*)'")


class _FakeWriteClient:
  """Records loads, queries, and table DDL issued by ``materialize``.

  The publish transaction is emulated against a ``_FakeManifestStore``: the
  guard predicate rendered in the script is evaluated against the committed
  rows with the query parameters, the script's own ``RAISE`` message is
  surfaced on conflict, and otherwise the staged manifest row replaces the
  committed one. ``stale_manifest_reads`` makes the pre-publish manifest read
  return nothing, which is how two importers that both observed no row are
  interleaved deterministically.
  """

  def __init__(
      self,
      *,
      manifest_rows: list[dict] | None = None,
      store: _FakeManifestStore | None = None,
      stale_manifest_reads: bool = False,
      load_error: Exception | None = None,
      transaction_error: Exception | None = None,
      delete_error: Exception | None = None,
  ) -> None:
    self.store = store or _FakeManifestStore(manifest_rows)
    self.stale_manifest_reads = stale_manifest_reads
    self.load_error = load_error
    self.transaction_error = transaction_error
    self.delete_error = delete_error
    self.loads: list[tuple[str, list[dict], object]] = []
    self.queries: list[tuple[str, dict]] = []
    self.created: list[str] = []
    self.created_tables: list[object] = []
    self.deleted: list[str] = []

  @property
  def manifest_rows(self) -> list[dict]:
    return self.store.rows

  def create_table(self, table, exists_ok: bool = False):
    assert exists_ok is True
    self.created.append(f"{table.project}.{table.dataset_id}.{table.table_id}")
    self.created_tables.append(table)
    return table

  def query(self, query: str, **kwargs) -> _FakeJob:
    self.queries.append((query, kwargs))
    params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
    if "BEGIN TRANSACTION" in query:
      if self.transaction_error is not None:
        return _FakeJob(error=self.transaction_error)
      return self._publish(query, params)
    if ".evalbench_import_manifest`" in query:
      if self.stale_manifest_reads:
        return _FakeJob([])
      return _FakeJob(
          [
              row
              for row in self.store.rows
              if row["job_id"] == params["job_id"]
              and row["import_version"] == params["import_version"]
          ]
      )
    raise AssertionError(f"unexpected query: {query}")

  def _publish(self, script: str, params: dict) -> _FakeJob:
    def same_version(row: dict) -> bool:
      return (
          row["job_id"] == params["job_id"]
          and row["import_version"] == params["import_version"]
      )

    predicates = []
    if "results_fingerprint != @results_fingerprint" in script:
      predicates.append(
          lambda row: any(
              row[key] != params[key]
              for key in (
                  "results_fingerprint",
                  "scores_fingerprint",
                  "configs_fingerprint",
              )
          )
      )
    if "events_table != @events_table" in script:
      predicates.append(
          lambda row: row["events_table"] != params["events_table"]
          or row["scores_table"] != params["scores_table"]
      )
    conflicts = [
        row
        for row in self.store.rows
        if same_version(row) and any(pred(row) for pred in predicates)
    ]
    if conflicts:
      message = _RAISE_MESSAGE.search(script).group(1)
      return _FakeJob(error=RuntimeError(f"400 {message}"))
    staged_manifest = next(
        rows
        for dest, rows, _ in self.loads
        if "evalbench_import_manifest" in dest
    )
    self.store.rows = [
        row for row in self.store.rows if not same_version(row)
    ] + list(staged_manifest)
    return _FakeJob()

  def load_table_from_json(self, rows, destination, job_config=None):
    self.loads.append((destination, list(rows), job_config))
    return _FakeJob(error=self.load_error)

  def delete_table(self, table_ref: str, not_found_ok: bool = False) -> None:
    assert not_found_ok is True
    self.deleted.append(table_ref)
    if self.delete_error is not None:
      raise self.delete_error


def _scored_run(*, extra_result: dict | None = None, scores=None):
  results = [
      {
          "eval_id": "ok-1",
          "prompt": "Passing scenario",
          "stdout": json.dumps({"response": "done"}),
          "returncode": 0,
          "run_time": _RUN_TIME,
      },
      {
          "eval_id": "crash-1",
          "prompt": "Crashing scenario",
          "stdout": "",
          "stderr": "boom",
          "returncode": 2,
          "run_time": _RUN_TIME,
      },
  ]
  if extra_result is not None:
    results.append(extra_result)
  if scores is None:
    scores = (
        {"eval_id": "ok-1", "comparator": "goal_completion", "score": 1},
        {"eval_id": "crash-1", "comparator": "goal_completion", "score": 0},
    )
  return EvalBenchRun(
      project_id="source-project",
      evalbench_dataset="evalbench",
      job_id="job-123",
      location="US",
      results=tuple(results),
      scores=tuple(scores),
      config_rows=(
          {
              "config": "experiment_config.orchestrator",
              "value": "geminicli",
              "run_time": _RUN_TIME,
          },
      ),
  )


def _transaction_queries(fake: _FakeWriteClient) -> list[str]:
  return [sql for sql, _ in fake.queries if "BEGIN TRANSACTION" in sql]


def test_materialize_publishes_events_scores_and_manifest_atomically() -> None:
  fake = _FakeWriteClient()
  run = _scored_run()

  result = run.materialize(
      target_project="analytics-project",
      target_dataset="bqaa",
      bq_client=fake,
  )

  assert result.status == "imported"
  assert result.events_table == "analytics-project.bqaa.evalbench_agent_events"
  assert result.scores_table == (
      "analytics-project.bqaa.evalbench_scores_imported"
  )
  assert result.manifest_table == (
      "analytics-project.bqaa.evalbench_import_manifest"
  )
  # Never the ADK plugin's production table, and never the source project.
  for destination, _, _ in fake.loads:
    assert destination.startswith("analytics-project.bqaa.evalbench_")
    assert ".agent_events" not in destination
  assert all(
      ref.startswith("analytics-project.bqaa.evalbench_")
      for ref in fake.created
  )

  # Every load targets a per-import staging table, not the published one.
  staged = {destination for destination, _, _ in fake.loads}
  assert len(staged) == 3
  assert all("_staging_" in ref for ref in staged)
  assert set(fake.deleted) == staged

  # Exactly one transaction publishes all three tables; no delete-then-append.
  transactions = _transaction_queries(fake)
  assert len(transactions) == 1
  script = transactions[0]
  assert script.count("DELETE FROM") == 3
  assert script.count("INSERT INTO") == 3
  assert script.index("BEGIN TRANSACTION") < script.index("DELETE FROM")
  assert script.index("INSERT INTO") < script.index("COMMIT TRANSACTION")
  assert "job-123" not in script
  _, kwargs = next(
      (sql, kw) for sql, kw in fake.queries if "BEGIN TRANSACTION" in sql
  )
  params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
  assert params["job_id"] == "job-123"
  assert params["import_version"] == result.import_version
  assert kwargs["job_config"].labels["sdk_feature"] == "evalbench-import"
  assert kwargs["location"] == "US"

  # Rows are bound to the import version and keep the mapper's contract.
  event_rows = next(
      rows for dest, rows, _ in fake.loads if "evalbench_agent_events" in dest
  )
  assert {row["import_version"] for row in event_rows} == {
      result.import_version
  }
  assert {row["job_id"] for row in event_rows} == {"job-123"}
  assert all(row["content"]["text_summary"] for row in event_rows)
  assert all(
      row["attributes"]["experiment_id"] == "job-123" for row in event_rows
  )
  score_rows = next(
      rows
      for dest, rows, _ in fake.loads
      if "evalbench_scores_imported" in dest
  )
  version = result.import_version
  assert {row["session_id"] for row in score_rows} == {
      f"evalbench:job-123:{version}:ok-1",
      f"evalbench:job-123:{version}:crash-1",
  }
  # Score joins stay aligned with the version-specific event identity.
  assert {row["session_id"] for row in score_rows} == {
      row["session_id"] for row in event_rows
  }
  assert {row["comparator"] for row in score_rows} == {"goal_completion"}
  manifest_rows = next(
      rows
      for dest, rows, _ in fake.loads
      if "evalbench_import_manifest" in dest
  )
  assert len(manifest_rows) == 1
  manifest = manifest_rows[0]
  assert manifest["job_id"] == "job-123"
  assert manifest["import_version"] == result.import_version
  assert manifest["source_project"] == "source-project"
  assert manifest["source_dataset"] == "evalbench"
  assert manifest["results_count"] == 2
  assert manifest["scores_count"] == 2
  assert manifest["configs_count"] == 1
  assert manifest["event_row_count"] == len(event_rows)
  assert manifest["score_row_count"] == len(score_rows)
  assert manifest["events_table"] == result.events_table
  assert manifest["scores_table"] == result.scores_table
  for key in (
      "results_fingerprint",
      "scores_fingerprint",
      "configs_fingerprint",
  ):
    assert len(manifest[key]) == 64
  assert manifest["imported_at"]
  assert result.manifest == manifest


def test_materialize_defaults_target_project_to_source_project() -> None:
  fake = _FakeWriteClient()
  result = _scored_run().materialize(target_dataset="bqaa", bq_client=fake)
  assert result.events_table == "source-project.bqaa.evalbench_agent_events"


def test_materialize_same_job_and_version_is_a_noop() -> None:
  first = _FakeWriteClient()
  run = _scored_run()
  imported = run.materialize(target_dataset="bqaa", bq_client=first)

  second = _FakeWriteClient(manifest_rows=[imported.manifest])
  again = run.materialize(target_dataset="bqaa", bq_client=second)

  assert again.status == "unchanged"
  assert again.import_version == imported.import_version
  assert second.loads == []
  assert _transaction_queries(second) == []


def test_materialize_unchanged_source_rejects_other_destination_tables() -> (
    None
):
  """A no-op must never report tables that were never written (#451 P1)."""
  run = _scored_run()
  imported = run.materialize(
      target_dataset="bqaa", bq_client=_FakeWriteClient()
  )
  assert imported.events_table == "source-project.bqaa.evalbench_agent_events"

  second = _FakeWriteClient(manifest_rows=[imported.manifest])
  with pytest.raises(ValueError) as excinfo:
    run.materialize(
        target_dataset="bqaa",
        events_table="evalbench_agent_events_v2",
        bq_client=second,
    )
  message = str(excinfo.value)
  assert "source-project.bqaa.evalbench_agent_events'" in message
  assert "evalbench_agent_events_v2" in message
  assert "new import_version" in message
  assert second.loads == []
  assert _transaction_queries(second) == []


def test_materialize_replace_cannot_relocate_a_version() -> None:
  """``replace=True`` must not orphan rows in the manifest's tables."""
  run = _scored_run()
  imported = run.materialize(
      target_dataset="bqaa", bq_client=_FakeWriteClient()
  )

  second = _FakeWriteClient(manifest_rows=[imported.manifest])
  with pytest.raises(ValueError, match="bound to the tables in its manifest"):
    run.materialize(
        target_dataset="bqaa",
        scores_table="evalbench_scores_imported_v2",
        replace=True,
        bq_client=second,
    )
  assert second.loads == []
  assert _transaction_queries(second) == []
  # The transaction guard enforces the same binding even when the pre-read
  # missed the manifest row.
  stale = _FakeWriteClient(store=second.store, stale_manifest_reads=True)
  with pytest.raises(ValueError, match="published concurrently"):
    run.materialize(
        target_dataset="bqaa",
        scores_table="evalbench_scores_imported_v2",
        replace=True,
        bq_client=stale,
    )
  assert stale.store.rows == [imported.manifest]


def test_materialize_replace_republishes_identical_version() -> None:
  first = _FakeWriteClient()
  run = _scored_run()
  imported = run.materialize(target_dataset="bqaa", bq_client=first)

  second = _FakeWriteClient(manifest_rows=[imported.manifest])
  again = run.materialize(target_dataset="bqaa", bq_client=second, replace=True)

  assert again.status == "replaced"
  assert again.import_version == imported.import_version
  assert len(_transaction_queries(second)) == 1


def test_materialize_derived_version_changes_when_source_changes() -> None:
  base = _scored_run().materialize(
      target_dataset="bqaa", bq_client=_FakeWriteClient()
  )
  changed = _scored_run(
      extra_result={
          "eval_id": "late-1",
          "prompt": "A scenario appended after the first import",
          "stdout": json.dumps({"response": "late"}),
          "returncode": 0,
      }
  ).materialize(target_dataset="bqaa", bq_client=_FakeWriteClient())

  assert changed.import_version != base.import_version
  assert changed.manifest["results_fingerprint"] != (
      base.manifest["results_fingerprint"]
  )
  assert changed.manifest["scores_fingerprint"] == (
      base.manifest["scores_fingerprint"]
  )


def test_materialize_fingerprint_ignores_source_row_order() -> None:
  run = _scored_run()
  reordered = dataclasses.replace(
      run,
      results=tuple(reversed(run.results)),
      scores=tuple(reversed(run.scores)),
  )
  first = run.materialize(target_dataset="bqaa", bq_client=_FakeWriteClient())
  second = reordered.materialize(
      target_dataset="bqaa", bq_client=_FakeWriteClient()
  )
  assert first.import_version == second.import_version


def test_materialize_explicit_version_rejects_changed_source() -> None:
  first = _FakeWriteClient()
  imported = _scored_run().materialize(
      target_dataset="bqaa", import_version="v1", bq_client=first
  )
  assert imported.import_version == "v1"

  changed = _scored_run(
      extra_result={
          "eval_id": "late-1",
          "prompt": "Appended after v1 was imported",
          "stdout": json.dumps({"response": "late"}),
      }
  )
  second = _FakeWriteClient(manifest_rows=[imported.manifest])
  with pytest.raises(ValueError, match="fingerprint"):
    changed.materialize(
        target_dataset="bqaa", import_version="v1", bq_client=second
    )
  assert second.loads == []
  assert _transaction_queries(second) == []


def test_materialize_staging_failure_leaves_target_untouched() -> None:
  fake = _FakeWriteClient(load_error=RuntimeError("load failed"))
  with pytest.raises(RuntimeError, match="load failed"):
    _scored_run().materialize(target_dataset="bqaa", bq_client=fake)

  assert _transaction_queries(fake) == []
  # Staging tables are always cleaned up.
  assert fake.deleted


def test_materialize_transaction_failure_drops_staging_and_raises() -> None:
  fake = _FakeWriteClient(transaction_error=RuntimeError("commit failed"))
  with pytest.raises(RuntimeError, match="commit failed"):
    _scored_run().materialize(target_dataset="bqaa", bq_client=fake)
  staged = {destination for destination, _, _ in fake.loads}
  assert set(fake.deleted) == staged


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_project", "bad.project"),
        ("target_dataset", "bqaa`; DROP TABLE x; --"),
        ("events_table", "agent events"),
        ("scores_table", "scores;"),
    ],
)
def test_materialize_rejects_unsafe_target_identifiers(
    field: str, value: str
) -> None:
  kwargs = {"target_dataset": "bqaa", "bq_client": _FakeWriteClient()}
  kwargs[field] = value
  with pytest.raises(ValueError, match=field):
    _scored_run().materialize(**kwargs)


@pytest.mark.parametrize("field", ["events_table", "scores_table"])
@pytest.mark.parametrize("value", ["agent_events", "AGENT_EVENTS"])
def test_materialize_rejects_reserved_agent_events_table(
    field: str, value: str
) -> None:
  fake = _FakeWriteClient()
  kwargs = {"target_dataset": "bqaa", "bq_client": fake}
  kwargs[field] = value
  with pytest.raises(ValueError, match=f"{field} must not be the reserved"):
    _scored_run().materialize(**kwargs)
  # Rejected before any BigQuery operation.
  assert fake.created == []
  assert fake.queries == []
  assert fake.loads == []


def test_publish_script_guards_manifest_inside_the_transaction() -> None:
  fake = _FakeWriteClient()
  result = _scored_run().materialize(
      target_dataset="bqaa", import_version="v1", bq_client=fake
  )
  script = _transaction_queries(fake)[0]

  guard = script.index("conflicting_manifest_rows = (")
  assert script.index("BEGIN TRANSACTION") < guard < script.index("DELETE FROM")
  assert "RAISE USING MESSAGE" in script
  assert script.index("RAISE USING MESSAGE") < script.index("DELETE FROM")
  assert "results_fingerprint != @results_fingerprint" in script
  assert "scores_fingerprint != @scores_fingerprint" in script
  assert "configs_fingerprint != @configs_fingerprint" in script
  assert "events_table != @events_table" in script
  assert "scores_table != @scores_table" in script
  assert "EXCEPTION WHEN ERROR THEN" in script
  assert script.index("ROLLBACK TRANSACTION") > script.index(
      "COMMIT TRANSACTION"
  )
  _, kwargs = next(
      (sql, kw) for sql, kw in fake.queries if "BEGIN TRANSACTION" in sql
  )
  params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
  assert params == {
      "job_id": "job-123",
      "import_version": "v1",
      "results_fingerprint": result.manifest["results_fingerprint"],
      "scores_fingerprint": result.manifest["scores_fingerprint"],
      "configs_fingerprint": result.manifest["configs_fingerprint"],
      "events_table": result.events_table,
      "scores_table": result.scores_table,
  }


def test_publish_script_replace_skips_fingerprint_guard_only() -> None:
  fake = _FakeWriteClient()
  _scored_run().materialize(
      target_dataset="bqaa", import_version="v1", replace=True, bq_client=fake
  )
  script = _transaction_queries(fake)[0]
  assert "results_fingerprint != @results_fingerprint" not in script
  assert "events_table != @events_table" in script


def test_materialize_concurrent_first_imports_cannot_overwrite_version() -> (
    None
):
  """Two importers both see no manifest row; only the first may commit."""
  store = _FakeManifestStore()
  importer_a = _FakeWriteClient(store=store, stale_manifest_reads=True)
  importer_b = _FakeWriteClient(store=store, stale_manifest_reads=True)
  run_a = _scored_run()
  run_b = _scored_run(
      extra_result={
          "eval_id": "late-1",
          "prompt": "Different content under the same explicit version",
          "stdout": json.dumps({"response": "late"}),
      }
  )

  first = run_a.materialize(
      target_dataset="bqaa", import_version="v1", bq_client=importer_a
  )
  assert first.status == "imported"
  assert store.rows == [first.manifest]

  # Importer B's pre-read observed nothing, so it reaches the transaction;
  # the in-transaction guard sees A's committed row and refuses.
  with pytest.raises(ValueError, match="published concurrently"):
    run_b.materialize(
        target_dataset="bqaa", import_version="v1", bq_client=importer_b
    )
  assert store.rows == [first.manifest]
  assert len(_transaction_queries(importer_b)) == 1
  staged = {destination for destination, _, _ in importer_b.loads}
  assert set(importer_b.deleted) == staged

  # replace=True is the explicit override and wins the same race.
  importer_c = _FakeWriteClient(store=store, stale_manifest_reads=True)
  replaced = run_b.materialize(
      target_dataset="bqaa",
      import_version="v1",
      replace=True,
      bq_client=importer_c,
  )
  assert store.rows == [replaced.manifest]


def test_materialize_publishes_version_specific_identities() -> None:
  run = _scored_run()
  fake_v1 = _FakeWriteClient()
  fake_v2 = _FakeWriteClient()
  run.materialize(target_dataset="bqaa", import_version="v1", bq_client=fake_v1)
  run.materialize(target_dataset="bqaa", import_version="v2", bq_client=fake_v2)

  def published_events(fake):
    return next(
        rows for dest, rows, _ in fake.loads if "evalbench_agent_events" in dest
    )

  v1 = published_events(fake_v1)
  v2 = published_events(fake_v2)
  assert {row["trace_id"] for row in v1} == {
      "evalbench:job-123:v1:ok-1",
      "evalbench:job-123:v1:crash-1",
  }
  assert all(row["session_id"] == row["trace_id"] for row in v1)
  # A reader that filters only by trace_id (Client.get_trace /
  # _GET_TRACE_QUERY) can never merge two retained versions into one trace.
  assert {row["trace_id"] for row in v1}.isdisjoint(
      row["trace_id"] for row in v2
  )
  assert {row["span_id"] for row in v1}.isdisjoint(row["span_id"] for row in v2)
  assert {row["invocation_id"] for row in v1}.isdisjoint(
      row["invocation_id"] for row in v2
  )
  assert all(
      row["attributes"]["evalbench_import_version"] == "v1" for row in v1
  )
  assert all(
      row["attributes"]["evalbench_scenario_id"] in {"ok-1", "crash-1"}
      for row in v1
  )


def test_unversioned_reader_identity_is_unchanged() -> None:
  rows = _scored_run().to_agent_event_rows()
  assert {row["session_id"] for row in rows} == {
      "evalbench:job-123:ok-1",
      "evalbench:job-123:crash-1",
  }
  assert all(
      "evalbench_import_version" not in row["attributes"] for row in rows
  )


def test_materialize_rejects_caller_selected_manifest_table() -> None:
  """The manifest registry is fixed per dataset and cannot be redirected."""
  fake = _FakeWriteClient()
  with pytest.raises(TypeError, match="manifest_table"):
    _scored_run().materialize(
        target_dataset="bqaa",
        manifest_table="other_manifest",
        bq_client=fake,
    )
  assert fake.created == []
  assert fake.queries == []
  assert fake.loads == []


def test_materialize_consults_canonical_registry_for_custom_tables() -> None:
  """Every import checks ``<target>.evalbench_import_manifest`` regardless of
  which events/scores tables it writes."""
  fake = _FakeWriteClient()
  result = _scored_run().materialize(
      target_project="analytics-project",
      target_dataset="bqaa",
      events_table="custom_events",
      scores_table="custom_scores",
      import_version="v1",
      bq_client=fake,
  )
  registry = "analytics-project.bqaa.evalbench_import_manifest"
  assert result.manifest_table == registry
  assert evalbench.MANIFEST_TABLE == "evalbench_import_manifest"
  pre_reads = [sql for sql, _ in fake.queries if "BEGIN TRANSACTION" not in sql]
  assert pre_reads and all(f"`{registry}`" in sql for sql in pre_reads)
  script = _transaction_queries(fake)[0]
  assert f"FROM `{registry}`" in script
  assert f"DELETE FROM `{registry}`" in script
  assert f"INSERT INTO `{registry}`" in script
  # The manifest staging table derives from the registry name as well.
  assert any(f"{registry}_staging_" in dest for dest, _, _ in fake.loads)


def test_changed_source_cannot_replace_shared_rows_via_second_manifest() -> (
    None
):
  """Codex P1 regression: publish ``job/v1``, then publish *changed* source
  for the same ``job/v1`` into the same events/scores tables. With one
  canonical registry the second import cannot route around the first
  manifest row, so the shared rows are never deleted or replaced."""
  store = _FakeManifestStore()
  first_client = _FakeWriteClient(store=store)
  first = _scored_run().materialize(
      target_dataset="bqaa",
      events_table="shared_events",
      scores_table="shared_scores",
      import_version="v1",
      bq_client=first_client,
  )
  assert first.status == "imported"
  assert store.rows == [first.manifest]

  changed = _scored_run(
      extra_result={
          "eval_id": "late-1",
          "prompt": "Changed content under the same version",
          "stdout": json.dumps({"response": "late"}),
      }
  )
  # The pre-read sees the registry row and refuses before staging anything.
  second_client = _FakeWriteClient(store=store)
  with pytest.raises(ValueError, match="different source fingerprints"):
    changed.materialize(
        target_dataset="bqaa",
        events_table="shared_events",
        scores_table="shared_scores",
        import_version="v1",
        bq_client=second_client,
    )
  assert store.rows == [first.manifest]
  assert second_client.loads == []
  assert _transaction_queries(second_client) == []

  # Even an importer whose pre-read is stale hits the same registry inside
  # the transaction and is rolled back; nothing is deleted from the shared
  # tables.
  stale_client = _FakeWriteClient(store=store, stale_manifest_reads=True)
  with pytest.raises(ValueError, match="published concurrently"):
    changed.materialize(
        target_dataset="bqaa",
        events_table="shared_events",
        scores_table="shared_scores",
        import_version="v1",
        bq_client=stale_client,
    )
  assert store.rows == [first.manifest]


def _colliding_runs() -> (
    tuple[tuple[EvalBenchRun, str], tuple[EvalBenchRun, str]]
):
  """Two ``(run, import_version)`` pairs whose naive ``:``-joined identity
  would both read ``evalbench:job-123:release:1:case``."""

  def run_for(scenario_id: str) -> EvalBenchRun:
    return _scored_run(
        extra_result={
            "eval_id": scenario_id,
            "prompt": "Prompt",
            "stdout": json.dumps(
                {
                    "response": "done",
                    "tool_calls": [
                        {"tool_name": "search", "args": {}, "result": "x"}
                    ],
                }
            ),
            "run_time": _RUN_TIME,
        },
        scores=({"eval_id": scenario_id, "comparator": "goal", "score": 1},),
    )

  return (run_for("case"), "release:1"), (run_for("1:case"), "release")


def test_session_identity_escapes_delimiters() -> None:
  left = evalbench._session_identity(
      "job-123", "case", import_version="release:1"
  )
  right = evalbench._session_identity(
      "job-123", "1:case", import_version="release"
  )
  assert left != right
  assert left == "evalbench:job-123:release\\:1:case"
  assert right == "evalbench:job-123:release:1\\:case"
  # Backslashes in a component are escaped too, so escaping is reversible.
  assert (
      evalbench._session_identity("job-123", "a\\:b", import_version=None)
      == "evalbench:job-123:a\\\\\\:b"
  )
  # A plain read of scenario ``v1:case`` never aliases published ``v1``.
  assert evalbench._session_identity(
      "job-123", "v1:case", import_version=None
  ) != evalbench._session_identity("job-123", "case", import_version="v1")
  # Common case (no delimiters) keeps the documented readable form.
  assert (
      evalbench._session_identity("job-123", "ok-1", import_version="v1")
      == "evalbench:job-123:v1:ok-1"
  )


@pytest.mark.parametrize(
    "column", ["session_id", "trace_id", "invocation_id", "span_id"]
)
def test_versioned_event_identities_are_collision_safe(column: str) -> None:
  (run_a, version_a), (run_b, version_b) = _colliding_runs()
  rows_a = [
      row
      for row in run_a.to_agent_event_rows(import_version=version_a)
      if row["attributes"]["evalbench_scenario_id"] == "case"
  ]
  rows_b = [
      row
      for row in run_b.to_agent_event_rows(import_version=version_b)
      if row["attributes"]["evalbench_scenario_id"] == "1:case"
  ]
  assert rows_a and rows_b
  # Same shape (user + tool start/end + completed) so the ids line up 1:1.
  assert [row["event_type"] for row in rows_a] == [
      row["event_type"] for row in rows_b
  ]
  ids_a = {row[column] for row in rows_a}
  ids_b = {row[column] for row in rows_b}
  assert ids_a.isdisjoint(ids_b), column
  parents_a = {row["parent_span_id"] for row in rows_a} - {None}
  parents_b = {row["parent_span_id"] for row in rows_b} - {None}
  assert parents_a.isdisjoint(parents_b)


def test_versioned_score_identities_are_collision_safe() -> None:
  (run_a, version_a), (run_b, version_b) = _colliding_runs()
  scores_a = {
      row["session_id"] for row in run_a.to_score_rows(import_version=version_a)
  }
  scores_b = {
      row["session_id"] for row in run_b.to_score_rows(import_version=version_b)
  }
  assert scores_a.isdisjoint(scores_b)
  # Scores still join their own version's events (only the extra scenario
  # is scored in ``_colliding_runs``).
  events_a = {
      row["session_id"]
      for row in run_a.to_agent_event_rows(import_version=version_a)
  }
  assert scores_a == {"evalbench:job-123:release\\:1:case"}
  assert scores_a <= events_a


def test_stable_id_is_not_ambiguous_across_part_boundaries() -> None:
  assert evalbench._stable_id("a\x1fb", "c", length=16) != evalbench._stable_id(
      "a", "b\x1fc", length=16
  )
  assert evalbench._stable_id("ab", "c", length=16) != evalbench._stable_id(
      "a", "bc", length=16
  )


def test_staging_tables_expire_and_cleanup_failure_does_not_mask_publish() -> (
    None
):
  fake = _FakeWriteClient(delete_error=RuntimeError("drop denied"))
  result = _scored_run().materialize(target_dataset="bqaa", bq_client=fake)

  assert result.status == "imported"
  assert len(fake.manifest_rows) == 1
  staging = [t for t in fake.created_tables if "_staging_" in t.table_id]
  assert len(staging) == 3
  assert all(table.expires is not None for table in staging)
  assert all(
      config.write_disposition == "WRITE_APPEND" for _, _, config in fake.loads
  )
  assert len(fake.deleted) == 3

  # A failed publish still surfaces its own error, not the cleanup's.
  failing = _FakeWriteClient(
      transaction_error=RuntimeError("commit failed"),
      delete_error=RuntimeError("drop denied"),
  )
  with pytest.raises(RuntimeError, match="commit failed"):
    _scored_run().materialize(target_dataset="bqaa", bq_client=failing)


def test_json_safe_keeps_non_finite_floats_loadable() -> None:
  assert evalbench._json_safe(
      {"nan": math.nan, "inf": math.inf, "ninf": -math.inf, "ok": 1.5}
  ) == {"nan": "NaN", "inf": "Infinity", "ninf": "-Infinity", "ok": 1.5}
  rows = _scored_run(
      scores=(
          {
              "eval_id": "ok-1",
              "comparator": "goal_completion",
              "score": math.nan,
          },
      )
  ).to_score_rows(import_version="v1")
  assert rows[0]["score"] is None
  assert rows[0]["source_row"]["score"] == "NaN"
  json.dumps(rows[0], allow_nan=False)


def test_score_rows_resolve_nested_scenario_ids_like_results() -> None:
  rows = _scored_run(
      scores=(
          {
              "eval_results": json.dumps({"eval_id": "ok-1"}),
              "comparator": "goal_completion",
              "score": 1,
          },
          {
              "scenario": {"id": "crash-1"},
              "comparator": "goal_completion",
              "score": 0,
          },
      )
  ).to_score_rows(import_version="v1")
  assert [row["scenario_id"] for row in rows] == ["crash-1", "ok-1"]
  assert [row["session_id"] for row in rows] == [
      "evalbench:job-123:v1:crash-1",
      "evalbench:job-123:v1:ok-1",
  ]


def test_from_bigquery_snapshot_reads_all_sources_as_of_one_timestamp() -> None:
  fake = _FakeBigQueryClient({"results": [], "scores": [], "configs": []})
  snapshot = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)

  run = EvalBenchRun.from_bigquery(
      project_id="source-project",
      evalbench_dataset="evalbench",
      job_id="job-123",
      snapshot_at=snapshot,
      bq_client=fake,
  )

  assert run.snapshot_at == snapshot
  assert len(fake.calls) == 3
  for query, kwargs in fake.calls:
    assert "FOR SYSTEM_TIME AS OF @snapshot_at" in query
    params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
    assert params["snapshot_at"] == snapshot


def test_materialize_records_snapshot_timestamp_in_manifest() -> None:
  snapshot = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
  run = dataclasses.replace(_scored_run(), snapshot_at=snapshot)
  result = run.materialize(target_dataset="bqaa", bq_client=_FakeWriteClient())
  assert result.manifest["source_snapshot_at"] == snapshot.isoformat()


# ---------------------------------------------------------------------------
# Failed-session denominator (W0.4): returncode 0 means completed, not passed
# ---------------------------------------------------------------------------


def _verdicts(run: EvalBenchRun, policy: EvalScorePolicy):
  return {
      verdict.session_id: verdict
      for verdict in classify_sessions(
          run.to_agent_event_rows(import_version="v1"),
          run.to_score_rows(import_version="v1"),
          policy,
      )
  }


def test_classify_sessions_counts_low_scoring_completed_runs_as_failed() -> (
    None
):
  run = _scored_run(
      extra_result={
          "eval_id": "wrong-1",
          "prompt": "Completed but wrong answer",
          "stdout": json.dumps({"response": "42"}),
          "returncode": 0,
      },
      scores=(
          {"eval_id": "ok-1", "comparator": "goal_completion", "score": 1},
          {"eval_id": "crash-1", "comparator": "goal_completion", "score": 0},
          {"eval_id": "wrong-1", "comparator": "goal_completion", "score": 0.2},
      ),
  )
  verdicts = _verdicts(run, EvalScorePolicy({"goal_completion": 0.5}))

  assert set(verdicts) == {
      "evalbench:job-123:v1:ok-1",
      "evalbench:job-123:v1:crash-1",
      "evalbench:job-123:v1:wrong-1",
  }
  passed = verdicts["evalbench:job-123:v1:ok-1"]
  assert passed.failed is False
  assert passed.process_failed is False
  assert passed.score_failed is False

  crashed = verdicts["evalbench:job-123:v1:crash-1"]
  assert crashed.failed is True
  assert crashed.process_failed is True
  assert crashed.missing_completion is True

  wrong = verdicts["evalbench:job-123:v1:wrong-1"]
  assert wrong.failed is True
  assert wrong.process_failed is False
  assert wrong.missing_completion is False
  assert wrong.score_failed is True
  assert wrong.failing_scores == {"goal_completion": 0.2}

  failed = sorted(v.session_id for v in verdicts.values() if v.failed)
  assert failed == [
      "evalbench:job-123:v1:crash-1",
      "evalbench:job-123:v1:wrong-1",
  ]


def test_classify_sessions_returncode_zero_without_scores_is_not_a_pass() -> (
    None
):
  run = _scored_run(scores=())
  verdicts = _verdicts(run, EvalScorePolicy({"goal_completion": 0.5}))
  completed = verdicts["evalbench:job-123:v1:ok-1"]
  assert completed.process_failed is False
  assert completed.score_failed is True
  assert completed.failed is True
  assert completed.failing_scores == {"goal_completion": None}

  lenient = _verdicts(
      run, EvalScorePolicy({"goal_completion": 0.5}, missing_score_fails=False)
  )
  assert lenient["evalbench:job-123:v1:ok-1"].failed is False


def test_stderr_is_a_process_failure_even_when_returncode_is_zero() -> None:
  """returncode 0 + final response + stderr must not publish as OK (#451)."""
  run = _scored_run(
      extra_result={
          "eval_id": "noisy-1",
          "prompt": "Completed with a traceback on stderr",
          "stdout": json.dumps({"response": "done"}),
          "stderr": "Traceback (most recent call last): boom",
          "returncode": 0,
          "run_time": _RUN_TIME,
      },
      scores=(
          {"eval_id": "ok-1", "comparator": "goal_completion", "score": 1},
          {"eval_id": "crash-1", "comparator": "goal_completion", "score": 0},
          {"eval_id": "noisy-1", "comparator": "goal_completion", "score": 1},
      ),
  )
  rows = [
      row
      for row in run.to_agent_event_rows(import_version="v1")
      if row["session_id"].endswith(":noisy-1")
  ]
  assert [row["event_type"] for row in rows] == [
      "USER_MESSAGE_RECEIVED",
      "AGENT_COMPLETED",
  ]
  completed = rows[-1]
  assert completed["status"] == "ERROR"
  assert completed["error_message"] == (
      "stderr: Traceback (most recent call last): boom"
  )
  assert "returncode" not in completed["error_message"]
  assert completed["attributes"]["evalbench_error_fields"] == {
      "stderr": "Traceback (most recent call last): boom"
  }
  assert any(row["status"] == "ERROR" for row in rows)

  # Python reference: a process failure despite a passing score.
  verdicts = _verdicts(run, EvalScorePolicy({"goal_completion": 0.5}))
  noisy = verdicts["evalbench:job-123:v1:noisy-1"]
  assert noisy.process_failed is True
  assert noisy.missing_completion is False
  assert noisy.score_failed is False
  assert noisy.failed is True
  assert verdicts["evalbench:job-123:v1:ok-1"].failed is False

  # SQL path: process failure is any ERROR event in the session, never the
  # exit code, so the published ERROR row above fails the denominator.
  sql = failed_sessions_sql(
      target_project="p",
      target_dataset="d",
      policy=EvalScorePolicy({"goal_completion": 0.5}),
  )
  assert "LOGICAL_OR(status = 'ERROR') AS process_failed" in sql
  assert "s.process_failed\n    OR s.missing_completion" in sql
  assert "returncode" not in sql


def test_classify_sessions_and_sql_collapse_duplicate_comparator_rows() -> None:
  run = _scored_run(
      scores=(
          {"eval_id": "ok-1", "comparator": "goal_completion", "score": 0.9},
          {"eval_id": "ok-1", "comparator": "goal_completion", "score": 0.1},
          {"eval_id": "ok-1", "comparator": "goal_completion", "score": 0.3},
      )
  )
  verdicts = _verdicts(run, EvalScorePolicy({"goal_completion": 0.5}))
  # One entry per comparator carrying the lowest failing score.
  assert verdicts["evalbench:job-123:v1:ok-1"].failing_scores == {
      "goal_completion": 0.1
  }

  sql = failed_sessions_sql(
      target_project="p",
      target_dataset="d",
      policy=EvalScorePolicy({"goal_completion": 0.5}),
  )
  assert "GROUP BY s.session_id, p.comparator" in sql
  assert (
      "MIN(IF(sc.score < p.min_score, sc.score, NULL)) AS failing_score" in sql
  )
  assert "COUNTIF(failing) AS failing_score_count" in sql
  assert "STRUCT(comparator AS comparator, failing_score AS score)" in sql


def test_classify_sessions_without_policy_only_uses_process_signals() -> None:
  verdicts = _verdicts(_scored_run(), EvalScorePolicy())
  assert verdicts["evalbench:job-123:v1:ok-1"].failed is False
  assert verdicts["evalbench:job-123:v1:crash-1"].failed is True


def test_failed_sessions_sql_pins_import_version_and_renders_policy() -> None:
  sql = failed_sessions_sql(
      target_project="analytics-project",
      target_dataset="bqaa",
      policy=EvalScorePolicy({"goal_completion": 0.5, "sql_correctness": 1.0}),
  )

  assert "`analytics-project.bqaa.evalbench_agent_events`" in sql
  assert "`analytics-project.bqaa.evalbench_scores_imported`" in sql
  assert sql.count("import_version = @import_version") >= 2
  assert sql.count("job_id = @job_id") >= 2
  assert "STRUCT('goal_completion' AS comparator, 0.5 AS min_score)" in sql
  assert "STRUCT('sql_correctness' AS comparator, 1.0 AS min_score)" in sql
  assert "status = 'ERROR'" in sql
  assert "event_type = 'AGENT_COMPLETED'" in sql
  # Completion is never inferred from the process exit code.
  assert "returncode" not in sql


def test_failed_sessions_sql_rejects_unsafe_comparator_names() -> None:
  with pytest.raises(ValueError, match="comparator"):
    failed_sessions_sql(
        target_project="p",
        target_dataset="d",
        policy=EvalScorePolicy({"x' OR 1=1 --": 0.5}),
    )


def test_failed_sessions_sql_without_policy_still_counts_process_failures() -> (
    None
):
  sql = failed_sessions_sql(target_project="p", target_dataset="d")
  assert "status = 'ERROR'" in sql
  assert "min_score" not in sql
