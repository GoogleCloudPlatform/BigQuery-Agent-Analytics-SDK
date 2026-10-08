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


def canonical_pages_sha256(pages: list[dict[str, Any]]) -> str:
  """Return the deterministic SHA-256 hex digest for a contract pages list."""
  payload = json.dumps(pages, sort_keys=True, separators=(",", ":")).encode(
      "utf-8"
  )
  return hashlib.sha256(payload).hexdigest()


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

  # 2. Datasource 40-field schema & native datetime invariants
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
  sql_sha = hashlib.sha256(sql_path.read_bytes()).hexdigest()
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
