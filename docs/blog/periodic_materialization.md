# Periodic materialization for BigQuery Agent Analytics: keep your agent decision graph fresh, every six hours

*BigQuery property graphs and BigQuery Conversational Analytics are in Preview on Google Cloud. The BigQuery Agent Analytics Plugin and SDK are generally available. Examples in this post use synthetic data.*

Today we are making it dramatically easier to keep a BigQuery property graph of your AI agent's decisions current with the events your agents are actually producing — on a schedule, against the same BigQuery project, with no new database to stand up. The new `bqaa-materialize-window` command in the BigQuery Agent Analytics SDK takes the events captured by the BigQuery Agent Analytics Plugin and writes them into the property graph you already defined, every N hours. Run it from Cloud Build, Cloud Workflows, an external scheduler, or — for the canonical worked example — wrap it in a Cloud Run Job + Cloud Scheduler trigger using the deploy script shipped with the SDK's migration v5 demo. Events stay in a read-only events dataset; the graph lives in a separate read/write graph dataset; the runtime service account is granted exactly the narrow IAM each side needs.

This is the small operational change that converts agent observability from "engineering will dig through logs" into "the audit team asks the question and gets an answer in seconds."

And it ships production-shaped on day one. The 0.3.2 release of the SDK lands a deploy whose **default** posture is **least-privilege split service accounts, a tunable retry budget, structured JSON logs on every run, and a state-table audit trail** — every customer who runs the deploy with the four required arguments gets that shape on the first try. Regulated and operations teams can then **opt into** a zero-LLM extraction path, an orphan-session watchdog, a backfill mode for incident response, and a Terraform module that mirrors the bash deploy. The story below walks the audit answer first; the production surface, the customer-playbook README, the codelab, and the Terraform module follow once you've seen what the answer looks like.

## Why this is different

Three properties separate this from "stand up a graph database next to BigQuery and ETL into it":

- **BigQuery-native.** No new graph database to operate. No separate pipeline to maintain. The events, the graph, the IAM, the billing, the query language — all stay in BigQuery. The only thing the deploy creates outside BigQuery is a Cloud Run Job (the materializer) and a Cloud Scheduler trigger (the cron). Both retire when you stop paying for them; neither holds state.
- **Governed by design.** The events dataset is read-only to the materializer; the graph dataset is the only write target; the runtime service account holds exactly the BigQuery and (optionally) Vertex AI roles it needs, and the scheduler-caller service account holds only `roles/run.invoker` on the job. Every run lands a row in the state table — window scanned, sessions discovered, sessions materialized, sessions failed, `mode` (`steady` / `backfill` / `orphan_scan` / `orphan_ledger`), JSON report — giving the audit team a queryable history of what the materializer did and when, without log digging.
- **Audit-grade extraction.** Production deployments choose between two extractors at deploy time. The default uses BigQuery's `AI.GENERATE` for fast onboarding against any new event shape. Regulated paths flip to `--extraction-mode=compiled-only`, which swaps in a deterministic reference-extractor module the customer authors against their ontology — no Vertex AI dependency, Python the auditor can read. Same materializer, same graph, two extraction policies — pick the one that fits the workload.

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

The graph captures your domain language: what the agent saw, what it decided, what options were on the table, what the outcome was. You author the graph contract once — the table DDL, the `CREATE PROPERTY GRAPH` statement, the ontology that names the entities, and the binding that maps entities to tables — and the migration v5 demo in the SDK repository ships a complete starting point you can copy and edit for your domain. (Teams that want the zero-LLM extraction path add one more module — a small reference extractor keyed to the ontology; see the codelab below.) Engineering teams that already think in graphs can write the contract directly; teams new to graphs start from the demo and edit.

**3. The SDK runs `bqaa-materialize-window` against the binding you point it at.** You hand the command your events dataset and the path to the binding that describes your graph. It scans the last N hours of `agent_events`, extracts the structured shape per session, materializes the entity and relationship tables, and emits a structured JSON report. Schedule the command however your team already schedules jobs: Cloud Build trigger, Workflows, GitHub Actions, an external cron. The SDK's migration v5 demo also ships a `deploy_cloud_run_job.sh` script that wraps the command as a Cloud Run Job with a Cloud Scheduler trigger — the canonical worked example of the production pattern, using the demo's bundled artifacts. Bring your own scheduler for your own graph.

