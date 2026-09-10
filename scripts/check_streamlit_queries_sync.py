#!/usr/bin/env python3
"""Check that canonical Grafana SQL matches the Streamlit dashboard's queries."""

import difflib
import inspect
from pathlib import Path
import re
import sys
from unittest.mock import MagicMock

for mod in [
    "streamlit",
    "pandas",
    "google.cloud",
    "google.cloud.bigquery",
    "google.api_core",
    "google.api_core.exceptions",
    "google.auth",
    "google.auth.exceptions",
    "plotly",
]:
  if mod not in sys.modules:
    try:
      __import__(mod)
    except ImportError:
      sys.modules[mod] = MagicMock()

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STREAMLIT_DIR = REPOSITORY_ROOT / "dashboards" / "streamlit"
if str(STREAMLIT_DIR) not in sys.path:
  sys.path.insert(0, str(STREAMLIT_DIR))

import datetime as dt

import models
import queries

QUERIES_DIRECTORY = (
    REPOSITORY_ROOT / "dashboards" / "grafana" / "queries"
    if (REPOSITORY_ROOT / "dashboards" / "grafana" / "queries").exists()
    else REPOSITORY_ROOT / "grafana" / "queries"
)

CANONICAL_QUERIES = {
    "overview_totals.sql": queries.build_overview_totals_sql,
    "events_over_time.sql": queries.build_events_over_time_sql,
    "errors_over_time.sql": queries.build_errors_over_time_sql,
    "events_by_agent.sql": queries.build_events_by_agent_sql,
    "top_errors.sql": queries.build_top_errors_sql,
    "llm_tokens_over_time.sql": queries.build_llm_tokens_over_time_sql,
    "llm_latency_percentiles.sql": queries.build_llm_latency_percentiles_sql,
    "tokens_by_model.sql": queries.build_tokens_by_model_sql,
    "llm_calls_total.sql": queries.build_llm_calls_total_sql,
    "tool_usage.sql": queries.build_tool_usage_sql,
    "tool_latency.sql": queries.build_tool_latency_sql,
    "tool_errors.sql": queries.build_tool_errors_sql,
    "recent_sessions.sql": queries.build_recent_sessions_sql,
    "trace_detail.sql": queries.build_trace_detail_sql,
}

QUERY_LIMITS = {
    "top_errors.sql": models.TOP_ERRORS_LIMIT,
    "tool_errors.sql": models.TOOL_ERRORS_LIMIT,
    "recent_sessions.sql": models.RECENT_SESSIONS_LIMIT,
    "trace_detail.sql": models.TRACE_DETAIL_LIMIT,
}

EXEMPT_CANONICAL_QUERIES = {
    "estimated_cost.sql": "Priced in Python UI",
    "var_agent.sql": "Filter options handled by build_filter_options_sql",
    "var_user_id.sql": "Filter options",
    "var_event_type.sql": "Filter options",
    "var_session_id.sql": "Filter options",
}

EXEMPT_STREAMLIT_BUILDERS = {
    "build_filter_options_sql": "Aggregated filter options dropdown",
}

DEFAULT_NOW = dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
DEFAULT_WINDOW = models.Window(
    start=DEFAULT_NOW - dt.timedelta(hours=24), end=DEFAULT_NOW
)


def check_unmapped_canonical_queries(
    directory: Path, mapped_files: set[str]
) -> int:
  """Fail if the queries directory holds .sql files not mapped or exempted."""
  try:
    sql_files = {path.name for path in directory.glob("*.sql")}
  except OSError as error:
    print(
        f"ERROR: cannot list canonical queries in {directory}: {error}",
        file=sys.stderr,
    )
    return 1

  covered = set(mapped_files) | set(EXEMPT_CANONICAL_QUERIES)
  unmapped = sorted(sql_files - covered)
  if not unmapped:
    return 0

  try:
    directory_label = directory.relative_to(REPOSITORY_ROOT).as_posix()
  except ValueError:
    directory_label = str(directory)

  border = "  " + "*" * 64
  title = ("* ERROR: unmapped SQL files in " + directory_label + "/").ljust(63)
  body = (
      "  The .sql file(s) below are missing from CANONICAL_QUERIES and\n"
      "  EXEMPT_CANONICAL_QUERIES, so they are never validated against the\n"
      "  dashboard:\n" + "".join(f"    - {name}\n" for name in unmapped)
  )
  print(
      f"\n{border}\n  {title}*\n{border}\n{body}{border}",
      file=sys.stderr,
  )
  return len(unmapped)


