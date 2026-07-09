#!/usr/bin/env bash
# Fresh, repeatable demo setup: preflight -> bootstrap (fresh dataset) ->
# inventory -> deterministic sessions. Idempotent: bootstrap converges, so
# re-running against the same dataset resumes rather than duplicating.
#
# Usage: PROJECT=<proj> [DATASET=bqaa_hero_demo_<date>] [REGION=us-central1] reset_demo.sh
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
DATASET="${DATASET:-bqaa_hero_demo_$(date +%Y%m%d)}"
REGION="${REGION:-us-central1}"
BQ_LOCATION="${BQ_LOCATION:-US}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$HERE/evidence}"
export PROJECT DATASET REGION BQ_LOCATION EVIDENCE_DIR

echo "==> Preflight (fails fast; nothing has been mutated)"
"$HERE/scripts/preflight.sh" "$PROJECT" "$DATASET"

echo
echo "==> Bootstrap plan (mutates nothing)"
python3 -m bigquery_agent_analytics_tracing.otlp.cli bootstrap \
  --project "$PROJECT" --dataset "$DATASET" --region "$REGION" \
  --bq-location "$BQ_LOCATION" \
  --build-from-source \
  --signals logs,metrics,traces --source claude-code,codex \
  --out "$EVIDENCE_DIR/artifacts" | tee "$EVIDENCE_DIR/bootstrap_plan.txt" | tail -3

echo
echo "==> Bootstrap execute (fresh install ~15 min incl. Cloud Build; converges on re-run)"
python3 -m bigquery_agent_analytics_tracing.otlp.cli bootstrap \
  --project "$PROJECT" --dataset "$DATASET" --region "$REGION" \
  --bq-location "$BQ_LOCATION" \
  --build-from-source \
  --signals logs,metrics,traces --source claude-code,codex \
  --out "$EVIDENCE_DIR/artifacts" --execute

echo
echo "==> Recording the resource inventory (feeds teardown.sh)"
"$HERE/scripts/write_inventory.sh"

echo
echo "==> Deterministic demo sessions (both products, per-product landing gates)"
ENDPOINT=$(gcloud run services describe bqaa-otlp-receiver --project "$PROJECT" \
  --region "$REGION" --format='value(status.url)')
ENDPOINT="$ENDPOINT" "$HERE/scripts/run_sessions.sh"

echo
echo "Demo is ready. Next: scripts/run_queries.sh   (teardown: scripts/teardown.sh)"
