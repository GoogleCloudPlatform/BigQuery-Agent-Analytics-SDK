#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Live: checks the access docs/service-account-auth.md sets up, against a real
# project, as the writer and reader service accounts (impersonated with
# gcloud, so the caller needs roles/iam.serviceAccountTokenCreator on each):
#   the writer may insert rows into the events table (bigquery.tables.get
#     and bigquery.tables.updateData) and nothing else there: it cannot
#     read rows, update or delete the table, or run a query;
#   the reader (optional) can query the views, and cannot read the table,
#     directly or through a query;
#   the project's roles reach neither: the events dataset keeps no
#     projectReaders or projectWriters entry, and the views dataset no
#     projectWriters (as the caller, who must be able to read the datasets).
# Nothing is written, changed or deleted: the table's rights are asked with
# testIamPermissions, and the reads and queries it should be refused are
# tried. With INSERT_PROBE=1 it also inserts one row as the writer (event_type
# ACCESS_CHECK, which the views leave out).
#   PROJECT=my-gcp-project DATASET=agent_analytics \
#   WRITER_SA=bqaa-writer@my-gcp-project.iam.gserviceaccount.com \
#   [READER_SA=...] [TABLE=agent_events] [VIEWS_DATASET=<dataset>_reporting] \
#   bash tests/access.live.sh
set -u
: "${PROJECT:?set PROJECT}" "${DATASET:?set DATASET}" "${WRITER_SA:?set WRITER_SA}"
TABLE="${TABLE:-agent_events}"
VIEWS_DATASET="${VIEWS_DATASET:-${DATASET}_reporting}"
API="https://bigquery.googleapis.com/bigquery/v2/projects/$PROJECT"

passed=0
failed=0
check() { # description, then a command that must succeed
  local what="$1"
  shift
  if "$@"; then passed=$((passed + 1)); echo "ok - $what"; else failed=$((failed + 1)); echo "not ok - $what"; fi
}
token_for() { gcloud auth print-access-token --impersonate-service-account="$1" 2>/dev/null; }
# An HTTP status, from a request with the token given.
status() { # token, method, url, [json body]
  curl -s -o /dev/null -w '%{http_code}' -X "$2" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' ${4:+--data "$4"} "$3"
}
# The table permissions a token holds, of those asked, sorted and comma-joined.
held() { # token, permissions...
  local token="$1"
  shift
  local asked
  asked="$(printf '"%s",' "$@")"
  curl -s -X POST -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    --data "{\"permissions\": [${asked%,}]}" "$API/datasets/$DATASET/tables/$TABLE:testIamPermissions" |
    python3 -c 'import json, sys; print(",".join(sorted(json.load(sys.stdin).get("permissions", []))))'
}
# The special groups a dataset's access names, sorted and comma-joined, or
# "unreadable" when bq cannot show it.
groups() { # dataset
  bq --format=json show "$PROJECT:$1" 2>/dev/null |
    python3 -c 'import json, sys; print(",".join(sorted(e["specialGroup"] for e in json.load(sys.stdin).get("access", []) if "specialGroup" in e)))' 2>/dev/null ||
    echo unreadable
}
query_status() { # token, SQL
  status "$1" POST "$API/queries" "{\"query\": \"$2\", \"useLegacySql\": false, \"location\": \"${LOCATION:-US}\"}"
}
ASKED=(bigquery.tables.get bigquery.tables.updateData bigquery.tables.getData bigquery.tables.update bigquery.tables.delete bigquery.tables.setIamPolicy)

events_groups="$(groups "$DATASET")"
check "the events dataset gives project Viewers and Editors nothing (its groups: ${events_groups:-none})" \
  bash -c "[[ '$events_groups' != unreadable && ',$events_groups,' != *,projectReaders,* && ',$events_groups,' != *,projectWriters,* ]]"
views_groups="$(groups "$VIEWS_DATASET")"
check "the views dataset gives project Editors nothing (its groups: ${views_groups:-none})" \
  bash -c "[[ '$views_groups' != unreadable && ',$views_groups,' != *,projectWriters,* ]]"

writer="$(token_for "$WRITER_SA")"
check "a token for the writer $WRITER_SA" test -n "$writer"
check 'the writer holds exactly bigquery.tables.get and bigquery.tables.updateData on the table' \
  test "$(held "$writer" "${ASKED[@]}")" = "bigquery.tables.get,bigquery.tables.updateData"
check 'the writer cannot read the table'"'"'s rows (tabledata.list: 403)' \
  test "$(status "$writer" GET "$API/datasets/$DATASET/tables/$TABLE/data?maxResults=1")" = 403
check 'the writer cannot run a query (jobs.query: 403)' \
  test "$(query_status "$writer" "SELECT 1")" = 403
if [[ "${INSERT_PROBE:-0}" == 1 ]]; then
  row="{\"rows\": [{\"insertId\": \"access-check-$RANDOM$RANDOM\", \"json\": {\"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"event_type\": \"ACCESS_CHECK\", \"agent\": \"access-check\"}}]}"
  check 'the writer inserts a row (insertAll: 200)' \
    test "$(status "$writer" POST "$API/datasets/$DATASET/tables/$TABLE/insertAll" "$row")" = 200
fi

if [[ -n "${READER_SA:-}" ]]; then
  reader="$(token_for "$READER_SA")"
  check "a token for the reader $READER_SA" test -n "$reader"
  check 'the reader queries a view' \
    test "$(query_status "$reader" "SELECT COUNT(*) FROM \`$PROJECT.$VIEWS_DATASET.daily_usage_by_user\`")" = 200
  check 'the reader cannot query the table' \
    test "$(query_status "$reader" "SELECT COUNT(*) FROM \`$PROJECT.$DATASET.$TABLE\`")" = 403
  check 'the reader cannot read the table'"'"'s rows (tabledata.list: 403)' \
    test "$(status "$reader" GET "$API/datasets/$DATASET/tables/$TABLE/data?maxResults=1")" = 403
  check 'the reader holds no right on the table' test -z "$(held "$reader" "${ASKED[@]}")"
fi

echo "$passed passed, $failed failed"
((failed == 0))
