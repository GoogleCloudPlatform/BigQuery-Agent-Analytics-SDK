summary: Build scheduled, audit-grade decision traces for AI agents by materializing BigQuery property graphs from agent event logs. You will define a property graph contract for a sample decision domain, run the bqaa-materialize-window CLI to populate it, then observe the same code path running in a production-shape Cloud Run Job and Cloud Scheduler deploy demonstrated against the SDK's bundled migration v5 artifacts. Conclude with a Graph Query Language (GQL) audit traversal.
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

This codelab uses the `bqaa-materialize-window` command in the BigQuery Agent Analytics SDK to continuously convert raw agent event logs into a queryable BigQuery property graph, on a schedule, without any external graph database or ETL pipeline. The companion blog post is [Trace AI Agent Decisions with BigQuery Property Graphs](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/docs/blog/periodic_materialization.md).

### Two Paths Walked Side by Side

This codelab walks two paths so the boundary is clear before any production work begins:

* **Custom Graph Path (most of the codelab).** You author the graph contract (table DDL, property-graph DDL, ontology, binding) for a sample DecisionRequest decision flow, seed synthetic events, run `bqaa-materialize-window` directly, and query the result in GQL. Cron this same command from Cloud Build, Cloud Workflows, or any external scheduler to keep your own graph fresh.
* **Production-Deploy Shape (one section near the end).** The SDK's `deploy_cloud_run_job.sh` script is demonstrated against the *bundled migration v5 demo artifacts*, not the codelab's custom graph. The deploy script does not yet accept arbitrary artifact paths; that work is tracked as an open follow-up in the SDK repository. The purpose of running the deploy is to observe the production shape end-to-end: split service accounts, retry budget, structured JSON logs, and the state-table audit trail.

### What You Will Build

* A BigQuery property graph that models a generic agent decision flow (Decision Request → Decision Option → Decision Outcome).
* A populated `agent_events` table containing a small synthetic event corpus you can re-generate.
* A working `bqaa-materialize-window` run that fills the graph from those events using the default `AI.GENERATE` extraction mode, plus a tour of the zero-LLM `--extraction-mode=compiled-only` audited path exercised end-to-end by the migration v5 demo.
* A one-shot backfill against a historical window using `--backfill --from / --to --state-key-suffix`, isolated from the live cron's high-water mark.
* A Cloud Run Job and Cloud Scheduler trigger walked end-to-end against the SDK's bundled migration v5 artifacts, deployed with the production defaults shipped in 0.3.2: split runtime and scheduler service accounts, tunable `--max-retries`, and an opt-in orphan-session watchdog.
* The same deploy expressed as a Terraform module, the infrastructure-as-code mirror of the bash deploy.
* An audit-style GQL query that traces a single decision end-to-end.

### What You Will Learn

* How the BigQuery Agent Analytics Plugin writes to `agent_events`.
* How to author a property graph contract (DDL, ontology, binding) for an agent decision domain.
* How to run `bqaa-materialize-window` against a custom graph, and how to deploy the same command as a Cloud Run Job and Cloud Scheduler trigger (using either the bash script or the Terraform module).
* The 0.3.2 production-grade capabilities: split service accounts, retry budget, orphan watchdog, compiled-only extraction.
* How to backfill a historical window without disturbing the live cron's checkpoint.
* How to query a BigQuery property graph in GQL.

### What You Will Need

* A Google Cloud project with billing enabled.
* Owner or Editor role on that project. You will create datasets, deploy a Cloud Run Job, and grant IAM.
* The `gcloud` CLI installed and authenticated, or access to Cloud Shell.
* Python 3.10 or newer.
* Familiarity with BigQuery SQL. GQL knowledge is not required.

**Total time: about 45 minutes.**

## Before You Begin
Duration: 0:05

### Pick a Project and Region

Open Cloud Shell or a local terminal:

```bash
export PROJECT_ID="your-project-id"
export REGION="us-central1"
export EVENTS_DATASET="agent_analytics_demo"
export GRAPH_DATASET="agent_graph_demo"
gcloud config set project "$PROJECT_ID"
```

### Enable the Required APIs

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

