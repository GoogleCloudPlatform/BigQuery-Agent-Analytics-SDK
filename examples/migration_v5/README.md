# Migration v5 Demo — Fixture Foundation

**Status:** Phase 1 of [issue #107](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — fixtures only. The four-guarantee notebook (`examples/migration_v5_demo_notebook.ipynb`) is a follow-up commit on this branch.

The fixtures here exist so the four-guarantee MAKO demo notebook can run end-to-end against real BigQuery. **Per the issue's reshape requirement, exactly two files are user-authored inputs:**

| File | Authorship |
|------|------------|
| `mako_core.ttl` | **Authored.** The real MAKO ontology, pulled from the [reference gist](https://gist.github.com/haiyuan-eng-google/a69ff6282ebcc877f77f9aa4e3db1afd). Domain-agnostic decision semantics for Yahoo Monetization Platform. |
| `mako_agent.py` | **Authored.** Loads `mako_core.ttl`, normalizes it (resolves OWL importer `FILL_IN`s + drops cross-namespace dangling relationships), generates a binding for any `(project, dataset)` pair, derives table DDL + property-graph SQL from the binding, and produces a deterministic event stream whose payloads carry only MAKO-declared properties. |

Every other file in this directory is a **reproducibility snapshot** the agent regenerates from those two authored inputs:

| File | Generator |
|------|-----------|
| `ontology.yaml` | `mako_agent.load_mako_ontology()` — `gm import-owl` output, FILL_INs resolved, dangling cross-namespace relationships dropped. |
| `binding.yaml` | `mako_agent.make_binding(ontology, project=..., dataset=...)` — derived from the ontology with the demo entity set. |
| `table_ddl.sql` | `mako_agent.make_table_ddl(binding)` — derived from the binding. |
| `property_graph.sql` | `mako_agent.make_property_graph_sql(binding, ontology=...)` — derived; edge columns match `table_ddl.sql`. |
| `events.jsonl` | `mako_agent.generate_events()` — 398 deterministic events across 50 sessions. |

Run `python mako_agent.py --project X --dataset Y` to regenerate every snapshot for a fresh dataset.

## Design decisions — open for review

These are the choices that get expensive to revisit once the notebook is built on top. Push back here.

### 1. Six-entity demo allowlist (TTL-driven; the rest of MAKO is still loaded)

The full MAKO TTL imports as 18 entities (after dangling-relationship drops); the agent's `DEMO_ENTITIES` allowlist narrows the **binding scope** to six:

- `AgentSession`
- `DecisionExecution` (**the central hub** — MAKO ties everything together through this entity, not through `DecisionPoint` directly)
- `DecisionPoint`
- `Candidate`
- `SelectionOutcome`
- `ContextSnapshot`

This is **agent configuration**, not ontology curation. The full 18-entity ontology is loaded into `Ontology`; the binding just scopes to six. The relationships in the binding are **derived from MAKO's actual declared relationships** by intersecting with the demo entity set — no hardcoded edge list. The agent picks up nine relationships from MAKO without any name authoring:

```
atContextSnapshot:        DecisionExecution → ContextSnapshot
evaluatesCandidate:       DecisionPoint     → Candidate
executedAtDecisionPoint:  DecisionExecution → DecisionPoint
hasSelectionOutcome:      DecisionExecution → SelectionOutcome
partOfSession:            DecisionExecution → AgentSession
rejectedCandidate:        SelectionOutcome  → Candidate
selectedCandidate:        SelectionOutcome  → Candidate
... (and 2 others)
```

### 2. FILL_IN resolution: synthesize `id: string` everywhere

The MAKO TTL doesn't declare `owl:hasKey`, so the OWL importer marks 17 concrete entities' primary keys as `FILL_IN`. The agent resolves each one by synthesizing an `id: string` property + primary key. This matches MAKO's "every artifact has a stable identifier" design contract (per the TTL's role-trait + provenance framing). If a future MAKO revision adds `owl:hasKey` declarations, the resolver leaves those alone — only `FILL_IN` placeholders get rewritten.

### 3. Telemetry envelope vs. MAKO domain model

The agent generates events with a clear separation:

- **MAKO models the domain entities** (`AgentSession.sessionId`, `DecisionExecution.businessEntityId` / `latencyMs` / `spanId` / `traceId`, `DecisionPoint.reversibility`, `ContextSnapshot.snapshotPayload` / `snapshotTimestamp`). `Candidate` and `SelectionOutcome` are structural classes — MAKO declares no data properties on them; events involving them carry only `id` references.
- **The BQ AA plugin telemetry envelope wraps each event** with `event_type` / `session_id` / `span_id` / `event_timestamp` / `content`. The envelope is the plugin's contract, not MAKO's. The agent's events look like what the plugin would emit so the demo's extractors see the same surface as production.

Every field inside the `content` dict either (a) names a MAKO-declared data property of the entity being instanced, or (b) is a foreign-key reference to another MAKO entity. No demo-only invented fields.

### 4. Cross-namespace relationships dropped (with audit trail)

MAKO extends PROV-O + PKO + DCAT. Four relationships in the TTL point to entities outside MAKO's own namespace (`delegatedBy → prov:Agent`, etc.); the OWL importer leaves them declared but without a materialized target. The agent drops these so the ontology loads cleanly and writes the dropped names into the ontology's top-level annotations under `mako_demo:dropped_cross_namespace_relationships`, so the loss is auditable from a loaded model.

### 5. `(project, dataset)` is a generator parameter, not a snapshot value

`make_binding(...)` and `make_property_graph_sql(...)` take `project` + `dataset` arguments. The checked-in `binding.yaml` / `table_ddl.sql` / `property_graph.sql` are snapshots produced against the default `test-project-0728-467323` / `migration_v5_demo` so reviewers can `cat` them, but the notebook regenerates them at runtime against a fresh `migration_v5_demo_<8-hex>` dataset.

### 6. Deterministic events; reproducible across machines

`generate_events(seed=20260512, session_count=50)` produces 398 events:
- 50 `agent_session_started`
- 50 `agent_session_ended`
- 149 `context_captured` (~3 per session via `randint(2, 4)`)
- 149 `mako_decision` (one per decision execution)

Same `(seed, session_count)` always produces byte-identical output. The `mako_decision` events carry MAKO `DecisionExecution` properties + references to the related `DecisionPoint` / `Candidate` / `SelectionOutcome` / `ContextSnapshot` instances so the notebook's Beat 3 extractors can build the full decision flow.

## Validation commands run

```bash
# Agent runs end-to-end and regenerates every snapshot.
PYTHONPATH=src python examples/migration_v5/mako_agent.py
# → {"ontology_entities": 18, "binding_entities": 6,
#    "binding_relationships": 9, "events": 398}

# Generated ontology validates clean.
python -m bigquery_ontology.cli validate examples/migration_v5/ontology.yaml

# Generated binding validates against the generated ontology.
python -m bigquery_ontology.cli validate examples/migration_v5/binding.yaml \
    --ontology examples/migration_v5/ontology.yaml
```

All three pass.

## What's NOT in this commit

- The notebook itself (`examples/migration_v5_demo_notebook.ipynb`). Lands as a follow-up commit on the same branch once the fixture shape is settled.
- Reference extractor for `mako_decision` events. Will be regenerated by the agent (or a sibling helper) once the binding shape is locked.
- NODE/FIELD/EDGE synthetic failure fixtures for Beat 3.6. Will land alongside the notebook commit — they're tightly coupled to the cell that exercises them.
- End-to-end execution against real BigQuery. The notebook commit will execute every cell and inline the outputs.
- `docs/README.md` / `CHANGELOG.md` entries. Land with the notebook PR so the index points at a complete artifact.

## Related

- [#107 storyboard](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — per-cell plan the notebook implements.
- [#107 MAKO requirement](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107#issuecomment-4435535476) — "test with the real ontology" comment that pinned the MAKO TTL as the input.
- [Rollout guide](../../docs/extractor_compilation_rollout_guide.md) — Phase C pipeline reference for Beat 3 cells.
- [Ontology runtime reader](../../docs/ontology_runtime_reader.md) — #58 reader API used in Beat 4.
