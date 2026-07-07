#!/usr/bin/env bash
# Preflight for the hero demo: fail in <2 minutes with actionable messages,
# BEFORE anything mutates. Read-only. Usage: preflight.sh <project> [dataset]
set -uo pipefail

PROJECT="${1:?usage: preflight.sh <project> [dataset]}"
DATASET="${2:-}"

PASS=0; FAIL=0
ok()   { printf 'OK    %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf 'FAIL  %s\n      fix: %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }

# --- local CLIs -------------------------------------------------------------
command -v gcloud >/dev/null && ok "gcloud present ($(gcloud version 2>/dev/null | head -1))" \
  || bad "gcloud missing" "install the Google Cloud SDK"
command -v bq >/dev/null && ok "bq present ($(bq version 2>/dev/null | head -1))" \
  || bad "bq missing" "ships with the Google Cloud SDK"
command -v python3 >/dev/null && ok "python3 present" || bad "python3 missing" "install Python 3.10+"
python3 -c "import bigquery_agent_analytics_tracing.otlp.cli" 2>/dev/null \
  && ok "bqaa-otel importable" \
  || bad "bqaa-otel not importable" "pip install 'producers[receiver]' or export PYTHONPATH=producers/src"

# Product CLIs: a missing/unauthenticated CLI stalls the demo harder than a
# missing GCP role (sessions hang waiting for login).
if command -v claude >/dev/null; then
  ok "claude CLI present ($(claude --version 2>/dev/null | head -1))"
else
  bad "claude CLI missing" "install Claude Code and log in once interactively"
fi
if command -v codex >/dev/null; then
  CODEX_V=$(codex --version 2>/dev/null | head -1)
  ok "codex CLI present (${CODEX_V})"
  [ -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ] \
    && ok "codex auth.json present" \
    || bad "codex not authenticated" "run codex once interactively to create auth.json"
else
  bad "codex CLI missing" "install Codex >= 0.142.5 (config shapes are version-pinned)"
fi

# --- GCP auth / project / billing -------------------------------------------
ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)
[ -n "$ACCOUNT" ] && ok "gcloud authenticated as ${ACCOUNT}" \
  || bad "no active gcloud account" "gcloud auth login && gcloud auth application-default login"
gcloud projects describe "$PROJECT" --format='value(projectId)' >/dev/null 2>&1 \
  && ok "project ${PROJECT} accessible" \
  || bad "project ${PROJECT} not accessible" "check the id and your permissions"
BILLING=$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null)
[ "$BILLING" = "True" ] && ok "billing enabled" \
  || bad "billing not enabled (or not visible)" "link a billing account; Cloud Build/Run refuse without it"

# --- permissions (best effort, via the Resource Manager API) -----------------
# (gcloud has no test-iam-permissions subcommand for projects; use the REST
# call — it returns exactly the subset of tested permissions the caller has.)
PERMS=$(curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token 2>/dev/null)" \
  -H "Content-Type: application/json" \
  "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT}:testIamPermissions" \
  -d '{"permissions":["run.services.create","pubsub.topics.create","bigquery.datasets.create","secretmanager.secrets.create","cloudbuild.builds.create"]}' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("permissions", [])))' 2>/dev/null)
if [ "${PERMS:-0}" -ge 5 ]; then
  ok "deploy permissions present (run/pubsub/bq/secret/cloudbuild)"
else
  bad "missing some deploy permissions (${PERMS:-0}/5 granted)" \
      "need roles covering Cloud Run, Pub/Sub, BigQuery, Secret Manager, Cloud Build"
fi

# --- dataset state (avoid demoing into a dirty dataset) ----------------------
if [ -n "$DATASET" ]; then
  if bq --project_id="$PROJECT" --headless show --dataset "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
    ok "dataset ${DATASET} exists (bootstrap will converge; use a fresh name for a clean-slate demo)"
  else
    ok "dataset ${DATASET} does not exist yet (bootstrap will create it)"
  fi
fi

echo
echo "${PASS} ok, ${FAIL} failed"
[ "$FAIL" -eq 0 ] || { echo "Preflight FAILED — do not start the demo clock."; exit 1; }
echo "Preflight green — safe to bootstrap/present."
