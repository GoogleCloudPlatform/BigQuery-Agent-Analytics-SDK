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

import base64
import datetime
import decimal
import email.utils
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
import tempfile

import pytest
import yaml

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
  assert visual_types == {
      "cardVisual",
      "clusteredBarChart",
      "areaChart",
      "lineChart",
  }
  assert validate_mod.TIME_SERIES_VISUAL_TYPES == {"areaChart", "lineChart"}


def test_chart_metadata_has_measures():
  charts = validate_mod.load_chart_metadata()
  for c in charts:
    assert len(c["measures"]) >= 1, c["id"]
    assert len(c["measure_info"]) == len(c["measures"])


# Chart id -> oracle column prefix of the six period-over-period KPI cards.
KPI_CARD_PREFIXES = {
    "usage-total-tokens": "pop_total_tokens",
    "usage-total-traces": "pop_total_traces",
    "usage-total-sessions": "pop_total_sessions",
    "usage-total-calls": "pop_llm_calls",
    "usage-total-users": "pop_total_users",
    "performance-total-errors": "pop_tool_errors",
}


def test_chart_metadata_kpi_cards_bind_value_and_change_pct():
  charts = validate_mod.load_chart_metadata()
  kpi_cards = [c for c in charts if c["id"] in KPI_CARD_PREFIXES]
  assert len(kpi_cards) == 6
  for c in kpi_cards:
    prefix = KPI_CARD_PREFIXES[c["id"]]
    # Each scorecard binds the KPI value and its "Change %" callout, and the
    # live comparison checks both against the oracle's current/change columns.
    assert len(c["measures"]) == 2, f"{c['id']}: {c['measures']}"
    assert c["measures"][0].startswith("KPI "), c["id"]
    assert c["measures"][1] == f"{c['measures'][0]} Change %", c["id"]
    assert [m["oracle_column"] for m in c["measure_info"]] == [
        f"{prefix}_current",
        f"{prefix}_change",
    ], c["id"]


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
  assert len(bars) == 12
  for c in bars:
    assert c["top_n"] in (5, 10), c["id"]
  llm_top5 = next(
      c for c in bars if c["id"] == "usage-top-5-agents-by-llm-calls"
  )
  assert llm_top5["top_n"] == 5
  assert llm_top5["measures"] == ["Top 5 Agents by LLM Calls"]
  assert llm_top5["measure_info"][0]["oracle_column"] == "total_llm_calls"


def test_chart_metadata_time_series_chart_with_series():
  charts = validate_mod.load_chart_metadata()
  series_charts = [
      c
      for c in charts
      if c["visual_type"] in validate_mod.TIME_SERIES_VISUAL_TYPES
      and c["series"]
  ]
  assert len(series_charts) == 1
  assert series_charts[0]["id"] == "usage-tool-calls-over-time"
  assert series_charts[0]["visual_type"] == "lineChart"


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


def test_compare_card_empty_oracle_is_rejected():
  oracle_row = {}
  dax_row = {"[v0]": None}
  match, _, _, _ = validate_mod.compare_card(oracle_row, [dax_row], ["col_a"])
  assert match is False


def test_compare_card_c5_1_empty_oracle_empty_dax():
  """C5-1: empty oracle and empty dax -> fail."""
  assert (
      validate_mod.compare_card({}, [], ["pop_total_tokens_current"])[0]
      is False
  )


def test_compare_card_c5_2_null_oracle_empty_dax():
  """C5-2: null-valued oracle and empty dax -> fail."""
  assert validate_mod.compare_card({"x": None}, [], ["x"])[0] is False


def test_compare_card_c5_3_empty_oracle_blank_dax():
  """C5-3: empty oracle with a blank dax row -> fail."""
  assert validate_mod.compare_card({}, [{"[v0]": None}], ["x"])[0] is False


def test_compare_card_c5_4_two_dax_rows_is_rejected():
  """C5-4: two dax rows -> fail (card expects exactly one)."""
  assert (
      validate_mod.compare_card({"x": 10}, [{"[v0]": 10}, {"[v0]": 99}], ["x"])[
          0
      ]
      is False
  )


def test_compare_card_null_oracle_null_dax_is_valid():
  """Explicit null on both sides is a valid match (blank)."""
  assert (
      validate_mod.compare_card({"x": None}, [{"[v0]": None}], ["x"])[0] is True
  )


def test_compare_card_dax_row_count_rejected():
  """dax_rows that is not a list of exactly 1 dict -> fail."""
  match, code, _, _ = validate_mod.compare_card({"x": 10}, [], ["x"])
  assert match is False
  assert code == "dax_row_count"


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
  assert "intruder" not in detail


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
  # Exactly N members, the two slots tied at the N-th value taken by c and d.
  dax = [
      {"agent": "a", "v": 100},
      {"agent": "c", "v": 80},
      {"agent": "d", "v": 80},
  ]
  match, detail, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 3)
  assert match is True
  assert detail == "tie_adjusted"


def test_compare_topn_rejects_more_than_n_dax_rows():
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
  match, detail, _, _ = validate_mod.compare_topn(oracle, dax, "v", "agent", 3)
  assert match is False
  assert detail == "row_count"


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


def test_compare_series_with_series_null_vs_empty_is_distinguishable():
  """Null and empty-string series values must remain distinguishable."""
  oracle = [{"timestamp_date": "2026-07-03", "tool_name": None, "v": 4}]
  dax_empty = [
      {
          "[Date]": "2026-07-03T00:00:00",
          "events_v1[tool_completed_name]": "",
          "[v]": 4,
      }
  ]
  match, _, _, _ = validate_mod.compare_series(
      oracle, dax_empty, "v", "timestamp_date", has_series=True
  )
  assert match is False


def test_compare_series_with_series_null_both_sides_passes():
  """Null series on both sides matches."""
  oracle = [{"timestamp_date": "2026-07-03", "tool_name": None, "v": 4}]
  dax = [
      {
          "[Date]": "2026-07-03T00:00:00",
          "events_v1[tool_completed_name]": None,
          "[v]": 4,
      }
  ]
  match, _, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date", has_series=True
  )
  assert match is True


def test_compare_series_with_series_tool_name_passes():
  """The tool_name oracle column maps to the tool_completed_name DAX key."""
  oracle = [{"timestamp_date": "2026-07-03", "tool_name": "search", "v": 4}]
  dax = [
      {
          "[Date]": "2026-07-03T00:00:00",
          "events_v1[tool_completed_name]": "search",
          "[v]": 4,
      }
  ]
  match, _, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date", has_series=True
  )
  assert match is True


def test_compare_series_missing_series_key_returns_missing_series():
  """Missing tool_name/tool_completed_name with has_series=True fails."""
  oracle = [{"timestamp_date": "2026-07-03", "v": 4}]
  dax = [
      {"[Date]": "2026-07-03T00:00:00", "[v]": 4},
  ]
  match, detail, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date", has_series=True
  )
  assert match is False
  assert detail == "missing_series"


def test_compare_series_missing_series_key_on_dax_returns_missing_series():
  """DAX row without a series dimension key fails."""
  oracle = [{"timestamp_date": "2026-07-03", "tool_name": "search", "v": 4}]
  dax = [{"[Date]": "2026-07-03T00:00:00", "[v]": 4}]
  match, detail, _, _ = validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date", has_series=True
  )
  assert match is False
  assert detail == "missing_series"


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


# ---------------------------------------------------------------------------
# Strict GUID / identifier validation
# ---------------------------------------------------------------------------

GUID_A = "11111111-1111-1111-1111-111111111111"
GUID_B = "22222222-2222-2222-2222-222222222222"
GUID_C = "33333333-3333-3333-3333-333333333333"

_BAD_GUID_STRINGS = [
    GUID_A + "\n",
    GUID_A + "\r\n",
    GUID_A + "\r",
    GUID_A + " ",
    " " + GUID_A,
    "\n" + GUID_A,
    GUID_A + "\x00",
    GUID_A[:-1],
    GUID_A + "0",
    GUID_A.replace("-", ""),
    "",
    "not-a-guid",
]
_NON_STRING_GUIDS = [None, 123, 1.5, True, GUID_A.encode("ascii"), [GUID_A]]
_BOTH_MODULES = pytest.mark.parametrize(
    "module", [deploy_mod, validate_mod], ids=["deploy", "live"]
)


@_BOTH_MODULES
@pytest.mark.parametrize("bad", _BAD_GUID_STRINGS + _NON_STRING_GUIDS, ids=repr)
def test_validate_guid_rejects_malformed_values(module, bad):
  with pytest.raises(ValueError, match="invalid"):
    module.validate_guid(bad)


@_BOTH_MODULES
@pytest.mark.parametrize("bad", _BAD_GUID_STRINGS, ids=repr)
def test_guid_regex_alone_rejects_malformed_strings(module, bad):
  """A caller that uses GUID_RE.match() directly is protected as well."""
  assert module.GUID_RE.match(bad) is None


@_BOTH_MODULES
def test_validate_guid_accepts_upper_and_lower_case(module):
  upper = "ABCDEF12-ABCD-ABCD-ABCD-ABCDEF123456"
  assert module.validate_guid(upper) == upper
  assert module.validate_guid(upper.lower()) == upper.lower()


@_BOTH_MODULES
def test_validate_guid_error_names_the_field(module):
  with pytest.raises(ValueError, match="invalid semantic_model_id"):
    module.validate_guid(GUID_A + "\n", "semantic_model_id")


@pytest.mark.parametrize(
    "pattern_name, good",
    [
        ("PROJECT_RE", "test-project-0728-467323"),
        ("DATASET_RE", "bqaa_fixture_adk_1_27_0"),
        ("TABLE_RE", "agent_events"),
        ("LOCATION_RE", "US"),
    ],
)
def test_live_identifier_regexes_reject_trailing_newline(pattern_name, good):
  pattern = getattr(validate_mod, pattern_name)
  assert pattern.match(good)
  validate_mod.require_identifier("identifier", good, pattern)
  for bad in (good + "\n", good + "\r\n", good + " ", "\n" + good):
    assert pattern.match(bad) is None, repr(bad)
    assert pattern.fullmatch(bad) is None, repr(bad)
    with pytest.raises(ValueError, match="invalid"):
      validate_mod.require_identifier("identifier", bad, pattern)


@pytest.mark.parametrize("bad", [None, 7, b"agent_events", ["agent_events"]])
def test_require_identifier_rejects_non_strings(bad):
  with pytest.raises(ValueError, match="invalid"):
    validate_mod.require_identifier("table ID", bad, validate_mod.TABLE_RE)


def test_deploy_main_rejects_workspace_id_with_trailing_newline(tmp_path):
  with pytest.raises(ValueError, match="invalid workspace ID"):
    deploy_mod.main(
        [
            "--workspace-id",
            GUID_A + "\n",
            "--project-dir",
            str(tmp_path),
            "--dry-run",
        ]
    )


# ---------------------------------------------------------------------------
# Fabric REST double
# ---------------------------------------------------------------------------

FABRIC = deploy_mod.FABRIC_API_BASE
LIST_URL = f"{FABRIC}/workspaces/{GUID_A}/semanticModels"
REPORTS_URL = f"{FABRIC}/workspaces/{GUID_A}/reports"
OP_URL = f"{FABRIC}/operations/op-1"
OP_RESULT_URL = f"{FABRIC}/operations/op-1/result"
LRO_HEADERS = {"Location": OP_URL, "x-ms-operation-id": "op-1"}
NAME = "BQAA Agent Analytics"
PARTS = [{"path": "definition.pbism", "data": b"{}"}]


def _resp(status, body=b"", headers=None):
  if isinstance(body, (dict, list)):
    body = json.dumps(body)
  if isinstance(body, str):
    body = body.encode("utf-8")
  return status, dict(headers or {}), body


def _item(item_id, name=NAME):
  return {"displayName": name, "id": item_id}


class _FakeFabric:
  """Scripted stand-in for ``deploy_fabric.http_request``.

  Every entry of *script* is ``(method, url, response)``.  A request that is
  not the next scripted one fails the test, and each URL passes the tool's own
  allow-list first, exactly as the real ``http_request`` does.
  """

  def __init__(self, *script):
    self.script = list(script)
    self.requests = []

  def __call__(self, url, method, token, body=None):
    deploy_mod.validate_operation_url(url)
    self.requests.append((method, url, body))
    assert self.script, f"unexpected request: {method} {url}"
    expected_method, expected_url, response = self.script.pop(0)
    assert (method, url) == (expected_method, expected_url)
    return response

  def posts(self):
    return [r for r in self.requests if r[0] == "POST"]


def _install(monkeypatch, *script):
  fake = _FakeFabric(*script)
  monkeypatch.setattr(deploy_mod, "http_request", fake)
  return fake


@pytest.fixture(name="sleeps")
def _sleeps(monkeypatch):
  """Replace time.sleep in the deploy tool and record the requested waits."""
  waits = []
  monkeypatch.setattr(deploy_mod.time, "sleep", waits.append)
  return waits


def _http_date(delta_seconds):
  when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
      seconds=delta_seconds
  )
  return email.utils.format_datetime(when, usegmt=True)


# ---------------------------------------------------------------------------
# LRO polling: empty result body and Retry-After
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result_response",
    [_resp(200, b""), _resp(200, b"  \n"), _resp(204, b"")],
    ids=["empty-200", "blank-200", "no-content-204"],
)
def test_poll_lro_succeeded_with_empty_result_body_is_an_empty_dict(
    monkeypatch, sleeps, result_response
):
  _install(
      monkeypatch,
      ("GET", OP_URL, _resp(200, {"status": "Succeeded"})),
      ("GET", OP_RESULT_URL, result_response),
  )
  assert deploy_mod.poll_lro("tok", OP_URL, "op-1") == {}


