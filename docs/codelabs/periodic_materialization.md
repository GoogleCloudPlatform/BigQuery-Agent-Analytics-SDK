summary: Build a queryable BigQuery property graph of your AI agent's decisions. You will apply a graph schema to a BigQuery dataset, seed it with sample agent events, run the bqaa-materialize-window CLI to extract a decision graph from those events, and query the result with Graph Query Language (GQL) to trace any decision end-to-end.
id: bqaa-periodic-materialization
categories: bigquery,adk,agents
tags: bigquery,adk,bigquery-agent-analytics,cloud-run,cloud-scheduler,property-graph,gql
status: Draft
authors: BigQuery Agent Analytics team
feedback link: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues

# Trace AI Agent Decisions with BigQuery Property Graphs

## Introduction
Duration: 0:03

*BigQuery property graphs, BigQuery Conversational Analytics, and the BigQuery Agent Analytics SDK are currently in Preview on Google Cloud. The BigQuery Agent Analytics Plugin is Generally Available (GA). Examples in this codelab use synthetic data.*

As autonomous AI agents take on more operational responsibilities (evaluating loan applications, managing marketing budgets, approving access requests), organizations must be able to audit and explain their decisions. Reconstructing the exact context, alternatives considered, and final rationale of an agent's decision is essential for compliance, risk management, and operational trust.

This codelab uses the BigQuery Agent Analytics SDK to transform raw agent event logs into a queryable BigQuery property graph, on a schedule, without any external graph database or ETL pipeline.

### What You Will Build

* A BigQuery property graph that models a generic agent decision flow: a request comes in, the agent weighs options, an outcome is committed.
* A populated `agent_events` table with a synthetic event corpus.
* A working `bqaa-materialize-window` run that fills the graph from those events.
* A one-shot backfill against a historical window without affecting the live cron's checkpoint.
* An audit-style GQL query that traces a single decision end-to-end.

### What You Will Learn

* How the BigQuery Agent Analytics Plugin writes to `agent_events`.
* How a property graph is composed from a small set of declarative artifacts (table DDL, property-graph DDL, ontology, binding).
* How to run `bqaa-materialize-window` against a property graph.
* How to query a BigQuery property graph in GQL.
* The production-grade capabilities the SDK supports for enterprise deployments.

### What You Will Need

* A Google Cloud project with billing enabled.
* Owner or Editor role on that project. You will create a BigQuery dataset and grant IAM.
* The `gcloud` CLI installed and authenticated, or access to Cloud Shell.
* Python 3.10 or newer.
* Familiarity with BigQuery SQL. GQL knowledge is not required.

**Total time: about 35 minutes.**

## Before You Begin
Duration: 0:05

### Pick a Project and Region

Open Cloud Shell or a local terminal:

```bash
export PROJECT_ID="your-project-id"
export REGION="us-central1"
export DATASET="agent_analytics_demo"
gcloud config set project "$PROJECT_ID"
```

The single `DATASET` variable holds both the raw `agent_events` table and the materialized graph tables. Using one dataset keeps the codelab simple. Production deployments often split events and graph into separate datasets so IAM can be granted narrowly per dataset.

### Enable the Required APIs

```bash
gcloud services enable \
    bigquery.googleapis.com \
    aiplatform.googleapis.com \
    --project="$PROJECT_ID"
```

The `aiplatform.googleapis.com` API is required because the SDK's default extraction path calls BigQuery's `AI.GENERATE` function. If you later switch to deterministic extraction with `--extraction-mode=compiled-only`, this API is no longer needed.

### Create the BigQuery Dataset

```bash
bq --location=US mk --dataset "$PROJECT_ID:$DATASET"
```

You should see "Dataset '...' successfully created". If the dataset already exists, the command errors harmlessly. Leave it in place.

## Install the SDK
Duration: 0:02

Set up a Python virtual environment and install the SDK from PyPI:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install bigquery-agent-analytics
```

Verify the install:

```bash
bqaa-materialize-window --help | head -8
```

You should see the CLI banner.

### Authenticate

If you are on a workstation:

```bash
gcloud auth login
gcloud auth application-default login
```

Cloud Shell users can skip this step; credentials are already configured.

## Get the Codelab Artifacts
Duration: 0:02

The codelab ships a set of ready-to-use artifacts: the property-graph schema, the ontology, the binding, and a synthetic event generator. You do not author any of these yourself; the codelab uses them as-is, and the [README in the artifacts folder](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/codelab/periodic_materialization/README.md) explains how to adapt them for your own decision domain.

Download the artifacts to a working directory:

```bash
mkdir -p ~/bqaa-codelab && cd ~/bqaa-codelab

