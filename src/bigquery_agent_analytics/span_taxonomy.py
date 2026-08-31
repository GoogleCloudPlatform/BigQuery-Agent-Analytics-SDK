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

"""Span-level G1 attribution over native ``agent_events`` rows (#466).

The localization layer of the AgentForensics RFC (#435 Phase 2): given the
native ``agent_events``-shaped rows of one *failed* session, attach each
G1-frozen failure category the session tripped to the span where the
failure is observable, as ``(trace_id, span_id, failure_category,
evidence, confidence)`` rows.

Contracts honored:

* **Denominator.** Session-level ``failed_sessions`` + G1
  (``evalbench.classify_sessions`` / ``failure_taxonomy``) stays the
  denominator. This module never classifies a session: it takes the
  landed three-flag verdict as input and only *localizes* the categories
  that verdict already tripped. A session no flag tripped gets no span
  labels.
* **Taxonomy.** Frozen names only (``failure_taxonomy.py``, taxonomy
  v0.1.0, ``g1_frozen: True``). ``SpanFailureLabel`` rejects any category
  outside ``FROZEN_CATEGORY_NAMES`` at construction — no fourth string
  can be emitted here.
* **No synthetic span identifiers.** Every emitted ``span_id`` is the
  real native ``span_id`` of an input ``agent_events`` row. The silence
  case is a *gap marker anchored to a real span*: the label targets the
  last existing span with ``target_kind="gap_after_span"`` and evidence
  stating that no subsequent event (no ``TOOL_STARTING``, no
  ``AGENT_COMPLETED``) occurred. A row carrying no ``span_id`` fails
  closed with ``ValueError`` rather than inventing an id.
* **Identity.** ``eval_id`` is the frozen first-8-with-full-id-on-collision
  scenario id (``native_events._native_scenario_ids``), so span labels
  join the same ``failed_sessions`` / G1 rows the session-level contract
  publishes.
* **#429 reuse.** The turn coordinate is the #429 turn-tagging /
  ``sub_trajectories`` one: turns are anchored at ``USER_MESSAGE_RECEIVED``
  events, and ``turn_index`` addresses the same windows the
  ``start_turn`` / ``end_turn`` fields of ``sub_trajectories`` address.
  No parallel span model is introduced: the inputs are the same
  ``agent_events`` rows ``trace.Span.from_bigquery_row`` reads.
* **Mechanical, offline.** Everything is pure and deterministic: no
  BigQuery, no LLM/judge, no network, and nothing starts the six-week
  clock. ``confidence`` is ``MECHANICAL_CONFIDENCE`` (1.0) because each
  evidence string states a checkable fact of the event stream, not a
  judged probability; sub-1.0 confidences are reserved for the labeler /
  judge study of the plan of record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import dataclasses
from typing import Any, Optional

from .evalbench import _parse_timestamp
from .evalbench import _usable_text
from .evalbench import classify_sessions
from .evalbench import EvalScorePolicy
from .failure_taxonomy import categorize_failed_session
from .failure_taxonomy import FROZEN_CATEGORY_NAMES
from .native_events import _COMPLETION_EVENT
from .native_events import _native_scenario_ids
from .native_events import _PROMPT_EVENT
from .native_events import _tool_names
from .native_events import _TOOL_START_EVENT
from .native_events import NativeAgentEventsRun

# Deterministic labels carry full confidence: the evidence is a checkable
# fact of the event stream (an ERROR status, an absent event), not a judged
# probability. Sub-1.0 values are reserved for the labeler/judge study.
MECHANICAL_CONFIDENCE = 1.0

# The label targets the span itself (the row carries the failure marker,
# e.g. status=ERROR) ...
TARGET_SPAN = "span"
# ... or the *gap after* the span: the trace went silent after this real
# span. The anchor stays a real native span_id; only the evidence says the
# failure is the absence of anything following it.
TARGET_GAP_AFTER_SPAN = "gap_after_span"


@dataclasses.dataclass(frozen=True)
class SpanFailureLabel:
  """One G1-frozen category localized to one real native span.

  ``as_tuple()`` is the RFC #435 Phase 2 shape ``(trace_id, span_id,
  failure_category, evidence, confidence)``; the remaining fields keep the
  label joinable (``session_id`` / ``eval_id`` per the frozen first-8
  identity rule) and inspectable (``target_kind``, the #429 turn
  coordinate ``turn_index``).
  """

  session_id: str
  eval_id: str
  trace_id: Optional[str]
  span_id: str
  failure_category: str
  evidence: str
  confidence: float
  target_kind: str
  turn_index: int

  def __post_init__(self) -> None:
    if self.failure_category not in FROZEN_CATEGORY_NAMES:
      raise ValueError(
          f"failure_category {self.failure_category!r} is not in the"
          " G1-frozen taxonomy v0.1.0; the freeze is unconditional"
          " (defer new names to a versioned taxonomy revision)"
      )
    if self.target_kind not in (TARGET_SPAN, TARGET_GAP_AFTER_SPAN):
      raise ValueError(
          f"target_kind must be {TARGET_SPAN!r} or"
          f" {TARGET_GAP_AFTER_SPAN!r}, got {self.target_kind!r}"
      )
    if not isinstance(self.span_id, str) or not self.span_id:
      raise ValueError(
          "span_id must be a non-empty real native span id; span labels"
          " never invent span identifiers"
      )

  def as_tuple(
      self,
  ) -> tuple[Optional[str], str, str, str, float]:
    """The RFC tuple: (trace_id, span_id, failure_category, evidence, confidence)."""
    return (
        self.trace_id,
        self.span_id,
        self.failure_category,
        self.evidence,
        self.confidence,
    )


def label_failed_session_spans(
    session_events: Sequence[Mapping[str, Any]],
    verdict: Any,
    *,
    eval_id: Optional[str] = None,
    gold_events: Sequence[Mapping[str, Any]] = (),
) -> tuple[SpanFailureLabel, ...]:
  """Localize one failed session's G1 categories onto a real span.

  ``session_events`` are the raw native ``agent_events``-shaped rows of
  exactly one session (the *source* rows, not the published snapshot rows
  — the publish-time ERROR prompt marker is a snapshot artifact, not a
  failure point). ``verdict`` is the landed three-flag contract
  (``SessionVerdict`` or a mapping with ``process_failed`` /
  ``missing_completion`` / ``score_failed``); its categories come from the
  unchanged ``failure_taxonomy.categorize_failed_session``, so the
  session-level denominator is read, never recomputed. ``gold_events``
  optionally holds a completed sibling's rows for missing-tool evidence
  (same role as ``native_events.native_next_action``).

  Target selection is mechanical: the first raw ``status == "ERROR"`` row
  when one exists (``target_kind="span"``), otherwise the last existing
  span as the silence boundary (``target_kind="gap_after_span"``). Every
  tripped category yields one label on that target, in frozen order.

  Raises:
    ValueError: rows span several sessions, the verdict tripped a category
      but the target row has no real ``span_id`` (no synthetic ids), or no
      joinable ``eval_id`` is available.
  """
  categories = categorize_failed_session(verdict)
  if not categories:
    return ()
  ordered = _ordered_single_session(session_events)
  if not ordered:
    raise ValueError(
        "cannot localize a failed session with no agent_events rows"
    )
  resolved_eval_id = eval_id or _verdict_field(verdict, "scenario_id")
  if not resolved_eval_id:
    raise ValueError(
        "eval_id is required (frozen first-8 identity, full session_id on"
        " collision) so span labels stay joinable to failed_sessions"
    )
  session_id = _usable_text(ordered[0].get("session_id")) or _verdict_field(
      verdict, "session_id"
  )
  if not session_id:
    raise ValueError("agent_events rows carry no session_id")

  error_index = next(
      (
          index
          for index, event in enumerate(ordered)
          if event.get("status") == "ERROR"
      ),
      None,
  )
  if error_index is not None:
    target_index, target_kind = error_index, TARGET_SPAN
  else:
    target_index, target_kind = len(ordered) - 1, TARGET_GAP_AFTER_SPAN
  target = ordered[target_index]
  span_id = _usable_text(target.get("span_id"))
  if span_id is None:
    raise ValueError(
        f"the target {target.get('event_type')!r} row of session"
        f" {session_id!r} has no span_id; refusing to invent a synthetic"
        " span identifier (#466)"
    )
  trace_id = _usable_text(target.get("trace_id"))
  turn_index = _turn_index(ordered, target_index)

  evidence_by_category = _evidence(
      ordered,
      target_index=target_index,
      target_kind=target_kind,
      gold_events=gold_events,
      verdict=verdict,
  )
  return tuple(
      SpanFailureLabel(
          session_id=session_id,
          eval_id=resolved_eval_id,
          trace_id=trace_id,
          span_id=span_id,
          failure_category=category,
          evidence=evidence_by_category[category],
          confidence=MECHANICAL_CONFIDENCE,
          target_kind=target_kind,
          turn_index=turn_index,
      )
      for category in categories
  )


def label_native_run(
    run: NativeAgentEventsRun,
    *,
    policy: Optional[EvalScorePolicy] = None,
) -> tuple[SpanFailureLabel, ...]:
  """Span labels for every failed session of one native run, offline.

  The session-level denominator is computed exactly as the landed contract
  does — ``classify_sessions`` over the run's mapped event rows and its
  deterministic ``goal_completion`` score facts — and each failed verdict
  is localized onto that session's *raw* source rows. Completed sibling
  sessions of the same run supply the missing-tool evidence pool. Pure and
  deterministic: no BigQuery client, no writes, and the clock stays off.
  """
  kept, _ = run._kept_and_skipped()  # pylint: disable=protected-access
  if not kept:
    return ()
  scenario_ids = _native_scenario_ids(tuple(sorted(kept)))
  score_rows = [
      dict(fact)
      for fact, _ in run._score_facts_with_prompts()  # pylint: disable=protected-access
  ]
  verdicts = classify_sessions(
      run.to_agent_event_rows(), score_rows, policy or EvalScorePolicy()
  )
  gold_pool: list[dict[str, Any]] = []
  for session_id in sorted(kept):
    events = kept[session_id]
    if any(e.get("event_type") == _COMPLETION_EVENT for e in events):
      gold_pool.extend(events)
  labels: list[SpanFailureLabel] = []
  for verdict in verdicts:
    if not verdict.failed:
      continue
    labels.extend(
        label_failed_session_spans(
            kept[verdict.session_id],
            verdict,
            eval_id=scenario_ids[verdict.session_id],
            gold_events=gold_pool,
        )
    )
  return tuple(labels)


def _evidence(
    ordered: list[Mapping[str, Any]],
    *,
    target_index: int,
    target_kind: str,
    gold_events: Sequence[Mapping[str, Any]],
    verdict: Any,
) -> dict[str, str]:
  """Per-category evidence: checkable facts of the event stream."""
  target = ordered[target_index]
  descriptor = (
      f"{target.get('event_type')} span {_usable_text(target.get('span_id'))}"
      f" at {_timestamp_text(target)}"
  )
  called = _tool_names(ordered)
  missing_tools = sorted(_tool_names(gold_events) - called)
  silence = (
      f" the trace goes silent after {descriptor}: no subsequent"
      " agent_events row exists"
  )

  if target_kind == TARGET_SPAN:
    error_message = _usable_text(target.get("error_message"))
    error_suffix = f": {error_message}" if error_message else ""
    tool_blockers = (
        f"process_failed: {descriptor} logged status ERROR{error_suffix}"
    )
  else:
    never_called = (
        f" and {', '.join(missing_tools)} was never called (the completed"
        " sibling called it)"
        if missing_tools
        else " and no tool was ever started"
    )
    tool_blockers = (
        f"process_failed: no {_TOOL_START_EVENT} event follows"
        f" {descriptor}{never_called};{silence}"
    )

  failing_scores = _verdict_field(verdict, "failing_scores")
  if isinstance(failing_scores, Mapping) and failing_scores:
    gate = ", ".join(
        f"{name}={'missing' if value is None else value}"
        for name, value in sorted(failing_scores.items())
    )
    gate_text = f" ({gate})"
  else:
    gate_text = ""
  return {
      "finalization": (
          f"missing_completion: no {_COMPLETION_EVENT} event follows"
          f" {descriptor};{silence}"
      ),
      "tool blockers": tool_blockers,
      "task/planning": (
          f"score_failed: the session's score gate failed{gate_text}; no"
          f" further plan step follows {descriptor}"
      ),
  }


def _ordered_single_session(
    session_events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
  """Validate one-session input and order it by timestamp when possible."""
  rows = list(session_events)
  session_ids = {
      _usable_text(row.get("session_id"))
      for row in rows
      if _usable_text(row.get("session_id")) is not None
  }
  if len(session_ids) > 1:
    raise ValueError(
        "span localization is per-session; got rows for sessions"
        f" {sorted(session_ids)!r}"
    )
  keyed = []
  for index, row in enumerate(rows):
    timestamp = _parse_timestamp(row.get("timestamp"))
    if timestamp is None:
      return rows  # Preserve caller order when timestamps are unusable.
    keyed.append((timestamp, index, row))
  return [row for _, _, row in sorted(keyed, key=lambda item: item[:2])]


def _turn_index(ordered: list[Mapping[str, Any]], target_index: int) -> int:
  """The #429 turn coordinate: USER_MESSAGE_RECEIVED-anchored turn number.

  The turn containing the target span, counted the way ``start_turn`` /
  ``end_turn`` of the #429 ``sub_trajectories`` count user turns. A span
  before any user message belongs to turn 0.
  """
  prompts = sum(
      1
      for event in ordered[: target_index + 1]
      if event.get("event_type") == _PROMPT_EVENT
  )
  return max(0, prompts - 1)


def _timestamp_text(row: Mapping[str, Any]) -> str:
  timestamp = _parse_timestamp(row.get("timestamp"))
  return timestamp.isoformat() if timestamp is not None else "<no timestamp>"


def _verdict_field(verdict: Any, name: str) -> Any:
  if isinstance(verdict, Mapping):
    return verdict.get(name)
  return getattr(verdict, name, None)
