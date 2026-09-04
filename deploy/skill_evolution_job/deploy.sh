#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Deploy the skill-evolution Cloud Run Job + the Cloud Scheduler
# trigger that fires it weekly.
#
# Usage:
#
#   ./deploy.sh \
#     --project PROJECT_ID \
#     --region REGION \
#     --dataset EVENTS_DATASET \
#     [--dataset-location US] \
#     [--schedule "0 9 * * 1"] \
#     [--job-name bqaa-skill-evolution] \
#     [--github-repo OWNER/REPO] \
#     [--agent-registry PATH_IN_REPO] \
#     [--gh-secret SECRET_NAME] \
#     [--gcs-bucket BUCKET] \
#     [--base-branch main] \
#     [--task-timeout 14400] \
#     [--single-sa] [--smoke] [--down]
#
# What this script does (in order):
#
# 1. Enables the required APIs (Cloud Run, Cloud Build, Artifact
#    Registry, Cloud Scheduler, Secret Manager) and ensures an
#    Artifact Registry Docker repo for the job image.
#
# 2. Creates the runtime + scheduler-caller service accounts if
#    absent. Default: two SAs — ``bqaa-skill-evo-runtime-sa``
#    (holds the BigQuery / Vertex AI / Secret Manager / GCS roles
#    below) and ``bqaa-skill-evo-scheduler-sa`` (holds only
#    ``roles/run.invoker`` on the job). Under ``--single-sa``: a
#    single ``bqaa-skill-evo-sa`` serves both paths. The runtime
#    SA is granted:
#      * project-level ``roles/bigquery.jobUser`` (jobs.create —
#        the quality report runs BigQuery queries).
#      * dataset-level ``roles/bigquery.dataViewer`` on the events
#        dataset (read-only — the job never writes events).
#      * project-level ``roles/aiplatform.user`` (the evolution
#        agent + LLM judge call Gemini via Vertex AI).
#      * secret-level ``roles/secretmanager.secretAccessor`` on
#        the ``--gh-secret`` secret (GitHub token for cloning the
#        host repo + opening evolution PRs). Skipped when
#        ``--gh-secret`` is not passed (GCS-only / dry-run mode).
#      * bucket-level ``roles/storage.objectAdmin`` on the
#        ``--gcs-bucket`` runs bucket (run-artifact uploads).
#        Skipped when ``--gcs-bucket`` is not passed.
#
# 3. Builds a self-contained staging dir (this component + the
#    SDK's ``scripts/skill_evolution.py`` + ``scripts/
#    quality_report.py`` + ``scripts/eval/eval_config.json``) and
#    submits it to Cloud Build with the bundled Dockerfile.
#
# 4. Deploys the Cloud Run Job from the built image with
#    ``--service-account`` pointing at the runtime SA,
#    ``--set-secrets GH_TOKEN=<secret>:latest`` (when
#    ``--gh-secret`` is passed), ``--task-timeout`` 14400s by
#    default (a full report → evolve → PR loop is LLM-bound; the
#    proven upper bound for large skill sets is 28800 — raise via
#    ``--task-timeout``), ``--memory 2Gi``, and ``--max-retries 0``
#    (an evolution run opens branches and PRs — retrying a
#    half-finished run could duplicate them; failed runs are
#    re-attempted by the next scheduled fire instead).
#
# 5. Grants the scheduler-caller SA ``roles/run.invoker`` on the
#    job and creates / updates the Cloud Scheduler trigger
#    pointing at the Cloud Run Jobs ``:run`` endpoint. Default
#    cadence ``0 9 * * 1`` (Mondays 09:00 — weekly evolution).
#
# 6. If ``--smoke`` is passed, executes the job once with
#    ``--args=--test`` (the component's self-test: engine
#    located, registry parsed, tools registered, hooks resolved)
#    and greps the execution logs for the ``SELF-TEST PASS``
#    sentinel — so "did it deploy correctly?" is one command away.
#
# 7. ``--down`` tears down the scheduler trigger + the Cloud Run
#    Job (service accounts, IAM grants, the secret, the bucket and
#    the built images are retained — they are cheap, idempotent to
#    re-create, and may be shared).

