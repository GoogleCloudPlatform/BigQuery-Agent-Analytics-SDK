# BigQuery Agent Analytics
## Context Graph V3: Decision Semantics for Agentic Ads — Design Document

**Google ADK v1.21.0+ | Gemini 3 Flash | BigQuery Property Graphs & GQL**
**March 2026**

---

## 1. Overview

This document describes the design and implementation of Context Graph V3, which extends the V2 **System of Reasoning** layer with **Decision Semantics** — a structured model for agent decision points, candidate scoring, and rejection rationale. Built on the BigQuery Agent Analytics SDK, V3 constructs a 6-pillar BigQuery Property Graph that cross-links technical execution traces (from ADK) with business-domain entities (extracted via AI.GENERATE), decision points, and candidate options. This enables causal reasoning, GQL-based trace reconstruction, world-change detection, and EU-compliant audit trails for long-running agent-to-agent (A2A) tasks.

The demo provides a production-ready interactive prototype showcasing how organizations can build observability, debugging, HITL safety layers, and regulatory audit capabilities on top of their multi-agent advertising infrastructure.

### 1.1 Key Capabilities

- **6-Pillar Property Graph** — TechNode (ADK spans) + BizNode (AI.GENERATE extracted) + DecisionPoint (decision nodes) + CandidateNode (candidate options) + Caused edges (span lineage) + Evaluated cross-links (artifact lineage) + MadeDecision edges (span→decision) + CandidateEdge edges (decision→candidate)
- **Decision Semantics** — Model agent decisions with candidates, scores, selection status (SELECTED/DROPPED), and rejection rationale for EU audit compliance
- **AI.GENERATE with output_schema** — Strict structured entity and decision extraction using `output_schema` parameter for guaranteed JSON schema conformance
- **GQL trace reconstruction** — Native Graph Query Language replaces recursive CTEs for quantified-path traversal
- **EU audit trail** — Forward GQL traversal from TechNode through DecisionPoint to CandidateNode for regulatory compliance
- **World-change detection** — Pre-HITL safety check with fail-closed semantics (query/callback errors → `check_failed=True, is_safe_to_approve=False`)
- **Artifact lineage** — `artifact_uri` on BizNode and Evaluated edge for GCS object tracking
- **MERGE with DELETE** — Stale BizNode cleanup via `WHEN NOT MATCHED BY SOURCE ... THEN DELETE`
- **Parameterized GQL** — `@biz_entity`, `@session_id`, `@decision_type` prevent SQL injection in graph queries

### 1.2 Technology Stack

| Component | Technology |
|-----------|-----------|
| Agent Framework | Google ADK v1.21.0+ |
| AI Model | Gemini 3 Flash (`gemini-3-flash-preview`) |
| Data Warehouse | Google BigQuery |
| Graph Engine | BigQuery Property Graphs (`CREATE PROPERTY GRAPH` DDL) |
| Query Language | GQL (Graph Query Language) with quantified-path patterns |
| AI Functions | `AI.GENERATE` with `output_schema` for structured extraction |
| Tracing | OpenTelemetry (`trace_id`, `span_id`, `parent_span_id`) |
| Streaming | BigQuery Storage Write API |
| SDK | `bigquery-agent-analytics` Python package |
| Frontend | React 18 with inline SVG graph visualization |

### 1.3 Demo Scenario: ADCP Multi-Agent Media Buying with Decision Semantics

The demo simulates the **Ad Context Protocol (ADCP)** — a multi-agent media buying workflow with **Decision Semantics** where:

1. A **Buyer Agent** submits a campaign brief (brand, budget, targeting)
2. A **Media Planner Agent** queries inventory, matches audiences, allocates budget, and evaluates candidates
3. A **Root Agent** pauses for HITL approval before provisioning
4. **Decision Semantics** records which candidates were selected vs. dropped, with scores and rejection rationale
5. **World-change detection** verifies entities haven't drifted during the approval window

Two sessions demonstrate decision semantics in action:

| Session | Client | Decision Type | Candidates | Selected | Dropped | Rejection Reason |
|---------|--------|---------------|------------|----------|---------|-----------------|
| `sess-nike-summer` | Nike | `audience_selection` | 3 | Athletes 18-35 (0.92) | Fitness Enthusiasts (0.71), Running Community (0.65) | Budget constraints |
| `sess-elf-cosmetics` | ELF Cosmetics | `placement_selection` | 4 | Instagram Reels (0.95), TikTok TopView (0.93) | LinkedIn Sponsored (0.22), Yahoo Homepage (0.31) | Audience mismatch (Gen Z affinity below 0.70) |

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
| `confidence` | FLOAT64 | Extraction confidence (0.0-1.0) |
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

