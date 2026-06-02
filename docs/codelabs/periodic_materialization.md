summary: Build a queryable BigQuery property graph of your AI agent's decisions. You will apply a graph schema to a BigQuery dataset, seed it with sample agent events, run the bqaa context-graph CLI to extract a decision graph from those events, and query the result with Graph Query Language (GQL) to trace any decision end-to-end.
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

This codelab uses the BigQuery Agent Analytics SDK to transform raw agent event logs into a **Context Graph** — a queryable BigQuery property graph of agent decisions — on a schedule, without any external graph database or ETL pipeline.

### What You Will Build

* A Context Graph (a BigQuery property graph) that models a generic agent decision flow: a request comes in, the agent weighs options, an outcome is committed.
* A populated `agent_events` table with a synthetic event corpus.
* A working `bqaa context-graph` run that fills the graph from those events.
* A one-shot replay (backfill) of a past time window — useful when events arrived during an outage — without disturbing the regular refresh schedule.
* An audit-style GQL query that traces a single decision end-to-end.

### What You Will Learn

* How the BigQuery Agent Analytics Plugin writes to `agent_events`.
* How a property graph is composed from a small set of declarative artifacts (table DDL, property-graph DDL, ontology, binding).
* How to run `bqaa context-graph` against a property graph.
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

<!-- colab:skip -->
```bash
export PROJECT_ID="your-project-id"
export REGION="us-central1"
export DATASET="agent_analytics_demo"
gcloud config set project "$PROJECT_ID"
```

<!-- colab:cell python
import os

# Change these three lines to match your GCP environment.
PROJECT_ID = "your-project-id"
REGION     = "us-central1"
DATASET    = "agent_analytics_demo"

# Export to the shell environment so the !-prefixed cells below see them.
os.environ["PROJECT_ID"] = PROJECT_ID
os.environ["REGION"]     = REGION
os.environ["DATASET"]    = DATASET

print(f"PROJECT_ID = {PROJECT_ID}")
print(f"REGION     = {REGION}")
print(f"DATASET    = {DATASET}")
-->

<!-- colab:cell python
# Authenticate against your GCP project.
# In colab.research.google.com this prompts for an account interactively.
# In Vertex AI Colab Enterprise the project service account auth is used
# automatically.
try:
    from google.colab import auth
    auth.authenticate_user()
    print("Authenticated via google.colab.auth.")
except ImportError:
    print(
        "google.colab not available; assuming gcloud Application Default"
        " Credentials are already configured."
    )

!gcloud config set project $PROJECT_ID
-->

The single `DATASET` variable holds both the raw `agent_events` table and the materialized graph tables. Using one dataset keeps the codelab simple. Production deployments often split events and graph into separate datasets so IAM can be granted narrowly per dataset.

### Enable the Required APIs

<!-- colab:code bash -->
```bash
gcloud services enable \
    bigquery.googleapis.com \
    aiplatform.googleapis.com \
    --project="$PROJECT_ID"
```

The `aiplatform.googleapis.com` API is required because the SDK's default extraction path calls BigQuery's `AI.GENERATE` function. If you later switch to deterministic extraction with `--extraction-mode=compiled-only`, this API is no longer needed.

### Create the BigQuery Dataset

<!-- colab:code bash -->
```bash
bq --location=US mk --dataset "$PROJECT_ID:$DATASET"
```

You should see "Dataset '...' successfully created". If the dataset already exists, the command errors harmlessly. Leave it in place.

## Install the SDK
Duration: 0:02

Set up a Python virtual environment and install the SDK from PyPI:

<!-- colab:skip -->
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install bigquery-agent-analytics
```

<!-- colab:cell python
# Install the SDK and the BigQuery client library directly into the
# notebook runtime. The local-terminal codelab path creates a venv
# first; in Colab the runtime is already isolated, so a plain pip
# install is enough.
!pip install --quiet --upgrade bigquery-agent-analytics google-cloud-bigquery
-->

Verify the install:

<!-- colab:code bash -->
```bash
bqaa context-graph --help | head -8
```

You should see the CLI banner.

> 💡 **Migrating from `bqaa-materialize-window`?** The old standalone command still works and accepts the same flags — it now prints a one-line deprecation notice on stderr and forwards to the same handler. Existing scripts and Cloud Run Jobs keep running while you migrate.

### Authenticate

If you are on a workstation:

<!-- colab:skip -->
```bash
gcloud auth login
gcloud auth application-default login
```

Cloud Shell users can skip this step; credentials are already configured. (In Colab the configuration cell above already handled this via `google.colab.auth`.)

## Get the Codelab Artifacts
Duration: 0:02

The codelab ships a set of ready-to-use artifacts: the property-graph schema, the ontology, the binding, and a synthetic event generator. You do not author any of these yourself; the codelab uses them as-is, and the [README in the artifacts folder](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/codelab/periodic_materialization/README.md) explains how to adapt them for your own decision domain.

Download the artifacts to a working directory:

<!-- colab:skip -->
```bash
mkdir -p ~/bqaa-codelab && cd ~/bqaa-codelab

