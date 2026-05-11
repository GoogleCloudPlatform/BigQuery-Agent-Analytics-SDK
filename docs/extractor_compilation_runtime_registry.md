# Compiled Structured Extractors — Runtime Registry Adapter (PR C2.c.1)

**Status:** Implemented (PR C2.c.1 of issue #75 Phase C / Milestone C2)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) (PR C2.a), [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) (PR C2.b)
**Working plan:** issue #96, Milestone C2 / PR C2.c.1

---

## What this is

The adapter that glues C2.a's bundle loader and C2.b's runtime fallback wrapper into one call: `build_runtime_extractor_registry(...)` returns an `event_type → extractor` dict ready to pass into the existing `run_structured_extractors` hook. **This PR ships the adapter, not the orchestrator call-site swap** — deciding which orchestrator paths actually adopt the registry is a separate scope (C2.c.2).

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    build_runtime_extractor_registry,
    WrappedRegistry,
    OutcomeCallback,
)

registry: WrappedRegistry = build_runtime_extractor_registry(
    bundles_root=...,
    expected_fingerprint=...,
    fallback_extractors={"bka_decision": extract_bka_decision_event, ...},
    resolved_graph=resolved_graph,
    event_type_allowlist=("bka_decision", "tool_call"),  # None to consider every event_type
    on_outcome=my_telemetry_callback,                    # None to skip per-event audit
)

# registry.extractors goes straight into the existing runtime hook:
merged = run_structured_extractors(
    events=events,
    extractors=registry.extractors,
    spec=spec,
)

