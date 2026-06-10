# Context Graph — Agent Decision Lineage on BigQuery Property Graphs

Turn raw agent event logs into a **Context Graph**: a queryable BigQuery
property graph of your agent's decisions. Every request, every candidate the
agent weighed, every committed outcome — connected, queryable in GQL, and kept
fresh on a schedule. No external graph database, no ETL pipeline.

This directory ships a complete worked example built on the MAKO decision
ontology (an 18-entity production decision model), including a runnable ADK
agent, the SQL artifacts that define the graph, and a packaged
Cloud Run + Cloud Scheduler deployment.

## The graph is defined by two SQL artifacts

Everything the Context Graph needs is declared in two plain SQL files. They
are the contract; the rest of this directory exists to generate, populate,
and operate them.

| Artifact | What it declares |
|---|---|
| [`table_ddl.sql`](./table_ddl.sql) | `CREATE TABLE` for every node and edge table — one table per entity (`decision_execution`, `candidate`, `selection_outcome`, …) and per relationship (`selected_candidate`, `evaluates_candidate`, …). Every table carries the SDK metadata columns `session_id STRING, extracted_at TIMESTAMP`. |
| [`property_graph.sql`](./property_graph.sql) | `CREATE OR REPLACE PROPERTY GRAPH` over those tables — node labels with `KEY` columns, edge labels with `SOURCE KEY ... REFERENCES` / `DESTINATION KEY ... REFERENCES`. This is the query surface GQL traversals run against. |

A representative slice of `property_graph.sql`:

```sql
CREATE OR REPLACE PROPERTY GRAPH `<project>.<dataset>.mako_demo_graph`
  NODE TABLES (
    `<project>.<dataset>.decision_execution` AS decision_execution
      KEY (decision_execution_id)
      LABEL DecisionExecution
        PROPERTIES (decision_execution_id, business_entity_id, latency_ms, span_id, trace_id),
    `<project>.<dataset>.candidate` AS candidate
      KEY (candidate_id)
      LABEL Candidate PROPERTIES (candidate_id)
    -- ... 9 more node tables
  )
  EDGE TABLES (
    `<project>.<dataset>.evaluates_candidate` AS evaluates_candidate
      KEY (decision_point_id, candidate_id)
      SOURCE KEY (decision_point_id) REFERENCES decision_point (decision_point_id)
      DESTINATION KEY (candidate_id) REFERENCES candidate (candidate_id)
      LABEL evaluatesCandidate
    -- ... 13 more edge tables
  );
```

### Apply the schema

Apply the table DDL first, then the property-graph DDL (the property graph
references the tables, so they must exist first). The checked-in files are
generated for a reference project; point them at your own project and dataset
before applying:

```bash
bq query --use_legacy_sql=false < table_ddl.sql
bq query --use_legacy_sql=false < property_graph.sql
```

Both files are idempotent — re-running them is safe. This is the only time
the SQL files are used: from here on the **deployed graph in BigQuery is the
single source of truth**.

### Materialize the graph from agent events

`bqaa context-graph` reads raw `agent_events` rows (written by the BigQuery
Agent Analytics Plugin), derives the entities and relationships to extract
**directly from your deployed graph** — it reads the graph's definition back
from `INFORMATION_SCHEMA.PROPERTY_GRAPHS` — and populates the node and edge
tables:

```bash
bqaa context-graph \
    --project-id your-project \
    --dataset-id your_dataset \
    --graph mako_demo_graph \
    --lookback-hours 24 \
    --format json
```

That single `--graph` flag is the whole configuration — no SQL file handed to
the materializer, no separate ontology or binding file on this path.

### Query it

With the graph populated, decision lineage is one GQL traversal:

```sql
GRAPH `<project>.<dataset>.mako_demo_graph`
MATCH (de:DecisionExecution)-[:hasSelectionOutcome]->(so:SelectionOutcome)
        -[:selectedCandidate]->(c:Candidate)
RETURN de.decision_execution_id, so.selection_outcome_id, c.candidate_id
```

## Run it every N hours in production

The graph should follow your events within a chosen latency budget.
[`periodic_materialization/`](./periodic_materialization/) is the production
path: a packaged Cloud Run Job + Cloud Scheduler trigger that runs
`bqaa context-graph` on your schedule. (The standalone
`bqaa-materialize-window` command remains a deprecated alias for the same
handler if existing scripts still call it.)

