#!/usr/bin/env bash
# Teardown for the hero demo. DRY-RUN BY DEFAULT: prints exactly what would
# be deleted and exits. --confirm executes and then verifies EVERY resource
# class is actually gone (a failed delete cannot hide — verification checks
# existence, not command exit codes). Consumes the inventory written by
# write_inventory.sh; every name must match a bqaa demo allowlist pattern.
#
#   teardown.sh [--confirm] [--dataset-only] [--verify-only]
#
# Tiers:
#   dataset-scoped : DTS scheduled MERGE + the BigQuery dataset (real
#                    telemetry lives here — tear down promptly)
#   pipeline       : Cloud Run services, topics/subscriptions, secret,
#                    Artifact Registry repo, service accounts + IAM binding.
#                    Skipped with --dataset-only.
# --verify-only runs just the existence verification (no deletions): on a
# live deployment everything reports STILL EXISTS, proving detection works.
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$HERE/evidence}"
INVENTORY="${INVENTORY:-$EVIDENCE_DIR/demo_resources.json}"
CONFIRM=0; DATASET_ONLY=0; VERIFY_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --confirm) CONFIRM=1 ;;
    --dataset-only) DATASET_ONLY=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    *) echo "unknown flag: $arg"; exit 2 ;;
  esac
done

[ -f "$INVENTORY" ] || { echo "ERROR: inventory not found: $INVENTORY"; echo "run scripts/write_inventory.sh first"; exit 2; }
inv() { python3 -c "import json,sys; v=json.load(open('$INVENTORY'))$1; print('\n'.join(v) if isinstance(v,list) else v)"; }

PROJECT=$(inv "['project']"); DATASET=$(inv "['dataset']"); REGION=$(inv "['region']")
BQ_LOCATION=$(inv ".get('bq_location', 'US')")
DTS=$(inv "['dts_transfer_config']")

# Allowlist guard: refuse to touch anything that does not match the demo's
# naming patterns — never judgment at runtime, always the pattern contract.
# The DATASET pattern matters most: `bq rm -r -f` on an arbitrary dataset is
# the single most destructive command here, so a poisoned inventory must be
# refused, not obeyed.
guard() {  # guard <value> <what> <pattern>... — case alternation via '|'
  # inside an expanded variable does NOT work in bash, so patterns are
  # separate arguments and tried in turn.
  local val="$1" what="$2" pat; shift 2
  for pat in "$@"; do
    case "$val" in $pat) return 0 ;; esac
  done
  echo "REFUSING: $what '$val' does not match allowlist patterns: $*"
  exit 3
}
guard "$DATASET" "dataset" "bqaa_hero_demo_*" "otlp_e2e_*"
for svc in $(inv "['cloud_run_services']"); do guard "$svc" "cloud run service" "bqaa-otlp-*"; done
for t in $(inv "['pubsub_topics']"); do guard "$t" "topic" "bqaa-otlp*"; done
for s in $(inv "['pubsub_subscriptions']"); do guard "$s" "subscription" "bqaa-otlp*"; done
guard "$(inv "['secret']")" "secret" "bqaa-otlp-*"
guard "$(inv "['artifact_repo']")" "artifact repo" "bqaa"
for sa in $(inv "['service_accounts']"); do guard "$sa" "service account" "bqaa-otlp-*"; done

run() {  # run <description> <cmd...>
  local desc="$1"; shift
  if [ "$CONFIRM" -eq 1 ]; then
    echo "DELETE  $desc"
    local out
    if ! out=$("$@" 2>&1); then
      # Visible, never silent — and the existence verification below is the
      # authority on whether the resource is actually gone.
      echo "        delete command failed (verification will decide):"
      echo "        $(echo "$out" | head -1)"
    fi
  else
    echo "WOULD DELETE  $desc"
    echo "              $*"
  fi
}