```bash
./deploy_cloud_run_job.sh \
    --project your-project --region us-central1 \
    --events-dataset agent_analytics \
    --graph-dataset graph_v5 \
    --schedule "0 */6 * * *" \
    --smoke
```

Every six hours, on whatever cadence you pick, the job:

- Scans the last six hours of `agent_events`.
- Picks out the sessions that completed in that window.
- Extracts the structured shape your property graph expects, per session.
- Materializes the entity and relationship tables.
- Writes a structured JSON report to Cloud Logging — `jsonPayload.ok`, `jsonPayload.sessions_materialized`, per-table row counts.

A checkpoint table — `_bqaa_materialization_state` in the same graph dataset — doubles as a queryable audit log: which window ran when, how many sessions materialized, how many rows per table, whether the run was clean. Late-arriving events get caught by an overlap window on the next run; the checkpoint never regresses.

**4. The audit answer is a single query.** Once the graph is fresh, the executive's question is one traversal — every option the agent weighed, which one it chose, and the rationale:

```sql
SELECT *
FROM GRAPH_TABLE (
  graph_v5.agent_decisions_graph
  MATCH (de:DecisionExecution) -[:executedAtDecisionPoint]-> (dp:DecisionPoint),
        (dp) -[:evaluatesCandidate]-> (option:Candidate),
        (de) -[:hasSelectionOutcome]-> (so:SelectionOutcome),
        (so) -[:selectedCandidate]-> (chosen:Candidate)
  WHERE de.business_entity_id = 'customer-4029-7'
  COLUMNS (
    de.decision_execution_id AS decision,
    dp.decision_point_id      AS question,
    option.candidate_id       AS option_considered,
    chosen.candidate_id       AS chosen_option,
    so.rationale              AS rationale
  )
);
```

The result is one row per option the agent weighed; `chosen_option` is the same on every row and identifies the one the agent committed. The audit-committee meeting reads it directly off the screen (synthetic):

| decision | question | option_considered | chosen_option | rationale |
|---|---|---|---|---|
| de-9c2e | dp-mortgage-approval | cand-decline | **cand-decline ✓** | *"DTI exceeds 40% threshold and two recent late payments fall inside the 90-day risk window."* |
| de-9c2e | dp-mortgage-approval | cand-refer-to-human | cand-decline | *"DTI exceeds 40% threshold and two recent late payments fall inside the 90-day risk window."* |
| de-9c2e | dp-mortgage-approval | cand-approve | cand-decline | *"DTI exceeds 40% threshold and two recent late payments fall inside the 90-day risk window."* |

The agent considered three options, picked `cand-decline`, and recorded the rationale once against the chosen outcome. Drill in with `MATCH (cs:ContextSnapshot)` to see what the agent saw before it decided; aggregate over `DecisionExecution` for portfolio-level questions. Three seconds from question to answer. The audit-committee meeting is no longer a budget request.

The same graph supports SQL aggregations for portfolio questions ("how many declines, what was the average confidence, which rationales drove them?") and scheduled queries for monitoring patterns ("alert me when any decline cites X for borrowers under 25"). Engineers query in SQL or GQL. Data scientists run aggregates in notebooks. Business users on the BigQuery Conversational Analytics Preview ask the same questions in natural language; Conversational Analytics resolves them against the property graph configured as a knowledge source and returns a structured answer card.

No new database to stand up. No separate operational stack. The graph, the events, the IAM, the billing — all stay in BigQuery.

## Production-shape defaults

