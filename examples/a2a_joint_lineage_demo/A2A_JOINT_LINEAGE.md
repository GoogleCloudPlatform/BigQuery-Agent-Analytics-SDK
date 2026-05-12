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

### ADK 1.33 sub-session shape

Under `google-adk` 1.33, `RemoteA2aAgent` does **not** emit its `A2A_INTERACTION` row under the supervisor's `session_id`. The agent spawns its own caller-side `InvocationContext` with a fresh session id, so the row lands in a sibling caller-side session whose `agent = 'audience_risk_reviewer'` and `root_agent_name = 'audience_risk_reviewer'`. The two events share `user_id` and `app_name` but carry no foreign key linking them back to the supervisor.

To keep the `CallerCampaignRun -> RemoteAgentInvocation` edge valid, `run_caller_agent.py` materializes an explicit mapping table after caller flush:

```text
<CALLER_DATASET>.supervisor_a2a_invocations
  caller_session_id            ← supervisor session_id (FK → campaign_runs)
  supervisor_span_id           ← TOOL_STARTING for audience_risk_reviewer
  supervisor_ts                ← TOOL_STARTING timestamp
  a2a_invocation_session_id    ← RemoteA2aAgent sub-session id
  a2a_invocation_span_id       ← A2A_INTERACTION span id
  a2a_invocation_timestamp     ← A2A_INTERACTION timestamp
  a2a_task_id                  ← a2a_metadata."a2a:task_id"
  a2a_context_id               ← a2a_metadata."a2a:context_id"
  receiver_session_id_from_response  ← COALESCE of the two response paths
```

Each supervisor's `TOOL_STARTING` for `audience_risk_reviewer` is paired with the first `A2A_INTERACTION` in that tool call's time window: `supervisor_ts <= a2a_timestamp < next_supervisor_ts` for the same `user_id`. This is deterministic because campaign briefs run sequentially, and it is stricter than global row-number pairing: a stale or extra A2A sub-session row can no longer shift every later campaign. Gate G1.5 in `run_caller_agent.py` asserts mapping count == campaign count and rejects NULL `a2a_context_id` rows; `build_joint_graph.remote_agent_invocations` then reads from this mapping rather than from the raw `agent_events` table. The receiver-side stitch (`a2a_context_id == receiver.session_id`) is unchanged.

## What the auditor sees

`build_joint_graph.py` writes six `CREATE OR REPLACE TABLE` projections into `<AUDITOR_DATASET>`:

| Auditor table | Source | Purpose |
|---|---|---|
| `caller_campaign_runs` | `<CALLER_DATASET>.campaign_runs` | Renames `session_id` → `caller_session_id` to match the graph DDL's `KEY (caller_session_id)` |
| `remote_agent_invocations` | `<CALLER_DATASET>.supervisor_a2a_invocations`, **scoped by `INNER JOIN caller_campaign_runs`** | One row per remote A2A call **for the current campaign run only**. Reads through the supervisor↔A2A-sub-session mapping that `run_caller_agent.py` materializes after caller flush (ADK 1.33 split-session shape — see above). Carries lineage IDs (task/context); drops raw `a2a_request` / `a2a_response` / `content`. The `caller_campaign_runs` join keeps no-reset reruns from carrying orphaned remote invocations whose `CallerCampaignRun` source vanished. |
| `receiver_runs` | receiver `agent_events` `GROUP BY session_id`, **filtered to `session_id IN (SELECT a2a_context_id FROM remote_agent_invocations)`** | Receiver-side session roots **for sessions matched to current caller campaigns**. Smoke session + sessions left from prior runs are excluded. |
| `receiver_planning_decisions` | `<RECEIVER_DATASET>.decision_points`, **filtered to `session_id IN (SELECT receiver_session_id FROM receiver_runs)`** | Receiver-side decisions extracted from `LLM_RESPONSE` text, scoped to the receiver sessions retained in `receiver_runs` |
| `receiver_decision_options` | `<RECEIVER_DATASET>.candidates`, **filtered to `session_id IN (SELECT receiver_session_id FROM receiver_runs)`** | Receiver-side options weighed (`rejection_rationale` lives here as a property), same scoping as `receiver_planning_decisions` |
| `joint_a2a_edges` | inner join of `remote_agent_invocations` and `receiver_runs` on `a2a_context_id == receiver_session_id` | The cross-org stitch as a first-class edge table |