The `aiplatform.googleapis.com` API is required because the SDK's default extraction path calls BigQuery's `AI.GENERATE` to extract entities and relationships from event content. The reference-extractor path that ships with the SDK skips this dependency for known event shapes, but the demo here uses the `AI.GENERATE` fallback so the codelab works without any custom extractor code.

### Create Two BigQuery Datasets

Periodic materialization treats events and graph as separate datasets so you can grant IAM narrowly. Create both:

```bash
bq --location=US mk --dataset "$PROJECT_ID:$EVENTS_DATASET"
bq --location=US mk --dataset "$PROJECT_ID:$GRAPH_DATASET"
```

You should see "Dataset '...' successfully created" twice. If a dataset already exists, the command errors harmlessly. Leave it in place.

## Installation and Setup
Duration: 0:03

### Clone the SDK Repository

The deploy script, the Terraform module, and the demo agent script all live in the SDK repository:

```bash
git clone https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK.git
cd BigQuery-Agent-Analytics-SDK
```

### Set Up a Python Virtual Environment

Pick one of the two install paths. The codelab works either way.

**Option A: Editable from the Clone** (recommended if you want to read the SDK source or iterate on it while you go):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

**Option B: Pinned from PyPI** (recommended if you are treating the SDK as a black box):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install 'bigquery-agent-analytics>=0.3.2'
```

Release 0.3.2 closes the migration v5 production track tracked in [issue #187](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/187) and lands every flag this codelab uses. The single quotes around the pin are required so the shell does not interpret `>=` as a redirection operator. Install takes about a minute either way.

Verify (use the first form if you took Option A, the second if you took Option B):

```bash
# Option A: editable install
PYTHONPATH=src python -m bigquery_agent_analytics.cli --help | head -8

# Option B: PyPI install (no PYTHONPATH needed)
bqaa-materialize-window --help | head -8
```

You should see the CLI banner. The rest of the codelab uses the Option A invocation (`PYTHONPATH=src python -m bigquery_agent_analytics.cli materialize-window ...`); if you took Option B, substitute `bqaa-materialize-window` for that whole prefix.

### Authenticate

If you are on a workstation:

```bash
gcloud auth login
gcloud auth application-default login
```

Cloud Shell users can skip this step; credentials are already configured.

## Phase 1: Define the Property Graph
Duration: 0:08

A property graph in BigQuery represents your domain-specific decision model: the agent's context, decision points, alternatives evaluated, and selected outcomes. You define this model once using standard Data Definition Language (DDL).

For this codelab, you will author four artifacts that together describe the decision domain:

1. **Property graph DDL** (`property_graph.sql`) that ties node and edge tables into a queryable graph.
2. **Node and edge table DDL** (`table_ddl.sql`) for the physical tables the materializer writes into.
3. **Ontology** (`ontology.yaml`) that names the entities and their primary keys.
4. **Binding** (`binding.yaml`) that maps ontology entities to physical BigQuery tables.

In a production deployment, these four artifacts are the only graph contract your team authors. The SDK's bundled migration v5 demo provides a complete starting point you can copy and adapt for your own domain. The demo graph below models a generic agent decision flow: a request comes in, the agent weighs options, an outcome is committed.

```
DecisionRequest --[evaluatesOption]--> DecisionOption
              \--[resultedIn]--------> DecisionOutcome
