# Data lineage — table-by-table source map

This document traces every table the demo writes back to its source, and explains the cross-table KEY contracts that the joint property graph DDL relies on.

## Layer 1 — BQ AA Plugin raw spans

| Table | Written by | Schema | Columns the demo reads |
|---|---|---|---|
| `<CALLER_DATASET>.agent_events` | Caller's `BigQueryAgentAnalyticsPlugin` (in `run_caller_agent.py`) | Plugin-defined; one row per ADK event | `session_id`, `span_id`, `event_type`, `attributes` (JSON), `content` (JSON), `timestamp` |
| `<RECEIVER_DATASET>.agent_events` | Receiver's `BigQueryAgentAnalyticsPlugin` (in `run_receiver_server.py`) | Same plugin schema | Same columns |

Notes:

- The two plugin instances are independent; they don't share state. The auditor projection layer (Layer 4) is what links them.
- `event_type` includes `INVOCATION_STARTING`, `AGENT_STARTING`, `USER_MESSAGE_RECEIVED`, `LLM_REQUEST`, `LLM_RESPONSE`, `TOOL_STARTING`, `TOOL_COMPLETED`, `AGENT_COMPLETED`, `INVOCATION_COMPLETED`, plus — on the caller side only — `A2A_INTERACTION` whenever the supervisor calls `RemoteA2aAgent`.
- **ADK 1.33 caller-side telemetry shape:** `RemoteA2aAgent` spawns its own caller-side `InvocationContext` with a fresh `session_id`, so the `A2A_INTERACTION` row does **not** live under the supervisor session. It lands in a sibling caller-side session whose `agent='audience_risk_reviewer'` and `root_agent_name='audience_risk_reviewer'`. The supervisor and sub-session share `user_id` and `app_name` but carry no foreign key linking them. The Layer 2 mapping table below reconstructs that link before Layer 4 runs.

## Layer 2 — Demo metadata

| Table | Written by | Purpose |
|---|---|---|
| `<CALLER_DATASET>.campaign_runs` | `run_caller_agent.py` writes via `load_table_from_json` after caller flush | Records the `(session_id, campaign, brand, brief, run_order, event_count)` tuple per successful caller campaign. Without this, the auditor projection has no human-readable label for caller sessions. |
| `<CALLER_DATASET>.supervisor_a2a_invocations` | `run_caller_agent.py` CTAS after caller flush, scoped to the current run's `session_id IN UNNEST(@sessions)` | Bridges the ADK 1.33 split-session shape. Pairs each supervisor `TOOL_STARTING` (`content.tool='audience_risk_reviewer'`, `content.tool_origin='A2A'`) with the chronologically-first `A2A_INTERACTION` (`agent='audience_risk_reviewer'`) in the same `[supervisor_ts, next_supervisor_ts)` window for the same `user_id`. Carries `caller_session_id` (supervisor session, FK → `campaign_runs`), `a2a_invocation_session_id` (sub-session), `a2a_invocation_span_id`, `a2a_task_id`, `a2a_context_id`, and `receiver_session_id_from_response`. Gate G1.5 in `run_caller_agent.py` rejects NULL `a2a_context_id` rows and asserts mapping count == campaign count. Stricter than global `ROW_NUMBER()` pairing: a stale or extra A2A sub-session row no longer shifts every later campaign's mapping. |

## Layer 3 — SDK extraction outputs (per-org)

`build_org_graphs.py` runs `ContextGraphManager.build_context_graph(use_ai_generate=True, include_decisions=True)` against each dataset; the SDK creates these tables in each:

| Table | KEY | Source | Used by auditor as |
|---|---|---|---|
| `extracted_biz_nodes` | `biz_node_id` | `AI.GENERATE` over `agent_events` content | (caller side) — not used in Phase 1 joint graph |
| `context_cross_links` | `link_id` | `AI.GENERATE` cross-link extraction | (caller side) — not used in Phase 1 joint graph |
| `decision_points` | `decision_id` | `AI.GENERATE` over `LLM_RESPONSE` text | **Receiver side: source for `receiver_planning_decisions`** |
| `candidates` | `candidate_id` | `AI.GENERATE` over `LLM_RESPONSE` text (paired with `decision_id`) | **Receiver side: source for `receiver_decision_options`** |
| `made_decision_edges` | `edge_id` | SQL `INSERT` from `decision_points` | (receiver side) — not used in Phase 1 joint graph |
| `candidate_edges` | `edge_id` | SQL `INSERT` from `candidates` | (receiver side) — not used in Phase 1 joint graph |
| `agent_context_graph` | (property graph) | `CREATE OR REPLACE PROPERTY GRAPH` over the six tables above | Per-org graph; not consumed by the joint graph |