The flow:

1. **Get events** — point at your existing `agent_events` table (the
   BigQuery Agent Analytics Plugin already writes here; if you don't have one
   yet, seed it with the sample agent below).
2. **Local dry-run** — `python periodic_materialization/run_job.py` with env
   vars. Same code path as the deployed job, no Cloud Run required. Verifies
   your IAM / dataset setup before paying for a deploy.
3. **Deploy** — one command:

   ```bash
   ./examples/context_graph/periodic_materialization/deploy_cloud_run_job.sh \
     --project your-project --region us-central1 \
     --events-dataset your_events_dataset \
     --graph-dataset your_graph_dataset \
     --schedule "0 */6 * * *" --smoke
   ```

   Deploys the Cloud Run Job, creates the runtime service account with narrow
   IAM (events-READ, graph-WRITE), wires the Cloud Scheduler trigger, and runs
   `--smoke` to verify in one shot. The deploy script's
   `--graph <name>` mode derives everything from your deployed graph at run
   time (via `INFORMATION_SCHEMA.PROPERTY_GRAPHS`) — nothing staged, no
   checked-in files to edit.

4. **Verify** — Cloud Logging shows the JSON report on every run
   (`jsonPayload.ok`, `sessions_materialized`, `rows_materialized`, per-table
   `table_statuses`). The state table at
   `<graph_dataset>._bqaa_materialization_state` is a queryable audit log.
5. **Alert** — Cloud Monitoring on `severity=ERROR` OR `jsonPayload.ok=false`.
   The `jsonPayload.failures[].error_code` distinguishes `empty_extraction`
   (AI/IAM) from `materialization_failed` (schema/write-perm).

See [`periodic_materialization/README.md`](./periodic_materialization/README.md)
for the full playbook: required APIs, IAM matrix, recommended schedules per
latency target, Cloud Monitoring alert queries, state-table SQL, and
troubleshooting. For a guided end-to-end walkthrough, start with the
[Periodic Materialization codelab](../../docs/codelabs/periodic_materialization.md)
and the [scheduled deploy runbook](../../docs/guides/scheduled-context-graph-deploy.md).

## Try it with the sample agent

The demo's event source of truth is **a runnable agent talking to the
BigQuery Agent Analytics plugin**, not a hand-coded event generator.

```bash
python examples/context_graph/run_agent.py --sessions 3 \
    --project your-project --dataset your_dataset
```

`mako_demo_agent.py` defines a real ADK agent whose tools mirror a decision
flow — `capture_context`, `propose_decision_point`, `evaluate_candidate`,
`commit_outcome`, `complete_execution`, plus a feedback/reward loop
(`record_rejection`, `apply_constraint`, `record_outcome_signal`,
`compute_reward`). As it runs, the plugin streams traces into `agent_events`;
the materializer then turns them into graph rows. The agent uses Vertex AI
Gemini by default (`DEMO_AGENT_MODEL=gemini-2.5-flash`).

---

## Advanced: the explicit ontology + binding pipeline

Everything below this line is the **advanced** path. The two-artifact flow
above covers the common case; reach for an explicit `ontology.yaml` +
`binding.yaml` only when you need finer control — human-readable descriptions
that steer the AI extraction prompt, entity inheritance, derived (computed)
properties, or column renames. You then pass `--ontology`/`--binding` to
`bqaa context-graph` instead of `--graph`.

The artifact pipeline that turns an OWL TTL into binding + DDL +
property-graph SQL is **ontology-agnostic** — see `ontology_artifacts.py` —
and the MAKO config (in `mako_artifacts.py`) is one concrete configuration of
it.

### Pluggable contract

The pipeline takes a single :class:`ontology_artifacts.OntologyConfig` plus a target `(project, dataset)` and produces four files:

