# Periodic materialization for BigQuery Agent Analytics: keep your agent decision graph fresh, every six hours

*BigQuery property graphs and BigQuery Conversational Analytics are in Preview on Google Cloud. The BigQuery Agent Analytics Plugin and SDK are generally available. Examples in this post use synthetic data.*

Today we are making it dramatically easier to keep a BigQuery property graph of your AI agent's decisions current with the events your agents are actually producing — on a schedule, with one Cloud Run Job, and no new database. The new periodic-materialization deploy in the BigQuery Agent Analytics SDK takes the events captured by the BigQuery Agent Analytics Plugin and writes them into the property graph you already defined, every N hours, in the same BigQuery project. Events stay in a read-only events dataset; the graph lives in a separate read/write graph dataset; the runtime service account is granted exactly the narrow IAM each side needs.

This is the small operational change that converts agent observability from "engineering will dig through logs" into "the audit team asks the question and gets an answer in seconds."

## The business case for an answer on the same day

Autonomous agents are increasingly making decisions that cost real money or carry real regulatory weight: credit declines, prior-authorization denials, marketing budget pulls, supplier picks, refund grants, access approvals. The events stream is the easy part — the BigQuery Agent Analytics Plugin already captures every decision into a sixteen-column `agent_events` table the moment your agent boots, no code changes anywhere else.

The hard part is the next question. The risk officer wants to know why agent A-1188 declined customer 4029-7's loan on March 11. The compliance team wants the rationale categories that drove last quarter's denials. The CFO wants the total tokens-and-dollars cost of the marketing agent's autonomous moves. Each of these is a *traversal*: the context the agent saw, the decision point it was at, the options it weighed, the outcome it committed, and the rationale behind it.

A trained engineer can SQL their way to most traversals — given two weeks. The audit committee meets on Thursday. The state regulator is on the phone Monday morning. The cost of the engineering-led answer isn't the engineering hour — it's the decision the executive can't defend until the answer arrives.

The new periodic-materialization deploy moves the join from the audit-hour to a background schedule. Your agent's events keep flowing into the events dataset; your property graph stays fresh in the graph dataset next door; the audit question becomes a single query. Same BigQuery project. Same IAM. Same billing.

## How periodic materialization works

The SDK ships three building blocks. You provide one input — the property graph that describes your decision domain — and the deploy script handles the rest.

**1. Events flow in continuously.** The BigQuery Agent Analytics Plugin, which is generally available and a drop-in for ADK, writes every agent event to `agent_events` via the BigQuery Storage Write API. The full event-type catalog (decision events, LLM requests and responses, tool calls, human-in-the-loop approvals, agent-to-agent interactions) lands in one sixteen-column table, with auto-generated typed views per event type. The plugin uses OpenTelemetry-compatible identifiers when your team has OTel configured and works standalone otherwise.

```python
from google.adk.plugins import BigQueryAgentAnalyticsPlugin

plugin = BigQueryAgentAnalyticsPlugin(
    project_id="your-project",
    dataset_id="agent_analytics",
)
runner = Runner(agent=root_agent, plugins=[plugin])
```

That is the entire instrumentation surface. Drop the plugin in; rows show up in `agent_events`.

**2. You provide the property graph that describes your decisions.** A property graph in BigQuery is a set of node tables, a set of edge tables, and a `CREATE OR REPLACE PROPERTY GRAPH` statement that ties them together. You write the schema once (`property_graph.sql`) and apply it to your dataset:

```bash
bq query --use_legacy_sql=false < property_graph.sql
```

The graph captures your domain language: what the agent saw, what it decided, what options were on the table, what the outcome was. This is the only artifact you author. Engineering teams that already think in graphs can write it directly; teams new to graphs can start from the migration v5 demo in the SDK repository and edit.

**3. The SDK runs a Cloud Run Job every N hours that materializes events into your graph.** One command — `deploy_cloud_run_job.sh` — creates a Cloud Run Job that runs `bqaa-materialize-window`, a Cloud Scheduler trigger that fires on a cron expression you pick, and a runtime service account with narrow IAM (events read-only, graph read/write). The `--smoke` flag runs the job once after deploy and tails the logs so you know the deploy works end-to-end.

```bash
./deploy_cloud_run_job.sh \
    --project your-project --region us-central1 \
    --events-dataset agent_analytics \
    --graph-dataset graph_v5 \
    --schedule "0 */6 * * *" \
    --smoke
```

Every six hours the job:

- Scans the last six hours of `agent_events`.
- Picks out the sessions that completed in that window.
- Extracts the structured shape your property graph expects, per session.
- Materializes the entity and relationship tables.
- Writes a structured JSON report to Cloud Logging — `jsonPayload.ok`, `jsonPayload.sessions_materialized`, per-table row counts.

