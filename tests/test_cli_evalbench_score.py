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
"""Tests for ``bq-agent-sdk evalbench-score`` (#435 slice 3, #97)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import cli
from bigquery_agent_analytics import evalbench
from bigquery_agent_analytics.cli import app
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import LLMAsJudge
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.trace import TraceFilter

runner = CliRunner()

_NOW = datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc)
_SESSION_IDS = (
    "evalbench-import:job-123:v2:scenario-a",
    "evalbench-import:job-123:v2:scenario-b",
)
_PINNED = evalbench.EvalBenchImportSessions(
    job_id="job-123",
    import_version="v2",
    events_table="analytics-project.bqaa.evalbench_agent_events",
    scores_table="analytics-project.bqaa.evalbench_scores_imported",
    session_ids=_SESSION_IDS,
    manifest={"job_id": "job-123", "import_version": "v2"},
)
_BASE_ARGS = [
    "evalbench-score",
    "--project-id",
    "analytics-project",
    "--dataset-id",
    "bqaa",
    "--job-id",
    "job-123",
]


def _report(passed: int, total: int) -> EvaluationReport:
  return EvaluationReport(
      dataset="test",
      evaluator_name="correctness_judge",
      total_sessions=total,
      passed_sessions=passed,
      failed_sessions=total - passed,
      created_at=_NOW,
      session_scores=[
          SessionScore(
              session_id=_SESSION_IDS[i % len(_SESSION_IDS)],
              scores={"correctness": 0.9 if i < passed else 0.2},
              passed=i < passed,
              llm_feedback=None if i < passed else "Wrong table joined.",
          )
          for i in range(total)
      ],
  )


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: EvaluationReport | None = None,
    pinned: evalbench.EvalBenchImportSessions = _PINNED,
    pin_error: Exception | None = None,
    evaluate_error: Exception | None = None,
) -> tuple[MagicMock, list[dict], list[dict]]:
  """Stub the client factory and the manifest/session resolver."""
  client = MagicMock()
  if evaluate_error is not None:
    client.evaluate.side_effect = evaluate_error
  else:
    client.evaluate.return_value = report or _report(2, 2)
  build_calls: list[dict] = []

  def fake_build_client(project_id, dataset_id, table_id, location=None, **kw):
    build_calls.append(
        {
            "project_id": project_id,
            "dataset_id": dataset_id,
            "table_id": table_id,
            "location": location,
            **kw,
        }
    )
    return client

  pin_calls: list[dict] = []

  def fake_import_sessions(**kwargs):
    pin_calls.append(kwargs)
    if pin_error is not None:
      raise pin_error
    return pinned

  monkeypatch.setattr(cli, "_build_client", fake_build_client)
  monkeypatch.setattr(evalbench, "import_sessions", fake_import_sessions)
  return client, build_calls, pin_calls


def test_scores_latest_version_with_correctness_judge_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  client, build_calls, pin_calls = _patch(monkeypatch)

  result = runner.invoke(app, _BASE_ARGS)

  assert result.exit_code == 0, result.output
  assert build_calls == [
      {
          "project_id": "analytics-project",
          "dataset_id": "bqaa",
          "table_id": "evalbench_agent_events",
          "location": None,
          "endpoint": None,
          "connection_id": None,
      }
  ]
  assert pin_calls == [
      {
          "target_project": "analytics-project",
          "target_dataset": "bqaa",
          "job_id": "job-123",
          "import_version": None,
          "location": None,
          "bq_client": client.bq_client,
      }
  ]
  client.evaluate.assert_called_once()
  kwargs = client.evaluate.call_args.kwargs
  judge = kwargs["evaluator"]
  assert isinstance(judge, LLMAsJudge)
  assert judge.name == "correctness_judge"
  assert [(c.name, c.threshold) for c in judge._criteria] == [
      ("correctness", 0.5)
  ]
  filters = kwargs["filters"]
  assert isinstance(filters, TraceFilter)
  assert filters.experiment_id == "job-123"
  assert list(filters.session_ids) == list(_SESSION_IDS)
  assert filters.limit == len(_SESSION_IDS)
  assert kwargs["strict"] is False

  payload = json.loads(result.output)
  assert payload["total_sessions"] == 2
  assert payload["details"]["evalbench"] == {
      "job_id": "job-123",
      "import_version": "v2",
      "events_table": "analytics-project.bqaa.evalbench_agent_events",
      "pinned_sessions": 2,
  }


def test_pins_version_and_passes_judge_options_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  client, build_calls, pin_calls = _patch(monkeypatch)

  result = runner.invoke(
      app,
      _BASE_ARGS
      + [
          "--import-version",
          "v2",
          "--evaluator",
          "hallucination",
          "--threshold",
          "0.8",
          "--strict",
          "--location",
          "US",
          "--endpoint",
          "gemini-2.5-pro",
          "--connection-id",
          "proj.us.conn",
          "--format",
          "text",
      ],
  )

  assert result.exit_code == 0, result.output
  (build,) = build_calls
  assert build["location"] == "US"
  assert build["endpoint"] == "gemini-2.5-pro"
  assert build["connection_id"] == "proj.us.conn"
  (pin,) = pin_calls
  assert pin["import_version"] == "v2"
  assert pin["location"] == "US"
  kwargs = client.evaluate.call_args.kwargs
  judge = kwargs["evaluator"]
  assert judge.name == "hallucination_judge"
  assert [c.threshold for c in judge._criteria] == [0.8]
  assert kwargs["strict"] is True
  assert "Evaluation Report: correctness_judge" in result.output


def test_sentiment_judge_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
  client, _, _ = _patch(monkeypatch)

  result = runner.invoke(app, _BASE_ARGS + ["--evaluator", "sentiment"])

  assert result.exit_code == 0, result.output
  judge = client.evaluate.call_args.kwargs["evaluator"]
  assert judge.name == "sentiment_judge"


def test_unknown_evaluator_exits_2_before_any_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  client, build_calls, pin_calls = _patch(monkeypatch)

  result = runner.invoke(app, _BASE_ARGS + ["--evaluator", "latency"])

  assert result.exit_code == 2
  assert "unknown evaluator: 'latency'" in result.output
  assert "correctness|hallucination|sentiment" in result.output
  assert build_calls == []
  assert pin_calls == []
  client.evaluate.assert_not_called()


def test_reserved_agent_events_table_exits_2_before_any_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  client, build_calls, pin_calls = _patch(monkeypatch)

  result = runner.invoke(app, _BASE_ARGS + ["--table-id", "agent_events"])

  assert result.exit_code == 2
  assert "reserved ADK plugin table" in result.output
  assert build_calls == []
  assert pin_calls == []
  client.evaluate.assert_not_called()


def test_table_not_bound_to_version_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  client, _, _ = _patch(monkeypatch)

  result = runner.invoke(app, _BASE_ARGS + ["--table-id", "other_events"])

  assert result.exit_code == 2
  assert "analytics-project.bqaa.evalbench_agent_events" in result.output
  assert "analytics-project.bqaa.other_events" in result.output
  client.evaluate.assert_not_called()


def test_unpublished_job_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
  client, _, _ = _patch(
      monkeypatch,
      pin_error=ValueError(
          "EvalBench job 'job-123' has no published import in"
          " 'analytics-project.bqaa.evalbench_import_manifest'"
      ),
  )

  result = runner.invoke(app, _BASE_ARGS)

  assert result.exit_code == 2
  assert "no published import" in result.output
  client.evaluate.assert_not_called()


def test_version_without_sessions_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  empty = evalbench.EvalBenchImportSessions(
      job_id="job-123",
      import_version="v2",
      events_table="analytics-project.bqaa.evalbench_agent_events",
      scores_table="analytics-project.bqaa.evalbench_scores_imported",
      session_ids=(),
  )
  client, _, _ = _patch(monkeypatch, pinned=empty)

  result = runner.invoke(app, _BASE_ARGS)

  assert result.exit_code == 2
  assert "no sessions" in result.output
  client.evaluate.assert_not_called()


def test_bigquery_error_during_evaluate_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  _patch(monkeypatch, evaluate_error=RuntimeError("403 Access Denied"))

  result = runner.invoke(app, _BASE_ARGS)

  assert result.exit_code == 2
  assert "403 Access Denied" in result.output


def test_exit_code_flag_returns_1_and_emits_fail_lines_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  _patch(monkeypatch, report=_report(1, 2))

  result = runner.invoke(app, _BASE_ARGS + ["--exit-code"])

  assert result.exit_code == 1
  assert "--exit-code: 1 session(s) failed (of 2 evaluated)" in result.output
  assert (
      "FAIL session=evalbench-import:job-123:v2:scenario-b"
      " metric=correctness" in result.output
  )
  assert 'feedback="Wrong table joined."' in result.output


def test_exit_code_flag_returns_0_when_all_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  _patch(monkeypatch, report=_report(2, 2))

  result = runner.invoke(app, _BASE_ARGS + ["--exit-code"])

  assert result.exit_code == 0, result.output
  assert "FAIL session=" not in result.output


def test_without_exit_code_flag_failures_still_exit_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  _patch(monkeypatch, report=_report(0, 2))

  result = runner.invoke(app, _BASE_ARGS)

  assert result.exit_code == 0, result.output
  assert json.loads(result.output)["failed_sessions"] == 2


@pytest.mark.parametrize("value", ["-0.1", "1.1", "nan", "inf", "-inf"])
def test_invalid_threshold_exits_2_before_any_client(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
  client, build_calls, pin_calls = _patch(monkeypatch)
  judge_calls: list[float] = []
  with_t, without_t = cli._LLM_JUDGES["correctness"]

  def spy_with_t(t):
    judge_calls.append(t)
    return with_t(t)

  monkeypatch.setitem(cli._LLM_JUDGES, "correctness", (spy_with_t, without_t))

  result = runner.invoke(app, _BASE_ARGS + ["--threshold", value])

  assert result.exit_code == 2, result.output
  assert "Error: --threshold must be a finite value in [0.0, 1.0]" in (
      result.output
  )
  assert judge_calls == []
  assert build_calls == []
  assert pin_calls == []
  client.evaluate.assert_not_called()


def test_invalid_threshold_with_exit_code_still_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  # A negative threshold must not turn ``--exit-code`` into a green gate.
  client, build_calls, _ = _patch(monkeypatch, report=_report(0, 2))

  result = runner.invoke(
      app, _BASE_ARGS + ["--threshold", "-0.1", "--exit-code"]
  )

  assert result.exit_code == 2, result.output
  assert "FAIL session=" not in result.output
  assert build_calls == []
  client.evaluate.assert_not_called()


@pytest.mark.parametrize("value", ["0.0", "1.0"])
def test_boundary_thresholds_are_accepted(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
  client, _, _ = _patch(monkeypatch)

  result = runner.invoke(app, _BASE_ARGS + ["--threshold", value])

  assert result.exit_code == 0, result.output
  judge = client.evaluate.call_args.kwargs["evaluator"]
  assert [c.threshold for c in judge._criteria] == [float(value)]
