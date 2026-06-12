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
from datetime import timedelta
from datetime import timezone
import json
import random

import pytest

from bigquery_agent_analytics.seed_events import _outcome_allocation
from bigquery_agent_analytics.seed_events import _shuffled_cycle
from bigquery_agent_analytics.seed_events import build_realistic_corpus
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


def test_decision_corpus_is_byte_identical_after_refactor() -> None:
  # Pin decision output so the seam refactor cannot change it.
  rows = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  assert len(rows) == 3 * _EVENTS_PER_SESSION
  assert rows[0]["agent"] == "demo-agent"
  assert rows[0]["user_id"] == "demo-user"
  assert rows[-1]["event_type"] == "AGENT_COMPLETED"
  # Same (seed, now) still byte-identical.
  assert generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW) == rows
  # Golden values pin the seeded RNG-consumption order: a change to
  # _decision_session's draw sequence would break these immediately.
  assert rows[0]["session_id"] == "sess-a3b1799d"
  assert rows[0]["span_id"] == "bc8960a923b8c1e9"


def test_decision_result_reports_success_outcome_counts() -> None:
  result = run_seed_events(
      project_id="p",
      dataset_id="d",
      sessions=4,
      seed=1,
      dry_run=True,
      now=_FIXED_NOW,
      bq_client=_FakeBQClient(),
  )
  assert result.session_outcome_counts == {"success": 4}
  assert result.to_json()["session_outcome_counts"] == {"success": 4}


def test_outcome_allocation_exact_at_100() -> None:
  assert _outcome_allocation(100) == {
      "success": 70,
      "failed": 10,
      "orphaned": 10,
      "truncated": 10,
  }


def test_outcome_allocation_scales_and_keeps_min_one() -> None:
  assert _outcome_allocation(10) == {
      "success": 7,
      "failed": 1,
      "orphaned": 1,
      "truncated": 1,
  }
  assert _outcome_allocation(4) == {
      "success": 1,
      "failed": 1,
      "orphaned": 1,
      "truncated": 1,
  }


def test_outcome_allocation_rejects_too_few_sessions() -> None:
  for bad in (3, 1, 0):
    with pytest.raises(
        ValueError, match="decision-realistic requires sessions >= 4"
    ):
      _outcome_allocation(bad)


def test_shuffled_cycle_covers_roster_and_is_deterministic() -> None:
  roster = ("a", "b", "c", "d")
  out1 = _shuffled_cycle(random.Random(1), roster, 10)
  out2 = _shuffled_cycle(random.Random(1), roster, 10)
  assert out1 == out2  # deterministic for a fixed rng seed
  assert len(out1) == 10
  assert set(out1) == set(roster)  # every roster member appears
  # >=2 distinct even for small n
  assert len(set(_shuffled_cycle(random.Random(2), roster, 2))) >= 2


def _by_session(rows: list[dict]) -> dict[str, list[dict]]:
  grouped: dict[str, list[dict]] = {}
  for row in rows:
    grouped.setdefault(row["session_id"], []).append(row)
  return grouped


def test_realistic_outcome_mix_exact_at_100() -> None:
  rows, counts = build_realistic_corpus(random.Random(42), _FIXED_NOW, 100)
  assert counts == {
      "success": 70,
      "failed": 10,
      "orphaned": 10,
      "truncated": 10,
  }

  sessions = _by_session(rows)
  assert len(sessions) == 100
  orphaned = [
      s
      for s in sessions.values()
      if not any(r["event_type"] == "AGENT_COMPLETED" for r in s)
  ]
  failed = [
      s
      for s in sessions.values()
      if any(
          r["event_type"] == "AGENT_COMPLETED" and r["status"] == "error"
          for r in s
      )
  ]
  truncated = [
      s for s in sessions.values() if any(r["is_truncated"] for r in s)
  ]
  assert len(orphaned) == 10
  assert len(failed) == 10
  assert len(truncated) == 10
  # Truncated is identified solely by is_truncated rows: it must still be a
  # completed (not failed) session, so this stays distinct from `failed`.
  for session in truncated:
    terminals = [r for r in session if r["event_type"] == "AGENT_COMPLETED"]
    assert len(terminals) == 1 and terminals[0]["status"] == "ok"


