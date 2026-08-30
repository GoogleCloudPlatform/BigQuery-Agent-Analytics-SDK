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


def test_evalbench_run_keeps_v051_positional_argument_order() -> None:
  """``snapshot_at`` must not shift the v0.5.1 positional fields.

  v0.5.1 callers construct ``EvalBenchRun(project, dataset, job, location,
  results, scores, config_rows)``; inserting ``snapshot_at`` ahead of
  ``results`` would silently swallow ``results`` into ``snapshot_at``.
  """
  results = ({"eval_id": "e-1", "scenario_id": "s-1"},)
  scores = ({"eval_id": "e-1", "comparator": "goal_completion", "score": 1},)
  config_rows = (
      {"config": "experiment_config.orchestrator", "value": "geminicli"},
  )

  run = EvalBenchRun(
      "source-project",
      "evalbench",
      "job-123",
      "US",
      results,
      scores,
      config_rows,
  )

  assert run.project_id == "source-project"
  assert run.evalbench_dataset == "evalbench"
  assert run.job_id == "job-123"
  assert run.location == "US"
  assert run.results == results
  assert run.scores == scores
  assert run.config_rows == config_rows
  assert run.snapshot_at is None
  assert not isinstance(run.snapshot_at, tuple)

  snapshot_at = datetime(2026, 8, 29, tzinfo=timezone.utc)
  pinned = EvalBenchRun(
      "source-project",
      "evalbench",
      "job-123",
      "US",
      results,
      scores,
      config_rows,
      snapshot_at,
  )
  assert pinned.results == results
  assert pinned.snapshot_at == snapshot_at


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


class _FakeSnapshot:
  """What one BigQuery transaction sees: the committed state at its start."""

  def __init__(self, rows: list[dict], lock_rows: int, lock_claims: int):
    self.rows = rows
    self.lock_rows = lock_rows
    self.lock_claims = lock_claims


class _FakeManifestStore:
  """Committed state of one target dataset shared by several fake clients.

  ``rows`` is the manifest registry; ``events``/``scores`` are the published
  mirror tables; ``lock_rows`` counts sentinel rows in
  ``evalbench_import_lock`` and ``lock_claims`` is the sentinel's
  ``claim_count`` (every committed publish mutates it).
  """

  def __init__(self, rows: list[dict] | None = None) -> None:
    self.rows: list[dict] = list(rows or [])
    self.events: list[dict] = []
    self.scores: list[dict] = []
    self.lock_rows = 0
    self.lock_claims = 0

  def snapshot(self) -> _FakeSnapshot:
    return _FakeSnapshot(
        rows=[dict(row) for row in self.rows],
        lock_rows=self.lock_rows,
        lock_claims=self.lock_claims,
    )


_RAISE_MESSAGE = re.compile(r"RAISE USING MESSAGE = '([^']*)'")
# The RAISE message of one ``IF <guard> > 0 THEN`` block of the publish script.
_GUARD_MESSAGE = r"IF {guard} > 0 THEN\s*RAISE USING MESSAGE = '([^']*)'"
_CONCURRENT_UPDATE_ERROR = (
    "400 Transaction is aborted due to concurrent update against table"
    " {lock_table}. Transaction ID: fake"
)


def _same_version(row: dict, params: dict) -> bool:
  return (
      row["job_id"] == params["job_id"]
      and row["import_version"] == params["import_version"]
  )


