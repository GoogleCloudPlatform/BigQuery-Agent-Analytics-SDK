# Ontology Runtime Reader (issue #58 reader)

**Status:** Implemented (issue #58 reader follow-on to PR #92)
**Parent epic:** [issue #58](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/58)
**Builds on:** [PR #92 concept-index emission](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/92), [`docs/entity_resolution_primitives.md`](entity_resolution_primitives.md)

---

## What this is

PR #92 ships **emission**: `gm compile --emit-concept-index` writes a deterministic concept-index table plus an `__meta` sibling carrying `compile_fingerprint` / `compile_id` provenance. This module ships the **reader**: a public Python surface in `bigquery_agent_analytics` that loads ontology + binding, attaches a fingerprint-strict BigQuery-backed concept-index lookup, and exposes two reference entity resolvers.

Reader is read-only by design. The emission side is the writer.

## Public surface

```python
from bigquery_agent_analytics import (
    OntologyRuntime,
    EntityResolver,
    ExactEntityResolver,
    LabelSynonymResolver,
    ConceptIndexLookup,
    ConceptIndexRowView,
    ResolverCandidate,
    ConceptIndexError,
    FingerprintMismatchError,
    MetaTableMissingError,
    MetaTableEmptyError,
)
```

## Usage

### In-memory only (no concept index)

```python
from bigquery_agent_analytics import OntologyRuntime, ExactEntityResolver

runtime = OntologyRuntime.from_files(
    ontology_path="ont.yaml",
    binding_path="bnd.yaml",
    compiler_version="bigquery_ontology 0.2.3",
)

# Walk the loaded models
print(runtime.entity("CaliforniaRegion").abstract)
print(runtime.synonyms_for("Region"))           # ('Area', 'Zone')
print(runtime.schemes_for("CaliforniaRegion"))  # ('GeoScheme',)
print(runtime.notation_for("CaliforniaRegion")) # 'CA'

# Exact-name resolution without any BigQuery roundtrip
candidates = ExactEntityResolver(runtime).resolve("CaliforniaRegion")
```

### With concept-index lookup

```python
from google.cloud import bigquery
from bigquery_agent_analytics import OntologyRuntime, LabelSynonymResolver

runtime = OntologyRuntime.from_files(
    ontology_path="ont.yaml",
    binding_path="bnd.yaml",
    compiler_version="bigquery_ontology 0.2.3",
    concept_index_table="my-project.my_dataset.concept_index",
    bq_client=bigquery.Client(project="my-project", location="US"),
)

# Eager fingerprint verification ran inside from_files;
# if it had failed the constructor would have raised
# FingerprintMismatchError before returning.

resolver = LabelSynonymResolver(runtime)
candidates = resolver.resolve("California")
# Returns ResolverCandidate(entity_name=..., matched_label=...,
#                           matched_label_kind='name'|'pref'|'alt'|...,
#                           compile_fingerprint=...)
```

## `OntologyRuntime` accessors

| Method | Returns | Notes |
|--------|---------|-------|
| `entity(name, *, case_insensitive=False)` | `Entity \| None` | Single-entity lookup. |
| `entities()` | `tuple[Entity, ...]` | Declared order. |
| `relationship(name)` | `Relationship \| None` | Single-rel lookup. |
| `relationships()` | `tuple[Relationship, ...]` | Declared order. |
| `synonyms_for(entity_name)` | `tuple[str, ...]` | The `synonyms:` YAML field. |
| `schemes_for(entity_name)` | `tuple[str, ...]` | `skos:inScheme` annotation values (scalar OR list). |
| `notation_for(entity_name)` | `str \| None` | First `skos:notation` value if any. |
| `labels_for(entity_name)` | `tuple[(label, kind), ...]` | Name + synonyms + `skos:prefLabel` / `skos:altLabel` / `skos:hiddenLabel` (with or without `@<lang>`). Kinds match the concept-index emission vocabulary. |
| `annotations_for(entity_name)` | `dict[str, AnnotationValue]` | Raw annotations. |
| `compile_fingerprint` (property) | `str` | Locally-computed full 64-hex sha256. |
| `compile_id` (property) | `str` | 12-hex display token. |

## `EntityResolver` Protocol

Single method: `resolve(query, *, limit=10) -> list[ResolverCandidate]`.

Reference implementations:

* **`ExactEntityResolver(runtime, *, case_insensitive=False)`** — in-memory match on `entity_name`. Returns at most one candidate (entity_name is unique). No BigQuery roundtrip.
* **`LabelSynonymResolver(runtime)`** — BQ-backed match against the concept-index `label` / `synonym` / `notation` rows. Requires `runtime.concept_index`. Re-ranks results by label-kind priority (`name > pref > alt > hidden > synonym > notation`); within a kind, the emission's stable sort order is preserved.

**Out of scope for this slice** (explicit non-goals): embedding-backed resolvers, LLM-driven matching, fuzzy / Levenshtein matching, cross-language fallback. The Protocol surface stays small enough that fuzzier resolvers can be added in future PRs without touching `OntologyRuntime`.

## Trust contract — fingerprint-strict reads

Same discipline as Phase C compiled extractors: stale provenance must never produce a confident match. The fingerprint check runs at **three points**:

1. **Construction-time, eager.** `OntologyRuntime.from_files(...)` / `from_models(...)` calls `ConceptIndexLookup.verify()` when a `concept_index_table` is supplied. The runtime computes the expected fingerprint locally via `compile_fingerprint(fingerprint_model(ontology), fingerprint_model(binding), compiler_version)` and compares against the `__meta` sibling table's row. Mismatch → `FingerprintMismatchError` raised before the constructor returns.
2. **Explicit re-check.** `runtime.concept_index.verify()` is exposed as a public method so callers can re-check before a long batch.
3. **Per-query defense in depth.** Every `lookup_*` SQL query includes `WHERE compile_fingerprint = @expected_compile_fingerprint`. Even if the table is swapped or partially corrupted between verify and query, rows with a stale fingerprint can't surface in the result.

### Stable failure codes

| Exception | Trigger |
|-----------|---------|
| `FingerprintMismatchError` | `__meta` row's `compile_fingerprint` differs from the locally-computed value. The table was compiled from a different ontology + binding (or different compiler version). |
| `MetaTableMissingError` | The `__meta` sibling doesn't exist or the query failed. Without it, the reader has no fingerprint to compare and must fail-closed. |
| `MetaTableEmptyError` | `__meta` exists but contains zero rows. PR #92 emits exactly one meta row; an empty table indicates manual tampering. |

All three subclass `ConceptIndexError` for blanket-catch.

## Concept-index lookup API

| Method | Use case |
|--------|----------|
| `lookup_by_label(label, *, case_insensitive=True, label_kinds=None, language=None, limit=100)` | "Find concepts matching this label." Backs `LabelSynonymResolver`. |
| `lookup_by_entity_name(entity_name, *, label_kinds=None, limit=100)` | "Show me every label for this concept." Inverse direction. |
| `lookup_by_notation(notation, *, limit=100)` | "Find concepts by notation code." Exact match (no case folding — notations are display tokens like `"ACME-7"`). |

Every method returns `list[ConceptIndexRowView]` carrying the full emission schema (entity_name, label, label_kind, notation, scheme, language, is_abstract, compile_id, compile_fingerprint).

## Tests

CI suite — `tests/test_ontology_runtime.py` (39 cases) using in-memory fake BigQuery clients:

- **`TestOntologyRuntimeConstruction`** (5) — in-memory + from-files factories; `concept_index_table` requires `bq_client`; eager fingerprint verification at construction; matching-fingerprint happy path.
- **`TestOntologyRuntimeAccessors`** (10) — entity / relationships lookup, declared-order, case-sensitivity, synonyms / annotations / schemes / notation / labels traversal (covers SKOS `inScheme` list + scalar normalization, language-suffixed annotations), provenance properties (compile_fingerprint / compile_id).
- **`TestConceptIndexLookupVerify`** (4) — happy path, mismatch, missing meta table, empty meta table.
- **`TestConceptIndexLookupQueries`** (8) — label / entity_name / notation lookups; `WHERE compile_fingerprint = @expected_fp` defense-in-depth lock; label-kind / language / case-insensitive filters; empty result not an error.
- **`TestExactEntityResolver`** (5) — known entity, missing entity, case-sensitivity (default + opt-in), empty query.
- **`TestLabelSynonymResolver`** (5) — requires concept index; happy path; label-kind priority re-ranking (`name > pref > alt > hidden > synonym > notation`); limit cap; empty query.
- **`TestEntityResolverProtocol`** (2) — both reference resolvers satisfy `isinstance(resolver, EntityResolver)`.

Live BQ suite — `tests/test_ontology_runtime_live.py` (1 case), gated behind `BQAA_RUN_LIVE_TESTS=1` + `BQAA_RUN_LIVE_ONTOLOGY_RUNTIME_TESTS=1` + `PROJECT_ID` + `DATASET_ID`. Compiles a tiny ontology to concept-index SQL via PR #92's emission path, executes the DDL to create real BQ tables, attaches the runtime, runs `LabelSynonymResolver` + notation lookups, asserts every candidate carries the runtime's `compile_fingerprint`, drops the tables on the way out.

## Out of scope (deferred)

- **Embedding / LLM-backed resolvers** — future PRs can layer fuzzier matching on top of the `EntityResolver` Protocol without changing `OntologyRuntime`'s surface.
- **Cross-language fallback** — `lookup_by_label` filters by language when asked; no automatic "if French missed, try English."
- **Mutation** — read-only by design. The emission side (`gm compile --emit-concept-index`) is the writer.
- **Result ranking by user signals** — candidates come back in the emission's stable sort + label-kind priority. Ranking by usage / recency / context belongs in the consumer.

## Related

- [PR #92 concept-index emission](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/92) — the writer side. The reader verifies against the meta rows that emission produces.
- [`docs/entity_resolution_primitives.md`](entity_resolution_primitives.md) — the broader entity-resolution RFC `EntityResolver` slots into.
- [`docs/implementation_plan_concept_index_runtime.md`](implementation_plan_concept_index_runtime.md) — A-series (emission) shipped; this PR ships the B-series reader scope.