Phase 1 deliberately uses only the receiver's `decision_points` + `candidates` for the joint graph because that's where the audit signal lives ("what did the remote governance agent reject and why"). Caller-side decisions are visible in the caller's own per-org graph; surfacing them in the joint graph is a Phase 2 expansion.

**Receiver-side dual-path writer.** The receiver's `decision_points` and `candidates` tables can be populated by either path:

1. **Primary — SDK `AI.GENERATE`.** `ContextGraphManager.build_context_graph(use_ai_generate=True, include_decisions=True)` runs `AI.GENERATE` with a typed `output_schema => 'decisions ARRAY<STRUCT<...>>'`, then wraps the returned `decisions` array with `TO_JSON_STRING(...)` for the Python parser. Deterministic `model_params` (`temperature=0.0`, `topP=0.1`).
2. **Fallback — `build_org_graphs._repair_receiver_extraction_from_prompt_contract`.** If the receiver-extraction gate observes `decisions < DEMO_MIN_RECEIVER_DECISIONS` or `candidates < DEMO_MIN_RECEIVER_CANDIDATES` (defaults 3 / 9), the demo parses receiver `LLM_RESPONSE` rows with a strict regex matching the `receiver_agent/prompts.py` contract (`- name — SELECTED|DROPPED — score N.NN — rationale: ...`) and re-stores via `ContextGraphManager.store_decision_points` + `create_decision_edges`. `store_decision_points` deletes existing decision rows for the parsed `session_id`s, then appends replacement rows via a BigQuery load job; it does **not** truncate the whole table. If a future prompt change makes the fallback under-perform, the receiver-extraction gate trips loudly instead of silently producing a thin graph.

The schema of the tables is identical under both paths; downstream Layer 4 projections do not need to distinguish them.

## Layer 4 — Auditor projections

`build_joint_graph.py` writes six `CREATE OR REPLACE TABLE` projections into `<AUDITOR_DATASET>`. Inputs span Layers 1, 2, and 3 — raw caller/receiver `agent_events` (Layer 1), demo metadata (Layer 2), and SDK-extracted graph backing tables (Layer 3) — as listed in the **Source projection** column:

| Auditor table | Layer | KEY | Source projection | Why renamed |
|---|---|---|---|---|
| `caller_campaign_runs` | 2 | `caller_session_id` | `SELECT session_id AS caller_session_id, campaign, brand, brief, run_order, event_count FROM <CALLER>.campaign_runs` | Graph DDL needs `caller_session_id` as the KEY; source has bare `session_id` |
| `remote_agent_invocations` | 2 + 4 | `remote_invocation_id` | `SELECT TO_HEX(SHA256(CONCAT(m.a2a_invocation_session_id, ':', m.a2a_invocation_span_id))) AS remote_invocation_id, m.caller_session_id, m.a2a_invocation_span_id AS caller_span_id, m.a2a_task_id, m.a2a_context_id, m.receiver_session_id_from_response, m.a2a_invocation_timestamp AS timestamp FROM <CALLER>.supervisor_a2a_invocations m JOIN <AUDITOR>.caller_campaign_runs ccr ON m.caller_session_id = ccr.caller_session_id` | One row per remote A2A call. Reads through the Layer 2 supervisor↔sub-session mapping (ADK 1.33 split-session shape); `caller_session_id` still points at the supervisor session so the graph DDL's `CallerCampaignRun -[:DelegatedVia]-> RemoteAgentInvocation` edge resolves without DDL change. Deterministic synthetic ID. Lineage IDs only — drops raw `a2a_request` / `a2a_response` / `content`. Joined against `caller_campaign_runs` so reruns without `./reset.sh` don't carry orphaned remote invocations whose `CallerCampaignRun` source vanished. |
| `receiver_runs` | 1 + 4 | `receiver_session_id` | `SELECT session_id AS receiver_session_id, MIN(timestamp), MAX(timestamp), COUNT(*), COUNTIF(event_type='AGENT_COMPLETED') FROM <RECEIVER>.agent_events WHERE session_id IN (SELECT DISTINCT a2a_context_id FROM <AUDITOR>.remote_agent_invocations WHERE a2a_context_id IS NOT NULL) GROUP BY session_id` | Receiver has no campaign briefs; this is the only sensible session-root projection. Scoped to current caller `a2a_context_id` set so no-reset reruns and the smoke_receiver session don't carry stale receiver runs into the auditor surface or the BQ Studio Explorer. |
| `receiver_planning_decisions` | 3 + 4 | `decision_id` | `SELECT decision_id, session_id, span_id, decision_type, description FROM <RECEIVER>.decision_points WHERE session_id IN (SELECT receiver_session_id FROM <AUDITOR>.receiver_runs)` | Same key as source; scoped to receiver sessions retained in `receiver_runs` so stale extractions stay out of the auditor view |
| `receiver_decision_options` | 3 + 4 | `candidate_id` | `SELECT candidate_id, decision_id, session_id, name, score, status, rejection_rationale FROM <RECEIVER>.candidates WHERE session_id IN (SELECT receiver_session_id FROM <AUDITOR>.receiver_runs)` | Same key as source; same scoping as `receiver_planning_decisions` |
| `joint_a2a_edges` | 4 (self) | `edge_id` | `SELECT TO_HEX(SHA256(CONCAT(r.remote_invocation_id, ':', rr.receiver_session_id))) AS edge_id, r.remote_invocation_id, rr.receiver_session_id, r.a2a_context_id, r.a2a_task_id FROM remote_agent_invocations r JOIN receiver_runs rr ON r.a2a_context_id = rr.receiver_session_id` | The cross-org stitch as a first-class edge table |