class _FakeWriteClient:
  """Records loads, queries, and table DDL issued by ``materialize``.

  The publish transaction is emulated against a ``_FakeManifestStore`` with
  BigQuery's snapshot-isolation rules
  (https://cloud.google.com/bigquery/docs/transactions#transaction_concurrency):

  * every read inside the transaction (the manifest guard) sees the
    ``_FakeSnapshot`` taken when the transaction began, never later commits;
  * appends never conflict, and a keyed ``DELETE`` that matches nothing
    mutates nothing, so neither can fail a concurrent transaction;
  * the lock claim ``UPDATE`` mutates the pre-existing sentinel row. If a
    concurrent transaction committed a mutation of that row after this
    transaction's snapshot (``lock_claims`` moved), BigQuery cancels this
    transaction with the "concurrent update" error, which is what the fake
    raises. Only one of two concurrent publishes can therefore commit.

  ``stale_manifest_reads`` makes every manifest read *before* this client's
  first publish transaction return nothing (a stale pre-read); reads after
  the transaction see committed state. ``transaction_snapshot`` pins the
  snapshot the transaction itself starts from (default: the committed state
  when ``BEGIN TRANSACTION`` runs). Passing one snapshot to two clients
  models two transactions that both began before either committed.
  """

  def __init__(
      self,
      *,
      manifest_rows: list[dict] | None = None,
      store: _FakeManifestStore | None = None,
      stale_manifest_reads: bool = False,
      transaction_snapshot: _FakeSnapshot | None = None,
      load_error: Exception | None = None,
      transaction_error: Exception | None = None,
      delete_error: Exception | None = None,
  ) -> None:
    self.store = store or _FakeManifestStore(manifest_rows)
    self.stale_manifest_reads = stale_manifest_reads
    self.transaction_attempted = False
    self.transaction_snapshot = transaction_snapshot
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
      self.transaction_attempted = True
      if self.transaction_error is not None:
        return _FakeJob(error=self.transaction_error)
      snapshot = self.transaction_snapshot or self.store.snapshot()
      return self._publish(query, params, snapshot)
    if ".evalbench_import_lock`" in query and query.startswith("INSERT INTO"):
      # Seeding is INSERT-only, so it never conflicts and may run twice.
      assert params == {}
      if self.store.lock_rows == 0:
        self.store.lock_rows = 1
      return _FakeJob()
    if ".evalbench_import_manifest`" in query:
      if self.stale_manifest_reads and not self.transaction_attempted:
        return _FakeJob([])
      return _FakeJob(
          [row for row in self.store.rows if _same_version(row, params)]
      )
    raise AssertionError(f"unexpected query: {query}")

  def _publish(
      self, script: str, params: dict, snapshot: _FakeSnapshot
  ) -> _FakeJob:
    # 1. Lock claim: the script's first statement after BEGIN TRANSACTION.
    lock_table = re.search(r"UPDATE `([^`]+)`", script).group(1)
    assert lock_table.endswith(".evalbench_import_lock")
    assert script.index("UPDATE `") < script.index(
        "conflicting_manifest_rows ="
    )
    if snapshot.lock_rows == 0:
      # ``IF @@row_count = 0 THEN RAISE``: nothing was mutated.
      message = _RAISE_MESSAGE.findall(script)[0]
      return _FakeJob(error=RuntimeError(f"400 {message}"))
    if snapshot.lock_claims != self.store.lock_claims:
      # Another transaction mutated the sentinel after this snapshot.
      return _FakeJob(
          error=RuntimeError(
              _CONCURRENT_UPDATE_ERROR.format(lock_table=lock_table)
          )
      )

    # 2. Manifest guard, evaluated against the transaction's snapshot:
    #    absent -> publish, conflicting -> RAISE, identical -> RAISE the
    #    "unchanged" message (without replace) so nothing is rewritten.
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
    if "source_project != @source_project" in script:
      predicates.append(
          lambda row: row["source_project"] != params["source_project"]
          or row["source_dataset"] != params["source_dataset"]
      )
    if "events_table != @events_table" in script:
      predicates.append(
          lambda row: row["events_table"] != params["events_table"]
          or row["scores_table"] != params["scores_table"]
      )
    existing = [row for row in snapshot.rows if _same_version(row, params)]
    conflicts = [
        row for row in existing if any(pred(row) for pred in predicates)
    ]
    if conflicts:
      message = _GUARD_MESSAGE.format(guard="conflicting_manifest_rows")
      return _FakeJob(
          error=RuntimeError(f"400 {re.search(message, script).group(1)}")
      )
    identical_guard = re.search(
        _GUARD_MESSAGE.format(guard="existing_manifest_rows"), script
    )
    if existing and identical_guard is not None:
      return _FakeJob(error=RuntimeError(f"400 {identical_guard.group(1)}"))

    # 3. Commit: keyed DELETEs then INSERTs from staging, plus the claim.
    def staged(table: str) -> list[dict]:
      return next(rows for dest, rows, _ in self.loads if table in dest)

    store = self.store
    store.lock_claims += 1
    store.events = [
        row for row in store.events if not _same_version(row, params)
    ] + staged(params["events_table"] + "_staging_")
    store.scores = [
        row for row in store.scores if not _same_version(row, params)
    ] + staged(params["scores_table"] + "_staging_")
    store.rows = [
        row for row in store.rows if not _same_version(row, params)
    ] + staged("evalbench_import_manifest")
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