def test_poll_lro_succeeded_returns_the_result_object(monkeypatch, sleeps):
  _install(
      monkeypatch,
      ("GET", OP_URL, _resp(200, {"status": "Succeeded"})),
      ("GET", OP_RESULT_URL, _resp(200, {"id": GUID_A})),
  )
  assert deploy_mod.poll_lro("tok", OP_URL, "op-1") == {"id": GUID_A}


@pytest.mark.parametrize(
    "result_response, message",
    [
        (
            _resp(500, {"errorCode": "Boom", "message": "bad"}),
            "LRO result fetch failed: Boom",
        ),
        (_resp(200, b"<html>not json</html>"), "non-JSON"),
        (_resp(200, b'["not", "an", "object"]'), "unexpected JSON shape"),
    ],
    ids=["http-500", "non-json", "json-array"],
)
def test_poll_lro_rejects_a_bad_result_body(
    monkeypatch, sleeps, result_response, message
):
  _install(
      monkeypatch,
      ("GET", OP_URL, _resp(200, {"status": "Succeeded"})),
      ("GET", OP_RESULT_URL, result_response),
  )
  with pytest.raises(RuntimeError, match=message):
    deploy_mod.poll_lro("tok", OP_URL, "op-1")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("5", 5),
        (" 7 ", 7),
        (12, 12),
        ("0", 0),
        ("-3", 0),
        ("99999", deploy_mod.MAX_LRO_SECONDS),
        (None, deploy_mod.DEFAULT_RETRY_AFTER),
        ("", deploy_mod.DEFAULT_RETRY_AFTER),
        ("soon", deploy_mod.DEFAULT_RETRY_AFTER),
        ("1.5", deploy_mod.DEFAULT_RETRY_AFTER),
        ("9" * 5000, deploy_mod.DEFAULT_RETRY_AFTER),
    ],
)
def test_parse_retry_after_delta_seconds_and_garbage(value, expected):
  assert deploy_mod._parse_retry_after(value) == expected


def test_parse_retry_after_http_date_in_the_future():
  assert 20 <= deploy_mod._parse_retry_after(_http_date(30)) <= 30


def test_parse_retry_after_http_date_in_the_past_is_zero():
  assert deploy_mod._parse_retry_after(_http_date(-3600)) == 0


def test_parse_retry_after_far_future_http_date_is_clamped():
  assert (
      deploy_mod._parse_retry_after(_http_date(86400))
      == deploy_mod.MAX_LRO_SECONDS
  )


def test_parse_retry_after_respects_a_custom_maximum():
  assert deploy_mod._parse_retry_after("100", 20) == 20
  assert deploy_mod._parse_retry_after(_http_date(86400), 45) == 45


def test_parse_retry_after_treats_a_zoneless_date_as_utc():
  when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
      seconds=30
  )
  zoneless = email.utils.format_datetime(when.replace(tzinfo=None))
  assert zoneless.endswith("-0000")
  assert 20 <= deploy_mod._parse_retry_after(zoneless) <= 30


def test_poll_lro_honors_http_date_and_any_header_case(monkeypatch, sleeps):
  _install(
      monkeypatch,
      (
          "GET",
          OP_URL,
          _resp(200, {"status": "Running"}, {"retry-after": _http_date(5)}),
      ),
      (
          "GET",
          OP_URL,
          _resp(200, {"status": "Running"}, {"RETRY-AFTER": "3"}),
      ),
      ("GET", OP_URL, _resp(200, {"status": "Succeeded"})),
      ("GET", OP_RESULT_URL, _resp(200, b"")),
  )
  assert deploy_mod.poll_lro("tok", OP_URL, "op-1") == {}
  assert sleeps[0] == deploy_mod.DEFAULT_RETRY_AFTER
  assert 0 <= sleeps[1] <= 5
  assert sleeps[2] == 3


# ---------------------------------------------------------------------------
# find_existing_item: pagination, failures, duplicates
# ---------------------------------------------------------------------------


def _find():
  return deploy_mod.find_existing_item("tok", GUID_A, "semanticModels", NAME)


def test_find_existing_item_follows_continuation_token(monkeypatch):
  fake = _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(
              200,
              {
                  "value": [_item(GUID_B, "other")],
                  "continuationToken": "tok en&x=1+2",
              },
          ),
      ),
      (
          "GET",
          f"{LIST_URL}?continuationToken=tok%20en%26x%3D1%2B2",
          _resp(200, {"value": [_item(GUID_C)]}),
      ),
  )
  assert _find() == GUID_C
  assert not fake.script


def test_find_existing_item_prefers_the_continuation_uri(monkeypatch):
  next_url = f"{LIST_URL}?continuationToken=abc"
  fake = _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(
              200,
              {
                  "value": [],
                  "continuationUri": next_url,
                  "continuationToken": "zzz",
              },
          ),
      ),
      ("GET", next_url, _resp(200, {"value": [_item(GUID_B)]})),
  )
  assert _find() == GUID_B
  assert not fake.script


def test_find_existing_item_rejects_an_untrusted_continuation_uri(monkeypatch):
  fake = _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(
              200,
              {
                  "value": [],
                  "continuationUri": "https://evil.example.com/steal?x=1",
              },
          ),
      ),
  )
  with pytest.raises(ValueError, match="untrusted operation URL"):
    _find()
  assert [url for _, url, _ in fake.requests] == [LIST_URL]


def test_find_existing_item_rejects_a_continuation_uri_to_another_collection(
    monkeypatch,
):
  """A continuationUri pointing to a different workspace or collection is
  rejected even if it targets a valid Fabric host."""
  other_url = (
      f"{deploy_mod.FABRIC_API_BASE}/workspaces/other-workspace-id/reports"
  )
  _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(200, {"value": [], "continuationUri": other_url}),
      ),
  )
  with pytest.raises(ValueError, match="Invalid continuationUri collection"):
    _find()


def test_find_existing_item_fails_closed_on_a_listing_error(monkeypatch):
  _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(500, {"errorCode": "InternalError", "message": "later"}),
      ),
  )
  with pytest.raises(
      RuntimeError, match=r"list semanticModels failed \(500\): InternalError"
  ):
    _find()


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_find_existing_item_raises_on_any_non_200(monkeypatch, status):
  _install(monkeypatch, ("GET", LIST_URL, _resp(status, b"{}")))
  with pytest.raises(
      RuntimeError, match=rf"list semanticModels failed \({status}\)"
  ):
    _find()


def test_find_existing_item_rejects_duplicates_on_one_page(monkeypatch):
  _install(
      monkeypatch,
      ("GET", LIST_URL, _resp(200, {"value": [_item(GUID_B), _item(GUID_C)]})),
  )
  with pytest.raises(RuntimeError, match="Ambiguous duplicate semanticModels"):
    _find()


def test_find_existing_item_rejects_duplicates_across_pages(monkeypatch):
  _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(200, {"value": [_item(GUID_B)], "continuationToken": "p2"}),
      ),
      (
          "GET",
          f"{LIST_URL}?continuationToken=p2",
          _resp(200, {"value": [_item(GUID_C)]}),
      ),
  )
  with pytest.raises(RuntimeError, match="Ambiguous duplicate semanticModels"):
    _find()


def test_find_existing_item_same_id_repeated_is_not_ambiguous(monkeypatch):
  _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(200, {"value": [_item(GUID_B)], "continuationToken": "p2"}),
      ),
      (
          "GET",
          f"{LIST_URL}?continuationToken=p2",
          _resp(200, {"value": [_item(GUID_B)]}),
      ),
  )
  assert _find() == GUID_B


def test_find_existing_item_is_none_only_after_a_complete_scan(monkeypatch):
  fake = _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(
              200,
              {"value": [_item(GUID_B, "other")], "continuationToken": "p2"},
          ),
      ),
      (
          "GET",
          f"{LIST_URL}?continuationToken=p2",
          _resp(200, {"value": [_item(GUID_C, "another")]}),
      ),
  )
  assert _find() is None
  assert len(fake.requests) == 2


def test_find_existing_item_name_match_is_exact(monkeypatch):
  _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(
              200,
              {
                  "value": [
                      _item(GUID_B, NAME.lower()),
                      _item(GUID_C, NAME + " "),
                  ]
              },
          ),
      ),
  )
  assert _find() is None


@pytest.mark.parametrize(
    "entry",
    [
        {"displayName": NAME},
        {"displayName": NAME, "id": None},
        {"displayName": NAME, "id": ""},
        {"displayName": NAME, "id": "   "},
    ],
    ids=["no-id", "null-id", "empty-id", "blank-id"],
)
def test_find_existing_item_rejects_a_match_without_an_id(monkeypatch, entry):
  """A matching entry without a usable id is an error, never absence."""
  _install(
      monkeypatch,
      ("GET", LIST_URL, _resp(200, {"value": [_item(GUID_C, "other"), entry]})),
  )
  with pytest.raises(RuntimeError, match="with a missing or empty id"):
    _find()


@pytest.mark.parametrize(
    "bad_id",
    [123, "bad-id", GUID_B + "\n", ["x"]],
    ids=["number", "not-a-guid", "trailing-newline", "list"],
)
def test_find_existing_item_rejects_a_match_with_an_invalid_id(
    monkeypatch, bad_id
):
  """A matching entry whose id is not exactly one GUID raises ValueError."""
  _install(
      monkeypatch,
      ("GET", LIST_URL, _resp(200, {"value": [_item(bad_id)]})),
  )
  with pytest.raises(ValueError, match="invalid semanticModels_id"):
    _find()


def test_find_existing_item_ignores_non_matching_entries_without_an_id(
    monkeypatch,
):
  """Only a matching entry has to carry an id; other names are skipped."""
  _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(
              200,
              {"value": [{"displayName": "other"}, _item(GUID_B)]},
          ),
      ),
  )
  assert _find() == GUID_B


@pytest.mark.parametrize(
    "junk", ["x", None, 3, [1]], ids=["string", "null", "number", "list"]
)
def test_find_existing_item_rejects_non_object_entries(monkeypatch, junk):
  """A malformed entry could be the wanted item, so the listing fails."""
  _install(
      monkeypatch,
      ("GET", LIST_URL, _resp(200, {"value": [junk, _item(GUID_B)]})),
  )
  with pytest.raises(RuntimeError, match="returned a non-object item"):
    _find()


@pytest.mark.parametrize(
    "entry, error",
    [
        ({"displayName": "sample"}, RuntimeError),
        ({"displayName": "sample", "id": None}, RuntimeError),
        ({"displayName": "sample", "id": ""}, RuntimeError),
        ({"displayName": "sample", "id": "not-a-guid"}, ValueError),
        ("sample", RuntimeError),
    ],
    ids=["no-id", "null-id", "empty-id", "non-guid-id", "non-object"],
)
def test_deploy_item_update_never_creates_after_a_malformed_match(
    monkeypatch, entry, error
):
  """Only the listing GET is sent; the malformed match never becomes a POST."""
  fake = _install(
      monkeypatch,
      ("GET", REPORTS_URL, _resp(200, {"value": [entry]})),
      ("POST", REPORTS_URL, _resp(201, {"id": GUID_C})),
  )
  with pytest.raises(error):
    deploy_mod.deploy_item("tok", GUID_A, "reports", [], "sample", True)
  assert [method for method, _, _ in fake.requests] == ["GET"]
  assert fake.posts() == []


def test_deploy_item_update_with_an_empty_listing_still_creates(monkeypatch):
  fake = _install(
      monkeypatch,
      ("GET", REPORTS_URL, _resp(200, {"value": []})),
      ("POST", REPORTS_URL, _resp(201, {"id": GUID_C})),
  )
  assert (
      deploy_mod.deploy_item("tok", GUID_A, "reports", [], "sample", True)
      == GUID_C
  )
  assert [method for method, _, _ in fake.requests] == ["GET", "POST"]
  assert fake.posts()[0][2]["displayName"] == "sample"


def test_deploy_item_update_with_a_valid_match_updates_in_place(monkeypatch):
  fake = _install(
      monkeypatch,
      ("GET", REPORTS_URL, _resp(200, {"value": [_item(GUID_B, "sample")]})),
      ("POST", f"{REPORTS_URL}/{GUID_B}/updateDefinition", _resp(200, b"")),
  )
  assert (
      deploy_mod.deploy_item("tok", GUID_A, "reports", [], "sample", True)
      == GUID_B
  )
  assert [(m, u) for m, u, _ in fake.requests] == [
      ("GET", REPORTS_URL),
      ("POST", f"{REPORTS_URL}/{GUID_B}/updateDefinition"),
  ]
  assert "displayName" not in fake.posts()[0][2]


def test_find_existing_item_stops_on_a_self_referencing_continuation(
    monkeypatch,
):
  _install(
      monkeypatch,
      (
          "GET",
          LIST_URL,
          _resp(200, {"value": [], "continuationUri": LIST_URL}),
      ),
  )
  with pytest.raises(RuntimeError, match="pagination did not terminate"):
    _find()


@pytest.mark.parametrize(
    "body",
    [{"value": None}, {"value": "x"}, {"value": {"id": GUID_B}}, {"value": 3}],
    ids=["null", "string", "object", "number"],
)
def test_find_existing_item_rejects_a_malformed_value_list(monkeypatch, body):
  _install(monkeypatch, ("GET", LIST_URL, _resp(200, body)))
  with pytest.raises(RuntimeError, match="unexpected response shape"):
    _find()