BASE="https://raw.githubusercontent.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/main/examples/codelab/periodic_materialization"
for f in property_graph.sql table_ddl.sql ontology.yaml binding.yaml seed_events.py; do
  curl -fsSL "$BASE/$f" -o "$f"
done
ls
```

You should see five files:

```
binding.yaml  ontology.yaml  property_graph.sql  seed_events.py  table_ddl.sql
```

The decision flow these artifacts describe has three node types and two heterogeneous edges:

```
DecisionRequest --[evaluatesOption]--> DecisionOption
              \--[resultedIn]--------> DecisionOutcome
```

`DecisionRequest` is the question the agent received. `DecisionOption` is one alternative the agent considered. `DecisionOutcome` records the committed choice and the rationale.

## Phase 1: Apply the Property Graph Schema
Duration: 0:04

The materializer writes into BigQuery tables, so they must exist before the first run. Apply the table DDL first, then the property-graph DDL (the property graph references those tables, and BigQuery rejects a `CREATE PROPERTY GRAPH` that points at tables that do not yet exist):

```bash
envsubst < table_ddl.sql      | bq query --use_legacy_sql=false
envsubst < property_graph.sql | bq query --use_legacy_sql=false
```

You should see five `CREATE TABLE` results and one `CREATE PROPERTY GRAPH` result. The DDL is idempotent; you can re-run it safely.

### Render the Binding

The materializer reads `binding.yaml` directly. Substitute the shell variables once before any tool reads the file:

```bash
envsubst < binding.yaml > binding.rendered.yaml
```

After this, `binding.rendered.yaml` contains your real project ID and dataset name instead of the `${...}` markers. If you skip this step, `bqaa-materialize-window` validates against literal `${PROJECT_ID}` text and fails closed.

## Phase 2: Generate Sample Agent Events
Duration: 0:04

In production, the BigQuery Agent Analytics Plugin captures events automatically as your ADK agent runs:

```python
from google.adk.plugins import BigQueryAgentAnalyticsPlugin

plugin = BigQueryAgentAnalyticsPlugin(
    project_id="your-project-id",
    dataset_id="agent_analytics_demo",
)
runner = Runner(agent=root_agent, plugins=[plugin])
```

For this codelab you use a small synthetic event generator that writes the same shape of rows directly to `agent_events`. Run it:

```bash
pip install google-cloud-bigquery
python seed_events.py \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --sessions 5
```

You should see "Inserted 30 events across 5 sessions into ...".

Verify the events landed:

```bash
bq query --use_legacy_sql=false \
    "SELECT event_type, COUNT(*) AS n FROM \`$PROJECT_ID.$DATASET.agent_events\` GROUP BY event_type ORDER BY n DESC"
```

You should see 25 `TOOL_COMPLETED` rows and 5 `AGENT_COMPLETED` rows (each session emits one `submit_request`, three `evaluate_option`, one `commit_outcome`, and one closing `AGENT_COMPLETED` — five tool events plus one agent terminator per session). The `AGENT_COMPLETED` rows are the session terminators that the materializer keys on for terminal-event detection.

## Phase 3: Materialize the Decision Graph
Duration: 0:05

Run the materializer locally:

```bash
bqaa-materialize-window \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --ontology ~/bqaa-codelab/ontology.yaml \
    --binding ~/bqaa-codelab/binding.rendered.yaml \
    --lookback-hours 24 \
    --format json
```

You should see a structured JSON report:

```json
{
  "run_id": "...",
  "sessions_discovered": 5,
  "sessions_materialized": 5,
  "sessions_failed": 0,
  "rows_materialized": {
    "DecisionRequest": 5,
    "DecisionOption": 15,
    "DecisionOutcome": 5
  },
  "ok": true
}
```

`ok: true` indicates the materializer found five completed sessions, extracted the decision flow from each via `AI.GENERATE`, and wrote the corresponding rows into the graph tables.

If you see `ok: false` with `error_code = "empty_extraction"`, the most common cause is that the `aiplatform.googleapis.com` API has not propagated yet, or your account is missing `roles/aiplatform.user`. Wait a minute and retry, or grant the role:

```bash
USER_EMAIL=$(gcloud auth list --filter=status:ACTIVE --format="value(account)")
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="user:$USER_EMAIL" --role="roles/aiplatform.user"
```

Verify the graph has rows:

```bash
bq query --use_legacy_sql=false \
    "SELECT COUNT(*) AS n FROM \`$PROJECT_ID.$DATASET.decision_request\`"
