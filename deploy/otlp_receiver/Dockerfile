# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License").
#
# One image, two Cloud Run roles (issue #316, PR 5):
#   - receiver (default CMD): OTLP HTTP endpoint via gunicorn
#   - consumer (override CMD): Pub/Sub -> BigQuery subscriber
# Build with the repository root as the build context:
#   docker build -f deploy/otlp_receiver/Dockerfile -t <image> .

FROM python:3.12-slim

WORKDIR /app

# Install the producer package with the receiver extra (gunicorn,
# google-cloud-pubsub, opentelemetry-proto).
COPY producers /src/producers
RUN pip install --no-cache-dir "/src/producers[receiver]"

COPY deploy/otlp_receiver/run_consumer.py ./run_consumer.py

ENV PORT=8080

# Default role: OTLP receiver. Override CMD to run the consumer:
#   CMD ["python", "run_consumer.py"]
CMD exec gunicorn --factory --bind ":${PORT}" --workers 2 --threads 8 \
    --access-logfile - \
    bigquery_agent_analytics_tracing.otlp.app:make_app
