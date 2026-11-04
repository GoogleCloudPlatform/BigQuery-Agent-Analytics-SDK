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

The run fails closed: an error in the DAX response, a corrupt value, an
off-by-one count, a chart that is empty on both sides, or a run in which no
oracle query returned any row is never reported as a pass.  ``--allow-empty``
opts in to deliberately empty windows.
"""

import argparse
import datetime
import decimal
import hashlib
import json
import math
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
# `\Z` (not `$`) so a trailing newline is never accepted, even by callers that
# use `.match()` instead of `.fullmatch()`.
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}\Z")
TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}\Z")
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")
FILTER_PARAMS = (
    "filter_agent",
    "filter_user_id",
    "filter_trace_id",
    "filter_span_id",
    "filter_tool_name",
)
FLOAT_TOLERANCE = decimal.Decimal("1e-9")
LIMIT_RE = re.compile(r"\nLIMIT\s+\d+\s*$", re.IGNORECASE)
# Plain decimal text only: rejects NaN/Infinity, digit-group underscores and
# anything else decimal.Decimal() would otherwise be willing to parse.
_NUMERIC_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A value that is present but is not a finite number.  It is kept distinct
# from None (a genuine NULL/BLANK) so corrupt data can never pass as a blank.
_CORRUPT = object()
# Bounds of the report's loaded "Current" window, read from the model itself.
DATE_WINDOW_DAX = (
    "EVALUATE CALCULATETABLE(ROW(\"min_date\", MIN('Date'[Date]), "
    "\"max_date\", MAX('Date'[Date])), 'Date'[Window] = \"Current\")\n"
)
# Used only when the Date-table lookup fails; the receipt records the fallback.
FIXTURE_WINDOW = ("2026-04-18", "2026-07-16")
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_EMPTY = "EMPTY"


def validate_guid(value, label="GUID"):
  """Return *value* when it is exactly one GUID string, else raise."""
  if not isinstance(value, str) or not GUID_RE.fullmatch(value):
    raise ValueError(f"invalid {label}")
  return value


def require_identifier(label, value, pattern):
  if not isinstance(value, str) or not pattern.fullmatch(value):
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
    measure_info = []
    for m in measures:
      if m not in measure_map:
        raise ValueError(f"chart {cid!r} binds unknown measure {m!r}")
      info = dict(measure_map[m])
      info["comparison_type"] = _measure_comparison_type(measure_map[m])
      measure_info.append(info)
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
            "measure_info": measure_info,
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
            max(1, int(retry_after))
            if retry_after and str(retry_after).strip().isdigit()
            else backoff_seconds[attempt]
        )
        _time.sleep(wait_s)
        continue
      raise RuntimeError(f"executeQueries HTTP {exc.code}") from None


class DaxResponseError(ValueError):
  """The executeQueries response is malformed or reports an error."""


def _has_error(node):
  return isinstance(node, dict) and bool(
      node.get("error") or node.get("errors")
  )


def extract_dax_rows(response):
  """Return the rows of the first result table of an executeQueries response.

  The response envelope is validated before anything is compared: an error at
  the response, result or table level (a truncated result included) or a
  missing result/table is a failure, whereas a table that is present with no
  rows is a legitimately empty result.
  """
  if not isinstance(response, dict) or _has_error(response):
    raise DaxResponseError("dax_response_error")
  results = response.get("results")
  if not isinstance(results, list) or not results:
    raise DaxResponseError("dax_response_error")
  first_rows = None
  for result_index, result in enumerate(results):
    if not isinstance(result, dict) or _has_error(result):
      raise DaxResponseError("dax_response_error")
    tables = result.get("tables")
    if not isinstance(tables, list) or not tables:
      raise DaxResponseError("dax_response_error")
    for table_index, table in enumerate(tables):
      if not isinstance(table, dict) or _has_error(table):
        raise DaxResponseError("dax_response_error")
      rows = table.get("rows")
      if rows is None:
        rows = []
      if not isinstance(rows, list):
        raise DaxResponseError("dax_response_error")
      if result_index == 0 and table_index == 0:
        first_rows = rows
  return first_rows


# ---------------------------------------------------------------------------
# Comparison engine (pure functions for unit testing)
# ---------------------------------------------------------------------------


def _to_number(value):
  """Classify *value* as NULL (None), corrupt (_CORRUPT) or an exact Decimal.

  None and blank text are NULL/BLANK.  Booleans, non-finite floats and text
  that is not a plain decimal number are corrupt: they are present, so they
  must never be mistaken for a blank.
  """
  if value is None:
    return None
  if isinstance(value, bool):
    return _CORRUPT
  if isinstance(value, int):
    return decimal.Decimal(value)
  if isinstance(value, float):
    if not math.isfinite(value):
      return _CORRUPT
    return decimal.Decimal(repr(value))
  if isinstance(value, decimal.Decimal):
    return value if value.is_finite() else _CORRUPT
  if isinstance(value, str):
    text = value.strip()
    if not text:
      return None
    if not _NUMERIC_RE.fullmatch(text):
      return _CORRUPT
    return decimal.Decimal(text)
  return _CORRUPT


def values_match(a, b, metric_type=None):
  """Compare two scalar values under the parity rules in spec.md §9.2.

  NULL/BLANK matches only NULL/BLANK, and a corrupt value matches nothing.
  Two integral values (counts, token totals) must be exactly equal; a value
  with a fractional part is compared with a relative tolerance of 1e-9.

  When *metric_type* is ``"float"`` the integral-exactness rule is relaxed so
  that large values stored as floats are compared with the tolerance instead
  of requiring exact integer equality.  When *metric_type* is ``"int"``
  the integral-exactness rule is always enforced.
  """
  da, db = _to_number(a), _to_number(b)
  if da is _CORRUPT or db is _CORRUPT:
    return False
  if da is None or db is None:
    return da is None and db is None

  if metric_type == "int":
    if da != da.to_integral_value() or db != db.to_integral_value():
      return False
    return da == db
  if metric_type == "float":
    try:
      with decimal.localcontext() as context:
        context.prec = 100
        return abs(da - db) <= FLOAT_TOLERANCE * max(
            decimal.Decimal(1), abs(db)
        )
    except decimal.DecimalException:
      return False

  # A Python int (or a string/Decimal whose value is integral) forces exact
  # comparison.  A Python float always uses tolerance even when its value
  # is integral (e.g. 100.0 vs 100.0000000001), so large floats stored by
  # DAX are not rejected for sub-integer drift.
  def _is_exact(value, number):
    if isinstance(value, bool):
      return False
    if isinstance(value, int):
      return True
    if isinstance(value, (str, decimal.Decimal)):
      return number == number.to_integral_value()
    return False

  try:
    with decimal.localcontext() as context:
      context.prec = 100
      if _is_exact(a, da) or _is_exact(b, db):
        return (
            da == da.to_integral_value()
            and db == db.to_integral_value()
            and da == db
        )
      return abs(da - db) <= FLOAT_TOLERANCE * max(decimal.Decimal(1), abs(db))
  except decimal.DecimalException:
    return False


def _has_dax_key(dax_row, key):
  """Return True iff *dax_row* is a dict containing *key* in any form."""
  if not isinstance(dax_row, dict):
    return False
  if key in dax_row:
    return True
  bare = key.strip("[]")
  if bare in dax_row:
    return True
  bracketed = f"[{bare}]"
  return bracketed in dax_row


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


def _measure_comparison_type(measure_dict):
  """Return a measure's declared ``comparison_type``: ``"int"`` or ``"float"``.

  The type is an explicit field of semantic_model.yaml, checked by the
  generator against the measure's format and DAX.  It is never derived from
  the display format, and a missing or unknown type stops the run.
  """
  ctype = (
      measure_dict.get("comparison_type")
      if isinstance(measure_dict, dict)
      else None
  )
  if ctype not in ("int", "float"):
    name = measure_dict.get("name") if isinstance(measure_dict, dict) else None
    raise ValueError(f"measure {name!r} has no valid comparison_type")
  return ctype


def compare_card(oracle_row, dax_rows, oracle_cols, metric_types=None):
  """Compare a card oracle row with DAX ROW result.

  dax_rows is the list of rows from executeQueries tables[0].rows.
  Each DAX row has keys like ``[v0]``, ``[v1]``, etc.

  Returns ``(match, failure_code, oracle_value, dax_value)``.  The
  failure_code is ``"oracle_row"`` when the oracle row is empty,
  ``"dax_row_count"`` when DAX returned zero or multiple rows, a column
  name when the oracle column is missing or the DAX key is absent, or
  ``None`` on a match.
  """
  if not isinstance(oracle_row, dict) or not oracle_row:
    return False, "oracle_row", None, None
  if (
      not isinstance(dax_rows, list)
      or len(dax_rows) != 1
      or not isinstance(dax_rows[0], dict)
  ):
    return (
        False,
        "dax_row_count",
        len(dax_rows) if isinstance(dax_rows, list) else None,
        None,
    )
  dax_row = dax_rows[0]
  for i, col in enumerate(oracle_cols):
    d_val = _get_dax_value(dax_row, f"v{i}")
    if col not in oracle_row:
      return False, col, None, d_val
    if not _has_dax_key(dax_row, f"v{i}"):
      return False, col, _resolve_oracle_col(oracle_row, col), None
    o_val = _resolve_oracle_col(oracle_row, col)
    m_type = (
        metric_types[i] if (metric_types and i < len(metric_types)) else None
    )
    if not values_match(o_val, d_val, metric_type=m_type):
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
    oracle_rows,
    dax_rows,
    value_col,
    axis_col,
    has_series=False,
    metric_type=None,
):
  """Compare time-series or bar data on the union of axis keys.

  Returns ``(ok, detail, oracle_value, dax_value)``.  On a mismatch ``detail``
  is ``row_index=<n>``, the position in the sorted union of axis keys, so an
  axis label (which can be user data) never reaches a receipt.
  """
  oracle_map = {}
  seen_oracle_keys = set()
  for row in oracle_rows:
    if not isinstance(row, dict):
      return False, "oracle_row_type", None, None
    if axis_col not in row or value_col not in row:
      return False, "oracle_missing_key", None, None
    key = str(row.get(axis_col, ""))
    if has_series:
      if "tool_completed_name" in row:
        s_val = row["tool_completed_name"]
      elif "tool_name" in row:
        s_val = row["tool_name"]
      else:
        return False, "missing_series", None, None
      key = (key, _dim_key(s_val))
    if key in seen_oracle_keys:
      return False, "duplicate_axis_key", None, None
    seen_oracle_keys.add(key)
    oracle_map[key] = row.get(value_col)

  dax_map = {}
  seen_dax_keys = set()
  axis_keys = ("Date[Date]", "[Date]", f"events_v1[{axis_col}]", axis_col)
  for dax_row in dax_rows:
    if not isinstance(dax_row, dict):
      return False, "dax_row_type", None, None
    # Require explicit metric key presence on the DAX row.
    if not _has_dax_key(dax_row, "v"):
      return False, "dax_missing_metric", None, None
    # Require an axis key too: a row without one would be keyed "None".
    if not any(_has_dax_key(dax_row, key) for key in axis_keys):
      return False, "dax_missing_axis", None, None
    # Extract axis key - try multiple key forms
    date_val = (
        _get_dax_value(dax_row, "Date[Date]")
        or _get_dax_value(dax_row, "[Date]")
        or _get_dax_value(dax_row, f"events_v1[{axis_col}]")
        or _get_dax_value(dax_row, axis_col)
    )
    d_key = (
        _normalize_date_key(date_val) if axis_col == "timestamp_date" else None
    )
    if d_key is None:
      d_key = str(date_val)

    if has_series:
      found, series_val = _dax_dimension(dax_row, "tool_completed_name")
      if not found:
        found, series_val = _dax_dimension(dax_row, "tool_name")
      if not found:
        return False, "missing_series", None, None
      d_key = (d_key, _dim_key(series_val))

    # Check for a duplicate axis key BEFORE any skip so a wrong-then-right
    # duplicate or blank-then-right duplicate never overwrites or is ignored.
    if d_key in seen_dax_keys:
      return False, "duplicate_axis_key", None, None
    seen_dax_keys.add(d_key)

    d_val = _get_dax_value(dax_row, "v")
    dax_map[d_key] = d_val

  all_keys = sorted(set(oracle_map.keys()) | set(dax_map.keys()))
  for index, key in enumerate(all_keys):
    o_val = oracle_map.get(key)
    d_val = dax_map.get(key)
    # Both BLANK is a match (spec §9.2); a corrupt cell is never BLANK, so it
    # falls through to values_match() and fails there.
    if _to_number(o_val) is None and _to_number(d_val) is None:
      continue
    if not values_match(o_val, d_val, metric_type=metric_type):
      return False, f"row_index={index}", o_val, d_val
  return True, None, None, None


def _dim_key(value):
  """Typed dimension key: NULL stays distinct from the empty string."""
  return (0, "") if value is None else (1, str(value))


def _dax_dimension(dax_row, dim_col):
  """Return ``(found, value)`` for the dimension column of a DAX row."""
  if isinstance(dax_row, dict):
    for key in (
        f"events_v1[{dim_col}]",
        f"'events_v1'[{dim_col}]",
        dim_col,
        f"[{dim_col}]",
    ):
      if key in dax_row:
        return True, dax_row[key]
  return False, None


def compare_topn(
    oracle_rows, dax_rows, value_col, dim_col, n, metric_type=None
):
  """Tie-aware Top-N comparison.

  A DAX Top-N result holds at most *n* members, each member once (the model's
  TOPN measures break ties on the dimension), so more than *n* non-blank rows
  or a repeated member is a failure.  The ordered value sequences must be
  equal; members may differ only among the items tied with the N-th value.

  Returns ``(ok, detail, oracle_info, dax_info)``.  ``detail`` is a structural
  code and never contains a dimension label, so it is safe to put in a
  receipt.
  """
  oracle_dim_col = (
      "tool_name"
      if dim_col in ("tool_completed_name", "tool_error_name")
      else dim_col
  )
  oracle_items = []
  for row in oracle_rows:
    number = _to_number(row.get(value_col))
    if number is _CORRUPT:
      return False, "corrupt_value", None, None
    if number is None:
      continue
    if oracle_dim_col in row:
      member = row[oracle_dim_col]
    elif dim_col in row:
      member = row[dim_col]
    else:
      return False, "missing_dimension", None, None
    oracle_items.append((_dim_key(member), number))

  dax_items = []
  for row in dax_rows:
    raw = _get_dax_value(row, "v")
    if raw is None:
      raw = _get_dax_value(row, value_col)
    number = _to_number(raw)
    if number is _CORRUPT:
      return False, "corrupt_value", None, None
    if number is None:
      continue
    found, member = _dax_dimension(row, dim_col)
    if not found:
      return False, "missing_dimension", None, None
    dax_items.append((_dim_key(member), number))

  # Cardinality and uniqueness are checked on the complete non-blank DAX
  # result, before anything is sliced to N.
  if len(dax_items) > n:
    return False, "row_count", len(oracle_items), len(dax_items)
  dax_members = [member for member, _ in dax_items]
  if len(set(dax_members)) != len(dax_members):
    return False, "duplicate_member", None, None

  def rank(item):
    return (-item[1], item[0])

  oracle_top = sorted(oracle_items, key=rank)[:n]
  dax_top = sorted(dax_items, key=rank)
  o_vals = [number for _, number in oracle_top]
  d_vals = [number for _, number in dax_top]
  if len(o_vals) != len(d_vals):
    return False, "length", len(o_vals), len(d_vals)
  for index, (o_val, d_val) in enumerate(zip(o_vals, d_vals)):
    if not values_match(o_val, d_val, metric_type=metric_type):
      return False, f"value[{index}]", o_val, d_val
  # Every DAX member must be a real oracle member carrying the same value.
  oracle_by_member = dict(oracle_items)
  for index, (member, number) in enumerate(dax_top):
    if member not in oracle_by_member:
      return False, "member not in oracle", None, number
    if not values_match(
        oracle_by_member[member], number, metric_type=metric_type
    ):
      return False, f"member[{index}]", oracle_by_member[member], number
  o_members = [member for member, _ in oracle_top]
  d_members = [member for member, _ in dax_top]
  tie_adjusted = o_members != d_members
  if tie_adjusted and o_vals:
    nth_val = o_vals[-1]
    for o_member, d_member, d_val in zip(o_members, d_members, d_vals):
      if o_member != d_member and not values_match(
          d_val, nth_val, metric_type=metric_type
      ):
        return False, "non-tie member order mismatch", None, None
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
      # Every date/series group, uncapped: the visual has no row cap, and the
      # oracle of a series chart runs without its LIMIT (README D13).
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


def _parse_iso_date(text):
  """Parse a strict ``YYYY-MM-DD`` calendar date."""
  if not isinstance(text, str) or not _ISO_DATE_RE.fullmatch(text):
    raise ValueError(f"invalid date: {text!r}")
  try:
    return datetime.date.fromisoformat(text)
  except ValueError:
    raise ValueError(f"invalid date: {text!r}") from None


def parse_window(spec):
  """Parse ``YYYY-MM-DD:YYYY-MM-DD`` into a validated ``(start, end)`` pair."""
  parts = spec.split(":")
  if len(parts) != 2:
    raise ValueError(f"invalid window: {spec}")
  start, end = _parse_iso_date(parts[0]), _parse_iso_date(parts[1])
  if start > end:
    raise ValueError(f"invalid window: {spec} (start is after end)")
  return parts[0], parts[1]


def resolve_default_window(
    token, workspace_id, dataset_id, allow_fallback=False
):
  """Return ``(start, end, source)`` for the report's loaded "Current" window.

  The bounds come from the model's own Date table, so the oracle and the DAX
  side are both evaluated over the window the report actually shows.  When
  that lookup fails the fixture range is used only when *allow_fallback* is
  True; otherwise a ``RuntimeError`` is raised so the caller fails closed.
  """
  try:
    rows = extract_dax_rows(
        execute_dax(token, workspace_id, dataset_id, DATE_WINDOW_DAX)
    )
    row = rows[0]
    start = _parse_iso_date(
        _normalize_date_key(_get_dax_value(row, "min_date"))
    )
    end = _parse_iso_date(_normalize_date_key(_get_dax_value(row, "max_date")))
    if start > end:
      raise ValueError("reversed window")
    return start.isoformat(), end.isoformat(), "date_table"
  except Exception:  # pylint: disable=broad-except
    if not allow_fallback:
      raise RuntimeError("default_window_lookup_failed") from None
    return FIXTURE_WINDOW[0], FIXTURE_WINDOW[1], "fixture_fallback"


def _daily_llm_trend_rows(oracle_path, args, billing_project, start, end):
  """Re-run the LLM-call trend oracle at UTC date grain.

  COUNT(DISTINCT CONCAT(trace_id, '|', span_id)) is evaluated at event_date
  grain (spec §9.2 compare_at: event_date) so cross-minute duplicate
  (trace_id, span_id) pairs are not double-counted.
  """
  daily_sql = oracle_path.read_text()
  for placeholder, replacement in (
      ("{{PROJECT}}", args.project),
      ("{{DATASET}}", args.dataset),
      ("{{TABLE}}", args.table),
  ):
    daily_sql = daily_sql.replace(placeholder, replacement)
  clause_replacements = [
      (
          "TIMESTAMP_TRUNC(timestamp, MINUTE) AS timestamp_minute",
          "DATE(timestamp, 'UTC') AS timestamp_date",
      ),
      ("GROUP BY timestamp_minute", "GROUP BY timestamp_date"),
      ("ORDER BY timestamp_minute DESC", "ORDER BY timestamp_date DESC"),
  ]
  for old_clause, new_clause in clause_replacements:
    if old_clause not in daily_sql:
      raise RuntimeError("llm_trend_clause_missing")
    daily_sql = daily_sql.replace(old_clause, new_clause)
  daily_sql = LIMIT_RE.sub("", daily_sql)
  with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_sql = pathlib.Path(tmp_dir) / "llm_trends_daily.sql"
    tmp_sql.write_text(daily_sql)
    rows, _ = run_oracle_query(
        tmp_sql,
        args.project,
        billing_project,
        args.dataset,
        args.table,
        args.location,
        start,
        end,
    )
  return rows


def oracle_runs_uncapped(chart):
  """Return True when a chart's oracle query runs without its final LIMIT.

  The LLM-call trend is re-aggregated to day grain, and a series chart (Tool
  Calls Over Time) is drawn with no row cap, so every date/series group is
  compared rather than the source's first 5,000 rows (README D13).  Top-N
  charts drop the LIMIT for their separate tie-aware query.
  """
  return chart["id"] == "usage-llm-call-trends" or bool(chart.get("series"))


def _compare_chart(chart, oracle_rows, topn_rows, dax_rows, allow_empty=False):
  """Compare one chart; return ``(is_match, tie_adjusted, error_detail)``.

  The error detail is built only from structural codes, row indices and
  configured column names, never from a data value or a dimension label.
  """
  visual_type = chart["visual_type"]

  if visual_type == "cardVisual":
    oracle_cols = [m.get("oracle_column", "") for m in chart["measure_info"]]
    metric_types = [m.get("comparison_type") for m in chart["measure_info"]]
    if (
        allow_empty
        and not oracle_rows
        and chart["id"].startswith("performance-p")
    ):
      oracle_row = {col: None for col in oracle_cols}
    else:
      oracle_row = oracle_rows[0] if len(oracle_rows) == 1 else {}
    match, col, _, _ = compare_card(
        oracle_row, dax_rows, oracle_cols, metric_types=metric_types
    )
    return match, False, (None if match else f"{col}: mismatch")

  if visual_type == "areaChart":
    if chart["id"] == "usage-llm-call-trends":
      value_col = "total_llm_calls"
    else:
      value_col = chart["measure_info"][0].get("oracle_column", "")
    m_type = (
        chart["measure_info"][0].get("comparison_type")
        if chart.get("measure_info")
        else None
    )
    match, detail, _, _ = compare_series(
        oracle_rows,
        dax_rows,
        value_col,
        "timestamp_date",
        has_series=bool(chart.get("series")),
        metric_type=m_type,
    )
    return match, False, (None if match else f"series mismatch at {detail}")

  if visual_type == "clusteredBarChart":
    value_col = chart["measure_info"][0].get("oracle_column", "")
    m_type = (
        chart["measure_info"][0].get("comparison_type")
        if chart.get("measure_info")
        else None
    )
    dim_col = chart.get("category", "").replace("events_v1.", "")
    if topn_rows is not None:
      match, detail, _, _ = compare_topn(
          topn_rows,
          dax_rows,
          value_col,
          dim_col,
          chart["top_n"],
          metric_type=m_type,
      )
      return (
          match,
          bool(match and detail == "tie_adjusted"),
          (None if match else f"top-n mismatch: {detail}"),
      )
    match, detail, _, _ = compare_series(
        oracle_rows,
        dax_rows,
        value_col,
        dim_col,
        has_series=False,
        metric_type=m_type,
    )
    return match, False, (None if match else f"bar mismatch at {detail}")

  return False, False, "unsupported_visual_type"


def _run_chart(chart, window, args, billing_project, token):
  """Validate one chart in one window and return its sanitized result.

  Every failure is reported as an allowlisted code: upstream error text, data
  values and dimension labels are never copied into the result.
  """
  chart_id = chart["id"]
  start, end = window["start"], window["end"]
  oracle_path = LOOKER_ROOT / chart["oracle_query"]
  dax_query = build_dax_query(chart, start, end)
  top_n = chart.get("top_n")
  wants_topn = (
      chart["visual_type"] == "clusteredBarChart"
      and top_n is not None
      and top_n <= 10
  )

  def result(status, error=None, oracle_rows=(), dax_rows=(), tie=False):
    try:
      oracle_sha = sha256_file(oracle_path)
    except OSError:
      oracle_sha = None
    return {
        "chart_id": chart_id,
        "window": window["label"],
        "status": status,
        "tie_adjusted": tie,
        "oracle_row_count": len(oracle_rows),
        "dax_row_count": len(dax_rows),
        "oracle_query_sha256": oracle_sha,
        "dax_query_sha256": sha256_text(dax_query),
        "error": _sanitize_error(error),
    }

  try:
    oracle_rows, _ = run_oracle_query(
        oracle_path,
        args.project,
        billing_project,
        args.dataset,
        args.table,
        args.location,
        start,
        end,
        remove_limit=oracle_runs_uncapped(chart),
    )
    if chart_id == "usage-llm-call-trends":
      oracle_rows = _daily_llm_trend_rows(
          oracle_path, args, billing_project, start, end
      )
    topn_rows = None
    if wants_topn:
      topn_rows, _ = run_oracle_query(
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
  except Exception:  # pylint: disable=broad-except
    return result(STATUS_FAIL, "oracle_query_failed")

  try:
    dax_rows = extract_dax_rows(
        execute_dax(token, args.workspace_id, args.dataset_id, dax_query)
    )
  except DaxResponseError:
    return result(STATUS_FAIL, "dax_response_error", oracle_rows)
  except Exception:  # pylint: disable=broad-except
    return result(STATUS_FAIL, "dax_request_failed", oracle_rows)

  try:
    is_match, tie_adjusted, error = _compare_chart(
        chart,
        oracle_rows,
        topn_rows,
        dax_rows,
        allow_empty=getattr(args, "allow_empty", False),
    )
  except Exception:  # pylint: disable=broad-except
    return result(STATUS_FAIL, "comparison_error", oracle_rows, dax_rows)
  if not is_match:
    return result(STATUS_FAIL, error, oracle_rows, dax_rows)
  # A grouped visual (areaChart, clusteredBarChart) that is empty on both
  # sides is EMPTY: nothing was compared.  A cardVisual is never EMPTY:
  # a scalar card always returns exactly one row, so compare_card already
  # rejects zero-row DAX results via "dax_row_count".
  if not oracle_rows and not dax_rows:
    return result(STATUS_EMPTY, "empty_result", oracle_rows, dax_rows)
  return result(STATUS_PASS, None, oracle_rows, dax_rows, tie_adjusted)


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
      help=(
          "default | fixture | YYYY-MM-DD:YYYY-MM-DD (repeatable); 'default'"
          " is the report's loaded Current window, read from the model's Date"
          " table; 'fixture' uses the fixture window"
          f" {FIXTURE_WINDOW[0]}:{FIXTURE_WINDOW[1]}"
      ),
  )
  parser.add_argument("--report-id", default=None)
  parser.add_argument("--output")
  parser.add_argument(
      "--allow-empty",
      action="store_true",
      help=(
          "accept grouped visuals (area/bar charts) that are empty on both"
          " sides, and a run in which no oracle query returned any row (for"
          " deliberate empty-window checks); a cardVisual must always return"
          " exactly one row and is never EMPTY"
      ),
  )
  parser.add_argument(
      "--allow-fixture-fallback",
      action="store_true",
      help=(
          "when the default window cannot be read from the Date table, use the"
          " fixture window instead of failing closed"
      ),
  )
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

  # Every explicit window is validated before any query is issued.
  specs = list(dict.fromkeys(args.window or ["default"]))
  explicit = {
      spec: parse_window(spec)
      for spec in specs
      if spec not in ("default", "fixture")
  }
  windows = []
  for spec in specs:
    if spec == "default":
      try:
        start, end, source = resolve_default_window(
            token,
            args.workspace_id,
            args.dataset_id,
            allow_fallback=args.allow_fixture_fallback,
        )
      except RuntimeError:
        results = []
        receipt = {
            "kind": "sanitized-powerbi-live-parity",
            "windows": ["default"],
            "window_details": [
                {
                    "window": "default",
                    "source": "lookup_failed",
                    "charts_expected": len(charts),
                    "charts_pass": 0,
                    "charts_fail": 0,
                    "charts_empty": 0,
                }
            ],
            "allow_empty": args.allow_empty,
            "charts_expected": len(charts),
            "charts_pass": 0,
            "charts_fail": 0,
            "charts_empty": 0,
            "oracle_rows_total": 0,
            "results": results,
            "overall_pass": False,
            "pass": False,
        }
        if args.output:
          output = pathlib.Path(args.output)
          output.parent.mkdir(parents=True, exist_ok=True)
          output.write_text(
              json.dumps(receipt, indent=1, sort_keys=True) + "\n"
          )
        print(
            "live parity FAIL: default window lookup failed",
            file=sys.stderr,
        )
        return 1
      if source != "date_table":
        print(
            "warning: the loaded window could not be read from the Date table;"
            " using the fixture window",
            file=sys.stderr,
        )
    elif spec == "fixture":
      start, end, source = FIXTURE_WINDOW[0], FIXTURE_WINDOW[1], "fixture"
    else:
      (start, end), source = explicit[spec], "explicit"
    windows.append(
        {"label": spec, "start": start, "end": end, "source": source}
    )

  results = []
  for window in windows:
    for chart in charts:
      results.append(_run_chart(chart, window, args, billing_project, token))

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

  def count(status, label=None):
    return sum(
        1
        for r in results
        if r["status"] == status and (label is None or r["window"] == label)
    )

  charts_expected = len(charts) * len(windows)
  charts_pass = count(STATUS_PASS)
  charts_fail = count(STATUS_FAIL)
  charts_empty = count(STATUS_EMPTY)
  oracle_rows_total = sum(r["oracle_row_count"] for r in results)
  overall_pass = (
      charts_expected > 0
      and len(results) == charts_expected
      and charts_fail == 0
      and (args.allow_empty or (charts_empty == 0 and oracle_rows_total > 0))
  )

  receipt = {
      "kind": "sanitized-powerbi-live-parity",
      "git_sha": git_sha,
      "run_timestamp_utc": now_utc,
      "executed_at_utc": now_utc,
      "dataset_id": args.dataset_id,
      "report_id": args.report_id,
      "windows": [w["label"] for w in windows],
      "window_details": [
          {
              "window": w["label"],
              "start": w["start"],
              "end": w["end"],
              "source": w["source"],
              "charts_expected": len(charts),
              "charts_pass": count(STATUS_PASS, w["label"]),
              "charts_fail": count(STATUS_FAIL, w["label"]),
              "charts_empty": count(STATUS_EMPTY, w["label"]),
          }
          for w in windows
      ],
      "allow_empty": args.allow_empty,
      "charts_expected": charts_expected,
      "charts_pass": charts_pass,
      "charts_fail": charts_fail,
      "charts_empty": charts_empty,
      "oracle_rows_total": oracle_rows_total,
      "results": results,
      "overall_pass": overall_pass,
      "pass": overall_pass,
  }

  if args.output:
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(f"sanitized receipt: {output}")

  summary = (
      f"{charts_pass}/{charts_expected} charts PASS across"
      f" {len(windows)} window(s)"
  )
  if overall_pass:
    if charts_empty:
      summary += f", {charts_empty} empty (allowed)"
    print(f"live parity OK: {summary}")
    return 0
  reasons = [f"{charts_fail} failed", f"{charts_empty} empty"]
  if oracle_rows_total == 0:
    reasons.append("no oracle rows returned")
  print(f"live parity FAIL: {summary}; {', '.join(reasons)}", file=sys.stderr)
  return 1


if __name__ == "__main__":
  sys.exit(main())
