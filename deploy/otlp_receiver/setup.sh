#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License").
#
# Deploy the OTel-native OTLP receiver (issue #316, PR 5): BigQuery schema +
# views, Pub/Sub topics + a push subscription with DLQ, a Secret Manager bearer
# token, dedicated least-privilege service accounts, the Cloud Run receiver +
# push-consumer, and the scheduled MERGE into agent_events_otlp.
#
# Prereqs: gcloud + bq authenticated, a billing-enabled project, and Docker (for
# `gcloud builds submit`). Run from the repository root.
#
#   PROJECT=my-proj DATASET=agent_analytics REGION=us-central1 BQ_LOCATION=US \
#     bash deploy/otlp_receiver/setup.sh
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
DATASET="${DATASET:-agent_analytics}"
REGION="${REGION:-us-central1}"           # Cloud Run / Artifact Registry region
BQ_LOCATION="${BQ_LOCATION:-US}"          # BigQuery dataset + scheduled-query loc
ENABLE_SPANS="${ENABLE_SPANS:-0}"         # 1 to create/land otel_spans
SOURCE_PRODUCT="${SOURCE_PRODUCT:-claude_code}"

AR_REPO="${AR_REPO:-bqaa}"
MAIN_TOPIC="${MAIN_TOPIC:-bqaa-otlp}"
DLQ_TOPIC="${DLQ_TOPIC:-bqaa-otlp-dlq}"
SUBSCRIPTION="${SUBSCRIPTION:-bqaa-otlp-sub}"
SECRET="${SECRET:-bqaa-otlp-token}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/otlp-receiver:latest}"
RECEIVER_SVC="${RECEIVER_SVC:-bqaa-otlp-receiver}"
CONSUMER_SVC="${CONSUMER_SVC:-bqaa-otlp-consumer}"
HERE="$(cd "$(dirname "$0")" && pwd)"

RECEIVER_SA="bqaa-otlp-receiver@${PROJECT}.iam.gserviceaccount.com"
CONSUMER_SA="bqaa-otlp-consumer@${PROJECT}.iam.gserviceaccount.com"
PUSH_SA="bqaa-otlp-push@${PROJECT}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

echo "==> Enabling APIs"
gcloud services enable --project "$PROJECT" \
  run.googleapis.com pubsub.googleapis.com bigquery.googleapis.com \
  bigquerydatatransfer.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com iam.googleapis.com

echo "==> Ensuring Artifact Registry repo '${AR_REPO}' exists"
gcloud artifacts repositories describe "$AR_REPO" --project "$PROJECT" \
  --location "$REGION" >/dev/null 2>&1 || \
gcloud artifacts repositories create "$AR_REPO" --project "$PROJECT" \
  --location "$REGION" --repository-format=docker

echo "==> Creating BigQuery dataset (${BQ_LOCATION}) + native schema"
bq --project_id="$PROJECT" --location="$BQ_LOCATION" mk -f --dataset \
  "${PROJECT}:${DATASET}" >/dev/null
SPANS_FLAG=""; [ "$ENABLE_SPANS" = "1" ] && SPANS_FLAG="--enable-spans"
PYTHONPATH=producers/src python3 "${HERE}/gen_schema_sql.py" "$DATASET" $SPANS_FLAG \
  | bq --project_id="$PROJECT" --location="$BQ_LOCATION" query --use_legacy_sql=false

echo "==> Creating the bearer token secret"
if ! gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  openssl rand -hex 32 | gcloud secrets create "$SECRET" --project "$PROJECT" \
    --replication-policy=automatic --data-file=-
fi

echo "==> Creating service accounts"
for sa in bqaa-otlp-receiver bqaa-otlp-consumer bqaa-otlp-push; do
  gcloud iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" \
    --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$sa" --project "$PROJECT"
done

echo "==> Creating Pub/Sub topics"
gcloud pubsub topics create "$MAIN_TOPIC" --project "$PROJECT" 2>/dev/null || true
gcloud pubsub topics create "$DLQ_TOPIC" --project "$PROJECT" 2>/dev/null || true

echo "==> Granting least-privilege IAM"
# Receiver: read the token secret + publish to the main ingest topic. (Parse/
# decode dead letters travel the main topic with delivery.dlq=true and are
# written to otlp_dead_letter by the consumer; the receiver never publishes to
# the transport DLQ topic.)
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" \
  --member "serviceAccount:${RECEIVER_SA}" \
  --role roles/secretmanager.secretAccessor >/dev/null