```

You should see five rows.

### The Zero-LLM Extraction Path

The local run above uses the default extractor, which calls BigQuery's `AI.GENERATE` to extract entities and relationships from event content. The SDK also supports a `--extraction-mode=compiled-only` flag that swaps in a reference-extractor module: deterministic Python keyed to your ontology, no Vertex AI dependency, the audited code path.

Production deployments that need to certify their data path to a regulator typically run `--extraction-mode=compiled-only` and remove `roles/aiplatform.user` from the runtime service account entirely. The reference extractor is a small Python module that maps event-content shape to entity-and-relationship dicts; the materializer wires the rest. The codelab stays on the `AI.GENERATE` default.

## Phase 4: Query the Decision Trace
Duration: 0:05

With the graph populated, the audit question is a single GQL traversal. Save the following as `traversal.sql`:

```sql
SELECT *
FROM GRAPH_TABLE (
  ${DATASET}.agent_decisions_graph
  MATCH
    (req:DecisionRequest) -[eo:evaluatesOption]-> (opt:DecisionOption),
    (req)                 -[ri:resultedIn]->      (out:DecisionOutcome)
  COLUMNS (
    req.request_id   AS request,
    req.request_text AS question,
    opt.option_label AS considered,
    opt.confidence   AS score,
    out.status       AS outcome,
    out.rationale    AS rationale
  )
);
```

Run it:

```bash
envsubst < traversal.sql | bq query --use_legacy_sql=false --max_rows=20
```

You should see fifteen rows: three options per request, five requests. Each row shows the request, the option the agent considered, its confidence score, the final outcome, and the rationale.

For a single decision's full picture, filter by `request_id` to get the row set an audit team needs: the question that came in, the options that were weighed (with scores), and the rationale that was committed.

### The Same Answer in Natural Language

Once your project is on the BigQuery Conversational Analytics Preview, you can register the property graph as a knowledge source and ask the question in plain English:

> *"Why did the agent commit outcome X on request Y?"*

Conversational Analytics resolves the question against the graph and returns a structured answer card. See the [Conversational Analytics documentation](https://cloud.google.com/bigquery/docs/conversational-analytics) for setup.

## Advanced: Backfill a Historical Window
Duration: 0:04

Backfill mode is the operations workflow you reach for when events arrived during an outage, when a binding change requires a one-shot re-extraction, or when an audit team asks for a specific historical quarter. The same code path the scheduled cron uses can be pointed at any fixed `[from, to)` window, and the run writes its high-water mark to an isolated state-table namespace (controlled by `--state-key-suffix`) so it never disturbs the live cron's checkpoint.

For this codelab, run a backfill against an **empty historical window** (eight to nine hours ago, before you seeded any events). The backfill discovers zero sessions to materialize, which lets you demonstrate the state-table isolation property without re-processing the sessions you already materialized in Phase 3. Production backfills target windows where new events actually arrived; the empty-window run shows the audit-trail behavior in a single command.

```bash
FROM=$(date -u -d "9 hours ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
       || date -u -v-9H +"%Y-%m-%dT%H:%M:%SZ")
TO=$(date -u -d "8 hours ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
     || date -u -v-8H +"%Y-%m-%dT%H:%M:%SZ")

bqaa-materialize-window \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --ontology ~/bqaa-codelab/ontology.yaml \
    --binding ~/bqaa-codelab/binding.rendered.yaml \
    --lookback-hours 1 \
    --backfill --from "$FROM" --to "$TO" \
    --state-key-suffix codelab_backfill_demo \
    --format json
```

(The `date -u -d ...` form is GNU `date` on Linux and Cloud Shell; the `date -u -v-9H` form is BSD `date` on macOS. The `||` falls back to the macOS form if the GNU form fails.)

You should see a JSON report with `"sessions_materialized": 0` (because the backfill window does not contain any of the synthetic events you seeded in Phase 2). The interesting result is the new row in the state table:

```bash
bq query --use_legacy_sql=false \
    "SELECT mode, scan_start, scan_end, sessions_materialized, ok \
     FROM \`$PROJECT_ID.$DATASET._bqaa_materialization_state\` \
     ORDER BY run_started_at DESC LIMIT 5"