def _lock_seed_queries(fake: _FakeWriteClient) -> list[str]:
  return [
      sql
      for sql, _ in fake.queries
      if sql.startswith("INSERT INTO") and ".evalbench_import_lock`" in sql
  ]


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

  # The fixed lock table is created alongside the three mirror tables.
  assert "analytics-project.bqaa.evalbench_import_lock" in fake.created

  # Exactly one transaction publishes all three tables; no delete-then-append.
  transactions = _transaction_queries(fake)
  assert len(transactions) == 1
  script = transactions[0]
  assert script.count("DELETE FROM") == 3
  assert script.count("INSERT INTO") == 3
  assert script.index("BEGIN TRANSACTION") < script.index("DELETE FROM")
  # The lock sentinel is seeded by its own committed job before the
  # transaction begins, so the claim UPDATE has a row to mutate.
  seeds = _lock_seed_queries(fake)
  assert len(seeds) == 1
  order = [sql for sql, _ in fake.queries]
  assert order.index(seeds[0]) < order.index(script)
  assert "WHERE NOT EXISTS" in seeds[0]
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
      f"evalbench-import:job-123:{version}:ok-1",
      f"evalbench-import:job-123:{version}:crash-1",
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

  # The claim UPDATE of the pre-existing sentinel is the first statement of
  # the transaction: it is the only statement guaranteed to mutate a row, so
  # it is what BigQuery serializes concurrent publishes on.
  claim = script.index("UPDATE `source-project.bqaa.evalbench_import_lock`")
  guard = script.index("conflicting_manifest_rows = (")
  assert script.index("BEGIN TRANSACTION") < claim < guard
  assert guard < script.index("DELETE FROM")
  assert "SET claim_count = claim_count + 1" in script
  assert f"WHERE lock_id = '{evalbench._IMPORT_LOCK_ID}'" in script
  assert script.index("IF @@row_count = 0 THEN") < guard
  assert evalbench._LOCK_MISSING_MESSAGE in script
  assert "RAISE USING MESSAGE" in script
  assert script.index("RAISE USING MESSAGE") < script.index("DELETE FROM")
  assert "results_fingerprint != @results_fingerprint" in script
  assert "scores_fingerprint != @scores_fingerprint" in script
  assert "configs_fingerprint != @configs_fingerprint" in script
  assert "events_table != @events_table" in script
  assert "scores_table != @scores_table" in script
  assert "source_project != @source_project" in script
  assert "source_dataset != @source_dataset" in script
  # Absent / identical / conflicting: an identical manifest row is left in
  # place (RAISE -> ROLLBACK) rather than deleted and re-inserted.
  assert evalbench._PUBLISH_CONFLICT_MESSAGE in script
  assert evalbench._PUBLISH_UNCHANGED_MESSAGE in script
  assert (
      script.index("IF conflicting_manifest_rows > 0 THEN")
      < script.index("IF existing_manifest_rows > 0 THEN")
      < script.index("DELETE FROM")
  )
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
      "source_project": "source-project",
      "source_dataset": "evalbench",
  }


def test_publish_script_replace_skips_fingerprint_guard_only() -> None:
  fake = _FakeWriteClient()
  _scored_run().materialize(
      target_dataset="bqaa", import_version="v1", replace=True, bq_client=fake
  )
  script = _transaction_queries(fake)[0]
  assert "results_fingerprint != @results_fingerprint" not in script
  assert "source_project != @source_project" not in script
  assert "IF existing_manifest_rows > 0 THEN" not in script
  assert evalbench._PUBLISH_UNCHANGED_MESSAGE not in script
  assert "events_table != @events_table" in script


def test_materialize_stale_pre_read_is_caught_by_transaction_guard() -> None:
  """Importer B's pre-read is stale but its transaction starts after A
  committed, so the in-transaction manifest guard refuses it."""
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


def _published_rows(fake: _FakeWriteClient, table: str) -> list[dict]:
  return next(rows for dest, rows, _ in fake.loads if table in dest)


