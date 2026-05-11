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

"""Revalidation harness for compiled structured extractors
(issue #75 Milestone C2.d).

PR 4c's :func:`measure_compile` proves a single compile-and-
compare pass. C2.c.2's orchestrator wires the compiled path
into the runtime. **This module turns "works in tests" into
"keeps proving itself after rollout"** — a batch-mode runner
that takes a corpus of events, drives each through
:func:`run_with_fallback` with the matching compiled and
reference extractor, and aggregates the per-event
:class:`FallbackOutcome` decisions into a structured report.

The report's job is to surface drift. C2.b's decision tree
(``compiled_unchanged`` / ``compiled_filtered`` /
``fallback_for_event``) is already the right vocabulary for
"how often did the compiled extractor hold up versus need
adjustment." Revalidation runs that decision tree against a
sample of real events and aggregates the counts — per
event_type and overall — so operators can see whether the
compiled bundle's behavior matches the handwritten reference
that gated its compile.

Out of scope (this PR keeps the core in-memory; persistence
and orchestration follow once the report shape is stable):

* **Scheduled / cron orchestration.** The revalidation harness
  is a pure function over events. Wiring it to a Cloud
  Scheduler / cron / GitHub Actions schedule is the caller's
  concern.
* **Result persistence to BigQuery / disk.** The report
  dataclass has ``to_json`` so callers can write it wherever
  they want, but the harness doesn't decide where.
* **CLI / one-shot binary.** A ``bqaa-revalidate-extractors``
  CLI is a natural follow-up once the report shape lands.
* **Sampling strategy.** The caller decides which events to
  revalidate — random sample, time window, session subset,
  etc. The harness consumes the events the caller hands it.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from typing import Any, Callable, Optional

from ..structured_extraction import StructuredExtractionResult
from .runtime_fallback import FallbackOutcome
from .runtime_fallback import run_with_fallback

# Cap on per-event_type sample-divergence entries in the report.
# Hard-coded for C2.d; if real reports grow noisier than this,
# C2.d.1 can expose it.
_DEFAULT_SAMPLE_DIVERGENCE_CAP = 10


@dataclasses.dataclass(frozen=True)
class EventTypeCounts:
  """Per-event-type aggregation of revalidation outcomes."""

  event_type: str
  total: int
  compiled_unchanged: int
  compiled_filtered: int
  fallback_for_event: int
  # Subset of ``fallback_for_event`` where the compiled
  # extractor crashed (``compiled_exception`` is set on the
  # outcome). Surfaced separately so an operator can see the
  # *kind* of fallback — bundle bug vs. ontology drift vs.
  # validator rejection — without parsing per-event outcomes.
  compiled_exceptions: int

  @property
  def compiled_unchanged_rate(self) -> float:
    return self.compiled_unchanged / self.total if self.total else 0.0

  @property
  def compiled_filtered_rate(self) -> float:
    return self.compiled_filtered / self.total if self.total else 0.0

  @property
  def fallback_for_event_rate(self) -> float:
    return self.fallback_for_event / self.total if self.total else 0.0

  @property
  def exception_rate(self) -> float:
    return self.compiled_exceptions / self.total if self.total else 0.0


@dataclasses.dataclass(frozen=True)
class RevalidationReport:
  """One revalidation run's report.

  Fields:

  * ``counts_by_event_type`` — per-event-type
    :class:`EventTypeCounts`. Keyed by event_type. Sorted by
    name in the JSON serialization for deterministic diffs.
  * ``total_events`` — sum of all per-event-type ``total``s,
    *excluding* events whose event_type wasn't in
    ``compiled_extractors`` (those events have no compiled
    path to revalidate; see ``skipped_events``).
  * ``skipped_events`` — events whose event_type isn't covered
    by any compiled extractor. Revalidation only makes sense
    when there's something compiled to validate; these are
    reported for visibility but excluded from the rate
    denominators.
  * Aggregate counts (``total_compiled_unchanged`` etc.) — sum
    of per-event-type counts. ``compiled_unchanged_rate`` is
    the headline KPI: fraction of revalidated events where the
    compiled extractor's output validated clean.
  * ``sample_divergences`` — capped at
    ``_DEFAULT_SAMPLE_DIVERGENCE_CAP`` total. Each entry is a
    short, stable string identifying a non-``compiled_unchanged``
    outcome so an operator can drill in. Format:
    ``\"<event_type>: <decision> [exception=...|dropped_node_ids=...]\"``.
  * Audit fields (``started_at`` / ``finished_at``).
  """

  counts_by_event_type: dict[str, EventTypeCounts]
  total_events: int
  skipped_events: int
  total_compiled_unchanged: int
  total_compiled_filtered: int
  total_fallback_for_event: int
  total_compiled_exceptions: int
  sample_divergences: tuple[str, ...]
  started_at: str
  finished_at: str

  @property
  def compiled_unchanged_rate(self) -> float:
    return (
        self.total_compiled_unchanged / self.total_events
        if self.total_events
        else 0.0
    )

  @property
  def compiled_filtered_rate(self) -> float:
    return (
        self.total_compiled_filtered / self.total_events
        if self.total_events
        else 0.0
    )

  @property
  def fallback_for_event_rate(self) -> float:
    return (
        self.total_fallback_for_event / self.total_events
        if self.total_events
        else 0.0
    )

  @property
  def exception_rate(self) -> float:
    return (
        self.total_compiled_exceptions / self.total_events
        if self.total_events
        else 0.0
    )

  def to_json(self) -> str:
    """Serialize the report to a stable JSON string. Useful for
    persistence (writing to disk, BigQuery, telemetry pipelines)
    and for diffing reports across revalidation runs."""
    payload = {
        "counts_by_event_type": {
            et: dataclasses.asdict(counts)
            for et, counts in sorted(self.counts_by_event_type.items())
        },
        "total_events": self.total_events,
        "skipped_events": self.skipped_events,
        "total_compiled_unchanged": self.total_compiled_unchanged,
        "total_compiled_filtered": self.total_compiled_filtered,
        "total_fallback_for_event": self.total_fallback_for_event,
        "total_compiled_exceptions": self.total_compiled_exceptions,
        "sample_divergences": list(self.sample_divergences),
        "started_at": self.started_at,
        "finished_at": self.finished_at,
    }
    return json.dumps(payload, sort_keys=True, indent=2)


@dataclasses.dataclass(frozen=True)
class RevalidationThresholds:
  """Optional thresholds for :func:`check_thresholds`.

  Every field is ``None`` by default — meaning "no threshold on
  this dimension." Set the ones the caller cares about; leave
  the rest. Rates are fractions in ``[0, 1]``.
  """

  min_compiled_unchanged_rate: Optional[float] = None
  max_compiled_filtered_rate: Optional[float] = None
  max_fallback_for_event_rate: Optional[float] = None
  max_exception_rate: Optional[float] = None


@dataclasses.dataclass(frozen=True)
class ThresholdCheckResult:
  """Outcome of :func:`check_thresholds`."""

  ok: bool
  violations: tuple[str, ...]


def revalidate_compiled_extractors(
    *,
    events: list[dict],
    compiled_extractors: dict[str, Callable[..., StructuredExtractionResult]],
    reference_extractors: dict[str, Callable[..., StructuredExtractionResult]],
    resolved_graph: Any,
    spec: Any = None,
    sample_divergence_cap: int = _DEFAULT_SAMPLE_DIVERGENCE_CAP,
) -> RevalidationReport:
  """Run every event through ``run_with_fallback(compiled,
  reference)`` and aggregate the outcomes into a report.

  Args:
    events: Sample events to revalidate against. Typically a
      random / time-window sample drawn from production
      telemetry, but the caller decides — this function is
      sampling-agnostic.
    compiled_extractors: ``event_type → compiled callable``,
      typically ``discover_bundles(...).registry`` from C2.a.
      Events whose ``event_type`` isn't in this dict have no
      compiled path to revalidate and are counted in
      ``skipped_events``.
    reference_extractors: ``event_type → handwritten
      callable``. Must cover every event_type in
      ``compiled_extractors``; events whose event_type has a
      compiled entry but no reference entry are also skipped
      (the wrapper requires both).
    resolved_graph: Forwarded to :func:`run_with_fallback`.
    spec: Forwarded to each extractor's ``(event, spec)`` call.
    sample_divergence_cap: Maximum number of sample-divergence
      strings to include in the report. Defaults to 10.

  Returns:
    A :class:`RevalidationReport` aggregating per-event-type
    counts and rates.
  """
  started_at = _now_iso_utc()

  per_type_running: dict[str, _RunningCounts] = {}
  skipped_events = 0
  sample_divergences: list[str] = []

  for event in events:
    if not isinstance(event, dict):
      skipped_events += 1
      continue
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or not event_type:
      skipped_events += 1
      continue
    compiled = compiled_extractors.get(event_type)
    reference = reference_extractors.get(event_type)
    if compiled is None or reference is None:
      skipped_events += 1
      continue

    outcome = run_with_fallback(
        event=event,
        spec=spec,
        resolved_graph=resolved_graph,
        compiled_extractor=compiled,
        fallback_extractor=reference,
    )

    running = per_type_running.setdefault(event_type, _RunningCounts())
    running.total += 1
    if outcome.decision == "compiled_unchanged":
      running.compiled_unchanged += 1
    elif outcome.decision == "compiled_filtered":
      running.compiled_filtered += 1
      if len(sample_divergences) < sample_divergence_cap:
        sample_divergences.append(
            _summarize_outcome(event_type=event_type, outcome=outcome)
        )
    elif outcome.decision == "fallback_for_event":
      running.fallback_for_event += 1
      if outcome.compiled_exception is not None:
        running.compiled_exceptions += 1
      if len(sample_divergences) < sample_divergence_cap:
        sample_divergences.append(
            _summarize_outcome(event_type=event_type, outcome=outcome)
        )

  counts_by_event_type = {
      et: EventTypeCounts(
          event_type=et,
          total=r.total,
          compiled_unchanged=r.compiled_unchanged,
          compiled_filtered=r.compiled_filtered,
          fallback_for_event=r.fallback_for_event,
          compiled_exceptions=r.compiled_exceptions,
      )
      for et, r in per_type_running.items()
  }

  total_events = sum(c.total for c in counts_by_event_type.values())
  total_compiled_unchanged = sum(
      c.compiled_unchanged for c in counts_by_event_type.values()
  )
  total_compiled_filtered = sum(
      c.compiled_filtered for c in counts_by_event_type.values()
  )
  total_fallback_for_event = sum(
      c.fallback_for_event for c in counts_by_event_type.values()
  )
  total_compiled_exceptions = sum(
      c.compiled_exceptions for c in counts_by_event_type.values()
  )

  return RevalidationReport(
      counts_by_event_type=counts_by_event_type,
      total_events=total_events,
      skipped_events=skipped_events,
      total_compiled_unchanged=total_compiled_unchanged,
      total_compiled_filtered=total_compiled_filtered,
      total_fallback_for_event=total_fallback_for_event,
      total_compiled_exceptions=total_compiled_exceptions,
      sample_divergences=tuple(sample_divergences),
      started_at=started_at,
      finished_at=_now_iso_utc(),
  )


def check_thresholds(
    report: RevalidationReport,
    thresholds: RevalidationThresholds,
) -> ThresholdCheckResult:
  """Compare a :class:`RevalidationReport` against a set of
  rate thresholds.

  Returns ``ok=True`` iff every set threshold is satisfied.
  Unset thresholds (``None`` fields) are ignored. Violations
  are returned as human-readable strings naming the offending
  rate and the threshold it failed.

  This is a separate function (not baked into
  :func:`revalidate_compiled_extractors`) because the report
  shape is pure data and thresholds are a policy concern. The
  same report can be evaluated against different threshold
  sets — production gate vs. canary gate vs. nightly-trend
  gate.
  """
  violations: list[str] = []
  if thresholds.min_compiled_unchanged_rate is not None:
    if report.compiled_unchanged_rate < thresholds.min_compiled_unchanged_rate:
      violations.append(
          f"compiled_unchanged_rate {report.compiled_unchanged_rate:.4f} "
          f"< min {thresholds.min_compiled_unchanged_rate:.4f}"
      )
  if thresholds.max_compiled_filtered_rate is not None:
    if report.compiled_filtered_rate > thresholds.max_compiled_filtered_rate:
      violations.append(
          f"compiled_filtered_rate {report.compiled_filtered_rate:.4f} "
          f"> max {thresholds.max_compiled_filtered_rate:.4f}"
      )
  if thresholds.max_fallback_for_event_rate is not None:
    if report.fallback_for_event_rate > thresholds.max_fallback_for_event_rate:
      violations.append(
          f"fallback_for_event_rate {report.fallback_for_event_rate:.4f} "
          f"> max {thresholds.max_fallback_for_event_rate:.4f}"
      )
  if thresholds.max_exception_rate is not None:
    if report.exception_rate > thresholds.max_exception_rate:
      violations.append(
          f"exception_rate {report.exception_rate:.4f} "
          f"> max {thresholds.max_exception_rate:.4f}"
      )
  return ThresholdCheckResult(
      ok=not violations,
      violations=tuple(violations),
  )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


@dataclasses.dataclass
class _RunningCounts:
  """Mutable accumulator used while iterating events. Frozen
  :class:`EventTypeCounts` instances are materialized at the
  end of the run."""

  total: int = 0
  compiled_unchanged: int = 0
  compiled_filtered: int = 0
  fallback_for_event: int = 0
  compiled_exceptions: int = 0


def _summarize_outcome(*, event_type: str, outcome: FallbackOutcome) -> str:
  """One-line summary of a non-unchanged outcome. Truncated to
  keep sample lists scannable; full audit is on the outcome
  itself if the caller wants more."""
  if outcome.decision == "compiled_filtered":
    dropped = (
        f"nodes={list(outcome.dropped_node_ids)}"
        if outcome.dropped_node_ids
        else f"edges={list(outcome.dropped_edge_ids)}"
    )
    return f"{event_type}: compiled_filtered ({dropped})"
  if outcome.decision == "fallback_for_event":
    if outcome.compiled_exception is not None:
      return (
          f"{event_type}: fallback_for_event "
          f"(exception={outcome.compiled_exception})"
      )
    failure_codes = sorted({f.code for f in outcome.validation_failures})
    return (
        f"{event_type}: fallback_for_event "
        f"(validator_codes={failure_codes})"
    )
  return f"{event_type}: {outcome.decision}"


def _now_iso_utc() -> str:
  return (
      datetime.datetime.now(datetime.timezone.utc)
      .replace(microsecond=0)
      .isoformat()
      .replace("+00:00", "Z")
  )