The scope chain (`caller_campaign_runs` → `remote_agent_invocations` → `receiver_runs` → receiver decisions/options) means the auditor surface always reflects the current campaign run only. `build_joint_graph.py`'s `_verify_graph` runs **four** coverage gates:

1. **Stitch coverage** — every `remote_agent_invocations` row has a matching `joint_a2a_edges` row.
2. **Aggregate receiver-extraction coverage** — scoped `receiver_planning_decisions ≥ DEMO_MIN_RECEIVER_DECISIONS` (default 3) and `receiver_decision_options ≥ DEMO_MIN_RECEIVER_CANDIDATES` (default 9). Catches the case where the unscoped receiver dataset satisfied `build_org_graphs.py` but the scope chain dropped most rows.
3. **Per-receiver-session coverage** — every stitched receiver session has ≥1 decision and ≥3 candidates (the per-call receiver-prompt contract). Catches the case where the aggregate count meets the threshold because some sessions over-produce while others have zero, leaving Block 4 empty for a specific campaign.
4. **End-to-end traversal smoke** — walks all the way to `ReceiverDecisionOption`, so a broken scope chain or KEY/REFERENCES drift in the property graph DDL produces zero rows even when the upstream gates pass.

All projections use `CREATE OR REPLACE TABLE … AS SELECT …` so re-runs are idempotent. Redaction of raw payloads (`a2a_request`, `a2a_response`, `content`) is a *convention* enforced by the projection SELECT lists, not an IAM-enforced control. A single-project demo cannot enforce IAM-level redaction — that's the production cross-org story (out of scope here; see the working-group plan in #129).

The Phase 1 joint property graph has 5 node labels and 4 edge labels. `receiver_planning_decisions` and `receiver_decision_options` each back **both** a node and an edge — BigQuery permits this table-reuse pattern and it keeps the first joint graph smaller than introducing dedicated edge tables. See `DATA_LINEAGE.md` for the per-table mapping.

## BigQuery Studio walkthrough

`render_queries.sh` writes `bq_studio_queries.gql` with concrete `<PROJECT>` / `<AUDITOR_DATASET>` / `<DEMO_CALLER_SESSION_ID>` values inlined. Open BigQuery Studio in the demo project and paste each block.

### Block 1 — Stitch health and coverage

```sql
WITH ri AS (
  SELECT
    COUNT(*)                                                  AS a2a_calls,
    COUNTIF(a2a_context_id IS NOT NULL)                       AS calls_with_context_id,
    COUNTIF(receiver_session_id_from_response IS NOT NULL)    AS calls_with_receiver_echo
  FROM `<P>.<AUDITOR>.remote_agent_invocations`
),
edges AS (
  SELECT COUNT(*) AS stitched_edges
  FROM `<P>.<AUDITOR>.joint_a2a_edges`
)
SELECT
  ri.a2a_calls,
  ri.calls_with_context_id,
  ri.calls_with_receiver_echo,
  edges.stitched_edges,
  ri.a2a_calls - edges.stitched_edges AS unstitched_calls
FROM ri, edges;
```

What it tells you:

- `a2a_calls` should equal the number of campaigns × delegations-per-campaign (3 in the default config).
- `calls_with_context_id = a2a_calls` always; `a2a:context_id` is set unconditionally by `RemoteA2aAgent`.
- `calls_with_receiver_echo` may be less than `a2a_calls` for `A2AMessage`-shaped responses; treat as diagnostic only. The actual stitch uses `a2a_context_id` against `receiver.session_id`, not the echo.
- `stitched_edges` is the number of remote calls that joined cleanly to a receiver session via `joint_a2a_edges`.
- **`unstitched_calls > 0` is the failure signal.** It means some remote A2A calls have no matching receiver session — typically the receiver session service rewrote `session_id` so the equality `a2a_context_id == receiver.session_id` no longer holds. `build_joint_graph.py` already fails its strict-coverage gate on this; Block 1 is the operator-side reproduction.

### Block 2 — End-to-end A2A path

```sql
GRAPH `<P>.<AUDITOR>.a2a_joint_context_graph`
MATCH (campaign:CallerCampaignRun)
      -[:DelegatedVia]->(remote:RemoteAgentInvocation)
      -[:HandledBy]->(receiver:ReceiverAgentRun)
RETURN campaign.caller_session_id,
       campaign.campaign,
       remote.a2a_context_id,
       remote.a2a_task_id,
       receiver.receiver_session_id,
       receiver.event_count
LIMIT 20;
```

One row per remote A2A call. Pick any `caller_session_id` from the first column and use it as the `@caller_session` parameter in Block 4.

### Block 3 — Remote governance rejections

Walks every dropped option the receiver returned across the demo. The `option.rejection_rationale` column carries the concrete reason the receiver gave (PII proxy risk, age-range mismatch, etc.) — this is the audit signal for "the remote agent said no, here's why."

### Block 4 — Right-to-explanation for one campaign

The Article 22 / Article 86 query: for one specific caller campaign, return every option the remote agent considered, the score, the SELECTED/DROPPED status, and the rationale. Both selected and dropped options appear because `rejection_rationale` is a column property (NULL for SELECTED, non-NULL for DROPPED).

`render_queries.sh` inlines `DEMO_CALLER_SESSION_ID` from `.env`; `run_caller_agent.py` records the first successful caller session there on every run.

### Block 5 — Redaction proof

Lists every column named `a2a_request` / `a2a_response` / `content` across the auditor projections. **Expected: zero rows.** The auditor surface intentionally drops raw payloads; this query is the single statement that demonstrates the convention is in force.

If this returns rows, an upstream change has leaked payload columns into the auditor projection tables — fix the projection SQL in `build_joint_graph.py` before merging.

## Closing the loop — the analyst agent

`run_analyst_agent.py` is the demo's last hop. It instantiates an ADK agent (`analyst_agent/agent.py`) whose system prompt frames it as an audit analyst with four bounded BigQuery tools — one per headline audit question:

| Tool | Maps to | When the agent picks it |
|---|---|---|
| `stitch_health()` | Block 1 | "Is the audit graph healthy? Are all calls accounted for?" |
| `list_campaigns()` | (discovery) | "What campaigns are in scope?" or session-id lookup when the user names a campaign |
| `audit_campaign(caller_session_id)` | Block 4 | "Walk me through the audit path for campaign X" |
| `find_governance_rejections(decision_type=None, max_score=None)` | Block 3 (filtered) | Portfolio-level scans for dropped options |

Each tool runs a parameterized BigQuery query against the auditor projections and the `a2a_joint_context_graph`. Results are dict-shaped and bounded (default 25 campaigns, 30 rejection rows) so the LLM doesn't blow its context window. Raw `a2a_request` / `a2a_response` payloads are never returned — the redaction contract from the projection layer carries through to the agent surface.

The analyst's own ADK session — its tool-call sequence, its LLM responses, its final summary — lands in `<ANALYST_DATASET>.agent_events` via a third BQ AA Plugin instance. Operators can run `ContextGraphManager.build_context_graph` against that dataset for audit-of-the-audit lineage if they need it; that's left as a follow-on.

The default `run_analyst_agent.py` invocation fires four canned questions (one per tool) so a fresh demo exercises the whole tool surface. Positional arguments override the canned set for ad-hoc questions:

```bash
./.venv/bin/python3 run_analyst_agent.py \
  "Across all campaigns, which dropped options scored below 0.3?"
```

