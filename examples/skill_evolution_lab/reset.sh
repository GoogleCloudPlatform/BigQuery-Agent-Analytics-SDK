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
#
# Revert the demo to V0 -- both the local working copy AND the Skill Registry.
#
# Usage:
#   ./reset.sh                                   # local working copy only
#   WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./reset.sh   # also revert registry
#   DELETE_REGISTRY=1 SKILL_ID=... ./reset.sh    # delete the registry skill
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[ -f .env ] && source .env

# 1. Local: restore the flawed V0 working copy.
cp skills/SKILL.v0.md skills/SKILL.md
echo "Restored local skills/SKILL.md to V0."

# 2. Registry: revert the latest revision to V0, or delete the skill.
if [ "${DELETE_REGISTRY:-0}" = "1" ]; then
  : "${SKILL_ID:?set SKILL_ID}"
  GOOGLE_CLOUD_PROJECT="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}" \
    uv run python registry_cli.py delete --skill-id "$SKILL_ID" \
      --location "${REGISTRY_LOCATION:-${REGION:-us-central1}}"
elif [ "${WITH_REGISTRY:-0}" = "1" ]; then
  : "${SKILL_ID:?set SKILL_ID with WITH_REGISTRY=1}"
  echo "Reverting registry '$SKILL_ID' to V0 (new revision == V0 content)..."
  GOOGLE_CLOUD_PROJECT="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}" \
    uv run python registry_cli.py update --skill-id "$SKILL_ID" \
      --skill-dir skills --location "${REGISTRY_LOCATION:-${REGION:-us-central1}}"
fi

echo "Reset complete (system is back on V0)."
