#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License").
#
# Deploy the OTel-native OTLP receiver (issue #316, PR 5): BigQuery schema +
# views, Pub/Sub topics/subscriptions + DLQ, a Secret Manager bearer token, the
# Cloud Run receiver + consumer, and the scheduled MERGE into agent_events_otlp.
#
# Prereqs: gcloud + bq authenticated, a billing-enabled project, and Docker (for
# `gcloud builds submit`). Run from the repository root.
#
#   PROJECT=my-proj DATASET=agent_analytics REGION=us-central1 \
#     bash deploy/otlp_receiver/setup.sh
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
DATASET="${DATASET:-agent_analytics}"
REGION="${REGION:-us-central1}"
ENABLE_SPANS="${ENABLE_SPANS:-0}"          # 1 to create/land otel_spans
SOURCE_PRODUCT="${SOURCE_PRODUCT:-claude_code}"

MAIN_TOPIC="${MAIN_TOPIC:-bqaa-otlp}"
DLQ_TOPIC="${DLQ_TOPIC:-bqaa-otlp-dlq}"
SUBSCRIPTION="${SUBSCRIPTION:-bqaa-otlp-sub}"
SECRET="${SECRET:-bqaa-otlp-token}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT}/bqaa/otlp-receiver:latest}"
RECEIVER_SVC="${RECEIVER_SVC:-bqaa-otlp-receiver}"
CONSUMER_SVC="${CONSUMER_SVC:-bqaa-otlp-consumer}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Enabling APIs"
gcloud services enable --project "$PROJECT" \
  run.googleapis.com pubsub.googleapis.com bigquery.googleapis.com \
  bigquerydatatransfer.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com

echo "==> Creating BigQuery dataset + native schema"
bq --project_id="$PROJECT" mk -f --dataset "${PROJECT}:${DATASET}" >/dev/null || true
SPANS_FLAG=""; [ "$ENABLE_SPANS" = "1" ] && SPANS_FLAG="--enable-spans"
PYTHONPATH=producers/src python3 "${HERE}/gen_schema_sql.py" "$DATASET" $SPANS_FLAG \
  | bq --project_id="$PROJECT" query --use_legacy_sql=false

echo "==> Creating the bearer token secret (paste/generate a strong token)"
if ! gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  openssl rand -hex 32 | gcloud secrets create "$SECRET" --project "$PROJECT" \
    --replication-policy=automatic --data-file=-
fi

echo "==> Creating Pub/Sub topics + subscription with DLQ"
gcloud pubsub topics create "$MAIN_TOPIC" --project "$PROJECT" 2>/dev/null || true
gcloud pubsub topics create "$DLQ_TOPIC" --project "$PROJECT" 2>/dev/null || true
gcloud pubsub subscriptions create "$SUBSCRIPTION" --project "$PROJECT" \
  --topic "$MAIN_TOPIC" --dead-letter-topic "$DLQ_TOPIC" \
  --max-delivery-attempts 5 --ack-deadline 60 2>/dev/null || true

echo "==> Building image"
gcloud builds submit --project "$PROJECT" --tag "$IMAGE" \
  --config=/dev/stdin . <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ['build','-f','deploy/otlp_receiver/Dockerfile','-t','${IMAGE}','.']
images: ['${IMAGE}']
EOF

MAIN_TOPIC_PATH="projects/${PROJECT}/topics/${MAIN_TOPIC}"
DLQ_TOPIC_PATH="projects/${PROJECT}/topics/${DLQ_TOPIC}"
SUB_PATH="projects/${PROJECT}/subscriptions/${SUBSCRIPTION}"

echo "==> Deploying the OTLP receiver (Cloud Run)"
gcloud run deploy "$RECEIVER_SVC" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --allow-unauthenticated \
  --set-secrets "BQAA_OTLP_TOKEN=${SECRET}:latest" \
  --set-env-vars "BQAA_OTLP_MAIN_TOPIC=${MAIN_TOPIC_PATH},BQAA_OTLP_DLQ_TOPIC=${DLQ_TOPIC_PATH},BQAA_OTLP_SOURCE_PRODUCT=${SOURCE_PRODUCT},BQAA_OTLP_ENABLE_TRACES=${ENABLE_SPANS}"

echo "==> Deploying the Pub/Sub -> BigQuery consumer (Cloud Run)"
gcloud run deploy "$CONSUMER_SVC" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --no-cpu-throttling --command python --args run_consumer.py \
  --set-env-vars "BQAA_PROJECT=${PROJECT},BQAA_DATASET=${DATASET},BQAA_OTLP_SUBSCRIPTION=${SUB_PATH},BQAA_OTLP_ENABLE_TRACES=${ENABLE_SPANS}"

echo "==> Registering the scheduled MERGE into agent_events_otlp (every 15 min)"
PYTHONPATH=producers/src python3 "${HERE}/gen_schema_sql.py" "$DATASET" --merge-only \
  > /tmp/agent_events_otlp_merge.sql
bq --project_id="$PROJECT" mk --transfer_config --location="$REGION" \
  --data_source=scheduled_query --display_name="bqaa_agent_events_otlp_merge" \
  --schedule="every 15 minutes" \
  --params="$(python3 -c 'import json,sys;print(json.dumps({"query":open("/tmp/agent_events_otlp_merge.sql").read()}))')" \
  || echo "  (scheduled query may already exist; update it in the BigQuery console)"

RECEIVER_URL="$(gcloud run services describe "$RECEIVER_SVC" --project "$PROJECT" \
  --region "$REGION" --format='value(status.url)')"

cat <<EOF

==> Done. Receiver: ${RECEIVER_URL}
    Endpoints: ${RECEIVER_URL}/v1/logs , ${RECEIVER_URL}/v1/metrics
    Bearer token: gcloud secrets versions access latest --secret=${SECRET} --project ${PROJECT}

Next: configure Claude Code / Codex to export to this endpoint (see README.md),
then run the smoke test:
    BQAA_OTLP_ENDPOINT=${RECEIVER_URL} BQAA_OTLP_TOKEN=<token> \\
      BQAA_PROJECT=${PROJECT} BQAA_DATASET=${DATASET} \\
      python -m pytest producers/tests/test_otlp_e2e.py -v
EOF
