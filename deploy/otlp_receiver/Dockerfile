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

# Pinned by digest for release reproducibility (python:3.12-slim as of
# 2026-07-10). Full hash-locked pip dependencies are follow-up work; the
# freeze layer below records what was actually resolved for audit.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

WORKDIR /app

# Install the producer package with the receiver extra (gunicorn,
# google-cloud-pubsub, opentelemetry-proto). Dependencies come from the
# reviewed hash-locked constraints (pip-compile --generate-hashes inside
# this exact base image); --require-hashes means a mutated upstream
# release cannot change production bytes at the same source commit. The
# package itself installs --no-deps on top. Regenerate the lock with:
#   docker run --rm -v "$PWD:/src" python:3.12-slim@<base digest> bash -c \
#     "pip install -q pip-tools && cd /src && pip-compile --generate-hashes \
#      --strip-extras --extra receiver \
#      -o deploy/otlp_receiver/requirements.lock producers/pyproject.toml"
COPY deploy/otlp_receiver/requirements.lock /src/requirements.lock
COPY producers /src/producers
# The PEP 517 build backend is hash-locked too: a bare --no-deps install
# would still download an unpinned hatchling into an isolated build env
# (#356 round 5). --no-build-isolation makes the locked one authoritative.
RUN pip install --no-cache-dir --require-hashes -r /src/requirements.lock \
    && pip install --no-cache-dir --require-hashes -r /src/producers/build-requirements.lock \
    && pip install --no-cache-dir --no-build-isolation --no-deps /src/producers \
    && pip freeze > /opt/bqaa-resolved-requirements.txt

ENV PORT=8080

# Default role: OTLP receiver. Consumer role overrides CMD to:
#   gunicorn --bind :$PORT \
#     'bigquery_agent_analytics_tracing.otlp.consumer:make_push_app_from_env()'
# gunicorn calls factories via the 'module:callable()' syntax; '--factory'
# is a uvicorn flag and makes the container exit 2 on startup.
CMD exec gunicorn --bind ":${PORT}" --workers 2 --threads 8 \
    --access-logfile - \
    'bigquery_agent_analytics_tracing.otlp.app:make_app()'
