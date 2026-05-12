# Migration v5 Demo — Fixture Foundation

**Status:** Phase 1 of [issue #107](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — fixtures only. The four-guarantee notebook (`examples/migration_v5_demo_notebook.ipynb`) is a follow-up commit on this branch.

This directory holds the inputs the four-guarantee MAKO demo notebook consumes. The fixtures are checked in *before* the notebook so the contracts that bake in here (entity subset, primary-key strategy, SKOS notation behavior, event shape) can be reviewed independently — not buried inside 30 executed notebook cells.

## What's in here

| File | Origin | Purpose |
|------|--------|---------|
| `mako_core.ttl` | **User-authored input.** Pulled from the [reference gist](https://gist.github.com/haiyuan-eng-google/a69ff6282ebcc877f77f9aa4e3db1afd). | The real MAKO ontology (Yahoo Monetization Platform's "Monetization Agents Knowledge Ontology"). Domain-agnostic decision semantics, agent coordination, outcome tracking. |
| `ontology.yaml` | **Auto-generated.** `gm import-owl mako_core.ttl --include-namespace https://ontology.yahoo.com/mako/`. | Full 41-entity auto-import of MAKO. Has 17 `FILL_IN` placeholders for `keys.primary` (the importer can't infer primary keys from OWL alone). The notebook displays this in Section 0 to show the realistic "import → resolve FILL_INs → curate" workflow. |
| `ontology_demo.yaml` | **Hand-curated.** | 5-entity demo subset of MAKO with `FILL_IN`s resolved. Validates clean against `gm validate`. See "Design decisions" below for the curation rationale. |
| `binding.yaml` | **Auto-scaffolded.** `gm scaffold --ontology ontology_demo.yaml --dataset migration_v5_demo --project test-project-0728-467323 --out .`. | The "one file in, two files out" minimum-input path. Maps the 5 demo entities to BigQuery tables. |
| `table_ddl.sql` | Auto-scaffolded with `binding.yaml`. | Companion DDL. |
| `property_graph.sql` | **User-authored** (Beat 1's "you own the graph definition" evidence). | `CREATE PROPERTY GRAPH` for the demo subset with a `__DATASET__` placeholder for per-run dataset substitution. |
| `seed_events.py` | Demo-specific. | Deterministic seeded RNG generator → 404 events across 50 sessions. Same seed always produces byte-identical output so the notebook is reproducible. |
| `reference_extractors.py` | Demo-specific. | Handwritten reference extractor for `mako_decision` events + the exact `EXTRACTORS` / `RESOLVED_GRAPH` / `SPEC` module contract `bqaa-revalidate-extractors` requires. |
| `revalidation_thresholds.json` | Demo-specific. | Threshold gate values for the revalidation CLI in Beat 3. |

## Design decisions — open for review

These are the fixture contracts that get expensive to revisit once the notebook is built on top. Push back here, not in the notebook PR.

### 1. 5-entity demo subset of MAKO

MAKO has **41 entity classes**; the demo features **5**: `AgentSession`, `DecisionPoint`, `Candidate`, `SelectionOutcome`, `ContextSnapshot`.

These form a coherent narrative — *a session contains decision points; each picks among candidates against a context; selection produces an outcome.* The other 36 entities (BusinessConstraint, RewardComputation, AgentDelegation, HumanReviewGate, ModelVersion, InterAgentMessage, ...) scaffold the same way; the notebook calls this out explicitly in Section 0 ("here's the auto-import for all 41; we focus on these 5 for the four-guarantee narrative").

**Alternative I rejected:** ship the full 41 entities. Forces the notebook to deal with FILL_IN resolution across 17 entities + a binding spec touching all 41 + a property graph DDL with many more edges than the narrative needs. The story dilutes; the four guarantees blur.

### 2. `id` as the primary key everywhere

Every demo entity has `keys.primary: [id]` with `id: string`. Matches MAKO's "every artifact has a stable identifier" design contract. The seed events use `_stable_id(prefix, *parts)` (sha256 over `prefix:part1:part2`) so IDs are deterministic across notebook runs.

**Alternative I rejected:** compound natural keys (`session_id + decision_idx`, etc.). More authentic to how production systems often key entities, but it forces the notebook to thread the compound shape through `binding.yaml` properties, the `CREATE PROPERTY GRAPH` `KEY` clauses, and the reference extractor's `node_id` construction. Single string keys keep the four-guarantee story uncluttered.

### 3. `skos:notation` on `DecisionPoint` is **NON-sorted**

`DecisionPoint` declares `skos:notation: [DECISION_POINT, DP]`. First-authored is `DECISION_POINT`; lex-min is `DP`.

This is deliberate — exercises the round-3 lex-min display-token rule from the #58 reader (`notation_for()` returns lex-min, matching PR #92's emission) in Beat 4 of the notebook. If the demo only used single-notation entities, the notebook never proves that bit of the contract end-to-end. The other 4 entities use single-value notations to keep things simple.

### 4. Seeded event shape

- 50 sessions × (1 start + 2–4 (context + decision) pairs + 1 end) = **404 events**.
- Event payload mirrors the BQ AA plugin's `event_type / session_id / span_id / event_timestamp / content` shape, so Beat 3 extractors see the same surface as production.
- 4 decision types — `AUDIENCE_SEGMENT`, `BID_VALUE`, `CREATIVE_VARIANT`, `FREQUENCY_CAP`. MAKO is monetization-platform-flavored; these match the domain.
- Seeded RNG (`_RANDOM_SEED = 20260512`); committed to one seed value, bumped only on intentional corpus change.

**Coverage check:** each guarantee has at least one event the notebook can run against.

| Beat | What it exercises | Events involved |
|------|-------------------|-----------------|
| 1 (own) | `--skip-property-graph` build → no `CREATE OR REPLACE PROPERTY GRAPH` job emitted. | All 404 — Beat 1 needs the build to complete. |
| 2 (validate) | `binding-validate` against a column-renamed table. | All 404 — Beat 2 fails *before* extraction touches events. |
| 3 (extract cheaply) | Compile + measure + revalidate against `mako_decision` events. | 152 `mako_decision` events. |
| 4 (resolve) | `LabelSynonymResolver` against concept-index-backed labels. | None — Beat 4 is over the ontology, not the event corpus. |

### 5. Reference extractor scope

Only `mako_decision` is handwritten. The other event types (`agent_session_started`, `context_captured`, `agent_session_ended`) pass through to the AI-extraction path or are ignored — the notebook frames this as "structured events extract deterministically; AI fills the gaps" per Beat 3.

**Why not also extract `context_captured`?** The notebook needs at least one "AI fills the gap" event type to demonstrate the contrast. Keeping `context_captured` AI-handled gives the demo a clean before/after on AI cost.

## Validation commands run

```bash
# Ontology validates clean.
python -m bigquery_ontology.cli validate examples/migration_v5/ontology_demo.yaml

# Binding validates against the ontology.
python -m bigquery_ontology.cli validate examples/migration_v5/binding.yaml \
    --ontology examples/migration_v5/ontology_demo.yaml

# Seed generator produces 404 events across 50 sessions.
PYTHONPATH=src:examples/migration_v5 python -c "
import seed_events
print(f'events: {len(seed_events.generate_events())}')"

# Reference extractor produces the expected MAKO shape.
PYTHONPATH=src:examples/migration_v5 python -c "
import seed_events, reference_extractors
ev = next(e for e in seed_events.generate_events() if e.event_type == 'mako_decision')
out = reference_extractors.extract_mako_decision_event(ev.to_dict(), None)
print(f'nodes: {len(out.nodes)} -> {[n.entity_name for n in out.nodes]}')
print(f'edges: {len(out.edges)} -> {[e.relationship_name for e in out.edges]}')"
```

All four pass.

## What's NOT in this commit

- The notebook itself (`examples/migration_v5_demo_notebook.ipynb`). Lands as a follow-up commit on the same branch once the fixture shape is settled.
- NODE/FIELD/EDGE synthetic failure fixtures for Beat 3.6. Will land alongside the notebook commit — they're tightly coupled to the cell that exercises them.
- End-to-end execution against real BigQuery. The notebook commit will execute every cell and inline the outputs.
- `docs/README.md` / `CHANGELOG.md` entries. Land with the notebook PR so the index points at a complete artifact.

## Related

- [#107 storyboard](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — per-cell plan the notebook implements.
- [#107 MAKO requirement](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107#issuecomment-4435535476) — "test with the real ontology" comment that pinned the MAKO TTL as the input.
- [Rollout guide](../../docs/extractor_compilation_rollout_guide.md) — Phase C pipeline reference for Beat 3 cells.
- [Ontology runtime reader](../../docs/ontology_runtime_reader.md) — #58 reader API used in Beat 4.