@pytest.mark.parametrize(
    "response, message",
    [
        (_resp(200, b""), "empty body"),
        (_resp(200, b"   \n"), "empty body"),
        (_resp(200, b"<html>"), "non-JSON"),
        (_resp(200, b"[1, 2]"), "unexpected JSON shape"),
    ],
    ids=["empty", "blank", "html", "array"],
)
def test_find_existing_item_rejects_a_listing_that_is_not_an_object(
    monkeypatch, response, message
):
  _install(monkeypatch, ("GET", LIST_URL, response))
  with pytest.raises(RuntimeError, match=message):
    _find()


def test_find_existing_item_object_without_value_raises(monkeypatch):
  _install(monkeypatch, ("GET", LIST_URL, _resp(200, {})))
  with pytest.raises(RuntimeError, match="unexpected response shape"):
    _find()


# ---------------------------------------------------------------------------
# deploy_item: the id of an update is never lost, a created id is validated
# ---------------------------------------------------------------------------


def _deploy(update=False, item_type="semanticModels"):
  return deploy_mod.deploy_item(
      "tok", GUID_A, item_type, PARTS, NAME, update=update
  )


UPDATE_URL = f"{LIST_URL}/{GUID_B}/updateDefinition"
_FOUND = ("GET", LIST_URL, _resp(200, {"value": [_item(GUID_B)]}))


@pytest.mark.parametrize(
    "response",
    [_resp(200, b""), _resp(200, b"  "), _resp(200, b"OK"), _resp(200, {})],
    ids=["empty", "blank", "non-json", "empty-object"],
)
def test_deploy_item_update_keeps_the_id_when_the_response_has_no_id(
    monkeypatch, response
):
  _install(monkeypatch, _FOUND, ("POST", UPDATE_URL, response))
  assert _deploy(update=True) == GUID_B


def test_deploy_item_update_returns_the_id_from_the_response_body(monkeypatch):
  _install(
      monkeypatch, _FOUND, ("POST", UPDATE_URL, _resp(200, {"id": GUID_C}))
  )
  assert _deploy(update=True) == GUID_C


def test_deploy_item_update_with_empty_lro_result_keeps_the_id(
    monkeypatch, sleeps
):
  _install(
      monkeypatch,
      _FOUND,
      ("POST", UPDATE_URL, _resp(202, b"", LRO_HEADERS)),
      ("GET", OP_URL, _resp(200, {"status": "Succeeded"})),
      ("GET", OP_RESULT_URL, _resp(200, b"")),
  )
  assert _deploy(update=True) == GUID_B


def test_deploy_item_update_with_lro_result_id(monkeypatch, sleeps):
  _install(
      monkeypatch,
      _FOUND,
      (
          "POST",
          UPDATE_URL,
          _resp(
              202,
              b"",
              {"location": OP_URL, "X-MS-Operation-Id": "op-1"},
          ),
      ),
      ("GET", OP_URL, _resp(200, {"status": "Succeeded"})),
      ("GET", OP_RESULT_URL, _resp(200, {"id": GUID_C})),
  )
  assert _deploy(update=True) == GUID_C


def test_deploy_item_update_false_never_lists_and_creates(monkeypatch):
  fake = _install(monkeypatch, ("POST", LIST_URL, _resp(201, {"id": GUID_C})))
  assert _deploy(update=False) == GUID_C
  method, url, body = fake.requests[0]
  assert (method, url) == ("POST", LIST_URL)
  assert body["displayName"] == NAME
  assert body["definition"]["format"] == "TMDL"


def test_deploy_item_update_without_a_match_creates_the_item(monkeypatch):
  fake = _install(
      monkeypatch,
      ("GET", LIST_URL, _resp(200, {"value": []})),
      ("POST", LIST_URL, _resp(201, {"id": GUID_C})),
  )
  assert _deploy(update=True) == GUID_C
  assert len(fake.posts()) == 1


@pytest.mark.parametrize(
    "response",
    [_resp(201, b""), _resp(200, {}), _resp(201, {"id": ""}), _resp(201, b"x")],
    ids=["empty", "empty-object", "empty-id", "non-json"],
)
def test_deploy_item_create_without_an_id_fails(monkeypatch, response):
  _install(monkeypatch, ("POST", LIST_URL, response))
  with pytest.raises(RuntimeError, match="create semanticModels returned no"):
    _deploy()


def test_deploy_item_create_with_lro_result_without_an_id_fails(
    monkeypatch, sleeps
):
  _install(
      monkeypatch,
      ("POST", LIST_URL, _resp(202, b"", LRO_HEADERS)),
      ("GET", OP_URL, _resp(200, {"status": "Succeeded"})),
      ("GET", OP_RESULT_URL, _resp(200, b"")),
  )
  with pytest.raises(RuntimeError, match="returned no item id"):
    _deploy()


@pytest.mark.parametrize("bad_id", ["not-a-guid", GUID_C + "\n", 12345])
def test_deploy_item_create_validates_the_returned_id(monkeypatch, bad_id):
  _install(monkeypatch, ("POST", LIST_URL, _resp(201, {"id": bad_id})))
  with pytest.raises(ValueError, match="invalid semanticModels_id"):
    _deploy()


def test_deploy_item_create_accepts_a_well_formed_id(monkeypatch):
  _install(monkeypatch, ("POST", LIST_URL, _resp(201, {"id": GUID_C})))
  assert _deploy() == GUID_C


def test_deploy_item_202_without_lro_headers_fails(monkeypatch):
  _install(monkeypatch, ("POST", LIST_URL, _resp(202, b"", {})))
  with pytest.raises(RuntimeError, match="missing LRO headers"):
    _deploy()


def test_deploy_item_http_error_is_reported(monkeypatch):
  _install(
      monkeypatch,
      (
          "POST",
          LIST_URL,
          _resp(400, {"errorCode": "InvalidDefinition", "message": "bad"}),
      ),
  )
  with pytest.raises(RuntimeError, match="deploy failed: InvalidDefinition"):
    _deploy()


# ---------------------------------------------------------------------------
# deploy_fabric.main --update, end to end against the Fabric double
# ---------------------------------------------------------------------------


def _make_project(tmp_path):
  project_dir = tmp_path / "project"
  model_dir = project_dir / deploy_mod.MODEL_DIR_NAME
  (model_dir / "definition").mkdir(parents=True)
  (model_dir / ".platform").write_text("{}")
  (model_dir / "definition.pbism").write_text("{}")
  (model_dir / "definition" / "database.tmdl").write_text("database")
  report_dir = project_dir / deploy_mod.REPORT_DIR_NAME
  (report_dir / "definition").mkdir(parents=True)
  (report_dir / ".platform").write_text("{}")
  (report_dir / "definition.pbir").write_text(
      json.dumps({"datasetReference": {"byPath": {"path": "../model"}}})
  )
  (report_dir / "definition" / "version.json").write_text("{}")
  token_file = tmp_path / "token.txt"
  token_file.write_text("secret-token")
  os.chmod(token_file, 0o600)
  return project_dir, token_file


def _deploy_argv(project_dir, token_file, *extra):
  return [
      "--workspace-id",
      GUID_A,
      "--token-file",
      str(token_file),
      "--project-dir",
      str(project_dir),
      *extra,
  ]


def test_main_update_keeps_both_ids_when_the_service_returns_no_body(
    tmp_path, monkeypatch, capsys
):
  project_dir, token_file = _make_project(tmp_path)
  fake = _install(
      monkeypatch,
      ("GET", LIST_URL, _resp(200, {"value": [_item(GUID_B)]})),
      ("POST", f"{LIST_URL}/{GUID_B}/updateDefinition", _resp(200, b"")),
      ("GET", REPORTS_URL, _resp(200, {"value": [_item(GUID_C)]})),
      ("POST", f"{REPORTS_URL}/{GUID_C}/updateDefinition", _resp(200, b"")),
  )
  assert deploy_mod.main(_deploy_argv(project_dir, token_file, "--update")) == 0
  assert not fake.script
  out = capsys.readouterr().out
  assert f"semantic model id: {GUID_B}" in out
  assert f"report id: {GUID_C}" in out
  assert f"/groups/{GUID_A}/reports/{GUID_C}" in out
  report_body = fake.posts()[1][2]
  parts = {
      part["path"]: base64.b64decode(part["payload"])
      for part in report_body["definition"]["parts"]
  }
  pbir = json.loads(parts["definition.pbir"])
  assert (
      pbir["datasetReference"]["byConnection"]["connectionString"]
      == f"semanticmodelid={GUID_B}"
  )


def test_main_update_lookup_failure_never_posts(tmp_path, monkeypatch):
  project_dir, token_file = _make_project(tmp_path)
  fake = _install(monkeypatch, ("GET", LIST_URL, _resp(500, b"{}")))
  with pytest.raises(RuntimeError, match=r"list semanticModels failed \(500\)"):
    deploy_mod.main(_deploy_argv(project_dir, token_file, "--update"))
  assert fake.posts() == []


@pytest.mark.parametrize(
    "model_id, exc, message",
    [
        ("", RuntimeError, "semantic model id is empty"),
        (None, RuntimeError, "semantic model id is empty"),
        ("not-a-guid", ValueError, "invalid semantic_model_id"),
        (GUID_B + "\n", ValueError, "invalid semantic_model_id"),
    ],
    ids=["empty", "none", "malformed", "trailing-newline"],
)
def test_main_rejects_a_bad_model_id_before_deploying_the_report(
    tmp_path, monkeypatch, model_id, exc, message
):
  project_dir, token_file = _make_project(tmp_path)
  deployed = []

  def fake_deploy_item(
      token, workspace_id, item_type, parts, display_name, update=False
  ):
    deployed.append(item_type)
    return model_id

  monkeypatch.setattr(deploy_mod, "deploy_item", fake_deploy_item)
  with pytest.raises(exc, match=message):
    deploy_mod.main(_deploy_argv(project_dir, token_file))
  assert deployed == ["semanticModels"]


def _dax_response(rows):
  return {"results": [{"tables": [{"rows": rows}]}]}


def _date_row(min_date, max_date):
  return _dax_response([{"[min_date]": min_date, "[max_date]": max_date}])


# ---------------------------------------------------------------------------
# Live validator: scalar comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("1000000000", 1000000001, False),
        ("9007199254740992", "9007199254740993", False),
        (1_000_000_000, 1_000_000_001, False),
        (2**53, 2**53 + 1, False),
        ("1000000000", "1000000000", True),
        (1.0, 1.0000000001, True),
        (1.0, 1.00000001, False),
        ("corrupt-string", None, False),
        (None, "corrupt-string", False),
        ("corrupt-string", "corrupt-string", False),
        (None, None, True),
        ("", None, True),
        ("  ", None, True),
        (100, "100", True),
        (True, 1, False),
        (1, True, False),
        (False, 0, False),
        (float("nan"), float("nan"), False),
        (float("inf"), float("inf"), False),
        (float("nan"), None, False),
        ("NaN", None, False),
        ("Infinity", "Infinity", False),
        ("1e3", 1000, True),
        ("0x10", 16, False),
        ("1_000", 1000, False),
        ([1], None, False),
        ({"a": 1}, {"a": 1}, False),
        (decimal.Decimal("5"), 5, True),
        (decimal.Decimal("NaN"), 5, False),
    ],
)
def test_values_match_matrix_in_both_argument_orders(a, b, expected):
  assert validate_mod.values_match(a, b) is expected
  assert validate_mod.values_match(b, a) is expected


# ---------------------------------------------------------------------------
# Live validator: series, Top-N and card comparisons
# ---------------------------------------------------------------------------


def test_compare_series_corrupt_value_is_not_a_blank_in_either_direction():
  corrupt_oracle = [{"timestamp_date": "2026-07-01", "v": "garbage"}]
  corrupt_dax = [{"[Date]": "2026-07-01T00:00:00", "[v]": "garbage"}]
  ok, detail, _, _ = validate_mod.compare_series(
      corrupt_oracle, [], "v", "timestamp_date"
  )
  assert (ok, detail) == (False, "row_index=0")
  ok, detail, _, _ = validate_mod.compare_series(
      [], corrupt_dax, "v", "timestamp_date"
  )
  assert (ok, detail) == (False, "row_index=0")
  ok, _, _, _ = validate_mod.compare_series(
      corrupt_oracle, corrupt_dax, "v", "timestamp_date"
  )
  assert ok is False


def test_compare_series_large_counts_compare_exactly():
  oracle = [{"timestamp_date": "2026-07-01", "v": 1000000000}]
  dax = [{"[Date]": "2026-07-01T00:00:00", "[v]": 1000000001}]
  result = validate_mod.compare_series(oracle, dax, "v", "timestamp_date")
  assert result == (False, "row_index=0", 1000000000, 1000000001)


def test_compare_series_null_dax_date_key_is_a_mismatch_not_a_crash():
  oracle = [{"timestamp_date": "2026-07-01", "v": 5}]
  dax = [
      {"[Date]": None, "[v]": 5},
      {"[Date]": "2026-07-01T00:00:00", "[v]": 5},
  ]
  ok, _, _, _ = validate_mod.compare_series(oracle, dax, "v", "timestamp_date")
  assert ok is False


def test_compare_series_blank_on_both_sides_is_skipped():
  oracle = [{"timestamp_date": "2026-07-01", "v": None}]
  dax = [{"[Date]": "2026-07-01T00:00:00", "[v]": None}]
  ok, _, _, _ = validate_mod.compare_series(oracle, dax, "v", "timestamp_date")
  assert ok is True


def _topn(oracle, dax, n):
  return validate_mod.compare_topn(oracle, dax, "v", "agent", n)


def test_compare_topn_rejects_three_rows_for_top_two():
  oracle = [
      {"agent": "a", "v": 100},
      {"agent": "b", "v": 90},
      {"agent": "c", "v": 90},
  ]
  ok, detail, _, _ = _topn(oracle, list(oracle), 2)
  assert (ok, detail) == (False, "row_count")


