# A2A Joint Lineage — stitch contract + BQ Studio walkthrough

This document explains *how* the demo turns two independent BQ AA Plugin trace tables into one queryable joint property graph, and walks through the five blocks in `bq_studio_queries.gql`.

## The stitch contract

Two BQ AA Plugin instances run in two processes:

- **Caller** — `run_caller_agent.py` runs the media-planning supervisor through `InMemoryRunner` with `plugins=[caller_bq_logging_plugin]`. Spans land in `<CALLER_DATASET>.agent_events`.
- **Receiver** — `run_receiver_server.py` serves the audience-risk reviewer over A2A via `to_a2a()`, with the BQ AA Plugin attached through an explicit `Runner(plugins=[receiver_plugin])` (the default-runner path drops plugins). Spans land in `<RECEIVER_DATASET>.agent_events`.

When the supervisor calls `audience_risk_reviewer` (a `RemoteA2aAgent` wrapped in `AgentTool`), the caller-side plugin emits an `A2A_INTERACTION` event carrying:

```text
attributes.a2a_metadata."a2a:task_id"
attributes.a2a_metadata."a2a:context_id"
attributes.a2a_metadata."a2a:request"
attributes.a2a_metadata."a2a:response"
```

The auditor projection joins the two sides at **context level**:

```text
caller.A2A_INTERACTION.a2a_metadata."a2a:context_id"
  ==
receiver.agent_events.session_id
```

Why this equality holds:

1. `RemoteA2aAgent` populates `a2a:context_id` on the caller side (see `remote_a2a_agent.py:521-525` in `adk-python`).
2. `convert_a2a_request_to_agent_run_request` (`request_converter.py:111`) sets `session_id := request.context_id` on the receiver side.
3. `run_receiver_server.py` runs an `InMemorySessionService` that honors explicit session ids — `_prepare_session` passes `session_id=session_id` to `create_session` (`a2a_agent_executor_impl.py:296-302`).

The `joint_a2a_edges` projection in `build_joint_graph.py` materializes this join as the `HandledBy` edge in the property graph.

## What the auditor sees

`build_joint_graph.py` writes six `CREATE OR REPLACE TABLE` projections into `<AUDITOR_DATASET>`:

| Auditor table | Source | Purpose |
|---|---|---|
| `caller_campaign_runs` | `<CALLER_DATASET>.campaign_runs` | Renames `session_id` → `caller_session_id` to match the graph DDL's `KEY (caller_session_id)` |
| `remote_agent_invocations` | caller `agent_events` `WHERE event_type = 'A2A_INTERACTION'` | One row per remote A2A call. Carries lineage IDs (task/context); drops raw `a2a_request` / `a2a_response` / `content` |
| `receiver_runs` | receiver `agent_events` `GROUP BY session_id` | Receiver-side session roots — there is no campaign brief on the receiver, so this is the only sensible root |
| `receiver_planning_decisions` | `<RECEIVER_DATASET>.decision_points` | Receiver-side decisions extracted from `LLM_RESPONSE` text |
| `receiver_decision_options` | `<RECEIVER_DATASET>.candidates` | Receiver-side options weighed (`rejection_rationale` lives here as a property) |
| `joint_a2a_edges` | inner join of the two above on `a2a_context_id == receiver_session_id` | The cross-org stitch as a first-class edge table |

All projections use `CREATE OR REPLACE TABLE … AS SELECT …` so re-runs are idempotent. Redaction of raw payloads (`a2a_request`, `a2a_response`, `content`) is a *convention* enforced by the projection SELECT lists, not an IAM-enforced control. A single-project demo cannot enforce IAM-level redaction — that's the production cross-org story (out of scope here; see the working-group plan in #129).

The Phase 1 joint property graph has 5 node labels and 4 edge labels. `receiver_planning_decisions` and `receiver_decision_options` each back **both** a node and an edge — BigQuery permits this table-reuse pattern and it keeps the first joint graph smaller than introducing dedicated edge tables. See `DATA_LINEAGE.md` for the per-table mapping.

## BigQuery Studio walkthrough

