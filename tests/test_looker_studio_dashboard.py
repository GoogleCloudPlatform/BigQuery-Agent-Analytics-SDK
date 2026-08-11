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

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.parse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard/looker_studio"


def _load_dashboard_module(name):
  module_path = DASHBOARD / f"tools/{name}.py"
  spec = importlib.util.spec_from_file_location(
      f"looker_studio_{name}", module_path
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _load_hydration_module():
  return _load_dashboard_module("hydrate_dashboard")


def test_portable_linking_api_configuration():
  hydration = _load_hydration_module()
  link = hydration.build_link(
      "customer-project-123",
      "agent_analytics",
      "agent_events",
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
      "sentinelbqaaevents",
      "agent_events",
  ]


def test_hyphenated_bigquery_table_ids_are_supported_by_python_tools():
  table = "events_agent_cur-phenix"
  hydration = _load_hydration_module()
  live_validation = _load_dashboard_module("validate_live_bqaa")

  assert hasattr(hydration, "DATASET_RE")
  assert hasattr(hydration, "TABLE_RE")
  assert hasattr(live_validation, "DATASET_RE")
  assert hasattr(live_validation, "TABLE_RE")
  assert (
      hydration.require_identifier("table ID", table, hydration.TABLE_RE)
      == table
  )
  assert (
      live_validation.require_identifier(
          "table ID", table, live_validation.TABLE_RE
      )
      == table
  )

  link = hydration.build_link(
      "customer-project-123",
      "agent_analytics",
      table,
      "billing-project-123",
      "Customer BQAA",
  )
  sql_replace = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)[
      "ds.ds230.sqlReplace"
  ][0].split(",")
  assert sql_replace[-2:] == ["sentinelbqaaevents", table]


@pytest.mark.parametrize(
    ("label", "value", "pattern"),
    [
        ("project ID", "UPPERCASE", "PROJECT_RE"),
        ("project ID", "project;drop", "PROJECT_RE"),
        ("dataset ID", "bad-dataset", "DATASET_RE"),
        ("dataset ID", "data`set", "DATASET_RE"),
        ("table ID", "table,other", "TABLE_RE"),
        ("table ID", "data`set", "TABLE_RE"),
    ],
)
def test_hydration_identifiers_fail_closed(label, value, pattern):
  hydration = _load_hydration_module()
  with pytest.raises(ValueError):
    hydration.require_identifier(label, value, getattr(hydration, pattern))


@pytest.mark.parametrize(
    ("project", "dataset", "table", "billing_project", "report_name"),
    [
        (
            "xsentinelbqaaevents",
            "agent_analytics",
            "agent_events",
            "billing-project-123",
            "Customer BQAA",
        ),
        (
            "customer-project-123",
            "customer_sentinelbqaaevents_data",
            "agent_events",
            "billing-project-123",
            "Customer BQAA",
        ),
    ],
)
def test_hydration_rejects_sequential_replacement_collisions(
    project, dataset, table, billing_project, report_name
):
  hydration = _load_hydration_module()
  with pytest.raises(ValueError, match="reserved template sentinel"):
    hydration.build_link(
        project,
        dataset,
        table,
        billing_project,
        report_name,
    )


@pytest.mark.parametrize(
    ("project", "dataset", "table", "billing_project"),
    [
        (
            "test-project-0728-467323",
            "agent_analytics",
            "agent_events",
            "billing-project-123",
        ),
        (
            "customer-project-123",
            "bqaa_fixture_adk_1_27_0",
            "agent_events",
            "billing-project-123",
        ),
        (
            "customer-project-123",
            "agent_analytics",
            "custom_sentinelbqaaevents_table",
            "billing-project-123",
        ),
        (
            "customer-project-123",
            "agent_analytics",
            "agent_events",
            "test-project-0728-467323",
        ),
    ],
)
def test_hydration_allows_nonsequential_sentinel_text(
    project, dataset, table, billing_project
):
  hydration = _load_hydration_module()
  hydration.build_link(
      project,
      dataset,
      table,
      billing_project,
      "Customer BQAA",
  )


