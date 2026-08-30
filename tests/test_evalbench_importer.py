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

import pytest

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


class _FakeWriteClient:
  """Records loads, queries, and table DDL issued by ``materialize``."""

  def __init__(
      self,
      *,
      manifest_rows: list[dict] | None = None,
      load_error: Exception | None = None,
      transaction_error: Exception | None = None,
  ) -> None:
    self.manifest_rows = list(manifest_rows or [])
    self.load_error = load_error
    self.transaction_error = transaction_error
    self.loads: list[tuple[str, list[dict], object]] = []
    self.queries: list[tuple[str, dict]] = []
    self.created: list[str] = []
    self.deleted: list[str] = []

  def create_table(self, table, exists_ok: bool = False):
    assert exists_ok is True
    self.created.append(f"{table.project}.{table.dataset_id}.{table.table_id}")
    return table

  def query(self, query: str, **kwargs) -> _FakeJob:
    self.queries.append((query, kwargs))
    if "BEGIN TRANSACTION" in query:
      return _FakeJob(error=self.transaction_error)
    if ".evalbench_import_manifest`" in query:
      return _FakeJob(self.manifest_rows)
    raise AssertionError(f"unexpected query: {query}")

  def load_table_from_json(self, rows, destination, job_config=None):
    self.loads.append((destination, list(rows), job_config))
    return _FakeJob(error=self.load_error)

  def delete_table(self, table_ref: str, not_found_ok: bool = False) -> None:
    assert not_found_ok is True
    self.deleted.append(table_ref)


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
  assert params == {
      "job_id": "job-123",
      "import_version": result.import_version,
  }
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
  assert {row["session_id"] for row in score_rows} == {
      "evalbench:job-123:ok-1",
      "evalbench:job-123:crash-1",
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
          run.to_agent_event_rows(),
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
      "evalbench:job-123:ok-1",
      "evalbench:job-123:crash-1",
      "evalbench:job-123:wrong-1",
  }
  passed = verdicts["evalbench:job-123:ok-1"]
  assert passed.failed is False
  assert passed.process_failed is False
  assert passed.score_failed is False

  crashed = verdicts["evalbench:job-123:crash-1"]
  assert crashed.failed is True
  assert crashed.process_failed is True
  assert crashed.missing_completion is True

  wrong = verdicts["evalbench:job-123:wrong-1"]
  assert wrong.failed is True
  assert wrong.process_failed is False
  assert wrong.missing_completion is False
  assert wrong.score_failed is True
  assert wrong.failing_scores == {"goal_completion": 0.2}

  failed = sorted(v.session_id for v in verdicts.values() if v.failed)
  assert failed == ["evalbench:job-123:crash-1", "evalbench:job-123:wrong-1"]


def test_classify_sessions_returncode_zero_without_scores_is_not_a_pass() -> (
    None
):
  run = _scored_run(scores=())
  verdicts = _verdicts(run, EvalScorePolicy({"goal_completion": 0.5}))
  completed = verdicts["evalbench:job-123:ok-1"]
  assert completed.process_failed is False
  assert completed.score_failed is True
  assert completed.failed is True
  assert completed.failing_scores == {"goal_completion": None}

  lenient = _verdicts(
      run, EvalScorePolicy({"goal_completion": 0.5}, missing_score_fails=False)
  )
  assert lenient["evalbench:job-123:ok-1"].failed is False


def test_classify_sessions_without_policy_only_uses_process_signals() -> None:
  verdicts = _verdicts(_scored_run(), EvalScorePolicy())
  assert verdicts["evalbench:job-123:ok-1"].failed is False
  assert verdicts["evalbench:job-123:crash-1"].failed is True


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
