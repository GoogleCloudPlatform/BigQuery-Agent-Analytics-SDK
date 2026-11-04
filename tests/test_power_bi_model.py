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


def test_events_v1_29_columns_50_measures_and_tmdl_hygiene():
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

  # 50 measures in exact order, DISTINCTCOUNTNOBLANK-only rule.
  assert "DISTINCTCOUNT(" not in events_text
  assert "DISTINCTCOUNTNOBLANK(" in events_text
  measure_decls = re.findall(
      r"^\tmeasure (?:'([^']+)'|(\S+)) = (.+)$", events_text, re.MULTILINE
  )
  assert len(measure_decls) == 50
  actual_measure_names = tuple(m[0] or m[1] for m in measure_decls)
  assert actual_measure_names == pbip_model.MEASURE_NAMES
  assert len(set(actual_measure_names)) == 50
  assert actual_measure_names[-1] == "Top 5 Agents by LLM Calls"

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


# ---------------------------------------------------------------------------
# Strict identifier and date validation: patterns match the raw string.
# ---------------------------------------------------------------------------

_CLEAN_IDS = (
    "test-project-0728-467323",
    "bqaa_fixture_adk_1_27_0",
    "agent_events",
)
_ID_ERRORS = (
    "Invalid BigQuery project ID",
    "Invalid BigQuery dataset ID",
    "Invalid BigQuery table ID",
)


def _render_sql(pbip_model, project, dataset, table):
  return pbip_model.render_events_sql(
      project,
      dataset,
      table,
      datetime.date(2026, 1, 18),
      datetime.date(2026, 7, 16),
  )


def _contracts_and_spec():
  pbip_common = _load_pbi_module("pbip_common")
  return pbip_common.load_contracts(), pbip_common.load_spec(
      "semantic_model.yaml"
  )


def _spec_parameter(model_spec, name):
  for param in model_spec["parameters"]:
    if param["name"] == name:
      return param
  raise AssertionError(f"parameter {name} is not declared in the spec")


def _copy_looker(tmp_path):
  dest = tmp_path / "looker_studio"
  shutil.copytree(LOOKER, dest)
  return dest


def test_render_events_sql_accepts_the_clean_control_identifiers():
  pbip_model = _load_pbi_module("pbip_model")
  sql = _render_sql(pbip_model, *_CLEAN_IDS)
  assert (
      "FROM `test-project-0728-467323.bqaa_fixture_adk_1_27_0.agent_events`"
      in sql
  )


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize(
    "suffix", ["\n", "\r\n", "\r", " ", "`", ";", "'", '"', "#", "\x00"]
)
def test_render_events_sql_rejects_a_suffix_after_a_valid_identifier(
    position, suffix
):
  pbip_model = _load_pbi_module("pbip_model")
  args = list(_CLEAN_IDS)
  args[position] += suffix
  with pytest.raises(ValueError, match=_ID_ERRORS[position]):
    _render_sql(pbip_model, *args)


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("prefix", ["\n", " ", "`", "\t"])
def test_render_events_sql_rejects_a_prefix_before_a_valid_identifier(
    position, prefix
):
  pbip_model = _load_pbi_module("pbip_model")
  args = list(_CLEAN_IDS)
  args[position] = prefix + args[position]
  with pytest.raises(ValueError, match=_ID_ERRORS[position]):
    _render_sql(pbip_model, *args)


@pytest.mark.parametrize(
    "position,bad",
    [
        (0, ""),
        (0, "Test-Project-0728"),
        (0, "abcd"),
        (0, None),
        (0, 123),
        (1, ""),
        (1, "1_leading_digit"),
        (1, "has-hyphen"),
        (1, None),
        (2, ""),
        (2, "-leading-hyphen"),
        (2, "has space"),
        (2, None),
        (2, b"agent_events"),
    ],
)
def test_render_events_sql_rejects_malformed_or_non_string_identifiers(
    position, bad
):
  pbip_model = _load_pbi_module("pbip_model")
  args = list(_CLEAN_IDS)
  args[position] = bad
  with pytest.raises(ValueError, match=_ID_ERRORS[position]):
    _render_sql(pbip_model, *args)


