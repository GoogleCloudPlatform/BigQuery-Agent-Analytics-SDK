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

# Offline test of setup.sh: a stand-in `bq` on PATH logs every call (and the
# SQL each query reads), so nothing reaches Google Cloud. Run from anywhere:
#   bash tests/setup.test.sh
set -euo pipefail

plugin="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
cat >"$work/bin/bq" <<'BQ'
#!/usr/bin/env bash
# Logs its arguments, and for a query the SQL it reads; `show` finds a
# dataset only if it is listed in $BQ_EXISTING.
printf 'bq %s\n' "$*" >>"$BQ_LOG"
for arg in "$@"; do
  if [[ "$arg" == query ]]; then { echo '--- sql'; cat; echo '--- end'; } >>"$BQ_LOG"; fi
done
if [[ " $* " == *" show "* ]]; then
  for name in ${BQ_EXISTING:-}; do [[ " $* " == *":$name "* || "$*" == *":$name" ]] && exit 0; done
  exit 1
fi
exit 0
BQ
chmod +x "$work/bin/bq"

passed=0
failed=0
check() { # description, then a command that must succeed
  local what="$1"
  shift
  if "$@"; then passed=$((passed + 1)); echo "ok - $what"; else failed=$((failed + 1)); echo "not ok - $what"; fi
}
has() { grep -qF -- "$1" "$2"; }
lacks() { ! grep -qF -- "$1" "$2"; }

run_setup() { # log file, then environment assignments, then setup.sh arguments
  local log="$1"
  shift
  : >"$log"
  env -i PATH="$work/bin:/usr/bin:/bin" BQ_LOG="$log" "$@" bash "$plugin/setup.sh" ${SETUP_ARGS:-} >"$log.out" 2>"$log.err"
}

# 1. A first run creates both datasets, the table and the two views.
log="$work/first.log"
check 'a first run succeeds' run_setup "$log" BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics
check 'it creates the events dataset in US' has 'bq --project_id=my-gcp-project --location=US mk --dataset' "$log"
check 'it creates the views dataset' has 'my-gcp-project:agent_analytics_reporting' "$log"
check 'the table DDL names the table' has 'CREATE TABLE IF NOT EXISTS `my-gcp-project.agent_analytics.agent_events`' "$log"
check 'the table gets its event_id column' has 'ADD COLUMN IF NOT EXISTS event_id STRING' "$log"
check 'the model view is made in the views dataset' has 'CREATE OR REPLACE VIEW `my-gcp-project.agent_analytics_reporting.daily_usage_by_model`' "$log"
check 'the user view is made in the views dataset' has 'CREATE OR REPLACE VIEW `my-gcp-project.agent_analytics_reporting.daily_usage_by_user`' "$log"
check 'the views read the events table' has 'FROM `my-gcp-project.agent_analytics.agent_events`' "$log"
check "the views keep to this mod's rows" test "$(grep -cF "AND JSON_VALUE(attributes, '\$.writer.plugin') = 'bigquery-agent-analytics-mod'" "$log")" -eq 2
check 'no placeholder is left' lacks '{' "$log"
check 'queries are standard SQL in the location' has 'query --use_legacy_sql=false --location=US' "$log"
check 'three queries in all: the table, then the two views' test "$(grep -c -- '--- sql' "$log")" -eq 3

# 2. Run again with both datasets there: nothing is made but the views.
log="$work/again.log"
check 'a second run succeeds' run_setup "$log" BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics BQ_EXISTING='agent_analytics agent_analytics_reporting'
check 'no dataset is created again' lacks 'mk --dataset' "$log"
check 'the table DDL runs again (CREATE ... IF NOT EXISTS)' has 'CREATE TABLE IF NOT EXISTS' "$log"

# 3. Other names, and the fallbacks the mod reads.
log="$work/names.log"
check 'fallback variables and a views dataset of its own' run_setup "$log" GOOGLE_CLOUD_PROJECT=other-project BQ_DATASET=events BQ_TABLE=coding_events BQAA_LOCATION=EU BQAA_VIEWS_DATASET=reports
check 'the table comes from BQ_TABLE' has '`other-project.events.coding_events`' "$log"
check 'the views go to BQAA_VIEWS_DATASET' has '`other-project.reports.daily_usage_by_model`' "$log"
check 'the location is EU' has '--location=EU mk --dataset' "$log"
check 'the table option is printed when not the default' has '  table: coding_events' "$log.out"

# 4. A dry run calls bq for nothing and prints the SQL.
log="$work/dry.log"
check 'a dry run succeeds' env SETUP_ARGS=--dry-run bash -c "$(declare -f run_setup); work='$work'; plugin='$plugin'; run_setup '$log' BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics"
check 'a dry run makes no bq call' test ! -s "$log"
check 'a dry run prints the view SQL' has 'CREATE OR REPLACE VIEW `my-gcp-project.agent_analytics_reporting.daily_usage_by_model`' "$log.out"

# 5. Names that are not plain names, and missing settings, stop it before any call.
for bad in 'BQAA_DATASET=x;DROP' 'BQAA_DATASET=a.b' 'BQAA_TABLE=t`s' 'BQAA_VIEWS_DATASET=v-w' 'BQAA_PROJECT_ID=My_Project' 'BQAA_LOCATION=us east'; do
  log="$work/bad.log"
  check "refused: $bad" bash -c "$(declare -f run_setup); work='$work'; plugin='$plugin'; ! run_setup '$log' BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics '$bad'"
  check "no bq call for: $bad" test ! -s "$log"
done
log="$work/none.log"
check 'no project: refused' bash -c "$(declare -f run_setup); work='$work'; plugin='$plugin'; ! run_setup '$log' BQAA_DATASET=agent_analytics"
check 'no project: says which variable' has 'set BQAA_PROJECT_ID' "$log.err"
check 'an unknown argument is refused' bash -c "! bash '$plugin/setup.sh' --force 2>/dev/null"

# 6. The command the mod prints when BigQuery cannot find the dataset (the
# shape tests/destination.test.ts holds it to) sets every name setup.sh
# reads: a views dataset, project, dataset or table already set in the shell
# that runs it, valid or not, changes nothing.
printed="BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics BQAA_TABLE=agent_events BQAA_VIEWS_DATASET=agent_analytics_reporting BQAA_LOCATION=US bash '$plugin/setup.sh'"
for inherited in ambient_reports v-w; do
  log="$work/printed-$inherited.log"
  : >"$log"
  check "the printed command runs with BQAA_VIEWS_DATASET=$inherited in the shell" env -i PATH="$work/bin:/usr/bin:/bin" BQ_LOG="$log" BQAA_VIEWS_DATASET="$inherited" GCP_PROJECT_ID=other-project BQ_DATASET=other_dataset BQ_TABLE=other_table bash -c "$printed >'$log.out' 2>'$log.err'"
  check "  its views go to agent_analytics_reporting" has 'CREATE OR REPLACE VIEW `my-gcp-project.agent_analytics_reporting.daily_usage_by_user`' "$log"
  check "  nothing goes to $inherited" lacks "$inherited" "$log"
  check "  nothing goes to the shell's project, dataset or table" bash -c "! grep -qE 'other-project|other_dataset|other_table' '$log'"
done

echo "$passed passed, $failed failed"
((failed == 0))
