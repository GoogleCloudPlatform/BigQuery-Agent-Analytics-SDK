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

# Provider version pins. The Cloud Run Job resource family
# (``google_cloud_run_v2_job``) stabilised in google ~> 4.40,
# the dataset IAM ``google_bigquery_dataset_iam_member`` resource
# has been stable for years. Pin a generous lower bound rather
# than a hard exact match so customers on a slightly newer
# provider keep working — the module's surface only uses
# long-stable resource arguments.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 4.84.0, < 7.0.0"
    }
  }
}