def test_compare_topn_genuine_boundary_tie_still_passes():
  oracle = [
      {"agent": "a", "v": 100},
      {"agent": "b", "v": 90},
      {"agent": "c", "v": 90},
  ]
  dax = [{"agent": "a", "v": 100}, {"agent": "c", "v": 90}]
  assert _topn(oracle, dax, 2) == (True, "tie_adjusted", None, None)


def test_compare_topn_oracle_larger_than_n_with_dax_exactly_n_passes():
  oracle = [{"agent": f"m{i}", "v": 100 - i} for i in range(7)]
  dax = oracle[:5]
  assert _topn(oracle, dax, 5) == (True, None, None, None)


def test_compare_topn_blank_dax_rows_do_not_count_toward_n():
  oracle = [{"agent": "a", "v": 100}, {"agent": "b", "v": 90}]
  dax = oracle + [{"agent": "c", "v": None}, {"agent": "d", "v": ""}]
  assert _topn(oracle, dax, 2) == (True, None, None, None)


def test_compare_topn_identical_null_members_pass():
  oracle = [{"agent": None, "v": 5}, {"agent": "a", "v": 3}]
  dax = [
      {"events_v1[agent]": None, "[v]": 5},
      {"events_v1[agent]": "a", "[v]": 3},
  ]
  assert _topn(oracle, dax, 5) == (True, None, None, None)


def test_compare_topn_null_member_is_not_the_empty_string():
  oracle_null = [{"agent": None, "v": 5}]
  oracle_empty = [{"agent": "", "v": 5}]
  dax_null = [{"events_v1[agent]": None, "[v]": 5}]
  dax_empty = [{"events_v1[agent]": "", "[v]": 5}]
  assert _topn(oracle_null, dax_empty, 5)[:2] == (False, "member not in oracle")
  assert _topn(oracle_empty, dax_null, 5)[:2] == (False, "member not in oracle")


def test_compare_topn_rejects_a_repeated_member():
  oracle = [{"agent": "a", "v": 100}, {"agent": "b", "v": 90}]
  dax = [{"agent": "a", "v": 100}, {"agent": "a", "v": 100}]
  ok, detail, _, _ = _topn(oracle, dax, 5)
  assert (ok, detail) == (False, "duplicate_member")


@pytest.mark.parametrize("bad", ["oops", float("nan"), True, "1_0"])
def test_compare_topn_corrupt_values_fail_on_either_side(bad):
  good = [{"agent": "a", "v": 5}]
  ok, detail, _, _ = _topn([{"agent": "a", "v": bad}], good, 5)
  assert (ok, detail) == (False, "corrupt_value")
  ok, detail, _, _ = _topn(good, [{"agent": "a", "v": bad}], 5)
  assert (ok, detail) == (False, "corrupt_value")


def test_compare_topn_missing_dimension_fails_on_either_side():
  ok, detail, _, _ = _topn([{"v": 5}], [{"agent": "a", "v": 5}], 5)
  assert (ok, detail) == (False, "missing_dimension")
  ok, detail, _, _ = _topn([{"agent": "a", "v": 5}], [{"v": 5}], 5)
  assert (ok, detail) == (False, "missing_dimension")


def test_compare_topn_detail_never_carries_a_dimension_label():
  oracle = [{"agent": "a", "v": 100}, {"agent": "b", "v": 90}]
  dax = [{"agent": "a", "v": 100}, {"agent": "intruder", "v": 90}]
  result = _topn(oracle, dax, 5)
  assert result[0] is False
  assert "intruder" not in repr(result)


def test_compare_topn_detects_a_non_tie_member_swap():
  oracle = [{"agent": "a", "v": 100}, {"agent": "b", "v": 90}]
  dax = [{"agent": "b", "v": 100}, {"agent": "a", "v": 90}]
  ok, detail, _, _ = _topn(oracle, dax, 5)
  assert ok is False
  assert detail.startswith("member[")


# Every Top-N row is validated before a blank metric is omitted: a malformed
# row must fail on its own, not vanish and leave the remaining rows matching.
_TOPN_ORACLE = [{"agent": "a", "v": 7}, {"agent": "b", "v": 5}]
_TOPN_DAX = [
    {"events_v1[agent]": "a", "[v]": 7},
    {"events_v1[agent]": "b", "[v]": 5},
]


@pytest.mark.parametrize(
    "extra, code",
    [
        ("garbage-row", "dax_row_type"),
        (None, "dax_row_type"),
        (7, "dax_row_type"),
        (["c", 3], "dax_row_type"),
        ({"events_v1[agent]": "c"}, "dax_missing_metric"),
        ({"events_v1[agent]": None}, "dax_missing_metric"),
        ({"[v]": None}, "missing_dimension"),
        ({"[v]": 3}, "missing_dimension"),
        ({"events_v1[agent]": "a", "[v]": None}, "duplicate_member"),
        ({"events_v1[agent]": "b", "[v]": ""}, "duplicate_member"),
    ],
    ids=[
        "string",
        "null",
        "number",
        "list",
        "no-metric",
        "null-member-no-metric",
        "blank-no-dimension",
        "valued-no-dimension",
        "blank-repeat",
        "empty-string-repeat",
    ],
)
def test_compare_topn_rejects_a_malformed_dax_row(extra, code):
  assert _topn(_TOPN_ORACLE, _TOPN_DAX, 5) == (True, None, None, None)
  for dax in (_TOPN_DAX + [extra], [extra] + _TOPN_DAX):
    assert _topn(_TOPN_ORACLE, dax, 5) == (False, code, None, None)


@pytest.mark.parametrize(
    "extra, code",
    [
        ("garbage-row", "oracle_row_type"),
        (None, "oracle_row_type"),
        (7, "oracle_row_type"),
        (["c", 3], "oracle_row_type"),
        ({"agent": "c"}, "oracle_missing_metric"),
        ({"agent": None}, "oracle_missing_metric"),
        ({"v": None}, "missing_dimension"),
        ({"v": 3}, "missing_dimension"),
        ({"agent": "a", "v": None}, "duplicate_member"),
        ({"agent": "b", "v": 5}, "duplicate_member"),
    ],
    ids=[
        "string",
        "null",
        "number",
        "list",
        "no-metric",
        "null-member-no-metric",
        "blank-no-dimension",
        "valued-no-dimension",
        "blank-repeat",
        "identical-repeat",
    ],
)
def test_compare_topn_rejects_a_malformed_oracle_row(extra, code):
  assert _topn(_TOPN_ORACLE, _TOPN_DAX, 5) == (True, None, None, None)
  for oracle in (_TOPN_ORACLE + [extra], [extra] + _TOPN_ORACLE):
    assert _topn(oracle, _TOPN_DAX, 5) == (False, code, None, None)


def test_compare_topn_rejects_malformed_rows():
  """Each malformed row is a structured failure, never a skipped row."""
  oracle = [{"agent": "a", "count": 7}]
  valid = {"events_v1[agent]": "a", "[v]": 7}

  def compare(dax):
    return validate_mod.compare_topn(
        oracle, dax, "count", "agent", 5, metric_type="int"
    )

  assert compare([valid]) == (True, None, None, None)
  assert compare([valid, "garbage"]) == (False, "dax_row_type", None, None)
  assert compare([valid, {"events_v1[agent]": "b"}]) == (
      False,
      "dax_missing_metric",
      None,
      None,
  )
  assert compare([valid, {"events_v1[agent]": "a", "[v]": None}]) == (
      False,
      "duplicate_member",
      None,
      None,
  )

  oracle = [{"agent": "a", "n": 7}, {"agent": "b", "n": 5}]
  dax = [
      {"events_v1[agent]": "a", "v": 7},
      {"events_v1[agent]": "b", "v": 5},
  ]
  assert validate_mod.compare_topn(oracle, dax, "n", "agent", 5) == (
      True,
      None,
      None,
      None,
  )
  for oracle_rows, dax_rows, code in (
      (oracle, dax + ["garbage-row"], "dax_row_type"),
      (oracle, dax + [{"events_v1[agent]": "c"}], "dax_missing_metric"),
      (oracle + [{"agent": "c"}], dax, "oracle_missing_metric"),
      # A non-object oracle row used to raise AttributeError; it is now a
      # structured code (the run already failed it as comparison_error).
      (oracle + ["garbage-row"], dax, "oracle_row_type"),
  ):
    assert validate_mod.compare_topn(
        oracle_rows, dax_rows, "n", "agent", 5
    ) == (False, code, None, None)


def test_compare_topn_omits_a_well_formed_blank_category():
  blank_oracle = [{"agent": None, "v": None}, {"agent": "z", "v": None}]
  blank_dax = [
      {"events_v1[agent]": None, "[v]": None},
      {"events_v1[agent]": "z", "[v]": ""},
  ]
  # A blank category on both sides, or on one side only, is omitted, and
  # it does not count toward N.
  for oracle, dax in (
      (_TOPN_ORACLE + blank_oracle, _TOPN_DAX + blank_dax),
      (blank_oracle + _TOPN_ORACLE, blank_dax + _TOPN_DAX),
      (_TOPN_ORACLE + blank_oracle, _TOPN_DAX),
      (_TOPN_ORACLE, _TOPN_DAX + blank_dax),
  ):
    assert _topn(oracle, dax, 2) == (True, None, None, None)


def test_execute_dax_asks_for_explicit_nulls(monkeypatch):
  """A BLANK arrives as an explicit null, so an absent key is malformed."""
  sent = []

  class _Response:

    def __enter__(self):
      return self

    def __exit__(self, *exc_info):
      return False

    def read(self):
      return b'{"results": []}'

  def fake_urlopen(request, timeout):
    sent.append((json.loads(request.data), timeout))
    return _Response()

  monkeypatch.setattr(validate_mod.urllib.request, "urlopen", fake_urlopen)
  query = 'EVALUATE ROW("v", 1)'
  assert validate_mod.execute_dax("token", GUID_A, GUID_B, query) == {
      "results": []
  }
  assert sent == [
      (
          {
              "queries": [{"query": query}],
              "serializerSettings": {"includeNulls": True},
          },
          60,
      )
  ]


def _chart(chart_id):
  return next(
      c for c in validate_mod.load_chart_metadata() if c["id"] == chart_id
  )


def test_top5_llm_calls_chart_is_compared_as_a_top_5():
  chart = _chart("usage-top-5-agents-by-llm-calls")
  oracle = [
      {"agent": f"agent_{i}", "total_llm_calls": 100 - i} for i in range(7)
  ]
  dax = [{"events_v1[agent]": f"agent_{i}", "[v]": 100 - i} for i in range(5)]
  assert validate_mod._compare_chart(chart, oracle, oracle, dax) == (
      True,
      False,
      None,
  )
  six = dax + [{"events_v1[agent]": "agent_5", "[v]": 95}]
  assert validate_mod._compare_chart(chart, oracle, oracle, six) == (
      False,
      False,
      "top-n mismatch: row_count",
  )


@pytest.mark.parametrize("chart_id", sorted(KPI_CARD_PREFIXES))
def test_kpi_card_compares_value_and_change_pct_independently(chart_id):
  chart = _chart(chart_id)
  prefix = KPI_CARD_PREFIXES[chart_id]
  cols = [m["oracle_column"] for m in chart["measure_info"]]
  oracle_row = {cols[0]: 1200, cols[1]: 0.25, f"{prefix}_prior": 960}
  assert validate_mod.compare_card(
      oracle_row, [{"[v0]": 1200, "[v1]": 0.25}], cols
  ) == (True, None, None, None)
  ok, col, _, _ = validate_mod.compare_card(
      oracle_row, [{"[v0]": 1200, "[v1]": 0.5}], cols
  )
  assert (ok, col) == (False, f"{prefix}_change")
  ok, col, _, _ = validate_mod.compare_card(
      oracle_row, [{"[v0]": 1201, "[v1]": 0.25}], cols
  )
  assert (ok, col) == (False, f"{prefix}_current")
  ok, col, _, _ = validate_mod.compare_card(
      oracle_row, [{"[v0]": 1200, "[v1]": None}], cols
  )
  assert (ok, col) == (False, f"{prefix}_change")


@pytest.mark.parametrize("chart_id", sorted(KPI_CARD_PREFIXES))
def test_kpi_card_blank_oracle_does_not_match_a_zero_dax_value(chart_id):
  chart = _chart(chart_id)
  cols = [m["oracle_column"] for m in chart["measure_info"]]
  oracle_row = {cols[0]: None, cols[1]: None}
  ok, col, _, _ = validate_mod.compare_card(
      oracle_row, [{"[v0]": 0, "[v1]": None}], cols
  )
  assert (ok, col) == (False, cols[0])


@pytest.mark.parametrize("chart_id", sorted(KPI_CARD_PREFIXES))
def test_build_dax_query_kpi_card_reads_value_and_change_pct(chart_id):
  chart = _chart(chart_id)
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert f'"v0", CALCULATE([{chart["measures"][0]}]' in dax
  assert f'"v1", CALCULATE([{chart["measures"][1]}]' in dax


def test_build_dax_query_top5_llm_calls_chart():
  chart = _chart("usage-top-5-agents-by-llm-calls")
  dax = validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16")
  assert "[Top 5 Agents by LLM Calls]" in dax
  assert "'events_v1'[agent]" in dax


# ---------------------------------------------------------------------------
# Live validator: executeQueries response envelope
# ---------------------------------------------------------------------------

