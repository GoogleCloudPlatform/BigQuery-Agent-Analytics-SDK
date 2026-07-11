#!/usr/bin/env bash
# Regenerate the hash-locked dependency files inside the exact pinned
# base image the Dockerfile uses. Supply-chain posture (#356 round 7):
# the generator itself (pip-tools) installs from a reviewed hash lock
# (pip-tools.lock — the one acknowledged bootstrap input); the
# repository mounts READ-ONLY; outputs are produced in a scratch
# directory and only the expected lock files are copied back after the
# container exits.
set -euo pipefail
BASE="python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf"
# Scratch lives under $HOME: macOS Docker Desktop shares /Users but not
# /tmp or /var/folders — a mount outside the share silently swallows the
# outputs inside the VM.
mkdir -p "$HOME/.cache"
OUT=$(mktemp -d "$HOME/.cache/bqaa-regen-locks.XXXXXX")
docker run --rm -v "$PWD:/src:ro" -v "$OUT:/out" "$BASE" bash -c "
  set -euo pipefail
  pip install -q --require-hashes -r /src/deploy/otlp_receiver/pip-tools.lock &&
  pip-compile --quiet --generate-hashes --strip-extras --extra receiver \
    --no-build-isolation \
    --output-file /out/requirements.lock \
    /src/producers/pyproject.toml &&
  pip-compile --quiet --generate-hashes --strip-extras \
    --output-file /out/build-requirements.lock \
    /src/deploy/otlp_receiver/build-toolchain.in
"
cp "$OUT/requirements.lock" deploy/otlp_receiver/requirements.lock
cp "$OUT/build-requirements.lock" producers/build-requirements.lock
rm -rf "$OUT"
echo "locks regenerated — review the diffs before committing"