gcloud pubsub topics add-iam-policy-binding "$MAIN_TOPIC" --project "$PROJECT" \
  --member "serviceAccount:${RECEIVER_SA}" --role roles/pubsub.publisher >/dev/null
# Consumer: write BigQuery.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${CONSUMER_SA}" --role roles/bigquery.dataEditor >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${CONSUMER_SA}" --role roles/bigquery.jobUser >/dev/null
# Pub/Sub service agent: mint OIDC tokens for the push endpoint + publish to the
# transport DLQ (the subscriber grant is added after the subscription exists).
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" --project "$PROJECT" \
  --member "serviceAccount:${PUBSUB_AGENT}" --role roles/pubsub.publisher >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$PUSH_SA" --project "$PROJECT" \
  --member "serviceAccount:${PUBSUB_AGENT}" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null

echo "==> Building image"
gcloud builds submit --project "$PROJECT" --config=/dev/stdin . <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ['build','-f','deploy/otlp_receiver/Dockerfile','-t','${IMAGE}','.']
images: ['${IMAGE}']
EOF

MAIN_TOPIC_PATH="projects/${PROJECT}/topics/${MAIN_TOPIC}"

echo "==> Deploying the OTLP receiver (Cloud Run)"
gcloud run deploy "$RECEIVER_SVC" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --allow-unauthenticated --service-account "$RECEIVER_SA" \
  --set-secrets "BQAA_OTLP_TOKEN=${SECRET}:latest" \
  --set-env-vars "BQAA_OTLP_MAIN_TOPIC=${MAIN_TOPIC_PATH},BQAA_OTLP_SOURCE_PRODUCT=${SOURCE_PRODUCT},BQAA_OTLP_ENABLE_TRACES=${ENABLE_SPANS}"

echo "==> Deploying the Pub/Sub push consumer (Cloud Run HTTP service)"
gcloud run deploy "$CONSUMER_SVC" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --no-allow-unauthenticated --service-account "$CONSUMER_SA" \
  --command gunicorn \
  --args "--factory,--bind,0.0.0.0:8080,--workers,2,--threads,8,bigquery_agent_analytics_tracing.otlp.consumer:make_push_app_from_env" \
  --set-env-vars "BQAA_PROJECT=${PROJECT},BQAA_DATASET=${DATASET},BQAA_OTLP_ENABLE_TRACES=${ENABLE_SPANS}"

CONSUMER_URL="$(gcloud run services describe "$CONSUMER_SVC" --project "$PROJECT" \
  --region "$REGION" --format='value(status.url)')"
gcloud run services add-iam-policy-binding "$CONSUMER_SVC" --project "$PROJECT" \
  --region "$REGION" --member "serviceAccount:${PUSH_SA}" \
  --role roles/run.invoker >/dev/null

echo "==> Creating the push subscription (OIDC) with DLQ"
gcloud pubsub subscriptions create "$SUBSCRIPTION" --project "$PROJECT" \
  --topic "$MAIN_TOPIC" --push-endpoint "${CONSUMER_URL}/" \
  --push-auth-service-account "$PUSH_SA" \
  --dead-letter-topic "$DLQ_TOPIC" --max-delivery-attempts 5 \
  --ack-deadline 60 2>/dev/null || \
gcloud pubsub subscriptions update "$SUBSCRIPTION" --project "$PROJECT" \
  --push-endpoint "${CONSUMER_URL}/" --push-auth-service-account "$PUSH_SA"

# Dead-letter forwarding needs the Pub/Sub service agent to acknowledge on the
# source subscription in addition to publishing to the DLQ topic.
gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" --project "$PROJECT" \
  --member "serviceAccount:${PUBSUB_AGENT}" --role roles/pubsub.subscriber >/dev/null

echo "==> Registering the scheduled MERGE into agent_events_otlp (every 15 min)"
PYTHONPATH=producers/src python3 "${HERE}/gen_schema_sql.py" "$DATASET" --merge-only \
  > /tmp/agent_events_otlp_merge.sql
if bq --project_id="$PROJECT" --location="$BQ_LOCATION" ls --transfer_config \
     --format=json 2>/dev/null | grep -q '"displayName": "bqaa_agent_events_otlp_merge"'; then
  echo "  scheduled query already exists — update it in the BigQuery console if needed"
else
  bq --project_id="$PROJECT" --location="$BQ_LOCATION" mk --transfer_config \
    --data_source=scheduled_query --display_name="bqaa_agent_events_otlp_merge" \
    --schedule="every 15 minutes" \
    --params="$(python3 -c 'import json;print(json.dumps({"query":open("/tmp/agent_events_otlp_merge.sql").read()}))')"
fi

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
