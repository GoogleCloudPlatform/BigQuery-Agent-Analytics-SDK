# Terraform module — periodic materialization

This module is the Infrastructure-as-Code mirror of [`deploy_cloud_run_job.sh`](../deploy_cloud_run_job.sh). It lands the same six resources the bash deploy does — graph dataset, runtime + scheduler-caller service accounts, IAM bindings, Cloud Run v2 Job, Cloud Scheduler trigger — but as declarative Terraform so it slots into managed-fleet operations (`terraform plan` review, drift detection, multi-environment promotion, etc.).

Resolves [#186](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/186). Defaults match the post-#230 deploy surface: **split runtime + scheduler-caller SAs**, **`max_retries = 2`**, **`extraction_mode = "ai-fallback"`**.

## What this module deploys

| Resource | Terraform type | Mirrors bash deploy section |
|---|---|---|
| Graph dataset | `google_bigquery_dataset` | §1 — `bq mk` (pre-create so the runtime SA never needs `bigquery.datasets.create`) |
| Runtime SA + scheduler-caller SA | `google_service_account` ×2 | §2 — `_ensure_sa` (split by default; `single_sa = true` collapses to one) |
| Project + dataset IAM grants | `google_project_iam_member`, `google_bigquery_dataset_iam_member` | §3 — `_retry_iam gcloud projects add-iam-policy-binding` and the dataset-level grant block. Terraform's dependency graph handles the IAM-propagation race declaratively (the `depends_on` on the Cloud Run Job resource ensures grants land before the first invocation) |
| Cloud Run v2 Job | `google_cloud_run_v2_job` | §4 — `gcloud run jobs deploy`. Same env-var set the bash deploy wires (`BQAA_PROJECT_ID`, `BQAA_EVENTS_DATASET_ID`, `BQAA_GRAPH_DATASET_ID`, `BQAA_LOCATION`, `BQAA_LOOKBACK_HOURS`, `BQAA_OVERLAP_MINUTES`, `BQAA_EXTRACTION_MODE`, `BQAA_MAX_RETRIES`, conditionally `BQAA_MAX_SESSIONS`, `BQAA_MAX_SESSION_AGE_HOURS`, `BQAA_REFERENCE_EXTRACTORS_MODULE`, `BQAA_PROPERTY_GRAPH` when `property_graph = true`, and `BQAA_ENDPOINT` when `endpoint != ""`) |
| `roles/run.invoker` on the job → scheduler SA | `google_cloud_run_v2_job_iam_member` | §5 — `_retry_iam gcloud run jobs add-iam-policy-binding` |
| Cloud Scheduler trigger | `google_cloud_scheduler_job` | §6 — `gcloud scheduler jobs create http` with `--oauth-service-account-email` pointing at the scheduler SA |

## What's intentionally outside the module

**Container image build + publish.** The bash deploy builds the image from local sources via Cloud Buildpacks (`gcloud run jobs deploy --source $STAGING`), assembling a staging directory with `run_job.py` + `reference_extractor.py` + the demo artifacts + the vendored SDK source + `Procfile` + `requirements.txt` before invoking the build. That staging step is non-trivial — pointing `gcloud builds submit` directly at the `periodic_materialization/` directory would build an image that's missing the SDK source, the demo artifacts, and the Procfile.

The module takes the **published** image URI as the required `image_uri` variable. To produce a Terraform-compatible image, use the bundled [`../build_image.sh`](../build_image.sh) helper — it stages the exact layout `deploy_cloud_run_job.sh` produces in its temp dir, then runs `gcloud builds submit` against that staging dir. Same image contents either way; Terraform just consumes the publish artifact instead of doing the build inline. For schema-derived deploys (`property_graph = true`), build the image with `build_image.sh --property-graph path/to/property_graph.sql` so the placeholdered `property_graph.sql` + `table_ddl.sql` are staged instead of `ontology.yaml`/`binding.yaml`.

```bash
# From the repo root.
IMAGE_URI="$(./examples/migration_v5/periodic_materialization/build_image.sh \
  --project my-project \
  --repo bqaa \
  --region us-central1 \
  --create-repo)"
echo "$IMAGE_URI"
# → us-central1-docker.pkg.dev/my-project/bqaa/periodic-materialization:<tag>

# Then point Terraform at the published image:
terraform apply -var "image_uri=$IMAGE_URI" -var "project_id=my-project" ...
```

CI pipelines should call `build_image.sh` (or replicate the staging layout themselves) on every commit that touches `run_job.py`, the demo artifacts, or the vendored SDK source.

## Bash deploy vs Terraform module

| Dimension | `deploy_cloud_run_job.sh` | Terraform module |
|---|---|---|
| Source build | ✓ (Cloud Buildpacks from local sources, inline) | — (caller's CI) |
| Audience | Notebook reader, evaluator, one-shot deploy | Infra team, managed-fleet ops, multi-env |
| Plan / preview before apply | ✗ | ✓ (`terraform plan`) |
| Drift detection | ✗ | ✓ (`terraform plan` shows drift) |
| Idempotent re-apply | ✓ (skip-if-exists per resource) | ✓ (declarative; state file owns reconciliation) |
| State store | None — re-runs probe live state | Terraform state (recommended: GCS-backed remote state) |
| Output for downstream wiring | Echo block (string scrape) | `terraform output` (machine-readable) |
| IAM propagation race | `_retry_iam` shell wrapper | `depends_on` dependency graph |
| Smoke run | `--smoke` flag | `gcloud run jobs execute` after `terraform apply` |

Both tools land **the same six resources with the same flag surface**. If the bash deploy works for your operations model, this Terraform module is not a forced upgrade — it's a parallel option for customers whose infra teams already operate IaC.

## Quickstart

### 0. Project APIs

A clean GCP project doesn't have the BigQuery / Cloud Run / Cloud Scheduler / IAM APIs enabled by default. The module enables them on `terraform apply` via `google_project_service` resources (with `disable_on_destroy = false`, so a `terraform destroy` of this module doesn't disable APIs other workloads might depend on). Customers whose central infra repo manages project services elsewhere can set `manage_apis = false`.

If `manage_apis = false`, enable the APIs manually before `terraform apply`:

```bash
gcloud services enable \
  bigquery.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com \
  --project=my-project

# Only needed in ai-fallback mode (the default):
gcloud services enable aiplatform.googleapis.com --project=my-project
```

For the image-build step (Cloud Buildpacks via `gcloud builds submit`), also enable `cloudbuild.googleapis.com` and `artifactregistry.googleapis.com` — `build_image.sh` invokes those but doesn't auto-enable them.

### 1. Configure your inputs

```hcl
# terraform.tfvars
project_id         = "my-project"
region             = "us-central1"
events_dataset_id  = "agent_analytics"
graph_dataset_id   = "migration_v5_graph"
schedule           = "0 */6 * * *"
image_uri          = "us-central1-docker.pkg.dev/my-project/bqaa/periodic-materialization:v1"
# Optional overrides — defaults match the bash deploy
# property_graph        = false   # true → schema-derived mode (see below)
# extraction_mode       = "ai-fallback"
# endpoint              = ""       # AI.GENERATE model (BQAA_ENDPOINT); e.g. "gemini-3.5-flash"
# max_retries           = 2
# max_session_age_hours = 24
# single_sa             = false
# location              = "US"
```

#### Schema-derived mode (`property_graph = true`)

For a **rename-free, codelab-style graph** you can skip the explicit
`ontology.yaml` / `binding.yaml` and derive the spec from a property graph (the
[#286](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/286)
one-artifact flow). Set `property_graph = true` and build the image with
`build_image.sh --property-graph path/to/property_graph.sql` (a placeholdered
`${PROJECT_ID}` / `${DATASET}` `table_ddl.sql` must sit next to it). The module
then sets `BQAA_PROPERTY_GRAPH=property_graph.sql` on the Job so the runtime
derives the ontology + binding from the property graph + your live table
schemas. `property_graph = true` is rejected at plan time with
`extraction_mode = "compiled-only"` (no reference extractors are staged in
derived mode). Leave `property_graph = false` (default) for the explicit MAKO /
compiled-extractor path.

### 2. Init + plan + apply

```bash
cd examples/migration_v5/periodic_materialization/terraform
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

### 3. Inspect outputs

```bash
terraform output
# runtime_sa_email     = "bqaa-periodic-runtime-sa@my-project.iam.gserviceaccount.com"
# scheduler_sa_email   = "bqaa-periodic-scheduler-sa@my-project.iam.gserviceaccount.com"
# cloud_run_job_name   = "bqaa-periodic-materialization"
# scheduler_name       = "bqaa-periodic-materialization-cron"
# graph_dataset_id     = "migration_v5_graph"
```

### 4. Optional smoke

```bash
PROJECT=my-project   # or whatever you set ``project_id`` to in tfvars
REGION=us-central1   # or your ``region`` tfvars value

gcloud run jobs execute "$(terraform output -raw cloud_run_job_name)" \
  --project "$PROJECT" \
  --region "$REGION" \
  --wait
```

`project_id` and `region` aren't exported as Terraform outputs — they're inputs the caller already knows. Either inline them as shown above or, if you'd rather not retype, define them as outputs in your wrapper module that calls this one.

### 5. Tear down

```bash
terraform destroy
```

The destroy removes every resource the module created (graph dataset, both SAs, all IAM bindings, the Cloud Run Job, the scheduler trigger). The events dataset is **not** managed by the module and is left untouched.

## State file

Use a GCS-backed remote backend for any deployment beyond a single-operator experiment — local state files don't survive operator turnover and don't lock against concurrent applies. Minimal backend block:

```hcl
terraform {
  backend "gcs" {
    bucket = "your-tfstate-bucket"
    prefix = "bqaa/periodic-materialization"
  }
}
```

## Variables

See [`variables.tf`](./variables.tf) for the full surface and the per-variable rationale. Required:

* `project_id`
* `region`
* `events_dataset_id`
* `graph_dataset_id`
* `schedule`
* `image_uri`

Optional (defaults match `deploy_cloud_run_job.sh`):

* `location` — `"US"`
* `job_name` — `"bqaa-periodic-materialization"`
* `extraction_mode` — `"ai-fallback"`
* `endpoint` — `""` (AI.GENERATE model; wires `BQAA_ENDPOINT` when non-empty, e.g. `"gemini-3.5-flash"`; `""` → runtime default `gemini-2.5-flash`)
* `max_retries` — `2`
* `max_session_age_hours` — `null` (watchdog disabled)
* `single_sa` — `false` (split-SA default)
* `max_sessions` — `null` (unlimited)
* `lookback_hours` — `6`
* `overlap_minutes` — `15`
* `task_timeout_seconds` — `1800`
* `manage_apis` — `true` (enables BigQuery / Cloud Run / Cloud Scheduler / IAM / (conditionally) Vertex AI APIs via `google_project_service`; set `false` if your central infra repo manages project services elsewhere)
* `deletion_protection` — `false` (Cloud Run v2 Job deletion-protection. Default matches the bash deploy's `gcloud run jobs delete` lifecycle — `terraform destroy` works without a separate apply. Production deploys that want the safety net opt in with `true`)

## Outputs

* `runtime_sa_email`
* `scheduler_sa_email`
* `cloud_run_job_name`
* `scheduler_name`
* `graph_dataset_id`

See [`outputs.tf`](./outputs.tf) for the full definitions.
