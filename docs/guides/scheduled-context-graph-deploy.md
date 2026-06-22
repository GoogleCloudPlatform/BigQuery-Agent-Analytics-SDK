# Deploy an Agent Context Graph on a schedule — consume the deployed graph

This runbook takes the [Periodic Materialization codelab](../codelabs/periodic_materialization.md)
graph from a local run to a **hands-off scheduled production deploy**, using the
deployed-graph (`--graph`) flow: you create the property graph in BigQuery once
(standard `bq` DDL), and the Cloud Run Job reads the graph's definition back
from `INFORMATION_SCHEMA.PROPERTY_GRAPHS` on every run and derives the
ontology + binding from it — no hand-written `ontology.yaml` / `binding.yaml`,
and no SQL file shipped in the image. The deployed graph is the single source
of truth: what you query with GQL is exactly what the job materializes.

It's the rename-free / common path. For richer graphs (descriptions to steer the
AI prompt, entity inheritance, derived properties, column renames, or a
compiled extractor) keep the explicit `--ontology` / `--binding` path in the
[context-graph deploy README](../../examples/context_graph/periodic_materialization/README.md),
which is also the deep reference for IAM, scheduling, monitoring, and teardown.

> Requires `bigquery-agent-analytics >= 0.3.4`.

## The shape

```
agent_events                run_job.py (Cloud Run Job, every N hrs):
(your EVENTS dataset)  ───►   1. read graph DDL from INFORMATION_SCHEMA
   read-only                     .PROPERTY_GRAPHS (your GRAPH dataset)
                              2. derive ontology+binding from it
                              3. materialize sessions  → node/edge tables
                                                          (your GRAPH dataset)
                                          │
                                          ▼
                              _bqaa_materialization_state
                              (checkpoint — in the GRAPH dataset)
```

Two datasets, two lifecycles: **events** is read-only (your agents write it);
**graph** is writable (you create the tables + the property graph once; the job
writes the materialized rows and the state table). You deploy the graph schema
yourself in step 1; from then on the job only ever *reads* the definition.

## 0. Prerequisites

```bash
export PROJECT_ID="your-project"
export EVENTS_DS="agent_analytics"   # read-only: holds agent_events
export GRAPH_DS="decision_graph"     # writable: holds the materialized graph

gcloud services enable \
  bigquery.googleapis.com run.googleapis.com cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com --project="$PROJECT_ID"
```

`aiplatform.googleapis.com` is needed because deployed-graph mode uses
`AI.GENERATE` extraction (`ai-fallback`). The deploy script grants the runtime
service account `roles/aiplatform.user` automatically in that mode.

You need an **events dataset with a populated `agent_events` table** (your ADK
agent writes it via the BigQuery Agent Analytics plugin). To follow along
without an agent, seed a corpus:

```bash
bqaa seed-events --project-id "$PROJECT_ID" --dataset-id "$EVENTS_DS" \
    --scenario decision --sessions 5
```

## 1. Deploy your graph to BigQuery

This is the only schema work you do, and you do it once. The codelab ships
both DDL files, ready to adapt to your own decision domain:

```bash
cp examples/context_graph/codelab/property_graph.sql .
cp examples/context_graph/codelab/table_ddl.sql .

bq --location=US mk --dataset "$PROJECT_ID:$GRAPH_DS" 2>/dev/null || true

# The files are placeholdered for envsubst; the graph lives in the GRAPH
# dataset, so render ${DATASET} as $GRAPH_DS. Tables first — CREATE PROPERTY
# GRAPH rejects references to tables that do not exist yet.
DATASET="$GRAPH_DS" envsubst < table_ddl.sql      | bq query --use_legacy_sql=false
DATASET="$GRAPH_DS" envsubst < property_graph.sql | bq query --use_legacy_sql=false
```

BigQuery now records the graph's definition; everything downstream consumes it
by name (`agent_decisions_graph`) via `INFORMATION_SCHEMA.PROPERTY_GRAPHS`.
Confirm it's there:

```bash
bq query --use_legacy_sql=false \
 "SELECT property_graph_name FROM \`$PROJECT_ID.$GRAPH_DS\`.INFORMATION_SCHEMA.PROPERTY_GRAPHS"
# → agent_decisions_graph
```

## 2. Validate locally before you pay for Cloud Run

Run the job's runtime entrypoint on your laptop against the real datasets — same
code path the Cloud Run Job runs, no container, nothing staged:

```bash
pip install -e .   # or: pip install 'bigquery-agent-analytics>=0.3.4'

BQAA_PROJECT_ID="$PROJECT_ID" \
BQAA_EVENTS_DATASET_ID="$EVENTS_DS" \
BQAA_GRAPH_DATASET_ID="$GRAPH_DS" \
BQAA_GRAPH="agent_decisions_graph" \
BQAA_LOOKBACK_HOURS="72" \
BQAA_LOCATION="US" \
python examples/context_graph/periodic_materialization/run_job.py
```

Expect a JSON report with `"mode": "deployed-graph"` and
`"sessions_materialized" > 0`. This is the cheapest way to confirm the
graph lookup + split-dataset wiring before deploying.

To validate a specific `AI.GENERATE` model here too, add
`BQAA_ENDPOINT="gemini-3.5-flash"` to the env block above — the same knob the
deploy wires from `--endpoint` (bash) / `endpoint` (Terraform).

## 3. Deploy on a schedule

Pick one path. The **bash** deploy builds its own image inline (Cloud
Buildpacks from local source), so you do *not* run `build_image.sh` for it.
**Terraform** consumes a pre-published image, so that path builds the image
first.

### Option A — bash (one command, with a smoke run)

```bash
./examples/context_graph/periodic_materialization/deploy_cloud_run_job.sh \
  --project "$PROJECT_ID" --region us-central1 \
  --events-dataset "$EVENTS_DS" --graph-dataset "$GRAPH_DS" \
  --schedule "0 */6 * * *" \
  --graph agent_decisions_graph \
  --smoke
```

`--smoke` runs the job once after deploy and tails the logs, so you find out
*now* whether it works. The script builds + publishes the image from local
source, pre-creates the graph dataset, sets up least-privilege service accounts
+ IAM, deploys the Cloud Run Job with `BQAA_GRAPH=agent_decisions_graph`,
and wires the Cloud Scheduler trigger. (`--graph` is incompatible with
`--extraction-mode=compiled-only`, which the script rejects at the boundary.)

To pick the `AI.GENERATE` extraction model — e.g. a Gemini 3.x model — add
`--endpoint`; it's wired as `BQAA_ENDPOINT` on the Job. Default is unset, so the
runtime keeps its `gemini-2.5-flash` default:

```bash
./examples/context_graph/periodic_materialization/deploy_cloud_run_job.sh \
  --project "$PROJECT_ID" --region us-central1 \
  --events-dataset "$EVENTS_DS" --graph-dataset "$GRAPH_DS" \
  --schedule "0 */6 * * *" \
  --graph agent_decisions_graph \
  --endpoint gemini-3.5-flash \
  --smoke
```

### Option B — Terraform

Terraform takes a published image as input, so build one first. Deployed-graph
mode needs nothing staged beyond the runtime, so the default build works as-is:

```bash
IMAGE_URI="$(./examples/context_graph/periodic_materialization/build_image.sh \
  --project "$PROJECT_ID" --repo bqaa --create-repo)"
  # → REGION-docker.pkg.dev/.../...:<tag>
```

Then point Terraform at it:

```hcl
# terraform.tfvars  (image_uri is passed on the CLI below from $IMAGE_URI)
project_id        = "your-project"
region            = "us-central1"
events_dataset_id = "agent_analytics"
graph_dataset_id  = "decision_graph"
schedule          = "0 */6 * * *"
graph             = "agent_decisions_graph"
# endpoint        = "gemini-3.5-flash"   # optional: AI.GENERATE model (BQAA_ENDPOINT)
```