All Layer 4 tables are idempotent via `CREATE OR REPLACE TABLE`. Re-running `build_joint_graph.py` rebuilds them in place — no duplicates accumulate.

## Layer 5 — Joint property graph

`<AUDITOR_DATASET>.a2a_joint_context_graph` (5 nodes, 4 edges):

| Label | Kind | Backing table | KEY |
|---|---|---|---|
| `CallerCampaignRun` | NODE | `caller_campaign_runs` | `caller_session_id` |
| `RemoteAgentInvocation` | NODE | `remote_agent_invocations` | `remote_invocation_id` |
| `ReceiverAgentRun` | NODE | `receiver_runs` | `receiver_session_id` |
| `ReceiverPlanningDecision` | NODE | `receiver_planning_decisions` | `decision_id` |
| `ReceiverDecisionOption` | NODE | `receiver_decision_options` | `candidate_id` |
| `DelegatedVia` | EDGE | `remote_agent_invocations` | (same physical table as `RemoteAgentInvocation` node) |
| `HandledBy` | EDGE | `joint_a2a_edges` | dedicated edge table |
| `ReceiverMadeDecision` | EDGE | `receiver_planning_decisions` | (same physical table as `ReceiverPlanningDecision` node) |
| `ReceiverWeighedOption` | EDGE | `receiver_decision_options` | (same physical table as `ReceiverDecisionOption` node) |

Three of the four edges share their physical table with a node. BigQuery permits this; the alias (`AS DelegatedVia` vs `AS RemoteAgentInvocation`) disambiguates. Each edge declares its own `KEY` and its own `SOURCE KEY (col) REFERENCES NodeLabel (col)` / `DESTINATION KEY (col) REFERENCES NodeLabel (col)` so the FK contract is explicit.

This is the same pattern the existing `examples/decision_lineage_demo/rich_property_graph.gql.tpl` uses for the `NextStep` self-loop on `rich_agent_steps`.

## End-to-end traversal

The headline auditor query (Block 4 in `bq_studio_queries.gql`):

```text
CallerCampaignRun
  --[DelegatedVia]-->  RemoteAgentInvocation
  --[HandledBy]-->     ReceiverAgentRun
  --[ReceiverMadeDecision]-->  ReceiverPlanningDecision
  --[ReceiverWeighedOption]--> ReceiverDecisionOption
```

Reads as: "this caller campaign delegated this remote A2A call, which was handled by this receiver session, which made this planning decision, which weighed this option (with this rejection rationale)."

Six BigQuery tables on the join path; one GQL query against the property graph hides the joins from the auditor.
