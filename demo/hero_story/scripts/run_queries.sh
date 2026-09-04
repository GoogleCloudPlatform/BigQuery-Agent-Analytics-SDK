#!/usr/bin/env bash
# Execute the leadership SQL pack for one demo run; print each result and
# write CSVs to evidence/sql/. Requires the run id (argument or the file
# persisted by run_sessions.sh) so stale rows can never pad the numbers.
#
# Usage: PROJECT=<proj> DATASET=<ds> run_queries.sh [DEMO_RUN_ID] [WINDOW_HOURS]
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
DATASET="${DATASET:?set DATASET}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$HERE/evidence}"
RUN_ID="${1:-$(cat "$EVIDENCE_DIR/DEMO_RUN_ID" 2>/dev/null || true)}"
WINDOW="${2:-24}"
[ -n "$RUN_ID" ] || { echo "usage: run_queries.sh <DEMO_RUN_ID> — none given and evidence/DEMO_RUN_ID missing"; exit 2; }

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
" "$PROJECT" "$DATASET" "$RUN_ID" || exit 2


# The exact scripted prompts live in ONE place (sourced, never duplicated):
# sql/05 searches for BOTH verbatim as the privacy proof.
source "$(dirname "$0")/demo_prompts.sh"

mkdir -p "$EVIDENCE_DIR/sql"
echo "SQL pack for DEMO_RUN_ID=$RUN_ID (window ${WINDOW}h)"

# Refresh the projection first: the scheduled MERGE runs every 15 minutes,
# so a demo querying right after its sessions would otherwise find an empty
# event-mix (03). Same idempotent MERGE verify --smoke runs; safe to race.
echo "refreshing agent_events_otlp projection (idempotent MERGE)..."
python3 - "$PROJECT" "$DATASET" << 'MERGE_EOF'
import sys
from bigquery_agent_analytics_tracing.otlp import sql
from google.cloud import bigquery
project, dataset = sys.argv[1], sys.argv[2]
bigquery.Client(project=project).query(
    sql.agent_events_otlp_merge_sql(f"{project}.{dataset}")
).result()
print("projection refreshed")
MERGE_EOF
FAILED=0
for f in "$HERE"/sql/*.sql; do
  name=$(basename "$f" .sql)
  PARAMS=(--parameter="demo_run_id::${RUN_ID}" --parameter="window_hours:INT64:${WINDOW}")
  case "$name" in 05_*) PARAMS+=(
      --parameter="scripted_prompt_claude::${CLAUDE_PROMPT}"
      --parameter="scripted_prompt_codex::${CODEX_PROMPT}"
  );; esac
  echo; echo "=== $name ==="
  if sed "s/\${dataset}/${PROJECT}.${DATASET}/g" "$f" \
      | bq query --project_id="$PROJECT" --headless --use_legacy_sql=false \
          --format=csv "${PARAMS[@]}" > "$EVIDENCE_DIR/sql/${name}.csv" 2> "$EVIDENCE_DIR/sql/${name}.err"; then
    column -s, -t < "$EVIDENCE_DIR/sql/${name}.csv" | head -12
    rm -f "$EVIDENCE_DIR/sql/${name}.err"
  else
    echo "QUERY FAILED — $EVIDENCE_DIR/sql/${name}.err:"; head -3 "$EVIDENCE_DIR/sql/${name}.err"
    FAILED=1
  fi
done
echo
[ "$FAILED" -eq 0 ] && echo "All queries succeeded; CSVs in evidence/sql/." || { echo "Some queries failed."; exit 1; }
