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

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
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
