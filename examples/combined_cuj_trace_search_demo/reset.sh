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

# Cleans local demo outputs and caches; optionally drops demo BigQuery tables
# or deletes synthetic scale benchmark rows.
# Usage:
#   ./reset.sh                     # Local cleanup only
#   ./reset.sh --delete-scale-rows # Also delete scale_synth_* rows in BigQuery
#   ./reset.sh --drop-demo-tables  # Also delete scale rows and drop BigQuery tables in .env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DROP_TABLES=false
DELETE_SCALE_ROWS=false

for arg in "$@"; do
  case "$arg" in
    --drop-demo-tables)
      DROP_TABLES=true
      ;;
    --delete-scale-rows)
      DELETE_SCALE_ROWS=true
      ;;
    -h|--help)
      echo "Usage: ./reset.sh [--delete-scale-rows] [--drop-demo-tables]"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

rm -rf "$SCRIPT_DIR/reports" "$SCRIPT_DIR/.pytest_cache" \
  "$SCRIPT_DIR/__pycache__" "$SCRIPT_DIR/agent/__pycache__" \
  "$SCRIPT_DIR/analytics/__pycache__" "$SCRIPT_DIR/report/__pycache__" \
  "$SCRIPT_DIR/tests/__pycache__"

echo "Removed local reports and Python caches."

if [[ "$DELETE_SCALE_ROWS" == "true" || "$DROP_TABLES" == "true" ]]; then
  if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "No .env found; skipping BigQuery cleanup."
    exit 0
  fi
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
  PROJECT_ID="${PROJECT_ID:?missing in .env}"
  DATASET_ID="${DATASET_ID:?missing in .env}"
  TABLE_ID="${TABLE_ID:-agent_events}"
  EVAL_TURNS_TABLE="${EVAL_TURNS_TABLE:-eval_turns}"
  BQ_LOCATION="${BQ_LOCATION:-US}"

  echo "Deleting synthetic scale rows (scale_synth_*) from ${PROJECT_ID}.${DATASET_ID}.${EVAL_TURNS_TABLE}..."
  bq query --project_id="$PROJECT_ID" --location="$BQ_LOCATION" --use_legacy_sql=false \
    "DELETE FROM \`${PROJECT_ID}.${DATASET_ID}.${EVAL_TURNS_TABLE}\` WHERE STARTS_WITH(session_id, 'scale_synth_')" || true

  if [[ "$DROP_TABLES" == "true" ]]; then
    echo "Dropping BigQuery demo tables in ${PROJECT_ID}:${DATASET_ID}..."
    bq rm -f -t "${PROJECT_ID}:${DATASET_ID}.${TABLE_ID}" || true
    bq rm -f -t "${PROJECT_ID}:${DATASET_ID}.${EVAL_TURNS_TABLE}" || true
    bq rm -f -t "${PROJECT_ID}:${DATASET_ID}.${EVAL_TURNS_TABLE}_scale" || true
    echo "BigQuery demo tables dropped."
  fi
fi
