# Data Lineage — how the seven tables produce the property graph

A step-by-step map of how the demo's BigQuery dataset is built and
how every node label / edge label in `agent_context_graph` traces
back to a specific table and writer.

Useful for: explaining the architecture to leadership, onboarding
someone running setup for the first time, or proving to a reviewer
exactly where any given graph element comes from.

---

## Step 1 — Three writers populate seven tables

```
┌────────────────────────────────────────────────────────────────────┐
│  WHO writes        WHEN          WHICH TABLE                       │
├────────────────────────────────────────────────────────────────────┤
│  BQ AA Plugin     run_agent.py   agent_events                      │
│  (live, inside    (one row per                                     │
│   the runner)     span)                                            │
│                                                                    │
│  SDK — AI.GEN.    build_graph.py extracted_biz_nodes               │
│  (extract_biz_    (MERGE,                                          │
│   nodes)          per-session                                      │
│                   idempotent)                                      │
│                                                                    │
│  SDK — AI.GEN.    build_graph.py decision_points  ┐ via store_     │
│  (extract_dec.    (DELETE-by-                     │  decision_     │
│   _points)        session +                       ├─ points        │
│                   load job)     candidates        ┘  (dedupe +     │
│                                                       load job)    │
│                                                                    │
│  SDK — SQL only   build_graph.py context_cross_links               │
│  (create_cross_   (DML INSERT)  made_decision_edges                │
│   links + ...)                  candidate_edges                    │
└────────────────────────────────────────────────────────────────────┘
```

### 1a. `agent_events` — the foundation

The BQ AA Plugin attached to `InMemoryRunner` writes one row per
span the live ADK agent emits. For one campaign session, the demo
produces 27 rows:

  * 1 `INVOCATION_STARTING`
  * 1 `AGENT_STARTING`
  * 1 `USER_MESSAGE_RECEIVED`
  * 5 `LLM_REQUEST` / 5 `LLM_RESPONSE` (the prompt requires five
    decisions)
  * 5 `TOOL_STARTING` / 5 `TOOL_COMPLETED` (linked by `tool_call_id`)
  * 1 `HITL_CONFIRMATION_REQUEST` / 1 `_COMPLETED`
  * 1 `AGENT_COMPLETED`
  * 1 `INVOCATION_COMPLETED`

Each row carries `span_id`, `parent_span_id`, `session_id`,
`event_type`, `agent`, `timestamp`, plus a JSON `content` payload
(the LLM_RESPONSE bodies are what name candidates and rationale).

### 1b. `extracted_biz_nodes` — entities AI.GENERATE found

`mgr.extract_biz_nodes(session_ids)` runs a single `MERGE` against
`agent_events` whose USING clause invokes `AI.GENERATE` per row.
Prompt: *"extract Product / Targeting / Campaign / Budget / Audience
/ Creative / Placement entities from this payload."* Output: one
row per `(span_id, node_type, node_value)` triple. The MERGE's
`WHEN NOT MATCHED BY SOURCE AND target.session_id IN (...) THEN
DELETE` clause keeps the table per-session-idempotent in a single
statement.

### 1c. `decision_points` + `candidates` — decisions AI.GENERATE found

A second `AI.GENERATE` call asks the model to identify *"moments
where the agent evaluated multiple candidates and selected or
rejected them"* and return per decision: `decision_type`,
`description`, plus a list of `{name, score, status,
rejection_rationale}` candidates.

The Python side parses the JSON and builds `DecisionPoint` and
`Candidate` records. `store_decision_points(...)` writes them via:

  1. **Dedupe in Python** by `decision_id` / `candidate_id`
     (in-batch dedup — guards against `AI.GENERATE` returning
     overlapping items in one extraction).
  2. **`DELETE FROM ... WHERE session_id IN (...)`** (per-session
     reseat — guards against re-running `build_graph.py`).
  3. **`load_table_from_json`** to managed storage, which is
     visible to the just-issued `DELETE` (avoiding the
     streaming-insert buffer pitfall that breaks `insert_rows_json`).

### 1d. The three edge tables — pure SQL DML

Once the node tables are populated:

  * `mgr.create_cross_links([sessions])` — `INSERT INTO
    context_cross_links` joining BizNode rows to TechNode spans
    by `span_id`. One Evaluated edge per BizNode.
  * `mgr.create_decision_edges([sessions])` — two inserts:
    - `made_decision_edges` (TechNode `span_id` → DecisionPoint
      `decision_id`).
    - `candidate_edges` (DecisionPoint `decision_id` →
      CandidateNode `candidate_id`, with `edge_type` set to
      `SELECTED_CANDIDATE` or `DROPPED_CANDIDATE` and the
      `rejection_rationale` carried on the edge).

---

## Step 2 — `CREATE OR REPLACE PROPERTY GRAPH` wires the tables into eight graph elements

The complete contract from `property_graph.gql.tpl`:

