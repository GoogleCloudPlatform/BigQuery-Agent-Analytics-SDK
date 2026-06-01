# Ask Your Agent's Decisions in Plain English

*A Conversational Analytics-first guide to the BigQuery agent context graph.*

Your AI agents make decisions all day: which loan to approve, which maintenance
window to schedule, which candidate to pick. The BigQuery Agent Analytics SDK
turns that raw event stream into a queryable **decision graph** (requests,
the options each request weighed, and the outcome that was committed).

Most teams reach for SQL to query that graph. This guide takes the other path
first: **ask in plain English with Conversational Analytics, and drop to graph
query language (GQL) only when you need the exact lineage.** Same graph, two
front doors.

## Who this is for

The person asking the question is usually *not* the person who wrote the
pipeline: an operations lead auditing why a request was denied, a PM checking
how often an agent defers, a risk analyst spot-checking low-confidence
approvals. They think in questions, not joins.

Conversational Analytics (CA) lets them ask the graph directly. The GQL section
at the end is for when an answer needs to become a saved query, a dashboard, or
an audit artifact.

> This is a higher-level companion to the
> [Periodic Materialization codelab](../codelabs/periodic_materialization.md),
> which walks through building the graph step by step. Start there if you want
> the full setup with every command; come back here for the business-reader
> workflow.

## One-time setup: a graph worth asking about

CA is only as good as the data behind it, so seed a realistic corpus first, then
materialize it. The SDK ships a generator that produces production-shaped
telemetry: multiple agents and users over several days, with successful, failed,
truncated, and abandoned (orphaned) sessions.

```bash
export PROJECT_ID="your-project-id"
export DATASET="agent_decisions"

# 1. Seed ~100 realistic decision sessions (deterministic with --seed).
bqaa seed-events \
    --project-id "$PROJECT_ID" --dataset-id "$DATASET" \
    --scenario decision-realistic --seed 42

# 2. Materialize the decision graph from the events.
#    The ontology.yaml / binding.yaml bundle comes from the codelab; render the
#    binding's ${PROJECT_ID}/${DATASET} placeholders with envsubst first.
bqaa context-graph \
    --project-id "$PROJECT_ID" --dataset-id "$DATASET" \
    --ontology ontology.yaml --binding binding.rendered.yaml \
    --lookback-hours 80
```

The seed step reports the exact mix it wrote:

```json
{
  "scenario": "decision-realistic",
  "sessions": 100,
  "session_outcome_counts": {"success": 70, "failed": 10, "orphaned": 10, "truncated": 10}
}
```

The 10 **orphaned** sessions never emitted a terminal event, so they are not
materialized into the graph (an agent that never finished has no committed
decision). That is intentional, and it is one of the things CA can surface for
you below.

> Need the ontology/binding bundle, the graph-table DDL, and the property-graph
> definition? They live in
> [`examples/codelab/periodic_materialization/`](../../examples/codelab/periodic_materialization/),
> and the [codelab](../codelabs/periodic_materialization.md) renders and applies
> them end to end.

### Point Conversational Analytics at the graph

In the Google Cloud console, open **Conversational Analytics**, create a data
agent, and connect it to your `$DATASET`. Add the materialized tables
(`decision_request`, `decision_option`, `decision_outcome`) and the raw
`agent_events` table as sources. Give the agent a one-line description of the
domain, for example: *"Each row is an AI agent's decision: a request, the
options it weighed with confidence scores, and the outcome it committed."*

That context is what lets CA translate "low-confidence approvals" into the right
filter without anyone writing SQL.

## Part 1 — Ask in plain English

These are the questions a business reader actually asks. Type each one into the
Conversational Analytics chat over your data agent. Each screenshot below shows
CA's answer over the seeded `decision-realistic` corpus.

> **Capturing the screenshots:** ask CA the exact question in the caption, then
> drop the screenshot into the referenced path. Keep the question visible in the
> shot so readers see the plain-English input and the answer together.

### 1. "How many decisions did each agent make, and how many failed?"

