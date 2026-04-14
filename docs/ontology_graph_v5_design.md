# Ontology Graph V5: TTL Import, Mixed Extraction, Temporal Lineage

Build production-scale context graphs from OWL ontologies, mixed telemetry
sources, and cross-session entity evolution — all in BigQuery, driven by a
single YAML spec.

**Notebook:** [`examples/ontology_graph_v5_demo.ipynb`](../examples/ontology_graph_v5_demo.ipynb)

---

## Why This Matters

V4 introduced configuration-driven graph pipelines: declare an ontology in
YAML, extract structured data from agent telemetry via `AI.GENERATE`, and
materialize it into a BigQuery Property Graph. But real ad-tech pipelines
hit three walls that V4 does not address:

1. **Ontology bootstrapping.** Industry-standard ontologies ship as OWL/Turtle
   files (IAB, ADCP, FIBO). Hand-translating 36 classes and 112 properties
   into YAML is error-prone and slow.
2. **Extraction reliability.** AI.GENERATE is powerful but non-deterministic.
   For high-volume structured events (BKA decision traces, bid responses),
   the LLM is unnecessary overhead — and a source of hallucinated fields.
3. **Temporal scope.** V4 graphs are session-scoped snapshots. Answering
   *"How did this ad unit's targeting change between yesterday's campaign
   and today's?"* requires cross-session entity lineage.

V5 solves all three with backwards-compatible extensions to the existing
pipeline.

---

## Architecture

```
                          ┌───────────────┐
OWL/Turtle files  ───────►│  TTL Importer  │──► GraphSpec YAML
(YAMO, IAB, ADCP)         │  (Phase 1+2)  │    (runtime-ready)
                          └───────────────┘
                                  │
                                  ▼
┌─────────────────────┐   ┌─────────────────────┐
│   YAML Ontology     │   │  Structured          │
│   Specification     │   │  Extractor Registry   │
│   (GraphSpec)       │   │  (BKA, ADCP, ...)     │
└────────┬────────────┘   └────────┬──────────────┘
         │                         │
         ▼                         ▼
┌──────────────────────────────────────────────────┐
│   OntologyGraphManager                            │
│   ┌──────────────┐  ┌──────────────────────────┐ │
│   │ Structured    │  │ AI.GENERATE              │ │
│   │ (fast-path)   │  │ (unhandled spans only)   │ │
│   └──────┬───────┘  └──────────┬───────────────┘ │
│          │    dedup by node_id  │                  │
│          └──────────┬───────────┘                  │
└─────────────────────┼──────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────┐
│   Materializer (batch_load)                          │
│   staging table → DELETE → INSERT → DROP staging     │
│   Per-table TableStatus: idempotent = True/False     │
└────────┬────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────┐     ┌──────────────────────────────┐
│   Property Graph    │────►│  GQL Queries                  │
│   DDL Transpiler    │     │  Forward + Lineage traversal  │
└─────────────────────┘     └──────────────────────────────┘
```

---

## What V5 Adds to V4

| Capability | V4 | V5 |
|------------|----|----|
| **Ontology source** | Hand-authored YAML | YAML + OWL/Turtle import (`ttl_import` / `ttl_resolve`) |
| **Extraction** | AI.GENERATE only | Structured extractors + AI for unhandled spans |
| **Write mode** | Streaming insert (buffer conflicts) | Batch load via staging tables (`write_mode="batch_load"`) |
| **Temporal scope** | Single-session snapshots | Cross-session lineage via `detect_lineage_edges` |
| **GQL** | Forward traversal | + `compile_lineage_gql` for evolution queries |
| **Status reporting** | Row counts only | `MaterializationResult` with per-table `TableStatus` |

All V5 features are additive — existing V4 specs and workflows work unchanged.

---

## Feature 1: OWL/Turtle Import

### The problem

Yahoo's YAMO ontology (v2.2) ships as a ~10K-line Turtle file with 36 classes,
112 properties, and 31 SKOS concept schemes. Manually translating this into
GraphSpec YAML is tedious and error-prone.

### The solution: two-phase import

```
OWL/TTL file  ──► ttl_import()  ──► *.import.yaml (unresolved)
                                    + ImportReport
                                         │
                                  user edits / defaults
                                         │
                                         ▼
                                    ttl_resolve()  ──► GraphSpec YAML
                                                       (load_graph_spec ready)
```

