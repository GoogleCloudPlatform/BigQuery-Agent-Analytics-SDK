# Compiled Structured Extractors — Orchestrator Call-Site Swap (PR C2.c.2)

**Status:** Implemented (PR C2.c.2 of issue #75 Phase C / Milestone C2)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_runtime_registry.md`](extractor_compilation_runtime_registry.md) (PR C2.c.1), [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) (PR C2.b), [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) (PR C2.a)
**Working plan:** issue #96, Milestone C2 / PR C2.c.2

---

## What this is

The actual call-site swap that puts compiled structured extractors on the runtime path. C2.c.1 shipped the registry adapter (`build_runtime_extractor_registry`); this PR wires that adapter into `OntologyGraphManager` so the existing `run_structured_extractors` call inside `extract_graph` picks up the compiled-with-fallback behavior automatically when the manager is built from a bundle root.

The wire-up is a new classmethod, `OntologyGraphManager.from_bundles_root(...)`. The existing `__init__` and `from_ontology_binding` paths are unchanged — back-compat is preserved by construction, since direct-constructor callers leave `manager.runtime_registry = None`.

## Public API

```python
from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

manager = OntologyGraphManager.from_bundles_root(
    project_id="my-project",
    dataset_id="my-dataset",
    ontology=ontology,
    binding=binding,
    bundles_root=pathlib.Path("/path/to/bundles"),
    expected_fingerprint=runtime_fingerprint,
    fallback_extractors={"bka_decision": extract_bka_decision_event, ...},
    event_type_allowlist=("bka_decision", "tool_call"),  # optional
    on_outcome=my_telemetry_callback,                    # optional
)

# Use the manager exactly like a manager built from from_ontology_binding —
# extract_graph internally calls run_structured_extractors(manager.extractors, ...).
graph = manager.extract_graph(session_ids=..., use_ai_generate=True)

# Audit telemetry:
registry = manager.runtime_registry  # WrappedRegistry (or None for direct __init__)
registry.bundles_without_fallback    # event_types skipped because no fallback
registry.fallbacks_without_bundle    # event_types with no usable compiled entry
registry.discovery.failures          # underlying load failures for the wider audit
```

## Two manager attributes that always work together

- **`manager.extractors`** — the dict the runtime actually invokes (the same dict `run_structured_extractors` already consumes). For event_types with both a compiled bundle and a fallback, this is a wrapper closure; for fallback-only event_types, the original callable; for compiled-only event_types, nothing (fail-closed per C2's safety contract).
- **`manager.runtime_registry`** — the audit handle. `WrappedRegistry` from C2.c.1 when constructed via `from_bundles_root`; `None` when constructed via the legacy `__init__`. Always non-`None` for managers wired to a bundles root, even if discovery found zero bundles.

The split keeps the mental model intact: `extractors` is *what runtime uses*, `runtime_registry` is *the audit object next to it*.

## Why a classmethod rather than a constructor parameter

The constructor stays simple (one construction mode = one set of inputs). Adding `bundles_root` / `expected_fingerprint` to `__init__` would mix two construction modes and make future call sites harder to audit — there'd be no syntactic clue from the call site whether the manager is bundle-wired.

`from_bundles_root` makes the bundle-wired path explicit. The parameter shape mirrors `from_ontology_binding` so existing callers reach for the parallel pattern; the only differences are the bundle-specific args (`bundles_root`, `expected_fingerprint`, `event_type_allowlist`, `on_outcome`) and that `extractors=` becomes `fallback_extractors=` (since that's what the registry adapter calls them and the safety-contract role is different from "the dict the runtime uses").

## Negative-path behavior

A bundle discovered for `event_x` with no matching fallback is **not** registered (`manager.extractors` doesn't contain `event_x`). The audit handle records it:

```python
manager.runtime_registry.bundles_without_fallback == ("event_x",)
"event_x" not in manager.extractors
```

This matches C2.c.1's fail-closed policy — C2.b's safety contract requires a fallback, so registering a compiled-only event_type would invert the C2 guarantees. The audit field surfaces the configuration gap so operators can fix it; the runtime stays safe.

## Inherited `use_ai_generate` gate

`extract_graph` runs structured extractors only under:

```python
if self.extractors and use_ai_generate:
    raw_events = self._fetch_raw_events(session_ids)
    structured_result = run_structured_extractors(raw_events, self.extractors, self.spec)
```

This gate pre-dates C2.c.2 — the bundle-wired path inherits it as-is. **When `use_ai_generate=False`, the compiled extractors do not run**, even if `manager.extractors` is fully populated from `from_bundles_root`. The non-AI path falls back to `_extract_payloads(session_ids)` and returns the stub graph that has always been there.

Whether to decouple structured extraction from the AI flag is a separate scope decision. C2.c.2 deliberately does *not* change this gate so that the call-site swap is a pure substitution: the same conditions that triggered the legacy `extractors=` path trigger the new `from_bundles_root` path, no more and no less. A regression test (`test_extract_graph_skips_structured_when_use_ai_generate_false`) pins this inherited behavior so any future decoupling shows up as a deliberate change.

## Tests (8 cases in `tests/test_ontology_graph_from_bundles_root.py`)

- **`TestOntologyGraphManagerDirectInit`** (1) — direct `__init__` leaves `runtime_registry = None`; `extractors` identity is preserved.
- **`TestFromBundlesRootNoBundles`** (1) — `bundles_root` exists but contains no bundles → `manager.extractors` identity-preserves the fallback; `runtime_registry.fallbacks_without_bundle` lists the uncovered event_types.
- **`TestFromBundlesRootCompiledAndFallback`** (1) — hand-written bundle + matching fallback → wrapped closure registered (different identity from the original fallback); calling it drives `run_with_fallback` and the `on_outcome` callback fires with `decision="compiled_unchanged"`.
- **`TestFromBundlesRootCompiledOnlyNoFallback`** (1) — **negative case** — bundle for `event_x` with empty `fallback_extractors`. `event_x` is NOT registered; surfaced in `bundles_without_fallback`. Behavioral check: running `run_structured_extractors` over an `event_x` event yields an empty result (the runtime's "no extractor → skip" path).
- **`TestFromBundlesRootEndToEnd`** (1) — real BKA compiled bundle (driven through the full Phase C compile pipeline) + real `extract_bka_decision_event` as fallback, wired into the manager, fed through `run_structured_extractors` via `manager.extractors`. Both BKA sample events produce `compiled_unchanged` outcomes; both `mako_DecisionPoint` nodes appear in the merged result; callback log shows expected per-event traces.
- **`TestFromBundlesRootExtractGraphCallSite`** (3) — **the production call site itself**, including the merge:
  - Monkeypatches `_fetch_raw_events` + `_extract_via_ai_generate` and calls `manager.extract_graph(..., use_ai_generate=True)`; asserts `on_outcome` fired with the compiled path. Proves `extract_graph` actually invokes the wrapped registry, not just `run_structured_extractors` in isolation.
  - Same setup with a **non-empty** compiled bundle that returns one `ExtractedNode` per event; asserts the structured node propagates through the merge into the final `ExtractedGraph` returned by `extract_graph`. Pins the compiled-output-is-merged behavior.
  - Pins the inherited `use_ai_generate=False` gate: structured extractors don't run, the fetch hook isn't called, `on_outcome` doesn't fire.

## Out of scope (deferred)

- **BigQuery-table bundle mirror** for cross-process distribution. C2.c.3 — adds support for fetching bundles from a BigQuery table in addition to a filesystem path.
- **Revalidation harness** (scheduled / on-demand agreement check between compiled and reference outputs). C2.d.
- **`AI.GENERATE`-backed fallback adapter** that fits the `StructuredExtractor` signature so it can be passed as a `fallback_extractors` value. Orchestrator integration's concern.
- **Other orchestrator entry points.** This PR adds `from_bundles_root` to `OntologyGraphManager`. Other managers / orchestrators that also call `run_structured_extractors` can adopt the same pattern when they need compiled extractors; the registry adapter and decision tree don't change.

## Related

- [`extractor_compilation_runtime_registry.md`](extractor_compilation_runtime_registry.md) — `build_runtime_extractor_registry` and `WrappedRegistry` shape. `from_bundles_root` is a thin wrapper around this adapter.
- [`extractor_compilation_runtime_fallback.md`](extractor_compilation_runtime_fallback.md) — `run_with_fallback` decision tree and safety contract.
- [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) — `discover_bundles` and the trust-boundary failure codes that surface in `manager.runtime_registry.discovery.failures`.
- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — the RFC that made client-side Python the Phase 1 runtime target.
