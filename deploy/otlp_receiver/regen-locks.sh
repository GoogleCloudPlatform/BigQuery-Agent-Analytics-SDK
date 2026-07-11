#!/usr/bin/env bash
# Regenerate ALL hash-locked dependency files inside the exact pinned
# base image the Dockerfile uses. Supply-chain posture (#356 rounds 7-9):
#   - TWO-PHASE convergence: phase 1 (env from the reviewed checked-in
#     pip-tools.lock) produces a candidate generator lock; phase 2 runs
#     in a FRESH container installed from that candidate, regenerates
#     the generator lock again (must be byte-identical — one-run
#     convergent even across generator upgrades) and only then compiles
#     the downstream locks with the NEW toolchain;
#   - the repository mounts READ-ONLY; outputs land in a scratch dir;
#   - copyback is transactional: staged next to the destinations,
#     originals backed up, rollback on ERR/INT/TERM;
#   - the base image reference is parsed from the Dockerfile's single
#     canonical ARG, and every literal reference must agree;
#   - caller-cwd independent; scratch cleaned on every exit path.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="$REPO_ROOT/deploy/otlp_receiver/Dockerfile"

# --base-ref-start-- (executed verbatim by the test suite)
REFS=$(grep -oE 'python:[0-9.]+-slim@sha256:[0-9a-f]{64}' "$DOCKERFILE" | sort -u)
if [ "$(echo "$REFS" | grep -c .)" != "1" ]; then
  echo "Dockerfile base references are not a single canonical value:" >&2
  echo "$REFS" >&2
  exit 1
fi
BASE="$REFS"
# --base-ref-end--

mkdir -p "$HOME/.cache"
OUT=$(mktemp -d "$HOME/.cache/bqaa-regen-locks.XXXXXX")
trap 'rm -rf "$OUT"' EXIT

echo "==> phase 1: candidate generator lock (env from the reviewed lock)"
docker run --rm -v "$REPO_ROOT:/src:ro" -v "$OUT:/out" "$BASE" bash -c "
  set -euo pipefail
  pip install -q --require-hashes -r /src/deploy/otlp_receiver/pip-tools.lock
  pip-compile --quiet --generate-hashes --strip-extras --allow-unsafe \
    --output-file /out/pip-tools.candidate.lock \
    /src/deploy/otlp_receiver/pip-tools.in
"

echo "==> phase 2: fresh env from the CANDIDATE; convergence + downstream"
docker run --rm -v "$REPO_ROOT:/src:ro" -v "$OUT:/out" "$BASE" bash -c "
  set -euo pipefail
  pip install -q --require-hashes -r /out/pip-tools.candidate.lock
  pip-compile --quiet --generate-hashes --strip-extras --allow-unsafe \
    --output-file /out/pip-tools.lock \
    /src/deploy/otlp_receiver/pip-tools.in
  # Convergence compares RESOLUTION content: the pip-compile header
  # embeds each phase's --output-file path, which legitimately differs.
  diff <(grep -v '^#' /out/pip-tools.candidate.lock) \
       <(grep -v '^#' /out/pip-tools.lock) > /dev/null || {
    echo 'generator lock is not one-run convergent — rerun after review' >&2
    exit 1
  }
  pip-compile --quiet --generate-hashes --strip-extras --extra receiver \
    --no-build-isolation \
    --output-file /out/requirements.lock \
    /src/producers/pyproject.toml
  pip-compile --quiet --generate-hashes --strip-extras \
    --output-file /out/build-requirements.lock \
    /src/deploy/otlp_receiver/build-toolchain.in
"

for f in pip-tools.lock requirements.lock build-requirements.lock; do
  test -s "$OUT/$f" || { echo "generated $f is missing or empty — aborting"; exit 1; }
done

# --copyback-start-- (executed verbatim by the test suite; requires
# $OUT plus DEST_PIP_TOOLS/DEST_REQUIREMENTS/DEST_BUILD to be set)
DEST_PIP_TOOLS="${DEST_PIP_TOOLS:-$REPO_ROOT/deploy/otlp_receiver/pip-tools.lock}"
DEST_REQUIREMENTS="${DEST_REQUIREMENTS:-$REPO_ROOT/deploy/otlp_receiver/requirements.lock}"
DEST_BUILD="${DEST_BUILD:-$REPO_ROOT/producers/build-requirements.lock}"
rollback() {
  for dest in "$DEST_PIP_TOOLS" "$DEST_REQUIREMENTS" "$DEST_BUILD"; do
    [ -f "$dest.bqaa-bak" ] && mv -f "$dest.bqaa-bak" "$dest"
    rm -f "$dest.bqaa-new"
  done
}
trap 'rollback' ERR INT TERM
for pair in "pip-tools.lock:$DEST_PIP_TOOLS" \
            "requirements.lock:$DEST_REQUIREMENTS" \
            "build-requirements.lock:$DEST_BUILD"; do
  src_name="${pair%%:*}"; dest="${pair#*:}"
  cp "$OUT/$src_name" "$dest.bqaa-new"   # stage next to the destination
  cp "$dest" "$dest.bqaa-bak"            # keep the original for rollback
done
for dest in "$DEST_PIP_TOOLS" "$DEST_REQUIREMENTS" "$DEST_BUILD"; do
  mv -f "$dest.bqaa-new" "$dest"
done
for dest in "$DEST_PIP_TOOLS" "$DEST_REQUIREMENTS" "$DEST_BUILD"; do
  rm -f "$dest.bqaa-bak"
done
trap - ERR INT TERM
# --copyback-end--
echo "locks regenerated — review the diffs before committing"