@pytest.mark.parametrize(
    "pattern_name,clean",
    [
        ("PROJECT_RE", "abcde-1"),
        ("DATASET_RE", "my_dataset"),
        ("TABLE_RE", "my_table-1"),
    ],
)
def test_identifier_patterns_do_not_accept_a_trailing_newline(
    pattern_name, clean
):
  pbip_common = _load_pbi_module("pbip_common")
  pattern = getattr(pbip_common, pattern_name)
  assert pattern.match(clean) is not None
  assert pattern.fullmatch(clean) is not None
  # A '$' anchor would let a terminal LF through; the patterns must not.
  assert pattern.match(clean + "\n") is None
  assert pattern.fullmatch(clean + "\n") is None


@pytest.mark.parametrize(
    "name,bad",
    [
        ("BQAA_ProjectId", "test-project-0728-467323\n"),
        ("BQAA_ProjectId", None),
        ("BQAA_BillingProjectId", "test-project-0728-467323\n"),
        ("BQAA_DatasetId", "bqaa_fixture_adk_1_27_0\n"),
        ("BQAA_TableId", "agent_events\n"),
        ("BQAA_WindowEndDate", "2026-07-16\n"),
        ("BQAA_WindowEndDate", " 2026-07-16"),
        ("BQAA_WindowEndDate", "2026-02-30"),
        ("BQAA_WindowEndDate", "2026-13-01"),
        ("BQAA_WindowEndDate", "2026-04-31"),
        ("BQAA_WindowEndDate", "2027-02-29"),
        ("BQAA_WindowEndDate", "2026-7-16"),
        ("BQAA_WindowEndDate", datetime.date(2026, 7, 16)),
        ("BQAA_WindowEndDate", 20260716),
    ],
)
def test_model_spec_rejects_non_canonical_parameter_defaults(name, bad):
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  broken = copy.deepcopy(model_spec)
  _spec_parameter(broken, name)["default"] = bad
  with pytest.raises(ValueError, match=f"Default for {name}"):
    pbip_model._validate_model_spec(contracts, broken)
  with pytest.raises(ValueError, match=f"Default for {name}"):
    pbip_model.build_model(contracts, broken)


@pytest.mark.parametrize(
    "default", ["auto", "2026-07-16", "2028-02-29", "2000-02-29"]
)
def test_model_spec_accepts_auto_and_real_calendar_dates(default):
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  ok = copy.deepcopy(model_spec)
  _spec_parameter(ok, "BQAA_WindowEndDate")["default"] = default
  pbip_model._validate_model_spec(contracts, ok)
  expressions = pbip_model.build_model(contracts, ok)[
      "definition/expressions.tmdl"
  ]
  assert f'expression BQAA_WindowEndDate = "{default}" meta' in expressions


def _spec_expressions_text():
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  return pbip_model.build_model(contracts, model_spec)[
      "definition/expressions.tmdl"
  ]


def _rewrite(expr_text, **overrides):
  generate_pbip = _load_pbi_module("generate_pbip")
  kwargs = {"project": "abcde-1", "dataset": "my_dataset", "table": "my_table"}
  kwargs.update(overrides)
  return generate_pbip.rewrite_expressions_parameters(expr_text, **kwargs)


@pytest.mark.parametrize(
    "window_end",
    [
        "2026-02-30",
        "2026-13-01",
        "2026-04-31",
        "2027-02-29",
        "2026-00-10",
        "2026-07-00",
    ],
)
def test_rewrite_expressions_parameters_rejects_impossible_dates(window_end):
  with pytest.raises(ValueError, match="Invalid calendar date"):
    _rewrite(_spec_expressions_text(), window_end=window_end)


@pytest.mark.parametrize(
    "window_end",
    [
        "2026-7-16",
        "20260716",
        "2026-07-16T00:00:00",
        "tomorrow",
        "2026-07-16 x",
    ],
)
def test_rewrite_expressions_parameters_rejects_malformed_dates(window_end):
  with pytest.raises(ValueError, match="Invalid window end date"):
    _rewrite(_spec_expressions_text(), window_end=window_end)


def test_rewrite_expressions_parameters_accepts_a_leap_day_and_auto():
  text = _spec_expressions_text()
  leap = _rewrite(text, window_end="2028-02-29")
  assert 'expression BQAA_WindowEndDate = "2028-02-29" meta' in leap
  auto = _rewrite(text, window_end="AUTO")
  assert 'expression BQAA_WindowEndDate = "auto" meta' in auto


