# Compiled Structured Extractors — Revalidation Harness (PR C2.d)

**Status:** Implemented (PR C2.d of issue #75 Phase C / Milestone C2)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) (PR C2.b), [`extractor_compilation_runtime_registry.md`](extractor_compilation_runtime_registry.md) (PR C2.c.1), [`extractor_compilation_bka_measurement.md`](extractor_compilation_bka_measurement.md) (PR 4c)
**Working plan:** issue #96, Milestone C2 / PR C2.d

---

## What this is

PR 4c's `measure_compile` proves a single compile-and-compare pass. C2.c.2's orchestrator wires the compiled path into the runtime. **This module turns "works in tests" into "keeps proving itself after rollout"** — a batch-mode runner that takes a corpus of events, drives each through `run_with_fallback` *and* a direct reference-extractor call, and aggregates the per-event outcomes into a structured report.

The report has **two orthogonal dimensions**, both load-bearing:

1. **Runtime decision** — what `run_with_fallback` did on this event: `compiled_unchanged` / `compiled_filtered` / `fallback_for_event`. This is C2.b's safety vocabulary ("did the schema validator accept the compiled output").
2. **Agreement against reference** — did the compiled extractor's output match the handwritten reference's output on this event? `parity_match` / `parity_divergence` / `parity_not_checked` (the last for `fallback_for_event` events, where the compiled output was discarded).

The agreement dimension catches **schema-valid but semantically wrong** outputs — the case where the compiled extractor emits a node that survives the validator but disagrees with the reference (e.g. wrong property value). The schema-only check would silently call this `compiled_unchanged`; parity makes the drift visible.

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

# Schema-safety KPI
print(report.compiled_unchanged_rate)

# Agreement KPI
print(report.parity_match_rate)

# Per-event-type breakdown
for et, counts in report.counts_by_event_type.items():
    print(et, counts.total, counts.compiled_unchanged_rate, counts.parity_match_rate)

# Threshold check (gates both dimensions)
result = check_thresholds(report, RevalidationThresholds(
    min_compiled_unchanged_rate=0.95,
    max_fallback_for_event_rate=0.05,
    min_parity_match_rate=0.99,
))
if not result.ok:
    for violation in result.violations:
        print("FAIL:", violation)
```

## Per-event flow

For each event the harness:

1. Drives the event through `run_with_fallback` with a **no-op fallback** so the call yields a clean runtime-decision signal. The wrapper's `compiled_unchanged` / `compiled_filtered` / `fallback_for_event` decision lands directly in the report's decision counts.
2. For events whose decision is `compiled_unchanged` or `compiled_filtered`, calls the reference extractor separately and compares its output against the wrapper's output via three comparators:
   - **`_compare_nodes`** (from `measurement.py`): same `node_id` set; matching `entity_name`, `labels`, and `(name, value)` property-set per shared node. Preceded by a local **duplicate-node-id guard** because `_compare_nodes` keys nodes by `node_id` via dict construction and would silently collapse duplicates; #76 catches duplicate node_ids on the compiled side, but reference output isn't validated, so the symmetric guard has to live here.
   - **`_compare_edges`** (defined in `revalidation.py`): same `edge_id` set; matching `relationship_name`, `from_node_id`, `to_node_id`, and property-set per shared edge. Has its own inline duplicate-edge-id guard since #76 doesn't enforce edge-id uniqueness at all. Lives here rather than `measurement.py` because the renderer doesn't emit edges (so `measure_compile` doesn't need this) but revalidation runs against any compiled / handwritten pair, including ones that emit edges.
   - **`_compare_span_handling`** (from `measurement.py`): `fully_handled_span_ids` and `partially_handled_span_ids` sets must match.

   Result lands as `parity_match` or `parity_divergence`.
3. For `fallback_for_event` events the compiled output never reaches downstream, so parity is recorded as `parity_not_checked` and excluded from the `parity_match_rate` denominator.

**Every failure mode on the reference side becomes a parity divergence**, never a batch abort:

- Reference raises → `reference extractor raised X: msg`.
- Reference returns a non-`StructuredExtractionResult` (including `None`) → `reference extractor returned <TypeName>, not StructuredExtractionResult`. Without this guard the comparators would hit `AttributeError` on `.nodes` and crash the batch.
- Comparator itself raises (e.g. corrupt internals that bypass the isinstance check) → `parity comparator raised X: msg`.

`KeyboardInterrupt` / `SystemExit` still propagate so operator cancellation works.

## `RevalidationReport` shape

```
counts_by_event_type           : dict[str, EventTypeCounts]
total_events                   : int                      # all revalidated events
skipped_events                 : int                      # no compiled path → not revalidated

# Runtime-decision dimension
total_compiled_unchanged       : int
total_compiled_filtered        : int
total_fallback_for_event       : int
total_compiled_path_faults     : int                      # subset of fallback_for_event

# Agreement-against-reference dimension
total_parity_matches           : int
total_parity_divergences       : int
total_parity_not_checked       : int                      # = total_fallback_for_event

# Sample listings (per-dimension caps)
sample_decision_divergences    : tuple[str, ...]
sample_parity_divergences      : tuple[str, ...]

# Audit
started_at                     : str                      # UTC ISO timestamp
finished_at                    : str

# Computed properties
compiled_unchanged_rate        : float
compiled_filtered_rate         : float
fallback_for_event_rate        : float
compiled_path_fault_rate       : float
parity_match_rate              : float                    # matches / (matches + divergences)
```

Per-event-type:
```
EventTypeCounts:
  event_type, total,
  compiled_unchanged, compiled_filtered, fallback_for_event, compiled_path_faults,
  parity_matches, parity_divergences, parity_not_checked
  + rate properties (including parity_match_rate)
```

### Why `parity_not_checked` is excluded from `parity_match_rate`

A `fallback_for_event` event is one where the wrapper already filtered the compiled output out for safety. Counting it as a parity divergence would conflate "compiled output never reached production" with "compiled output reached production and was wrong." Only the events the wrapper actually emitted are included in the parity denominator.

### Why `compiled_path_faults` and not `compiled_exceptions`

`run_with_fallback`'s `compiled_exception` audit field fires for three distinct paths: an actual exception, a wrong return type, or malformed `StructuredExtractionResult` internals. All three are bugs in the compiled bundle (not ontology drift) but only the first is literally an exception. `compiled_path_faults` covers the full set without mis-naming the malformed-internals cases.

## `RevalidationThresholds` and `check_thresholds`

The report is pure data. Threshold checks are a policy concern — a separate function so the same report can be evaluated against different threshold sets (production gate vs. canary gate vs. nightly-trend gate).

Threshold fields:

```
min_compiled_unchanged_rate        : Optional[float]
max_compiled_filtered_rate         : Optional[float]
max_fallback_for_event_rate        : Optional[float]
max_compiled_path_fault_rate       : Optional[float]
min_parity_match_rate              : Optional[float]
```

All default to `None` (no threshold on that dimension). Set the ones the caller cares about; leave the rest.

**Rate bounds are enforced at construction.** Every non-None rate must be in `[0, 1]`; a typo like `max_fallback_for_event_rate=5` (intended as 5%) raises `ValueError` instead of silently disabling the gate (no observed rate can ever exceed 5). NaN and bool are also rejected — NaN makes every comparison false; bool `True` would silently mean 100%.

The `ThresholdCheckResult` lists every violation (not just the first), each as a human-readable string naming the failed rate and the threshold it failed.

## What gets skipped

Events that can't be revalidated end up in `report.skipped_events` rather than the rate denominators:

- Events whose `event_type` has no compiled extractor (`compiled_extractors[event_type]` is missing).
- Events whose `event_type` has no reference extractor (revalidation needs both).
- Malformed events (not a dict, missing `event_type`, empty-string `event_type`).

Revalidation only makes sense when there's a compiled path to validate; the skipped count is reported for visibility but doesn't pollute the headline rates.

## Determinism + persistence

`RevalidationReport.to_json()` is deterministic (sorted keys, fixed formatting) so reports persisted to disk / BigQuery / a telemetry pipeline can be diffed across revalidation runs to spot trends. The harness doesn't decide where reports go — `to_json` lets the caller plug into whatever persistence path they already have.

## Tests (21 cases in `tests/test_extractor_compilation_revalidation.py`)

Coverage spans both dimensions:

- **`TestRevalidationHappyPath`** (1) — deterministic BKA fixture: handwritten extractor on both sides, 3 events → all `compiled_unchanged` AND all `parity_match`.
- **`TestRevalidationParity`** (8) — the load-bearing parity coverage:
  - Schema-valid wrong output: compiled extractor emits a `mako_DecisionPoint` node with the wrong `decision_id`. Decision is `compiled_unchanged` (validator accepts it); **parity catches the drift**.
  - `parity_not_checked` for `fallback_for_event` events: a crashing compiled extractor has its compiled output discarded; parity is excluded from the match-rate denominator instead of inflating the divergence count.
  - Reference-extractor exception safety: a reference that crashes on one event is recorded as a parity divergence and the batch continues.
  - **Edge drift**: compiled emits an edge whose `to_node_id` disagrees with the reference. Node sets and span sets match, so without edge parity this would silently aggregate as a match; with edge parity the wrong endpoint surfaces.
  - **Duplicate edge_id**: compiled emits two edges sharing the same `edge_id` and the reference emits one matching the last duplicate. Without explicit duplicate detection, dict keying would silently collapse the duplicates and the run would look like a match. The check surfaces it as a parity divergence naming the offending IDs.
  - **Reference duplicate node_id**: reference emits two nodes sharing the same `node_id` where the last duplicate matches compiled's single node. #76 catches duplicate_node_id on the compiled side, but reference output isn't validated, so the local guard in `_check_parity` covers it before delegating to `_compare_nodes`.
  - **Reference returns `None`**: must NOT abort the batch with `AttributeError`. Recorded as a parity divergence naming the wrong return type.
  - **Comparator-raises**: a monkey-patched `_compare_nodes` that raises is caught at the parity-check boundary; the divergence string names the comparator that exploded.
- **`TestRevalidationDrift`** (1) — schema-failing drift surfaces in BOTH dimensions: `compiled_filtered` (validator drops the bad node) AND `parity_divergence` (filtered output disagrees with reference's real output).
- **`TestRevalidationCompiledException`** (2) — compiled extractor that raises lands as `fallback_for_event` with the underlying outcome's `compiled_exception` field set; the report counts those as `compiled_path_faults` separately from validator-driven fallbacks.
- **`TestRevalidationThresholds`** (5) — flagship gate (unchanged rate < 0.95); empty thresholds always pass; multiple thresholds (including `min_parity_match_rate`) all evaluated; **rate-bounds validation** rejects out-of-range / NaN / bool at construction; boundary values (0.0 and 1.0) are accepted.
- **`TestRevalidationAuditShape`** (4) — skipped-events accounting; malformed events skipped; `to_json` deterministic + sorted; both sample-divergence caps respected independently.

## Out of scope (deferred)

- **Scheduled / cron orchestration.** The harness is a pure function over events. Wiring it to Cloud Scheduler / cron / GitHub Actions is the caller's concern.
- **Persistence (BigQuery, disk).** `RevalidationReport.to_json()` gives callers a stable string; where to write it is their choice.
- **CLI / one-shot binary.** A `bqaa-revalidate-extractors` CLI is a natural follow-up once the report shape is stable in production.
- **Sampling strategy.** Random sample, time window, session subset — the caller decides which events to revalidate. The harness consumes the events the caller hands it.
- **Auto-fix workflow.** When the report trips a threshold, what happens next (rebuild the bundle? alert operators? roll back?) is a policy concern. The harness produces the signal; downstream decides what to do with it.

## Related

- [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) — `run_with_fallback` decision tree. Revalidation is a batch driver around it (with the fallback wired to a no-op so reference exceptions can't crash the batch).
- [`extractor_compilation_runtime_registry.md`](extractor_compilation_runtime_registry.md) — `on_outcome` callback in the production registry. The same per-event audit channel; revalidation aggregates it in batch.
- [`extractor_compilation_bka_measurement.md`](extractor_compilation_bka_measurement.md) — `measure_compile` is the one-shot compile-and-measure utility; revalidation is the ongoing-check utility. They share `_compare_nodes` / `_compare_span_handling` so the same agreement semantics apply at compile time and at revalidation time. Revalidation adds `_compare_edges` on top (kept here because the renderer doesn't emit edges, but handwritten / future extractors can).