def test_realistic_terminal_event_invariant() -> None:
  rows, _ = build_realistic_corpus(random.Random(7), _FIXED_NOW, 100)
  for session in _by_session(rows).values():
    terminals = [r for r in session if r["event_type"] == "AGENT_COMPLETED"]
    assert len(terminals) in (0, 1)  # orphaned -> 0, others -> exactly 1


def test_realistic_failed_sessions_have_error_message() -> None:
  rows, _ = build_realistic_corpus(random.Random(7), _FIXED_NOW, 100)
  for session in _by_session(rows).values():
    for row in session:
      if row["event_type"] == "AGENT_COMPLETED" and row["status"] == "error":
        assert row["error_message"]  # non-empty


def test_realistic_variable_option_count_in_range() -> None:
  rows, _ = build_realistic_corpus(random.Random(7), _FIXED_NOW, 100)
  for session in _by_session(rows).values():
    options = [
        r
        for r in session
        if json.loads(r["content"]).get("tool") == "evaluate_option"
    ]
    assert 2 <= len(options) <= 6


def test_realistic_multi_day_span_and_no_future() -> None:
  rows, _ = build_realistic_corpus(random.Random(7), _FIXED_NOW, 100)
  ts = [datetime.fromisoformat(r["timestamp"]) for r in rows]
  span = max(ts) - min(ts)
  assert span > timedelta(hours=24)
  assert span <= timedelta(hours=72)
  assert max(ts) <= _FIXED_NOW  # no future timestamps


def test_realistic_multiple_agents_and_users() -> None:
  rows, _ = build_realistic_corpus(random.Random(7), _FIXED_NOW, 100)
  assert len({r["agent"] for r in rows}) >= 2
  assert len({r["user_id"] for r in rows}) >= 2


def test_realistic_is_deterministic() -> None:
  a, ca = build_realistic_corpus(random.Random(42), _FIXED_NOW, 100)
  b, cb = build_realistic_corpus(random.Random(42), _FIXED_NOW, 100)
  assert a == b and ca == cb


def test_realistic_rejects_too_few_sessions() -> None:
  with pytest.raises(
      ValueError, match="decision-realistic requires sessions >= 4"
  ):
    build_realistic_corpus(random.Random(1), _FIXED_NOW, 3)


def test_generate_seed_events_supports_realistic_scenario() -> None:
  rows = generate_seed_events(
      sessions=100,
      seed=42,
      now=_FIXED_NOW,
      scenario=Scenario.DECISION_REALISTIC,
  )
  assert len(_by_session(rows)) == 100


def test_default_sessions_per_scenario() -> None:
  decision = run_seed_events(
      project_id="p",
      dataset_id="d",
      seed=1,
      dry_run=True,
      now=_FIXED_NOW,
      bq_client=_FakeBQClient(),
  )
  assert decision.sessions == 5

  realistic = run_seed_events(
      project_id="p",
      dataset_id="d",
      seed=1,
      dry_run=True,
      scenario="decision-realistic",
      now=_FIXED_NOW,
      bq_client=_FakeBQClient(),
  )
  assert realistic.sessions == 100
  assert realistic.session_outcome_counts == {
      "success": 70,
      "failed": 10,
      "orphaned": 10,
      "truncated": 10,
  }


def test_explicit_sessions_overrides_scenario_default() -> None:
  result = run_seed_events(
      project_id="p",
      dataset_id="d",
      sessions=40,
      seed=1,
      dry_run=True,
      scenario="decision-realistic",
      now=_FIXED_NOW,
      bq_client=_FakeBQClient(),
  )
  assert result.sessions == 40
  assert sum(result.session_outcome_counts.values()) == 40
