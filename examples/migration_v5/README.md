# Migration v5 Demo — Fixture Foundation

**Status:** The four-guarantee MAKO notebook (`examples/migration_v5_demo_notebook.ipynb`) is live end-to-end against `test-project-0728-467323`. PR [#155](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/155) shipped the fixture foundation; PR [#157](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/157) added `reference_extractor.py`; PR [#160](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/160) wired Beat 3 (compile + cache + runtime + savings) and Beat 4.4 (hub-shape non-zero) live.

The demo's event source of truth is **a runnable agent talking to the BQ AA plugin**, not a hand-coded event generator. This directory's authored inputs are split accordingly.

## Authorship boundary

| File | Authored? | What it does |
|------|-----------|--------------|
| `mako_core.ttl` | **Authored.** | The real MAKO ontology, pulled from the [reference gist](https://gist.github.com/haiyuan-eng-google/a69ff6282ebcc877f77f9aa4e3db1afd). Domain-agnostic decision semantics for Yahoo Monetization Platform. |
| `mako_artifacts.py` | **Authored.** | Pure-Python pipeline: imports the TTL → resolves `FILL_IN` primary keys → drops dangling cross-namespace relationships → strips inheritance → generates ontology / binding / table DDL / property-graph SQL for any `(project, dataset)`. **Does not generate events.** |
| `mako_demo_agent.py` | **Authored.** | Runnable ADK agent + `BigQueryAgentAnalyticsPlugin` wiring. Defines five MAKO decision-flow tools (`capture_context`, `propose_decision_point`, `evaluate_candidate`, `commit_outcome`, `complete_execution`) and a system prompt that walks the agent through them. Real plugin traces land in `agent_events` when the agent runs. |
| `run_agent.py` | **Authored.** | Driver. `python run_agent.py --sessions 50 --project X --dataset Y` runs the agent for N sessions and lets the plugin populate `agent_events`. |
| `export_events_jsonl.py` | **Authored.** | Optional. Exports a pinned subset of `agent_events` to a local JSONL file for the notebook's deterministic offline revalidation tests. Not an event generator — it reads from BigQuery. |
| `ontology.yaml` | **Generated** by `mako_artifacts.regenerate_snapshots()`. | TTL-import output with `FILL_IN`s resolved, dangling cross-namespace relationships dropped, inheritance stripped. |
| `binding.yaml` | **Generated.** | Derived from the ontology + `(project, dataset)`. 6 entities + 7 heterogeneous relationships. |
| `table_ddl.sql` | **Generated.** | Companion to the binding. Carries SDK metadata columns (`session_id STRING, extracted_at TIMESTAMP`) on every node and edge table. |
| `property_graph.sql` | **Generated.** | `CREATE OR REPLACE PROPERTY GRAPH` over the same tables. Node KEY + edge `REFERENCES` use the per-entity PK columns. |
| `events.jsonl` | **Captured.** | Optional offline snapshot exported via `export_events_jsonl.py`. Not checked in; populated on demand. |

Checked-in `binding.yaml` / `table_ddl.sql` / `property_graph.sql` target the default `(test-project-0728-467323, migration_v5_demo)` pair so reviewers can read them as-is. The notebook regenerates against a fresh `migration_v5_demo_<8-hex>` dataset at runtime.

## Demo flow (what the notebook does)

```
mako_core.ttl                                                    ┐
       │                                                         │
       ▼                                                         │ Beat 0 — setup
mako_artifacts.regenerate_snapshots(project, dataset)            │
       │                                                         │
       ├── ontology.yaml                                         │
       ├── binding.yaml                                          │
       ├── table_ddl.sql       ────► BigQuery (CREATE TABLE)     │
       └── property_graph.sql  ────► BigQuery (CREATE PROPERTY   │
                                              GRAPH)             ┘

run_agent.py --sessions N    ────► ADK runner + BQ AA plugin     ┐ Beat 0 — populate
                                          │                      │     agent_events
                                          ▼                      │
                                   agent_events table             ┘

ontology-build --skip-property-graph    ────► populates the      ┐
binding-validate                              MAKO node + edge   │ Beats 1–4
ontology-build (extracts the graph)           tables             │ consume
OntologyRuntime + LabelSynonymResolver                           ┘
```

Beat 3's compile / runtime / savings cells run live, using `reference_extractor.extract_mako_decision_event` as the runtime fallback under a focused compiled bundle that handles the `complete_execution` event type.

## Design decisions

### 1. MAKO `DecisionExecution` is the central hub

The demo entity allowlist (`DEMO_ENTITIES` in `mako_artifacts.py`) is six entities: `AgentSession`, `DecisionExecution`, `DecisionPoint`, `Candidate`, `SelectionOutcome`, `ContextSnapshot`. `DecisionExecution` is non-obvious but load-bearing — per MAKO's TTL, it's the entity that's `partOfSession` an `AgentSession`, `atContextSnapshot` a `ContextSnapshot`, `executedAtDecisionPoint` a `DecisionPoint`, `hasSelectionOutcome` a `SelectionOutcome`. The decision-flow story doesn't hold together without it.

The edge set is **TTL-driven with two filters**: `make_binding` walks `ontology.relationships` and picks every relationship whose endpoints both fall within `DEMO_ENTITIES` *and* which is not a self-edge. The current binding has **seven** real MAKO relationships (`atContextSnapshot`, `evaluatesCandidate`, `executedAtDecisionPoint`, `hasSelectionOutcome`, `partOfSession`, `rejectedCandidate`, `selectedCandidate`). MAKO's two self-edges (`evolvedFrom`, `supersededBy`, both DecisionExecution → DecisionExecution) are documented under design decision 9 below.

### 2. FILL_IN resolution: synthesize `id: string`

The MAKO TTL doesn't declare `owl:hasKey` on most entities, so the OWL importer marks 17 concrete entities' primary keys as `FILL_IN`. `mako_artifacts.py` resolves each one to a synthesized `id: string` property + primary key. Matches MAKO's "every artifact has a stable identifier" design contract. If a future MAKO revision adds `owl:hasKey` declarations, the resolver leaves those alone — only `FILL_IN` placeholders get rewritten.

### 3. Cross-namespace relationships dropped (with audit trail)

MAKO extends PROV-O + PKO + DCAT. Four relationships in the TTL point to entities outside MAKO's own namespace (`delegatedBy → prov:Agent`, etc.). The artifact pipeline drops these so the ontology loads cleanly and records the dropped names under the ontology's top-level `mako_demo:dropped_cross_namespace_relationships` annotation. The loss is auditable from a loaded model.

### 4. Agent uses realistic tool names; mapping is explicit

A real ADK agent exposes business/task-oriented tools, not tools whose argument names mirror TTL property names. The demo follows that convention — tool names are imperative business verbs (`capture_context`, `propose_decision_point`, `evaluate_candidate`, `commit_outcome`, `complete_execution`) and tool argument / return-value keys use ordinary snake_case (`audience_size`, `budget_remaining_usd`, `business_entity_id`).

The **explicit mapping** between what the agent emits and what extraction materializes into the MAKO graph:

| Tool field (trace) | Materialized → MAKO property | Materialization rule |
|---|---|---|
| `capture_context.audience_size` | `ContextSnapshot.snapshotPayload` (component) | Folded into the JSON `snapshotPayload` blob. |
| `capture_context.budget_remaining_usd` | `ContextSnapshot.snapshotPayload` (component) | Same. |
| `capture_context.context_id` | `ContextSnapshot.id` (primary key) | 1:1. |
| `propose_decision_point.decision_point_id` | `DecisionPoint.id` (primary key) | 1:1. |
| `propose_decision_point.reversibility` | `DecisionPoint.reversibility` | 1:1. |
| `propose_decision_point.decision_type` | — | **Trace-only.** MAKO does not declare `decisionType` on `DecisionPoint`; the field exists in the trace for analytics but isn't materialized. |
| `evaluate_candidate.candidate_id` | `Candidate.id` (primary key) | 1:1. |
| `evaluate_candidate.candidate_label` | — | **Trace-only.** `Candidate` has no MAKO-declared data properties; the label exists in the trace as reasoning context. |
| `evaluate_candidate.decision_point_id` | `evaluatesCandidate` edge (DecisionPoint → Candidate) | Edge endpoint. |
| `commit_outcome.outcome_id` | `SelectionOutcome.id` | 1:1. |
| `commit_outcome.selected_candidate_id` | `selectedCandidate` edge (SelectionOutcome → Candidate) | Edge endpoint. |
| `commit_outcome.rationale` | — | **Trace-only.** `SelectionOutcome` has no MAKO-declared rationale field. |
| `complete_execution.execution_id` | `DecisionExecution.id` | 1:1. |
| `complete_execution.business_entity_id` | `DecisionExecution.businessEntityId` | 1:1 (column `business_entity_id`). |
| `complete_execution.latency_ms` | `DecisionExecution.latencyMs` (INT64) | 1:1 (column `latency_ms`, **typed INT64** in `table_ddl.sql`). |
| `complete_execution.{decision_point,context,outcome}_id` | `executedAtDecisionPoint` / `atContextSnapshot` / `hasSelectionOutcome` edges | Each is an edge endpoint pointing at the parent `DecisionExecution`. |
| `session_id` (envelope) | `partOfSession` edge (DecisionExecution → AgentSession) | Plugin envelope. Both the compiled extractor (for `complete_execution` events; live in Beat 3.5) and the reference extractor (fallback path) synthesize the `AgentSession` node + `partOfSession` edge from this envelope field. Beat 4.4's hub-shape GQL traversal returns non-zero rows. |

**Rule of thumb:** only fields with a TTL-declared target property are materialized; everything else stays in the raw `agent_events` trace as reasoning context. The mapping above is the contract `reference_extractor.extract_mako_decision_event` (and the compiled bundle Beat 3.3 emits) implement.

The agent uses Vertex AI Gemini by default (`DEMO_AGENT_MODEL=gemini-2.5-flash`). Same wiring pattern as `examples/decision_lineage_demo/agent/agent.py`.

### 5. `(project, dataset)` is a parameter, not a baked-in value

`mako_artifacts.regenerate_snapshots(project=..., dataset=...)` and `run_agent.py --project X --dataset Y` both take the target as input. The checked-in snapshots use `test-project-0728-467323` / `migration_v5_demo` as defaults so reviewers can `cat` them; the notebook regenerates everything against a fresh `migration_v5_demo_<8-hex>` dataset at runtime.

### 6. `events.jsonl` is captured, not synthesized

If kept, `events.jsonl` is the output of `export_events_jsonl.py` reading from `agent_events`. The notebook may use it as an offline corpus for Beat 3's revalidation tests (deterministic input the threshold gates can lock against), but the demo's primary event surface is the live `agent_events` table.

The exporter's `SELECT` projects the subset of the BQ AA plugin's schema the notebook needs (`google/adk/plugins/bigquery_agent_analytics_plugin.py::_get_events_schema`): `timestamp`, `event_type`, `agent`, `session_id`, `invocation_id`, `user_id`, `trace_id`, `span_id`, `parent_span_id`, `status`, `error_message`, `is_truncated`, plus `content` / `attributes` / `latency_ms` (all JSON). The plugin's full schema also includes `content_parts` (REPEATED RECORD for multimodal parts); the exporter omits it because the MAKO decision flow is text-only. There is no `event_id`, `payload`, `agent_name`, or `partition_date` column on the plugin's table — the plugin partitions on `timestamp` (DAY).

### 7. Table DDL carries SDK metadata columns

`make_table_ddl()` appends `session_id STRING, extracted_at TIMESTAMP` to every node and edge table because the materializer writes both on every `materialize()` call and `binding_validation.py` requires them on every bound table. Without them, the notebook's binding-validate step would fail before ontology-build. When a domain property already maps to one of those columns (MAKO's `AgentSession.sessionId → session_id`), the metadata copy is skipped to avoid a duplicate-column error.

### 8. Per-entity PK columns

Every entity's PK column is `{entity_short}_id` (`decision_execution_id`, `candidate_id`, `agent_session_id`, …), not a bare `id`. The materializer's `_relationship_columns` (`ontology_materializer.py`) looks up edge FK columns in `src_prop_map[col].sdk_type` — that lookup requires the FK column to *exactly* name a column on the source entity. With bare `id`, every cross-entity edge would land `(id STRING, id STRING)` (duplicate-column error) and the FK→PK type lookup would still miss. Per-entity names match the convention the original V5 spec used (`YMGO_Context_Graph_V3`: `decision_id`, `adUnitId`) and the SDK's integration-test fixture (`tests/fixtures/test_binding.yaml`: `customer_id` → `cust_id`).

Notebook GQL queries reference `de.decision_execution_id` (not `de.id`); the Beat 4 cells carry an entity → PK column map for the same reason.

### 9. Self-edges dropped from the binding

MAKO declares `evolvedFrom` and `supersededBy` as `DecisionExecution → DecisionExecution` self-edges. The materializer's FK→PK lookup can't disambiguate the two endpoints from a single source entity, and the natural composite key `(decision_execution_id, decision_execution_id)` is a duplicate-column error. `src_/dst_` prefixing avoids the duplicate but still misses the materializer's property-column lookup.

The ontology still declares both edges (the TTL is unchanged); the binding scope drops them. A future binding revision could re-add self-edges if the SDK accepts FK-to-PK column mapping or the materializer learns to handle them via per-endpoint prefixes; for now the seven heterogeneous edges carry the decision-flow narrative end-to-end.

### 10. Inheritance stripped from MAKO entities

The MAKO TTL declares `mako:Candidate rdfs:subClassOf mako:RoleTrait`; the OWL importer surfaces this as `Candidate.extends: RoleTrait`. `gm compile` v0 doesn't support inheritance and rejects the binding (`compile-validation — Entity 'Candidate' uses 'extends'`), which blocks Section 4's `--emit-concept-index` step. `_strip_inheritance` drops `extends` from the post-import YAML and audit-trails the discard under `mako_demo:stripped_inheritance`. `RoleTrait` is a marker class in MAKO (REQ-ONT-022) with no properties beyond the `id` PK every other entity already has, so the discard has no semantic effect on the demo's six-entity scope.

## Validation commands run (all pass)

```bash
# Artifact pipeline runs end-to-end and regenerates snapshots.
PYTHONPATH=src python examples/migration_v5/mako_artifacts.py
# → {"ontology_entities": 18, "binding_entities": 6,
#    "binding_relationships": 7}

# Generated ontology validates clean.
python -m bigquery_ontology.cli validate examples/migration_v5/ontology.yaml

# Generated binding validates against the generated ontology.
python -m bigquery_ontology.cli validate examples/migration_v5/binding.yaml \
    --ontology examples/migration_v5/ontology.yaml

# Property graph + concept index DDL compiles clean.
python -m bigquery_ontology.cli compile \
    --emit-concept-index \
    --concept-index-table 'test-project-0728-467323.migration_v5_demo.mako_concept_index' \
    --ontology examples/migration_v5/ontology.yaml \
    examples/migration_v5/binding.yaml

# Demo agent + plugin import cleanly.
PYTHONPATH=src:examples/migration_v5 python -c "
import mako_demo_agent
print(type(mako_demo_agent.root_agent).__name__,
      len(mako_demo_agent.root_agent.tools),
      type(mako_demo_agent.bq_logging_plugin).__name__)"
# → LlmAgent 5 BigQueryAgentAnalyticsPlugin

# Driver --help works without live BQ / Vertex.
PYTHONPATH=src python examples/migration_v5/run_agent.py --help

# Exporter --help + identifier validation.
PYTHONPATH=src python examples/migration_v5/export_events_jsonl.py --help
```

A live end-to-end notebook run (`run_agent.py --sessions 3` + Beat 1–4 cells against a fresh scratch dataset on `test-project-0728-467323`) is captured with cell outputs inline in `examples/migration_v5_demo_notebook.ipynb`. Per-beat evidence (exact numbers depend on which `--sessions N` run produced the committed snapshot):

- **Beat 1**: GQL `DecisionExecution` count `before=0, after=N>0`, `rows_materialized total>0`, `property_graph_status='skipped:user_requested'`, zero SDK-issued `CREATE OR REPLACE PROPERTY GRAPH` jobs.
- **Beat 2**: `binding-validate` exits 1 with a `missing_column` failure after column rename; restore + re-validate exits 0; combined `ontology-build --skip-property-graph --validate-binding` matches Beat 1's status + non-zero `rows_materialized`.
- **Beat 3.6**: synthetic `ExtractedGraph` triggers all three `FallbackScope` failures (`NODE + FIELD + EDGE`).
- **Beat 4**: concept index emitted + applied; `LabelSynonymResolver.resolve("DecisionExecution")` returns 1 candidate with a 12-hex `compile_id`; `GRAPH_TABLE` count over the user-authored property graph is non-zero. Hub-shape `(DecisionExecution)-[partOfSession]->(AgentSession)` returns at least one row per current session — the compiled extractor wired in Beat 3.5 synthesizes the envelope-side `AgentSession` + `partOfSession`.

## Run periodic materialization (Cloud Run Job + Cloud Scheduler)

The notebook materializes a graph once, ad hoc. Real deployments want the graph kept fresh on a cron. `examples/migration_v5/periodic_materialization/` packages `bqaa-materialize-window` as a Cloud Run Job + Cloud Scheduler trigger, using the v5 demo binding (re-targeted to the customer's project at runtime).

**Local dry-run** — exercise the path against your own BigQuery without deploying:

```bash
pip install -r examples/migration_v5/periodic_materialization/requirements.txt

BQAA_PROJECT_ID=your-project \
BQAA_EVENTS_DATASET_ID=your_events_dataset \
BQAA_GRAPH_DATASET_ID=your_graph_dataset \
BQAA_LOOKBACK_HOURS=6 \
python examples/migration_v5/periodic_materialization/run_job.py
```

**Deploy** — one command, with `--smoke` to verify the deploy by running the job once and tailing logs:

```bash
./examples/migration_v5/periodic_materialization/deploy_cloud_run_job.sh \
  --project your-project \
  --region us-central1 \
  --events-dataset your_events_dataset \
  --graph-dataset your_graph_dataset \
  --schedule "0 */6 * * *" \
  --smoke
```

The deploy script bundles `run_job.py` + the demo artifacts (`ontology.yaml`, `binding.yaml`, `table_ddl.sql`) + `requirements.txt` into a staging dir, deploys via `gcloud run jobs deploy --source`, creates a service account, grants `roles/run.invoker`, and wires the Cloud Scheduler HTTP trigger.

The job's JSON report (per run) lands in Cloud Logging as a structured entry. Filter on `resource.labels.job_name` for the materialization audit log. The state table at `<graph_dataset>._bqaa_materialization_state` is a queryable history.

See [`periodic_materialization/README.md`](./periodic_materialization/README.md) for the full operational contract, env-var reference, and troubleshooting notes.

## What's NOT in this commit

- `docs/README.md` / `CHANGELOG.md` entries — staged for a follow-up PR alongside the user-facing release notes.

## Related

- [#107 storyboard](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — per-cell plan the notebook implements.
- [#107 MAKO requirement](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107#issuecomment-4435535476) — "test with the real ontology" comment.
- [Round-2 reshape clarification](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/155#issuecomment-4437670647) — pinned the "TTL + runnable agent" contract.
- [`examples/decision_lineage_demo/`](../decision_lineage_demo/) — reference pattern for ADK agent + BQ AA plugin wiring.
- [Rollout guide](../../docs/extractor_compilation_rollout_guide.md) — Phase C pipeline reference for Beat 3 cells.
- [Ontology runtime reader](../../docs/ontology_runtime_reader.md) — #58 reader API used in Beat 4.