A checkpoint table — `_bqaa_materialization_state` in the same graph dataset — doubles as a queryable audit log: which window ran when, how many sessions materialized, how many rows per table, whether the run was clean. Late-arriving events get caught by an overlap window on the next run; the checkpoint never regresses.

**4. The audit answer is a single query.** Once the graph is fresh, the executive's question is one traversal:

```sql
SELECT *
FROM GRAPH_TABLE (
  graph_v5.agent_decisions_graph
  MATCH (de:DecisionExecution)
        -[:atContextSnapshot]-> (cs:ContextSnapshot),
        (de) -[:executedAtDecisionPoint]-> (dp:DecisionPoint),
        (de) -[:hasSelectionOutcome]-> (so:SelectionOutcome)
  WHERE de.business_entity_id = 'customer-4029-7'
  COLUMNS (cs.snapshot_payload AS context,
           dp.decision_point_id AS decision_point,
           so.outcome_id AS outcome,
           so.rationale AS rationale)
);
```

The result is one row per option the agent weighed, with the rationale recorded against the chosen outcome. The audit-committee meeting reads it directly off the screen (synthetic):

| Request | Question the agent answered | Option considered | Confidence | Outcome | Rationale |
|---|---|---|---|---|---|
| req-9c2e | *"Approve $340K mortgage for customer 4029-7?"* | Decline | **0.83 (chosen)** | committed | *"DTI exceeds 40% threshold and two recent late payments fall inside the 90-day risk window."* |
| req-9c2e | *"Approve $340K mortgage for customer 4029-7?"* | Refer to human | 0.51 | — | *"DTI is borderline but recent payment behavior is the harder signal."* |
| req-9c2e | *"Approve $340K mortgage for customer 4029-7?"* | Approve | 0.14 | — | *"DTI breach is structural, not transient."* |

Three seconds from question to answer. The audit-committee meeting is no longer a budget request.

The same graph supports SQL aggregations for portfolio questions ("how many declines, what was the average confidence, which rationales drove them?") and scheduled queries for monitoring patterns ("alert me when any decline cites X for borrowers under 25"). Engineers query in SQL or GQL. Data scientists run aggregates in notebooks. Business users on the BigQuery Conversational Analytics Preview ask the same questions in natural language; Conversational Analytics resolves them against the property graph configured as a knowledge source and returns a structured answer card.

No new database to stand up. No separate operational stack. The graph, the events, the IAM, the billing — all stay in BigQuery.

## Trusted by industry leaders

> *[Customer quote from Yahoo to be inserted here once approved.]*
>
> — *[Title, Name], Yahoo*

*(Additional customer voices welcome. Reach out to the BigQuery Agent Analytics team via the [SDK repository](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK) to share your deployment story.)*

## Where this fits

The same pattern works wherever an agent makes consequential decisions and someone eventually has to explain them:

- **Credit and underwriting agents** in regulated lending — turn every decline into a traversable rationale chain for audit and appeal.
- **Prior-authorization agents** in health payers — give the state regulator's call a same-day answer instead of a same-week investigation.
- **Marketing-budget agents** that move spend mid-campaign — let the CMO defend an autonomous reallocation in tomorrow's earnings prep.
- **Procurement agents** that pick suppliers — make sourcing decisions queryable by category, vendor, and rationale.
- **Trading and risk agents** that act inside time windows — produce a per-trade decision audit at end-of-day.
- **Customer-service agents** that grant refunds or waive fees — surface the rationale behind every monetary concession.
- **Internal IT agents** that approve access requests — give security review an after-the-fact view of every grant.

Each one has the same three ingredients: an event stream the plugin captures automatically, a property graph that describes the decision shape, and a stakeholder who will eventually ask "why did the agent do that?"

## Get started today

The SDK repository ships a worked end-to-end example, including the property-graph schema, a runnable agent, the Cloud Run Job deploy, and a customer playbook covering required APIs, the IAM matrix, recommended schedules per latency target, the JSON-log schema, Cloud Monitoring alert queries, the state-table audit log, troubleshooting, and live-deployment evidence captured against a real Google Cloud project.

- Repository → [BigQuery-Agent-Analytics-SDK](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
- Customer playbook → [`examples/migration_v5/periodic_materialization/`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization)
- Codelab → *Periodic materialization for BigQuery Agent Analytics* (45-minute hands-on, self-contained from scratch)

BigQuery property graphs / GQL and BigQuery Conversational Analytics are in Preview on Google Cloud — check the Preview documentation for your region. The BigQuery Agent Analytics Plugin and SDK are generally available and ship with this release.

Three commands, one Cloud Run Job, the audit answer waiting before the meeting starts. That is the operational change worth making this week.
