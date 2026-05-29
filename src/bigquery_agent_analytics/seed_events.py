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

Writes a small corpus of TOOL_COMPLETED + AGENT_COMPLETED events to a
configured ``agent_events`` table. Each session is a 3-step decision flow
(submit_request -> evaluate_option x3 -> commit_outcome) closed by an
AGENT_COMPLETED row, which is the terminal event the materializer keys on.

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
import random
from typing import Any, Optional


class Scenario(str, enum.Enum):
  """Synthetic event scenarios. Extensible seam for #247."""

  DECISION = "decision"


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


def _hex(rng: random.Random, length: int) -> str:
  """Deterministic hex id of ``length`` chars, driven by ``rng``."""
  return f"{rng.getrandbits(4 * length):0{length}x}"


def _row(
    rng: random.Random,
    event_type: str,
    session_id: str,
    content: dict,
    ts: datetime,
) -> dict:
  return {
      "timestamp": ts.isoformat(),
      "event_type": event_type,
      "agent": "demo-agent",
      "session_id": session_id,
      "invocation_id": _hex(rng, 32),
      "user_id": "demo-user",
      "trace_id": session_id[:16],
      "span_id": _hex(rng, 16),
      "parent_span_id": None,
      "status": "ok",
      "error_message": None,
      "is_truncated": False,
      "content": json.dumps(content),
      "attributes": "{}",
      "latency_ms": "{}",
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


_SCENARIO_BUILDERS = {Scenario.DECISION: _decision_session}


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
  builder = _SCENARIO_BUILDERS[scenario]
  rows: list[dict] = []
  cur = now - timedelta(minutes=10)
  for _ in range(sessions):
    rows.extend(builder(rng, cur))
    cur += timedelta(seconds=30)
  return rows