def check_unmapped_streamlit_builders() -> int:
  """Fail if queries.py defines build_*_sql functions not mapped or exempted."""
  all_builders = {
      name
      for name, _ in inspect.getmembers(queries, inspect.isfunction)
      if name.startswith("build_") and name.endswith("_sql")
  }
  mapped_builder_names = {
      builder.__name__
      for builder in CANONICAL_QUERIES.values()
      if hasattr(builder, "__name__")
  }
  covered = mapped_builder_names | set(EXEMPT_STREAMLIT_BUILDERS)
  unmapped = sorted(all_builders - covered)
  if not unmapped:
    return 0

  border = "  " + "*" * 64
  title = "* ERROR: unmapped Streamlit SQL builders in queries.py".ljust(63)
  body = (
      "  The builder function(s) below are missing from CANONICAL_QUERIES\n"
      "  and EXEMPT_STREAMLIT_BUILDERS, so they are never validated against\n"
      "  canonical Grafana SQL:\n"
      + "".join(f"    - {name}\n" for name in unmapped)
  )
  print(
      f"\n{border}\n  {title}*\n{border}\n{body}{border}",
      file=sys.stderr,
  )
  return len(unmapped)


def assert_timestamp_bounds(sql: str, window: models.Window) -> None:
  """Assert that timestamp bounds strictly match window.start and window.end with proper operators."""
  matches = re.findall(
      r'([a-zA-Z0-9_.]+)\s*(>=|<=|>|<|=)\s*TIMESTAMP\s*"([^"]+)"',
      sql,
  )
  if not matches:
    return

  expected_start = window.start.astimezone(dt.timezone.utc).strftime(
      "%Y-%m-%d %H:%M:%S+00:00"
  )
  expected_end = window.end.astimezone(dt.timezone.utc).strftime(
      "%Y-%m-%d %H:%M:%S+00:00"
  )

  start_cols = []
  end_cols = []

  for col, op, ts_literal in matches:
    if op == ">=":
      assert (
          ts_literal == expected_start
      ), f"Expected start timestamp literal '{expected_start}', got '{ts_literal}' for {col}"
      start_cols.append(col)
    elif op == "<":
      assert (
          ts_literal == expected_end
      ), f"Expected end timestamp literal '{expected_end}', got '{ts_literal}' for {col}"
      end_cols.append(col)
    else:
      raise AssertionError(
          f"Disallowed operator '{op}' with TIMESTAMP '{ts_literal}' on {col}"
      )

  assert len(start_cols) > 0, "No start timestamp bound (>=) found"
  assert len(end_cols) > 0, "No end timestamp bound (<) found"
  assert sorted(start_cols) == sorted(
      end_cols
  ), f"Mismatched start bounds {start_cols} and end bounds {end_cols}"


def assert_streamlit_query(
    filename: str, sql: str, window: models.Window
) -> None:
  """Validate that Streamlit query has required bounds and adaptations."""
  matches = re.findall(
      r'([a-zA-Z0-9_.]+)\s*(>=|<=|>|<|=)\s*TIMESTAMP\s*"([^"]+)"',
      sql,
  )
  assert (
      len(matches) >= 2
  ), f"Streamlit query {filename} is missing timestamp bounds"
  assert_timestamp_bounds(sql, window)

  if filename in ("tool_usage.sql", "tool_latency.sql"):
    assert "IFNULL(tool_name, 'unknown')" in sql, (
        f"Streamlit query {filename} expected to contain"
        " IFNULL(tool_name, 'unknown')"
    )


