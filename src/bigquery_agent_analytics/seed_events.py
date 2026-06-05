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
"""Synthetic agent_events generator backing ``bqaa seed-events`` (#246).

Writes a corpus of TOOL_COMPLETED + AGENT_COMPLETED events to a configured
``agent_events`` table. The base session is a decision flow (submit_request
-> evaluate_option -> commit_outcome); the ``decision`` scenario closes every
session with an AGENT_COMPLETED terminal row, while ``decision-realistic``
mixes in failed, truncated, and orphaned (no terminal event) sessions. The
materializer keys on the terminal AGENT_COMPLETED row.

``--seed`` freezes IDs and content (and event structure); timestamps stay
anchored to run time so seeded events land inside the materializer's
``--lookback-hours`` window. Inject a fixed ``now`` via the SDK for
byte-identical rows.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import enum
import json
import math
import random
from typing import Any, Optional


class Scenario(str, enum.Enum):
  """Synthetic event scenarios. Extensible seam for #247."""

  DECISION = "decision"
  DECISION_REALISTIC = "decision-realistic"


_EVENT_SCHEMA_FIELDS = (
    ("timestamp", "TIMESTAMP", "REQUIRED"),
    ("event_type", "STRING", "REQUIRED"),
    ("agent", "STRING", "NULLABLE"),
    ("session_id", "STRING", "NULLABLE"),
    ("invocation_id", "STRING", "NULLABLE"),
    ("user_id", "STRING", "NULLABLE"),
    ("trace_id", "STRING", "NULLABLE"),
    ("span_id", "STRING", "NULLABLE"),
    ("parent_span_id", "STRING", "NULLABLE"),
    ("status", "STRING", "NULLABLE"),
    ("error_message", "STRING", "NULLABLE"),
    ("is_truncated", "BOOLEAN", "NULLABLE"),
    ("content", "JSON", "NULLABLE"),
    ("attributes", "JSON", "NULLABLE"),
    ("latency_ms", "JSON", "NULLABLE"),
)

_TOPICS = (
    "approve loan",
    "schedule maintenance",
    "grant access",
    "release budget",
)

_REALISTIC_AGENTS = (
    "loan-advisor",
    "ops-scheduler",
    "access-broker",
    "budget-allocator",
)
_REALISTIC_USERS = tuple(f"user-{i:03d}" for i in range(12))
_REALISTIC_OPTION_LABELS = (
    "approve",
    "reject",
    "defer",
    "escalate",
    "delegate",
    "hold",
)
_REALISTIC_WINDOW = timedelta(hours=72)
# Held back from ``now`` so a session's per-step offsets never produce a
# timestamp after ``now`` (a session spans at most ~8s; 60s is safe margin).
_MAX_SESSION_SPAN = timedelta(seconds=60)


def _hex(rng: random.Random, length: int) -> str:
  """Deterministic hex id of ``length`` chars, driven by ``rng``."""
  return f"{rng.getrandbits(4 * length):0{length}x}"


def _row(
    rng: random.Random,
    event_type: str,
    session_id: str,
    content: dict,
    ts: datetime,
    *,
    agent: str = "demo-agent",
    user_id: str = "demo-user",
) -> dict:
  return {
      "timestamp": ts.isoformat(),
      "event_type": event_type,
      "agent": agent,
      "session_id": session_id,
      "invocation_id": _hex(rng, 32),
      "user_id": user_id,
      # One trace per session in the demo corpus; trace_id mirrors session_id.
      "trace_id": session_id,
      "span_id": _hex(rng, 16),
      "parent_span_id": None,
      "status": "ok",
      "error_message": None,
      "is_truncated": False,
      "content": json.dumps(content),
      "attributes": "{}",
      "latency_ms": "{}",
  }


