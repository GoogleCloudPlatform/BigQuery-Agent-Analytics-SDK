# Deploy a Context Graph on a schedule — one artifact

This runbook takes the [Periodic Materialization codelab](../codelabs/periodic_materialization.md)
graph from a local run to a **hands-off scheduled production deploy**, using the
schema-derived (`--property-graph`) flow: you ship one `property_graph.sql` (plus
its companion `table_ddl.sql`) and the Cloud Run Job derives the ontology +
binding from it — no hand-written `ontology.yaml` / `binding.yaml`.

It's the rename-free / common path. For richer graphs (descriptions to steer the
AI prompt, entity inheritance, derived properties, column renames, or a
compiled extractor) keep the explicit `--ontology` / `--binding` path in the
[migration-v5 deploy README](../../examples/migration_v5/periodic_materialization/README.md),
which is also the deep reference for IAM, scheduling, monitoring, and teardown.

> Requires `bigquery-agent-analytics >= 0.3.3`.

## The shape

```
agent_events                run_job.py (Cloud Run Job, every N hrs):
(your EVENTS dataset)  ───►   1. apply table_ddl.sql  → graph tables exist
   read-only                  2. derive ontology+binding from property_graph.sql
                              3. materialize sessions  → node/edge tables
                                                          (your GRAPH dataset)
                                          │
                                          ▼
                              _bqaa_materialization_state
                              (checkpoint — in the GRAPH dataset)
```

Two datasets, two lifecycles: **events** is read-only (your agents write it);
**graph** is writable (the job creates the tables, the materialized graph, and
the state table). `bqaa context-graph --property-graph` resolves the
`${PROJECT_ID}` / `${DATASET}` placeholders so everything lands in the graph
dataset while events stay read-only.

## 0. Prerequisites

```bash
export PROJECT_ID="your-project"
export EVENTS_DS="agent_analytics"     # read-only: holds agent_events
export GRAPH_DS="agent_decisions_graph"  # writable: holds the materialized graph

gcloud services enable \
  bigquery.googleapis.com run.googleapis.com cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com --project="$PROJECT_ID"
```

`aiplatform.googleapis.com` is needed because schema-derived mode uses
`AI.GENERATE` extraction (`ai-fallback`). The deploy script grants the runtime
service account `roles/aiplatform.user` automatically in that mode.

You need an **events dataset with a populated `agent_events` table** (your ADK
agent writes it via the BigQuery Agent Analytics plugin). To follow along
without an agent, seed a corpus:

```bash
bqaa seed-events --project-id "$PROJECT_ID" --dataset-id "$EVENTS_DS" \
    --scenario decision --sessions 5
```

## 1. Your two artifacts

Schema-derived mode needs exactly two **placeholdered** files. The codelab ships
both, ready to adapt:

```bash
cp examples/codelab/periodic_materialization/property_graph.sql .
cp examples/codelab/periodic_materialization/table_ddl.sql .
```

Both must use `${PROJECT_ID}` / `${DATASET}` (not hardcoded references) — the
deploy refuses hardcoded artifacts so the scheduled job can never derive against
the wrong dataset, and `table_ddl.sql` must sit next to `property_graph.sql`.

## 2. Validate locally before you pay for Cloud Run

Run the job's runtime entrypoint on your laptop against the real datasets — same
code path the Cloud Run Job runs, no container:

```bash
pip install -e .   # or: pip install 'bigquery-agent-analytics>=0.3.3'

# Stage the runtime + your two artifacts together (mirrors what the deploy
# script bundles into the image).
mkdir -p /tmp/cg-deploy && cp \
  examples/migration_v5/periodic_materialization/run_job.py \
  property_graph.sql table_ddl.sql /tmp/cg-deploy/

BQAA_PROJECT_ID="$PROJECT_ID" \
BQAA_EVENTS_DATASET_ID="$EVENTS_DS" \
BQAA_GRAPH_DATASET_ID="$GRAPH_DS" \
BQAA_PROPERTY_GRAPH="property_graph.sql" \
BQAA_LOOKBACK_HOURS="72" \
BQAA_LOCATION="US" \
python /tmp/cg-deploy/run_job.py
```

Expect a JSON report with `"mode": "property-graph"` and
`"sessions_materialized" > 0`. This is the cheapest way to confirm the
placeholder + split-dataset wiring before deploying.

## 3. Deploy on a schedule

Pick one path. The **bash** deploy builds its own image inline (Cloud
Buildpacks from local source), so you do *not* run `build_image.sh` for it.
**Terraform** consumes a pre-published image, so that path builds the image
first.

### Option A — bash (one command, with a smoke run)

```bash
./examples/migration_v5/periodic_materialization/deploy_cloud_run_job.sh \
  --project "$PROJECT_ID" --region us-central1 \
  --events-dataset "$EVENTS_DS" --graph-dataset "$GRAPH_DS" \
  --schedule "0 */6 * * *" \
  --property-graph property_graph.sql \
  --smoke
```

`--smoke` runs the job once after deploy and tails the logs, so you find out
*now* whether it works. The script builds + publishes the image from local
source, pre-creates the graph dataset, sets up least-privilege service accounts
+ IAM, deploys the Cloud Run Job with `BQAA_PROPERTY_GRAPH=property_graph.sql`,
and wires the Cloud Scheduler trigger. (`--property-graph` is incompatible with
`--extraction-mode=compiled-only`, which the script rejects at the boundary.)

### Option B — Terraform

Terraform takes a published image as input, so build one first with
`build_image.sh --property-graph` (it stages `property_graph.sql` + the sibling
`table_ddl.sql` instead of `ontology.yaml`/`binding.yaml`/`reference_extractor.py`):

```bash
IMAGE_URI="$(./examples/migration_v5/periodic_materialization/build_image.sh \
  --project "$PROJECT_ID" --repo bqaa --create-repo \
  --property-graph property_graph.sql)"     # → REGION-docker.pkg.dev/.../...:<tag>
```

Then point Terraform at it:

```hcl
# terraform.tfvars  (image_uri is passed on the CLI below from $IMAGE_URI)
project_id        = "your-project"
region            = "us-central1"
events_dataset_id = "agent_analytics"
graph_dataset_id  = "agent_decisions_graph"
schedule          = "0 */6 * * *"
property_graph    = true
```

```bash
cd examples/migration_v5/periodic_materialization/terraform
terraform init
terraform apply -var "image_uri=$IMAGE_URI"
```

`property_graph = true` sets `BQAA_PROPERTY_GRAPH` on the Job; a plan-time
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
`mode = "property-graph"`, `sessions_materialized`, `sessions_failed`, and
`ok`. Wire a Cloud Monitoring alert on `ok = false` (see the
[deploy README](../../examples/migration_v5/periodic_materialization/README.md#cloud-monitoring-alerts)).

Then query the graph itself — the materialized decision traces are GQL-queryable
exactly as in [Phase 4 of the codelab](../codelabs/periodic_materialization.md).

## When to use explicit `--ontology` / `--binding` instead

Schema-derived mode covers rename-free graphs with AI extraction. Reach for the
explicit pair (omit `--property-graph`; the deploy bundles `ontology.yaml` +
`binding.yaml`) when you need: human-readable descriptions to steer the
`AI.GENERATE` prompt, entity inheritance, derived (computed) properties, column
renames, or a deterministic compiled extractor (`--extraction-mode=compiled-only`).
The [migration-v5 deploy README](../../examples/migration_v5/periodic_materialization/README.md)
is the reference for that path and for the full IAM matrix, recommended
schedules, monitoring, and teardown.