def strip_comments(sql: str) -> str:
  sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
  lines = []
  for line in sql.splitlines():
    line = re.split(r"--|#", line, maxsplit=1)[0]
    lines.append(line)
  return "\n".join(lines)


def normalize_whitespace_and_parens(sql: str) -> str:
  sql = re.sub(r"\s+", " ", sql)
  sql = re.sub(r"\(\s+", "(", sql)
  sql = re.sub(r"\s+\)", ")", sql)
  return sql.strip()


def normalize_time_filters(sql: str) -> str:
  sql = re.sub(r'\bAND\s+([a-zA-Z0-9_.]+)\s*<\s*TIMESTAMP\s*"[^"]+"', "", sql)
  sql = re.sub(r'([a-zA-Z0-9_.]+)\s*<\s*TIMESTAMP\s*"[^"]+"\s+AND\b', "", sql)
  sql = re.sub(
      r'([a-zA-Z0-9_.]+)\s*>=\s*TIMESTAMP\s*"[^"]+"',
      r"$__timeFilter(\1)",
      sql,
  )
  sql = re.sub(r"\$__timeFilter\(\s*([^\s)]+)\s*\)", r"$__timeFilter(\1)", sql)
  return sql


def normalize_time_groups(sql: str) -> str:
  sql = re.sub(
      r"\$__timeGroup\(([^,]+),\s*\$__interval\)",
      r"TIMESTAMP_TRUNC(\1, __INTERVAL__)",
      sql,
  )
  sql = re.sub(
      r"TIMESTAMP_TRUNC\(([^,]+),\s*(HOUR|MINUTE|DAY|\$__interval|__INTERVAL__)\)",
      r"TIMESTAMP_TRUNC(\1, __INTERVAL__)",
      sql,
  )
  sql = re.sub(
      r"(TIMESTAMP_TRUNC\([^,]+,\s*__INTERVAL__\))\s+AS\s+time\b",
      r"\1 AS bucket",
      sql,
  )
  sql = re.sub(r"\bGROUP\s+BY\s+time\b", "GROUP BY bucket", sql)
  sql = re.sub(r"\bORDER\s+BY\s+time\b", "ORDER BY bucket", sql)
  return sql


def normalize_params_and_refs(sql: str) -> str:
  sql = sql.replace("ARRAY<STRING>[${agent:sqlstring}]", "@agents")
  sql = sql.replace("ARRAY<STRING>[${user_id:sqlstring}]", "@user_ids")
  sql = sql.replace("ARRAY<STRING>[${event_type:sqlstring}]", "@event_types")
  sql = sql.replace("ARRAY<STRING>[${session_id:sqlstring}]", "@session_ids")
  sql = sql.replace("${agent:sqlstring}", "@agents")
  sql = sql.replace("${user_id:sqlstring}", "@user_ids")
  sql = sql.replace("${event_type:sqlstring}", "@event_types")
  sql = sql.replace("${session_id:sqlstring}", "@session_ids")

  sql = sql.replace(
      "`${project}.${dataset}.${table}`", "`project.dataset.events`"
  )
  sql = sql.replace(
      "`${project}.${dataset}.${view_prefix}", "`project.dataset.adk_"
  )

  return sql


def framework_adaptations(sql: str, filename: str) -> str:
  if filename in ("tool_usage.sql", "tool_latency.sql"):
    sql = sql.replace("IFNULL(tool_name, 'unknown') AS tool_name", "tool_name")
    sql = sql.replace("IFNULL(tool_name, 'unknown')", "tool_name")

  if filename == "llm_calls_total.sql":
    sql = re.sub(
        r",\s*IFNULL\(SUM\(usage_prompt_tokens\),\s*0\)\s*AS\s*prompt_tokens",
        "",
        sql,
    )
    sql = re.sub(
        r",\s*IFNULL\(SUM\(usage_completion_tokens\),\s*0\)\s*AS\s*completion_tokens",
        "",
        sql,
    )
    sql = re.sub(
        r",\s*IFNULL\(SUM\(usage_total_tokens\),\s*0\)\s*AS\s*total_tokens",
        "",
        sql,
    )

  return sql


