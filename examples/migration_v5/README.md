# Migration v5 Demo — Ontology-Driven Artifact Pipeline

**Status:** The four-guarantee notebook (`examples/migration_v5_demo_notebook.ipynb`) is live end-to-end against `test-project-0728-467323` using the MAKO ontology as the canonical reference example. PR [#155](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/155) shipped the fixture foundation; PR [#157](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/157) added `reference_extractor.py`; PR [#160](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/160) wired Beat 3 + Beat 4.4 live.

The demo's event source of truth is **a runnable agent talking to the BQ AA plugin**, not a hand-coded event generator. The artifact pipeline that turns a TTL into binding + DDL + property-graph SQL is **ontology-agnostic** — see `ontology_artifacts.py` — and the MAKO config (in `mako_artifacts.py`) is one concrete configuration of it.

## Pluggable contract

The pipeline takes a single :class:`ontology_artifacts.OntologyConfig` plus a target `(project, dataset)` and produces four files:

| Generated file | What it is |
|---|---|
| `ontology.yaml` | `import-owl` output with `FILL_IN` PKs resolved, cross-namespace dangling relationships dropped, inheritance stripped. |
| `binding.yaml` | Maps the configured entity allowlist onto BigQuery tables for the target `(project, dataset)`. Heterogeneous edges use the legacy `from_columns: [<col>]` shape; self-edges (`evolvedFrom`, `supersededBy`) use the explicit dict-shape `from_columns: [{src_<col>: <pk_prop>}]` so the SDK's canonical FK→PK mapping disambiguates the src/dst endpoints. Out-of-scope relationships filtered. |
| `table_ddl.sql` | `CREATE TABLE` SQL for every node + edge table, with SDK metadata columns (`session_id STRING, extracted_at TIMESTAMP`) on every bound table. |
| `property_graph.sql` | `CREATE OR REPLACE PROPERTY GRAPH` over those tables. Node `KEY` + edge `SOURCE KEY ... REFERENCES` use the per-entity PK columns. |

The pipeline applies three OWL-TTL normalizations generic enough to apply to any reasonable ontology:

1. **`FILL_IN` PKs → `id: string`.** The OWL importer marks entities without `owl:hasKey` declarations as `FILL_IN`; the resolver synthesizes a stable `id` property. Entities that already declare `owl:hasKey` are left untouched.
2. **Cross-namespace dangling relationships dropped.** TTLs that extend upstream ontologies (PROV-O, PKO, DCAT, …) often reference upstream entities the importer didn't pull; the pipeline drops these and records them under a `{annotation_prefix}:dropped_cross_namespace_relationships` audit annotation.
3. **Inheritance stripped.** `gm compile` v0 doesn't support `extends:` clauses; the pipeline drops them and records the loss under `{annotation_prefix}:stripped_inheritance`.

## Example ontologies in this directory

This demo ships **two** ontology configs. The MAKO config is the load-bearing reference example (real production ontology, full agent, runnable demo); the Simple Request Flow config is a smoke fixture that proves the pipeline is genuinely ontology-agnostic.

| Config | TTL | Where | Surface |
|---|---|---|---|
| `MAKO_CONFIG` | `mako_core.ttl` (Yahoo Monetization decision ontology, 18 namespace entities) | `mako_artifacts.py` | Full demo: notebook, `mako_demo_agent.py` (5 decision-flow tools), `reference_extractor.py`, periodic materialization deploy. |
| `SIMPLE_REQUEST_FLOW_CONFIG` | `example_ontologies/simple_request_flow.ttl` (3-entity Request → Action → Outcome flow) | `example_ontologies/simple_request_flow_config.py` | Smoke fixture only. Exercised by `tests/test_migration_v5_ontology_artifacts.py`. No runnable agent ships with it. |

A new ontology plugs in the same way: write a TTL, define an `OntologyConfig` naming it, call `regenerate_snapshots(your_config, project=..., dataset=...)`.

## Authorship boundary

| File | Authored? | What it does |
|------|-----------|--------------|
| `ontology_artifacts.py` | **Authored.** | Generic ontology-agnostic pipeline: `OntologyConfig` dataclass + `load_ontology`, `make_binding`, `make_table_ddl`, `make_property_graph_sql`, `regenerate_snapshots`. **Does not generate events.** |
| `mako_core.ttl` | **Authored.** | The real MAKO ontology, pulled from the [reference gist](https://gist.github.com/haiyuan-eng-google/a69ff6282ebcc877f77f9aa4e3db1afd). Domain-agnostic decision semantics for Yahoo Monetization Platform. |
| `mako_artifacts.py` | **Authored.** | MAKO-specific config: `MAKO_CONFIG` + thin back-compat wrappers around the generic pipeline. The notebook imports from this module. |
| `mako_demo_agent.py` | **Authored.** | Runnable ADK agent + `BigQueryAgentAnalyticsPlugin` wiring. Defines five MAKO decision-flow tools (`capture_context`, `propose_decision_point`, `evaluate_candidate`, `commit_outcome`, `complete_execution`) and a system prompt that walks the agent through them. Real plugin traces land in `agent_events` when the agent runs. MAKO-specific by design — the agent's tools mirror MAKO's decision flow. |
| `run_agent.py` | **Authored.** | Driver. `python run_agent.py --sessions 50 --project X --dataset Y` runs the MAKO agent for N sessions and lets the plugin populate `agent_events`. |
| `export_events_jsonl.py` | **Authored.** | Optional. Exports a pinned subset of `agent_events` to a local JSONL file for the notebook's deterministic offline revalidation tests. Not an event generator — it reads from BigQuery. |
| `example_ontologies/simple_request_flow.ttl` | **Authored.** | Pluggability smoke fixture (3 entities, 2 relationships, no cross-namespace imports). |
| `example_ontologies/simple_request_flow_config.py` | **Authored.** | `SIMPLE_REQUEST_FLOW_CONFIG` — second `OntologyConfig` that plugs into the same generic pipeline. |
| `ontology.yaml` / `binding.yaml` / `table_ddl.sql` / `property_graph.sql` | **Generated** for MAKO by `mako_artifacts.regenerate_snapshots()`. Checked in so reviewers can read them as-is. | TTL-derived artifacts for `(test-project-0728-467323, migration_v5_demo)`. The notebook regenerates against a fresh `migration_v5_demo_<8-hex>` dataset at runtime. |
| `events.jsonl` | **Captured.** | Optional offline snapshot exported via `export_events_jsonl.py`. Not checked in; populated on demand. |

The Simple Request Flow config's snapshots are **not checked in** — they're regenerated to a tmpdir by the pluggability test. The TTL + config are the only files under `example_ontologies/`.

## Demo flow (what the notebook does)

```
mako_core.ttl                                                    ┐
       │                                                         │
       ▼                                                         │ Beat 0 — setup
ontology_artifacts.regenerate_snapshots(MAKO_CONFIG, ...)        │
   (called via mako_artifacts.regenerate_snapshots)              │
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
binding-validate                              node + edge        │ Beats 1–4
ontology-build (extracts the graph)           tables             │ consume
OntologyRuntime + LabelSynonymResolver                           ┘
```

Beat 3's compile / runtime / savings cells run live, using `reference_extractor.extract_mako_decision_event` as the runtime fallback under a focused compiled bundle that handles the `complete_execution` event type.

## Reference example: MAKO design decisions

The decisions below are documented in MAKO terms because MAKO is the load-bearing reference example, but the underlying pipeline behavior is general. Section numbers map to the transformations the generic pipeline applies (see "Pluggable contract" above).

### 1. MAKO `DecisionExecution` is the central hub

The MAKO demo entity allowlist (`DEMO_ENTITIES` in `mako_artifacts.py`) is **eleven entities** split across the two-beat arc:

* **Beats 1–4 hub** (6 entities): `AgentSession`, `DecisionExecution`, `DecisionPoint`, `Candidate`, `SelectionOutcome`, `ContextSnapshot`. `DecisionExecution` is non-obvious but load-bearing — per MAKO's TTL, it's the entity that's `partOfSession` an `AgentSession`, `atContextSnapshot` a `ContextSnapshot`, `executedAtDecisionPoint` a `DecisionPoint`, `hasSelectionOutcome` a `SelectionOutcome`. The decision-flow story doesn't hold together without it.
* **Beat 5 feedback / reward loop** (5 entities): `BusinessConstraint`, `ConstraintApplication`, `RejectionReason`, `OutcomeSignal`, `RewardComputation`. Each captures one slice of "what happened *after* the decision" — constraint evaluations, candidate rejections, observed real-world outcomes, and the RL reward computed from those outcomes.

The edge set is **TTL-driven with one filter**: `make_binding` walks `ontology.relationships` and picks every relationship whose endpoints both fall within the entity allowlist. The current MAKO binding has **fourteen** relationships covering eleven entities:

* **Beats 1–4 hub** (7 edges + 2 self-edges, 6 entities): `atContextSnapshot`, `evaluatesCandidate`, `executedAtDecisionPoint`, `hasSelectionOutcome`, `partOfSession`, `rejectedCandidate`, `selectedCandidate`, plus the two `DecisionExecution → DecisionExecution` self-edges (`evolvedFrom`, `supersededBy`).
* **Beat 5 feedback / reward loop** (5 edges, 5 entities): `hasRejectionReason` (Candidate → RejectionReason), `appliedConstraint` (ConstraintApplication → BusinessConstraint), `filteredByConstraint` (Candidate → ConstraintApplication), `producedOutcome` (DecisionExecution → OutcomeSignal), `derivedReward` (RewardComputation → OutcomeSignal).

The self-edges use the explicit dict-shape `from_columns` introduced in #179 and consumed by C2; see design decision 9 below for the column convention.

### 2. FILL_IN resolution: synthesize `id: string`

The MAKO TTL doesn't declare `owl:hasKey` on most entities, so the OWL importer marks 17 concrete entities' primary keys as `FILL_IN`. The pipeline resolves each one to a synthesized `id: string` property + primary key. Matches MAKO's "every artifact has a stable identifier" design contract. If a future MAKO revision adds `owl:hasKey` declarations, the resolver leaves those alone — only `FILL_IN` placeholders get rewritten. This rule is **general** — any TTL with `FILL_IN` PKs gets the same treatment.

### 3. Cross-namespace relationships dropped (with audit trail)

MAKO extends PROV-O + PKO + DCAT. Four relationships in the TTL point to entities outside MAKO's own namespace (`delegatedBy → prov:Agent`, etc.). The artifact pipeline drops these so the ontology loads cleanly and records the dropped names under the ontology's top-level `mako_demo:dropped_cross_namespace_relationships` annotation. The loss is auditable from a loaded model. **General behavior**: any TTL extending upstream ontologies gets the same treatment; the annotation key uses the config's `annotation_prefix`.

### 4. Agent uses realistic tool names; mapping is explicit

A real ADK agent exposes business/task-oriented tools, not tools whose argument names mirror TTL property names. The MAKO demo follows that convention — tool names are imperative business verbs (`capture_context`, `propose_decision_point`, `evaluate_candidate`, `commit_outcome`, `complete_execution`) and tool argument / return-value keys use ordinary snake_case (`audience_size`, `budget_remaining_usd`, `business_entity_id`).

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

`mako_artifacts.regenerate_snapshots(project=..., dataset=...)` and `run_agent.py --project X --dataset Y` both take the target as input. The checked-in snapshots use `test-project-0728-467323` / `migration_v5_demo` as defaults so reviewers can `cat` them; the notebook regenerates everything against a fresh `migration_v5_demo_<8-hex>` dataset at runtime. The generic pipeline takes `(project, dataset)` the same way — they're never config-time constants.

### 6. `events.jsonl` is captured, not synthesized

If kept, `events.jsonl` is the output of `export_events_jsonl.py` reading from `agent_events`. The notebook may use it as an offline corpus for Beat 3's revalidation tests (deterministic input the threshold gates can lock against), but the demo's primary event surface is the live `agent_events` table.

The exporter's `SELECT` projects the subset of the BQ AA plugin's schema the notebook needs (`google/adk/plugins/bigquery_agent_analytics_plugin.py::_get_events_schema`): `timestamp`, `event_type`, `agent`, `session_id`, `invocation_id`, `user_id`, `trace_id`, `span_id`, `parent_span_id`, `status`, `error_message`, `is_truncated`, plus `content` / `attributes` / `latency_ms` (all JSON). The plugin's full schema also includes `content_parts` (REPEATED RECORD for multimodal parts); the exporter omits it because the MAKO decision flow is text-only.

### 7. Table DDL carries SDK metadata columns

`make_table_ddl()` appends `session_id STRING, extracted_at TIMESTAMP` to every node and edge table because the materializer writes both on every `materialize()` call and `binding_validation.py` requires them on every bound table. Without them, the notebook's binding-validate step would fail before ontology-build. When a domain property already maps to one of those columns (MAKO's `AgentSession.sessionId → session_id`), the metadata copy is skipped to avoid a duplicate-column error. **General behavior**: applies to every config.

### 8. Per-entity PK columns

Every entity's PK column is `{entity_short}_id` (`decision_execution_id`, `candidate_id`, `agent_session_id`, …), not a bare `id`. Even after C2 wired the canonical FK→PK mapping (so the materializer can resolve self-edges where `src_<col>_id` deliberately differs from the PK column), heterogeneous edges keep the legacy `list[str]` shape — and that shape pairs binding columns positionally with the endpoint's PK columns. With bare `id` as the PK, every cross-entity edge would land `(id STRING, id STRING)` (duplicate-column error) and the heterogeneous-edge codepath would have no way to disambiguate without forcing every binding into dict-shape. Per-entity names match the convention the original V5 spec used (`YMGO_Context_Graph_V3`: `decision_id`, `adUnitId`) and the SDK's integration-test fixture. **General behavior**: applies to every config; the Simple Request Flow binding produces `request_id` / `action_id` / `outcome_id` for the same reason.

Notebook GQL queries reference `de.decision_execution_id` (not `de.id`); the Beat 4 cells carry an entity → PK column map for the same reason.

### 9. Self-edges via explicit FK→PK mapping

MAKO declares `evolvedFrom` and `supersededBy` as `DecisionExecution → DecisionExecution` self-edges. The natural composite `(decision_execution_id, decision_execution_id)` is a duplicate-column error, and bare `src_/dst_` prefixing on its own misses the materializer's property-column lookup (the FK column no longer matches any property name on the endpoint entity).

C2 (`feat/relationship-canonical-column-mapping`) wires the canonical FK→PK mapping from #179 through the materializer, the validator, and the PG DDL compiler — so the binding can declare self-edges using the dict-shape `from_columns`:

```yaml
- name: evolvedFrom
  source: <project>.<dataset>.evolved_from
  from_columns:
  - src_decision_execution_id: id     # edge_col -> endpoint PK property
  to_columns:
  - dst_decision_execution_id: id
```

`make_binding()` emits this shape for any `rel.from_ == rel.to`. The materializer's `_route_edge` uses the canonical `from_column_mapping` / `to_column_mapping` to translate the parsed node-id segment (keyed by endpoint *column*) into the right edge-table FK column. The PG DDL compiler resolves `SOURCE KEY (src_decision_execution_id) REFERENCES decision_execution (decision_execution_id)` correctly. **General behavior**: applies to any config — self-edges are no longer dropped.

### 10. Inheritance stripped from entities

The MAKO TTL declares `mako:Candidate rdfs:subClassOf mako:RoleTrait`; the OWL importer surfaces this as `Candidate.extends: RoleTrait`. `gm compile` v0 doesn't support inheritance and rejects the binding (`compile-validation — Entity 'Candidate' uses 'extends'`), which blocks Section 4's `--emit-concept-index` step. The pipeline drops `extends` from the post-import YAML and audit-trails the discard under the config's `annotation_prefix` (`mako_demo:stripped_inheritance` for MAKO). `RoleTrait` is a marker class in MAKO (REQ-ONT-022) with no properties beyond the `id` PK every other entity already has, so the discard has no semantic effect on the demo's 11-entity scope. **General behavior**: applies to any TTL with `extends:` clauses.

## Validation commands run (all pass)

```bash
# MAKO artifact pipeline runs end-to-end and regenerates snapshots.
PYTHONPATH=src python examples/migration_v5/mako_artifacts.py
# → {"binding_entities": 11, "binding_relationships": 14,
#    "ontology_entities": 18}

# Generic pipeline works against the Simple Request Flow smoke fixture.
PYTHONPATH=src pytest tests/test_migration_v5_ontology_artifacts.py
# → 10 passed (MAKO snapshot regression + pluggability assertions
#   + owl:hasKey regression coverage)

# Generated MAKO ontology validates clean.
python -m bigquery_ontology.cli validate examples/migration_v5/ontology.yaml

# Generated MAKO binding validates against the generated ontology.
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
# → LlmAgent 9 BigQueryAgentAnalyticsPlugin

# Driver --help works without live BQ / Vertex.
PYTHONPATH=src python examples/migration_v5/run_agent.py --help

# Exporter --help + identifier validation.
PYTHONPATH=src python examples/migration_v5/export_events_jsonl.py --help
```

A live end-to-end notebook run (`run_agent.py --sessions 3` + Beat 1–4 cells against a fresh scratch dataset on `test-project-0728-467323`) is captured with cell outputs inline in `examples/migration_v5_demo_notebook.ipynb`. Per-beat evidence (exact numbers depend on which `--sessions N` run produced the committed snapshot):

- **Beat 1**: GQL `DecisionExecution` count `before=0, after=N>0`, `rows_materialized total>0`, `property_graph_status='skipped:user_requested'`, zero SDK-issued `CREATE OR REPLACE PROPERTY GRAPH` jobs.
- **Beat 2**: `binding-validate` exits 1 with a `missing_column` failure after column rename; restore + re-validate exits 0; cell 2.5 reuses cell 1.4's `property_graph_status='skipped:user_requested'` + non-zero `rows_materialized` to close the combined "validate + build" flow without re-materializing inside BigQuery's streaming-buffer window.
- **Beat 3.6**: synthetic `ExtractedGraph` triggers all three `FallbackScope` failures (`NODE + FIELD + EDGE`).
- **Beat 4**: concept index emitted + applied; `LabelSynonymResolver.resolve("DecisionExecution")` returns 1 candidate with a 12-hex `compile_id`; `GRAPH_TABLE` count over the user-authored property graph is non-zero. Hub-shape `(DecisionExecution)-[partOfSession]->(AgentSession)` returns at least one row per current session — the compiled extractor wired in Beat 3.5 synthesizes the envelope-side `AgentSession` + `partOfSession`.
- **Beat 5**: feedback / reward loop closes the demo arc. The agent emits four additional tool calls per decision — `record_rejection` (one per losing candidate), optional `apply_constraint` (when a candidate is filtered by policy), `record_outcome_signal` (one to three per execution; observed real-world result), `compute_reward` (one per execution; aggregates the signals into a scalar RL reward). The reference extractor at `examples/migration_v5/reference_extractor.py` covers all four, emitting `RejectionReason`, `BusinessConstraint` + `ConstraintApplication`, `OutcomeSignal`, and `RewardComputation` nodes plus the edges (`hasRejectionReason`, `appliedConstraint`, `filteredByConstraint`, `producedOutcome`, `derivedReward`) that wire them back into the Beat 1–4 hub. **Live notebook smoke passed**: a single coherent end-to-end run against a fresh `migration_v5_demo_0a300070` scratch dataset on `test-project-0728-467323` with three MAKO agent sessions, captured in `examples/migration_v5_demo_notebook.ipynb` — all 29 code cells executed with monotonic execution counts 1–29 and zero error outputs. Beat 1's build materializes `rows_materialized total=89` across 18 tables (Beats 1–4 hub + Beat 5 entities + edges); Beat 5's cells extract 125 nodes / 46 edges and confirm 6 `OutcomeSignal` + 3 `RewardComputation` + 8 `RejectionReason` rows landed in BigQuery alongside their edges. Both payoff GQL traversals project unique edge tuples via `SELECT DISTINCT` and assert their row count matches the underlying `DISTINCT` edge count: `(DecisionExecution)-[producedOutcome]->(OutcomeSignal)<-[derivedReward]-(RewardComputation)` returns 6 unique rows with real `reward_value` floats (0.80 / 1.00) from the agent's `compute_reward` payload; `(Candidate)-[hasRejectionReason]->(RejectionReason)` returns 8 unique rows attributing every losing candidate to a recorded reason. The `DISTINCT` projection makes the cells robust to BigQuery's streaming-buffer DELETE rejection (which can pin Beat 1.4 + Beat 3.5's row sets within the same ~30 min window) and the assertions catch any join-cardinality regression. The binding scope grows from 6 to **11** entities; 9 to **14** relationships (the prior 9 already included the two `DecisionExecution` self-edges from C2).

## Run this every N hours in production

The notebook walks through the four guarantees once, ad hoc. Real deployments want the graph kept fresh on a cron — events arrive continuously, the materialized entity/relationship tables should follow within a chosen latency budget.

[`periodic_materialization/`](./periodic_materialization/) is the production path: a packaged Cloud Run Job + Cloud Scheduler trigger that runs `bqaa-materialize-window` every N hours against your project, using the MAKO demo's bound artifacts. The deploy script bundles the checked-in `binding.yaml` / `ontology.yaml` / `table_ddl.sql` from this directory — running it against a different `OntologyConfig` means regenerating those snapshots for your config first and pointing the deploy at the new files. The deploy script doesn't yet wire that as a CLI flag; that's a natural follow-up but out of scope for this PR.

The flow customers actually follow:

1. **Get events** — point at your existing `agent_events` table (the BQ AA plugin already writes here; if you don't have one yet, seed via `python examples/migration_v5/run_agent.py --sessions 3` against a scratch dataset to populate it with MAKO traces).
2. **Local dry-run** — `python periodic_materialization/run_job.py` with env vars. Same code path as the deployed job, no Cloud Run required. Verifies your IAM / dataset setup before paying for a deploy.
3. **Deploy** — one command:

   ```bash
   ./examples/migration_v5/periodic_materialization/deploy_cloud_run_job.sh \
     --project your-project --region us-central1 \
     --events-dataset your_events_dataset \
     --graph-dataset your_graph_dataset \
     --schedule "0 */6 * * *" --smoke
   ```

   Deploys the Cloud Run Job, creates the runtime service account with narrow IAM (events-READ, graph-WRITE), wires the Cloud Scheduler trigger, and runs `--smoke` to verify in one shot.

4. **Verify** — Cloud Logging shows the JSON report on every run (`jsonPayload.ok`, `sessions_materialized`, `rows_materialized`, per-table `table_statuses`). The state table at `<graph_dataset>._bqaa_materialization_state` is a queryable audit log.
5. **Alert** — Cloud Monitoring on `severity=ERROR` OR `jsonPayload.ok=false`. The `jsonPayload.failures[].error_code` distinguishes `empty_extraction` (AI/IAM) from `materialization_failed` (schema/write-perm).

See **[`periodic_materialization/README.md`](./periodic_materialization/README.md)** for the full customer playbook: required APIs, IAM matrix, recommended schedules per latency target, Cloud Monitoring alert queries, state-table SQL, troubleshooting, and live-deployment evidence captured against the canonical test project.

## Related

- [#107 storyboard](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — per-cell plan the notebook implements.
- [#107 MAKO requirement](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107#issuecomment-4435535476) — "test with the real ontology" comment.
- [Round-2 reshape clarification](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/155#issuecomment-4437670647) — pinned the "TTL + runnable agent" contract.
- [`examples/decision_lineage_demo/`](../decision_lineage_demo/) — reference pattern for ADK agent + BQ AA plugin wiring.
- [Rollout guide](../../docs/extractor_compilation_rollout_guide.md) — Phase C pipeline reference for Beat 3 cells.
- [Ontology runtime reader](../../docs/ontology_runtime_reader.md) — #58 reader API used in Beat 4.
