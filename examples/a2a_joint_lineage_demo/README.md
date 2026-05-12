# A2A Joint Lineage Demo

Two BQ AA Plugin instances. Two `agent_events` tables. One A2A delegation in the middle. The caller's media-planning supervisor delegates audience-risk review to the receiver's governance agent over A2A; both sides write traces to BigQuery; the SDK materializes a context graph for each side independently; an auditor projection stitches them into a single joint property graph; **a third agent — the audit-analyst — closes the loop by reading that joint graph back through bounded BigQuery tools and answering natural-language audit questions**. The analyst's own reasoning trace also lands in BigQuery via the BQ AA Plugin, giving you audit-of-the-audit lineage in the same data model.

This bundle implements the plan in [issue #129](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/129) and ships in three slices:

- **PR 1** (merged) — caller / receiver agents, receiver server with custom-runner plugin attach, smoke gate, caller driver, dual `ContextGraphManager` materialization.
- **PR 2** (merged) — auditor projections (`build_joint_graph.py`), Phase 1 joint property graph (`joint_property_graph.gql.tpl`), 5 paste-and-run BigQuery Studio blocks (`bq_studio_queries.gql.tpl`), `render_queries.sh`, and the narrative docs.
- **PR 3** (merged) — SDK `A2A_INTERACTION` typed view (`adk_a2a_interactions`) for downstream consumers that want the A2A metadata without writing JSON extraction by hand.
- **Analyst loop** — `analyst_agent/` package + `run_analyst_agent.py`. Closes the demo loop: an ADK agent with four bounded BigQuery tools (`stitch_health`, `list_campaigns`, `audit_campaign`, `find_governance_rejections`) answers natural-language audit questions against the joint graph. The analyst's own traces land in `<ANALYST_DATASET>.agent_events`.

## Running it

```bash
./setup.sh
```

For the user-facing E2E demo, run:

```bash
./run_e2e_demo.sh
```

That script starts the receiver A2A server in the background, verifies
that receiver plugin rows land in BigQuery, runs the caller campaigns,
builds the caller and receiver SDK context graphs, builds the auditor
joint graph, renders `bq_studio_queries.gql`, **runs the analyst agent
against the canned audit question set (closes the loop)**, and stops
the receiver server on exit.

For debugging, the same flow can be run manually in two terminals:

```bash
# Terminal A — long-lived receiver server
./.venv/bin/python3 run_receiver_server.py

# Terminal B — smoke + caller campaigns + dual graph + auditor graph
./.venv/bin/python3 smoke_receiver.py
./.venv/bin/python3 run_caller_agent.py
./.venv/bin/python3 build_org_graphs.py
./.venv/bin/python3 build_joint_graph.py
./.venv/bin/python3 run_analyst_agent.py     # canned questions
# or:
./.venv/bin/python3 run_analyst_agent.py "Why was X rejected for campaign Y?"
```

`build_joint_graph.py` already runs `./render_queries.sh` itself, so `bq_studio_queries.gql` is on disk after that command. Re-run `./render_queries.sh` only if you edit `.env` (e.g. swap `DEMO_CALLER_SESSION_ID` to inspect a different campaign) or change the `*.gql.tpl` templates.

`run_analyst_agent.py` runs four canned questions by default (one per analyst tool); pass any free-text question(s) as positional args to ask ad-hoc.

**For a clean verification run: `./reset.sh && ./setup.sh`, then run the two-terminal flow above.** `reset.sh` drops the caller, receiver, auditor, and analyst datasets entirely; `setup.sh` recreates them. The plugin creates tables, not datasets, so a bare `./reset.sh` would leave the demo unable to write. Resetting up front guarantees `build_org_graphs.py`'s discover-all-sessions pass reflects only the current campaigns.

The auditor-side projections built by `build_joint_graph.py` are scoped to the current campaign run regardless (the chain `caller_campaign_runs → remote_agent_invocations → receiver_runs → receiver decisions/options` filters out anything not matched to a current caller session). Stale rows from prior runs still accumulate in the **source** layers — `<CALLER_DATASET>.agent_events`, `<RECEIVER_DATASET>.agent_events`, and the per-org `decision_points` / `candidates` tables `build_org_graphs.py` writes — and remain visible in the BQ Studio Explorer for those datasets. Skip the reset if you're iterating and want that source-side history kept; reset if you want a guaranteed-clean per-org and acceptance-gate baseline.

After `run_e2e_demo.sh` succeeds (or after the manual two-terminal flow returns zero on every step), you have:

- `<PROJECT>.a2a_caller_demo.agent_events` — caller-side spans, including `A2A_INTERACTION` rows (ADK 1.33: written under the `audience_risk_reviewer` sub-session, not the supervisor's session — see `A2A_JOINT_LINEAGE.md` for the mapping)
- `<PROJECT>.a2a_caller_demo.campaign_runs` — campaign ↔ caller-session map
- `<PROJECT>.a2a_caller_demo.supervisor_a2a_invocations` — supervisor↔A2A-sub-session mapping that bridges the ADK 1.33 split-session telemetry shape, written by `run_caller_agent.py` after caller flush
- `<PROJECT>.a2a_caller_demo.{extracted_biz_nodes,decision_points,candidates,…}` — caller graph backing tables
- `<PROJECT>.a2a_caller_demo.agent_context_graph` — caller property graph
- `<PROJECT>.a2a_receiver_demo.agent_events` — receiver-side spans
- `<PROJECT>.a2a_receiver_demo.{extracted_biz_nodes,decision_points,candidates,…}` — receiver graph backing tables
- `<PROJECT>.a2a_receiver_demo.agent_context_graph` — receiver property graph
- `<PROJECT>.a2a_auditor_demo.{caller_campaign_runs,remote_agent_invocations,receiver_runs,receiver_planning_decisions,receiver_decision_options,joint_a2a_edges}` — auditor projections (redacted)
- `<PROJECT>.a2a_auditor_demo.a2a_joint_context_graph` — joint property graph spanning both orgs
- `<PROJECT>.a2a_analyst_demo.agent_events` — analyst-agent traces (one ADK session per question, including the tool-call lineage that produced each answer)

Open BigQuery Studio in the project, navigate to `a2a_auditor_demo`, and paste blocks from `bq_studio_queries.gql` (rendered by `render_queries.sh`). See [`A2A_JOINT_LINEAGE.md`](A2A_JOINT_LINEAGE.md) for the per-block walkthrough.

For a presentation-ready path, use:

- [`BQ_STUDIO_WALKTHROUGH.md`](BQ_STUDIO_WALKTHROUGH.md) — click-by-click BigQuery Studio guide
- [`DEMO_NARRATION.md`](DEMO_NARRATION.md) — 5-minute talk track for users
- [`DATA_LINEAGE.md`](DATA_LINEAGE.md) — table-by-table source map

## Stitch contract

The auditor stitches caller and receiver at **context/session level**:

```text
caller.agent_events.attributes.a2a_metadata."a2a:context_id"
  ==
receiver.agent_events.session_id
```

This works because [`adk-python`'s `convert_a2a_request_to_agent_run_request`](https://github.com/google/adk-python/blob/main/src/google/adk/a2a/converters/request_converter.py) sets `session_id := request.context_id`, and `run_receiver_server.py` runs an `InMemorySessionService` that honors explicit session ids. `build_joint_graph.py`'s `joint_a2a_edges` projection materializes the join.

**Caller-side mapping (ADK 1.33).** `RemoteA2aAgent` in `google-adk` 1.33 spawns its own caller-side `InvocationContext` with a fresh `session_id`, so the `A2A_INTERACTION` row lives in a sibling caller-side session (`agent='audience_risk_reviewer'`) — *not* under the supervisor session that triggered the delegation. The two sessions share only `user_id` and `app_name`. `run_caller_agent.py` therefore materializes an explicit `supervisor_a2a_invocations` mapping table (per-tool-call time-window pairing within the current run) and `build_joint_graph.remote_agent_invocations` reads from that mapping, so the auditor's `CallerCampaignRun -> RemoteAgentInvocation` edge keeps pointing at the supervisor session id. See [`A2A_JOINT_LINEAGE.md`](A2A_JOINT_LINEAGE.md#adk-133-sub-session-shape) for the full mapping schema and acceptance-gate G1.5.

Per-span `a2a_task_id` propagation onto receiver spans is **deferred to a follow-up** because current ADK request conversion does not plumb `RequestContext.task_id` into the receiver invocation context for the BQ AA Plugin to stamp. The auditor join does not depend on it.

## Known limitations

- **Gemini 3.x preview models — `global`-only, full-URL only.** Verified live against Vertex AI + BigQuery `AI.GENERATE` in May 2026:
  - The agent defaults to `gemini-3.1-pro-preview` at **`global`** (`DEMO_AGENT_LOCATION=global`). The model is **not** published at `us-central1` or any other regional location — a regional lookup returns 404.
  - The BQ AI.GENERATE endpoint defaults to the full HTTPS URL `https://aiplatform.googleapis.com/v1/projects/<PROJECT_ID>/locations/global/publishers/google/models/gemini-3-flash-preview`. The model ID is `gemini-3-flash-preview` (not `gemini-3-flash`); the BQML simple-name resolver does **not** recognize either short form during the Gemini 3 preview, so the full URL is required. `setup.sh` resolves `<PROJECT_ID>` at .env-write time.
  - Both are preview models — confirm Vertex AI preview access on the demo project before running.
  - **To fall back to stable models** on a project without preview access, override all three:
    ```bash
    DEMO_AGENT_LOCATION=us-central1
    DEMO_AGENT_MODEL=gemini-2.5-pro
    DEMO_AI_ENDPOINT=gemini-2.5-flash
    ```
    The `gemini-2.5-*` simple names work directly with BQML's resolver.
- **Receiver task-level spans:** context-level stitch works now; per-span `a2a_task_id` is a separate follow-up that needs ADK runtime plumbing plus a plugin change.
- **`adk_session_id` response echo:** the response-metadata path may or may not populate for `A2AMessage`-shaped responses; the stitch above does not depend on it. Treat the `receiver_session_id_from_response` column as diagnostic only.
- **Cross-org security:** this is a one-project demo. Caller, receiver, and auditor datasets sit in the same project and the auditor redaction is enforced by curated projection tables, not IAM. Production cross-org redaction is a separate working group.
- **Streaming / long-running A2A:** out of scope. The demo uses synchronous request/response A2A.
- **A2A error paths:** failed remote calls may not produce `A2A_INTERACTION` rows. Auditor coverage of the error path is a follow-up.
- **Receiver extraction quality:** the receiver's response shape is enforced by the system prompt. `build_org_graphs.py` first uses SDK `AI.GENERATE` extraction with typed SQL-style `output_schema`; if that returns too few rows for the live demo, it falls back to a deterministic parser for the receiver's strict three-option response shape and then reruns the acceptance gate.

## Files

```text
examples/a2a_joint_lineage_demo/
├── README.md                       ← this file
├── A2A_JOINT_LINEAGE.md            ← stitch contract + walkthrough
├── DATA_LINEAGE.md                 ← table-by-table source map
├── BQ_STUDIO_WALKTHROUGH.md        ← click-by-click BigQuery Studio guide
├── DEMO_NARRATION.md               ← 5-minute presenter talk track
├── setup.sh                        ← bootstrap (datasets, .env, deps)
├── run_e2e_demo.sh                 ← one-command live demo runner
├── reset.sh                        ← drop caller + receiver + auditor + analyst datasets
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
├── analyst_agent/                  ← audit-analyst that closes the loop
│   ├── __init__.py
│   ├── agent.py
│   ├── prompts.py
│   └── tools.py                    ← 4 bounded BQ tools over the joint graph
├── run_receiver_server.py          ← custom Runner(..., plugins=[...]) + to_a2a()
├── smoke_receiver.py               ← receiver-row gate
├── run_caller_agent.py             ← caller campaigns + 3 acceptance gates
├── build_org_graphs.py             ← dual ContextGraphManager.build_context_graph
├── build_joint_graph.py            ← auditor projections + joint property graph
├── run_analyst_agent.py            ← natural-language audit Q&A loop
├── joint_property_graph.gql.tpl    ← Phase 1 5-node / 4-edge graph DDL
└── bq_studio_queries.gql.tpl       ← 5 paste-and-run BQ Studio blocks
```

Apache 2.0.
