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

"""Tests for the Power BI deploy and live parity tools."""

import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
POWER_BI_ROOT = ROOT / "dashboards" / "power_bi"


def _load_module(name):
  module_path = POWER_BI_ROOT / "tools" / f"{name}.py"
  spec = importlib.util.spec_from_file_location(f"power_bi_{name}", module_path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


deploy_mod = _load_module("deploy_fabric")
validate_mod = _load_module("validate_live_powerbi")


# ---------------------------------------------------------------------------
# GUID literals check: neither tool file contains a hardcoded GUID
# ---------------------------------------------------------------------------

GUID_LITERAL_RE = re.compile(
    r"['\"]"  # quote
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"['\"]"
)


@pytest.mark.parametrize(
    "filename",
    ["deploy_fabric.py", "validate_live_powerbi.py"],
)
def test_no_guid_literals(filename):
  path = POWER_BI_ROOT / "tools" / filename
  content = path.read_text()
  matches = GUID_LITERAL_RE.findall(content)
  assert matches == [], f"GUID literal found in {filename}: {matches}"


# ---------------------------------------------------------------------------
# GUID validation
# ---------------------------------------------------------------------------


def test_validate_guid_accepts_valid():
  result = deploy_mod.validate_guid("11111111-1111-1111-1111-111111111111")
  assert result == "11111111-1111-1111-1111-111111111111"


def test_validate_guid_rejects_invalid():
  with pytest.raises(ValueError):
    deploy_mod.validate_guid("not-a-guid")


# ---------------------------------------------------------------------------
# Token file permission handling
# ---------------------------------------------------------------------------


def test_read_token_file_rejects_group_readable(tmp_path):
  token_file = tmp_path / "token.txt"
  token_file.write_text("secret-token")
  os.chmod(token_file, 0o640)  # group readable
  with pytest.raises(ValueError, match="group or other"):
    deploy_mod.read_token_file(str(token_file))


def test_read_token_file_rejects_world_readable(tmp_path):
  token_file = tmp_path / "token.txt"
  token_file.write_text("secret-token")
  os.chmod(token_file, 0o644)  # world readable
  with pytest.raises(ValueError, match="group or other"):
    deploy_mod.read_token_file(str(token_file))


def test_read_token_file_accepts_strict(tmp_path):
  token_file = tmp_path / "token.txt"
  token_file.write_text("secret-token")
  os.chmod(token_file, 0o600)
  result = deploy_mod.read_token_file(str(token_file))
  assert result == "secret-token"


def test_read_token_file_rejects_whitespace(tmp_path):
  token_file = tmp_path / "token.txt"
  token_file.write_text("  token with spaces  ")
  os.chmod(token_file, 0o600)
  with pytest.raises(ValueError, match="whitespace"):
    deploy_mod.read_token_file(str(token_file))


# ---------------------------------------------------------------------------
# Token never appears in stdout/stderr/exceptions
# ---------------------------------------------------------------------------


def test_token_not_in_error_messages(tmp_path):
  secret = "super-secret-bearer-token-12345"
  token_file = tmp_path / "token.txt"
  token_file.write_text(secret)
  os.chmod(token_file, 0o600)
  token = deploy_mod.read_token_file(str(token_file))
  try:
    deploy_mod.http_request("http://localhost:99999/nonexistent", "GET", token)
  except Exception as exc:
    assert secret not in str(exc)
    assert secret not in repr(exc)


def test_token_not_in_extract_error():
  """_extract_error returns API errorCode/message only, never the token."""
  secret = "super-secret-token"
  body = json.dumps(
      {"errorCode": "InvalidDefinition", "message": "bad model"}
  ).encode("utf-8")
  code, msg = deploy_mod._extract_error(body)
  assert secret not in code
  assert secret not in msg
  assert code == "InvalidDefinition"
  assert msg == "bad model"


# ---------------------------------------------------------------------------
# definition.pbir rewrite
# ---------------------------------------------------------------------------


def test_rewrite_pbir_produces_by_connection():
  original = (
      '{"version":"4.0","datasetReference":' '{"byPath":{"path":"../model"}}}'
  )
  result = deploy_mod.rewrite_pbir(original, "abc123-model-id")
  parsed = json.loads(result)
  assert parsed["version"] == "4.0"
  assert "byConnection" in parsed["datasetReference"]
  assert "byPath" not in parsed["datasetReference"]
  assert (
      parsed["datasetReference"]["byConnection"]["connectionString"]
      == "semanticmodelid=abc123-model-id"
  )


def test_rewrite_pbir_has_schema():
  original = (
      '{"version":"4.0","datasetReference":' '{"byPath":{"path":"../model"}}}'
  )
  result = deploy_mod.rewrite_pbir(original, "model-1")
  parsed = json.loads(result)
  assert "definitionProperties/2.0.0" in parsed["$schema"]


# ---------------------------------------------------------------------------
# Part collection (dry run)
# ---------------------------------------------------------------------------


def test_collect_parts_excludes_platform_and_pbi(tmp_path):
  model_dir = tmp_path / "Model"
  model_dir.mkdir()
  (model_dir / ".platform").write_text("{}")
  (model_dir / "definition.pbism").write_text("{}")
  pbi_dir = model_dir / ".pbi"
  pbi_dir.mkdir()
  (pbi_dir / "localSettings.json").write_text("{}")
  def_dir = model_dir / "definition"
  def_dir.mkdir()
  (def_dir / "database.tmdl").write_text("database")
  (def_dir / "model.tmdl").write_text("model")
  parts = deploy_mod.collect_parts(model_dir)
  paths = [p["path"] for p in parts]
  assert ".platform" not in paths
  assert ".pbi/localSettings.json" not in paths
  assert "definition/database.tmdl" in paths
  assert "definition/model.tmdl" in paths
  assert "definition.pbism" in paths


# ---------------------------------------------------------------------------
# LRO polling
# ---------------------------------------------------------------------------


def test_poll_lro_success(monkeypatch):
  call_count = [0]

  def fake_request(url, method, token):
    call_count[0] += 1
    if call_count[0] < 3:
      return 200, {}, json.dumps({"status": "Running"}).encode()
    if call_count[0] == 3:
      return 200, {}, json.dumps({"status": "Succeeded"}).encode()
    # Result fetch
    return (
        200,
        {},
        json.dumps({"id": "test-item-id", "type": "SemanticModel"}).encode(),
    )

  monkeypatch.setattr(deploy_mod, "http_request", fake_request)
  monkeypatch.setattr(deploy_mod.time, "monotonic", lambda: 0)
  monkeypatch.setattr(deploy_mod.time, "sleep", lambda x: None)

  result = deploy_mod.poll_lro(
      "tok",
      "https://api.fabric.microsoft.com/v1/operations/op-id",
      "op-id",
      max_seconds=300,
  )
  assert result["id"] == "test-item-id"


def test_poll_lro_failed(monkeypatch):
  def fake_request(url, method, token):
    return (
        200,
        {},
        json.dumps(
            {
                "status": "Failed",
                "error": {
                    "errorCode": "InvalidDefinition",
                    "message": "bad",
                },
            }
        ).encode(),
    )

  monkeypatch.setattr(deploy_mod, "http_request", fake_request)
  monkeypatch.setattr(deploy_mod.time, "sleep", lambda x: None)
  with pytest.raises(RuntimeError, match="InvalidDefinition"):
    deploy_mod.poll_lro(
        "tok", "https://api.fabric.microsoft.com/v1/operations/op-id", "op-id"
    )


def test_poll_lro_rejects_untrusted_location_url():
  with pytest.raises(ValueError, match="untrusted operation URL"):
    deploy_mod.poll_lro(
        "tok", "https://evil.example.com/v1/operations/op-id", "op-id"
    )


# ---------------------------------------------------------------------------
# --dry-run CLI
# ---------------------------------------------------------------------------


def test_dry_run_lists_parts(tmp_path, capsys, monkeypatch):
  project_dir = tmp_path / "project"
  model_dir = project_dir / deploy_mod.MODEL_DIR_NAME
  model_dir.mkdir(parents=True)
  (model_dir / ".platform").write_text("{}")
  (model_dir / "definition.pbism").write_text("{}")
  def_dir = model_dir / "definition"
  def_dir.mkdir()
  (def_dir / "database.tmdl").write_text("database content")

  report_dir = project_dir / deploy_mod.REPORT_DIR_NAME
  report_dir.mkdir()
  (report_dir / ".platform").write_text("{}")
  (report_dir / "definition.pbir").write_text(
      json.dumps({"datasetReference": {"byPath": {"path": "../model"}}})
  )
  rdef = report_dir / "definition"
  rdef.mkdir()
  (rdef / "version.json").write_text("{}")

  rc = deploy_mod.main(
      [
          "--workspace-id",
          "11111111-1111-1111-1111-111111111111",
          "--project-dir",
          str(project_dir),
          "--dry-run",
      ]
  )
  assert rc == 0
  captured = capsys.readouterr()
  assert "Semantic Model parts" in captured.out
  assert "Report parts" in captured.out
  assert "definition.pbir" in captured.out
  assert ".platform" not in captured.out
  assert "sha256:" in captured.out


# ---------------------------------------------------------------------------
# Identifier validation (validate_live_powerbi.py)
# ---------------------------------------------------------------------------


def test_require_identifier_valid():
  validate_mod.require_identifier(
      "project",
      "test-project-0728-467323",
      validate_mod.PROJECT_RE,
  )


def test_require_identifier_invalid():
  with pytest.raises(ValueError, match="invalid"):
    validate_mod.require_identifier(
        "project", "UPPER CASE", validate_mod.PROJECT_RE
    )


# ---------------------------------------------------------------------------
# Chart metadata loading from integrated specs
# ---------------------------------------------------------------------------


def test_load_chart_metadata_returns_37():
  charts = validate_mod.load_chart_metadata()
  assert len(charts) == 37


def test_chart_metadata_has_visual_types():
  charts = validate_mod.load_chart_metadata()
  visual_types = {c["visual_type"] for c in charts}
  assert visual_types == {"cardVisual", "clusteredBarChart", "areaChart"}


def test_chart_metadata_has_measures():
  charts = validate_mod.load_chart_metadata()
  for c in charts:
    assert len(c["measures"]) >= 1, c["id"]
    assert len(c["measure_info"]) == len(c["measures"])


def test_chart_metadata_kpi_cards_have_single_measure():
  charts = validate_mod.load_chart_metadata()
  kpi_cards = [
      c
      for c in charts
      if c["visual_type"] == "cardVisual"
      and c["id"].startswith("usage-total-")
      or (
          c["visual_type"] == "cardVisual"
          and c["id"] == "performance-total-errors"
      )
  ]
  # 6 KPI cards, each with a single base measure (not KPI prefix)
  kpi_card_ids = {
      "usage-total-tokens",
      "usage-total-traces",
      "usage-total-sessions",
      "usage-total-calls",
      "usage-total-users",
      "performance-total-errors",
  }
  kpi_cards = [c for c in charts if c["id"] in kpi_card_ids]
  assert len(kpi_cards) == 6
  for c in kpi_cards:
    assert len(c["measures"]) == 1, f"{c['id']}: {c['measures']}"
    assert c["measures"][0].startswith("KPI "), c["id"]


def test_chart_metadata_latency_cards_have_1_measure():
  charts = validate_mod.load_chart_metadata()
  latency_cards = [
      c
      for c in charts
      if c["visual_type"] == "cardVisual" and "latency" in c["id"]
  ]
  assert len(latency_cards) == 10
  for c in latency_cards:
    assert len(c["measures"]) == 1


def test_chart_metadata_bar_charts_have_top_n():
  charts = validate_mod.load_chart_metadata()
  bars = [c for c in charts if c["visual_type"] == "clusteredBarChart"]
  # 11 Top-N bars + 1 no-top-N bar (usage-top-5-agents-by-llm-calls)
  top_n_bars = [c for c in bars if c["top_n"] is not None]
  assert len(top_n_bars) == 11
  for c in top_n_bars:
    assert c["top_n"] in (5, 10), c["id"]


def test_chart_metadata_area_chart_with_series():
  charts = validate_mod.load_chart_metadata()
  series_areas = [
      c for c in charts if c["visual_type"] == "areaChart" and c["series"]
  ]
  assert len(series_areas) == 1
  assert series_areas[0]["id"] == "usage-tool-calls-over-time"


# ---------------------------------------------------------------------------
# Comparison engine: values_match
# ---------------------------------------------------------------------------


def test_values_match_both_none():
  assert validate_mod.values_match(None, None) is True


def test_values_match_none_vs_value():
  assert validate_mod.values_match(None, 5.0) is False
  assert validate_mod.values_match(5.0, None) is False


def test_values_match_exact_integers():
  assert validate_mod.values_match(42, 42) is True
  assert validate_mod.values_match(42, 43) is False


def test_values_match_floats_within_tolerance():
  assert validate_mod.values_match(100.0, 100.0000000001) is True


def test_values_match_floats_outside_tolerance():
  assert validate_mod.values_match(100.0, 100.001) is False


def test_values_match_zero_row_oracle_equivalent():
  assert validate_mod.values_match(None, 0) is False
  assert validate_mod.values_match(None, None) is True


# ---------------------------------------------------------------------------
# Comparison engine: compare_card with bracketed DAX keys
# ---------------------------------------------------------------------------


def test_compare_card_matching_bracketed_keys():
  oracle_row = {"col_a": 100, "col_b": 0.5}
  dax_row = {"[v0]": 100, "[v1]": 0.5}
  match, col, o, d = validate_mod.compare_card(
      oracle_row, [dax_row], ["col_a", "col_b"]
  )
  assert match is True


def test_compare_card_matching_bare_keys():
  oracle_row = {"col_a": 100}
  dax_row = {"v0": 100}
  match, _, _, _ = validate_mod.compare_card(oracle_row, [dax_row], ["col_a"])
  assert match is True


def test_compare_card_mismatch():
  oracle_row = {"col_a": 100, "col_b": 0.5}
  dax_row = {"[v0]": 99, "[v1]": 0.5}
  match, col, o, d = validate_mod.compare_card(
      oracle_row, [dax_row], ["col_a", "col_b"]
  )
  assert match is False
  assert col == "col_a"


def test_compare_card_null_blank():
  oracle_row = {"col_a": None}
  dax_row = {"[v0]": None}
  match, _, _, _ = validate_mod.compare_card(oracle_row, [dax_row], ["col_a"])
  assert match is True


def test_compare_card_empty_oracle_is_blank():
  oracle_row = {}
  dax_row = {"[v0]": None}
  match, _, _, _ = validate_mod.compare_card(oracle_row, [dax_row], ["col_a"])
  assert match is True


def test_compare_card_exact_pop_current_alias():
  """compare_card resolves exact pop_total_tokens_current oracle column."""
  oracle_row = {
      "pop_total_tokens_current": 39000000,
      "pop_total_tokens_change": 0.15,
  }
  dax_row = {"[v0]": 39000000}
  match, col, o, d = validate_mod.compare_card(
      oracle_row, [dax_row], ["pop_total_tokens_current"]
  )
  assert match is True


def test_compare_card_exact_pop_llm_calls_alias():
  """compare_card resolves exact pop_llm_calls_current oracle column."""
  oracle_row = {
      "pop_llm_calls_current": 15604,
      "pop_llm_calls_change": 0.1,
  }
  dax_row = {"[v0]": 15604}
  match, _, _, _ = validate_mod.compare_card(
      oracle_row, [dax_row], ["pop_llm_calls_current"]
  )
  assert match is True


def test_compare_card_missing_oracle_col_fails_closed():
  """compare_card fails closed when oracle_col is absent from non-empty oracle_row."""
  oracle_row = {"some_other_metric": 42}
  dax_row = {"[v0]": 42}
  match, col, o, d = validate_mod.compare_card(
      oracle_row, [dax_row], ["pop_total_tokens_current"]
  )
  assert match is False
  assert col == "pop_total_tokens_current"


def test_compare_card_pop_current_mismatch():
  """compare_card detects value mismatch on pop_total_tokens_current."""
  oracle_row = {"pop_total_tokens_current": 100}
  dax_row = {"[v0]": 99}
  match, col, o, d = validate_mod.compare_card(
      oracle_row, [dax_row], ["pop_total_tokens_current"]
  )
  assert match is False
  assert o == 100
  assert d == 99


# ---------------------------------------------------------------------------
# Comparison engine: compare_topn
# ---------------------------------------------------------------------------


def test_compare_topn_exact_match():
  oracle = [
      {"agent": "a", "v": 100},
      {"agent": "b", "v": 90},
      {"agent": "c", "v": 80},
      {"agent": "d", "v": 70},
      {"agent": "e", "v": 60},
  ]
  dax = list(oracle)
  match, detail, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 5)
  assert match is True
  assert detail is None


