summary: Keep a BigQuery property graph of your AI agent's decisions fresh from agent_events on a six-hour schedule. You will materialize a custom property graph locally with bqaa-materialize-window (cron from Cloud Build / Workflows / external scheduler), then see the same code path run as a production-shape Cloud Run Job + Cloud Scheduler deploy demonstrated against the SDK's bundled migration v5 artifacts. End with an audit-style GQL traversal.
id: bqaa-periodic-materialization
categories: bigquery,adk,agents
tags: bigquery,adk,bigquery-agent-analytics,cloud-run,cloud-scheduler,property-graph,gql
status: Draft
authors: BigQuery Agent Analytics team
feedback link: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues

# Periodic materialization for BigQuery Agent Analytics

## Introduction
Duration: 0:03

If your AI agent makes decisions that someone will eventually want explained — a credit decline, a marketing reallocation, a procurement pick, an access grant — the gap between "events captured in BigQuery" and "auditable explanation on demand" is usually filled by an engineer with a SQL editor. In this codelab you will close that gap by running `bqaa-materialize-window` against a property graph you provide, then seeing the same command wrapped in a production-shape Cloud Run Job + Cloud Scheduler deploy, and finally querying the resulting graph in GQL.

This codelab walks **two paths** side by side so the boundary is clear before you copy anything to production:

- **Custom graph path** (most of the codelab) — you author the graph contract (table DDL, property-graph DDL, ontology, binding) for a small DecisionRequest decision flow, seed events, and run `bqaa-materialize-window` directly. Cron this same command from Cloud Build, Cloud Workflows, or any external scheduler to keep your own graph fresh.
- **Production-deploy shape** (one section near the end) — the SDK's `deploy_cloud_run_job.sh` is demonstrated against the **bundled migration v5 demo artifacts** (not the codelab's custom graph; the deploy script doesn't yet accept arbitrary artifact paths, tracked as an open follow-up). The point of running the deploy is to observe the production shape end-to-end: split service accounts, retries, structured JSON logs, the state-table audit trail.

The codelab is self-contained from scratch. You will create the BigQuery datasets, the property graph, the demo events, run materialization (custom graph) and the production deploy (bundled artifacts), and the query — all in one Google Cloud project. At the end the cleanup section tears down both paths (the bash deploy uses several `gcloud`/`bq` commands; the Terraform path uses one `terraform destroy`).

### What you'll build

- A BigQuery property graph that describes a generic agent decision flow (Decision Request → Decision Option → Decision Outcome).
- A populated `agent_events` table with a small synthetic event corpus you can re-generate.
- A working `bqaa-materialize-window` run that fills the graph from those events in the default `AI.GENERATE` extraction mode, plus a tour of the zero-LLM `--extraction-mode=compiled-only` audited path (which the migration v5 demo exercises end-to-end against a real reference-extractor module). Cron this command from your scheduler of choice (Cloud Build, Workflows, external cron) to keep the graph fresh.
- A one-shot backfill against a historical window using `--backfill --from / --to --state-key-suffix` — the same code path the cron uses, isolated from the live high-water mark.
- A Cloud Run Job + Cloud Scheduler trigger walked end-to-end against the SDK's bundled migration v5 demo artifacts, deployed with the production-shape defaults shipped in 0.3.2: **split runtime + scheduler-caller service accounts, tunable `--max-retries`, and an opt-in orphan-session watchdog**. (The deploy script bundles the migration v5 artifacts today; for the codelab's custom graph you'll cron `materialize-window` directly. Adapting the deploy script to accept arbitrary artifact paths is tracked as an open follow-up.)
- The same deploy as a Terraform module — the IaC mirror of the bash deploy, same six resources, behind `terraform plan` / drift detection.
- An audit-style GQL query that traces a single decision end-to-end.

### What you'll learn

- How the BigQuery Agent Analytics Plugin writes to `agent_events`.
- How to deploy `bqaa-materialize-window` as a Cloud Run Job with a Cloud Scheduler trigger — bash today, or Terraform if your team operates IaC.
- The 0.3.2 production-shape deploy surface: split SAs, retries, orphan watchdog, compiled-only extraction.
- How to backfill a historical window without disturbing the live cron.
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

`aiplatform.googleapis.com` is required because the SDK's default extraction path calls BigQuery's `AI.GENERATE` to extract entities and relationships from event content. The reference-extractor path that ships with the SDK skips this dependency for known event shapes, but the demo here uses the `AI.GENERATE` fallback so the codelab works without any custom extractor code.

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

The deploy script, the Terraform module, and the demo agent script live in the SDK repository:

```bash
git clone https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK.git
cd BigQuery-Agent-Analytics-SDK
```

### Set up a Python virtual environment