Periodic materialization shipped this surface as a single coordinated release because audit-grade deployments need more than a working cron. The 0.3.2 release closes the design-partner asks from issue [#187](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/187) and lands the deploy with production-hardened defaults out of the box.

- **Least-privilege by default.** The deploy creates two service accounts — `bqaa-periodic-runtime-sa` (the identity the Cloud Run Job runs as, granted only the BigQuery + Vertex AI roles it needs) and `bqaa-periodic-scheduler-sa` (the identity Cloud Scheduler uses to invoke the job, granted only `run.invoker`). One-SA simplicity is available behind a `--single-sa` flag for development; the production posture is split.
- **Tunable retries and a hard ceiling on stuck sessions.** `--max-retries` (default 2) governs in-window retry budget; `--max-session-age-hours` is an opt-in orphan watchdog that flags sessions older than the bound as orphaned and writes the diagnosis to the state table so an operator can drain stale state without the cron silently pulling the same broken sessions for days.
- **A zero-LLM extraction path for regulated audits.** `--extraction-mode=compiled-only` swaps the default `AI.GENERATE` extractor for a reference-extractor module the customer authors against their ontology — deterministic Python, audited, no Vertex AI dependency. Production deployments that need to certify their data path to a regulator can run compiled-only and remove `roles/aiplatform.user` from the runtime SA entirely. ("Compiled" here describes the extraction *mode*, not a fingerprint-stable compiled bundle; the `--bundles-root` path for compiled bundles is a separate surface.)
- **Backfill mode for incident response.** When events arrived during an outage, when a binding change requires a one-shot re-extraction, or when an audit committee asks for a historical window, `--backfill --from $TS --to $TS --state-key-suffix backfill_2026q1` extracts the fixed window into an isolated state-table namespace without disturbing the live cron's high-water mark.
- **A Terraform module that mirrors the bash deploy.** Infrastructure-as-code teams point their Terraform pipelines at the [periodic-materialization module](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization/terraform); a `build_image.sh` helper packages the same image the bash deploy builds. The bash deploy is the lighter-weight onboarding; the Terraform module is the same six resources behind `terraform plan`, drift detection, and multi-environment promotion.
- **An outcome-signal feedback loop.** The same property graph carries `OutcomeSignal`, `RewardComputation`, and `RejectionReason` nodes that close the loop on agent quality — reward-shaping, rejection-cause analysis, and constraint-violation alerts — without a second data store. Walked end-to-end in the [migration v5 demo notebook](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5_demo_notebook.ipynb).

The deploy defaults cover split SAs, retry budget, structured JSON logs, and the state-table audit trail — every customer who runs `deploy_cloud_run_job.sh` with the four required arguments lands that shape on the first try. The remaining flags above opt into stricter (compiled-only) or incident-response (backfill, orphan watchdog) or IaC-aligned (Terraform) workflows; pick the ones your operating model needs.

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

The SDK ships a worked end-to-end example, including the property-graph schema, a runnable agent, both deploy paths (bash and Terraform), and a customer playbook covering required APIs, the IAM matrix, recommended schedules per latency target, the JSON-log schema, Cloud Monitoring alert queries, the state-table audit log, troubleshooting, and live-deployment evidence captured against a real Google Cloud project.

```bash
pip install 'bigquery-agent-analytics>=0.3.2'
```

- Repository → [BigQuery-Agent-Analytics-SDK](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
- Customer playbook → [`examples/migration_v5/periodic_materialization/`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization)
- Terraform module → [`examples/migration_v5/periodic_materialization/terraform/`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization/terraform) — IaC mirror of the bash deploy, same six resources
- Feedback / reward loop walkthrough → [migration v5 demo notebook](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5_demo_notebook.ipynb) — Beats 1 through 5, end-to-end against a live BigQuery project
- Codelab → *Periodic materialization for BigQuery Agent Analytics* (45-minute hands-on, self-contained from scratch)
- Changelog → [`CHANGELOG.md`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/CHANGELOG.md) `[0.3.2]` block

BigQuery property graphs / GQL and BigQuery Conversational Analytics are in Preview on Google Cloud — check the Preview documentation for your region. The BigQuery Agent Analytics Plugin and SDK are generally available; 0.3.2 is the release that closes the migration v5 production track.

One command on the cron of your choice, audited extraction, retries with a budget, an orphan watchdog catching what cron missed, and the audit answer waiting before the meeting starts. That is the operational change worth making this week.