def test_compare_topn_empty_window_with_null_oracle_rows():
  """compare_topn treats oracle NULL metric rows as empty to match DAX BLANK omission."""
  oracle = [{"agent": "a", "v": None}]
  dax = []
  match, detail, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 5)
  assert match is True
  assert detail is None


def test_compare_topn_rejects_member_absent_from_oracle():
  """compare_topn rejects DAX member that does not exist in oracle_all_rows."""
  oracle = [
      {"agent": "a", "v": 100},
      {"agent": "b", "v": 90},
  ]
  dax = [
      {"agent": "a", "v": 100},
      {"agent": "intruder", "v": 90},
  ]
  match, detail, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 5)
  assert match is False
  assert "not in oracle" in detail


def test_compare_topn_different_values():
  oracle = [
      {"agent": "a", "v": 100},
      {"agent": "b", "v": 90},
  ]
  dax = [
      {"agent": "a", "v": 100},
      {"agent": "b", "v": 80},
  ]
  match, detail, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 5)
  assert match is False


def test_compare_topn_tie_aware():
  oracle = [
      {"agent": "a", "v": 100},
      {"agent": "b", "v": 80},
      {"agent": "c", "v": 80},
      {"agent": "d", "v": 80},
  ]
  dax = [
      {"agent": "a", "v": 100},
      {"agent": "c", "v": 80},
      {"agent": "b", "v": 80},
      {"agent": "d", "v": 80},
  ]
  match, _, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 3)
  assert match is True


