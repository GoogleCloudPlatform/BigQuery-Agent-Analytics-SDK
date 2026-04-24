# **RFC: Entity resolution primitives for BigQuery-Agent-Analytics-SDK**

**Scope:** Two packages in one repo — `bigquery_ontology` (compiler) and `bigquery_agent_analytics` (Consumption/analytics layer for trace data from BQ Agent Analytics). The RFC does **not** standardize agent-side ranking and business policy.

---

## **1\. Problem**

The SDK today has **two directions** through the ontology stack. A third is missing.

```
Direction 1 (shipping): Schema bridge
    bigquery_ontology.Ontology + Binding
         ↓ runtime_spec / resolved_spec
    SDK GraphSpec / ResolvedGraph
         ↓
    OntologyMaterializer (SQL DDL)
    # Hardened by #68 (abstract-element adapter filter).

Direction 2 (shipping): Population via agent traces
    Agent trace rows in BigQuery
         ↓ extract_graph / extract_biz_nodes / extract_decision_points
    Typed entity + relationship rows (data layer)
    # Uses AI.GENERATE server-side.

Direction 3 (MISSING — this RFC): Trace resolution
    Free-text input from users / tools / sibling agents
         ↓ EntityResolver → concept_index lookup
    Candidate matching a DECLARED entity (schema layer)
```

**What Direction 3 is, in one sentence:** given a free-text string observed in a trace or handed to a pipeline — a geo like `"San Francisco-Stockton-Modesto"`, a format like `"display_static"`, a tool-call argument recorded in the trace — return the ontology entity it refers to.

**What Direction 3 is *not*:**

- Not agent-trace input. The caller supplies the string explicitly.  
- Not new graph rows. The output is a match against entities *declared* in the ontology YAML, whether or not Direction 2 has populated them yet.  
- Not a competitor to the `AI.GENERATE` extraction path. Direction 3 is pure lookup — it finds declared things; it does not create new ones.

**Why this matters.** Before \#58, every vertical reinvented Direction 3 on top of ad-hoc SQL. One production user (agentic media buying) quantified the gap at **\~85% of brief-validation value** and built a 5-layer resolver (notation → lexical → token-set → Jaccard → Levenshtein) over \~10K lines of TTL (274 SKOS concepts, 942 synonyms, 210 GAM DMA display names). It works. Every vertical reinventing the same thing is a missing primitive.

**Scope of this repo.** Two packages, both **not** live-agent libraries:

- **`bigquery_ontology`** — build-time compiler \+ model classes. Consumed by operators (via `gm` CLI) and by any downstream package that needs `Ontology` / `Binding` objects in memory.  
- **`bigquery_agent_analytics`** — **consumption/analytics layer for trace data already in BigQuery.** Consumed by evaluation pipelines, observability dashboards, analysis notebooks, curation scripts, and batch orchestrators. The live-agent side is owned by the **BQ AA Plugin** (separate package — runs inside the agent and writes traces to BQ). This SDK reads what the plugin wrote.

Neither package is designed as a turn-time agent SDK. This RFC does not add an in-agent runtime surface.

**How the three directions compose in production:**

| Direction | Who calls it | When | What happens |
| :---- | :---- | :---- | :---- |
| 1 | Operator / CI | Once per ontology change (build-time) | `gm compile` → DDL \+ concept-index SQL published to BQ. |
| 2 | Batch orchestrator | Scheduled over accumulated traces (post-processing) | `extract_graph` / `extract_biz_nodes` from `bigquery_agent_analytics` → `AI.GENERATE` populates entity / relationship tables. |
| 3 | Eval / analysis / curation pipeline (this RFC) | On accumulated data, at the pipeline's cadence | Pipeline imports `OntologyRuntime` \+ a resolver from `bigquery_agent_analytics` and calls `.resolve(...)` or `.validate_against_ontology(...)`. Each call is a BQ query against the concept index. |

**Typical Direction 3 callers:**

