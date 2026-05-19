summary: Keep a BigQuery property graph of your AI agent's decisions fresh from agent_events on a six-hour schedule. Capture events with the BigQuery Agent Analytics Plugin, deploy a Cloud Run Job + Cloud Scheduler that materializes them into your provided property graph, and answer audit-style questions in GQL.
id: bqaa-periodic-materialization
categories: bigquery,adk,agents
tags: bigquery,adk,bigquery-agent-analytics,cloud-run,cloud-scheduler,property-graph,gql
status: Draft
authors: BigQuery Agent Analytics team
feedback link: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues

# Periodic materialization for BigQuery Agent Analytics

## Introduction
Duration: 0:03

If your AI agent makes decisions that someone will eventually want explained — a credit decline, a marketing reallocation, a procurement pick, an access grant — the gap between "events captured in BigQuery" and "auditable explanation on demand" is usually filled by an engineer with a SQL editor. In this codelab you will close that gap by deploying a Cloud Run Job + Cloud Scheduler that materializes your agent's events into a BigQuery property graph every six hours, and then querying the graph in GQL.

The codelab is self-contained from scratch. You will create the BigQuery datasets, the property graph, the demo events, the materialization deploy, and the query — all in one Google Cloud project. At the end you tear it all down with three commands.

### What you'll build

- A BigQuery property graph that describes a generic agent decision flow (Decision Request → Decision Option → Decision Outcome).
- A populated `agent_events` table with a small synthetic event corpus you can re-generate.
- A working `bqaa-materialize-window` run that fills the graph from those events. Cron this command from your scheduler of choice (Cloud Build, Workflows, external cron) to keep the graph fresh.
- A Cloud Run Job + Cloud Scheduler trigger walked end-to-end against the SDK's bundled migration v5 demo artifacts as the deployment-pattern reference. (The deploy script doesn't yet accept arbitrary artifact paths; the codelab's custom graph runs through `materialize-window` directly.)
- An audit-style GQL query that traces a single decision end-to-end.

### What you'll learn

- How the BigQuery Agent Analytics Plugin writes to `agent_events`.
- How to deploy `bqaa-materialize-window` as a Cloud Run Job with a Cloud Scheduler trigger.
- How to apply a property-graph schema you authored to a BigQuery dataset.
- How to query the resulting graph in GQL.

### What you'll need

- A Google Cloud project with billing enabled.
- Owner or Editor on that project (you will create datasets, deploy a Cloud Run Job, and grant IAM).
- The `gcloud` CLI installed and authenticated, or access to Cloud Shell.
- Python 3.10 or newer.
- Familiarity with BigQuery SQL. GQL knowledge is not required.

BigQuery property graphs / GQL is in Preview on Google Cloud. The BigQuery Agent Analytics Plugin and SDK are generally available. Check the BigQuery property-graph Preview documentation for your region before deploying to production.

**Total time: about 45 minutes.**

## Before you begin
Duration: 0:05

### Pick a project and region

Open Cloud Shell or a local terminal:

```bash
export PROJECT_ID="your-project-id"
export REGION="us-central1"
export EVENTS_DATASET="agent_analytics_demo"
export GRAPH_DATASET="agent_graph_demo"
gcloud config set project "$PROJECT_ID"
```

### Enable the required APIs

The deploy touches five Google Cloud services:

```bash
gcloud services enable \
    bigquery.googleapis.com \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com \
    aiplatform.googleapis.com \
    --project="$PROJECT_ID"
```

`aiplatform.googleapis.com` is required because the SDK's default extraction path calls BigQuery's `AI.GENERATE` to extract entities and relationships from event content. The compiled-extractor path that ships with the SDK skips this dependency for known event shapes, but the demo here uses the AI.GENERATE fallback so the codelab works without any custom extractor code.

### Create two BigQuery datasets

Periodic materialization treats events and graph as separate datasets so you can grant IAM narrowly. Create both:

```bash
bq --location=US mk --dataset "$PROJECT_ID:$EVENTS_DATASET"
bq --location=US mk --dataset "$PROJECT_ID:$GRAPH_DATASET"
```

You should see "Dataset '...' successfully created" twice. If a dataset already exists, the command errors harmlessly — leave it in place.

## Installation and setup
Duration: 0:03

### Clone the SDK repository

The deploy script and the demo agent script live in the SDK repository:

```bash
git clone https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK.git
cd BigQuery-Agent-Analytics-SDK
```

### Set up a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