set -euo pipefail

# ----------------------------------------------------------- #
# Arg parsing                                                  #
# ----------------------------------------------------------- #

PROJECT=""
REGION=""
DATASET=""
TABLE="agent_events"
DATASET_LOCATION="US"
SCHEDULE="0 9 * * 1"
JOB_NAME="bqaa-skill-evolution"
GITHUB_REPO=""
AGENT_REGISTRY=""
GH_SECRET=""
GCS_BUCKET=""
BASE_BRANCH="main"
TASK_TIMEOUT="14400"
EXTRA_REQUIREMENTS=""
SCRIPTS_DIR=""
SINGLE_SA=false
SMOKE=false
DOWN=false

# Print usage. ``usage 0`` for ``--help`` (success), ``usage 1``
# for parse / required-arg errors — so ``--help`` doesn't look
# like a failure to CI wrappers that pivot on exit codes.
usage() {
  cat <<EOF
Usage: $0 [options]

Required:
  --project PROJECT_ID       GCP project.
  --region REGION            Cloud Run region (e.g. us-central1).
  --dataset DATASET          BigQuery dataset with agent_events
                             (the SDK's event table).

Optional:
  --table TABLE              Events table in --dataset consumed by
                             scripts/quality_report.py
                             (default: agent_events).
  --dataset-location LOC     BigQuery location (default: US).
  --schedule "CRON"          Cloud Scheduler cron
                             (default: "0 9 * * 1" — Mondays 09:00).
  --job-name NAME            Cloud Run Job name
                             (default: bqaa-skill-evolution).
  --github-repo OWNER/REPO   Host agent repo the job clones, evolves
                             skills in, and opens PRs against.
                             Omit for GCS-only / dry-run mode (the
                             job produces reports + artifacts but
                             no PRs).
  --agent-registry PATH      agent_registry.json path. Relative
                             paths resolve inside the --github-repo
                             clone. Required for evolution runs;
                             without it the job stops after the
                             quality report.
  --gh-secret NAME           Existing Secret Manager secret holding
                             a GitHub token with repo scope on
                             --github-repo. Create one with:
                               printf '%s' "\$TOKEN" | \\
                                 gcloud secrets create NAME --data-file=-
                             Wired to the job as GH_TOKEN.
  --gcs-bucket BUCKET        GCS bucket for run artifacts (created
                             if absent). Wired as EVOLUTION_GCS_BUCKET.
  --base-branch NAME         Base branch for evolution PRs
                             (default: main).
  --task-timeout SECONDS     Cloud Run task timeout (default: 14400).
  --extra-requirements FILE  Extra pip requirements appended to the image
                             (dependencies your EVOLUTION_HOOKS adapter needs).
  --scripts-dir DIR          Bake the engine + report scripts from this
                             directory instead of this checkout's
                             scripts/ (e.g. another SDK branch whose
                             skill_evolution.py supports agentic error
                             analysts and the incumbent guard). Must
                             contain skill_evolution.py,
                             quality_report.py, eval/eval_config.json.
  --single-sa               Use one combined service account for
                             both the job runtime and the scheduler
                             caller. Default: two SAs (least
                             privilege — the scheduler caller only
                             holds roles/run.invoker on the job).
  --smoke                    After deploy, execute the job once with
                             --test and require the SELF-TEST PASS
                             sentinel in its logs.
  --down                     Delete the scheduler trigger + the
                             Cloud Run Job, then exit. Needs
                             --project and --region (and --job-name
                             if not the default).
  -h | --help                Show this help.
EOF
  exit "${1:-1}"
}

# With ``set -u``, a bare ``$2`` reference raises "unbound
# variable" when a flag that needs a value trails the arg list.
# ``require_arg`` reads it defensively and fails with a clean
# usage error instead.
require_arg() {
  local flag="$1"
  local value="${2-}"
  if [[ -z "$value" || "$value" == -* ]]; then
    echo "Error: $flag requires a value." >&2
    usage 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)          require_arg "$1" "${2-}"; PROJECT="$2"; shift 2 ;;
    --region)           require_arg "$1" "${2-}"; REGION="$2"; shift 2 ;;
    --dataset)          require_arg "$1" "${2-}"; DATASET="$2"; shift 2 ;;
    --table)            require_arg "$1" "${2-}"; TABLE="$2"; shift 2 ;;
    --dataset-location) require_arg "$1" "${2-}"; DATASET_LOCATION="$2"; shift 2 ;;
    --schedule)         require_arg "$1" "${2-}"; SCHEDULE="$2"; shift 2 ;;
    --job-name)         require_arg "$1" "${2-}"; JOB_NAME="$2"; shift 2 ;;
    --github-repo)      require_arg "$1" "${2-}"; GITHUB_REPO="$2"; shift 2 ;;
    --agent-registry)   require_arg "$1" "${2-}"; AGENT_REGISTRY="$2"; shift 2 ;;
    --gh-secret)        require_arg "$1" "${2-}"; GH_SECRET="$2"; shift 2 ;;
    --gcs-bucket)       require_arg "$1" "${2-}"; GCS_BUCKET="$2"; shift 2 ;;
    --base-branch)      require_arg "$1" "${2-}"; BASE_BRANCH="$2"; shift 2 ;;
    --task-timeout)     require_arg "$1" "${2-}"; TASK_TIMEOUT="$2"; shift 2 ;;
    --extra-requirements) require_arg "$1" "${2-}"; EXTRA_REQUIREMENTS="$2"; shift 2 ;;
    --scripts-dir)      require_arg "$1" "${2-}"; SCRIPTS_DIR="$2"; shift 2 ;;
    --single-sa)        SINGLE_SA=true; shift ;;
    --smoke)            SMOKE=true; shift ;;
    --down)             DOWN=true; shift ;;
    -h|--help)          usage 0 ;;
    *)                  echo "Unknown argument: $1" >&2; usage 1 ;;
  esac
