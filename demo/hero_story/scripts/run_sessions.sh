#!/usr/bin/env bash
# Deterministic demo sessions: one real Claude Code and one real Codex
# session, both tagged with a fresh DEMO_RUN_ID (carried as the `env`
# resource attribute — the query boundary for the whole SQL pack), with a
# landing assertion so Act 3 never opens on an empty dashboard.
#
# Usage:
#   PROJECT=<proj> DATASET=<ds> ENDPOINT=<receiver-url> run_sessions.sh
# Optional: DEMO_RUN_ID (default: generated), EVIDENCE_DIR, TEAM, COST_CENTER
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
DATASET="${DATASET:?set DATASET}"
ENDPOINT="${ENDPOINT:?set ENDPOINT (receiver base URL)}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$HERE/evidence}"
DEMO_RUN_ID="${DEMO_RUN_ID:-hero$(date +%Y%m%d%H%M%S)}"
TEAM="${TEAM:-platform}"
COST_CENTER="${COST_CENTER:-demo-001}"

# Same identifier rules as the product CLI: these values are interpolated
# into BigQuery SQL identifiers below.
python3 -c "
import re, sys
project, dataset, run_id = sys.argv[1], sys.argv[2], sys.argv[3]
if not re.fullmatch(r'[a-z0-9.:-]+', project, re.IGNORECASE):
    sys.exit('invalid GCP project id %r' % project)
if not re.fullmatch(r'\\w+', dataset, re.ASCII):
    sys.exit('invalid BigQuery dataset id %r' % dataset)
if not re.fullmatch(r'\\w+', run_id, re.ASCII):
    sys.exit('invalid DEMO_RUN_ID %r (word characters only)' % run_id)
" "$PROJECT" "$DATASET" "$DEMO_RUN_ID" || exit 2

# The exact scripted prompts — sql/05 searches for these strings verbatim.
CLAUDE_PROMPT="Summarize in one sentence why unified telemetry matters for platform teams."
CODEX_PROMPT="Summarize in one sentence why unified telemetry matters for platform teams (codex)."

mkdir -p "$EVIDENCE_DIR"
TOKEN=$(gcloud secrets versions access latest --secret=bqaa-otlp-token --project "$PROJECT")

# ---- version capture (Codex OTel behavior is version-sensitive, #317) ------
{
  echo "DEMO_RUN_ID: $DEMO_RUN_ID"
  echo "timestamp_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "project: $PROJECT | dataset: $DATASET"
  echo "bqaa_commit: $(git -C "$HERE/../.." rev-parse --short HEAD 2>/dev/null || echo 'installed-package')"
  echo "gcloud: $(gcloud version 2>/dev/null | head -1)"
  echo "bq: $(bq version 2>/dev/null | head -1)"
  echo "claude: $(claude --version 2>/dev/null | head -1)"
  echo "codex: $(codex --version 2>/dev/null | head -1)"
  echo "signals: logs,metrics,traces | privacy: baseline"
  echo "scripted_prompt_claude: $CLAUDE_PROMPT"
  echo "scripted_prompt_codex: $CODEX_PROMPT"
} > "$EVIDENCE_DIR/versions.txt"

# ---- privacy beat capture: the replay refusal (expected exit 2) ------------
set +e
python3 -m bigquery_agent_analytics_tracing.otlp.cli config \
  --endpoint "$ENDPOINT" --source claude-code --privacy replay \
  --out "$EVIDENCE_DIR/should-not-exist" > /dev/null 2> "$EVIDENCE_DIR/replay_refusal.txt"
REFUSAL_RC=$?
set -e
if [ "$REFUSAL_RC" -ne 2 ]; then
  echo "ERROR: replay refusal returned rc=$REFUSAL_RC (expected 2)"; exit 1
fi
echo "privacy gate captured (exit 2) -> evidence/replay_refusal.txt"

# ---- Claude Code session (env vars == generated managed settings) ----------
echo "==> Claude Code session (baseline privacy, traces tier)"
(
  export CLAUDE_CODE_ENABLE_TELEMETRY=1
  export OTEL_LOGS_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_TRACES_EXPORTER=otlp
  export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_ENDPOINT="$ENDPOINT"
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ${TOKEN},x-bqaa-source-product=claude_code"
  export OTEL_RESOURCE_ATTRIBUTES="env=${DEMO_RUN_ID},department=${TEAM},cost_center=${COST_CENTER}"
  export OTEL_LOGS_EXPORT_INTERVAL=2000 OTEL_METRIC_EXPORT_INTERVAL=5000
  cd "$EVIDENCE_DIR"
  claude -p "$CLAUDE_PROMPT" --model haiku < /dev/null 2>&1 | tail -1
  sleep 10  # let the exporters flush the final batch — do not shorten
)

