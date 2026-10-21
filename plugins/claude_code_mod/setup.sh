#!/usr/bin/env bash
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

# Creates what the mod writes to, and the usage views over it, with the bq CLI:
#   1. The dataset for the events table, when it is missing, in BQAA_LOCATION.
#   2. The events table from sql/agent_events_ddl.sql (BQAA schema v2,
#      partitioned by day on timestamp, clustered on event_type, agent and
#      user_id) when it is missing, and its event_id column.
#   3. A second dataset for the views (default <dataset>_reporting): people
#      who read the views then need no access to the table, and the
#      identities that write rows none to the views.
#   4. The views in sql/views/: daily_usage_by_model and daily_usage_by_user.
#
# Safe to run again: it creates what is missing, replaces the two views and
# removes nothing. --dry-run prints the bq commands and the SQL and runs none.
#
# Usage:
#   BQAA_PROJECT_ID=my-project BQAA_DATASET=agent_analytics ./setup.sh [--dry-run]
#
# Settings, read from the same environment variables as the mod:
#   BQAA_PROJECT_ID     the project (or GCP_PROJECT_ID, GOOGLE_CLOUD_PROJECT)
#   BQAA_DATASET        the dataset of the events table (or BQ_DATASET)
#   BQAA_TABLE          the events table (or BQ_TABLE; default agent_events)
#   BQAA_LOCATION       where new datasets go (default US)
#   BQAA_VIEWS_DATASET  the dataset of the views (default <dataset>_reporting)
#
# Needs the bq CLI, signed in as someone who may create datasets, tables and
# views in the project.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

dry_run=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -h | --help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "setup.sh: unknown argument \"$arg\" (try --dry-run or --help)" >&2
      exit 2
      ;;
  esac
done

project="${BQAA_PROJECT_ID:-${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}}"
dataset="${BQAA_DATASET:-${BQ_DATASET:-}}"
table="${BQAA_TABLE:-${BQ_TABLE:-agent_events}}"
location="${BQAA_LOCATION:-US}"
views_dataset="${BQAA_VIEWS_DATASET:-${dataset}_reporting}"

fail() {
  echo "setup.sh: $*" >&2
  exit 1
}

[[ -n "$project" ]] || fail "set BQAA_PROJECT_ID to the project that holds the dataset"
[[ -n "$dataset" ]] || fail "set BQAA_DATASET to the dataset for the events table"
# The names go into SQL and into sed patterns, so only plain ones are taken.
# A project id is 6 to 30 lowercase letters, digits and hyphens, starting with
# a letter; a domain-scoped one has its domain and a colon in front.
[[ "$project" =~ ^([a-z0-9.-]+:)?[a-z][-a-z0-9]{4,28}[a-z0-9]$ ]] || fail "BQAA_PROJECT_ID \"$project\" is not a project id"
for name in "$dataset" "$table" "$views_dataset"; do
  [[ "$name" =~ ^[A-Za-z0-9_]+$ && ${#name} -le 1024 ]] || fail "\"$name\" is not a plain dataset or table name (letters, digits and underscores)"
done
[[ "$location" =~ ^[A-Za-z0-9-]+$ ]] || fail "BQAA_LOCATION \"$location\" is not a location"

# A SQL file with its placeholders filled in.
render() {
  sed -e "s/{PROJECT}/${project}/g" \
    -e "s/{VIEWS_DATASET}/${views_dataset}/g" \
    -e "s/{DATASET}/${dataset}/g" \
    -e "s/{TABLE}/${table}/g" \
    "$1"
}

# Runs a SQL file as one standard-SQL script, or prints it.
query() {
  local sql
  sql="$(render "$1")"
  if ((dry_run)); then
    printf '+ bq --project_id=%s query --use_legacy_sql=false --location=%s <<SQL\n%s\nSQL\n' "$project" "$location" "$sql"
  else
    printf '%s\n' "$sql" | bq --project_id="$project" query --use_legacy_sql=false --location="$location" >/dev/null
  fi
}

# Creates a dataset unless it exists.
dataset_ready() {
  if ((dry_run)); then
    printf '+ bq --project_id=%s show --dataset %s:%s || bq --project_id=%s --location=%s mk --dataset --description=%q %s:%s\n' \
      "$project" "$project" "$1" "$project" "$location" "$2" "$project" "$1"
  elif bq --project_id="$project" show --dataset "$project:$1" >/dev/null 2>&1; then
    echo "==> $project:$1 is there already"
  else
    echo "==> $project:$1 is missing: making it, in $location"
    bq --project_id="$project" --location="$location" mk --dataset --description="$2" "$project:$1" >/dev/null
  fi
}

echo "==> $project: table $dataset.$table, views in $views_dataset"
if ((dry_run)); then echo "==> Dry run: nothing is created"; fi

dataset_ready "$dataset" "BigQuery Agent Analytics events written by the bigquery-agent-analytics-mod coding-agent mod"
echo "==> Table $dataset.$table (created if missing)"
query "$here/sql/agent_events_ddl.sql"

dataset_ready "$views_dataset" "Usage views over $project.$dataset.$table"
for view in daily_usage_by_model daily_usage_by_user; do
  echo "==> View $views_dataset.$view"
  query "$here/sql/views/$view.sql"
done

echo
echo "Done. The mod writes to $project.$dataset.$table with these options:"
echo "  project: $project"
echo "  dataset: $dataset"
if [[ "$table" != agent_events ]]; then echo "  table: $table"; fi
# The bootstrap's names for what is not its default: the table, the views
# dataset and the location.
names=""
if [[ "$table" != agent_events ]]; then names+=" --table $table"; fi
if [[ "$views_dataset" != "${dataset}_reporting" ]]; then names+=" --views-dataset $views_dataset"; fi
if [[ "$location" != US ]]; then names+=" --location $location"; fi
echo "Next, who writes and who reads (docs/service-account-auth.md). For a team, from a"
echo "checkout of haiyuan-eng-google/bigquery_agent_analytics_skill, which holds the bootstrap:"
echo "  python3 plugins/bigquery-agent-analytics-tracing/scripts/setup_gcp_prereqs.py \\"
echo "    --project $project --dataset $dataset$names --no-create-dataset --no-create-table \\"
echo "    --service-account bqaa-row-writer --writer-role row-writer \\"
echo "    --token-creator group:WRITERS --reader group:READERS"
echo "sets up a writer service account that may only insert rows into the table, and"
echo "readers of the views in $project:$views_dataset who need no access to the table."