def test_materialize_stale_pre_read_identical_publish_is_unchanged() -> None:
  """Stale pre-read, identical content *and* provenance: the transaction
  distinguishes an identical manifest from an absent one, preserves A's
  rows and manifest, and reports ``unchanged`` instead of re-publishing."""
  store = _FakeManifestStore()
  importer_a = _FakeWriteClient(store=store, stale_manifest_reads=True)
  first = _scored_run().materialize(
      target_dataset="bqaa",
      import_version="v1",
      imported_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
      bq_client=importer_a,
  )
  assert first.status == "imported"
  assert store.lock_claims == 1
  published_events = _published_rows(importer_a, "evalbench_agent_events")

  importer_b = _FakeWriteClient(store=store, stale_manifest_reads=True)
  same = _scored_run().materialize(
      target_dataset="bqaa",
      import_version="v1",
      imported_at=datetime(2026, 5, 2, 8, 0, tzinfo=timezone.utc),
      bq_client=importer_b,
  )
  assert same.status == "unchanged"
  assert same.manifest == first.manifest
  assert same.event_row_count == first.event_row_count
  assert same.score_row_count == first.score_row_count
  # B reached the transaction (its pre-read saw nothing) but was rolled
  # back: A's manifest (with A's imported_at), rows, and lock claim survive.
  assert len(_transaction_queries(importer_b)) == 1
  assert store.rows == [first.manifest]
  assert store.events == published_events
  assert store.lock_claims == 1
  staged = {destination for destination, _, _ in importer_b.loads}
  assert set(importer_b.deleted) == staged


@pytest.mark.parametrize(
    "field, other_value",
    [("project_id", "other-project"), ("evalbench_dataset", "evalbench_copy")],
)
def test_materialize_stale_pre_read_provenance_drift_is_a_conflict(
    field: str, other_value: str
) -> None:
  """Codex P1 on a8e8b5c: equal source rows read from another source
  project/dataset produce equal fingerprints, but ``source_project`` /
  ``source_dataset`` change the manifest and the published
  ``attributes.evalbench_source_*`` values. A stale-pre-read importer whose
  transaction starts after the first commit must treat that as a
  conflicting version, not as absent, and must not rewrite it without
  ``replace=True``."""
  store = _FakeManifestStore()
  importer_a = _FakeWriteClient(store=store, stale_manifest_reads=True)
  first = _scored_run().materialize(
      target_dataset="bqaa", import_version="v1", bq_client=importer_a
  )
  assert first.status == "imported"
  published_events = _published_rows(importer_a, "evalbench_agent_events")

  other_source = dataclasses.replace(_scored_run(), **{field: other_value})
  assert other_source.fingerprints() == _scored_run().fingerprints()
  # Same destination tables as A, so only provenance differs.
  target = {"target_project": "source-project", "target_dataset": "bqaa"}

  importer_b = _FakeWriteClient(store=store, stale_manifest_reads=True)
  with pytest.raises(ValueError, match="published concurrently"):
    other_source.materialize(
        **target, import_version="v1", bq_client=importer_b
    )
  assert len(_transaction_queries(importer_b)) == 1
  assert store.rows == [first.manifest]
  assert store.events == published_events
  assert store.lock_claims == 1
  assert all(
      row["attributes"]["evalbench_source_project"] == "source-project"
      and row["attributes"]["evalbench_source_dataset"] == "evalbench"
      for row in store.events
  )
  staged = {destination for destination, _, _ in importer_b.loads}
  assert set(importer_b.deleted) == staged

  # A fresh pre-read reports the same drift before staging anything.
  fresh = _FakeWriteClient(store=store)
  with pytest.raises(ValueError, match="already exists with different") as info:
    other_source.materialize(**target, import_version="v1", bq_client=fresh)
  assert other_value in str(info.value)
  assert "new import_version" in str(info.value)
  assert fresh.loads == []
  assert _transaction_queries(fresh) == []
  assert store.rows == [first.manifest]

  # replace=True is the explicit override: the version is re-published from
  # the new source and the manifest records the new provenance.
  importer_c = _FakeWriteClient(store=store)
  replaced = other_source.materialize(
      **target, import_version="v1", replace=True, bq_client=importer_c
  )
  assert replaced.status == "replaced"
  assert store.rows == [replaced.manifest]
  manifest_key = "source_project" if field == "project_id" else "source_dataset"
  assert replaced.manifest[manifest_key] == other_value
  attribute_key = "evalbench_" + manifest_key
  assert all(
      row["attributes"][attribute_key] == other_value for row in store.events
  )
  assert store.lock_claims == 2


