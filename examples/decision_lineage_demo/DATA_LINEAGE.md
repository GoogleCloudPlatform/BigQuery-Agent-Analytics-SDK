# Data Lineage — how the demo tables produce the rich graph

A step-by-step map of how the demo's BigQuery dataset is built and
how every node label / edge label in `rich_agent_context_graph`
traces back to a specific table and writer.

Useful for: explaining the architecture to leadership, onboarding
someone running setup for the first time, or proving to a reviewer
exactly where any given graph element comes from.

---

The SDK first builds the canonical `agent_context_graph` from seven
SDK tables. The demo then adds SQL-only projection tables and creates
`rich_agent_context_graph`, the graph presenters open in BigQuery
Studio.

---

## Step 1 — Three writers populate the seven SDK tables

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

### 1e. `campaign_runs` — exact campaign/session mapping

`run_agent.py` writes `campaign_runs` after all six sessions finish
successfully. This avoids relying on timestamp order to infer which
session belonged to which campaign. Each row stores the exact
`session_id`, campaign name, brand, full campaign brief, run order,
and streamed event count.

### 1f. `build_rich_graph.py` — SQL-only presentation layer

After the SDK tables exist, `build_rich_graph.py` creates demo-only
projection tables:

  * `campaign_runs` — reused from `run_agent.py` when present;
    synthesized only as a fallback for legacy or cloned datasets.
  * `rich_agent_steps` — one row per distinct `span_id`, deduped from
    `agent_events` so the rich graph's AgentStep / NextStep labels
    satisfy BigQuery's KEY contract even when the raw plugin table has
    duplicate span rows.
  * `rich_decision_types` — one row per normalized decision type.
  * `rich_candidate_statuses` — normally `SELECTED` and `DROPPED`.
  * `rich_rejection_reasons` — one row per distinct dropped-candidate
    rationale.
  * `rich_campaign_span_edges`, `rich_campaign_decision_edges`,
    `rich_decision_type_edges`, `rich_candidate_status_edges`, and
    `rich_candidate_reason_edges` — edges from those presentation
    nodes back to the SDK-owned facts.

No live agent call and no `AI.GENERATE` call happens in this step.
It is deterministic SQL, plus a fallback load job for `campaign_runs`
only when `run_agent.py` did not already write that table.

---

## Step 2 — Property graph DDL wires tables into two graph surfaces

The SDK-shaped graph from `property_graph.gql.tpl` is compact:

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

The richer demo graph from `rich_property_graph.gql.tpl` keeps the
same underlying tables but uses ads-domain labels for the presenter
surface:

| Rich graph element | Comes from table | KEY column | Why it exists in the demo |
|---|---|---|---|
| **CampaignRun** | `campaign_runs` | `session_id` | Makes each campaign run visible as the root of a graph visualization |
| **AgentStep** | `rich_agent_steps` | `span_id` | Business-readable, key-clean name for a plugin-recorded span |
| **MediaEntity** | `extracted_biz_nodes` | `biz_node_id` | Audience, channel, creative, budget, or other media-planning entity |
| **PlanningDecision** | `decision_points` | `decision_id` | One choice the agent made during campaign planning |
| **DecisionCategory** | `rich_decision_types` | `decision_type_id` | Groups equivalent decision categories for portfolio questions |
| **DecisionOption** | `candidates` | `candidate_id` | One option the agent selected or dropped |
| **OptionOutcome** | `rich_candidate_statuses` | `status_id` | Promotes SELECTED / DROPPED from a property into visible graph nodes |
| **DropReason** | `rich_rejection_reasons` | `reason_id` | Promotes dropped-option rationale into visible "why rejected" nodes |
| **CampaignActivity** | `rich_campaign_span_edges` | `session_id` → `span_id` | Connects a campaign run to its raw plugin spans |
| **NextStep** | `rich_agent_steps` | `parent_span_id` → `span_id` | Parent → child sequence in the agent run |
| **ConsideredEntity** | `context_cross_links` | `span_id` → `biz_node_id` | Which agent step produced or considered which media entity |
| **DecidedAt** | `made_decision_edges` | `span_id` → `decision_id` | Span where the planning decision committed |
| **CampaignDecision** | `rich_campaign_decision_edges` | `session_id` → `decision_id` | Connects a campaign run directly to extracted decisions |
| **InCategory** | `rich_decision_type_edges` | `decision_id` → `decision_type_id` | Connects each decision to its normalized category |
| **WeighedOption** | `candidate_edges` | `decision_id` → `candidate_id` | Connects each planning decision to the options it weighed |
| **HasOutcome** | `rich_candidate_status_edges` | `candidate_id` → `status_id` | Connects each option to SELECTED or DROPPED |
| **RejectedBecause** | `rich_candidate_reason_edges` | `candidate_id` → `reason_id` | Connects dropped options to rationale nodes |

