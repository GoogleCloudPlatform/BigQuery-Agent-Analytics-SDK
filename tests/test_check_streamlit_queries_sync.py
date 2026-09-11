import io
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import check_streamlit_queries_sync


def run_check_with_patch(query_filename, patched_sql):
  original_get_query = check_streamlit_queries_sync.get_streamlit_query

  def mock_get_query(filename, window=None):
    if filename == query_filename:
      return patched_sql
    return original_get_query(filename, window)

  with patch(
      "check_streamlit_queries_sync.get_streamlit_query",
      side_effect=mock_get_query,
  ):
    # We also need to capture stderr to check the diff
    stderr_capture = io.StringIO()
    with patch("sys.stderr", stderr_capture):
      with pytest.raises(SystemExit) as exc_info:
        check_streamlit_queries_sync.main()
      return exc_info.value.code, stderr_capture.getvalue()


def test_baseline_check():
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


def test_altered_error_predicate():
  original_sql = check_streamlit_queries_sync.get_streamlit_query(
      "overview_totals.sql"
  )
  # Change _ERROR to _WARNING
  altered_sql = original_sql.replace("'_ERROR'", "'_WARNING'")
  code, stderr = run_check_with_patch("overview_totals.sql", altered_sql)
  assert code == 1
  assert "has drifted from Grafana" in stderr
  assert "-'_ERROR'" in stderr or "+'_WARNING'" in stderr or "WARNING" in stderr


def test_missing_filter():
  original_sql = check_streamlit_queries_sync.get_streamlit_query(
      "events_over_time.sql"
  )
  altered_sql = original_sql.replace(
      "AND ('___ALL___' IN UNNEST(@session_ids) OR session_id IN"
      " UNNEST(@session_ids))",
      "",
  )
  code, stderr = run_check_with_patch("events_over_time.sql", altered_sql)
  assert code == 1
  assert "has drifted from Grafana" in stderr
  assert "session_ids" in stderr


def test_unexpected_filter_on_exempt_query():
  original_sql = check_streamlit_queries_sync.get_streamlit_query(
      "errors_over_time.sql"
  )
  # Add @event_types filter directly into the WHERE clause of errors_over_time.sql
  unexpected_filter = "  AND ('___ALL___' IN UNNEST(@event_types) OR event_type IN UNNEST(@event_types))\n"
  assert "WHERE " in original_sql
  altered_sql = original_sql.replace("WHERE ", f"WHERE {unexpected_filter}")
  code, stderr = run_check_with_patch("errors_over_time.sql", altered_sql)
  assert code == 1
  assert "has drifted from Grafana" in stderr
  assert "event_types" in stderr


def test_modified_aggregation():
  original_sql = check_streamlit_queries_sync.get_streamlit_query(
      "llm_latency_percentiles.sql"
  )
  # Change OFFSET(50) to OFFSET(90)
  altered_sql = original_sql.replace("OFFSET(50)", "OFFSET(90)")
  code, stderr = run_check_with_patch(
      "llm_latency_percentiles.sql", altered_sql
  )
  assert code == 1
  assert "has drifted from Grafana" in stderr
  assert "OFFSET(90)" in stderr


def test_altered_table_or_view():
  original_sql = check_streamlit_queries_sync.get_streamlit_query(
      "tool_usage.sql"
  )
  # Change tool_starts to tool_completions
  altered_sql = original_sql.replace(
      "`project.dataset.adk_tool_starts`",
      "`project.dataset.adk_tool_completions`",
  )
  code, stderr = run_check_with_patch("tool_usage.sql", altered_sql)
  assert code == 1
  assert "has drifted from Grafana" in stderr
  assert "tool_completions" in stderr


def test_direct_cli_execution():
  script_path = REPOSITORY_ROOT / "scripts" / "check_streamlit_queries_sync.py"
  result = subprocess.run(
      [sys.executable, str(script_path)], capture_output=True, text=True
  )
  assert result.returncode == 0
  assert (
      f"All {len(check_streamlit_queries_sync.CANONICAL_QUERIES)} Streamlit"
      " dashboard queries match canonical Grafana SQL." in result.stdout
  )


def test_normalize_query_reordered_where_conditions():
  sql_a = """
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
  sql_b = """
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
  norm_a = check_streamlit_queries_sync.normalize_query(sql_a, "tool_usage.sql")
  norm_b = check_streamlit_queries_sync.normalize_query(sql_b, "tool_usage.sql")
  assert norm_a == norm_b


def test_normalize_query_detects_swapped_timestamp_bounds():
  swapped_sql = """
  SELECT
    tool_name,
    COUNT(*) AS invocations
  FROM `project.dataset.adk_tool_starts`
  WHERE timestamp >= TIMESTAMP "2025-01-01 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2024-12-31 12:00:00+00:00"
  GROUP BY tool_name
  ORDER BY invocations DESC
  """
  with pytest.raises(AssertionError) as exc_info:
    check_streamlit_queries_sync.normalize_query(swapped_sql, "tool_usage.sql")
  assert "Expected start timestamp literal" in str(exc_info.value)


