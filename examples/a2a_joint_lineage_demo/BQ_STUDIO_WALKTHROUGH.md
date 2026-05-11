# BigQuery Studio Walkthrough — A2A Joint Lineage

This walkthrough shows a real ADK caller agent delegating to a real
receiver agent over A2A, with both sides logged by the BQ AA Plugin and
stitched into one auditor-facing property graph.

Pair this with [`DEMO_NARRATION.md`](DEMO_NARRATION.md) for the
presenter talk track.

## Before You Start

Run setup once:

```bash
cd examples/a2a_joint_lineage_demo
./setup.sh
```

For a presentation run, use the one-command runner:

```bash
./run_e2e_demo.sh
```

The runner starts the receiver A2A server, smoke-tests that receiver
plugin writes land in BigQuery, runs the caller campaigns, builds both
per-org SDK context graphs, builds the auditor joint graph, and renders
`bq_studio_queries.gql`.

## Step 0 — Open BigQuery Studio

1. Open `https://console.cloud.google.com/bigquery?project=<PROJECT_ID>`.
2. In **Explorer**, expand these datasets:
   - `a2a_caller_demo`
   - `a2a_receiver_demo`
   - `a2a_auditor_demo`
3. In `a2a_auditor_demo`, confirm the property graph
   `a2a_joint_context_graph` is present.

What to say:

> There are two independent plugin trace tables. The caller and receiver
> never share memory. The auditor dataset is the redacted stitch layer
> that turns those traces into one queryable graph.

## Step 1 — Show The Raw BQ AA Data

Open a new query and run:

```sql
SELECT
  'caller' AS side,
  event_type,
  COUNT(*) AS rows
FROM `<PROJECT_ID>.a2a_caller_demo.agent_events`
GROUP BY event_type
UNION ALL
SELECT
  'receiver' AS side,
  event_type,
  COUNT(*) AS rows
FROM `<PROJECT_ID>.a2a_receiver_demo.agent_events`
GROUP BY event_type
ORDER BY side, rows DESC;
```

Point out:

- Caller rows include `A2A_INTERACTION`.
- Receiver rows are ordinary ADK spans: request, LLM, response, and
  completion events.
- This is real plugin output, not seeded rows.

## Step 2 — Confirm The A2A Stitch

Paste **Block 1** from `bq_studio_queries.gql`.

Expected:

- `a2a_calls > 0`
- `stitched_edges = a2a_calls`
- `unstitched_calls = 0`

What to say:

> The caller-side A2A event carries a context id. The receiver uses that
> same value as its session id. That equality is the stitch key:
> caller context id equals receiver session id.

## Step 3 — Visualize The Cross-Agent Path

Paste **Block 2** from `bq_studio_queries.gql`.

After it runs:

1. Click the **Graph** tab in BigQuery Studio.
2. Show the path:
   `CallerCampaignRun -> RemoteAgentInvocation -> ReceiverAgentRun`.
3. Click a `RemoteAgentInvocation` node and show `a2a_context_id`.
4. Click a `ReceiverAgentRun` node and show `receiver_session_id`.

What to say:

> This is the cross-agent handoff as a graph edge. The caller made a
> remote delegation, and the receiver session that handled it is now
> visible without exposing raw request or response payloads.

## Step 4 — Show Decisions And Rejections

Paste **Block 3** from `bq_studio_queries.gql`.

This walks:

```text
RemoteAgentInvocation
  -> ReceiverAgentRun
  -> ReceiverPlanningDecision
  -> ReceiverDecisionOption
```

Point out:

- Each row is a dropped receiver option.
- `rejection_rationale` is the auditor-facing explanation.
- The reason came from receiver trace text via the SDK context-graph
  extraction path.

## Step 5 — Right-To-Explanation Drilldown

Paste **Block 4** from `bq_studio_queries.gql`.

`run_caller_agent.py` records `DEMO_CALLER_SESSION_ID` into `.env`, and
`build_joint_graph.py` renders that value into the query file.

Show:

- One campaign.
- The remote receiver decision.
- All selected and dropped options.
- The rationale on dropped options.

What to say:

> This is the practical audit question: for one campaign, what did the
> remote governance agent consider, what did it select, what did it drop,
> and why?

## Step 6 — Redaction Proof

Paste **Block 5** from `bq_studio_queries.gql`.

Expected result: zero rows.

What to say:

> The auditor graph exposes lineage ids, decision labels, scores,
> outcomes, and rationale. It does not expose raw A2A request, raw A2A
> response, or full trace content in the auditor projection tables.

## What This Demo Proves

- Real ADK agents can be instrumented with the BQ AA Plugin.
- A caller and receiver can write separate trace tables.
- The SDK can materialize context graphs independently for both sides.
- A redacted auditor layer can stitch the two sides with graph semantics.
- BigQuery Studio can visualize the handoff and answer audit questions
  with GQL.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `run_e2e_demo.sh` says receiver did not become ready | Port already in use or Vertex/auth failure during server startup | Check `receiver_server.log`; change `RECEIVER_A2A_URL` or fix auth |
| Smoke gate fails | Receiver server is not writing BQ AA Plugin rows | Confirm `run_receiver_server.py` uses `Runner(..., plugins=[receiver_plugin])` |
| `unstitched_calls > 0` | Receiver session service did not honor caller context ids | Use the demo's `InMemorySessionService` path; see `A2A_JOINT_LINEAGE.md` |
| Receiver-scope gate fails | Receiver response was too loose for extraction | Inspect receiver `LLM_RESPONSE` rows and tighten `receiver_agent/prompts.py` |
| Block 5 returns rows | Auditor projections leaked raw payload columns | Fix `build_joint_graph.py` projection SQL |