`render_queries.sh` writes `bq_studio_queries.gql` with concrete `<PROJECT>` / `<AUDITOR_DATASET>` / `<DEMO_CALLER_SESSION_ID>` values inlined. Open BigQuery Studio in the demo project and paste each block.

### Block 1 — Stitch health

```sql
SELECT COUNT(*)                                                  AS a2a_calls,
       COUNTIF(a2a_context_id IS NOT NULL)                       AS calls_with_context_id,
       COUNTIF(receiver_session_id_from_response IS NOT NULL)    AS calls_with_receiver_echo
FROM `<P>.<AUDITOR>.remote_agent_invocations`;
```

What it tells you:

- `a2a_calls` should equal the number of campaigns × delegations-per-campaign (3 in the default config).
- `calls_with_context_id = a2a_calls` always; `a2a:context_id` is set unconditionally by `RemoteA2aAgent`.
- `calls_with_receiver_echo` may be less than `a2a_calls` for `A2AMessage`-shaped responses; treat as diagnostic only. The actual stitch uses `a2a_context_id` against `receiver.session_id`, not the echo.

### Block 2 — End-to-end A2A path

```sql
GRAPH `<P>.<AUDITOR>.a2a_joint_context_graph`
MATCH (campaign:CallerCampaignRun)
      -[:DelegatedVia]->(remote:RemoteAgentInvocation)
      -[:HandledBy]->(receiver:ReceiverAgentRun)
RETURN campaign.campaign,
       remote.a2a_context_id,
       remote.a2a_task_id,
       receiver.receiver_session_id,
       receiver.event_count
LIMIT 20;
```

One row per remote A2A call. Pick any `caller_session_id` from this output and use it as the `@caller_session` parameter in Block 4.

### Block 3 — Remote governance rejections

Walks every dropped option the receiver returned across the demo. The `option.rejection_rationale` column carries the concrete reason the receiver gave (PII proxy risk, age-range mismatch, etc.) — this is the audit signal for "the remote agent said no, here's why."

### Block 4 — Right-to-explanation for one campaign

The Article 22 / Article 86 query: for one specific caller campaign, return every option the remote agent considered, the score, the SELECTED/DROPPED status, and the rationale. Both selected and dropped options appear because `rejection_rationale` is a column property (NULL for SELECTED, non-NULL for DROPPED).

`render_queries.sh` inlines `DEMO_CALLER_SESSION_ID` from `.env`; `run_caller_agent.py` records the first successful caller session there on every run.

### Block 5 — Redaction proof

Lists every column named `a2a_request` / `a2a_response` / `content` across the auditor projections. **Expected: zero rows.** The auditor surface intentionally drops raw payloads; this query is the single statement that demonstrates the convention is in force.

If this returns rows, an upstream change has leaked payload columns into the auditor view set — fix the projection SQL in `build_joint_graph.py` before merging.

## Failure modes and what each gate catches

| Symptom | Likely cause | Where it surfaces |
|---|---|---|
| `smoke_receiver.py` exits with "row count did not increase" | Receiver running with `to_a2a()`'s default plugin-free runner, or BQ AA Plugin failing to write | smoke gate before any caller campaign runs |
| `run_caller_agent.py` G1 fails | Caller plugin failed to write or campaign produced no `A2A_INTERACTION` row | Per-session breakdown logged |
| `run_caller_agent.py` G2 fails after polling | Receiver writes lagging beyond the poll window, or receiver plugin missing | G2 message names the failure mode |
| `run_caller_agent.py` G3 fails | `context_id != session_id` — receiver session service rewriting ids, or non-`InMemorySessionService` | G3 message names this directly |
| `build_org_graphs.py` receiver gate fails (decision_points < 3) | Receiver prompt isn't enforcing the three-option format | Tighten `receiver_agent/prompts.py`, not the graph DDL |
| `build_joint_graph.py` traversal smoke returns zero rows | `joint_a2a_edges` is empty — `a2a_context_id` doesn't match any `receiver_session_id` | Run Block 1 to inspect; usually the receiver session service issue from G3 |
| Block 5 returns rows | An upstream change leaked payload columns into the auditor surface | Fix the projection SQL in `build_joint_graph.py` |
