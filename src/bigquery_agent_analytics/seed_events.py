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
``retail-returns`` scenario (#313) emits a richer multi-agent refund/exchange
trace -- INVOCATION/AGENT/LLM/TOOL events with token-usage and latency
telemetry -- keeping exactly one terminal AGENT_COMPLETED per session. The
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
  RETAIL_RETURNS = "retail-returns"


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

# --- retail-returns scenario (#313) -------------------------------------- #
# A multi-agent refund/exchange trace. One shared session_id/trace_id per
# case; the three agents are distinguished by ``agent`` and span lineage.
_RETAIL_AGENTS = (
    "retail-intake-triage-agent",  # top-level; owns the terminal event
    "retail-fraud-abuse-agent",
    "retail-quality-defect-agent",
)
_RETAIL_OUTCOMES = (
    "refund_approved",
    "exchange_offered",
    "manual_review",
    "fraud_rejected",
    "defect_escalated",
    "system_error",
)
# Relatable product-quality text for filterable demo queries.
_RETAIL_PRODUCT_FEEDBACK = (
    "zipper is broken",
    "sizing runs incredibly small",
    "sole separated after one wear",
    "stitching came loose",
    "barcode label is unreadable",
)
# Diagnostic error strings; a `WHERE error_message LIKE '%legacy_crm_db%'`
# demo returns rows because ``system_error`` sessions always carry one.
_RETAIL_LEGACY_CRM_ERRORS = (
    "legacy_crm_db timeout while retrieving order history",
    "legacy_crm_db returned stale customer profile",
)
_RETAIL_MODEL = "gemini-2.0-flash"
_RETAIL_MODEL_VERSION = "gemini-2.0-flash-001"
# Per-row timestamp step. The largest retail session emits ~32 rows, so the
# intra-session span is ~16s -- comfortably inside _MAX_SESSION_SPAN (60s),
# keeping ``max(timestamp) <= now``. ``latency_ms`` is reported independently.
_RETAIL_STEP = timedelta(seconds=0.5)


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
    attributes: Optional[dict] = None,
    latency_ms: Optional[dict] = None,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    status: str = "ok",
    error_message: Optional[str] = None,
    is_truncated: bool = False,
) -> dict:
  """Build one agent_events row.

  ``content``/``attributes``/``latency_ms`` are accepted as Python objects and
  serialized with ``json.dumps`` to preserve the string-valued JSON-column
  shape (and byte-stability). ``invocation_id`` then ``span_id`` consume the
  RNG in that order; passing an explicit ``span_id`` skips the RNG draw, so
  callers that leave it ``None`` keep the legacy consumption order intact.
  """
  invocation_id = _hex(rng, 32)
  span = _hex(rng, 16) if span_id is None else span_id
  return {
      "timestamp": ts.isoformat(),
      "event_type": event_type,
      "agent": agent,
      "session_id": session_id,
      "invocation_id": invocation_id,
      "user_id": user_id,
      # One trace per session in the demo corpus; trace_id mirrors session_id.
      "trace_id": session_id,
      "span_id": span,
      "parent_span_id": parent_span_id,
      "status": status,
      "error_message": error_message,
      "is_truncated": is_truncated,
      "content": json.dumps(content),
      "attributes": "{}" if attributes is None else json.dumps(attributes),
      "latency_ms": "{}" if latency_ms is None else json.dumps(latency_ms),
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


def _retail_outcome_allocation(sessions: int) -> dict[str, int]:
  """Even, deterministic split of ``sessions`` across the six retail outcomes.

  The remainder is distributed to the first buckets. All six buckets are
  non-empty once ``sessions >= len(_RETAIL_OUTCOMES)`` (true for the
  100-session default); smaller runs leave trailing buckets at zero but the
  counts always sum to ``sessions``.
  """
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  base, extra = divmod(sessions, len(_RETAIL_OUTCOMES))
  return {
      name: base + (1 if i < extra else 0)
      for i, name in enumerate(_RETAIL_OUTCOMES)
  }


def _retail_usage(rng: random.Random, lo: int, hi: int) -> dict[str, int]:
  """Synthetic token usage with ``total == prompt + completion`` (both > 0)."""
  total = rng.randint(lo, hi)
  completion = rng.randint(max(1, total // 10), max(2, total // 2))
  return {
      "prompt": total - completion,
      "completion": completion,
      "total": total,
  }


def _retail_llm_latency(rng: random.Random) -> dict[str, int]:
  """LLM latency with ``0 < time_to_first_token_ms <= total_ms``."""
  total = rng.randint(800, 7500)
  ttft = rng.randint(150, min(900, total))
  return {"total_ms": total, "time_to_first_token_ms": ttft}


def _retail_returns_session(
    rng: random.Random, start: datetime, outcome: str, feedback: str
) -> list[dict]:
  """One retail return/refund/exchange case as a single shared trace.

  All rows share ``session_id``/``trace_id``; the three retail agents are
  distinguished by ``agent`` and ``parent_span_id`` lineage under the
  invocation span. Exactly one terminal ``AGENT_COMPLETED`` is emitted (the
  intake agent's final completion); ``system_error`` cases mark it
  ``status="error"`` with a ``legacy_crm_db`` message and model the mid-trace
  failure as a non-terminal ``TOOL_ERROR`` -- never an orphan/no-terminal
  session. ``latency_ms`` is a reported metric, independent of the small
  per-row timestamp offsets.
  """
  intake, fraud, quality = _RETAIL_AGENTS
  session_id = f"sess-{_hex(rng, 8)}"
  user_id = f"cust-{_hex(rng, 6)}"
  inv_span = _hex(rng, 16)
  intake_span = _hex(rng, 16)
  fraud_span = _hex(rng, 16)
  quality_span = _hex(rng, 16)
  is_error = outcome == "system_error"
  rows: list[dict] = []
  step = 0

  def add(
      event_type: str,
      content: dict,
      *,
      agent: str,
      parent: Optional[str],
      span: Optional[str] = None,
      attributes: Optional[dict] = None,
      latency: Optional[dict] = None,
      status: str = "ok",
      error_message: Optional[str] = None,
  ) -> None:
    nonlocal step
    rows.append(
        _row(
            rng,
            event_type,
            session_id,
            content,
            start + step * _RETAIL_STEP,
            agent=agent,
            user_id=user_id,
            attributes=attributes,
            latency_ms=latency,
            span_id=span,
            parent_span_id=parent,
            status=status,
            error_message=error_message,
        )
    )
    step += 1

  def llm_call(agent, agent_span, lo, hi, prompt, response, tools):
    add(
        "LLM_REQUEST",
        {"messages": [{"role": "user", "content": prompt}]},
        agent=agent,
        parent=agent_span,
        attributes={
            "model": _RETAIL_MODEL,
            "llm_config": {"temperature": 0.2, "max_output_tokens": 1024},
            "tools": tools,
        },
    )
    usage = _retail_usage(rng, lo, hi)
    add(
        "LLM_RESPONSE",
        {"response": response, "usage": usage},
        agent=agent,
        parent=agent_span,
        attributes={
            "model_version": _RETAIL_MODEL_VERSION,
            "usage_metadata": {
                "prompt_token_count": usage["prompt"],
                "candidates_token_count": usage["completion"],
                "total_token_count": usage["total"],
            },
        },
        latency=_retail_llm_latency(rng),
    )

  def tool_call(agent, agent_span, name, args, result, *, error=None):
    add(
        "TOOL_STARTING",
        {"tool": name, "args": args},
        agent=agent,
        parent=agent_span,
    )
    if error is not None:
      add(
          "TOOL_ERROR",
          {"tool": name, "args": args},
          agent=agent,
          parent=agent_span,
          latency={"total_ms": rng.randint(20, 1200)},
          status="error",
          error_message=error,
      )
    else:
      add(
          "TOOL_COMPLETED",
          {"tool": name, "result": result},
          agent=agent,
          parent=agent_span,
          latency={"total_ms": rng.randint(20, 1200)},
      )

  add(
      "INVOCATION_STARTING",
      {"returns_case": outcome},
      agent=intake,
      parent=None,
      span=inv_span,
  )

  # --- intake & triage agent ---
  add(
      "AGENT_STARTING",
      {
          "task": "intake_and_triage",
          "text_summary": (
              "Authenticate the customer, look up order history, and triage"
              " the return request."
          ),
      },
      agent=intake,
      parent=inv_span,
      span=intake_span,
  )
  llm_call(
      intake,
      intake_span,
      800,
      2500,
      "Customer wants to return an item; classify the request.",
      {"intent": "return", "needs_photo": True},
      [
          "authenticate_customer",
          "lookup_order_history",
          "classify_return_reason",
          "inspect_item_photo",
      ],
  )
  tool_call(
      intake,
      intake_span,
      "authenticate_customer",
      {"customer_id": user_id},
      {"verified": True},
  )
  if is_error:
    tool_call(
        intake,
        intake_span,
        "lookup_order_history",
        {"customer_id": user_id},
        None,
        error=rng.choice(_RETAIL_LEGACY_CRM_ERRORS),
    )
  else:
    tool_call(
        intake,
        intake_span,
        "lookup_order_history",
        {"customer_id": user_id},
        {"order_id": f"ord-{_hex(rng, 6)}", "items": rng.randint(1, 4)},
    )
  tool_call(
      intake,
      intake_span,
      "classify_return_reason",
      {"customer_text": feedback},
      {"reason": "defect"},
  )
  tool_call(
      intake,
      intake_span,
      "inspect_item_photo",
      {"customer_text": feedback},
      {"condition": "used", "notes": feedback},
  )

  # --- fraud & abuse agent ---
  add(
      "AGENT_STARTING",
      {
          "task": "fraud_and_abuse",
          "text_summary": (
              "Assess return fraud and abuse risk from the customer's"
              " return-to-purchase history and cross-account patterns."
          ),
      },
      agent=fraud,
      parent=inv_span,
      span=fraud_span,
  )
  llm_call(
      fraud,
      fraud_span,
      300,
      1200,
      "Assess return fraud / abuse risk for this customer.",
      {"risk": "high" if outcome == "fraud_rejected" else "low"},
      ["compute_return_ratio", "cross_account_pattern_match", "score_risk"],
  )
  tool_call(
      fraud,
      fraud_span,
      "compute_return_ratio",
      {"customer_id": user_id},
      {"ratio": round(rng.uniform(0.0, 0.9), 2)},
  )
  tool_call(
      fraud,
      fraud_span,
      "cross_account_pattern_match",
      {"customer_id": user_id},
      {"matches": rng.randint(0, 3)},
  )
  tool_call(
      fraud,
      fraud_span,
      "score_risk",
      {"customer_id": user_id},
      {
          "score": round(rng.uniform(0.0, 1.0), 2),
          "decision": "reject" if outcome == "fraud_rejected" else "allow",
      },
  )

  # --- product quality & defect agent (skipped for clear low-risk paths) ---
  if outcome not in {"exchange_offered", "fraud_rejected"}:
    add(
        "AGENT_STARTING",
        {
            "task": "quality_and_defect",
            "text_summary": (
                "Categorize the product defect and extract quality signals"
                " from the customer's description."
            ),
        },
        agent=quality,
        parent=inv_span,
        span=quality_span,
    )
    llm_call(
        quality,
        quality_span,
        300,
        1200,
        f"Categorize the defect described as: {feedback}",
        {"defect_bucket": "hardware", "severity": "high"},
        ["categorize_defect", "extract_quality_signal"],
    )
    tool_call(
        quality,
        quality_span,
        "categorize_defect",
        {"customer_text": feedback},
        {"bucket": "hardware"},
    )
    tool_call(
        quality,
        quality_span,
        "extract_quality_signal",
        {"customer_text": feedback},
        {"signal": feedback, "confidence": round(rng.uniform(0.5, 0.99), 2)},
    )

  # --- intake composes the customer-facing resolution ---
  llm_call(
      intake,
      intake_span,
      200,
      700,
      "Compose the final customer-facing resolution.",
      {"resolution": outcome},
      [],
  )

  # Exactly one terminal AGENT_COMPLETED per session (the intake agent).
  if is_error:
    add(
        "AGENT_COMPLETED",
        {"final": True, "outcome": outcome},
        agent=intake,
        parent=inv_span,
        latency={"total_ms": rng.randint(3000, 20000)},
        status="error",
        error_message=rng.choice(_RETAIL_LEGACY_CRM_ERRORS),
    )
  else:
    add(
        "AGENT_COMPLETED",
        {"final": True, "outcome": outcome},
        agent=intake,
        parent=inv_span,
        latency={"total_ms": rng.randint(3000, 20000)},
    )
  add(
      "INVOCATION_COMPLETED",
      {"outcome": outcome},
      agent=intake,
      parent=None,
  )
  return rows


def build_retail_returns_corpus(
    rng: random.Random, now: datetime, sessions: int
) -> tuple[list[dict], dict[str, int]]:
  """Corpus builder for ``retail-returns`` (see issue #313).

  Emits a multi-agent refund/exchange trace per session with LLM token/latency
  telemetry, multi-day spread over ``[now - 72h, now - _MAX_SESSION_SPAN]``,
  and deterministic outcome buckets. Supports any ``sessions >= 1``; full
  six-bucket / three-agent / error / product-feedback coverage is guaranteed
  in the 100-session default. Returns ``(rows, session_outcome_counts)``.
  """
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  counts = _retail_outcome_allocation(sessions)
  outcomes: list[str] = []
  for name in _RETAIL_OUTCOMES:
    outcomes.extend([name] * counts[name])
  rng.shuffle(outcomes)

  window_start = now - _REALISTIC_WINDOW
  window_end = now - _MAX_SESSION_SPAN
  slot = (window_end - window_start).total_seconds() / sessions
  starts = [
      window_start + timedelta(seconds=i * slot + rng.uniform(0, slot))
      for i in range(sessions)
  ]
  feedback = _shuffled_cycle(rng, _RETAIL_PRODUCT_FEEDBACK, sessions)

  rows: list[dict] = []
  for i in range(sessions):
    rows.extend(
        _retail_returns_session(rng, starts[i], outcomes[i], feedback[i])
    )
  return rows, counts


# scenario -> corpus builder ``(rng, now, sessions) -> (rows, outcome_counts)``
_SCENARIO_BUILDERS = {
    Scenario.DECISION: _build_decision_corpus,
    Scenario.DECISION_REALISTIC: build_realistic_corpus,
    Scenario.RETAIL_RETURNS: build_retail_returns_corpus,
}
assert set(_SCENARIO_BUILDERS) == set(Scenario), (
    "every Scenario needs a builder; missing: "
    f"{set(Scenario) - set(_SCENARIO_BUILDERS)}"
)

_SCENARIO_DEFAULT_SESSIONS = {
    Scenario.DECISION: 5,
    Scenario.DECISION_REALISTIC: 100,
    Scenario.RETAIL_RETURNS: 100,
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
  ``decision-realistic``, 100 for ``retail-returns``. Pass an explicit value
  to override.
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
