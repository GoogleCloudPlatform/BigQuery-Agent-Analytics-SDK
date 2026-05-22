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

output "runtime_sa_email" {
  description = "Email of the runtime service account (the Cloud Run Job's identity). Useful for downstream modules wiring additional dataset-level grants — e.g. read access on a second events dataset for a multi-tenant deployment."
  value       = google_service_account.runtime.email
}

output "scheduler_sa_email" {
  description = "Email of the scheduler-caller service account (the Cloud Scheduler trigger's OAuth identity). Under ``var.single_sa`` this equals ``runtime_sa_email``."
  value       = local.scheduler_sa_email
}

output "cloud_run_job_name" {
  description = "Cloud Run Job name. Pass to ``gcloud run jobs execute`` to fire a manual invocation, or use as the ``--job`` selector when listing executions."
  value       = google_cloud_run_v2_job.periodic.name
}

output "scheduler_name" {
  description = "Cloud Scheduler trigger name. Pass to ``gcloud scheduler jobs pause`` / ``resume`` for ops control without re-applying the module."
  value       = google_cloud_scheduler_job.periodic_cron.name
}

output "graph_dataset_id" {
  description = "Materialized graph dataset ID (the BQ dataset the entity / relationship tables + ``_bqaa_materialization_state`` audit table live in)."
  value       = google_bigquery_dataset.graph.dataset_id
}
