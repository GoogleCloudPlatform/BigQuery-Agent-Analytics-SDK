#!/usr/bin/env python3
"""Validate Looker Studio product contracts, chart manifests, and bindings.

Supports ``--profile adk``, ``--profile bqca``, and ``--profile all`` (default).
Verifies 1:1 alignment between product contracts, chart manifests, and report
template bindings, checks SHA-256 digests for SQL, pages, and manifests, and
enforces the BQCA nine-event allowlist (zero tool or LLM_REQUEST events).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sqlite3
import sys
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

BQCA_UNLOGGED_EVENT_TYPES = (
    "TOOL_STARTING",
    "TOOL_COMPLETED",
    "TOOL_ERROR",
    "LLM_REQUEST",
)

_DEFAULT_BASE_TABLE_TYPES = {
    "timestamp": "TIMESTAMP",
    "event_type": "STRING",
    "agent": "STRING",
    "session_id": "STRING",
    "invocation_id": "STRING",
    "user_id": "STRING",
    "trace_id": "STRING",
    "span_id": "STRING",
    "parent_span_id": "STRING",
    "content": "JSON",
    "attributes": "JSON",
    "latency_ms": "JSON",
    "status": "STRING",
    "error_message": "STRING",
    "is_truncated": "BOOL",
}

_SEMANTIC_TYPES_BY_DATA_TYPE = {
    "INT64": {"NUMBER"},
    "DOUBLE": {"NUMBER"},
    "STRING": {"TEXT"},
    "BOOL": {"BOOLEAN"},
    "DATE": {"YEAR_MONTH_DAY"},
    "TIMESTAMP": {"YEAR_MONTH_DAY_SECOND", "YEAR_MONTH_DAY_HOUR"},
}

_EXPECTED_CHART_METRIC_AGGREGATIONS = {
    "total_tokens": "SUM",
    "input_tokens": "SUM",
    "output_tokens": "SUM",
    "thoughts_tokens": "SUM",
    "cached_tokens": "SUM",
    "similar_queries_count": "SUM",
    "turn_latency_ms": "AVG",
    "llm_latency_ms": "AVG",
    "ttft_ms": "AVG",
    "total_latency_ms": "AVG",
    "session_id": "COUNT_DISTINCT",
    "invocation_id": "COUNT_DISTINCT",
    "completed_turn_id": "COUNT_DISTINCT",
    "error_message": "COUNT",
    "span_id": "COUNT",
    "extracted_sql": "COUNT",
}

_SPEC_KIND_BY_CHART_TYPE = {
    "SCORECARD": "scorecard",
    "bar_chart": "barChart",
    "line_chart": "lineChart",
    "pie_chart": "pieChart",
    "table": "table",
}


def canonical_pages_sha256(pages: list[dict[str, Any]]) -> str:
  """Return the deterministic SHA-256 hex digest for a contract pages list."""
  payload = json.dumps(pages, sort_keys=True, separators=(",", ":")).encode(
      "utf-8"
  )
  return hashlib.sha256(payload).hexdigest()


def _split_top_level_csv(select_body: str) -> list[str]:
  """Split a SQL SELECT list on top-level commas outside parens and strings."""
  items: list[str] = []
  buf: list[str] = []
  depth = 0
  in_single = False
  i = 0
  while i < len(select_body):
    ch = select_body[i]
    if in_single:
      buf.append(ch)
      if ch == "\\" and i + 1 < len(select_body):
        buf.append(select_body[i + 1])
        i += 2
        continue
      if ch == "'":
        in_single = False
    else:
      if ch == "'":
        in_single = True
        buf.append(ch)
      elif ch == "(":
        depth += 1
        buf.append(ch)
      elif ch == ")":
        depth = max(0, depth - 1)
        buf.append(ch)
      elif ch == "," and depth == 0:
        item = "".join(buf).strip()
        if item:
          items.append(item)
        buf = []
      else:
        buf.append(ch)
    i += 1
  tail = "".join(buf).strip()
  if tail:
    items.append(tail)
  return items


def _infer_sql_expr_data_type(expr: str, base_types: dict[str, str]) -> str:
  """Infer the Looker Studio data_type for a projected SQL expression."""
  cleaned = expr.strip()
  if cleaned in base_types:
    return base_types[cleaned]
  if re.match(r"^DATE\s*\(", cleaned, re.IGNORECASE):
    return "DATE"
  if re.match(r"^TIMESTAMP(?:_TRUNC)?\s*\(", cleaned, re.IGNORECASE):
    return "TIMESTAMP"
  if (
      cleaned == "raw_is_error"
      or re.search(r"\bLOGICAL_(?:OR|AND)\s*\(", cleaned, re.IGNORECASE)
      or re.search(r"=\s*'true'\s*,\s*FALSE\s*\)$", cleaned, re.IGNORECASE)
      or re.search(r"=\s*1\s*,\s*FALSE\s*\)$", cleaned, re.IGNORECASE)
      or re.search(r">\s*0\s*,\s*FALSE\s*\)$", cleaned, re.IGNORECASE)
      or re.search(
          r"ENDS_WITH\s*\([^)]*'_ERROR'\)\s*\)$", cleaned, re.IGNORECASE
      )
      or re.search(r"=\s*'INVOCATION_(?:STARTING|COMPLETED)'\s*$", cleaned)
      or re.search(
          r"ROW_NUMBER\s*\(\)\s*OVER\s*\(.*\)\s*=\s*1\s*\)$", cleaned, re.S
      )
  ):
    return "BOOL"
  if re.search(r"\bAS\s+FLOAT64\b", cleaned, re.IGNORECASE):
    return "DOUBLE"
  if re.search(r"\bAS\s+INT64\b", cleaned, re.IGNORECASE) or re.search(
      r"\bARRAY_LENGTH\s*\(", cleaned, re.IGNORECASE
  ):
    return "INT64"
  return "STRING"


def extract_sql_projected_columns(
    sql_text: str,
    base_table_types: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
  """Extract ordered (column_name, data_type) pairs projected by BQCA SQL."""
  base_types = dict(_DEFAULT_BASE_TABLE_TYPES)
  if base_table_types:
    base_types.update(base_table_types)

  cte_match = re.search(
      r"bqca_fields\s+AS\s*\(\s*SELECT\b(.*?)\bFROM\s+(?:bqca_events|raw_events)\s*\)",
      sql_text,
      re.IGNORECASE | re.DOTALL,
  )
  outer_match = re.search(
      r"\)\s*SELECT\b(.*?)\bFROM\s+bqca_fields\b",
      sql_text,
      re.IGNORECASE | re.DOTALL,
  )
  if not cte_match or not outer_match:
    raise ValueError("Unable to parse bqca_fields CTE or outer SELECT")

  def _parse_select_items(
      body: str, known: list[tuple[str, str]]
  ) -> list[tuple[str, str]]:
    known_map = dict(known)
    cols: list[tuple[str, str]] = []
    for item in _split_top_level_csv(body):
      if item == "*":
        cols.extend(known)
        continue
      except_match = re.match(
          r"^\*\s+EXCEPT\s*\(([^)]+)\)\s*$", item, re.IGNORECASE
      )
      if except_match:
        excluded = {
            c.strip() for c in except_match.group(1).split(",") if c.strip()
        }
        cols.extend((k, v) for k, v in known if k not in excluded)
        continue
      alias_match = re.search(
          r"^(.*?)\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$",
          item,
          re.IGNORECASE | re.DOTALL,
      )
      if alias_match:
        expr, col_name = alias_match.group(1).strip(), alias_match.group(2)
        dtype = _infer_sql_expr_data_type(expr, {**base_types, **known_map})
        cols.append((col_name, dtype))
      else:
        col_name = item.strip()
        dtype = known_map.get(col_name, base_types.get(col_name, "STRING"))
        cols.append((col_name, dtype))
    return cols

  cte_cols = _parse_select_items(cte_match.group(1), [])
  return _parse_select_items(outer_match.group(1), cte_cols)


def _validate_component_embedded_spec(
    comp: dict[str, Any],
    *,
    datasource_id: str | None,
    field_by_name: dict[str, dict[str, Any]],
    field_by_display: dict[str, dict[str, Any]],
) -> list[str]:
  """Validate a manifest component's embedded Looker Studio spec block."""
  errors: list[str] = []
  comp_id = comp.get("id", "<unknown>")
  spec = comp.get("spec")
  if not isinstance(spec, dict):
    return [f"component {comp_id} missing embedded spec mapping"]

  if spec.get("id") != comp.get("component_id"):
    errors.append(
        f"component {comp_id} spec.id {spec.get('id')!r} !="
        f" component_id {comp.get('component_id')!r}"
    )

  chart_type = comp.get("chart_type")
  spec_kind = _SPEC_KIND_BY_CHART_TYPE.get(chart_type)
  if not spec_kind or not isinstance(spec.get(spec_kind), dict):
    errors.append(
        f"component {comp_id} missing embedded spec.{spec_kind} block for"
        f" chart_type {chart_type!r}"
    )
    return errors

  inner = spec[spec_kind]
  dataset = inner.get("dataset", {})
  if dataset.get("datasourceId") != datasource_id:
    errors.append(
        f"component {comp_id} spec.{spec_kind}.dataset.datasourceId mismatch"
    )

  spec_cols = dataset.get("columns", [])
  if not isinstance(spec_cols, list) or not spec_cols:
    errors.append(f"component {comp_id} spec.{spec_kind}.dataset.columns empty")
    return errors

  col_by_id: dict[str, dict[str, Any]] = {}
  spec_field_names: list[str] = []
  for col in spec_cols:
    cid = col.get("id")
    fname = col.get("fieldName")
    if isinstance(cid, str):
      col_by_id[cid] = col
    if isinstance(fname, str):
      spec_field_names.append(fname)
    if fname not in field_by_name:
      errors.append(
          f"component {comp_id} embedded spec references unknown fieldName"
          f" {fname!r}"
      )

  if spec_field_names != comp.get("columns", []):
    errors.append(
        f"component {comp_id} embedded spec column fieldNames"
        f" {spec_field_names} != summary columns {comp.get('columns')}"
    )

  fields_cfg = inner.get("fields", {})
  if comp.get("category") == "scorecard":
    primary_id = fields_cfg.get("primaryMetricId")
    primary_col = (
        col_by_id.get(primary_id) if isinstance(primary_id, str) else None
    )
    if not primary_col or len(spec_cols) != 1:
      errors.append(
          f"scorecard {comp_id} embedded spec must bind a single primaryMetricId"
      )
      return errors
    expected_field = comp.get("field")
    expected_ds_field = field_by_display.get(expected_field, {})
    if primary_col.get("fieldName") != expected_ds_field.get("name"):
      errors.append(
          f"scorecard {comp_id} embedded spec fieldName"
          f" {primary_col.get('fieldName')!r} != _{expected_field}_"
      )
    expected_agg = comp.get("aggregation")
    actual_agg = primary_col.get("aggregation")
    if actual_agg != expected_agg:
      errors.append(
          f"scorecard {comp_id} embedded spec aggregation {actual_agg!r} !="
          f" {expected_agg!r}"
      )
    if expected_ds_field.get("data_type") == "STRING" and actual_agg in {
        "SUM",
        "AVG",
    }:
      errors.append(
          f"scorecard {comp_id} cannot apply numeric aggregation {actual_agg!r}"
          f" to STRING field {expected_field!r}"
      )
  else:
    if spec_kind in ("barChart", "lineChart"):
      dim_ids = [fields_cfg.get("primaryAxis", {}).get("fieldId")]
      met_ids = list(fields_cfg.get("metricSeries", {}).get("metricIds", []))
    elif spec_kind == "pieChart":
      dim_ids = [fields_cfg.get("dimensionId")]
      met_ids = [fields_cfg.get("metricId")]
    elif spec_kind == "table":
      dim_ids = list(fields_cfg.get("dimensionIds", []))
      met_ids = list(fields_cfg.get("metricIds", []))
    else:
      dim_ids, met_ids = [], []

    expected_dims = list(comp.get("dimensions", []))
    actual_dim_fields: list[str] = []
    for did in dim_ids:
      dcol = col_by_id.get(did) if isinstance(did, str) else None
      if not dcol:
        errors.append(
            f"chart {comp_id} embedded spec missing dimension column id {did!r}"
        )
        continue
      if "aggregation" in dcol:
        errors.append(
            f"chart {comp_id} dimension column {did!r} must not declare"
            f" aggregation {dcol.get('aggregation')!r}"
        )
      ds_f = field_by_name.get(dcol.get("fieldName", ""), {})
      actual_dim_fields.append(ds_f.get("display_name", ""))
    if actual_dim_fields != expected_dims:
      errors.append(
          f"chart {comp_id} embedded spec dimensions {actual_dim_fields} !="
          f" {expected_dims}"
      )

    raw_mets = list(comp.get("metrics", []))
    expected_met_fields = [
        m["field"] if isinstance(m, dict) else m for m in raw_mets
    ]
    actual_met_fields: list[str] = []
    for idx, mid in enumerate(met_ids):
      mcol = col_by_id.get(mid) if isinstance(mid, str) else None
      if not mcol:
        errors.append(
            f"chart {comp_id} embedded spec missing metric column id {mid!r}"
        )
        continue
      ds_f = field_by_name.get(mcol.get("fieldName", ""), {})
      m_display = ds_f.get("display_name", "")
      actual_met_fields.append(m_display)
      actual_agg = mcol.get("aggregation")
      expected_agg = None
      if idx < len(raw_mets) and isinstance(raw_mets[idx], dict):
        expected_agg = raw_mets[idx].get("aggregation")
      if expected_agg is None:
        expected_agg = _EXPECTED_CHART_METRIC_AGGREGATIONS.get(m_display)
      if not actual_agg or (
          expected_agg is not None and actual_agg != expected_agg
      ):
        errors.append(
            f"chart {comp_id} metric {m_display!r} embedded spec aggregation"
            f" {actual_agg!r} != expected {expected_agg!r}"
        )
      if ds_f.get("data_type") == "STRING" and actual_agg in {"SUM", "AVG"}:
        errors.append(
            f"chart {comp_id} cannot apply numeric aggregation {actual_agg!r}"
            f" to STRING field {m_display!r}"
        )
    if actual_met_fields != expected_met_fields:
      errors.append(
          f"chart {comp_id} embedded spec metrics {actual_met_fields} !="
          f" {expected_met_fields}"
      )

  return errors