**Phase 1** parses the OWL file, maps classes/properties/relationships, and
emits an unresolved artifact with `FILL_IN` placeholders for ambiguities
(missing keys, multi-parent inheritance). **Phase 2** resolves placeholders
and produces runtime-ready YAML.

### Usage

```python
from bigquery_agent_analytics import ttl_import, ttl_resolve
from bigquery_agent_analytics.ontology_models import load_graph_spec_from_string

# Phase 1: Import
result = ttl_import(
    "yamo_v2.2.ttl",
    include_namespaces=["https://example.com/yamo#"],
)
print(f"Classes: {result.report.classes_mapped}")
print(f"Placeholders: {len(result.report.placeholders)}")
print(f"Type warnings: {len(result.report.type_warnings)}")

# Save the unresolved artifact
with open("yamo.import.yaml", "w") as f:
    f.write(result.yaml_text)

# Phase 2: Resolve
resolved = ttl_resolve(
    "yamo.import.yaml",
    defaults={
        "entities[DecisionPoint].keys.primary": ["decision_id"],
    },
)

# Load as a validated GraphSpec
spec = load_graph_spec_from_string(resolved)
print(f"Entities: {[e.name for e in spec.entities]}")
```

### Type mapping

The importer maps OWL XSD types to the runtime type subset:

| OWL / XSD type | Runtime type | Notes |
|----------------|-------------|-------|
| `xsd:string`, `xsd:token`, `xsd:anyURI` | `string` | |
| `xsd:integer`, `xsd:int`, `xsd:long` | `int64` | |
| `xsd:double`, `xsd:float` | `double` | |
| `xsd:decimal` | `double` | Narrowed with warning |
| `xsd:boolean` | `boolean` | |
| `xsd:date` | `date` | |
| `xsd:dateTime` | `timestamp` | |
| `xsd:time` | `string` | Unsupported, narrowed with warning |
| `rdf:JSON` | `string` | Serialized as text |

### Artifact boundary

The unresolved artifact uses a distinct `ontology_import:` metadata block
that makes it visually and programmatically distinguishable from a valid
GraphSpec. Feeding it directly to `load_graph_spec()` produces a clear
`ValueError` pointing at the unresolved placeholder.

---

## Feature 2: Mixed Structured + AI Extraction

### The problem

For high-volume ADCP decision traces (5M+ events/day), running every event
through `AI.GENERATE` is slow, expensive, and non-deterministic. Structured
events like BKA decision traces have typed fields that can be extracted
directly.

### The solution: structured extractor registry

Register deterministic extractors for known event types. The AI path
handles only the unstructured remainder.

```python
from bigquery_agent_analytics import (
    OntologyGraphManager,
    extract_bka_decision_event,
)

extractor = OntologyGraphManager(
    project_id="my-project",
    dataset_id="agent_analytics",
    spec=spec,
    extractors={"BKA_DECISION": extract_bka_decision_event},
)

graph = extractor.extract_graph(
    session_ids=["adcp-033c95d7a97d"],
    use_ai_generate=True,
)
```

### How deduplication works

```
1. Fetch all events for session (all event types)
2. Run registered extractors → nodes, edges, handled span IDs
3. Build AI.GENERATE transcript excluding fully-handled spans
4. For partially-handled spans: include in transcript with hint
5. Run AI.GENERATE on filtered transcript
6. Merge: structured wins on node_id conflict
```

Structured extractors return a `StructuredExtractionResult` with
`fully_handled_span_ids` (excluded from AI transcript) and
`partially_handled_span_ids` (included with extraction hint).

### Writing a custom extractor

```python
from bigquery_agent_analytics import StructuredExtractionResult

def extract_bid_response(event, spec):
    content = event.get("content", {})
    if "bid_id" not in content:
        return StructuredExtractionResult()

    node = ExtractedNode(
        node_id=f"{event['session_id']}:BidResponse:bid_id={content['bid_id']}",
        entity_name="BidResponse",
        labels=["BidResponse"],
        properties=[
            ExtractedProperty(name="bid_id", value=content["bid_id"]),
            ExtractedProperty(name="bid_amount", value=content.get("amount", 0)),
        ],
    )
    return StructuredExtractionResult(
        nodes=[node],
        fully_handled_span_ids={event["span_id"]},
    )

# Register it
extractor = OntologyGraphManager(
    ...,
    extractors={
        "BKA_DECISION": extract_bka_decision_event,
        "BID_RESPONSE": extract_bid_response,
    },
)
```