### 2.4 Decision Points Table: `decision_points` (NEW in V3)

Decision points identified in agent traces where candidates were evaluated:

| Column | Type | Description |
|--------|------|-------------|
| `decision_id` | STRING | Unique identifier for this decision point |
| `session_id` | STRING | Session containing this decision |
| `span_id` | STRING | The span where the decision was made |
| `decision_type` | STRING | Category: `audience_selection`, `placement_selection`, `budget_allocation` |
| `description` | STRING | Human-readable description of the decision |

### 2.5 Candidates Table: `candidates` (NEW in V3)

Candidate options evaluated at each decision point:

| Column | Type | Description |
|--------|------|-------------|
| `candidate_id` | STRING | Unique identifier for this candidate |
| `decision_id` | STRING | The decision point this candidate belongs to |
| `session_id` | STRING | Session containing this candidate |
| `name` | STRING | Candidate name/label |
| `score` | FLOAT64 | Evaluation score (0.0-1.0) |
| `status` | STRING | `SELECTED` or `DROPPED` |
| `rejection_rationale` | STRING | Why the candidate was dropped (required for DROPPED, supports EU audit) |

### 2.6 Made Decision Edges Table: `made_decision_edges` (NEW in V3)

Edges connecting TechNodes to the DecisionPoints they produced:

| Column | Type | Description |
|--------|------|-------------|
| `edge_id` | STRING | Composite: `span_id:MADE_DECISION:decision_id` |
| `span_id` | STRING | Source TechNode span |
| `decision_id` | STRING | Destination DecisionPoint |
| `created_at` | TIMESTAMP | Edge creation time |

### 2.7 Candidate Edges Table: `candidate_edges` (NEW in V3)

Edges connecting DecisionPoints to their CandidateNodes:

| Column | Type | Description |
|--------|------|-------------|
| `edge_id` | STRING | Composite: `decision_id:status:candidate_id` |
| `decision_id` | STRING | Source DecisionPoint |
| `candidate_id` | STRING | Destination CandidateNode |
| `edge_type` | STRING | `SELECTED_CANDIDATE` or `DROPPED_CANDIDATE` |
| `rejection_rationale` | STRING | Rationale propagated to the edge |
| `created_at` | TIMESTAMP | Edge creation time |

**Two-table edge model:** BigQuery Property Graph DDL requires separate edge tables when source/destination types differ. `made_decision_edges` connects `TechNode → DecisionPoint`, while `candidate_edges` connects `DecisionPoint → CandidateNode`. These cannot be combined into a single edge table because the SOURCE and DESTINATION key references must be homogeneous within a single EDGE TABLE declaration.

### 2.8 Property Graph Model

The Context Graph uses a **6-pillar architecture** implemented as a BigQuery Property Graph:

```
┌────────────────────────┐     Caused        ┌────────────────────────┐
│       TechNode         │ ────────────────► │       TechNode         │
│  (agent_events)        │                   │  (agent_events)        │
│  KEY: span_id          │                   │  KEY: span_id          │
│  Props: event_type,    │                   │                        │
│    agent, timestamp,   │                   │                        │
│    content, status     │                   │                        │
└────────┬───────┬───────┘                   └────────────────────────┘
         │       │
         │       │ MadeDecision (NEW)
         │       │
         │       ▼
         │  ┌────────────────────────┐
         │  │    DecisionPoint       │
         │  │  (decision_points)     │
         │  │  KEY: decision_id      │
         │  │  Props: decision_type, │
         │  │    description,        │
         │  │    session_id, span_id │
         │  └────────┬───────────────┘
         │           │
         │           │ CandidateEdge (NEW)
         │           │
         │           ▼
         │  ┌────────────────────────┐
         │  │    CandidateNode       │
         │  │  (candidates)          │
         │  │  KEY: candidate_id     │
         │  │  Props: name, score,   │
         │  │    status,             │
         │  │    rejection_rationale │
         │  └────────────────────────┘
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

**Pillar 3 — DecisionPoint (NEW):** The `decision_points` table. Decision moments extracted via AI.GENERATE, keyed by `decision_id`.

**Pillar 4 — CandidateNode (NEW):** The `candidates` table. Options evaluated at each decision point, keyed by `candidate_id`.

**Pillar 5 — Caused edges + Evaluated edges + MadeDecision edges (NEW):** Causal span lineage, cross-links, and span-to-decision linkage.

**Pillar 6 — CandidateEdge edges (NEW):** Decision-to-candidate linkage with `edge_type` (`SELECTED_CANDIDATE` / `DROPPED_CANDIDATE`) and `rejection_rationale` as edge properties.

---

## 3. BigQuery AI Functions & Graph Queries

### 3.1 AI.GENERATE with output_schema (BizNode Extraction)

Structured entity extraction using `output_schema` to force JSON conformance:

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

### 3.2 AI.GENERATE with output_schema (Decision Extraction — NEW in V3)

Decision point extraction uses a dedicated `output_schema` for structured decision data including candidates with scores, selection status, and rejection rationale:

```sql
SELECT
  base.span_id,
  base.session_id,
  REGEXP_REPLACE(
    REGEXP_REPLACE(
      AI.GENERATE(
        CONCAT(
          'Identify decision points in this agent payload. ',
          'A decision point is where the agent evaluated multiple ',
          'candidates and selected or rejected them. ',
          'For each decision, return the decision_type, description, ',
          'and all candidates with name, score (0-1), status ',
          '(SELECTED or DROPPED), and rejection_rationale ',
          '(null if selected, required reason if dropped).',
          '\n\nPayload:\n',
          COALESCE(
            JSON_EXTRACT_SCALAR(base.content, '$.text_summary'),
            JSON_EXTRACT_SCALAR(base.content, '$.response'),
            JSON_EXTRACT_SCALAR(base.content, '$.text'),
            TO_JSON_STRING(base.content)
          )
        ),
        endpoint => '{endpoint}',
        output_schema => '{output_schema}'
      ).result,
      r'^```(?:json)?\s*', ''),
    r'\s*```$', '')
  AS decisions_json
FROM `project.dataset.agent_events` AS base
WHERE base.session_id IN UNNEST(@session_ids)
  AND base.event_type IN (
    'LLM_RESPONSE',
    'TOOL_COMPLETED',
    'AGENT_COMPLETED',
    'HITL_CONFIRMATION_REQUEST_COMPLETED'
  )
  AND base.content IS NOT NULL
