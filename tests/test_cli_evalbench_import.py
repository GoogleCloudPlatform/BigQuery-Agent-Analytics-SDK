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
"""Tests for ``bq-agent-sdk evalbench-import`` (#435)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import evalbench
from bigquery_agent_analytics.cli import app

runner = CliRunner()


class _RecordingRun:

  def __init__(self, calls: list[dict]) -> None:
    self._calls = calls

  def materialize(self, **kwargs) -> evalbench.EvalBenchImportResult:
    self._calls.append({"materialize": kwargs})
    return evalbench.EvalBenchImportResult(
        job_id="job-123",
        import_version=kwargs.get("import_version") or "abc123",
        status="imported",
        events_table="analytics-project.bqaa.evalbench_agent_events",
        scores_table="analytics-project.bqaa.evalbench_scores_imported",
        manifest_table="analytics-project.bqaa.evalbench_import_manifest",
        event_row_count=4,
        score_row_count=2,
        manifest={"job_id": "job-123"},
    )


def _patch_from_bigquery(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
  calls: list[dict] = []

  def fake_from_bigquery(cls, **kwargs):
    calls.append({"from_bigquery": kwargs})
    return _RecordingRun(calls)

  monkeypatch.setattr(
      evalbench.EvalBenchRun,
      "from_bigquery",
      classmethod(fake_from_bigquery),
  )
  return calls


def test_evalbench_import_reads_source_and_materializes_to_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)

  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--location",
          "US",
          "--snapshot-at",
          "2026-05-01T08:00:00Z",
          "--target-project",
          "analytics-project",
          "--target-dataset",
          "bqaa",
          "--import-version",
          "v1",
          "--replace",
      ],
  )

  assert result.exit_code == 0, result.output
  assert calls[0]["from_bigquery"] == {
      "project_id": "source-project",
      "evalbench_dataset": "evalbench",
      "job_id": "job-123",
      "location": "US",
      "snapshot_at": datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
  }
  materialize = calls[1]["materialize"]
  assert materialize["target_project"] == "analytics-project"
  assert materialize["target_dataset"] == "bqaa"
  assert materialize["events_table"] == "evalbench_agent_events"
  assert materialize["scores_table"] == "evalbench_scores_imported"
  assert materialize["import_version"] == "v1"
  assert materialize["replace"] is True
  assert materialize["failed_sessions_view"] == "evalbench_failed_sessions"
  assert materialize["policy"] is None
  payload = json.loads(result.output)
  assert payload["status"] == "imported"
  assert payload["import_version"] == "v1"
  assert payload["event_row_count"] == 4


def test_evalbench_import_defaults_target_to_source_and_derived_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)

  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--target-dataset",
          "bqaa",
      ],
  )

  assert result.exit_code == 0, result.output
  assert calls[0]["from_bigquery"]["snapshot_at"] is None
  materialize = calls[1]["materialize"]
  assert materialize["target_project"] is None
  assert materialize["import_version"] is None
  assert materialize["replace"] is False
  assert materialize["failed_sessions_view"] == "evalbench_failed_sessions"


def test_evalbench_import_view_and_policy_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)

  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--target-dataset",
          "bqaa",
          "--failed-sessions-view",
          "failed_sessions_job_123",
          "--min-score",
          "goal_completion=0.5",
      ],
  )

  assert result.exit_code == 0, result.output
  materialize = calls[1]["materialize"]
  assert materialize["failed_sessions_view"] == "failed_sessions_job_123"
  assert materialize["policy"] == evalbench.EvalScorePolicy(
      {"goal_completion": 0.5}
  )


def test_evalbench_import_can_skip_the_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)
  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--target-dataset",
          "bqaa",
          "--skip-failed-sessions-view",
      ],
  )
  assert result.exit_code == 0, result.output
  assert calls[1]["materialize"]["failed_sessions_view"] is None


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--failed-sessions-view", "agent_events"),
        ("--min-score", "goal_completion"),
    ],
)
def test_evalbench_import_rejects_bad_view_or_policy_before_reading(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
  calls = _patch_from_bigquery(monkeypatch)
  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--target-dataset",
          "bqaa",
          option,
          value,
      ],
  )
  assert result.exit_code == 2, result.output
  assert calls == []


def test_evalbench_import_rejects_bad_snapshot_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)
  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--target-dataset",
          "bqaa",
          "--snapshot-at",
          "yesterday",
      ],
  )
  assert result.exit_code == 2
  assert "snapshot-at" in result.output
  assert calls == []


@pytest.mark.parametrize("option", ["--events-table", "--scores-table"])
def test_evalbench_import_rejects_reserved_agent_events_table(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
  calls = _patch_from_bigquery(monkeypatch)
  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--target-dataset",
          "bqaa",
          option,
          "agent_events",
      ],
  )
  assert result.exit_code == 2, result.output
  assert "reserved ADK plugin table 'agent_events'" in result.output
  # Rejected before any BigQuery read or write.
  assert calls == []


def test_evalbench_import_surfaces_materialize_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  def failing_from_bigquery(cls, **kwargs):
    raise ValueError(
        "import_version 'v1' already exists with different fingerprints"
    )

  monkeypatch.setattr(
      evalbench.EvalBenchRun,
      "from_bigquery",
      classmethod(failing_from_bigquery),
  )
  result = runner.invoke(
      app,
      [
          "evalbench-import",
          "--project-id",
          "source-project",
          "--evalbench-dataset",
          "evalbench",
          "--job-id",
          "job-123",
          "--target-dataset",
          "bqaa",
      ],
  )
  assert result.exit_code == 2
  assert "different fingerprints" in result.output
