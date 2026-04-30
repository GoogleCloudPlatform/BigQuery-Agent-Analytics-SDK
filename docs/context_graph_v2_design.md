# BigQuery Agent Analytics
## Context Graph V2: System of Reasoning for Agentic Ads — Design Document

**Google ADK v1.21.0+ | Gemini 3 Flash | BigQuery Property Graphs & GQL**
**March 2026**

---

> **Implementation update (PR #99, 2026-04).** The BizNode extraction
> path described in this V2 design no longer passes `output_schema =>`
> to `AI.GENERATE` — current BigQuery rejects JSON-Schema strings.
> The SDK now asks the model in-prompt for a JSON array and parses
> the result with `JSON_EXTRACT_ARRAY`. The design intent (typed
> entity extraction; one row per `(span_id, node_type, node_value)`)
> is unchanged. The current SQL is `_EXTRACT_BIZ_NODES_QUERY` in
> `src/bigquery_agent_analytics/context_graph.py`. The same change
> is documented at the top of `context_graph_v3_design.md`, which
> extends this V2 design.

---

## 1. Overview

This document describes the design and implementation of Context Graph V2, a **System of Reasoning** layer for agentic advertising built on the BigQuery Agent Analytics SDK. The system constructs a BigQuery Property Graph that cross-links technical execution traces (from ADK) with business-domain entities (extracted via AI.GENERATE), enabling causal reasoning, GQL-based trace reconstruction, and world-change detection for long-running agent-to-agent (A2A) tasks.

The demo provides a production-ready interactive prototype showcasing how organizations can build observability, debugging, and HITL safety layers on top of their multi-agent advertising infrastructure.

### 1.1 Key Capabilities

- **4-Pillar Property Graph** — TechNode (ADK spans) + BizNode (AI.GENERATE extracted) + Caused edges (span lineage) + Evaluated cross-links (artifact lineage)
- **AI.GENERATE for typed extraction** — Structured entity extraction by asking `AI.GENERATE` in-prompt for JSON output; the SQL strips markdown fences and `JSON_EXTRACT_ARRAY` parses each row. (Historically passed via `output_schema =>`; see the Implementation update at the top of this doc.)
- **GQL trace reconstruction** — Native Graph Query Language replaces recursive CTEs for quantified-path traversal
- **World-change detection** — Pre-HITL safety check with fail-closed semantics (query/callback errors → `check_failed=True, is_safe_to_approve=False`)
- **Artifact lineage** — `artifact_uri` on BizNode and Evaluated edge for GCS object tracking
- **MERGE with DELETE** — Stale BizNode cleanup via `WHEN NOT MATCHED BY SOURCE ... THEN DELETE`
- **Parameterized GQL** — `@biz_entity`, `@session_id` prevent SQL injection in graph queries

### 1.2 Technology Stack

| Component | Technology |
|-----------|-----------|
| Agent Framework | Google ADK v1.21.0+ |
| AI Model | Gemini 3 Flash (`gemini-3-flash-preview`) |
| Data Warehouse | Google BigQuery |
| Graph Engine | BigQuery Property Graphs (`CREATE PROPERTY GRAPH` DDL) |
| Query Language | GQL (Graph Query Language) with quantified-path patterns |
| AI Functions | `AI.GENERATE` (prompt-shaped JSON contract; see Implementation update) |
| Tracing | OpenTelemetry (`trace_id`, `span_id`, `parent_span_id`) |
| Streaming | BigQuery Storage Write API |
| SDK | `bigquery-agent-analytics` Python package |
| Frontend | React 18 with inline SVG graph visualization |

### 1.3 Demo Scenario: ADCP Multi-Agent Media Buying

The demo simulates the **Ad Context Protocol (ADCP)** — a multi-agent media buying workflow where:

1. A **Buyer Agent** submits a campaign brief (brand, budget, targeting)
2. A **Media Planner Agent** queries inventory, matches audiences, and allocates budget
3. A **Root Agent** pauses for HITL approval before provisioning
4. **World-change detection** verifies entities haven't drifted during the approval window

Three sessions demonstrate the three possible outcomes:

| Session | Client | Budget | Outcome | World-Change Status |
|---------|--------|--------|---------|-------------------|
| `sess-elf-cosmetics` | ELF Cosmetics | $50,000 | Approved | Safe (0 stale / 7 checked) |
| `sess-nike-summer` | Nike | $200,000 | Drift Detected | 2 stale entities (inventory depleted + price changed) |
| `sess-tesla-q1` | Tesla | $100,000 | Check Failed | Query error → fail-closed |

---

## 2. Data Model

### 2.1 Core Schema: `agent_events` (ADK Plugin)

The foundation is the ADK `agent_events` table, written by the BigQuery Agent Analytics Plugin:

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | TIMESTAMP | Event timestamp (UTC, microsecond precision) |
| `event_type` | STRING | `LLM_REQUEST`, `TOOL_COMPLETED`, `HITL_CONFIRMATION_REQUEST`, etc. |
| `agent` | STRING | Agent name (`root_agent`, `media_planner`) |
| `session_id` | STRING | Conversation session identifier |
| `invocation_id` | STRING | Single turn within a session |
| `user_id` | STRING | User identifier |
| `trace_id` | STRING | OpenTelemetry trace ID (32-char hex) |
| `span_id` | STRING | OpenTelemetry span ID (16-char hex) |
| `parent_span_id` | STRING | Parent span for causal chain reconstruction |
| `content` | JSON | Event payload (user message, tool result, etc.) |
| `content_parts` | RECORD (REPEATED) | Multimodal segments with `artifact_uri` for GCS refs |
| `latency_ms` | JSON | Performance metrics |
| `status` | STRING | `OK` or `ERROR` |
| `error_message` | STRING | Exception message |

**Partitioning:** `PARTITION BY DATE(timestamp)`
**Clustering:** `CLUSTER BY event_type, agent, user_id`

### 2.2 BizNode Table: `context_graph_biz_nodes`

Business entities extracted from agent traces via AI.GENERATE:

| Column | Type | Description |
|--------|------|-------------|
| `biz_node_id` | STRING | Composite key: `span_id:node_type:node_value` |
| `span_id` | STRING | Source span from `agent_events` |
| `session_id` | STRING | Session that produced this entity |
| `node_type` | STRING | Entity category: `Product`, `Targeting`, `Campaign`, `Budget` |
| `node_value` | STRING | Entity value: `"Instagram Reels"`, `"Gen Z Female 18-24"`, `"$50,000"` |
| `confidence` | FLOAT64 | Extraction confidence (0.0–1.0) |
| `artifact_uri` | STRING | GCS URI for persisted artifacts (e.g., campaign config JSON) |
| `created_at` | TIMESTAMP | Extraction timestamp |

**Key design:** Composite `biz_node_id = span_id:node_type:node_value` prevents key collisions when the same span produces multiple entities of different types.

### 2.3 Cross-Links Table: `context_graph_cross_links`

Edges connecting BizNodes to their source TechNodes:

| Column | Type | Description |
|--------|------|-------------|
| `link_id` | STRING | Derived from `biz_node_id` |
| `span_id` | STRING | Source TechNode span |
| `biz_node_id` | STRING | Destination BizNode |
| `link_type` | STRING | Relationship type (e.g., `"extracted_from"`) |
| `artifact_uri` | STRING | Artifact reference on the edge |
| `created_at` | TIMESTAMP | Link creation time |

### 2.4 Property Graph Model

The Context Graph uses a **4-pillar architecture** implemented as a BigQuery Property Graph:

```
┌────────────────────────┐     Caused        ┌────────────────────────┐
│       TechNode         │ ────────────────► │       TechNode         │
│  (agent_events)        │                   │  (agent_events)        │
│  KEY: span_id          │                   │  KEY: span_id          │
│  Props: event_type,    │                   │                        │
│    agent, timestamp,   │                   │                        │
│    content, status     │                   │                        │
└────────────┬───────────┘                   └────────────────────────┘
             │
             │ Evaluated (cross-link)
             │
             ▼
┌────────────────────────┐
│       BizNode          │
│  (biz_nodes table)     │
│  KEY: biz_node_id      │
│  Props: node_type,     │
│    node_value,         │
│    confidence,         │
│    artifact_uri        │
└────────────────────────┘
```

**Pillar 1 — TechNode:** The `agent_events` table. Each row is a graph vertex keyed by `span_id`.

**Pillar 2 — BizNode:** The `biz_nodes` table. Business entities extracted via AI.GENERATE, keyed by composite `biz_node_id`.

**Pillar 3 — Caused edges:** Implicit parent→child span lineage. The `agent_events` table doubles as the edge table using `parent_span_id → span_id`.

**Pillar 4 — Evaluated edges:** Explicit cross-links from `cross_links` table, connecting TechNode spans to their extracted BizNodes. Carries `artifact_uri` and `link_type` as edge properties.

---

## 3. BigQuery AI Functions & Graph Queries

### 3.1 AI.GENERATE with output_schema

The key innovation in V2 is using `output_schema` to force structured JSON output from AI.GENERATE, eliminating post-hoc parsing failures:

```sql
AI.GENERATE(
  CONCAT('Extract business entities. Entity types: Product, Targeting, Campaign, Budget.',
         '\nPayload:\n', TO_JSON_STRING(base.content)),
  endpoint => 'https://aiplatform.googleapis.com/v1/projects/PROJECT/
    locations/global/publishers/google/models/gemini-3-flash-preview',
  output_schema => '{"type":"ARRAY","items":{"type":"OBJECT","properties":{
    "entity_type":{"type":"STRING"},
    "entity_value":{"type":"STRING"},
    "confidence":{"type":"NUMBER"}}}}'
).result
```

The `output_schema` parameter guarantees the response conforms to the specified JSON schema, producing an array of `{entity_type, entity_value, confidence}` objects.

### 3.2 MERGE with DELETE Semantics

BizNode extraction uses a 3-way MERGE for idempotent upsert with stale cleanup:

```sql
MERGE `project.dataset.extracted_biz_nodes` AS target
USING (...) AS source
ON target.biz_node_id = source.biz_node_id
WHEN MATCHED THEN UPDATE SET confidence = source.confidence
WHEN NOT MATCHED BY TARGET THEN INSERT (...)
WHEN NOT MATCHED BY SOURCE
  AND target.session_id IN UNNEST(@session_ids) THEN DELETE
```

The `WHEN NOT MATCHED BY SOURCE ... DELETE` clause removes stale BizNodes that no longer appear in re-extraction results for the given sessions.

### 3.3 Property Graph DDL

```sql
CREATE OR REPLACE PROPERTY GRAPH `project.dataset.agent_context_graph`
  NODE TABLES (
    `project.dataset.agent_events` AS TechNode
      KEY (span_id) LABEL TechNode
      PROPERTIES (event_type, agent, timestamp, session_id, invocation_id,
                  content, latency_ms, status, error_message),

    `project.dataset.extracted_biz_nodes` AS BizNode
      KEY (biz_node_id) LABEL BizNode
      PROPERTIES (node_type, node_value, confidence, session_id,
                  span_id, artifact_uri)
  )
  EDGE TABLES (
    `project.dataset.agent_events` AS Caused
      KEY (span_id)
      SOURCE KEY (parent_span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (span_id) REFERENCES TechNode (span_id)
      LABEL Caused,

    `project.dataset.context_cross_links` AS Evaluated
      KEY (link_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (biz_node_id) REFERENCES BizNode (biz_node_id)
      LABEL Evaluated
      PROPERTIES (artifact_uri, link_type, created_at)
  )
```

### 3.4 GQL Reasoning Chain

Trace why a business entity was selected using quantified-path traversal:

```sql
GRAPH `project.dataset.agent_context_graph`
MATCH
  (decision:TechNode)-[c:Caused]->{1,20}(step:TechNode)
    -[e:Evaluated]->(biz:BizNode)
WHERE decision.event_type = @decision_event_type
  AND biz.node_value = @biz_entity
RETURN
  decision.span_id AS decision_span_id,
  step.span_id AS reasoning_span_id,
  step.event_type AS step_type,
  step.agent AS step_agent,
  biz.node_type AS entity_type,
  biz.node_value AS entity_value,
  biz.confidence AS entity_confidence,
  biz.artifact_uri AS artifact_uri
ORDER BY step.timestamp ASC
```

The pattern `(decision)-[Caused]->{1,20}(step)-[Evaluated]->(biz)` traverses up to 20 hops of causal lineage from a decision event to the business entities it depends on. This replaces recursive CTEs with a declarative graph pattern.

### 3.5 GQL Trace Reconstruction

Native graph traversal replaces recursive CTEs for reconstructing session traces:

```sql
GRAPH `project.dataset.agent_context_graph`
MATCH
  (parent:TechNode)-[c:Caused]->(child:TechNode)
WHERE parent.session_id = @session_id
   OR child.session_id = @session_id
RETURN
  parent.span_id AS parent_span_id,
  parent.event_type AS parent_event_type,
  parent.agent AS parent_agent,
  child.span_id AS child_span_id,
  child.event_type AS child_event_type,
  child.agent AS child_agent,
  child.timestamp AS child_timestamp
ORDER BY child.timestamp ASC
```

### 3.6 World-Change Detection Query

Joins BizNodes with agent events to get `evaluated_at` timestamps for freshness checking:

```sql
SELECT
  b.node_type,
  b.node_value,
  b.confidence,
  b.span_id,
  b.artifact_uri,
  e.timestamp AS evaluated_at
FROM `project.dataset.extracted_biz_nodes` b
JOIN `project.dataset.agent_events` e
  ON b.span_id = e.span_id
WHERE b.session_id = @session_id
ORDER BY e.timestamp ASC
```

---

## 4. World-Change Detection (HITL Safety)

### 4.1 Problem Statement

In long-running A2A workflows (e.g., a media buy that requires human approval), the real world can change between when the agent evaluated entities and when the human approves the decision. Ad inventory can sell out, prices can change, audiences can shift.

### 4.2 Solution: Fail-Closed Detection

The `detect_world_changes()` method implements a pre-HITL safety check:

1. Query all BizNodes for the session with their `evaluated_at` timestamps
2. For each entity, call a user-supplied `current_state_fn` to check current state
3. Compare original vs. current state to detect drift
4. Return a `WorldChangeReport` with alerts and safety verdict

**Fail-closed semantics** are critical:

| Scenario | `is_safe_to_approve` | `check_failed` | Behavior |
|----------|---------------------|----------------|----------|
| All entities current | `True` | `False` | Safe to approve |
| Drift detected | `False` | `False` | Block approval, show alerts |
| BigQuery query fails | `False` | `True` | Block approval (fail-closed) |
| `current_state_fn` throws | `False` | `True` | Block approval (fail-closed) |

The fail-closed design ensures that operational failures (API outages, query timeouts) can never be misreported as "safe to approve."

### 4.3 Drift Types

| Drift Type | Example | Severity |
|-----------|---------|----------|
| `inventory_depleted` | Yahoo Homepage Takeover → sold out | 0.95 (critical) |
| `price_changed` | Strava Routes $40K → $52K (+30%) | 0.72 (moderate) |
| `audience_shifted` | Segment reach dropped below threshold | 0.60 (low) |
| `campaign_paused` | External campaign suspension | 0.90 (critical) |

### 4.4 Python SDK Usage

```python
from bigquery_agent_analytics import ContextGraphManager, ContextGraphConfig

cgm = ContextGraphManager(
    project_id="my-project",
    dataset_id="agent_analytics",
    config=ContextGraphConfig(endpoint="gemini-3-flash-preview"),
)

def check_inventory(node):
    # Call real-time inventory API
    return {"available": True, "current_value": "in stock"}

report = cgm.detect_world_changes(
    session_id="sess-elf-cosmetics",
    current_state_fn=check_inventory,
)

print(report.summary())
# World Change Report - Session: sess-elf-cosmetics
#   Entities checked : 7
#   Stale entities   : 0
#   Safe to approve  : True

# Fail-closed on errors:
# report.check_failed   # True if query or callback failed
# report.is_safe_to_approve  # Always False when check_failed=True
```

---

## 5. Production Query Patterns

The demo includes 5 production-ready BigQuery queries covering the full Context Graph workflow.

### 5.1 Extract Business Entities (AI.GENERATE + output_schema)
Uses `AI.GENERATE` with `output_schema` to extract typed business entities (Product, Targeting, Campaign, Budget) from agent trace payloads. MERGE with 3-way logic handles upsert and stale cleanup in a single statement.

### 5.2 Property Graph DDL (4-Pillar Architecture)
`CREATE OR REPLACE PROPERTY GRAPH` DDL defining the 4-pillar graph: TechNode vertices (from `agent_events`), BizNode vertices (from `biz_nodes`), Caused edges (span lineage), and Evaluated edges (cross-links with `artifact_uri`).

### 5.3 GQL Reasoning Chain ("Why was X selected?")
Quantified-path GQL query that traverses from a HITL decision event through up to 20 hops of causal lineage to the business entities that influenced the decision. Returns the full reasoning chain with confidence scores and artifact URIs.

### 5.4 GQL Trace Reconstruction (replaces recursive CTEs)
Native GQL traversal of the Caused edge type to reconstruct session traces. Returns parent-child span pairs ordered by timestamp. The SDK merges GQL results with isolated events (spans without edges) for completeness.

### 5.5 World-Change Detection (Fail-Closed)
Joins BizNodes with agent events to retrieve `evaluated_at` timestamps. The Python SDK layer applies the `current_state_fn` callback and enforces fail-closed semantics.

---

## 6. Interactive Demo Features

### 6.1 Property Graph Visualization
Interactive SVG-based graph rendering of the 4-pillar architecture. TechNodes (circles) are color-coded by event type: blue (user input), green (agent lifecycle), red (tool calls), yellow (HITL events). BizNodes (rectangles) are color-coded by entity type: orange (Product), cyan (Targeting), green (Budget), purple (Campaign). Caused edges (solid grey) show span lineage; Evaluated edges (dashed blue) show cross-links. Clicking nodes reveals detailed properties.

### 6.2 World-Change Detection Panel
Dedicated panel showing the pre-HITL safety check results for the selected session. Displays the overall verdict (Safe/Drift Detected/Check Failed), entity counts, and individual drift alerts with severity scores. The "Check Failed" state includes an explanation of fail-closed semantics.

### 6.3 SQL / GQL Query Explorer
Interactive query browser with 5 production-ready queries. Each query includes full SQL/GQL, category badges (Extraction, Graph, Safety), and feature tags (AI.GENERATE, output_schema, Property Graph, GQL). Syntax-highlighted with dark theme and copy-to-clipboard support.

### 6.4 Python SDK Panel
Live-updating Python code showing the full SDK workflow for the selected session:
1. Initialize `ContextGraphManager` with `ContextGraphConfig`
2. Extract BizNodes via `AI.GENERATE + output_schema`
3. Create cross-links
4. Create Property Graph
5. GQL trace reconstruction via `client.get_session_trace_gql()`
6. Explain decisions via `cgm.explain_decision()`
7. World-change detection via `cgm.detect_world_changes()`

### 6.5 Session Selector
Switch between three ADCP sessions demonstrating different outcomes: approved (ELF Cosmetics), drift detected (Nike), and check failed (Tesla). Each session shows different event counts, BizNode counts, and world-change statuses.

---

## 7. Architecture

### 7.1 Data Flow

```
┌──────────────────────────────────────────────────────────┐
│                   Agent Runtime (ADK v1.21.0+)           │
│                                                          │
│  Buyer Agent ─── Media Planner ─── Root Agent (HITL)     │
│                        │                                 │
│              BigQueryAgentAnalyticsPlugin                 │
│                        │                                 │
│          BQ Storage Write API (streaming)                 │
└────────────────────────┬─────────────────────────────────┘
                         │ Writes events
                         ▼
┌──────────────────────────────────────────────────────────┐
│              BigQuery (agent_events)                      │
│  Partitioned by DATE(timestamp)                          │
│  Clustered by event_type, agent, user_id                 │
└────────────────────────┬─────────────────────────────────┘
                         │ Reads events
                         ▼
┌──────────────────────────────────────────────────────────┐
│      BigQuery Agent Analytics SDK (context_graph.py)     │
│                                                          │
│  1. AI.GENERATE + output_schema → biz_nodes table        │
│  2. Cross-links → cross_links table                      │
│  3. CREATE PROPERTY GRAPH (DDL)                          │
│  4. GQL queries for reasoning + trace reconstruction     │
│  5. World-change detection (fail-closed)                 │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│         BigQuery Property Graph                          │
│                                                          │
│  TechNode ──Caused──► TechNode                           │
│     │                                                    │
│     └──Evaluated──► BizNode (+ artifact_uri)             │
└──────────────────────────────────────────────────────────┘
```

### 7.2 SDK Module: `context_graph.py`

The Context Graph is implemented as a standalone module (`~1300 lines`) in the BigQuery Agent Analytics SDK:

| Class | Responsibility |
|-------|---------------|
| `ContextGraphManager` | Main entry point: extraction, cross-links, graph creation, GQL queries, world-change detection |
| `ContextGraphConfig` | Configuration: endpoint, table names, graph name, extraction prompt |
| `BizNode` | Dataclass representing an extracted business entity |
| `WorldChangeReport` | Pydantic model: safety verdict, alerts, fail-closed flag |
| `WorldChangeAlert` | Pydantic model: individual drift alert with severity |

**Key design decisions:**

| Decision | Rationale |
|----------|-----------|
| Standalone module | No internal imports from other SDK modules; independently testable |
| `output_schema` in AI.GENERATE | Eliminates JSON parsing failures from free-form LLM output |
| Composite `biz_node_id` | `span_id:node_type:node_value` prevents collisions from same-span multi-entity extraction |
| MERGE with 3-way logic | Single atomic statement handles insert, update, and stale cleanup |
| Fail-closed world-change | Query errors and callback errors both produce `check_failed=True, is_safe_to_approve=False` |
| Legacy endpoint rejection | `project.dataset.model` refs raise `ValueError` instead of silently producing bad Vertex AI URLs |
| GQL + flat trace merge | GQL returns only edge pairs; SDK merges isolated events from flat SQL for completeness |
| Timezone-safe sorting | `datetime(1970,1,1,tzinfo=timezone.utc)` fallback instead of naive `datetime.min` |

### 7.3 Client Integration

The `Client` class exposes GQL trace reconstruction:

```python
# client.py
def get_session_trace_gql(self, session_id, config=None) -> Trace:
    """Reconstructs a session trace using GQL graph traversal.

    1. Runs GQL query via ContextGraphManager.reconstruct_trace_gql()
    2. Fetches flat trace via get_session_trace() for isolated events
    3. Backfills parent_span_id when spans arrive out of order
    4. Merges isolated spans not covered by GQL
    5. Sorts by timezone-aware timestamps
    6. Falls back to flat SQL when GQL returns no edges
    """
```

### 7.4 Visualization Layer

- React 18 frontend with Google Cloud design system styling
- Inline SVG graph with positioned TechNodes (circles) and BizNodes (rectangles)
- Caused edges (solid lines) and Evaluated cross-links (dashed lines)
- Interactive node selection with property panel
- Dark-themed SQL/GQL code viewer with copy support
- Session switcher for comparing outcomes across ADCP sessions

---

## 8. Testing

The Context Graph module has 50 dedicated tests covering:

| Test Category | Count | Examples |
|--------------|-------|---------|
| BizNode extraction | 5 | AI.GENERATE path, client-side path, output_schema in SQL |
| BizNode storage & retrieval | 5 | Store with artifact_uri, read back, session filtering |
| Cross-links | 3 | Create, composite link_id, delete error handling |
| Property Graph | 3 | DDL generation, graph creation, config override |
| GQL queries | 4 | Reasoning chain, causal chain, trace reconstruction |
| World-change detection | 5 | Safe, drift detected, fn exception (fail-closed), query failure (fail-closed) |
| End-to-end pipeline | 2 | `build_context_graph()`, partial failure handling |
| Client integration | 4 | `get_session_trace_gql()`, parent backfill, chronological ordering, isolated event merge |
| Edge cases | 5 | Legacy endpoint rejection, evaluated_at passthrough, MERGE delete, empty sessions |

Full test suite: **562 tests** (50 context graph + 512 existing SDK tests), all passing.

---

## 9. Deployment

The demo is deployed as a single self-contained HTML file with embedded React/Babel. No build step or server is required.

### 9.1 Live Demo
[https://context-graph-v2-demo.vercel.app](https://context-graph-v2-demo.vercel.app)

### 9.2 Source Code
- Demo: [`examples/context_graph_v2_demo.html`](../examples/context_graph_v2_demo.html)
- Notebook: [`examples/context_graph_adcp_demo.ipynb`](../examples/context_graph_adcp_demo.ipynb)
- SDK module: [`src/bigquery_agent_analytics/context_graph.py`](../src/bigquery_agent_analytics/context_graph.py)
- Tests: [`tests/test_context_graph.py`](../tests/test_context_graph.py)

### 9.3 Production Deployment Notes

For production use:
1. Replace simulated session data with real BigQuery connections
2. Configure Vertex AI connection for AI.GENERATE (`us.vertex_ai_connection`)
3. Create `agent_events` table using the ADK plugin schema
4. Set up ADK BigQuery Agent Analytics Plugin for event streaming
5. Create the BizNode and cross-links tables via `ContextGraphManager`
6. Configure `current_state_fn` callbacks pointing to real inventory/pricing APIs
7. Integrate `detect_world_changes()` into HITL approval workflows

---

## 10. Evolution from V1

| Aspect | V1 (Context Graphs & Decision Traces) | V2 (System of Reasoning) |
|--------|---------------------------------------|--------------------------|
| Graph Model | Simulated property graph via recursive CTEs | Native BigQuery Property Graph with `CREATE PROPERTY GRAPH` DDL |
| Entity Extraction | Manual event classification | AI.GENERATE with `output_schema` for structured extraction |
| Traversal | Recursive CTEs for decision traces | GQL with quantified-path patterns (`->{1,20}`) |
| Business Layer | Single vertex types (User, Session, LLM, Tool) | Separate TechNode + BizNode with cross-links |
| Safety | None | World-change detection with fail-closed semantics |
| Artifacts | Not tracked | `artifact_uri` on BizNode + Evaluated edge |
| Stale Data | Not handled | MERGE with `WHEN NOT MATCHED BY SOURCE ... DELETE` |
| SDK Integration | Standalone demo | Full `context_graph.py` module with 50 tests |
| Query Injection | Raw SQL interpolation | Parameterized `@biz_entity`, `@session_id` |