# ---- Codex session (generated artifact + run-id environment override) ------
echo "==> Codex session (isolated CODEX_HOME; your real ~/.codex is untouched)"
CODEX_DEMO_HOME="$EVIDENCE_DIR/codex_home"
mkdir -p "$CODEX_DEMO_HOME"
cp "${CODEX_HOME:-$HOME/.codex}/auth.json" "$CODEX_DEMO_HOME/"
# Start from the bootstrap-generated artifact: fill the token, then carry the
# run id in `environment` (codex cannot set arbitrary resource attributes;
# `environment` lands as the `env` resource attribute — the query boundary).
ARTIFACTS_DIR="${ARTIFACTS_DIR:-$EVIDENCE_DIR/artifacts}"
if [ ! -f "$ARTIFACTS_DIR/codex.config.toml" ]; then
  echo "ERROR: $ARTIFACTS_DIR/codex.config.toml not found."
  echo "  Generate it first (either of):"
  echo "    bqaa-otel bootstrap ... --out $ARTIFACTS_DIR --execute"
  echo "    bqaa-otel config --endpoint $ENDPOINT --source claude-code,codex \\"
  echo "      --signals logs,metrics,traces --out $ARTIFACTS_DIR"
  exit 1
fi
sed -e "s/<token>/${TOKEN}/g" \
    -e "s/environment = \"prod\"/environment = \"${DEMO_RUN_ID}\"/" \
    "$ARTIFACTS_DIR/codex.config.toml" > "$CODEX_DEMO_HOME/config.toml"
(
  cd "$EVIDENCE_DIR"
  CODEX_HOME="$CODEX_DEMO_HOME" codex exec --skip-git-repo-check -s read-only \
    "$CODEX_PROMPT" < /dev/null 2>&1 | tail -1
  sleep 10
)

# ---- landing assertion: never open Act 3 on an empty dashboard -------------
echo "==> Waiting for rows to land (logs, tokens, spans; up to ~4 min)"
QUALIFIED="${PROJECT}.${DATASET}"
check() { bq query --project_id="$PROJECT" --headless --use_legacy_sql=false --format=csv "$1" 2>/dev/null | tail -1; }
for i in $(seq 1 16); do
  LOGS=$(check "SELECT COUNT(*) FROM \`${QUALIFIED}.otel_logs_dedup\` WHERE JSON_VALUE(resource_attributes,'\$.env')='${DEMO_RUN_ID}'")
  SPANS=$(check "SELECT COUNT(*) FROM \`${QUALIFIED}.otel_spans_dedup\` WHERE JSON_VALUE(resource_attributes,'\$.env')='${DEMO_RUN_ID}'")
  TOKENS=$(check "SELECT CAST(COALESCE(SUM(t),0) AS INT64) FROM (
      SELECT CAST(value AS FLOAT64) AS t FROM \`${QUALIFIED}.otel_metric_sum_dedup\` WHERE metric_name='claude_code.token.usage' AND JSON_VALUE(resource_attributes,'\$.env')='${DEMO_RUN_ID}'
      UNION ALL SELECT \`sum\` FROM \`${QUALIFIED}.otel_metric_histogram_dedup\` WHERE metric_name='codex.turn.token_usage' AND JSON_VALUE(resource_attributes,'\$.env')='${DEMO_RUN_ID}')")
  echo "  poll $i: logs=${LOGS:-0} spans=${SPANS:-0} tokens=${TOKENS:-0}"
  if [ "${LOGS:-0}" -gt 0 ] && [ "${SPANS:-0}" -gt 0 ] && [ "${TOKENS:-0}" -gt 0 ]; then
    break
  fi
  sleep 15
done
if [ "${LOGS:-0}" -eq 0 ] || [ "${SPANS:-0}" -eq 0 ] || [ "${TOKENS:-0}" -eq 0 ]; then
  echo "ERROR: rows did not land (logs=${LOGS:-0} spans=${SPANS:-0} tokens=${TOKENS:-0}) — do not proceed to Act 3"
  exit 1
fi

echo "$DEMO_RUN_ID" > "$EVIDENCE_DIR/DEMO_RUN_ID"
echo
echo "DEMO_RUN_ID=$DEMO_RUN_ID   (persisted to evidence/DEMO_RUN_ID)"
echo "Next: scripts/run_queries.sh"