def _load_yaml(path: pathlib.Path) -> dict[str, Any]:
  data = yaml.safe_load(path.read_text())
  if not isinstance(data, dict):
    raise ValueError(f"{path}: expected YAML mapping")
  return data


def validate_adk(root: pathlib.Path = ROOT) -> list[str]:
  """Return a list of contract validation errors for the ADK profile."""
  errors: list[str] = []
  manifest = _load_yaml(root / "spec/chart_manifest.yaml")
  contract = _load_yaml(root / "spec/product_contract.yaml")
  binding = _load_yaml(root / "bindings/report_template.yaml")
  sql_bytes = (root / "sql/events_v1.template.sql").read_bytes()
  sql_sha = hashlib.sha256(sql_bytes).hexdigest()

  manifest_charts = manifest.get("charts", [])
  contract_charts = contract.get("charts", [])
  if len(manifest_charts) != 37:
    errors.append(
        f"adk chart_manifest expected 37 charts, got {len(manifest_charts)}"
    )
  if len(contract_charts) != 37:
    errors.append(
        f"adk product_contract expected 37 charts, got {len(contract_charts)}"
    )
  if {c["id"] for c in manifest_charts} != {c["id"] for c in contract_charts}:
    errors.append("adk chart_manifest and product_contract chart IDs diverge")
  if len(contract.get("pages", [])) != 8:
    errors.append(
        f"adk product_contract expected 8 pages, got {len(contract.get('pages', []))}"
    )
  if binding.get("reviewed_template_sql", {}).get("sha256") != sql_sha:
    errors.append("adk reviewed_template_sql.sha256 does not match SQL bytes")
  if (
      binding.get("live_template_verification", {}).get("repository_sql_sha256")
      != sql_sha
  ):
    errors.append(
        "adk live_template_verification.repository_sql_sha256 does not match"
        " SQL bytes"
    )
  return errors


