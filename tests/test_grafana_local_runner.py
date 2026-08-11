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

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "grafana" / "run_local.py"


def _load_runner():
  spec = importlib.util.spec_from_file_location("grafana_run_local", RUNNER)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


runner = _load_runner()


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("customer-project-123", runner.PROJECT_RE),
        ("agent_analytics", runner.DATASET_RE),
        ("agent_events", runner.TABLE_RE),
        ("events_agent_cur-phenix", runner.TABLE_RE),
        ("adk_", runner.VIEW_PREFIX_RE),
    ],
)
def test_identifiers_accepted(value, pattern):
  assert runner.require_identifier("x", value, pattern) == value


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("UPPERCASE", runner.PROJECT_RE),
        ("short", runner.PROJECT_RE),
        ("project;drop", runner.PROJECT_RE),
        ("bad-dataset", runner.DATASET_RE),
        ("data`set", runner.DATASET_RE),
        ("table,other", runner.TABLE_RE),
        ("pre fix", runner.VIEW_PREFIX_RE),
    ],
)
def test_identifiers_rejected(value, pattern):
  with pytest.raises(ValueError):
    runner.require_identifier("x", value, pattern)


@pytest.mark.parametrize("value", ["1.25", "5.00", "0", "12"])
def test_prices_accepted(value):
  assert runner.require_price("x", value) == value


@pytest.mark.parametrize(
    "value", ["1e3", "-1", "abc", "1.25; DROP", "1/0", "", "NaN", ".5"]
)
def test_prices_rejected(value):
  # These constants are interpolated into panel arithmetic, so anything
  # that is not a plain decimal must fail closed.
  with pytest.raises(ValueError):
    runner.require_price("x", value)


def _real_dashboard():
  return json.loads((ROOT / "grafana" / "bqaa-dashboard.json").read_text())


def test_patch_dashboard_fills_all_six_constants():
  constants = {
      "project": "customer-project-123",
      "dataset": "agent_analytics",
      "table": "agent_events",
      "view_prefix": "adk_",
      "price_per_1m_input_tokens": "1.25",
      "price_per_1m_output_tokens": "5.00",
  }
  patched = runner.patch_dashboard(_real_dashboard(), constants)
  by_name = {v["name"]: v for v in patched["templating"]["list"]}
  for name, value in constants.items():
    assert by_name[name]["type"] == "constant"
    assert by_name[name]["query"] == value
    assert by_name[name]["current"] == {"text": value, "value": value}


def test_patch_dashboard_leaves_query_variables_untouched():
  original = _real_dashboard()
  patched = runner.patch_dashboard(original, {"project": "customer-p-123"})
  originals = {v["name"]: v for v in original["templating"]["list"]}
  for variable in patched["templating"]["list"]:
    if variable["name"] != "project":
      assert variable == originals[variable["name"]]


def test_patch_dashboard_refuses_non_constant_targets():
  # "agent" is a query variable; writing it would break the dashboard's
  # constants-stay-constants injection-safety contract.
  with pytest.raises(ValueError, match="not a patchable constant"):
    runner.patch_dashboard(_real_dashboard(), {"agent": "billing-agent"})


def test_patch_dashboard_refuses_type_mismatch():
  dashboard = _real_dashboard()
  for variable in dashboard["templating"]["list"]:
    if variable["name"] == "project":
      variable["type"] = "textbox"
  with pytest.raises(ValueError, match="not\\s+constant"):
    runner.patch_dashboard(dashboard, {"project": "customer-p-123"})


def test_patch_dashboard_optional_time_window():
  patched = runner.patch_dashboard(
      _real_dashboard(),
      {"project": "customer-p-123"},
      time_from="2026-06-15T00:00:00Z",
      time_to="2026-08-11T00:00:00Z",
  )
  assert patched["time"] == {
      "from": "2026-06-15T00:00:00Z",
      "to": "2026-08-11T00:00:00Z",
  }
  untouched = runner.patch_dashboard(
      _real_dashboard(), {"project": "customer-p-123"}
  )
  assert untouched["time"] == _real_dashboard()["time"]