BASE="https://raw.githubusercontent.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/main/examples/codelab/periodic_materialization"
for f in property_graph.sql table_ddl.sql ontology.yaml binding.yaml seed_events.py; do
  curl -fsSL "$BASE/$f" -o "$f"
done
ls
```

<!-- colab:cell python
# Source of the bundled codelab artifacts.
#
# By default, BASE points at the latest `main` branch. If you are running
# this notebook against a feature branch under review, override BASE in
# your shell before launching the notebook (or edit the line below).
import os
os.environ["BASE"] = os.environ.get(
    "BASE",
    "https://raw.githubusercontent.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/main/examples/codelab/periodic_materialization",
)
print(f"Downloading codelab artifacts from: {os.environ['BASE']}")

!mkdir -p ~/bqaa-codelab && cd ~/bqaa-codelab && \
  for f in property_graph.sql table_ddl.sql ontology.yaml binding.yaml seed_events.py; do \
    curl -fsSL "$BASE/$f" -o "$f"; \
  done && \
  ls -la
-->

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

<!-- colab:code bash -->
```bash
cd ~/bqaa-codelab
envsubst < table_ddl.sql      | bq query --use_legacy_sql=false
envsubst < property_graph.sql | bq query --use_legacy_sql=false
```

You should see five `CREATE TABLE` results and one `CREATE PROPERTY GRAPH` result. The DDL is idempotent; you can re-run it safely.

### Render the Binding

The materializer reads `binding.yaml` directly. Substitute the shell variables once before any tool reads the file:

<!-- colab:code bash -->
```bash
cd ~/bqaa-codelab
envsubst < binding.yaml > binding.rendered.yaml
```

After this, `binding.rendered.yaml` contains your real project ID and dataset name instead of the `${...}` markers. If you skip this step, `bqaa context-graph` validates against literal `${PROJECT_ID}` text and fails closed.

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

<!-- colab:code bash -->
```bash
pip install google-cloud-bigquery
cd ~/bqaa-codelab
bqaa seed-events \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --sessions 5
```

The command prints a JSON report. For 5 sessions you should see `"events_generated": 30`, `"events_inserted": 30`, and `"ok": true`.

Preview the corpus at a glance — how many sessions, how many events, and the time range they span — in one row:

<!-- colab:code bash -->
```bash
bq query --use_legacy_sql=false \
    "SELECT COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS events, MIN(timestamp) AS earliest_event, MAX(timestamp) AS latest_event FROM \`$PROJECT_ID.$DATASET.agent_events\`"
```

For the default 5-session run this shows 5 sessions and 30 events spanning a few minutes. (Seed the realistic scenario below and the same query reports ~100 sessions across roughly three days.)

Verify the events landed:

<!-- colab:code bash -->
```bash
bq query --use_legacy_sql=false \
    "SELECT event_type, COUNT(*) AS n FROM \`$PROJECT_ID.$DATASET.agent_events\` GROUP BY event_type ORDER BY n DESC"
```

You should see 25 `TOOL_COMPLETED` rows and 5 `AGENT_COMPLETED` rows (each session emits one `submit_request`, three `evaluate_option`, one `commit_outcome`, and one closing `AGENT_COMPLETED` — five tool events plus one agent terminator per session). The `AGENT_COMPLETED` rows are the session terminators that the materializer keys on for terminal-event detection.

### Optional: Realistic-scale data

The 5-session corpus above is intentionally tiny so the first run is fast. When you want production-shaped data — multiple agents and users spread over several days, with failed, orphaned, and truncated sessions — use the `decision-realistic` scenario. It defaults to 100 sessions over a 72-hour window; the first-run path above is unchanged.

<!-- colab:code bash -->
```bash
bqaa seed-events \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --scenario decision-realistic \
    --sessions 100 \
    --seed 42
