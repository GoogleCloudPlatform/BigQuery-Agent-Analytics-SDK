# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Negative regression tests for the public-demo SQL policy lint.

Review of the Grafana public demo raised three adversarial queries the lint
accepted. Two are now rejected and stay rejected here:

1. the required 72-hour predicate present only inside a `/* ... */` block
   comment, with a wider bound the one that actually runs;
2. a correctly-placeholdered table UNIONed with a real project path such as
   `customer-prod.private.agent_events` — quoted, bare, or quoted identifier by
   identifier, since BigQuery reads all of those as the same table.

The third, `WHERE <required predicate> OR TRUE`, is not caught today. It is a
gap, not a permitted pattern: such a query is unbounded and must fail review.
`test_or_true_mutant_is_not_caught_today` pins that so it stays visible.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "scripts" / "check_grafana_queries_sync.py"
PUBLIC_DEMO_QUERIES = ROOT / "grafana" / "queries" / "public-demo"


def _load_check():
  spec = importlib.util.spec_from_file_location(
      "check_grafana_queries_sync", CHECK_SCRIPT
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


check = _load_check()

# A query that satisfies every public-demo convention: one backticked
# placeholder table path, both halves of the half-open 72-hour window, no
# Grafana interpolation. Each mutant below is this query with one edit, so a
# failing assertion points at that edit and nothing else.
BASELINE_QUERY = """SELECT COUNT(DISTINCT session_id) AS sessions
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
HAVING COUNT(*) > 0
"""

# Mutant 1: the required window sits in a block comment while a wider 70-hour
# bound is what BigQuery would run. A substring search over the raw text sees
# the required predicate and passes; `active_sql()` must strip the block first.
BLOCK_COMMENT_MUTANT = """SELECT COUNT(DISTINCT session_id) AS sessions
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_events`
/* WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
     AND timestamp < CURRENT_TIMESTAMP() */
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 70 HOUR)
"""

# Mutant 2: a real project/dataset/table alongside the placeholders. The
# placeholder-count check passes — every occurrence of the placeholder is still
# backticked — so only the foreign-path scan can catch this one.
FOREIGN_PATH_MUTANT = """SELECT session_id
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()

UNION ALL

SELECT session_id
FROM `customer-prod.private.agent_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
"""

# Backticks are optional in BigQuery when the identifiers need no escaping, so
# the bare form names the same real table and must be rejected the same way.
FOREIGN_PATH_MUTANT_UNQUOTED = FOREIGN_PATH_MUTANT.replace(
    "`customer-prod.private.agent_events`", "customer-prod.private.agent_events"
)

# Quoting each identifier separately puts the dots outside the backticks, which
# is the same table again in a form neither scan used to read. Searching between
# two backticks finds the bare `.` separators, not a path, so the lint used to
# reject this file while reporting `['.']` and never naming the real project.
FOREIGN_PATH_MUTANT_SEGMENT_QUOTED = FOREIGN_PATH_MUTANT.replace(
    "`customer-prod.private.agent_events`",
    "`customer-prod`.`private`.`agent_events`",
)

# Mixed quoting: one identifier quoted, the dots bare. Nothing used to see this
# path at all — the file failed only because its extra UNION arm unbalanced the
# predicate count, which says nothing about the table it names.
FOREIGN_PATH_MUTANT_MIXED_QUOTED = FOREIGN_PATH_MUTANT.replace(
    "`customer-prod.private.agent_events`",
    "customer-prod.`private`.agent_events",
)

# Mutant 3: both halves of the window are present and uncommented, so the lint
# counts them and passes, even though `OR TRUE` makes the scan unbounded.
OR_TRUE_MUTANT = """SELECT COUNT(DISTINCT session_id) AS sessions
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_events`
WHERE (timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP())
  OR TRUE
"""


def _lint(query: str, filename: str = "mutant.sql") -> int:
  """Run the public-demo SQL policy over one in-memory query.

  `check_public_demo_queries()` reads the text it is handed, so a mutant never
  has to be written to disk.
  """

  return check.check_public_demo_queries({filename: query})


def test_baseline_query_passes():
  """Guards the mutants: each one differs from this by a single edit."""

  assert _lint(BASELINE_QUERY) == 0


def test_shipped_public_demo_queries_pass():
  """The 18 queries that actually ship must satisfy their own policy."""

  shipped = {
      path.name: path.read_text(encoding="utf-8")
      for path in sorted(PUBLIC_DEMO_QUERIES.glob("*.sql"))
  }
  assert shipped, f"no .sql files found in {PUBLIC_DEMO_QUERIES}"
  assert check.check_public_demo_queries(shipped) == 0


def test_block_comment_mutant_is_rejected(capsys):
  """A 72-hour bound parked in `/* ... */` must not satisfy the window check."""

  errors = _lint(BLOCK_COMMENT_MUTANT, "mutant_block_comment.sql")
  assert errors > 0

  message = capsys.readouterr().err
  assert "mutant_block_comment.sql" in message
  assert "uncommented" in message
  assert "INTERVAL 72 HOUR" in message


def test_active_sql_strips_block_comments():
  """Unit-level companion to the mutant above, for a clearer failure."""

  stripped = check.active_sql(BLOCK_COMMENT_MUTANT)
  assert "INTERVAL 72 HOUR" not in stripped
  assert "INTERVAL 70 HOUR" in stripped


@pytest.mark.parametrize(
    ("filename", "query"),
    [
        ("mutant_foreign_path_backticked.sql", FOREIGN_PATH_MUTANT),
        ("mutant_foreign_path_bare.sql", FOREIGN_PATH_MUTANT_UNQUOTED),
        (
            "mutant_foreign_path_segment_quoted.sql",
            FOREIGN_PATH_MUTANT_SEGMENT_QUOTED,
        ),
        (
            "mutant_foreign_path_mixed_quoted.sql",
            FOREIGN_PATH_MUTANT_MIXED_QUOTED,
        ),
    ],
)
def test_foreign_table_path_mutant_is_rejected(filename, query, capsys):
  """A real project path beside the placeholders must fail and be named.

  Quoted, bare, or quoted identifier by identifier: every spelling of the same
  table has to reach the author as the path they wrote, not as a fragment of it.
  """

  assert _lint(query, filename) > 0

  message = capsys.readouterr().err
  assert filename in message
  assert "customer-prod.private.agent_events" in message


def test_unquoted_table_path_needs_backticks_stripped():
  """Unit-level companion: the regex only reads a mixed path once quotes go.

  The capture begins at an identifier character, so a backtick immediately
  after `FROM ` ends the search. Stripping backticks is what the call site does
  about that, and this pins both halves of it.
  """

  mixed = "FROM customer-prod.`private`.agent_events"
  assert check.UNQUOTED_TABLE_PATH.findall(mixed) == []
  assert check.UNQUOTED_TABLE_PATH.findall(mixed.replace("`", "")) == [
      "customer-prod.private.agent_events"
  ]


def test_or_true_mutant_is_not_caught_today():
  """`WHERE <bound> OR TRUE` passes the lint. That is a gap, not a licence.

  Such a query is unbounded and must still fail review. If a future change
  makes the lint reject it, replace this test with one asserting rejection and
  update the CI-scope note in `grafana/queries/public-demo/README.md`.
  """

  assert _lint(OR_TRUE_MUTANT, "mutant_or_true.sql") == 0


def test_readme_documents_the_or_true_gap():
  """Keep the uncaught case documented where an editor of the SQL will see it."""

  readme = (PUBLIC_DEMO_QUERIES / "README.md").read_text(encoding="utf-8")
  assert "OR TRUE" in readme
