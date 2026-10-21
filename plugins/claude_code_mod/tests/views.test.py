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

"""Offline test of the usage views in sql/views/, on a table several writers share.

Runs both views over a fixture with this mod's rows and other producers' rows
(an ADK-style writer with its own token names, the settings-hook plugin), a
duplicate, another event type and a row without event_id. It checks that
requests, sessions and token totals all count the same rows: this mod's.

The views are BigQuery SQL. DuckDB runs them after five dialect
substitutions, each of which must match (a change to the views that a
substitution no longer fits fails here instead of passing untested):
the backticked table and view names, the OPTIONS clause, JSON_VALUE (DuckDB's
json_extract_string), SAFE_CAST ... INT64 (TRY_CAST ... BIGINT) and DATE().
QUALIFY, the window, COUNT(DISTINCT), COALESCE and GROUP BY run as written.

Needs DuckDB for Python (python3 -m pip install duckdb, or PYTHON pointing at
an interpreter that has it). Run from anywhere:
    python3 tests/views.test.py
Exit 0: every check passed; 1: a check failed; 77: DuckDB is missing.
"""

import json
import pathlib
import re
import sys

try:
    import duckdb
except ImportError:
    print('skip - DuckDB for Python is not installed (python3 -m pip install duckdb)')
    sys.exit(77)

PLUGIN = pathlib.Path(__file__).resolve().parent.parent
MOD = 'bigquery-agent-analytics-mod'
passed = 0
failed = 0


def check(what, ok, detail=''):
    global passed, failed
    if ok:
        passed += 1
        print(f'ok - {what}')
    else:
        failed += 1
        print(f'not ok - {what}{": " + detail if detail else ""}')


def substitute(sql, pattern, replacement, what, flags=0):
    out, n = re.subn(pattern, replacement, sql, flags=flags)
    if n == 0:
        raise SystemExit(f'not ok - the {what} substitution found nothing to change; update tests/views.test.py with the views')
    return out


def as_duckdb(sql):
    """The view's BigQuery SQL in DuckDB's dialect, as setup.sh renders it."""
    for placeholder, name in (('{PROJECT}', 'p'), ('{VIEWS_DATASET}', 'reports'), ('{DATASET}', 'd'), ('{TABLE}', 'agent_events')):
        sql = sql.replace(placeholder, name)
    sql = substitute(sql, r'`p\.d\.agent_events`', 'agent_events', 'table name')
    sql = substitute(sql, r'`p\.reports\.(\w+)`', r'\1', 'view name')
    sql = substitute(sql, r"^OPTIONS \(description = '[^']*'\)\n", '', 'OPTIONS clause', re.M)
    sql = substitute(sql, r'\bJSON_VALUE\(', 'json_extract_string(', 'JSON_VALUE')
    sql = substitute(sql, r'\bSAFE_CAST\(([^()]*\([^()]*\)) AS INT64\)', r'TRY_CAST(\1 AS BIGINT)', 'SAFE_CAST')
    sql = substitute(sql, r'\bDATE\(timestamp\)', 'CAST(timestamp AS DATE)', 'DATE()')
    return sql


def usage(**counts):
    return counts


def row(ts, event_id, event_type, session, user, attributes):
    return (ts, event_id, event_type, 'coding-agent', session, user, json.dumps(attributes))


def mod_attributes(model, metadata):
    return {'root_agent_name': 'coding-agent', 'source': 'coding_agent', 'model': model, 'usage_metadata': metadata, 'writer': {'plugin': MOD, 'version': '0.1.0'}}