```

### Save the Property Graph DDL

Create a working directory and save the property-graph schema. The `${PROJECT_ID}` and `${GRAPH_DATASET}` placeholders are substituted by your shell when you apply the DDL:

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

### Save the Node and Edge Table DDL

The materializer writes into BigQuery tables, so you create them before the first run. Save as `table_ddl.sql`:

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

### Save the Ontology

The materializer pairs your property graph with a small ontology file, the entity vocabulary the SDK uses when it constructs the `AI.GENERATE` extraction prompt. In a production deployment you author this once alongside your graph. Save as `ontology.yaml`:

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

### Save the Binding

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

`from_columns` (and `to_columns`) accept two entry shapes inside the list. The list-of-strings shape above (`[request_id]`) works when the foreign-key column on the edge has the same name as the primary-key property on the source entity. When the FK column has a different name, or when the edge is a self-edge (a relationship from an entity type back to itself, where both endpoints would otherwise collide on the same column name), use the list-of-single-key-dicts shape so the materializer can disambiguate:

```yaml
# List of {edge_column: target_PK_property} single-key dicts.
# Use this when the edge column name differs from the source
# entity's PK property, or for any self-edge.
from_columns: [{parent_request_id: request_id}]
to_columns:   [{child_request_id:  request_id}]
```

The outer list is always required (`from_columns` is `list[ColumnRef]`, never a bare dict). For composite primary keys, give one single-key dict entry per key column. The dict shape is required for self-edges and recommended whenever the FK column does not share the source PK property's name. For this codelab's binding both edge columns share the source PK's name, so the list-of-strings shape works as-is.

### Render the Placeholders in binding.yaml

The materializer reads `binding.yaml` directly. There is no template step in the CLI, so substitute the shell variables once before any tool reads the file:

```bash
envsubst < binding.yaml > binding.yaml.tmp && mv binding.yaml.tmp binding.yaml
```

After this, `binding.yaml` should contain your real project ID and graph dataset name instead of the `${...}` markers. If you skip this step, `materialize-window` validates against literal `${PROJECT_ID}` text and fails closed.

### Apply the DDL

Table DDL runs first. The property graph references those tables, and BigQuery rejects a `CREATE PROPERTY GRAPH` that points at tables that do not yet exist:

```bash
envsubst < table_ddl.sql      | bq query --use_legacy_sql=false
envsubst < property_graph.sql | bq query --use_legacy_sql=false
```

You should see five `CREATE TABLE` results and one `CREATE PROPERTY GRAPH` result. If you re-run, the `IF NOT EXISTS` clauses make the table creation idempotent and the property graph is replaced.

## Phase 2: Ingest Sample Agent Events
Duration: 0:08

In production, the BigQuery Agent Analytics Plugin captures events automatically as your ADK agent runs:

```python
from google.adk.plugins import BigQueryAgentAnalyticsPlugin

plugin = BigQueryAgentAnalyticsPlugin(
    project_id="your-project-id",
    dataset_id="agent_analytics_demo",
)
runner = Runner(agent=root_agent, plugins=[plugin])
```

For this codelab you skip the agent setup and use a small synthetic-event generator that writes the same shape of rows directly to `agent_events`. The script populates a handful of completed decision sessions so periodic materialization has something to process.

### Save the Event Generator

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

### Run the Generator

```bash
pip install google-cloud-bigquery
python seed_events.py \
    --project-id "$PROJECT_ID" \
    --dataset-id "$EVENTS_DATASET" \
    --sessions 5
```

You should see "Inserted 30 events across 5 sessions into ...".

### Verify the Events Landed

```bash
bq query --use_legacy_sql=false \
    "SELECT event_type, COUNT(*) AS n FROM \`$PROJECT_ID.$EVENTS_DATASET.agent_events\` GROUP BY event_type ORDER BY n DESC"
```

You should see 15 `TOOL_COMPLETED` rows, 5 `AGENT_COMPLETED` rows, and possibly some others depending on how many times you ran the generator.

The `AGENT_COMPLETED` rows are the session terminators periodic materialization picks up. They mark a session as ready to materialize.

## Phase 3: Execute the Materializer Locally
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

`ok: true` indicates the materializer found five completed sessions, extracted the decision flow from each via `AI.GENERATE`, and wrote the corresponding rows into the graph dataset.

If you see `ok: false` with `error_code = "empty_extraction"`, the most common cause is that the `aiplatform.googleapis.com` API has not propagated yet, or your user account is missing `roles/aiplatform.user`. Wait a minute and retry, or grant the role:

```bash
USER_EMAIL=$(gcloud auth list --filter=status:ACTIVE --format="value(account)")
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="user:$USER_EMAIL" --role="roles/aiplatform.user"
```

### Verify the Graph Has Rows

```bash
bq query --use_legacy_sql=false \
    "SELECT COUNT(*) AS n FROM \`$PROJECT_ID.$GRAPH_DATASET.decision_request\`"
