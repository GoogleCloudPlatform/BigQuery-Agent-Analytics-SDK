#!/usr/bin/env bash
# Preflight for the hero demo: fail in <2 minutes with actionable messages,
# BEFORE anything mutates. Read-only. Usage: preflight.sh <project> [dataset]
set -uo pipefail

PROJECT="${1:?usage: preflight.sh <project> [dataset]}"
DATASET="${2:-}"

PASS=0; FAIL=0; WARN=0
ok()   { printf 'OK    %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf 'FAIL  %s\n      fix: %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
warn() { printf 'WARN  %s\n      note: %s\n' "$1" "$2"; WARN=$((WARN+1)); }

# Same identifier rules as the product CLI (BootstrapSettings/VerifySettings):
# these values are interpolated into SQL identifiers by the demo scripts.
python3 -c "
import re, sys
project, dataset = sys.argv[1], sys.argv[2]
if not re.fullmatch(r'[a-z0-9.:-]+', project, re.IGNORECASE):
    sys.exit('FAIL  invalid GCP project id %r' % project)
if not re.fullmatch(r'\\w+', dataset, re.ASCII):
    sys.exit('FAIL  invalid BigQuery dataset id %r' % dataset)
" "$PROJECT" "${DATASET:-placeholder_ds}" || exit 1

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

# --- permissions (via the Resource Manager API) -------------------------------
# (gcloud has no test-iam-permissions subcommand for projects; the REST call
# returns exactly the subset of tested permissions the caller holds.)
# This list mirrors what bootstrap ACTUALLY does — creates AND the
# setIamPolicy/actAs/DTS surface where real runs have failed mid-deploy.
REQUIRED_PERMS="serviceusage.services.enable artifactregistry.repositories.create run.services.create run.services.setIamPolicy pubsub.topics.create pubsub.topics.setIamPolicy pubsub.subscriptions.create pubsub.subscriptions.update pubsub.subscriptions.setIamPolicy bigquery.datasets.create bigquery.jobs.create bigquery.transfers.update secretmanager.secrets.create secretmanager.versions.add secretmanager.secrets.setIamPolicy cloudbuild.builds.create iam.serviceAccounts.create iam.serviceAccounts.setIamPolicy iam.serviceAccounts.actAs resourcemanager.projects.setIamPolicy"
PERM_RESULT=$(curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token 2>/dev/null)" \
  -H "Content-Type: application/json" \
  "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT}:testIamPermissions" \
  -d "{\"permissions\":[$(echo "$REQUIRED_PERMS" | tr ' ' '\n' | sed 's/.*/"&"/' | paste -sd, -)]}" \
  | REQUIRED="$REQUIRED_PERMS" python3 -c '
import json, os, sys
granted = set(json.load(sys.stdin).get("permissions", []))
required = os.environ["REQUIRED"].split()
missing = [p for p in required if p not in granted]
counts = str(len(required) - len(missing)) + "/" + str(len(required))
print(counts + "|" + ",".join(missing))' 2>/dev/null)
PERM_COUNTS="${PERM_RESULT%%|*}"
PERM_MISSING="${PERM_RESULT#*|}"
if [ -n "$PERM_RESULT" ] && [ -z "$PERM_MISSING" ]; then
  ok "deploy permissions present (${PERM_COUNTS}: create + setIamPolicy/actAs/DTS surface)"
else
  bad "missing deploy permissions (${PERM_COUNTS:-0/14}): ${PERM_MISSING:-could not query}" \
      "grant roles covering these before starting the clock"
fi

# --- org policy: can Cloud Run be invoked by allUsers? ------------------------
# The receiver deploys --allow-unauthenticated (bearer auth at the app
# layer). Domain-restricted sharing (iam.allowedPolicyMemberDomains) blocks
# the allUsers grant and the deploy fails ~12 minutes in, at Cloud Run.
ORG_POLICY=$(gcloud resource-manager org-policies describe \
  constraints/iam.allowedPolicyMemberDomains --project "$PROJECT" \
  --effective --format=json 2>/dev/null)
if [ -z "$ORG_POLICY" ]; then
  warn "org policy iam.allowedPolicyMemberDomains not readable" \
       "cannot verify allUsers is permitted; if domain-restricted sharing is enforced, the Cloud Run deploy fails ~12 min in"
elif printf '%s' "$ORG_POLICY" | python3 -c '
import json, sys
p = json.load(sys.stdin).get("listPolicy", {})
sys.exit(1 if (p.get("allowedValues") or p.get("allValues") == "DENY") else 0)' 2>/dev/null; then
  ok "org policy permits allUsers member grants (Cloud Run --allow-unauthenticated will work)"
else
  bad "domain-restricted sharing is enforced (iam.allowedPolicyMemberDomains)" \
      "the receiver needs an allUsers invoker grant; get a policy exception or use a project where it is allowed"
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
echo "${PASS} ok, ${WARN} warnings, ${FAIL} failed"
[ "$FAIL" -eq 0 ] || { echo "Preflight FAILED — do not start the demo clock."; exit 1; }
echo "Preflight green — safe to bootstrap/present."