```

The JSON report's `session_outcome_counts` shows the mix — roughly `{"success": 70, "failed": 10, "orphaned": 10, "truncated": 10}`.

> Once you have this realistic corpus, the [Conversational Analytics-first guide](../guides/conversational-analytics-first.md) shows how to ask it in plain English ("which requests weighed a low-confidence option?") before dropping to GQL.

Confirm the outcome distribution by classifying each session from its rows (orphaned = no `AGENT_COMPLETED`; failed = `AGENT_COMPLETED` with `status = 'error'`; truncated = any row with `is_truncated = true`; otherwise success). A first pass classifies each session, then a second aggregates per outcome:

<!-- colab:code bash -->
```bash
bq query --use_legacy_sql=false \
    "WITH per_session AS (SELECT session_id, CASE WHEN COUNTIF(event_type = 'AGENT_COMPLETED') = 0 THEN 'orphaned' WHEN COUNTIF(event_type = 'AGENT_COMPLETED' AND status = 'error') > 0 THEN 'failed' WHEN COUNTIF(is_truncated) > 0 THEN 'truncated' ELSE 'success' END AS outcome FROM \`$PROJECT_ID.$DATASET.agent_events\` GROUP BY session_id) SELECT outcome, COUNT(*) AS sessions FROM per_session GROUP BY outcome ORDER BY outcome"
```

You should see roughly 70 success, 10 failed, 10 orphaned, and 10 truncated (plus the 5 successful sessions from the first-run corpus if you seeded that earlier in the same dataset).

The 10 orphaned sessions never emitted `AGENT_COMPLETED`, so the default `bqaa context-graph` run skips them (it materializes only terminal-event-closed sessions). To surface them as `session_orphaned` instead of silently retrying forever, add `--max-session-age-hours` when you materialize — see the orphan-watchdog discussion later in this codelab.

> **Note** — this scenario spreads its sessions across a 72-hour window on purpose. The Phase 3 walkthrough below uses `--lookback-hours 24` and is written for the small 5-session first-run corpus, so its exact counts assume you have *not* run this optional step. If you did, that is the intended lesson: a 24-hour materialization window picks up only the recent slice of a multi-day backlog — widen `--lookback-hours` (or backfill) to capture the older sessions.

## Phase 3: Materialize the Decision Graph
Duration: 0:05

`bqaa context-graph` reads the raw `agent_events`, uses the **ontology** to identify which entities and relationships to extract and the **binding** to map them onto your BigQuery tables and columns, then populates the tables behind the property graph. The property-graph schema you applied in Phase 1 only defines the *query surface* over those tables — it cannot populate them, which is exactly why this step requires both `--ontology` and `--binding`.

Run the materializer locally:

<!-- colab:code bash -->
```bash
bqaa context-graph \
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

<!-- colab:markdown -->
```bash
USER_EMAIL=$(gcloud auth list --filter=status:ACTIVE --format="value(account)")
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="user:$USER_EMAIL" --role="roles/aiplatform.user"
```

Verify the graph has rows:

<!-- colab:code bash -->
```bash
bq query --use_legacy_sql=false \
    "SELECT COUNT(*) AS n FROM \`$PROJECT_ID.$DATASET.decision_request\`"
```

You should see five rows.

### Two Ways to Extract Decisions From Events

The materializer offers two extraction paths. Pick the one that matches your workload:

* **Default extraction.** The easiest path. Uses BigQuery's `AI.GENERATE` to read event content and infer entities and relationships. Works against any event shape with no extra code. This is what the codelab uses.
* **Deterministic extraction** (`--extraction-mode=compiled-only`). The lower-cost, audit-friendly path. Uses a small Python reference extractor you write once for your ontology. No Vertex AI calls, no per-token charges, fully reproducible output. Production deployments choose this when cost predictability or strict reproducibility matters.

> 💡 **Tip.** Deterministic extraction is also the path for regulated workloads that need to remove the Vertex AI dependency from the runtime service account entirely. See the [production deployment guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization) for the IAM details.

## Phase 4: Query the Decision Trace
Duration: 0:05

With the graph populated, you can answer the audit question directly. Take a concrete one: *"For each request, what options did the agent weigh, and how did it resolve?"* In GQL that is a single traversal across the request, its options, and its outcome. Save the following as `traversal.sql`:

<!-- colab:markdown -->
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

<!-- colab:skip -->
```bash
envsubst < traversal.sql | bq query --use_legacy_sql=false --max_rows=20
```

