#!/usr/bin/env bash
# Regenerate ALL hash-locked dependency files inside the exact pinned
# base image the Dockerfile uses. Supply-chain posture (#356 rounds 7-8):
#   - the generator (pip-tools + the metadata build backend) installs
#     from the reviewed pip-tools.lock, and pip-tools.lock is ITSELF
#     regenerated from pip-tools.in in the same run (full convergence:
#     CI diffs all three outputs);
#   - the repository mounts READ-ONLY; outputs land in a scratch dir;
#   - the worktree is touched only after every output is validated;
#   - the base image reference is parsed from the Dockerfile (single
#     canonical source — the generator cannot drift from the build);
#   - caller-cwd independent; scratch is cleaned on every exit path.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE=$(grep -m1 -oE 'python:3\.12-slim@sha256:[0-9a-f]{64}' \
  "$REPO_ROOT/deploy/otlp_receiver/Dockerfile")
test -n "$BASE" || { echo "could not parse the base image from the Dockerfile"; exit 1; }

# Scratch lives under $HOME: macOS Docker Desktop shares /Users but not
# /tmp or /var/folders — a mount outside the share silently swallows the
# outputs inside the VM.
mkdir -p "$HOME/.cache"
OUT=$(mktemp -d "$HOME/.cache/bqaa-regen-locks.XXXXXX")
trap 'rm -rf "$OUT"' EXIT

docker run --rm -v "$REPO_ROOT:/src:ro" -v "$OUT:/out" "$BASE" bash -c "
  set -euo pipefail
  pip install -q --require-hashes -r /src/deploy/otlp_receiver/pip-tools.lock
  pip-compile --quiet --generate-hashes --strip-extras --allow-unsafe \
    --output-file /out/pip-tools.lock \
    /src/deploy/otlp_receiver/pip-tools.in
  pip-compile --quiet --generate-hashes --strip-extras --extra receiver \
    --no-build-isolation \
    --output-file /out/requirements.lock \
    /src/producers/pyproject.toml
  pip-compile --quiet --generate-hashes --strip-extras \
    --output-file /out/build-requirements.lock \
    /src/deploy/otlp_receiver/build-toolchain.in
"

# Validate EVERY output before touching the worktree: a failure between
# copies must never leave a mixed lock set.
for f in pip-tools.lock requirements.lock build-requirements.lock; do
  test -s "$OUT/$f" || { echo "generated $f is missing or empty — aborting"; exit 1; }
done
cp "$OUT/pip-tools.lock" "$REPO_ROOT/deploy/otlp_receiver/pip-tools.lock"
cp "$OUT/requirements.lock" "$REPO_ROOT/deploy/otlp_receiver/requirements.lock"
cp "$OUT/build-requirements.lock" "$REPO_ROOT/producers/build-requirements.lock"
echo "locks regenerated — review the diffs before committing"
