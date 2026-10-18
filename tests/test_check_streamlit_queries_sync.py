"""Unit and regression tests for scripts/check_streamlit_queries_sync.py."""

import io
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
  sys.path.insert(0, str(SCRIPTS_DIR))

import check_streamlit_queries_sync


def run_check_with_patch(query_filename: str, patched_sql: str):
  original_get_query = check_streamlit_queries_sync.get_streamlit_query

  def mock_get_query(filename, window=None):
    if filename == query_filename:
      return patched_sql
    return original_get_query(filename, window)

  with patch(
      "check_streamlit_queries_sync.get_streamlit_query",
      side_effect=mock_get_query,
  ):
    stderr_capture = io.StringIO()
    with patch("sys.stderr", stderr_capture):
      with pytest.raises(SystemExit) as exc_info:
        check_streamlit_queries_sync.main()
      return exc_info.value.code, stderr_capture.getvalue()


def test_baseline_check():
  """All shipped Streamlit SQL builders match canonical Grafana SQL."""
  stderr_capture = io.StringIO()
  stdout_capture = io.StringIO()
  with patch("sys.stderr", stderr_capture), patch("sys.stdout", stdout_capture):
    with pytest.raises(SystemExit) as exc_info:
      check_streamlit_queries_sync.main()
    assert exc_info.value.code == 0
  expected_msg = (
      f"All {len(check_streamlit_queries_sync.CANONICAL_QUERIES)} Streamlit"
      " dashboard queries match canonical Grafana SQL."
  )
  assert expected_msg in stdout_capture.getvalue()

  # Direct CLI entrypoint (__main__)
  cli_res = subprocess.run(
      [sys.executable, str(SCRIPTS_DIR / "check_streamlit_queries_sync.py")],
      capture_output=True,
      text=True,
      check=False,
  )
  assert cli_res.returncode == 0
  assert expected_msg in cli_res.stdout


@pytest.mark.parametrize(
    ("filename", "old", "new", "expected_token"),
    [
        ("overview_totals.sql", "'_ERROR'", "'_WARNING'", "WARNING"),
        (
            "events_over_time.sql",
            (
                "AND ('___ALL___' IN UNNEST(@session_ids) OR session_id IN"
                " UNNEST(@session_ids))"
            ),
            "",
            "session_ids",
        ),
        (
            "errors_over_time.sql",
            "WHERE ",
            (
                "WHERE AND ('___ALL___' IN UNNEST(@event_types) OR event_type"
                " IN UNNEST(@event_types))\n"
            ),
            "event_types",
        ),
        (
            "llm_latency_percentiles.sql",
            "OFFSET(50)",
            "OFFSET(90)",
            "OFFSET(90)",
        ),
        (
            "tool_usage.sql",
            "`project.dataset.adk_tool_starts`",
            "`project.dataset.adk_tool_completions`",
            "tool_completions",
        ),
        (
            "tool_usage.sql",
            "AS invocations",
            "AS call_count",
            "call_count",
        ),
        (
            "llm_calls_total.sql",
            "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,",
            "",
            "missing required token column usage_prompt_tokens",
        ),
    ],
)
def test_query_drift_detected(filename, old, new, expected_token):
  """Structural or predicate mutations in Streamlit queries fail sync check."""
  original_sql = check_streamlit_queries_sync.get_streamlit_query(filename)
  assert old in original_sql
  altered_sql = original_sql.replace(old, new)
  code, stderr = run_check_with_patch(filename, altered_sql)
  assert code == 1
  assert expected_token in stderr
  if "missing required token column" not in expected_token:
    assert "has drifted from Grafana" in stderr


