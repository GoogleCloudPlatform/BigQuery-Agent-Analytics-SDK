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

# Show the current agent prompt from Vertex AI Prompt Registry.
#
# Usage:
#   ./show_prompt.sh                    # Uses config.json defaults
#   ./show_prompt.sh <prompt_id>        # Specific prompt ID
#   ./show_prompt.sh --versions         # List all versions

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Get project and location
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
LOCATION=$(jq -r '.vertex_location // "us-central1"' "$SCRIPT_DIR/config.json" 2>/dev/null || echo "us-central1")

# Get prompt ID from argument or config.json
PROMPT_ID="${1:-}"
SHOW_VERSIONS=false

if [[ "$PROMPT_ID" == "--versions" ]]; then
  SHOW_VERSIONS=true
  PROMPT_ID=""
fi

if [[ -z "$PROMPT_ID" ]]; then
  PROMPT_ID=$(jq -r '.vertex_prompt_id // ""' "$SCRIPT_DIR/config.json" 2>/dev/null || echo "")
fi

if [[ -z "$PROMPT_ID" || "$PROMPT_ID" == "null" ]]; then
  PROMPT_ID="${VERTEX_PROMPT_ID:-}"
fi

if [[ -z "$PROMPT_ID" ]]; then
  echo "ERROR: No prompt ID. Pass as argument or set in config.json / .env" >&2
  exit 1
fi

TOKEN=$(gcloud auth print-access-token)
BASE_URL="https://${LOCATION}-aiplatform.googleapis.com/v1"
DATASET_URL="${BASE_URL}/projects/${PROJECT_ID}/locations/${LOCATION}/datasets/${PROMPT_ID}"

if [[ "$SHOW_VERSIONS" == "true" ]]; then
  echo "Versions for prompt ${PROMPT_ID}:"
  echo ""
  curl -s -H "Authorization: Bearer ${TOKEN}" \
    "${DATASET_URL}/datasetVersions" | \
    jq -r '.datasetVersions[]? | "  \(.displayName // .name)  created: \(.createTime)"'
  exit 0
fi

# Fetch the dataset and extract system instruction
RESPONSE=$(curl -s -H "Authorization: Bearer ${TOKEN}" "${DATASET_URL}")

# Extract prompt text via jq
PROMPT_TEXT=$(echo "$RESPONSE" | jq -r '.metadata.promptApiSchema.multimodalPrompt.promptMessage.systemInstruction.parts[0].text // empty')

if [[ -z "$PROMPT_TEXT" ]]; then
  echo "ERROR: Could not extract prompt text from response" >&2
  echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
  exit 1
fi

# Count versions
VERSION_COUNT=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
  "${DATASET_URL}/datasetVersions" | \
  jq '[.datasetVersions // [] | length] | add // 0')
VERSION=$((VERSION_COUNT + 1))

CHAR_COUNT=${#PROMPT_TEXT}

echo ""
echo "  Prompt ID:  ${PROMPT_ID}"
echo "  Version:    v${VERSION}"
echo "  Length:     ${CHAR_COUNT} chars"
echo "  Project:    ${PROJECT_ID}"
echo "  Location:   ${LOCATION}"
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "$PROMPT_TEXT"
echo "────────────────────────────────────────────────────────────────"
echo ""
