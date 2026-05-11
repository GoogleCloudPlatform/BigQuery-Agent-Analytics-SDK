#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Render every *.gql.tpl in this directory with .env values inlined.
# Currently produces:
#   joint_property_graph.gql   (CREATE OR REPLACE PROPERTY GRAPH for
#                               the auditor's joint graph)
#   bq_studio_queries.gql      (five paste-and-run BQ Studio blocks)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run ./setup.sh first." >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${PROJECT_ID:?missing in .env}"
: "${AUDITOR_DATASET_ID:?missing in .env (PR 2 added this var; re-run setup.sh)}"
# DEMO_CALLER_SESSION_ID is only required for Block 4 of
# bq_studio_queries.gql.tpl. The other blocks render fine without it.
DEMO_CALLER_SESSION_ID="${DEMO_CALLER_SESSION_ID:-}"

render_one() {
  local tpl="$1"
  local out="$2"
  sed \
    -e "s|__PROJECT_ID__|${PROJECT_ID}|g" \
    -e "s|__AUDITOR_DATASET_ID__|${AUDITOR_DATASET_ID}|g" \
    -e "s|__DEMO_CALLER_SESSION_ID__|${DEMO_CALLER_SESSION_ID}|g" \
    "$tpl" > "$out"
  echo "Rendered $out"
}

if [[ -f "$SCRIPT_DIR/joint_property_graph.gql.tpl" ]]; then
  render_one \
    "$SCRIPT_DIR/joint_property_graph.gql.tpl" \
    "$SCRIPT_DIR/joint_property_graph.gql"
fi
if [[ -f "$SCRIPT_DIR/bq_studio_queries.gql.tpl" ]]; then
  if [[ -z "$DEMO_CALLER_SESSION_ID" ]]; then
    echo "WARNING: DEMO_CALLER_SESSION_ID not set in .env — Block 4" \
         "in bq_studio_queries.gql will contain a literal empty" \
         "session id. run_caller_agent.py records this on success." >&2
  fi
  render_one \
    "$SCRIPT_DIR/bq_studio_queries.gql.tpl" \
    "$SCRIPT_DIR/bq_studio_queries.gql"
fi