def test_materialize_concurrent_first_imports_serialize_on_lock_sentinel() -> (
    None
):
  """Codex P1: two *truly concurrent* first imports of one version.

  Both transactions begin from the same snapshot, before either commits, so
  under BigQuery snapshot isolation both guards see no manifest row and both
  keyed DELETEs match nothing; INSERTs never conflict. Without a real claim
  both would commit and the version would hold two manifests and a mixed
  corpus. The claim UPDATE of the pre-existing sentinel is a row mutation,
  so BigQuery cancels the second transaction that performs it
  (transactions#transaction_concurrency: "conflicting transactions are
  cancelled"). The fake models exactly that rule; it is not a live BigQuery
  test.
  """
  store = _FakeManifestStore()
  store.lock_rows = 1  # sentinel seeded by an earlier import into the dataset
  shared_snapshot = store.snapshot()  # both transactions begin here
  importer_a = _FakeWriteClient(
      store=store,
      stale_manifest_reads=True,
      transaction_snapshot=shared_snapshot,
  )
  importer_b = _FakeWriteClient(
      store=store,
      stale_manifest_reads=True,
      transaction_snapshot=shared_snapshot,
  )
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
  assert store.lock_claims == 1
  assert store.rows == [first.manifest]

  # B's snapshot predates A's commit: its guard sees nothing, but its claim
  # UPDATE conflicts with A's committed mutation of the sentinel, so BigQuery
  # cancels B. Nothing B staged reaches the published tables.
  with pytest.raises(ValueError, match="claimed the import lock") as info:
    run_b.materialize(
        target_dataset="bqaa", import_version="v1", bq_client=importer_b
    )
  assert "concurrent update" in str(info.value.__cause__)
  assert len(_transaction_queries(importer_b)) == 1
  assert store.lock_claims == 1
  assert store.rows == [first.manifest]
  assert store.events == _published_rows(importer_a, "evalbench_agent_events")
  assert store.scores == _published_rows(
      importer_a, "evalbench_scores_imported"
  )
  # No mixed corpus: B's extra scenario never landed.
  assert all(
      row["attributes"]["evalbench_scenario_id"] != "late-1"
      for row in store.events
  )
  staged = {destination for destination, _, _ in importer_b.loads}
  assert set(importer_b.deleted) == staged

  # Re-running B against the committed state reports the real conflict
  # before staging anything; identical content would report "unchanged".
  retry = _FakeWriteClient(store=store)
  with pytest.raises(ValueError, match="different source fingerprints"):
    run_b.materialize(
        target_dataset="bqaa", import_version="v1", bq_client=retry
    )
  assert retry.loads == []
  same = run_a.materialize(
      target_dataset="bqaa",
      import_version="v1",
      bq_client=_FakeWriteClient(store=store),
  )
  assert same.status == "unchanged"
  assert store.lock_claims == 1