The opening question for any operations review: volume and failure rate per
agent. CA groups by `agent` and counts outcomes without anyone naming a column.

![CA: decisions and failures per agent](./images/ca-01-decisions-per-agent.png)

### 2. "Show me the approvals with confidence below 0.5"

The risk-analyst question. Low-confidence decisions that still went through are
exactly what a reviewer wants to see. CA filters `decision_option.confidence`
and joins to the committed outcome.

![CA: low-confidence approvals](./images/ca-02-low-confidence-approvals.png)

### 3. "What did the budget-allocator agent decide, and how confident was it?"

Drill into one agent. CA returns the requests it handled, the option it
committed, and the confidence it assigned each alternative, so you can see how
close the call was.

![CA: budget-allocator decisions and confidence](./images/ca-03-budget-allocator-confidence.png)

### 4. "Which requests never reached a decision?"

The abandoned-work question. These are the orphaned sessions: events arrived,
but no terminal `AGENT_COMPLETED` was ever written, so nothing was committed. CA
finds the sessions in `agent_events` with no matching outcome.

![CA: requests with no committed decision](./images/ca-04-orphaned-requests.png)

### 5. "What are the most common decision outcomes?"

The shape-of-the-business question. A simple distribution over
`decision_outcome.status` that tells you whether your agents mostly approve,
reject, or defer.

![CA: outcome distribution](./images/ca-05-outcome-distribution.png)

## Part 2 — Inspect the GQL

Plain English is the fast path. When you need the *exact* lineage to live in a
saved query, a scheduled report, or an audit log, drop to the graph directly.

The materializer stitches the decision tables into a BigQuery property graph
(`agent_decisions_graph`). The same "what did this request weigh, and what came
of it" question from CA above is one `GRAPH_TABLE` traversal:

```sql
SELECT request, considered, score, outcome
FROM GRAPH_TABLE (
  agent_decisions.agent_decisions_graph
  MATCH
    (req:DecisionRequest) -[:evaluatesOption]-> (opt:DecisionOption),
    (req)                 -[:resultedIn]->      (out:DecisionOutcome)
  COLUMNS (
    req.request_id   AS request,
    req.request_text AS question,
    opt.option_label AS considered,
    opt.confidence   AS score,
    out.status       AS outcome
  )
)
ORDER BY request, score DESC;
```

Real output for one request and the options it weighed (from the seeded graph):

```
request     question                          considered  score  outcome
----------  --------------------------------  ----------  -----  ----------
req-069f14  Should we schedule maintenance?   approve      0.91  committed
req-069f14  Should we schedule maintenance?   reject       0.75  committed
req-069f14  Should we schedule maintenance?   escalate     0.75  committed
req-069f14  Should we schedule maintenance?   delegate     0.50  committed
req-069f14  Should we schedule maintenance?   hold         0.48  committed
req-069f14  Should we schedule maintenance?   defer        0.33  committed
```

The agent weighed six options and committed the highest-confidence one
(`approve`, 0.91). Because the default extractor uses `AI.GENERATE`, the exact
entities can vary run to run; pass `--extraction-mode=compiled-only` for
reproducible output.

Now the question is portable: schedule it, alert on it, or join it into a
dashboard. CA found the shape; GQL pins it down.

## When to use which

| Use Conversational Analytics when… | Use GQL when… |
|---|---|
| Exploring: "is anything weird in last week's denials?" | The answer becomes a saved/scheduled query |
| A business reader needs an answer without SQL | You need exact, reproducible lineage for an audit |
| You're iterating on the question itself | You're wiring the result into a dashboard or alert |
| One-off spot checks | Automation and monitoring |

Start in plain English. Reach for GQL the moment an answer needs to outlive the
conversation.

## Related

- [Periodic Materialization codelab](../codelabs/periodic_materialization.md) — build the graph step by step.
- [`bqaa seed-events`](../../examples/codelab/periodic_materialization/README.md) — the synthetic data generator, including the `decision-realistic` scenario used here.