- An eval step that checks "of N free-text `geo:` values in yesterday's traces, how many resolve against the GAM DMA scheme?" — observability and drift reporting.  
- A curation script that canonicalizes a column of historical user inputs into declared entity keys for a training / eval dataset.  
- A pre-processing job that resolves brief parameters against the ontology before briefs are enqueued for downstream processing.

The three directions compose at the **data layer** (entity-key joins in SQL), not via shared Python state. \#58 adds Direction 3 only; Directions 1 and 2 are untouched. **Live-agent resolution at turn time is not solved here** — if a future design requires it, it would live in a separate agent-facing package that may reuse the `EntityResolver` `Protocol` contract but does not belong in a trace-consumption SDK.

## **2\. Goals and non-goals**

**Goals — Direction 3 only.**

- Stable typed read surface over loaded ontologies: annotations, synonyms, notations, scheme membership, abstract-relationship traversal.  
- BigQuery-native concept index compiled from `(Ontology, Binding)` — enables SQL fuzzy via `EDIT_DISTANCE` / `JACCARD` / `SOUNDEX` in one line.  
- `EntityResolver` Protocol \+ two SQL-only references (exact, synonym-aware). Protocol is the contract; implementations swap.  
- Strict provenance by default — no matches from an index that doesn't correspond to the loaded models.

**Non-goals.**

- **A live-agent integration surface.** `bigquery_agent_analytics` is the **consumption/analytics layer** for trace data the BQ AA Plugin already wrote to BQ. It is not designed to be imported by a live agent at turn time. The BQ AA Plugin (separate package) handles the in-agent side. Live-agent resolution — an agent calling a resolver on every turn — would need a separate agent-facing package; pending design, out of scope here.  
- **Direction 1 and 2 behavior.** `runtime_spec` / `resolved_spec` / `OntologyMaterializer` / `extract_graph` / `extract_biz_nodes` all unchanged.  
- A general string-matching library. BigQuery already has the primitives.  
- A 5-layer resolver in core. Domain-tuned thresholds are not portable. See §12 for user-composed variants.  
- Sub-50ms SLA. Latency depends on index size and resolver choice.  
- Concept-scheme browser UI.  
- **Agent-side ranking, business policy, or user-facing copy.** SDK returns structured candidates; the agent composes everything else.

## **3\. Guiding principles**

- **SDK returns candidates; the caller composes policy.** SDK exposes read access over what's declared and returns structured matches. The calling pipeline (eval, curation, analysis — or, in a future agent-facing package, an agent) decides matcher order, thresholds, phrasing. Two reference resolvers ship; domain packs live in `contrib/` or external.  
- **Additive, not coupling.** Direction 3 has no dependency on Direction 2 having run. The concept index is built from the *declared* ontology, not from populated data. A pure-SKOS taxonomy with zero Direction 2 population still supports the full Direction 3 surface.  
- **SQL-first, LLM-optional.** Core resolvers are deterministic SQL. LLM-backed variants compose against the Protocol — see §12.

## **4\. Proposed primitives**

### **4.1 `OntologyRuntime` — read accessor**

Stateless wrapper over a validated `(Ontology, Binding)` pair. No BQ round-trip on construction.

```py
from bigquery_agent_analytics import OntologyRuntime, SynonymResolver

rt = OntologyRuntime.load(
    ontology_path="ontology.yaml",
    binding_path="binding.yaml",
    concept_index_table="my-proj.my_ds.ontology_concept_index",
    # defaults: verify_concept_index="strict", verify_ttl_seconds=60
)

rt.synonyms("DMA")                      # ["Designated Market Area", ...]
rt.annotation("DMA", "skos:notation")   # "807"
rt.in_scheme("NielsenDMA")              # list[Entity]
rt.broader("RetailBanking")             # list[Entity] via skos:broader

result = SynonymResolver(runtime=rt).resolve(
    input_value="Consumer Banking",
    scheme="BankingTaxonomy",           # scheme= XOR entity=, mutually exclusive
    limit=5,
)
```