done

SCHEDULER_NAME="${JOB_NAME}-cron"

# ----------------------------------------------------------- #
# --down: teardown and exit                                    #
# ----------------------------------------------------------- #

if [[ "$DOWN" == true ]]; then
  for var in PROJECT REGION; do
    if [[ -z "${!var}" ]]; then
      flag=$(printf '%s' "$var" | tr '[:upper:]_' '[:lower:]-')
      echo "Error: --$flag is required for --down (use --help)." >&2
      exit 1
    fi
  done
  echo "==> deleting Cloud Scheduler job: $SCHEDULER_NAME"
  gcloud scheduler jobs delete "$SCHEDULER_NAME" \
    --project "$PROJECT" --location "$REGION" --quiet 2>/dev/null \
    || echo "    (not found — skipping)"
  echo "==> deleting Cloud Run Job: $JOB_NAME"
  gcloud run jobs delete "$JOB_NAME" \
    --project "$PROJECT" --region "$REGION" --quiet 2>/dev/null \
    || echo "    (not found — skipping)"
  echo
  echo "Torn down. Retained (idempotent to re-create, may be shared):"
  echo "  service accounts + IAM grants, the --gh-secret secret,"
  echo "  the --gcs-bucket bucket, and built images in Artifact Registry."
  exit 0
fi

# ----------------------------------------------------------- #
# Validation                                                   #
# ----------------------------------------------------------- #

for var in PROJECT REGION DATASET; do
  if [[ -z "${!var}" ]]; then
    flag=$(printf '%s' "$var" | tr '[:upper:]_' '[:lower:]-')
    echo "Error: --$flag is required (use --help)." >&2
    exit 1
  fi
done

