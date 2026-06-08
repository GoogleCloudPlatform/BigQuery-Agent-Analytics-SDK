# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Module shape mirrors deploy_cloud_run_job.sh's section ordering:
#   1. Pre-create the graph dataset.
#   2. Service accounts (split by default; combined under single_sa).
#   3. Project + dataset IAM grants on the runtime SA.
#   4. Cloud Run v2 Job (with the env vars the bash deploy wires).
#   5. roles/run.invoker on the Cloud Run Job for the scheduler SA.
#   6. Cloud Scheduler trigger.
#
# Image build is OUTSIDE the module — Terraform takes the
# published ``var.image_uri`` as input. The bash deploy's
# Cloud Buildpacks + ``--source`` flow doesn't slot into IaC.

# ---- Locals ---- #

locals {
  runtime_sa_account_id = var.single_sa ? "bqaa-periodic-sa" : "bqaa-periodic-runtime-sa"
  runtime_sa_email      = "${local.runtime_sa_account_id}@${var.project_id}.iam.gserviceaccount.com"
  runtime_sa_display    = var.single_sa ? "BQAA periodic-materialization runtime + scheduler" : "BQAA periodic-materialization runtime"

  scheduler_sa_account_id = var.single_sa ? "bqaa-periodic-sa" : "bqaa-periodic-scheduler-sa"
  scheduler_sa_email      = "${local.scheduler_sa_account_id}@${var.project_id}.iam.gserviceaccount.com"
  scheduler_sa_display    = var.single_sa ? "BQAA periodic-materialization runtime + scheduler" : "BQAA periodic-materialization scheduler caller"

  scheduler_job_name = "${var.job_name}-cron"

  # Env vars wired into the Cloud Run Job. Mirrors the
  # bash deploy's ENV_VARS array (see
  # ``deploy_cloud_run_job.sh`` section 4) so a customer
  # comparing the two side-by-side sees the same env surface.
  env_vars = merge(
    {
      BQAA_PROJECT_ID        = var.project_id
      BQAA_EVENTS_DATASET_ID = var.events_dataset_id
      BQAA_GRAPH_DATASET_ID  = var.graph_dataset_id
      BQAA_LOCATION          = var.location
      BQAA_LOOKBACK_HOURS    = tostring(var.lookback_hours)
      BQAA_OVERLAP_MINUTES   = tostring(var.overlap_minutes)
      BQAA_EXTRACTION_MODE   = var.extraction_mode
      BQAA_MAX_RETRIES       = tostring(var.max_retries)
    },
    var.max_sessions == null ? {} : {
      BQAA_MAX_SESSIONS = tostring(var.max_sessions)
    },
    var.max_session_age_hours == null ? {} : {
      BQAA_MAX_SESSION_AGE_HOURS = tostring(var.max_session_age_hours)
    },
    # Compiled-only mode points the orchestrator at the
    # reference extractor module that ships under the demo
    # (the bash deploy stages ``reference_extractor.py`` next
    # to ``run_job.py``; a Terraform deploy expects the same
    # file alongside the entrypoint in the published image).
    var.extraction_mode == "compiled-only" ? {
      BQAA_REFERENCE_EXTRACTORS_MODULE = "reference_extractor"
    } : {},
    # Schema-derived mode (#286): tell the runtime to derive the spec from the
    # staged ``property_graph.sql`` instead of the explicit ontology/binding
    # pair. Mirrors the bash deploy's ``BQAA_PROPERTY_GRAPH`` wiring.
    var.property_graph ? {
      BQAA_PROPERTY_GRAPH = "property_graph.sql"
    } : {},
    # AI.GENERATE model selection (#298). Empty string (default) leaves
    # BQAA_ENDPOINT unset so the runtime keeps its own gemini-2.5-flash
    # default; a value wires the env var. Mirrors the bash deploy's
    # ``--endpoint`` → ``BQAA_ENDPOINT`` wiring. No-op under compiled-only.
    var.endpoint == "" ? {} : {
      BQAA_ENDPOINT = var.endpoint
    },
  )
}

# ---- 0. Project services (clean-project bootstrap) ---- #