<!-- colab:cell python
# Run the same GQL traversal from the notebook. We embed the SQL in a
# Python f-string so the ${DATASET} placeholder is filled in by the
# notebook's PROJECT_ID / DATASET variables, and we display the result
# as a pandas DataFrame for in-cell readability.
DATASET = os.environ["DATASET"]
PROJECT_ID = os.environ["PROJECT_ID"]
traversal_sql = f"""
SELECT *
FROM GRAPH_TABLE (
  {DATASET}.agent_decisions_graph
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
)
ORDER BY request, score DESC;
"""

from google.cloud import bigquery
client = bigquery.Client(project=PROJECT_ID)
results_df = client.query(traversal_sql).to_dataframe()
results_df
-->

You should see fifteen rows: three options per request, five requests. Each row shows the request, the option the agent considered, its confidence score, the final outcome, and the rationale.

For a single decision's full picture, filter by `request_id` to get the row set an audit team needs: the question that came in, the options that were weighed (with scores), and the rationale that was committed.

### Ask the Same Question in Plain English

Not every audit reader writes GQL. With **BigQuery Conversational Analytics** (Preview), your compliance team can ask the same kind of question in natural language and get back a structured answer card — no query syntax, no joins to learn.

Register the `agent_decisions_graph` (along with the `agent_events` and decision tables) as a Conversational Analytics data source, then ask the audit question directly:

**Audit question (plain English):** *"Which requests never reached a committed outcome?"*

Conversational Analytics reasons over the graph, writes the SQL for you, and replies in plain English with a supporting table — here, that every recorded request reached a committed outcome:

![Conversational Analytics answering "Which requests never reached a committed outcome?" over the decision graph: every recorded request reached a committed outcome (orphaned sessions are not materialized as graph nodes)](images/ca-conversation.png)

> The reply above reflects the realistic-scale corpus from the optional *Realistic-scale data* step (90 materialized requests, all committed). Your exact numbers depend on which corpus you seeded — the default 5-session run shows five.

