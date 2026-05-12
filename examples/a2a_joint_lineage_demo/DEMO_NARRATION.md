# Demo Narration — A2A Joint Lineage

Target length: 5 minutes.

Audience: users evaluating whether BQ AA SDK can turn real multi-agent
runtime traces into audit-ready context graphs.

## Setup Line

> We are looking at two real ADK agents. A media-planning supervisor
> delegates audience-risk review to a remote governance agent over A2A.
> Both agents are instrumented with the BQ AA Plugin, both write trace
> rows to BigQuery, and the SDK materializes context graphs from those
> traces.

## Beat 1 — The Raw Evidence

Show the caller and receiver `agent_events` event-type counts.

> This is the raw evidence layer. The caller has normal ADK spans plus
> `A2A_INTERACTION` rows. The receiver has its own independent ADK spans.
> There is no shared process state here. The only bridge is the A2A
> context id that the caller sends and the receiver uses as its session
> id.

If a technical audience member asks about ADK 1.33's split-session
shape:

> Under ADK 1.33, the `RemoteA2aAgent` runs in its own caller-side
> session, so the `A2A_INTERACTION` row lives in a sibling caller
> session — not under the supervisor. The demo writes a small mapping
> table, `supervisor_a2a_invocations`, that pairs each supervisor's
> tool call to the corresponding A2A row by time window. The auditor
> reads through that mapping, so the runtime shift doesn't break the
> graph.

User takeaway:

> The system starts from actual runtime telemetry, not hand-authored
> audit records.

## Beat 2 — Two Local Graphs

Point at:

- `a2a_caller_demo.agent_context_graph`
- `a2a_receiver_demo.agent_context_graph`
- receiver `decision_points`
- receiver `candidates`

> The SDK builds a context graph for each side independently. That keeps
> ownership clean: the caller graph describes the supervisor run, and the
> receiver graph describes what the governance agent decided.

If decision extraction came up:

> Decision extraction goes through BigQuery's `AI.GENERATE` with a
> typed SQL `output_schema` so we get a structured `decisions` array
> back, not free-text JSON in a markdown fence. For this demo we also
> ship a strict-prompt fallback parser so a single flaky model call
> can't take the audit graph below threshold — if `AI.GENERATE` under-
> extracts, the demo re-parses the receiver's known response shape and
> rewrites `decision_points` and `candidates` deterministically.

User takeaway:

> Each agent can retain its own context graph without handing raw traces
> to another team.

## Beat 3 — The Auditor Stitch

Run Block 1 from `bq_studio_queries.gql`.

> Now the auditor layer performs one narrow stitch. It joins caller
> `a2a_context_id` to receiver `session_id`. The health check proves every
> remote call has a matching receiver session.

When `unstitched_calls = 0`:

> That zero matters. It means the auditor graph covers every remote
> delegation in the current campaign run.

User takeaway:

> A2A delegation becomes queryable lineage.

## Beat 4 — The Graph Picture

Run Block 2 and open the BigQuery Studio Graph tab.

> The graph now reads naturally: a campaign run delegated through a
> remote A2A invocation, and that invocation was handled by a receiver
> agent run.

Click nodes and show properties.

> The properties are identifiers and run metadata. They are enough to
> trace the handoff, but they do not expose the raw A2A request or raw
> receiver response in the auditor surface.

User takeaway:

> The graph is useful for inspection without being a raw data dump.

## Beat 5 — The Decision Trail

Run Block 3.

> This is where the trace becomes an audit answer. The receiver made a
> planning decision, weighed options, selected one, and dropped others.
> For dropped options, the graph carries a rejection rationale.

Point at a concrete row.

> A reviewer can ask, "why did the remote agent reject this audience?"
> and get the rationale from the extracted receiver trace.

User takeaway:

> The SDK context graph turns unstructured runtime behavior into a
> queryable decision trail.

## Beat 6 — One Campaign Explanation

Run Block 4.

> This is the right-to-explanation drilldown for one caller campaign. We
> can show all options the remote governance agent considered, which one
> it selected, which ones it dropped, the score, and the rationale.

User takeaway:

> The same graph supports both portfolio-level review and case-level
> explanation.

## Beat 7 — Redaction Proof

Run Block 5.