def consume_sql_string_literal(text: str, pos: int) -> tuple[str, int]:
  """Consumes a SQL single-quoted string literal starting at pos."""
  quote_char = text[pos]
  result = [quote_char]
  i = pos + 1
  n = len(text)
  while i < n:
    ch = text[i]
    if ch == "\\":
      result.append(text[i : i + 2])
      i += 2
    elif ch == quote_char:
      if i + 1 < n and text[i + 1] == quote_char:
        result.append(text[i : i + 2])
        i += 2
      else:
        result.append(ch)
        i += 1
        break
    else:
      result.append(ch)
      i += 1
  return "".join(result), i


def split_top_level(text: str, delimiter: str) -> list[str]:
  parts = []
  depth = 0
  current = ""
  i = 0
  pattern = re.compile(delimiter, re.IGNORECASE)
  while i < len(text):
    if text[i] == "'":
      chunk, i = consume_sql_string_literal(text, i)
      current += chunk
      continue

    if text[i] == "(":
      depth += 1
    elif text[i] == ")":
      depth = max(0, depth - 1)

    if depth == 0:
      m = pattern.match(text[i:])
      if m:
        parts.append(current.strip())
        current = ""
        i += m.end()
        continue

    current += text[i]
    i += 1

  if current.strip():
    parts.append(current.strip())
  return parts


def split_conditions_and_sort(
    clause_text: str, split_delim: str, join_delim: str
) -> str:
  parts = split_top_level(clause_text, split_delim)
  return join_delim.join(sorted(parts))


def split_expressions_and_trim(clause_text: str) -> str:
  parts = split_top_level(clause_text, r"\s*,\s*")
  # DO NOT SORT SELECT projections, just split and trim
  return ",\n  ".join(parts)


def custom_clause_split(sql: str) -> str:
  parts = []
  depth = 0
  current = ""
  i = 0
  clauses = [
      "SELECT",
      "FROM",
      "WHERE",
      "GROUP BY",
      "HAVING",
      "ORDER BY",
      "LIMIT",
      "UNION ALL",
  ]

  def get_clause_match(s: str, pos: int) -> str | None:
    for c in clauses:
      if re.match(r"(?i)\b" + c + r"\b", s[pos:]):
        return c
    return None

  current_clause = ""
  res_clauses = []

  while i < len(sql):
    if sql[i] == "'":
      chunk, i = consume_sql_string_literal(sql, i)
      current += chunk
      continue

    if sql[i] == "(":
      depth += 1
    elif sql[i] == ")":
      depth = max(0, depth - 1)

    if depth == 0:
      c = get_clause_match(sql, i)
      if c:
        if current_clause or current.strip():
          res_clauses.append((current_clause, current.strip()))
        current_clause = c.upper()
        current = ""
        i += len(c)
        continue

    current += sql[i]
    i += 1

  if current_clause or current.strip():
    res_clauses.append((current_clause, current.strip()))

  formatted_res = []
  for clause_name, text in res_clauses:
    if not text:
      if clause_name:
        formatted_res.append(clause_name)
      continue

    if clause_name in ["WHERE", "HAVING"]:
      text = split_conditions_and_sort(text, r"\s+AND\s+", "\n  AND ")
      formatted_res.append(f"{clause_name}\n  {text}")
    elif clause_name == "SELECT":
      text = split_expressions_and_trim(text)
      formatted_res.append(f"{clause_name}\n  {text}")
    elif clause_name:
      formatted_res.append(f"{clause_name} {text}")
    else:
      formatted_res.append(text)

  return "\n".join(formatted_res)


