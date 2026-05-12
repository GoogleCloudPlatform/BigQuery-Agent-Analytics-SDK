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
:func:`run_with_fallback` plus a direct reference-extractor
call, and aggregates the per-event outcomes (decision +
agreement) into a structured report.

The report has **two orthogonal dimensions**, both load-bearing:

1. **Runtime decision** — what
   :func:`run_with_fallback` did on this event:
   ``compiled_unchanged`` / ``compiled_filtered`` /
   ``fallback_for_event``. This is C2.b's safety vocabulary
   ("did the schema validator accept the compiled output").
2. **Agreement against reference** — did the compiled
   extractor's output match the handwritten reference's
   output on this event? ``parity_match`` /
   ``parity_divergence`` / ``parity_not_checked`` (the last
   for ``fallback_for_event`` events, where the compiled
   output was discarded). This catches **schema-valid but
   semantically wrong** outputs — the case where the
   compiled extractor emits a node that survives the
   validator but disagrees with the reference (e.g. wrong
   property value). The schema-only check would silently
   call this ``compiled_unchanged``.

Both dimensions land in :class:`RevalidationReport`. The
report is JSON-serializable for persistence and gates via
:class:`RevalidationThresholds` + :func:`check_thresholds`.

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

import collections
import dataclasses
import datetime
import json
from typing import Any, Callable, Optional

from ..structured_extraction import StructuredExtractionResult
from .measurement import _compare_nodes
from .measurement import _compare_span_handling
from .measurement import _hashable
from .runtime_fallback import FallbackOutcome
from .runtime_fallback import run_with_fallback

# Cap on per-report sample-divergence entries. Two independent
# caps — one per dimension — so a run that's noisy on the
# decision side doesn't crowd out parity samples and vice versa.
# Hard-coded for C2.d; if real reports grow noisier than this,
# C2.d.1 can expose them.
_DEFAULT_SAMPLE_DIVERGENCE_CAP = 10