---

## Step 3 — Worked example: how Block 2 (the visualization GQL) traverses these

Block 2 from `bq_studio_queries.gql`:

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH p =
  (cr:CampaignRun)-[:CampaignDecision]->(dp:PlanningDecision)
    -[:WeighedOption]->(c:DecisionOption)-[:HasOutcome]->(st:OptionOutcome)
WHERE cr.session_id = '<SESSION_ID>'
RETURN p;
```

What the engine does, table by table:

  1. **CampaignRun binding** — scans `campaign_runs` for the selected
     `session_id`.
  2. **CampaignDecision edge** — joins
     `rich_campaign_decision_edges` on `session_id`; each edge points
     at one extracted decision.
  3. **PlanningDecision binding** — joins `decision_points` on
     `decision_id`.
  4. **WeighedOption edge** — joins `candidate_edges` on
     `decision_id`; each decision fans out to its selected and
     dropped candidates.
  5. **DecisionOption binding** — joins `candidates` on
     `candidate_id`.
  6. **HasOutcome edge** — joins
     `rich_candidate_status_edges` so SELECTED / DROPPED appear as
     graph nodes, not just properties.
  7. **Result** — paths bound to `p` with campaign, decision,
     candidate, and status nodes.
     BigQuery Studio's **Graph** tab renders these as fan-outs
     (one per decision) of branches (one per candidate), with
     `edge_type` and node properties available in the click-through
     panel.

---

## Step 4 — The same map for the EU-compliance questions

| Question ([`DEMO_QUESTIONS.md`](DEMO_QUESTIONS.md)) | Tables touched | Why |
|---|---|---|
| **Q1** Right to explanation for one campaign | `decision_points` ⋈ `candidate_edges` ⋈ `candidates` ⋈ `rich_candidate_status_edges` ⋈ `rich_candidate_statuses` | Per-decision, walks edges to surface SELECTED + DROPPED + rationale |
| **Q2** Bias audit (rationales citing age / demo) | `decision_points` ⋈ `candidate_edges` ⋈ `candidates` ⋈ status tables | LIKE-filter on `candidates.rejection_rationale` (extracted from the LLM trace text by AI.GENERATE; exact wording can vary across runs — see [`README.md`](README.md)) |
| **Q3** Human-oversight trigger (score < 0.7) | `decision_points` ⋈ `candidate_edges` ⋈ `candidates` ⋈ status tables | Filter on selected candidates with `score < 0.7`; empty result is the audit artifact |
| **Q4** Reproducibility (one decision's full lineage) | `campaign_runs` ⋈ `decision_points` plus `agent_events` ⋈ `made_decision_edges` ⋈ `candidate_edges` ⋈ `candidates` | Walks from CampaignRun and AgentStep through the decision and options back to the evidence span (`NextStep` and `ConsideredEntity` are reachable but not used in the shipped Q4 GQL) |
| **Q5** Pattern audit (rejection counts by type) | `decision_points` ⋈ `rich_decision_type_edges` ⋈ `rich_decision_types` ⋈ `candidate_edges` ⋈ `candidates` ⋈ status tables | `GROUP BY DecisionCategory` with `COUNT(c)` + `AVG(c.score)` |

Every regulator-shaped question is a walk over a fixed subset of
the SDK tables plus deterministic rich projections, expressed as GQL
against graph labels — no bespoke ETL and no per-question table
writes. **The seven SDK tables are the audit substrate; the rich
property graph is the presenter-friendly query view over them.**

---

## Step 5 — Recreate the graph from existing tables

If the seven SDK backing tables are already populated (a previous
setup run, a clone of someone else's dataset, or a manual
INSERT-from-SELECT into a new project) and you just need the
graph layer, `property_graph.gql.tpl` is a standalone `CREATE OR
REPLACE PROPERTY GRAPH` you apply with one `bq query` against the
existing tables. `setup.sh` renders it next to
`bq_studio_queries.gql`.

For the richer graph, run `build_rich_graph.py` first so the
derived tables exist, then apply `rich_property_graph.gql`. See
[`SETUP_NEW_PROJECT.md`](SETUP_NEW_PROJECT.md)
→ "Recreate the property graph from existing tables".
