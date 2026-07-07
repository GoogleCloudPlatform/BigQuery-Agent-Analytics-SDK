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
# Setup for the skill-evolution lab:
#   1. Resolve the GCP project + region and write .env.
#   2. Enable the required API (aiplatform.googleapis.com -- Vertex AI).
#   3. Reset the working skill to V0.
#   4. (Optional) register the V0 skill in the Skill Registry.
#
# Usage:
#   ./setup.sh [PROJECT_ID] [REGION]
#   WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./setup.sh   # also create V0 skill
#
# Required IAM: roles/aiplatform.user (Gemini + Skill Registry), plus rights to
# enable services (roles/serviceusage.serviceUsageAdmin) on first run.
# Authenticate with: gcloud auth application-default login
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PROJECT_ID="${1:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}}"
REGION="${2:-${REGION:-us-central1}}"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: no project. Pass it: ./setup.sh my-project [region]" >&2
  exit 1
fi

if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "ERROR: no ADC. Run: gcloud auth application-default login" >&2
  exit 1
fi

cat > .env <<EOF
PROJECT_ID="$PROJECT_ID"
GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
REGION="$REGION"
GOOGLE_GENAI_USE_VERTEXAI=True
# Models (override before run_e2e_demo.sh if desired):
AGENT_MODEL="gemini-3.1-flash-lite"
ANALYST_MODEL="gemini-3.1-pro-preview"
JUDGE_MODEL="gemini-2.5-flash"
JUDGE_LOCATION="$REGION"
EOF
echo "Wrote .env (project=$PROJECT_ID region=$REGION)."

# Enable the one API this example needs: Vertex AI. It serves Gemini
# generateContent (agent, analysts, judge), the text-embedding model used for
# golden-Q&A matching, AND the Skill Registry (hosted at
# {region}-aiplatform.googleapis.com). No BigQuery is required -- scoring runs
# offline via --conversations-file.
echo "Enabling required GCP API: aiplatform.googleapis.com ..."
if gcloud services enable aiplatform.googleapis.com --project "$PROJECT_ID"; then
  echo "  aiplatform.googleapis.com enabled."
else
  echo "  WARN: could not enable aiplatform.googleapis.com automatically." >&2
  echo "        Enable 'Vertex AI API' on $PROJECT_ID before running the demo." >&2
fi

# Ensure the working copy starts at the flawed V0.
cp skills/SKILL.v0.md skills/SKILL.md
echo "Reset working copy skills/SKILL.md to V0."

if [ "${WITH_REGISTRY:-0}" = "1" ]; then
  : "${SKILL_ID:?set SKILL_ID with WITH_REGISTRY=1}"
  echo "Creating V0 skill '$SKILL_ID' in the Skill Registry ($REGION)..."
  GOOGLE_CLOUD_PROJECT="$PROJECT_ID" uv run python registry_cli.py create \
    --skill-id "$SKILL_ID" --skill-dir skills --location "$REGION"
fi

echo ""
echo "Setup complete. Run the demo with:  ./run_e2e_demo.sh"