_BAD_DAX_RESPONSES = {
    "none": None,
    "list": [],
    "string": "text",
    "top-level-error": {"error": {"code": "X"}},
    "top-level-errors": {"errors": [{"code": "X"}]},
    "empty-object": {},
    "no-results": {"results": []},
    "null-results": {"results": None},
    "results-string": {"results": "x"},
    "result-error": {"results": [{"error": {"code": "X"}}]},
    "result-errors": {"results": [{"errors": ["x"]}]},
    "no-tables": {"results": [{}]},
    "empty-tables": {"results": [{"tables": []}]},
    "null-tables": {"results": [{"tables": None}]},
    "truncated-table": {
        "results": [{"tables": [{"error": {"code": "Truncated"}, "rows": []}]}]
    },
    "rows-string": {"results": [{"tables": [{"rows": "x"}]}]},
    "table-string": {"results": [{"tables": ["x"]}]},
    "result-string": {"results": ["x"]},
    "error-beside-results": {
        "error": {"code": "X"},
        "results": [{"tables": [{"rows": [{"[v0]": 1}]}]}],
    },
    "second-result-error": {
        "results": [{"tables": [{"rows": []}]}, {"error": {"code": "X"}}]
    },
    "second-table-error": {
        "results": [{"tables": [{"rows": []}, {"error": {"code": "X"}}]}]
    },
}


@pytest.mark.parametrize(
    "response", list(_BAD_DAX_RESPONSES.values()), ids=list(_BAD_DAX_RESPONSES)
)
def test_extract_dax_rows_rejects_error_and_malformed_envelopes(response):
  with pytest.raises(validate_mod.DaxResponseError, match="dax_response_error"):
    validate_mod.extract_dax_rows(response)


@pytest.mark.parametrize(
    "response, expected",
    [
        (
            {"results": [{"tables": [{"rows": [{"[v0]": None}]}]}]},
            [{"[v0]": None}],
        ),
        ({"results": [{"tables": [{"rows": []}]}]}, []),
        ({"results": [{"tables": [{}]}]}, []),
        ({"results": [{"tables": [{"rows": None}]}]}, []),
        (
            {"results": [{"tables": [{"rows": [{"[v0]": 5}]}]}]},
            [{"[v0]": 5}],
        ),
    ],
    ids=["blank-scalar", "empty-rows", "no-rows-key", "null-rows", "one-row"],
)
def test_extract_dax_rows_accepts_well_formed_results(response, expected):
  assert validate_mod.extract_dax_rows(response) == expected


# ---------------------------------------------------------------------------
# Live validator: windows
# ---------------------------------------------------------------------------

_BAD_WINDOWS = [
    "2026-07-16:2026-04-18",
    "2026-02-30:2026-03-01",
    "2026-04-01:2026-13-01",
    "2026-07-01",
    "2026-07-01:2026-07-02:2026-07-03",
    "20260701:20260702",
    "2026-07-01:2026-07-02\n",
    "2026-07-01 :2026-07-02",
    "2026-07-1:2026-07-02",
    "a:b",
    ":",
    "",
]


@pytest.mark.parametrize("spec", _BAD_WINDOWS, ids=repr)
def test_parse_window_rejects_invalid_specs(spec):
  with pytest.raises(ValueError, match="invalid"):
    validate_mod.parse_window(spec)


@pytest.mark.parametrize(
    "spec",
    ["2026-04-18:2026-07-16", "2026-07-16:2026-07-16", "2028-02-29:2028-02-29"],
)
def test_parse_window_accepts_valid_specs(spec):
  assert validate_mod.parse_window(spec) == tuple(spec.split(":"))


def _resolve(monkeypatch, response=None, error=None, allow_fallback=False):
  def fake_execute_dax(token, workspace_id, dataset_id, query):
    assert query == validate_mod.DATE_WINDOW_DAX
    if error is not None:
      raise error
    return response

  monkeypatch.setattr(validate_mod, "execute_dax", fake_execute_dax)
  return validate_mod.resolve_default_window(
      "tok", GUID_A, GUID_B, allow_fallback=allow_fallback
  )


def test_resolve_default_window_reads_the_date_table(monkeypatch):
  response = _date_row("2026-08-01T00:00:00", "2026-10-29T00:00:00")
  assert _resolve(monkeypatch, response) == (
      "2026-08-01",
      "2026-10-29",
      "date_table",
  )


@pytest.mark.parametrize(
    "response, error",
    [
        (None, RuntimeError("Date table unavailable")),
        ({"error": {"code": "X"}}, None),
        ({"results": []}, None),
        ({"results": [{"tables": [{"rows": []}]}]}, None),
        (_date_row("2026-10-29T00:00:00", "2026-08-01T00:00:00"), None),
        (_date_row("not-a-date", "2026-08-01T00:00:00"), None),
        (_date_row(None, None), None),
        (_date_row("2026-02-30T00:00:00", "2026-03-01T00:00:00"), None),
        ({"results": [{"tables": [{"rows": [{"[other]": 1}]}]}]}, None),
    ],
    ids=[
        "request-error",
        "error-envelope",
        "no-results",
        "no-rows",
        "reversed",
        "garbage",
        "null-dates",
        "impossible-date",
        "missing-columns",
    ],
)
def test_resolve_default_window_fails_closed_without_allow_fallback(
    monkeypatch, response, error
):
  with pytest.raises(RuntimeError, match="default_window_lookup_failed"):
    validate_mod.resolve_default_window("tok", GUID_A, GUID_B)


@pytest.mark.parametrize(
    "response, error",
    [
        (None, RuntimeError("Date table unavailable")),
        ({"error": {"code": "X"}}, None),
        ({"results": []}, None),
        ({"results": [{"tables": [{"rows": []}]}]}, None),
        (_date_row("2026-10-29T00:00:00", "2026-08-01T00:00:00"), None),
        (_date_row("not-a-date", "2026-08-01T00:00:00"), None),
        (_date_row(None, None), None),
        (_date_row("2026-02-30T00:00:00", "2026-03-01T00:00:00"), None),
        ({"results": [{"tables": [{"rows": [{"[other]": 1}]}]}]}, None),
    ],
    ids=[
        "request-error",
        "error-envelope",
        "no-results",
        "no-rows",
        "reversed",
        "garbage",
        "null-dates",
        "impossible-date",
        "missing-columns",
    ],
)
def test_resolve_default_window_falls_back_with_allow_fallback(
    monkeypatch, response, error
):
  start, end, source = _resolve(
      monkeypatch, response, error, allow_fallback=True
  )
  assert (start, end) == validate_mod.FIXTURE_WINDOW
  assert source == "fixture_fallback"


# ---------------------------------------------------------------------------
# Live validator: main() accounting, fail-closed rules, sanitization
# ---------------------------------------------------------------------------

PBI_PROJECT = "test-project-0728-467323"
PBI_DATASET = "bqaa_fixture_adk_1_27_0"
WINDOW = "2026-04-18:2026-07-16"
SENTINEL = "confidential_customer_x"


def _no_date_table():
  raise RuntimeError("Date table unavailable")


class _LiveHarness:
  """Runs ``validate_live_powerbi.main`` against stubbed oracle/DAX callers."""

  def __init__(self, tmp_path, monkeypatch, charts=None):
    self.oracle_calls = []
    self.dax_queries = []
    self.window_lookups = 0
    self.oracle = lambda name, start, end, remove_limit: []
    self.dax = lambda query: _dax_response([])
    self.date_window = lambda: _date_row(
        "2026-04-18T00:00:00", "2026-07-16T00:00:00"
    )
    self.token_file = tmp_path / "pbi-token.txt"
    self.token_file.write_text("pbi-secret-token")
    os.chmod(self.token_file, 0o600)
    self.output = tmp_path / "receipt.json"
    monkeypatch.setattr(validate_mod, "run_oracle_query", self._run_oracle)
    monkeypatch.setattr(validate_mod, "execute_dax", self._execute_dax)
    if charts is not None:
      monkeypatch.setattr(validate_mod, "load_chart_metadata", lambda: charts)

  def _run_oracle(
      self,
      sql_path,
      project,
      billing_project,
      dataset,
      table,
      location,
      start,
      end,
      remove_limit=False,
  ):
    self.oracle_calls.append((sql_path.name, start, end, remove_limit))
    return self.oracle(sql_path.name, start, end, remove_limit), "job"

  def _execute_dax(self, token, workspace_id, dataset_id, query):
    if query == validate_mod.DATE_WINDOW_DAX:
      self.window_lookups += 1
      return self.date_window()
    self.dax_queries.append(query)
    return self.dax(query)

  def run(self, *extra):
    argv = [
        "--workspace-id",
        GUID_A,
        "--dataset-id",
        GUID_B,
        "--pbi-token-file",
        str(self.token_file),
        "--project",
        PBI_PROJECT,
        "--dataset",
        PBI_DATASET,
        "--output",
        str(self.output),
        *extra,
    ]
    rc = validate_mod.main(argv)
    return rc, json.loads(self.output.read_text())

  def text(self, captured):
    return self.output.read_text() + captured.out + captured.err


def _tokens_card_harness(tmp_path, monkeypatch):
  harness = _LiveHarness(
      tmp_path, monkeypatch, charts=[_chart("usage-total-tokens")]
  )
  harness.oracle = lambda *_: [
      {"pop_total_tokens_current": 5, "pop_total_tokens_change": 0.5}
  ]
  harness.dax = lambda _: _dax_response([{"[v0]": 5, "[v1]": 0.5}])
  return harness


def _consistent_pair(chart):
  """Oracle rows and a matching DAX response for one chart."""
  cols = [m.get("oracle_column", "") for m in chart["measure_info"]]
  if chart["visual_type"] == "cardVisual":
    return (
        [{col: 5 for col in cols}],
        _dax_response([{f"[v{i}]": 5 for i in range(len(cols))}]),
    )
  if chart["visual_type"] in validate_mod.TIME_SERIES_VISUAL_TYPES:
    llm_trends = chart["id"] == "usage-llm-call-trends"
    value_col = "total_llm_calls" if llm_trends else cols[0]
    oracle_row = {"timestamp_date": "2026-07-01", value_col: 5}
    dax_row = {"[Date]": "2026-07-01T00:00:00", "[v]": 5}
    if chart["series"]:
      oracle_row["tool_name"] = "t1"
      dax_row["events_v1[tool_completed_name]"] = "t1"
    return [oracle_row], _dax_response([dax_row])
  dim_col = chart["category"].replace("events_v1.", "")
  oracle_dim = (
      "tool_name"
      if dim_col in ("tool_completed_name", "tool_error_name")
      else dim_col
  )
  return (
      [{oracle_dim: "m1", cols[0]: 5}],
      _dax_response([{f"events_v1[{dim_col}]": "m1", "[v]": 5}]),
  )


def _make_all_charts_consistent(harness, charts, window):
  start, end = window.split(":")
  by_oracle = {pathlib.Path(c["oracle_query"]).name: c for c in charts}
  by_dax = {validate_mod.build_dax_query(c, start, end): c for c in charts}
  assert len(by_oracle) == len(by_dax) == len(charts)
  llm_trends = next(c for c in charts if c["id"] == "usage-llm-call-trends")

  def oracle(name, _start, _end, _remove_limit):
    # The daily LLM-trend re-query runs from a temporary file.
    return _consistent_pair(by_oracle.get(name, llm_trends))[0]

  harness.oracle = oracle
  harness.dax = lambda query: _consistent_pair(by_dax[query])[1]
  return by_oracle, by_dax


