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
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
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

resolve_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    echo "$PYTHON_BIN"
  elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    echo "$SCRIPT_DIR/.venv/bin/python"
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    echo "$REPO_ROOT/.venv/bin/python"
  else
    echo "python3"
  fi
}

PYTHON_BIN="$(resolve_python)"

EVAL_TURNS_TABLE_EXPLICIT=false
if [[ -n "${EVAL_TURNS_TABLE:-}" ]]; then
  EVAL_TURNS_TABLE_EXPLICIT=true
fi

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
        if [[ -z "${!key:-}" ]]; then
          printf -v "$key" '%s' "$val"
          if [[ "$key" == "EVAL_TURNS_TABLE" && -n "$val" ]]; then
            EVAL_TURNS_TABLE_EXPLICIT=true
          fi
        fi
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

# Return codes:
#   0 = table exists and has bqaa_demo=combined_cuj_trace_search
#   2 = table confirmed absent (404 / Not found)
#   3 = table exists and is NOT owned by this demo
#   1 = inspection / auth / 503 / parse error (fail closed)
check_table_owned_by_demo() {
  local fq_colon="$1"
  local show_out=""
  local err_file=""
  local err_out=""
  err_file="$(mktemp)"
  if ! show_out="$(bq show --format=prettyjson "$fq_colon" 2>"$err_file")"; then
    err_out="$(cat "$err_file")"
    rm -f "$err_file"
    if [[ "$err_out" == *"Not found"* || "$show_out" == *"Not found"* ]]; then
      return 2
    fi
    echo "ERROR: Failed to inspect ${fq_colon}: ${err_out:-$show_out}" >&2
    return 1
  fi
  rm -f "$err_file"
  local parse_rc=0
  if printf '%s' "$show_out" | PYTHONSAFEPATH=1 "$PYTHON_BIN" -c '
import json, sys
try:
  data = json.load(sys.stdin)
except Exception:
  raise SystemExit(1)
if not isinstance(data, dict):
  raise SystemExit(1)
labels = data.get("labels") or {}
if not isinstance(labels, dict):
  raise SystemExit(1)
raise SystemExit(0 if labels.get("bqaa_demo") == "combined_cuj_trace_search" else 3)
' >/dev/null 2>&1; then
    parse_rc=0
  else
    parse_rc=$?
  fi
  if [[ "$parse_rc" -eq 0 ]]; then
    return 0
  elif [[ "$parse_rc" -eq 3 ]]; then
    return 3
  fi
  echo "ERROR: Failed to parse bq show metadata for ${fq_colon}." >&2
  return 1
}

discover_run_scoped_tables() {
  RUN_SCOPED_TABLES=()
  local ls_out=""
  local ls_rc=0
  local err_file=""
  local err_out=""
  err_file="$(mktemp)"
  if ls_out="$(bq ls --format=json --max_results=10000 "${PROJECT_ID}:${DATASET_ID}" 2>"$err_file")"; then
    ls_rc=0
  else
    ls_rc=$?
  fi
  err_out="$(cat "$err_file")"
  rm -f "$err_file"
  if [[ "$ls_rc" -ne 0 ]]; then
    if [[ "$err_out" == *"Not found"* || "$ls_out" == *"Not found"* ]]; then
      return 0
    fi
    echo "ERROR: Failed to list tables in ${PROJECT_ID}:${DATASET_ID}: ${err_out:-$ls_out}" >&2
    FAILURES=$((FAILURES + 1))
    return 1
  fi
  local parsed_list=""
  local parse_rc=0
  if parsed_list="$(
    printf '%s' "$ls_out" | PYTHONSAFEPATH=1 "$PYTHON_BIN" -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
  raise SystemExit(0)
try:
  items = json.loads(raw)
except json.JSONDecodeError:
  raise SystemExit(1)
if not isinstance(items, list):
  raise SystemExit(1)
for item in items:
  tid = (item.get("tableReference") or {}).get("tableId", "")
  if isinstance(tid, str) and (tid.startswith("eval_turns_") or "__stage_" in tid):
    print(tid)
'
  )"; then
    parse_rc=0
  else
    parse_rc=$?
  fi
  if [[ "$parse_rc" -ne 0 ]]; then
    echo "ERROR: Failed to parse bq ls JSON for ${PROJECT_ID}:${DATASET_ID}." >&2
    FAILURES=$((FAILURES + 1))
    return 1
  fi
  while IFS= read -r cand_tbl; do
    [[ -z "$cand_tbl" || "$cand_tbl" == "$TABLE_ID" || "$cand_tbl" == "$EVAL_TURNS_TABLE" ]] && continue
    RUN_SCOPED_TABLES+=("$cand_tbl")
  done <<< "$parsed_list"
  return 0
}