def _strip_sql_comments(sql_text: str) -> str:
  """Strip single-line (--) and block (/* */) SQL comments outside quotes."""
  out: list[str] = []
  i = 0
  n = len(sql_text)
  in_single = False
  in_backtick = False
  while i < n:
    ch = sql_text[i]
    if in_single:
      out.append(ch)
      if ch == "\\" and i + 1 < n:
        out.append(sql_text[i + 1])
        i += 2
        continue
      if ch == "'":
        in_single = False
      i += 1
    elif in_backtick:
      out.append(ch)
      if ch == "`":
        in_backtick = False
      i += 1
    else:
      if ch == "'":
        in_single = True
        out.append(ch)
        i += 1
      elif ch == "`":
        in_backtick = True
        out.append(ch)
        i += 1
      elif ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
        i += 2
        while i < n and sql_text[i] != "\n":
          i += 1
      elif ch == "/" and i + 1 < n and sql_text[i + 1] == "*":
        i += 2
        while i + 1 < n and not (sql_text[i] == "*" and sql_text[i + 1] == "/"):
          i += 1
        i += 2
      else:
        out.append(ch)
        i += 1
  return "".join(out)


def _validate_bqca_sql_behavioral_oracle(sql_text: str) -> list[str]:
  """Execute BQCA SQL against synthetic edge-case turns in SQLite."""
  try:
    cols = [c[0] for c in extract_sql_projected_columns(sql_text)]
  except ValueError as exc:
    return [f"bqca SQL behavioral oracle parse failure: {exc}"]
  if len(cols) < 37:
    return [
        f"bqca SQL behavioral oracle expected >= 37 bqca_fields columns, got {len(cols)}"
    ]
  bqca_cols = cols[:37] + ["raw_agent_response_rn"]

  s = _strip_sql_comments(sql_text)
  s = re.sub(r"FROM\s+`[^`]+`", "FROM source_events", s)
  s = s.replace("@DS_START_DATE", "'20260401'").replace(
      "@DS_END_DATE", "'20260430'"
  )
  s = re.sub(
      r"ARRAY_TO_STRING\s*\(\s*ARRAY\s*\(\s*SELECT\s+JSON_VALUE\s*\(\s*\w+\s*,\s*('[^']+')\s*\)\s*FROM\s+UNNEST\s*\(\s*JSON_QUERY_ARRAY\s*\(\s*content\s*,\s*('[^']+')\s*\)\s*\).*?\)\s*,\s*('[^']*')\s*\)",
      lambda m: (
          f"BQ_JSON_PARTS_JOIN(content, {m.group(2)}, {m.group(1)},"
          f" {m.group(3)})"
      ),
      s,
      flags=re.DOTALL | re.IGNORECASE,
  )
  s = re.sub(
      r"ARRAY_LENGTH\s*\(\s*JSON_QUERY_ARRAY\s*\(\s*content\s*,\s*('[^']+')\s*\)\s*\)",
      r"BQ_JSON_ARRAY_LENGTH(content, \1)",
      s,
      flags=re.IGNORECASE,
  )
  s = re.sub(r"\br'", "'", s)
  s = re.sub(r"\bSAFE_CAST\s*\(", "CAST(", s, flags=re.IGNORECASE)
  s = re.sub(r"\bAS\s+STRING\b", "AS TEXT", s, flags=re.IGNORECASE)
  s = re.sub(r"\bAS\s+FLOAT64\b", "AS REAL", s, flags=re.IGNORECASE)
  s = re.sub(r"\bAS\s+INT64\b", "AS INTEGER", s, flags=re.IGNORECASE)
  s = re.sub(
      r"FIRST_VALUE\s*\((.*?)\s+IGNORE\s+NULLS\s*\)\s+OVER\s*\(\s*PARTITION\s+BY\s+(.*?)\s+ORDER\s+BY\s+(.*?)\s+ROWS\s+BETWEEN\s+UNBOUNDED\s+PRECEDING\s+AND\s+UNBOUNDED\s+FOLLOWING\s*\)",
      lambda m: (
          f"FIRST_VALUE({m.group(1)}) OVER (PARTITION BY {m.group(2)} ORDER BY"
          f" CASE WHEN ({m.group(1)}) IS NULL THEN 1 ELSE 0 END ASC,"
          f" {m.group(3)} ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED"
          " FOLLOWING)"
      ),
      s,
      flags=re.DOTALL | re.IGNORECASE,
  )
  s = re.sub(
      r"LAST_VALUE\s*\((.*?)\s+IGNORE\s+NULLS\s*\)\s+OVER\s*\(\s*PARTITION\s+BY\s+(.*?)\s+ORDER\s+BY\s+(.*?)\s+ROWS\s+BETWEEN\s+UNBOUNDED\s+PRECEDING\s+AND\s+UNBOUNDED\s+FOLLOWING\s*\)",
      lambda m: (
          f"LAST_VALUE({m.group(1)}) OVER (PARTITION BY {m.group(2)} ORDER BY"
          f" CASE WHEN ({m.group(1)}) IS NULL THEN 0 ELSE 1 END ASC,"
          f" {m.group(3)} ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED"
          " FOLLOWING)"
      ),
      s,
      flags=re.DOTALL | re.IGNORECASE,
  )
  s = re.sub(
      r"FIRST_VALUE\s*\((.*?)\s+RESPECT\s+NULLS\s*\)\s+OVER\s*\(",
      r"FIRST_VALUE(\1) OVER (",
      s,
      flags=re.DOTALL | re.IGNORECASE,
  )
  s = re.sub(
      r"LAST_VALUE\s*\((.*?)\s+RESPECT\s+NULLS\s*\)\s+OVER\s*\(",
      r"LAST_VALUE(\1) OVER (",
      s,
      flags=re.DOTALL | re.IGNORECASE,
  )
  s = re.sub(
      r"\bLOGICAL_OR\s*\(([^()]+)\)\s+OVER\s*\(",
      r"MAX(CASE WHEN (\1) THEN 1 ELSE 0 END) OVER (",
      s,
      flags=re.IGNORECASE,
  )
  s = re.sub(
      r"\bLOGICAL_AND\s*\(([^()]+)\)\s+OVER\s*\(",
      r"MIN(CASE WHEN (\1) IS NULL THEN NULL WHEN (\1) THEN 1 ELSE 0 END) OVER (",
      s,
      flags=re.IGNORECASE,
  )
  s = re.sub(
      r"DATE_ADD\s*\((.*?),\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
      r"DATE_ADD_DAY(\1, \2)",
      s,
      flags=re.DOTALL | re.IGNORECASE,
  )
  s = re.sub(
      r"TIMESTAMP_TRUNC\s*\(\s*timestamp\s*,\s*HOUR\s*,\s*'UTC'\s*\)",
      "TIMESTAMP_TRUNC_HOUR(timestamp, 'UTC')",
      s,
      flags=re.IGNORECASE,
  )
  s = re.sub(r"\bTIMESTAMP\s*\(", "BQ_TIMESTAMP(", s, flags=re.IGNORECASE)
  s = re.sub(r"\bDATE\s*\(", "BQ_DATE(", s, flags=re.IGNORECASE)
  s = re.sub(
      r"\*\s+EXCEPT\s*\(([^)]+)\)",
      lambda m: ", ".join(
          c
          for c in bqca_cols
          if c not in {x.strip() for x in m.group(1).split(",")}
      ),
      s,
      flags=re.IGNORECASE,
  )

  def _resolve_json_path(doc: Any, path: Any) -> Any:
    if doc is None or path is None:
      return None
    try:
      cur = json.loads(doc) if isinstance(doc, str) else doc
    except Exception:
      return None
    p = str(path)
    if p.startswith("$"):
      p = p[1:]
    tokens = re.findall(r'\."([^"]+)"|\.([A-Za-z0-9_]+)|\[(\d+)\]', p)
    for quoted, bare, idx in tokens:
      if quoted or bare:
        key = quoted or bare
        if isinstance(cur, dict) and key in cur:
          cur = cur[key]
        else:
          return None
      elif idx != "":
        i = int(idx)
        if isinstance(cur, list) and 0 <= i < len(cur):
          cur = cur[i]
        else:
          return None
    return cur

  def _bq_json_value(doc: Any, path: Any) -> str | None:
    val = _resolve_json_path(doc, path)
    if val is None or isinstance(val, (dict, list)):
      return None
    if isinstance(val, bool):
      return "true" if val else "false"
    return str(val)

  def _bq_json_parts_join(
      doc: Any, arr_path: Any, item_path: Any, sep: Any
  ) -> str | None:
    arr = _resolve_json_path(doc, arr_path)
    if not isinstance(arr, list):
      return None
    parts = [
        v for item in arr if (v := _bq_json_value(item, item_path)) is not None
    ]
    return str(sep).join(parts)

  def _bq_json_array_length(doc: Any, arr_path: Any) -> int | None:
    arr = _resolve_json_path(doc, arr_path)
    return len(arr) if isinstance(arr, list) else None

  def _regexp_extract(val: Any, pat: Any) -> str | None:
    if val is None or pat is None:
      return None
    m = re.search(str(pat), str(val))
    if not m:
      return None
    return m.group(1) if m.lastindex else m.group(0)

  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.create_function("IF", 3, lambda cond, t, f: t if bool(cond) else f)
  conn.create_function(
      "CONCAT",
      -1,
      lambda *args: None
      if any(a is None for a in args)
      else "".join(str(a) for a in args),
  )
  conn.create_function("JSON_VALUE", 2, _bq_json_value)
  conn.create_function("BQ_JSON_PARTS_JOIN", 4, _bq_json_parts_join)
  conn.create_function("BQ_JSON_ARRAY_LENGTH", 2, _bq_json_array_length)
  conn.create_function(
      "TO_JSON_STRING",
      1,
      lambda v: v if (v is None or isinstance(v, str)) else json.dumps(v),
  )
  conn.create_function(
      "SHA256",
      1,
      lambda v: None
      if v is None
      else hashlib.sha256(str(v).encode("utf-8")).digest(),
  )
  conn.create_function(
      "TO_HEX",
      1,
      lambda b: None
      if b is None
      else (
          b.hex()
          if isinstance(b, (bytes, bytearray))
          else str(b).encode("utf-8").hex()
      ),
  )
  conn.create_function(
      "ENDS_WITH",
      2,
      lambda s_val, suf: None
      if (s_val is None or suf is None)
      else (1 if str(s_val).endswith(str(suf)) else 0),
  )
  conn.create_function("REGEXP_EXTRACT", 2, _regexp_extract)
  conn.create_function(
      "PARSE_DATE",
      2,
      lambda fmt, s_val: f"{s_val[:4]}-{s_val[4:6]}-{s_val[6:8]}"
      if s_val and len(str(s_val)) == 8
      else None,
  )
  conn.create_function(
      "DATE_ADD_DAY",
      2,
      lambda d, n: f"{d[:8]}{int(d[8:10]) + int(n):02d}" if d else None,
  )
  conn.create_function(
      "BQ_TIMESTAMP", 2, lambda d, tz: f"{d}T00:00:00Z" if d else None
  )
  conn.create_function(
      "BQ_DATE", 2, lambda ts, tz: str(ts)[:10] if ts else None
  )
  conn.create_function(
      "TIMESTAMP_TRUNC_HOUR",
      2,
      lambda ts, tz: f"{str(ts)[:13]}:00:00Z" if ts else None,
  )

  conn.execute("""
      CREATE TABLE source_events (
        timestamp TEXT, event_type TEXT, agent TEXT, session_id TEXT,
        invocation_id TEXT, user_id TEXT, trace_id TEXT, span_id TEXT,
        parent_span_id TEXT, content TEXT, attributes TEXT, latency_ms TEXT,
        status TEXT, error_message TEXT, is_truncated INTEGER
      )
  """)
  fixtures = [
      (
          "2026-04-05T10:00:00Z",
          "INVOCATION_STARTING",
          "root",
          "sess-1",
          "inv-edge-1",
          "  alice@example.com:1  ",
          "trace-1",
          "span-1a",
          None,
          "{}",
          json.dumps(
              {
                  "fast_path": "true",
                  "session_metadata": {
                      "state": {
                          "conversation-id": "  conv-1  ",
                          "data-agent-id": "  agent-alpha  ",
                          "custom_labels": {"persona": "  analyst  "},
                      }
                  },
              }
          ),
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T10:00:01Z",
          "LLM_RESPONSE",
          "root",
          "sess-1",
          "inv-edge-1",
          None,
          "trace-1",
          "span-1b",
          None,
          "{}",
          json.dumps(
              {
                  "usage_metadata": {
                      "prompt_token_count": 100,
                      "candidates_token_count": 50,
                      "total_token_count": 150,
                  }
              }
          ),
          json.dumps({"total_ms": 400, "time_to_first_token_ms": 80}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T10:00:02Z",
          "INVOCATION_COMPLETED",
          "root",
          "sess-1",
          "inv-edge-1",
          None,
          "trace-1",
          "span-1c",
          None,
          "{}",
          "{}",
          json.dumps({"total_ms": 1200}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T10:00:03Z",
          "INVOCATION_COMPLETED",
          "root",
          "sess-1",
          "inv-edge-1",
          None,
          "trace-1",
          "span-1d",
          None,
          "{}",
          "{}",
          json.dumps({"total_ms": 2500}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T10:01:00Z",
          "INVOCATION_STARTING",
          "root",
          "sess-1",
          "inv-edge-1b",
          None,
          "trace-1b",
          "span-1e",
          None,
          "{}",
          "{}",
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T10:01:01Z",
          "AGENT_ERROR",
          "root",
          "sess-1",
          "inv-edge-1b",
          None,
          "trace-1b",
          "span-1f",
          None,
          "{}",
          "{}",
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T10:01:02Z",
          "INVOCATION_COMPLETED",
          "root",
          "sess-1",
          "inv-edge-1b",
          None,
          "trace-1b",
          "span-1g",
          None,
          "{}",
          "{}",
          json.dumps({"total_ms": 1800}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T11:00:00Z",
          "INVOCATION_STARTING",
          "root",
          "sess-2",
          "inv-late-attr",
          None,
          "trace-2",
          "span-2a",
          None,
          "{}",
          "{}",
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T11:00:01Z",
          "AGENT_RESPONSE",
          "root",
          "sess-2",
          "inv-late-attr",
          None,
          "trace-2",
          "span-2b",
          None,
          json.dumps({"text_summary": "```sql\nSELECT 42\n```"}),
          json.dumps(
              {
                  "session_metadata": {
                      "state": {
                          "conversation-id": "conv-2",
                          "data-agent-id": "agent-beta",
                          "custom_labels": {"persona": "manager"},
                      }
                  }
              }
          ),
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T11:00:02Z",
          "INVOCATION_COMPLETED",
          "root",
          "sess-2",
          "inv-late-attr",
          None,
          "trace-2",
          "span-2c",
          None,
          "{}",
          json.dumps(
              {
                  "session_metadata": {
                      "state": {
                          "conversation-id": "conv-2-override",
                          "data-agent-id": "agent-beta-override",
                          "custom_labels": {"persona": "override"},
                      }
                  }
              }
          ),
          json.dumps({"total_ms": 900}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T12:00:00Z",
          "INVOCATION_STARTING",
          "root",
          "sess-3",
          "   ",
          None,
          "trace-partial",
          "span-3a",
          None,
          "{}",
          json.dumps(
              {"session_metadata": {"state": {"data-agent-id": "agent-gamma"}}}
          ),
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T12:00:01Z",
          "INVOCATION_COMPLETED",
          "root",
          "sess-3",
          "inv-partial-3",
          None,
          "trace-partial",
          "span-3b",
          None,
          "{}",
          "{}",
          json.dumps({"total_ms": 1500}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T13:00:00Z",
          "INVOCATION_STARTING",
          "root",
          "sess-4",
          "  ",
          None,
          "trace-fallback-4",
          "span-4a",
          None,
          "{}",
          "{}",
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T14:00:00Z",
          "INVOCATION_COMPLETED",
          "root",
          "sess-5",
          None,
          None,
          "trace-null-5",
          "span-5a",
          None,
          "{}",
          json.dumps(
              {
                  "session_metadata": {
                      "state": {"data-agent-id": "agent-orphan-a"}
                  }
              }
          ),
          json.dumps({"total_ms": 9999}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T14:00:00Z",
          "LLM_RESPONSE",
          "root",
          "sess-5",
          None,
          None,
          "trace-null-5",
          "span-5b",
          None,
          "{}",
          json.dumps({"fast_path": "true"}),
          json.dumps({"total_ms": 100}),
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T15:00:00Z",
          "AGENT_RESPONSE",
          "root",
          "sess-6",
          None,
          None,
          "trace-null-6",
          None,
          None,
          json.dumps({"text_summary": "```sql\nSELECT 101\n```"}),
          "{}",
          None,
          "OK",
          None,
          0,
      ),
      (
          "2026-04-05T15:00:00Z",
          "AGENT_RESPONSE",
          "root",
          "sess-6",
          None,
          None,
          "trace-null-6",
          None,
          None,
          json.dumps({"text_summary": "```sql\nSELECT 202\n```"}),
          "{}",
          None,
          "OK",
          None,
          0,
      ),
  ]
  conn.executemany(
      "INSERT INTO source_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      fixtures,
  )
  try:
    all_rows = [dict(r) for r in conn.execute(s).fetchall()]
  except sqlite3.Error as exc:
    conn.close()
    return [f"bqca SQL behavioral oracle execution error: {exc}"]
  conn.close()
  by_span = {r["span_id"]: r for r in all_rows if r.get("span_id") is not None}

  errs: list[str] = []
  orphan_sqls = {
      r.get("extracted_sql")
      for r in all_rows
      if r.get("timestamp") == "2026-04-05T15:00:00Z"
  }
  if orphan_sqls != {"SELECT 101", "SELECT 202"}:
    errs.append(
        "bqca SQL behavioral oracle: null-invocation AGENT_RESPONSE"
        f" partition collision ({orphan_sqls!r} != {{'SELECT 101', 'SELECT 202'}})"
    )
  for sp in ("span-1a", "span-1b", "span-1c", "span-1d"):
    r = by_span.get(sp, {})
    if r.get("data_agent_id") != "agent-alpha":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} data_agent_id={r.get('data_agent_id')!r} != 'agent-alpha'"
      )
    if r.get("conversation_id") != "conv-1":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} conversation_id={r.get('conversation_id')!r} != 'conv-1'"
      )
    if r.get("persona") != "analyst":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} persona={r.get('persona')!r} != 'analyst'"
      )
    if not r.get("fast_path") or r.get("fast_path_label") != "fast_path":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} fast_path={r.get('fast_path')!r}/{r.get('fast_path_label')!r} != 1/'fast_path'"
      )
  if (
      by_span.get("span-1c", {}).get("is_turn_complete")
      or by_span.get("span-1c", {}).get("completed_turn_id") is not None
      or by_span.get("span-1c", {}).get("turn_latency_ms") is not None
  ):
    errs.append(
        "bqca SQL behavioral oracle: span-1c duplicate completion was not suppressed"
    )
  if (
      not by_span.get("span-1d", {}).get("is_turn_complete")
      or by_span.get("span-1d", {}).get("completed_turn_id") != "inv-edge-1"
      or by_span.get("span-1d", {}).get("turn_latency_ms") != 2500.0
  ):
    errs.append(
        "bqca SQL behavioral oracle: span-1d canonical completion"
        f" mismatch ({by_span.get('span-1d', {})})"
    )
  if (
      not by_span.get("span-1f", {}).get("is_error")
      or by_span.get("span-1f", {}).get("error_message")
      != "[AGENT_ERROR: status=OK]"
  ):
    errs.append(
        "bqca SQL behavioral oracle: span-1f *_ERROR suffix detection"
        f" mismatch ({by_span.get('span-1f', {})})"
    )
  if (
      not by_span.get("span-1g", {}).get("is_turn_complete")
      or by_span.get("span-1g", {}).get("completed_turn_id") != "inv-edge-1b"
      or by_span.get("span-1g", {}).get("turn_latency_ms") != 1800.0
  ):
    errs.append(
        "bqca SQL behavioral oracle: span-1g multi-turn session completion"
        f" mismatch ({by_span.get('span-1g', {})})"
    )
  for sp in ("span-2a", "span-2b", "span-2c"):
    r = by_span.get(sp, {})
    if r.get("data_agent_id") != "agent-beta":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} data_agent_id={r.get('data_agent_id')!r} != 'agent-beta'"
      )
    if r.get("conversation_id") != "conv-2":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} conversation_id={r.get('conversation_id')!r} != 'conv-2'"
      )
    if r.get("persona") != "manager":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} persona={r.get('persona')!r} != 'manager'"
      )
    if r.get("fast_path") or r.get("fast_path_label") != "standard_nl2sql":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} fast_path={r.get('fast_path')!r}/{r.get('fast_path_label')!r}"
      )
  for sp in ("span-3a", "span-3b"):
    r = by_span.get(sp, {})
    if r.get("invocation_id") != "inv-partial-3":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} invocation_id={r.get('invocation_id')!r} != 'inv-partial-3'"
      )
    if r.get("data_agent_id") != "agent-gamma":
      errs.append(
          f"bqca SQL behavioral oracle: {sp} data_agent_id={r.get('data_agent_id')!r} != 'agent-gamma'"
      )
  if (
      by_span.get("span-3b", {}).get("completed_turn_id") != "inv-partial-3"
      or by_span.get("span-3b", {}).get("turn_latency_ms") != 1500.0
  ):
    errs.append(
        "bqca SQL behavioral oracle: span-3b partial-blank completion mismatch"
    )
  if by_span.get("span-4a", {}).get("invocation_id") != "trace-fallback-4":
    errs.append(
        "bqca SQL behavioral oracle: span-4a blank invocation_id did not fall"
        " back to trace_id"
    )
  if (
      by_span.get("span-5a", {}).get("invocation_id") is not None
      or by_span.get("span-5a", {}).get("is_turn_complete")
      or by_span.get("span-5a", {}).get("completed_turn_id") is not None
      or by_span.get("span-5a", {}).get("turn_latency_ms") is not None
  ):
    errs.append(
        "bqca SQL behavioral oracle: span-5a null invocation_id leaked into"
        " completed turn metrics"
    )
  if (
      by_span.get("span-5a", {}).get("fast_path")
      or by_span.get("span-5b", {}).get("data_agent_id") != "unattributed"
  ):
    errs.append(
        "bqca SQL behavioral oracle: span-5a/5b null invocation_id partition"
        " bleed"
    )
  return errs


