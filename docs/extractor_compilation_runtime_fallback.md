# Compiled Structured Extractors — Runtime Fallback (PR C2.b)

**Status:** Implemented (PR C2.b of issue #75 Phase C / Milestone C2)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md), [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) (PR C2.a), [#76 validator](ontology/validation.md)
**Working plan:** issue #96, Milestone C2 / PR C2.b

---

## What this is

The runtime safety net for compiled extractors. When a compiled extractor produces output that crashes, doesn't match the contract, or violates the ontology in ways that can't be salvaged, this wrapper substitutes the *fallback* extractor (the existing handwritten or `AI.GENERATE` path). When the violations are pinpointable to specific nodes / edges, the wrapper drops just those elements **and downgrades the event's span-handling so the AI transcript still sees the source span and can recover the missing pieces.**

C2.b is the wrapper *policy* only — it doesn't yet wire into the orchestrator. The actual call-site swap inside `ontology_graph.py` / wherever the orchestrator calls extractors is C2.c.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    run_with_fallback,
    FallbackOutcome,
)

outcome: FallbackOutcome = run_with_fallback(
    event=...,                  # one telemetry event dict
    spec=...,                   # forwarded to both extractors
    resolved_graph=...,         # the ResolvedGraph the validator compares against
    compiled_extractor=...,     # output validated against #76
    fallback_extractor=...,     # called only on event-scope rejection
)

# outcome.decision is one of:
#   "compiled_unchanged"  — compiled output validates clean
#   "compiled_filtered"   — bad nodes/edges dropped; span downgraded; rest kept
#   "fallback_for_event"  — full event re-extracted by fallback
```

## Decision tree

The wrapper applies the decision tree top-down; first match wins.

| Step | Condition | Decision |
|------|-----------|----------|
| 1 | Compiled extractor raises *or* returns a non-`StructuredExtractionResult` value | `fallback_for_event` (compiled_exception captured in audit record) |
| 2 | Validate compiled output via `validate_extracted_graph`. No failures | `compiled_unchanged` |
| 3 | Any `EVENT`-scope failure, *or* any failure with neither a `node_id` nor an `edge_id` we can pinpoint | `fallback_for_event` |
| 4 | Otherwise (every failure has a usable `node_id`/`edge_id`) | `compiled_filtered` |

`FallbackScope.EVENT` is reserved for this runtime layer — the wrapper handles it defensively, but #76 itself doesn't currently emit it.

## Drop policy in `compiled_filtered`

Per-element drops are conservative — drop the whole containing element rather than salvage individual properties:

- `NODE` failure with `node_id` → drop that node by ID.
- `EDGE` failure with `edge_id` → drop that edge by ID.
- `FIELD` failure with `node_id` → drop the whole containing **node** (not just the bad property).
- `FIELD` failure with `edge_id` → drop the whole containing **edge**.
- After per-element drops, **orphan-clean** any edge whose `from_node_id` or `to_node_id` was dropped. The audit's `dropped_edge_ids` lists both direct and orphan-cleaned edges.

## Span-handling downgrade — load-bearing

When the wrapper returns `compiled_filtered`, it **always** downgrades the event's span-handling:

```
fully_handled_span_ids:    remove event["span_id"]
partially_handled_span_ids: add    event["span_id"]
```

Why this matters: `fully_handled_span_ids` means "exclude this span from the `AI.GENERATE` transcript." If the wrapper drops a bad node but leaves the span fully handled, the lost fact is **never recoverable** — the AI never sees the source span. By downgrading to partially handled, the compiled output contributes the valid structured pieces *and* AI still sees the source span for the missing pieces. That's what makes per-element fallback real in the existing runtime architecture.

If the event has no `span_id`, the downgrade is a no-op. The valid pieces still come through; there's just no span to downgrade.

## What the wrapper does **not** do

- **Validate the fallback output.** The fallback path is the existing baseline — handwritten extractors that have been in production, or the `AI.GENERATE` SQL path. If the fallback ever produces bad output, the runtime has bigger problems than this wrapper can solve.
- **Catch fallback exceptions.** Same reasoning. Exceptions from `fallback_extractor` propagate to the caller, matching existing runtime behavior.
- **Run the fallback for per-element failures.** The fallback's contract is "extract from one whole event" — running it for one specific bad node within an event isn't a thing it knows how to do. Per-element failures drop the bad piece and let AI recover via the partial-span path.

## `FallbackOutcome` shape

```
result                  : StructuredExtractionResult  # always populated
decision                : "compiled_unchanged" | "compiled_filtered" | "fallback_for_event"
compiled_exception      : Optional[str]               # "<ExceptionType>: <message>" or "WrongReturnType: <type>"
dropped_node_ids        : tuple[str, ...]             # populated only on compiled_filtered
dropped_edge_ids        : tuple[str, ...]             # direct + orphan-cleaned
validation_failures     : tuple[ValidationFailure, ...]  # the report driving the decision (empty when validation didn't run)
```

`frozen=True`. The audit fields are designed so telemetry can group on `decision`, count `compiled_exception` types, and surface `dropped_*` cardinalities.

## Tests (16 cases in `tests/test_extractor_compilation_runtime_fallback.py`)

- **`TestRunWithFallbackCompiledUnchanged`** (2) — valid compiled output passes through; empty compiled output is vacuously valid (no fallback call).
- **`TestRunWithFallbackForEventTriggers`** (7) — compiled raises; compiled returns wrong type; compiled returns `None`; `EVENT`-scope validator failure; unpinpointable failure (no `node_id` / `edge_id`); mixed `EVENT` + per-element failures (EVENT wins); fallback-extractor exceptions propagate without being swallowed.
- **`TestRunWithFallbackCompiledFiltered`** (4) — `NODE`-scope failure drops node (real validator run on a ghost-entity node); orphan cleanup drops edges referencing a dropped node; `EDGE`-scope failure drops edge while keeping nodes; `FIELD`-scope with `node_id` drops whole containing node.
- **`TestRunWithFallbackSpanDowngrade`** (2) — load-bearing: a node failure on a fully-handled span moves the span to `partially_handled_span_ids`; events without `span_id` skip the downgrade gracefully.
- **`TestRunWithFallbackEndToEnd`** (1) — real BKA bundle (compiled via the full pipeline + loaded via `load_bundle`) as `compiled_extractor`, real `extract_bka_decision_event` as `fallback_extractor`. Both produce identical output for every BKA sample event → `decision="compiled_unchanged"`. Proves the wrapper plays nicely with the rest of Phase C.

## Out of scope (deferred to other C2 sub-PRs)

- **Orchestrator call-site swap** — where in `ontology_graph.py` / the orchestrator does `run_with_fallback` actually replace direct extractor calls? C2.c.
- **BigQuery-table bundle mirror** for cross-process distribution. C2.c.
- **Revalidation harness** — scheduled / on-demand agreement check between compiled and reference outputs. C2.d.
- **AI.GENERATE-backed adapter** that fits the `StructuredExtractor` callable signature so it can be passed as `fallback_extractor`. The wrapper itself is signature-agnostic; how the runtime *constructs* an AI.GENERATE fallback is the orchestrator integration's concern, not this wrapper's.

## Related

- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — the RFC that decided client-side Python is the Phase 1 runtime target. C2.b is the safety net that decision needs.
- [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) — C2.a's loader produces the `compiled_extractor` that this wrapper validates.
- [`ontology/validation.md`](ontology/validation.md) — the failure-code surface (`ValidationFailure.scope` / `code` / `node_id` / `edge_id`) that this wrapper routes on.