@pytest.mark.parametrize(
    "field,message",
    [
        ("project", "Invalid BigQuery project ID"),
        ("dataset", "Invalid BigQuery dataset ID"),
        ("table", "Invalid BigQuery table ID"),
    ],
)
def test_rewrite_expressions_parameters_rejects_a_trailing_newline(
    field, message
):
  clean = {"project": "abcde-1", "dataset": "my_dataset", "table": "my_table"}
  with pytest.raises(ValueError, match=message):
    _rewrite(_spec_expressions_text(), **{field: clean[field] + "\n"})


# ---------------------------------------------------------------------------
# Oracle references: every cited column is a real output alias, and every
# measure carries a digest of the oracle SQL it cites.
# ---------------------------------------------------------------------------

_KPI_PRIOR_ALIASES = {
    "KPI Total Tokens Prior": ("usage-total-tokens", "pop_total_tokens_prior"),
    "KPI Total Traces Prior": ("usage-total-traces", "pop_total_traces_prior"),
    "KPI Total Sessions Prior": (
        "usage-total-sessions",
        "pop_total_sessions_prior",
    ),
    "KPI Total LLM Calls Prior": ("usage-total-calls", "pop_llm_calls_prior"),
    "KPI Total Users Prior": ("usage-total-users", "pop_total_users_prior"),
    "KPI Tool Errors Prior": (
        "performance-total-errors",
        "pop_tool_errors_prior",
    ),
}


@pytest.mark.parametrize("measure_name", sorted(_KPI_PRIOR_ALIASES))
def test_kpi_prior_measures_cite_a_real_oracle_output_alias(measure_name):
  pbip_common = _load_pbi_module("pbip_common")
  contracts, model_spec = _contracts_and_spec()
  query, alias = _KPI_PRIOR_ALIASES[measure_name]
  measure = next(m for m in model_spec["measures"] if m["name"] == measure_name)
  assert measure["oracle_queries"] == [f"oracle/queries/{query}.sql"]
  assert measure["oracle_column"] == alias
  sql = contracts.oracle_files[query]
  assert pbip_common.has_output_alias(sql, alias)
  assert re.search(rf"^[ \t]*prev\.value AS {alias},$", sql, re.MULTILINE)


@pytest.mark.parametrize("measure_name", sorted(_KPI_PRIOR_ALIASES))
def test_build_model_fails_when_a_kpi_prior_alias_is_removed(
    tmp_path, measure_name
):
  pbip_common = _load_pbi_module("pbip_common")
  pbip_model = _load_pbi_module("pbip_model")
  query, alias = _KPI_PRIOR_ALIASES[measure_name]
  looker = _copy_looker(tmp_path)
  sql_path = looker / "oracle" / "queries" / f"{query}.sql"
  original = sql_path.read_text(encoding="utf-8")
  alias_line = re.compile(rf"^[ \t]*prev\.value AS {alias},\n", re.MULTILINE)
  assert len(alias_line.findall(original)) == 1
  sql_path.write_text(alias_line.sub("", original), encoding="utf-8")

  contracts = pbip_common.load_contracts(looker_root=looker)
  model_spec = pbip_common.load_spec("semantic_model.yaml")
  with pytest.raises(
      ValueError, match=f"oracle_column '{alias}' is not an output alias"
  ):
    pbip_model.build_model(contracts, model_spec)


@pytest.mark.parametrize(
    "sql,column,expected",
    [
        ("SELECT cur.value AS pop_x_prior FROM t", "pop_x_prior", True),
        ("SELECT cur.value as pop_x_prior FROM t", "pop_x_prior", True),
        ("SELECT cur.value AS POP_X_PRIOR FROM t", "pop_x_prior", True),
        ("SELECT cur.value AS\n  pop_x_prior\nFROM t", "pop_x_prior", True),
        ("SELECT a AS pop_x_prior_extra FROM t", "pop_x_prior", False),
        ("SELECT a AS xpop_x_prior FROM t", "pop_x_prior", False),
        ("SELECT pop_x_prior FROM t", "pop_x_prior", False),
        ("SELECT a -- AS pop_x_prior\nFROM t", "pop_x_prior", False),
        ("SELECT a /* AS pop_x_prior */ FROM t", "pop_x_prior", False),
        ("SELECT a AS popxx FROM t", "pop.x", False),
    ],
)
def test_has_output_alias_matrix(sql, column, expected):
  pbip_common = _load_pbi_module("pbip_common")
  assert pbip_common.has_output_alias(sql, column) is expected