# registry.bundles_without_fallback / registry.fallbacks_without_bundle for coverage telemetry
```

## Wiring matrix

For each event_type in scope (filtered by `event_type_allowlist` if set):

| Compiled bundle | Handwritten fallback | Registry entry | Audit |
|---|---|---|---|
| ✓ | ✓ | Wrapped closure that calls `run_with_fallback(compiled, fallback)` and (optionally) `on_outcome(event_type, outcome)` | — |
| ✗ | ✓ | Original fallback callable (same identity, no wrapping) | Listed in `fallbacks_without_bundle` |
| ✓ | ✗ | **Not registered** — fail-closed default; C2.b's safety contract requires a fallback | Listed in `bundles_without_fallback` |
| ✗ | ✗ | — | — |

The "compiled-only fail-closed" policy is the part of this PR that makes C2's safety contract load-bearing: a compiled extractor without a handwritten fallback would run *without* the validator-driven safety net, which inverts the C2 guarantees. Skip and surface in audit.

## `WrappedRegistry` shape

```
extractors                  : dict[str, StructuredExtractor]   # ready for run_structured_extractors
discovery                   : DiscoveryResult                  # full audit from C2.a
bundles_without_fallback    : tuple[str, ...]                  # compiled-only event_types (skipped)
fallbacks_without_bundle    : tuple[str, ...]                  # fallback-only event_types (registered unchanged)
```

`bundles_without_fallback` and `fallbacks_without_bundle` are sorted tuples for deterministic audit output.

`bundles_without_fallback` is the strict signal: a compiled bundle was successfully discovered but had no matching fallback, so it was skipped. Always a configuration-error signal, never a coverage signal.

`fallbacks_without_bundle` is the wider signal: there's no *usable* compiled registry entry for the event_type. That includes "no bundle was ever built" *and* "a bundle exists but discovery rejected it" — fingerprint mismatch, `manifest_unreadable`, event-type collision, etc. — with the underlying reason in `discovery.failures`. Rollout telemetry that wants to distinguish "bundle never built" from "bundle rejected" should cross-reference `discovery.failures` for the failure code rather than treat `fallbacks_without_bundle` as a pure "no coverage yet" count.

## Allowlist semantics

`event_type_allowlist` filters **both** candidate pools — compiled-bundle discovery and fallback registration. An event_type outside the allowlist is silently dropped from both pools; it does NOT appear in either audit field (it's outside the caller's stated scope).

- `None` → consider every event_type from both pools.
- `("a", "b")` → only `a` and `b`.
- `()` → register nothing.

## `on_outcome` callback

`(event_type, outcome) -> None`. Invoked from inside each *wrapped* extractor — once per event after `run_with_fallback` produces the outcome, before the result is returned to the runtime. Fires on **every** wrapped invocation including `compiled_unchanged` outcomes, so callers can compute denominator metrics:

- compiled-unchanged rate (compiled extractor's output validated clean)
- compiled-filtered rate (per-element drops happened)
- fallback rate (whole-event fallback triggered)
- exception rate (subset of fallback rate where `compiled_exception` is set)

The callback is **not** invoked for fallback-only registry entries (no compiled extractor → no `FallbackOutcome` to report; the fallback IS the only path).

**Callback exceptions propagate.** Telemetry callbacks should be correct; silently swallowing here would hide instrumentation bugs and defeat the audit channel. A caller that wants non-blocking telemetry can layer their own try/except inside the callback.

## Tests (22 cases in `tests/test_extractor_compilation_runtime_registry.py`)

- **`TestRegistryWiringMatrix`** (4) — compiled+fallback wraps via `run_with_fallback`; fallback-only passes through unchanged (identity preserved); compiled-only is skipped and listed in `bundles_without_fallback`; neither-present is empty registry.
- **`TestRegistryAuditSurfaces`** (3) — `fallbacks_without_bundle` records the "no usable compiled registry entry" cases; audit lists are sorted; mixed three-event scenario lands each event in the right bucket.
- **`TestRegistryAllowlist`** (3) — allowlist filters both pools (out-of-scope event_types appear in NEITHER audit field); empty allowlist registers nothing; `None` considers everything.
- **`TestRegistryOnOutcomeCallback`** (4) — fires on `compiled_unchanged` (with a real validator + empty result); fires on `fallback_for_event`; one call per invocation (denominator metric); callback exceptions propagate (no silent swallow).
- **`TestRegistryEndToEnd`** (1) — real BKA bundle (compiled via the full Phase C pipeline) + real `extract_bka_decision_event` as fallback, fed through `run_structured_extractors` via the registry. Both sample events produce `compiled_unchanged` outcomes; both nodes appear in the merged result; callback log shows the expected per-event traces.
- **`TestRegistryFallbackCallableValidation`** (7) — build-time validation of every entry in `fallback_extractors`: `None` value rejected; non-callable (int) rejected; out-of-allowlist invalid still rejected (full dict validated, not just the scoped subset); non-string key rejected (would silently never match in `run_structured_extractors` and crash audit-tuple sorting); empty-string key rejected; mixed-key-types rejected before the sort crash (clearer error message); one-arg callable rejected via `_signature_compatible` (the same `(event, spec)` contract check the bundle loader uses for compiled callables).

## Out of scope (deferred)

- **Orchestrator call-site swap** — where in `ontology_graph.py` / the runtime does the registry produced here actually replace direct extractor calls? **C2.c.2.**
- **BigQuery-table bundle mirror** for cross-process distribution. **C2.c.3.**
- **Revalidation harness** — scheduled / on-demand agreement check between compiled and reference outputs. **C2.d.**
- **`AI.GENERATE`-backed fallback adapter** that fits the `StructuredExtractor` signature. The registry wires arbitrary callables; constructing an `AI.GENERATE`-backed one is the orchestrator integration's concern.

## Related

- [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) — `discover_bundles` / `LoadedBundle` / `DiscoveryResult` / `LoadFailure`. C2.c.1 calls `discover_bundles` for the compiled-bundle pool.
- [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) — `run_with_fallback` / `FallbackOutcome`. C2.c.1's wrapped extractors are closures over this.
- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — the RFC that made client-side Python the Phase 1 runtime target. The registry adapter here is what makes that decision usable in one call.