def test_unmapped_and_missing_query_guards(tmp_path):
  """Guards reject unmapped files, unmapped builders, and missing files."""
  # 1. Canonical query directory coverage (non-existent, clean, and unmapped)
  assert (
      check_streamlit_queries_sync.check_unmapped_canonical_queries(
          tmp_path / "does_not_exist", set()
      )
      == 1
  )
  (tmp_path / "overview_totals.sql").write_text("SELECT 1")
  (tmp_path / "estimated_cost.sql").write_text("SELECT 1")
  assert (
      check_streamlit_queries_sync.check_unmapped_canonical_queries(
          tmp_path, {"overview_totals.sql"}
      )
      == 0
  )

  (tmp_path / "extra_unknown.sql").write_text("SELECT 1")
  (tmp_path / "extra_unknown_2.sql").write_text("SELECT 1")
  assert (
      check_streamlit_queries_sync.check_unmapped_canonical_queries(
          tmp_path, {"overview_totals.sql"}
      )
      == 2
  )

  # 2. Streamlit SQL builder coverage + get_streamlit_query error path
  assert check_streamlit_queries_sync.check_unmapped_streamlit_builders() == 0
  with (
      patch.object(
          check_streamlit_queries_sync.queries,
          "build_brand_new_feature_sql",
          create=True,
          new=lambda refs, window: "SELECT 1",
      ),
      patch.object(
          check_streamlit_queries_sync.queries,
          "build_second_unmapped_sql",
          create=True,
          new=lambda refs, window: "SELECT 1",
      ),
  ):
    assert check_streamlit_queries_sync.check_unmapped_streamlit_builders() == 2

  with pytest.raises(ValueError, match="Unknown query: nonexistent.sql"):
    check_streamlit_queries_sync.get_streamlit_query("nonexistent.sql")

  # 3. Missing canonical query file in main()
  stderr_capture = io.StringIO()
  with (
      patch.dict(
          check_streamlit_queries_sync.CANONICAL_QUERIES,
          {"missing_query.sql": lambda refs, window: "SELECT 1"},
      ),
      patch("sys.stderr", stderr_capture),
  ):
    with pytest.raises(SystemExit) as exc_info:
      check_streamlit_queries_sync.main()
    assert exc_info.value.code != 0
    assert (
        "Missing canonical query missing_query.sql" in stderr_capture.getvalue()
    )