@pytest.mark.parametrize(
    "bad,message",
    [
        (None, "must name its oracle_column"),
        ("", "must name its oracle_column"),
        (7, "must name its oracle_column"),
        ("pop_not_a_column", "is not an output alias"),
        ("pop_total_tokens", "is not an output alias"),
    ],
)
def test_model_spec_rejects_a_bad_oracle_column(bad, message):
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  broken = copy.deepcopy(model_spec)
  measure = next(
      m for m in broken["measures"] if m["name"] == "KPI Total Tokens Prior"
  )
  measure["oracle_column"] = bad
  with pytest.raises(ValueError, match=message):
    pbip_model._validate_model_spec(contracts, broken)


def test_model_spec_rejects_an_unknown_oracle_query():
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  broken = copy.deepcopy(model_spec)
  broken["measures"][0]["oracle_queries"] = ["oracle/queries/not-a-query.sql"]
  with pytest.raises(ValueError, match="unknown oracle query"):
    pbip_model._validate_model_spec(contracts, broken)


def test_model_spec_rejects_a_wrong_measure_count():
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  short = copy.deepcopy(model_spec)
  short["measures"].pop()
  with pytest.raises(ValueError, match="Expected 50 measures"):
    pbip_model._validate_model_spec(contracts, short)


def _measure_blocks(events_text):
  """Splits events_v1.tmdl into {measure name: its property lines}."""
  blocks = {}
  current = None
  for line in events_text.splitlines():
    declaration = re.match(r"^\tmeasure (?:'((?:[^']|'')+)'|(\S+)) = ", line)
    if declaration:
      current = (declaration.group(1) or declaration.group(2)).replace(
          "''", "'"
      )
      blocks[current] = []
    elif current is not None:
      if line.startswith("\t\t") or line == "":
        blocks[current].append(line)
      else:
        current = None
  return blocks


def test_oracle_digest_is_short_stable_and_content_sensitive():
  pbip_common = _load_pbi_module("pbip_common")
  files = pbip_common.OracleFilesDict(
      {
          "oracle/queries/a.sql": "SELECT 1\n",
          "oracle/queries/b.sql": "SELECT 2\n",
      }
  )
  digest = pbip_common.oracle_digest(files, ["oracle/queries/a.sql"])
  assert re.fullmatch(r"[0-9a-f]{16}", digest)
  assert digest == pbip_common.oracle_digest(files, ["oracle/queries/a.sql"])

  # Line endings are normalized so the digest does not depend on the checkout.
  crlf = pbip_common.OracleFilesDict({"oracle/queries/a.sql": "SELECT 1\r\n"})
  assert pbip_common.oracle_digest(crlf, ["oracle/queries/a.sql"]) == digest

  # An edit, a different name, a different query set, or a different order
  # each change the digest.
  edited = pbip_common.OracleFilesDict({"oracle/queries/a.sql": "SELECT 2\n"})
  assert pbip_common.oracle_digest(edited, ["oracle/queries/a.sql"]) != digest
  renamed = pbip_common.OracleFilesDict({"oracle/queries/c.sql": "SELECT 1\n"})
  assert pbip_common.oracle_digest(renamed, ["oracle/queries/c.sql"]) != digest
  assert pbip_common.oracle_digest(files, ["oracle/queries/b.sql"]) != digest
  both = pbip_common.oracle_digest(
      files, ["oracle/queries/a.sql", "oracle/queries/b.sql"]
  )
  assert both != digest
  assert both != pbip_common.oracle_digest(
      files, ["oracle/queries/b.sql", "oracle/queries/a.sql"]
  )


def test_every_measure_is_stamped_with_its_oracle_digest():
  pbip_common = _load_pbi_module("pbip_common")
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  events_text = pbip_model.build_model(contracts, model_spec)[
      "definition/tables/events_v1.tmdl"
  ]
  blocks = _measure_blocks(events_text)
  assert tuple(blocks) == pbip_model.MEASURE_NAMES
  for measure in model_spec["measures"]:
    expected = pbip_common.oracle_digest(
        contracts.oracle_files, measure["oracle_queries"]
    )
    annotations = [
        line
        for line in blocks[measure["name"]]
        if line.strip().startswith("annotation ")
    ]
    assert annotations == [
        f"\t\tannotation bqaa_oracle_sha256 = {expected}"
    ], measure["name"]


