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

"""Deterministic seeded MAKO events for the migration v5 demo.

Generates ~50 sessions of MAKO-shaped decision-flow events.
Each session = one ``AgentSession`` containing 2-4
``DecisionPoint``s; each decision evaluates 3-5
``Candidate``s against a ``ContextSnapshot`` and picks one
``SelectionOutcome``.

Seeded RNG so each run produces byte-identical output —
critical for the notebook's reproducibility claim.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import random
from typing import Iterator

# Stable seed — bump only on intentional corpus changes.
_RANDOM_SEED = 20260512
_SESSION_COUNT = 50
_DECISION_TYPES = (
    "AUDIENCE_SEGMENT",
    "BID_VALUE",
    "CREATIVE_VARIANT",
    "FREQUENCY_CAP",
)
_CANDIDATE_LABELS = {
    "AUDIENCE_SEGMENT": (
        "Premium Subscribers",
        "Mobile First-Time Visitors",
        "Returning High-Intent Buyers",
        "Lapsed 90-day",
        "Affluent 25-44",
    ),
    "BID_VALUE": (
        "$0.50 CPM",
        "$0.75 CPM",
        "$1.20 CPM",
        "$2.00 CPM",
    ),
    "CREATIVE_VARIANT": (
        "Hero Image v3",
        "Animated Carousel A",
        "Static Card B",
        "Video Pre-roll Long",
    ),
    "FREQUENCY_CAP": (
        "1/day",
        "3/day",
        "5/week",
        "no cap",
    ),
}


@dataclasses.dataclass(frozen=True)
class SeededEvent:
  """One MAKO event row destined for the notebook's
  ``agent_events`` table. Mirrors the BQ AA plugin's event
  payload shape (event_type / session_id / span_id / content
  dict) so the storyboard's Beat 3 extractors see the same
  surface they'd see in production."""

  event_type: str
  session_id: str
  span_id: str
  event_timestamp: str
  content: dict

  def to_dict(self) -> dict:
    return {
        "event_type": self.event_type,
        "session_id": self.session_id,
        "span_id": self.span_id,
        "event_timestamp": self.event_timestamp,
        "content": self.content,
    }


def generate_events() -> list[SeededEvent]:
  """Return the canonical list of seeded events.

  Deterministic: same seed → same output across machines."""
  rng = random.Random(_RANDOM_SEED)
  base_ts = datetime.datetime(2026, 5, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
  events: list[SeededEvent] = []

  for session_idx in range(1, _SESSION_COUNT + 1):
    session_id = _stable_id("session", session_idx)
    session_start = base_ts + datetime.timedelta(hours=session_idx)

    # AgentSession-start event.
    events.append(
        SeededEvent(
            event_type="agent_session_started",
            session_id=session_id,
            span_id=_stable_id("span_session_start", session_idx),
            event_timestamp=_iso(session_start),
            content={
                "agent_session_id": session_id,
                "agent_name": "mako_decision_agent",
            },
        )
    )

    decisions_per_session = rng.randint(2, 4)
    for decision_idx in range(1, decisions_per_session + 1):
      decision_type = rng.choice(_DECISION_TYPES)
      decision_id = _stable_id("decision", session_idx, decision_idx)
      context_id = _stable_id("context", session_idx, decision_idx)
      decision_ts = session_start + datetime.timedelta(
          minutes=10 * decision_idx
      )

      # ContextSnapshot event (precedes the decision).
      events.append(
          SeededEvent(
              event_type="context_captured",
              session_id=session_id,
              span_id=_stable_id("span_ctx", session_idx, decision_idx),
              event_timestamp=_iso(decision_ts - datetime.timedelta(seconds=2)),
              content={
                  "context_id": context_id,
                  "snapshot_payload": json.dumps(
                      {
                          "audience_size": rng.randint(1000, 100_000),
                          "budget_remaining_usd": round(
                              rng.uniform(50.0, 5000.0), 2
                          ),
                      }
                  ),
                  "snapshot_timestamp": _iso(
                      decision_ts - datetime.timedelta(seconds=2)
                  ),
              },
          )
      )

      # The decision itself — the BKA-decision-style event
      # the demo's reference extractor targets.
      candidate_count = rng.randint(3, 5)
      candidates = []
      for cand_idx in range(candidate_count):
        cand_id = _stable_id("cand", session_idx, decision_idx, cand_idx)
        cand_label = rng.choice(_CANDIDATE_LABELS[decision_type])
        candidates.append(
            {
                "candidate_id": cand_id,
                "candidate_label": cand_label,
                "score": round(rng.uniform(0.1, 0.99), 3),
            }
        )
      selected = max(candidates, key=lambda c: c["score"])
      outcome_id = _stable_id("outcome", session_idx, decision_idx)

      events.append(
          SeededEvent(
              event_type="mako_decision",
              session_id=session_id,
              span_id=_stable_id("span_decision", session_idx, decision_idx),
              event_timestamp=_iso(decision_ts),
              content={
                  "decision_id": decision_id,
                  "decision_type": decision_type,
                  "context_id": context_id,
                  "candidates": candidates,
                  "outcome_id": outcome_id,
                  "selected_candidate_id": selected["candidate_id"],
                  "rationale": (
                      f"selected highest-score candidate "
                      f"({selected['candidate_label']}) at "
                      f"score={selected['score']}"
                  ),
              },
          )
      )

    # AgentSession-end event.
    events.append(
        SeededEvent(
            event_type="agent_session_ended",
            session_id=session_id,
            span_id=_stable_id("span_session_end", session_idx),
            event_timestamp=_iso(
                session_start
                + datetime.timedelta(minutes=10 * decisions_per_session + 1)
            ),
            content={
                "agent_session_id": session_id,
                "decisions_count": decisions_per_session,
            },
        )
    )

  return events


def _stable_id(prefix: str, *parts) -> str:
  """Deterministic short ID derived from prefix + parts.

  Avoids ``uuid.uuid4`` (non-deterministic) — same input
  always yields the same ID so notebook outputs round-trip
  cleanly across runs."""
  key = ":".join((prefix, *(str(p) for p in parts)))
  digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
  return f"{prefix}-{digest[:12]}"


def _iso(ts: datetime.datetime) -> str:
  return ts.isoformat()


def as_jsonl() -> Iterator[str]:
  """Yield one JSON-encoded event per line. Used by the
  notebook to materialize the seed corpus as a `.jsonl`
  file for revalidation tests."""
  for event in generate_events():
    yield json.dumps(event.to_dict())


def event_count() -> int:
  """Number of seed events. Useful for the notebook's
  cell-output sanity check."""
  return len(generate_events())


if __name__ == "__main__":  # pragma: no cover
  for line in as_jsonl():
    print(line)