```

You should see at least two rows: a `mode = 'steady'` row from the Phase 3 materialization and a `mode = 'backfill'` row from this run. The two rows have different `state_key` values (the `--state-key-suffix` you passed changes the hash input), so the backfill checkpoint sits in its own namespace and the live cron's high-water mark in `state_key = 'steady'` stays untouched. That isolation property is what lets a backfill run concurrently with the production cron without interference.

In a real outage-recovery scenario you would point `--from` and `--to` at the window where the missed events actually landed, and the backfill would materialize those sessions. The state-table behavior you just observed is the audit trail an operator follows to confirm the catch-up ran.

## Production-Grade Capabilities
Duration: 0:03

The BigQuery Agent Analytics SDK includes several features designed to support enterprise-grade deployments. The local run you completed in Phase 3 uses default behavior; the optional flags below add stricter controls when your operating model needs them.

**Default behavior:**

* **Structured JSON logs on every run** with `--format json`, including per-table row counts and per-session failure diagnostics for alerting and audit.
* **State-table audit trail.** Every run lands a row in `_bqaa_materialization_state` recording the window scanned, sessions discovered, sessions materialized, mode (`steady`, `backfill`, `orphan_scan`, `orphan_ledger`), and a full JSON report.
* **Tunable retry budget** (`--max-retries`, default 2) for transient failures.
* **Exactly-once processing.** The state table's high-water mark advances only on successful sessions; replays after a partial failure pick up where the last run left off.

**Opt-in for stricter or incident-response workflows:**

* **Deterministic parsing** (`--extraction-mode compiled-only`). Disables LLM calls entirely and runs a reference-extractor module. Required by audits that must certify the data path; removes the Vertex AI dependency.
* **Orphan-session watchdog** (`--max-session-age-hours`). Sessions older than the cap that have not terminated are flagged as orphaned and written to the state table with `mode = 'orphan_scan'`, so an operator can drain stale state without the cron silently re-pulling broken sessions.
* **Backfill mode** (`--backfill --from / --to --state-key-suffix`). Re-materializes a fixed historical window without affecting the active schedule's progress markers. You exercised the audit-trail behavior in the *Advanced: Backfill a Historical Window* section above.
* **Per-window batch cap** (`--max-sessions`). Caps the number of sessions processed per scan to handle hostile event spikes.

For scheduled execution, the SDK ships a deploy script and a Terraform module that wrap `bqaa-materialize-window` as a Cloud Run Job triggered by Cloud Scheduler. Both create the same six resources (graph dataset, runtime service account, scheduler-caller service account, IAM bindings, Cloud Run Job, Cloud Scheduler trigger) with least-privilege defaults. See the [periodic-materialization deployment guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization) for the worked example, the IAM matrix, the recommended schedules, and the Cloud Monitoring alert queries.

## Clean Up
Duration: 0:03

Tear down what you created so you do not get billed for an idle dataset:

```bash
bq rm -r -f --dataset "$PROJECT_ID:$DATASET"
```

That single command removes the dataset, the agent events, the graph tables, and the state table together.

## Summary
Duration: 0:02

You have:

* Created a BigQuery dataset and applied a property-graph schema describing an agent decision domain.
* Populated `agent_events` with a synthetic event corpus.
* Run `bqaa-materialize-window` to extract a decision graph from those events.
* Backfilled a historical window into an isolated state-table namespace without disturbing the live checkpoint.
* Queried the resulting graph in GQL and seen the audit-style answer.

The same pattern applies wherever an agent makes consequential decisions: credit underwriting, prior authorization, marketing budget moves, procurement, customer service, and internal IT. To build your own decision graph, copy the codelab artifacts as a starting point and adapt the four declarative files (table DDL, property-graph DDL, ontology, binding) to your domain.

### Further Reading

* [BigQuery Agent Analytics SDK repository](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
* [Codelab artifacts and adaptation guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/codelab/periodic_materialization)
* [Periodic-materialization deployment guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization): required APIs, IAM matrix, recommended schedules, Cloud Monitoring alert queries, and the Terraform module.
* [BigQuery property graphs documentation](https://cloud.google.com/bigquery/docs/reference/standard-sql/graph-intro) (Preview).
* [BigQuery Conversational Analytics documentation](https://cloud.google.com/bigquery/docs/conversational-analytics) (Preview).