# A fresh GCP project doesn't have the BigQuery / Cloud Run /
# Cloud Scheduler / IAM APIs enabled by default. Without these,
# ``terraform apply`` fails part-way through with confusing
# "API not enabled" errors per-resource. Enabling them up front
# matches what the bash deploy's "General prerequisites"
# section asks customers to do manually.
#
# ``disable_on_destroy = false`` is deliberate: another workload
# on the same project might depend on these APIs, and a
# ``terraform destroy`` of THIS module shouldn't disable them
# project-wide.
#
# Vertex AI (``aiplatform.googleapis.com``) is conditional on
# ``extraction_mode`` — only ai-fallback mode calls
# ``AI.GENERATE``. Mirrors the conditional IAM grant below.
#
# Customers whose central infra repo manages project services
# elsewhere can set ``var.manage_apis = false`` to skip these
# resources entirely.

locals {
  required_apis = var.manage_apis ? toset(concat(
    [
      "bigquery.googleapis.com",
      "run.googleapis.com",
      "cloudscheduler.googleapis.com",
      "iam.googleapis.com",
    ],
    var.extraction_mode == "ai-fallback" ? ["aiplatform.googleapis.com"] : [],
  )) : toset([])
}

resource "google_project_service" "required" {
  for_each = local.required_apis

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

# ---- 1. Graph dataset ---- #

# Pre-creating the dataset (instead of letting ``run_job.py``'s
# ``_ensure_graph_dataset`` create it on first run) keeps the
# runtime SA's perms narrow — it never needs
# ``bigquery.datasets.create``. Matches the bash deploy's
# ``bq mk`` step.
resource "google_bigquery_dataset" "graph" {
  project     = var.project_id
  dataset_id  = var.graph_dataset_id
  location    = var.location
  description = "BQAA periodic-materialization graph dataset (created by Terraform module)."

  # Deliberately no ``default_table_expiration_ms`` — the
  # entity / relationship tables are intended to persist across
  # cron runs and accumulate history. The bash deploy's
  # ``1h TTL`` is for the notebook's *throwaway scratch*
  # dataset, not production graph data.

  # Wait for the BigQuery API to be enabled before trying to
  # create the dataset; otherwise a clean-project ``terraform
  # apply`` errors here with "BigQuery API has not been used".
  depends_on = [google_project_service.required]
}

# ---- 2. Service accounts ---- #

# Runtime SA: identity for the Cloud Run Job. Holds the
# BigQuery (+ optionally Vertex AI) roles below.
resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = local.runtime_sa_account_id
  display_name = local.runtime_sa_display

  depends_on = [google_project_service.required]
}

# Scheduler-caller SA: identity for the Cloud Scheduler HTTP
# trigger. Holds ONLY ``roles/run.invoker`` on the specific
# Cloud Run Job (granted in the iam_member resource below).
# When ``var.single_sa == true``, ``account_id`` collapses to
# the same value as the runtime SA above — Terraform's
# duplicate-resource handling would normally reject that, so
# we gate this resource on ``count``.
resource "google_service_account" "scheduler" {
  count        = var.single_sa ? 0 : 1
  project      = var.project_id
  account_id   = local.scheduler_sa_account_id
  display_name = local.scheduler_sa_display

  depends_on = [google_project_service.required]
}

# ---- 3. IAM grants on the runtime SA ---- #

