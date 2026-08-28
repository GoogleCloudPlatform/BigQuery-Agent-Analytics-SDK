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
"""Tests for the EvalBench BigQuery reader and event-row mapper (#97)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json

import pytest

from bigquery_agent_analytics.evalbench import EvalBenchRun

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