def normalize_query(
    sql: str, filename: str, window: models.Window | None = None
) -> str:
  if window is None:
    window = DEFAULT_WINDOW
  sql = strip_comments(sql)
  sql = normalize_params_and_refs(sql)
  assert_timestamp_bounds(sql, window)
  sql = normalize_time_filters(sql)
  sql = normalize_time_groups(sql)
  sql = framework_adaptations(sql, filename)
  sql = normalize_whitespace_and_parens(sql)

  def process_subqueries(text: str) -> str:
    parts = []
    depth = 0
    current = ""
    i = 0
    while i < len(text):
      if text[i] == "'":
        chunk, i = consume_sql_string_literal(text, i)
        current += chunk
        continue

      if text[i] == "(":
        if depth == 0:
          parts.append(current)
          current = ""
        else:
          current += "("
        depth += 1
      elif text[i] == ")":
        depth = max(0, depth - 1)
        if depth == 0:
          if re.match(r"^\s*SELECT\b", current, re.IGNORECASE):
            parts.append("(\n" + custom_clause_split(current) + "\n)")
          else:
            parts.append("(" + current + ")")
          current = ""
        else:
          current += ")"
      else:
        current += text[i]
      i += 1
    parts.append(current)
    return "".join(parts)

  sql = process_subqueries(sql)
  sql = custom_clause_split(sql)

  # Strip any extra newlines or spaces
  lines = [line.rstrip() for line in sql.splitlines() if line.strip()]
  return "\n".join(lines)


def get_streamlit_query(
    filename: str, window: models.Window | None = None
) -> str:
  refs = models.TableRefs("project", "dataset", "events", "adk_")
  if window is None:
    window = DEFAULT_WINDOW

  if filename not in CANONICAL_QUERIES:
    raise ValueError(f"Unknown query: {filename}")

  builder = CANONICAL_QUERIES[filename]
  if filename in QUERY_LIMITS:
    return builder(refs, window, QUERY_LIMITS[filename])
  return builder(refs, window)


def main():
  errors = 0
  errors += check_unmapped_canonical_queries(
      QUERIES_DIRECTORY, set(CANONICAL_QUERIES.keys())
  )
  errors += check_unmapped_streamlit_builders()

  refs = models.TableRefs("project", "dataset", "events", "adk_")
  window = DEFAULT_WINDOW

  for filename in CANONICAL_QUERIES:
    grafana_path = QUERIES_DIRECTORY / filename
    if not grafana_path.exists():
      print(f"ERROR: Missing canonical query {filename}", file=sys.stderr)
      errors += 1
      continue

    grafana_sql = grafana_path.read_text(encoding="utf-8")
    streamlit_sql = get_streamlit_query(filename, window)

    try:
      assert_streamlit_query(filename, streamlit_sql, window)
    except AssertionError as error:
      print(
          f"ERROR: Streamlit query {filename} failed assertion: {error}",
          file=sys.stderr,
      )
      errors += 1
      continue

    norm_grafana = normalize_query(grafana_sql, filename, window)
    norm_streamlit = normalize_query(streamlit_sql, filename, window)

    if norm_grafana != norm_streamlit:
      print(
          f"ERROR: Streamlit query {filename} has drifted from Grafana",
          file=sys.stderr,
      )
      diff = difflib.unified_diff(
          norm_grafana.splitlines(),
          norm_streamlit.splitlines(),
          fromfile=f"{QUERIES_DIRECTORY.relative_to(REPOSITORY_ROOT)}/{filename}",
          tofile=f"Streamlit ({filename})",
          lineterm="",
      )
      print("\n".join(diff), file=sys.stderr)
      errors += 1

  if errors == 0:
    print(
        f"All {len(CANONICAL_QUERIES)} Streamlit dashboard queries match"
        " canonical Grafana SQL."
    )
    sys.exit(0)
  else:
    sys.exit(1)


if __name__ == "__main__":
  main()