```

You should see five rows. If you see zero, check that the local run reported `sessions_materialized > 0`.

### A Note on the Zero-LLM Extraction Path

The local run above uses the default extractor, which calls BigQuery's `AI.GENERATE` to extract entities and relationships from event content. The SDK also ships a `--extraction-mode=compiled-only` flag that swaps in a **reference-extractor module**: deterministic Python keyed to your ontology, no Vertex AI dependency, the audited code path. Production deployments that need to certify their data path to a regulator typically run `--extraction-mode=compiled-only` and remove `roles/aiplatform.user` from the runtime service account entirely. (The word "compiled" here describes the extraction *mode*; it does not refer to fingerprint-stable compiled bundles. The `--bundles-root` path for compiled bundles is a separate, orthogonal surface.)

Running compiled-only mode requires a `reference_extractor.py` keyed to your ontology. The SDK's migration v5 example ships a reference extractor, which is what the live notebook smoke tests run against. For your own graph you author one extractor module that maps event-content shape to entity-and-relationship dicts; the materializer wires the rest. The codelab's custom DecisionRequest graph does not ship a reference extractor, so the codelab stays on the `AI.GENERATE` default. When you are ready, the migration v5 reference extractor at [`examples/migration_v5/reference_extractor.py`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/reference_extractor.py) is the template to copy from.

### Backfill a Historical Window

The same code path the cron uses can also be pointed at a fixed historical window. This is useful when events arrived during an outage, when a binding change requires a one-shot re-extraction, or when an audit committee asks for a specific quarter. Backfill mode writes its high-water mark to an **isolated** state-table namespace (controlled by `--state-key-suffix`) so it never disturbs the live cron's checkpoint.

Re-seed a few events with a backdated timestamp to give the backfill something to find:

```bash
python seed_events.py \
    --project-id "$PROJECT_ID" \
    --dataset-id "$EVENTS_DATASET" \
    --sessions 3
```

Then run a backfill against the last 48 hours, into an isolated state-table namespace:

```bash
FROM=$(date -u -d "48 hours ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
       || date -u -v-48H +"%Y-%m-%dT%H:%M:%SZ")
TO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

PYTHONPATH=$HOME/BigQuery-Agent-Analytics-SDK/src \
python -m bigquery_agent_analytics.cli materialize-window \
    --project-id "$PROJECT_ID" \
    --dataset-id "$EVENTS_DATASET" \
    --ontology ~/bqaa-codelab/ontology.yaml \
    --binding ~/bqaa-codelab/binding.yaml \
    --lookback-hours 1 \
    --backfill --from "$FROM" --to "$TO" \
    --state-key-suffix codelab_backfill_demo \
    --format json
```

(The `date -u -d ...` form is GNU `date` on Linux and Cloud Shell; the `date -u -v-48H` form is BSD `date` on macOS. The `||` falls back to the macOS form if the GNU form fails.)

You should see a JSON report with the backfill window's `sessions_materialized` count, and the state table now has a new row whose `state_key` is hashed from the suffix you passed:

```bash
bq query --use_legacy_sql=false \
    "SELECT mode, scan_start, scan_end, sessions_materialized, ok \
     FROM \`$PROJECT_ID.$GRAPH_DATASET._bqaa_materialization_state\` \
     ORDER BY run_started_at DESC LIMIT 5"