| Generated file | What it is |
|---|---|
| `table_ddl.sql` | `CREATE TABLE` SQL for every node + edge table, with SDK metadata columns (`session_id STRING, extracted_at TIMESTAMP`) on every bound table. |
| `property_graph.sql` | `CREATE OR REPLACE PROPERTY GRAPH` over those tables. Node `KEY` + edge `SOURCE KEY ... REFERENCES` use the per-entity PK columns. |
| `ontology.yaml` | `import-owl` output with `FILL_IN` PKs resolved, cross-namespace dangling relationships dropped, inheritance stripped. |
| `binding.yaml` | Maps the configured entity allowlist onto BigQuery tables for the target `(project, dataset)`. Heterogeneous edges use the legacy `from_columns: [<col>]` shape; self-edges (`evolvedFrom`, `supersededBy`) use the explicit dict-shape `from_columns: [{src_<col>: <pk_prop>}]` so the SDK's canonical FK→PK mapping disambiguates the src/dst endpoints. Out-of-scope relationships filtered. |

The pipeline applies three OWL-TTL normalizations generic enough to apply to any reasonable ontology:

1. **`FILL_IN` PKs → `id: string`.** The OWL importer marks entities without `owl:hasKey` declarations as `FILL_IN`; the resolver synthesizes a stable `id` property. Entities that already declare `owl:hasKey` are left untouched.
2. **Cross-namespace dangling relationships dropped.** TTLs that extend upstream ontologies (PROV-O, PKO, DCAT, …) often reference upstream entities the importer didn't pull; the pipeline drops these and records them under a `{annotation_prefix}:dropped_cross_namespace_relationships` audit annotation.
3. **Inheritance stripped.** `gm compile` v0 doesn't support `extends:` clauses; the pipeline drops them and records the loss under `{annotation_prefix}:stripped_inheritance`.

### Example ontologies in this directory

This demo ships **two** ontology configs. The MAKO config is the load-bearing reference example (real production ontology, full agent, runnable demo); the Simple Request Flow config is a smoke fixture that proves the pipeline is genuinely ontology-agnostic.

| Config | TTL | Where | Surface |
|---|---|---|---|
| `MAKO_CONFIG` | `mako_core.ttl` (Yahoo Monetization decision ontology, 18 namespace entities) | `mako_artifacts.py` | Full demo: notebook, `mako_demo_agent.py` (5 decision-flow tools), `reference_extractor.py`, periodic materialization deploy. |
| `SIMPLE_REQUEST_FLOW_CONFIG` | `example_ontologies/simple_request_flow.ttl` (3-entity Request → Action → Outcome flow) | `example_ontologies/simple_request_flow_config.py` | Smoke fixture only. Exercised by `tests/test_context_graph_ontology_artifacts.py`. No runnable agent ships with it. |

A new ontology plugs in the same way: write a TTL, define an `OntologyConfig` naming it, call `regenerate_snapshots(your_config, project=..., dataset=...)`.

### File inventory

| File | What it does |
|------|--------------|
| `ontology_artifacts.py` | Generic ontology-agnostic pipeline: `OntologyConfig` dataclass + `load_ontology`, `make_binding`, `make_table_ddl`, `make_property_graph_sql`, `regenerate_snapshots`. **Does not generate events.** |
| `mako_core.ttl` | The MAKO ontology. Domain-agnostic decision semantics for Yahoo Monetization Platform. |
| `mako_artifacts.py` | MAKO-specific config: `MAKO_CONFIG` + thin back-compat wrappers around the generic pipeline. The notebook imports from this module. |
| `mako_demo_agent.py` | Runnable ADK agent + `BigQueryAgentAnalyticsPlugin` wiring. Defines the MAKO decision-flow tools and a system prompt that walks the agent through them. Real plugin traces land in `agent_events` when the agent runs. MAKO-specific by design — the agent's tools mirror MAKO's decision flow. |
| `run_agent.py` | Driver. `python run_agent.py --sessions 50 --project X --dataset Y` runs the MAKO agent for N sessions and lets the plugin populate `agent_events`. |
| `export_events_jsonl.py` | Optional. Exports a pinned subset of `agent_events` to a local JSONL file for deterministic offline revalidation tests. Not an event generator — it reads from BigQuery. |
| `reference_extractor.py` | Hand-authored compiled reference extractor for the MAKO decision flow (the deterministic fallback path). |
| `example_ontologies/simple_request_flow.ttl` | Pluggability smoke fixture (3 entities, 2 relationships, no cross-namespace imports). |
| `example_ontologies/simple_request_flow_config.py` | `SIMPLE_REQUEST_FLOW_CONFIG` — second `OntologyConfig` that plugs into the same generic pipeline. |
| `ontology.yaml` / `binding.yaml` / `table_ddl.sql` / `property_graph.sql` | **Generated** for MAKO by `mako_artifacts.regenerate_snapshots()`. Checked in so they can be read as-is; regenerate against your own `(project, dataset)` to adapt them. |

