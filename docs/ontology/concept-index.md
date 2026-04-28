# Concept Index — Reference (Phase 1)

Status: draft
Scope: the BigQuery sidecar tables emitted by `gm compile --emit-concept-index`. Phase 1 ships the SQL emission only; the runtime consumer in `bigquery_agent_analytics` (`OntologyRuntime`, resolvers, strict verification) lands in Phases 2–3 per [`docs/implementation_plan_concept_index_runtime.md`](../implementation_plan_concept_index_runtime.md). Companion to [`compilation.md`](compilation.md) (`gm compile` core) and [`cli.md`](cli.md) (`gm` flag reference).

The concept index is **opt-in**: a `gm compile` invocation without `--emit-concept-index` is byte-identical to today and writes no extra DDL.

## 1. What it is

Two BigQuery tables emitted as a sidecar to the property-graph DDL:

- **`<output_table>`** — the main concept index, one row per `(entity_name, label, label_kind, language, scheme)` tuple, plus per-entity provenance columns. Resolvers run SQL against this.
- **`<output_table>__meta`** — a single-row sibling carrying the full provenance fingerprints. The runtime layer uses this to verify that the index in BigQuery still corresponds to the `(Ontology, Binding)` it was loaded from.

Both tables are written in the same `gm compile` run via `CREATE OR REPLACE TABLE T AS SELECT * FROM UNNEST(ARRAY<STRUCT<...>>[...])`. Each statement is atomic per BigQuery's DDL semantics; pair-consistency between main and meta is enforced at runtime via a shared `compile_fingerprint` rather than a DDL transaction (BigQuery doesn't have those for cross-table DDL).

## 2. When to use it

