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

"""Unit tests for the BQAA Power BI project generator core (S1a) and semantic model (S1b)."""

import copy
import datetime
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import sys
from types import ModuleType
import uuid

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POWER_BI = ROOT / "dashboards" / "power_bi"
LOOKER = ROOT / "dashboards" / "looker_studio"


def _load_pbi_module(name: str) -> ModuleType:
  mod_name = f"power_bi_{name}"
  if mod_name in sys.modules:
    return sys.modules[mod_name]
  module_path = POWER_BI / "tools" / f"{name}.py"
  spec = importlib.util.spec_from_file_location(mod_name, module_path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[mod_name] = module
  spec.loader.exec_module(module)
  return module


def test_pbip_common_constants_and_interfaces():
  pbip_common = _load_pbi_module("pbip_common")
  assert Path(pbip_common.REPO_ROOT) == ROOT
  assert Path(pbip_common.POWER_BI_ROOT) == POWER_BI
  assert Path(pbip_common.LOOKER_ROOT) == LOOKER
  assert (
      Path(pbip_common.MODEL_DIR)
      == POWER_BI / "BQAA_Agent_Analytics.SemanticModel"
  )
  assert (
      Path(pbip_common.REPORT_DIR) == POWER_BI / "BQAA_Agent_Analytics.Report"
  )
  assert pbip_common.DISPLAY_NAME == "BQAA Agent Analytics"
  assert set(pbip_common.HEADER_RECTS) == {
      "page-heading",
      "slicer-date",
      "slicer-agent",
      "slicer-user-id",
      "slicer-trace-id",
      "slicer-span-id",
  }


def test_stable_uuid_deterministic_and_forbids_uuid4(monkeypatch):
  pbip_common = _load_pbi_module("pbip_common")

  def _forbid_uuid4(*_args, **_kwargs):
    raise AssertionError("uuid.uuid4 must never be called")

  monkeypatch.setattr(uuid, "uuid4", _forbid_uuid4)
  u1 = pbip_common.stable_uuid(
      "platform", "SemanticModel", "BQAA_Agent_Analytics"
  )
  u2 = pbip_common.stable_uuid(
      "platform", "SemanticModel", "BQAA_Agent_Analytics"
  )
  u3 = pbip_common.stable_uuid("platform", "Report", "BQAA_Agent_Analytics")
  assert u1 == u2
  assert u1 != u3
  parsed = uuid.UUID(u1)
  assert parsed.version == 5


def test_dump_json_and_literal_escaping():
  pbip_common = _load_pbi_module("pbip_common")
  dumped = pbip_common.dump_json(
      {"version": "1.0", "$schema": "https://example.com/s.json"}
  )
  assert dumped.endswith("\n")
  lines = dumped.splitlines()
  assert '"$schema": "https://example.com/s.json"' in lines[1]

  lit = pbip_common.literal("Agent's ' Latency")
  assert lit == {"expr": {"Literal": {"Value": "'Agent''s '' Latency'"}}}


def test_canvas_rect_and_page_height_formulas():
  pbip_common = _load_pbi_module("pbip_common")
  rect = pbip_common.canvas_rect(
      {"row": 0, "col": 0, "width": 24, "height": 14}
  )
  assert rect == {"x": 16, "y": 128, "width": 1240, "height": 552}
  assert pbip_common.page_height([rect]) == 720

  tall_rect = pbip_common.canvas_rect(
      {"row": 25, "col": 0, "width": 12, "height": 8}
  )
  assert tall_rect == {"x": 16, "y": 1128, "width": 616, "height": 312}
  assert pbip_common.page_height([tall_rect]) == 1464

  with pytest.raises(ValueError):
    pbip_common.canvas_rect({"row": 0, "col": 20, "width": 6, "height": 4})


def test_load_contracts_and_check_can_fail_on_mutated_manifest(tmp_path):
  pbip_common = _load_pbi_module("pbip_common")
  contracts = pbip_common.load_contracts()
  assert len(contracts.manifest["charts"]) == 37
  assert len(contracts.oracle_files) == 37
  assert "oracle/queries/usage-total-tokens.sql" in contracts.oracle_files
  assert "usage-total-tokens.sql" in contracts.oracle_files
  assert "usage-total-tokens" in contracts.oracle_files

  # Mutate a copy of the Looker contracts to prove fail-closed check (#check-can-fail).
  broken_looker = tmp_path / "looker_studio"
  shutil.copytree(LOOKER, broken_looker)
  manifest_path = broken_looker / "spec" / "chart_manifest.yaml"
  manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
  manifest_data["charts"].pop()
  manifest_path.write_text(yaml.safe_dump(manifest_data), encoding="utf-8")
  with pytest.raises(ValueError, match="Expected 37 charts"):
    pbip_common.load_contracts(looker_root=broken_looker)

  # Restore 37 charts but break F4 listener exception set.
  manifest_data = copy.deepcopy(contracts.manifest)
  for c in manifest_data["charts"]:
    c["listen"]["user_id"] = True
  manifest_path.write_text(yaml.safe_dump(manifest_data), encoding="utf-8")
  with pytest.raises(ValueError, match="F4 User ID listener"):
    pbip_common.load_contracts(looker_root=broken_looker)


def test_generator_write_determinism_and_check_drift_detection(
    tmp_path, monkeypatch
):
  pbip_common = _load_pbi_module("pbip_common")
  generate_pbip = _load_pbi_module("generate_pbip")

  def _forbid_uuid4(*_args, **_kwargs):
    raise AssertionError("uuid.uuid4 must never be called")

  monkeypatch.setattr(uuid, "uuid4", _forbid_uuid4)

  tmp_pbi = tmp_path / "power_bi"
  tmp_pbi.mkdir(parents=True)
  if (POWER_BI / "spec").exists():
    shutil.copytree(POWER_BI / "spec", tmp_pbi / "spec")

  monkeypatch.setattr(pbip_common, "POWER_BI_ROOT", tmp_pbi)

  first = generate_pbip.write_generated_files(power_bi_root=tmp_pbi)
  second = generate_pbip.write_generated_files(power_bi_root=tmp_pbi)
  assert first == second

  assert generate_pbip.main(["--check"]) == 0

  # Hand-edit a generated file and confirm --check fails (#check-can-fail).
  pbir_path = tmp_pbi / "BQAA_Agent_Analytics.Report" / "definition.pbir"
  data = json.loads(pbir_path.read_text(encoding="utf-8"))
  data["version"] = "9.9"
  pbir_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
  assert generate_pbip.main(["--check"]) == 1

  # Restore via --write and confirm --check passes again.
  assert generate_pbip.main(["--write"]) == 0
  assert generate_pbip.main(["--check"]) == 0


def test_semantic_model_invariants_i1_to_i8_and_print_sql(monkeypatch):
  pbip_common = _load_pbi_module("pbip_common")
  pbip_model = _load_pbi_module("pbip_model")
  generate_pbip = _load_pbi_module("generate_pbip")

  contracts = pbip_common.load_contracts()
  model_spec = pbip_common.load_spec("semantic_model.yaml")
  model_files = pbip_model.build_model(contracts, model_spec)

  expr_text = model_files["definition/expressions.tmdl"]
  events_tmdl = model_files["definition/tables/events_v1.tmdl"]

  # I1: reconstruct EventsSelectLines from expressions.tmdl and compare byte-for-byte.
  select_lines = pbip_model.events_select_lines(contracts)
  assert len(select_lines) == 128
  assert select_lines[0] == "SELECT"
  assert select_lines[-1] == "  ) AS tool_error_pk"

  m_block_match = re.search(
      r"EventsSelectLines = \{\n(.*?)\n\t\t\t\},", expr_text, re.DOTALL
  )
  assert m_block_match is not None
  reconstructed: list[str] = []
  for raw_line in m_block_match.group(1).splitlines():
    m_lit = re.match(r'^\t\t\t\t"(.*)"(?:,)?$', raw_line)
    assert (
        m_lit is not None
    ), f"Unexpected EventsSelectLines entry: {raw_line!r}"
    reconstructed.append(pbip_model.unescape_m_string(m_lit.group(1)))
  assert reconstructed == select_lines

  # I2: FROM line uses validated TableRef and WHERE clause is the half-open UTC predicate.
  assert '"FROM " & TableRef' in expr_text
  assert (
      '"WHERE timestamp >= TIMESTAMP(" & DateLit(LoadStart) & ", \'UTC\')"'
      in expr_text
  )
  assert (
      '"  AND timestamp < TIMESTAMP(DATE_ADD(" & DateLit(Anchor) & ", INTERVAL 1 DAY), \'UTC\')"'
      in expr_text
  )

  # I3: raw parameter names appear only in declaration line (IsParameterQuery) and validator line (Is*/Reject).
  for param_name in pbip_model.PARAMETER_NAMES:
    matching_lines = [
        line for line in expr_text.splitlines() if param_name in line
    ]
    assert (
        len(matching_lines) == 2
    ), f"Expected {param_name} on 2 lines (declaration + validator), got {matching_lines}"
    assert "IsParameterQuery=true" in matching_lines[0]
    assert f'Reject("{param_name}")' in matching_lines[1]
    assert re.search(
        r"if Is[A-Za-z]+\(" + re.escape(param_name) + r"\)", matching_lines[1]
    )
    assert param_name not in events_tmdl

  # I4 & I5: no EnableFolding and no Implementation in connector/native query calls.
  for tmdl_text in model_files.values():
    assert "EnableFolding" not in tmdl_text
    assert "Implementation" not in tmdl_text

  # I6: explicit WindowEndDate skips AutoAnchor via lazy branch.
  assert (
      'Anchor = if Text.Lower(WindowEndRaw) = "auto" then AutoAnchor else ParseIsoDate(WindowEndRaw)'
      in expr_text
  )

  # I7 & I8: --print-sql equals Python render_events_sql and TMDL composition, with 0 ;, ", #.
  buf = io.StringIO()
  monkeypatch.setattr(sys, "stdout", buf)
  rc = generate_pbip.main(
      [
          "--print-sql",
          "--window-end",
          "2026-07-16",
          "--lookback-days",
          "90",
      ]
  )
  assert rc == 0
  printed_sql = buf.getvalue().rstrip("\n")

  expected_sql = pbip_model.render_events_sql(
      "test-project-0728-467323",
      "bqaa_fixture_adk_1_27_0",
      "agent_events",
      load_start=datetime.date(2026, 1, 18),
      anchor=datetime.date(2026, 7, 16),
      contracts=contracts,
  )
  assert printed_sql == expected_sql
  assert ";" not in printed_sql
  assert '"' not in printed_sql
  assert "#" not in printed_sql
  assert printed_sql.splitlines()[:128] == select_lines
  assert printed_sql.splitlines()[128:] == [
      "FROM `test-project-0728-467323.bqaa_fixture_adk_1_27_0.agent_events`",
      "WHERE timestamp >= TIMESTAMP(DATE '2026-01-18', 'UTC')",
      "  AND timestamp < TIMESTAMP(DATE_ADD(DATE '2026-07-16', INTERVAL 1 DAY), 'UTC')",
  ]

  # F20 validation failure checks on render_events_sql (#check-can-fail).
  with pytest.raises(ValueError, match="Invalid BigQuery project ID"):
    pbip_model.render_events_sql(
        "BAD_PROJECT!",
        "bqaa_fixture_adk_1_27_0",
        "agent_events",
        datetime.date(2026, 1, 18),
        datetime.date(2026, 7, 16),
    )
  with pytest.raises(ValueError, match="Invalid BigQuery dataset ID"):
    pbip_model.render_events_sql(
        "test-project-0728-467323",
        "bad-dataset-with-hyphen",
        "agent_events",
        datetime.date(2026, 1, 18),
        datetime.date(2026, 7, 16),
    )
  with pytest.raises(ValueError, match="Invalid BigQuery table ID"):
    pbip_model.render_events_sql(
        "test-project-0728-467323",
        "bqaa_fixture_adk_1_27_0",
        "-bad_table",
        datetime.date(2026, 1, 18),
        datetime.date(2026, 7, 16),
    )


def test_events_v1_29_columns_49_measures_and_tmdl_hygiene():
  pbip_common = _load_pbi_module("pbip_common")
  pbip_model = _load_pbi_module("pbip_model")

  contracts = pbip_common.load_contracts()
  model_spec = pbip_common.load_spec("semantic_model.yaml")
  model_files = pbip_model.build_model(contracts, model_spec)

  events_text = model_files["definition/tables/events_v1.tmdl"]
  date_text = model_files["definition/tables/Date.tmdl"]
  rel_text = model_files["definition/relationships.tmdl"]
  db_text = model_files["definition/database.tmdl"]
  model_text = model_files["definition/model.tmdl"]

  assert "compatibilityLevel: 1567" in db_text
  assert "defaultPowerBIDataSourceVersion: powerBI_V3" in model_text
  assert "ref table events_v1" in model_text
  assert "ref table Date" in model_text
  assert "fromColumn: events_v1.event_date" in rel_text
  assert "toColumn: Date.Date" in rel_text
  assert "dataCategory: Time" in date_text

  # 29 columns in exact order.
  col_decls = re.findall(r"^\tcolumn (\S+)$", events_text, re.MULTILINE)
  assert tuple(col_decls) == pbip_model.EVENT_COLUMN_NAMES
  assert len(col_decls) == 29

  # Hidden PK columns.
  for pk_col in (
      "event_pk",
      "llm_response_pk",
      "tool_completed_pk",
      "tool_error_pk",
  ):
    assert f"column {pk_col}\n\t\tdataType: string\n\t\tisHidden" in events_text

  # 49 measures in exact order, DISTINCTCOUNTNOBLANK-only rule.
  assert "DISTINCTCOUNT(" not in events_text
  assert "DISTINCTCOUNTNOBLANK(" in events_text
  measure_decls = re.findall(
      r"^\tmeasure (?:'([^']+)'|(\S+)) = (.+)$", events_text, re.MULTILINE
  )
  assert len(measure_decls) == 49
  actual_measure_names = tuple(m[0] or m[1] for m in measure_decls)
  assert actual_measure_names == pbip_model.MEASURE_NAMES

  # Mutated spec fails build_model (#check-can-fail).
  broken_spec = copy.deepcopy(model_spec)
  broken_spec["measures"][1]["dax"] = "DISTINCTCOUNT('events_v1'[trace_id])"
  with pytest.raises(ValueError, match="DISTINCTCOUNTNOBLANK"):
    pbip_model.build_model(contracts, broken_spec)


def test_output_zip_and_rewrite_expressions_parameters(tmp_path):
  import zipfile

  generate_pbip = _load_pbi_module("generate_pbip")
  out_zip = tmp_path / "custom_bqaa.zip"
  rc = generate_pbip.main(
      [
          "--output-zip",
          str(out_zip),
          "--project",
          "my-prod-project-123",
          "--dataset",
          "my_agent_dataset",
          "--table",
          "my_agent_events",
          "--billing-project",
          "my-billing-proj-456",
          "--window-end",
          "2026-07-16",
          "--lookback-days",
          "30",
          "--use-storage-api",
          "false",
      ]
  )
  assert rc == 0
  assert out_zip.is_file()

  with zipfile.ZipFile(out_zip, "r") as zf:
    assert zf.testzip() is None
    names = zf.namelist()
    assert len(names) == 118
    assert "BQAA_Agent_Analytics/BQAA_Agent_Analytics.pbip" in names
    expr_arc = (
        "BQAA_Agent_Analytics/"
        "BQAA_Agent_Analytics.SemanticModel/definition/expressions.tmdl"
    )
    assert expr_arc in names
    expr_text = zf.read(expr_arc).decode("utf-8")
    assert 'expression BQAA_ProjectId = "my-prod-project-123" meta' in expr_text
    assert 'expression BQAA_DatasetId = "my_agent_dataset" meta' in expr_text
    assert 'expression BQAA_TableId = "my_agent_events" meta' in expr_text
    assert (
        'expression BQAA_BillingProjectId = "my-billing-proj-456" meta'
        in expr_text
    )
    assert 'expression BQAA_WindowEndDate = "2026-07-16" meta' in expr_text
    assert "expression BQAA_LookbackDays = 30 meta" in expr_text
    assert "expression BQAA_UseStorageApi = false meta" in expr_text

  # Invalid project ID must fail closed (#check-can-fail).
  bad_zip = tmp_path / "bad.zip"
  assert (
      generate_pbip.main(
          [
              "--output-zip",
              str(bad_zip),
              "--project",
              "BAD_PROJECT!",
          ]
      )
      == 1
  )
  assert not bad_zip.exists()

  # Trailing newline must be rejected by .fullmatch() (#check-can-fail).
  with pytest.raises(ValueError, match="Invalid BigQuery project ID"):
    generate_pbip.rewrite_expressions_parameters(
        expr_text,
        project="abcde-1\n",
        dataset="my_agent_dataset",
        table="my_agent_events",
    )