CONSUMER_SA="bqaa-otlp-consumer@${PROJECT}.iam.gserviceaccount.com"

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "Teardown plan from $INVENTORY (project=$PROJECT dataset=$DATASET location=$BQ_LOCATION)"
  [ "$CONFIRM" -eq 1 ] || echo "DRY RUN — re-run with --confirm to execute."
  echo
  echo "--- dataset-scoped ---"
  if [ -n "$DTS" ]; then
    run "DTS scheduled MERGE ($DTS)" \
      bq --headless --project_id="$PROJECT" --location="$BQ_LOCATION" rm -f --transfer_config "$DTS"
  else
    echo "no DTS transfer config recorded for dataset $DATASET"
  fi
  run "BigQuery dataset ${PROJECT}:${DATASET} (contains real telemetry)" \
    bq --headless --project_id="$PROJECT" rm -r -f --dataset "${PROJECT}:${DATASET}"

  if [ "$DATASET_ONLY" -eq 0 ]; then
    echo
    echo "--- pipeline (shared across datasets; skip with --dataset-only) ---"
    for svc in $(inv "['cloud_run_services']"); do
      run "Cloud Run service $svc" \
        gcloud run services delete "$svc" --project "$PROJECT" --region "$REGION" --quiet
    done
    for s in $(inv "['pubsub_subscriptions']"); do
      run "subscription $s" gcloud pubsub subscriptions delete "$s" --project "$PROJECT" --quiet
    done
    for t in $(inv "['pubsub_topics']"); do
      run "topic $t" gcloud pubsub topics delete "$t" --project "$PROJECT" --quiet
    done
    run "secret $(inv "['secret']")" \
      gcloud secrets delete "$(inv "['secret']")" --project "$PROJECT" --quiet
    run "artifact repo $(inv "['artifact_repo']") (and its images)" \
      gcloud artifacts repositories delete "$(inv "['artifact_repo']")" \
        --project "$PROJECT" --location "$REGION" --quiet
    run "project jobUser binding for ${CONSUMER_SA}" \
      gcloud projects remove-iam-policy-binding "$PROJECT" \
        --member "serviceAccount:${CONSUMER_SA}" --role roles/bigquery.jobUser --quiet
    for sa in $(inv "['service_accounts']"); do
      run "service account ${sa}@${PROJECT}.iam.gserviceaccount.com" \
        gcloud iam service-accounts delete "${sa}@${PROJECT}.iam.gserviceaccount.com" \
          --project "$PROJECT" --quiet
    done
  fi

  # Dry run stops here; --confirm and --verify-only fall through to verify.
  [ "$CONFIRM" -eq 1 ] || exit 0
fi

# ---- verification: existence checks for EVERY resource class ---------------
# The authority on teardown success. Under --verify-only against a live
# deployment, everything reports STILL EXISTS (proves detection works).
echo
echo "--- post-teardown verification (existence checks, not exit codes) ---"
V_FAIL=0
gone() {  # gone <what> <cmd...>
  # PASS only on a KNOWN not-found response; an auth/API/permission failure
  # is UNVERIFIABLE and fails the verification — a probe error must never
  # masquerade as "gone".
  local what="$1"; shift
  local out
  if out=$("$@" 2>&1); then
    echo "FAIL  $what STILL EXISTS"; V_FAIL=1
  elif printf '%s' "$out" | grep -qiE 'not.?found|does not exist|was not found|no such'; then
    echo "PASS  $what is gone"
  else
    echo "FAIL  $what UNVERIFIABLE: $(printf '%s' "$out" | head -1 | cut -c1-100)"; V_FAIL=1
  fi
}
# The listing must SUCCEED for a PASS: a failed listing is unverifiable,
# and matching covers the legacy unsuffixed display name (query-text check,
# mirroring bootstrap._find_merge_config) as well as the suffixed one.
DTS_LISTING=$(bq --headless --project_id="$PROJECT" --location="$BQ_LOCATION" ls --transfer_config \
  --transfer_location="$BQ_LOCATION" --format=json 2>&1)