Set `endpoint` to pick the `AI.GENERATE` model (e.g. a Gemini 3.x model); it
wires `BQAA_ENDPOINT` on the Job. Leave it at its `""` default to keep the
runtime's `gemini-2.5-flash`.

```bash
cd examples/context_graph/periodic_materialization/terraform
terraform init
terraform apply -var "image_uri=$IMAGE_URI"
```

`graph = "agent_decisions_graph"` sets `BQAA_GRAPH` on the Job; a plan-time
precondition rejects it together with `extraction_mode = "compiled-only"`.

## 4. Verify

After the first run (the `--smoke` execution, or the first scheduled fire):

```bash
# Graph dataset: node/edge tables + the checkpoint table.
bq query --use_legacy_sql=false \
 "SELECT table_name FROM \`$PROJECT_ID.$GRAPH_DS\`.INFORMATION_SCHEMA.TABLES ORDER BY table_name"
# → decision_request, decision_option, decision_outcome, evaluates_option,
#   resulted_in, _bqaa_materialization_state

# Events dataset stayed read-only (only agent_events).
bq query --use_legacy_sql=false \
 "SELECT table_name FROM \`$PROJECT_ID.$EVENTS_DS\`.INFORMATION_SCHEMA.TABLES"
# → agent_events
```

The Cloud Logging entry per run is structured JSON; the key fields are
`mode = "deployed-graph"`, `sessions_materialized`, `sessions_failed`, and
`ok`. Wire a Cloud Monitoring alert on `ok = false` (see the
[deploy README](../../examples/context_graph/periodic_materialization/README.md#cloud-monitoring-alerts)).

Then query the graph itself — the materialized decision traces are GQL-queryable
exactly as in [Phase 4 of the codelab](../codelabs/periodic_materialization.md).

## Troubleshooting

**`Property graph ... not found in ... (INFORMATION_SCHEMA.PROPERTY_GRAPHS)`**
— the job looked for `BQAA_GRAPH` in the graph dataset and didn't find it. The
error lists the graphs that *do* exist there. Usual causes: step 1 was applied
to a different dataset than `BQAA_GRAPH_DATASET_ID`, or the `CREATE PROPERTY
GRAPH` statement failed (its tables didn't exist yet). Re-run step 1 and
confirm with the `INFORMATION_SCHEMA.PROPERTY_GRAPHS` query shown there. If
your graph lives in a *different* dataset on purpose, pass a qualified name:
`--graph other_dataset.agent_decisions_graph`.

**`404 NOT_FOUND ... Publisher Model ... was not found or your project does not
have access to it`** when you set `--endpoint` / `BQAA_ENDPOINT` to a Gemini 3.x
model. This is almost always a *location* mismatch, not an access problem:
Gemini 3.x models on Vertex AI are served from the **`global`** location, and
the SDK already resolves a short model name (`gemini-3.5-flash`) to a
`locations/global` publisher URL. If you still hit a 404, pass the model name
exactly as published for Vertex AI's `global` endpoint (a regional name like a
`us-central1`-only model will 404), and confirm the model is enabled for your
project. The runtime's default `gemini-2.5-flash` works without any of this.

## When to use explicit `--ontology` / `--binding` instead

Deployed-graph mode covers rename-free graphs with AI extraction. Reach for the
explicit pair (omit `--graph`; the deploy bundles `ontology.yaml` +
`binding.yaml`) when you need: human-readable descriptions to steer the
`AI.GENERATE` prompt, entity inheritance, derived (computed) properties, column
renames, or a deterministic compiled extractor (`--extraction-mode=compiled-only`).
The [context-graph deploy README](../../examples/context_graph/periodic_materialization/README.md)
is the reference for that path and for the full IAM matrix, recommended
schedules, monitoring, and teardown.