def test_normalize_query_clause_ordering_and_literals():
  """WHERE is order-invariant, SELECT is ordered, and literals keep parens."""
  sql_where_a = """
  SELECT
    tool_name,
    COUNT(*) AS invocations
  FROM `project.dataset.adk_tool_starts`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
    AND ('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))
    AND ('___ALL___' IN UNNEST(@user_ids) OR user_id IN UNNEST(@user_ids))
  GROUP BY tool_name
  HAVING COUNT(*) > 0 AND SUM(1) > 0
  ORDER BY invocations DESC
  """
  sql_where_b = """
  /* Multi-line
     comment */
  SELECT
    tool_name, -- inline comment
    COUNT(*) AS invocations # hash comment
  FROM `project.dataset.adk_tool_starts`
  WHERE ('___ALL___' IN UNNEST(@user_ids) OR user_id IN UNNEST(@user_ids))
    AND timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND ('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  GROUP BY tool_name
  HAVING SUM(1) > 0 AND COUNT(*) > 0
  ORDER BY invocations DESC
  """
  assert check_streamlit_queries_sync.normalize_query(
      sql_where_a, "tool_usage.sql"
  ) == check_streamlit_queries_sync.normalize_query(
      sql_where_b, "tool_usage.sql"
  )

  # SELECT column order, comma splitting, and column count are preserved
  sql_select_ab = """
  SELECT col_a, col_b
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  sql_select_ab_tight = sql_select_ab.replace("col_a, col_b", "col_a,col_b")
  sql_select_ba = """
  SELECT col_b, col_a
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  sql_select_only_a = """
  SELECT col_a
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  norm_ab = check_streamlit_queries_sync.normalize_query(
      sql_select_ab, "dummy.sql"
  )
  assert "SELECT\n  col_a,\n  col_b" in norm_ab
  assert norm_ab == check_streamlit_queries_sync.normalize_query(
      sql_select_ab_tight, "dummy.sql"
  )
  assert norm_ab != check_streamlit_queries_sync.normalize_query(
      sql_select_ba, "dummy.sql"
  )
  assert norm_ab != check_streamlit_queries_sync.normalize_query(
      sql_select_only_a, "dummy.sql"
  )

  # String literals with escaped quotes, nested parens, and case-insensitive AND
  where_body = (
      "col2 = 'val with '' and \\' and (unbalanced AND kw'"
      " and COALESCE(IF(a AND b, 1, 2) AND d, 3) = 1"
      " AND col3 = 1"
  )
  conjuncts = check_streamlit_queries_sync.split_top_level(
      where_body, r"\s+AND\s+"
  )
  assert conjuncts == [
      "col2 = 'val with '' and \\' and (unbalanced AND kw'",
      "COALESCE(IF(a AND b, 1, 2) AND d, 3) = 1",
      "col3 = 1",
  ]


def test_timestamp_bounds_validation_and_quote_tolerance():
  """Single/double quoted bounds normalize identically; bad bounds fail."""
  window = check_streamlit_queries_sync.DEFAULT_WINDOW
  sql_single = """
  SELECT tool_name, COUNT(*) AS invocations
  FROM `project.dataset.adk_tool_starts`
  WHERE timestamp >= TIMESTAMP '2024-12-31 12:00:00+00:00'
    AND timestamp < TIMESTAMP '2025-01-01 12:00:00+00:00'
  GROUP BY tool_name
  """
  sql_double = sql_single.replace("'", '"')

  # Non-UTC window converts to UTC before bound verification
  plus_two = check_streamlit_queries_sync.dt.timezone(
      check_streamlit_queries_sync.dt.timedelta(hours=2)
  )
  win_non_utc = check_streamlit_queries_sync.models.Window(
      start=window.start.astimezone(plus_two),
      end=window.end.astimezone(plus_two),
  )
  check_streamlit_queries_sync.assert_timestamp_bounds(sql_single, win_non_utc)
  check_streamlit_queries_sync.assert_timestamp_bounds(sql_double, window)
  assert check_streamlit_queries_sync.normalize_time_filters(
      sql_single
  ) == check_streamlit_queries_sync.normalize_time_filters(sql_double)

  # Swapped double-quoted bounds or wrong single-quoted start date fail
  for bad_sql in [
      """
      SELECT tool_name
      FROM `project.dataset.adk_tool_starts`
      WHERE timestamp >= TIMESTAMP "2025-01-01 12:00:00+00:00"
        AND timestamp < TIMESTAMP "2024-12-31 12:00:00+00:00"
      """,
      sql_single.replace(
          "2024-12-31 12:00:00+00:00", "2020-01-01 00:00:00+00:00"
      ),
  ]:
    with pytest.raises(
        AssertionError, match="Expected start timestamp literal"
    ):
      check_streamlit_queries_sync.normalize_query(bad_sql, "tool_usage.sql")

  # Wrong end timestamp, disallowed operator, missing bound, or mismatched cols
  with pytest.raises(AssertionError, match="missing timestamp bounds"):
    check_streamlit_queries_sync.assert_streamlit_query(
        "tool_usage.sql", "SELECT 1", window
    )
  for bad_bound_sql, match_msg in [
      (
          sql_single.replace(
              "2025-01-01 12:00:00+00:00", "2025-01-02 00:00:00+00:00"
          ),
          "Expected end timestamp literal",
      ),
      (
          sql_single.replace("timestamp < TIMESTAMP", "timestamp <= TIMESTAMP"),
          "Disallowed operator '<='",
      ),
      (
          "SELECT 1 FROM t WHERE timestamp < TIMESTAMP"
          " '2025-01-01 12:00:00+00:00'",
          "No start timestamp bound",
      ),
      (
          "SELECT 1 FROM t WHERE timestamp >= TIMESTAMP"
          " '2024-12-31 12:00:00+00:00'",
          "No end timestamp bound",
      ),
      (
          sql_single.replace("AND timestamp <", "AND other_ts <"),
          "Mismatched start bounds",
      ),
  ]:
    with pytest.raises(AssertionError, match=match_msg):
      check_streamlit_queries_sync.assert_timestamp_bounds(
          bad_bound_sql, window
      )


def test_framework_adaptations_and_required_columns():
  """Verifies file-scoped IFNULL stripping and required token columns."""
  window = check_streamlit_queries_sync.DEFAULT_WINDOW

  # 1. IFNULL(tool_name, 'unknown') stripped only for tool_usage / tool_latency
  sql_with_ifnull = "SELECT IFNULL(tool_name, 'unknown') AS tool_name FROM foo"
  assert (
      "IFNULL(tool_name, 'unknown')"
      in check_streamlit_queries_sync.normalize_query(
          sql_with_ifnull, "overview_totals.sql"
      )
  )
  for tool_file in ("tool_usage.sql", "tool_latency.sql"):
    norm_tool = check_streamlit_queries_sync.normalize_query(
        sql_with_ifnull, tool_file
    )
    assert "IFNULL(tool_name, 'unknown')" not in norm_tool
    assert "tool_name" in norm_tool
    with pytest.raises(
        AssertionError, match="IFNULL\\(tool_name, 'unknown'\\)"
    ):
      check_streamlit_queries_sync.assert_streamlit_query(
          tool_file,
          """
          SELECT tool_name
          FROM `project.dataset.adk_tool_starts`
          WHERE timestamp >= TIMESTAMP '2024-12-31 12:00:00+00:00'
            AND timestamp < TIMESTAMP '2025-01-01 12:00:00+00:00'
          """,
          window,
      )

  # Whitespace and quote tolerance in assert_streamlit_query
  check_streamlit_queries_sync.assert_streamlit_query(
      "tool_usage.sql",
      """
      SELECT IFNULL(  tool_name  ,  "unknown"  ) AS tool_name,
        COUNT(*) AS invocations
      FROM `project.dataset.adk_tool_starts`
      WHERE timestamp >= TIMESTAMP '2024-12-31 12:00:00+00:00'
        AND timestamp < TIMESTAMP '2025-01-01 12:00:00+00:00'
      GROUP BY tool_name
      """,
      window,
  )

  # 2. llm_calls_total.sql requires all 3 token columns and normalizes spacing
  original_llm = check_streamlit_queries_sync.get_streamlit_query(
      "llm_calls_total.sql"
  )
  for col, alias, snippet in [
      (
          "usage_prompt_tokens",
          "prompt_tokens",
          "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,",
      ),
      (
          "usage_completion_tokens",
          "completion_tokens",
          "IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,",
      ),
      (
          "usage_total_tokens",
          "total_tokens",
          "IFNULL(SUM(usage_total_tokens), 0) AS total_tokens",
      ),
  ]:
    bad_sql = original_llm.replace(snippet, "")
    with pytest.raises(
        AssertionError, match=f"missing required token column {col} as {alias}"
    ):
      check_streamlit_queries_sync.assert_streamlit_query(
          "llm_calls_total.sql", bad_sql, window
      )

  grafana_llm = (
      check_streamlit_queries_sync.QUERIES_DIRECTORY / "llm_calls_total.sql"
  ).read_text(encoding="utf-8")
  whitespace_variant_sql = (
      "SELECT\n"
      "  COUNT(DISTINCT CONCAT(trace_id, '|', span_id))\n"
      "    + COUNTIF(trace_id IS NULL OR span_id IS NULL) AS llm_calls ,\n"
      "  IFNULL (  SUM (  usage_prompt_tokens  )  ,  0  )   AS   prompt_tokens"
      "  ,\n"
      "  IFNULL( SUM( usage_completion_tokens ) , 0 ) AS completion_tokens ,\n"
      "  IFNULL  (  SUM  (  usage_total_tokens  )  ,  0  )  AS  total_tokens\n"
      "FROM `project.dataset.adk_llm_responses`\n"
      'WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"\n'
      '  AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"\n'
      "  AND ('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))\n"
      "  AND ('___ALL___' IN UNNEST(@user_ids) OR user_id IN"
      " UNNEST(@user_ids))\n"
      "  AND ('___ALL___' IN UNNEST(@session_ids) OR session_id IN"
      " UNNEST(@session_ids))\n"
      "HAVING COUNT(*) > 0\n"
  )
  check_streamlit_queries_sync.assert_streamlit_query(
      "llm_calls_total.sql", whitespace_variant_sql, window
  )
  assert check_streamlit_queries_sync.normalize_query(
      whitespace_variant_sql, "llm_calls_total.sql", window
  ) == check_streamlit_queries_sync.normalize_query(
      grafana_llm, "llm_calls_total.sql", window
  )
