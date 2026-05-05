# Compiled Structured Extractors — Runtime Target

**Status:** Decision
**Parent epic:** [issue #75 — Compile-time code generation for structured trace extractors](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Working plan:** [issue #96, comment 4363301699](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699), Phase C / Milestone C1 / PR 4a
**Date:** 2026-05-05

---

## TL;DR

For Phase 1 of structured-extractor compilation (issue #75 P0.2),
compiled extractors emit **plain Python functions** that run
**client-side**, plugged into the existing
`run_structured_extractors()` hook in
`src/bigquery_agent_analytics/structured_extraction.py:198`. No new
deploy surface, no SQL/UDF translation layer.

This decision settles the prerequisite identified in #75 P0.2 and
unblocks the compile harness work in PR 4b.

## Why this decision needs a record

Compiled extractors have to execute somewhere, and the choice of
where shapes the entire compiler — what language the templates emit,
how bundles are stored, how the runtime loads them, and what the
cost model looks like. The original #75 proposal silently assumed
client-side Python; the P0.2 prerequisite was added in the scoped
rework so the choice is explicit. This doc is the explicit choice.

## The three candidates

Reproduced from #75 P0.2 with current-tree references.

| Option | Execution location | Latency | Cost model | Build complexity |
|---|---|---|---|---|
| **A. Client-side Python** | SDK process loads compiled bundle, runs extractor on events fetched from BQ, writes results back. | One BQ round-trip per batch; no network hop per event. | Slot time for the trace fetch + write only. No `AI.GENERATE` cost on the compiled path. | **Lowest.** Bundle is a Python module; no deploy pipeline. |
| **B. BigQuery Remote Function** | Extractor wrapped as a Cloud Run endpoint, invoked from SQL. | In-SQL, but one HTTP hop per row/batch. | Cloud Run minutes + IAM + deploy surface. | **Highest.** Per-bundle deploy pipeline, IAM rotation, version pinning, network surface. |
| **C. BigQuery SQL / Python UDF** | Generated extractor compiled to SQL plus UDFs and run inside BQ. | In-SQL, no network hop. | Slot time only. | **Middle.** Translation layer from extraction rules to UDF code, plus SQL-side test harness. |

## Decision — Phase 1: Option A

Phase 1 ships compiled extractors as **plain Python** intended to
execute client-side via the existing `run_structured_extractors()`
hook. Concretely, the decision settles three things and three only:

1. **Compile target language.** Generated bundles are pure Python
   modules — not SQL, not Cloud Run packages. Field-kind templates
   from ontology v0 emit Python source.
2. **Callable ABI.** A compiled extractor is a Python callable
   matching the `StructuredExtractor` signature already accepted
   by `run_structured_extractors()` at
   `structured_extraction.py:198–232` (one event dict + spec in,
   one `StructuredExtractionResult` out). No change to that
   function's signature, no change to its callers.
3. **No new BQ surface for Phase 1.** `ontology_graph.py`'s
   session-aggregated `AI.GENERATE` SQL is unchanged. No Remote
   Function deploy pipeline. No new BQ object types.

What this decision **does not** settle (deferred to the PRs below):

- Bundle storage layout — local repo path, BQ-table mirror, or
  both — stays an [open question on #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75).
  PR 4b picks a default for **local bundle layout** so the harness
  has somewhere to write; runtime discovery and any BQ-table
  mirror are explicit C2 concerns.
- The runtime loader that imports a bundle and registers its
  callables into the `extractors` dict the orchestrator hands to
  `run_structured_extractors()` is **C2 work**, not PR 4b. PR 4b
  ships compile + smoke-test in isolation; PR 4c measures one
  compiled extractor by invoking the callable directly. The
  orchestrator integration only matters once the C1 measurements
  clear the F1 / fallback gate.
- Per-event fallback (re-running the whole event through the
  hand-written or `AI.GENERATE` path) is the C2 wrapper's job.
  Per-field / per-node / per-edge fallback uses the validator from
  #76 and is also wired in C2.

The compile target is therefore: **Python source filling field-kind
templates from ontology v0**, AST-validated, smoke-tested against a
sample of real events, ontology-validated through #76, and
fingerprinted per #75's `(ontology, binding, event_schema,
event_allowlist, transcript_builder_version,
content_serialization_rules, extraction_rules, template_version,
compiler_package_version)` shape.

## Why Option A wins for Phase 1

1. **Phase 1 already runs client-side.** `run_structured_extractors()`
   today is invoked from the orchestrator after events are fetched,
   before materialization. The round-trip cost the table charges
   Option A is a cost the codebase already pays. There's no extra
   round-trip from picking Option A — it picks the cost model
   that's already on the table.

2. **Smallest commitment under uncertainty.** Phase 1 has a hard
   go/no-go gate: the compiled path has to clear F1 ≥ 0.95 and
   fallback ≤ 10% on real traces (#75 measurement section). If
   Phase 1 misses the bar, Phase 2 doesn't proceed and the epic's
   scope contracts to Phase 1 only. Option A is the only candidate
   whose investment can be discarded without leaving deploy
   infrastructure (B) or a SQL-translation layer (C) to dismantle.

3. **Matches the existing hand-written extractor pattern.**
   `structured_extraction.py` already runs typed Python extractors
   client-side. Compiled Phase 1 callables use the same callable
   shape as registry entries — measured as replacements in PR 4c —
   so when C2 wires the runtime loader it slots into the same
   merge / validation plumbing the registry already exercises,
   not a parallel runtime.

4. **Test surface is plain unit tests.** Option C's UDF path needs
   per-test BQ session orchestration; Option B's Remote Function
   path needs deploy fixtures. Option A bundles run inside pytest
   the same way `extract_bka_decision_event` does today.

5. **Authoring scale, not runtime cost, is what Phase 1 sells.**
   Phase 1 isn't trying to beat hand-written extractors on token
   cost — both are zero-token. The win is "declare event payload
   shape + extraction rules, let the compiler emit the function."
   That win is independent of where the compiled function executes.
   Adding deploy surface to chase a runtime-cost story Phase 1
   isn't telling would be inverting the scope.

## What this rules out

- **Option B (Remote Function) for Phase 1 and Phase 2.** The
  deploy surface is disproportionate to the problem and there's no
  user need calling for it. If a concrete need later appears
  (e.g., a customer wanting compiled extractors callable from
  BI-tool SQL the SDK doesn't run), that's a separate proposal —
  not a Phase 2 default.

- **Option C (SQL / Python UDF) for Phase 1.** Defers the
  translation-layer investment until Phase 1 has measurements that
  warrant it. See "Phase 2 escape hatch" below.

## Phase 2 escape hatch

#75 Phase 2 targets `ontology_graph.extract_graph()`'s session-
aggregated `AI.GENERATE` path. That tier currently runs **inside
BigQuery** (`ontology_graph.py:90–136`), so a compiled replacement
that also runs in BQ has a much shorter payoff than for Phase 1's
client-side tier.

Concretely: when Phase 2 starts (gated on Phase 1 measurements
clearing the bar), the runtime-target choice is **re-opened** with
the explicit expectation that **Option C (Python UDF / SQL UDF)**
becomes the primary candidate for the session-aggregated tier and
Option A stays the default only for the structured-event tier.
Option B remains off the table.

The Phase 2 RFC will inherit the framing here and only need to
record the C-vs-A choice for the new tier.

## Implementation surface this commits to

### Phase 1 PR 4b — compile harness + smoke-test runner

Per the [#96 working plan](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699),
PR 4b ships **compile-time only** — no orchestrator integration:

- New module: `src/bigquery_agent_analytics/extractor_compilation/`
  (final path TBD in PR 4b) containing the template-fill pipeline,
  AST validator, smoke-test runner, and fingerprint helper.
- Manifest schema: `{fingerprint, event_types, compiler_version,
  template_version, ...}` per bundle. Exact fields finalized in
  PR 4b once the fingerprint inputs from #75 are reified in code.
- A default **local bundle layout** so the harness has somewhere
  to write generated `.py` modules and the manifest. Runtime
  discovery, BQ-table mirror, and the choice of in-repo vs
  external storage are deliberately left open per #75 and revisited
  in C2.
- Callable ABI: each generated function matches the
  `StructuredExtractor` signature `run_structured_extractors()`
  already accepts.
- Validation gates: AST check, smoke test against a sample of real
  events, ontology validation through the #76 validator. Any
  failure rejects the bundle.
- No changes to `ontology_graph.py`'s SQL. No orchestrator hook,
  no bundle loader, no deploy pipeline.

### Phase 1 PR 4c — first compiled extractor + measurement

Compiles `extract_bka_decision_event` and measures F1 / per-event
extractor latency / fallback rate against the hand-written and
`AI.GENERATE` baselines. PR 4c invokes compiled callables directly
from the measurement harness; it does not require the orchestrator
loader either.

### Milestone C2 (gated on C1 measurements) — runtime integration

C2 owns the orchestrator-side integration that this RFC deliberately
keeps out of PR 4b/4c:

- Bundle loader called before `run_structured_extractors(...)`,
  inserting compiled callables into the `extractors` dict for
  every `event_type` whose fingerprint matches the active
  `(ontology, binding, event_schema, …)`.
- Runtime discovery — where the loader looks for bundles. The
  in-repo / BQ-mirror / both question gets resolved here, informed
  by what the SDK-using repos actually want.
- Per-field / per-node / per-edge fallback wired through #76's
  validator. Per-event fallback through the existing hand-written
  or `AI.GENERATE` path.
- Revalidation harness (scheduled / on-demand agreement check).

The split keeps PR 4b/4c discardable if Phase 1 measurements miss
the F1 ≥ 0.95 / fallback ≤ 10% gate — no orchestrator change is
left behind to dismantle.

## Related

- [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75) — epic
- [issue #76](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/76) — validator (P0.1, merged in #113)
- [issue #96 working plan](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699) — sequencing
- [`docs/ontology/validation.md`](ontology/validation.md) — failure-code surface compiled extractors must clear before acceptance
- [`structured_extraction.py:198`](../src/bigquery_agent_analytics/structured_extraction.py) — the runtime hook this decision wires the compiled path into