The Simple Request Flow config's snapshots are **not checked in** — they're regenerated to a tmpdir by the pluggability test. The TTL + config are the only files under `example_ontologies/`.

### Demo flow (what the notebook does)

The end-to-end walkthrough lives in
`examples/_archive/context_graph_historical_notebook.ipynb`:

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

Beat 3's compile / runtime / savings cells run live, using `reference_extractor.extract_mako_decision_event` as the runtime fallback under a focused compiled bundle that handles the `complete_execution` event type. Beat 5 closes the arc with the feedback/reward loop: the reference extractor emits `RejectionReason`, `BusinessConstraint` + `ConstraintApplication`, `OutcomeSignal`, and `RewardComputation` nodes plus the edges (`hasRejectionReason`, `appliedConstraint`, `filteredByConstraint`, `producedOutcome`, `derivedReward`) that wire them back into the Beat 1–4 hub. A complete live run with cell outputs inline is captured in the archived notebook.

### Reference example: MAKO design decisions

The decisions below are documented in MAKO terms because MAKO is the load-bearing reference example, but the underlying pipeline behavior is general. Section numbers map to the transformations the generic pipeline applies (see "Pluggable contract" above).

#### 1. MAKO `DecisionExecution` is the central hub

The MAKO demo entity allowlist (`DEMO_ENTITIES` in `mako_artifacts.py`) is **eleven entities** split across the two-beat arc:

* **Beats 1–4 hub** (6 entities): `AgentSession`, `DecisionExecution`, `DecisionPoint`, `Candidate`, `SelectionOutcome`, `ContextSnapshot`. `DecisionExecution` is non-obvious but load-bearing — per MAKO's TTL, it's the entity that's `partOfSession` an `AgentSession`, `atContextSnapshot` a `ContextSnapshot`, `executedAtDecisionPoint` a `DecisionPoint`, `hasSelectionOutcome` a `SelectionOutcome`. The decision-flow story doesn't hold together without it.
* **Beat 5 feedback / reward loop** (5 entities): `BusinessConstraint`, `ConstraintApplication`, `RejectionReason`, `OutcomeSignal`, `RewardComputation`. Each captures one slice of "what happened *after* the decision" — constraint evaluations, candidate rejections, observed real-world outcomes, and the RL reward computed from those outcomes.

The edge set is **TTL-driven with one filter**: `make_binding` walks `ontology.relationships` and picks every relationship whose endpoints both fall within the entity allowlist. The current MAKO binding has **fourteen** relationships covering eleven entities:

* **Beats 1–4 hub** (7 edges + 2 self-edges, 6 entities): `atContextSnapshot`, `evaluatesCandidate`, `executedAtDecisionPoint`, `hasSelectionOutcome`, `partOfSession`, `rejectedCandidate`, `selectedCandidate`, plus the two `DecisionExecution → DecisionExecution` self-edges (`evolvedFrom`, `supersededBy`).
* **Beat 5 feedback / reward loop** (5 edges, 5 entities): `hasRejectionReason` (Candidate → RejectionReason), `appliedConstraint` (ConstraintApplication → BusinessConstraint), `filteredByConstraint` (Candidate → ConstraintApplication), `producedOutcome` (DecisionExecution → OutcomeSignal), `derivedReward` (RewardComputation → OutcomeSignal).

The self-edges use the explicit dict-shape `from_columns`; see design decision 9 below for the column convention.

#### 2. FILL_IN resolution: synthesize `id: string`

The MAKO TTL doesn't declare `owl:hasKey` on most entities, so the OWL importer marks 17 concrete entities' primary keys as `FILL_IN`. The pipeline resolves each one to a synthesized `id: string` property + primary key. Matches MAKO's "every artifact has a stable identifier" design contract. If a future MAKO revision adds `owl:hasKey` declarations, the resolver leaves those alone — only `FILL_IN` placeholders get rewritten. This rule is **general** — any TTL with `FILL_IN` PKs gets the same treatment.

#### 3. Cross-namespace relationships dropped (with audit trail)

