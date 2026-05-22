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
# Stage the periodic-materialization container source + publish to
# Artifact Registry.
#
# This is the image-build counterpart to ``deploy_cloud_run_job.sh``.
# The bash deploy does build + IAM + Cloud Run Job + Scheduler all
# inline. The Terraform module (see ``terraform/``) does only the
# IAM + Cloud Run Job + Scheduler half — it takes the published
# image URI as input. This helper closes the gap by producing the
# image the Terraform module needs.
#
# The staging layout mirrors what ``deploy_cloud_run_job.sh``
# section 3 assembles (Procfile + run_job.py + reference_extractor +
# demo artifacts + vendored SDK source). Keeping the layout
# byte-identical to the bash deploy's staging dir means a customer
# can build with this script + deploy with Terraform and get a
# container behaviorally identical to the bash deploy's
# ``gcloud run jobs deploy --source`` output.

set -euo pipefail

PROJECT=""
REPO=""
REGION="us-central1"
IMAGE="periodic-materialization"
TAG=""

usage() {
  cat <<EOF
Usage: $0 --project PROJECT_ID --repo AR_REPO [options]

Required:
  --project PROJECT_ID   GCP project (used for both gcloud builds AND
                         Artifact Registry).
  --repo AR_REPO         Artifact Registry repository name (will be
                         created with --create-repo if absent).

Optional:
  --region REGION        Artifact Registry region (default: us-central1).
  --image IMAGE_NAME     Image name within the AR repo (default:
                         periodic-materialization).
  --tag TAG              Image tag (default: short git SHA of HEAD, or
                         a timestamp if HEAD isn't a git ref).
  --create-repo          Create the AR repo if it doesn't exist.
  -h | --help            Show this help.

Outputs (last line of stdout):
  REGION-docker.pkg.dev/PROJECT/REPO/IMAGE:TAG

Pipe the last line into ``terraform apply -var image_uri=\$(...)``.
EOF
  exit "${1:-1}"
}

CREATE_REPO=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)      PROJECT="$2"; shift 2 ;;
    --repo)         REPO="$2"; shift 2 ;;
    --region)       REGION="$2"; shift 2 ;;
    --image)        IMAGE="$2"; shift 2 ;;
    --tag)          TAG="$2"; shift 2 ;;
    --create-repo)  CREATE_REPO=true; shift ;;
    -h|--help)      usage 0 ;;
    *)              echo "Unknown arg: $1" >&2; usage 1 ;;
  esac
done

if [[ -z "$PROJECT" || -z "$REPO" ]]; then
  echo "Error: --project and --repo are required." >&2
  usage 1
fi

if [[ -z "$TAG" ]]; then
  # Default tag: short git SHA if HEAD is reachable, timestamp
  # otherwise. Either keeps tags monotonically increasing so a
  # later ``terraform apply`` against a fresh tag forces a Cloud
  # Run revision rollover.
  if TAG="$(git rev-parse --short HEAD 2>/dev/null)"; then
    :
  else
    TAG="t$(date +%Y%m%d%H%M%S)"
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${ARTIFACTS_DIR}/../.." && pwd)"

STAGING="$(mktemp -d -t bqaa-build-XXXXXXXX)"
trap 'rm -rf "$STAGING"' EXIT

echo "==> staging build context at $STAGING" >&2

# Mirror ``deploy_cloud_run_job.sh`` section 3 exactly. The
# layout here is what the Cloud Run runtime expects:
#
#   STAGING/
#     run_job.py          ← entrypoint (Procfile points here)
#     reference_extractor.py
#     ontology.yaml
#     binding.yaml
#     table_ddl.sql
#     requirements.txt    ← ``./sdk_src`` + ancillary deps
#     Procfile            ← ``web: python run_job.py``
#     sdk_src/
#       pyproject.toml
#       README.md         ← stub (hatch reads ``readme`` field)
#       src/
#         bigquery_agent_analytics/
#         bigquery_ontology/
#
# Any drift from the bash deploy's staging output here will
# show up as a behavioral difference between bash-built and
# Terraform-built images. The covering test in
# ``tests/test_materialize_window.py::TestBuildImageStaging``
# pins these copies to lock the contract.

cp "${SCRIPT_DIR}/run_job.py" "$STAGING/"
cp "${ARTIFACTS_DIR}/ontology.yaml" "$STAGING/"
cp "${ARTIFACTS_DIR}/binding.yaml" "$STAGING/"
cp "${ARTIFACTS_DIR}/table_ddl.sql" "$STAGING/"
cp "${ARTIFACTS_DIR}/reference_extractor.py" "$STAGING/"

mkdir -p "$STAGING/sdk_src/src"
cp -r "$REPO_ROOT/src/bigquery_agent_analytics" "$STAGING/sdk_src/src/"
cp -r "$REPO_ROOT/src/bigquery_ontology" "$STAGING/sdk_src/src/"
cp "$REPO_ROOT/pyproject.toml" "$STAGING/sdk_src/"
echo "# bigquery-agent-analytics (vendored for periodic-materialization deploy)" \
  > "$STAGING/sdk_src/README.md"

cat > "$STAGING/requirements.txt" <<EOF
# Auto-generated by build_image.sh. Installs the SDK from the
# vendored source bundled into the build context, so the
# deployed image uses the same SDK code as the local dry-run.
./sdk_src
google-cloud-bigquery>=3.0.0
pyyaml>=6.0
EOF

cat > "$STAGING/Procfile" <<EOF
web: python run_job.py
EOF

# Make sure the target AR repo exists (idempotent).
if [[ "$CREATE_REPO" == "true" ]]; then
  if ! gcloud artifacts repositories describe "$REPO" \
      --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
    echo "==> creating Artifact Registry repo: $REPO in $REGION" >&2
    gcloud artifacts repositories create "$REPO" \
      --repository-format=docker \
      --location="$REGION" \
      --project="$PROJECT" \
      --description="BQAA periodic-materialization images" \
      --quiet
  fi
fi

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${IMAGE}:${TAG}"
echo "==> building + pushing $IMAGE_URI" >&2
# ``--pack image=URI`` selects Buildpacks (the same builder the
# bash deploy's ``gcloud run jobs deploy --source`` flow uses).
# Plain ``--tag`` would require a Dockerfile, which the staging
# layout intentionally omits — the Procfile + ``requirements.txt``
# are enough for Buildpacks' Python builder to produce a runnable
# image.
gcloud builds submit \
  --project "$PROJECT" \
  --pack "image=${IMAGE_URI}" \
  --quiet \
  "$STAGING" >&2

# The image URI is the LAST line of stdout — wrappers can do
# ``IMAGE_URI=$(./build_image.sh ...)`` to capture it.
echo "$IMAGE_URI"
