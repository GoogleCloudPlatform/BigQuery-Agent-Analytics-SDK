#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

# Render bq_studio_queries.gql.tpl with .env values inlined.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
TPL="$SCRIPT_DIR/bq_studio_queries.gql.tpl"
OUT="$SCRIPT_DIR/bq_studio_queries.gql"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run ./setup.sh first." >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${PROJECT_ID:?missing in .env}"
: "${DATASET_ID:?missing in .env}"
: "${DEMO_SESSION_ID:?missing in .env}"

sed \
  -e "s|__PROJECT_ID__|${PROJECT_ID}|g" \
  -e "s|__DATASET_ID__|${DATASET_ID}|g" \
  -e "s|__SESSION_ID__|${DEMO_SESSION_ID}|g" \
  "$TPL" > "$OUT"

echo "Rendered $OUT"