def test_generated_sql_artifacts_cannot_drift(tmp_path):
  generator = _load_dashboard_module("gen_events_tmpl")
  renderer = _load_dashboard_module("render_template")
  bindings = yaml.safe_load(
      (DASHBOARD / "bindings/template_bindings.yaml").read_text()
  )["placeholders"]

  logical_events = generator.generate()
  generated_logical = tmp_path / "events_v1.sql.tmpl"
  generated_logical.write_text(logical_events)
  assert (
      generated_logical.read_bytes()
      == (DASHBOARD / "sql/events_v1.sql.tmpl").read_bytes()
  )

  expected = {
      "sql/events_v1.template.sql": renderer.render_text(
          logical_events,
          bindings,
          "sql/events_v1.sql.tmpl",
      ),
      "sql/preflight.template.sql": renderer.render_text(
          (DASHBOARD / "sql/preflight.sql.tmpl").read_text(),
          bindings,
          "sql/preflight.sql.tmpl",
      ),
  }
  for path, rendered in expected.items():
    generated = tmp_path / Path(path).name
    generated.write_text(rendered)
    assert generated.read_bytes() == (DASHBOARD / path).read_bytes()


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


def test_product_contract_covers_every_parity_chart_and_live_fix():
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/chart_manifest.yaml").read_text()
  )
  product = yaml.safe_load(
      (DASHBOARD / "spec/product_contract.yaml").read_text()
  )

  source_ids = {chart["id"] for chart in manifest["charts"]}
  product_charts = product["charts"]
  assert {chart["id"] for chart in product_charts} == source_ids
  assert len(product_charts) == 37
  assert len({chart["title"] for chart in product_charts}) == 37

  titles = {chart["id"]: chart["title"] for chart in product_charts}
  assert titles["usage-events-by-agent"] == "Tool Completions by Agent"
  assert titles["usage-total-calls"] == "Total LLM Calls"
  assert titles["usage-top-5-users-by-session"] == "Top 5 Users by Sessions"
  assert titles["performance-average-llm-latency-in-ms"].endswith("(ms)")
  assert all("Llm" not in title for title in titles.values())
  assert all("Over the Time" not in title for title in titles.values())

  assert [page["name"] for page in product["pages"]] == [
      "Token Consumption",
      "Agent & Sessions",
      "Tool Usage",
      "LLM Interactions",
      "User Analytics",
      "Latency",
      "Errors",
      "Trace Inspector",
  ]
  assert product["defaults"]["date_range"] == {
      "mode": "rolling",
      "start_offset_days": 89,
      "end_offset_days": 0,
      "include_today": True,
      "page_scope": "all_report_pages",
  }
  assert product["layout"]["date_control"] == {
      "scope": "report_level",
      "present_on_all_pages": True,
      "left": 825,
      "top_range": [43, 45],
  }
  assert product["filtering"]["date_controls"] == {
      "apply_to_all_charts_on_page": True,
      "report_level_override": {
          "field": "agent_events.timestamp_date",
          "default_range_days": 90,
          "persists_across_pages": True,
          "supersedes": [
              "usage-control-date",
              "performance-control-date",
          ],
      },
  }
  assert product["layout"]["percentile_order"] == {
      "llm": ["P50", "P75", "P90", "P99"],
      "tool": ["P50", "P75", "P90", "P99"],
  }
  assert (
      product["behavioral_fixes"]["usage-llm-call-trends"]["dimension"]
      == "event_date"
  )
  assert (
      product["behavioral_fixes"]["usage-llm-call-trends"]["oracle_grain"]
      == "minute"
  )
  assert (
      product["behavioral_fixes"]["usage-llm-call-trends"]["compare_at"]
      == "event_date"
  )
  assert product["layout"]["latency_sections"] == {
      "llm_percentile_top": 377,
      "tool_percentile_top": 494,
      "trend_title_top": 611,
      "trend_chart_top": 670,
      "overlap_free": True,
  }
  page_bounds = product["layout"]["page_bounds"]
  assert page_bounds["minimum_bottom_padding_px"] == 24
  assert page_bounds["acceptance_rule"] == (
      "component_top_plus_height_lte_page_height_minus_bottom_padding"
  )
  assert page_bounds["coordinate_space"] == "page_local_css_px"
  assert page_bounds["verification_status"] == "verified"
  assert page_bounds["verified_date"] == "2026-07-29"
  assert page_bounds["tracking_issue"] == (
      "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#388"
  )
  assert [page["name"] for page in page_bounds["pages"]] == [
      "Token Consumption",
      "Latency",
  ]
  for page in page_bounds["pages"]:
    assert (
        page["max_component_bottom"]
        <= page["page_height"] - page_bounds["minimum_bottom_padding_px"]
    )
    assert page["bottom_padding"] == (
        page["page_height"] - page["max_component_bottom"]
    )
  assert product["filtering"]["top_user_rankings"] == {
      "group_remaining_as_others": False,
      "charts": [
          "usage-top-5-users-with-most-tokens-consumption",
          "usage-top-5-users-with-most-traces",
          "usage-top-5-users-by-session",
          "usage-top-5-users-by-events",
      ],
  }
  assert product["behavioral_fixes"]["tool-completed-charts"]["charts"] == [
      "usage-tool-invocations",
      "usage-tool-calls-over-time",
      "performance-tool-latency-trend",
  ]
  assert product["visual_system"]["single_series"] == {
      "mode": "google_blue",
      "color": "#4285f4",
      "legend": "hidden_when_title_defines_metric",
  }
  assert product["visual_system"]["multi_series"] == {
      "mode": "categorical_google_palette",
      "legend": "visible",
      "dimension_values_are_series_labels": True,
  }
  assert product["viewer_qa"] == {
      "chart_implementation": "native_data_studio",
      "community_visualizations": "not_used",
      "completion_signal": "non_degenerate_rendered_output",
      "cold_load_timeout_seconds": 90,
      "fresh_load_runs": 3,
      "navigation_loops": 3,
      "observed_baseline": {
          "verified_date": "2026-07-27",
          "viewport_width_css_px": 1568,
          "cold_load": {
              "blank_observed_at_seconds": 40,
              "fully_rendered_by_seconds": 70,
              "cache_state": "view_miss",
          },
          "warm_navigation": {
              "return_rendered_within_seconds": 10,
              "second_page_rendered_within_seconds": 18,
          },
          "network": {
              "usercontent_goog_requests": 0,
              "community_visualization_requests": 0,
          },
      },
      "required_evidence": [
          "browser_and_version",
          "signed_in_state",
          "viewport_css_pixels",
          "load_type",
          "page_navigation_sequence",
          "time_to_non_degenerate_render",
          "timestamped_page_capture",
          "failed_network_requests",
          "bigquery_job_activity",
      ],
  }
  assert product["viewport_support"] == {
      "layout_mode": "freeform",
      "target": "desktop",
      "minimum_supported_width_css_px": 1280,
      "recommended_width_css_px": 1440,
      "narrow_screen_support": "not_supported_in_v1",
      "responsive_template": "separate_report_required",
      "minimum_width_validation": "pending",
      "last_validated_width_css_px": 1568,
  }
  assert "live_series_mode" not in product["visual_system"]
  deferred = {item["id"] for item in product["deferred_enhancements"]}
  assert "llm-error-visibility" in deferred
  assert "responsive-mobile-template" in deferred
  assert "native-chart-rendering-investigation" in deferred
  assert {
      "session_id",
      "model_version",
  }.issubset(product["filtering"]["filter_bar"]["available_fields"])
  assert (
      product["filtering"]["predefined_tool_name_control"]["status"]
      == "intentionally_not_published"
  )


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
      "start_offset_days": 89,
      "end_offset_days": 0,
      "include_today": True,
      "page_scope": "all_report_pages",
  }
  assert web["sentinels"] == {
      "project": bindings["PROJECT"],
      "dataset": bindings["DATASET"],
      "table": bindings["TABLE"],
  }
  attestation = report["reviewed_template_sql"]
  template = (DASHBOARD / "sql/events_v1.template.sql").read_bytes()
  assert report["published_date"] == "2026-07-29"
  assert attestation == {
      "sha256": hashlib.sha256(template).hexdigest(),
      "reviewed_date": "2026-07-24",
      "scope": "repository_artifact_only",
  }
  assert report["live_template_verification"] == {
      "verified_date": "2026-07-29",
      "repository_sql_sha256": hashlib.sha256(template).hexdigest(),
      "method": [
          "connector_custom_query_review",
          "page_bounds_containment_probe",
          "sqlreplace_table_only_smoke_test",
          "canonical_viewer_credentials_review",
          "published_eight_page_ux_smoke_test",
          "published_non_degenerate_chart_data_capture",
          "editor_configuration_assertions",
          "published_tool_page_refresh",
          "published_include_today_default_validation",
      ],
      "result": "PASSED",
      "limitation": "mutable_external_report_requires_reverification_after_changes",
  }
  assert report["product_contract"] == "spec/product_contract.yaml"
  assert report["viewer_qa_contract"] == {
      "issue": ("GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#381"),
      "protocol": "docs/rendering-and-viewport-support.md",
      "status": "NOT_REPRODUCED_UNDER_PROTOCOL",
      "observed_baseline_comment": (
          "https://github.com/GoogleCloudPlatform/"
          "BigQuery-Agent-Analytics-SDK/pull/383#issuecomment-5098030747"
      ),
  }
  assert report["product_verification"] == {
      "verified_date": "2026-07-28",
      "pages": 8,
      "checks": [
          "expected_page_and_chart_titles_present",
          "no_too_many_rows_errors",
          "no_date_control_chart_overlaps",
          "llm_call_volume_dimension_is_event_date",
          "llm_and_tool_percentile_order_is_p50_p75_p90_p99",
          "llm_and_token_p1_bindings_render_non_degenerate_data",
          "latency_sections_are_aligned_and_non_overlapping",
          "single_series_legends_do_not_expose_internal_field_names",
          "top_user_rankings_do_not_group_remaining_users_as_others",
          "tool_charts_exclude_non_completed_rows",
          "multi_series_charts_use_categorical_legends",
          "no_partial_update_footer_after_refresh",
          "default_date_range_includes_today_on_seven_dashboard_pages",
      ],
      "result": "PASSED",
  }
  assert report["known_live_issues"] == []
  assert report["source_contract"] == {
      "mode": "BASE_TABLE",
      "generated_views_required": False,
      "replacement_identifiers": ["PROJECT", "DATASET", "TABLE"],
  }
  assert report["credential_mode"] == "VIEWERS"
  assert report["generated_report_credential_gate"] == {
      "observed_initial_mode": "OWNERS",
      "required_before_sharing": "VIEWERS",
      "verification_path": "Resource > Manage added data sources > Edit",
  }


