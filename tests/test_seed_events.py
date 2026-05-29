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
from bigquery_agent_analytics.seed_events import Scenario

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_EVENTS_PER_SESSION = 6  # submit(1) + evaluate(3) + commit(1) + completed(1)


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


def test_payload_shape_and_terminal_events() -> None:
  rows = generate_seed_events(sessions=4, seed=1, now=_FIXED_NOW)
  assert len(rows) == 4 * _EVENTS_PER_SESSION

  expected_cols = {
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
  assert Scenario.DECISION.value == "decision"
