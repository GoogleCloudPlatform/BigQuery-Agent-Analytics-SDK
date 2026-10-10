"""Unit and regression tests for scripts/check_streamlit_queries_sync.py."""

import io
from pathlib import Path
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
  assert (
      f"All {len(check_streamlit_queries_sync.CANONICAL_QUERIES)} Streamlit"
      " dashboard queries match canonical Grafana SQL."
      in stdout_capture.getvalue()
  )


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


def test_unmapped_and_missing_query_guards(tmp_path):
  """Guards reject unmapped files, unmapped builders, and missing files."""
  # 1. Canonical query directory coverage
  (tmp_path / "overview_totals.sql").write_text("SELECT 1")
  (tmp_path / "estimated_cost.sql").write_text("SELECT 1")
  assert (
      check_streamlit_queries_sync.check_unmapped_canonical_queries(
          tmp_path, {"overview_totals.sql"}
      )
      == 0
  )

  (tmp_path / "extra_unknown.sql").write_text("SELECT 1")
  assert (
      check_streamlit_queries_sync.check_unmapped_canonical_queries(
          tmp_path, {"overview_totals.sql"}
      )
      == 1
  )

  # 2. Streamlit SQL builder coverage
  assert check_streamlit_queries_sync.check_unmapped_streamlit_builders() == 0
  with patch.object(
      check_streamlit_queries_sync.queries,
      "build_brand_new_feature_sql",
      create=True,
      new=lambda refs, window: "SELECT 1",
  ):
    assert check_streamlit_queries_sync.check_unmapped_streamlit_builders() == 1

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
  ORDER BY invocations DESC
  """
  sql_where_b = """
  SELECT
    tool_name,
    COUNT(*) AS invocations
  FROM `project.dataset.adk_tool_starts`
  WHERE ('___ALL___' IN UNNEST(@user_ids) OR user_id IN UNNEST(@user_ids))
    AND timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND ('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  GROUP BY tool_name
  ORDER BY invocations DESC
  """
  assert check_streamlit_queries_sync.normalize_query(
      sql_where_a, "tool_usage.sql"
  ) == check_streamlit_queries_sync.normalize_query(
      sql_where_b, "tool_usage.sql"
  )

  # SELECT column order and column count are preserved
  sql_select_ab = """
  SELECT col_a, col_b
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
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
  assert norm_ab != check_streamlit_queries_sync.normalize_query(
      sql_select_ba, "dummy.sql"
  )
  assert norm_ab != check_streamlit_queries_sync.normalize_query(
      sql_select_only_a, "dummy.sql"
  )

  # String literals with unbalanced parens and AND do not split clauses
  where_body = "col2 = 'value with (unbalanced paren AND keyword' AND col3 = 1"
  conjuncts = check_streamlit_queries_sync.split_top_level(
      where_body, r"\s+AND\s+"
  )
  assert conjuncts == [
      "col2 = 'value with (unbalanced paren AND keyword'",
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

  check_streamlit_queries_sync.assert_timestamp_bounds(sql_single, window)
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
  norm_tool = check_streamlit_queries_sync.normalize_query(
      sql_with_ifnull, "tool_usage.sql"
  )
  assert "IFNULL(tool_name, 'unknown')" not in norm_tool
  assert "tool_name" in norm_tool

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
  for col, snippet in [
      (
          "usage_prompt_tokens",
          "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,",
      ),
      (
          "usage_completion_tokens",
          "IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,",
      ),
      (
          "usage_total_tokens",
          "IFNULL(SUM(usage_total_tokens), 0) AS total_tokens",
      ),
  ]:
    bad_sql = original_llm.replace(snippet, "")
    with pytest.raises(
        AssertionError, match=f"missing required token column {col}"
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
