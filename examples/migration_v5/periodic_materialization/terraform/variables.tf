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

# Variables mirror the post-#230 deploy-script surface (split SAs
# + tunable max_retries by default; --single-sa as the escape
# hatch). The image build is intentionally outside the module —
# Terraform takes the published container image as input via
# ``image_uri`` and leaves build/publish to the caller's CI.

# ---- Required inputs ---- #

variable "project_id" {
  description = "GCP project ID. The graph dataset, service accounts, Cloud Run Job, and Cloud Scheduler trigger all land in this project."
  type        = string
}

variable "region" {
  description = "Cloud Run region (e.g. ``us-central1``). The Cloud Scheduler trigger is created in the same region by convention so a single regional outage doesn't strand the trigger pointing at an unreachable job."
  type        = string
}

variable "events_dataset_id" {
  description = "BigQuery dataset that holds ``agent_events`` (the BQ AA plugin's write target). The runtime SA gets dataset-level ``roles/bigquery.dataViewer`` on this dataset; reads only, never writes."
  type        = string
}

variable "graph_dataset_id" {
  description = "BigQuery dataset where the materialized property-graph tables + the ``_bqaa_materialization_state`` audit table live. The module pre-creates this dataset (so the runtime SA never needs ``bigquery.datasets.create``) and grants the runtime SA ``roles/bigquery.dataEditor`` on it."
  type        = string
}

variable "schedule" {
  description = "Cloud Scheduler cron expression (e.g. ``\"0 */6 * * *\"`` for every six hours). Combined with the orchestrator's ``BQAA_LOOKBACK_HOURS`` + ``BQAA_OVERLAP_MINUTES`` env vars to decide the discovery window per fire."
  type        = string
}

variable "image_uri" {
  description = "Fully-qualified container image URI (``REGION-docker.pkg.dev/PROJECT/REPO/IMAGE:TAG`` or any reachable registry path). Image build + publish are intentionally OUTSIDE this module — the bash deploy script builds from local sources via Cloud Buildpacks; Terraform takes the published image as input so IaC stays declarative. See the README for the recommended build path (CI builds + publishes; Terraform deploys)."
  type        = string
}

# ---- Optional inputs (defaults match deploy_cloud_run_job.sh) ---- #

variable "location" {
  description = "BigQuery dataset location (``US``, ``EU``, region-scoped like ``us-central1``). Must match both the events dataset and the graph dataset — cross-region BQ queries get rejected."
  type        = string
  default     = "US"
}

variable "job_name" {
  description = "Cloud Run Job name. Default matches the bash deploy script."
  type        = string
  default     = "bqaa-periodic-materialization"
}

variable "extraction_mode" {
  description = "Extraction path: ``ai-fallback`` (structured extractors + AI.GENERATE for any uncovered span) or ``compiled-only`` (structured extractors only; no LLM calls; uncovered spans surface as typed ``empty_extraction`` failures with sample diagnostics). The module grants ``roles/aiplatform.user`` to the runtime SA only in ai-fallback mode. Note: ``empty_extraction`` is the failure code for compiled-only extraction gaps; ``session_orphaned`` is a *separate* code emitted by the orphan watchdog (``var.max_session_age_hours``) — they live on different cron paths."
  type        = string
  default     = "ai-fallback"
  validation {
    condition     = contains(["ai-fallback", "compiled-only"], var.extraction_mode)
    error_message = "extraction_mode must be 'ai-fallback' or 'compiled-only'."
  }
}

variable "property_graph" {
  description = "Schema-derived mode (#286). When ``true``, the runtime derives the ontology + binding from a staged ``property_graph.sql`` + the table schemas instead of an explicit ``ontology.yaml`` / ``binding.yaml`` pair (the module sets ``BQAA_PROPERTY_GRAPH=property_graph.sql``). The published image must be built with ``build_image.sh --property-graph`` so the placeholdered (``$${PROJECT_ID}`` / ``$${DATASET}``) ``property_graph.sql`` + ``table_ddl.sql`` are staged. Use for rename-free graphs; not compatible with ``extraction_mode = \"compiled-only\"`` (no reference extractors are staged in derived mode). ``false`` (default) = explicit ontology + binding (the migration-v5 / compiled-extractor path)."
  type        = bool
  default     = false
}

variable "max_retries" {
  description = "Cloud Run Job retry count on failure. The orchestrator's session-level idempotency + append-only state table make additional retries safe. Default 2 matches the deploy script post-#183 (production posture: silently absorb transient BQ slot pressure / rate-limit noise instead of paging on-call)."
  type        = number
  default     = 2
  validation {
    condition     = var.max_retries >= 0
    error_message = "max_retries must be >= 0."
  }
}

variable "max_session_age_hours" {
  description = "If set (> 0), enables the orphan-session watchdog (issue #180). Each cron pass additionally scans for sessions whose first event is older than N hours but which never emitted ``AGENT_COMPLETED``; each new orphan surfaces as a typed ``session_orphaned`` failure. ``null`` (default) disables the watchdog."
  type        = number
  default     = null
}

variable "single_sa" {
  description = "If ``true``, use a single combined service account (``bqaa-periodic-sa``) for both the Cloud Run Job runtime AND the Cloud Scheduler OAuth caller. The default (``false``) creates two SAs — ``bqaa-periodic-runtime-sa`` (BQ + Vertex AI roles) and ``bqaa-periodic-scheduler-sa`` (only ``roles/run.invoker`` on the job) — for least-privilege per #182."
  type        = bool
  default     = false
}

variable "max_sessions" {
  description = "Cost guardrail: hard cap on the number of sessions materialized per cron run. ``null`` (default) means unlimited."
  type        = number
  default     = null
}

variable "lookback_hours" {
  description = "Discovery scan window size (hours). The orchestrator scans events whose terminal-event timestamp falls in ``[scan_start, scan_end)`` where ``scan_end = now`` and ``scan_start = max(last_checkpoint - overlap_minutes, now - lookback_hours)``."
  type        = number
  default     = 6
}

variable "overlap_minutes" {
  description = "Re-scan window for late-arriving events. The orchestrator re-considers events newer than ``last_checkpoint - overlap_minutes`` on every pass so a late insert doesn't get silently skipped. Bump higher (e.g. 60) if the events table sometimes lags ingestion by tens of minutes."
  type        = number
  default     = 15
}

variable "task_timeout_seconds" {
  description = "Per-task Cloud Run timeout in seconds. Matches the bash deploy script's ``--task-timeout 30m`` (= 1800s). Bump higher if your average session has very long event histories or if AI.GENERATE responses are slow."
  type        = number
  default     = 1800
}

variable "manage_apis" {
  description = "If ``true`` (default), the module enables the BigQuery, Cloud Run, Cloud Scheduler, IAM, and (in ai-fallback mode) Vertex AI APIs on the project via ``google_project_service`` with ``disable_on_destroy = false``. Set ``false`` if the operator's central infra repo manages project services elsewhere and granting service-management perms to the deploy SA is undesired."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Cloud Run v2 Job deletion-protection setting. Default ``false`` so ``terraform destroy`` works without a separate ``terraform apply`` to clear the flag (matches the bash deploy's ``gcloud run jobs delete`` lifecycle). Production deploys that want the extra safety net can set ``true`` — at the cost of needing a two-step destroy (apply with ``false``, then destroy)."
  type        = bool
  default     = false
}
