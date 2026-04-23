# Implementation Plan: Concept Index + Runtime Entity Resolution

## Scope

Implement the runtime entity resolution primitives specified in [issue #58](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/58) on top of the SKOS import work in [issue #57](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/57).

Two packages touched:

- `bigquery_ontology` — compiler, CLI, fingerprint infrastructure.
- `bigquery_agent_analytics` — `OntologyRuntime`, resolvers, verification.

**Package-boundary scope.** Both packages are build-time + trace-consumption libraries. `bigquery_ontology` is offline CLI + model classes. `bigquery_agent_analytics` is the consumption-layer SDK read by evaluation, curation, and analysis pipelines over trace data the BQ AA Plugin already wrote to BigQuery. **Neither is a turn-time agent SDK**; the live-agent side is owned by the BQ AA Plugin (separate package). The word *Runtime* in this plan refers to the `OntologyRuntime` class and to library call time at the consuming pipeline, not to a live agent loop. A future agent-facing resolver package may reuse the `EntityResolver` `Protocol` introduced here, but it is out of scope for this plan.

This plan assumes issue #57 is either merged or lands in parallel. The concept index's value is ~80% from SKOS annotations (`skos:notation`, `skos:prefLabel`, `skos:altLabel`, `skos:broader`) being preserved through import.

## Acceptance criteria

- `gm compile --emit-concept-index --concept-index-table <fqn>` produces a concept index + sibling meta table for an ontology + binding, with byte-identical SQL across runs.
- `OntologyRuntime.load(...)` / `.from_models(...)` wraps a validated `(Ontology, Binding)` pair and exposes read accessors over annotations, synonyms, notations, concept-scheme membership, and abstract-relationship traversal.
- `EntityResolver` Protocol + `ExactMatchResolver` and `SynonymResolver` references work against the concept index with correct dedup and scope semantics.
- Strict verification defaults on; first-call and TTL re-checks both enforce pair consistency + full-fingerprint freshness.
- All exception types wired: `ConceptIndexMismatchError`, `ConceptIndexProvenanceMissing`, `ConceptIndexInconsistentPair`, `ConceptIndexRefreshed`.
- Inline-UNNEST path is fully atomic per statement; shadow-swap fallback is documented as offline/admin.
- Test suite covers determinism, pair-consistency, TTL re-check, scope semantics, candidate dedup, bounded validation output, and shadow-path failure handling.

## Work breakdown

### Bucket A — ontology package (`bigquery_ontology/`)

| Item | File | Additive? | Dependencies |
|---|---|---|---|
| A1 | Internal fingerprint module (`_fingerprint.py`, new) — canonical model serialization + SHA-256. Shared implementation detail, not public API | new | none |
| A2 | Concept-index row builder (`concept_index.py`, new) — iterates `(ontology, binding)`, applies abstract-always / concrete-iff-bound rule, emits sorted row list | new | A1, #57's `abstract` field |
| A3 | `compile_concept_index()` in `graph_ddl_compiler.py` | additive function in existing module | A1, A2 |
| A4 | Meta row emission inside `compile_concept_index()` | part of A3 | A1 |
| A5 | Inline-UNNEST path SQL generation (`CREATE OR REPLACE TABLE ... AS SELECT UNNEST(...)`) with `compile_id` column | part of A3 | A4 |
| A6 | Shadow-swap fallback for >50K rows | part of A3 | A5 |
| A7 | CLI extension: `--emit-concept-index` + `--concept-index-table` in `cli.py:299` | edits existing command | A3 |
| A8 | Docs: `docs/ontology/concept-index.md` (new), update `docs/ontology/cli.md` and `docs/ontology/compilation.md` | docs | A3, A7 |

### Bucket B — SDK package (`bigquery_agent_analytics/`)

| Item | File | Additive? | Dependencies |
|---|---|---|---|
| B1 | `ontology_runtime.py` (new) — `OntologyRuntime` class with `load` / `from_models` classmethods | new | A1 (shares fingerprint impl) |
| B2 | Read accessors: `entities()`, `entity()`, `synonyms()`, `annotation()`, `in_scheme()`, `broader()`, `narrower()`, `related()` | part of B1 | #57 abstract-relationship traversal semantics |
| B3 | `validate_against_ontology()` with bounded output (`known_value_count`, `known_values_sample`, `sample_limit`, mutually-exclusive `scheme=` / `entity=`) | part of B1 | B2 |
| B4 | `entity_resolver.py` (new) — `EntityResolver` Protocol, `Candidate`, `ResolveResult` dataclasses | new | none |
| B5 | `ExactMatchResolver` — name + notation exact match via concept index | part of B4 | B4, A3 schema |
| B6 | `SynonymResolver` — extends B5 with label-based exact match | part of B4 | B5 |
| B7 | Candidate dedup logic (one per entity, winning-label priority, `limit=N` distinct) | shared helper | B4 |

### Bucket C — verification layer

| Item | File | Notes |
|---|---|---|
| C1 | Exception classes in `ontology_runtime.py` — `ConceptIndexMismatchError`, `ConceptIndexProvenanceMissing`, `ConceptIndexInconsistentPair`, `ConceptIndexRefreshed` | Public API surface |
| C2 | First-call verification — read meta, compute local fingerprints, compare | Lazy (on first concept-index access, not construction) |
| C3 | Pair-consistency check with 2s one-shot retry | Used by C2 and C4 |
| C4 | TTL re-check with full pair-consistency + full-fingerprint freshness | Two queries per stale call: `SELECT DISTINCT compile_id FROM main LIMIT 2` + `SELECT compile_id, ontology_fingerprint, binding_fingerprint FROM meta LIMIT 1` |
| C5 | `verify_concept_index` flag handling: `"strict"` (default), `"missing_ok"`, `"off"` | |
| C6 | `verify_ttl_seconds` flag handling: `60` (default), `0` (every-call), `None` (snapshot-bound) | |

### Bucket D — tests

| Item | Scope |
|---|---|
| D1 | Fingerprint determinism (non-semantic YAML edits produce identical fingerprints; semantic edits change them) |
| D2 | Compile output byte-identical across runs for same `(ontology, binding, output_table, compiler_version)` |
| D3 | Row scope (abstract always included; concrete iff bound; cross-cutting test with mixed ontology) |
| D4 | Multi-scheme denormalization (concept in 2 schemes → 2 rows; `DISTINCT entity_name` in query returns 1) |
| D5 | Notation as first-class row (notation matches via `label = @input AND label_kind = 'notation'`) |
| D6 | Candidate dedup: same entity via multiple labels/schemes → one candidate; `limit=N` returns N distinct entities |
| D7 | Winning-label priority rule (`name > pref > alt > hidden > synonym > notation`, lexicographic tiebreaker) |
| D8 | Scope semantics: `scheme=` and `entity=` mutually exclusive; neither = error; both = error |
| D9 | Pair consistency: inconsistent pair triggers retry; persistent inconsistency raises `ConceptIndexInconsistentPair` |
| D10 | TTL re-check: fresh cache skips BQ; stale cache runs full check; matching values refresh cache; differing values raise `ConceptIndexRefreshed` |
| D11 | 48-bit collision hypothetical: full fingerprints catch the collision that short `compile_id` doesn't (can simulate by injecting a crafted meta row) |
| D12 | First-call verification: mismatched fingerprints raise `ConceptIndexMismatchError`; missing meta raises `ConceptIndexProvenanceMissing` |
| D13 | Validation bounded output: `known_values_sample` capped at `sample_limit`; `known_value_count` correct; `candidates=None` unless composed |
| D14 | Shadow-path emission correctness (>50K rows → both tables shadow-swap; compile_id present in both) |
| D15 | Abstract-entity filter: resolver with `WHERE NOT is_abstract` returns only concrete; default returns both |

## Phase plan

Five phases, each independently mergeable. Each leaves `main` in a shippable state.

### Phase 1 — Ontology compiler foundation (no runtime dependency)

Ships the compile-time half. No SDK code touched. Users can emit the concept index from CLI; nothing reads it yet.

Work: A1, A2, A3, A4, A5, A7, A8 (docs partial).

Tests: D1, D2, D3, D4, D5, D14 (shadow-path skeleton; full shadow test in Phase 3).

**Definition of done**: `gm compile --emit-concept-index --concept-index-table <fqn>` produces a valid, byte-identical concept index + meta table for a fixture ontology. Re-running produces identical SQL. SKOS-annotated ontologies produce notation rows.

**Out of scope for this phase**: shadow-swap (A6 stub only), any SDK consumer code, verification logic.

### Phase 2 — SDK read accessors + resolver Protocol (no verification)

Ships `OntologyRuntime` with lookups but without strict verification. Users can resolve against the concept index if it exists; verification is `"off"` unless opted in later.

Work: B1, B2, B3, B4, B5, B6, B7, C1 (exception classes defined but not raised yet).

Tests: D6, D7, D8, D13, D15.

**Definition of done**: `OntologyRuntime.load(...)` wraps validated models, exposes all read accessors. `ExactMatchResolver` and `SynonymResolver` run against a concept index table and return correctly deduped candidates with scope semantics honored. Validation returns bounded output.

**Out of scope**: verification, TTL re-check, shadow-path.

### Phase 3 — Verification layer (strict default on)

The correctness gate. Wires C2-C6 on top of Phase 2. Default changes from `"off"` to `"strict"`.

Work: C2, C3, C4, C5, C6, A6 (full shadow-swap implementation).

Tests: D9, D10, D11, D12, D14.

**Definition of done**: Strict verification enforces pair consistency and full-fingerprint freshness on first access and on TTL expiry. All four exception types raise in their documented conditions. Shadow-path emission works for >50K-row fixtures with documented transient-failure behavior.

**Special attention**: preserve the TTL re-check reading BOTH tables with FULL fingerprints (watchpoint from review). Add a regression test specifically for the single-table-sentinel hole and the 48-bit-collision hypothetical.

### Phase 4 — Integration, migration, docs

Work: integration tests across ontology → compile → runtime → resolve; end-to-end example in `examples/`; migration note for users who had local resolution code; full doc pass.

The migration note explicitly splits into two paths — the motivating feedback-gist resolver ran at live-agent time, and the SDK does not replace it directly:

- **Trace-consumption migration** (pipelines / notebooks / curation scripts): direct drop-in. `SynonymResolver` + the compiled concept index replaces the pipeline's local resolver. This is the primary supported path.
- **Live-agent migration**: not yet supported. Keep your existing in-agent resolver until a separate agent-facing package ships that reuses the `EntityResolver` `Protocol`. Users who want forward-compatibility can prototype against the Protocol today so that swap is mechanical when the agent-facing package lands.

**Definition of done**: `examples/concept_index_quickstart.py` (or similar) runs end-to-end against a real BQ dataset using a fixture ontology. README section added. Migration note published with both paths clearly labeled.

### Phase 5 — Contrib + polish

Ships reference resolver implementations beyond `ExactMatchResolver` / `SynonymResolver` as `contrib/` packages. Yahoo's layered (IAB/DMA-tuned) resolver is an early candidate per the feedback gist.

Work: `bigquery_ontology/contrib/advertising/` stub with Yahoo's resolver (if contributed). Additional domain packs (healthcare, finance) land later.

## File-by-file changes

### New files

- `src/bigquery_ontology/_fingerprint.py` — **internal** module (underscore prefix) with canonical JSON serialization of Pydantic models + SHA-256. Internal function: `fingerprint_model(model: BaseModel) -> str`. Short variant for `compile_id`: `compile_id(ontology_fingerprint, binding_fingerprint, compiler_version) -> str`. Not re-exported from any `__init__.py`. Shared implementation between `compile_concept_index()` (ontology package) and `OntologyRuntime` (SDK package) via absolute import `from bigquery_ontology._fingerprint import ...`. Underscore prefix makes it clear this isn't semver-stable surface; it's an implementation detail both packages happen to need.
- `src/bigquery_ontology/concept_index.py` — row builder. Function: `build_rows(ontology: Ontology, binding: Binding) -> list[ConceptIndexRow]`. Applies "abstract always, concrete iff bound" rule. Emits one row per `(entity_name, label, label_kind, language, scheme)` tuple plus one notation row per entity per `skos:notation`. Sorts by `(scheme, entity_name, label_kind, language, label, notation, is_abstract)` with NULLs last. **Not re-exported from `bigquery_ontology/__init__.py` in v1.** Module is importable directly (`from bigquery_ontology.concept_index import build_rows, ConceptIndexRow`) for users who need pre-SQL row access — same pattern as the existing `from bigquery_ontology.graph_ddl_compiler import compile_graph` alongside the package-root export. Package-level re-export can be added later if a concrete caller appears; keeping it out of the root for v1 avoids growing semver surface ahead of need.
- `src/bigquery_agent_analytics/ontology_runtime.py` — `OntologyRuntime` class with classmethods, read accessors, validation, verification. Exception classes (`ConceptIndexMismatchError` etc.) live here too.
- `src/bigquery_agent_analytics/entity_resolver.py` — `EntityResolver` Protocol, `Candidate`, `ResolveResult`, `ExactMatchResolver`, `SynonymResolver`.
- `docs/ontology/concept-index.md` — user-facing documentation for `--emit-concept-index`, schema, provenance, verification modes.
- Test files mirroring the current repo test layout — SDK tests flat, ontology tests in a subdirectory:
  - `tests/bigquery_ontology/test_fingerprint.py` (for `_fingerprint.py` — tests import the underscore module directly)
  - `tests/bigquery_ontology/test_concept_index.py`
  - `tests/bigquery_ontology/test_compile_concept_index.py`
  - `tests/test_ontology_runtime.py` (SDK-level, top-level `tests/` per current convention)
  - `tests/test_entity_resolver.py`
  - `tests/test_verification.py`

### Modified files

- `src/bigquery_ontology/graph_ddl_compiler.py` — add `compile_concept_index(ontology, binding, *, output_table) -> str`. Preserve `compile_graph()` contract byte-identically. No changes to existing function bodies.
- `src/bigquery_ontology/cli.py:299` — `compile` command gains `--emit-concept-index` and `--concept-index-table` flags. When absent, behavior is byte-identical to today.
- `src/bigquery_ontology/__init__.py` — add `from .graph_ddl_compiler import compile_concept_index` so the new public function is importable as `from bigquery_ontology import compile_concept_index`, matching the existing pattern for `compile_graph` (`__init__.py:50` today).
- `src/bigquery_agent_analytics/__init__.py` — add the new public surface to the try/except re-export block (same pattern as `Client`, `CodeEvaluator`, etc.):
  - `OntologyRuntime` from `.ontology_runtime`
  - `EntityResolver`, `ExactMatchResolver`, `SynonymResolver`, `Candidate`, `ResolveResult` from `.entity_resolver`
  - `ConceptIndexMismatchError`, `ConceptIndexProvenanceMissing`, `ConceptIndexInconsistentPair`, `ConceptIndexRefreshed` from `.ontology_runtime`
- `docs/ontology/cli.md` — document new flags.
- `docs/ontology/compilation.md` — mention the sibling DML emitter.
- `docs/ontology/owl-import.md` — note that SKOS `skos:notation` lands as annotation (for #57 compatibility), and will appear as a first-class concept-index row in the resolver surface.

### Unchanged files

- `src/bigquery_ontology/ontology_models.py` — no model changes for this work (issue #57 handles the `abstract` field separately).
- `src/bigquery_ontology/binding_models.py` — no changes in v1. Binding-side toggle is deferred per the issue.
- All other `bigquery_agent_analytics/*.py` — runtime accessor is purely additive; no existing file is edited.

## Implementation watchpoints

From the final review pass, three watchpoints to preserve across this implementation and any future refactors:

### W1 — Canonical serialization rules must match between compiler and runtime

The fingerprint must be computed identically on both sides. `src/bigquery_ontology/_fingerprint.py` is the single source of truth — both `compile_concept_index()` (writing meta) and `OntologyRuntime` (reading and comparing) import from it via absolute import. Regression test: round-trip a model through YAML → load → fingerprint, then edit the YAML's whitespace/comments, re-load, re-fingerprint, assert identical. Also test that semantic edits (rename entity, change target dataset) produce different fingerprints.

Pin specifically in the module docstring: `Pydantic.model_dump(mode="json", by_alias=False, exclude_none=False)` with keys sorted at every nesting level and `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` for the final encoding. Never use `yaml.dump()` or `str(model)` for fingerprint input.

### W2 — TTL re-check must read both tables with full fingerprints

A future "optimizer" might see the TTL path and try to reduce it to a single-table sentinel `SELECT compile_id FROM meta LIMIT 1`. That would silently reintroduce the old meta / new main race. Or might try to reduce to short-compile-id comparison only, reintroducing the 48-bit collision hole.

Guard: a regression test that mocks a race window (meta unchanged, main updated with different compile_id but matching short hash via crafted rows) and asserts the runtime catches it. Also a test that a single-table sentinel implementation fails the test. Comment in `_ttl_recheck()` citing the specific failure modes, with a link back to issue #58.

### W3 — Shadow-path failure handling must match the documented operational contract

When the shadow path's `DROP` + `RENAME` pair fails mid-swap, the current contract is: raise cleanly, let the operator's retry detect orphaned shadow tables and resume. A tempting shortcut is to wrap this in background "self-healing" retry logic inside the compiler, which would mask partial-swap states from operators and break the "pause traffic during shadow refresh" guidance.

Guard: keep the compiler's shadow-swap path non-self-healing. The compiler detects orphaned shadow tables on its next invocation and resumes deterministically; it does not spin retry loops on its own. Test: inject a mid-swap failure, verify `gm compile` errors with a clear message; verify a subsequent `gm compile` completes the swap without recompiling.

## Rollout notes

- **Backward compatibility**: `gm compile` without `--emit-concept-index` is byte-identical to today's output. Existing users see no behavioral change.
- **Ontology package version bump**: new public API (`compile_concept_index`, re-exported from `bigquery_ontology/__init__.py`) warrants a minor version bump. `_fingerprint.py` is internal and does not factor into semver.
- **SDK version bump**: new public API (`OntologyRuntime`, `EntityResolver` + `ExactMatchResolver` + `SynonymResolver`, dataclasses, exception classes, all re-exported from `bigquery_agent_analytics/__init__.py`) warrants a minor version bump.
- **Existing resolution code in user applications**: no deprecation. Users continue their existing resolution approach until they opt into the SDK primitive.
- **BQ permissions**: `--emit-concept-index` requires `bigquery.tables.create` on the target dataset (existing `compile_graph()` requirement, unchanged). Runtime reading requires `bigquery.tables.getData` on the concept index and meta tables (standard).

## Open watchlist (not blocking, track during implementation)

- Behavior when ontology has >100K concepts — current plan emits shadow-swap at >50K; may need a LOAD-job path at the next order of magnitude. Out of scope for v1; track via GitHub issue if real users hit this.
- Pointer-indirection (`{output_table}__current`) as a future mitigation for shadow-path transient failures. Explicitly deferred per issue #58; track for v2 if real users request.
- `asyncio` variants of `EntityResolver.resolve()` — v1 ships sync only; add async later if adoption signals need.
- Binding-side opt-in (`index:` block on `Binding` model) — v1 ships CLI-only; add binding toggle in v2 with explicit precedence rule.

## Estimated effort

Rough sizing in engineering-weeks, single developer, including tests and docs:

- Phase 1 (ontology compiler): 2 weeks
- Phase 2 (SDK read + resolver): 2 weeks
- Phase 3 (verification layer): 2 weeks (most subtle; plan for iteration on the retry / TTL semantics)
- Phase 4 (integration + migration + docs): 1 week
- Phase 5 (contrib): 0.5 week for scaffolding + review of Yahoo's contribution when ready

Total: ~7.5 weeks of focused work. Can run Phases 1 and 2 in parallel across two developers for ~4 weeks wall-clock.

## References

- Issue #58: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/58
- Issue #57: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/57
- Feedback gist (original motivating use case): https://gist.github.com/haiyuan-eng-google/54c3d3366b3d75b659561ef4e24e9374
