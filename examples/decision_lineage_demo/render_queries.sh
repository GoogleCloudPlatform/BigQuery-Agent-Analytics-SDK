#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

# Render every *.gql.tpl in this directory with .env values inlined.
# Currently produces:
#   bq_studio_queries.gql  (six demo blocks for BQ Studio)
#   property_graph.gql     (CREATE OR REPLACE PROPERTY GRAPH DDL —
#                           rebuilds the graph from the seven
#                           backing tables without rerunning the
#                           agent or AI.GENERATE)

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
: "${DATASET_ID:?missing in .env}"
# DEMO_SESSION_ID is only required for templates that reference
# __SESSION_ID__ (currently just bq_studio_queries.gql.tpl). Render
# the SDK-shape DDL template even when no session is recorded yet.
DEMO_SESSION_ID="${DEMO_SESSION_ID:-}"

render_one() {
  local tpl="$1"
  local out="$2"
  sed \
    -e "s|__PROJECT_ID__|${PROJECT_ID}|g" \
    -e "s|__DATASET_ID__|${DATASET_ID}|g" \
    -e "s|__SESSION_ID__|${DEMO_SESSION_ID}|g" \
    "$tpl" > "$out"
  echo "Rendered $out"
}

if [[ -f "$SCRIPT_DIR/bq_studio_queries.gql.tpl" ]]; then
  if [[ -z "$DEMO_SESSION_ID" ]]; then
    echo "WARNING: DEMO_SESSION_ID not set in .env — Block 2/3 in" \
         "the rendered queries will contain a literal empty session." >&2
  fi
  render_one \
    "$SCRIPT_DIR/bq_studio_queries.gql.tpl" \
    "$SCRIPT_DIR/bq_studio_queries.gql"
fi
if [[ -f "$SCRIPT_DIR/property_graph.gql.tpl" ]]; then
  render_one \
    "$SCRIPT_DIR/property_graph.gql.tpl" \
    "$SCRIPT_DIR/property_graph.gql"
fi