```

You should see at least two rows: one from the cron-style local run earlier in this section (a `mode = 'steady'` row corresponding to the standard scan), and one from the backfill (`mode = 'backfill'`, with a different `state_key` because the suffix changes the hash input). The live cron's high-water mark, sitting under its own `state_key`, is untouched. That is the property that lets backfill run concurrently with the production cron.

## Phase 4: Query the Decision Trace
Duration: 0:05

Once the materialization job has run, your property graph is ready for analysis. Using standard Graph Query Language (GQL) syntax in BigQuery, you can traverse the decision graph to pull every option the agent considered, the final choice, and the recorded rationale.

### The Audit Traversal

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

You should see fifteen rows: three options per request, five requests. Each row shows the request, the option considered, its confidence, the final outcome, and the rationale.

For a single decision's full picture, filter by `request_id` and you get the row set the audit team needs: the question that came in, the options that were weighed (with scores), and the rationale that was committed.

### The Same Answer in Natural Language

Once your project is on the BigQuery Conversational Analytics Preview, you can register the property graph as a knowledge source and ask the question in plain English:

> *"Why did the agent commit outcome X on request Y?"*

Conversational Analytics resolves the question against the graph and returns a structured answer card. See the [Conversational Analytics documentation](https://cloud.google.com/bigquery/docs/conversational-analytics) for setup.

## Production Deployment with Bundled Artifacts
Duration: 0:12

This section observes the production-deploy shape end-to-end. It uses the SDK's bundled migration v5 demo artifacts, **not** the codelab's custom DecisionRequest graph. The deploy script does not yet accept arbitrary artifact paths, so the codelab's custom graph runs through `materialize-window` directly (as you did in Phase 3) or wrapped in a Cloud Build or Cloud Workflows scheduler that calls the same CLI. The purpose of running the deploy here is to see the production resource shape: split service accounts, retry budget, the JSON-log envelope, and the state-table audit trail.

The SDK's `deploy_cloud_run_job.sh` script runs `bqaa-materialize-window` as a Cloud Run Job, creates the service accounts with the narrow IAM the deploy needs, wires a Cloud Scheduler trigger, and, with the `--smoke` flag, runs the job once after deploy to verify end-to-end. The 0.3.2 release of the script lands every resource with **production defaults**: split runtime and scheduler service accounts, `--max-retries=2`, a structured JSON log on every run, and the state-table audit trail. The remaining flags (compiled-only, orphan watchdog, backfill, Terraform) opt into stricter or incident-response or IaC-aligned workflows.

### Deploy the Migration v5 Example

```bash
cd $HOME/BigQuery-Agent-Analytics-SDK/examples/migration_v5/periodic_materialization

# Use separate datasets so the demo deploy does not collide with
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

1. Creation of the **runtime** service account `bqaa-periodic-runtime-sa` and the **scheduler-caller** service account `bqaa-periodic-scheduler-sa`. Two SAs by default. The runtime SA holds the BigQuery and Vertex AI permissions the Cloud Run Job needs at execution time; the scheduler SA holds only `roles/run.invoker` on the job. This is the least-privilege posture. If you want the legacy single-SA shape, pass `--single-sa` and the script creates a combined `bqaa-periodic-sa` instead.
2. IAM grants: `dataViewer` on the events dataset and `dataEditor` on the graph dataset for the runtime SA; `bigquery.jobUser` and (in default `ai-fallback` extraction mode) `aiplatform.user` at the project level; `run.invoker` on the Cloud Run Job for the scheduler SA.
3. Container build via Cloud Build.
4. Deployment of the Cloud Run Job `bqaa-periodic-materialization` with environment variables for `--max-retries`, `--lookback-hours`, `--overlap-minutes`, and the extraction mode.
5. Creation of the Cloud Scheduler trigger `bqaa-periodic-materialization-cron` (cron `0 */6 * * *`, every six hours on the hour) authenticated as the scheduler SA.
6. The smoke check run.
7. A structured JSON report.

If the smoke check reports `ok: true`, the production-shape deploy is complete. The job fires again at the top of the next six-hour window.

### Production-Grade Capabilities

The 0.3.2 release of the BigQuery Agent Analytics SDK includes several features designed to support enterprise-grade deployments. The deploy you just ran uses the defaults; the optional flags below add stricter controls when your operating model needs them.

* **Least-Privilege Split Service Accounts.** Default. The deployment scripts separate execution privileges across two service accounts as described above.
* **Tunable Retry Budget (`--max-retries`).** Default `2`. Governs the in-window retry budget for transient failures. Surfaces as the `BQAA_MAX_RETRIES` environment variable inside the Cloud Run Job.
* **Deterministic Parsing (`--extraction-mode compiled-only`).** Opt-in. The zero-LLM audited path. Disables LLM calls entirely and uses predefined Python parsing logic to guarantee deterministic, reproducible graph builds. Requires a reference extractor keyed to your ontology (the bundled migration v5 example ships one). Removes the `roles/aiplatform.user` grant from the runtime SA automatically.
* **Outage Resiliency and Backfills (`--max-session-age-hours`, `--backfill`).** Opt-in. Sessions older than the watchdog cap that have not terminated are flagged as orphaned and written to the state table with `mode = 'orphan_scan'`, so an operator can drain stale state without the cron silently re-pulling broken sessions. Backfill mode re-materializes a fixed historical window without affecting your active schedule's progress markers.
* **Infrastructure-as-Code Integration.** Opt-in. The SDK includes a complete Terraform module to provision all necessary GCP resources with consistent IAM configurations. See the next subsection.
* **Cap on Per-Window Batch Size (`--max-sessions`).** Default unlimited. Useful for hostile event spikes.
* **Single-SA Escape Hatch (`--single-sa`).** Opt-in. Collapses the two SAs into one combined `bqaa-periodic-sa` for migration ergonomics from earlier versions.

