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

# Tear down PR 1 demo state.
#
# Drops the caller and receiver datasets created by setup.sh. Leaves
# the .venv intact (delete it manually if you want a fresh install).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: No project. Export PROJECT_ID or 'gcloud config set project ...'" >&2
  exit 1
fi

CALLER_DATASET_ID="${CALLER_DATASET_ID:-a2a_caller_demo}"
RECEIVER_DATASET_ID="${RECEIVER_DATASET_ID:-a2a_receiver_demo}"

echo "Tearing down A2A Joint Lineage demo state in project $PROJECT_ID..."
for ds in "$CALLER_DATASET_ID" "$RECEIVER_DATASET_ID"; do
  if bq show "${PROJECT_ID}:${ds}" &>/dev/null 2>&1; then
    echo "  Removing dataset ${ds}..."
    bq rm -r -f --dataset "${PROJECT_ID}:${ds}" 2>/dev/null || true
  else
    echo "  (skip) dataset ${ds} does not exist"
  fi
done

echo "Done. .venv preserved; remove with: rm -rf $SCRIPT_DIR/.venv"
