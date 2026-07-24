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
import shutil
import subprocess
import urllib.parse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard/looker_studio"


def _load_hydration_module():
  module_path = DASHBOARD / "tools/hydrate_dashboard.py"
  spec = importlib.util.spec_from_file_location(
      "looker_studio_hydration", module_path
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_portable_linking_api_configuration():
  hydration = _load_hydration_module()
  link = hydration.build_link(
      "customer-project-123",
      "agent_analytics",
      "custom-views",
      "billing-project-123",
      "Customer BQAA",
  )

  parsed = urllib.parse.urlparse(link)
  parameters = urllib.parse.parse_qs(parsed.query)
  assert parsed.scheme == "https"
  assert parsed.netloc == "lookerstudio.google.com"
  assert parameters["c.mode"] == ["view"]
  assert parameters["ds.ds230.billingProjectId"] == ["billing-project-123"]
  assert parameters["ds.ds230.refreshFields"] == ["false"]
  assert parameters["ds.ds230.sqlReplace"][0].split(",") == [
      "test-project-0728-467323",
      "customer-project-123",
      "bqaa_fixture_adk_1_27_0",
      "agent_analytics",
      "vsentinelbqaa",
      "custom-views",
  ]


@pytest.mark.parametrize(
    ("label", "value", "pattern"),
    [
        ("project ID", "UPPERCASE", "PROJECT_RE"),
        ("project ID", "project;drop", "PROJECT_RE"),
        ("dataset ID", "bad-dataset", "ID_RE"),
        ("dataset ID", "data`set", "ID_RE"),
        ("view prefix", "v,other", "PREFIX_RE"),
    ],
)
def test_hydration_identifiers_fail_closed(label, value, pattern):
  hydration = _load_hydration_module()
  with pytest.raises(ValueError):
    hydration.require_identifier(label, value, getattr(hydration, pattern))


def test_chart_manifest_and_independent_queries_are_complete():
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/chart_manifest.yaml").read_text()
  )
  charts = manifest["charts"]
  assert len(charts) == 37
  assert len([c for c in charts if c["source_dashboard"] == "usage"]) == 21
  assert (
      len([c for c in charts if c["source_dashboard"] == "performance"]) == 16
  )

  mapped = {chart["oracle_query"] for chart in charts}
  observed = {
      str(path.relative_to(DASHBOARD))
      for path in (DASHBOARD / "oracle/queries").glob("*.sql")
  }
  assert mapped == observed


def test_report_and_web_bindings_cannot_drift():
  report = yaml.safe_load(
      (DASHBOARD / "bindings/report_template.yaml").read_text()
  )
  bindings = yaml.safe_load(
      (DASHBOARD / "bindings/template_bindings.yaml").read_text()
  )["placeholders"]

  source = (DASHBOARD / "docs/report-config.mjs").read_text()
  payload = source.split("Object.freeze(", 1)[1].rsplit(");", 1)[0]
  web = json.loads(payload)

  assert web["reportId"] == report["report_id"]
  assert web["dataSourceAlias"] == report["data_source_alias"]
  assert report["default_date_range"] == {
      "mode": "rolling",
      "start_offset_days": 365,
      "end_offset_days": 1,
      "include_today": False,
      "page_scope": "all_dashboard_pages",
  }
  assert web["sentinels"] == {
      "project": bindings["PROJECT"],
      "dataset": bindings["DATASET"],
      "viewPrefix": bindings["VIEW_PREFIX"],
  }


def test_union_and_preflight_cover_the_portable_bqaa_contract():
  union = (DASHBOARD / "sql/events_v1.sql.tmpl").read_text()
  preflight = (DASHBOARD / "sql/preflight.sql.tmpl").read_text()
  profile = json.loads(
      (DASHBOARD / "spec/compatibility_profile.json").read_text()
  )

  assert union.count("FROM `{{PROJECT}}.{{DATASET}}.{{VIEW_PREFIX}}_") == 15
  assert len(profile["intersection_views"]) == 15
  assert "usage_metadata" in preflight
  assert "WRONG_OBJECT_TYPE" in preflight
  assert "@DS_START_DATE" in union
  assert "@DS_END_DATE" in union


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js not installed"
)
def test_browser_configurator_javascript_contract():
  subprocess.run(
      ["node", "tools/test_web_configurator.mjs"],
      cwd=DASHBOARD,
      check=True,
  )


def test_googlecloudplatform_pages_configuration():
  page = (DASHBOARD / "docs/index.html").read_text()
  assert "github.com/caohy1988" not in page
  assert (
      "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
      in page
  )
  assert (
      "https://googlecloudplatform.github.io/"
      "BigQuery-Agent-Analytics-SDK/" in page
  )

  workflow = (ROOT / ".github/workflows/looker-studio-pages.yml").read_text()
  assert "path: dashboard/looker_studio/docs" in workflow
  assert "pages: write" in workflow
  assert "id-token: write" in workflow
  assert (
      "actions/deploy-pages@"
      "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
  )