def validate_bqca(
    root: pathlib.Path = ROOT,
    *,
    contract_override: dict[str, Any] | None = None,
    manifest_override: dict[str, Any] | None = None,
    binding_override: dict[str, Any] | None = None,
    sql_override: str | None = None,
    compat_override: dict[str, Any] | None = None,
    bundle_override: dict[str, Any] | None = None,
) -> list[str]:
  """Return a list of contract validation errors for the BQCA profile."""
  errors: list[str] = []
  manifest_path = root / "spec/bqca_chart_manifest.yaml"
  contract_path = root / "spec/bqca_product_contract.yaml"
  binding_path = root / "bindings/bqca_report_template.yaml"
  sql_path = root / "sql/bqca_events_v1.template.sql"
  compat_path = root / "spec/bqca_compatibility_profile.json"
  bundle_path = root / "spec/bqca_dashboard_template.json"

  manifest = (
      manifest_override
      if manifest_override is not None
      else _load_yaml(manifest_path)
  )
  contract = (
      contract_override
      if contract_override is not None
      else _load_yaml(contract_path)
  )
  binding = (
      binding_override
      if binding_override is not None
      else _load_yaml(binding_path)
  )
  sql_text = (
      sql_override
      if sql_override is not None
      else sql_path.read_text(encoding="utf-8")
  )
  compat = (
      compat_override
      if compat_override is not None
      else json.loads(compat_path.read_text(encoding="utf-8"))
  )
  bundle = (
      bundle_override
      if bundle_override is not None
      else json.loads(bundle_path.read_text(encoding="utf-8"))
  )

  # 1. Surface & ID parity across binding, contract, manifest, and JSON bundle
  report_id = binding.get("report_id")
  datasource_id = binding.get("datasource_id")
  alias = binding.get("data_source_alias")
  surface = contract.get("surface", {})
  meta = manifest.get("meta", {})
  ds = manifest.get("datasource", {})
  bundle_ds = bundle.get("datasource", {})

  if (
      surface.get("canonical_report_id") != report_id
      or meta.get("report_id") != report_id
      or bundle.get("report_id") != report_id
  ):
    errors.append(
        "bqca report_id mismatch across binding, contract, manifest, and bundle"
    )
  if (
      surface.get("datasource_id") != datasource_id
      or meta.get("datasource_id") != datasource_id
      or ds.get("datasource_id") != datasource_id
      or bundle.get("datasource_id") != datasource_id
      or bundle_ds.get("datasource_id") != datasource_id
  ):
    errors.append(
        "bqca datasource_id mismatch across binding, contract, manifest, and"
        " bundle"
    )
  if (
      surface.get("data_source_alias") != alias
      or meta.get("data_source_alias") != alias
      or ds.get("data_source_alias") != alias
      or bundle.get("data_source_alias") != alias
      or bundle_ds.get("data_source_alias") != alias
  ):
    errors.append(
        "bqca data_source_alias mismatch across binding, contract, manifest,"
        " and bundle"
    )
  if surface.get("source_parity_contract") != "spec/bqca_chart_manifest.yaml":
    errors.append(
        "bqca product_contract surface.source_parity_contract must be"
        " spec/bqca_chart_manifest.yaml"
    )
  if binding.get("chart_manifest") != "spec/bqca_chart_manifest.yaml":
    errors.append(
        "bqca_report_template chart_manifest must be"
        " spec/bqca_chart_manifest.yaml"
    )
  if (
      surface.get("dashboard_template_bundle")
      != "spec/bqca_dashboard_template.json"
      or binding.get("dashboard_template_bundle")
      != "spec/bqca_dashboard_template.json"
  ):
    errors.append(
        "bqca dashboard_template_bundle must be"
        " spec/bqca_dashboard_template.json"
    )
  if (
      surface.get("compatibility_profile")
      != "spec/bqca_compatibility_profile.json"
      or binding.get("compatibility_profile")
      != "spec/bqca_compatibility_profile.json"
  ):
    errors.append(
        "bqca compatibility_profile must be"
        " spec/bqca_compatibility_profile.json"
    )

  # 1b. Compatibility profile parity against base table and event allowlist
  expected_cols = {
      col["name"]: col["type"]
      for col in contract.get("base_table_contract", {}).get(
          "required_columns", []
      )
      if isinstance(col, dict) and "name" in col and "type" in col
  }
  if compat.get("source_object") != binding.get("default_table"):
    errors.append("bqca compatibility_profile.source_object mismatch")
  if compat.get("source_object_type") != "BASE TABLE":
    errors.append(
        "bqca compatibility_profile.source_object_type must be BASE TABLE"
    )
  if compat.get("generated_views_required") is not False:
    errors.append(
        "bqca compatibility_profile.generated_views_required must be False"
    )
  if (
      compat.get("required_columns") != expected_cols
      or len(expected_cols) != 15
  ):
    errors.append(
        "bqca compatibility_profile.required_columns does not match 15-column"
        " base_table_contract"
    )
  if compat.get("allowed_event_types") != contract.get(
      "allowed_event_types", []
  ):
    errors.append(
        "bqca compatibility_profile.allowed_event_types does not match"
        " product_contract"
    )
  if compat.get("excluded_event_types") != list(BQCA_UNLOGGED_EVENT_TYPES):
    errors.append(
        "bqca compatibility_profile.excluded_event_types does not match"
        " BQCA_UNLOGGED_EVENT_TYPES"
    )

  # 2. Datasource 41-field schema, SQL column projection parity, & native datetime invariants
  ds_fields = ds.get("fields", [])
  if ds.get("field_count") != 41 or len(ds_fields) != 41:
    errors.append(f"bqca manifest expected 41 SQL fields, got {len(ds_fields)}")
  if ds.get("use_datetime_type") is not True:
    errors.append("bqca manifest datasource.use_datetime_type must be True")
  if ds.get("parameter_configuration") != ["DS_START_DATE", "DS_END_DATE"]:
    errors.append(
        "bqca manifest parameter_configuration must be [DS_START_DATE, DS_END_DATE]"
    )
  if (
      bundle_ds.get("field_count") != 41
      or bundle_ds.get("fields") != ds_fields
      or bundle_ds.get("use_datetime_type") is not True
      or bundle_ds.get("parameter_configuration")
      != ["DS_START_DATE", "DS_END_DATE"]
  ):
    errors.append(
        "bqca dashboard_template_bundle datasource schema diverges from"
        " bqca_chart_manifest"
    )

  field_by_display = {f["display_name"]: f for f in ds_fields}
  field_by_name = {f["name"]: f for f in ds_fields}
  for f in ds_fields:
    fname = f.get("name")
    dname = f.get("display_name")
    dtype = f.get("data_type")
    stype = f.get("semantic_type")
    if fname != f"_{dname}_":
      errors.append(
          f"bqca manifest field name {fname!r} does not match _{dname}_"
      )
    allowed_sem = _SEMANTIC_TYPES_BY_DATA_TYPE.get(dtype, set())
    if stype not in allowed_sem:
      errors.append(
          f"bqca manifest field {dname!r} semantic_type {stype!r} incompatible"
          f" with data_type {dtype!r}"
      )

  base_table_types = {
      col["name"]: col["type"]
      for col in contract.get("base_table_contract", {}).get(
          "required_columns", []
      )
      if isinstance(col, dict) and "name" in col and "type" in col
  }
  try:
    sql_projected = extract_sql_projected_columns(sql_text, base_table_types)
  except ValueError as exc:
    errors.append(f"bqca SQL projection parse error: {exc}")
    sql_projected = []

  if sql_projected:
    sql_col_names = [c[0] for c in sql_projected]
    manifest_col_names = [f.get("display_name") for f in ds_fields]
    if sql_col_names != manifest_col_names:
      errors.append(
          "bqca SQL projected columns do not match manifest datasource.fields:"
          f" sql={sql_col_names} vs manifest={manifest_col_names}"
      )
    for col_name, sql_dtype in sql_projected:
      mf = field_by_display.get(col_name)
      if mf is None:
        errors.append(
            f"bqca SQL projected column {col_name!r} missing from manifest"
            " datasource.fields"
        )
        continue
      if mf.get("data_type") != sql_dtype:
        errors.append(
            f"bqca manifest field {col_name!r} data_type"
            f" {mf.get('data_type')!r} does not match SQL projected type"
            f" {sql_dtype!r}"
        )

  for name, expected in (
      binding.get("block_datasource", {}).get("fields", {}).items()
  ):
    actual = field_by_name.get(name)
    if not actual:
      errors.append(f"bqca manifest missing block_datasource field {name}")
      continue
    if actual.get("data_type") != expected.get("data_type") or actual.get(
        "semantic_type"
    ) != expected.get("semantic_type"):
      errors.append(
          f"bqca manifest field {name} type mismatch: {actual} vs {expected}"
      )

  # 3. 7 pages and 34 components (21 scorecards + 13 charts/tables) 1:1 parity
  c_pages = contract.get("pages", [])
  m_pages = manifest.get("pages", [])
  m_comps = manifest.get("components", [])
  b_pages = bundle.get("pages", [])
  b_comps = bundle.get("components", [])
  if len(c_pages) != 7 or len(m_pages) != 7 or len(b_pages) != 7:
    errors.append(
        f"bqca expected 7 pages, got contract={len(c_pages)},"
        f" manifest={len(m_pages)}, bundle={len(b_pages)}"
    )
  if len(m_comps) != 34 or len(b_comps) != 34:
    errors.append(
        f"bqca expected 34 components, got manifest={len(m_comps)},"
        f" bundle={len(b_comps)}"
    )

  total_sc = 0
  total_ch = 0
  flat_from_pages: list[dict[str, Any]] = []
  flat_from_bundle_pages: list[dict[str, Any]] = []
  for c_page, m_page, b_page in zip(c_pages, m_pages, b_pages):
    if (
        c_page.get("id") != m_page.get("id")
        or c_page.get("name") != m_page.get("name")
        or b_page.get("id") != m_page.get("id")
        or b_page.get("name") != m_page.get("name")
    ):
      errors.append(
          f"bqca page mismatch: {c_page.get('id')} vs {m_page.get('id')} vs"
          f" {b_page.get('id')}"
      )
    c_scs = c_page.get("scorecards", [])
    c_chs = c_page.get("charts", [])
    if len(c_scs) + len(c_chs) != c_page.get("component_count"):
      errors.append(f"bqca page {c_page.get('id')} component_count mismatch")
    if m_page.get("component_count") != c_page.get(
        "component_count"
    ) or b_page.get("component_count") != c_page.get("component_count"):
      errors.append(
          f"bqca manifest/bundle page {m_page.get('id')} component_count"
          " mismatch"
      )

    page_m_comps = m_page.get("components", [])
    page_b_comps = b_page.get("components", [])
    if len(page_m_comps) != c_page.get("component_count") or len(
        page_b_comps
    ) != c_page.get("component_count"):
      errors.append(
          f"bqca manifest/bundle page {m_page.get('id')} components length"
          " mismatch"
      )
    flat_from_pages.extend(page_m_comps)
    flat_from_bundle_pages.extend(page_b_comps)

    occupied_cells: dict[tuple[int, int], str] = {}
    for m_comp, b_comp in zip(page_m_comps, page_b_comps):
      for shared_key in (
          "id",
          "given_id",
          "component_id",
          "page_id",
          "page_title",
          "category",
          "component_type",
          "chart_type",
          "label",
          "title",
          "field",
          "aggregation",
          "datasource_id",
          "data_source_alias",
          "dimensions",
          "metrics",
          "columns",
          "geometry",
          "spec",
      ):
        if b_comp.get(shared_key) != m_comp.get(shared_key):
          errors.append(
              f"bqca bundle component {m_comp.get('id')} key {shared_key!r}"
              " diverges from manifest"
          )
      gp = b_comp.get("grid_position")
      if not b_comp.get("responsive_section") or not isinstance(gp, dict):
        errors.append(
            f"bqca bundle component {m_comp.get('id')} missing"
            " responsive_section or grid_position"
        )
      else:
        row = gp.get("row")
        row_span = gp.get("row_span")
        col = gp.get("column")
        col_span = gp.get("column_span")
        if (
            not all(isinstance(v, int) for v in (row, row_span, col, col_span))
            or row < 1
            or row_span < 1
            or col < 1
            or col_span < 1
            or col + col_span - 1 > 12
        ):
          errors.append(
              f"bqca bundle component {b_comp.get('id')} invalid"
              f" grid_position {gp}"
          )
        else:
          for r_idx in range(row, row + row_span):
            for c_idx in range(col, col + col_span):
              cell = (r_idx, c_idx)
              if cell in occupied_cells:
                errors.append(
                    f"bqca bundle page {b_page.get('id')} grid cell {cell}"
                    f" overlap between {occupied_cells[cell]} and"
                    f" {b_comp.get('id')}"
                )
              else:
                occupied_cells[cell] = str(b_comp.get("id"))

    if all(
        isinstance(c.get("grid_position"), dict)
        and isinstance(c.get("geometry"), dict)
        and all(
            k in c["grid_position"]
            for k in ("row", "column", "row_span", "column_span")
        )
        and all(k in c["geometry"] for k in ("top", "left"))
        for c in page_b_comps
    ):
      by_grid = [
          c["id"]
          for c in sorted(
              page_b_comps,
              key=lambda c: (
                  c["grid_position"]["row"],
                  c["grid_position"]["column"],
                  c["id"],
              ),
          )
      ]
      by_geom = [
          c["id"]
          for c in sorted(
              page_b_comps,
              key=lambda c: (
                  c["geometry"]["top"],
                  c["geometry"]["left"],
                  c["id"],
              ),
          )
      ]
      if by_grid != by_geom:
        errors.append(
            f"bqca bundle page {b_page.get('id')} grid_position reading order"
            f" {by_grid} diverges from geometry order {by_geom}"
        )

    for comp in page_m_comps:
      errors.extend(
          _validate_component_embedded_spec(
              comp,
              datasource_id=datasource_id,
              field_by_name=field_by_name,
              field_by_display=field_by_display,
          )
      )

    m_sc_by_id = {
        c["id"]: c for c in page_m_comps if c.get("category") == "scorecard"
    }
    m_ch_by_id = {
        c["id"]: c for c in page_m_comps if c.get("category") == "chart"
    }
    if [s["id"] for s in c_scs] != list(m_sc_by_id):
      errors.append(
          f"bqca page {c_page.get('id')} scorecard IDs diverge from manifest"
      )
    if [ch["id"] for ch in c_chs] != list(m_ch_by_id):
      errors.append(
          f"bqca page {c_page.get('id')} chart IDs diverge from manifest"
      )

    for sc in c_scs:
      total_sc += 1
      if sc.get("field") not in field_by_display:
        errors.append(
            f"scorecard {sc.get('id')} references unknown field {sc.get('field')!r}"
        )
      m_sc = m_sc_by_id.get(sc["id"])
      if m_sc:
        if (
            m_sc.get("label") != sc.get("label")
            or m_sc.get("field") != sc.get("field")
            or m_sc.get("aggregation") != sc.get("aggregation")
        ):
          errors.append(f"scorecard {sc['id']} contract vs manifest mismatch")

    for ch in c_chs:
      total_ch += 1
      dims = list(
          ch.get("dimensions", [ch["dimension"]] if "dimension" in ch else [])
      )
      for d in dims:
        if d not in field_by_display:
          errors.append(
              f"chart {ch.get('id')} references unknown dimension {d!r}"
          )
      if "metrics" in ch:
        c_mets = [dict(m) if isinstance(m, dict) else m for m in ch["metrics"]]
      elif "metric" in ch:
        c_mets = [{"field": ch["metric"], "aggregation": ch["aggregation"]}]
      else:
        c_mets = []
      for m in c_mets:
        mf = m["field"] if isinstance(m, dict) else m
        if mf not in field_by_display:
          errors.append(
              f"chart {ch.get('id')} references unknown metric {mf!r}"
          )
      m_ch = m_ch_by_id.get(ch["id"])
      if m_ch:
        if (
            m_ch.get("title") != ch.get("title")
            or m_ch.get("chart_type") != ch.get("type")
            or m_ch.get("dimensions") != dims
            or m_ch.get("metrics") != c_mets
        ):
          errors.append(f"chart {ch['id']} contract vs manifest mismatch")

  if total_sc != 21 or total_ch != 13 or (total_sc + total_ch) != 34:
    errors.append(
        f"bqca component totals expected 21+13=34, got {total_sc}+{total_ch}"
    )
  if [c["id"] for c in flat_from_pages] != [c["id"] for c in m_comps]:
    errors.append(
        "bqca manifest top-level components list does not match page components"
    )
  if flat_from_bundle_pages != b_comps:
    errors.append(
        "bqca bundle top-level components list does not match page components"
    )

  # 4. SHA-256 digests (SQL, contract pages, manifest, and JSON template bundle)
  sql_bytes = (
      sql_override.encode("utf-8")
      if sql_override is not None
      else sql_path.read_bytes()
  )
  sql_sha = hashlib.sha256(sql_bytes).hexdigest()
  pages_sha = canonical_pages_sha256(c_pages)
  if manifest_override is not None:
    manifest_sha = hashlib.sha256(
        yaml.safe_dump(manifest_override, sort_keys=False).encode("utf-8")
    ).hexdigest()
  else:
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
  if bundle_override is not None:
    bundle_sha = hashlib.sha256(
        (json.dumps(bundle_override, indent=2) + "\n").encode("utf-8")
    ).hexdigest()
  else:
    bundle_sha = hashlib.sha256(bundle_path.read_bytes()).hexdigest()

  if bundle_ds.get("sql_sha256") != sql_sha:
    errors.append(
        "bqca dashboard_template_bundle datasource.sql_sha256 mismatch"
    )

  live = binding.get("live_template_verification", {})
  if binding.get("reviewed_template_sql", {}).get("sha256") != sql_sha:
    errors.append("bqca reviewed_template_sql.sha256 mismatch")
  if live.get("repository_sql_sha256") != sql_sha:
    errors.append(
        "bqca live_template_verification.repository_sql_sha256 mismatch"
    )
  if live.get("pages_sha256") != pages_sha:
    errors.append(
        f"bqca live_template_verification.pages_sha256 mismatch: expected {pages_sha}, got {live.get('pages_sha256')}"
    )
  if live.get("manifest_sha256") != manifest_sha:
    errors.append(
        f"bqca live_template_verification.manifest_sha256 mismatch: expected {manifest_sha}, got {live.get('manifest_sha256')}"
    )
  if live.get("template_bundle_sha256") != bundle_sha:
    errors.append(
        "bqca live_template_verification.template_bundle_sha256 mismatch:"
        f" expected {bundle_sha}, got {live.get('template_bundle_sha256')}"
    )

  # 5. Semantic SQL invariants (M03, M04, M07, M14, M15, M16, R4-1..R4-3, R4-N2) & behavioral oracle
  sql_norm = " ".join(_strip_sql_comments(sql_text).split())
  required_sql_fragments = {
      "trace_resolved per-trace invocation_id inheritance": (
          "IF( invocation_id IS NULL, NULL, COALESCE("
          " NULLIF(TRIM(invocation_id), ''), IF( NULLIF(TRIM(trace_id), '') IS"
          " NULL, NULL, FIRST_VALUE(NULLIF(TRIM(invocation_id), '') IGNORE"
          " NULLS) OVER ( PARTITION BY NULLIF(TRIM(trace_id), '') ORDER BY"
          " timestamp ASC, event_type ASC, NULLIF(TRIM(invocation_id), '') ASC"
          " ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING ) ),"
          " NULLIF(TRIM(trace_id), ''), NULLIF(TRIM(session_id), ''),"
          " CAST(timestamp AS STRING) ) ) AS turn_id"
      ),
      "null invocation_id SHA256 partition isolation": (
          "IF( turn_id IS NULL, CONCAT( '__null_invocation__:', CAST(timestamp"
          " AS STRING), ':', IFNULL(span_id, ''), ':', IFNULL(event_type, ''),"
          " ':', TO_HEX( SHA256( CONCAT( IFNULL(TO_JSON_STRING(attributes),"
          " ''), '|', IFNULL(TO_JSON_STRING(content), ''), '|', IFNULL(status,"
          " ''), '|', IFNULL(error_message, '') ) ) ) ), turn_id ) AS"
          " turn_partition_key"
      ),
      "raw_conversation_id whitespace trimming": (
          "NULLIF( TRIM( JSON_VALUE(attributes,"
          " '$.session_metadata.state.\"conversation-id\"') ), '' ) AS"
          " raw_conversation_id"
      ),
      "raw_data_agent_id whitespace trimming": (
          "NULLIF( TRIM( JSON_VALUE(attributes,"
          " '$.session_metadata.state.\"data-agent-id\"') ), '' ) AS"
          " raw_data_agent_id"
      ),
      "raw_explicit_persona whitespace trimming": (
          "NULLIF( TRIM( JSON_VALUE( attributes,"
          " '$.session_metadata.state.custom_labels.persona' ) ), '' ) AS"
          " raw_explicit_persona"
      ),
      "fast_path attribute extraction": (
          "IFNULL( LOWER(JSON_VALUE(attributes, '$.fast_path')) = 'true', FALSE"
          " ) AS raw_fast_path"
      ),
      "raw_is_error 3-condition error attribution": (
          "( IFNULL(UPPER(TRIM(status)) = 'ERROR', FALSE) OR"
          " NULLIF(TRIM(error_message), '') IS NOT NULL OR"
          " ENDS_WITH(UPPER(TRIM(IFNULL(event_type, ''))), '_ERROR') ) AS"
          " raw_is_error"
      ),
      "INVOCATION_COMPLETED deduplication window": (
          "IF( event_type = 'INVOCATION_COMPLETED' AND turn_id IS NOT NULL,"
          " ROW_NUMBER() OVER ( PARTITION BY turn_id, IF(event_type ="
          " 'INVOCATION_COMPLETED', 1, 0) ORDER BY SAFE_CAST(JSON_VALUE(latency_ms,"
          " '$.total_ms') AS FLOAT64) DESC NULLS LAST, timestamp DESC, span_id"
          " DESC ), NULL ) AS raw_turn_complete_rn"
      ),
      "data_agent_id turn-window propagation": (
          "IF( turn_id IS NULL, COALESCE(raw_data_agent_id, 'unattributed'),"
          " COALESCE( FIRST_VALUE(raw_data_agent_id IGNORE NULLS) OVER ("
          " PARTITION BY turn_partition_key ORDER BY timestamp ASC, event_type"
          " ASC, span_id ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED"
          " FOLLOWING ), 'unattributed' ) ) AS data_agent_id"
      ),
      "conversation_id turn-window propagation": (
          "IF( turn_id IS NULL, raw_conversation_id,"
          " FIRST_VALUE(raw_conversation_id IGNORE NULLS) OVER ( PARTITION BY"
          " turn_partition_key ORDER BY timestamp ASC, event_type ASC, span_id"
          " ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING ) )"
          " AS conversation_id"
      ),
      "persona turn-window propagation": (
          "IF( turn_id IS NULL, COALESCE( raw_explicit_persona,"
          " raw_email_persona, raw_data_agent_id, 'unattributed' ), COALESCE("
          " FIRST_VALUE(raw_explicit_persona IGNORE NULLS) OVER ( PARTITION BY"
          " turn_partition_key ORDER BY timestamp ASC, event_type ASC, span_id"
          " ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING ),"
          " FIRST_VALUE(raw_email_persona IGNORE NULLS) OVER ( PARTITION BY"
          " turn_partition_key ORDER BY timestamp ASC, event_type ASC, span_id"
          " ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING ),"
          " FIRST_VALUE(raw_data_agent_id IGNORE NULLS) OVER ( PARTITION BY"
          " turn_partition_key ORDER BY timestamp ASC, event_type ASC, span_id"
          " ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING ),"
          " 'unattributed' ) ) AS persona"
      ),
      "fast_path turn-window propagation": (
          "IF( turn_id IS NULL, raw_fast_path, LOGICAL_OR(raw_fast_path) OVER ("
          " PARTITION BY turn_partition_key ) ) AS fast_path"
      ),
      "is_turn_complete deduplication gate": (
          "IFNULL(raw_turn_complete_rn = 1, FALSE) AS is_turn_complete"
      ),
      "completed_turn_id deduplication gate": (
          "IF(raw_turn_complete_rn = 1, turn_id, NULL) AS completed_turn_id"
      ),
      "turn_latency_ms deduplication gate": (
          "IF( raw_turn_complete_rn = 1, SAFE_CAST(JSON_VALUE(latency_ms,"
          " '$.total_ms') AS FLOAT64), NULL ) AS turn_latency_ms"
      ),
      "fast_path_label derivation": (
          "IF(fast_path, 'fast_path', 'standard_nl2sql') AS fast_path_label"
      ),
  }
  for label, fragment in required_sql_fragments.items():
    if fragment not in sql_norm:
      errors.append(f"bqca SQL missing required {label} ({fragment!r})")

  errors.extend(_validate_bqca_sql_behavioral_oracle(sql_text))

  ext = binding.get("external_access_verification", {})
  ext_status = ext.get("status")
  if ext_status not in {
      "PASSING",
      "FAILING",
      "PENDING_PUBLIC_SHARING_ALLOWLIST",
  }:
    errors.append(
        f"bqca external_access_verification.status {ext_status!r} invalid"
    )
  elif ext_status == "PASSING":
    controls = {
        c.get("method"): c
        for c in ext.get("controls", [])
        if isinstance(c, dict)
    }
    api_ctrl = controls.get("permissions_api_link_role_check", {})
    canary_ctrl = controls.get("external_identity_link_access_check", {})
    open_tracked = any(
        isinstance(i, dict)
        and i.get("issue") == ext.get("tracking_issue")
        and i.get("status") == "OPEN"
        for i in binding.get("known_live_issues", [])
    )
    if (
        binding.get("link_access") != "PUBLIC"
        or api_ctrl.get("last_result") != "LINK_VIEWER_ALLUSERS_PRESENT"
        or canary_ctrl.get("last_result") not in {"PASSED", "PASSING"}
        or not canary_ctrl.get("link_access_verified_date")
        or open_tracked
    ):
      errors.append(
          "bqca external_access_verification.status is PASSING without PUBLIC"
          " link_access, passing controls, link_access_verified_date, and"
          " resolved known_live_issues"
      )

  # 6. Forbidden unlogged event types check
  for rel in (
      "spec/bqca_chart_manifest.yaml",
      "spec/bqca_product_contract.yaml",
      "spec/bqca_dashboard_template.json",
      "bindings/bqca_report_template.yaml",
      "sql/bqca_events_v1.template.sql",
  ):
    text = (root / rel).read_text()
    for forbidden in BQCA_UNLOGGED_EVENT_TYPES:
      if forbidden in text:
        errors.append(
            f"{rel} contains forbidden unlogged event type {forbidden}"
        )

  return errors


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
      "--profile",
      choices=("adk", "bqca", "all"),
      default="all",
      help="dashboard profile to validate (default: all)",
  )
  args = parser.parse_args(argv)
  errors: list[str] = []
  if args.profile in ("adk", "all"):
    errors.extend(validate_adk(ROOT))
  if args.profile in ("bqca", "all"):
    errors.extend(validate_bqca(ROOT))
  if errors:
    for err in errors:
      print(f"ERROR: {err}", file=sys.stderr)
    return 1
  print(f"ok: contracts validated ({args.profile})")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