def _shuffled_cycle(
    rng: random.Random, roster: tuple[str, ...], n: int
) -> list[str]:
  """Return a length-``n`` assignment cycling ``roster``, shuffled in place.

  Repeats the roster to length ``n`` then shuffles, so every member appears
  (for ``n >= len(roster)``) and at least ``min(n, len(roster))`` distinct
  values are present -- a true coverage guarantee, not a probabilistic one.
  """
  reps = -(-n // len(roster))  # ceil division
  cycle = (list(roster) * reps)[:n]
  rng.shuffle(cycle)
  return cycle


def _outcome_allocation(sessions: int) -> dict[str, int]:
  """Exact, deterministic outcome-bucket counts for ``decision-realistic``.

  Each edge bucket (failed/orphaned/truncated) gets ``round-half-up`` of 10%,
  floored at 1; ``success`` takes the remainder. Exact 70/10/10/10 at 100.
  Requires ``sessions >= 4`` (else ``success`` would be < 1).
  """
  edge = max(1, math.floor(0.10 * sessions + 0.5))
  success = sessions - 3 * edge
  if success < 1:
    raise ValueError("decision-realistic requires sessions >= 4")
  return {
      "success": success,
      "failed": edge,
      "orphaned": edge,
      "truncated": edge,
  }


def _decision_session(rng: random.Random, now: datetime) -> list[dict]:
  session_id = f"sess-{_hex(rng, 8)}"
  request_id = f"req-{_hex(rng, 6)}"
  topic = rng.choice(_TOPICS)
  rows: list[dict] = [
      _row(
          rng,
          "TOOL_COMPLETED",
          session_id,
          {
              "tool": "submit_request",
              "result": {
                  "request_id": request_id,
                  "request_text": f"Should we {topic}?",
              },
          },
          now,
      )
  ]

  options = [
      {
          "option_id": f"opt-{_hex(rng, 5)}",
          "option_label": label,
          "confidence": round(rng.uniform(0.1, 0.95), 2),
      }
      for label in ("yes", "no", "defer")
  ]
  for i, opt in enumerate(options):
    rows.append(
        _row(
            rng,
            "TOOL_COMPLETED",
            session_id,
            {
                "tool": "evaluate_option",
                "result": {"request_id": request_id, **opt},
            },
            now + timedelta(seconds=i + 1),
        )
    )

  selected = max(options, key=lambda o: o["confidence"])
  rationale = (
      f"Picked '{selected['option_label']}' "
      f"(confidence {selected['confidence']:.2f}) over "
      f"the {len(options) - 1} alternatives."
  )
  rows.append(
      _row(
          rng,
          "TOOL_COMPLETED",
          session_id,
          {
              "tool": "commit_outcome",
              "result": {
                  "request_id": request_id,
                  "outcome_id": f"out-{_hex(rng, 6)}",
                  "status": "committed",
                  "rationale": rationale,
              },
          },
          now + timedelta(seconds=5),
      )
  )
  rows.append(
      _row(
          rng,
          "AGENT_COMPLETED",
          session_id,
          {"final": True},
          now + timedelta(seconds=6),
      )
  )
  return rows


def _build_decision_corpus(
    rng: random.Random, now: datetime, sessions: int
) -> tuple[list[dict], dict[str, int]]:
  """Corpus builder for the small ``decision`` scenario.

  Reproduces the exact pre-refactor loop (30s apart from ``now - 10min``,
  delegating to ``_decision_session``) so output stays byte-identical.
  """
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  rows: list[dict] = []
  cur = now - timedelta(minutes=10)
  for _ in range(sessions):
    rows.extend(_decision_session(rng, cur))
    cur += timedelta(seconds=30)
  return rows, {"success": sessions}


def _realistic_session(
    rng: random.Random,
    start: datetime,
    outcome: str,
    agent: str,
    user: str,
    topic: str,
) -> list[dict]:
  """One realistic decision session starting at ``start``.

  ``outcome`` is one of success/failed/orphaned/truncated and shapes the
  terminal event and truncation flag. Option count varies 2..6.
  """
  session_id = f"sess-{_hex(rng, 8)}"
  request_id = f"req-{_hex(rng, 6)}"
  rows: list[dict] = []

  def add(
      event_type,
      content,
      offset,
      *,
      status="ok",
      error_message=None,
      is_truncated=False,
  ):
    row = _row(
        rng,
        event_type,
        session_id,
        content,
        start + timedelta(seconds=offset),
        agent=agent,
        user_id=user,
    )
    row["status"] = status
    row["error_message"] = error_message
    row["is_truncated"] = is_truncated
    rows.append(row)

  add(
      "TOOL_COMPLETED",
      {
          "tool": "submit_request",
          "result": {
              "request_id": request_id,
              "request_text": f"Should we {topic}?",
          },
      },
      0,
  )

  k = rng.randint(2, 6)
  options = [
      {
          "option_id": f"opt-{_hex(rng, 5)}",
          "option_label": _REALISTIC_OPTION_LABELS[j],
          "confidence": round(rng.uniform(0.1, 0.95), 2),
      }
      for j in range(k)
  ]
  for i, opt in enumerate(options):
    content = {
        "tool": "evaluate_option",
        "result": {"request_id": request_id, **opt},
    }
    # Truncated sessions clip one evaluate row's payload.
    clip = outcome == "truncated" and i == 0
    if clip:
      content["result"]["notes"] = "(payload truncated)"
    add("TOOL_COMPLETED", content, i + 1, is_truncated=clip)

  selected = max(options, key=lambda o: o["confidence"])
  add(
      "TOOL_COMPLETED",
      {
          "tool": "commit_outcome",
          "result": {
              "request_id": request_id,
              "outcome_id": f"out-{_hex(rng, 6)}",
              "status": "committed",
              "selected": selected["option_label"],
          },
      },
      k + 1,
  )

  if outcome == "orphaned":
    return rows  # no terminal event -- exercises the orphan watchdog
  if outcome == "failed":
    add(
        "AGENT_COMPLETED",
        {"final": True},
        k + 2,
        status="error",
        error_message="agent run failed: downstream timeout after commit",
    )
  else:  # success or truncated
    add("AGENT_COMPLETED", {"final": True}, k + 2)
  return rows


def build_realistic_corpus(
    rng: random.Random, now: datetime, sessions: int
) -> tuple[list[dict], dict[str, int]]:
  """Corpus builder for ``decision-realistic`` (see spec #247).

  Fixed deterministic mix (70/10/10/10 at 100, scaled otherwise), multi-day
  spread over ``[now - 72h, now - _MAX_SESSION_SPAN]``, multiple
  agents/users/topics. Returns ``(rows, session_outcome_counts)``.
  """
  counts = _outcome_allocation(sessions)  # raises if sessions < 4
  outcomes: list[str] = []
  for name in ("success", "failed", "orphaned", "truncated"):
    outcomes.extend([name] * counts[name])
  rng.shuffle(outcomes)

  window_start = now - _REALISTIC_WINDOW
  window_end = now - _MAX_SESSION_SPAN
  slot = (window_end - window_start).total_seconds() / sessions
  starts = [
      window_start + timedelta(seconds=i * slot + rng.uniform(0, slot))
      for i in range(sessions)
  ]

  agents = _shuffled_cycle(rng, _REALISTIC_AGENTS, sessions)
  users = _shuffled_cycle(rng, _REALISTIC_USERS, sessions)
  topics = _shuffled_cycle(rng, _TOPICS, sessions)

  rows: list[dict] = []
  for i in range(sessions):
    rows.extend(
        _realistic_session(
            rng, starts[i], outcomes[i], agents[i], users[i], topics[i]
        )
    )
  return rows, counts


# scenario -> corpus builder ``(rng, now, sessions) -> (rows, outcome_counts)``
_SCENARIO_BUILDERS = {
    Scenario.DECISION: _build_decision_corpus,
    Scenario.DECISION_REALISTIC: build_realistic_corpus,
}
assert set(_SCENARIO_BUILDERS) == set(Scenario), (
    "every Scenario needs a builder; missing: "
    f"{set(Scenario) - set(_SCENARIO_BUILDERS)}"
)

_SCENARIO_DEFAULT_SESSIONS = {
    Scenario.DECISION: 5,
    Scenario.DECISION_REALISTIC: 100,
}
assert set(_SCENARIO_DEFAULT_SESSIONS) == set(Scenario), (
    "every Scenario needs a default session count; missing: "
    f"{set(Scenario) - set(_SCENARIO_DEFAULT_SESSIONS)}"
)


def generate_seed_events(
    *,
    sessions: int,
    seed: Optional[int],
    now: datetime,
    scenario: Scenario = Scenario.DECISION,
) -> list[dict]:
  """Build synthetic agent_events rows. Pure: no I/O.

  ``(seed, now)`` fixed -> byte-identical rows. ``seed=None`` -> live RNG.
  Raises ``ValueError`` if ``sessions < 1``.
  """
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  rng = random.Random(seed)
  rows, _counts = _SCENARIO_BUILDERS[scenario](rng, now, sessions)
  return rows


@dataclasses.dataclass(frozen=True)
class SeedEventsResult:
  """Outcome of a seed-events run."""

  table_ref: str
  scenario: str
  sessions: int
  events_generated: int
  events_inserted: int
  dry_run: bool
  ok: bool
  event_type_counts: dict[str, int]
  errors: list[dict]
  # {} when not populated (e.g. a SeedEventsResult constructed without it).
  session_outcome_counts: dict[str, int] = dataclasses.field(
      default_factory=dict
  )

  def to_json(self) -> dict[str, Any]:
    return {
        "table_ref": self.table_ref,
        "scenario": self.scenario,
        "sessions": self.sessions,
        "events_generated": self.events_generated,
        "events_inserted": self.events_inserted,
        "dry_run": self.dry_run,
        "ok": self.ok,
        "event_type_counts": dict(self.event_type_counts),
        "session_outcome_counts": dict(self.session_outcome_counts),
        "errors": list(self.errors),
    }


def run_seed_events(
    *,
    project_id: str,
    dataset_id: str,
    sessions: Optional[int] = None,
    seed: Optional[int] = None,
    scenario: Scenario | str = Scenario.DECISION,
    events_table: str = "agent_events",
    dry_run: bool = False,
    now: Optional[datetime] = None,
    bq_client: Optional[Any] = None,
) -> SeedEventsResult:
  """Generate synthetic events and (unless ``dry_run``) insert them.

  ``sessions`` defaults per scenario: 5 for ``decision``, 100 for
  ``decision-realistic``. Pass an explicit value to override.
  Invalid input (``sessions < 1``, unknown ``scenario``) raises; the CLI
  maps that to exit 2. BigQuery insert errors are modeled as ``ok=False``
  with ``errors`` populated -- not raised -- so the JSON report stays
  authoritative (CLI exit 1).
  """
  scenario = Scenario(scenario) if isinstance(scenario, str) else scenario
  if sessions is None:
    sessions = _SCENARIO_DEFAULT_SESSIONS[scenario]
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  if now is None:
    now = datetime.now(timezone.utc)

  rng = random.Random(seed)
  rows, session_outcome_counts = _SCENARIO_BUILDERS[scenario](
      rng, now, sessions
  )
  counts: dict[str, int] = {}
  for row in rows:
    counts[row["event_type"]] = counts.get(row["event_type"], 0) + 1
  table_ref = f"{project_id}.{dataset_id}.{events_table}"

  if dry_run:
    return SeedEventsResult(
        table_ref=table_ref,
        scenario=scenario.value,
        sessions=sessions,
        events_generated=len(rows),
        events_inserted=0,
        dry_run=True,
        ok=True,
        event_type_counts=counts,
        errors=[],
        session_outcome_counts=session_outcome_counts,
    )

  from google.cloud import bigquery

  client = bq_client or bigquery.Client(project=project_id)
  table = bigquery.Table(table_ref, schema=_event_schema())
  table.time_partitioning = bigquery.TimePartitioning(field="timestamp")
  client.create_table(table, exists_ok=True)
  errors = client.insert_rows_json(table_ref, rows)
  if errors:
    return SeedEventsResult(
        table_ref=table_ref,
        scenario=scenario.value,
        sessions=sessions,
        events_generated=len(rows),
        events_inserted=0,
        dry_run=False,
        ok=False,
        event_type_counts=counts,
        errors=list(errors),
        session_outcome_counts=session_outcome_counts,
    )
  return SeedEventsResult(
      table_ref=table_ref,
      scenario=scenario.value,
      sessions=sessions,
      events_generated=len(rows),
      events_inserted=len(rows),
      dry_run=False,
      ok=True,
      event_type_counts=counts,
      errors=[],
      session_outcome_counts=session_outcome_counts,
  )


def _event_schema() -> list:
  """Build the BigQuery schema lazily (keeps import-time deps minimal)."""
  from google.cloud import bigquery

  return [
      bigquery.SchemaField(name, field_type, mode=mode)
      for name, field_type, mode in _EVENT_SCHEMA_FIELDS
  ]