# Project-level: ``bigquery.jobs.create``. Required for any BQ
# query / DML / load job the orchestrator runs.
resource "google_project_iam_member" "runtime_bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Project-level: Vertex AI access (only in ai-fallback mode).
# Without this, ``AI.GENERATE`` returns "user does not have the
# permission" and the orchestrator silently extracts empty
# graphs. The bash deploy makes the grant conditional on
# ``--extraction-mode``; Terraform mirrors that via ``count``.
# Issue #166's verification surfaced this.
resource "google_project_iam_member" "runtime_aiplatform_user" {
  count   = var.extraction_mode == "ai-fallback" ? 1 : 0
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Dataset-level: read-only access to the events dataset. The
# events dataset stays effectively read-only per the README
# contract — the runtime SA reads ``agent_events`` and never
# writes here.
resource "google_bigquery_dataset_iam_member" "runtime_events_viewer" {
  project    = var.project_id
  dataset_id = var.events_dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

# Dataset-level: read + write on the graph dataset. The
# materializer's entity / relationship / state-table writes
# all land here.
resource "google_bigquery_dataset_iam_member" "runtime_graph_editor" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.graph.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

# ---- 4. Cloud Run v2 Job ---- #

resource "google_cloud_run_v2_job" "periodic" {
  project  = var.project_id
  location = var.region
  name     = var.job_name

  # ``DEFAULT`` launch stage matches what ``gcloud run jobs
  # deploy`` produces — the v2 API exposes a ``launch_stage``
  # argument but most customers leave it implicit. Set
  # explicitly so plan diffs against the bash deploy's
  # post-state stay clean.
  launch_stage = "GA"

  # Cloud Run v2 added a ``deletion_protection`` default of
  # ``true`` in newer provider releases. Leaving it on would
  # make ``terraform destroy`` fail with "cannot destroy job
  # without setting deletion_protection=false and running
  # ``terraform apply``". The bash deploy has no analogous
  # block — its ``gcloud run jobs delete`` cleanup just works.
  # We surface the setting as ``var.deletion_protection`` so
  # production deploys can opt back in, but the module
  # default is ``false`` to match the bash deploy's lifecycle.
  deletion_protection = var.deletion_protection

  template {
    # Cloud Run owns the retry policy here. Matches the bash
    # deploy's ``--max-retries "$MAX_RETRIES"``; the runtime
    # also reads BQAA_MAX_RETRIES from env to surface the
    # value in Cloud Logging (issue #183).
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runtime.email
      max_retries     = var.max_retries
      timeout         = "${var.task_timeout_seconds}s"

      containers {
        image = var.image_uri

        dynamic "env" {
          for_each = local.env_vars
          content {
            name  = env.key
            value = env.value
          }
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  # Schema-derived mode has no reference extractors staged, so
  # ``compiled-only`` would empty_extract at runtime. Reject the
  # combination at plan time, mirroring the bash deploy's boundary check.
  lifecycle {
    precondition {
      condition     = !(var.property_graph && var.extraction_mode == "compiled-only")
      error_message = "property_graph = true (schema-derived mode) does not support extraction_mode = \"compiled-only\": no reference extractors are staged in derived mode. Use \"ai-fallback\", or the explicit ontology/binding path."
    }
  }

  # Allow IAM bindings on the runtime SA to settle before the
  # Cloud Run Job starts. Same race the bash deploy's
  # ``_retry_iam`` defends against — Terraform's dependency
  # graph lets us express it declaratively instead.
  depends_on = [
    google_project_iam_member.runtime_bigquery_job_user,
    google_bigquery_dataset_iam_member.runtime_events_viewer,
    google_bigquery_dataset_iam_member.runtime_graph_editor,
    google_project_iam_member.runtime_aiplatform_user,
  ]
}

# ---- 5. roles/run.invoker on the job for the scheduler SA ---- #

# The scheduler-caller SA holds ONLY this binding (least-
# privilege). The grant lives at the job-resource level, not
# project-wide — the SA can fire THIS job and nothing else.
# Under ``var.single_sa``, the binding lands on the same SA
# the Cloud Run Job runs as.
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.periodic.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.scheduler_sa_email}"

  depends_on = [
    google_service_account.runtime,
    google_service_account.scheduler,
  ]
}

# ---- 6. Cloud Scheduler trigger ---- #

resource "google_cloud_scheduler_job" "periodic_cron" {
  project  = var.project_id
  region   = var.region
  name     = local.scheduler_job_name
  schedule = var.schedule
  # UTC is the only sane default for a global SDK; customers
  # who want local time can override via a wrapper.
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    # Cloud Run Jobs ``:run`` endpoint. The path uses the v1
    # namespaces-style URL because Cloud Scheduler's
    # ``--http-method`` POST requires that shape; the v2 Cloud
    # Run Job resource is happy to be invoked through it.
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.periodic.name}:run"

    oauth_token {
      service_account_email = local.scheduler_sa_email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  # The scheduler grant must be in place before the trigger
  # fires the first time.
  depends_on = [
    google_cloud_run_v2_job_iam_member.scheduler_invoker,
  ]
}
