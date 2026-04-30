# Decision Lineage Demo — Talk Track (BigQuery Studio)

Total runtime: about **4 minutes** of speaking, plus a few seconds
of query latency per block. Setup runs once before the demo on your
laptop. The narrated portion happens in BigQuery Studio.

## The data behind this demo

A real ADK media-planner agent — `agent/agent.py` — was run against
six different campaign briefs from `campaigns.py`. The
**BigQuery Agent Analytics Plugin** was attached to the runner, so
every span (INVOCATION, AGENT, LLM_REQUEST, LLM_RESPONSE, TOOL_*,
HITL) the agent produced is in `agent_events`.

The SDK's `ContextGraphManager.build_context_graph(...)` then ran
across every session: `AI.GENERATE` extracted business entities into
BizNodes and decisions/candidates into DecisionPoints + CandidateNodes
+ rejection rationale, the SQL-only edges and property-graph DDL
were emitted, and the result is `agent_context_graph` in
`decision_lineage_demo`.

Nothing on the demo path is hand-baked.

## Pre-roll setup (do once, before the demo)

```bash
cd examples/decision_lineage_demo
./setup.sh
```

Setup runs the live agent (3-7 minutes — six ADK invocations),
builds the graph via `AI.GENERATE` (30-90s), and writes
`bq_studio_queries.gql` with your project / dataset / first-session
inlined.

Have these open before you start:

1. A browser tab on BigQuery Studio for your demo project.
2. The `decision_lineage_demo` dataset expanded in the Explorer
   pane — `agent_context_graph` should be visible alongside the
   backing tables.
3. `bq_studio_queries.gql` open in a side editor so you can copy
   each block.

---

## Pre-roll narration (15s)

> "We've all seen agent demos that produce an answer and ask you to
> trust it. This is a demo about what's underneath. We ran a real
> ADK media-planner agent against six campaign briefs — Nike, Adidas,
> Puma, Reebok, Lululemon. The BQ AA Plugin recorded every span.
> The SDK then extracted decisions and candidates from the trace
> text. What's in BigQuery now is everything the agent reasoned
> through, queryable as a property graph."

---

## Block 1 — What did the SDK extract? (~45s)

Paste the five COUNT queries (1a → 1e) into BigQuery Studio tabs
and run sequentially.

> "Setup did two things. First it ran the live ADK agent six times,
> with the BQ AA Plugin attached — every span landed in
> `agent_events`. Then `build_context_graph` ran two `AI.GENERATE`
> calls across all sessions: one extracted business entities, the
> other extracted decisions and the candidates the agent weighed.
>
> Counts confirm the graph is populated: TechNodes for every span
> the plugin wrote, BizNodes for entities the model identified,
> DecisionPoints across the six sessions — typically several per
> session — and the matching CandidateNodes.
>
> Block 1e is the per-session breakdown — one row per session,
> showing how many decisions the model extracted from each
> campaign run."

---

## Block 2 — Visualize ONE session (~75s)

Paste Block 2 and run.

This returns paths from the chosen session's spans through to its
candidates. **BigQuery Studio renders this as an interactive graph
diagram.**

> "We scoped the visualization to a single session so the diagram
> stays readable. Each fan-out is one decision the agent made.
> Middle node is the DecisionPoint. Right side is every candidate
> the agent considered for that decision, labelled SELECTED or
> DROPPED on the edge.
>
> Pick a dropped candidate and click it. Its properties pane shows
> the score the agent assigned and the rejection rationale extracted
> verbatim from the agent's reasoning. None of this is hand-baked —
> it came from `AI.GENERATE` reading the LLM_RESPONSE text the
> plugin captured."

(To visualize a different campaign, swap the session id in Block 2
with any other id from Block 1e and re-run.)

---

## Block 3 — EU-audit traversal (~75s)

Paste Block 3 and run.

> "Block 2 was the visual. Block 3 is the same data as a table —
> the exact GQL the SDK ships as `mgr.get_eu_audit_gql(session_id)`.
> One row per (decision, candidate) the SDK extracted, with the
> rejection rationale on dropped ones.
>
> Read off two or three of the rationales — the wording is the
> demo. These are pulled out of trace text the agent produced; the
> SDK didn't make any of them up."

---

## Block 4 — Dropped candidates across the portfolio (~45s)

Paste Block 4 and run, then Block 4b.

> "Up to now we've been inside one session. Block 4 spans every
> session — one row per dropped candidate with rationale, sorted
> by session and decision.
>
> Block 4b is the operational metric: across every campaign the
> agent ran, how many candidates did it reject per decision type,
> and at what average score? That's the line product teams want
> to track over time."

---

## Block 5 — Close calls (~40s)

Paste Block 5 and run.

> "Last cut: every decision where the agent picked one candidate
> over another by less than 0.05 points. These are the rows worth
> a second look — the agent committed, but it was close. With this
> graph schema the question is one GQL query."

---

## Close (15s)

> "Three things to take away:
>
> 1. The graph is built end-to-end by the SDK from real ADK +
>    plugin traces. No instrumentation in the agent beyond
>    attaching the plugin to the runner.
> 2. Every audit query is GQL. BigQuery Studio renders both as
>    a graph diagram and as a table — pick whichever surface your
>    audience needs.
> 3. The schema composes with the rest of the SDK — same session
>    IDs, same span IDs, same agent_events row shape. If you've
>    already got the plugin in production, this is one
>    `build_context_graph` call away."