---

## Feature 3: Batch Load Materialization

### The problem

V4 uses BigQuery streaming inserts (`insert_rows_json`) followed by
`DELETE` for session-scoped idempotency. But `DELETE` fails silently on
tables with active streaming buffers, causing stale duplicate rows.

### The solution: `write_mode="batch_load"`

```python
from bigquery_agent_analytics import OntologyMaterializer

materializer = OntologyMaterializer(
    project_id="my-project",
    dataset_id="agent_analytics",
    spec=spec,
    write_mode="batch_load",
)

result = materializer.materialize_with_status(graph, session_ids)
print(f"Rows: {result.row_counts}")
for ref, status in result.table_statuses.items():
    print(f"  {ref}: idempotent={status.idempotent}")
```

**How it works:**

1. Load rows into a staging table via `load_table_from_json`
2. `DELETE` existing session rows from the target
3. `INSERT INTO target SELECT * FROM staging`
4. Drop the staging table

Because `batch_load` never calls `insert_rows_json`, it does not create
new streaming-buffer conflicts on the target table. If the target already
has an active streaming buffer from prior writes (e.g., a previous run
used `write_mode="streaming"`), the `DELETE` in step 2 may still fail —
the `TableStatus` will report `cleanup_status="delete_failed"` and
`idempotent=False` in that case.

For fully clean idempotency, use `batch_load` on fresh tables or
scratch datasets (as the demo notebook does).

---

## Feature 4: Cross-Session Temporal Lineage

### The problem

V4 graphs are session-scoped: the same `adUnitId` in two sessions produces
two independent nodes with no connection. Answering *"What changed on this
ad unit between runs?"* requires manual log comparison.

### The solution: concrete-entity lineage edges

Add self-edge relationships that track property changes on shared-key
entities across sessions.

### YAML spec: lineage relationship

```yaml
relationships:
  # ... existing V4 relationships ...

  - name: sup_YahooAdUnitEvolvedFrom
    description: "Tracks evolution of a YahooAdUnit across sessions."
    from_entity: sup_YahooAdUnit
    to_entity: sup_YahooAdUnit
    binding:
      source: "{{ env }}.sup_yahoo_ad_unit_lineage"
      from_columns: [adUnitId]
      to_columns: [adUnitId]
      from_session_column: from_session_id    # V5: session column override
      to_session_column: to_session_id        # V5: session column override
    properties:
      - name: from_session_id
        type: string
      - name: to_session_id
        type: string
      - name: event_time
        type: timestamp
      - name: changed_properties
        type: string
```

The `from_session_column` / `to_session_column` fields tell the DDL
compiler to map these columns to the node's `session_id` key for
SOURCE / DESTINATION endpoints, enabling cross-session traversal.

### Detecting lineage

```python
from bigquery_agent_analytics import detect_lineage_edges

lineage_edges = detect_lineage_edges(
    current_graph=graph_session_b,
    current_session_id="session-b",
    prior_graphs={"session-a": graph_session_a},
    lineage_entity_types=["sup_YahooAdUnit"],
    spec=spec,
)

for edge in lineage_edges:
    changed = next(p.value for p in edge.properties if p.name == "changed_properties")
    print(f"  Changed: {changed}")
    # e.g., "adUnitName,adUnitPosition"
```

### Generated Property Graph DDL

The lineage edge table uses session column overrides for cross-session
references:

```sql
`project.dataset.sup_yahoo_ad_unit_lineage` AS sup_YahooAdUnitEvolvedFrom
  KEY (adUnitId, from_session_id, to_session_id, session_id)
  SOURCE KEY (adUnitId, from_session_id)
    REFERENCES sup_YahooAdUnit (adUnitId, session_id)
  DESTINATION KEY (adUnitId, to_session_id)
    REFERENCES sup_YahooAdUnit (adUnitId, session_id)
  LABEL sup_YahooAdUnitEvolvedFrom
  PROPERTIES (
    from_session_id,
    to_session_id,
    event_time,
    changed_properties,
    extracted_at
  )
```

### GQL: cross-session lineage traversal