@dataclasses.dataclass(frozen=True)
class EventTypeCounts:
  """Per-event-type aggregation of revalidation outcomes.

  Covers both dimensions (runtime decision + agreement against
  reference) so a single ``EventTypeCounts`` row tells you
  both *what the runtime did* and *whether the output was
  right*.
  """

  event_type: str
  total: int
  # Runtime-decision counts (from run_with_fallback).
  compiled_unchanged: int
  compiled_filtered: int
  fallback_for_event: int
  # Subset of ``fallback_for_event`` where the compiled
  # extractor failed in a way that fingered the compiled
  # *path*, not the data: exceptions, wrong return type,
  # or malformed result internals. ``run_with_fallback``
  # captures all three via its ``compiled_exception`` audit
  # field; we surface the rollup so an operator can see the
  # *kind* of fallback — bundle bug vs. ontology drift vs.
  # validator rejection — without parsing per-event outcomes.
  compiled_path_faults: int
  # Agreement-against-reference counts. The new dimension
  # added in C2.d's P1 fix: schema-only validation can pass
  # while compiled output silently disagrees with the
  # reference; parity counts catch that.
  parity_matches: int
  parity_divergences: int
  parity_not_checked: int

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
  def compiled_path_fault_rate(self) -> float:
    return self.compiled_path_faults / self.total if self.total else 0.0

  @property
  def parity_match_rate(self) -> float:
    """Matches over *checked* events. Events whose parity was
    ``not_checked`` (the compiled output wasn't used) are
    excluded from the denominator — including them would
    conflate "compiled output never reached production" with
    "compiled output reached production and was wrong"."""
    checked = self.parity_matches + self.parity_divergences
    return self.parity_matches / checked if checked else 0.0


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
  * Runtime-decision aggregate counts
    (``total_compiled_unchanged`` etc.) — sum of per-event-
    type counts.
  * Agreement aggregate counts (``total_parity_matches`` etc.)
    — sum of per-event-type counts.
  * ``sample_decision_divergences`` — capped non-
    ``compiled_unchanged`` decision samples. Format:
    ``"<event_type>: <decision> [exception=...|dropped_node_ids=...]"``.
  * ``sample_parity_divergences`` — capped parity-divergence
    samples. Format:
    ``"<event_type>: <comparator detail>"``. Surfaced
    separately so a run with lots of ``compiled_filtered``
    events (which are decision divergences) doesn't crowd
    out the parity samples an operator actually needs to
    triage semantic drift.
  * Audit fields (``started_at`` / ``finished_at``).
  """

  counts_by_event_type: dict[str, EventTypeCounts]
  total_events: int
  skipped_events: int
  total_compiled_unchanged: int
  total_compiled_filtered: int
  total_fallback_for_event: int
  total_compiled_path_faults: int
  total_parity_matches: int
  total_parity_divergences: int
  total_parity_not_checked: int
  sample_decision_divergences: tuple[str, ...]
  sample_parity_divergences: tuple[str, ...]
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
  def compiled_path_fault_rate(self) -> float:
    return (
        self.total_compiled_path_faults / self.total_events
        if self.total_events
        else 0.0
    )

  @property
  def parity_match_rate(self) -> float:
    """Matches over *checked* events. See
    :meth:`EventTypeCounts.parity_match_rate` for why
    ``parity_not_checked`` is excluded from the denominator."""
    checked = self.total_parity_matches + self.total_parity_divergences
    return self.total_parity_matches / checked if checked else 0.0

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
        "total_compiled_path_faults": self.total_compiled_path_faults,
        "total_parity_matches": self.total_parity_matches,
        "total_parity_divergences": self.total_parity_divergences,
        "total_parity_not_checked": self.total_parity_not_checked,
        "sample_decision_divergences": list(self.sample_decision_divergences),
        "sample_parity_divergences": list(self.sample_parity_divergences),
        "started_at": self.started_at,
        "finished_at": self.finished_at,
    }
    return json.dumps(payload, sort_keys=True, indent=2)


@dataclasses.dataclass(frozen=True)
class RevalidationThresholds:
  """Optional thresholds for :func:`check_thresholds`.

  Every field is ``None`` by default — meaning "no threshold on
  this dimension." Set the ones the caller cares about; leave
  the rest. Rates are fractions in ``[0, 1]``; values outside
  that range are rejected at construction time so a typo like
  ``max_fallback_for_event_rate=5`` (intended as 5%) fails loud
  instead of silently disabling the gate.
  """

  min_compiled_unchanged_rate: Optional[float] = None
  max_compiled_filtered_rate: Optional[float] = None
  max_fallback_for_event_rate: Optional[float] = None
  max_compiled_path_fault_rate: Optional[float] = None
  min_parity_match_rate: Optional[float] = None

  def __post_init__(self) -> None:
    # Each threshold names a fraction in [0, 1]. A value above
    # 1 silently disables the gate (no observed rate can ever
    # exceed it), a negative value disables the matching
    # min-gate, and ``NaN`` makes every comparison False — all
    # three patterns hide misconfiguration. Reject at
    # construction so the failure surfaces at the call site
    # that built the thresholds, not three runs later when an
    # operator wonders why nothing tripped.
    for field in dataclasses.fields(self):
      value = getattr(self, field.name)
      if value is None:
        continue
      if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"RevalidationThresholds field {field.name!r} must be a "
            f"number in [0, 1] or None; got "
            f"{type(value).__name__}={value!r}"
        )
      # ``value != value`` catches NaN without importing math.
      if value != value or value < 0.0 or value > 1.0:
        raise ValueError(
            f"RevalidationThresholds field {field.name!r} must be in "
            f"[0, 1]; got {value!r}"
        )


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
  """Run every event through both extractors and aggregate the
  outcomes into a report covering both the runtime decision
  (from :func:`run_with_fallback`) and agreement against the
  reference.

  Per-event flow:

  1. Drive the event through :func:`run_with_fallback` with a
     **no-op fallback** so the call yields a clean runtime-
     decision signal without consuming a reference invocation.
     The wrapper's ``compiled_unchanged`` / ``compiled_filtered``
     / ``fallback_for_event`` decision lands directly in the
     report's decision counts.
  2. For events whose decision is ``compiled_unchanged`` or
     ``compiled_filtered``, call the reference extractor
     separately and compare its output against the wrapper's
     output via three comparators: :func:`_compare_nodes`
     and :func:`_compare_span_handling` from
     ``measurement.py``, plus :func:`_compare_edges` defined
     locally (kept here because the renderer doesn't emit
     edges, so ``measure_compile`` doesn't need it).
     Result is recorded as ``parity_match`` /
     ``parity_divergence``.
  3. For ``fallback_for_event`` events the compiled output
     never reaches downstream, so parity is recorded as
     ``parity_not_checked``.

  Every failure mode on the reference side becomes a parity
  divergence, never a batch abort: exceptions, non-
  ``StructuredExtractionResult`` returns (including
  ``None``), and comparator crashes all funnel into the
  divergence channel with a descriptive string. The wrapper
  itself never crashes the batch on compiled-extractor
  failures (per :func:`run_with_fallback`'s contract).

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
      (revalidation needs both extractors to be meaningful).
    resolved_graph: Forwarded to :func:`run_with_fallback`.
    spec: Forwarded to each extractor's ``(event, spec)`` call.
    sample_divergence_cap: Per-dimension cap on the number of
      sample-divergence strings included in the report. Applies
      independently to ``sample_decision_divergences`` and
      ``sample_parity_divergences``. Defaults to 10.

  Returns:
    A :class:`RevalidationReport` aggregating per-event-type
    counts and rates across both dimensions.
  """
  started_at = _now_iso_utc()

  per_type_running: dict[str, _RunningCounts] = {}
  skipped_events = 0
  sample_decision_divergences: list[str] = []
  sample_parity_divergences: list[str] = []

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
        # No-op fallback: the wrapper's "fallback" output isn't
        # exercised here; we want the runtime decision, and the
        # parity check below calls reference directly under
        # exception protection. Keeping the fallback no-op
        # avoids double-invoking reference on fallback events
        # *and* keeps reference-extractor exceptions from
        # propagating out through ``run_with_fallback`` (which
        # by design forwards fallback exceptions unchanged).
        fallback_extractor=_noop_fallback,
    )

    running = per_type_running.setdefault(event_type, _RunningCounts())
    running.total += 1

    # Dimension 1: runtime decision.
    if outcome.decision == "compiled_unchanged":
      running.compiled_unchanged += 1
    elif outcome.decision == "compiled_filtered":
      running.compiled_filtered += 1
      if len(sample_decision_divergences) < sample_divergence_cap:
        sample_decision_divergences.append(
            _summarize_outcome(event_type=event_type, outcome=outcome)
        )
    elif outcome.decision == "fallback_for_event":
      running.fallback_for_event += 1
      if outcome.compiled_exception is not None:
        running.compiled_path_faults += 1
      if len(sample_decision_divergences) < sample_divergence_cap:
        sample_decision_divergences.append(
            _summarize_outcome(event_type=event_type, outcome=outcome)
        )

    # Dimension 2: agreement against reference. Only meaningful
    # when the compiled output actually reached downstream
    # (decisions other than ``fallback_for_event``). For
    # ``fallback_for_event`` events the compiled output was
    # discarded by the wrapper, so a parity comparison would
    # measure something the runtime never used — recorded as
    # ``parity_not_checked`` and excluded from the parity-match
    # denominator.
    if outcome.decision == "fallback_for_event":
      running.parity_not_checked += 1
      continue

    parity_divergence = _check_parity(
        wrapper_result=outcome.result,
        reference_extractor=reference,
        event=event,
        spec=spec,
    )
    if parity_divergence is None:
      running.parity_matches += 1
    else:
      running.parity_divergences += 1
      if len(sample_parity_divergences) < sample_divergence_cap:
        sample_parity_divergences.append(f"{event_type}: {parity_divergence}")

  counts_by_event_type = {
      et: EventTypeCounts(
          event_type=et,
          total=r.total,
          compiled_unchanged=r.compiled_unchanged,
          compiled_filtered=r.compiled_filtered,
          fallback_for_event=r.fallback_for_event,
          compiled_path_faults=r.compiled_path_faults,
          parity_matches=r.parity_matches,
          parity_divergences=r.parity_divergences,
          parity_not_checked=r.parity_not_checked,
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
  total_compiled_path_faults = sum(
      c.compiled_path_faults for c in counts_by_event_type.values()
  )
  total_parity_matches = sum(
      c.parity_matches for c in counts_by_event_type.values()
  )
  total_parity_divergences = sum(
      c.parity_divergences for c in counts_by_event_type.values()
  )
  total_parity_not_checked = sum(
      c.parity_not_checked for c in counts_by_event_type.values()
  )

  return RevalidationReport(
      counts_by_event_type=counts_by_event_type,
      total_events=total_events,
      skipped_events=skipped_events,
      total_compiled_unchanged=total_compiled_unchanged,
      total_compiled_filtered=total_compiled_filtered,
      total_fallback_for_event=total_fallback_for_event,
      total_compiled_path_faults=total_compiled_path_faults,
      total_parity_matches=total_parity_matches,
      total_parity_divergences=total_parity_divergences,
      total_parity_not_checked=total_parity_not_checked,
      sample_decision_divergences=tuple(sample_decision_divergences),
      sample_parity_divergences=tuple(sample_parity_divergences),
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
  if thresholds.max_compiled_path_fault_rate is not None:
    if (
        report.compiled_path_fault_rate
        > thresholds.max_compiled_path_fault_rate
    ):
      violations.append(
          f"compiled_path_fault_rate {report.compiled_path_fault_rate:.4f} "
          f"> max {thresholds.max_compiled_path_fault_rate:.4f}"
      )
  if thresholds.min_parity_match_rate is not None:
    if report.parity_match_rate < thresholds.min_parity_match_rate:
      violations.append(
          f"parity_match_rate {report.parity_match_rate:.4f} "
          f"< min {thresholds.min_parity_match_rate:.4f}"
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
  compiled_path_faults: int = 0
  parity_matches: int = 0
  parity_divergences: int = 0
  parity_not_checked: int = 0


def _noop_fallback(event: dict, spec: Any) -> StructuredExtractionResult:
  """Empty :class:`StructuredExtractionResult`. Used as the
  fallback in revalidation's ``run_with_fallback`` call so the
  wrapper never invokes the reference indirectly; the parity
  check calls reference directly under exception protection."""
  return StructuredExtractionResult()


def _check_parity(
    *,
    wrapper_result: StructuredExtractionResult,
    reference_extractor: Callable[..., StructuredExtractionResult],
    event: dict,
    spec: Any,
) -> Optional[str]:
  """Compare *wrapper_result* against the reference extractor's
  output on *event*. Returns ``None`` on agreement, or a short
  divergence string naming what differed.

  Every failure mode on the reference side is funneled into a
  parity-divergence string so one bad reference event can't
  kill the batch:

  * Reference raises → ``reference extractor raised X: msg``.
  * Reference returns a non-``StructuredExtractionResult``
    (including ``None``) → ``reference extractor returned
    <TypeName>, not StructuredExtractionResult``. Without this
    check the comparators below would hit
    ``AttributeError`` on ``.nodes`` and crash the batch.
  * Comparator itself raises (e.g. a malformed span-set on
    either side blows up the comparator's ``set(...)``
    coercion) → ``parity comparator raised X: msg``.

  ``KeyboardInterrupt`` / ``SystemExit`` propagate so operator
  cancellation still works.
  """
  try:
    ref_result = reference_extractor(event, spec)
  except Exception as exc:  # noqa: BLE001 — record + continue
    return f"reference extractor raised {type(exc).__name__}: {exc}"

  if not isinstance(ref_result, StructuredExtractionResult):
    return (
        f"reference extractor returned {type(ref_result).__name__}, "
        f"not StructuredExtractionResult"
    )

  # Comparator calls wrapped in try/except: a malformed
  # internal (e.g. ``ref_result.fully_handled_span_ids`` that
  # was stored as a list / None / str — the dataclass doesn't
  # enforce ``set[str]`` at runtime) makes the comparator
  # raise rather than return a divergence string. Catching
  # here keeps the batch alive; the parity-divergence message
  # names the comparator so an operator can triage which side
  # has the bad shape.
  try:
    # Duplicate node_id guard, *before* ``_compare_nodes``.
    # ``_compare_nodes`` in measurement.py keys nodes by
    # ``node_id`` via ``{n.node_id: n for n in ...}`` and would
    # silently collapse duplicates — a malformed reference with
    # duplicate node_ids could overwrite down to a single node
    # that happens to match compiled, and the run would report
    # ``parity_match``. #76's validator catches
    # ``duplicate_node_id`` on the *compiled* side before the
    # wrapper output reaches us, but reference output isn't
    # validated. The local guard is symmetric (checks both
    # sides) so the parity contract — "every reference-side
    # failure mode becomes a parity divergence" — actually
    # holds.
    node_dupe_divergence = _check_duplicate_node_ids(
        ref_result.nodes, wrapper_result.nodes
    )
    if node_dupe_divergence is not None:
      return node_dupe_divergence

    # Node-set agreement: same ``node_id`` set with matching
    # entity_name / labels / property-set per node. Defers to
    # measurement.py's comparator so revalidation and
    # measure_compile stay byte-aligned on parity semantics.
    node_divergence = _compare_nodes(ref_result.nodes, wrapper_result.nodes)
    if node_divergence is not None:
      return node_divergence

    # Edge-set agreement: same ``edge_id`` set with matching
    # relationship_name / endpoints / property-set per edge.
    # Edge-emitting compiled extractors that drift on edge
    # endpoints or relationship would otherwise look like a
    # parity match — the wrapper accepts them and the
    # node comparator never sees them.
    edge_divergence = _compare_edges(ref_result.edges, wrapper_result.edges)
    if edge_divergence is not None:
      return edge_divergence

    # Span-handling agreement: ``fully_handled_span_ids`` and
    # ``partially_handled_span_ids`` sets must match. Important
    # because C2.b's compiled_filtered path downgrades span
    # handling from fully → partially; a wrong span-handling
    # set silently changes what the AI extractor sees
    # downstream.
    span_divergence = _compare_span_handling(ref_result, wrapper_result)
    if span_divergence is not None:
      return span_divergence
  except Exception as exc:  # noqa: BLE001 — record + continue
    return f"parity comparator raised {type(exc).__name__}: {exc}"

  return None


def _check_duplicate_node_ids(ref_nodes, cmp_nodes) -> Optional[str]:
  """Return a divergence string if either side has duplicate
  ``node_id``s, or ``None`` otherwise.

  Lives in revalidation rather than ``measurement.py`` because
  measure_compile compares against a freshly-compiled extractor
  whose duplicates would already be caught by the smoke-test
  validator; revalidation runs against arbitrary reference
  extractors whose output isn't validated by #76, so the guard
  has to live here. Checks both sides symmetrically — compiled
  duplicates are usually caught by #76 before the wrapper
  output reaches us, but the symmetric check is cheap and
  makes the contract self-contained.
  """
  ref_counts = collections.Counter(n.node_id for n in ref_nodes)
  cmp_counts = collections.Counter(n.node_id for n in cmp_nodes)
  ref_dupes = sorted(nid for nid, n in ref_counts.items() if n > 1)
  cmp_dupes = sorted(nid for nid, n in cmp_counts.items() if n > 1)
  if not (ref_dupes or cmp_dupes):
    return None
  parts = []
  if ref_dupes:
    parts.append(f"reference duplicates: {ref_dupes}")
  if cmp_dupes:
    parts.append(f"compiled duplicates: {cmp_dupes}")
  return "duplicate node_id — " + "; ".join(parts)


def _compare_edges(ref_edges, cmp_edges) -> Optional[str]:
  """Return a divergence string or ``None`` if the edge sets
  match.

  Match criterion: same set of ``edge_id`` values, and for
  each shared ``edge_id`` the ``relationship_name``,
  ``from_node_id``, ``to_node_id``, and property
  ``(name, value)`` set are equal.

  Lives in revalidation rather than measurement because the
  renderer doesn't emit edges (per the renderer docstring) so
  ``measure_compile`` doesn't need edge parity. Revalidation
  is sampling-agnostic — it runs against any compiled / handwritten
  pair, including ones that emit edges.

  Duplicate ``edge_id``s on either side are reported as a
  divergence before the dict-keyed comparison begins.
  Without this check, duplicates would silently collapse
  during ``{e.edge_id: e for e in ...}`` construction — the
  last entry would win, and a coincidental match against the
  reference's single-entry view would look like agreement.
  #76's validator catches ``duplicate_node_id`` but doesn't
  enforce edge-id uniqueness, so the protection has to live
  here.
  """
  ref_counts = collections.Counter(e.edge_id for e in ref_edges)
  cmp_counts = collections.Counter(e.edge_id for e in cmp_edges)
  ref_dupes = sorted(eid for eid, n in ref_counts.items() if n > 1)
  cmp_dupes = sorted(eid for eid, n in cmp_counts.items() if n > 1)
  if ref_dupes or cmp_dupes:
    parts = []
    if ref_dupes:
      parts.append(f"reference duplicates: {ref_dupes}")
    if cmp_dupes:
      parts.append(f"compiled duplicates: {cmp_dupes}")
    return "duplicate edge_id — " + "; ".join(parts)

  ref_by_id = {e.edge_id: e for e in ref_edges}
  cmp_by_id = {e.edge_id: e for e in cmp_edges}
  if set(ref_by_id) != set(cmp_by_id):
    only_ref = sorted(set(ref_by_id) - set(cmp_by_id))
    only_cmp = sorted(set(cmp_by_id) - set(ref_by_id))
    parts = []
    if only_ref:
      parts.append(f"only in reference: {only_ref}")
    if only_cmp:
      parts.append(f"only in compiled: {only_cmp}")
    return "edge_id mismatch — " + "; ".join(parts)
  for edge_id in sorted(ref_by_id):
    ref_edge = ref_by_id[edge_id]
    cmp_edge = cmp_by_id[edge_id]
    if ref_edge.relationship_name != cmp_edge.relationship_name:
      return (
          f"edge {edge_id!r} relationship_name "
          f"{ref_edge.relationship_name!r} (ref) != "
          f"{cmp_edge.relationship_name!r} (compiled)"
      )
    if ref_edge.from_node_id != cmp_edge.from_node_id:
      return (
          f"edge {edge_id!r} from_node_id "
          f"{ref_edge.from_node_id!r} (ref) != "
          f"{cmp_edge.from_node_id!r} (compiled)"
      )
    if ref_edge.to_node_id != cmp_edge.to_node_id:
      return (
          f"edge {edge_id!r} to_node_id "
          f"{ref_edge.to_node_id!r} (ref) != "
          f"{cmp_edge.to_node_id!r} (compiled)"
      )
    ref_props = {(p.name, _hashable(p.value)) for p in ref_edge.properties}
    cmp_props = {(p.name, _hashable(p.value)) for p in cmp_edge.properties}
    if ref_props != cmp_props:
      only_ref = sorted(ref_props - cmp_props)
      only_cmp = sorted(cmp_props - ref_props)
      bits = []
      if only_ref:
        bits.append(f"only in reference: {only_ref}")
      if only_cmp:
        bits.append(f"only in compiled: {only_cmp}")
      return f"edge {edge_id!r} property set mismatch — " + "; ".join(bits)
  return None


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