def test_datasource_yaml_defaults_to_adc():
  rendered = runner.render_datasource_yaml("customer-project-123")
  assert "authenticationType: gce" in rendered
  assert "defaultProject: customer-project-123" in rendered
  assert "privateKey" not in rendered


def test_datasource_yaml_jwt_uses_block_scalar(tmp_path):
  key_path = tmp_path / "key.json"
  key_path.write_text(
      json.dumps(
          {
              "client_email": "grafana@customer-project-123.iam.example.com",
              "private_key": (
                  "-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB\n-----END PRIVATE"
                  " KEY-----\n"
              ),
              "token_uri": "https://oauth2.example.com/token",
          }
      )
  )
  key = runner.load_service_account_key(key_path)
  rendered = runner.render_datasource_yaml("customer-project-123", key)
  assert "authenticationType: jwt" in rendered
  assert "clientEmail: grafana@customer-project-123.iam.example.com" in (
      rendered
  )
  # Real line breaks under a block scalar, never literal \n escapes
  # (grafana/README.md calls this out explicitly).
  assert "privateKey: |" in rendered
  assert "        -----BEGIN PRIVATE KEY-----\n        AAAA\n" in rendered
  assert "\\n" not in rendered


def test_service_account_key_must_be_complete(tmp_path):
  key_path = tmp_path / "key.json"
  key_path.write_text(json.dumps({"client_email": "x@example.com"}))
  with pytest.raises(ValueError, match="private_key"):
    runner.load_service_account_key(key_path)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "x86_64", "linux-amd64"),
    ],
)
def test_pick_dist(system, machine, expected):
  assert runner.pick_dist(system, machine) == expected


def test_pick_dist_rejects_unsupported():
  with pytest.raises(ValueError, match="manual steps"):
    runner.pick_dist("Windows", "AMD64")


def test_grafana_pin_satisfies_plugin_floor():
  # The plugin's grafanaDependency ranges end unbounded at >=12.2.5-0
  # (issue #421); a pin below 12.2.5 would reintroduce the opaque
  # react/jsx-runtime failure on fresh installs.
  major, minor, patch = (int(x) for x in runner.GRAFANA_VERSION.split("."))
  assert (major, minor, patch) >= (12, 2, 5)


def test_provision_only_writes_everything_without_network(tmp_path):
  result = subprocess.run(
      [
          sys.executable,
          str(RUNNER),
          "--project",
          "customer-project-123",
          "--dataset",
          "agent_analytics",
          "--provision-only",
          "--workdir",
          str(tmp_path),
      ],
      capture_output=True,
      text=True,
      check=True,
  )
  assert "provisioning written" in result.stdout
  datasource = (tmp_path / "provisioning/datasources/bigquery.yaml").read_text()
  assert "authenticationType: gce" in datasource
  dashboard = json.loads(
      (tmp_path / "dashboards/bqaa-dashboard.json").read_text()
  )
  by_name = {v["name"]: v for v in dashboard["templating"]["list"]}
  assert by_name["project"]["query"] == "customer-project-123"
  assert by_name["table"]["query"] == "agent_events"
  # The committed dashboard is a source file and must never be mutated.
  committed = {v["name"]: v for v in _real_dashboard()["templating"]["list"]}
  assert committed["project"]["query"] != "customer-project-123"


def test_rejects_bad_identifiers_before_writing(tmp_path):
  result = subprocess.run(
      [
          sys.executable,
          str(RUNNER),
          "--project",
          "UPPERCASE",
          "--dataset",
          "agent_analytics",
          "--provision-only",
          "--workdir",
          str(tmp_path),
      ],
      capture_output=True,
      text=True,
  )
  assert result.returncode != 0
  assert not (tmp_path / "provisioning").exists()