MOD_USAGE = usage(prompt_tokens=8, completion_tokens=2, total_tokens=10, input_tokens=8, output_tokens=2, cache_read_input_tokens=0, cache_creation_input_tokens=0)
FIXTURE = [
    # This mod: one model request, and the same row sent twice.
    row('2026-10-14 10:00:00', 'a' * 32, 'LLM_RESPONSE', 's1', 'person', mod_attributes('example-model', MOD_USAGE)),
    row('2026-10-14 10:00:00', 'a' * 32, 'LLM_RESPONSE', 's1', 'person', mod_attributes('example-model', MOD_USAGE)),
    # This mod, in a table without event_id (an older schema): kept.
    row('2026-10-14 13:00:00', None, 'LLM_RESPONSE', 's1', 'person', mod_attributes('example-model', usage(prompt_tokens=3, completion_tokens=1, total_tokens=4, input_tokens=3, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))),
    # This mod, another day, model and user.
    row('2026-10-15 09:00:00', 'e' * 32, 'LLM_RESPONSE', 's3', 'other', mod_attributes('other-model', usage(prompt_tokens=6, completion_tokens=1, total_tokens=7, input_tokens=5, output_tokens=1, cache_read_input_tokens=1, cache_creation_input_tokens=0))),
    # This mod, not a model response.
    row('2026-10-14 12:00:00', 'd' * 32, 'TOOL_COMPLETED', 's1', 'person', {'writer': {'plugin': MOD}}),
    # Another producer with the ADK token names, and no writer marker.
    row('2026-10-14 11:00:00', 'b' * 32, 'LLM_RESPONSE', 's2', 'person', {'model': 'example-model', 'usage_metadata': {'prompt_token_count': 60, 'candidates_token_count': 40, 'total_token_count': 100}}),
    # The settings-hook plugin, which names its tokens as this mod does.
    row('2026-10-14 11:30:00', 'c' * 32, 'LLM_RESPONSE', 's4', 'person', {'model': 'example-model', 'usage_metadata': {'prompt_tokens': 70, 'completion_tokens': 30, 'total_tokens': 100}, 'writer': {'plugin': 'bigquery-agent-analytics-tracing'}}),
]


def run(views):
    db = duckdb.connect()
    db.execute('CREATE TABLE agent_events (timestamp TIMESTAMP, event_id VARCHAR, event_type VARCHAR, agent VARCHAR, session_id VARCHAR, user_id VARCHAR, attributes VARCHAR)')
    db.executemany('INSERT INTO agent_events VALUES (?, ?, ?, ?, ?, ?, ?)', FIXTURE)
    out = {}
    for name, sql in views.items():
        db.execute(sql)
        cursor = db.execute(f'SELECT * FROM {name} ORDER BY ALL')
        columns = [c[0] for c in cursor.description]
        out[name] = [{k: (str(v) if k == 'day' else v) for k, v in zip(columns, r)} for r in cursor.fetchall()]
    return out


raw = {name: (PLUGIN / 'sql' / 'views' / f'{name}.sql').read_text() for name in ('daily_usage_by_model', 'daily_usage_by_user')}
results = run({name: as_duckdb(sql) for name, sql in raw.items()})

check('by model: one row per day and model, this mod only', results['daily_usage_by_model'] == [
    {'day': '2026-10-14', 'model': 'example-model', 'model_requests': 2, 'sessions': 1, 'input_tokens': 11, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0, 'output_tokens': 3, 'total_tokens': 14},
    {'day': '2026-10-15', 'model': 'other-model', 'model_requests': 1, 'sessions': 1, 'input_tokens': 5, 'cache_read_input_tokens': 1, 'cache_creation_input_tokens': 0, 'output_tokens': 1, 'total_tokens': 7},
], json.dumps(results['daily_usage_by_model']))
check('by user: one row per day and user, this mod only', results['daily_usage_by_user'] == [
    {'day': '2026-10-14', 'user_id': 'person', 'sessions': 1, 'model_requests': 2, 'input_tokens': 11, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0, 'output_tokens': 3, 'total_tokens': 14},
    {'day': '2026-10-15', 'user_id': 'other', 'sessions': 1, 'model_requests': 1, 'input_tokens': 5, 'cache_read_input_tokens': 1, 'cache_creation_input_tokens': 0, 'output_tokens': 1, 'total_tokens': 7},
], json.dumps(results['daily_usage_by_user']))

# The same views without the writer filter count other producers' requests
# and sessions, but not their tokens: the mismatch the filter removes.
filter_line = re.compile(r"^\s*AND JSON_VALUE\(attributes, '\$\.writer\.plugin'\) = 'bigquery-agent-analytics-mod'\n", re.M)
check('both views filter on the writer marker', all(len(filter_line.findall(sql)) == 1 for sql in raw.values()))
unscoped = run({name: as_duckdb(filter_line.sub('', sql)) for name, sql in raw.items()})
day = next((r for r in unscoped['daily_usage_by_model'] if r['day'] == '2026-10-14'), None)
check('without the filter, other producers raise the counts but not the tokens', day is not None and (day['model_requests'], day['sessions'], day['total_tokens']) == (4, 3, 114), json.dumps(day))

print(f'{passed} passed, {failed} failed')
sys.exit(0 if failed == 0 else 1)