The deploy as written above takes none of the opt-in flags; it uses the production defaults. Re-running with any of them is idempotent. The script skips resources that already exist and updates only what changed.

### Read the JSON Log

The smoke run's report lands in Cloud Logging:

```bash
gcloud logging read \
    "resource.type=cloud_run_job AND jsonPayload.run_id!=\"\"" \
    --limit 5 --format json --project "$PROJECT_ID"
```

The fields to know:

* `jsonPayload.ok`: `true` on success, `false` on any failure mode.
* `jsonPayload.sessions_materialized`: how many sessions wrote rows this window.
* `jsonPayload.rows_materialized`: per-table row counts.
* `jsonPayload.failures[].error_code`: `empty_extraction` (AI or IAM issue), `materialization_failed` (schema or write-permission issue), or, when `--max-session-age-hours` is enabled, `session_orphaned` (session exceeded the watchdog age cap; emitted only when the watchdog is on).

A single Cloud Monitoring alert on `jsonPayload.ok = false` is the recommended posture. The `error_code` field tells the operator which Google Cloud configuration to inspect without log digging.

### The Terraform Alternative

Teams that operate infrastructure as code can land the same six resources through a Terraform module instead of the bash script. The module lives at [`examples/migration_v5/periodic_materialization/terraform/`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization/terraform) and mirrors the bash deploy exactly: same SA names, same IAM grants, same Cloud Run Job environment variables, same scheduler trigger. The bash deploy is lighter-weight onboarding; the Terraform module is the same six resources behind `terraform plan`, drift detection, and multi-environment promotion.

The Terraform module takes the **container image URI as a required input**; it does not build the image inline the way the bash deploy does. The SDK bundles a `build_image.sh` helper that stages the exact layout the bash deploy assembles (run script, reference extractor, demo artifacts, vendored SDK source, `Procfile`, `requirements.txt`) and runs `gcloud builds submit` against the staging directory. Same image contents either way; Terraform consumes the publish artifact instead of doing the build inline.

If you have already run the bash deploy in this codelab and want to see the Terraform path, skip ahead to the cleanup section first to free the resource names. Then:

```bash
# Build and publish the container image.
cd $HOME/BigQuery-Agent-Analytics-SDK
IMAGE_URI="$(./examples/migration_v5/periodic_materialization/build_image.sh \
    --project "$PROJECT_ID" \
    --repo bqaa \
    --region "$REGION" \
    --create-repo)"
echo "$IMAGE_URI"
# Example output:
# us-central1-docker.pkg.dev/your-project/bqaa/periodic-materialization:<tag>

# Configure Terraform variables.
cd examples/migration_v5/periodic_materialization/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set project_id, region, image_uri, etc.

# Plan and apply.
terraform init
terraform plan -out=tfplan
terraform apply tfplan

# Smoke the deployed job.
gcloud run jobs execute "$(terraform output -raw cloud_run_job_name)" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --wait
```

`terraform output` then prints the runtime SA email, scheduler SA email, the Cloud Run Job name, and the scheduler trigger name as machine-readable values your downstream wiring can consume. Tear down with `terraform destroy` instead of the per-resource `gcloud ... delete` commands.

See [`terraform/README.md`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/periodic_materialization/terraform/README.md) for the full variable reference, the recommended GCS-backend state-file block, and the bash-vs-Terraform comparison matrix.

## Clean Up
Duration: 0:03

Tear down what you created so you do not get billed for an idle Cloud Run Job. If you used the **Terraform path**, the cleanest teardown is one command:

```bash
cd $HOME/BigQuery-Agent-Analytics-SDK/examples/migration_v5/periodic_materialization/terraform
terraform destroy
```

