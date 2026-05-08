# A2A Joint Lineage Demo (PR 1)

Two BQ AA Plugin instances. Two `agent_events` tables. One A2A delegation in the middle. The caller's media-planning supervisor delegates audience-risk review to the receiver's governance agent over A2A; both sides write traces to BigQuery; the SDK materializes a context graph for each side independently.

This is the **PR 1 slice** of [issue #129](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/129). It demonstrates the trace-and-graph contract. PR 2 adds the auditor projection layer and the joint property graph.

## What ships in PR 1

- **Two ADK agents** (`caller_agent/`, `receiver_agent/`) — caller is a media-planning supervisor with four local tools and a `RemoteA2aAgent` delegation; receiver is pure-LLM and produces an extractor-friendly response shape.
- **Receiver server** (`run_receiver_server.py`) — uses `to_a2a()` with an explicit `Runner(plugins=[...])` so the BQ AA Plugin actually attaches. The default-runner path drops plugins on the floor; this is the most common failure mode the plan calls out.
- **Smoke gate** (`smoke_receiver.py`) — sends one A2A request and asserts receiver `agent_events` rows exist before caller campaigns run. Catches the missing-plugin case immediately.
- **Caller driver** (`run_caller_agent.py`) — runs three campaign briefs through the supervisor, writes `caller_campaign_runs`, then runs three acceptance gates (A2A_INTERACTION present, receiver rows nonzero, ≥1 caller↔receiver session match).
- **Dual graph materialization** (`build_org_graphs.py`) — runs `ContextGraphManager.build_context_graph(... include_decisions=True)` over each org's dataset independently. Asserts the receiver extracted ≥3 decisions and ≥9 candidates (the prompt-contract gate).

## What does not ship in PR 1

- Auditor projections (`build_joint_graph.py`).
- `joint_property_graph.gql.tpl` and the joint property graph.
- BigQuery Studio walkthrough blocks.
- `A2A_JOINT_LINEAGE.md`, `DATA_LINEAGE.md`, `SETUP_NEW_PROJECT.md`.

These land in PR 2 once the trace shape is verified.

## Running it

```bash
./setup.sh
```

Then in two terminals:

```bash
# Terminal A — long-lived receiver server
./.venv/bin/python3 run_receiver_server.py

# Terminal B — smoke + caller campaigns + dual graph build
./.venv/bin/python3 smoke_receiver.py
./.venv/bin/python3 run_caller_agent.py
./.venv/bin/python3 build_org_graphs.py
```

After all three commands return zero, you have:

- `<PROJECT>.a2a_caller_demo.agent_events` — caller-side spans, including `A2A_INTERACTION` rows
- `<PROJECT>.a2a_caller_demo.campaign_runs` — campaign ↔ caller-session map
- `<PROJECT>.a2a_caller_demo.{extracted_biz_nodes,decision_points,candidates,...}` — caller graph backing tables
- `<PROJECT>.a2a_caller_demo.agent_context_graph` — caller property graph
- `<PROJECT>.a2a_receiver_demo.agent_events` — receiver-side spans
- `<PROJECT>.a2a_receiver_demo.{extracted_biz_nodes,decision_points,candidates,...}` — receiver graph backing tables
- `<PROJECT>.a2a_receiver_demo.agent_context_graph` — receiver property graph

The auditor's joint graph over both is the PR 2 deliverable.

## Stitch contract

Phase 1 stitches caller and receiver at **context/session level**:

```text
caller.agent_events.attributes.a2a_metadata."a2a:context_id"
  ==
receiver.agent_events.session_id
```

This works because [`adk-python`'s `convert_a2a_request_to_agent_run_request`](https://github.com/google/adk-python/blob/main/src/google/adk/a2a/converters/request_converter.py) sets `session_id := request.context_id`, and `run_receiver_server.py` runs an `InMemorySessionService` that honors explicit session ids.

Per-span `a2a_task_id` propagation onto receiver spans is **deferred to a follow-up** because current ADK request conversion does not plumb `RequestContext.task_id` into the receiver invocation context for the BQ AA Plugin to stamp. PR 1 does not depend on it.

## Known limitations

- **Receiver task-level spans:** context-level stitch works now; per-span `a2a_task_id` is a separate follow-up that needs ADK runtime plumbing plus a plugin change.
- **`adk_session_id` response echo:** the response-metadata path may or may not populate for `A2AMessage`-shaped responses; the stitch above does not depend on it. Treat as diagnostic.
- **Cross-org security:** this is a one-project demo. The caller and receiver datasets sit in the same project and the auditor convention is enforced by curated views, not IAM. Production cross-org redaction is a separate working group.
- **Streaming / long-running A2A:** out of scope. The demo uses synchronous request/response A2A.
- **A2A error paths:** failed remote calls may not produce `A2A_INTERACTION` rows. Auditor coverage of the error path is a follow-up.
- **Receiver extraction quality:** the receiver's response shape is enforced only by the system prompt. Loose prompts produce empty `decision_points`. The acceptance gate in `build_org_graphs.py` catches this.

## Files

```text
examples/a2a_joint_lineage_demo/
├── README.md                  ← this file
├── setup.sh                   ← bootstrap (datasets, .env, deps)
├── reset.sh                   ← drop caller + receiver datasets
├── .gitignore
├── campaigns.py               ← three campaign briefs
├── caller_agent/              ← supervisor with local tools + RemoteA2aAgent
│   ├── __init__.py
│   ├── agent.py
│   ├── prompts.py
│   └── tools.py
├── receiver_agent/            ← pure-LLM governance reviewer
│   ├── __init__.py
│   ├── agent.py
│   └── prompts.py
├── run_receiver_server.py     ← custom Runner(..., plugins=[...]) + to_a2a()
├── smoke_receiver.py          ← receiver-row gate
├── run_caller_agent.py        ← caller campaigns + 3 acceptance gates
└── build_org_graphs.py        ← dual ContextGraphManager.build_context_graph
```

Apache 2.0.