The `[dev]` extra brings the test toolchain. Install takes about a minute.

Verify:

```bash
PYTHONPATH=src python -m bigquery_agent_analytics.cli --help | head -8
```

You should see the CLI banner with subcommands including `materialize-window`.

### Authenticate

If you are on a workstation:

```bash
gcloud auth login
gcloud auth application-default login
```

Cloud Shell users can skip this step — credentials are already configured.

## Provide your property graph
Duration: 0:08

In production you author one artifact: a property graph that describes your agent's decision domain. Periodic materialization keeps it filled from `agent_events`. In this codelab you will copy the demo graph below into three files in your working directory. In a real deployment, you would replace these with the graph your team designed for your domain.

The demo graph models a generic agent decision flow: a request comes in, the agent weighs options, an outcome is committed. Three node types, two heterogeneous edges.

```
DecisionRequest --[evaluatesOption]--> DecisionOption
              \--[resultedIn]--------> DecisionOutcome
```

### Save the property-graph DDL

Create a working directory and save the property-graph schema. The `${PROJECT_ID}` / `${GRAPH_DATASET}` placeholders will be filled by your shell when you apply the DDL:

```bash
mkdir -p ~/bqaa-codelab && cd ~/bqaa-codelab
```

Save the following as `property_graph.sql`:

```sql
CREATE OR REPLACE PROPERTY GRAPH `${PROJECT_ID}.${GRAPH_DATASET}.agent_decisions_graph`
  NODE TABLES (
    `${PROJECT_ID}.${GRAPH_DATASET}.decision_request` AS decision_request
      KEY (request_id)
      LABEL DecisionRequest PROPERTIES (request_id, request_text, requested_at),
    `${PROJECT_ID}.${GRAPH_DATASET}.decision_option` AS decision_option
      KEY (option_id)
      LABEL DecisionOption PROPERTIES (option_id, option_label, confidence),
    `${PROJECT_ID}.${GRAPH_DATASET}.decision_outcome` AS decision_outcome
      KEY (outcome_id)
      LABEL DecisionOutcome PROPERTIES (outcome_id, status, rationale, decided_at)
  )
  EDGE TABLES (
    `${PROJECT_ID}.${GRAPH_DATASET}.evaluates_option` AS evaluates_option
      KEY (request_id, option_id)
      SOURCE KEY (request_id) REFERENCES decision_request (request_id)
      DESTINATION KEY (option_id) REFERENCES decision_option (option_id)
      LABEL evaluatesOption,
    `${PROJECT_ID}.${GRAPH_DATASET}.resulted_in` AS resulted_in
      KEY (request_id, outcome_id)
      SOURCE KEY (request_id) REFERENCES decision_request (request_id)
      DESTINATION KEY (outcome_id) REFERENCES decision_outcome (outcome_id)
      LABEL resultedIn
  );
```

### Save the node + edge table DDL

The materializer writes into BigQuery tables; you need to create them before the first run. Save as `table_ddl.sql`:

```sql
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${GRAPH_DATASET}.decision_request` (
  request_id STRING, request_text STRING, requested_at TIMESTAMP,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${GRAPH_DATASET}.decision_option` (
  option_id STRING, option_label STRING, confidence FLOAT64,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${GRAPH_DATASET}.decision_outcome` (
  outcome_id STRING, status STRING, rationale STRING, decided_at TIMESTAMP,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${GRAPH_DATASET}.evaluates_option` (
  request_id STRING, option_id STRING,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${GRAPH_DATASET}.resulted_in` (
  request_id STRING, outcome_id STRING,
  session_id STRING, extracted_at TIMESTAMP
);
```

The `session_id` and `extracted_at` columns are SDK metadata the materializer writes on every run. They are required on every bound table.

### Save the ontology that pairs with your property graph

The materializer pairs your property graph with a small ontology file — the entity vocabulary the SDK uses when it constructs the `AI.GENERATE` extraction prompt. In a production deployment you author this once alongside your graph; here you paste the demo version. Save as `ontology.yaml`:

```yaml
ontology: agent_decision_flow
entities:
  - name: DecisionRequest
    keys:
      primary: [requestId]
    properties:
      - {name: requestId,   type: string}
      - {name: requestText, type: string}
      - {name: requestedAt, type: timestamp}
  - name: DecisionOption
    keys:
      primary: [optionId]
    properties:
      - {name: optionId,    type: string}
      - {name: optionLabel, type: string}
      - {name: confidence,  type: double}
  - name: DecisionOutcome
    keys:
      primary: [outcomeId]
    properties:
      - {name: outcomeId,   type: string}
      - {name: status,      type: string}
      - {name: rationale,   type: string}
      - {name: decidedAt,   type: timestamp}
relationships:
  - {name: evaluatesOption, from: DecisionRequest, to: DecisionOption}
  - {name: resultedIn,      from: DecisionRequest, to: DecisionOutcome}
```

### Save the binding the SDK reads

The materializer needs to know which entity maps to which BigQuery table. Save as `binding.yaml`:

```yaml
binding: agent_decisions_binding
ontology: agent_decision_flow
target:
  backend: bigquery
  project: ${PROJECT_ID}
  dataset: ${GRAPH_DATASET}
entities:
  - name: DecisionRequest
    source: ${PROJECT_ID}.${GRAPH_DATASET}.decision_request
    properties:
      - {name: requestId,    column: request_id}
      - {name: requestText,  column: request_text}
      - {name: requestedAt,  column: requested_at}
  - name: DecisionOption
    source: ${PROJECT_ID}.${GRAPH_DATASET}.decision_option
    properties:
      - {name: optionId,     column: option_id}
      - {name: optionLabel,  column: option_label}
      - {name: confidence,   column: confidence}
  - name: DecisionOutcome
    source: ${PROJECT_ID}.${GRAPH_DATASET}.decision_outcome
    properties:
      - {name: outcomeId,    column: outcome_id}
      - {name: status,       column: status}
      - {name: rationale,    column: rationale}
      - {name: decidedAt,    column: decided_at}
relationships:
  - name: evaluatesOption
    source: ${PROJECT_ID}.${GRAPH_DATASET}.evaluates_option
    from_columns: [request_id]
    to_columns:   [option_id]
  - name: resultedIn
    source: ${PROJECT_ID}.${GRAPH_DATASET}.resulted_in
    from_columns: [request_id]
    to_columns:   [outcome_id]
```

### Render the placeholders in binding.yaml

The materializer reads `binding.yaml` directly — there is no template step in the CLI — so substitute the shell variables once before any tool reads the file:

```bash
envsubst < binding.yaml > binding.yaml.tmp && mv binding.yaml.tmp binding.yaml
```

After this, `binding.yaml` should contain your real project ID and graph dataset name instead of the `${...}` markers. Skip this step and `materialize-window` will validate against literal `${PROJECT_ID}` text and fail closed.

### Apply the DDL

Table DDL runs first (the property graph references those tables; BigQuery rejects a `CREATE PROPERTY GRAPH` that points at tables that don't yet exist):

```bash
envsubst < table_ddl.sql      | bq query --use_legacy_sql=false
envsubst < property_graph.sql | bq query --use_legacy_sql=false
```

You should see five `CREATE TABLE` results and one `CREATE PROPERTY GRAPH` result. If you re-run, the `IF NOT EXISTS` clauses make the table creation idempotent and the property graph is replaced.

## Generate sample agent events
Duration: 0:08

In production, the BigQuery Agent Analytics Plugin captures events automatically as your ADK agent runs:

```python
from google.adk.plugins import BigQueryAgentAnalyticsPlugin

plugin = BigQueryAgentAnalyticsPlugin(
    project_id="your-project",
    dataset_id="agent_analytics_demo",
)
runner = Runner(agent=root_agent, plugins=[plugin])
```

For this codelab you'll skip the agent setup and use a small synthetic-event generator that writes the same shape of rows directly to `agent_events`. The script populates a handful of completed decision sessions so periodic materialization has something to chew on.

### Save the event generator

Save the following as `seed_events.py`:

```python
"""Synthetic agent_events generator for the periodic-materialization codelab.

Writes a small corpus of TOOL_COMPLETED + AGENT_COMPLETED events to
the configured agent_events table. Each "session" is a 3-step decision
flow: submit_request -> evaluate_option (x3) -> commit_outcome. The
session is closed by an AGENT_COMPLETED row, which is what
bqaa-materialize-window keys on.
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery

_EVENT_SCHEMA = [
    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("agent", "STRING"),
    bigquery.SchemaField("session_id", "STRING"),
    bigquery.SchemaField("invocation_id", "STRING"),
    bigquery.SchemaField("user_id", "STRING"),
    bigquery.SchemaField("trace_id", "STRING"),
    bigquery.SchemaField("span_id", "STRING"),
    bigquery.SchemaField("parent_span_id", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("error_message", "STRING"),
    bigquery.SchemaField("is_truncated", "BOOLEAN"),
    bigquery.SchemaField("content", "JSON"),
    bigquery.SchemaField("attributes", "JSON"),
    bigquery.SchemaField("latency_ms", "JSON"),
]


def _row(event_type: str, session_id: str, content: dict, ts: datetime) -> dict:
  return {
      "timestamp": ts.isoformat(),
      "event_type": event_type,
      "agent": "demo-agent",
      "session_id": session_id,
      "invocation_id": str(uuid.uuid4()),
      "user_id": "demo-user",
      "trace_id": session_id[:16],
      "span_id": str(uuid.uuid4())[:16],
      "parent_span_id": None,
      "status": "ok",
      "error_message": None,
      "is_truncated": False,
      "content": json.dumps(content),
      "attributes": "{}",
      "latency_ms": "{}",
  }


def _decision_session(now: datetime) -> list[dict]:
  session_id = f"sess-{uuid.uuid4().hex[:8]}"
  request_id = f"req-{uuid.uuid4().hex[:6]}"
  topics = ["approve loan", "schedule maintenance", "grant access", "release budget"]
  topic = random.choice(topics)
  rows: list[dict] = []

  rows.append(_row("TOOL_COMPLETED", session_id,
                   {"tool": "submit_request",
                    "result": {"request_id": request_id,
                               "request_text": f"Should we {topic}?"}},
                   now))

  options = [
      {"option_id": f"opt-{uuid.uuid4().hex[:5]}",
       "option_label": label,
       "confidence": round(random.uniform(0.1, 0.95), 2)}
      for label in ("yes", "no", "defer")
  ]
  for i, opt in enumerate(options):
    rows.append(_row("TOOL_COMPLETED", session_id,
                     {"tool": "evaluate_option",
                      "result": {"request_id": request_id, **opt}},
                     now + timedelta(seconds=i + 1)))

  selected = max(options, key=lambda o: o["confidence"])
  outcome_id = f"out-{uuid.uuid4().hex[:6]}"
  rationale = (f"Picked '{selected['option_label']}' "
               f"(confidence {selected['confidence']:.2f}) over "
               f"the {len(options)-1} alternatives.")
  rows.append(_row("TOOL_COMPLETED", session_id,
                   {"tool": "commit_outcome",
                    "result": {"request_id": request_id,
                               "outcome_id": outcome_id,
                               "status": "committed",
                               "rationale": rationale}},
                   now + timedelta(seconds=5)))

  rows.append(_row("AGENT_COMPLETED", session_id, {"final": True},
                   now + timedelta(seconds=6)))
  return rows


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--project-id", required=True)
  parser.add_argument("--dataset-id", required=True)
  parser.add_argument("--sessions", type=int, default=5)
  args = parser.parse_args()

  client = bigquery.Client(project=args.project_id)
  table_ref = f"{args.project_id}.{args.dataset_id}.agent_events"
  table = bigquery.Table(table_ref, schema=_EVENT_SCHEMA)
  table.time_partitioning = bigquery.TimePartitioning(field="timestamp")
  client.create_table(table, exists_ok=True)

  rows: list[dict] = []
  now = datetime.now(timezone.utc) - timedelta(minutes=10)
  for _ in range(args.sessions):
    rows.extend(_decision_session(now))
    now += timedelta(seconds=30)

  errors = client.insert_rows_json(table_ref, rows)
  if errors:
    raise RuntimeError(f"Insert errors: {errors}")
  print(f"Inserted {len(rows)} events across {args.sessions} sessions "
        f"into {table_ref}")


if __name__ == "__main__":
  main()
```

### Run the generator

```bash
pip install google-cloud-bigquery
python seed_events.py \
    --project-id "$PROJECT_ID" \
    --dataset-id "$EVENTS_DATASET" \
    --sessions 5
```

You should see "Inserted 30 events across 5 sessions into ...".

### Verify the events landed

```bash
bq query --use_legacy_sql=false \
    "SELECT event_type, COUNT(*) AS n FROM \`$PROJECT_ID.$EVENTS_DATASET.agent_events\` GROUP BY event_type ORDER BY n DESC"
```

You should see 15 `TOOL_COMPLETED` rows, 5 `AGENT_COMPLETED` rows, and possibly some others depending on how many times you ran the generator.

The `AGENT_COMPLETED` rows are the ones periodic materialization picks up — they mark a session as ready to materialize.

## Run materialization locally
Duration: 0:06

Before paying for the Cloud Run deploy, run the same code path locally. This catches IAM, binding, and dataset issues with a sub-minute feedback loop.

```bash
PYTHONPATH=$HOME/BigQuery-Agent-Analytics-SDK/src \
python -m bigquery_agent_analytics.cli materialize-window \
    --project-id "$PROJECT_ID" \
    --dataset-id "$EVENTS_DATASET" \
    --ontology ~/bqaa-codelab/ontology.yaml \
    --binding ~/bqaa-codelab/binding.yaml \
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

`ok: true` means the materializer found five completed sessions, extracted the decision flow from each via `AI.GENERATE`, and wrote the corresponding rows into the graph dataset.

Negative: if you see `ok: false` with `error_code = "empty_extraction"`, the most common cause is that the `aiplatform.googleapis.com` API hasn't propagated yet or your user account is missing `roles/aiplatform.user`. Wait a minute and retry, or grant the role:

```bash
USER_EMAIL=$(gcloud auth list --filter=status:ACTIVE --format="value(account)")
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="user:$USER_EMAIL" --role="roles/aiplatform.user"
```

### Verify the graph has rows

```bash
bq query --use_legacy_sql=false \
    "SELECT COUNT(*) AS n FROM \`$PROJECT_ID.$GRAPH_DATASET.decision_request\`"
```

You should see five rows. If you see zero, check that the local run reported `sessions_materialized > 0`.

## Run the deploy as a worked example
Duration: 0:10

The SDK ships `deploy_cloud_run_job.sh` under the migration v5 example directory. It runs `bqaa-materialize-window` as a Cloud Run Job, creates a runtime service account with the narrow IAM the job needs, wires a Cloud Scheduler trigger, and — with the `--smoke` flag — runs the job once after deploy to verify end-to-end. The script today bundles its own demo artifacts (the migration v5 ontology, binding, and property-graph DDL); the artifacts the codelab walked you through are not yet pluggable into this script. Adapting the script to accept arbitrary artifact paths is an open follow-up; file an issue or PR against the SDK repository if your team needs it.

For this codelab, the cleanest way to see the full Cloud Run + Cloud Scheduler shape end-to-end is to run the deploy as-is. It deploys the migration v5 example into your project — separate datasets from the ones you've been using — so you can observe a real `bqaa-periodic-materialization` job firing on cron, the JSON-log output, and the IAM grants the deploy expects. Once you've seen the moving parts you can fork the script to bundle your own artifacts.

### Deploy the migration v5 example

```bash
cd $HOME/BigQuery-Agent-Analytics-SDK/examples/migration_v5/periodic_materialization

# Use separate datasets so the demo deploy doesn't collide with
# the codelab's tables.
bq --location=US mk --dataset "$PROJECT_ID:bqaa_demo_events" || true
bq --location=US mk --dataset "$PROJECT_ID:bqaa_demo_graph"  || true

./deploy_cloud_run_job.sh \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --events-dataset bqaa_demo_events \
    --graph-dataset  bqaa_demo_graph \
    --schedule "0 */6 * * *" \
    --smoke
```

The first deploy takes about three minutes. You should see, in order:

1. Creating the runtime service account `bqaa-periodic-sa`.
2. Granting IAM: `dataViewer` on the events dataset, `dataEditor` on the graph dataset, `bigquery.jobUser` and `aiplatform.user` at the project, `run.invoker` on the Cloud Run Job for the scheduler service account.
3. Building the container via Cloud Build.
4. Deploying the Cloud Run Job `bqaa-periodic-materialization`.
5. Creating the Cloud Scheduler trigger `bqaa-periodic-materialization-cron` (cron `0 */6 * * *` — every six hours on the hour).
6. Running the smoke check.
7. A structured JSON report.

If the smoke check reports `ok: true`, the production-shape deploy is complete. The job will fire again at the top of the next six-hour window.

### Until the deploy script accepts your artifacts: cron the local command

For the codelab graph you authored, the simplest "every six hours" cadence today is to run the same `bqaa-materialize-window` command from a Cloud Build trigger (or Cloud Workflows, or any external scheduler) on a cron. The local command from the previous section is what fires; the artifacts live in your git repository or a Cloud Storage bucket the scheduler reads from. The migration v5 deploy script is the worked example of how to package the same command into a Cloud Run Job once you're ready.

### Read the JSON log

The smoke run's report is in Cloud Logging:

```bash
gcloud logging read \
    "resource.type=cloud_run_job AND jsonPayload.run_id!=\"\"" \
    --limit 5 --format json --project "$PROJECT_ID"
```

The fields to know:

- `jsonPayload.ok` — `true` on success, `false` on any failure mode.
- `jsonPayload.sessions_materialized` — how many sessions wrote rows this window.
- `jsonPayload.rows_materialized` — per-table row counts.
- `jsonPayload.failures[].error_code` — `empty_extraction` (AI/IAM issue) or `materialization_failed` (schema/write-perm issue).

A single Cloud Monitoring alert on `jsonPayload.ok = false` is the recommended posture. The `error_code` field tells the operator which Google Cloud configuration to inspect without log digging.

## Query the graph
Duration: 0:05

With the graph populated, the audit question is a single GQL traversal.

### The audit traversal

Save the following as `traversal.sql`:

```sql
SELECT *
FROM GRAPH_TABLE (
  ${GRAPH_DATASET}.agent_decisions_graph
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

You should see fifteen rows — three options per request, five requests — each showing the request, the option considered, its confidence, the final outcome, and the rationale.

For a single decision's full picture, filter by `request_id` and you get the row-set the audit team actually needs: the question that came in, the options that were weighed (with scores), and the rationale that was committed.

### The same answer in natural language

Once your project is on the BigQuery Conversational Analytics Preview, you can register the property graph as a knowledge source and ask the question in plain English:

> *"Why did the agent commit outcome X on request Y?"*

Conversational Analytics resolves the question against the graph and returns a structured answer card. See the [Conversational Analytics documentation](https://docs.cloud.google.com/bigquery/docs/conversational-analytics) for setup.

## Clean up
Duration: 0:03

Tear down what you created so you don't get billed for an idle Cloud Run Job. The resource names below match the defaults the deploy script uses (`bqaa-periodic-materialization` for the job, `bqaa-periodic-materialization-cron` for the scheduler, `bqaa-periodic-sa` for the service account). If you customized any of them with `--job-name`, substitute accordingly.

```bash
# Cloud Scheduler trigger
gcloud scheduler jobs delete \
    bqaa-periodic-materialization-cron \
    --location="$REGION" \
    --project="$PROJECT_ID" --quiet

# Cloud Run Job
gcloud run jobs delete \
    bqaa-periodic-materialization \
    --region="$REGION" \
    --project="$PROJECT_ID" --quiet

# BigQuery datasets (codelab + demo deploy)
bq rm -r -f --dataset "$PROJECT_ID:$EVENTS_DATASET"
bq rm -r -f --dataset "$PROJECT_ID:$GRAPH_DATASET"
bq rm -r -f --dataset "$PROJECT_ID:bqaa_demo_events" 2>/dev/null || true
bq rm -r -f --dataset "$PROJECT_ID:bqaa_demo_graph"  2>/dev/null || true

# Runtime service account (optional)
gcloud iam service-accounts delete \
    "bqaa-periodic-sa@$PROJECT_ID.iam.gserviceaccount.com" \
    --project="$PROJECT_ID" --quiet
```

## Congratulations
Duration: 0:02

You have:

- Provided a BigQuery property graph for your agent's decision domain.
- Populated `agent_events` with a synthetic event corpus.
- Run `bqaa-materialize-window` locally to fill the graph from those events.
- Deployed a Cloud Run Job + Cloud Scheduler trigger that keeps the graph fresh every six hours.
- Queried the graph in GQL and seen the audit-style answer.

The pattern works wherever an agent makes consequential decisions: credit underwriting, prior authorization, marketing budget moves, procurement, customer service, internal IT. Swap the demo graph for the one your team designs for your domain, point the deploy at your `agent_events` table, and the audit answer is one query away.

### Further reading

- [BigQuery Agent Analytics SDK repository](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
- [Customer playbook for periodic materialization](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/periodic_materialization/README.md) — required APIs, IAM matrix, recommended schedules, Cloud Monitoring alert queries, troubleshooting.
- [BigQuery property graphs documentation](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/graph-intro) (Preview).
- [BigQuery Conversational Analytics documentation](https://docs.cloud.google.com/bigquery/docs/conversational-analytics) (Preview).

The hard part of agent governance was never the events. It was the join, the traversal, and the cadence. With `bqaa-materialize-window` on whatever cron your team already runs, all three are one query away.
