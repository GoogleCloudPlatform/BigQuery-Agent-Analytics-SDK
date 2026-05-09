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

## Close

> This is the end-to-end pattern: real agents, BQ AA Plugin telemetry,
> per-agent context graphs, and one redacted joint graph for audit. The
> key is that the graph is built from runtime evidence, so the demo is
> not just a dashboard. It is a lineage surface over what the agents
> actually did.

## Questions To Invite

- Can we run this against our own ADK agents?
- Can the caller and receiver live in different projects?
- Can we add stricter redaction with IAM?
- Can we carry task-level A2A ids onto receiver spans?
- Can the context graph align decisions to an ontology?

The current answer:

> Yes for real ADK agents and BigQuery-backed context graphs today. The
> cross-project IAM/redaction, receiver task-id propagation, and ontology
> alignment are follow-up tracks already split out from the implementation
> issue.