def test_materialize_lock_claim_requires_seeded_sentinel() -> None:
  """If the sentinel is not in the transaction snapshot the claim mutates
  nothing, and the ``@@row_count`` guard aborts rather than publishing
  without serialization."""
  store = _FakeManifestStore()
  fake = _FakeWriteClient(
      store=store, transaction_snapshot=store.snapshot()  # lock_rows == 0
  )
  with pytest.raises(RuntimeError, match="lock sentinel is missing"):
    _scored_run().materialize(
        target_dataset="bqaa", import_version="v1", bq_client=fake
    )
  assert store.rows == []
  assert store.events == []
  staged = {destination for destination, _, _ in fake.loads}
  assert set(fake.deleted) == staged


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
      "evalbench-import:job-123:v1:ok-1",
      "evalbench-import:job-123:v1:crash-1",
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
  pre_reads = [
      sql
      for sql, _ in fake.queries
      if "BEGIN TRANSACTION" not in sql and sql not in _lock_seed_queries(fake)
  ]
  assert pre_reads and all(f"`{registry}`" in sql for sql in pre_reads)
  # The lock is the dataset's fixed lock table as well, never a custom one.
  assert all(
      "`analytics-project.bqaa.evalbench_import_lock`" in sql
      for sql in _lock_seed_queries(fake)
  )
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
  would both read ``evalbench-import:job-123:release:1:case``."""

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
  assert left == "evalbench-import:job-123:release\\:1:case"
  assert right == "evalbench-import:job-123:release:1\\:case"
  # Backslashes in a published component are escaped too, so escaping is
  # reversible.
  assert (
      evalbench._session_identity("job-123", "a\\:b", import_version="v1")
      == "evalbench-import:job-123:v1:a\\\\\\:b"
  )
  # Common case (no delimiters) keeps the documented readable form.
  assert (
      evalbench._session_identity("job-123", "ok-1", import_version="v1")
      == "evalbench-import:job-123:v1:ok-1"
  )


def test_published_identities_never_alias_plain_reads() -> None:
  """The two families live in disjoint namespaces: every plain identity
  starts with ``evalbench:`` and every published one with
  ``evalbench-import:``, whatever the (unescaped) plain components contain."""
  plain = evalbench._session_identity("job-123", "v1:case", import_version=None)
  assert plain == "evalbench:job-123:v1:case"  # v0.5.1 form, unescaped
  assert plain != evalbench._session_identity(
      "job-123", "case", import_version="v1"
  )
  # Even a scenario id that spells out the published namespace cannot alias
  # it, because the plain prefix is fixed and differs.
  contrived = evalbench._session_identity(
      "job-123", "import:job-123:v1:case", import_version=None
  )
  assert contrived.startswith("evalbench:")
  assert not contrived.startswith("evalbench-import:")


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
  assert scores_a == {"evalbench-import:job-123:release\\:1:case"}
  assert scores_a <= events_a


def test_published_stable_id_is_not_ambiguous_across_part_boundaries() -> None:
  published = evalbench._published_stable_id
  assert published("a\x1fb", "c", length=16) != published(
      "a", "b\x1fc", length=16
  )
  assert published("ab", "c", length=16) != published("a", "bc", length=16)
  # Distinct framing from the legacy hash, so a published span id can never
  # reproduce a v0.5.1 one for the same parts.
  assert published("x", "user", length=16) != evalbench._stable_id(
      "x", "user", length=16
  )


# Golden vectors produced by the v0.5.1 module (commit 3fb6a00,
# ``to_agent_event_rows()`` without ``import_version``) for a single-tool
# scenario. They pin the public unversioned identity contract: session/trace
# ids are the unescaped ``evalbench:{job_id}:{scenario_id}`` and the
# invocation/span ids come from the legacy ``_stable_id`` hash.
_V051_IDENTITY_VECTORS = {
    "case": {
        "session_id": "evalbench:job-123:case",
        "invocation_id": "f567e6f5a86a69f608edb2751cc3771c",
        "root_span_id": "fe515c0a118b87b6",
        "tool_span_id": "4bd7d835aaebbb51",
        "completed_span_id": "0628e23feb7a53c8",
    },
    # A scenario id containing the delimiter stays verbatim (no escaping).
    "v1:case": {
        "session_id": "evalbench:job-123:v1:case",
        "invocation_id": "1916bbd16e0d961e89622a612952e96a",
        "root_span_id": "9bde893b313678b5",
        "tool_span_id": "6ab0da7e7d3e8bd4",
        "completed_span_id": "bd312c799fb82468",
    },
    "refund-1": {
        "session_id": "evalbench:job-123:refund-1",
        "invocation_id": "c89ad7e24957d27a83c903f2f1e2a372",
        "root_span_id": "a8bcda92a6445014",
        "tool_span_id": "0374231379db3981",
        "completed_span_id": "7dac2cffcd509c0b",
    },
}


def _golden_run(scenario_id: str) -> EvalBenchRun:
  return _run(
      {
          "eval_id": scenario_id,
          "prompt": "Prompt",
          "stdout": json.dumps(
              {
                  "response": "done",
                  "tool_calls": [
                      {"tool_name": "search", "args": {"q": "x"}, "result": "r"}
                  ],
              }
          ),
          "returncode": 0,
          "run_time": _RUN_TIME,
      }
  )


@pytest.mark.parametrize("scenario_id", sorted(_V051_IDENTITY_VECTORS))
def test_unversioned_identities_match_v051_golden_vectors(
    scenario_id: str,
) -> None:
  expected = _V051_IDENTITY_VECTORS[scenario_id]
  rows = _golden_run(scenario_id).to_agent_event_rows()
  by_type = {row["event_type"]: row for row in rows}
  assert set(by_type) == {
      "USER_MESSAGE_RECEIVED",
      "TOOL_STARTING",
      "TOOL_COMPLETED",
      "AGENT_COMPLETED",
  }
  assert {row["session_id"] for row in rows} == {expected["session_id"]}
  assert {row["trace_id"] for row in rows} == {expected["session_id"]}
  assert {row["invocation_id"] for row in rows} == {expected["invocation_id"]}
  user = by_type["USER_MESSAGE_RECEIVED"]
  assert (user["span_id"], user["parent_span_id"]) == (
      expected["root_span_id"],
      None,
  )
  for event_type in ("TOOL_STARTING", "TOOL_COMPLETED"):
    assert by_type[event_type]["span_id"] == expected["tool_span_id"]
    assert by_type[event_type]["parent_span_id"] == expected["root_span_id"]
  completed = by_type["AGENT_COMPLETED"]
  assert completed["span_id"] == expected["completed_span_id"]
  assert completed["parent_span_id"] == expected["root_span_id"]


def test_legacy_stable_id_hash_is_frozen() -> None:
  assert (
      evalbench._stable_id("evalbench:job-123:case", "invocation", length=32)
      == _V051_IDENTITY_VECTORS["case"]["invocation_id"]
  )
  assert (
      evalbench._stable_id("evalbench:job-123:case", "user", length=16)
      == _V051_IDENTITY_VECTORS["case"]["root_span_id"]
  )


def test_published_identities_differ_from_unversioned_ones() -> None:
  """Publishing a run under a version never reuses a plain-read id, so the
  two families can coexist in one table without sharing traces or spans."""
  run = _golden_run("case")
  plain = run.to_agent_event_rows()
  published = run.to_agent_event_rows(import_version="v1")
  for column in ("session_id", "trace_id", "invocation_id", "span_id"):
    assert {row[column] for row in plain}.isdisjoint(
        row[column] for row in published
    ), column
  assert all(
      row["session_id"].startswith("evalbench-import:") for row in published
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
      "evalbench-import:job-123:v1:crash-1",
      "evalbench-import:job-123:v1:ok-1",
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
      "evalbench-import:job-123:v1:ok-1",
      "evalbench-import:job-123:v1:crash-1",
      "evalbench-import:job-123:v1:wrong-1",
  }
  passed = verdicts["evalbench-import:job-123:v1:ok-1"]
  assert passed.failed is False
  assert passed.process_failed is False
  assert passed.score_failed is False

  crashed = verdicts["evalbench-import:job-123:v1:crash-1"]
  assert crashed.failed is True
  assert crashed.process_failed is True
  assert crashed.missing_completion is True

  wrong = verdicts["evalbench-import:job-123:v1:wrong-1"]
  assert wrong.failed is True
  assert wrong.process_failed is False
  assert wrong.missing_completion is False
  assert wrong.score_failed is True
  assert wrong.failing_scores == {"goal_completion": 0.2}

  failed = sorted(v.session_id for v in verdicts.values() if v.failed)
  assert failed == [
      "evalbench-import:job-123:v1:crash-1",
      "evalbench-import:job-123:v1:wrong-1",
  ]


def test_classify_sessions_returncode_zero_without_scores_is_not_a_pass() -> (
    None
):
  run = _scored_run(scores=())
  verdicts = _verdicts(run, EvalScorePolicy({"goal_completion": 0.5}))
  completed = verdicts["evalbench-import:job-123:v1:ok-1"]
  assert completed.process_failed is False
  assert completed.score_failed is True
  assert completed.failed is True
  assert completed.failing_scores == {"goal_completion": None}

  lenient = _verdicts(
      run, EvalScorePolicy({"goal_completion": 0.5}, missing_score_fails=False)
  )
  assert lenient["evalbench-import:job-123:v1:ok-1"].failed is False


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
  noisy = verdicts["evalbench-import:job-123:v1:noisy-1"]
  assert noisy.process_failed is True
  assert noisy.missing_completion is False
  assert noisy.score_failed is False
  assert noisy.failed is True
  assert verdicts["evalbench-import:job-123:v1:ok-1"].failed is False

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
  assert verdicts["evalbench-import:job-123:v1:ok-1"].failing_scores == {
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
  assert verdicts["evalbench-import:job-123:v1:ok-1"].failed is False
  assert verdicts["evalbench-import:job-123:v1:crash-1"].failed is True


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