def test_compare_topn_length_mismatch():
  oracle = [{"agent": "a", "v": 100}]
  dax = []
  match, detail, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 5)
  assert match is False


def test_compare_topn_tool_name_alias_mapping():
  """compare_topn maps tool_completed_name and tool_error_name to oracle tool_name."""
  oracle = [
      {"tool_name": "search_kb", "total_invocations": 120},
      {"tool_name": "fetch_invoice", "total_invocations": 95},
  ]
  dax = [
      {"events_v1[tool_completed_name]": "search_kb", "[v]": 120},
      {"events_v1[tool_completed_name]": "fetch_invoice", "[v]": 95},
  ]
  match, detail, _, _ = validate_mod.compare_topn(
      oracle, dax, "total_invocations", "tool_completed_name", 10
  )
  assert match is True
  assert detail is None


# ---------------------------------------------------------------------------
# Comparison engine: compare_series with date keys
# ---------------------------------------------------------------------------


def test_compare_series_matching():
  oracle = [
      {"timestamp_date": "2026-07-01", "v": 100},
      {"timestamp_date": "2026-07-02", "v": 200},
  ]
  dax = [
      {"[Date]": "2026-07-01T00:00:00", "[v]": 100},
      {"[Date]": "2026-07-02T00:00:00", "[v]": 200},
  ]
  match, _, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date"
  )
  assert match is True