The concept index is the read-side fabric for **Direction 3** of the SDK — runtime entity resolution from free-text strings to declared ontology entities (see [issue #58][issue58]). Concrete callers:

- An eval pipeline that asks *"of N free-text `geo:` values in yesterday's traces, how many resolve against the GAM DMA scheme?"*
- A curation script that canonicalizes a column of historical user inputs into declared entity keys for an eval dataset.
- A pre-processing job that resolves brief parameters against the ontology before briefs are enqueued downstream.

A future Phase 2 will ship `OntologyRuntime` + `EntityResolver` in `bigquery_agent_analytics` as the Python surface over this index. For Phase 1 (and for bulk analytics in general), a SQL pushdown directly against the index table is the supported pattern; see §8 "Common SQL patterns" below.

## 3. Emit the SQL

The CLI flow is identical to plain `gm compile`, with two additional flags:

```bash
gm compile finance-bq-prod.binding.yaml \
  --emit-concept-index \
  --concept-index-table my-proj.my_ds.ontology_concept_index \
  -o graph_ddl.sql
```

Or to stdout:

```bash
gm compile binding.yaml \
  --emit-concept-index \
  --concept-index-table my-proj.my_ds.ontology_concept_index \
  | bq query --nouse_legacy_sql
```

`gm compile` emits SQL text only; it does not call BigQuery. Executing the emitted SQL requires `bigquery.tables.create` on the target dataset for the main and `__meta` tables.

### Flag reference

| Flag | Required when emit is set | Purpose |
|---|---|---|
| `--emit-concept-index` | — | Opt-in toggle. Without it, output is byte-identical to plain `gm compile`. |
| `--concept-index-table <project.dataset.table>` | yes | Fully-qualified destination for the main table. The `__meta` sibling is suffixed automatically. No silent global default. Must match `^[A-Za-z0-9_-]+$` per segment; backticks rejected. |
| `--compiler-version <str>` | no | Override the version string flowed into `compile_fingerprint`. Defaults to the installed package version (`bigquery_ontology X.Y.Z`). Pin this when reproducing an older index from a checkout. |

### Errors

- **`--emit-concept-index` without `--concept-index-table`** → exit 2, `cli-missing-flag`. No silent global default per the RFC.
- **`--concept-index-table` without `--emit-concept-index`** → exit 2, `cli-orphan-flag`. Surfaces typos rather than silently dropping the value.
- **Invalid `--concept-index-table` value** (not three segments, contains backticks, invalid characters in a segment) → exit 1, structured error with the exact reason.
- **Zero-row case** → exit 1. The compiler refuses to emit a typeless empty array. This fires only when the ontology declares no abstract entities **and** the binding references no concrete entities (the row builder produces an empty list). An abstract-only ontology compiles successfully — abstract entities are always included regardless of binding (per the scope rule in §4 below).

## 4. Main table schema

The schema below is the **logical** shape of each row. `NOT NULL` annotations are invariants the row builder maintains by construction, not BigQuery-enforced constraints — see the "Constraints" note immediately after the snippet.

```sql
CREATE TABLE `<output_table>` (
  entity_name         STRING NOT NULL,
  label               STRING NOT NULL,   -- for label_kind='notation', holds the notation value
  label_kind          STRING NOT NULL,   -- 'name' | 'pref' | 'alt' | 'hidden' | 'synonym' | 'notation'
  notation            STRING,            -- per-entity display, repeats across rows of the same entity
  scheme              STRING,            -- skos:inScheme / topConceptOf membership; NULL if none
  language            STRING,            -- BCP-47 from skos:prefLabel@<lang> etc.; NULL for default
  is_abstract         BOOL   NOT NULL,   -- true for SKOS-derived informational entities
  compile_id          STRING NOT NULL,   -- 12-hex display token; same on every row of a compile
  compile_fingerprint STRING NOT NULL    -- 64-hex canonical integrity key
);
```

**Constraints.** The Phase 1 emitter writes the table via `CREATE OR REPLACE TABLE T AS SELECT * FROM UNNEST(ARRAY<STRUCT<...>>[...])` (CTAS). BigQuery's CTAS path does **not** carry `NOT NULL` from the underlying STRUCT into table-level column constraints — the resulting columns are nullable in `INFORMATION_SCHEMA.COLUMNS`. The annotations above are the row builder's contract: every row it emits populates the six "NOT NULL" columns, by construction. If a downstream caller wants BigQuery-enforced constraints, the runbook is to wrap the emitted CTAS in an explicit two-step `CREATE OR REPLACE TABLE T (col STRING NOT NULL, ...) AS SELECT ...` form locally (or run `ALTER TABLE T ALTER COLUMN col SET NOT NULL` after the fact). The Phase 1 emitter doesn't do either — keeping the schema CTAS-only is intentional for atomicity per statement and keeps the SQL byte-deterministic.

### Row multiplicity

One row per `(entity_name, label, label_kind, language, scheme)` membership tuple. A concept in 3 schemes × 5 labels emits 15 rows. `DISTINCT entity_name` over a multi-scheme concept returns 1.

### Label sources

| Source | Becomes | Notes |
|---|---|---|
| `Entity.name` | `label_kind='name'` row | Always emitted |
| `annotations["skos:prefLabel"]` | `label_kind='pref'` | `@<lang>` suffix populates the `language` column |
| `annotations["skos:altLabel"]` | `label_kind='alt'` | Same |
| `annotations["skos:hiddenLabel"]` | `label_kind='hidden'` | Same |
| `Entity.synonyms` | `label_kind='synonym'` | See "Known v1 limitation" below |
| `annotations["skos:notation"]` | One `label_kind='notation'` row + populates the per-row `notation` column on every row of the entity | Resolvers searching by `label` catch notation matches without a separate predicate |

### Scope rule

- **Abstract entities** are always included regardless of binding — they're informational and never need to be bound to a table.
- **Concrete entities** are included iff they appear in `binding.entities`.

### Schemes

`annotations["skos:inScheme"]` and `annotations["skos:topConceptOf"]` are unioned and deduped. A concept declared as the top of a scheme is a member of that scheme. Entities with no scheme membership produce rows with `scheme = NULL`.

### `label_kind` priority (resolver-side)

The schema's `label_kind` column is sorted lexicographically inside the emitted SQL only for byte-deterministic output. The **semantic** priority for resolver dedup is:

```sql
CASE label_kind
  WHEN 'name'     THEN 1
  WHEN 'pref'     THEN 2
  WHEN 'alt'      THEN 3
  WHEN 'hidden'   THEN 4
  WHEN 'synonym'  THEN 5
  WHEN 'notation' THEN 6
END AS priority
```

Resolvers `ORDER BY priority ASC` to pick the strongest label per entity. Never `ORDER BY label_kind` directly — the alphabetical order (`alt < hidden < name < notation < pref < synonym`) is wildly wrong.

### Known v1 limitation

The current OWL importer flattens selected-language `skos:prefLabel` / `skos:altLabel` / `skos:hiddenLabel` into `Entity.synonyms` (a flat list with no kind distinction). Non-selected-language labels keep their kind via `@<lang>`-suffixed annotation keys. So in v1, English-default SKOS imports produce `label_kind='synonym'` for what was originally pref/alt/hidden. This is acceptable: the resolver still returns these rows; they just don't outrank a plain `name` match in the priority above. A future OWL-importer fix that preserves selected-language kinds picks up the richer `label_kind` set without any change to the row builder or the runtime.

## 5. `__meta` table schema

Same logical-vs-enforced caveat as §4 applies: `NOT NULL` is a row-builder invariant, not a BigQuery-enforced column constraint, because the emitter uses CTAS.

```sql
CREATE TABLE `<output_table>__meta` (
  compile_fingerprint  STRING NOT NULL,  -- 64-hex canonical integrity key
  compile_id           STRING NOT NULL,  -- 12-hex display token
  ontology_fingerprint STRING NOT NULL,  -- "sha256:<64 hex>" of the validated Ontology
  binding_fingerprint  STRING NOT NULL,  -- "sha256:<64 hex>" of the validated Binding
  target_project       STRING NOT NULL,  -- from binding.target.project
  target_dataset       STRING NOT NULL,  -- from binding.target.dataset
  compiler_version     STRING NOT NULL   -- from --compiler-version (or package default)
);
```

Single row per emit. The meta table never contains historical compile data — each `gm compile --emit-concept-index` overwrites it.

**Atomicity contract.** Each `CREATE OR REPLACE TABLE` is atomic on its own (BigQuery DDL semantics). The main and `__meta` tables, however, are written as **two separate statements** and are not replaced in one cross-table transaction; BigQuery has no DDL transactions across tables. There is therefore a small refresh window where an external reader could observe the old main with the new meta, or vice versa. The shared `compile_fingerprint` exists exactly for this case: a runtime reader runs a pair-consistency check (see §6 "Provenance contract" and the manual SQL in §8) and treats a `main.compile_fingerprint != meta.compile_fingerprint` mismatch as "refresh in progress" rather than as steady-state corruption.

## 6. Provenance contract — Option 2

The concept index carries two provenance columns with **distinct roles**:

| Column | Role | Width | Used by |
|---|---|---|---|
| `compile_id` | **Display/debug token only.** Reports, queue rows, error messages, log lines. **Never the sole freshness check.** | 12 hex chars | Operator UX |
| `compile_fingerprint` | **Canonical integrity key.** Full SHA-256 over `ontology_fingerprint \|\| binding_fingerprint \|\| compiler_version` (NUL-delimited UTF-8). | 64 hex chars | Strict pair-consistency + runtime verification |

Structural invariant: `compile_id == compile_fingerprint[:12]`. The short form is always derived from the full form, never the reverse. This is enforced inside `_fingerprint.py` (the function `compile_id` literally returns `compile_fingerprint(...)[:12]`). A future refactor cannot let the two drift out of sync — see RFC §11 "Decisions pinned" (Option 2).

The Phase 3 strict-verification layer (when it lands) will use `compile_fingerprint` exclusively; `compile_id` is not on the verification path. A reducer "optimization" that swaps a strict query from `compile_fingerprint` to `compile_id` would reintroduce a 48-bit collision hole — the W2 watchpoint in the implementation plan calls this out and the Phase 3 tests will pin it.

## 7. Determinism

Re-running `gm compile --emit-concept-index --concept-index-table T` with the same inputs produces **byte-identical** SQL. This is a Phase 1 acceptance criterion (D2 in the plan). Determinism is built up at every layer:

| Layer | Stable across runs |
|---|---|
| Ontology fingerprint | Pydantic `model_dump(mode="json", exclude_none=False)` → sort-keyed compact JSON → SHA-256. Non-semantic YAML edits (whitespace, comments, key order in source) produce identical fingerprints. |
| Binding fingerprint | Same recipe. |
| `compile_fingerprint` | NUL-delimited UTF-8 of the three inputs, SHA-256. Same digest for the same triple. |
| Row builder | Sorted `(scheme, entity_name, label_kind, language, label, notation, is_abstract)` with NULLs last. |
| Emitter | Fixed column order, `None → NULL`, `bool → TRUE/FALSE` upper, `'` → `\'`, control chars → named or `\xHH` escapes, no timestamps. |

If `--compiler-version` is left at its default (the installed package version), the output is stable as long as the package version doesn't change between invocations. Pin `--compiler-version` explicitly when you need the same digest across upgrades.

## 8. Common SQL patterns

A future Phase 2 will ship `OntologyRuntime` + resolvers as a Python surface over this index. Until then — and as the supported path for bulk analytics regardless — SQL pushdown directly against the index is the natural pattern. The patterns below are written against the schema this PR emits; nothing in them depends on Phase 2 / 3 code shipping.

### Bulk resolution report

How many free-text `geo:` values in yesterday's traces resolve against the GAM DMA scheme?

```sql
SELECT
  JSON_VALUE(e.content, '$.args.geo')        AS raw_geo,
  ci.entity_name                              AS resolved,
  COUNT(*)                                    AS n
FROM `proj.ds.agent_events` e
LEFT JOIN `proj.ds.ontology_concept_index` ci
  ON LOWER(ci.label) = LOWER(JSON_VALUE(e.content, '$.args.geo'))
  AND ci.scheme = 'NielsenDMA'
WHERE e.event_type = 'TOOL_STARTING'
  AND DATE(e.timestamp) = CURRENT_DATE() - 1
GROUP BY raw_geo, resolved
ORDER BY n DESC;
```

### Coverage by scheme

What fraction of declared concepts in each scheme has been observed in production traces?

```sql
WITH observed AS (
  SELECT DISTINCT ci.entity_name, ci.scheme
  FROM `proj.ds.agent_events` e
  JOIN `proj.ds.ontology_concept_index` ci
    ON LOWER(ci.label) = LOWER(JSON_VALUE(e.content, '$.args.geo'))
  WHERE e.event_type = 'TOOL_STARTING'
)
SELECT
  ci.scheme,
  COUNT(DISTINCT ci.entity_name)            AS declared,
  COUNT(DISTINCT obs.entity_name)            AS observed,
  SAFE_DIVIDE(COUNT(DISTINCT obs.entity_name),
              COUNT(DISTINCT ci.entity_name)) AS coverage
FROM `proj.ds.ontology_concept_index` ci
LEFT JOIN observed obs USING (entity_name, scheme)
WHERE ci.scheme IS NOT NULL
GROUP BY ci.scheme
ORDER BY coverage ASC;
```

### Winning-label dedup (one candidate per entity)

```sql
SELECT entity_name, label, label_kind, scheme
FROM (
  SELECT
    entity_name, label, label_kind, scheme,
    ROW_NUMBER() OVER (
      PARTITION BY entity_name
      ORDER BY
        CASE label_kind
          WHEN 'name'     THEN 1
          WHEN 'pref'     THEN 2
          WHEN 'alt'      THEN 3
          WHEN 'hidden'   THEN 4
          WHEN 'synonym'  THEN 5
          WHEN 'notation' THEN 6
        END,
        label  -- lexicographic tiebreak
    ) AS rn
  FROM `proj.ds.ontology_concept_index`
  WHERE LOWER(label) = LOWER(@input)
)
WHERE rn = 1;
```

### Strict pair-consistency check (manual)

```sql
-- Both queries should return one row.
SELECT DISTINCT compile_fingerprint
FROM `proj.ds.ontology_concept_index`;

SELECT compile_fingerprint, ontology_fingerprint, binding_fingerprint
FROM `proj.ds.ontology_concept_index__meta`;
```

A future Phase 3 will wire the strict-verification layer into `OntologyRuntime` so it runs these checks automatically on first access and on a configurable TTL. Until then, the queries above are the supported manual path for ad-hoc operator inspection — and they remain useful afterwards for one-off debugging even once the runtime layer ships.

## 9. Out of scope (Phase 1)

- **Shadow-swap fallback for `>50K` rows** (A6) — Phase 3 deferred. v1 emits a single inline-UNNEST statement; very large indices may need a `_shadow` rename pattern. Tracked in the implementation plan.
- **Embedding-fuzzy matching** — `AI.EMBED` over labels + `ML.DISTANCE` is a future composition, not in core. See RFC §12.
- **Live-agent resolver package** — the `bigquery_agent_analytics` SDK is the trace-consumption side; turn-time resolution from a live agent is a separate future package. See RFC §11.

## 10. Related

- [Issue #58][issue58] — the design RFC for runtime entity resolution.
- [`docs/entity_resolution_primitives.md`](../entity_resolution_primitives.md) — full RFC text.
- [`docs/implementation_plan_concept_index_runtime.md`](../implementation_plan_concept_index_runtime.md) — phased build plan.
- [`compilation.md`](compilation.md) — `compile_graph` and the property-graph DDL pipeline this composes with.
- [`cli.md`](cli.md) — full `gm` CLI reference.

[issue58]: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/58