MAKO extends PROV-O + PKO + DCAT. Four relationships in the TTL point to entities outside MAKO's own namespace (`delegatedBy → prov:Agent`, etc.). The artifact pipeline drops these so the ontology loads cleanly and records the dropped names under the ontology's top-level `mako_demo:dropped_cross_namespace_relationships` annotation. The loss is auditable from a loaded model. **General behavior**: any TTL extending upstream ontologies gets the same treatment; the annotation key uses the config's `annotation_prefix`.

#### 4. Agent uses realistic tool names; mapping is explicit

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
| `session_id` (envelope) | `partOfSession` edge (DecisionExecution → AgentSession) | Plugin envelope. Both the compiled extractor (for `complete_execution` events) and the reference extractor (fallback path) synthesize the `AgentSession` node + `partOfSession` edge from this envelope field. |

**Rule of thumb:** only fields with a TTL-declared target property are materialized; everything else stays in the raw `agent_events` trace as reasoning context. The mapping above is the contract `reference_extractor.extract_mako_decision_event` (and the compiled bundle) implement.

Same wiring pattern as `examples/decision_lineage_demo/agent/agent.py`.

#### 5. `(project, dataset)` is a parameter, not a baked-in value

`mako_artifacts.regenerate_snapshots(project=..., dataset=...)` and `run_agent.py --project X --dataset Y` both take the target as input. The checked-in snapshots carry reference defaults so the files can be read as-is; regenerate against your own `(project, dataset)` to adapt them. The generic pipeline takes `(project, dataset)` the same way — they're never config-time constants.

#### 6. `events.jsonl` is captured, not synthesized

If kept, `events.jsonl` is the output of `export_events_jsonl.py` reading from `agent_events`. It serves as an offline corpus for deterministic revalidation tests, but the demo's primary event surface is the live `agent_events` table.

The exporter's `SELECT` projects the subset of the BigQuery Agent Analytics plugin's schema the tests need (`google/adk/plugins/bigquery_agent_analytics_plugin.py::_get_events_schema`): `timestamp`, `event_type`, `agent`, `session_id`, `invocation_id`, `user_id`, `trace_id`, `span_id`, `parent_span_id`, `status`, `error_message`, `is_truncated`, plus `content` / `attributes` / `latency_ms` (all JSON). The plugin's full schema also includes `content_parts` (REPEATED RECORD for multimodal parts); the exporter omits it because the MAKO decision flow is text-only.

#### 7. Table DDL carries SDK metadata columns