If you used the **bash deploy path**, the per-resource teardown follows. The resource names below match the deploy script's defaults: `bqaa-periodic-materialization` for the job, `bqaa-periodic-materialization-cron` for the scheduler, `bqaa-periodic-runtime-sa` and `bqaa-periodic-scheduler-sa` for the two service accounts (split SAs are the 0.3.2 default; if you passed `--single-sa`, delete `bqaa-periodic-sa` instead). If you customized the job name with `--job-name`, substitute accordingly.

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

# Service accounts (split-SA default; delete both)
gcloud iam service-accounts delete \
    "bqaa-periodic-runtime-sa@$PROJECT_ID.iam.gserviceaccount.com" \
    --project="$PROJECT_ID" --quiet 2>/dev/null || true
gcloud iam service-accounts delete \
    "bqaa-periodic-scheduler-sa@$PROJECT_ID.iam.gserviceaccount.com" \
    --project="$PROJECT_ID" --quiet 2>/dev/null || true

# If you deployed with --single-sa, this is the one to delete instead:
gcloud iam service-accounts delete \
    "bqaa-periodic-sa@$PROJECT_ID.iam.gserviceaccount.com" \
    --project="$PROJECT_ID" --quiet 2>/dev/null || true
```

## Summary
Duration: 0:02

You have, for the **custom graph**:

* Authored a BigQuery property graph contract (table DDL, property-graph DDL, ontology, and binding) for a generic agent decision domain.
* Populated `agent_events` with a synthetic event corpus.
* Run `bqaa-materialize-window` locally against that custom graph, in the default `AI.GENERATE` extraction mode.
* Backfilled a historical window into an isolated state-table namespace without disturbing the live cron's checkpoint.
* Queried the resulting graph in GQL and seen the audit-style answer.

And for the **production-deploy shape** (demonstrated against the bundled migration v5 artifacts):

* Deployed a Cloud Run Job and Cloud Scheduler trigger that runs every six hours with the 0.3.2 defaults: split runtime and scheduler service accounts, retry budget, structured JSON logs, and the state-table audit trail.
* Seen which knobs are opt-in: `--max-session-age-hours` orphan watchdog, `--extraction-mode=compiled-only` zero-LLM path, `--single-sa` for the legacy single-SA shape.
* Seen the same deploy expressed as a Terraform module that drops into an existing IaC pipeline.

The pattern works wherever an agent makes consequential decisions: credit underwriting, prior authorization, marketing budget moves, procurement, customer service, and internal IT. For your own graph today, author the contract (using the codelab's DecisionRequest example or the migration v5 demo as a starting point), then cron `bqaa-materialize-window` from Cloud Build, Cloud Workflows, or an external scheduler. The deploy script's wrapper shape (the SAs, the scheduler trigger, the JSON logs) is the same one to adopt once your team is ready to package the command as a Cloud Run Job. Adapting the deploy to accept arbitrary artifact paths is an open follow-up tracked against the SDK repository.

### Further Reading

* [BigQuery Agent Analytics SDK repository](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
* [Companion blog post: Trace AI Agent Decisions with BigQuery Property Graphs](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/docs/blog/periodic_materialization.md)
* [Customer playbook for periodic materialization](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/periodic_materialization/README.md): required APIs, IAM matrix, recommended schedules, Cloud Monitoring alert queries, troubleshooting.
* [Terraform module for periodic materialization](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization/terraform): IaC mirror of the bash deploy, same six resources, variable reference, GCS state-backend block, and the bash-vs-Terraform comparison.
* [Migration v5 demo notebook (Beats 1–5)](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5_demo_notebook.ipynb): the end-to-end walk through the SDK's four decision-lineage guarantees plus the outcome-signal feedback and reward loop, run against a live BigQuery project.
* [Reference extractor pattern](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/reference_extractor.py): the template for the compiled-only extraction path your team would author for a regulated deployment.
* [CHANGELOG `[0.3.2]` block](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/CHANGELOG.md): the full Added and Fixed surface from the migration v5 production-track release, with PR references for every flag mentioned in this codelab.
* [BigQuery property graphs documentation](https://cloud.google.com/bigquery/docs/reference/standard-sql/graph-intro) (Preview).
* [BigQuery Conversational Analytics documentation](https://cloud.google.com/bigquery/docs/conversational-analytics) (Preview).