See the [Conversational Analytics documentation](https://cloud.google.com/bigquery/docs/conversational-analytics) for setup.

## Advanced: Replay a Past Window
Duration: 0:04

Sometimes you need to re-process a past time window: events arrived during an outage, a schema change requires re-extraction, or an audit team asks about a specific historical period. Backfill mode lets you do that **without disturbing the regular refresh schedule** — the replay runs against a fixed start-and-end window you choose, and its progress is tracked separately from the regular refresh.

In a real recovery you would point the backfill at the window where the missed events actually arrived. For this codelab, run a backfill against an **empty historical window** (eight to nine hours ago, before you seeded any events). The replay finishes immediately because there is nothing to materialize, which lets you see the audit trail it produces without re-touching the rows you already wrote in Phase 3.

<!-- colab:skip -->
```bash
FROM=$(date -u -d "9 hours ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
       || date -u -v-9H +"%Y-%m-%dT%H:%M:%SZ")
TO=$(date -u -d "8 hours ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
     || date -u -v-8H +"%Y-%m-%dT%H:%M:%SZ")

bqaa context-graph \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --ontology ~/bqaa-codelab/ontology.yaml \
    --binding ~/bqaa-codelab/binding.rendered.yaml \
    --lookback-hours 1 \
    --backfill --from "$FROM" --to "$TO" \
    --state-key-suffix codelab_backfill_demo \
    --format json
```

<!-- colab:cell python
import datetime, os
now = datetime.datetime.now(datetime.timezone.utc)
os.environ["FROM"] = (now - datetime.timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
os.environ["TO"]   = (now - datetime.timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
print(f"Backfill window FROM = {os.environ['FROM']}")
print(f"Backfill window TO   = {os.environ['TO']}")

!cd ~/bqaa-codelab && bqaa context-graph \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --ontology ontology.yaml \
    --binding binding.rendered.yaml \
    --lookback-hours 1 \
    --backfill --from "$FROM" --to "$TO" \
    --state-key-suffix codelab_backfill_demo \
    --format json
-->

(The `date -u -d ...` form is GNU `date` on Linux and Cloud Shell; the `date -u -v-9H` form is BSD `date` on macOS. The `||` falls back to the macOS form if the GNU form fails.)

You should see a JSON report with `"sessions_materialized": 0` because the window you picked doesn't overlap with the events you seeded. Now inspect the audit trail this run wrote into the state table:

<!-- colab:code bash -->
```bash
bq query --use_legacy_sql=false \
    "SELECT mode, scan_start, scan_end, sessions_materialized, ok \
     FROM \`$PROJECT_ID.$DATASET._bqaa_materialization_state\` \
     ORDER BY run_started_at DESC LIMIT 5"
```

You should see at least two rows: one from the Phase 3 materialization (`mode = 'steady'`) and one from this backfill (`mode = 'backfill'`). The two are independent — the backfill's progress is tracked separately, so it cannot disrupt the regular refresh.

> 💡 **Tip — how the isolation works (optional detail).** Each materializer run writes a row to the `_bqaa_materialization_state` table keyed by a `state_key` hash. Backfill mode mixes the `--state-key-suffix` you pass into that hash, so the backfill writes to a different `state_key` than the regular schedule. Same table, different rows, separate progress markers. Production operators query this table to confirm a catch-up actually ran.

## Production-Grade Capabilities
Duration: 0:03

The local run you completed in Phase 3 uses default behavior. Real deployments care about cost, reliability, and audit posture — the SDK supports each one out of the box.

**What you get by default:**

* **Every run leaves a clear audit trail.** Structured JSON logs go to Cloud Logging, and a per-run row lands in a state table inside your dataset — useful for alerting, dashboards, and answering "did the refresh actually happen?"
* **Transient failures retry automatically.** The materializer retries a small number of times (default two) before flagging a failure, so a slow query or a brief Vertex AI hiccup doesn't take down the run.
* **No double-counting.** Progress only advances on sessions that fully succeeded, so retrying a partially-failed run picks up exactly where it left off.

**What you opt into when you need it:**

* **Lower-cost, deterministic extraction** (`--extraction-mode=compiled-only`). Swaps the LLM-based extractor for a small reference-extractor module you write once. Removes the Vertex AI dependency and the per-token cost. Recommended for steady-state production workloads and any audit that requires reproducibility.
* **Catch stuck sessions** (`--max-session-age-hours`). If your agents sometimes fail to emit a terminal event, this flags long-running sessions as orphaned so operators can drain them — instead of the scheduled refresh silently retrying them forever.
* **Replay a past window** (`--backfill --from / --to`). You exercised this in *Advanced: Replay a Past Window* above. The replay is tracked separately from the regular schedule so it cannot interfere with the live refresh.
* **Bound the per-run batch size** (`--max-sessions`). Useful when an upstream event spike threatens to overwhelm a single scan.

> 💡 **From "run this once" to "run this every six hours."** The SDK ships a deploy script and a Terraform module that wrap `bqaa context-graph` as a Cloud Run Job triggered by Cloud Scheduler, with least-privilege service accounts and the IAM grants the job needs. See the [periodic-materialization deployment guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization) for the worked example, the IAM matrix, and the recommended schedules.

## Clean Up
Duration: 0:03

Tear down what you created so you do not get billed for an idle dataset:

<!-- colab:skip -->
```bash
bq rm -r -f --dataset "$PROJECT_ID:$DATASET"
```

<!-- colab:cell python
# UNCOMMENT TO RUN. This permanently deletes the dataset and everything
# in it (events, graph tables, state table). Left commented so a
# "Run all" pass does not tear down your work.
# !bq rm -r -f --dataset "$PROJECT_ID:$DATASET"
-->

That single command removes the dataset, the agent events, the graph tables, and the state table together.

## Summary
Duration: 0:02

You have:

* Created a BigQuery dataset and applied a property-graph schema describing an agent decision domain.
* Populated `agent_events` with a synthetic event corpus.
* Run `bqaa context-graph` to extract a decision graph from those events.
* Replayed a past time window with backfill mode, separately from the regular refresh schedule.
* Queried the resulting graph in GQL and seen the audit-style answer.

The same pattern applies wherever an agent makes consequential decisions: credit underwriting, prior authorization, marketing budget moves, procurement, customer service, and internal IT. To build your own decision graph, copy the codelab artifacts as a starting point and adapt the four declarative files (table DDL, property-graph DDL, ontology, binding) to your domain.

### Further Reading

* [BigQuery Agent Analytics SDK repository](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
* [Codelab artifacts and adaptation guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/codelab/periodic_materialization)
* [Periodic-materialization deployment guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization): required APIs, IAM matrix, recommended schedules, Cloud Monitoring alert queries, and the Terraform module.
* [BigQuery property graphs documentation](https://cloud.google.com/bigquery/docs/reference/standard-sql/graph-intro) (Preview).
* [BigQuery Conversational Analytics documentation](https://cloud.google.com/bigquery/docs/conversational-analytics) (Preview).