```sql
GRAPH `project.dataset.YMGO_Context_Graph_V3`
MATCH
  (prev:sup_YahooAdUnit)-[ev:sup_YahooAdUnitEvolvedFrom]->(cur:sup_YahooAdUnit)
WHERE cur.session_id = @session_id
RETURN
  prev.adUnitId AS prev_adUnitId,
  prev.adUnitName AS prev_adUnitName,
  prev.adUnitPosition AS prev_adUnitPosition,
  ev.from_session_id,
  ev.changed_properties,
  ev.event_time,
  cur.adUnitName AS cur_adUnitName,
  cur.adUnitPosition AS cur_adUnitPosition
ORDER BY ev.event_time DESC
LIMIT @result_limit
```

**Example output:**

| prev_adUnitName | prev_adUnitPosition | changed_properties | cur_adUnitName | cur_adUnitPosition |
|-----------------|--------------------|--------------------|----------------|-------------------|
| Yahoo Homepage | Premium Placement | adUnitName,adUnitPosition | Yahoo Homepage (Redesigned) | BTF |

---

## Demo Scenario: Yahoo ADCP Ad Decisioning

The same Yahoo ADCP domain from V4, extended with V5 capabilities:

```
                                    sup_YahooAdUnitEvolvedFrom
                                    (cross-session lineage)
                                           │
                                           ▼
DecisionPoint ──CandidateEdge──► YahooAdUnit ◄──ForCandidate── RejectionReason
                                    ▲
                                    │
                          sup_YahooAdUnitEvolvedFrom
                          (from prior session)
```

The V5 notebook (`ontology_graph_v5_demo.ipynb`) demonstrates:

1. **TTL import** of a YAMO sample ontology
2. **Mixed extraction** with a BKA decision extractor + AI.GENERATE fallback
3. **Batch materialization** with `idempotent=True` per table
4. **Temporal lineage** with a synthetic follow-up session showing property evolution
5. **GQL execution** — both forward traversal and lineage traversal against BigQuery

All data is written to a run-scoped scratch dataset that auto-expires,
ensuring deterministic and isolated demo execution.

---

## SDK Module Reference

| Module | Class / Function | Purpose |
|--------|-----------------|---------|
| `ttl_importer` | `ttl_import()` | Phase 1: OWL/Turtle to unresolved YAML |
| `ttl_importer` | `ttl_resolve()` | Phase 2: resolve placeholders to valid GraphSpec |
| `structured_extraction` | `StructuredExtractionResult` | Extractor output contract |
| `structured_extraction` | `extract_bka_decision_event()` | Example BKA extractor |
| `structured_extraction` | `run_structured_extractors()` | Run extractors on event list |
| `ontology_graph` | `OntologyGraphManager(extractors=...)` | Mixed extraction with registry |
| `ontology_graph` | `detect_lineage_edges()` | Cross-session entity diff |
| `ontology_materializer` | `OntologyMaterializer(write_mode=...)` | Batch or streaming writes |
| `ontology_materializer` | `materialize_with_status()` | Returns `MaterializationResult` |
| `ontology_orchestrator` | `compile_lineage_gql()` | Lineage GQL query generator |

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Two-phase import** | `FILL_IN` placeholders require human review; mixing unresolved artifacts with validated specs causes confusing errors. Distinct `*.import.yaml` format prevents accidental misuse. |
| **Structured-first extraction** | Known event types should not go through LLM round-trips. Dedup by `span_id` prevents double-extraction. Partial handling supports events with both typed fields and free-text reasoning. |
| **Batch load via staging** | Streaming inserts + `DELETE` fails on buffer-active tables. Staging tables + DML gives reliable idempotency and honest `TableStatus` reporting. |
| **Concrete self-edges for lineage** | A generic `EVOLVED_FROM` supertype cannot pass `GraphSpec` validation (requires concrete `from_entity`/`to_entity`). Per-entity lineage types are explicit and produce clean DDL. |
| **Session column overrides** | `from_session_column` / `to_session_column` on `BindingSpec` let the DDL compiler map edge columns to cross-session node keys without changing the global `(primary_key, session_id)` identity scheme. |
| **Schema filtering** | `_route_node` / `_route_edge` drop properties not declared in the spec before insert, preventing AI-hallucinated cross-entity fields from causing insert failures. |
