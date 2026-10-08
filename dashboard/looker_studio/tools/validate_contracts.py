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
      re.search(r"=\s*'true'\s*,\s*FALSE\s*\)$", cleaned, re.IGNORECASE)
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
      r"bqca_fields\s+AS\s*\(\s*SELECT\b(.*?)\bFROM\s+bqca_events\s*\)",
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


def validate_bqca(
    root: pathlib.Path = ROOT,
    *,
    contract_override: dict[str, Any] | None = None,
    manifest_override: dict[str, Any] | None = None,
    binding_override: dict[str, Any] | None = None,
    sql_override: str | None = None,
) -> list[str]:
  """Return a list of contract validation errors for the BQCA profile."""
  errors: list[str] = []
  manifest_path = root / "spec/bqca_chart_manifest.yaml"
  contract_path = root / "spec/bqca_product_contract.yaml"
  binding_path = root / "bindings/bqca_report_template.yaml"
  sql_path = root / "sql/bqca_events_v1.template.sql"

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

  # 1. Surface & ID parity across all three files
  report_id = binding.get("report_id")
  datasource_id = binding.get("datasource_id")
  alias = binding.get("data_source_alias")
  surface = contract.get("surface", {})
  meta = manifest.get("meta", {})
  ds = manifest.get("datasource", {})

  if (
      surface.get("canonical_report_id") != report_id
      or meta.get("report_id") != report_id
  ):
    errors.append(
        "bqca report_id mismatch across binding, contract, and manifest"
    )
  if (
      surface.get("datasource_id") != datasource_id
      or meta.get("datasource_id") != datasource_id
      or ds.get("datasource_id") != datasource_id
  ):
    errors.append(
        "bqca datasource_id mismatch across binding, contract, and manifest"
    )
  if (
      surface.get("data_source_alias") != alias
      or meta.get("data_source_alias") != alias
      or ds.get("data_source_alias") != alias
  ):
    errors.append(
        "bqca data_source_alias mismatch across binding, contract, and manifest"
    )
  if surface.get("source_parity_contract") != "spec/bqca_chart_manifest.yaml":
    errors.append(
        "bqca product_contract surface.source_parity_contract must be spec/bqca_chart_manifest.yaml"
    )
  if binding.get("chart_manifest") != "spec/bqca_chart_manifest.yaml":
    errors.append(
        "bqca_report_template chart_manifest must be spec/bqca_chart_manifest.yaml"
    )

  # 2. Datasource 40-field schema, SQL column projection parity, & native datetime invariants
  ds_fields = ds.get("fields", [])
  if ds.get("field_count") != 40 or len(ds_fields) != 40:
    errors.append(f"bqca manifest expected 40 SQL fields, got {len(ds_fields)}")
  if ds.get("use_datetime_type") is not True:
    errors.append("bqca manifest datasource.use_datetime_type must be True")
  if ds.get("parameter_configuration") != ["DS_START_DATE", "DS_END_DATE"]:
    errors.append(
        "bqca manifest parameter_configuration must be [DS_START_DATE, DS_END_DATE]"
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
  if len(c_pages) != 7 or len(m_pages) != 7:
    errors.append(
        f"bqca expected 7 pages, got contract={len(c_pages)}, manifest={len(m_pages)}"
    )
  if len(m_comps) != 34:
    errors.append(f"bqca manifest expected 34 components, got {len(m_comps)}")

  total_sc = 0
  total_ch = 0
  flat_from_pages: list[dict[str, Any]] = []
  for c_page, m_page in zip(c_pages, m_pages):
    if c_page.get("id") != m_page.get("id") or c_page.get("name") != m_page.get(
        "name"
    ):
      errors.append(
          f"bqca page mismatch: {c_page.get('id')} vs {m_page.get('id')}"
      )
    c_scs = c_page.get("scorecards", [])
    c_chs = c_page.get("charts", [])
    if len(c_scs) + len(c_chs) != c_page.get("component_count"):
      errors.append(f"bqca page {c_page.get('id')} component_count mismatch")
    if m_page.get("component_count") != c_page.get("component_count"):
      errors.append(
          f"bqca manifest page {m_page.get('id')} component_count mismatch"
      )

    page_m_comps = m_page.get("components", [])
    if len(page_m_comps) != c_page.get("component_count"):
      errors.append(
          f"bqca manifest page {m_page.get('id')} components length mismatch"
      )
    flat_from_pages.extend(page_m_comps)

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

  # 4. SHA-256 digests (SQL, contract pages, and manifest)
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

  # 5. Forbidden unlogged event types check
  for rel in (
      "spec/bqca_chart_manifest.yaml",
      "spec/bqca_product_contract.yaml",
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