Pick one of the two install paths. The codelab works either way.

**Option A: editable from the clone** (recommended if you want to read the source or iterate on the SDK while you go):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

**Option B: pinned from PyPI** (recommended if you're treating the SDK as a black box):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install 'bigquery-agent-analytics>=0.3.2'
```

0.3.2 is the release that closes the migration v5 production track (issue [#187](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/187)) and lands every flag this codelab uses. The single-quotes around the pin are required so the shell doesn't interpret `>=` as a redirection operator. Install takes about a minute either way.

Verify (use the first form if you took Option A, the second if you took Option B):

```bash
# Option A — editable install
PYTHONPATH=src python -m bigquery_agent_analytics.cli --help | head -8

# Option B — PyPI install (no PYTHONPATH needed)
bqaa-materialize-window --help | head -8
```

You should see the CLI banner. The rest of the codelab uses the Option A invocation (`PYTHONPATH=src python -m bigquery_agent_analytics.cli materialize-window ...`); if you took Option B, substitute `bqaa-materialize-window` for that whole prefix.

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

`from_columns` (and `to_columns`) accept two entry shapes inside the list. The list-of-strings shape above (`[request_id]`) works when the foreign-key column on the edge has the same name as the primary-key property on the source entity. When the FK column has a different name — or when the edge is a self-edge (a relationship from an entity type back to itself, so both endpoints would otherwise collide on the same column name) — use the list-of-single-key-dicts shape so the materializer can disambiguate:

```yaml
# List of {edge_column: target_PK_property} single-key dicts.
# Use this when the edge column name differs from the source
# entity's PK property, or for any self-edge.
from_columns: [{parent_request_id: request_id}]
to_columns:   [{child_request_id:  request_id}]
```

The outer list is required (`from_columns` is always a list, even for a single key). For composite primary keys, give one single-key dict entry per key column. The dict shape is required for self-edges and recommended whenever the FK column doesn't share the source PK property's name. For this codelab's binding both edge columns share the source PK's name, so the list-of-strings shape works as-is.

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

### A note on the zero-LLM extraction path

The local run above uses the default extractor, which calls BigQuery's `AI.GENERATE` to extract entities and relationships from event content. The SDK also ships a `--extraction-mode=compiled-only` flag that swaps in a **reference-extractor module** — deterministic Python keyed to your ontology, no Vertex AI dependency, the audited code path. Production deployments that need to certify their data path to a regulator typically run `--extraction-mode=compiled-only` and remove `roles/aiplatform.user` from the runtime service account entirely. ("Compiled" here describes the extraction *mode*; it does not mean fingerprint-stable compiled bundles — the `--bundles-root` path for compiled bundles is a separate, orthogonal surface.)

Running compiled-only mode requires a `reference_extractor.py` keyed to your ontology. The SDK's migration v5 example ships a reference extractor — that's what the live notebook smoke tests run against. For your own graph you author one extractor module that maps event-content shape to entity-and-relationship dicts; the materializer wires the rest. The codelab's custom DecisionRequest graph doesn't ship a reference extractor, so the codelab stays on the `AI.GENERATE` default. When you're ready, the migration v5 reference extractor at [`examples/migration_v5/reference_extractor.py`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/reference_extractor.py) is the template to copy from.

### Backfill a historical window

The same code path the cron uses can also be pointed at a fixed historical window — useful when events arrived during an outage, when a binding change requires a one-shot re-extraction, or when an audit committee asks for a specific quarter. Backfill mode writes its high-water mark to an **isolated** state-table namespace (controlled by `--state-key-suffix`) so it never disturbs the live cron's checkpoint.

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

(The `date -u -d ...` form is GNU `date` on Linux / Cloud Shell; the `date -u -v-48H` form is BSD `date` on macOS. The `||` falls back to the macOS form if the GNU form fails.)

You should see a JSON report with the backfill window's `sessions_materialized` count, and the state table will have a new row whose `state_key` is hashed from the suffix you passed:

```bash
bq query --use_legacy_sql=false \
    "SELECT mode, scan_start, scan_end, sessions_materialized, ok \
     FROM \`$PROJECT_ID.$GRAPH_DATASET._bqaa_materialization_state\` \
     ORDER BY run_started_at DESC LIMIT 5"
```

You should see at least two rows: one from the cron-style local run earlier in this section (a `mode` row corresponding to the standard scan), and one from the backfill (with a different `state_key` because the suffix changes the hash input). The live cron's high-water mark — sitting under its own `state_key` — is untouched. That's the property that lets backfill run concurrently with production cron.

## Run the deploy as a worked example
Duration: 0:12

The SDK ships `deploy_cloud_run_job.sh` under the migration v5 example directory. It runs `bqaa-materialize-window` as a Cloud Run Job, creates the service accounts with the narrow IAM the deploy needs, wires a Cloud Scheduler trigger, and — with the `--smoke` flag — runs the job once after deploy to verify end-to-end. The 0.3.2 release of the script lands every resource with **production-shape defaults**: split runtime + scheduler-caller service accounts, `--max-retries=2`, a structured JSON log on every run, and an optional `--max-session-age-hours` orphan watchdog.

The script bundles the migration v5 example's artifacts (the ontology, binding, property-graph DDL, and reference extractor); the artifacts the codelab walked you through are not yet pluggable into this script. Adapting the script to accept arbitrary artifact paths is an open follow-up — file an issue against the SDK repository if your team needs it. For the codelab graph, the recommended pattern today is to cron the local `materialize-window` command from Cloud Build or Cloud Workflows; see the [customer playbook](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/periodic_materialization/README.md) for both shapes.

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

1. Creating the **runtime** service account `bqaa-periodic-runtime-sa` and the **scheduler-caller** service account `bqaa-periodic-scheduler-sa`. Two SAs by default — the runtime SA holds the BigQuery + Vertex AI permissions the Cloud Run Job needs at execution time, the scheduler SA holds only `roles/run.invoker` on the job. This is the least-privilege posture; if you want the old single-SA shape (one identity for both roles), pass `--single-sa` and the script creates a combined `bqaa-periodic-sa` instead.
2. Granting IAM: `dataViewer` on the events dataset and `dataEditor` on the graph dataset for the runtime SA; `bigquery.jobUser` and (in default `ai-fallback` extraction mode) `aiplatform.user` at the project; `run.invoker` on the Cloud Run Job for the scheduler SA.
3. Building the container via Cloud Build.
4. Deploying the Cloud Run Job `bqaa-periodic-materialization` with environment variables for `--max-retries`, `--lookback-hours`, `--overlap-minutes`, and the extraction mode.
5. Creating the Cloud Scheduler trigger `bqaa-periodic-materialization-cron` (cron `0 */6 * * *` — every six hours on the hour) authenticated as the scheduler SA.
6. Running the smoke check.
7. A structured JSON report.

If the smoke check reports `ok: true`, the production-shape deploy is complete. The job will fire again at the top of the next six-hour window.

### Optional flags worth knowing

The deploy script accepts a handful of optional flags that map to operational knobs:

- `--extraction-mode compiled-only` — the zero-LLM audited path. Requires a reference extractor keyed to your ontology (the bundled migration v5 example ships one; the codelab's custom graph does not). Removes the `roles/aiplatform.user` grant from the runtime SA automatically.
- `--max-retries N` — in-window retry budget for transient failures (default `2`). Surfaces as the `BQAA_MAX_RETRIES` env var inside the Cloud Run Job.
- `--max-session-age-hours N` — opt-in orphan watchdog. Sessions older than `N` hours that haven't terminated are flagged as orphaned and written to the state table with `mode = 'orphan_scan'`, so an operator can drain stale state without the cron silently re-pulling broken sessions forever.
- `--max-sessions N` — cap the per-window batch size (default unlimited). Useful for hostile event spikes.
- `--single-sa` — collapse the two SAs into one combined `bqaa-periodic-sa`. The split is recommended; this flag exists for migration ergonomics from earlier versions.

The deploy as written above takes none of these — it uses the production defaults. Re-running with any of them is idempotent (the script skips resources that already exist and updates only what changed).

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
- `jsonPayload.failures[].error_code` — `empty_extraction` (AI/IAM issue), `materialization_failed` (schema/write-perm issue), or — when `--max-session-age-hours` is enabled — `session_orphaned` (session exceeded the watchdog age cap; emitted only when the watchdog is on).

A single Cloud Monitoring alert on `jsonPayload.ok = false` is the recommended posture. The `error_code` field tells the operator which Google Cloud configuration to inspect without log digging.

### The same deploy as Terraform (alternative path)

Teams that operate infrastructure-as-code can land the same six resources through a Terraform module instead of the bash script. The module lives at [`examples/migration_v5/periodic_materialization/terraform/`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization/terraform) and mirrors the bash deploy exactly: same SA names, same IAM grants, same Cloud Run Job env vars, same scheduler trigger. The bash deploy is lighter-weight onboarding; the Terraform module is the same six resources behind `terraform plan`, drift detection, and multi-environment promotion.

The Terraform module takes the **container image URI as a required input** — it doesn't build the image inline the way the bash deploy does. The SDK bundles a `build_image.sh` helper that stages the exact layout the bash deploy assembles (run script, reference extractor, demo artifacts, vendored SDK source, `Procfile`, `requirements.txt`) and runs `gcloud builds submit` against the staging dir. Same image contents either way; Terraform just consumes the publish artifact instead of doing the build inline.

If you've already run the bash deploy in this codelab and want to see the Terraform path, skip ahead to the cleanup section first to free the resource names. Then:

```bash
# Build and publish the container image.
cd $HOME/BigQuery-Agent-Analytics-SDK
IMAGE_URI="$(./examples/migration_v5/periodic_materialization/build_image.sh \
    --project "$PROJECT_ID" \
    --repo bqaa \
    --region "$REGION" \
    --create-repo)"
echo "$IMAGE_URI"
# → us-central1-docker.pkg.dev/your-project/bqaa/periodic-materialization:<tag>

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

Tear down what you created so you don't get billed for an idle Cloud Run Job. If you used the **Terraform path**, the cleanest teardown is one command:

```bash
cd $HOME/BigQuery-Agent-Analytics-SDK/examples/migration_v5/periodic_materialization/terraform
terraform destroy
```

If you used the **bash deploy path**, the per-resource teardown follows. The resource names below match the deploy script's defaults: `bqaa-periodic-materialization` for the job, `bqaa-periodic-materialization-cron` for the scheduler, `bqaa-periodic-runtime-sa` + `bqaa-periodic-scheduler-sa` for the two service accounts (split SAs are the 0.3.2 default; if you passed `--single-sa`, delete `bqaa-periodic-sa` instead). If you customized the job name with `--job-name`, substitute accordingly.

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

# Service accounts (split-SA default — delete both)
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

## Congratulations
Duration: 0:02

You have, for the **custom graph**:

- Authored a BigQuery property graph contract (table DDL + property-graph DDL + ontology + binding) for a generic agent decision domain.
- Populated `agent_events` with a synthetic event corpus.
- Run `bqaa-materialize-window` locally against that custom graph, in the default `AI.GENERATE` extraction mode.
- Backfilled a historical window into an isolated state-table namespace without disturbing the live cron's checkpoint.
- Queried the resulting graph in GQL and seen the audit-style answer.

And for the **production-deploy shape** (demonstrated against the bundled migration v5 artifacts):

- Deployed a Cloud Run Job + Cloud Scheduler trigger that runs every six hours with the 0.3.2 defaults: split runtime + scheduler-caller service accounts, retry budget, structured JSON logs, state-table audit trail.
- Seen which knobs are opt-in: `--max-session-age-hours` orphan watchdog, `--extraction-mode=compiled-only` zero-LLM path, `--single-sa` for the legacy single-SA shape.
- Seen the same deploy expressed as a Terraform module that drops into an existing IaC pipeline.

The pattern works wherever an agent makes consequential decisions: credit underwriting, prior authorization, marketing budget moves, procurement, customer service, internal IT. For your own graph today: author the contract (using the codelab's DecisionRequest example or the migration v5 demo as a starting point), then cron `bqaa-materialize-window` from Cloud Build / Cloud Workflows / an external scheduler. The deploy script's wrapper shape — the SAs, the scheduler trigger, the JSON logs — is the same one to adopt once your team is ready to package the command as a Cloud Run Job; adapting the deploy to accept arbitrary artifact paths is an open follow-up tracked against the SDK repository.

### Further reading

- [BigQuery Agent Analytics SDK repository](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
- [Customer playbook for periodic materialization](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/periodic_materialization/README.md) — required APIs, IAM matrix, recommended schedules, Cloud Monitoring alert queries, troubleshooting.
- [Terraform module for periodic materialization](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization/terraform) — IaC mirror of the bash deploy: same six resources, variable reference, GCS state-backend block, bash-vs-Terraform comparison.
- [Migration v5 demo notebook (Beats 1–5)](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5_demo_notebook.ipynb) — the end-to-end walk through the SDK's four decision-lineage guarantees plus the outcome-signal feedback / reward loop, run against a live BigQuery project.
- [Reference extractor pattern](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/migration_v5/reference_extractor.py) — template for the compiled-only extraction path your team would author for a regulated deployment.
- [CHANGELOG `[0.3.2]` block](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/CHANGELOG.md) — the full Added + Fixed surface from the migration v5 production track release, with PR references for every flag mentioned in this codelab.
- [BigQuery property graphs documentation](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/graph-intro) (Preview).
- [BigQuery Conversational Analytics documentation](https://docs.cloud.google.com/bigquery/docs/conversational-analytics) (Preview).

The hard part of agent governance was never the events. It was the join, the traversal, and the cadence. With `bqaa-materialize-window` on whatever cron your team already runs, all three are one query away — and with the 0.3.2 production-shape defaults (split SAs, retries, orphan watchdog, optional Terraform), the cron is one your auditor can sign off on.