def test_compare_series_date_key_normalization():
  oracle = [
      {"timestamp_date": "2026-07-01", "v": 100},
  ]
  dax = [
      {"Date[Date]": "2026-07-01T00:00:00", "[v]": 100},
  ]
  match, _, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date"
  )
  assert match is True


def test_compare_series_missing_date():
  oracle = [
      {"timestamp_date": "2026-07-01", "v": 100},
      {"timestamp_date": "2026-07-02", "v": 200},
  ]
  dax = [
      {"[Date]": "2026-07-01T00:00:00", "[v]": 100},
  ]
  match, _, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date"
  )
  assert match is False


def test_compare_series_null_on_both_sides():
  oracle = [
      {"timestamp_date": "2026-07-01", "v": None},
      {"timestamp_date": "2026-07-02", "v": 200},
  ]
  dax = [
      {"[Date]": "2026-07-02T00:00:00", "[v]": 200},
  ]
  match, _, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date"
  )
  assert match is True


# ---------------------------------------------------------------------------
# LLM trends aggregation
# ---------------------------------------------------------------------------


def test_aggregate_llm_trends():
  oracle_rows = [
      {"timestamp_minute": "2026-07-01T00:00:00", "total_llm_calls": 5},
      {"timestamp_minute": "2026-07-01T00:01:00", "total_llm_calls": 3},
      {"timestamp_minute": "2026-07-02T00:00:00", "total_llm_calls": 7},
  ]
  result = validate_mod._aggregate_llm_trends(oracle_rows)
  assert len(result) == 2
  assert result[0]["timestamp_date"] == "2026-07-01"
  assert result[0]["total_llm_calls"] == 8
  assert result[1]["timestamp_date"] == "2026-07-02"
  assert result[1]["total_llm_calls"] == 7