```
CREATE OR REPLACE PROPERTY GRAPH agent_context_graph
  NODE TABLES (
    agent_events        AS TechNode      KEY (span_id)
    extracted_biz_nodes AS BizNode       KEY (biz_node_id)
    decision_points     AS DecisionPoint KEY (decision_id)
    candidates          AS CandidateNode KEY (candidate_id)
  )
  EDGE TABLES (
    agent_events            AS Caused        SRC parent_span_id → TechNode.span_id
                                              DST span_id         → TechNode.span_id
    context_cross_links     AS Evaluated     SRC span_id         → TechNode.span_id
                                              DST biz_node_id     → BizNode.biz_node_id
    made_decision_edges     AS MadeDecision  SRC span_id         → TechNode.span_id
                                              DST decision_id     → DecisionPoint.decision_id
    candidate_edges         AS CandidateEdge SRC decision_id     → DecisionPoint.decision_id
                                              DST candidate_id    → CandidateNode.candidate_id
  )
```

Note that `agent_events` shows up **twice** — once as `TechNode`
(rows are nodes) and once as `Caused` (the same rows are also
edges, where `parent_span_id → span_id` defines the parent-child
chain). That is normal for property-graph DDL: the same table can
populate both a node label and an edge label.

| Graph element | Comes from table | KEY column | What you can ask via GQL |
|---|---|---|---|
| **TechNode** | `agent_events` | `span_id` | "every plugin-recorded span" |
| **BizNode** | `extracted_biz_nodes` | `biz_node_id` | "business entities the AI saw" |
| **DecisionPoint** | `decision_points` | `decision_id` | "moments the agent picked between options" |
| **CandidateNode** | `candidates` | `candidate_id` | "every option weighed at every decision" |
| **Caused** | `agent_events` | `parent_span_id` → `span_id` | causal trace lineage |
| **Evaluated** | `context_cross_links` | `span_id` → `biz_node_id` | which span produced which entity |
| **MadeDecision** | `made_decision_edges` | `span_id` → `decision_id` | which span produced which decision |
| **CandidateEdge** | `candidate_edges` | `decision_id` → `candidate_id` | which candidates the decision weighed (with `edge_type` SELECTED/DROPPED + rationale on the edge) |

---

## Step 3 — Worked example: how Block 2 (the visualization GQL) traverses these

Block 2 from `bq_studio_queries.gql`:

```sql
GRAPH `<P>.<D>.agent_context_graph`
MATCH p =
  (s:TechNode)-[:MadeDecision]->(dp:DecisionPoint)-[:CandidateEdge]->(c:CandidateNode)
WHERE dp.session_id = '<SESSION_ID>'
RETURN p;
```

What the engine does, table by table:

  1. **TechNode binding** — scans `agent_events` for spans whose
     `span_id` exists in `made_decision_edges.span_id`. (162 →
     ~5 rows for one session.)
  2. **MadeDecision edge** — joins `made_decision_edges` on
     `span_id`. Each match produces one TechNode → DecisionPoint
     pair.
  3. **DecisionPoint binding** — joins `decision_points` on
     `decision_id` and filters where `session_id = '<SESSION_ID>'`.
  4. **CandidateEdge edge** — joins `candidate_edges` on
     `decision_id`. Each decision fans out to its candidate edges.
  5. **CandidateNode binding** — joins `candidates` on
     `candidate_id`. The terminal node of each path.
  6. **Result** — paths bound to `p` with all four columns.
     BigQuery Studio's **Graph** tab renders these as fan-outs
     (one per decision) of branches (one per candidate), with
     `edge_type` and node properties available in the click-through
     panel.

---

## Step 4 — The same map for the EU-compliance questions

| Question ([`DEMO_QUESTIONS.md`](DEMO_QUESTIONS.md)) | Tables touched | Why |
|---|---|---|
| **Q1** Right to explanation for one campaign | `decision_points` ⋈ `candidate_edges` ⋈ `candidates` | Per-decision, walks edges to surface SELECTED + DROPPED + rationale |
| **Q2** Bias audit (rationales citing age / demo) | `decision_points` ⋈ `candidate_edges` ⋈ `candidates` | LIKE-filter on `candidates.rejection_rationale` (preserved verbatim from the LLM trace) |
| **Q3** Human-oversight trigger (score < 0.7) | `decision_points` ⋈ `candidate_edges` ⋈ `candidates` | Filter on `candidates.score`; empty result is the audit artifact |
| **Q4** Reproducibility (one decision's full lineage) | `agent_events` ⋈ `made_decision_edges` ⋈ `decision_points` ⋈ `candidate_edges` ⋈ `candidates` | Walks all four edge labels back to a single span_id |
| **Q5** Pattern audit (rejection counts by type) | `decision_points` ⋈ `candidate_edges` ⋈ `candidates` | `GROUP BY decision_type` with `COUNT(c)` + `AVG(c.score)` |

Every regulator-shaped question is a walk over a fixed subset of
the seven tables, expressed as GQL against the eight graph
elements — no bespoke ETL, no per-question table writes, no new
tables to maintain. **The seven tables are the audit substrate;
the property graph is a query view over them.**

---

## Step 5 — Recreate the graph from existing tables

If the seven backing tables are already populated (a previous
setup run, a clone of someone else's dataset, or a manual
INSERT-from-SELECT into a new project) and you just need the
graph layer, `property_graph.gql.tpl` is a standalone `CREATE OR
REPLACE PROPERTY GRAPH` you apply with one `bq query` against the
existing tables. `setup.sh` renders it next to
`bq_studio_queries.gql`. See [`SETUP_NEW_PROJECT.md`](SETUP_NEW_PROJECT.md)
→ "Recreate the property graph from existing tables".
