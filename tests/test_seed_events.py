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
"""Tests for the synthetic agent_events generator (issue #246)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json

import pytest

from bigquery_agent_analytics.seed_events import generate_seed_events
from bigquery_agent_analytics.seed_events import run_seed_events
from bigquery_agent_analytics.seed_events import Scenario
from bigquery_agent_analytics.seed_events import SeedEventsResult

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_EVENTS_PER_SESSION = 6  # submit(1) + evaluate(3) + commit(1) + completed(1)
_EXPECTED_COLS = {
    "timestamp",
    "event_type",
    "agent",
    "session_id",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "status",
    "error_message",
    "is_truncated",
    "content",
    "attributes",
    "latency_ms",
}


def test_same_seed_and_now_is_byte_identical() -> None:
  a = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  b = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  assert a == b


def test_different_seed_changes_content() -> None:
  a = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  b = generate_seed_events(sessions=3, seed=7, now=_FIXED_NOW)
  assert a != b


def test_seed_none_still_produces_valid_rows() -> None:
  rows = generate_seed_events(sessions=2, seed=None, now=_FIXED_NOW)
  assert len(rows) == 2 * _EVENTS_PER_SESSION
  assert all(set(row) == _EXPECTED_COLS for row in rows)


def test_payload_shape_and_terminal_events() -> None:
  rows = generate_seed_events(sessions=4, seed=1, now=_FIXED_NOW)
  assert len(rows) == 4 * _EVENTS_PER_SESSION

  expected_cols = _EXPECTED_COLS
  per_session_completed: dict[str, int] = {}
  for row in rows:
    assert set(row) == expected_cols
    assert row["event_type"] in {"TOOL_COMPLETED", "AGENT_COMPLETED"}
    json.loads(row["content"])  # valid JSON string
    if row["event_type"] == "AGENT_COMPLETED":
      per_session_completed[row["session_id"]] = (
          per_session_completed.get(row["session_id"], 0) + 1
      )

  assert len(per_session_completed) == 4
  assert all(count == 1 for count in per_session_completed.values())


@pytest.mark.parametrize("bad", [0, -5])
def test_sessions_must_be_at_least_one(bad: int) -> None:
  with pytest.raises(ValueError, match="sessions must be >= 1"):
    generate_seed_events(sessions=bad, seed=1, now=_FIXED_NOW)


def test_scenario_enum_default_is_decision() -> None:
  # Calling without scenario= must succeed using the DECISION default.
  rows = generate_seed_events(sessions=1, seed=1, now=_FIXED_NOW)
  assert len(rows) == _EVENTS_PER_SESSION
  assert Scenario.DECISION.value == "decision"


class _FakeBQClient:
  """Records create_table / insert_rows_json calls; returns canned errors."""

  def __init__(self, insert_errors: list | None = None) -> None:
    self.created: list = []
    self.inserted: list = []
    self._insert_errors = insert_errors or []

  def create_table(self, table: object, exists_ok: bool = False) -> object:
    self.created.append((table, exists_ok))
    return table

  def insert_rows_json(self, table_ref: str, rows: list) -> list:
    self.inserted.append((table_ref, rows))
    return self._insert_errors


def test_dry_run_generates_without_touching_bigquery() -> None:
  fake = _FakeBQClient()
  result = run_seed_events(
      project_id="p",
      dataset_id="d",
      sessions=2,
      seed=1,
      dry_run=True,
      now=_FIXED_NOW,
      bq_client=fake,
  )
  assert fake.created == [] and fake.inserted == []
  assert result.dry_run is True
  assert result.ok is True
  assert result.events_inserted == 0
  assert result.events_generated == 2 * _EVENTS_PER_SESSION
  assert result.event_type_counts["AGENT_COMPLETED"] == 2
  assert result.table_ref == "p.d.agent_events"


def test_insert_success_reports_inserted_count() -> None:
  fake = _FakeBQClient()
  result = run_seed_events(
      project_id="p",
      dataset_id="d",
      sessions=3,
      seed=1,
      now=_FIXED_NOW,
      bq_client=fake,
  )
  assert isinstance(result, SeedEventsResult)
  assert len(fake.created) == 1
  table_ref, rows = fake.inserted[0]
  assert table_ref == "p.d.agent_events"
  assert len(rows) == 3 * _EVENTS_PER_SESSION
  assert result.ok is True
  assert result.events_inserted == 3 * _EVENTS_PER_SESSION
  assert result.errors == []


def test_insert_errors_are_explicit_not_exceptions() -> None:
  bq_errors = [{"index": 0, "errors": [{"reason": "invalid"}]}]
  fake = _FakeBQClient(insert_errors=bq_errors)
  result = run_seed_events(
      project_id="p",
      dataset_id="d",
      sessions=1,
      seed=1,
      now=_FIXED_NOW,
      bq_client=fake,
  )
  assert result.ok is False
  assert result.errors == bq_errors
  assert result.events_inserted == 0


def test_run_seed_events_rejects_bad_sessions() -> None:
  with pytest.raises(ValueError, match="sessions must be >= 1"):
    run_seed_events(
        project_id="p",
        dataset_id="d",
        sessions=0,
        seed=1,
        now=_FIXED_NOW,
        bq_client=_FakeBQClient(),
    )


def test_run_seed_events_rejects_unknown_scenario() -> None:
  with pytest.raises(ValueError):
    run_seed_events(
        project_id="p",
        dataset_id="d",
        sessions=1,
        scenario="nonexistent",
        now=_FIXED_NOW,
        bq_client=_FakeBQClient(),
    )


def test_to_json_round_trips() -> None:
  result = run_seed_events(
      project_id="p",
      dataset_id="d",
      sessions=1,
      seed=1,
      dry_run=True,
      now=_FIXED_NOW,
      bq_client=_FakeBQClient(),
  )
  payload = result.to_json()
  assert json.loads(json.dumps(payload)) == payload
  assert payload["ok"] is True
  assert payload["events_inserted"] == 0