def test_report_level_date_range_includes_today_for_exactly_90_calendar_days():
  product = yaml.safe_load(
      (DASHBOARD / "spec/product_contract.yaml").read_text()
  )
  report = yaml.safe_load(
      (DASHBOARD / "bindings/report_template.yaml").read_text()
  )

  date_range = product["defaults"]["date_range"]
  assert report["default_date_range"] == date_range
  assert date_range["include_today"] is True
  assert date_range["end_offset_days"] == 0
  assert date_range["page_scope"] == "all_report_pages"
  assert (
      date_range["start_offset_days"] - date_range["end_offset_days"] + 1 == 90
  )

  date_controls = product["filtering"]["date_controls"]
  report_override = date_controls["report_level_override"]
  assert report_override["default_range_days"] == 90
  assert report_override["persists_across_pages"] is True
  assert report_override["supersedes"] == [
      "usage-control-date",
      "performance-control-date",
  ]


def test_report_level_override_preserves_the_immutable_source_controls():
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/chart_manifest.yaml").read_text()
  )

  date_controls = {
      control["id"]: control
      for control in manifest["controls"]
      if control["id"] in {"usage-control-date", "performance-control-date"}
  }
  assert date_controls["usage-control-date"]["default_value"] == "14 day"
  assert date_controls["performance-control-date"]["default_value"] == "7 day"
  assert {
      control["source_dashboard"] for control in date_controls.values()
  } == {
      "usage",
      "performance",
  }