ORDER BY base.timestamp ASC
```

The `_DECISION_POINT_OUTPUT_SCHEMA` guarantees the response conforms to an array of decision objects, each containing candidates:

```json
{
  "type": "ARRAY",
  "items": {
    "type": "OBJECT",
    "properties": {
      "decision_type": {"type": "STRING"},
      "description": {"type": "STRING"},
      "candidates": {
        "type": "ARRAY",
        "items": {
          "type": "OBJECT",
          "properties": {
            "name": {"type": "STRING"},
            "score": {"type": "NUMBER"},
            "status": {"type": "STRING"},
            "rejection_rationale": {"type": "STRING"}
          }
        }
      }
    }
  }
}
```

### 3.3 MERGE with DELETE Semantics

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

### 3.4 Property Graph DDL (6-Pillar)

```sql
CREATE OR REPLACE PROPERTY GRAPH `project.dataset.agent_context_graph`
  NODE TABLES (
    -- Technical execution nodes (spans from ADK plugin)
    `project.dataset.agent_events` AS TechNode
      KEY (span_id)
      LABEL TechNode
      PROPERTIES (
        event_type, agent, timestamp, session_id, invocation_id,
        content, latency_ms, status, error_message
      ),
    -- Business domain nodes (extracted entities)
    `project.dataset.extracted_biz_nodes` AS BizNode
      KEY (biz_node_id)
      LABEL BizNode
      PROPERTIES (
        node_type, node_value, confidence, session_id,
        span_id, artifact_uri
      ),
    -- Decision point nodes (NEW in V3)
    `project.dataset.decision_points` AS DecisionPoint
      KEY (decision_id)
      LABEL DecisionPoint
      PROPERTIES (
        session_id, span_id, decision_type, description
      ),
    -- Candidate nodes (NEW in V3)
    `project.dataset.candidates` AS CandidateNode
      KEY (candidate_id)
      LABEL CandidateNode
      PROPERTIES (
        decision_id, session_id, name, score, status,
        rejection_rationale
      )
  )
  EDGE TABLES (
    -- Causal lineage: parent span -> child span
    `project.dataset.agent_events` AS Caused
      KEY (span_id)
      SOURCE KEY (parent_span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (span_id) REFERENCES TechNode (span_id)
      LABEL Caused,

    -- Cross-link: technical event -> business entity it evaluated
    `project.dataset.context_cross_links` AS Evaluated
      KEY (link_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (biz_node_id) REFERENCES BizNode (biz_node_id)
      LABEL Evaluated
      PROPERTIES (
        artifact_uri, link_type, created_at
      ),

    -- TechNode -> DecisionPoint (span that made the decision) (NEW in V3)
    `project.dataset.made_decision_edges` AS MadeDecision
      KEY (edge_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      LABEL MadeDecision,

    -- DecisionPoint -> CandidateNode (selected or dropped) (NEW in V3)
    `project.dataset.candidate_edges` AS CandidateEdge
      KEY (edge_id)
      SOURCE KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      DESTINATION KEY (candidate_id) REFERENCES CandidateNode (candidate_id)
      LABEL CandidateEdge
      PROPERTIES (
        edge_type, rejection_rationale, created_at
      )
  )
```

### 3.5 MadeDecision Edge Creation (NEW in V3)

Edges from TechNode spans to the DecisionPoints they produced:

```sql
INSERT INTO `project.dataset.made_decision_edges`
  (edge_id, span_id, decision_id, created_at)
SELECT
  CONCAT(dp.span_id, ':MADE_DECISION:', dp.decision_id) AS edge_id,
  dp.span_id,
  dp.decision_id,
  CURRENT_TIMESTAMP() AS created_at
FROM `project.dataset.decision_points` dp
WHERE dp.session_id IN UNNEST(@session_ids)
```

### 3.6 CandidateEdge Creation (NEW in V3)

Edges from DecisionPoints to their CandidateNodes with typed edge semantics:

```sql
INSERT INTO `project.dataset.candidate_edges`
  (edge_id, decision_id, candidate_id, edge_type,
   rejection_rationale, created_at)
SELECT
  CONCAT(c.decision_id, ':', c.status, ':', c.candidate_id) AS edge_id,
  c.decision_id,
  c.candidate_id,
  CASE c.status
    WHEN 'SELECTED' THEN 'SELECTED_CANDIDATE'
    ELSE 'DROPPED_CANDIDATE'
  END AS edge_type,
  c.rejection_rationale,
  CURRENT_TIMESTAMP() AS created_at
FROM `project.dataset.candidates` c
WHERE c.session_id IN UNNEST(@session_ids)
```

### 3.7 GQL Reasoning Chain

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

### 3.8 GQL EU Audit Trail (NEW in V3)

Forward traversal from TechNode through DecisionPoint to CandidateNode for EU audit compliance:

```sql
GRAPH `project.dataset.agent_context_graph`
MATCH
  (step:TechNode)-[md:MadeDecision]->(dp:DecisionPoint)
    -[ce:CandidateEdge]->(cand:CandidateNode)
WHERE dp.session_id = @session_id
  AND dp.decision_type = @decision_type
RETURN
  dp.decision_id,
  dp.decision_type,
  dp.description AS decision_description,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.status AS candidate_status,
  cand.rejection_rationale,
  ce.edge_type,
  step.span_id,
  step.event_type,
  step.agent
ORDER BY dp.decision_id, cand.score DESC
LIMIT @result_limit
```

The pattern `(step:TechNode)-[md:MadeDecision]->(dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)` is a forward traversal that follows the decision-making chain: which span made the decision, what was decided, and which candidates were evaluated. The `decision_type` clause is optional and filters by decision category (e.g., `audience_selection`, `placement_selection`).

### 3.9 GQL Dropped Candidates (NEW in V3)

Filter for dropped candidates with rejection rationale:

```sql
GRAPH `project.dataset.agent_context_graph`
MATCH
  (dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)
WHERE dp.session_id = @session_id
  AND ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  dp.decision_id,
  dp.decision_type,
  dp.description AS decision_description,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.rejection_rationale
ORDER BY dp.decision_id, cand.score DESC
LIMIT @result_limit
```

### 3.10 GQL Trace Reconstruction

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

### 3.11 World-Change Detection Query

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
| `inventory_depleted` | Yahoo Homepage Takeover sold out | 0.95 (critical) |
| `price_changed` | Strava Routes $40K to $52K (+30%) | 0.72 (moderate) |
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

## 5. Decision Semantics (NEW in V3)

### 5.1 Motivation

EU regulations and enterprise governance increasingly require explainability for automated decisions. When an agent selects audience segments, ad placements, or budget allocations, organizations must be able to answer:

- **What was decided?** — The decision type and description
- **What were the options?** — All candidates that were evaluated
- **Why was each option selected or rejected?** — Scores, selection status, and rejection rationale
- **Which span made the decision?** — Traceability back to the technical execution

Decision Semantics provides a structured model for these requirements, extending the Context Graph with first-class decision and candidate entities.

### 5.2 Data Model

**DecisionPoint** — A moment where the agent evaluated multiple candidates:

```python
@dataclass
class DecisionPoint:
    decision_id: str       # Unique identifier
    session_id: str        # Session containing this decision
    span_id: str           # Span where the decision was made
    decision_type: str     # "audience_selection", "placement_selection", etc.
    description: str       # Human-readable description
    timestamp: Optional[datetime] = None
    metadata: Optional[dict] = None
```

**Candidate** — An option evaluated at a decision point:

```python
@dataclass
class Candidate:
    candidate_id: str      # Unique identifier
    decision_id: str       # Parent decision point
    session_id: str        # Session containing this candidate
    name: str              # Candidate name/label
    score: float = 0.0     # Evaluation score (0.0-1.0)
    status: str = "SELECTED"  # "SELECTED" or "DROPPED"
    rejection_rationale: Optional[str] = None  # Required for DROPPED
    properties: Optional[dict] = None          # Additional properties
```

### 5.3 EU Audit Trail

The EU audit trail uses forward GQL traversal to reconstruct the complete decision chain:

```
TechNode ──MadeDecision──► DecisionPoint ──CandidateEdge──► CandidateNode
  (span)                    (decision)                      (candidate)
```

This traversal returns all candidates (selected and dropped) with their scores, status, and rejection rationale. The `edge_type` on CandidateEdge distinguishes `SELECTED_CANDIDATE` from `DROPPED_CANDIDATE`.

### 5.4 Candidate Scoring

Each candidate receives a score between 0.0 and 1.0, representing the agent's evaluation of the option. Higher scores indicate better fit. The score is extracted by AI.GENERATE from the agent's reasoning payloads.

Example from the Nike audience selection decision:

| Candidate | Score | Status | Rationale |
|-----------|-------|--------|-----------|
| Athletes 18-35 | 0.92 | SELECTED | Best reach-to-cost ratio |
| Fitness Enthusiasts 25-44 | 0.71 | DROPPED | Budget constraints |
| Running Community 18-30 | 0.65 | DROPPED | Budget constraints |

### 5.5 Rejection Rationale

For DROPPED candidates, `rejection_rationale` is a required field that explains why the option was not selected. This supports:

- **EU regulatory compliance** — Automated decision explainability
- **Debugging** — Understanding why the agent made a specific choice
- **HITL review** — Giving human reviewers context for approval decisions

Example from the ELF Cosmetics placement selection:

| Candidate | Score | Status | Rationale |
|-----------|-------|--------|-----------|
| Instagram Reels | 0.95 | SELECTED | — |
| TikTok TopView | 0.93 | SELECTED | — |
| LinkedIn Sponsored | 0.22 | DROPPED | Gen Z affinity below 0.70 threshold; skews professional/35+ demographic |
| Yahoo Homepage | 0.31 | DROPPED | Gen Z affinity below 0.70 threshold; audience skews older demographic |

### 5.6 Dual-Path Decision Explanation

The `explain_decision()` method provides two paths:

1. **EU audit path** (when `session_id` is provided): Uses the EU audit GQL query to traverse `TechNode → MadeDecision → DecisionPoint → CandidateEdge → CandidateNode`. Supports `decision_type` filtering and `include_dropped` toggle.

2. **BizNode reasoning chain fallback** (when `session_id` is not provided): Falls back to the original quantified-path GQL query that traverses causal chains from a decision event to business entities.

```python
# EU audit path: decision semantics
results = cgm.explain_decision(
    session_id="sess-nike-summer",
    decision_type="audience_selection",
    include_dropped=True,
)

# BizNode fallback: reasoning chain
results = cgm.explain_decision(
    biz_entity="Instagram Reels",
    decision_event_type="HITL_CONFIRMATION_REQUEST_COMPLETED",
)
```

### 5.7 Audit Trail Export

The `export_audit_trail()` method provides a complete export of all decisions and candidates for a session:

```python
trail = cgm.export_audit_trail(
    session_id="sess-nike-summer",
    include_dropped=True,
    format="dict",  # or "json"
)
# Returns:
# [
#   {
#     "decision_id": "dp-nike-audience",
#     "decision_type": "audience_selection",
#     "description": "Select target audience for Nike summer campaign",
#     "span_id": "span-abc123",
#     "candidates": [
#       {"candidate_id": "cand-1", "name": "Athletes 18-35",
#        "score": 0.92, "status": "SELECTED", "rejection_rationale": null},
#       {"candidate_id": "cand-2", "name": "Fitness Enthusiasts 25-44",
#        "score": 0.71, "status": "DROPPED",
#        "rejection_rationale": "Budget constraints"},
#       ...
#     ]
#   }
# ]
```

---

## 6. Production Query Patterns

The demo includes 5 production-ready BigQuery queries covering the Decision Semantics workflow.

### 6.1 Extended AI.GENERATE output_schema (Decision Extraction)
Uses `AI.GENERATE` with `_DECISION_POINT_OUTPUT_SCHEMA` to extract structured decision data from agent payloads, including decision type, description, and all candidates with scores, status, and rejection rationale. Uses MERGE with 3-way logic for upsert and stale cleanup.

### 6.2 Decision & Candidate Tables DDL
`CREATE TABLE IF NOT EXISTS` DDL for the `decision_points`, `candidates`, `made_decision_edges`, and `candidate_edges` tables that support the Decision Semantics extension.

### 6.3 Extended Property Graph DDL (6-Pillar Architecture)
`CREATE OR REPLACE PROPERTY GRAPH` DDL defining the 6-pillar graph: TechNode vertices (from `agent_events`), BizNode vertices (from `biz_nodes`), DecisionPoint vertices (from `decision_points`), CandidateNode vertices (from `candidates`), Caused edges (span lineage), Evaluated edges (cross-links), MadeDecision edges (span→decision), and CandidateEdge edges (decision→candidate).

### 6.4 GQL EU Audit Trail ("Why was X shown instead of Y?")
Forward GQL traversal from TechNode through MadeDecision to DecisionPoint to CandidateEdge to CandidateNode. Returns all candidates with scores, selection status, and rejection rationale. Supports optional `decision_type` filtering.

### 6.5 GQL Dropped Candidates with Rationale
Filters CandidateEdge edges by `edge_type = 'DROPPED_CANDIDATE'` to surface all rejected options with rationale. Supports EU regulatory compliance requirements for explainability of automated decisions.

---

## 7. Interactive Demo Features

### 7.1 Decision Graph Visualization
Interactive SVG-based graph rendering of the 6-pillar architecture. TechNodes (circles) are color-coded by event type: blue (user input), green (agent lifecycle), red (tool calls), yellow (HITL events). BizNodes (rectangles) are color-coded by entity type. DecisionPoint nodes (diamonds) represent decision moments. CandidateNode nodes (rounded rectangles) are color-coded by status: green (SELECTED) and red (DROPPED). MadeDecision edges (solid purple) connect spans to decisions; CandidateEdge edges distinguish selected (solid green) from dropped (dashed red) candidates.

### 7.2 Audit Trail Panel
Dedicated panel showing the complete audit trail for the selected session. Displays all decision points with their candidates, scores, selection status, and rejection rationale. Dropped candidates are highlighted with the reason for rejection. Supports both the full audit view and filtered views (selected-only, dropped-only).

### 7.3 SQL / GQL Query Explorer
Interactive query browser with 5 production-ready queries. Each query includes full SQL/GQL, category badges (Extraction, Graph, Audit), and feature tags (AI.GENERATE, output_schema, Property Graph, GQL, Decision Semantics). Syntax-highlighted with dark theme and copy-to-clipboard support.

### 7.4 Python SDK Panel
Live-updating Python code showing the full SDK workflow for the selected session:
1. Initialize `ContextGraphManager` with `ContextGraphConfig`
2. Extract BizNodes via `cgm.extract_biz_nodes()`
3. Extract DecisionPoints and Candidates via `cgm.extract_decision_points()`
4. Store decision points via `cgm.store_decision_points()`
5. Create cross-links and decision edges via `cgm.create_cross_links()` and `cgm.create_decision_edges()`
6. Create Property Graph (6-pillar with `include_decisions=True`)
7. Explain decisions via `cgm.explain_decision(session_id=..., decision_type=..., include_dropped=True)`
8. Export audit trail via `cgm.export_audit_trail(session_id=..., format="json")`

### 7.5 Session Selector
Switch between ADCP sessions demonstrating different decision semantics outcomes: Nike (audience selection with 3 candidates, 2 dropped due to budget) and ELF Cosmetics (placement selection with 4 candidates, 2 dropped due to audience mismatch). Each session shows decision counts, candidate counts, selected/dropped breakdown, and decision types.

---

## 8. Architecture

### 8.1 Data Flow

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
│  3. AI.GENERATE + output_schema → decision_points +      │
│     candidates tables (NEW)                              │
│  4. Decision edges → made_decision_edges +               │
│     candidate_edges tables (NEW)                         │
│  5. CREATE PROPERTY GRAPH (6-pillar DDL)                 │
│  6. GQL queries for reasoning + trace + audit trail      │
│  7. World-change detection (fail-closed)                 │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│         BigQuery Property Graph (6-pillar)                │
│                                                          │
│  TechNode ──Caused──► TechNode                           │
│     │                                                    │
│     ├──Evaluated──► BizNode (+ artifact_uri)             │
│     │                                                    │
│     └──MadeDecision──► DecisionPoint (NEW)               │
│                            │                             │
│                            └──CandidateEdge──►           │
│                                CandidateNode (NEW)       │
│                                (SELECTED / DROPPED)      │
└──────────────────────────────────────────────────────────┘
```

### 8.2 SDK Module: `context_graph.py`

The Context Graph is implemented as a standalone module (`~2500 lines`) in the BigQuery Agent Analytics SDK:

| Class | Responsibility |
|-------|---------------|
| `ContextGraphManager` | Main entry point: extraction, cross-links, graph creation, GQL queries, world-change detection, decision semantics |
| `ContextGraphConfig` | Configuration: endpoint, table names (including decision tables), graph name, extraction prompt |
| `BizNode` | Dataclass representing an extracted business entity |
| `DecisionPoint` | Dataclass representing a decision point with type, description, and span linkage |
| `Candidate` | Dataclass representing a candidate with score, status, and rejection rationale |
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
| Two-table edge model | BigQuery Property Graph DDL requires separate edge tables when source/destination types differ |
| Idempotent delete-then-insert | Decision data uses delete-before-insert instead of MERGE for simplicity with multi-table consistency |
| Dual-path explain_decision | Session-aware EU audit path with BizNode reasoning chain fallback for backward compatibility |

### 8.3 Client Integration

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

### 8.4 Visualization Layer

- React 18 frontend with Google Cloud design system styling
- Inline SVG graph with positioned TechNodes (circles), BizNodes (rectangles), DecisionPoints (diamonds), and CandidateNodes (rounded rectangles)
- Caused edges (solid lines), Evaluated cross-links (dashed lines), MadeDecision edges (solid purple), CandidateEdge edges (green for selected, red dashed for dropped)
- Interactive node selection with property panel showing candidate details
- Dark-themed SQL/GQL code viewer with copy support
- Session switcher for comparing decision outcomes across ADCP sessions
- Audit trail panel with decision-level drill-down

---

## 9. Testing

The Context Graph module has 95 dedicated tests covering:

| Test Category | Count | Examples |
|--------------|-------|---------|
| BizNode extraction | 5 | AI.GENERATE path, client-side path, output_schema in SQL |
| BizNode storage & retrieval | 5 | Store with artifact_uri, read back, session filtering |
| Cross-links | 3 | Create, composite link_id, delete error handling |
| Property Graph | 5 | DDL generation (4-pillar + 6-pillar), graph creation with/without decisions, config override |
| GQL queries | 4 | Reasoning chain, causal chain, trace reconstruction |
| World-change detection | 5 | Safe, drift detected, fn exception (fail-closed), query failure (fail-closed) |
| End-to-end pipeline | 3 | `build_context_graph()`, partial failure, `build_context_graph(include_decisions=True)` |
| Client integration | 4 | `get_session_trace_gql()`, parent backfill, chronological ordering, isolated event merge |
| Edge cases | 5 | Legacy endpoint rejection, evaluated_at passthrough, MERGE delete, empty sessions |
| DecisionPoint/Candidate models | 3 | SELECTED candidate properties, DROPPED candidate with rationale, config table names |
| Decision extraction | 6 | AI.GENERATE JSON parsing, client-side stubs, empty rows, bad JSON, empty JSON, extraction failure |
| Decision storage | 4 | Store success, empty input, DP insert error, candidate insert error, table create failure |
| Decision edges | 2 | Create MadeDecision + CandidateEdge edges, edge creation failure |
| Decision DDL | 3 | 6-pillar DDL includes base pillars, custom graph name, edge source/destination types |
| EU audit GQL | 3 | Basic traversal, decision_type filter, forward traversal direction |
| Dropped candidates GQL | 1 | `edge_type = 'DROPPED_CANDIDATE'` filter |
| Decision point queries | 4 | Get decision points for session, get candidates for decision, query failures |
| explain_decision | 5 | Audit path with all candidates, filter dropped, decision_type filter, GQL error fallback, reasoning chain fallback |
| export_audit_trail | 3 | Dict format, JSON format, exclude dropped candidates |
| Idempotent operations | 1 | Delete-before-insert for decision data |

Full test suite: **609 tests** (95 context graph + 514 existing SDK tests), all passing.

---

## 10. Deployment

The demo is deployed as a single self-contained HTML file with embedded React/Babel. No build step or server is required.

### 10.1 Live Demo
[https://decision-semantics-demo.vercel.app](https://decision-semantics-demo.vercel.app)

### 10.2 Source Code
- Decision Semantics Demo: [`examples/decision_semantics_demo.html`](../examples/decision_semantics_demo.html)
- V2 Demo: [`examples/context_graph_v2_demo.html`](../examples/context_graph_v2_demo.html)
- Notebook: [`examples/context_graph_adcp_demo.ipynb`](../examples/context_graph_adcp_demo.ipynb)
- SDK module: [`src/bigquery_agent_analytics/context_graph.py`](../src/bigquery_agent_analytics/context_graph.py)
- Tests: [`tests/test_context_graph.py`](../tests/test_context_graph.py)

### 10.3 Production Deployment Notes

For production use:
1. Replace simulated session data with real BigQuery connections
2. Configure Vertex AI connection for AI.GENERATE (`us.vertex_ai_connection`)
3. Create `agent_events` table using the ADK plugin schema
4. Set up ADK BigQuery Agent Analytics Plugin for event streaming
5. Create the BizNode, cross-links, decision_points, candidates, and edge tables via `ContextGraphManager`
6. Configure `current_state_fn` callbacks pointing to real inventory/pricing APIs
7. Integrate `detect_world_changes()` into HITL approval workflows
8. Integrate `export_audit_trail()` into compliance reporting pipelines
9. Configure `explain_decision()` for EU regulatory audit responses

---

## 11. Evolution from V1/V2

| Aspect | V1 (Context Graphs & Decision Traces) | V2 (System of Reasoning) | V3 (Decision Semantics) |
|--------|---------------------------------------|--------------------------|------------------------|
| Graph Model | Simulated property graph via recursive CTEs | 4-pillar: TechNode + BizNode + Caused + Evaluated | 6-pillar: adds DecisionPoint + CandidateNode + MadeDecision + CandidateEdge |
| Entity Extraction | Manual event classification | AI.GENERATE with `output_schema` for entities | AI.GENERATE with `output_schema` for entities + decisions with candidates |
| Traversal | Recursive CTEs for decision traces | GQL with quantified-path patterns (`->{1,20}`) | GQL + EU audit forward traversal (TechNode→DecisionPoint→CandidateNode) |
| Business Layer | Single vertex types (User, Session, LLM, Tool) | Separate TechNode + BizNode with cross-links | TechNode + BizNode + DecisionPoint + CandidateNode with typed edges |
| Safety | None | World-change detection with fail-closed semantics | World-change detection + decision audit trails |
| Decision Modeling | Not tracked | Implicit in BizNode reasoning chains | Explicit DecisionPoint + Candidate with scores, status, rationale |
| Audit Compliance | None | None | EU audit trail with rejection rationale for all dropped candidates |
| Candidate Scoring | Not tracked | Not tracked | 0.0-1.0 score per candidate with SELECTED/DROPPED status |
| Edge Types | None | Caused + Evaluated (2 edge tables) | Caused + Evaluated + MadeDecision + CandidateEdge (4 edge tables) |
| Artifacts | Not tracked | `artifact_uri` on BizNode + Evaluated edge | `artifact_uri` + `rejection_rationale` on CandidateEdge |
| Stale Data | Not handled | MERGE with `WHEN NOT MATCHED BY SOURCE ... DELETE` | MERGE for BizNodes + idempotent delete-then-insert for decisions |
| SDK Integration | Standalone demo | Full `context_graph.py` module (~1300 lines, 50 tests) | Extended module (~2500 lines, 95 tests) |
| Query Injection | Raw SQL interpolation | Parameterized `@biz_entity`, `@session_id` | Parameterized + `@decision_type`, `@result_limit` |
| Explainability | None | `explain_decision()` via BizNode reasoning chain | Dual-path `explain_decision()`: EU audit GQL + BizNode fallback |