> Finally, the auditor projection deliberately excludes raw A2A request,
> raw A2A response, and full content columns. The result is zero rows,
> which is the proof that this curated surface is not exposing raw
> payload columns.

User takeaway:

> The demo separates trace collection from the auditor-facing surface.

## Beat 8 — Closing The Loop With An Analyst Agent

Run `./.venv/bin/python3 run_analyst_agent.py` (the one-command runner does this as step 6).

> So far the human has been reading the graph. This last step closes
> the loop: a third agent — the audit analyst — reads the joint graph
> back through four bounded BigQuery tools and answers natural-language
> audit questions. Its own reasoning trace lands in the analyst
> `agent_events` table, so the audit-of-the-audit lineage is itself a
> first-class BQ AA dataset.

Show the four canned answers the agent produces:

- "Is the joint audit graph healthy?" → tool: `stitch_health()`
- "What campaigns are in scope?" → tool: `list_campaigns()`
- "Walk me through the first campaign's audit path." → tool: `audit_campaign(...)`
- "What are the lowest-scored dropped options?" → tool: `find_governance_rejections(...)`

Then drop into ad-hoc mode:

```bash
./.venv/bin/python3 run_analyst_agent.py \
  "Why was anything dropped for the Adidas track campaign?"
```

User takeaway:

> The same data model used to record the trace is used to ask
> questions about the trace. An agent → BQAA → context graph → agent
> loop, not just agent → BQAA → human SQL.

## Close

> This is the end-to-end pattern: real agents, BQ AA Plugin telemetry,
> per-agent context graphs, one redacted joint graph for audit, and an
> analyst agent that reads that graph back in natural language. The
> key is that every step — including the analyst's reasoning — is
> built from runtime evidence in the same data model. It is a lineage
> surface over what the agents actually did, queryable both by humans
> (BigQuery Studio) and by other agents (the analyst's BQ tools).

## Presenter aside — robustness

Two design choices worth naming if a technical audience asks "what
happens when ADK or the model shifts under you":

- **ADK 1.33 split-session bridge.** ADK 1.33 changed
  `RemoteA2aAgent` to run in its own caller-side session, so the
  `A2A_INTERACTION` row no longer lives under the supervisor. The
  demo materializes a small caller-side mapping table,
  `supervisor_a2a_invocations`, that pairs each supervisor tool call
  to its `A2A_INTERACTION` row by per-tool-call time window:
  `supervisor_ts <= a2a_ts < next_supervisor_ts`, partitioned by
  `user_id`. The auditor's `remote_agent_invocations` projection
  reads through that mapping, so the `CallerCampaignRun ->
  RemoteAgentInvocation` edge in the property graph stays valid
  without touching the graph DDL. Gate G1.5 in
  `run_caller_agent.py` rejects empty or NULL pairings.
- **Receiver-extraction fallback.** Decision/candidate extraction
  uses `AI.GENERATE` with a SQL `output_schema` first. If the
  receiver-extraction gate sees fewer than the minimum required
  decisions/candidates, the demo re-parses receiver `LLM_RESPONSE`
  rows against the strict three-option contract from
  `receiver_agent/prompts.py` and rewrites `decision_points` and
  `candidates`. The receiver prompt is what makes the fallback
  reliable; if the prompt drifts, the fallback under-performs and
  the gate trips loudly rather than silently producing a thin
  graph.

Together those two design choices are why the demo can be re-run
against today's preview Gemini models and today's experimental ADK
A2A without the joint graph collapsing to zero.

## Questions To Invite

- Can we run this against our own ADK agents?
- Can the caller and receiver live in different projects?
- Can we add stricter redaction with IAM?
- Can we carry task-level A2A ids onto receiver spans?
- Can the context graph align decisions to an ontology?
- How does this hold up when ADK or the model changes shape?

The current answer:

> Yes for real ADK agents and BigQuery-backed context graphs today. The
> cross-project IAM/redaction, receiver task-id propagation, and ontology
> alignment are follow-up tracks already split out from the implementation
> issue. The ADK 1.33 sub-session shape and AI.GENERATE flakiness are
> already absorbed by the supervisor↔A2A mapping table and the receiver
> prompt-contract fallback parser, respectively.