def test_editing_a_cited_oracle_query_changes_only_the_citing_measures(
    tmp_path,
):
  pbip_common = _load_pbi_module("pbip_common")
  pbip_model = _load_pbi_module("pbip_model")
  contracts, model_spec = _contracts_and_spec()
  baseline = pbip_model.build_model(contracts, model_spec)

  query = "usage-total-traces"
  looker = _copy_looker(tmp_path)
  sql_path = looker / "oracle" / "queries" / f"{query}.sql"
  sql_path.write_text(
      sql_path.read_text(encoding="utf-8") + "-- edited after review\n",
      encoding="utf-8",
  )
  edited = pbip_model.build_model(
      pbip_common.load_contracts(looker_root=looker), model_spec
  )

  changed_files = {name for name in baseline if baseline[name] != edited[name]}
  assert changed_files == {"definition/tables/events_v1.tmdl"}

  citing = {
      m["name"]
      for m in model_spec["measures"]
      if f"oracle/queries/{query}.sql" in m["oracle_queries"]
  }
  assert citing
  old_blocks = _measure_blocks(baseline["definition/tables/events_v1.tmdl"])
  new_blocks = _measure_blocks(edited["definition/tables/events_v1.tmdl"])
  differing = {
      name for name in old_blocks if old_blocks[name] != new_blocks[name]
  }
  assert differing == citing

  def _without_digest(lines):
    return [line for line in lines if "bqaa_oracle_sha256" not in line]

  for name in old_blocks:
    assert _without_digest(old_blocks[name]) == _without_digest(
        new_blocks[name]
    )


# ---------------------------------------------------------------------------
# Top-N measures: declared top_n, the DAX TOPN, and the oracle LIMIT agree.
# ---------------------------------------------------------------------------


def _final_limit(sql):
  no_comments = re.sub(
      r"--[^\n]*", "", re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
  )
  limits = re.findall(r"\bLIMIT\s+(\d+)\b", no_comments, flags=re.IGNORECASE)
  return int(limits[-1]) if limits else None


def test_top_n_measures_agree_with_their_dax_and_oracle_limit():
  contracts, model_spec = _contracts_and_spec()
  top_n_measures = [
      m for m in model_spec["measures"] if m.get("top_n") is not None
  ]
  assert len(top_n_measures) == 12
  mismatches = []
  for measure in top_n_measures:
    declared = measure["top_n"]
    in_dax = [int(n) for n in re.findall(r"TOPN\(\s*(\d+)\s*,", measure["dax"])]
    queries = measure["oracle_queries"]
    limit = _final_limit(contracts.oracle_files[queries[0]])
    if in_dax != [declared] or len(queries) != 1 or limit != declared:
      mismatches.append((measure["name"], declared, in_dax, limit))
  assert mismatches == []


def test_top_5_agents_by_llm_calls_is_a_real_top_five_measure():
  contracts, model_spec = _contracts_and_spec()
  by_name = {m["name"]: m for m in model_spec["measures"]}
  measure = by_name["Top 5 Agents by LLM Calls"]
  assert measure["display_folder"] == "Charts"
  assert measure["top_n"] == 5
  assert "TOPN(5," in measure["dax"]
  assert "KEEPFILTERS(_top)" in measure["dax"]
  assert measure["oracle_queries"] == [
      "oracle/queries/usage-top-5-agents-by-llm-calls.sql"
  ]
  assert _final_limit(contracts.oracle_files[measure["oracle_queries"][0]]) == 5
  # The base measure stays un-truncated for the other LLM-call visuals.
  assert "TOPN(" not in by_name["LLM Calls"]["dax"]


def test_traces_by_agent_excludes_rows_without_an_agent():
  _, model_spec = _contracts_and_spec()
  by_name = {m["name"]: m for m in model_spec["measures"]}
  helper = by_name["Traces for Agents With Tokens"]["dax"]
  assert "ISBLANK([Total Tokens])" in helper
  assert "ISBLANK(SELECTEDVALUE('events_v1'[agent]))" in helper
  assert "BLANK()" in helper
  ranked = by_name["Traces by Agent"]["dax"]
  assert (
      "NOT ISBLANK([Traces for Agents With Tokens])" in ranked
      and "[Traces for Agents With Tokens], DESC" in ranked
  )
