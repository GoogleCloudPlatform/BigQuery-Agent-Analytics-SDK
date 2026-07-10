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
#
# Supply-chain contract (#349/#356): digest-pinned base; runtime deps AND
# the PEP 517 build backend install from reviewed hash-locked files
# (regenerate with deploy/otlp_receiver/regen-locks.sh); multi-stage so
# the shipped image carries neither the build toolchain nor the source
# tree — only the runtime lock and the built wheel.

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS builder

WORKDIR /build
COPY producers /build/producers
RUN pip install --no-cache-dir --require-hashes \
      -r /build/producers/build-requirements.lock \
    && python -m build --no-isolation --wheel --outdir /build/dist \
      /build/producers

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

WORKDIR /app

COPY deploy/otlp_receiver/requirements.lock /tmp/requirements.lock
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && pip install --no-cache-dir --no-deps /tmp/*.whl \
    && pip freeze > /opt/bqaa-resolved-requirements.txt \
    && rm /tmp/*.whl /tmp/requirements.lock

ENV PORT=8080

# Default role: OTLP receiver. Consumer role overrides CMD to:
#   gunicorn --bind :$PORT \
#     'bigquery_agent_analytics_tracing.otlp.consumer:make_push_app_from_env()'
# gunicorn calls factories via the 'module:callable()' syntax; '--factory'
# is a uvicorn flag and makes the container exit 2 on startup
# (hit live during the #324 e2e — first real Cloud Run deploy).
CMD exec gunicorn --bind ":${PORT}" --workers 2 --threads 8 \
    --access-logfile - \
    'bigquery_agent_analytics_tracing.otlp.app:make_app()'
