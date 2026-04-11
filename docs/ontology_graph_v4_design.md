# Ontology Graph V4 — Configuration-Driven Context Graph

Turn unstructured agent telemetry into a queryable knowledge graph using a
single YAML file. No code changes required — define your ontology, point
it at your data, and the SDK handles extraction, materialization, and
BigQuery Property Graph creation automatically.

**Live demo:** [ontology-v4-deploy.vercel.app](https://ontology-v4-deploy.vercel.app)
| **Notebook:** [`examples/ontology_graph_v4_demo.ipynb`](../examples/ontology_graph_v4_demo.ipynb)

---

## Why This Matters

Modern ad-tech and commerce platforms make thousands of micro-decisions per
request — which ad unit to show, which creative to select, which candidate
to reject and why. These decisions are captured as unstructured agent
telemetry, but answering questions like *"Why was this ad rejected?"* or
*"What was the confidence score for each candidate?"* requires tedious
manual log parsing.

The Ontology Graph V4 pipeline solves this by:

1. **Declaring** your business domain once in YAML — entities, relationships,
   keys, and table bindings.
2. **Extracting** structured graph data from raw agent logs using
   `AI.GENERATE` (Gemini), guided by your ontology.
3. **Materializing** typed nodes and edges into dedicated BigQuery tables.
4. **Creating** a native BigQuery Property Graph for GQL traversal.

The result: you can write queries like *"For session X, show me every
DecisionPoint, the candidates it evaluated, their scores, and why any
were rejected"* — in native GQL, with full type safety.

---

## Architecture

```
┌─────────────────────┐
│   YAML Ontology     │  Entities, relationships, keys, table bindings
│   Specification     │  (single source of truth)
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐     ┌──────────────────────────────┐
│   Schema Compiler   │────▶│  Extraction Prompt + Schema   │
└────────┬────────────┘     └──────────────────────────────┘
         │
         ▼
┌─────────────────────┐     ┌──────────────────────────────┐
│   AI.GENERATE       │────▶│  ExtractedGraph (Pydantic)    │
│   (BigQuery-native) │     │  Typed nodes + edges          │
└────────┬────────────┘     └──────────────────────────────┘
         │
         ▼
┌─────────────────────┐     ┌──────────────────────────────┐
│   Materializer      │────▶│  Physical BigQuery Tables     │
│   (table routing)   │     │  One table per entity/rel     │
└────────┬────────────┘     └──────────────────────────────┘
         │
         ▼
┌─────────────────────┐     ┌──────────────────────────────┐
│   DDL Transpiler    │────▶│  CREATE PROPERTY GRAPH        │
│                     │     │  NODE TABLES + EDGE TABLES    │
└────────┬────────────┘     └──────────────────────────────┘
         │
         ▼
┌─────────────────────┐
│   GQL Queries       │  MATCH (dp)-[ce]->(ad) WHERE ...
│   (graph traversal) │  Native BigQuery graph analytics
└─────────────────────┘
```

---

## YAML Ontology Specification

The YAML spec is the single source of truth. It defines what to extract,
where to store it, and how to link it in the graph.

### Format Reference

```yaml
graph:
  name: My_Context_Graph

  entities:
    - name: EntityName              # Node type name
      description: "..."            # Used in AI extraction prompt
      extends: ParentLabel          # Optional label inheritance
      binding:
        source: "{{ env }}.table"   # Physical BigQuery table
      keys:
        primary: [id_column]        # Primary key column(s)
      properties:
        - name: column_name
          type: string              # string | int64 | double | bool | timestamp

  relationships:
    - name: EdgeName                # Edge type name
      description: "..."
      from_entity: SourceEntity
      to_entity: TargetEntity
      binding:
        source: "{{ env }}.edge_table"
        from_columns: [source_key]
        to_columns: [target_key]
      properties:
        - name: edge_property
          type: double
```

**Key features:**

- **`{{ env }}` placeholders** — resolved at load time, so the same spec
  works across dev/staging/prod environments.
- **Label inheritance** (`extends`) — an entity can carry multiple labels
  in the graph (e.g., `YahooAdUnit` is also a `Candidate`).
- **Explicit key routing** — `from_columns` / `to_columns` define how edge
  tables reference source and target nodes. For Property Graph compilation,
  these must exactly match the referenced entity's full primary key;
  subset bindings are supported for table materialization but will be
  rejected by the DDL transpiler.

### Example: Yahoo ADCP Ad Decisioning

This real-world example models Yahoo's ad decisioning pipeline where
an agent evaluates candidate ad units at decision points.

```yaml
graph:
  name: YMGO_Context_Graph_V3

  entities:
    - name: mako_DecisionPoint
      description: "The atomic unit of decisioning where an agent evaluates alternatives."
      binding:
        source: "{{ env }}.decision_points"
      keys:
        primary: [decision_id]
      properties:
        - name: decision_id
          type: string
        - name: decision_type
          type: string

    - name: sup_YahooAdUnit
      extends: mako_Candidate
      description: "A specific ad slot on a Yahoo property being evaluated as a candidate."
      binding:
        source: "{{ env }}.yahoo_ad_units"
      keys:
        primary: [adUnitId]
      properties:
        - name: adUnitId
          type: string
        - name: adUnitName
          type: string
        - name: adUnitSize
          type: string
          description: "e.g., '300x250', '728x90'"
        - name: adUnitPosition
          type: string
          description: "ATF (above the fold) | BTF (below the fold)"

    - name: mako_RejectionReason
      description: "Structured reason why a Candidate was not selected at a DecisionPoint."
      binding:
        source: "{{ env }}.rejection_reasons"
      keys:
        primary: [rejection_id]
      properties:
        - name: rejection_id
          type: string
        - name: rejectionType
          type: string
          description: "RULE_BASED | MODEL_BASED | TIMEOUT | ERROR"
        - name: rejectionRule
          type: string
          description: "The specific rule or model threshold that caused rejection."

  relationships:
    - name: CandidateEdge
      description: "Connects a decision point to the evaluated Yahoo Ad Unit."
      from_entity: mako_DecisionPoint
      to_entity: sup_YahooAdUnit
      binding:
        source: "{{ env }}.candidate_edges"
        from_columns: [decision_id]
        to_columns: [adUnitId]
      properties:
        - name: edge_type
          type: string
          description: "SELECTED_CANDIDATE or DROPPED_CANDIDATE"
        - name: mako_scoreValue
          type: double
          description: "The confidence or predicted Q-value for this candidate."

    - name: ForCandidate
      description: "Maps the MAKO rejection rationale directly to the dropped candidate."
      from_entity: mako_RejectionReason
      to_entity: sup_YahooAdUnit
      binding:
        source: "{{ env }}.rejection_mappings"
        from_columns: [rejection_id]
        to_columns: [adUnitId]
```

This produces the following graph structure:

```
DecisionPoint ──CandidateEdge──▶ YahooAdUnit ◀──ForCandidate── RejectionReason
```

---

## Usage

### Python SDK

**One-shot pipeline** — runs the entire flow in a single call:

```python
from bigquery_agent_analytics import build_ontology_graph, compile_showcase_gql

# Run the full pipeline
result = build_ontology_graph(
    session_ids=["adcp-033c95d7a97d", "adcp-040c04837251"],
    spec_path="ymgo_graph_spec.yaml",
    project_id="my-project",
    dataset_id="agent_analytics",
    env="my-project.agent_analytics",
)

print(f"Extracted {len(result['graph'].nodes)} nodes, {len(result['graph'].edges)} edges")
print(f"Property Graph: {result['graph_ref']}")

# Generate a GQL traversal query
gql = compile_showcase_gql(result["spec"], "my-project", "agent_analytics")
print(gql)
```

**Step-by-step** — for more control over each phase:

```python
from bigquery_agent_analytics import (
    load_graph_spec,
    OntologyGraphManager,
    OntologyMaterializer,
    OntologyPropertyGraphCompiler,
    compile_extraction_prompt,
    compile_output_schema,
)

# 1. Load spec
spec = load_graph_spec("ymgo_graph_spec.yaml", env="my-project.agent_analytics")

# 2. Extract graph from agent telemetry
extractor = OntologyGraphManager(
    project_id="my-project",
    dataset_id="agent_analytics",
    spec=spec,
    endpoint="gemini-2.5-flash",
)
graph = extractor.extract_graph(session_ids=["adcp-033c95d7a97d"])

# 3. Create tables and materialize
materializer = OntologyMaterializer(
    project_id="my-project",
    dataset_id="agent_analytics",
    spec=spec,
)
materializer.create_tables()
materializer.materialize(graph, session_ids=["adcp-033c95d7a97d"])

# 4. Create Property Graph
compiler = OntologyPropertyGraphCompiler(
    project_id="my-project",
    dataset_id="agent_analytics",
    spec=spec,
)
compiler.create_property_graph()
```

### CLI

```bash
# Run the full pipeline
bq-agent-sdk ontology-build \
    --project-id=my-project \
    --dataset-id=agent_analytics \
    --spec-path=ymgo_graph_spec.yaml \
    --session-ids=adcp-033c95d7a97d,adcp-040c04837251 \
    --env=my-project.agent_analytics

# Generate a GQL showcase query
bq-agent-sdk ontology-showcase-gql \
    --project-id=my-project \
    --dataset-id=agent_analytics \
    --spec-path=ymgo_graph_spec.yaml \
    --env=my-project.agent_analytics
```

---

## Generated Property Graph DDL

The SDK dynamically generates BigQuery `CREATE PROPERTY GRAPH` DDL from
your YAML spec. For the Yahoo ADCP example above:

```sql
CREATE OR REPLACE PROPERTY GRAPH `my-project.agent_analytics.YMGO_Context_Graph_V3`
  NODE TABLES (
    `my-project.agent_analytics.decision_points` AS mako_DecisionPoint
      KEY (decision_id, session_id)
      LABEL mako_DecisionPoint
      PROPERTIES (decision_id, decision_type, session_id, extracted_at),
    `my-project.agent_analytics.yahoo_ad_units` AS sup_YahooAdUnit
      KEY (adUnitId, session_id)
      LABEL sup_YahooAdUnit
      LABEL mako_Candidate
      PROPERTIES (adUnitId, adUnitName, adUnitSize, adUnitPosition, session_id, extracted_at),
    `my-project.agent_analytics.rejection_reasons` AS mako_RejectionReason
      KEY (rejection_id, session_id)
      LABEL mako_RejectionReason
      PROPERTIES (rejection_id, rejectionType, rejectionRule, session_id, extracted_at)
  )
  EDGE TABLES (
    `my-project.agent_analytics.candidate_edges` AS CandidateEdge
      KEY (decision_id, adUnitId, session_id)
      SOURCE KEY (decision_id, session_id) REFERENCES mako_DecisionPoint (decision_id, session_id)
      DESTINATION KEY (adUnitId, session_id) REFERENCES sup_YahooAdUnit (adUnitId, session_id)
      LABEL CandidateEdge
      PROPERTIES (edge_type, mako_scoreValue, extracted_at),
    `my-project.agent_analytics.rejection_mappings` AS ForCandidate
      KEY (rejection_id, adUnitId, session_id)
      SOURCE KEY (rejection_id, session_id) REFERENCES mako_RejectionReason (rejection_id, session_id)
      DESTINATION KEY (adUnitId, session_id) REFERENCES sup_YahooAdUnit (adUnitId, session_id)
      LABEL ForCandidate
      PROPERTIES (extracted_at)
  )
```

**Design notes:**

- Node KEY includes `session_id` so the same business entity in different
  sessions produces distinct graph nodes, making multi-session builds safe.
- All KEY columns are also listed in PROPERTIES so they are queryable in GQL.
- `extracted_at` is automatically added to every node and edge table.

---

## GQL Query Examples

Once the Property Graph is created, you can traverse it with native GQL.

### Forward traversal: DecisionPoint to Candidates

```sql
GRAPH `my-project.agent_analytics.YMGO_Context_Graph_V3`
MATCH
  (dp:mako_DecisionPoint)-[ce:CandidateEdge]->(ad:sup_YahooAdUnit)
WHERE dp.session_id = @session_id
RETURN
  dp.decision_id,
  dp.decision_type,
  ce.edge_type,
  ce.mako_scoreValue,
  ad.adUnitId,
  ad.adUnitName,
  ad.adUnitSize,
  ad.adUnitPosition
ORDER BY dp.decision_id
LIMIT 100
```

### Rejection audit: Why was this ad unit rejected?

```sql
GRAPH `my-project.agent_analytics.YMGO_Context_Graph_V3`
MATCH
  (rr:mako_RejectionReason)-[fc:ForCandidate]->(ad:sup_YahooAdUnit)
WHERE ad.session_id = @session_id
RETURN
  ad.adUnitName,
  rr.rejectionType,
  rr.rejectionRule,
  rr.extracted_at
ORDER BY ad.adUnitName
```

### Full decision path: DecisionPoint to Candidate with Rejection Reasons

```sql
GRAPH `my-project.agent_analytics.YMGO_Context_Graph_V3`
MATCH
  (dp:mako_DecisionPoint)-[ce:CandidateEdge]->(ad:sup_YahooAdUnit),
  (rr:mako_RejectionReason)-[fc:ForCandidate]->(ad)
WHERE dp.session_id = @session_id
  AND ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  dp.decision_id,
  ad.adUnitName,
  ce.mako_scoreValue AS candidate_score,
  rr.rejectionType,
  rr.rejectionRule
```

---

## Adapting for Your Own Domain

The pipeline is fully generic. To model your own business domain:

1. **Define entities** — your domain objects (e.g., `Customer`, `Order`,
   `Product`, `Campaign`, `AdCreative`).
2. **Define relationships** — how they connect (e.g., `Placed` from
   `Customer` to `Order`, `Contains` from `Order` to `Product`).
3. **Set table bindings** — where each entity/relationship is stored.
4. **Run the pipeline** — no code changes needed.

### Minimal example: E-commerce Orders

```yaml
graph:
  name: ecommerce_graph

  entities:
    - name: Customer
      description: "A customer who places orders."
      binding:
        source: "{{ env }}.customers"
      keys:
        primary: [customer_id]
      properties:
        - name: customer_id
          type: string
        - name: customer_name
          type: string

    - name: Order
      description: "A purchase order."
      binding:
        source: "{{ env }}.orders"
      keys:
        primary: [order_id]
      properties:
        - name: order_id
          type: string
        - name: total_amount
          type: double

  relationships:
    - name: Placed
      description: "Customer placed an order."
      from_entity: Customer
      to_entity: Order
      binding:
        source: "{{ env }}.placed_edges"
        from_columns: [customer_id]
        to_columns: [order_id]
      properties:
        - name: placed_at
          type: string
```

```bash
bq-agent-sdk ontology-build \
    --spec-path=ecommerce_spec.yaml \
    --session-ids=sess-1,sess-2 \
    --project-id=my-project \
    --dataset-id=analytics \
    --env=my-project.analytics
```

---

## SDK Module Reference

| Module | Class / Function | Purpose |
|--------|-----------------|---------|
| `ontology_models` | `load_graph_spec()` | Parse YAML and resolve `{{ env }}` placeholders |
| `ontology_schema_compiler` | `compile_extraction_prompt()`, `compile_output_schema()` | Generate AI extraction prompt and JSON schema from spec |
| `ontology_graph` | `OntologyGraphManager` | Extract typed graph from agent telemetry via AI.GENERATE |
| `ontology_materializer` | `OntologyMaterializer` | Create tables and route extracted data into them |
| `ontology_property_graph` | `OntologyPropertyGraphCompiler`, `compile_property_graph_ddl()` | Generate and execute `CREATE PROPERTY GRAPH` DDL |
| `ontology_orchestrator` | `build_ontology_graph()`, `compile_showcase_gql()` | One-shot pipeline and GQL query generation |

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **YAML over code** | Domain experts can define ontologies without writing Python. Spec changes don't require redeployment. |
| **`{{ env }}` over Jinja2** | Simple string replacement keeps the spec portable without introducing a template engine dependency. |
| **Label-only inheritance** | `extends` adds a graph label, not property/binding inheritance. This keeps resolution deterministic and avoids diamond inheritance complexity. |
| **Session-scoped node identity** | Including `session_id` in every KEY means the same business entity in different sessions produces distinct nodes. This enables multi-session graph builds without collision. |
| **Delete-then-insert idempotency** | Re-running the pipeline for the same sessions replaces previous data rather than duplicating it. |
| **BigQuery-native extraction** | `AI.GENERATE` runs server-side in BigQuery, avoiding data transfer costs and keeping the pipeline SQL-native. |