if [ $? -ne 0 ]; then
  echo "FAIL  DTS listing UNVERIFIABLE: $(printf '%s' "$DTS_LISTING" | head -1 | cut -c1-100)"; V_FAIL=1
else
  DTS_LEFT=$(printf '%s' "$DTS_LISTING" | DATASET="$DATASET" python3 -c '
import json, os, sys
try:
    configs = json.load(sys.stdin) or []
except ValueError:
    configs = []
dataset = os.environ["DATASET"]
suffixed = "bqaa_agent_events_otlp_merge_" + dataset
legacy = "bqaa_agent_events_otlp_merge"
def matches(c):
    display = c.get("displayName", "")
    query = (c.get("params") or {}).get("query", "")
    return display == suffixed or (
        display == legacy and ("`" + dataset + ".agent_events_otlp`") in query
    )

print(sum(1 for c in configs if matches(c)))' 2>/dev/null || echo "?")
  if [ "$DTS_LEFT" = "0" ]; then
    echo "PASS  no DTS scheduled MERGE remains for $DATASET (incl. legacy unsuffixed)"
  elif [ "$DTS_LEFT" = "?" ]; then
    echo "FAIL  DTS listing UNVERIFIABLE (unparseable)"; V_FAIL=1
  else
    echo "FAIL  DTS scheduled MERGE STILL EXISTS for $DATASET (count: $DTS_LEFT — bills every 15 min)"; V_FAIL=1
  fi
fi
gone "dataset ${DATASET} (real telemetry)" \
  bq --headless --project_id="$PROJECT" show --dataset "${PROJECT}:${DATASET}"

if [ "$DATASET_ONLY" -eq 0 ]; then
  for svc in $(inv "['cloud_run_services']"); do
    gone "Cloud Run service $svc" \
      gcloud run services describe "$svc" --project "$PROJECT" --region "$REGION"
  done
  for s in $(inv "['pubsub_subscriptions']"); do
    gone "subscription $s" gcloud pubsub subscriptions describe "$s" --project "$PROJECT"
  done
  for t in $(inv "['pubsub_topics']"); do
    gone "topic $t" gcloud pubsub topics describe "$t" --project "$PROJECT"
  done
  gone "secret $(inv "['secret']")" \
    gcloud secrets describe "$(inv "['secret']")" --project "$PROJECT"
  gone "artifact repo $(inv "['artifact_repo']")" \
    gcloud artifacts repositories describe "$(inv "['artifact_repo']")" \
      --project "$PROJECT" --location "$REGION"
  for sa in $(inv "['service_accounts']"); do
    gone "service account ${sa}@${PROJECT}.iam.gserviceaccount.com" \
      gcloud iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" \
        --project "$PROJECT"
  done
  # A failed policy read must not masquerade as "binding gone".
  if POLICY=$(gcloud projects get-iam-policy "$PROJECT" \
      --flatten="bindings[].members" \
      --filter="bindings.role:roles/bigquery.jobUser AND bindings.members:serviceAccount:${CONSUMER_SA}" \
      --format="value(bindings.role)" 2>&1); then
    if [ -z "$POLICY" ]; then
      echo "PASS  project jobUser binding for ${CONSUMER_SA} is gone"
    else
      echo "FAIL  project jobUser binding for ${CONSUMER_SA} STILL EXISTS"; V_FAIL=1
    fi
  else
    echo "FAIL  IAM policy UNVERIFIABLE: $(printf '%s' "$POLICY" | head -1 | cut -c1-100)"; V_FAIL=1
  fi
fi

echo
if [ "$V_FAIL" -eq 0 ]; then
  echo "Teardown verified clean."
else
  [ "$VERIFY_ONLY" -eq 1 ] && echo "(--verify-only against live infra: STILL EXISTS rows are expected and prove detection works)"
  echo "Verification found remaining resources."
  exit 1
fi
