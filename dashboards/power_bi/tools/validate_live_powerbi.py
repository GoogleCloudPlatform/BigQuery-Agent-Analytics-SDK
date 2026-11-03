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

"""Live parity validator for the Power BI BQAA dashboard.

Runs the 37 oracle SQL queries via ``bq`` and compares their results
against DAX queries executed through the Power BI REST ``executeQueries``
endpoint.  Produces a sanitized receipt with no data values.
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOKER_ROOT = ROOT.parent / "looker_studio"
POWERBI_API_BASE = "https://api.powerbi.com/v1.0/myorg"
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$")
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FILTER_PARAMS = (
    "filter_agent",
    "filter_user_id",
    "filter_trace_id",
    "filter_span_id",
    "filter_tool_name",
)
FLOAT_TOLERANCE = 1e-9
LIMIT_RE = re.compile(r"\nLIMIT\s+\d+\s*$", re.IGNORECASE)


def validate_guid(value, label):
  if not GUID_RE.match(value):
    raise ValueError(f"invalid {label}")
  return value


def require_identifier(label, value, pattern):
  if not pattern.fullmatch(value):
    raise ValueError(f"invalid {label}: {value!r}")
  return value


def read_token_file(path, label="token"):
  p = pathlib.Path(path)
  if not p.is_file():
    raise ValueError(f"{label} file not found: {path}")
  mode = p.stat().st_mode
  if stat.S_IMODE(mode) & 0o077:
    raise ValueError(f"{label} file must not be readable by group or other")
  content = p.read_text().strip()
  if not content or any(c.isspace() for c in content):
    raise ValueError(f"{label} file must contain a single non-whitespace line")
  return content


def sha256_text(text):
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
  return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Chart metadata loading (chart_manifest.yaml + report_visuals.yaml
#                          + semantic_model.yaml)
# ---------------------------------------------------------------------------


def load_chart_metadata():
  """Return a list of 37 chart dicts enriched with visual/measure info."""
  manifest_path = LOOKER_ROOT / "spec" / "chart_manifest.yaml"
  rv_path = ROOT / "spec" / "report_visuals.yaml"
  sm_path = ROOT / "spec" / "semantic_model.yaml"

  manifest = yaml.safe_load(manifest_path.read_text())
  charts = manifest["charts"]
  if len(charts) != 37:
    raise ValueError(f"expected 37 charts, found {len(charts)}")

  report_visuals = yaml.safe_load(rv_path.read_text())
  visual_map = {v["id"]: v for v in report_visuals["parity_visuals"]}

  sm = yaml.safe_load(sm_path.read_text())
  measure_map = {m["name"]: m for m in sm["measures"]}

  enriched = []
  for chart in charts:
    cid = chart["id"]
    visual = visual_map.get(cid, {})
    measures = visual.get("measures", [])
    enriched.append(
        {
            "id": cid,
            "ls_chart": chart["ls_chart"],
            "looker_type": chart["looker_type"],
            "source_dashboard": chart["source_dashboard"],
            "oracle_query": chart["oracle_query"],
            "visual_type": visual.get("visual_type", ""),
            "measures": measures,
            "category": visual.get("category", ""),
            "series": visual.get("series", ""),
            "top_n": visual.get("top_n"),
            "listens_user_id": visual.get("listens_user_id", True),
            "measure_info": [measure_map.get(m, {}) for m in measures],
        }
    )
  return enriched


# ---------------------------------------------------------------------------
# Oracle query execution (mirrors looker_studio/tools/validate_live_bqaa.py)
# ---------------------------------------------------------------------------


def run_oracle_query(
    sql_path,
    source_project,
    billing_project,
    dataset,
    table,
    location,
    start,
    end,
    remove_limit=False,
):
  sql = (
      sql_path.read_text()
      .replace("{{PROJECT}}", source_project)
      .replace("{{DATASET}}", dataset)
      .replace("{{TABLE}}", table)
  )
  if remove_limit:
    sql = LIMIT_RE.sub("", sql)
  job_seed = f"{start}:{end}:{remove_limit}:{datetime.datetime.now(datetime.timezone.utc).timestamp()}:{sql}"
  job_id = f"pbi_parity_{hashlib.sha256(job_seed.encode()).hexdigest()[:12]}"
  command = [
      "bq",
      f"--project_id={billing_project}",
      f"--location={location}",
      "query",
      "--nouse_legacy_sql",
      "--format=json",
      "--max_rows=100000",
      f"--job_id={job_id}",
      f"--parameter=start_date:DATE:{start}",
      f"--parameter=end_date:DATE:{end}",
  ]
  for param in FILTER_PARAMS:
    command.append(f"--parameter={param}:ARRAY<STRING>:[]")
  process = subprocess.run(
      command,
      input=sql,
      capture_output=True,
      text=True,
      check=False,
  )
  if process.returncode:
    raise RuntimeError(f"{sql_path.name}: {process.stderr.strip()[:400]}")
  result = json.loads(process.stdout or "[]")
  if not isinstance(result, list):
    raise RuntimeError(f"{sql_path.name}: expected a JSON row list")
  return result, job_id


# ---------------------------------------------------------------------------
# DAX query execution
# ---------------------------------------------------------------------------


def execute_dax(token, workspace_id, dataset_id, dax_query):
  """Execute a DAX query with retry/backoff on transient HTTP errors."""
  import time as _time

  url = (
      f"{POWERBI_API_BASE}/groups/{workspace_id}"
      f"/datasets/{dataset_id}/executeQueries"
  )
  body = json.dumps(
      {
          "queries": [{"query": dax_query}],
          "serializerSettings": {"includeNulls": True},
      }
  )
  max_attempts = 3
  backoff_seconds = [1, 2, 4]
  for attempt in range(max_attempts):
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
      with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
      if exc.code in (429, 502, 503, 504) and attempt < max_attempts - 1:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        wait_s = (
            int(retry_after)
            if retry_after and str(retry_after).strip().isdigit()
            else backoff_seconds[attempt]
        )
        _time.sleep(wait_s)
        continue
      raise RuntimeError(f"executeQueries HTTP {exc.code}") from None


# ---------------------------------------------------------------------------
# Comparison engine (pure functions for unit testing)
# ---------------------------------------------------------------------------


def _to_float(value):
  if value is None:
    return None
  if isinstance(value, bool):
    return None
  if isinstance(value, (int, float)):
    return float(value)
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def values_match(a, b):
  """Compare two scalar values under the parity rules in spec.md §9.2."""
  a_none = a is None or _to_float(a) is None
  b_none = b is None or _to_float(b) is None
  if a_none and b_none:
    return True
  if a_none != b_none:
    return False
  fa, fb = _to_float(a), _to_float(b)
  return abs(fa - fb) <= FLOAT_TOLERANCE * max(1, abs(fb))


def _get_dax_value(dax_row, key):
  """Get a value from a DAX row, trying bracketed and bare keys."""
  if not isinstance(dax_row, dict):
    return None
  if key in dax_row:
    return dax_row[key]
  bare = key.strip("[]")
  if bare in dax_row:
    return dax_row[bare]
  bracketed = f"[{bare}]"
  if bracketed in dax_row:
    return dax_row[bracketed]
  return None


def _resolve_oracle_col(oracle_row, oracle_col):
  """Resolve an exact oracle column name to its value (fail-closed)."""
  if not isinstance(oracle_row, dict):
    return None
  if oracle_col in oracle_row:
    return oracle_row[oracle_col]
  if oracle_col == "value" and "value" in oracle_row:
    return oracle_row["value"]
  return None


def compare_card(oracle_row, dax_rows, oracle_cols):
  """Compare a card oracle row with DAX ROW result.

  dax_rows is the list of rows from executeQueries tables[0].rows.
  Each DAX row has keys like ``[v0]``, ``[v1]``, etc.
  """
  dax_row = dax_rows[0] if dax_rows else {}
  for i, col in enumerate(oracle_cols):
    d_val = _get_dax_value(dax_row, f"v{i}")
    if isinstance(oracle_row, dict) and oracle_row and col not in oracle_row:
      return False, col, None, d_val
    o_val = _resolve_oracle_col(oracle_row, col)
    if not values_match(o_val, d_val):
      return False, col, o_val, d_val
  return True, None, None, None


def _normalize_date_key(val):
  """Normalize a date value to YYYY-MM-DD string."""
  if val is None:
    return None
  return str(val)[:10]


def _aggregate_llm_trends(oracle_rows):
  """Aggregate minute-grain LLM call trend rows to UTC date grain."""
  totals = {}
  for row in oracle_rows:
    day = _normalize_date_key(
        row.get("timestamp_minute") or row.get("timestamp_date")
    )
    if not day:
      continue
    totals[day] = totals.get(day, 0) + int(row.get("total_llm_calls") or 0)
  return [
      {"timestamp_date": day, "total_llm_calls": totals[day]}
      for day in sorted(totals.keys())
  ]


def compare_series(
    oracle_rows, dax_rows, value_col, axis_col, has_series=False
):
  """Compare time-series or bar data on the union of axis keys."""
  oracle_map = {}
  for row in oracle_rows:
    key = str(row.get(axis_col, ""))
    if has_series:
      s_val = row.get("tool_completed_name") or row.get("tool_name") or ""
      key = f"{key}|{s_val}"
    oracle_map[key] = row.get(value_col)

  dax_map = {}
  for dax_row in dax_rows:
    if not isinstance(dax_row, dict):
      continue
    # Extract axis key - try multiple key forms
    date_val = (
        _get_dax_value(dax_row, "Date[Date]")
        or _get_dax_value(dax_row, "[Date]")
        or _get_dax_value(dax_row, f"events_v1[{axis_col}]")
        or _get_dax_value(dax_row, axis_col)
    )
    d_key = (
        _normalize_date_key(date_val)
        if axis_col == "timestamp_date"
        else str(date_val)
    )

    if has_series:
      series_val = _get_dax_value(
          dax_row, "events_v1[tool_completed_name]"
      ) or _get_dax_value(dax_row, "[tool_completed_name]")
      if series_val is None:
        continue
      d_key = f"{d_key}|{series_val}"

    d_val = _get_dax_value(dax_row, "v")
    dax_map[d_key] = d_val

  all_keys = set(oracle_map.keys()) | set(dax_map.keys())
  for key in sorted(all_keys):
    o_val = oracle_map.get(key)
    d_val = dax_map.get(key)
    # Skip if both are None/BLANK (spec §9.2)
    o_none = o_val is None or _to_float(o_val) is None
    d_none = d_val is None or _to_float(d_val) is None
    if o_none and d_none:
      continue
    if not values_match(o_val, d_val):
      return False, key, o_val, d_val
  return True, None, None, None


def compare_topn(oracle_rows, dax_rows, value_col, dim_col, n):
  """Tie-aware Top-N comparison.

  The ordered value sequences must be equal.  Members may differ only
  among items tied with the N-th value.
  """
  oracle_dim_col = (
      "tool_name"
      if dim_col in ("tool_completed_name", "tool_error_name")
      else dim_col
  )
  oracle_nonnull = [
      r for r in oracle_rows if _to_float(r.get(value_col)) is not None
  ]
  dax_nonnull = [
      r
      for r in dax_rows
      if _to_float(
          _get_dax_value(r, "v")
          if _get_dax_value(r, "v") is not None
          else _get_dax_value(r, value_col)
      )
      is not None
  ]
  oracle_sorted = sorted(
      oracle_nonnull,
      key=lambda r: (
          -(_to_float(r.get(value_col)) or 0),
          str(r.get(oracle_dim_col, r.get(dim_col, ""))),
      ),
  )[:n]
  dax_sorted = sorted(
      dax_nonnull,
      key=lambda r: (
          -(
              _to_float(
                  _get_dax_value(r, "v")
                  if _get_dax_value(r, "v") is not None
                  else _get_dax_value(r, value_col)
              )
              or 0
          ),
          str(
              _get_dax_value(r, f"events_v1[{dim_col}]")
              or _get_dax_value(r, dim_col)
              or ""
          ),
      ),
  )[:n]

  o_vals = [r.get(value_col) for r in oracle_sorted]
  o_dims = [
      str(r.get(oracle_dim_col, r.get(dim_col, ""))) for r in oracle_sorted
  ]
  d_vals = [
      _get_dax_value(r, "v")
      if _get_dax_value(r, "v") is not None
      else _get_dax_value(r, value_col)
      for r in dax_sorted
  ]
  d_dims = [
      str(
          _get_dax_value(r, f"events_v1[{dim_col}]")
          or _get_dax_value(r, dim_col)
          or ""
      )
      for r in dax_sorted
  ]
  if len(o_vals) != len(d_vals):
    return False, "length", o_vals, d_vals
  for i, (o, d) in enumerate(zip(o_vals, d_vals)):
    if not values_match(o, d):
      return False, f"value[{i}]", o, d
  # Verify dimension member keys using no_limit_rows for tie tolerance.
  oracle_dim_vals = {
      str(r.get(oracle_dim_col, r.get(dim_col, ""))): _to_float(
          r.get(value_col)
      )
      for r in oracle_nonnull
  }
  for i, d_dim in enumerate(d_dims):
    d_v = _to_float(d_vals[i])
    if d_dim not in oracle_dim_vals:
      return False, f"Member {d_dim} not in oracle", None, d_v
    o_v = oracle_dim_vals[d_dim]
    if not values_match(o_v, d_v):
      return False, f"member[{i}]", d_dim, d_v
  tie_adjusted = d_dims != o_dims
  if tie_adjusted and o_vals:
    nth_val = _to_float(o_vals[-1])
    for o_dim, d_dim, d_v in zip(o_dims, d_dims, d_vals):
      if o_dim != d_dim and not values_match(_to_float(d_v), nth_val):
        return False, "non-tie member order mismatch", o_dim, d_dim
  return True, ("tie_adjusted" if tie_adjusted else None), None, None


# ---------------------------------------------------------------------------
# DAX query builder
# ---------------------------------------------------------------------------


def _sanitize_error(error):
  """Sanitize an error string so no user IDs or trace IDs leak."""
  if error is None:
    return None
  s = str(error)[:200]
  s = re.sub(r"[\w.+-]+@[\w.-]+\.\w+", "[redacted]", s)
  s = re.sub(
      r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
      "[redacted]",
      s,
      flags=re.IGNORECASE,
  )
  s = re.sub(r"\buser-[0-9A-Za-z_-]+\b", "[redacted-user]", s)
  return s


def _date_dax(date_str):
  """Convert YYYY-MM-DD to DAX DATE(y,m,d) form."""
  parts = date_str.split("-")
  return f"DATE({parts[0]}, {parts[1]}, {parts[2]})"


def _to_dax_col(spec):
  """Convert a report_visuals column spec to a DAX column reference.

  e.g. ``events_v1.agent`` -> ``'events_v1'[agent]``,
  ``Date.Date`` -> ``'Date'[Date]``.
  """
  if "." not in spec:
    return spec
  table, col = spec.split(".", 1)
  return f"'{table}'[{col}]"


def build_dax_query(chart, start, end):
  """Build a DAX query for the given chart and window."""
  visual_type = chart["visual_type"]
  win = (
      "FILTER(ALL('Date'), 'Date'[Window] = \"Current\" "
      f"&& 'Date'[Date] >= {_date_dax(start)} "
      f"&& 'Date'[Date] <= {_date_dax(end)})"
  )

  if visual_type == "cardVisual":
    measures = chart["measures"]
    parts = []
    for i, m in enumerate(measures):
      parts.append(f'"v{i}", CALCULATE([{m}], {win})')
    return f"EVALUATE ROW({', '.join(parts)})\n"

  if visual_type == "areaChart":
    measure = chart["measures"][0]
    series = chart.get("series", "")
    if series:
      col = _to_dax_col(series)
      return (
          f"EVALUATE SUMMARIZECOLUMNS('Date'[Date], {col}, "
          f'{win}, "v", [{measure}])\n'
      )
    return (
        f"EVALUATE SUMMARIZECOLUMNS('Date'[Date], {win}, "
        f'"v", [{measure}])\n'
    )

  if visual_type == "clusteredBarChart":
    measure = chart["measures"][0]
    category = chart.get("category", "")
    col = _to_dax_col(category)
    return (
        f"EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS({col}, "
        f'"v", [{measure}]), {win})\n'
    )

  return ""


# ---------------------------------------------------------------------------
# Main validation flow
# ---------------------------------------------------------------------------


def main(argv=None):
  parser = argparse.ArgumentParser(
      description="Live parity validator for the Power BI BQAA dashboard"
  )
  parser.add_argument("--workspace-id", required=True)
  parser.add_argument("--dataset-id", required=True)
  parser.add_argument("--pbi-token-file", required=True)
  parser.add_argument("--project", required=True)
  parser.add_argument("--billing-project")
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--table", default="agent_events")
  parser.add_argument("--location", default="US")
  parser.add_argument(
      "--window",
      action="append",
      default=[],
      help="default | YYYY-MM-DD:YYYY-MM-DD (repeatable)",
  )
  parser.add_argument("--report-id", default=None)
  parser.add_argument("--output")
  args = parser.parse_args(argv)

  validate_guid(args.workspace_id, "workspace ID")
  validate_guid(args.dataset_id, "dataset ID")
  if args.report_id:
    validate_guid(args.report_id, "report ID")
  billing_project = args.billing_project or args.project
  require_identifier("project ID", args.project, PROJECT_RE)
  require_identifier("billing project ID", billing_project, PROJECT_RE)
  require_identifier("dataset ID", args.dataset, DATASET_RE)
  require_identifier("table ID", args.table, TABLE_RE)
  require_identifier("location", args.location, LOCATION_RE)

  token = read_token_file(args.pbi_token_file, "PBI token")

  charts = load_chart_metadata()

  windows = []
  if not args.window:
    windows.append(("default", "2026-04-18", "2026-07-16"))
  for w in args.window:
    if w == "default":
      windows.append(("default", "2026-04-18", "2026-07-16"))
    else:
      parts = w.split(":")
      if len(parts) != 2:
        raise ValueError(f"invalid window: {w}")
      datetime.date.fromisoformat(parts[0])
      datetime.date.fromisoformat(parts[1])
      windows.append((w, parts[0], parts[1]))

  results = []
  all_pass = True

  for win_label, start, end in windows:
    for chart in charts:
      chart_id = chart["id"]
      oracle_path = LOOKER_ROOT / chart["oracle_query"]
      try:
        oracle_rows, job_id = run_oracle_query(
            oracle_path,
            args.project,
            billing_project,
            args.dataset,
            args.table,
            args.location,
            start,
            end,
            remove_limit=(chart_id == "usage-llm-call-trends"),
        )
      except Exception as exc:  # pylint: disable=broad-except
        results.append(
            {
                "chart_id": chart_id,
                "window": win_label,
                "status": "FAIL",
                "tie_adjusted": False,
                "error": _sanitize_error(str(exc)[:200]),
            }
        )
        all_pass = False
        continue

      dax_query = build_dax_query(chart, start, end)
      try:
        dax_response = execute_dax(
            token, args.workspace_id, args.dataset_id, dax_query
        )
      except Exception as exc:  # pylint: disable=broad-except
        results.append(
            {
                "chart_id": chart_id,
                "window": win_label,
                "status": "FAIL",
                "tie_adjusted": False,
                "error": _sanitize_error(str(exc)[:200]),
            }
        )
        all_pass = False
        continue

      dax_results = dax_response.get("results", [])
      dax_rows = []
      if dax_results:
        tables = dax_results[0].get("tables", [])
        if tables:
          dax_rows = tables[0].get("rows", [])

      is_match = True
      tie_adjusted = False
      error_detail = None
      visual_type = chart["visual_type"]

      try:
        if visual_type == "cardVisual":
          oracle_cols = [
              m.get("oracle_column", "") for m in chart["measure_info"]
          ]
          oracle_row = oracle_rows[0] if oracle_rows else {}
          match, col, o_val, d_val = compare_card(
              oracle_row, dax_rows, oracle_cols
          )
          is_match = match
          if not match:
            error_detail = _sanitize_error(f"{col}: mismatch")

        elif visual_type == "areaChart":
          if chart_id == "usage-llm-call-trends":
            # Re-evaluate COUNT(DISTINCT CONCAT(trace_id, '|', span_id)) at
            # event_date grain (spec §9.2 compare_at: event_date) so cross-minute
            # duplicate (trace_id, span_id) pairs are not double-counted.
            daily_sql = (
                oracle_path.read_text()
                .replace("{{PROJECT}}", args.project)
                .replace("{{DATASET}}", args.dataset)
                .replace("{{TABLE}}", args.table)
                .replace(
                    "TIMESTAMP_TRUNC(timestamp, MINUTE) AS timestamp_minute",
                    "DATE(timestamp, 'UTC') AS timestamp_date",
                )
                .replace("GROUP BY timestamp_minute", "GROUP BY timestamp_date")
                .replace(
                    "ORDER BY timestamp_minute DESC",
                    "ORDER BY timestamp_date DESC",
                )
            )
            daily_sql = LIMIT_RE.sub("", daily_sql)
            with tempfile.TemporaryDirectory() as tmp_dir:
              tmp_sql = pathlib.Path(tmp_dir) / "llm_trends_daily.sql"
              tmp_sql.write_text(daily_sql)
              oracle_rows, job_id = run_oracle_query(
                  tmp_sql,
                  args.project,
                  billing_project,
                  args.dataset,
                  args.table,
                  args.location,
                  start,
                  end,
              )
            axis_col = "timestamp_date"
            value_col = "total_llm_calls"
          else:
            axis_col = "timestamp_date"
            value_col = chart["measure_info"][0].get("oracle_column", "")
          has_series = bool(chart.get("series"))
          if has_series:
            axis_col = "timestamp_date"
            value_col = chart["measure_info"][0].get("oracle_column", "")
          match, key, o_val, d_val = compare_series(
              oracle_rows,
              dax_rows,
              value_col,
              axis_col,
              has_series=has_series,
          )
          is_match = match
          if not match:
            error_detail = _sanitize_error(f"series mismatch at {key}")

        elif visual_type == "clusteredBarChart":
          top_n = chart.get("top_n")
          value_col = chart["measure_info"][0].get("oracle_column", "")
          category = chart.get("category", "")
          dim_col = category.replace("events_v1.", "")

          if chart_id == "usage-top-5-agents-by-llm-calls":
            match, key, o_val, d_val = compare_series(
                oracle_rows,
                dax_rows,
                value_col,
                dim_col,
                has_series=False,
            )
            is_match = match
            if not match:
              error_detail = _sanitize_error(f"bar mismatch at {key}")
          elif top_n is not None and top_n <= 10:
            no_limit_rows, _ = run_oracle_query(
                oracle_path,
                args.project,
                billing_project,
                args.dataset,
                args.table,
                args.location,
                start,
                end,
                remove_limit=True,
            )
            match, detail, o_val, d_val = compare_topn(
                no_limit_rows, dax_rows, value_col, dim_col, top_n
            )
            is_match = match
            tie_adjusted = bool(match and detail == "tie_adjusted")
            if not match:
              error_detail = _sanitize_error(f"{detail}")
          else:
            match, key, o_val, d_val = compare_series(
                oracle_rows,
                dax_rows,
                value_col,
                dim_col,
                has_series=False,
            )
            is_match = match
            if not match:
              error_detail = _sanitize_error(f"bar mismatch at {key}")
      except Exception as exc:  # pylint: disable=broad-except
        is_match = False
        error_detail = _sanitize_error(str(exc)[:200])

      status = "PASS" if is_match else "FAIL"
      if not is_match:
        all_pass = False

      results.append(
          {
              "chart_id": chart_id,
              "window": win_label,
              "status": status,
              "tie_adjusted": tie_adjusted,
              "oracle_row_count": len(oracle_rows),
              "dax_row_count": len(dax_rows),
              "oracle_query_sha256": sha256_file(oracle_path),
              "dax_query_sha256": sha256_text(dax_query),
              "error": error_detail,
          }
      )

  now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
  try:
    git_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    git_sha = git_proc.stdout.strip() if git_proc.returncode == 0 else "unknown"
  except OSError:
    git_sha = "unknown"

  receipt = {
      "kind": "sanitized-powerbi-live-parity",
      "git_sha": git_sha,
      "run_timestamp_utc": now_utc,
      "executed_at_utc": now_utc,
      "dataset_id": args.dataset_id,
      "report_id": args.report_id,
      "windows": [w[0] for w in windows],
      "charts_expected": 37,
      "charts_pass": sum(1 for r in results if r["status"] == "PASS"),
      "charts_fail": sum(1 for r in results if r["status"] == "FAIL"),
      "results": results,
      "overall_pass": all_pass,
      "pass": all_pass,
  }

  if args.output:
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(f"sanitized receipt: {output}")

  if all_pass:
    print("live parity OK: 37/37 charts PASS on all windows")
    return 0
  print(
      f"live parity FAIL: {receipt['charts_fail']} charts failed",
      file=sys.stderr,
  )
  return 1


if __name__ == "__main__":
  sys.exit(main())