**Identity rules:** entities are name-addressed (singular). Relationships are **traversal-first** — after \#62's relaxed `(name, from, to)` uniqueness, a `skos_broader` can repeat across endpoint pairs, so no `rt.relationship(name)`.

### **4.2 Concept index (opt-in at compile time)**

Emitted when `gm compile --emit-concept-index --concept-index-table <fqn>` is passed. Default shape: single atomic `CREATE OR REPLACE TABLE ... AS SELECT * FROM UNNEST([...])`. Shadow-swap fallback at \> 50K rows.

```sql
CREATE TABLE `{dataset}.ontology_concept_index` (
  entity_name         STRING NOT NULL,
  label               STRING NOT NULL,   -- for label_kind='notation', holds notation value
  label_kind          STRING NOT NULL,   -- 'name'|'pref'|'alt'|'hidden'|'synonym'|'notation'
  notation            STRING,            -- per-entity display, repeats across rows
  scheme              STRING,            -- NULL = not in any scheme
  language            STRING,
  is_abstract         BOOL   NOT NULL,
  compile_id          STRING NOT NULL,   -- 12 hex chars; display/debug token only
  compile_fingerprint STRING NOT NULL    -- 64 hex chars; canonical integrity key
);
```

**Two provenance columns, one role each.**

| Column | Role | Width | Used by |
|---|---|---|---|
| `compile_id` | Display/debug token — human-readable short tag for reports, queue rows, error messages, log lines. **Never the sole freshness check.** | 12 hex chars | Operator UX, dashboards, triage output |
| `compile_fingerprint` | Canonical integrity key — full SHA-256 over `ontology_fingerprint \|\| binding_fingerprint \|\| compiler_version`. | 64 hex chars | Strict pair-consistency + runtime verification (§5) |

Structural invariant: `compile_id == compile_fingerprint[:12]`. The short form is always derivable from the full form; never the other way around. Enforced at the `_fingerprint.py` module boundary so a future refactor cannot make them diverge.

**Row multiplicity:** one row per `(entity_name, label, label_kind, language, scheme)` tuple — concept in 3 schemes × 5 labels \= 15 rows. Resolvers filter by scheme without JOIN.

**Scope rule:** all abstract entities (informational — always included); concrete entities iff bound in the binding being compiled.

### **4.3 `EntityResolver` Protocol \+ references**

- `ExactMatchResolver` — `WHERE label = @input`. Catches name \+ notation \+ synonym.  
- `SynonymResolver` — extends with label-kind preference order.

**Scope:** `scheme=` and `entity=` mutually exclusive. Neither or both → `ValueError`. Narrower-closure deferred to v2.

**Dedup:** one candidate per entity. Winning-label priority: `name > pref > alt > hidden > synonym > notation`, lexicographic tiebreak. `limit=N` returns N distinct entities.

### **4.4 Validation**

`rt.validate_against_ontology(values, *, scheme=None, entity=None, sample_limit=20) → ValidationResult` with bounded output (`known_value_count`, `known_values_sample`). `candidates` stays `None` unless the caller composes validation with a resolver — keeps `validate` pure set-membership.

### **4.5 Trace-native consumption is a composition, not the primitive**

A `Trace` is not a single resolvable value — it's a container of many candidate values with different semantics and potentially different scopes (tool args, extracted structured values, user text, event metadata; probably **not** arbitrary model-response prose). Making the resolver Protocol accept `Trace | str` would blur two layers:

- **Matching** a value against ontology entities (resolver's job).  
- **Extracting** candidate values from telemetry (consumer's job — scope, field choice, and extraction policy all vary by domain).

v1 keeps the two layers separate by design:

- **Interactive / single-value** — use the Python `EntityResolver` Protocol (`resolve(input_value, scheme=...)`). Atomic operation.  
    
- **Bulk analytics over trace/event tables** — use documented **SQL pushdown** patterns against the concept-index table. BigQuery's natural execution model; no Python loop required. For example:

```sql
-- Resolve every tool-call geo arg in yesterday's traces against the GAM DMA scheme.
SELECT
  JSON_VALUE(e.content, '$.args.geo')       AS raw_geo,
  ci.entity_name                             AS resolved,
  COUNT(*)                                   AS n
FROM `proj.ds.agent_events` e
LEFT JOIN `proj.ds.ontology_concept_index` ci
  ON LOWER(ci.label) = LOWER(JSON_VALUE(e.content, '$.args.geo'))
  AND ci.scheme = 'NielsenDMA'
WHERE e.event_type = 'TOOL_STARTING'
  AND DATE(e.timestamp) = CURRENT_DATE() - 1
GROUP BY raw_geo, resolved;
```

Docs (`docs/ontology/concept-index.md`, from A8) will carry two or three canonical SQL patterns: bulk resolution report, resolution-drift report, coverage by scheme.

**What is explicitly out of v1:** a trace-field helper (e.g., a `TraceFieldResolver` wrapper that iterates configured trace fields and calls the Protocol per field). See §12 — it's deferred until real field patterns stabilize, and when it lands it should be a separate wrapper class rather than methods on `OntologyRuntime` (which would mix ontology access, extraction policy, and resolver orchestration into one surface).

## **5\. Verification — defaults and behavior matrix**

**This is the correctness gate. These defaults ship.**

| `verify_concept_index` | First access: missing `__meta` | First access: fingerprint mismatch | TTL re-check stale |
| :---- | :---- | :---- | :---- |
| `"strict"` (**default**) | `ConceptIndexProvenanceMissing` | `ConceptIndexMismatchError` | `ConceptIndexInconsistentPair` / `ConceptIndexRefreshed` |
| `"missing_ok"` | Silently proceed | `ConceptIndexMismatchError` | Same as strict |
| `"off"` | Silently proceed | Silently proceed | Skipped |

| `verify_ttl_seconds` | Behavior |
| :---- | :---- |
| `60` (**default**) | Cached verification is fresh for 60s of wall time; past that → re-check |
| `0` | Re-check on every resolve / validate call |
| `None` | Snapshot-bound — verify once on first access, never re-check |

**TTL re-check queries (stale cache):**

1. `SELECT DISTINCT compile_fingerprint FROM {output_table} LIMIT 2` — asserts exactly one value (pair consistency at full-fingerprint resolution). More than one → refresh in progress.
2. `SELECT compile_fingerprint, ontology_fingerprint, binding_fingerprint FROM {output_table}__meta LIMIT 1`.
3. Require: `main.compile_fingerprint == meta.compile_fingerprint` (pair consistency) **and** `meta.ontology_fingerprint == cached.ontology_fingerprint` **and** `meta.binding_fingerprint == cached.binding_fingerprint` (component freshness).

No short-ID arithmetic anywhere on the verification path. `compile_id` never appears in a strict-mode query.

Main/meta disagreement → 2s one-shot retry → persistent \= `ConceptIndexInconsistentPair`. Cache drift \= `ConceptIndexRefreshed` (operator recreates `OntologyRuntime` with updated models).

Why full fingerprints on both tables: see §10 W2.

## **6\. Tie to issue \#57**

Concept-index value is \~80% from SKOS annotations preserved through import (\#57, merged in \#62):

| SKOS | Becomes | Enables |
| :---- | :---- | :---- |
| `skos:notation` | `notation` annotation \+ first-class row | L1 code match trivial |
| `skos:prefLabel` / `altLabel` / `hiddenLabel` | Row per label with `label_kind` | L2 lexical trivial |
| `skos_broader` abstract relationship | `rt.broader()` / `rt.narrower()` traversal | Taxonomy-aware suggestions |
| Abstract entities with `skos_` prefix | `rt.in_scheme()` enumeration | Agent gets taxonomy context |

## **7\. Package changes (status as of main@b7e7361, 2026-04-23)**

### **`bigquery_ontology` — version bump: minor**

| File | Change | Status |
| :---- | :---- | :---- |
| `_fingerprint.py` | **New internal** — `fingerprint_model`, `compile_id` | **\#71 open** |
| `concept_index.py` | New row builder | Pending A2 |
| `graph_ddl_compiler.py` | Add `compile_concept_index`. `compile_graph` unchanged | Pending A3–A5 |
| `cli.py:299` | Add `--emit-concept-index` \+ `--concept-index-table`; no-flag byte-identical | Pending A7 |
| `__init__.py` | Re-export `compile_concept_index` | Pending |
| `ontology_models.py` / `binding_models.py` | **No changes** | — |

### **`bigquery_agent_analytics` — version bump: minor**

| File | Change | Status |
| :---- | :---- | :---- |
| `ontology_runtime.py` | **New** — `OntologyRuntime` \+ verification \+ 4 exceptions | Pending B1–B3, C1–C6 |
| `entity_resolver.py` | **New** — Protocol, `Candidate`, `ResolveResult`, `ExactMatchResolver`, `SynonymResolver` | Pending B4–B7 |
| `__init__.py` | Re-export above | Pending |
| All other modules | **No changes** | — |

**Exceptions** (all raised from `ontology_runtime`):

- `ConceptIndexMismatchError` — first-access fingerprint disagreement.  
- `ConceptIndexProvenanceMissing` — no `__meta` sibling.  
- `ConceptIndexInconsistentPair` — main/meta disagree after 2s retry.  
- `ConceptIndexRefreshed` — TTL re-check detects cache drift.

## **8\. Rollout — shippable per phase**

Each phase leaves `main` shippable. Independently mergeable.

| Phase | Scope | User-visible outcome | Weeks |
| :---- | :---- | :---- | :---- |
| 1 | Compiler foundation (A1–A5, A7, A8 partial) | `gm compile --emit-concept-index` produces a byte-deterministic index \+ meta sibling. Nothing reads it yet. | 2 |
| 2 | SDK read accessors \+ resolver Protocol (verification **off** intermediate) | `OntologyRuntime.load(...)` \+ `ExactMatchResolver` / `SynonymResolver` return correctly deduped candidates. | 2 |
| 3 | Verification layer (strict default on) \+ full shadow-swap | Strict provenance ships. Four exception types raise in documented conditions. | 2 |
| 4 | Integration \+ quickstart \+ docs | `examples/concept_index_quickstart.py` runs end-to-end on a real BQ dataset. Migration note published. | 1 |
| 5 | `contrib/` scaffolding | Reference advertising resolver available as `from bigquery_ontology.contrib.advertising import ...`. | 0.5 |

Single developer ≈ 7.5 weeks. Phases 1 \+ 2 parallelizable → \~4 weeks wall-clock for two developers.

## **9\. Alternatives considered, rejected — with decisive drawback**

| Alternative | Decisive drawback (not reopening) |
| :---- | :---- |
| Methods on `Ontology` / `Binding` directly | Couples pure-data models to runtime verification state — BQ I/O doesn't belong on a validated schema model. |
| Opt-out concept index (emit by default) | `gm compile` has always been pure SQL-text; silent BQ DDL on every compile breaks that contract. |
| YAML-text fingerprints | Non-semantic YAML edits (whitespace, comments, key order) would fire strict verification constantly → operators disable it → worse than no verification. |
| Single-table sentinel for TTL re-check | Reintroduces the meta/main refresh-window race; strict mode then serves wrong data under the banner of "verified." |
| Short-compile-id-only freshness check | 48 bits \= birthday bound \~16M compiles; small probability is not "zero." Strict contract cannot rely on it. |
| Polymorphic `entity=` (scheme if scheme-typed, entity if entity-typed) | Ontology authors changing an entity's shape silently change API semantics; callers need ontology knowledge to predict. |
| Ship full 5-layer resolver in core | Becomes everyone's default despite being domain-tuned for advertising — users in healthcare or legal get the wrong matcher with no warning. |
| Auto-promote `skos:broader` → `extends` (\#57-related) | Silent semantics drift per W3C SKOS primer — informational claim silently becomes formal subsumption. |
| `asyncio` resolver in v1 | No real user has asked; adding sync \+ async both now doubles surface for speculative value. |
| Binding-side index toggle (`index:` on Binding) | v1 ships one surface (CLI). Adding a second without precedence rule invites contradictory configs. |
| "Verify once, cache forever" | Long-lived services sail past an index refresh, returning matches from the new index under stale verification. |

## **10\. Risks and deferred watchlist**

### **Contract watchpoints — invariant, failure mode, regression test**

| \# | Invariant | Failure mode if broken | Regression test |
| :---- | :---- | :---- | :---- |
| W1 | `_fingerprint.py` is the **single** source of canonical serialization; both packages import it | Compiler writes fingerprint X, runtime computes fingerprint Y, strict mode rejects every valid index | `tests/bigquery_ontology/test_fingerprint.py`: round-trip YAML → load → fingerprint; semantic edits change it, whitespace edits don't (landed in \#71) |
| W2 | Strict verification uses `compile_fingerprint` (full 64-hex) on both tables — short `compile_id` never appears on the verification path | A reducer "optimization" to `SELECT compile_id FROM ...` would reintroduce the 48-bit collision hole under an out-of-band swap. A meta-only sentinel would reintroduce the refresh-window race | Assert strict-mode queries reference `compile_fingerprint` only; assert short-ID reducer fails a reintroduction test. Mock main/meta full-fingerprint mismatch and assert `ConceptIndexInconsistentPair` |
| W3 | Shadow-swap is **non-self-healing**; compiler errors out and next `gm compile` resumes | Background retry loops mask partial-swap states; operator "pause traffic during shadow refresh" guidance becomes unenforceable | Inject mid-swap `DROP`/`RENAME` failure → `gm compile` errors with clear message; subsequent `gm compile` completes the swap without recompiling |

### **Deferred (tracked, not blocking)**

- Ontologies \> 100K concepts — shadow-swap activates at 50K; a LOAD-job path may be needed at the next order of magnitude.  
- `{output_table}__current` pointer indirection as a v2 mitigation for shadow transient failures.  
- `asyncio` variants of `EntityResolver.resolve()`.  
- Binding-side opt-in (`index:` block on Binding) with precedence rule.  
- **Opinionated ADK/plugin field mappings** for a future `TraceFieldResolver`. Default field-path → scheme mappings for known ADK plugin trace shapes (`tool_starting.content.args.*`, `hitl_*.content.tool`, etc.). Deferred because the right defaults depend on how users' ontologies carve up tool-call argument schemas — no point ossifying a default before field feedback from v1 pipelines.

## **11\. Decisions pinned (closed)**

- Wrapper (`OntologyRuntime`), not methods on `Ontology`/`Binding`.  
- Opt-in concept index.  
- `typing.Protocol`, not `ABC`.  
- `validate_against_ontology` returns pure set-membership; `candidates` caller-composed.  
- `scheme=` XOR `entity=` in v1. Narrower-closure in v2 only if real callers ask.  
- `contrib/` for reference resolvers; external packages for user-owned domains.  
- Strict verification on by default; `verify_concept_index="off"` is the explicit opt-out.
- **Option 2 for provenance columns: `compile_fingerprint` is the canonical integrity key; `compile_id` is display-only.** Invariant `compile_id == compile_fingerprint[:12]` enforced at the `_fingerprint.py` module boundary. Short-ID arithmetic is forbidden on the strict verification path.

## **12\. Future directions — LLM composition (not in v1)**

v1 ships two deterministic SQL-based resolvers (`ExactMatchResolver`, `SynonymResolver`). The `EntityResolver` `Protocol` is the integration point for LLM-backed variants. All BQ-side LLM calls use `AI.EMBED` / `AI.GENERATE` (GA, no remote-model creation) — the same functions the SDK already uses in `feedback.py`, `insights.py`, and `extract_biz_nodes`.

| Pattern | What it catches | BQ function | Cost per call | Primary risk |
| :---- | :---- | :---- | :---- | :---- |
| **P1. Embedding fuzzy** — compile-time `AI.EMBED` over each label, runtime `ML.DISTANCE` against an input embedding | Typos, casing, rough paraphrases ("consumer banking" ≈ "retail banking") | `AI.EMBED` once per label at compile; one `AI.EMBED` per input \+ one `ML.DISTANCE` query at runtime | Low — one embedding per query, no generation | Confident-but-wrong matches across unrelated domains. Mitigation: threshold \+ `limit=N` |
| **P2. LLM disambiguation pass** — run P0/P1 first; invoke `AI.GENERATE` only when multiple candidates tie or zero match | Ambiguous multi-match ("which Priya?"), empty-result recovery | `AI.GENERATE` on hard cases only | Medium — most calls stay SQL-only | LLM picks something outside top-K. Mitigation: require output to be one of the provided candidates |
| **P3. LLM pre-normalization** — `AI.GENERATE` maps input to canonical form before `ExactMatchResolver` | Informal/free-text → canonical form | `AI.GENERATE` every call | High — LLM on the hot path | Canonical form may not exist in the index. Mitigation: verify match, fall back to P1 candidates |
| **P4. Ontology-grounded LLM resolver** — custom resolver calls `AI.GENERATE` with `rt.in_scheme(...)` enumeration as prompt context, typed `output_schema` | Cross-language, cross-phrasing, anything semantic | `AI.GENERATE` every call, large prompts | Highest — generation \+ grounding overhead | Same hallucination discipline: LLM-output entity must exist in the provided scheme enumeration |

**Rule of thumb:** exact codes / notations → P0 (shipped). Typos, phrasing drift → P1. Ambiguous multi-match → P2. Truly fuzzy / cross-language → P4.

**What's in-scope for a follow-up RFC, not v1:**

- Promoting P1 into core as a reference `EmbeddingResolver`. Requires: compile-time index-augmentation step (a `--embed-labels` flag on `gm compile`?); versioning of the embedding endpoint, because drift between compile-time and query-time embeddings is a **new verification concern** — possibly a W4 watchpoint alongside W1-W3; the `sdk_ai_function` telemetry dimension already lists `ai-embed`, but the compile-site label needs wiring.  
- Whether P2 / P3 / P4 belong in `contrib/` or stay user code.  
- Embedding-model rotation policy: if the operator changes the `AI.EMBED` endpoint, is it a `ConceptIndexRefreshed`\-style signal or silent drift? Likely needs a new `embedding_endpoint` column in `__meta` and a verification hop.  
- **A live-agent resolver package.** `bigquery_agent_analytics` is a trace-consumption SDK; it's not designed to be imported by a live agent at turn time. If real users need turn-time resolution (e.g., an agent grounding a brief argument before calling a tool), the right home is a separate agent-facing package that reuses the `EntityResolver` `Protocol` contract but lives on the live-agent side. Scoping, packaging, and BQ-latency mitigations (caching layer? materialized name→entity map in memory?) belong to that future RFC, not this one.  
- **A trace-field resolver wrapper** (separate class, not methods on `OntologyRuntime`). Takes a `Trace` / `Span` \+ a `{field_path: scheme}` mapping, extracts each field, calls the Protocol per field, returns structured results aligned to the trace. Deliberately kept out of v1: the right field set and extractor policies differ across ontologies and won't stabilize until pipelines built on v1 report which fields actually carry resolvable values. Keeping it as a separate class (tentatively `TraceFieldResolver`) avoids co-mingling ontology access, extraction policy, and resolver orchestration in one surface.

The feedback gist that motivated \#58 implemented P0 \+ P1 \+ P2 \+ P3 \+ P4 as a 5-layer stack. v1 ships P0; users wanting the rest wire them against the Protocol today, and a follow-up issue promotes P1 into core once the telemetry / versioning questions are resolved.

## **13\. References**

[Issue \#58](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/58) · [Issue \#57](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/57) · [PR \#68](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/68) · [PR \#71](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/71) · [In-repo plan](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/docs/implementation_plan_concept_index_runtime.md) · [Feedback gist](https://gist.github.com/haiyuan-eng-google/54c3d3366b3d75b659561ef4e24e9374)  