if ! [[ "$TASK_TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "Error: --task-timeout must be a positive integer (seconds); got '$TASK_TIMEOUT'." >&2
  exit 1
fi

if [[ -n "$EXTRA_REQUIREMENTS" && ! -f "$EXTRA_REQUIREMENTS" ]]; then
  echo "Error: --extra-requirements file not found: $EXTRA_REQUIREMENTS" >&2
  exit 1
fi

# A GitHub repo without a token can only be cloned if public, and
# PR creation always needs the token. Warn early rather than after
# a 5-minute build.
if [[ -n "$GITHUB_REPO" && -z "$GH_SECRET" ]]; then
  echo "Warning: --github-repo is set but --gh-secret is not. The job" >&2
  echo "  can clone a PUBLIC repo anonymously but cannot push branches" >&2
  echo "  or open evolution PRs without GH_TOKEN. Pass --gh-secret for" >&2
  echo "  the full PR loop." >&2
fi
if [[ -n "$GITHUB_REPO" && -z "$AGENT_REGISTRY" ]]; then
  echo "Warning: --github-repo is set but --agent-registry is not. The" >&2
  echo "  job needs the registry to locate skills; without it, runs stop" >&2
  echo "  after the quality report." >&2
fi

# The gh secret must already exist — this script can't invent the
# token value, and failing here beats failing inside the container.
if [[ -n "$GH_SECRET" ]]; then
  if ! gcloud secrets describe "$GH_SECRET" \
      --project "$PROJECT" >/dev/null 2>&1; then
    echo "Error: Secret Manager secret '$GH_SECRET' not found in project '$PROJECT'." >&2
    echo "Create it first:" >&2
    echo "  printf '%s' \"\$YOUR_GITHUB_TOKEN\" | gcloud secrets create $GH_SECRET --data-file=- --project $PROJECT" >&2
    exit 1
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Repo root — this component lives under ``deploy/``, so the SDK
# repo root (where ``scripts/`` lives) is two dirs up.
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Engine + report scripts baked into the image. Default: this
# checkout's scripts/. --scripts-dir points at another SDK checkout's
# scripts/ (a branch whose engine has capabilities this one lacks, e.g.
# agentic error analysts / incumbent guard) so the image is
# reproducible from flags instead of from files copied over by hand.
SCRIPTS_SRC="${SCRIPTS_DIR:-${REPO_ROOT}/scripts}"
SCRIPTS_SRC="$(cd "$SCRIPTS_SRC" 2>/dev/null && pwd)" || {
  if [[ -n "$SCRIPTS_DIR" ]]; then
    echo "Error: --scripts-dir is not a directory: ${SCRIPTS_DIR}" >&2
  else
    echo "Error: ${REPO_ROOT}/scripts not found — run this script from a full SDK checkout, or point --scripts-dir at one." >&2
  fi
  exit 1
}
for staged in skill_evolution.py quality_report.py eval/eval_config.json; do
  if [[ ! -f "${SCRIPTS_SRC}/${staged}" ]]; then
    echo "Error: expected ${staged} in ${SCRIPTS_SRC} — run this script from a full SDK checkout, or point --scripts-dir at one." >&2
    exit 1
  fi
done
if [[ -n "$SCRIPTS_DIR" ]]; then
  echo "==> engine + report scripts from --scripts-dir: ${SCRIPTS_SRC}"
fi

STAGING=""
_cleanup() {
  [[ -n "$STAGING" ]] && rm -rf "$STAGING"
}
trap _cleanup EXIT

# ----------------------------------------------------------- #
# 1. APIs + Artifact Registry repo                             #
# ----------------------------------------------------------- #

echo "==> enabling required APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project "$PROJECT" --quiet

AR_REPO="bqaa-jobs"
if ! gcloud artifacts repositories describe "$AR_REPO" \
    --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
  echo "==> creating Artifact Registry repo: $AR_REPO"
  gcloud artifacts repositories create "$AR_REPO" \
    --project "$PROJECT" \
    --location "$REGION" \
    --repository-format docker \
    --description "BigQuery Agent Analytics Cloud Run Job images"
else
  echo "==> Artifact Registry repo exists: $AR_REPO"
fi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${JOB_NAME}"

# ----------------------------------------------------------- #
# 2. Service accounts + IAM                                    #
# ----------------------------------------------------------- #
#
# Production-posture default: two SAs. The runtime SA does the
# BigQuery / Vertex / GitHub work; the scheduler-caller SA can
# fire the job and nothing else. ``--single-sa`` collapses them
# for operators who explicitly want the simpler combined identity.

if [[ "$SINGLE_SA" == "true" ]]; then
  RUNTIME_SA_NAME="bqaa-skill-evo-sa"
  SCHEDULER_SA_NAME="bqaa-skill-evo-sa"
  RUNTIME_SA_DISPLAY="BQAA skill-evolution runtime + scheduler"
  SCHEDULER_SA_DISPLAY="$RUNTIME_SA_DISPLAY"
else
  RUNTIME_SA_NAME="bqaa-skill-evo-runtime-sa"
  SCHEDULER_SA_NAME="bqaa-skill-evo-scheduler-sa"
  RUNTIME_SA_DISPLAY="BQAA skill-evolution runtime"
  SCHEDULER_SA_DISPLAY="BQAA skill-evolution scheduler caller"
fi
RUNTIME_SA_EMAIL="${RUNTIME_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHEDULER_SA_EMAIL="${SCHEDULER_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# Retry an IAM-binding command on the IAM-propagation race:
# ``service-accounts create`` returns once the SA exists in one
# replica, but the subsequent grant can read a lagging replica and
# fail with "Service account ... does not exist". Retrying the
# grant itself is the reliable fix (polling ``describe`` hits the
# replica that already said yes).
_retry_iam() {
  local attempts=0
  local max=20
  while [[ $attempts -lt $max ]]; do
    if "$@" >/dev/null 2>/tmp/_iam_err.$$; then
      rm -f /tmp/_iam_err.$$
      return 0
    fi
    if ! grep -qE "(does not exist|Service account)" /tmp/_iam_err.$$; then
      cat /tmp/_iam_err.$$ >&2
      rm -f /tmp/_iam_err.$$
      return 1
    fi
    sleep 3
    attempts=$((attempts + 1))
  done
  echo "Error: IAM grant did not succeed after ${max} retries" >&2
  cat /tmp/_iam_err.$$ >&2
  rm -f /tmp/_iam_err.$$
  return 1
}

_ensure_sa() {
  local sa_name="$1"
  local sa_email="$2"
  local display="$3"
  if ! gcloud iam service-accounts describe "$sa_email" \
      --project "$PROJECT" >/dev/null 2>&1; then
    echo "==> creating service account: $sa_email"
    gcloud iam service-accounts create "$sa_name" \
      --display-name "$display" \
      --project "$PROJECT"
  else
    echo "==> service account exists: $sa_email"
  fi
}
_ensure_sa "$RUNTIME_SA_NAME" "$RUNTIME_SA_EMAIL" "$RUNTIME_SA_DISPLAY"
if [[ "$SINGLE_SA" != "true" ]]; then
  _ensure_sa "$SCHEDULER_SA_NAME" "$SCHEDULER_SA_EMAIL" "$SCHEDULER_SA_DISPLAY"
fi

echo "==> granting project-level roles/bigquery.jobUser to $RUNTIME_SA_EMAIL"
_retry_iam gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role roles/bigquery.jobUser \
  --condition=None \
  --quiet

# The evolution agent, the quality report's LLM judge and the
# trajectory sampler all call Gemini through Vertex AI
# (GOOGLE_GENAI_USE_VERTEXAI=True below).
echo "==> granting project-level roles/aiplatform.user to $RUNTIME_SA_EMAIL"
_retry_iam gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role roles/aiplatform.user \
  --condition=None \
  --quiet

# Dataset-level IAM via the BigQuery Python client (the
# ``AccessEntry`` update API — ``bq add-iam-policy-binding`` is
# gated on project allowlisting in some environments). Reuses the
# operator's python3 when it already has google-cloud-bigquery,
# else a one-shot temp venv — the operator baseline stays
# ``gcloud`` + ``python3``.
PY_CMD="python3"
IAM_VENV=""
if ! python3 -c "import google.cloud.bigquery" >/dev/null 2>&1; then
  echo "==> creating temp venv with google-cloud-bigquery (for dataset IAM)"
  IAM_VENV="$(mktemp -d -t bqaa-iam-venv-XXXXXXXX)"
  python3 -m venv "$IAM_VENV" >/dev/null
  "$IAM_VENV/bin/pip" install --quiet google-cloud-bigquery
  PY_CMD="$IAM_VENV/bin/python"
  _cleanup() {
    [[ -n "$STAGING" ]] && rm -rf "$STAGING"
    [[ -n "$IAM_VENV" ]] && rm -rf "$IAM_VENV"
  }
  trap _cleanup EXIT
fi

# READER ≡ roles/bigquery.dataViewer — the job only reads events.
echo "==> granting dataset-level roles/bigquery.dataViewer on ${DATASET} to $RUNTIME_SA_EMAIL"
"$PY_CMD" - <<EOF || { echo "Error: dataset-level IAM grant failed." >&2; exit 1; }
import sys
from google.cloud import bigquery
client = bigquery.Client(project="${PROJECT}")
ds = client.get_dataset("${PROJECT}.${DATASET}")
sa = "${RUNTIME_SA_EMAIL}"
existing = [
    e for e in ds.access_entries
    if e.entity_type == "userByEmail"
    and e.entity_id == sa
    and e.role == "READER"
]
if existing:
    print("  already granted (READER)")
    sys.exit(0)
entries = list(ds.access_entries) + [
    bigquery.AccessEntry(
        role="READER", entity_type="userByEmail", entity_id=sa
    )
]
ds.access_entries = entries
client.update_dataset(ds, ["access_entries"])
print("  granted (READER)")
EOF

# Secret-level accessor grant — scoped to the one secret, not
# project-wide.
if [[ -n "$GH_SECRET" ]]; then
  echo "==> granting roles/secretmanager.secretAccessor on $GH_SECRET to $RUNTIME_SA_EMAIL"
  _retry_iam gcloud secrets add-iam-policy-binding "$GH_SECRET" \
    --project "$PROJECT" \
    --member "serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role roles/secretmanager.secretAccessor \
    --quiet
fi

# Runs bucket: created if absent, bucket-level objectAdmin (the
# job uploads run artifacts + downloads prior reports).
if [[ -n "$GCS_BUCKET" ]]; then
  if ! gcloud storage buckets describe "gs://${GCS_BUCKET}" \
      --project "$PROJECT" >/dev/null 2>&1; then
    echo "==> creating GCS bucket: gs://${GCS_BUCKET}"
    gcloud storage buckets create "gs://${GCS_BUCKET}" \
      --project "$PROJECT" \
      --location "$REGION" \
      --uniform-bucket-level-access
  else
    echo "==> GCS bucket exists: gs://${GCS_BUCKET}"
  fi
  echo "==> granting roles/storage.objectAdmin on gs://${GCS_BUCKET} to $RUNTIME_SA_EMAIL"
  _retry_iam gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
    --member "serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role roles/storage.objectAdmin
fi

# ----------------------------------------------------------- #
# 3. Staging dir + Cloud Build                                 #
# ----------------------------------------------------------- #
#
# The Dockerfile expects the component at the staging root and
# the SDK's engine + report scripts under ``scripts/`` (baked to
# ``/app/scripts``, where SDK_SCRIPTS_DIR points).

STAGING="$(mktemp -d -t bqaa-skill-evo-XXXXXXXX)"
echo "==> staging at $STAGING"

cp "${SCRIPT_DIR}/Dockerfile" "$STAGING/"
cp "${SCRIPT_DIR}/requirements.txt" "$STAGING/"
if [[ -n "$EXTRA_REQUIREMENTS" ]]; then
  # Host hook adapters (EVOLUTION_HOOKS) import host-repo modules that
  # may need packages beyond the job's own; append them to the image's
  # requirements so the adapter can import inside the container.
  echo "==> appending host requirements from $EXTRA_REQUIREMENTS"
  { echo ""; echo "# --extra-requirements (host hook dependencies)"; \
    cat "$EXTRA_REQUIREMENTS"; } >> "$STAGING/requirements.txt"
fi
cp "${SCRIPT_DIR}/main.py" "$STAGING/"
cp -r "${SCRIPT_DIR}/skill_evolution_job" "$STAGING/"
find "$STAGING" -type d -name __pycache__ -exec rm -rf {} +

mkdir -p "$STAGING/scripts/eval"
cp "${SCRIPTS_SRC}/skill_evolution.py" "$STAGING/scripts/"
cp "${SCRIPTS_SRC}/quality_report.py" "$STAGING/scripts/"
# quality_report.py auto-discovers eval/eval_config.json relative
# to itself — keep the same layout inside the image.
cp "${SCRIPTS_SRC}/eval/eval_config.json" "$STAGING/scripts/eval/"

echo "==> building image: $IMAGE"
gcloud builds submit "$STAGING" \
  --project "$PROJECT" \
  --region "$REGION" \
  --tag "$IMAGE" \
  --quiet

# ----------------------------------------------------------- #
# 4. Deploy the Cloud Run Job                                  #
# ----------------------------------------------------------- #

echo "==> deploying Cloud Run Job: $JOB_NAME"

ENV_VARS=(
  "PROJECT_ID=${PROJECT}"
  "REGION=${REGION}"
  "GOOGLE_GENAI_USE_VERTEXAI=True"
  "DATASET_ID=${DATASET}"
  "TABLE_ID=${TABLE}"
  "DATASET_LOCATION=${DATASET_LOCATION}"
  # Scheduled fires run the full loop: quality report → gate →
  # evolve → PR. CLI-style single modes remain reachable via
  # ``gcloud run jobs execute --args``.
  "FULL_LOOP=true"
)
if [[ -n "$AGENT_REGISTRY" ]]; then
  ENV_VARS+=("AGENT_REGISTRY=${AGENT_REGISTRY}")
fi
if [[ -n "$GITHUB_REPO" ]]; then
  ENV_VARS+=("GITHUB_REPO=${GITHUB_REPO}")
  ENV_VARS+=("GITHUB_BASE_BRANCH=${BASE_BRANCH}")
fi
if [[ -n "$GITHUB_REPO" && -n "$GH_SECRET" ]]; then
  # EVOLUTION_PUBLISH defaults to false (local runs produce dry-run
  # previews). PR mode is the point of this deployment, so flip it on
  # exactly when GitHub credentials are explicitly wired.
  ENV_VARS+=("EVOLUTION_PUBLISH=true")
fi
if [[ -n "$GCS_BUCKET" ]]; then
  ENV_VARS+=("EVOLUTION_GCS_BUCKET=${GCS_BUCKET}")
fi
ENV_VAR_FLAG="$(IFS=','; echo "${ENV_VARS[*]}")"

DEPLOY_ARGS=(
  --project "$PROJECT"
  --region "$REGION"
  --image "$IMAGE"
  --service-account "$RUNTIME_SA_EMAIL"
  --set-env-vars "$ENV_VAR_FLAG"
  --task-timeout "${TASK_TIMEOUT}s"
  --memory 2Gi
  # 0, deliberately: a retried half-finished evolution run could
  # open duplicate branches/PRs. The weekly schedule is the retry.
  --max-retries 0
)
if [[ -n "$GH_SECRET" ]]; then
  DEPLOY_ARGS+=(--set-secrets "GH_TOKEN=${GH_SECRET}:latest")
fi

gcloud run jobs deploy "$JOB_NAME" "${DEPLOY_ARGS[@]}"

# ----------------------------------------------------------- #
# 5. Scheduler trigger                                         #
# ----------------------------------------------------------- #

# ``roles/run.invoker`` on the specific job — in split-SA mode the
# ONLY role the scheduler caller holds. Retried for the same IAM
# replica lag as the project-level grants.
echo "==> granting roles/run.invoker on $JOB_NAME to $SCHEDULER_SA_EMAIL"
_retry_iam gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
  --project "$PROJECT" \
  --region "$REGION" \
  --member "serviceAccount:${SCHEDULER_SA_EMAIL}" \
  --role roles/run.invoker \
  --quiet

JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB_NAME}:run"

if gcloud scheduler jobs describe "$SCHEDULER_NAME" \
    --project "$PROJECT" \
    --location "$REGION" >/dev/null 2>&1; then
  echo "==> updating Cloud Scheduler job: $SCHEDULER_NAME"
  gcloud scheduler jobs update http "$SCHEDULER_NAME" \
    --project "$PROJECT" \
    --location "$REGION" \
    --schedule "$SCHEDULE" \
    --uri "$JOB_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA_EMAIL"
else
  echo "==> creating Cloud Scheduler job: $SCHEDULER_NAME"
  gcloud scheduler jobs create http "$SCHEDULER_NAME" \
    --project "$PROJECT" \
    --location "$REGION" \
    --schedule "$SCHEDULE" \
    --uri "$JOB_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA_EMAIL"
fi

echo
echo "Cloud Run Job:       projects/${PROJECT}/locations/${REGION}/jobs/${JOB_NAME}"
echo "Cloud Scheduler:     projects/${PROJECT}/locations/${REGION}/jobs/${SCHEDULER_NAME}"
echo "Schedule:            ${SCHEDULE}"
echo "Image:               ${IMAGE}"
if [[ "$SINGLE_SA" == "true" ]]; then
  echo "Service account:     ${RUNTIME_SA_EMAIL} (single-sa mode)"
else
  echo "Runtime SA:          ${RUNTIME_SA_EMAIL}"
  echo "Scheduler-caller SA: ${SCHEDULER_SA_EMAIL}"
fi

# ----------------------------------------------------------- #
# 6. Optional smoke run (--smoke)                              #
# ----------------------------------------------------------- #
#
# Executes the job once with ``--args=--test``: the component's
# self-test locates the evolution engine, parses the agent
# registry, registers the agent tools and reports hook status,
# then prints the ``SELF-TEST PASS`` sentinel. We require that
# exact sentinel in the execution's logs — a green exit code with
# missing sentinel means the wrong entrypoint ran.

if [[ "$SMOKE" == true ]]; then
  echo
  echo "==> running smoke execution (--smoke): --test self-test"
  set +e
  EXECUTION_NAME="$(
    gcloud run jobs execute "$JOB_NAME" \
      --project "$PROJECT" \
      --region "$REGION" \
      --args="--test" \
      --wait \
      --format='value(metadata.name)'
  )"
  SMOKE_RC=$?
  set -e
  echo "==> execution: ${EXECUTION_NAME:-<none>}"
  if [[ $SMOKE_RC -ne 0 || -z "$EXECUTION_NAME" ]]; then
    echo "Error: smoke execution failed (exit ${SMOKE_RC})." >&2
    exit 1
  fi

  # Cloud Logging ingestion lags the execution by a few seconds —
  # poll for the sentinel instead of reading once.
  echo "==> checking logs for SELF-TEST PASS"
  FOUND=false
  for _attempt in 1 2 3 4 5 6 7 8; do
    LOGS="$(gcloud logging read \
      "resource.type=cloud_run_job \
       AND resource.labels.job_name=${JOB_NAME} \
       AND labels.\"run.googleapis.com/execution_name\"=${EXECUTION_NAME}" \
      --project "$PROJECT" \
      --limit 200 \
      --format='value(textPayload)' 2>/dev/null || true)"
    if grep -q "SELF-TEST PASS" <<<"$LOGS"; then
      FOUND=true
      break
    fi
    sleep 10
  done
  if [[ "$FOUND" == true ]]; then
    echo "==> smoke OK: SELF-TEST PASS found in execution logs"
  else
    echo "Error: SELF-TEST PASS not found in logs for ${EXECUTION_NAME}." >&2
    echo "Inspect with:" >&2
    echo "  gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}' --project ${PROJECT} --limit 100" >&2
    exit 1
  fi
fi

echo
echo "Done."
