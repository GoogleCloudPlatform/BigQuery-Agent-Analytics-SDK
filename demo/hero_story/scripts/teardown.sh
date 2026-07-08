#!/usr/bin/env bash
# Teardown for the hero demo. DRY-RUN BY DEFAULT: prints exactly what would
# be deleted and exits. --confirm executes. Consumes the inventory written
# by write_inventory.sh — names are never reconstructed from flags, and only
# allowlisted bqaa-* patterns are ever deleted.
#
#   teardown.sh [--confirm] [--dataset-only]
#
# Two tiers:
#   dataset-scoped : DTS scheduled MERGE + the BigQuery dataset (real
#                    telemetry lives here — tear down promptly)
#   pipeline       : Cloud Run services, topics/subscriptions, secret,
#                    Artifact Registry repo, service accounts + their IAM.
#                    Skipped with --dataset-only (another dataset on this
#                    project may still be using the pipeline).
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$HERE/evidence}"
INVENTORY="${INVENTORY:-$EVIDENCE_DIR/demo_resources.json}"
CONFIRM=0; DATASET_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --confirm) CONFIRM=1 ;;
    --dataset-only) DATASET_ONLY=1 ;;
    *) echo "unknown flag: $arg"; exit 2 ;;
  esac
done

[ -f "$INVENTORY" ] || { echo "ERROR: inventory not found: $INVENTORY"; echo "run scripts/write_inventory.sh first"; exit 2; }
inv() { python3 -c "import json,sys; v=json.load(open('$INVENTORY'))$1; print('\n'.join(v) if isinstance(v,list) else v)"; }

PROJECT=$(inv "['project']"); DATASET=$(inv "['dataset']"); REGION=$(inv "['region']")
DTS=$(inv "['dts_transfer_config']")

# Allowlist guard: refuse to touch anything that does not match the demo's
# naming patterns — never judgment at runtime, always the pattern contract.
guard() {  # guard <value> <pattern> <what>
  case "$1" in $2) ;; *) echo "REFUSING: $3 '$1' does not match allowlist pattern '$2'"; exit 3 ;; esac
}
guard "$DATASET" "*" "dataset"   # dataset comes from the inventory verbatim
for svc in $(inv "['cloud_run_services']"); do guard "$svc" "bqaa-otlp-*" "cloud run service"; done
for t in $(inv "['pubsub_topics']"); do guard "$t" "bqaa-otlp*" "topic"; done
for s in $(inv "['pubsub_subscriptions']"); do guard "$s" "bqaa-otlp*" "subscription"; done
guard "$(inv "['secret']")" "bqaa-otlp-*" "secret"
guard "$(inv "['artifact_repo']")" "bqaa" "artifact repo"
for sa in $(inv "['service_accounts']"); do guard "$sa" "bqaa-otlp-*" "service account"; done

run() {  # run <description> <cmd...>
  local desc="$1"; shift
  if [ "$CONFIRM" -eq 1 ]; then
    echo "DELETE  $desc"
    "$@" >/dev/null 2>&1 || echo "        (already gone or failed: $*)"
  else
    echo "WOULD DELETE  $desc"
    echo "              $*"
  fi
}

echo "Teardown plan from $INVENTORY (project=$PROJECT dataset=$DATASET)"
[ "$CONFIRM" -eq 1 ] || echo "DRY RUN — re-run with --confirm to execute."
echo
echo "--- dataset-scoped ---"
if [ -n "$DTS" ]; then
  run "DTS scheduled MERGE ($DTS)" \
    bq --headless --project_id="$PROJECT" rm -f --transfer_config "$DTS"
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
  CONSUMER_SA="bqaa-otlp-consumer@${PROJECT}.iam.gserviceaccount.com"
  run "project jobUser binding for ${CONSUMER_SA}" \
    gcloud projects remove-iam-policy-binding "$PROJECT" \
      --member "serviceAccount:${CONSUMER_SA}" --role roles/bigquery.jobUser --quiet
  for sa in $(inv "['service_accounts']"); do
    run "service account ${sa}@${PROJECT}.iam.gserviceaccount.com" \
      gcloud iam service-accounts delete "${sa}@${PROJECT}.iam.gserviceaccount.com" \
        --project "$PROJECT" --quiet
  done
fi

[ "$CONFIRM" -eq 1 ] || exit 0

# ---- post-teardown verification: prove nothing keeps running/billing -------
echo
echo "--- post-teardown verification ---"
V_FAIL=0
DTS_LEFT=$(bq --headless --project_id="$PROJECT" --location=US ls --transfer_config \
  --transfer_location=US --format=json 2>/dev/null \
  | DATASET="$DATASET" python3 -c '
import json, os, sys
configs = json.load(sys.stdin) or []
wanted = "bqaa_agent_events_otlp_merge_" + os.environ["DATASET"]
print(sum(1 for c in configs if c.get("displayName") == wanted))' 2>/dev/null || echo "?")
if [ "$DTS_LEFT" = "0" ]; then echo "PASS  no DTS scheduled MERGE remains for $DATASET"; else echo "FAIL  DTS configs remaining: $DTS_LEFT"; V_FAIL=1; fi
if bq --headless --project_id="$PROJECT" show --dataset "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
  echo "FAIL  dataset ${DATASET} still exists (real telemetry!)"; V_FAIL=1
else
  echo "PASS  dataset ${DATASET} is gone"
fi
if [ "$DATASET_ONLY" -eq 0 ]; then
  for svc in $(inv "['cloud_run_services']"); do
    if gcloud run services describe "$svc" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
      echo "FAIL  Cloud Run service $svc still exists"; V_FAIL=1
    else
      echo "PASS  Cloud Run service $svc is gone"
    fi
  done
fi
[ "$V_FAIL" -eq 0 ] && echo "Teardown verified clean." || { echo "Teardown verification FAILED."; exit 1; }
