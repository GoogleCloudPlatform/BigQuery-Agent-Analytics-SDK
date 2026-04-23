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

"""Helpers for the scheduled streaming evaluation deployment surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import hashlib
import json
from typing import Any

from bigquery_agent_analytics import CodeEvaluator
from bigquery_agent_analytics import EvaluationReport
from bigquery_agent_analytics import serialize
from bigquery_agent_analytics import udf_kernels

STREAMING_EVALUATOR_PROFILE = "streaming_observability_v1"
STREAMING_PROCESSOR_NAME = STREAMING_EVALUATOR_PROFILE
STREAMING_RESULTS_TABLE = "streaming_evaluation_results"
STREAMING_STATE_TABLE = "_streaming_eval_state"
STREAMING_RUNS_TABLE = "_streaming_eval_runs"

DEFAULT_POLL_INTERVAL_MINUTES = 5
DEFAULT_OVERLAP_MINUTES = 15
DEFAULT_INITIAL_LOOKBACK_MINUTES = 30

TRIGGER_KIND_SESSION_TERMINAL = "session_terminal"
TRIGGER_KIND_ERROR_EVENT = "error_event"
_VALID_TRIGGER_KINDS = {
    TRIGGER_KIND_SESSION_TERMINAL,
    TRIGGER_KIND_ERROR_EVENT,
}

_LATENCY_THRESHOLD_MS = 5000.0
_MAX_ERROR_RATE = 0.1
_MAX_TURNS = 10


@dataclass(frozen=True)
class StreamingTrigger:
  """Normalized trigger row from the scheduled overlap scan."""

  session_id: str
  trace_id: str | None
  span_id: str | None
  event_type: str
  trigger_kind: str
  trigger_timestamp: datetime
  dedupe_key: str

  @property
  def is_final(self) -> bool:
    return self.trigger_kind == TRIGGER_KIND_SESSION_TERMINAL


def build_streaming_observability_evaluator() -> CodeEvaluator:
  """Build the fixed launch evaluator profile for streaming observability.

  Uses raw-budget gates (a session passes iff the observed metric is
  within the configured budget) for consistency with
  ``CodeEvaluator.latency`` / ``.error_rate`` / ``.turn_count``.
  Prior implementation used normalized scores with a 0.5 pass cutoff,
  which caused gates to fire at roughly half the configured budget.
  """

  def _score_latency(session_summary: dict[str, Any]) -> float:
    observed = session_summary.get("avg_latency_ms", 0) or 0
    return 1.0 if observed <= _LATENCY_THRESHOLD_MS else 0.0

  def _observed_error_rate(session_summary: dict[str, Any]) -> float:
    calls = session_summary.get("tool_calls", 0) or 0
    errors = session_summary.get("tool_errors", 0) or 0
    if calls <= 0:
      return 0.0
    return errors / calls

  def _score_error_rate(session_summary: dict[str, Any]) -> float:
    calls = session_summary.get("tool_calls", 0) or 0
    if calls <= 0:
      return 1.0
    return (
        1.0 if _observed_error_rate(session_summary) <= _MAX_ERROR_RATE else 0.0
    )

  def _score_turn_count(session_summary: dict[str, Any]) -> float:
    observed = session_summary.get("turn_count", 0) or 0
    return 1.0 if observed <= _MAX_TURNS else 0.0

  evaluator = CodeEvaluator(name=STREAMING_EVALUATOR_PROFILE)
  evaluator.add_metric(
      "latency",
      _score_latency,
      threshold=1.0,
      observed_key="avg_latency_ms",
      budget=_LATENCY_THRESHOLD_MS,
  )
  evaluator.add_metric(
      "error_rate",
      _score_error_rate,
      threshold=1.0,
      observed_fn=_observed_error_rate,
      budget=_MAX_ERROR_RATE,
  )
  evaluator.add_metric(
      "turn_count",
      _score_turn_count,
      threshold=1.0,
      observed_key="turn_count",
      budget=_MAX_TURNS,
  )
  return evaluator


def classify_trigger_kind(
    event_type: str | None,
    status: str | None = None,
    error_message: str | None = None,
) -> str | None:
  """Classify an event row into a launch trigger kind."""
  if event_type == "AGENT_COMPLETED":
    return TRIGGER_KIND_SESSION_TERMINAL
  if event_type == "TOOL_ERROR":
    return TRIGGER_KIND_ERROR_EVENT
  if status == "ERROR" and error_message is not None:
    return TRIGGER_KIND_ERROR_EVENT
  return None


def is_launch_trigger_row(
    event_type: str | None,
    status: str | None = None,
    error_message: str | None = None,
) -> bool:
  """Return ``True`` when a row qualifies for launch trigger handling."""
  return classify_trigger_kind(event_type, status, error_message) is not None


def build_trigger_dedupe_key(
    session_id: str,
    trace_id: str | None,
    span_id: str | None,
    event_type: str,
    trigger_timestamp: datetime | str,
) -> str:
  """Build a stable idempotency key for a trigger row."""
  timestamp = _coerce_timestamp(trigger_timestamp)
  timestamp_micros = int(timestamp.timestamp() * 1_000_000)
  raw = "|".join(
      [
          session_id or "",
          trace_id or "",
          span_id or "",
          event_type or "",
          str(timestamp_micros),
      ]
  )
  return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_trigger_payload(payload: dict[str, Any]) -> StreamingTrigger:
  """Parse a JSON payload into a validated ``StreamingTrigger``."""
  missing = [
      field
      for field in (
          "session_id",
          "event_type",
          "trigger_kind",
          "trigger_timestamp",
      )
      if not payload.get(field)
  ]
  if missing:
    raise ValueError(f"Missing required trigger fields: {', '.join(missing)}")

  trigger_kind = payload["trigger_kind"]
  if trigger_kind not in _VALID_TRIGGER_KINDS:
    raise ValueError(f"Unsupported trigger_kind: {trigger_kind}")

  trigger_timestamp = _coerce_timestamp(payload["trigger_timestamp"])
  dedupe_key = payload.get("dedupe_key") or build_trigger_dedupe_key(
      session_id=payload["session_id"],
      trace_id=payload.get("trace_id"),
      span_id=payload.get("span_id"),
      event_type=payload["event_type"],
      trigger_timestamp=trigger_timestamp,
  )

  return StreamingTrigger(
      session_id=payload["session_id"],
      trace_id=payload.get("trace_id"),
      span_id=payload.get("span_id"),
      event_type=payload["event_type"],
      trigger_kind=trigger_kind,
      trigger_timestamp=trigger_timestamp,
      dedupe_key=dedupe_key,
  )


def parse_trigger_row(row: Mapping[str, Any]) -> StreamingTrigger:
  """Parse a BigQuery row from the overlap scan into a ``StreamingTrigger``."""
  payload = dict(row)
  payload["trigger_timestamp"] = payload.get(
      "trigger_timestamp"
  ) or payload.get("timestamp")
  payload["trigger_kind"] = payload.get(
      "trigger_kind"
  ) or classify_trigger_kind(
      event_type=payload.get("event_type"),
      status=payload.get("status"),
      error_message=payload.get("error_message"),
  )
  return parse_trigger_payload(payload)


def serialize_streaming_result_row(
    trigger: StreamingTrigger,
    report: EvaluationReport,
    processed_at: datetime | None = None,
) -> dict[str, Any]:
  """Serialize one trigger execution to a fixed BigQuery row shape."""
  processed = processed_at or datetime.now(timezone.utc)
  session_score = report.session_scores[0] if report.session_scores else None
  aggregate_scores = (
      session_score.scores if session_score else report.aggregate_scores
  )
  details: dict[str, Any] = {}
  if report.details:
    details["report"] = report.details
  if session_score and session_score.details:
    details["session"] = session_score.details
  if session_score and session_score.llm_feedback:
    details["llm_feedback"] = session_score.llm_feedback

  passed = False
  if session_score is not None:
    passed = session_score.passed
  elif report.total_sessions > 0:
    passed = report.failed_sessions == 0

  return {
      "dedupe_key": trigger.dedupe_key,
      "session_id": trigger.session_id,
      "trace_id": trigger.trace_id,
      "span_id": trigger.span_id,
      "trigger_kind": trigger.trigger_kind,
      "trigger_event_type": trigger.event_type,
      "trigger_timestamp": trigger.trigger_timestamp,
      "is_final": trigger.is_final,
      "evaluator_profile": STREAMING_EVALUATOR_PROFILE,
      "passed": passed,
      "aggregate_scores_json": json.dumps(aggregate_scores, sort_keys=True),
      "details_json": json.dumps(details, sort_keys=True),
      "report_json": json.dumps(serialize(report), sort_keys=True),
      "processed_at": processed,
  }


def compute_scan_start(
    run_started_at: datetime | str,
    checkpoint_timestamp: datetime | str | None = None,
    overlap: timedelta | None = None,
    initial_lookback: timedelta | None = None,
) -> datetime:
  """Compute the lower bound for the next overlap scan."""
  run_started = _coerce_timestamp(run_started_at)
  overlap_window = overlap or timedelta(minutes=DEFAULT_OVERLAP_MINUTES)
  bootstrap_window = initial_lookback or timedelta(
      minutes=DEFAULT_INITIAL_LOOKBACK_MINUTES
  )

  if checkpoint_timestamp is None:
    return run_started - bootstrap_window

  checkpoint = _coerce_timestamp(checkpoint_timestamp)
  if checkpoint > run_started:
    checkpoint = run_started
  return checkpoint - overlap_window


def _coerce_timestamp(value: datetime | str) -> datetime:
  """Parse timestamps into UTC datetimes."""
  if isinstance(value, datetime):
    if value.tzinfo is None:
      return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

  normalized = value.strip()
  if normalized.endswith(" UTC"):
    normalized = normalized.removesuffix(" UTC") + "+00:00"
  if normalized.endswith("Z"):
    normalized = normalized[:-1] + "+00:00"

  try:
    parsed = datetime.fromisoformat(normalized)
  except ValueError as exc:
    raise ValueError(f"Invalid trigger_timestamp: {value}") from exc

  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return parsed.astimezone(timezone.utc)