def test_live_main_one_card_run_reports_its_own_denominator(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 0
  assert receipt["charts_expected"] == 1
  assert (
      receipt["charts_pass"],
      receipt["charts_fail"],
      receipt["charts_empty"],
  ) == (1, 0, 0)
  assert receipt["overall_pass"] is True
  assert receipt["pass"] is True
  out = capsys.readouterr().out
  assert "1/1 charts PASS across 1 window(s)" in out
  assert "37/37" not in out


def test_live_main_counts_every_chart_in_every_window_once(
    tmp_path, monkeypatch
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  other = "2026-07-17:2026-07-20"
  rc, receipt = harness.run(
      "--window", WINDOW, "--window", other, "--window", WINDOW
  )
  assert rc == 0
  assert receipt["windows"] == [WINDOW, other]
  assert receipt["charts_expected"] == 2
  assert receipt["charts_pass"] == 2
  assert [d["window"] for d in receipt["window_details"]] == [WINDOW, other]
  assert {d["source"] for d in receipt["window_details"]} == {"explicit"}
  assert [d["charts_pass"] for d in receipt["window_details"]] == [1, 1]


def test_live_main_one_failing_window_fails_the_run(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  harness.dax = lambda query: _dax_response(
      [{"[v0]": 6 if "DATE(2026, 07, 17)" in query else 5, "[v1]": 0.5}]
  )
  rc, receipt = harness.run(
      "--window", WINDOW, "--window", "2026-07-17:2026-07-20"
  )
  assert rc == 1
  assert (receipt["charts_pass"], receipt["charts_fail"]) == (1, 1)
  assert receipt["overall_pass"] is False
  assert "1/2 charts PASS across 2 window(s)" in capsys.readouterr().err


def test_live_main_all_37_consistent_charts_pass(tmp_path, monkeypatch, capsys):
  harness = _LiveHarness(tmp_path, monkeypatch)
  _make_all_charts_consistent(
      harness, validate_mod.load_chart_metadata(), WINDOW
  )
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 0
  assert (receipt["charts_expected"], receipt["charts_pass"]) == (37, 37)
  assert receipt["charts_fail"] == receipt["charts_empty"] == 0
  assert receipt["oracle_rows_total"] > 0
  assert (
      "live parity OK: 37/37 charts PASS across 1 window(s)"
      in capsys.readouterr().out
  )


def test_live_main_a_single_empty_chart_fails_unless_allowed(
    tmp_path, monkeypatch, capsys
):
  harness = _LiveHarness(tmp_path, monkeypatch)
  charts = validate_mod.load_chart_metadata()
  _make_all_charts_consistent(harness, charts, WINDOW)
  start, end = WINDOW.split(":")
  tokens = _chart("usage-total-tokens")
  tokens_dax = validate_mod.build_dax_query(tokens, start, end)
  base_oracle, base_dax = harness.oracle, harness.dax
  harness.oracle = lambda name, *rest: (
      [] if name == "usage-total-tokens.sql" else base_oracle(name, *rest)
  )
  harness.dax = lambda query: (
      _dax_response([]) if query == tokens_dax else base_dax(query)
  )
  # A cardVisual returning 0 DAX rows fails even under --allow-empty.
  rc, receipt = harness.run("--window", WINDOW, "--allow-empty")
  assert rc == 1
  assert receipt["charts_fail"] == 1
  assert receipt["overall_pass"] is False
  fail_result = [
      r for r in receipt["results"] if r["chart_id"] == "usage-total-tokens"
  ][0]
  assert fail_result["status"] == "FAIL"


def test_live_main_all_empty_run_is_not_a_pass(tmp_path, monkeypatch, capsys):
  harness = _LiveHarness(tmp_path, monkeypatch)
  # Scalar cardVisuals return exactly one row (always), while grouped
  # charts (bar/area) return [].  A card with blank values compares as
  # oracle_row={} which compare_card now rejects, so the run fails.
  charts = validate_mod.load_chart_metadata()
  harness.oracle = lambda *_: []

  def _empty_dax(query):
    return _dax_response([])

  harness.dax = _empty_dax
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert receipt["charts_expected"] == 37
  assert receipt["charts_pass"] == 0
  assert receipt["oracle_rows_total"] == 0
  assert receipt["overall_pass"] is False
  assert receipt["pass"] is False
  assert receipt["allow_empty"] is False
  assert "FAIL" in capsys.readouterr().err


def test_live_main_allow_empty_accepts_a_deliberately_empty_window(
    tmp_path, monkeypatch, capsys
):
  harness = _LiveHarness(tmp_path, monkeypatch)
  # Only grouped charts (bar/area) can be empty on both sides.  Card
  # visuals always return exactly one row, so a truly empty window
  # means the card oracle returns [] and the DAX returns [] too, which
  # compare_card rejects as a structural failure.  To exercise the
  # allow-empty path we make grouped charts empty and keep cards
  # consistent.
  charts = validate_mod.load_chart_metadata()
  card_count = sum(1 for c in charts if c["visual_type"] == "cardVisual")
  expected_empty = len(charts) - card_count
  start, end = WINDOW.split(":")

  def oracle(name, *rest):
    by_name = {pathlib.Path(c["oracle_query"]).name: c for c in charts}
    chart = by_name.get(name)
    if chart is None:
      # The daily LLM-trend re-query runs from a temp file.
      return []
    if chart["visual_type"] != "cardVisual":
      return []
    cols = [m.get("oracle_column", "") for m in chart["measure_info"]]
    return [{col: 5 for col in cols}]

  def dax(query):
    by_dax = {validate_mod.build_dax_query(c, start, end): c for c in charts}
    chart = by_dax.get(query)
    if chart and chart["visual_type"] != "cardVisual":
      return _dax_response([])
    cols = (
        [m.get("oracle_column", "") for m in chart["measure_info"]]
        if chart
        else []
    )
    return _dax_response([{f"[v{i}]": 5 for i in range(len(cols))}])

  harness.oracle = oracle
  harness.dax = dax
  rc, receipt = harness.run("--window", WINDOW, "--allow-empty")
  assert rc == 0
  assert receipt["allow_empty"] is True
  assert receipt["overall_pass"] is True
  assert receipt["charts_empty"] == expected_empty
  assert receipt["charts_pass"] == card_count
  assert f"{expected_empty} empty (allowed)" in capsys.readouterr().out


def test_live_main_dax_envelope_error_fails_even_with_allow_empty(
    tmp_path, monkeypatch
):
  harness = _LiveHarness(tmp_path, monkeypatch)
  harness.dax = lambda query: {"results": []}
  rc, receipt = harness.run("--window", WINDOW, "--allow-empty")
  assert rc == 1
  assert receipt["charts_fail"] == 37
  assert receipt["overall_pass"] is False
  assert {r["error"] for r in receipt["results"]} == {"dax_response_error"}


def test_live_main_a_run_without_charts_is_never_a_pass(tmp_path, monkeypatch):
  harness = _LiveHarness(tmp_path, monkeypatch, charts=[])
  rc, receipt = harness.run("--window", WINDOW, "--allow-empty")
  assert rc == 1
  assert receipt["charts_expected"] == 0
  assert receipt["overall_pass"] is False


def test_live_main_receipt_never_contains_numeric_values(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  harness.oracle = lambda *_: [
      {"pop_total_tokens_current": 7777701, "pop_total_tokens_change": 0.1}
  ]
  harness.dax = lambda _: _dax_response([{"[v0]": 8888802, "[v1]": 0.1}])
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert receipt["results"][0]["status"] == "FAIL"
  assert receipt["results"][0]["error"] == "pop_total_tokens_current: mismatch"
  text = harness.text(capsys.readouterr())
  for secret in ("7777701", "8888802", "pbi-secret-token"):
    assert secret not in text


def test_live_main_receipt_never_contains_dimension_labels(
    tmp_path, monkeypatch, capsys
):
  chart = _chart("usage-top-5-users-by-session")
  value_col = chart["measure_info"][0]["oracle_column"]
  dim_col = chart["category"].replace("events_v1.", "")
  harness = _LiveHarness(tmp_path, monkeypatch, charts=[chart])
  harness.oracle = lambda *_: [{dim_col: SENTINEL, value_col: 10}]
  harness.dax = lambda _: _dax_response(
      [{f"events_v1[{dim_col}]": "other_" + SENTINEL, "[v]": 10}]
  )
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert (
      receipt["results"][0]["error"] == "top-n mismatch: member not in oracle"
  )
  assert SENTINEL not in harness.text(capsys.readouterr())


def _raise_with_sentinel(*_):
  raise RuntimeError(f"upstream said {SENTINEL}")


@pytest.mark.parametrize(
    "stub, expected",
    [
        ("oracle", "oracle_query_failed"),
        ("dax", "dax_request_failed"),
    ],
)
def test_live_main_upstream_exceptions_are_reported_as_codes(
    tmp_path, monkeypatch, capsys, stub, expected
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  setattr(harness, stub, _raise_with_sentinel)
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert receipt["results"][0]["error"] == expected
  assert SENTINEL not in harness.text(capsys.readouterr())


def test_live_main_dax_error_envelope_text_never_reaches_the_receipt(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  harness.dax = lambda _: {"error": {"code": "X", "message": SENTINEL}}
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert receipt["results"][0]["error"] == "dax_response_error"
  assert SENTINEL not in harness.text(capsys.readouterr())


@pytest.mark.parametrize("bad_window", _BAD_WINDOWS, ids=repr)
def test_live_main_rejects_invalid_windows_before_any_query(
    tmp_path, monkeypatch, bad_window
):
  harness = _LiveHarness(tmp_path, monkeypatch)
  with pytest.raises(ValueError, match="invalid"):
    harness.run("--window", bad_window)
  assert harness.oracle_calls == []
  assert harness.dax_queries == []
  assert harness.window_lookups == 0


def test_live_main_invalid_window_is_rejected_before_the_date_lookup(
    tmp_path, monkeypatch
):
  harness = _LiveHarness(tmp_path, monkeypatch)
  with pytest.raises(ValueError, match="invalid"):
    harness.run("--window", "default", "--window", "2026-02-30:2026-03-01")
  assert harness.window_lookups == 0


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--workspace-id", GUID_A + "\n"),
        ("--dataset-id", GUID_B + "\n"),
        ("--report-id", GUID_C + "\n"),
        ("--project", PBI_PROJECT + "\n"),
        ("--billing-project", PBI_PROJECT + "\n"),
        ("--dataset", PBI_DATASET + "\n"),
        ("--table", "agent_events\n"),
        ("--location", "US\n"),
    ],
)
def test_live_main_rejects_arguments_with_a_trailing_newline(
    tmp_path, monkeypatch, flag, value
):
  harness = _LiveHarness(tmp_path, monkeypatch)
  with pytest.raises(ValueError, match="invalid"):
    harness.run("--window", WINDOW, flag, value)
  assert harness.oracle_calls == []
  assert harness.dax_queries == []


def test_live_main_default_window_comes_from_the_date_table(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  harness.date_window = lambda: _date_row(
      "2026-08-01T00:00:00", "2026-10-29T00:00:00"
  )
  rc, receipt = harness.run()
  assert rc == 0
  assert harness.window_lookups == 1
  assert {call[1:3] for call in harness.oracle_calls} == {
      ("2026-08-01", "2026-10-29")
  }
  assert "DATE(2026, 08, 01)" in harness.dax_queries[0]
  assert "DATE(2026, 10, 29)" in harness.dax_queries[0]
  detail = receipt["window_details"][0]
  assert (detail["window"], detail["start"], detail["end"]) == (
      "default",
      "2026-08-01",
      "2026-10-29",
  )
  assert detail["source"] == "date_table"
  assert "warning" not in capsys.readouterr().err


def test_live_main_default_window_fails_closed_without_allow_fixture_fallback(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  harness.date_window = _no_date_table
  rc, receipt = harness.run("--window", "default")
  assert rc == 1
  assert receipt["overall_pass"] is False
  assert receipt["window_details"][0]["source"] == "lookup_failed"
  err = capsys.readouterr().err
  assert "default window lookup failed" in err


def test_live_main_default_window_falls_back_with_allow_fixture_fallback(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  harness.date_window = _no_date_table
  rc, receipt = harness.run("--window", "default", "--allow-fixture-fallback")
  assert rc == 0
  detail = receipt["window_details"][0]
  assert (detail["start"], detail["end"]) == validate_mod.FIXTURE_WINDOW
  assert detail["source"] == "fixture_fallback"
  assert "fixture window" in capsys.readouterr().err


def test_live_main_fixture_window_uses_the_fixture_dates(
    tmp_path, monkeypatch, capsys
):
  harness = _tokens_card_harness(tmp_path, monkeypatch)
  harness.date_window = _no_date_table
  rc, receipt = harness.run("--window", "fixture")
  assert rc == 0
  detail = receipt["window_details"][0]
  assert (detail["start"], detail["end"]) == validate_mod.FIXTURE_WINDOW
  assert detail["source"] == "fixture"
  assert detail["window"] == "fixture"


def test_compare_chart_card_requires_exactly_one_oracle_row_and_int_exactness():
  p50_chart = _chart("performance-p50-llm-latency")
  is_match, _, err = validate_mod._compare_chart(
      p50_chart, [], [], [{"[v0]": None}]
  )
  assert is_match is False
  assert err == "oracle_row: mismatch"

  # Under allow_empty=True, percentile window queries returning 0 rows compare
  # as NULL against DAX BLANK().
  assert validate_mod._compare_chart(
      p50_chart, [], [], [{"[v0]": None}], allow_empty=True
  ) == (True, False, None)
  assert validate_mod._compare_chart(
      p50_chart, [], [], [{"[v0]": 0}], allow_empty=True
  ) == (False, False, "p50_llm_latency: mismatch")

  tokens_chart = _chart("usage-total-tokens")
  is_match_multi, _, err_multi = validate_mod._compare_chart(
      tokens_chart,
      [{"total_tokens": 10}, {"total_tokens": 20}],
      [],
      [{"[v0]": 10}],
  )
  assert is_match_multi is False
  assert err_multi == "oracle_row: mismatch"

  assert not validate_mod.values_match(
      1000000000.0, 1000000000.1, metric_type="int"
  )
  assert not validate_mod.values_match(10.5, 10.5, metric_type="int")
  assert validate_mod.values_match(
      1000000000.0, 1000000000.0, metric_type="int"
  )


# ---------------------------------------------------------------------------
# Live validator: a malformed or repeated series row fails closed
# ---------------------------------------------------------------------------

_DAY = "2026-07-01"
_NEXT_DAY = "2026-07-02"
_TOKENS_OVER_TIME = "usage-total-tokens-consumption-over-the-time"
_TOOL_CALLS = "usage-tool-calls-over-time"
_LLM_AGENTS = "usage-top-5-agents-by-llm-calls"
_NON_OBJECT_ROWS = ["garbage-row", None, 7, [_DAY, 5]]


def _series(oracle, dax, has_series=False):
  return validate_mod.compare_series(
      oracle, dax, "v", "timestamp_date", has_series=has_series
  )


def _day_row(day, value, key="[Date]"):
  return {key: f"{day}T00:00:00", "[v]": value}


def _tool_day_row(day, tool, value):
  return {
      "[Date]": f"{day}T00:00:00",
      "events_v1[tool_completed_name]": tool,
      "[v]": value,
  }


@pytest.mark.parametrize(
    "dax",
    [
        pytest.param(
            [_day_row(_DAY, 999), _day_row(_DAY, 5)], id="wrong-then-right"
        ),
        pytest.param(
            [_day_row(_DAY, 5), _day_row(_DAY, 999)], id="right-then-wrong"
        ),
        pytest.param([_day_row(_DAY, 5), _day_row(_DAY, 5)], id="identical"),
        pytest.param(
            [_day_row(_DAY, None), _day_row(_DAY, 5)], id="blank-then-right"
        ),
        pytest.param(
            [_day_row(_DAY, 5), _day_row(_DAY, None)], id="right-then-blank"
        ),
        pytest.param(
            [_day_row(_DAY, 5, key="Date[Date]"), {"[Date]": _DAY, "[v]": 5}],
            id="one-day-two-spellings",
        ),
    ],
)
def test_compare_series_rejects_a_repeated_dax_date(dax):
  oracle = [{"timestamp_date": _DAY, "v": 5}]
  assert _series(oracle, dax) == (False, "duplicate_axis_key", None, None)


def test_compare_series_one_row_per_date_matches_in_any_order():
  oracle = [
      {"timestamp_date": _DAY, "v": 5},
      {"timestamp_date": _NEXT_DAY, "v": 7},
  ]
  dax = [_day_row(_DAY, 5), _day_row(_NEXT_DAY, 7)]
  assert _series(oracle, dax) == (True, None, None, None)
  assert _series(oracle[::-1], dax[::-1]) == (True, None, None, None)


@pytest.mark.parametrize("first_value", [999, 5, None])
def test_compare_series_rejects_a_repeated_oracle_date(first_value):
  oracle = [
      {"timestamp_date": _DAY, "v": first_value},
      {"timestamp_date": _DAY, "v": 5},
  ]
  assert _series(oracle, [_day_row(_DAY, 5)]) == (
      False,
      "duplicate_axis_key",
      None,
      None,
  )


def test_compare_series_repeat_check_keys_on_the_date_and_tool_pair():
  oracle = [
      {"timestamp_date": _DAY, "tool_name": "search", "v": 4},
      {"timestamp_date": _DAY, "tool_name": "fetch", "v": 2},
  ]
  two_tools = [
      _tool_day_row(_DAY, "search", 4),
      _tool_day_row(_DAY, "fetch", 2),
  ]
  # Two tools on one day are two groups, not a repeat.
  assert _series(oracle, two_tools, True) == (True, None, None, None)
  for repeated in (
      [_tool_day_row(_DAY, "search", 4), _tool_day_row(_DAY, "search", 2)],
      [_tool_day_row(_DAY, "search", 2), _tool_day_row(_DAY, "search", 4)],
  ):
    assert _series(oracle, two_tools[1:] + repeated, True) == (
        False,
        "duplicate_axis_key",
        None,
        None,
    )
  assert _series(oracle + oracle[:1], two_tools, True) == (
      False,
      "duplicate_axis_key",
      None,
      None,
  )


def test_compare_series_null_and_empty_tools_are_distinct_groups():
  oracle = [
      {"timestamp_date": _DAY, "tool_name": None, "v": 4},
      {"timestamp_date": _DAY, "tool_name": "", "v": 2},
  ]
  dax = [_tool_day_row(_DAY, None, 4), _tool_day_row(_DAY, "", 2)]
  assert _series(oracle, dax, True) == (True, None, None, None)
  swapped = [_tool_day_row(_DAY, None, 2), _tool_day_row(_DAY, "", 4)]
  assert _series(oracle, swapped, True)[0] is False


@pytest.mark.parametrize("garbage", _NON_OBJECT_ROWS, ids=repr)
def test_compare_series_rejects_a_non_object_dax_row_anywhere(garbage):
  oracle = [{"timestamp_date": _DAY, "v": 5}]
  valid = _day_row(_DAY, 5)
  assert _series(oracle, [valid]) == (True, None, None, None)
  for dax in ([valid, garbage], [garbage, valid], [garbage]):
    assert _series(oracle, dax) == (False, "dax_row_type", None, None)


@pytest.mark.parametrize("garbage", _NON_OBJECT_ROWS, ids=repr)
def test_compare_series_rejects_a_non_object_oracle_row_anywhere(garbage):
  valid = {"timestamp_date": _DAY, "v": 5}
  dax = [_day_row(_DAY, 5)]
  for oracle in ([valid, garbage], [garbage, valid], [garbage]):
    assert _series(oracle, dax) == (False, "oracle_row_type", None, None)


@pytest.mark.parametrize(
    "row",
    [{"v": 5}, {"timestamp_date": _DAY}, {"timestamp": _DAY, "v": 5}, {}],
    ids=repr,
)
def test_compare_series_rejects_an_oracle_row_without_its_date_or_metric(row):
  assert _series([row], [_day_row(_DAY, 5)]) == (
      False,
      "oracle_missing_key",
      None,
      None,
  )
  # Control: an explicit NULL metric is present and compares as a BLANK.
  assert _series(
      [{"timestamp_date": _DAY, "v": None}], [_day_row(_DAY, None)]
  ) == (True, None, None, None)


def test_compare_series_rejects_a_dax_row_without_its_metric():
  oracle = [{"timestamp_date": _DAY, "v": None}]
  # The row names the day but carries no value column at all ...
  assert _series(oracle, [{"[Date]": f"{_DAY}T00:00:00"}]) == (
      False,
      "dax_missing_metric",
      None,
      None,
  )
  # ... whereas an explicit BLANK value is a real NULL-versus-BLANK match.
  assert _series(oracle, [_day_row(_DAY, None)]) == (True, None, None, None)
  assert _series(oracle, [{"Date[Date]": f"{_DAY}T00:00:00", "v": None}]) == (
      True,
      None,
      None,
      None,
  )


@pytest.mark.parametrize(
    "row", [{"[v]": 5}, {"v": 5, "events_v1[agent]": "a"}], ids=repr
)
def test_compare_series_rejects_a_dax_row_without_a_date(row):
  oracle = [{"timestamp_date": _DAY, "v": 5}]
  assert _series(oracle, [row]) == (False, "dax_missing_axis", None, None)


@pytest.mark.parametrize(
    "oracle_value, dax_value, expected",
    [
        (None, None, True),
        ("", "", True),
        ("", None, True),
        (None, "", True),
        (0, 0, True),
        (None, 0, False),
        (0, None, False),
    ],
)
def test_compare_series_blank_matches_only_blank(
    oracle_value, dax_value, expected
):
  oracle = [{"timestamp_date": _DAY, "v": oracle_value}]
  assert _series(oracle, [_day_row(_DAY, dax_value)])[0] is expected


def _one_chart_harness(tmp_path, monkeypatch, chart, oracle_rows, dax_rows):
  harness = _LiveHarness(tmp_path, monkeypatch, charts=[chart])
  harness.oracle = lambda *_: oracle_rows
  harness.dax = lambda _: _dax_response(dax_rows)
  return harness


@pytest.mark.parametrize(
    "oracle_value, dax_rows, error",
    [
        (5, [_day_row(_DAY, 999), _day_row(_DAY, 5)], "duplicate_axis_key"),
        (5, [_day_row(_DAY, 5), _day_row(_DAY, 5)], "duplicate_axis_key"),
        (5, [_day_row(_DAY, 5), "garbage-row"], "dax_row_type"),
        (None, [{"[Date]": f"{_DAY}T00:00:00"}], "dax_missing_metric"),
        (5, [{"[v]": 5}], "dax_missing_axis"),
    ],
    ids=["wrong-then-right", "identical", "garbage", "no-metric", "no-date"],
)
def test_live_main_fails_a_daily_chart_with_malformed_dax_rows(
    tmp_path, monkeypatch, oracle_value, dax_rows, error
):
  chart = _chart(_TOKENS_OVER_TIME)
  value_col = chart["measure_info"][0]["oracle_column"]
  oracle_rows = [{"timestamp_date": _DAY, value_col: oracle_value}]
  harness = _one_chart_harness(
      tmp_path, monkeypatch, chart, oracle_rows, dax_rows
  )
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert receipt["overall_pass"] is False
  result = receipt["results"][0]
  assert (result["status"], result["error"]) == (
      "FAIL",
      f"series mismatch at {error}",
  )
  assert (result["oracle_row_count"], result["dax_row_count"]) == (
      1,
      len(dax_rows),
  )


def test_live_main_passes_a_daily_chart_with_one_row_per_date(
    tmp_path, monkeypatch
):
  chart = _chart(_TOKENS_OVER_TIME)
  value_col = chart["measure_info"][0]["oracle_column"]
  oracle_rows = [
      {"timestamp_date": _DAY, value_col: 5},
      {"timestamp_date": _NEXT_DAY, value_col: None},
  ]
  dax_rows = [_day_row(_DAY, 5), _day_row(_NEXT_DAY, None)]
  harness = _one_chart_harness(
      tmp_path, monkeypatch, chart, oracle_rows, dax_rows
  )
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 0
  assert receipt["results"][0]["status"] == "PASS"


def test_live_main_fails_the_tool_calls_chart_on_a_non_object_row(
    tmp_path, monkeypatch
):
  chart = _chart(_TOOL_CALLS)
  value_col = chart["measure_info"][0]["oracle_column"]
  oracle_rows = [{"timestamp_date": _DAY, "tool_name": "search", value_col: 4}]
  valid = [_tool_day_row(_DAY, "search", 4)]
  harness = _one_chart_harness(tmp_path, monkeypatch, chart, oracle_rows, valid)
  rc, receipt = harness.run("--window", WINDOW)
  assert (rc, receipt["results"][0]["status"]) == (0, "PASS")
  harness.dax = lambda _: _dax_response(valid + ["garbage-row"])
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert receipt["results"][0]["error"] == "series mismatch at dax_row_type"


# The charts that take the tie-aware Top-N path in _run_chart.
_TOPN_CHART_IDS = sorted(
    c["id"]
    for c in validate_mod.load_chart_metadata()
    if c["visual_type"] == "clusteredBarChart"
    and c.get("top_n") is not None
    and c["top_n"] <= 10
)


def test_twelve_bar_charts_are_compared_as_top_n():
  assert len(_TOPN_CHART_IDS) == 12


@pytest.mark.parametrize("chart_id", _TOPN_CHART_IDS)
def test_live_main_fails_a_top_n_chart_with_malformed_rows(
    tmp_path, monkeypatch, chart_id
):
  chart = _chart(chart_id)
  value_col = chart["measure_info"][0]["oracle_column"]
  dim_col = chart["category"].replace("events_v1.", "")
  oracle_dim = (
      "tool_name"
      if dim_col in ("tool_completed_name", "tool_error_name")
      else dim_col
  )
  dax_dim = f"events_v1[{dim_col}]"
  oracle_rows = [
      {oracle_dim: "m1", value_col: 7},
      {oracle_dim: "m2", value_col: 5},
  ]
  dax_rows = [{dax_dim: "m1", "[v]": 7}, {dax_dim: "m2", "[v]": 5}]
  harness = _one_chart_harness(
      tmp_path, monkeypatch, chart, oracle_rows, dax_rows
  )
  rc, receipt = harness.run("--window", WINDOW)
  assert (rc, receipt["results"][0]["status"]) == (0, "PASS")
  # The second, uncapped oracle query is the tie-aware Top-N comparison.
  assert [call[3] for call in harness.oracle_calls] == [False, True]

  # A well-formed blank category on both sides is omitted and still passes.
  harness.oracle = lambda *_: oracle_rows + [
      {oracle_dim: "m3", value_col: None}
  ]
  harness.dax = lambda _: _dax_response(
      dax_rows + [{dax_dim: "m3", "[v]": None}]
  )
  rc, receipt = harness.run("--window", WINDOW)
  assert (rc, receipt["results"][0]["status"]) == (0, "PASS")

  malformed = [
      (oracle_rows, dax_rows + ["garbage-row"], "dax_row_type"),
      (oracle_rows, dax_rows + [{dax_dim: "m3"}], "dax_missing_metric"),
      (oracle_rows, dax_rows + [{"[v]": None}], "missing_dimension"),
      (
          oracle_rows,
          dax_rows + [{dax_dim: "m1", "[v]": None}],
          "duplicate_member",
      ),
      (oracle_rows + ["garbage-row"], dax_rows, "oracle_row_type"),
      (oracle_rows + [{oracle_dim: "m3"}], dax_rows, "oracle_missing_metric"),
      (oracle_rows + [{value_col: None}], dax_rows, "missing_dimension"),
      (
          oracle_rows + [{oracle_dim: "m1", value_col: None}],
          dax_rows,
          "duplicate_member",
      ),
  ]
  for bad_oracle, bad_dax, code in malformed:
    harness.oracle = lambda *_, rows=bad_oracle: rows
    harness.dax = lambda _, rows=bad_dax: _dax_response(rows)
    rc, receipt = harness.run("--window", WINDOW)
    result = receipt["results"][0]
    assert (rc, receipt["overall_pass"]) == (1, False), code
    assert (result["status"], result["error"]) == (
        "FAIL",
        f"top-n mismatch: {code}",
    )


# ---------------------------------------------------------------------------
# Live validator: Tool Calls Over Time compares every date/tool group, past
# the source's 5,000-row LIMIT (README known difference D13)
# ---------------------------------------------------------------------------


def _tool_day_oracle(tools, days, value_col):
  """Rows in the source's order: tool_name, then timestamp_date DESC."""
  first_day = datetime.date(2026, 4, 18)
  return [
      {
          "tool_name": f"tool_{tool:02d}",
          "timestamp_date": (
              first_day + datetime.timedelta(days=offset)
          ).isoformat(),
          value_col: tool + offset + 1,
      }
      for tool in range(tools)
      for offset in reversed(range(days))
  ]


def _tool_day_dax(oracle_rows, value_col):
  return [
      _tool_day_row(row["timestamp_date"], row["tool_name"], row[value_col])
      for row in oracle_rows
  ]


def test_only_the_llm_trend_and_the_series_chart_run_their_oracle_uncapped():
  charts = validate_mod.load_chart_metadata()
  assert {c["id"] for c in charts if validate_mod.oracle_runs_uncapped(c)} == {
      "usage-llm-call-trends",
      _TOOL_CALLS,
  }


def test_tool_calls_oracle_loses_only_its_final_limit():
  chart = _chart(_TOOL_CALLS)
  sql = (validate_mod.LOOKER_ROOT / chart["oracle_query"]).read_text()
  assert sql.rstrip().splitlines()[-1] == "LIMIT 5000"
  uncapped = validate_mod.LIMIT_RE.sub("", sql)
  assert uncapped == sql[: sql.rindex("\nLIMIT 5000")]
  assert uncapped.splitlines()[-1] == "ORDER BY tool_name, timestamp_date DESC"


def test_build_dax_query_tool_calls_chart_reads_every_group():
  chart = _chart(_TOOL_CALLS)
  assert validate_mod.build_dax_query(chart, "2026-04-18", "2026-07-16") == (
      "EVALUATE SUMMARIZECOLUMNS('Date'[Date], "
      "'events_v1'[tool_completed_name], "
      "FILTER(ALL('Date'), 'Date'[Window] = \"Current\" "
      "&& 'Date'[Date] >= DATE(2026, 04, 18) "
      "&& 'Date'[Date] <= DATE(2026, 07, 16)), "
      '"v", [Tool Completion Events])\n'
  )


def test_live_main_compares_all_5040_tool_day_groups(tmp_path, monkeypatch):
  chart = _chart(_TOOL_CALLS)
  value_col = chart["measure_info"][0]["oracle_column"]
  rows = _tool_day_oracle(56, 90, value_col)
  assert len(rows) == 5040
  harness = _LiveHarness(tmp_path, monkeypatch, charts=[chart])
  # The stub honours the LIMIT: a capped run returns the first 5,000 rows.
  harness.oracle = lambda name, start, end, remove_limit: (
      rows if remove_limit else rows[:5000]
  )
  harness.dax = lambda _: _dax_response(_tool_day_dax(rows, value_col))
  rc, receipt = harness.run("--window", WINDOW)
  assert harness.oracle_calls == [
      ("usage-tool-calls-over-time.sql", "2026-04-18", "2026-07-16", True)
  ]
  assert rc == 0
  result = receipt["results"][0]
  assert (
      result["status"],
      result["oracle_row_count"],
      result["dax_row_count"],
  ) == ("PASS", 5040, 5040)


def test_compare_chart_tool_calls_needs_every_group_on_both_sides():
  chart = _chart(_TOOL_CALLS)
  value_col = chart["measure_info"][0]["oracle_column"]
  rows = _tool_day_oracle(56, 90, value_col)
  dax = _tool_day_dax(rows, value_col)
  assert validate_mod._compare_chart(chart, rows, None, dax) == (
      True,
      False,
      None,
  )
  # The source's first 5,000 rows end inside tool_55, so its 40 oldest days
  # are missing and a LIMIT-capped oracle never passes against the model.
  assert {row["tool_name"] for row in rows[5000:]} == {"tool_55"}
  ok, _, error = validate_mod._compare_chart(chart, rows[:5000], None, dax)
  assert ok is False
  assert error.startswith("series mismatch at row_index=")
  # A model that drops those 40 groups fails the same way ...
  ok, _, error = validate_mod._compare_chart(chart, rows, None, dax[:5000])
  assert ok is False
  assert error.startswith("series mismatch at row_index=")
  # ... and so does a model missing one group of a middle tool.
  one_missing = [
      row
      for row in dax
      if (row["events_v1[tool_completed_name]"], row["[Date]"])
      != ("tool_30", "2026-04-18T00:00:00")
  ]
  assert len(one_missing) == 5039
  assert validate_mod._compare_chart(chart, rows, None, one_missing)[0] is False


def test_compare_chart_tool_calls_below_the_limit_matches_in_any_order():
  chart = _chart(_TOOL_CALLS)
  value_col = chart["measure_info"][0]["oracle_column"]
  rows = _tool_day_oracle(10, 90, value_col)
  dax = _tool_day_dax(rows, value_col)
  assert len(rows) == 900
  for oracle_rows, dax_rows in ((rows, dax), (rows[::-1], dax)):
    assert validate_mod._compare_chart(chart, oracle_rows, None, dax_rows) == (
        True,
        False,
        None,
    )


# ---------------------------------------------------------------------------
# Live validator: Top 5 Agents by LLM Calls draws five bars over a 500-row
# source (README known difference D14)
# ---------------------------------------------------------------------------


def _agent_rows(values):
  return [
      {"agent": f"agent_{i:03d}", "total_llm_calls": value}
      for i, value in enumerate(values)
  ]


def _agent_dax(oracle_rows):
  return [
      {"events_v1[agent]": row["agent"], "[v]": row["total_llm_calls"]}
      for row in oracle_rows
  ]


def test_top5_llm_agents_six_agents_compare_as_the_top_five():
  chart = _chart(_LLM_AGENTS)
  assert chart["top_n"] == 5
  oracle = _agent_rows([600, 590, 580, 570, 560, 550])
  dax = _agent_dax(oracle)
  assert validate_mod._compare_chart(chart, oracle, oracle, dax[:5]) == (
      True,
      False,
      None,
  )
  assert validate_mod._compare_chart(chart, oracle, oracle, dax) == (
      False,
      False,
      "top-n mismatch: row_count",
  )
  assert validate_mod._compare_chart(chart, oracle, oracle, dax[1:]) == (
      False,
      False,
      "top-n mismatch: value[0]",
  )


def test_top5_llm_agents_tie_for_fifth_in_a_501_agent_source():
  chart = _chart(_LLM_AGENTS)
  # Four leaders, then agent_004 and agent_005 tied for fifth place.
  oracle = _agent_rows(
      [1000, 900, 800, 700, 600, 600] + [500 - i for i in range(495)]
  )
  assert len(oracle) == 501
  top5 = _agent_dax(oracle[:5])
  tie_pick = top5[:4] + _agent_dax(oracle[5:6])
  for topn_rows in (oracle, oracle[:500]):
    assert validate_mod._compare_chart(
        chart, oracle[:500], topn_rows, top5
    ) == (True, False, None)
    assert validate_mod._compare_chart(
        chart, oracle[:500], topn_rows, tie_pick
    ) == (True, True, None)
  # An agent ranked below fifth never passes, whatever value the model shows.
  for intruder_value in (6, 600):
    intruder = top5[:4] + [
        {"events_v1[agent]": "agent_500", "[v]": intruder_value}
    ]
    ok, _, error = validate_mod._compare_chart(
        chart, oracle[:500], oracle, intruder
    )
    assert ok is False, intruder_value
    assert error.startswith("top-n mismatch: "), error
  unknown = top5[:4] + [{"events_v1[agent]": "agent_999", "[v]": 600}]
  assert validate_mod._compare_chart(chart, oracle[:500], oracle, unknown) == (
      False,
      False,
      "top-n mismatch: member not in oracle",
  )


# ---------------------------------------------------------------------------
# Live validator: comparison types come from the spec's explicit
# comparison_type, never from a display format
# ---------------------------------------------------------------------------

_FLOAT_COMPARISON_MEASURES = frozenset(
    {
        "Avg Tool Latency (ms)",
        "Avg LLM Latency (ms)",
        "P50 Tool Latency (ms)",
        "P75 Tool Latency (ms)",
        "P90 Tool Latency (ms)",
        "P99 Tool Latency (ms)",
        "P50 LLM Latency (ms)",
        "P75 LLM Latency (ms)",
        "P90 LLM Latency (ms)",
        "P99 LLM Latency (ms)",
        "KPI Total Tokens Change %",
        "KPI Total Traces Change %",
        "KPI Total Sessions Change %",
        "KPI Total LLM Calls Change %",
        "KPI Total Users Change %",
        "KPI Tool Errors Change %",
    }
)


def test_chart_metadata_comparison_types_are_the_pinned_set():
  types = {}
  for chart in validate_mod.load_chart_metadata():
    for info in chart["measure_info"]:
      types[info["name"]] = info["comparison_type"]
  assert set(types.values()) == {"int", "float"}
  assert {n for n, t in types.items() if t == "float"} == (
      _FLOAT_COMPARISON_MEASURES
  )
  for chart_id in KPI_CARD_PREFIXES:
    value, change = _chart(chart_id)["measure_info"]
    assert (value["comparison_type"], change["comparison_type"]) == (
        "int",
        "float",
    ), chart_id


def test_measure_comparison_type_ignores_the_display_format():
  assert (
      validate_mod._measure_comparison_type(
          {"name": "m", "format_string": "#,0.0", "comparison_type": "int"}
      )
      == "int"
  )
  assert (
      validate_mod._measure_comparison_type(
          {"name": "m", "format_string": "#,0", "comparison_type": "float"}
      )
      == "float"
  )


@pytest.mark.parametrize(
    "measure",
    [
        {},
        {"name": "Total Tokens", "format_string": "#,0.0"},
        {"name": "Total Tokens", "comparison_type": "exact"},
        {"name": "Total Tokens", "comparison_type": None},
        {"name": "Total Tokens", "comparison_type": "INT"},
        "int",
        None,
    ],
    ids=repr,
)
def test_measure_comparison_type_fails_closed_without_a_valid_type(measure):
  with pytest.raises(ValueError, match="has no valid comparison_type"):
    validate_mod._measure_comparison_type(measure)


def _spec_copy(tmp_path, monkeypatch):
  """Copies the Power BI spec files and points the live tool at the copy."""
  source = validate_mod.ROOT / "spec"
  spec_dir = tmp_path / "power_bi" / "spec"
  spec_dir.mkdir(parents=True)
  for name in ("report_visuals.yaml", "semantic_model.yaml"):
    (spec_dir / name).write_text((source / name).read_text())
  monkeypatch.setattr(validate_mod, "ROOT", spec_dir.parent)
  return spec_dir


def test_load_chart_metadata_refuses_a_measure_without_a_comparison_type(
    tmp_path, monkeypatch
):
  spec_dir = _spec_copy(tmp_path, monkeypatch)
  assert len(validate_mod.load_chart_metadata()) == 37
  model_path = spec_dir / "semantic_model.yaml"
  model = yaml.safe_load(model_path.read_text())
  measure = next(
      m for m in model["measures"] if m["name"] == "KPI Total Tokens"
  )
  del measure["comparison_type"]
  model_path.write_text(yaml.safe_dump(model, sort_keys=False))
  with pytest.raises(
      ValueError, match="'KPI Total Tokens' has no valid comparison_type"
  ):
    validate_mod.load_chart_metadata()


def test_load_chart_metadata_refuses_a_chart_bound_to_an_unknown_measure(
    tmp_path, monkeypatch
):
  spec_dir = _spec_copy(tmp_path, monkeypatch)
  visuals_path = spec_dir / "report_visuals.yaml"
  visuals = yaml.safe_load(visuals_path.read_text())
  tokens = next(
      v for v in visuals["parity_visuals"] if v["id"] == "usage-total-tokens"
  )
  tokens["measures"][0] = "KPI Total Tokenz"
  visuals_path.write_text(yaml.safe_dump(visuals, sort_keys=False))
  with pytest.raises(ValueError, match="binds unknown measure"):
    validate_mod.load_chart_metadata()


def test_a_count_card_is_compared_exactly_whatever_its_format():
  chart = _chart("usage-total-tokens")
  # A display-format edit must not loosen the exact count comparison.
  chart["measure_info"][0]["format_string"] = "#,0.0"
  cols = [m["oracle_column"] for m in chart["measure_info"]]
  oracle_row = {cols[0]: 1000000000, cols[1]: 0.125}
  assert validate_mod._compare_chart(
      chart, [oracle_row], [], [{"[v0]": 1000000001, "[v1]": 0.125}]
  ) == (False, False, f"{cols[0]}: mismatch")
  # The Change % ratio keeps its relative tolerance.
  assert validate_mod._compare_chart(
      chart, [oracle_row], [], [{"[v0]": 1000000000.0, "[v1]": 0.125 + 1e-12}]
  ) == (True, False, None)


# ---------------------------------------------------------------------------
# Live validator: EMPTY applies only to grouped visuals; a card never is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chart_id, error",
    [
        ("usage-total-tokens", "oracle_row: mismatch"),
        ("performance-p50-llm-latency", "dax_row_count: mismatch"),
    ],
)
def test_live_main_an_empty_card_fails_even_with_allow_empty(
    tmp_path, monkeypatch, chart_id, error
):
  harness = _one_chart_harness(tmp_path, monkeypatch, _chart(chart_id), [], [])
  rc, receipt = harness.run("--window", WINDOW, "--allow-empty")
  assert rc == 1
  result = receipt["results"][0]
  assert (result["status"], result["error"]) == ("FAIL", error)
  assert (result["oracle_row_count"], result["dax_row_count"]) == (0, 0)
  assert (receipt["charts_fail"], receipt["charts_empty"]) == (1, 0)


@pytest.mark.parametrize(
    "chart_id", [_TOKENS_OVER_TIME, "usage-top-5-users-by-session"]
)
def test_live_main_an_empty_grouped_chart_is_empty_only_when_allowed(
    tmp_path, monkeypatch, chart_id
):
  harness = _one_chart_harness(tmp_path, monkeypatch, _chart(chart_id), [], [])
  rc, receipt = harness.run("--window", WINDOW, "--allow-empty")
  assert rc == 0
  result = receipt["results"][0]
  assert (result["status"], result["error"]) == ("EMPTY", "empty_result")
  assert (receipt["charts_empty"], receipt["overall_pass"]) == (1, True)
  rc, receipt = harness.run("--window", WINDOW)
  assert rc == 1
  assert receipt["results"][0]["status"] == "EMPTY"
  assert receipt["overall_pass"] is False


def test_allow_empty_help_says_a_card_is_never_empty(capsys):
  with pytest.raises(SystemExit) as exit_info:
    validate_mod.main(["--help"])
  assert exit_info.value.code == 0
  text = " ".join(capsys.readouterr().out.split())
  assert (
      "accept grouped visuals (area/bar charts) that are empty on both sides"
      in text
  )
  assert (
      "a cardVisual must always return exactly one row and is never EMPTY"
      in text
  )