if [[ "$DELETE_SCALE_ROWS" == "true" || "$DROP_TABLES" == "true" ]]; then
  if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "No .env found; skipping BigQuery cleanup."
    exit 0
  fi
  PROJECT_ID="${PROJECT_ID:-}"
  DATASET_ID="${DATASET_ID:-}"
  TABLE_ID="${TABLE_ID:-}"
  EVAL_TURNS_TABLE="${EVAL_TURNS_TABLE:-}"
  BQ_LOCATION="${BQ_LOCATION:-}"
  load_safe_env "$SCRIPT_DIR/.env"
  TABLE_ID="${TABLE_ID:-agent_events}"
  EVAL_TURNS_TABLE="${EVAL_TURNS_TABLE:-eval_turns}"
  BQ_LOCATION="${BQ_LOCATION:-US}"
  : "${PROJECT_ID:?missing in .env}"
  : "${DATASET_ID:?missing in .env}"
  validate_bq_ident "PROJECT_ID" "$PROJECT_ID" '^[A-Za-z0-9_-]+$'
  validate_bq_ident "DATASET_ID" "$DATASET_ID" '^[A-Za-z0-9_]+$'
  validate_bq_ident "TABLE_ID" "$TABLE_ID" '^[A-Za-z0-9_]+$'
  validate_bq_ident "EVAL_TURNS_TABLE" "$EVAL_TURNS_TABLE" '^[A-Za-z0-9_]+$'
  validate_bq_ident "BQ_LOCATION" "$BQ_LOCATION" '^[A-Za-z0-9_-]+$'

  FAILURES=0
  RUN_SCOPED_TABLES=()
  discover_run_scoped_tables || true

  if [[ "$DELETE_SCALE_ROWS" == "true" ]]; then
    SCALE_CANDIDATES=("$EVAL_TURNS_TABLE")
    if [[ "${#RUN_SCOPED_TABLES[@]}" -gt 0 ]]; then
      for run_tbl in "${RUN_SCOPED_TABLES[@]}"; do
        if [[ "$run_tbl" == eval_turns_* && "$run_tbl" =~ ^[A-Za-z0-9_]+$ ]]; then
          SCALE_CANDIDATES+=("$run_tbl")
        fi
      done
    fi
    for scale_tbl in "${SCALE_CANDIDATES[@]}"; do
      set +e
      check_table_owned_by_demo "${PROJECT_ID}:${DATASET_ID}.${scale_tbl}"
      own_rc=$?
      set -e
      if [[ "$own_rc" -eq 0 ]]; then
        echo "Deleting synthetic scale rows (scale_synth_*) from ${PROJECT_ID}.${DATASET_ID}.${scale_tbl}..."
        if ! bq query --project_id="$PROJECT_ID" --location="$BQ_LOCATION" --use_legacy_sql=false \
          "DELETE FROM \`${PROJECT_ID}.${DATASET_ID}.${scale_tbl}\` WHERE STARTS_WITH(session_id, 'scale_synth_')"; then
          FAILURES=$((FAILURES + 1))
        fi
      elif [[ "$own_rc" -eq 2 ]]; then
        echo "Table ${PROJECT_ID}:${DATASET_ID}.${scale_tbl} not found; skipping scale row cleanup."
      elif [[ "$own_rc" -eq 3 ]]; then
        if [[ "$scale_tbl" == "$EVAL_TURNS_TABLE" && "$EVAL_TURNS_TABLE_EXPLICIT" == "true" ]]; then
          echo "ERROR: Refusing to modify ${PROJECT_ID}:${DATASET_ID}.${scale_tbl}: missing demo ownership label bqaa_demo=combined_cuj_trace_search." >&2
          FAILURES=$((FAILURES + 1))
        else
          echo "  Preserved non-demo table ${scale_tbl}."
        fi
      else
        FAILURES=$((FAILURES + 1))
      fi
    done
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
      elif [[ "$own_rc" -eq 3 ]]; then
        if [[ "$tbl" == "$EVAL_TURNS_TABLE" && "$EVAL_TURNS_TABLE_EXPLICIT" != "true" ]]; then
          echo "  Preserved non-demo table ${tbl}."
        else
          echo "ERROR: Refusing to modify ${PROJECT_ID}:${DATASET_ID}.${tbl}: missing demo ownership label bqaa_demo=combined_cuj_trace_search." >&2
          FAILURES=$((FAILURES + 1))
        fi
      elif [[ "$own_rc" -ne 0 ]]; then
        FAILURES=$((FAILURES + 1))
      elif ! bq rm -f -t "${PROJECT_ID}:${DATASET_ID}.${tbl}"; then
        FAILURES=$((FAILURES + 1))
      fi
    done

    if [[ "${#RUN_SCOPED_TABLES[@]}" -gt 0 ]]; then
      for run_tbl in "${RUN_SCOPED_TABLES[@]}"; do
        if [[ ! "$run_tbl" =~ ^[A-Za-z0-9_]+$ ]]; then
          continue
        fi
        set +e
        check_table_owned_by_demo "${PROJECT_ID}:${DATASET_ID}.${run_tbl}"
        own_rc=$?
        set -e
        if [[ "$own_rc" -eq 0 ]]; then
          if ! bq rm -f -t "${PROJECT_ID}:${DATASET_ID}.${run_tbl}"; then
            FAILURES=$((FAILURES + 1))
          fi
        elif [[ "$own_rc" -eq 3 ]]; then
          echo "  Preserved non-demo table ${run_tbl}."
        elif [[ "$own_rc" -ne 2 ]]; then
          FAILURES=$((FAILURES + 1))
        fi
      done
    fi
    if [[ "$FAILURES" -eq 0 ]]; then
      echo "BigQuery demo tables dropped."
    fi
  fi

  if [[ "$FAILURES" -ne 0 ]]; then
    echo "ERROR: BigQuery cleanup encountered ${FAILURES} failure(s)." >&2
    exit 1
  fi
fi
