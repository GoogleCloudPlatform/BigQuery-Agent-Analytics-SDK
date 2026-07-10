#!/usr/bin/env bash
# Regenerate BOTH hash-locked dependency files inside the exact pinned
# base image the Dockerfile uses, so the resolution platform matches
# production. Run from the repository root; review the diffs.
set -euo pipefail
BASE="python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf"
docker run --rm -v "$PWD:/src" "$BASE" bash -c "
  pip install -q pip-tools &&
  cd /src &&
  pip-compile --quiet --generate-hashes --strip-extras --extra receiver \
    --output-file deploy/otlp_receiver/requirements.lock \
    producers/pyproject.toml &&
  pip-compile --quiet --generate-hashes --strip-extras \
    --output-file producers/build-requirements.lock \
    deploy/otlp_receiver/build-toolchain.in
"
echo "locks regenerated — review the diffs before committing"
