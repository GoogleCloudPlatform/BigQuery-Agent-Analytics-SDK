# A2A Joint Lineage Demo

Two BQ AA Plugin instances. Two `agent_events` tables. One A2A delegation in the middle. The caller's media-planning supervisor delegates audience-risk review to the receiver's governance agent over A2A; both sides write traces to BigQuery; the SDK materializes a context graph for each side independently; an auditor projection stitches them into a single joint property graph that an external reviewer queries through BigQuery Studio.

This bundle implements the plan in [issue #129](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/129) and ships in two PRs:

- **PR 1** (merged) — caller / receiver agents, receiver server with custom-runner plugin attach, smoke gate, caller driver, dual `ContextGraphManager` materialization.
- **PR 2** (this slice) — auditor projections (`build_joint_graph.py`), Phase 1 joint property graph (`joint_property_graph.gql.tpl`), 5 paste-and-run BigQuery Studio blocks (`bq_studio_queries.gql.tpl`), `render_queries.sh`, and the narrative docs.

## Running it

```bash
./setup.sh
```

Then in two terminals:

```bash
# Terminal A — long-lived receiver server
./.venv/bin/python3 run_receiver_server.py

# Terminal B — smoke + caller campaigns + dual graph + auditor graph
./.venv/bin/python3 smoke_receiver.py
./.venv/bin/python3 run_caller_agent.py
./.venv/bin/python3 build_org_graphs.py
./.venv/bin/python3 build_joint_graph.py
```

`build_joint_graph.py` already runs `./render_queries.sh` itself, so `bq_studio_queries.gql` is on disk after the last command. Re-run `./render_queries.sh` only if you edit `.env` (e.g. swap `DEMO_CALLER_SESSION_ID` to inspect a different campaign) or change the `*.gql.tpl` templates.

**For a clean verification run: `./reset.sh && ./setup.sh`, then run the two-terminal flow above.** `reset.sh` drops the caller, receiver, and auditor datasets entirely; `setup.sh` recreates them. The plugin creates tables, not datasets, so a bare `./reset.sh` would leave the demo unable to write. Resetting up front guarantees `build_org_graphs.py`'s discover-all-sessions pass reflects only the current campaigns.

The auditor-side projections built by `build_joint_graph.py` are scoped to the current campaign run regardless (the chain `caller_campaign_runs → remote_agent_invocations → receiver_runs → receiver decisions/options` filters out anything not matched to a current caller session). Stale rows from prior runs still accumulate in the **source** layers — `<CALLER_DATASET>.agent_events`, `<RECEIVER_DATASET>.agent_events`, and the per-org `decision_points` / `candidates` tables `build_org_graphs.py` writes — and remain visible in the BQ Studio Explorer for those datasets. Skip the reset if you're iterating and want that source-side history kept; reset if you want a guaranteed-clean per-org and acceptance-gate baseline.

After all five commands return zero, you have:

- `<PROJECT>.a2a_caller_demo.agent_events` — caller-side spans, including `A2A_INTERACTION` rows
- `<PROJECT>.a2a_caller_demo.campaign_runs` — campaign ↔ caller-session map
- `<PROJECT>.a2a_caller_demo.{extracted_biz_nodes,decision_points,candidates,…}` — caller graph backing tables
- `<PROJECT>.a2a_caller_demo.agent_context_graph` — caller property graph
- `<PROJECT>.a2a_receiver_demo.agent_events` — receiver-side spans
- `<PROJECT>.a2a_receiver_demo.{extracted_biz_nodes,decision_points,candidates,…}` — receiver graph backing tables
- `<PROJECT>.a2a_receiver_demo.agent_context_graph` — receiver property graph
- `<PROJECT>.a2a_auditor_demo.{caller_campaign_runs,remote_agent_invocations,receiver_runs,receiver_planning_decisions,receiver_decision_options,joint_a2a_edges}` — auditor projections (redacted)
- `<PROJECT>.a2a_auditor_demo.a2a_joint_context_graph` — joint property graph spanning both orgs

Open BigQuery Studio in the project, navigate to `a2a_auditor_demo`, and paste blocks from `bq_studio_queries.gql` (rendered by `render_queries.sh`). See [`A2A_JOINT_LINEAGE.md`](A2A_JOINT_LINEAGE.md) for the per-block walkthrough.

## Stitch contract

The auditor stitches caller and receiver at **context/session level**:

```text
caller.agent_events.attributes.a2a_metadata."a2a:context_id"
  ==
receiver.agent_events.session_id
```

This works because [`adk-python`'s `convert_a2a_request_to_agent_run_request`](https://github.com/google/adk-python/blob/main/src/google/adk/a2a/converters/request_converter.py) sets `session_id := request.context_id`, and `run_receiver_server.py` runs an `InMemorySessionService` that honors explicit session ids. `build_joint_graph.py`'s `joint_a2a_edges` projection materializes the join.

Per-span `a2a_task_id` propagation onto receiver spans is **deferred to a follow-up** because current ADK request conversion does not plumb `RequestContext.task_id` into the receiver invocation context for the BQ AA Plugin to stamp. The auditor join does not depend on it.

## Known limitations

- **Receiver task-level spans:** context-level stitch works now; per-span `a2a_task_id` is a separate follow-up that needs ADK runtime plumbing plus a plugin change.
- **`adk_session_id` response echo:** the response-metadata path may or may not populate for `A2AMessage`-shaped responses; the stitch above does not depend on it. Treat the `receiver_session_id_from_response` column as diagnostic only.
- **Cross-org security:** this is a one-project demo. Caller, receiver, and auditor datasets sit in the same project and the auditor redaction is enforced by curated projection tables, not IAM. Production cross-org redaction is a separate working group.
- **Streaming / long-running A2A:** out of scope. The demo uses synchronous request/response A2A.
- **A2A error paths:** failed remote calls may not produce `A2A_INTERACTION` rows. Auditor coverage of the error path is a follow-up.
- **Receiver extraction quality:** the receiver's response shape is enforced only by the system prompt. Loose prompts produce empty `decision_points`. The acceptance gate in `build_org_graphs.py` catches this.

## Files

```text
examples/a2a_joint_lineage_demo/
├── README.md                       ← this file
├── A2A_JOINT_LINEAGE.md            ← stitch contract + walkthrough
├── DATA_LINEAGE.md                 ← table-by-table source map
├── setup.sh                        ← bootstrap (datasets, .env, deps)
├── reset.sh                        ← drop caller + receiver + auditor datasets
├── render_queries.sh               ← render *.gql.tpl with .env values
├── .gitignore
├── campaigns.py                    ← three campaign briefs
├── caller_agent/                   ← supervisor with local tools + RemoteA2aAgent
│   ├── __init__.py
│   ├── agent.py
│   ├── prompts.py
│   └── tools.py
├── receiver_agent/                 ← pure-LLM governance reviewer
│   ├── __init__.py
│   ├── agent.py
│   └── prompts.py
├── run_receiver_server.py          ← custom Runner(..., plugins=[...]) + to_a2a()
├── smoke_receiver.py               ← receiver-row gate
├── run_caller_agent.py             ← caller campaigns + 3 acceptance gates
├── build_org_graphs.py             ← dual ContextGraphManager.build_context_graph
├── build_joint_graph.py            ← auditor projections + joint property graph
├── joint_property_graph.gql.tpl    ← Phase 1 5-node / 4-edge graph DDL
└── bq_studio_queries.gql.tpl       ← 5 paste-and-run BQ Studio blocks
```

Apache 2.0.