def test_base_table_query_and_preflight_cover_the_bqaa_contract():
  query = (DASHBOARD / "sql/events_v1.sql.tmpl").read_text()
  preflight = (DASHBOARD / "sql/preflight.sql.tmpl").read_text()
  profile = json.loads(
      (DASHBOARD / "spec/compatibility_profile.json").read_text()
  )

  assert query.count("FROM `{{PROJECT}}.{{DATASET}}.{{TABLE}}`") == 1
  assert "VIEW_PREFIX" not in query
  assert profile["generated_views_required"] is False
  assert profile["source_object"] == "agent_events"
  assert "JSON_VALUE(content, '$.usage.total')" in query
  assert "$.usage_metadata.total_token_count" in query
  assert "JSON_VALUE(content, '$.tool')" in query
  assert "{{TABLE}}" in preflight
  assert "VIEW_PREFIX" not in preflight
  assert "WRONG_OBJECT_TYPE" in preflight
  assert "@DS_START_DATE" in query
  assert "@DS_END_DATE" in query


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js not installed"
)
def test_browser_configurator_javascript_contract():
  subprocess.run(
      ["node", "tools/test_web_configurator.mjs"],
      cwd=DASHBOARD,
      check=True,
  )


def _chrome_available():
  candidates = [
      "google-chrome",
      "google-chrome-stable",
      "chromium-browser",
      "chromium",
  ]
  if any(shutil.which(c) for c in candidates):
    return True
  return Path(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  ).exists()


