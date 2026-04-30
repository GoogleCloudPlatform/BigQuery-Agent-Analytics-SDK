# Decision Lineage Demo — Talk Track (BigQuery Studio)

Total runtime: about **4 minutes** of speaking, plus a few seconds
of query latency per block. Setup runs once before the demo on your
laptop. The narrated portion happens in BigQuery Studio.

## The trace

One media-planner agent invocation, **23 plugin-shape spans**:
INVOCATION_STARTING / AGENT_STARTING bookends, a user message, five
LLM_REQUEST→LLM_RESPONSE pairs (one per decision the planner makes),
three TOOL_STARTING→TOOL_COMPLETED pairs linked by `tool_call_id`,
an HITL final-approval request and confirmation, AGENT_COMPLETED,
and INVOCATION_COMPLETED.

The seeded narrative covers five decisions with three candidates
each (one SELECTED, two DROPPED with explicit rejection rationale).
**The actual decision and candidate counts in the graph depend on
what `AI.GENERATE` extracts at build time** — typically all five
decisions and fifteen candidates, occasionally one fewer when the
model consolidates two decisions or skips a borderline case. The
talk track below reads naturally either way.

## Pre-roll setup (do once, before the demo)

```bash
cd examples/decision_lineage_demo
./setup.sh
```

Setup seeds traces, runs the SDK extraction pipeline (two
`AI.GENERATE` calls — biz entities then decisions, 30-60s), and
writes `bq_studio_queries.gql` with your project / dataset / session
already substituted in.

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
> trust it. This is a demo about what's underneath. The agent we're
> looking at planned a Nike Summer Run media campaign — multiple
> decisions, the alternatives it considered for each, and the
> reasons it rejected the dropped ones. The extracted facts are
> now queryable rows in BigQuery, pulled from the agent's own
> traces by the SDK."

---

## Block 1 — What did the SDK extract? (~30s)

Paste the four COUNT queries from Block 1 in `bq_studio_queries.gql`
(1a → 1d) into a single BigQuery Studio tab and run them
sequentially.

> "Setup ran the SDK's `build_context_graph` pipeline against 23
> seeded plugin-shape trace rows. Two `AI.GENERATE` calls did all
> the downstream work: one extracted business entities the agent
> reasoned about, the other extracted decisions and the candidates
> the agent weighed.
>
> Counts confirm the graph is populated: 23 TechNodes for the
> spans you'd expect, plus a non-zero count for each of the new
> layers — BizNode, DecisionPoint, CandidateNode. The exact
> numbers depend on what the model returned this run, but every
> decision the agent made is here, with each candidate it
> considered. No manual instrumentation in the agent."

---

## Block 2 — Visualize the agent's reasoning (~80s)

Paste Block 2 from `bq_studio_queries.gql` into a new tab and run.

This returns paths from each span that made a decision through to
its candidates. **BigQuery Studio renders this as an interactive
graph diagram.**

> "This is the same property graph we just counted, rendered as a
> diagram. One fan-out per decision: audience selection, budget
> allocation, creative selection, channel strategy, launch
> scheduling — whatever the model extracted from the trace this
> run. The middle node is the DecisionPoint. The fan-out is every
> candidate the agent considered, labelled SELECTED or DROPPED on
> the edge.
>
> Pick a dropped candidate — say, the TikTok Spark Ads node. Its
> properties pane shows the score the agent assigned and the
> rejection rationale extracted verbatim from the agent's
> reasoning: 'channel overlap with Instagram Reels for the same
> demographic; Q1 retention 18% lower at matched spend.'
>
> Two things to notice. First, this is the agent's reasoning, not a
> highlight reel — the dropped candidates are first-class nodes
> with the same edge weight as the selected ones. Second, every
> single one of these nodes was extracted from raw trace text by
> `AI.GENERATE`. Drop in the BQ AA Plugin and the SDK does the rest."

---

## Block 3 — EU-audit traversal (~80s)

Paste Block 3 from `bq_studio_queries.gql` and run.

Output is a table: one row per (decision, candidate) the SDK
extracted, with rationale on dropped ones.

> "This GQL is what the SDK ships as
> `mgr.get_eu_audit_gql(session_id=...)` — the audit traversal a
> compliance reviewer would run.
>
> For the audience decision: `Athletes 18-35` was selected at the
> top score. The agent considered `Casual fitness enthusiasts` and
> dropped it for falling below the conversion threshold from the Q1
> cohort study. It considered `General sports fans` and dropped it
> on a brand-alignment audit.
>
> For the budget decision: `Instagram Reels` selected. `TikTok Spark
> Ads` dropped for channel overlap on the same demographic.
> `YouTube Shorts` dropped on CPM ceiling — 32% above the cap.
>
> For the creative theme: `Just Do It - Summer Edition` selected on
> Q1 brand-recall lift. `Run Past Your Limits` dropped under the
> recall floor. `The Daily Mile` dropped on tone alignment.
>
> Channel strategy and scheduling round out the audit — Instagram-led
> with TV reinforcement won over a TV-led plan that would have
> needed an incremental $200K, and the May 27 launch beat a July 4
> launch where CPMs would forecast 25% above ceiling.
>
> A reviewer asking 'what did the agent reject and why?' has the
> answer in one query. Not a screenshot, not a Slack thread — a
> reproducible GQL traversal against the audit record."

---

## Block 4 — Just the rejections (detail view) (~45s)

Paste Block 4 from `bq_studio_queries.gql` and run.

Same shape as `mgr.get_dropped_candidates_gql()` — Block 3 narrowed
to just the rejections.

> "Block 3 gave us the full audit. Block 4 narrows to just the
> rejections — one row per dropped candidate, ordered by decision
> and score. The seeded narrative gave each decision two dropped
> candidates, so you'll see roughly twice as many rows as
> DecisionPoints.
>
> Today this is one session. Drop the `session_id` filter and pivot
> on a date range and you fan this out across every agent run — or
> run the optional Block 4b instead, which aggregates this same
> predicate by decision type with `COUNT` and `AVG(score)`. That's
> the line product teams want to track."

---

## Close (15s)

> "Three things to take away:
>
> 1. The graph is built by the SDK from agent traces — no agent-side
>    instrumentation beyond the BQ AA Plugin.
> 2. The audit query is one method on the SDK, served as GQL.
>    BigQuery Studio renders it both as a graph diagram and as a
>    table — pick whichever surface your audience needs.
> 3. The schema composes with the rest of the SDK — same session
>    IDs, same span IDs, same agent_events row shape your ADK
>    plugin already writes."
