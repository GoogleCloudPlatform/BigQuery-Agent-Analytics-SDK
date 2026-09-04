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
"""Tests for ``bq-agent-sdk evalbench-failed-sessions`` (#435 slice 2)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import evalbench
from bigquery_agent_analytics.cli import app

runner = CliRunner()

_SESSION = evalbench.EvalBenchSession(
    job_id="job-123",
    import_version="v1",
    session_id="evalbench-import:job-123:v1:crash-1",
    trace_id="evalbench-import:job-123:v1:crash-1",
    scenario_id="crash-1",
    started_at=datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc),
    process_failed=True,
    missing_completion=True,
    score_failed=False,
    failed=True,
)
_LISTING = evalbench.EvalBenchFailedSessions(
    job_id="job-123",
    import_version="v1",
    events_table="analytics-project.bqaa.evalbench_agent_events",
    scores_table="analytics-project.bqaa.evalbench_scores_imported",
    sessions=[_SESSION],
    session_count=2,
    failed_count=1,
    manifest={"job_id": "job-123", "import_version": "v1"},
)
_BASE_ARGS = [
    "evalbench-failed-sessions",
    "--project-id",
    "analytics-project",
    "--target-dataset",
    "bqaa",
    "--job-id",
    "job-123",
]


def _patch_failed_sessions(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None = None
) -> list[dict]:
  calls: list[dict] = []

  def fake_failed_sessions(**kwargs):
    calls.append(kwargs)
    if error is not None:
      raise error
    return _LISTING

  monkeypatch.setattr(evalbench, "failed_sessions", fake_failed_sessions)
  return calls


def test_lists_latest_version_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_failed_sessions(monkeypatch)

  result = runner.invoke(app, _BASE_ARGS)

  assert result.exit_code == 0, result.output
  assert calls == [
      {
          "target_project": "analytics-project",
          "target_dataset": "bqaa",
          "job_id": "job-123",
          "import_version": None,
          "policy": None,
          "include_passed": False,
          "location": None,
      }
  ]
  payload = json.loads(result.output)
  assert payload["import_version"] == "v1"
  assert payload["failed_count"] == 1
  assert payload["sessions"][0]["session_id"] == (
      "evalbench-import:job-123:v1:crash-1"
  )
  assert (
      payload["sessions"][0]["trace_id"] == payload["sessions"][0]["session_id"]
  )
  # Slice 9 + G1 freeze: each session row carries the frozen taxonomy
  # names computed from its mechanical flags (process_failed +
  # missing_completion for this fixture map to tool blockers +
  # finalization, returned in frozen order) -- no extra field on the
  # fixture, no BigQuery.
  assert payload["sessions"][0]["taxonomy_categories"] == [
      "finalization",
      "tool blockers",
  ]


def test_pins_version_and_policy_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls = _patch_failed_sessions(monkeypatch)

  result = runner.invoke(
      app,
      _BASE_ARGS
      + [
          "--import-version",
          "v1",
          "--min-score",
          "goal_completion=0.5",
          "--min-score",
          "sql_correctness=1",
          "--missing-score-passes",
          "--include-passed",
          "--location",
          "US",
          "--format",
          "table",
      ],
  )

  assert result.exit_code == 0, result.output
  (call,) = calls
  assert call["import_version"] == "v1"
  assert call["policy"] == evalbench.EvalScorePolicy(
      {"goal_completion": 0.5, "sql_correctness": 1.0},
      missing_score_fails=False,
  )
  assert call["include_passed"] is True
  assert call["location"] == "US"
  assert "session_id" in result.output
  assert "evalbench-import:job-123:v1:crash-1" in result.output


@pytest.mark.parametrize(
    "option",
    ["goal_completion", "=0.5", "goal_completion=high", "bad name=0.5"],
)
def test_rejects_malformed_min_score(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
  calls = _patch_failed_sessions(monkeypatch)
  result = runner.invoke(app, _BASE_ARGS + ["--min-score", option])
  assert result.exit_code == 2, result.output
  assert "min-score" in result.output or "comparator" in result.output
  assert calls == []


def test_surfaces_consumer_errors(monkeypatch: pytest.MonkeyPatch) -> None:
  _patch_failed_sessions(
      monkeypatch,
      error=ValueError("EvalBench job 'job-123' has no published import"),
  )
  result = runner.invoke(app, _BASE_ARGS)
  assert result.exit_code == 2
  assert "no published import" in result.output
