# Compiled Structured Extractors — Revalidation Harness (PR C2.d)

**Status:** Implemented (PR C2.d of issue #75 Phase C / Milestone C2)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) (PR C2.b), [`extractor_compilation_runtime_registry.md`](extractor_compilation_runtime_registry.md) (PR C2.c.1), [`extractor_compilation_bka_measurement.md`](extractor_compilation_bka_measurement.md) (PR 4c)
**Working plan:** issue #96, Milestone C2 / PR C2.d

---

## What this is

PR 4c's `measure_compile` proves a single compile-and-compare pass. C2.c.2's orchestrator wires the compiled path into the runtime. **This module turns "works in tests" into "keeps proving itself after rollout"** — a batch-mode runner that takes a corpus of events, drives each through `run_with_fallback` with the matching compiled and reference extractor, and aggregates the per-event `FallbackOutcome` decisions into a structured report.

The report's job is to surface drift. C2.b's decision tree (`compiled_unchanged` / `compiled_filtered` / `fallback_for_event`) is already the right vocabulary for "how often did the compiled extractor hold up versus need adjustment." Revalidation runs that decision tree against a sample of real events and aggregates the counts — per event_type and overall — so operators can see whether the compiled bundle's behavior still matches the handwritten reference that gated its compile.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    revalidate_compiled_extractors,
    check_thresholds,
    RevalidationReport,
    RevalidationThresholds,
    ThresholdCheckResult,
    EventTypeCounts,
)

report: RevalidationReport = revalidate_compiled_extractors(
    events=sampled_events,                                  # list[dict]
    compiled_extractors=loaded_compiled_by_event_type,      # e.g. discover_bundles(...).registry
    reference_extractors={"bka_decision": extract_bka_decision_event, ...},
    resolved_graph=resolved_graph,
    spec=spec,                                              # optional, forwarded to extractors
    sample_divergence_cap=10,                               # default 10
)

# Headline KPI
print(report.compiled_unchanged_rate)

# Per-event-type breakdown
for et, counts in report.counts_by_event_type.items():
    print(et, counts.total, counts.compiled_unchanged_rate)

# Threshold check
result = check_thresholds(report, RevalidationThresholds(
    min_compiled_unchanged_rate=0.95,
    max_fallback_for_event_rate=0.05,
))
if not result.ok:
    for violation in result.violations:
        print("FAIL:", violation)
```

## `RevalidationReport` shape

```
counts_by_event_type        : dict[str, EventTypeCounts]
total_events                : int                      # all revalidated events
skipped_events              : int                      # no compiled path → not revalidated
total_compiled_unchanged    : int
total_compiled_filtered     : int
total_fallback_for_event    : int
total_compiled_exceptions   : int                      # subset of fallback_for_event
sample_divergences          : tuple[str, ...]          # capped at sample_divergence_cap
started_at                  : str                      # UTC ISO timestamp
finished_at                 : str

# Computed:
compiled_unchanged_rate     : float
compiled_filtered_rate      : float
fallback_for_event_rate     : float
exception_rate              : float
```

Per-event-type:
```
EventTypeCounts:
  event_type, total, compiled_unchanged, compiled_filtered,
  fallback_for_event, compiled_exceptions
  + rate properties
```

`compiled_exceptions` is split out from `fallback_for_event` so operators can distinguish **bundle bugs** (compiled extractor crashed) from **ontology drift** (validator rejected the output). Both end up as fallback_for_event in the production runtime; the rates tell different stories.

## What gets skipped

Events that can't be revalidated end up in `report.skipped_events` rather than the rate denominators:

- Events whose `event_type` has no compiled extractor (`compiled_extractors[event_type]` is missing).
- Events whose `event_type` has no reference extractor (the wrapper requires both).
- Malformed events (not a dict, missing `event_type`, empty-string `event_type`).

Revalidation only makes sense when there's a compiled path to validate; the skipped count is reported for visibility but doesn't pollute the headline rates.

## `RevalidationThresholds` and `check_thresholds`

The report is pure data. Threshold checks are a policy concern — a separate function so the same report can be evaluated against different threshold sets (production gate vs. canary gate vs. nightly-trend gate).

All four threshold fields default to `None` (no threshold on that dimension). Set the ones the caller cares about; leave the rest. The `ThresholdCheckResult` lists every violation (not just the first), each as a human-readable string naming the failed rate and the threshold it failed.

## Determinism + persistence

`RevalidationReport.to_json()` is deterministic (sorted keys, fixed formatting) so reports persisted to disk / BigQuery / a telemetry pipeline can be diffed across revalidation runs to spot trends. The harness doesn't decide where reports go — `to_json` lets the caller plug into whatever persistence path they already have.

## Tests (11 cases in `tests/test_extractor_compilation_revalidation.py`)

The four scenarios from the working plan plus audit-shape coverage:

- **`TestRevalidationHappyPath`** (1) — deterministic BKA fixture: handwritten extractor on both sides, 3 events → all `compiled_unchanged`, rates aggregate correctly.
- **`TestRevalidationDrift`** (1) — compiled extractor emits an `unknown_entity` node, validator drops it, decision is `compiled_filtered`. Counts and sample-divergence entry surface in the report.
- **`TestRevalidationCompiledException`** (2) — compiled extractor that raises lands as `fallback_for_event` with `compiled_exception` field set, and the report counts those *separately* from validator-driven fallbacks. The split is what lets operators see bundle-bug vs. ontology-drift at a glance.
- **`TestRevalidationThresholds`** (3) — threshold-fails case (unchanged rate < 0.95 trips the gate); empty thresholds always pass; multiple thresholds all evaluated (no short-circuit on first violation).
- **`TestRevalidationAuditShape`** (4) — skipped-events accounting for events whose `event_type` has no compiled extractor; malformed events skipped; `to_json` deterministic + sorted; `sample_divergence_cap` respected even with 20 drifted events.

## Out of scope (deferred)

- **Scheduled / cron orchestration.** The harness is a pure function over events. Wiring it to Cloud Scheduler / cron / GitHub Actions is the caller's concern.
- **Persistence (BigQuery, disk).** `RevalidationReport.to_json()` gives callers a stable string; where to write it is their choice.
- **CLI / one-shot binary.** A `bqaa-revalidate-extractors` CLI is a natural follow-up once the report shape is stable in production.
- **Sampling strategy.** Random sample, time window, session subset — the caller decides which events to revalidate. The harness consumes the events the caller hands it.
- **Auto-fix workflow.** When the report trips a threshold, what happens next (rebuild the bundle? alert operators? roll back?) is a policy concern. The harness produces the signal; downstream decides what to do with it.

## Related

- [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) — `run_with_fallback` decision tree. Revalidation is a batch driver around it.
- [`extractor_compilation_runtime_registry.md`](extractor_compilation_runtime_registry.md) — `on_outcome` callback in the production registry. The same per-event audit channel; revalidation aggregates it in batch.
- [`extractor_compilation_bka_measurement.md`](extractor_compilation_bka_measurement.md) — `measure_compile` is the one-shot compile-and-measure utility; revalidation is the ongoing-check utility. Different timing, same parity vocabulary.