def test_normalize_query_detects_reordered_select_columns():
  sql_a = """
  SELECT
    col_a,
    col_b
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  sql_b = """
  SELECT
    col_b,
    col_a
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  norm_a = check_streamlit_queries_sync.normalize_query(sql_a, "dummy.sql")
  norm_b = check_streamlit_queries_sync.normalize_query(sql_b, "dummy.sql")
  assert norm_a != norm_b


def test_normalize_query_detects_removed_select_column():
  sql_full = """
  SELECT
    col_a,
    col_b
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  sql_missing = """
  SELECT
    col_a
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  norm_full = check_streamlit_queries_sync.normalize_query(
      sql_full, "dummy.sql"
  )
  norm_missing = check_streamlit_queries_sync.normalize_query(
      sql_missing, "dummy.sql"
  )
  assert norm_full != norm_missing


def test_main_missing_canonical_query_file():
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


def test_check_unmapped_canonical_queries(tmp_path):
  (tmp_path / "overview_totals.sql").write_text("SELECT 1")
  (tmp_path / "estimated_cost.sql").write_text("SELECT 1")
  count = check_streamlit_queries_sync.check_unmapped_canonical_queries(
      tmp_path, {"overview_totals.sql"}
  )
  assert count == 0

  (tmp_path / "extra_unknown.sql").write_text("SELECT 1")
  count = check_streamlit_queries_sync.check_unmapped_canonical_queries(
      tmp_path, {"overview_totals.sql"}
  )
  assert count == 1


def test_check_unmapped_streamlit_builders():
  assert check_streamlit_queries_sync.check_unmapped_streamlit_builders() == 0

  with patch.object(
      check_streamlit_queries_sync.queries,
      "build_brand_new_feature_sql",
      create=True,
      new=lambda refs, window: "SELECT 1",
  ):
    assert check_streamlit_queries_sync.check_unmapped_streamlit_builders() == 1


def test_scoped_tool_name_normalization():
  sql_with_ifnull = "SELECT IFNULL(tool_name, 'unknown') AS tool_name FROM foo"
  norm_other = check_streamlit_queries_sync.normalize_query(
      sql_with_ifnull, "overview_totals.sql"
  )
  assert "IFNULL(tool_name, 'unknown')" in norm_other

  norm_tool = check_streamlit_queries_sync.normalize_query(
      sql_with_ifnull, "tool_usage.sql"
  )
  assert "IFNULL(tool_name, 'unknown')" not in norm_tool
  assert "tool_name" in norm_tool


def test_string_literal_depth_tracking():
  sql = (
      "SELECT col1 FROM tbl WHERE col2 = 'value with (parens) and AND "
      "keywords' AND col3 = 1"
  )
  clauses = check_streamlit_queries_sync.split_top_level(sql, r"\s+WHERE\s+")
  assert len(clauses) == 2
  assert "value with (parens)" in clauses[1]


def test_timestamp_bounds_quote_tolerance():
  window = check_streamlit_queries_sync.DEFAULT_WINDOW
  sql_single = """
  SELECT tool_name, COUNT(*) AS invocations
  FROM `project.dataset.adk_tool_starts`
  WHERE timestamp >= TIMESTAMP '2024-12-31 12:00:00+00:00'
    AND timestamp < TIMESTAMP '2025-01-01 12:00:00+00:00'
  GROUP BY tool_name
  ORDER BY invocations DESC
  """
  sql_double = """
  SELECT tool_name, COUNT(*) AS invocations
  FROM `project.dataset.adk_tool_starts`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  GROUP BY tool_name
  ORDER BY invocations DESC
  """
  check_streamlit_queries_sync.assert_timestamp_bounds(sql_single, window)
  check_streamlit_queries_sync.assert_timestamp_bounds(sql_double, window)
  assert check_streamlit_queries_sync.normalize_time_filters(
      sql_single
  ) == check_streamlit_queries_sync.normalize_time_filters(sql_double)


def test_assert_streamlit_query_ifnull_tolerance():
  window = check_streamlit_queries_sync.DEFAULT_WINDOW
  sql_variants = [
      """
      SELECT IFNULL(tool_name, 'unknown') AS tool_name, COUNT(*) AS invocations
      FROM `project.dataset.adk_tool_starts`
      WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
        AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
      GROUP BY tool_name
      """,
      """
      SELECT IFNULL(  tool_name  ,  "unknown"  ) AS tool_name, COUNT(*) AS invocations
      FROM `project.dataset.adk_tool_starts`
      WHERE timestamp >= TIMESTAMP '2024-12-31 12:00:00+00:00'
        AND timestamp < TIMESTAMP '2025-01-01 12:00:00+00:00'
      GROUP BY tool_name
      """,
  ]
  for sql in sql_variants:
    check_streamlit_queries_sync.assert_streamlit_query(
        "tool_usage.sql", sql, window
    )