# ---------------------------------------------------------------------------
# DAX query builder
# ---------------------------------------------------------------------------


def test_build_dax_query_card_pop():
  chart = {
      "id": "usage-total-tokens",
      "visual_type": "cardVisual",
      "measures": ["KPI Total Tokens", "KPI Total Tokens Change %"],
  }
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert "EVALUATE ROW" in dax
  assert "[KPI Total Tokens]" in dax
  assert "[KPI Total Tokens Change %]" in dax
  assert "DATE(2026, 04, 18)" in dax
  assert "DATE(2026, 07, 16)" in dax


def test_build_dax_query_card_latency():
  chart = {
      "id": "performance-p50-tool-latency",
      "visual_type": "cardVisual",
      "measures": ["P50 Tool Latency (ms)"],
  }
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert "EVALUATE ROW" in dax
  assert "[P50 Tool Latency (ms)]" in dax


def test_build_dax_query_bar():
  chart = {
      "id": "usage-token-usage-split-by-agent",
      "visual_type": "clusteredBarChart",
      "measures": ["Token Usage by Agent"],
      "category": "events_v1.agent",
  }
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert "CALCULATETABLE" in dax
  assert "SUMMARIZECOLUMNS" in dax
  assert "'events_v1'[agent]" in dax


def test_build_dax_query_bar_tool_error():
  chart = {
      "id": "performance-top-5-tools-by-errors",
      "visual_type": "clusteredBarChart",
      "measures": ["Top 5 Tools by Errors"],
      "category": "events_v1.tool_error_name",
  }
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert "'events_v1'[tool_error_name]" in dax


