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
"""Tests for ``bq-agent-sdk evalbench-native-import`` (#463)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import evalbench
from bigquery_agent_analytics import native_events
from bigquery_agent_analytics.cli import app

runner = CliRunner()

_SOURCE_TABLE = "test-project-0728-467323.bqaa_e2e_real.agent_events"


class _RecordingRun:

  def __init__(self, calls: list[dict]) -> None:
    self._calls = calls

  def materialize(self, **kwargs) -> evalbench.EvalBenchImportResult:
    self._calls.append({"materialize": kwargs})
    return evalbench.EvalBenchImportResult(
        job_id="mvp-e2e-real-traces",
        import_version=kwargs.get("import_version") or "abc123",
        status="imported",
        events_table="test-project-0728-467323.bqaa.evalbench_agent_events",
        scores_table="test-project-0728-467323.bqaa.evalbench_scores_imported",
        manifest_table=(
            "test-project-0728-467323.bqaa.evalbench_import_manifest"
        ),
        event_row_count=8,
        score_row_count=2,
        manifest={"job_id": "mvp-e2e-real-traces"},
    )


def _patch_from_bigquery(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
  calls: list[dict] = []

  def fake_from_bigquery(cls, **kwargs):
    calls.append({"from_bigquery": kwargs})
    return _RecordingRun(calls)

  monkeypatch.setattr(
      native_events.NativeAgentEventsRun,
      "from_bigquery",
      classmethod(fake_from_bigquery),
  )
  return calls


def test_native_import_reads_agent_events_and_materializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)

  result = runner.invoke(
      app,
      [
          "evalbench-native-import",
          "--source-table",
          _SOURCE_TABLE,
          "--job-id",
          "mvp-e2e-real-traces",
          "--target-dataset",
          "bqaa",
          "--session-id",
          "7e352c34-4c1c-4395-acd5-fb3c8f215346",
          "--location",
          "US",
          "--snapshot-at",
          "2026-08-30T08:00:00Z",
          "--import-version",
          "v1",
          "--min-score",
          "goal_completion=1.0",
      ],
  )

  assert result.exit_code == 0, result.output
  assert calls[0]["from_bigquery"] == {
      "source_table": _SOURCE_TABLE,
      "job_id": "mvp-e2e-real-traces",
      "session_ids": ["7e352c34-4c1c-4395-acd5-fb3c8f215346"],
      "location": "US",
      "snapshot_at": datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
  }
  materialize = calls[1]["materialize"]
  assert materialize["target_project"] is None
  assert materialize["target_dataset"] == "bqaa"
  assert materialize["events_table"] == "evalbench_agent_events"
  assert materialize["scores_table"] == "evalbench_scores_imported"
  assert materialize["import_version"] == "v1"
  assert materialize["replace"] is False
  assert materialize["failed_sessions_view"] == "evalbench_failed_sessions"
  assert materialize["policy"] == evalbench.EvalScorePolicy(
      {"goal_completion": 1.0}, missing_score_fails=True
  )
  payload = json.loads(result.output)
  assert payload["status"] == "imported"
  assert payload["import_version"] == "v1"
  assert payload["event_row_count"] == 8


def test_native_import_rejects_reserved_agent_events_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)
  result = runner.invoke(
      app,
      [
          "evalbench-native-import",
          "--source-table",
          _SOURCE_TABLE,
          "--job-id",
          "mvp-e2e-real-traces",
          "--target-dataset",
          "bqaa",
          "--events-table",
          "agent_events",
      ],
  )
  assert result.exit_code == 2
  assert "reserved ADK plugin table" in result.output
  assert calls == []


def test_native_import_rejects_bad_snapshot_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_from_bigquery(monkeypatch)
  result = runner.invoke(
      app,
      [
          "evalbench-native-import",
          "--source-table",
          _SOURCE_TABLE,
          "--job-id",
          "mvp-e2e-real-traces",
          "--target-dataset",
          "bqaa",
          "--snapshot-at",
          "not-a-timestamp",
      ],
  )
  assert result.exit_code == 2
  assert "--snapshot-at" in result.output
  assert calls == []