## Failure modes and what each gate catches

| Symptom | Likely cause | Where it surfaces |
|---|---|---|
| `smoke_receiver.py` exits with "row count did not increase" | Receiver running with `to_a2a()`'s default plugin-free runner, or BQ AA Plugin failing to write | smoke gate before any caller campaign runs |
| `run_caller_agent.py` G1 fails | Caller plugin failed to write, or supervisor LLM skipped the `audience_risk_reviewer` tool call (no `A2A_INTERACTION` agent='audience_risk_reviewer' rows materialized) | G1 prints observed-vs-expected counts |
| `run_caller_agent.py` G1.5 fails | Supervisor↔A2A mapping count != campaign count, or rows have NULL `a2a_context_id` — the chronological TS↔A2A pairing misaligned (typically a receiver timeout where TOOL_STARTING fired but A2A_INTERACTION did not) | Inspect `<CALLER_DATASET>.supervisor_a2a_invocations` against the caller `agent_events` TOOL_STARTING vs A2A_INTERACTION timeline |
| `run_caller_agent.py` G2 fails after polling | Receiver writes lagging beyond the poll window, or receiver plugin missing | G2 message names the failure mode |
| `run_caller_agent.py` G3 fails | `context_id != session_id` — receiver session service rewriting ids, or non-`InMemorySessionService` | G3 message names this directly |
| `build_org_graphs.py` receiver gate fails (decision_points < 3) | Receiver prompt isn't enforcing the three-option format | Tighten `receiver_agent/prompts.py`, not the graph DDL |
| `build_org_graphs.py` prints "applying demo fallback parser" | BigQuery `AI.GENERATE` returned too few typed decision rows for the receiver's strict prompt-shaped output | The fallback parser reads the receiver `LLM_RESPONSE` format directly and rewrites `decision_points` / `candidates`; if the gate still fails, tighten `receiver_agent/prompts.py` |
| `build_joint_graph.py` stitch-coverage gate fails (`unstitched_calls > 0`) | `a2a_context_id` doesn't match any `receiver_session_id` — receiver session service rewriting ids, or non-`InMemorySessionService` | Run Block 1 to inspect; usually the same root cause as G3 |
| `build_joint_graph.py` aggregate receiver-scope gate fails (`scoped_decisions < N` or `scoped_candidates < M`) | Auditor scope chain dropped most rows on a no-reset rerun (receiver session ids changed between runs) | `./reset.sh && ./setup.sh`, or override `DEMO_MIN_RECEIVER_DECISIONS` / `DEMO_MIN_RECEIVER_CANDIDATES` if intentionally running fewer campaigns |
| `build_joint_graph.py` per-receiver-session gate fails (lists sessions with `d=0` or `c<3`) | One specific receiver session has too-thin extraction *traversable through its retained decisions* — either the aggregate count looks fine but a specific session under-produces, or the session's candidates carry `decision_id` values that aren't in `receiver_planning_decisions` (so they're orphaned from the property graph traversal) | Inspect the receiver `agent_events` for the listed session ids; tighten `receiver_agent/prompts.py` if the `LLM_RESPONSE` text doesn't match the SELECTED\|DROPPED shape, or check the SDK extractor's per-session decision/candidate consistency |
| `build_joint_graph.py` end-to-end traversal returns zero rows after the prior gates pass | Property graph DDL or the edge chain between `ReceiverAgentRun` → `ReceiverPlanningDecision` → `ReceiverDecisionOption` is broken (KEY/REFERENCES drift in `joint_property_graph.gql.tpl`) | Re-render and re-create the graph; if the templates were edited, verify the EDGE TABLE clauses on `receiver_planning_decisions` (`ReceiverMadeDecision`) and `receiver_decision_options` (`ReceiverWeighedOption`) still match their projection columns |
| Block 5 returns rows | An upstream change leaked payload columns into the auditor surface | Fix the projection SQL in `build_joint_graph.py` |
