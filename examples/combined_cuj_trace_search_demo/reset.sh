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

load_safe_env() {
  local env_file="$1"
  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    local line
    line="${raw_line#"${raw_line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    local key="${line%%=*}"
    local val="${line#*=}"
    val="${val#\"}"; val="${val%\"}"
    val="${val#\'}"; val="${val%\'}"
    case "$key" in
      PROJECT_ID|DATASET_ID|TABLE_ID|EVAL_TURNS_TABLE|BQ_LOCATION)
        printf -v "$key" '%s' "$val"
        ;;
    esac
  done < "$env_file"
}

validate_bq_ident() {
  local name="$1"
  local val="$2"
  local pattern="$3"
  if [[ ! "$val" =~ $pattern ]]; then
    echo "ERROR: Invalid $name in .env: $val" >&2
    exit 1
  fi
}

check_table_owned_by_demo() {
  local fq_colon="$1"
  local show_out
  if ! show_out="$(bq show --format=prettyjson "$fq_colon" 2>&1)"; then
    if [[ "$show_out" == *"Not found"* ]]; then
      return 2
    fi
    echo "ERROR: Failed to inspect ${fq_colon}: ${show_out}" >&2
    return 1
  fi
  if PYTHONSAFEPATH=1 python3 - "$show_out" <<'PY' >/dev/null 2>&1
import json, sys
data = json.loads(sys.argv[1])
raise SystemExit(0 if data.get("labels", {}).get("bqaa_demo") == "combined_cuj_trace_search" else 1)
PY
  then
    return 0
  fi
  echo "ERROR: Refusing to modify ${fq_colon}: missing demo ownership label bqaa_demo=combined_cuj_trace_search." >&2
  return 1
}

if [[ "$DELETE_SCALE_ROWS" == "true" || "$DROP_TABLES" == "true" ]]; then
  if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "No .env found; skipping BigQuery cleanup."
    exit 0
  fi
  PROJECT_ID=""
  DATASET_ID=""
  TABLE_ID="agent_events"
  EVAL_TURNS_TABLE="eval_turns"
  BQ_LOCATION="US"
  load_safe_env "$SCRIPT_DIR/.env"
  : "${PROJECT_ID:?missing in .env}"
  : "${DATASET_ID:?missing in .env}"
  validate_bq_ident "PROJECT_ID" "$PROJECT_ID" '^[A-Za-z0-9_-]+$'
  validate_bq_ident "DATASET_ID" "$DATASET_ID" '^[A-Za-z0-9_]+$'
  validate_bq_ident "TABLE_ID" "$TABLE_ID" '^[A-Za-z0-9_]+$'
  validate_bq_ident "EVAL_TURNS_TABLE" "$EVAL_TURNS_TABLE" '^[A-Za-z0-9_]+$'
  validate_bq_ident "BQ_LOCATION" "$BQ_LOCATION" '^[A-Za-z0-9_-]+$'

  FAILURES=0
  if [[ "$DELETE_SCALE_ROWS" == "true" ]]; then
    set +e
    check_table_owned_by_demo "${PROJECT_ID}:${DATASET_ID}.${EVAL_TURNS_TABLE}"
    own_rc=$?
    set -e
    if [[ "$own_rc" -eq 0 ]]; then
      echo "Deleting synthetic scale rows (scale_synth_*) from ${PROJECT_ID}.${DATASET_ID}.${EVAL_TURNS_TABLE}..."
      if ! bq query --project_id="$PROJECT_ID" --location="$BQ_LOCATION" --use_legacy_sql=false \
        "DELETE FROM \`${PROJECT_ID}.${DATASET_ID}.${EVAL_TURNS_TABLE}\` WHERE STARTS_WITH(session_id, 'scale_synth_')"; then
        FAILURES=$((FAILURES + 1))
      fi
    elif [[ "$own_rc" -eq 2 ]]; then
      echo "Table ${PROJECT_ID}:${DATASET_ID}.${EVAL_TURNS_TABLE} not found; skipping scale row cleanup."
    else
      FAILURES=$((FAILURES + 1))
    fi
  fi

  if [[ "$DROP_TABLES" == "true" ]]; then
    echo "Dropping demo-owned BigQuery tables in ${PROJECT_ID}:${DATASET_ID}..."
    for tbl in "$TABLE_ID" "$EVAL_TURNS_TABLE"; do
      set +e
      check_table_owned_by_demo "${PROJECT_ID}:${DATASET_ID}.${tbl}"
      own_rc=$?
      set -e
      if [[ "$own_rc" -eq 2 ]]; then
        echo "  Skipped ${tbl} (already absent)."
      elif [[ "$own_rc" -ne 0 ]]; then
        FAILURES=$((FAILURES + 1))
      elif ! bq rm -f -t "${PROJECT_ID}:${DATASET_ID}.${tbl}"; then
        FAILURES=$((FAILURES + 1))
      fi
    done
    if [[ "$FAILURES" -eq 0 ]]; then
      echo "BigQuery demo tables dropped."
    fi
  fi

  if [[ "$FAILURES" -ne 0 ]]; then
    echo "ERROR: BigQuery cleanup encountered ${FAILURES} failure(s)." >&2
    exit 1
  fi
fi