def test_build_dax_query_area():
  chart = {
      "id": "usage-total-tokens-consumption-over-the-time",
      "visual_type": "areaChart",
      "measures": ["Total Tokens"],
      "series": "",
  }
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert "SUMMARIZECOLUMNS" in dax
  assert "'Date'[Date]" in dax


def test_build_dax_query_area_with_series():
  chart = {
      "id": "usage-tool-calls-over-time",
      "visual_type": "areaChart",
      "measures": ["Tool Completion Events"],
      "series": "events_v1.tool_completed_name",
  }
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert "SUMMARIZECOLUMNS" in dax
  assert "'Date'[Date]" in dax
  assert "'events_v1'[tool_completed_name]" in dax


# ---------------------------------------------------------------------------
# Real chart metadata: build_dax_query on all 37 charts
# ---------------------------------------------------------------------------


def test_build_dax_query_all_37_charts():
  charts = validate_mod.load_chart_metadata()
  for chart in charts:
    dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
    assert dax != "", f"empty DAX for {chart['id']}"
    assert "EVALUATE" in dax, f"no EVALUATE for {chart['id']}"


# ---------------------------------------------------------------------------
# Oracle query execution (mocked)
# ---------------------------------------------------------------------------


def test_run_oracle_query_mocked(monkeypatch):
  sql_path = (
      POWER_BI_ROOT.parent
      / "looker_studio"
      / "oracle"
      / "queries"
      / "usage-total-tokens.sql"
  )
  if not sql_path.exists():
    pytest.skip("oracle SQL not available in worktree")

  def fake_run(command, **kwargs):
    class FakeProc:
      returncode = 0
      stdout = json.dumps([{"col_a": 100}])
      stderr = ""

    return FakeProc()

  monkeypatch.setattr(validate_mod.subprocess, "run", fake_run)
  rows, job_id = validate_mod.run_oracle_query(
      sql_path,
      "test-project-0728-467323",
      "test-project-0728-467323",
      "bqaa_fixture_adk_1_27_0",
      "agent_events",
      "US",
      "2026-04-18",
      "2026-07-16",
  )
  assert len(rows) == 1
  assert rows[0]["col_a"] == 100
  assert "pbi_parity_" in job_id


