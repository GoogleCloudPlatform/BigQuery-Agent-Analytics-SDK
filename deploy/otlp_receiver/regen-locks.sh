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
# Validate the CONSUMERS, not merely a matching literal: exactly one
# digest-pinned global ARG PYTHON_BASE, and exactly two FROM stages that
# both consume ${PYTHON_BASE} (optional builder alias). Any literal or
# alternate-variable stage is refused.
ARG_LINES=$(grep -cE '^ARG PYTHON_BASE=' "$DOCKERFILE" || true)
if [ "$ARG_LINES" != "1" ]; then
  echo "Dockerfile must declare exactly one ARG PYTHON_BASE (found ${ARG_LINES})" >&2
  exit 1
fi
BASE=$(grep -E '^ARG PYTHON_BASE=' "$DOCKERFILE" | sed 's/^ARG PYTHON_BASE=//')
case "$BASE" in
  *@sha256:*) ;;
  *) echo "ARG PYTHON_BASE is not digest-pinned: $BASE" >&2; exit 1 ;;
esac
FROM_TOTAL=$(grep -cE '^FROM ' "$DOCKERFILE" || true)
FROM_OK=$(grep -cE '^FROM \$\{PYTHON_BASE\}( AS [A-Za-z0-9_-]+)?$' "$DOCKERFILE" || true)
if [ "$FROM_TOTAL" != "2" ] || [ "$FROM_OK" != "2" ]; then
  echo "Dockerfile stages must be exactly two FROM \${PYTHON_BASE} instructions" >&2
  echo "(total FROM: ${FROM_TOTAL}, consuming PYTHON_BASE: ${FROM_OK})" >&2
  exit 1
fi
# --base-ref-end--

mkdir -p "$HOME/.cache"
OUT=$(mktemp -d "$HOME/.cache/bqaa-regen-locks.XXXXXX")
trap 'rm -rf "$OUT"' EXIT

# --convergence-start-- (executed verbatim by the test suite; DOCKER
# defaults to docker and is overridden by tests with a stub)
DOCKER="${DOCKER:-docker}"
MAX_PHASES="${MAX_PHASES:-5}"
resolution_body() { grep -v '^#' "$1"; }
cp "$REPO_ROOT/deploy/otlp_receiver/pip-tools.lock" "$OUT/seed.lock"
CONVERGED=0
for phase in $(seq 1 "$MAX_PHASES"); do
  echo "==> generator convergence phase ${phase} (fresh hash-locked env)"
  "$DOCKER" run --rm -v "$REPO_ROOT:/src:ro" -v "$OUT:/out" "$BASE" bash -c "
    set -euo pipefail
    pip install -q --require-hashes -r /out/seed.lock
    pip-compile --quiet --generate-hashes --strip-extras --allow-unsafe \
      --output-file /out/candidate.lock \
      /src/deploy/otlp_receiver/pip-tools.in
  "
  if [ "$(resolution_body "$OUT/seed.lock")" = "$(resolution_body "$OUT/candidate.lock")" ]; then
    CONVERGED=1
    cp "$OUT/candidate.lock" "$OUT/pip-tools.lock"
    break
  fi
  # Keep the phase INPUT distinct from its OUTPUT before reseeding: on
  # exhaustion the diagnostics must show the last two resolutions, not
  # one lock copied twice (the overwrite below erases the delta).
  cp "$OUT/seed.lock" "$OUT/previous-seed.lock"
  cp "$OUT/candidate.lock" "$OUT/seed.lock"
done
if [ "$CONVERGED" != "1" ]; then
  # Preserve the evidence: a rerun from the unchanged checked-in seed is
  # a deterministic dead end, so the final phase's input
  # (previous-seed.lock) and output (candidate.lock) — two DISTINCT
  # resolutions whose diff is the non-convergence — must both survive.
  DIAG=$(mktemp -d "$HOME/.cache/bqaa-lock-diagnostics.XXXXXX")
  cp "$OUT/previous-seed.lock" "$OUT/candidate.lock" "$DIAG/" 2>/dev/null || true
  echo "generator lock did not converge within ${MAX_PHASES} phases" >&2
  echo "diagnostic locks preserved in: ${DIAG}" >&2
  exit 1
fi
# --convergence-end--

echo "==> downstream locks (fresh env from the CONVERGED generator lock)"
"$DOCKER" run --rm -v "$REPO_ROOT:/src:ro" -v "$OUT:/out" "$BASE" bash -c "
  set -euo pipefail
  pip install -q --require-hashes -r /out/pip-tools.lock
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
COMMITTED=0
BACKED_UP=()
rollback() {
  # Armed ONLY until every destination rename succeeded: after the
  # commit point, an interrupted best-effort cleanup must never undo a
  # completed transaction (reproduced in review: TERM during cleanup
  # left one new + two old locks with exit 0). Restore ONLY the backups
  # THIS invocation created: a post-commit interruption of a previous
  # run can strand stale .bqaa-bak files, and restoring one of those
  # for a destination this run never touched would mix lock
  # generations (#356 round 12). Stale backups are left in place for
  # forensics; a later successful run's cleanup removes them.
  if [ "$COMMITTED" = "0" ]; then
    if [ "${#BACKED_UP[@]}" -gt 0 ]; then
      for dest in "${BACKED_UP[@]}"; do
        [ -f "$dest.bqaa-bak" ] && mv -f "$dest.bqaa-bak" "$dest"
      done
    fi
    for dest in "$DEST_PIP_TOOLS" "$DEST_REQUIREMENTS" "$DEST_BUILD"; do
      rm -f "$dest.bqaa-new"
    done
  fi
}
on_int()  { rollback; exit 130; }
on_term() { rollback; exit 143; }
trap 'rollback' ERR
trap 'on_int' INT
trap 'on_term' TERM
for pair in "pip-tools.lock:$DEST_PIP_TOOLS" \
            "requirements.lock:$DEST_REQUIREMENTS" \
            "build-requirements.lock:$DEST_BUILD"; do
  src_name="${pair%%:*}"; dest="${pair#*:}"
  cp "$OUT/$src_name" "$dest.bqaa-new"   # stage next to the destination
  cp "$dest" "$dest.bqaa-bak"            # keep the original for rollback
  BACKED_UP+=("$dest")                   # rollback restores ONLY these
done
for dest in "$DEST_PIP_TOOLS" "$DEST_REQUIREMENTS" "$DEST_BUILD"; do
  mv -f "$dest.bqaa-new" "$dest"
done
COMMITTED=1
trap - ERR INT TERM
# Best-effort cleanup AFTER the commit point: a signal here can strand
# .bqaa-bak files but can never mix lock generations.
for dest in "$DEST_PIP_TOOLS" "$DEST_REQUIREMENTS" "$DEST_BUILD"; do
  rm -f "$dest.bqaa-bak"
done
# --copyback-end--
echo "locks regenerated — review the diffs before committing"
