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
| 1 | Compiled extractor raises (`Exception` or `SystemExit`) *or* returns a non-`StructuredExtractionResult` value | `fallback_for_event` (compiled_exception captured) |
| 1b | Compiled return value's `fully_handled_span_ids` / `partially_handled_span_ids` aren't a `set`/`frozenset` of non-empty strings | `fallback_for_event` (compiled_exception starts `"MalformedResultInternals:"`, names the offending field). The dataclass enforces no runtime types; this catch-up validation rejects `None`, raw strings, lists, and sets containing non-string or empty-string entries before the value can leak downstream. |
| 1c | Compiled return value's `nodes` / `edges` (e.g., `nodes=[{}]`) raise when the wrapper builds an `ExtractedGraph` for validation | `fallback_for_event` (compiled_exception starts `"MalformedResultInternals:"`) |
| 2 | Validate compiled output via `validate_extracted_graph`. No failures | `compiled_unchanged` |
| 3 | Any `EVENT`-scope failure, *or* any failure that isn't pinpointable for its scope | `fallback_for_event` |
| 4 | Otherwise (every failure is scope-pinpointable) | `compiled_filtered` |

`FallbackScope.EVENT` is reserved for this runtime layer — the wrapper handles it defensively, but #76 itself doesn't currently emit it. **Pinpointability is scope-specific**: NODE iff `node_id` is set, EDGE iff `edge_id` is set, FIELD iff *either* is set. (Critical for #76's `missing_endpoint_key` failures, which are `EDGE`-scope but populate both `node_id` (the referenced endpoint) and `edge_id` (the offending edge) — the wrapper drops the edge, not the endpoint.)

`KeyboardInterrupt` is **not** caught — operator cancellation propagates through.

## Drop policy in `compiled_filtered`

Drops are decided by `failure.scope`, not by which IDs happen to be set on the failure record. Per-element drops are conservative — drop the whole containing element rather than salvage individual properties:

- `NODE` scope → drop by `node_id`.
- `EDGE` scope → drop by `edge_id`. Even when `node_id` is also populated (as in `missing_endpoint_key`), the right thing to drop is the edge, not the referenced endpoint.
- `FIELD` scope → drop the whole containing element. The wrapper prefers `edge_id` if both are set (the property literally lives on the edge in that case), falling back to `node_id` otherwise.
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

## Tests (28 cases in `tests/test_extractor_compilation_runtime_fallback.py`)

- **`TestRunWithFallbackCompiledUnchanged`** (2) — valid compiled output passes through; empty compiled output is vacuously valid (no fallback call).
- **`TestRunWithFallbackForEventTriggers`** (7) — compiled raises; compiled returns wrong type; compiled returns `None`; `EVENT`-scope validator failure; unpinpointable failure; mixed `EVENT` + per-element failures (EVENT wins); fallback-extractor exceptions propagate without being swallowed.
- **`TestRunWithFallbackCompiledFiltered`** (4) — `NODE`-scope failure drops node (real validator run on a ghost-entity node); orphan cleanup drops edges referencing a dropped node; `EDGE`-scope failure drops edge while keeping nodes; `FIELD`-scope with `node_id` drops whole containing node.
- **`TestRunWithFallbackSpanDowngrade`** (2) — load-bearing: a node failure on a fully-handled span moves the span to `partially_handled_span_ids`; events without `span_id` skip the downgrade gracefully.
- **`TestRunWithFallbackEndToEnd`** (1) — real BKA bundle as `compiled_extractor`, real `extract_bka_decision_event` as `fallback_extractor`; identical output → `compiled_unchanged`.

Review-driven regression groups:

- **`TestRunWithFallbackMalformedInternals`** (1) — reviewer's exact repro: `StructuredExtractionResult(nodes=[{}])` (a dict instead of an `ExtractedNode`) makes `ExtractedGraph` construction raise pydantic `ValidationError`. The wrapper catches it and falls back; `compiled_exception` starts with `"MalformedResultInternals:"` so logs can route this separately from extractor exceptions.
- **`TestRunWithFallbackEdgeFailureWithBothIds`** (2) — `EDGE`-scope failure with both `node_id` and `edge_id` populated (mirroring #76's `missing_endpoint_key`) drops the edge, not the referenced endpoint; symmetric pinpointability check ensures NODE-scope failure missing `node_id` is treated as unpinpointable even when `edge_id` is set.
- **`TestRunWithFallbackSystemExit`** (2) — `SystemExit` from the compiled extractor is captured as `fallback_for_event`; `KeyboardInterrupt` propagates through so operator cancellation works.
- **`TestRunWithFallbackSpanSetShape`** (7) — `fully_handled_span_ids=None` falls back; `partially_handled_span_ids="span1"` falls back (would otherwise corrupt to `{"s","p","a","n","1"}` via `set(...)` coercion); list rejected (wrong container type); non-string entry rejected; empty-string entry rejected; `frozenset` accepted; combined NODE-failure + `fully_handled_span_ids=None` falls back via span-set validation rather than crashing in the filtered-path's `set(...)` coercion.

## Out of scope (deferred to other C2 sub-PRs)

- **Orchestrator call-site swap** — where in `ontology_graph.py` / the orchestrator does `run_with_fallback` actually replace direct extractor calls? C2.c.
- **BigQuery-table bundle mirror** for cross-process distribution. C2.c.
- **Revalidation harness** — scheduled / on-demand agreement check between compiled and reference outputs. C2.d.
- **AI.GENERATE-backed adapter** that fits the `StructuredExtractor` callable signature so it can be passed as `fallback_extractor`. The wrapper itself is signature-agnostic; how the runtime *constructs* an AI.GENERATE fallback is the orchestrator integration's concern, not this wrapper's.

## Related

- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — the RFC that decided client-side Python is the Phase 1 runtime target. C2.b is the safety net that decision needs.
- [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) — C2.a's loader produces the `compiled_extractor` that this wrapper validates.
- [`ontology/validation.md`](ontology/validation.md) — the failure-code surface (`ValidationFailure.scope` / `code` / `node_id` / `edge_id`) that this wrapper routes on.