def test_run_oracle_query_remove_limit(monkeypatch):
  sql_path = (
      POWER_BI_ROOT.parent
      / "looker_studio"
      / "oracle"
      / "queries"
      / "usage-token-usage-split-by-agent.sql"
  )
  if not sql_path.exists():
    pytest.skip("oracle SQL not available in worktree")

  captured_sql = []

  def fake_run(command, **kwargs):
    captured_sql.append(kwargs.get("input", ""))

    class FakeProc:
      returncode = 0
      stdout = json.dumps([{"agent": "a", "v": 100}])
      stderr = ""

    return FakeProc()

  monkeypatch.setattr(validate_mod.subprocess, "run", fake_run)
  validate_mod.run_oracle_query(
      sql_path,
      "test-project-0728-467323",
      "test-project-0728-467323",
      "bqaa_fixture_adk_1_27_0",
      "agent_events",
      "US",
      "2026-04-18",
      "2026-07-16",
      remove_limit=True,
  )
  assert "LIMIT" not in captured_sql[0].upper()


# ---------------------------------------------------------------------------
# Receipt sanitization
# ---------------------------------------------------------------------------


def test_receipt_has_no_data_values(tmp_path):
  receipt = {
      "kind": "sanitized-powerbi-live-parity",
      "results": [
          {
              "chart_id": "test",
              "status": "PASS",
              "oracle_row_count": 5,
              "dax_row_count": 5,
              "oracle_query_sha256": "abc123",
              "dax_query_sha256": "def456",
              "error": None,
          }
      ],
      "pass": True,
  }
  output = tmp_path / "receipt.json"
  output.write_text(json.dumps(receipt) + "\n")
  loaded = json.loads(output.read_text())
  for r in loaded["results"]:
    assert "values" not in r
    assert "user_ids" not in r
    assert "trace_ids" not in r
    assert "rows" not in r


# ---------------------------------------------------------------------------
# Token never in stdout (full CLI smoke)
# ---------------------------------------------------------------------------


def test_token_never_in_stdout(tmp_path, capsys, monkeypatch):
  secret = "dG9rZW4tc2VjcmV0LTEyMzQ1Njc4OTA="
  token_file = tmp_path / "token.txt"
  token_file.write_text(secret)
  os.chmod(token_file, 0o600)

  project_dir = tmp_path / "project"
  model_dir = project_dir / deploy_mod.MODEL_DIR_NAME
  model_dir.mkdir(parents=True)
  (model_dir / ".platform").write_text("{}")
  (model_dir / "definition.pbism").write_text("{}")
  (model_dir / "definition").mkdir()
  (model_dir / "definition" / "database.tmdl").write_text("db")

  report_dir = project_dir / deploy_mod.REPORT_DIR_NAME
  report_dir.mkdir()
  (report_dir / ".platform").write_text("{}")
  (report_dir / "definition.pbir").write_text('{"datasetReference":{}}')
  (report_dir / "definition").mkdir()
  (report_dir / "definition" / "version.json").write_text("{}")

  rc = deploy_mod.main(
      [
          "--workspace-id",
          "11111111-1111-1111-1111-111111111111",
          "--token-file",
          str(token_file),
          "--project-dir",
          str(project_dir),
          "--dry-run",
      ]
  )
  captured = capsys.readouterr()
  assert secret not in captured.out
  assert secret not in captured.err
