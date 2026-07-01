#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License").
#
# Thin wrapper: the deploy sequence now lives in `bqaa-otel bootstrap`
# (producers/src/bigquery_agent_analytics_tracing/otlp/bootstrap.py, issue
# #324) so the shell path and the CLI can't drift. Same env-var interface as
# before; run from the repository root.
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

SIGNALS="logs,metrics"
[ "$ENABLE_SPANS" = "1" ] && SIGNALS="logs,metrics,traces"

PYTHONPATH="producers/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m \
  bigquery_agent_analytics_tracing.otlp.cli bootstrap \
  --project "$PROJECT" --dataset "$DATASET" --region "$REGION" \
  --bq-location "$BQ_LOCATION" --signals "$SIGNALS" \
  --source-product "$SOURCE_PRODUCT" --execute