def _browser_gate_disposition(chrome_available, in_ci):
  """A missing browser may downgrade the gate locally, never in CI.

  Returns "run" or "skip"; raises when the required merge gate would be
  silently lost (CI without a browser must be a hard failure, not a skip).
  """
  if chrome_available:
    return "run"
  if in_ci:
    raise AssertionError(
        "Chrome/Chromium is missing on a CI runner: the browser gate would"
        " be silently skipped inside a required check. Provision a browser"
        " or fail loudly — do not skip."
    )
  return "skip"


def test_browser_gate_cannot_silently_skip_in_ci():
  assert _browser_gate_disposition(True, True) == "run"
  assert _browser_gate_disposition(True, False) == "run"
  assert _browser_gate_disposition(False, False) == "skip"
  with pytest.raises(AssertionError, match="silently skipped"):
    _browser_gate_disposition(False, True)


def test_configurator_loads_in_a_real_browser():
  # Runs inside the required Test (Python N) checks so the browser-level
  # gate is enforced by the existing main ruleset, not by an optional job.
  # In CI a missing browser is a hard failure (see disposition above).
  disposition = _browser_gate_disposition(
      _chrome_available(), bool(os.environ.get("CI"))
  )
  if disposition == "skip":
    pytest.skip("No Chrome/Chromium available outside CI")
  subprocess.run(
      ["bash", "tools/browser_smoke.sh"],
      cwd=DASHBOARD,
      check=True,
  )


def test_browser_smoke_negative_fixtures_are_detected():
  # The five negative fixtures — including nonzero-exit-after-healthy-DOM
  # and the delayed error that only the live marker reflects (the 5 s
  # virtual-time budget is the observation window) — must be enforced by
  # the required Test checks, not only by the optional standalone smoke
  # job: a reintroduced false-pass path has to turn a REQUIRED check red.
  disposition = _browser_gate_disposition(
      _chrome_available(), bool(os.environ.get("CI"))
  )
  if disposition == "skip":
    pytest.skip("No Chrome/Chromium available outside CI")
  subprocess.run(
      ["bash", "tools/browser_smoke.sh", "--self-test"],
      cwd=DASHBOARD,
      check=True,
  )


def test_googlecloudplatform_pages_configuration():
  page = (DASHBOARD / "docs/index.html").read_text()
  styles = (DASHBOARD / "docs/styles.css").read_text()
  assert "github.com/caohy1988" not in page
  assert (
      "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
      in page
  )
  assert (
      "https://googlecloudplatform.github.io/"
      "BigQuery-Agent-Analytics-SDK/" in page
  )
  assert 'rel="icon" href="./favicon.svg"' in page
  assert 'property="og:title"' in page
  assert 'name="twitter:card"' in page
  assert "Copy security checklist" in page
  assert "billing-project-hint" in page
  assert "Designed for desktop screens at least 1280 px wide" in page
  assert "allow up to 90 seconds" in page
  assert "@media (prefers-color-scheme: dark)" in styles
  assert (DASHBOARD / "docs/favicon.svg").is_file()

  # Trust cluster (#398/#399/#400): pre-click wait expectation, dialog
  # explanation with the exact SQL linked, and the Google Blue palette.
  assert 'content="#1967d2"' in page
  assert "create-wait-note" in page
  assert page.count("don’t close it") >= 2  # at the button AND in step 02
  assert "lookerstudio.google.com" in page
  assert "sql/events_v1.template.sql" in page
  assert 'class="notice notice-warning"' in page
  assert "--action: #1967d2" in styles
  assert "#096b5a" not in page
  assert "#096b5a" not in styles

  # The recurring #399 dialog verification is a durable release control,
  # not an issue comment: it must stay in the implementation contract.
  impl = (DASHBOARD / "docs/dashboard-implementation.md").read_text()
  assert "## Configurator release checks" in impl
  assert "acknowledgement-dialog comparison" in impl
  assert "every template republish" in impl

  workflow = (ROOT / ".github/workflows/looker-studio-pages.yml").read_text()
  assert "path: dashboard/looker_studio/docs" in workflow
  assert "pages: write" in workflow
  assert "id-token: write" in workflow
  assert (
      "actions/deploy-pages@"
      "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
  )