`make_table_ddl()` appends `session_id STRING, extracted_at TIMESTAMP` to every node and edge table because the materializer writes both on every `materialize()` call and `binding_validation.py` requires them on every bound table. Without them, the binding-validate step would fail before ontology-build. When a domain property already maps to one of those columns (MAKO's `AgentSession.sessionId → session_id`), the metadata copy is skipped to avoid a duplicate-column error. **General behavior**: applies to every config.

#### 8. Per-entity PK columns

Every entity's PK column is `{entity_short}_id` (`decision_execution_id`, `candidate_id`, `agent_session_id`, …), not a bare `id`. Even with the canonical FK→PK mapping in place (so the materializer can resolve self-edges where `src_<col>_id` deliberately differs from the PK column), heterogeneous edges keep the legacy `list[str]` shape — and that shape pairs binding columns positionally with the endpoint's PK columns. With bare `id` as the PK, every cross-entity edge would land `(id STRING, id STRING)` (duplicate-column error) and the heterogeneous-edge codepath would have no way to disambiguate without forcing every binding into dict-shape. **General behavior**: applies to every config; the Simple Request Flow binding produces `request_id` / `action_id` / `outcome_id` for the same reason.

GQL queries reference `de.decision_execution_id` (not `de.id`) for the same reason.

#### 9. Self-edges via explicit FK→PK mapping

MAKO declares `evolvedFrom` and `supersededBy` as `DecisionExecution → DecisionExecution` self-edges. The natural composite `(decision_execution_id, decision_execution_id)` is a duplicate-column error, and bare `src_/dst_` prefixing on its own misses the materializer's property-column lookup (the FK column no longer matches any property name on the endpoint entity).

The SDK's canonical FK→PK mapping runs through the materializer, the validator, and the PG DDL compiler — so the binding can declare self-edges using the dict-shape `from_columns`:

```yaml
- name: evolvedFrom
  source: <project>.<dataset>.evolved_from
  from_columns:
  - src_decision_execution_id: id     # edge_col -> endpoint PK property
  to_columns:
  - dst_decision_execution_id: id
```

`make_binding()` emits this shape for any `rel.from_ == rel.to`. The materializer's `_route_edge` uses the canonical `from_column_mapping` / `to_column_mapping` to translate the parsed node-id segment (keyed by endpoint *column*) into the right edge-table FK column. The PG DDL compiler resolves `SOURCE KEY (src_decision_execution_id) REFERENCES decision_execution (decision_execution_id)` correctly. **General behavior**: applies to any config — self-edges are never dropped.

#### 10. Inheritance stripped from entities

The MAKO TTL declares `mako:Candidate rdfs:subClassOf mako:RoleTrait`; the OWL importer surfaces this as `Candidate.extends: RoleTrait`. `gm compile` v0 doesn't support inheritance and rejects the binding (`compile-validation — Entity 'Candidate' uses 'extends'`), which blocks the `--emit-concept-index` step. The pipeline drops `extends` from the post-import YAML and audit-trails the discard under the config's `annotation_prefix` (`mako_demo:stripped_inheritance` for MAKO). `RoleTrait` is a marker class in MAKO with no properties beyond the `id` PK every other entity already has, so the discard has no semantic effect on the demo's 11-entity scope. **General behavior**: applies to any TTL with `extends:` clauses.

### Validate the pipeline

```bash
# MAKO artifact pipeline runs end-to-end and regenerates snapshots.
PYTHONPATH=src python examples/context_graph/mako_artifacts.py
# → {"binding_entities": 11, "binding_relationships": 14,
#    "ontology_entities": 18}

# Generic pipeline works against the Simple Request Flow smoke fixture.
PYTHONPATH=src pytest tests/test_context_graph_ontology_artifacts.py

# Generated MAKO ontology validates clean.
python -m bigquery_ontology.cli validate examples/context_graph/ontology.yaml

# Generated MAKO binding validates against the generated ontology.
python -m bigquery_ontology.cli validate examples/context_graph/binding.yaml \
    --ontology examples/context_graph/ontology.yaml

# Property graph + concept index DDL compiles clean.
python -m bigquery_ontology.cli compile \
    --emit-concept-index \
    --concept-index-table '<project>.<dataset>.mako_concept_index' \
    --ontology examples/context_graph/ontology.yaml \
    examples/context_graph/binding.yaml

# Demo agent + plugin import cleanly.
PYTHONPATH=src:examples/context_graph python -c "
import mako_demo_agent
print(type(mako_demo_agent.root_agent).__name__,
      len(mako_demo_agent.root_agent.tools),
      type(mako_demo_agent.bq_logging_plugin).__name__)"
# → LlmAgent 9 BigQueryAgentAnalyticsPlugin

# Driver --help works without live BQ / Vertex.
PYTHONPATH=src python examples/context_graph/run_agent.py --help

# Exporter --help + identifier validation.
PYTHONPATH=src python examples/context_graph/export_events_jsonl.py --help
```

### Deploying the explicit-ontology graph on a schedule

The periodic-materialization deploy also supports this path: bundle the
checked-in `binding.yaml` / `ontology.yaml` / `table_ddl.sql` instead of
pointing at a deployed graph. Running it against a different `OntologyConfig`
means regenerating those snapshots for your config first
(`mako_artifacts.py` / `ontology_artifacts.py`) and pointing the deploy at the
new files. The rename-free `--graph` mode described above remains the
common path.

## Related

- [`periodic_materialization/`](./periodic_materialization/) — production deployment: Cloud Run Job + Cloud Scheduler, IAM matrix, alerting.
- [Periodic Materialization codelab](../../docs/codelabs/periodic_materialization.md) — guided end-to-end walkthrough.
- [Scheduled deploy runbook](../../docs/guides/scheduled-context-graph-deploy.md) — take a deployed graph to a scheduled `--graph` deploy.
- [`examples/decision_lineage_demo/`](../decision_lineage_demo/) — reference pattern for ADK agent + BigQuery Agent Analytics plugin wiring.
- [Rollout guide](../../docs/extractor_compilation_rollout_guide.md) — extractor compilation pipeline reference.
- [Ontology runtime reader](../../docs/ontology_runtime_reader.md) — runtime reader API.
