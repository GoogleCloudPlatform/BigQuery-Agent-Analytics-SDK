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

# Drop the demo dataset, the rendered query bundle, and the .env so
# you can re-run setup from scratch. Destructive on the dataset.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
RENDERED_QUERIES="$SCRIPT_DIR/bq_studio_queries.gql"
RENDERED_DDL="$SCRIPT_DIR/property_graph.gql"
RENDERED_RICH_DDL="$SCRIPT_DIR/rich_property_graph.gql"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No .env found at $ENV_FILE — nothing to reset."
  exit 0
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PROJECT_ID="${PROJECT_ID:?missing in .env}"
DATASET_ID="${DATASET_ID:?missing in .env}"

echo "About to delete dataset: ${PROJECT_ID}:${DATASET_ID}"
echo "  (this removes all demo tables AND the property graph)"
read -r -p "Continue? [y/N] " confirm
confirm_lc=$(printf '%s' "$confirm" | tr '[:upper:]' '[:lower:]')
if [[ "$confirm_lc" != "y" && "$confirm_lc" != "yes" ]]; then
  echo "Aborted."
  exit 0
fi

bq rm -r -f --dataset "${PROJECT_ID}:${DATASET_ID}" || true
rm -f "$ENV_FILE" "$RENDERED_QUERIES" "$RENDERED_DDL" "$RENDERED_RICH_DDL"
rm -rf "$SCRIPT_DIR/.venv"
echo "Reset complete. Re-run ./setup.sh to seed and rebuild."