def test_llm_calls_total_missing_token_columns():
  window = check_streamlit_queries_sync.DEFAULT_WINDOW
  original_sql = check_streamlit_queries_sync.get_streamlit_query(
      "llm_calls_total.sql"
  )

  # Test missing prompt_tokens
  bad_sql_prompt = original_sql.replace(
      "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,", ""
  )
  with pytest.raises(
      AssertionError, match="missing required token column usage_prompt_tokens"
  ):
    check_streamlit_queries_sync.assert_streamlit_query(
        "llm_calls_total.sql", bad_sql_prompt, window
    )

  code, stderr = run_check_with_patch("llm_calls_total.sql", bad_sql_prompt)
  assert code == 1
  assert (
      "missing required token column usage_prompt_tokens as prompt_tokens"
      in stderr
  )

  # Test missing completion_tokens
  bad_sql_completion = original_sql.replace(
      "IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,", ""
  )
  with pytest.raises(
      AssertionError,
      match="missing required token column usage_completion_tokens",
  ):
    check_streamlit_queries_sync.assert_streamlit_query(
        "llm_calls_total.sql", bad_sql_completion, window
    )

  # Test missing total_tokens
  bad_sql_total = original_sql.replace(
      "IFNULL(SUM(usage_total_tokens), 0) AS total_tokens", ""
  )
  with pytest.raises(
      AssertionError, match="missing required token column usage_total_tokens"
  ):
    check_streamlit_queries_sync.assert_streamlit_query(
        "llm_calls_total.sql", bad_sql_total, window
    )


def test_llm_calls_total_whitespace_variants_normalize():
  """Whitespace variants in llm_calls_total.sql normalize cleanly."""
  window = check_streamlit_queries_sync.DEFAULT_WINDOW
  grafana_sql = (
      check_streamlit_queries_sync.QUERIES_DIRECTORY / "llm_calls_total.sql"
  ).read_text(encoding="utf-8")

  whitespace_variant_sql = """
  SELECT
    COUNT(DISTINCT CONCAT(trace_id, '|', span_id))
      + COUNTIF(trace_id IS NULL OR span_id IS NULL) AS llm_calls ,
    IFNULL (  SUM (  usage_prompt_tokens  )  ,  0  )   AS   prompt_tokens  ,
    IFNULL( SUM( usage_completion_tokens ) , 0 ) AS completion_tokens ,
    IFNULL  (  SUM  (  usage_total_tokens  )  ,  0  )  AS  total_tokens
  FROM `project.dataset.adk_llm_responses`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
    AND ('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))
    AND ('___ALL___' IN UNNEST(@user_ids) OR user_id IN UNNEST(@user_ids))
    AND ('___ALL___' IN UNNEST(@session_ids) OR session_id IN UNNEST(@session_ids))
  HAVING COUNT(*) > 0
  """

  check_streamlit_queries_sync.assert_streamlit_query(
      "llm_calls_total.sql", whitespace_variant_sql, window
  )
  norm_grafana = check_streamlit_queries_sync.normalize_query(
      grafana_sql, "llm_calls_total.sql", window
  )
  norm_variant = check_streamlit_queries_sync.normalize_query(
      whitespace_variant_sql, "llm_calls_total.sql", window
  )
  assert norm_variant == norm_grafana


def test_single_quote_timestamp_bounds():
  window = check_streamlit_queries_sync.DEFAULT_WINDOW
  sql = """
  SELECT tool_name
  FROM `project.dataset.adk_tool_starts`
  WHERE timestamp >= TIMESTAMP '2024-12-31 12:00:00+00:00'
    AND timestamp < TIMESTAMP '2025-01-01 12:00:00+00:00'
  """
  # Valid single-quote bounds pass
  check_streamlit_queries_sync.assert_timestamp_bounds(sql, window)

  # Invalid start timestamp with single quote fails
  bad_sql = sql.replace("2024-12-31", "2024-12-30")
  with pytest.raises(AssertionError, match="Expected start timestamp literal"):
    check_streamlit_queries_sync.assert_timestamp_bounds(bad_sql, window)


def test_select_columns_renamed_or_reordered():
  window = check_streamlit_queries_sync.DEFAULT_WINDOW
  original_sql = check_streamlit_queries_sync.get_streamlit_query(
      "tool_usage.sql"
  )

  # Renamed select column is detected as drift
  renamed_sql = original_sql.replace("AS invocations", "AS call_count")
  code, stderr = run_check_with_patch("tool_usage.sql", renamed_sql)
  assert code == 1
  assert "has drifted from Grafana" in stderr
  assert "call_count" in stderr

  # Reordered select columns are normalized consistently by custom_clause_split
  sql_1 = """
  SELECT
    col_a,
    col_b
  FROM `project.dataset.events`
  WHERE timestamp >= TIMESTAMP "2024-12-31 12:00:00+00:00"
    AND timestamp < TIMESTAMP "2025-01-01 12:00:00+00:00"
  """
  norm_1 = check_streamlit_queries_sync.normalize_query(
      sql_1, "dummy.sql", window
  )
  assert "col_a" in norm_1
  assert "col_b" in norm_1
