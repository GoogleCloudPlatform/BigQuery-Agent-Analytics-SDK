# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License").
#
# One image, two Cloud Run HTTP-service roles (issue #316, PR 5) — both serve
# $PORT, per the Cloud Run container contract:
#   - receiver (default CMD): OTLP HTTP endpoint (app:make_app)
#   - consumer (override CMD): Pub/Sub *push* handler (consumer:make_push_app_from_env)
# Build with the repository root as the build context:
#   docker build -f deploy/otlp_receiver/Dockerfile -t <image> .

FROM python:3.12-slim

WORKDIR /app

# Install the producer package with the receiver extra (gunicorn,
# google-cloud-pubsub, opentelemetry-proto).
COPY producers /src/producers
RUN pip install --no-cache-dir "/src/producers[receiver]"

ENV PORT=8080

# Default role: OTLP receiver. Consumer role overrides CMD to:
#   gunicorn --bind :$PORT \
#     'bigquery_agent_analytics_tracing.otlp.consumer:make_push_app_from_env()'
# gunicorn calls factories via the 'module:callable()' syntax; '--factory'
# is a uvicorn flag and makes the container exit 2 on startup.
CMD exec gunicorn --bind ":${PORT}" --workers 2 --threads 8 \
    --access-logfile - \
    'bigquery_agent_analytics_tracing.otlp.app:make_app()'
