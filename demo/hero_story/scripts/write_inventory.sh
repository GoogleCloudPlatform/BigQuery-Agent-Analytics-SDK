#!/usr/bin/env bash
# Write evidence/demo_resources.json — the inventory teardown.sh consumes.
# Names are recorded from the live deployment (queried, not reconstructed),
# so teardown deletes exactly what exists, and only that.
# Usage: PROJECT=<proj> DATASET=<ds> [REGION=us-central1] write_inventory.sh
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
DATASET="${DATASET:?set DATASET}"
REGION="${REGION:-us-central1}"
BQ_LOCATION="${BQ_LOCATION:-US}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$HERE/evidence}"
mkdir -p "$EVIDENCE_DIR"

# The DTS scheduled-MERGE resource name must be queried (its id is opaque).
DTS_NAME=$(bq --headless --project_id="$PROJECT" --location="$BQ_LOCATION" ls \
  --transfer_config --transfer_location="$BQ_LOCATION" --format=json 2>/dev/null \
  | DATASET="$DATASET" python3 -c '
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

for c in configs:
    if matches(c):
        print(c.get("name", ""))
        break')

RECEIVER_URL=$(gcloud run services describe bqaa-otlp-receiver --project "$PROJECT" \
  --region "$REGION" --format='value(status.url)' 2>/dev/null || true)

RUN_ID=$(cat "$EVIDENCE_DIR/DEMO_RUN_ID" 2>/dev/null || echo "")

python3 - "$PROJECT" "$DATASET" "$REGION" "$DTS_NAME" "$RECEIVER_URL" "$RUN_ID" "$BQ_LOCATION" << 'EOF' > "$EVIDENCE_DIR/demo_resources.json"
import json, sys
project, dataset, region, dts, url, run_id, bq_location = sys.argv[1:8]
# Redacted URL form for anything shared outside the org.
redacted = (url[: url.find("-") + 1] + "…run.app") if url else ""
print(json.dumps({
    "project": project,
    "dataset": dataset,
    "region": region,
    "bq_location": bq_location,
    "demo_run_id": run_id,
    "receiver_url_redacted": redacted,
    # Dataset-scoped resources (always safe to remove for this demo):
    "dts_transfer_config": dts,
    # Shared pipeline resources (deleted only with --include-pipeline;
    # another dataset on the same project may still be using them):
    "cloud_run_services": ["bqaa-otlp-receiver", "bqaa-otlp-consumer"],
    "pubsub_topics": ["bqaa-otlp", "bqaa-otlp-dlq"],
    "pubsub_subscriptions": ["bqaa-otlp-sub", "bqaa-otlp-dlq-sub"],
    "secret": "bqaa-otlp-token",
    "artifact_repo": "bqaa",
    "service_accounts": [
        "bqaa-otlp-receiver", "bqaa-otlp-consumer", "bqaa-otlp-push",
    ],
}, indent=2))
EOF
echo "wrote $EVIDENCE_DIR/demo_resources.json"
python3 -m json.tool "$EVIDENCE_DIR/demo_resources.json" > /dev/null && echo "inventory valid JSON"
