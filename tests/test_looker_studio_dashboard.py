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

import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboards/looker_studio"


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
      "minimum_width_validation": "passed",
      "minimum_width_navigation_drawer_state": "collapsed",
      "last_validated_width_css_px": 1280,
      "last_validated_date": "2026-08-11",
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
  assert report["published_date"] == "2026-08-11"
  assert attestation == {
      "sha256": hashlib.sha256(template).hexdigest(),
      "reviewed_date": "2026-07-24",
      "scope": "repository_artifact_only",
  }
  assert report["live_template_verification"] == {
      "verified_date": "2026-08-11",
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
      "verified_date": "2026-08-11",
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
          "default_date_range_includes_today_on_all_eight_report_pages",
      ],
      "result": "PASSED",
  }
  assert report["known_live_issues"] == [
      {
          "issue": "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#445",
          "symptom": (
              'Linking API copy fails for external identities with "This'
              " report isn't shared with you\" before any ds.* parameter is"
              " applied."
          ),
          "scope": "external_non_owner_identities",
          "reported_date": "2026-08-24",
          "cause_isolation": (
              "NOT_ISOLATED: a 2026-08-25 authenticated Permissions API read"
              " returned LINK_VIEWER allUsers and assets:search listed the"
              " report non-trashed, so the link role alone does not explain"
              " the denial. Remaining credible causes: the viewer"
              " copy-disable control, recipient-side Workspace sharing"
              " policy on the reporter's own organization, multi-account"
              " browser state, or a transient sharing/service state."
          ),
          "status": "OPEN",
      },
  ]
  assert report["external_access_verification"] == {
      "controls": [
          {
              "method": "permissions_api_link_role_check",
              "protocol": (
                  "Authenticated GET https://datastudio.googleapis.com/v1"
                  "/assets/{report_id}/permissions must list role"
                  " LINK_VIEWER with member allUsers. Call it from a Google"
                  " Workspace or Cloud Identity account holding the"
                  " datastudio.readonly OAuth scope (least privilege for"
                  " this read). Treat PERMISSION_DENIED as indeterminate:"
                  " it can mean a caller constraint (non-org account,"
                  " missing scope) or lost asset authorization, so verify"
                  " the principal, its organization authorization, and the"
                  " scope before reading a denial as either."
              ),
              "limitation": "does_not_expose_viewer_copy_disable_control",
              "last_observed_date": "2026-08-25",
              "last_result": "LINK_VIEWER_ALLUSERS_PRESENT",
          },
          {
              "method": "external_identity_link_access_check",
              "protocol": (
                  "From a signed-in, non-owner, out-of-domain Google"
                  " account holding no direct grant on the report — either"
                  " a personal account, or a managed account whose"
                  " Workspace policy is recorded as allowing Looker Studio"
                  " assets from untrusted external domains (recipient-side"
                  " policy can block a fully public template, which would"
                  " misread as an owner-side outage) — open"
                  " /reporting/create?c.reportId={report_id}"
                  "&c.mode=view&c.explain=true and confirm it reaches the"
                  " Linking API copy/review flow rather than the terminal"
                  ' "This report isn\'t shared with you" dialog. Record'
                  " last_observed_date and last_identity_class on every"
                  " run, pass or fail. On failure, record privately which"
                  " Google account the dialog selected before changing any"
                  " setting; never post account identifiers publicly."
              ),
              "last_identity_class": "unknown",
              "last_observed_date": "2026-08-24",
              "link_access_verified_date": None,
              "last_result": "FAILURE_REPORTED",
          },
      ],
      "cadence": "monthly_manual_until_automated",
      "next_due_date": "2026-09-24",
      "status": "FAILING",
      "tracking_issue": (
          "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#445"
      ),
  }
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


def test_external_access_attestation_is_dated_or_tracked():
  """#445: external copy access is a live fact only an identity can observe.

  Two controls are required — the Permissions API exposes the link role but
  not the separate viewer copy-disable control, so only the end-to-end copy
  canary clears the whole path. The attestation must never claim more than
  was observed, in either direction:

  - PASSING requires success evidence on BOTH controls, a canary date no
    older than the published template, and no matching OPEN live issue —
    so the status cannot flip while contradictory failure evidence remains.
  - FAILING requires failure evidence on the canary plus a matching OPEN
    known_live_issues entry, so an outage stays repository-visible until
    the canary is re-run from a signed-in, non-owner, out-of-domain account.

  Dates must not be in the future; incident history is durable — the
  tracked issue must keep at least one known_live_issues entry in BOTH
  states, transitioning to RESOLVED rather than being deleted, so recovery
  can never become provable by erasing the outage it claims to resolve.
  Because date-only values cannot prove within-day order, a RESOLVED entry
  must carry an explicit resolution_observed_date equal to the passing
  canary's date and no earlier than the incident: the ordered recovery
  marker is that recorded attestation, not an unsound strict date
  inequality. PASSING further requires the Permissions API evidence to be
  from the same attestation date as the canary (stale link-role evidence
  cannot combine with a fresh canary), and a canary identity class that
  rules out recipient-side Workspace policy (personal, or a managed account
  with verified policy — never unknown). next_due_date is anchored to the
  end-to-end canary's own last_observed_date — never to the API read alone,
  so refreshing the weaker control cannot advance the deadline. Nothing here
  fails purely by wall-clock passage (that would break unrelated PRs); the
  wall-clock half of the contract is the scheduled
  external-access-staleness.yml workflow, which consumes next_due_date.
  """
  report = yaml.safe_load(
      (DASHBOARD / "bindings/report_template.yaml").read_text()
  )
  attestation = report["external_access_verification"]
  controls = {control["method"]: control for control in attestation["controls"]}
  assert set(controls) == {
      "permissions_api_link_role_check",
      "external_identity_link_access_check",
  }
  today = datetime.date.today()

  api_check = controls["permissions_api_link_role_check"]
  assert (
      api_check["limitation"] == "does_not_expose_viewer_copy_disable_control"
  )
  assert api_check["last_result"] in {
      "LINK_VIEWER_ALLUSERS_PRESENT",
      "LINK_VIEWER_ALLUSERS_ABSENT",
  }
  api_observed = datetime.date.fromisoformat(api_check["last_observed_date"])
  assert api_observed <= today, "an observation cannot be dated in the future"

  canary = controls["external_identity_link_access_check"]
  assert "c.explain=true" in canary["protocol"]
  assert canary["last_result"] in {"PASSED", "FAILURE_REPORTED"}
  assert canary["last_identity_class"] in {
      "personal",
      "managed_verified_policy",
      "unknown",
  }
  canary_observed = datetime.date.fromisoformat(canary["last_observed_date"])
  assert (
      canary_observed <= today
  ), "an observation cannot be dated in the future"
  assert attestation["cadence"] == "monthly_manual_until_automated"

  tracking_issue = attestation["tracking_issue"]
  tracked_entries = [
      entry
      for entry in report["known_live_issues"]
      if entry["issue"] == tracking_issue
  ]
  assert tracked_entries, (
      "incident history is durable: the tracked issue must keep at least"
      " one known_live_issues entry (transition it to RESOLVED, never"
      " delete it) — otherwise recovery becomes provable by erasing the"
      " outage"
  )
  for entry in tracked_entries:
    assert entry["status"] in {"OPEN", "RESOLVED"}
  open_tracked = any(entry["status"] == "OPEN" for entry in tracked_entries)
  assert attestation["status"] in {"PASSING", "FAILING"}
  if attestation["status"] == "PASSING":
    assert canary["last_result"] == "PASSED"
    assert api_check["last_result"] == "LINK_VIEWER_ALLUSERS_PRESENT"
    assert api_observed == canary_observed, (
        "PASSING needs the link-role evidence from the same attestation"
        " date as the canary: a stale LINK_VIEWER read cannot vouch for a"
        " fresh copy"
    )
    assert canary["last_identity_class"] in {
        "personal",
        "managed_verified_policy",
    }, (
        "a PASSING canary must rule out recipient-side Workspace policy:"
        " use a personal account or a managed account with recorded policy"
    )
    verified = datetime.date.fromisoformat(canary["link_access_verified_date"])
    assert verified == canary_observed, (
        "a PASSING canary's verification date is its observation date —"
        " they cannot diverge"
    )
    published = datetime.date.fromisoformat(report["published_date"])
    assert verified >= published
    assert not open_tracked, (
        "PASSING contradicts an OPEN live issue: resolve the"
        " known_live_issues entry (or reopen the investigation) before"
        " flipping the status"
    )
    for entry in tracked_entries:
      incident = datetime.date.fromisoformat(entry["reported_date"])
      resolution = datetime.date.fromisoformat(
          entry["resolution_observed_date"]
      )
      assert incident <= resolution == verified, (
          "a RESOLVED incident must carry the passing canary's date as its"
          " explicit ordered recovery marker, no earlier than the incident:"
          f" reported {incident}, resolution {resolution}, canary {verified}"
      )
  else:
    assert canary["last_result"] == "FAILURE_REPORTED"
    assert canary["link_access_verified_date"] is None
    assert tracking_issue
    assert (
        open_tracked
    ), "a FAILING external-access status must be an OPEN known live issue"

  next_due = datetime.date.fromisoformat(attestation["next_due_date"])
  assert (
      canary_observed
      < next_due
      <= canary_observed + datetime.timedelta(days=35)
  ), (
      "next_due_date must schedule the next end-to-end canary run within the"
      " monthly cadence of the canary's own last observation — refreshing"
      " the Permissions API read alone must not advance the deadline"
  )


def test_staleness_check_consumes_the_attestation_deadline():
  """#445: the wall-clock half of the cadence contract must stay wired.

  The unit tests above never compare attestation dates to today, so the
  monthly cadence only recurs if the scheduled workflow actually runs the
  staleness script against next_due_date. Pin all three layers:

  - the script's stdlib field extraction agrees with a real YAML parse (the
    script deliberately avoids installing PyYAML in the scheduled job, so
    an attestation restructuring must fail here, not misparse there);
  - the script's verdicts flip exactly at the deadline;
  - the workflow, parsed structurally rather than substring-matched, has an
    active cron schedule, a read-only job whose executable step invokes the
    script and installs nothing, and a separate issue-writing job gated on
    the overdue output that performs no checkout and no package downloads.
  """
  spec = importlib.util.spec_from_file_location(
      "check_external_access_staleness",
      ROOT / "scripts" / "check_external_access_staleness.py",
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)

  attestation_text = (DASHBOARD / "bindings/report_template.yaml").read_text()
  attestation = yaml.safe_load(attestation_text)["external_access_verification"]
  fields = module.read_attestation_fields(attestation_text)
  assert fields == {
      "next_due_date": attestation["next_due_date"],
      "status": attestation["status"],
      "tracking_issue": attestation["tracking_issue"],
  }, "the script's stdlib extraction drifted from the YAML structure"

  next_due = datetime.date.fromisoformat(fields["next_due_date"])
  code, message = module.staleness(fields, next_due)
  assert code == 0 and "current" in message
  code, message = module.staleness(
      fields, next_due + datetime.timedelta(days=1)
  )
  assert code == 1
  assert "OVERDUE" in message
  assert "external_identity_link_access_check" in message
  assert fields["tracking_issue"] in message

  workflow = yaml.safe_load(
      (
          ROOT / ".github" / "workflows" / "external-access-staleness.yml"
      ).read_text()
  )
  # PyYAML reads the bare `on:` key as boolean True (YAML 1.1).
  triggers = workflow.get("on", workflow.get(True))
  assert triggers["schedule"], "the staleness check must run on a schedule"
  assert all("cron" in entry for entry in triggers["schedule"])

  check_job = workflow["jobs"]["check"]
  assert check_job["permissions"] == {"contents": "read"}
  check_runs = [step["run"] for step in check_job["steps"] if "run" in step]
  assert any(
      "scripts/check_external_access_staleness.py" in run for run in check_runs
  ), "the read-only job must execute the staleness script"
  assert not any(
      "pip install" in run for run in check_runs
  ), "the scheduled jobs must not resolve mutable package dependencies"

  report_job = workflow["jobs"]["report"]
  assert report_job["permissions"] == {"issues": "write"}
  assert report_job["needs"] == "check"
  assert "overdue" in report_job["if"]
  report_runs = [step["run"] for step in report_job["steps"] if "run" in step]
  assert len(report_job["steps"]) == len(report_runs), (
      "the issue-writing job must run no actions: no checkout, no package"
      " downloads — only the gh mutation"
  )
  assert any(
      "gh issue create" in run and "exit 1" in run for run in report_runs
  ), "the overdue path must open the tracking issue and fail the run"


def test_docs_name_the_terminal_report_not_shared_dialog():
  """#445 acceptance: every user-facing surface names the terminal denial.

  The configurator and both manuals must quote the dialog, attribute it to
  the shared template's access (not the user's setup), and must not fold it
  into the wait-it-out guidance written for the #398 provisioning flicker.
  Each surface must also keep the substance of the guidance, not just the
  quote: the dialog does not resolve by waiting, reporting surfaces must
  forbid posting account identifiers, and the pre-#446 wording that asked
  for the selected account must never come back.
  """
  fragments = ("This report isn", "shared with you")
  no_wait_guidance = re.compile(
      r"not (?:resolve|fix)\w* by waiting"
      r"|do not wait it out"
      r"|waiting will not fix"
      r"|never resolves by waiting"
  )
  privacy_prohibition = re.compile(
      r"(?:do not|never) post the account[’']s email address"
  )
  surfaces = (
      "docs/index.html",
      "docs/app.mjs",  # the dynamic status a user watches after clicking
      "README.md",
      "USER_MANUAL.md",
  )
  reporting_surfaces = {"docs/index.html", "README.md", "USER_MANUAL.md"}
  for relative in surfaces:
    # Collapse line wrapping and JS string-concat breaks ('" + "') so the
    # guidance may reflow across source lines.
    text = " ".join(
        re.sub(r'"\s*\+\s*"', "", (DASHBOARD / relative).read_text()).split()
    )
    for fragment in fragments:
      assert fragment in text, f"{relative} must quote the dialog verbatim"
    assert no_wait_guidance.search(text), (
        f"{relative} must say the terminal dialog is not resolved by"
        " waiting or retrying"
    )
    assert "account the dialog selected" not in text, (
        f"{relative} must not solicit the selected account: reporter"
        " identifiers are redacted per Publication safety"
    )
    if relative in reporting_surfaces:
      assert privacy_prohibition.search(
          text
      ), f"{relative} must forbid posting the account's email address"
      assert (
          "personal or part of an organization" in text
      ), f"{relative} must ask only for non-identifying account context"

  page = (DASHBOARD / "docs/index.html").read_text()
  assert 'id="report-not-shared"' in page
  assert page.count('href="#report-not-shared"') >= 2, (
      "both wait-it-out notes must distinguish the terminal dialog from the"
      " provisioning flicker"
  )
  assert "issues/445" in page
  styles = (DASHBOARD / "docs/styles.css").read_text()
  assert re.search(
      r"#report-not-shared\s*\{[^}]*font-size:\s*1rem", styles
  ), "the recovery guidance must render as body text, not fine print"


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
  # The self-test's negative fixtures — including
  # nonzero-exit-after-healthy-DOM, the delayed error that only the live
  # marker reflects (the 5 s virtual-time budget is the observation
  # window), and the server-startup fixtures (a non-bind failure must not
  # be retried, a pinned port must stay single-attempt, an alive-but-
  # unready child must fail immediately) — must be enforced by the
  # required Test checks, not only by the optional standalone smoke job:
  # a reintroduced false-pass or masked-failure path has to turn a
  # REQUIRED check red. The self-test also proves the positive side: a
  # real bind collision on the first attempt is retried on a fresh port
  # and the whole check still passes.
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


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_browser_smoke_rejects_invalid_readiness_budget(value):
  # The self-test lowers SMOKE_READY_POLLS for one fixture. A bad value
  # must fail fast with its own message (before any browser or server
  # starts), not as a misleading readiness timeout.
  result = subprocess.run(
      ["bash", "tools/browser_smoke.sh"],
      cwd=DASHBOARD,
      env={**os.environ, "SMOKE_READY_POLLS": value},
      capture_output=True,
      text=True,
      timeout=30,
  )
  assert result.returncode == 1
  assert "SMOKE_READY_POLLS must be a positive integer" in result.stderr


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
  # #448: one fully-qualified-table-ID entrance, no separate project or
  # dataset fields, and the paste affordance stays advertised.
  assert 'id="table-id"' in page
  assert 'id="project"' not in page
  assert 'id="dataset"' not in page
  assert (
      "Paste a fully qualified table ID or a BigQuery Console table link"
      in " ".join(page.split())
  )
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
  assert "path: dashboards/looker_studio/docs" in workflow
  assert "pages: write" in workflow
  assert "id-token: write" in workflow
  assert (
      "actions/deploy-pages@"
      "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
  )
  workflow_yaml = yaml.safe_load(workflow)
  assert (
      workflow_yaml["jobs"]["deploy"].get("if")
      == "github.ref == 'refs/heads/main'"
  ), "P1-4: deploy job must be gated to refs/heads/main"


# BQCA Prompt & Response Logging (dashboard Slice 1): a second profile over
# the shared, published template plus a BQCA reporting query.

BQCA_EVENT_TYPES = (
    "INVOCATION_STARTING",
    "USER_MESSAGE_RECEIVED",
    "AGENT_RESPONSE",
    "INVOCATION_COMPLETED",
    "LLM_RESPONSE",
    "EMBEDDING_SUGGESTION",
    "INVOCATION_ERROR",
    "AGENT_ERROR",
    "LLM_ERROR",
)
# #310: BQCA never logs these, so no BQCA artifact may name them.
BQCA_UNLOGGED_EVENT_TYPES = (
    "TOOL_STARTING",
    "TOOL_COMPLETED",
    "TOOL_ERROR",
    "LLM_REQUEST",
)
BQCA_SQL_FILES = (
    "sql/bqca_events_v1.sql.tmpl",
    "sql/bqca_events_v1.template.sql",
    "sql/bqca_preflight.sql.tmpl",
    "sql/bqca_preflight.template.sql",
)
BQCA_REPORT_ID = "42e79a1f-a979-4de8-911f-80d5a99543d5"
BQCA_REPORT_URL = f"https://lookerstudio.google.com/reporting/{BQCA_REPORT_ID}"
# SHA-256 of the retired BQCA report ID, whose organization allowed sharing
# only inside the organization. Stored as a digest so the test that bans the
# ID does not reintroduce it.
RETIRED_BQCA_REPORT_ID_SHA256 = (
    "46a7af104c8dc574723ce15ef4a0d8667efd22fa3c6c38267840abde407b1809"
)
BQCA_DATASOURCE_ID = "70e5ac1e-65bb-4c89-a9df-fa9990cb6029"
# SHA-256 of the BlockDatasource ID recorded on the source report before the
# copy's own embedded data source was recorded. Stored as a digest so the test
# that bans the ID does not reintroduce it.
RETIRED_BQCA_DATASOURCE_ID_SHA256 = (
    "dab46990e866e28766d6d686428437120f5ba12773da5e4894e9f0d131213fa2"
)
# The manifest and JSON bundle digests that were pinned when the live template
# was last verified (2026-10-09). The repository has changed both since, so
# `live_template_verification.pending_live_sync` records the live sync that is
# still to happen, tracked on BQCA_LIVE_SYNC_ISSUE.
BQCA_LIVE_VERIFIED_MANIFEST_SHA256 = (
    "a6b4effd99d59097d3c014cf6242a4f2750da8622e7ad71a03b8f5585a486054"
)
BQCA_LIVE_VERIFIED_BUNDLE_SHA256 = (
    "2631f878a9c382b0a81ab94347b434a7a8666fa2eae75844c86fca48216eb347"
)
BQCA_LIVE_SYNC_ISSUE = (
    "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
    "/issues/559"
)
BQCA_DATASOURCE_ALIAS = "ds0"
BQCA_DEFAULT_TABLE = "bqca_prompt_response_logs"
SHARED_SENTINELS = (
    "test-project-0728-467323",
    "bqaa_fixture_adk_1_27_0",
    "sentinelbqaaevents",
)
# The Looker Studio query reads both date parameters itself; a parameter that
# was replaced by '' left PARSE_DATE('%Y%m%d', '') in the published data
# source, which BigQuery rejects (the report showed "User Configuration
# Error").
BQCA_EVENTS_SQL_FILES = (
    "sql/bqca_events_v1.sql.tmpl",
    "sql/bqca_events_v1.template.sql",
)
BQCA_DATE_PARAMETERS = ("DS_START_DATE", "DS_END_DATE")
BQCA_EMPTY_DATE_LITERAL = "PARSE_DATE('%Y%m%d', '')"
# The embedded BlockDatasource's date fields, as verified against the live
# data source after the 2026-10-08 republish.
BQCA_BLOCK_DATASOURCE_FIELDS = {
    "_event_date_": {"data_type": "DATE", "semantic_type": "YEAR_MONTH_DAY"},
    "_timestamp_": {
        "data_type": "TIMESTAMP",
        "semantic_type": "YEAR_MONTH_DAY_SECOND",
    },
    "_event_hour_": {
        "data_type": "TIMESTAMP",
        "semantic_type": "YEAR_MONTH_DAY_HOUR",
    },
}


def _web_report_config():
  source = (DASHBOARD / "docs/report-config.mjs").read_text()
  payload = source.split("Object.freeze(", 1)[1].rsplit(");", 1)[0]
  return json.loads(payload)


def _link_parameters(link):
  parsed = urllib.parse.urlparse(link)
  assert (parsed.scheme, parsed.netloc, parsed.path) == (
      "https",
      "lookerstudio.google.com",
      "/reporting/create",
  )
  return urllib.parse.parse_qs(parsed.query)


def _fake_bq_query(responses):
  """Return (fake bq_query, recorded calls) answering from `responses`."""
  calls = []

  def fake(project, location, sql, maximum_bytes_billed=None):
    calls.append(
        {
            "project": project,
            "location": location,
            "sql": sql,
            "maximum_bytes_billed": maximum_bytes_billed,
        }
    )
    response = responses[len(calls) - 1]
    if isinstance(response, Exception):
      raise response
    return response

  return fake, calls


def _profile_rows(events, attributed, fast_path=0):
  rows = [
      {
          "event_type": event_type,
          "events_30d": "0",
          "events_with_data_agent_id": "0",
          "data_agent_id_coverage": None,
          "fast_path_events": "0",
      }
      for event_type in BQCA_EVENT_TYPES
  ]
  rows[0].update(
      events_30d=str(events),
      events_with_data_agent_id=str(attributed),
      fast_path_events=str(fast_path),
  )
  return rows


def test_bqca_generated_artifacts_cannot_drift(tmp_path):
  generator = _load_dashboard_module("gen_bqca_events_tmpl")
  renderer = _load_dashboard_module("render_template")
  web = _load_dashboard_module("render_web_config")
  validator = _load_dashboard_module("validate_contracts")

  assert generator.BQCA_EVENT_TYPES == BQCA_EVENT_TYPES
  logical = generator.generate()
  assert logical == (DASHBOARD / "sql/bqca_events_v1.sql.tmpl").read_text()
  assert generator.main(["--check"]) == 0
  stale = tmp_path / "bqca_events_v1.sql.tmpl"
  stale.write_text(logical + "-- hand edit\n")
  assert generator.main(["--check", "--output", str(stale)]) == 1

  bqca_bindings = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_template_bindings.yaml").read_text()
  )["placeholders"]
  expected = {
      "sql/bqca_events_v1.template.sql": renderer.render_text(
          logical, bqca_bindings, "sql/bqca_events_v1.sql.tmpl"
      ),
      "sql/bqca_preflight.template.sql": renderer.render_text(
          (DASHBOARD / "sql/bqca_preflight.sql.tmpl").read_text(),
          bqca_bindings,
          "sql/bqca_preflight.sql.tmpl",
      ),
  }
  assert renderer.render_profile("bqca") == expected
  for path, rendered in expected.items():
    assert (DASHBOARD / path).read_text() == rendered, path
  # The profile split leaves the ADK pairs, and their outputs, untouched.
  assert renderer.PAIRS == renderer.PROFILES["adk"]["pairs"]
  for path, rendered in renderer.render_profile("adk").items():
    assert (DASHBOARD / path).read_text() == rendered, path
  assert renderer.main(["--check"]) == 0
  assert renderer.main(["--profile", "bqca", "--check"]) == 0

  for path, rendered in web.outputs().items():
    assert (DASHBOARD / path).read_text() == rendered, path
  assert web.main(["--check"]) == 0
  assert web.main(["--profile", "bqca", "--check"]) == 0
  assert web.main(["--profile", "adk", "--check"]) == 0
  assert web.main(["--profile", "all", "--check"]) == 0

  assert validator.main(["--profile", "bqca"]) == 0
  assert validator.main(["--profile", "adk"]) == 0
  assert validator.main(["--profile", "all"]) == 0


def test_bqca_profile_reuses_the_published_template_bindings():
  shared = yaml.safe_load(
      (DASHBOARD / "bindings/report_template.yaml").read_text()
  )
  bqca = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  adk_placeholders = yaml.safe_load(
      (DASHBOARD / "bindings/template_bindings.yaml").read_text()
  )["placeholders"]
  bqca_placeholders = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_template_bindings.yaml").read_text()
  )["placeholders"]

  # BQCA uses its own dedicated 7-page tool-free report template and ds0 alias
  # while keeping the same three sentinel strings in sql/bqca_events_v1.template.sql.
  assert "shared_template" not in bqca
  assert bqca["report_id"] == BQCA_REPORT_ID
  assert bqca["report_id"] != shared["report_id"]
  assert bqca["data_source_alias"] == BQCA_DATASOURCE_ALIAS
  assert bqca["datasource_id"] == BQCA_DATASOURCE_ID
  assert bqca["product_contract"] == "spec/bqca_product_contract.yaml"
  assert bqca["chart_manifest"] == "spec/bqca_chart_manifest.yaml"
  assert bqca["link_access"] == "PUBLIC"
  assert bqca["publishing_mode"] == "MANUAL"
  assert (
      bqca["generated_report_credential_gate"]
      == shared["generated_report_credential_gate"]
  )
  assert bqca["source_contract"] == shared["source_contract"]
  assert bqca["governance"] == shared["governance"]
  assert bqca["default_date_range"] == shared["default_date_range"]
  assert bqca["product_verification"]["pages"] == 7
  assert bqca["product_verification"]["component_count"] == 34
  assert bqca["product_verification"]["scorecard_count"] == 21
  assert bqca["product_verification"]["chart_count"] == 13
  assert bqca["product_verification"]["result"] == "PASSED"
  assert bqca_placeholders == adk_placeholders
  assert (
      tuple(bqca_placeholders[name] for name in ("PROJECT", "DATASET", "TABLE"))
      == SHARED_SENTINELS
  )
  assert (
      bqca["default_report_name"] == "BigQuery Conversational Analytics (BQCA)"
  )
  assert bqca["default_table"] == BQCA_DEFAULT_TABLE
  assert bqca["datasource_name_prefix"] == "BQCA"
  assert bqca["custom_query_template"] == "sql/bqca_events_v1.template.sql"
  assert (DASHBOARD / bqca["custom_query_template"]).is_file()
  assert bqca["reviewed_template_sql"] == {
      "path": "sql/bqca_events_v1.template.sql",
      "sha256": hashlib.sha256(
          (DASHBOARD / "sql/bqca_events_v1.template.sql").read_bytes()
      ).hexdigest(),
  }

  contract = yaml.safe_load((DASHBOARD / bqca["product_contract"]).read_text())
  assert contract["surface"]["canonical_report_id"] == BQCA_REPORT_ID
  assert contract["surface"]["datasource_id"] == BQCA_DATASOURCE_ID
  assert contract["surface"]["data_source_alias"] == BQCA_DATASOURCE_ALIAS
  assert (
      contract["surface"]["source_parity_contract"]
      == "spec/bqca_chart_manifest.yaml"
  )
  assert tuple(contract["allowed_event_types"]) == BQCA_EVENT_TYPES
  assert [page["id"] for page in contract["pages"]] == [
      "p_539b9240",
      "p_a89cfece",
      "p_97efe693",
      "p_edf06c14",
      "p_08774fec",
      "p_88bcf5f8",
      "p_b87a335e",
  ]
  assert sum(page["component_count"] for page in contract["pages"]) == 34
  assert contract["excluded_adk_surfaces"]["tool_pages_included"] is False
  assert contract["excluded_adk_surfaces"]["tool_scorecards_included"] is False
  assert contract["excluded_adk_surfaces"]["tool_charts_included"] is False

  web = _web_report_config()
  assert list(web["profiles"]) == ["adk", "bqca"]
  adk_profile = web["profiles"]["adk"]
  for key in ("reportId", "dataSourceAlias", "sentinels", "defaultTable"):
    assert web[key] == adk_profile[key], key
  assert adk_profile["reportName"] == shared["default_report_name"]
  assert adk_profile["datasourceName"] == "BQAA"
  assert web["profiles"]["bqca"] == {
      "id": "bqca",
      "label": "BQCA Prompt & Response Logging",
      "reportId": bqca["report_id"],
      "dataSourceAlias": bqca["data_source_alias"],
      "sentinels": {
          "project": bqca_placeholders["PROJECT"],
          "dataset": bqca_placeholders["DATASET"],
          "table": bqca_placeholders["TABLE"],
      },
      "defaultTable": bqca["default_table"],
      "reportName": bqca["default_report_name"],
      "datasourceName": bqca["datasource_name_prefix"],
  }

  # The Python tools resolve the same files for each profile.
  hydration = _load_hydration_module()
  renderer = _load_dashboard_module("render_template")
  for profile in ("adk", "bqca"):
    assert (
        hydration.PROFILES[profile]["bindings"]
        == renderer.PROFILES[profile]["bindings"]
    )
  assert hydration.PROFILES["bqca"]["report"] == (
      "bindings/bqca_report_template.yaml"
  )


def test_bqca_surfaces_link_the_same_report_url():
  contract = yaml.safe_load(
      (DASHBOARD / "spec/bqca_product_contract.yaml").read_text()
  )
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  bundle = json.loads(
      (DASHBOARD / "spec/bqca_dashboard_template.json").read_text()
  )
  assert contract["surface"]["canonical_report_url"] == BQCA_REPORT_URL
  assert manifest["meta"]["report_id"] == BQCA_REPORT_ID
  assert manifest["meta"]["dashboard_url"] == BQCA_REPORT_URL
  assert bundle["dashboard_url"] == BQCA_REPORT_URL
  for relative in ("README.md", "docs/index.html", "docs/bqca/index.html"):
    assert BQCA_REPORT_URL in (DASHBOARD / relative).read_text(), relative


def test_retired_bqca_report_id_appears_nowhere_in_the_repository():
  """No tracked file may point at the retired BQCA report.

  Its organization allowed sharing only inside the organization, so external
  accounts could never copy it; a leftover reference in a doc, spec,
  generated configurator file, or test would send someone back to it.
  """
  listing = subprocess.run(
      ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
  ).stdout
  uuid = re.compile(rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
  hits = []
  for name in filter(None, listing.split(b"\0")):
    path = ROOT / os.fsdecode(name)
    if not path.is_file():
      continue
    for token in set(uuid.findall(path.read_bytes())):
      digest = hashlib.sha256(token.lower()).hexdigest()
      if digest == RETIRED_BQCA_REPORT_ID_SHA256:
        hits.append(os.fsdecode(name))
  assert not hits, f"retired BQCA report ID still referenced in {hits}"


def test_retired_bqca_datasource_id_appears_nowhere_in_the_repository():
  """No tracked file may name the source report's BlockDatasource.

  The live template embeds its own data source; a leftover reference to the
  one recorded on the source report would put the binding, specs, and live
  report out of step again.
  """
  listing = subprocess.run(
      ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
  ).stdout
  uuid = re.compile(rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
  hits = []
  for name in filter(None, listing.split(b"\0")):
    path = ROOT / os.fsdecode(name)
    if not path.is_file():
      continue
    for token in set(uuid.findall(path.read_bytes())):
      digest = hashlib.sha256(token.lower()).hexdigest()
      if digest == RETIRED_BQCA_DATASOURCE_ID_SHA256:
        hits.append(os.fsdecode(name))
  assert not hits, f"retired BQCA datasource ID still referenced in {hits}"


def test_bqca_sharing_records_owner_public_link_but_no_outside_check():
  """Public link sharing is on, but no outside account has verified it.

  On 2026-10-09 the owner turned on link sharing for anyone with the link.
  link_access records that owner-side intent, as for the ADK template; only
  the external-identity copy canary shows that an outside account can copy
  the report, and it has not run, so nothing may claim verified access.
  """
  bqca = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  assert bqca["link_access"] == "PUBLIC"
  attestation = bqca["external_access_verification"]
  assert attestation["status"] == "PENDING_EXTERNAL_IDENTITY_CHECK"
  controls = {c["method"]: c for c in attestation["controls"]}
  assert set(controls) == {
      "permissions_api_link_role_check",
      "external_identity_link_access_check",
  }
  for control in controls.values():
    assert control["last_result"] == "NOT_RUN"
    assert control["last_observed_date"] is None
  canary = controls["external_identity_link_access_check"]
  assert canary["link_access_verified_date"] is None
  assert canary["last_identity_class"] == "unknown"
  assert [
      issue["status"]
      for issue in bqca["known_live_issues"]
      if issue["issue"] == attestation["tracking_issue"]
  ] == ["OPEN"]

  # The user-facing copy agrees: sharing turned on 2026-10-09, copy check
  # still pending, and no trace of the retired allowlist state.
  documents = [DASHBOARD / "README.md", DASHBOARD / "USER_MANUAL.md"]
  documents += [
      DASHBOARD / "docs" / name
      for name in (
          "index.html",
          "bqca/index.html",
          "dashboard-implementation.md",
      )
  ]
  documents.append(ROOT / "docs/guides/bqca-prompt-response-logging-manual.md")
  for path in documents:
    text = " ".join(path.read_text().split())
    assert "2026-10-09" in text, path
    assert "copy check" in text, path
    for stale in (
        "PENDING_PUBLIC_SHARING_ALLOWLIST",
        "allowlist approval",
        "allowlisting",
    ):
      assert stale not in text, f"{path} still says {stale!r}"


def test_bqca_query_reads_only_bqca_events_with_canonical_extractions():
  logical = (DASHBOARD / "sql/bqca_events_v1.sql.tmpl").read_text()
  rendered = (DASHBOARD / "sql/bqca_events_v1.template.sql").read_text()
  adk = (DASHBOARD / "sql/events_v1.sql.tmpl").read_text()

  # One base-table scan inside the ADK query's exact UTC date window,
  # restricted to the nine event types BQCA logs.
  assert logical.count("FROM `{{PROJECT}}.{{DATASET}}.{{TABLE}}`") == 1
  window = (
      "WHERE timestamp >= TIMESTAMP( PARSE_DATE('%Y%m%d', @DS_START_DATE),"
      " 'UTC' ) AND timestamp < TIMESTAMP( DATE_ADD( PARSE_DATE('%Y%m%d',"
      " @DS_END_DATE), INTERVAL 1 DAY ), 'UTC' )"
  )
  assert window in " ".join(logical.split())
  assert window in " ".join(adk.split())
  filters = re.findall(r"AND event_type IN \(([^)]*)\)", logical)
  assert len(filters) == 1
  assert tuple(re.findall(r"'([A-Z_]+)'", filters[0])) == BQCA_EVENT_TYPES

  # #125: attribution reads session state, never agent/user_id/session_id.
  assert (
      "JSON_VALUE(attributes, '$.session_metadata.state.\"data-agent-id\"')"
      in logical
  )
  assert (
      "JSON_VALUE(attributes, '$.session_metadata.state.\"conversation-id\"')"
      in logical
  )
  # #153: an error is any of three signals, never status = 'ERROR' alone.
  assert (
      "IFNULL(UPPER(TRIM(status)) = 'ERROR', FALSE)\n"
      "      OR NULLIF(TRIM(error_message), '') IS NOT NULL\n"
      "      OR ENDS_WITH(UPPER(TRIM(IFNULL(event_type, ''))), '_ERROR')"
  ) in logical
  # #155: every markdown part of the response, in order, and a persona
  # only from an explicit label or a well-formed email local part.
  assert "JSON_QUERY_ARRAY(content, '$.response.parts')" in logical
  assert "ORDER BY part_offset" in logical
  assert "'\\n\\n'" in logical
  assert "custom_labels.persona" in logical
  assert "REGEXP_EXTRACT(\n        TRIM(user_id)," in logical
  assert r"r'^([A-Za-z0-9._%+-]+)@" in logical
  assert "LIKE '%@%'" not in logical
  assert "SPLIT(user_id" not in logical
  assert "'unattributed'" in logical

  for column in (
      "event_date",
      "event_hour",
      "data_agent_id",
      "conversation_id",
      "persona",
      "fast_path",
      "is_error",
      "is_turn_start",
      "is_turn_complete",
      "completed_turn_id",
      "total_latency_ms",
      "ttft_ms",
      "turn_latency_ms",
      "llm_latency_ms",
      "model_name",
      "model_version",
      "input_tokens",
      "output_tokens",
      "thoughts_tokens",
      "cached_tokens",
      "total_tokens",
      "user_prompt_text",
      "agent_response_text",
      "similar_queries_count",
      "embedding_suggestion_reason",
      "fast_path_label",
      "is_embedding_hit",
      "extracted_sql",
      "summary_text",
  ):
    assert re.search(rf"\bAS {column}\b", logical), column
  assert "tfft_ms" not in logical

  # F1: user_prompt_text joins all text parts in offset order BEFORE falling
  # back to $.parts[0].text so multipart user messages are never truncated.
  prompt_coalesce = logical.split(
      "event_type IN ('USER_MESSAGE_RECEIVED', 'INVOCATION_STARTING')", 1
  )[1].split("AS user_prompt_text", 1)[0]
  assert prompt_coalesce.index(
      "JSON_QUERY_ARRAY(content, '$.parts')"
  ) < prompt_coalesce.index("JSON_VALUE(content, '$.parts[0].text')")

  # P1-B & R2-P3-3: EMBEDDING_SUGGESTION emits {reason, suggested_columns} at
  # HEAD, so $.suggested_columns must appear FIRST in the COALESCE for
  # similar_queries_count, ahead of $.similar_queries_count and $.suggestions,
  # with a trailing 0 fallback so reason-only EMBEDDING_SUGGESTION rows emit 0.
  similar_coalesce = logical.split("event_type = 'EMBEDDING_SUGGESTION'", 1)[
      1
  ].split("AS similar_queries_count", 1)[0]
  assert (
      similar_coalesce.index("$.suggested_columns")
      < similar_coalesce.index("$.similar_queries_count")
      < similar_coalesce.index("$.suggestions")
  )
  assert re.search(r",\s*0\s*\)", similar_coalesce), similar_coalesce

  # R2-P2-3 & R3-P1-3: is_turn_complete, completed_turn_id, and turn_latency_ms
  # deduplicate multiple INVOCATION_COMPLETED rows per non-blank invocation_id
  # via raw_turn_complete_rn = 1.
  assert "AS raw_turn_complete_rn" in logical
  assert (
      "IF(raw_turn_complete_rn = 1, turn_id, NULL) AS completed_turn_id"
      in logical
  )
  assert (
      "IFNULL(raw_turn_complete_rn = 1, FALSE) AS is_turn_complete" in logical
  )
  assert re.search(
      r"ORDER BY\s+SAFE_CAST\(JSON_VALUE\(latency_ms,"
      r" '\$\.total_ms'\) AS FLOAT64\) DESC NULLS LAST,\s+timestamp"
      r" DESC,\s+span_id",
      logical,
  )

  # P2-2: generator docstring reflects that rejected drafts are suppressed and
  # a second AGENT_RESPONSE only happens when an accepted draft is discarded
  # by a workflow nudge.
  gen_source = (DASHBOARD / "tools/gen_bqca_events_tmpl.py").read_text()
  assert "Rejected drafts are suppressed" in gen_source
  assert "workflow nudge" in gen_source
  assert "including rejected drafts" not in gen_source

  # The rendered template binds the three shared sentinels exactly once.
  assert rendered.startswith(
      "-- GENERATED by tools/render_template.py from"
      " sql/bqca_events_v1.sql.tmpl — do not hand-edit.\n"
  )
  assert "{{" not in rendered
  assert rendered.count("FROM `{}.{}.{}`".format(*SHARED_SENTINELS)) == 1

  # The data profile runs through the bq CLI, so it takes no Looker Studio
  # date parameters, and it reports every allowlisted type, zero or not.
  profile = (DASHBOARD / "sql/bqca_preflight.sql.tmpl").read_text()
  assert profile.count("FROM `{{PROJECT}}.{{DATASET}}.{{TABLE}}`") == 1
  assert "@DS_" not in profile
  assert "INTERVAL 30 DAY" in profile
  assert "LEFT JOIN recent" in profile
  allowlist = re.search(r"UNNEST\(\[([^\]]*)\]\)", profile)
  assert allowlist
  assert tuple(re.findall(r"'([A-Z_]+)'", allowlist.group(1))) == (
      BQCA_EVENT_TYPES
  )
  for name in (
      "events_30d",
      "events_with_data_agent_id",
      "data_agent_id_coverage",
      "fast_path_events",
  ):
    assert f"AS {name}" in profile, name


def _bqca_date_window_defects(sql):
  """Return how `sql` misreads the Looker Studio date window (empty if right).

  The query must parse exactly the `@DS_START_DATE` and `@DS_END_DATE`
  parameters with PARSE_DATE('%Y%m%d', ...) and read no other `@DS_`
  parameter. The text is checked as written, comments included.
  """
  defects = []
  if re.search(
      r"PARSE_DATE\s*\([^()]*,\s*(?:''|\"\")\s*\)", sql, re.IGNORECASE
  ):
    defects.append("PARSE_DATE parses an empty string literal")
  calls = sorted(
      re.sub(r"\s+", "", call)
      for call in re.findall(r"PARSE_DATE\s*(\([^()]*\))", sql, re.IGNORECASE)
  )
  expected = sorted(f"('%Y%m%d',@{name})" for name in BQCA_DATE_PARAMETERS)
  if calls != expected:
    defects.append(f"PARSE_DATE calls {calls} are not exactly {expected}")
  parameters = sorted(set(re.findall(r"@(DS_[A-Z_]+)", sql)))
  if parameters != sorted(BQCA_DATE_PARAMETERS):
    defects.append(
        f"@DS_ parameters {parameters} are not {sorted(BQCA_DATE_PARAMETERS)}"
    )
  return defects


@pytest.mark.parametrize("relative", BQCA_EVENTS_SQL_FILES)
def test_bqca_events_sql_reads_both_date_parameters(relative):
  """The published report failed because both date parameters became ''.

  The embedded data source ran `PARSE_DATE('%Y%m%d', '')` instead of reading
  `@DS_START_DATE`/`@DS_END_DATE`; BigQuery rejects that as an invalid date
  and Looker Studio showed "User Configuration Error". Both the logical
  template and the rendered query that gets published must keep reading the
  parameters.
  """
  sql = (DASHBOARD / relative).read_text()

  assert BQCA_EMPTY_DATE_LITERAL not in sql
  assert _bqca_date_window_defects(sql) == []


BQCA_START_DATE_PARSE = "PARSE_DATE('%Y%m%d', @DS_START_DATE)"
BQCA_END_DATE_PARSE = "PARSE_DATE('%Y%m%d', @DS_END_DATE)"
# Ways the rendered query's date window can break, as replacements applied to
# the real file (each replaced text occurs exactly once in it).
BQCA_BROKEN_DATE_WINDOWS = {
    # The published query as it was found: both parameters blanked.
    "both-parameters-blanked": (
        (BQCA_START_DATE_PARSE, BQCA_EMPTY_DATE_LITERAL),
        (BQCA_END_DATE_PARSE, BQCA_EMPTY_DATE_LITERAL),
    ),
    "start-parameter-blanked": (
        (BQCA_START_DATE_PARSE, BQCA_EMPTY_DATE_LITERAL),
    ),
    "end-parameter-blanked-in-another-spelling": (
        (BQCA_END_DATE_PARSE, 'parse_date(\n    "%Y%m%d",\n    ""\n  )'),
    ),
    "start-date-hard-coded": (
        (BQCA_START_DATE_PARSE, "PARSE_DATE('%Y%m%d', '20200101')"),
    ),
    "end-date-reads-the-start-parameter": (
        (BQCA_END_DATE_PARSE, BQCA_START_DATE_PARSE),
    ),
    "start-bound-dropped": ((BQCA_START_DATE_PARSE, "DATE '2026-01-01'"),),
    "end-date-reads-an-unknown-parameter": (
        (BQCA_END_DATE_PARSE, "PARSE_DATE('%Y%m%d', @DS_OTHER_DATE)"),
    ),
}


@pytest.mark.parametrize(
    "replacements",
    list(BQCA_BROKEN_DATE_WINDOWS.values()),
    ids=list(BQCA_BROKEN_DATE_WINDOWS),
)
def test_bqca_date_window_check_rejects_broken_queries(replacements):
  """The date-window check must fail for each way the window can break."""
  sql = (DASHBOARD / "sql/bqca_events_v1.template.sql").read_text()
  assert _bqca_date_window_defects(sql) == []
  for old, new in replacements:
    assert sql.count(old) == 1, old
    sql = sql.replace(old, new)

  assert _bqca_date_window_defects(sql)


def test_bqca_block_datasource_invariants_and_live_attestation_are_recorded():
  """The published data source's required settings and live proof are pinned.

  Looker Studio runs the embedded query only when the data source declares
  both date parameters and types its date fields natively. The report is
  external and mutable, so both artifacts record those settings and the
  digest of the SQL they were verified against: changing the query, the
  pages, or a setting fails here until the data source is republished,
  verified again, and the records are updated.
  """
  validator = _load_dashboard_module("validate_contracts")
  bqca = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  contract = yaml.safe_load(
      (DASHBOARD / "spec/bqca_product_contract.yaml").read_text()
  )
  manifest_bytes = (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_bytes()
  bundle_bytes = (DASHBOARD / "spec/bqca_dashboard_template.json").read_bytes()
  rendered = (DASHBOARD / "sql/bqca_events_v1.template.sql").read_bytes()
  digest = hashlib.sha256(rendered).hexdigest()
  pages_digest = validator.canonical_pages_sha256(contract["pages"])
  manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
  bundle_digest = hashlib.sha256(bundle_bytes).hexdigest()

  invariants = {
      "use_datetime_type": True,
      "parameter_configuration": list(BQCA_DATE_PARAMETERS),
      "fields": BQCA_BLOCK_DATASOURCE_FIELDS,
  }
  assert bqca["block_datasource"] == invariants
  block = dict(contract["block_datasource"])
  assert block.pop("sql") == {
      "path": "sql/bqca_events_v1.template.sql",
      "forbidden_literals": [BQCA_EMPTY_DATE_LITERAL],
  }
  assert block == invariants
  # The declared parameters are exactly the ones the reviewed query reads.
  assert set(re.findall(r"@(DS_[A-Z_]+)", rendered.decode())) == set(
      bqca["block_datasource"]["parameter_configuration"]
  )

  # The live evidence names the exact SQL, pages, manifest, and JSON bundle it was gathered for.
  assert bqca["reviewed_template_sql"]["sha256"] == digest
  # The manifest and bundle it was gathered for are the `verified_*` digests.
  # Both have changed since, so their pinned digests differ, and a pending
  # live sync is recorded until the live template matches them again.
  evidence = dict(bqca["live_template_verification"])
  pending = evidence.pop("pending_live_sync")
  assert evidence == {
      "verified_date": "2026-10-09",
      "repository_sql_sha256": digest,
      "pages_sha256": pages_digest,
      "manifest_sha256": manifest_digest,
      "template_bundle_sha256": bundle_digest,
      "verified_manifest_sha256": BQCA_LIVE_VERIFIED_MANIFEST_SHA256,
      "verified_template_bundle_sha256": BQCA_LIVE_VERIFIED_BUNDLE_SHA256,
      "method": [
          "data_studio_web_service_publish_datasource",
          "data_studio_web_service_get_block_datasource",
          "lego_midtier_service_execute_query",
          "lego_midtier_service_render_dashboard_pdf",
      ],
      "publish_datasource": {
          "health": "HEALTHY",
          "versions": {
              "hydrated_bqca_table": 1791578476993,
              "canonical_sentinel_table": 1791578549613,
          },
      },
      "execute_query": {
          "hydrated_bqca_table": {
              "schema": "bqaa_base_table_15_columns",
              "date_range": "default_dates",
              "code": 0,
              "pages": [page["id"] for page in contract["pages"]],
              "non_empty": True,
          },
          "canonical_sentinel_table": [
              {"date_range": "default_dates", "code": 0, "size": 0},
              {"date_range": "20200101..20261231", "code": 0, "size": 57891},
          ],
      },
      "render_dashboard_pdf": {
          "hydrated_bqca_table": {
              "code": 0,
              "contains_user_configuration_error": False,
          },
      },
      "result": "PASSED",
      "limitation": "mutable_external_report_requires_reverification_after_changes",
  }
  verified = datetime.date.fromisoformat(evidence["verified_date"])
  assert (
      datetime.date.fromisoformat(bqca["published_date"])
      <= verified
      <= datetime.date.today()
  )
  assert manifest_digest != BQCA_LIVE_VERIFIED_MANIFEST_SHA256
  assert bundle_digest != BQCA_LIVE_VERIFIED_BUNDLE_SHA256
  # The pending sync gives its reason, its start, its tracking issue, and the
  # actions that end it: the exact qt_ IDs, the copy's own data source, and a
  # clean live audit before the record is removed.
  assert list(pending) == [
      "reason",
      "since_date",
      "tracking_issue",
      "required_actions",
  ]
  assert pending["reason"].strip()
  assert pending["since_date"] == "2026-10-29"
  assert (
      verified
      <= datetime.date.fromisoformat(pending["since_date"])
      <= datetime.date.today()
  )
  assert pending["tracking_issue"] == BQCA_LIVE_SYNC_ISSUE
  rename, repoint, reverify = pending["required_actions"]
  assert "exact qt_ IDs in spec/bqca_chart_manifest.yaml" in rename
  assert repoint.endswith(f"datasource_id to {BQCA_DATASOURCE_ID}.")
  assert "--live-report-textproto" in reverify
  assert "README.md" in reverify
  assert "USER_MANUAL.md" in reverify
  assert "docs/guides/bqca-prompt-response-logging-manual.md" in reverify
  assert "#bqca-column-id-note" in reverify
  assert reverify.endswith("remove pending_live_sync.")

  # The contract states the gate to pass after every publish, and points at
  # the evidence of the last run.
  assert contract["live_verification"] == {
      "gate": [
          {
              "rpc": "DataStudioWebService.PublishDatasource",
              "expect": {"health": "HEALTHY"},
          },
          {
              "rpc": "LegoMidtierService.ExecuteQuery",
              "expect": {"code": 0, "pages": "all_report_pages"},
          },
          {
              "rpc": "LegoMidtierService.RenderDashboardPdf",
              "expect": {
                  "code": 0,
                  "contains_user_configuration_error": False,
              },
          },
      ],
      "last_attested_date": contract["live_verification"]["last_attested_date"],
      "evidence": (
          "bindings/bqca_report_template.yaml#live_template_verification"
      ),
  }
  assert (
      datetime.date.fromisoformat(
          contract["live_verification"]["last_attested_date"]
      )
      <= verified
  )

  # External-access attestation in bqca_report_template.yaml is stdlib-parseable
  # by scripts/check_external_access_staleness.py and included in default checks.
  staleness_spec = importlib.util.spec_from_file_location(
      "check_external_access_staleness",
      ROOT / "scripts" / "check_external_access_staleness.py",
  )
  assert staleness_spec is not None and staleness_spec.loader is not None
  staleness_mod = importlib.util.module_from_spec(staleness_spec)
  staleness_spec.loader.exec_module(staleness_mod)
  assert staleness_mod.DEFAULT_ATTESTATION_PATHS == (
      staleness_mod.ATTESTATION_PATH,
      staleness_mod.BQCA_ATTESTATION_PATH,
  )
  bqca_raw = (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  bqca_ext = bqca["external_access_verification"]
  assert staleness_mod.read_attestation_fields(bqca_raw) == {
      "next_due_date": bqca_ext["next_due_date"],
      "status": bqca_ext["status"],
      "tracking_issue": bqca_ext["tracking_issue"],
  }
  assert (
      staleness_mod.main(
          [
              "--today",
              bqca_ext["next_due_date"],
              "--attestation-path",
              str(staleness_mod.BQCA_ATTESTATION_PATH),
          ]
      )
      == 0
  )
  overdue_day = (
      datetime.date.fromisoformat(bqca_ext["next_due_date"])
      + datetime.timedelta(days=1)
  ).isoformat()
  assert (
      staleness_mod.main(
          [
              "--today",
              overdue_day,
              "--attestation-path",
              str(staleness_mod.BQCA_ATTESTATION_PATH),
          ]
      )
      == 1
  )


def test_bqca_chart_manifest_and_contract_mutation_detection():
  """F6 & R2-P3-6: mutating any scorecard, chart, embedded spec, or SQL column fails."""
  validator = _load_dashboard_module("validate_contracts")
  assert validator.validate_bqca() == []

  contract = yaml.safe_load(
      (DASHBOARD / "spec/bqca_product_contract.yaml").read_text()
  )
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  binding = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  sql_text = (DASHBOARD / "sql/bqca_events_v1.template.sql").read_text()
  assert manifest["meta"]["page_count"] == 7
  assert manifest["meta"]["scorecard_count"] == 21
  assert manifest["meta"]["chart_count"] == 13
  assert manifest["meta"]["total_component_count"] == 34
  assert manifest["datasource"]["field_count"] == 41
  assert len(manifest["datasource"]["fields"]) == 41
  assert len(manifest["pages"]) == 7
  assert len(manifest["components"]) == 34

  def _recomputed_binding(m_override=None, c_override=None, s_override=None):
    b_copy = json.loads(json.dumps(binding))
    if m_override is not None:
      b_copy["live_template_verification"]["manifest_sha256"] = hashlib.sha256(
          yaml.safe_dump(m_override, sort_keys=False).encode("utf-8")
      ).hexdigest()
    if c_override is not None:
      b_copy["live_template_verification"]["pages_sha256"] = (
          validator.canonical_pages_sha256(c_override["pages"])
      )
    if s_override is not None:
      s_sha = hashlib.sha256(s_override.encode("utf-8")).hexdigest()
      b_copy["reviewed_template_sql"]["sha256"] = s_sha
      b_copy["live_template_verification"]["repository_sql_sha256"] = s_sha
    return b_copy

  # Mutating a scorecard field in contract["pages"] invalidates pages_sha256
  # and contract-vs-manifest parity.
  mutated_contract = json.loads(json.dumps(contract))
  mutated_contract["pages"][0]["scorecards"][0]["field"] = "input_tokens"
  errors = validator.validate_bqca(contract_override=mutated_contract)
  assert any("pages_sha256" in e for e in errors), errors
  assert any("kpi_total_tokens" in e for e in errors), errors

  # Mutating a chart metric in contract["pages"] invalidates pages_sha256.
  mutated_chart_contract = json.loads(json.dumps(contract))
  mutated_chart_contract["pages"][0]["charts"][0]["metric"] = "input_tokens"
  chart_errors = validator.validate_bqca(
      contract_override=mutated_chart_contract
  )
  assert any("pages_sha256" in e for e in chart_errors), chart_errors

  # Mutating a component title in manifest invalidates manifest_sha256 and
  # contract-vs-manifest parity.
  mutated_manifest = json.loads(json.dumps(manifest))
  mutated_manifest["pages"][0]["components"][0]["label"] = "Tampered Label"
  m_errors = validator.validate_bqca(manifest_override=mutated_manifest)
  assert any("manifest_sha256" in e for e in m_errors), m_errors
  assert any("kpi_total_tokens" in e for e in m_errors), m_errors

  # R2-P3-6 (a): mutating an embedded spec fieldName is caught even when
  # manifest_sha256 is recomputed.
  bad_spec_field = json.loads(json.dumps(manifest))
  bad_spec_field["pages"][0]["components"][0]["spec"]["scorecard"]["dataset"][
      "columns"
  ][0]["fieldName"] = "_nonexistent_field_"
  spec_field_errors = validator.validate_bqca(
      manifest_override=bad_spec_field,
      binding_override=_recomputed_binding(m_override=bad_spec_field),
  )
  assert any(
      "unknown fieldName" in e for e in spec_field_errors
  ), spec_field_errors

  # R2-P3-6 (b): mutating an embedded spec aggregation (on either a scorecard
  # or a chart) is caught even when manifest_sha256 is recomputed.
  bad_spec_agg = json.loads(json.dumps(manifest))
  bad_spec_agg["pages"][0]["components"][5]["spec"]["barChart"]["dataset"][
      "columns"
  ][1]["aggregation"] = "AVG"
  spec_agg_errors = validator.validate_bqca(
      manifest_override=bad_spec_agg,
      binding_override=_recomputed_binding(m_override=bad_spec_agg),
  )
  assert any(
      "chart_tokens_by_agent" in e and "aggregation" in e
      for e in spec_agg_errors
  ), spec_agg_errors

  # R2-P3-6 (c): mutating a manifest field's data_type (e.g. total_tokens from
  # INT64 to STRING) is caught against SQL projected types even when
  # manifest_sha256 is recomputed.
  bad_dtype_manifest = json.loads(json.dumps(manifest))
  for field_entry in bad_dtype_manifest["datasource"]["fields"]:
    if field_entry["display_name"] == "total_tokens":
      field_entry["data_type"] = "STRING"
      field_entry["semantic_type"] = "TEXT"
  dtype_errors = validator.validate_bqca(
      manifest_override=bad_dtype_manifest,
      binding_override=_recomputed_binding(m_override=bad_dtype_manifest),
  )
  assert any(
      "total_tokens" in e and "data_type" in e for e in dtype_errors
  ), dtype_errors

  # R2-P3-6 (d): renaming a projected column in SQL is caught against
  # manifest["datasource"]["fields"] even when SQL SHA-256 is recomputed.
  drifted_sql = sql_text.replace(") AS persona,", ") AS persona_idx,", 1)
  sql_drift_errors = validator.validate_bqca(
      sql_override=drifted_sql,
      binding_override=_recomputed_binding(s_override=drifted_sql),
  )
  assert any(
      "SQL projected columns do not match manifest" in e
      for e in sql_drift_errors
  ), sql_drift_errors


BQCA_SPEC_KIND_BY_CHART_TYPE = {
    "SCORECARD": "scorecard",
    "bar_chart": "barChart",
    "pie_chart": "pieChart",
    "table": "table",
}
# Keys under an embedded spec's `fields` block whose values name columns.
BQCA_SPEC_REFERENCE_KEYS = (
    "primaryMetricId",
    "fieldId",
    "dimensionId",
    "metricId",
    "sortFieldId",
)
BQCA_SPEC_REFERENCE_LIST_KEYS = ("dimensionIds", "metricIds")
BQCA_NOT_QT_SUFFIX = (
    " is not a qt_ column ID (^qt_[a-z0-9]{3,10}$), which this repository"
    " requires"
)


def _bqca_spec_inner(comp):
  return comp["spec"][BQCA_SPEC_KIND_BY_CHART_TYPE[comp["chart_type"]]]


def _bqca_spec_columns(comp):
  return _bqca_spec_inner(comp)["dataset"]["columns"]


def _bqca_spec_references(node):
  """Yield every column ID that an embedded spec `fields` block names."""
  if isinstance(node, dict):
    for key, value in node.items():
      if key in BQCA_SPEC_REFERENCE_KEYS:
        yield value
      elif key in BQCA_SPEC_REFERENCE_LIST_KEYS:
        yield from value
      else:
        yield from _bqca_spec_references(value)
  elif isinstance(node, list):
    for item in node:
      yield from _bqca_spec_references(item)


def _bqca_spec_sort_ids(comp):
  fields = _bqca_spec_inner(comp)["fields"]
  clauses = fields.get("primaryAxis", {}).get("sort", [])
  return [clause["sortFieldId"] for clause in clauses + fields.get("sort", [])]


def _bqca_page_components(document):
  return [comp for page in document["pages"] for comp in page["components"]]


def test_bqca_column_ids_are_report_wide_unique_qt_identifiers():
  """All 65 BQCA column IDs are deterministic qt_ IDs, unique report-wide.

  This pins the repository manifest and bundle. The live template keeps its
  legacy generic column IDs (`met`, `dim`, `d0`, `m0`, ...), with which
  components on one page of a copied report show each other's values, until
  it is synced from these contracts; `verified_date` in
  `bindings/bqca_report_template.yaml` stays 2026-10-09 until then, and
  reports copied before the sync must be re-created after it.
  """
  validator = _load_dashboard_module("validate_contracts")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  bundle = json.loads(
      (DASHBOARD / "spec/bqca_dashboard_template.json").read_text()
  )

  def _column_ids(components):
    column_ids = []
    for comp in components:
      own = []
      for slot, column in enumerate(_bqca_spec_columns(comp)):
        assert column["id"] == validator.bqca_qt_column_id(
            comp["component_id"], slot, column["fieldName"]
        ), (comp["id"], slot, column)
        own.append(column["id"])
      # Every column is bound, and every binding names a column of the same
      # component.
      references = set(_bqca_spec_references(_bqca_spec_inner(comp)["fields"]))
      assert references == set(own), (comp["id"], references, own)
      column_ids.extend(own)
    return column_ids

  column_ids = _column_ids(_bqca_page_components(manifest))
  assert len(column_ids) == 65
  assert len(set(column_ids)) == 65
  for column_id in column_ids:
    assert re.fullmatch(r"qt_[0-9a-f]{8}", column_id), column_id
  assert _column_ids(manifest["components"]) == column_ids
  assert _column_ids(_bqca_page_components(bundle)) == column_ids
  assert _column_ids(bundle["components"]) == column_ids
  # Pin the formula's output, not only its self-consistency.
  assert (
      validator.bqca_qt_column_id("cd-vWRq2EdpQyI", 0, "_total_tokens_")
      == "qt_d779815a"
  )
  assert (
      validator.bqca_qt_column_id("cd-kM4cFH3pweE", 3, "_thoughts_tokens_")
      == "qt_0db8b52b"
  )
  assert column_ids[0] == "qt_d779815a"
  assert "qt_0db8b52b" in column_ids


def test_validate_contracts_rejects_generic_or_repeated_column_ids():
  """#check-can-fail: each column-ID or same-page scorecard defect fails.

  Every case re-pins the manifest and bundle digests to the mutated
  documents, so only the column-ID, column-reference, or scorecard checks
  can catch it.
  """
  validator = _load_dashboard_module("validate_contracts")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  bundle = json.loads(
      (DASHBOARD / "spec/bqca_dashboard_template.json").read_text()
  )
  binding = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  page_components = _bqca_page_components(manifest)
  by_id = {comp["id"]: comp for comp in page_components}

  def _validate_documents(mutate_documents):
    """Apply `mutate_documents` to copies of both documents, re-pin, validate."""
    m_copy = json.loads(json.dumps(manifest))
    b_copy = json.loads(json.dumps(bundle))
    mutate_documents(m_copy, b_copy)
    repinned = json.loads(json.dumps(binding))
    live = repinned["live_template_verification"]
    live["manifest_sha256"] = hashlib.sha256(
        yaml.safe_dump(m_copy, sort_keys=False).encode("utf-8")
    ).hexdigest()
    live["template_bundle_sha256"] = hashlib.sha256(
        (json.dumps(b_copy, indent=2) + "\n").encode("utf-8")
    ).hexdigest()
    return validator.validate_bqca(
        manifest_override=m_copy,
        bundle_override=b_copy,
        binding_override=repinned,
    )

  def _mutating(mutations, *, manifest_too=True):
    """Return a document mutation applying `mutations[id]` to each copy of id."""

    def mutate_documents(m_copy, b_copy):
      for document in (m_copy, b_copy) if manifest_too else (b_copy,):
        for comp in _bqca_page_components(document) + document["components"]:
          if comp["id"] in mutations:
            mutations[comp["id"]](comp)

    return mutate_documents

  def _validate(component, mutate, *, manifest_too=True):
    """Apply `mutate` to every copy of `component`, re-pin, and validate."""
    return _validate_documents(
        _mutating({component: mutate}, manifest_too=manifest_too)
    )

  def _rename(slot, new_id):
    """Rename one column and every reference to it."""

    def mutate(comp):
      inner = _bqca_spec_inner(comp)
      old_id = inner["dataset"]["columns"][slot]["id"]
      inner["dataset"]["columns"][slot]["id"] = new_id
      inner["fields"] = json.loads(
          json.dumps(inner["fields"]).replace(
              json.dumps(old_id), json.dumps(new_id)
          )
      )

    return mutate

  # Re-pinning alone keeps the contract green.
  assert _validate("kpi_total_tokens", lambda comp: None) == []

  # A generic or malformed ID fails even when it is unique.
  for bad_id in ("met", "qt_D779815A", "qt_ab", "qt_0123456789a", "t0.qt_x1y2"):
    assert _validate("kpi_total_tokens", _rename(0, bad_id)) == [
        f"component kpi_total_tokens spec.scorecard.dataset.columns id"
        f" {bad_id!r}{BQCA_NOT_QT_SUFFIX}"
    ], bad_id

  # A unique, well-formed qt_ ID fails when it is not its slot's derived ID,
  # in the manifest and in the bundle.
  for fake_id in ("qt_deadbeef", "qt_12345678"):
    assert _validate("kpi_total_tokens", _rename(0, fake_id)) == [
        f"bqca {source} component {BQCA_KPI_TOTAL} column 0 id {fake_id!r} is"
        " not its derived ID 'qt_d779815a'"
        for source in ("manifest", "bundle")
    ], fake_id

  # A qt_ ID reused by another component fails, on the same page or not, and
  # is not that component's derived ID either.
  last_on_last_page = manifest["pages"][-1]["components"][-1]
  for component in ("kpi_input_tokens", last_on_last_page["id"]):
    owner = f"{component} ({by_id[component]['component_id']})"
    own_id = _bqca_spec_columns(by_id[component])[0]["id"]
    assert _validate(component, _rename(0, "qt_d779815a")) == [
        f"bqca {source} column id 'qt_d779815a' is used by both"
        f" kpi_total_tokens (cd-vWRq2EdpQyI) and {owner}; column IDs must be"
        " unique across the report"
        for source in ("manifest", "bundle")
    ] + [
        f"bqca {source} component {owner} column 0 id 'qt_d779815a' is not"
        f" its derived ID {own_id!r}"
        for source in ("manifest", "bundle")
    ], component

  # Two slots whose digests share their first 8 (or 9) hex digits both get a
  # 9- (or 10-) hex-digit ID, and two that share 10 cannot be told apart.
  # Moving the two scorecards to such component IDs needs exactly those IDs.
  def _move(
      total_component_id, input_component_id, total_id=None, input_id=None
  ):
    """Move both scorecards, renaming their columns to the IDs given."""

    def moving(component_id, column_id):
      def mutate(comp):
        comp["component_id"] = comp["spec"]["id"] = component_id
        if column_id is not None:
          _rename(0, column_id)(comp)

      return mutate

    return _mutating(
        {
            "kpi_total_tokens": moving(total_component_id, total_id),
            "kpi_input_tokens": moving(input_component_id, input_id),
        }
    )

  for total_component_id, input_component_id, total_id, input_id in (
      ("cd-t0000166904", "cd-i0000005894", "qt_d96d65ad4", "qt_d96d65ad9"),
      ("cd-t0000022648", "cd-i0000082636", "qt_fed2087483", "qt_fed208748b"),
  ):
    assert _validate_documents(
        _move(total_component_id, input_component_id)
    ) == [
        error
        for source in ("manifest", "bundle")
        for error in (
            f"bqca {source} component kpi_total_tokens ({total_component_id})"
            f" column 0 id 'qt_d779815a' is not its derived ID {total_id!r}",
            f"bqca {source} component kpi_input_tokens ({input_component_id})"
            f" column 0 id 'qt_c90b69a4' is not its derived ID {input_id!r}",
        )
    ], total_id
    assert (
        _validate_documents(
            _move(total_component_id, input_component_id, total_id, input_id)
        )
        == []
    ), total_id

  collision = (
      "column IDs cannot be derived: column slots"
      " cd-i0000212782:0:_input_tokens_, cd-t0002954566:0:_total_tokens_ share"
      " the first 10 hex digits '75f609f3f6' of their SHA-256, so no qt_ ID"
      " with at most 10 hex digits tells them apart"
  )
  assert _validate_documents(_move("cd-t0002954566", "cd-i0000212782")) == [
      f"bqca {source} {collision}" for source in ("manifest", "bundle")
  ]

  # The manifest's top-level components list must repeat its pages'
  # components in full, not only their ids.
  def _rename_top_level_copy(m_copy, b_copy):
    del b_copy
    for comp in m_copy["components"]:
      if comp["id"] == "kpi_total_tokens":
        _rename(0, "met")(comp)

  assert _validate_documents(_rename_top_level_copy) == [
      "bqca manifest top-level components list does not match page components"
  ]

  # A component that repeats its own column ID is named once.
  over_time_ids = [
      column["id"]
      for column in _bqca_spec_columns(by_id["chart_tokens_over_time"])
  ]
  repeat_errors = _validate(
      "chart_tokens_over_time", _rename(2, over_time_ids[1])
  )
  assert (
      f"component chart_tokens_over_time repeats column id"
      f" {over_time_ids[1]!r} in spec.barChart.dataset.columns"
  ) in repeat_errors, repeat_errors
  assert not any("is used by both" in e for e in repeat_errors), repeat_errors

  # Bundle-only column-ID drift is named, even when the bundle is re-pinned.
  drift_errors = _validate(
      "kpi_total_tokens", _rename(0, "qt_00000000"), manifest_too=False
  )
  assert (
      "bqca bundle component kpi_total_tokens column ids ['qt_00000000'] !="
      " manifest column ids ['qt_d779815a']"
  ) in drift_errors, drift_errors

  # A sort clause must name one of the component's own columns, for bar
  # charts (primaryAxis.sort) and for the other kinds (fields.sort).
  sorted_charts = [
      comp for comp in page_components if _bqca_spec_sort_ids(comp)
  ]
  assert len(sorted_charts) == 13
  bar = next(c for c in sorted_charts if c["chart_type"] == "bar_chart")
  other = next(c for c in sorted_charts if c["chart_type"] != "bar_chart")

  def _dangle(comp):
    fields = _bqca_spec_inner(comp)["fields"]
    clauses = (fields.get("primaryAxis") or fields)["sort"]
    clauses[0]["sortFieldId"] = "qt_ffffffff"

  for chart in (bar, other):
    sort_errors = _validate(chart["id"], _dangle)
    assert sort_errors == [
        f"chart {chart['id']} embedded spec sort references undefined column"
        " id 'qt_ffffffff'"
    ], sort_errors

  # The sort clauses must be a list.
  def _unlist_sort(comp):
    axis = _bqca_spec_inner(comp)["fields"]["primaryAxis"]
    axis["sort"] = axis["sort"][0]

  unlisted_errors = _validate(bar["id"], _unlist_sort)
  assert unlisted_errors == [
      f"chart {bar['id']} embedded spec sort must be a list"
  ], unlisted_errors

  # Sort direction must be 'ASC' or 'DESC', reverseSortOrder (when present)
  # must be a bool, and null mapping/list nodes return structured errors.
  for chart in (bar, other):

    def _bad_sort_dir(comp):
      fields = _bqca_spec_inner(comp)["fields"]
      clauses = (fields.get("primaryAxis") or fields)["sort"]
      clauses[0]["sortDir"] = "SIDEWAYS"

    assert _validate(chart["id"], _bad_sort_dir) == [
        f"chart {chart['id']} embedded spec sortDir 'SIDEWAYS' must be"
        " 'ASC' or 'DESC'"
    ]

    def _bad_reverse_sort_order(comp):
      fields = _bqca_spec_inner(comp)["fields"]
      holder = fields.get("primaryAxis") or fields
      holder["reverseSortOrder"] = "false"

    assert _validate(chart["id"], _bad_reverse_sort_order) == [
        f"chart {chart['id']} embedded spec reverseSortOrder 'false' must be a"
        " bool"
    ]

  def _null_fields(comp):
    _bqca_spec_inner(comp)["fields"] = None

  assert _validate(bar["id"], _null_fields) == [
      f"component {bar['id']} missing embedded spec.barChart.fields mapping"
  ]

  def _null_dataset(comp):
    _bqca_spec_inner(comp)["dataset"] = None

  assert _validate(bar["id"], _null_dataset) == [
      f"component {bar['id']} spec.barChart.dataset.datasourceId mismatch",
      f"component {bar['id']} spec.barChart.dataset.columns empty",
  ]

  # Every `*Id`/`*Ids` key under `fields` must be one that the kind reads its
  # columns from: a bar chart reads its sort from `primaryAxis.sort` only, and
  # a table has no `primaryMetricId`. This holds for a column the component
  # defines, too.
  bar_metric = _bqca_spec_inner(bar)["fields"]["metricSeries"]["metricIds"][0]
  for sort_field in ("met", bar_metric):

    def _add_fields_sort(comp, sort_field=sort_field):
      _bqca_spec_inner(comp)["fields"]["sort"] = [
          {"sortFieldId": sort_field, "sortDir": "DESC"}
      ]

    assert _validate(bar["id"], _add_fields_sort) == [
        f"component {bar['id']} spec.barChart.fields.sort.sortFieldId"
        f" {sort_field!r} is not a column reference a barChart reads"
    ], sort_field

  table = next(c for c in page_components if c["chart_type"] == "table")
  table_dimension = _bqca_spec_inner(table)["fields"]["dimensionIds"][0]

  def _add_primary_metric(comp):
    _bqca_spec_inner(comp)["fields"]["primaryMetricId"] = table_dimension

  assert _validate(table["id"], _add_primary_metric) == [
      f"component {table['id']} spec.table.fields.primaryMetricId"
      f" {table_dimension!r} is not a column reference a table reads"
  ]

  # Every column must be referenced from one of those keys.
  extra_field = "_timestamp_"
  assert extra_field not in bar["columns"]
  extra_slot = len(_bqca_spec_columns(bar))
  extra_id = validator.bqca_qt_column_id(
      bar["component_id"], extra_slot, extra_field
  )

  def _add_unreferenced_column(comp):
    _bqca_spec_inner(comp)["dataset"]["columns"].append(
        {"id": extra_id, "fieldName": extra_field, "displayName": "Timestamp"}
    )
    comp["columns"].append(extra_field)

  assert _validate(bar["id"], _add_unreferenced_column) == [
      f"component {bar['id']} spec.barChart.dataset.columns id {extra_id!r} is"
      " not referenced by spec.barChart.fields"
  ]

  # A top-level key containing a dot (such as `'primaryAxis.fieldId'`) is a
  # single key, not the nested path `('primaryAxis', 'fieldId')`, so it is
  # rejected as an unread key and does not count as referencing `extra_id`.
  def _add_dotted_top_level_reference(comp):
    _add_unreferenced_column(comp)
    _bqca_spec_inner(comp)["fields"]["primaryAxis.fieldId"] = extra_id

  assert _validate(bar["id"], _add_dotted_top_level_reference) == [
      f"component {bar['id']} spec.barChart.fields['primaryAxis.fieldId']"
      f" {extra_id!r} is not a column reference a barChart reads",
      f"component {bar['id']} spec.barChart.dataset.columns id {extra_id!r} is"
      " not referenced by spec.barChart.fields",
  ]

  # Two scorecards on one page must not show the same `(field, aggregation)`
  # measure, even when every column ID, digest, and contract entry is coherent
  # and even when one or both scorecards carry `tile_filter` metadata (since
  # the embedded scorecard spec has no filter clause).
  dup_id = validator.bqca_qt_column_id("cd-gQM6-qRztkI", 0, "_total_tokens_")
  contract = yaml.safe_load(
      (DASHBOARD / "spec/bqca_product_contract.yaml").read_text()
  )
  for total_filter, input_filter in (
      (None, None),
      (None, {"field": "persona", "values": ["analyst"]}),
      (
          {"field": "persona", "values": ["analyst"]},
          {"field": "persona", "values": ["engineer"]},
      ),
  ):
    m_dup = json.loads(json.dumps(manifest))
    b_dup = json.loads(json.dumps(bundle))
    c_dup = json.loads(json.dumps(contract))
    bi_dup = json.loads(json.dumps(binding))
    for doc in (m_dup, b_dup):
      for collection in (
          [c for p in doc["pages"] for c in p["components"]],
          doc["components"],
      ):
        for comp in collection:
          if comp["id"] == "kpi_total_tokens" and total_filter is not None:
            comp["tile_filter"] = total_filter
          elif comp["id"] == "kpi_input_tokens":
            comp["field"] = "total_tokens"
            comp["columns"] = ["_total_tokens_"]
            inner = _bqca_spec_inner(comp)
            inner["dataset"]["columns"][0]["id"] = dup_id
            inner["dataset"]["columns"][0]["fieldName"] = "_total_tokens_"
            inner["fields"]["primaryMetricId"] = dup_id
            if input_filter is not None:
              comp["tile_filter"] = input_filter
    for page in c_dup["pages"]:
      for sc in page.get("scorecards", []):
        if sc["id"] == "kpi_total_tokens" and total_filter is not None:
          sc["tile_filter"] = total_filter
        elif sc["id"] == "kpi_input_tokens":
          sc["field"] = "total_tokens"
          if input_filter is not None:
            sc["tile_filter"] = input_filter
    bi_dup["live_template_verification"]["manifest_sha256"] = hashlib.sha256(
        yaml.safe_dump(m_dup, sort_keys=False).encode("utf-8")
    ).hexdigest()
    bi_dup["live_template_verification"]["template_bundle_sha256"] = (
        hashlib.sha256(
            (json.dumps(b_dup, indent=2) + "\n").encode("utf-8")
        ).hexdigest()
    )
    bi_dup["live_template_verification"]["pages_sha256"] = (
        validator.canonical_pages_sha256(c_dup["pages"])
    )
    assert validator.validate_bqca(
        contract_override=c_dup,
        manifest_override=m_dup,
        binding_override=bi_dup,
        bundle_override=b_dup,
    ) == [
        "bqca page p_539b9240 scorecards kpi_total_tokens and kpi_input_tokens"
        " both show SUM(total_tokens)"
    ]

  # The same field under another aggregation is another measure.
  def _show_average_total_tokens(comp):
    comp["field"] = "total_tokens"
    _bqca_spec_columns(comp)[0]["fieldName"] = "_total_tokens_"
    comp["aggregation"] = "AVG"
    _bqca_spec_columns(comp)[0]["aggregation"] = "AVG"

  assert not any(
      "both show" in e
      for e in _validate("kpi_input_tokens", _show_average_total_tokens)
  )


def _bqca_validate_repinned(edit):
  """Validate the BQCA contracts with `edit` applied to manifest and bundle.

  The binding's manifest and bundle digests are re-pinned to the edited
  copies, so only the checks of their content can fail.
  """
  validator = _load_dashboard_module("validate_contracts")
  # A JSON round trip gives the manifest's top-level components list its own
  # copies of the page components, as the bundle's are.
  manifest = json.loads(
      json.dumps(
          yaml.safe_load(
              (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
          )
      )
  )
  bundle = json.loads(
      (DASHBOARD / "spec/bqca_dashboard_template.json").read_text()
  )
  binding = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  edit(manifest)
  edit(bundle)
  live = binding["live_template_verification"]
  live["manifest_sha256"] = hashlib.sha256(
      yaml.safe_dump(manifest, sort_keys=False).encode("utf-8")
  ).hexdigest()
  live["template_bundle_sha256"] = hashlib.sha256(
      (json.dumps(bundle, indent=2) + "\n").encode("utf-8")
  ).hexdigest()
  return validator.validate_bqca(
      manifest_override=manifest,
      bundle_override=bundle,
      binding_override=binding,
  )


def _bqca_spec_block_edit(component, key, block):
  """Return an edit that sets `spec[key]` in every copy of `component`.

  A callable `block` is called with the component to get the block.
  """

  def edit(document):
    for comp in _bqca_page_components(document) + document["components"]:
      if comp["id"] == component:
        value = block(comp) if callable(block) else block
        comp["spec"][key] = json.loads(json.dumps(value))

  return edit


# One case per block that a component's embedded spec must not hold: an edit
# of the manifest and bundle that adds it, and the exact errors it must produce.
BQCA_EXTRA_SPEC_BLOCK_CASES = [
    pytest.param(lambda document: None, [], id="unchanged"),
    pytest.param(
        # kpi_input_tokens' own column ID, in a block of another kind.
        _bqca_spec_block_edit(
            "kpi_total_tokens",
            "barChart",
            {
                "dataset": {
                    "datasourceId": BQCA_DATASOURCE_ID,
                    "columns": [
                        {
                            "id": "qt_c90b69a4",
                            "fieldName": "_input_tokens_",
                            "aggregation": "SUM",
                        }
                    ],
                },
                "fields": {"metricSeries": {"metricIds": ["qt_c90b69a4"]}},
            },
        ),
        [
            "component kpi_total_tokens spec.barChart is not id, fixedLayout,"
            " or the spec.scorecard block that chart_type 'SCORECARD' reads"
        ],
        id="another_kind_repeating_another_components_column_id",
    ),
    pytest.param(
        # The legacy generic IDs, in a block of another kind.
        _bqca_spec_block_edit(
            "kpi_total_tokens",
            "table",
            {
                "dataset": {
                    "datasourceId": BQCA_DATASOURCE_ID,
                    "columns": [
                        {"id": "dim", "fieldName": "_data_agent_id_"},
                        {
                            "id": "met",
                            "fieldName": "_total_tokens_",
                            "aggregation": "SUM",
                        },
                    ],
                },
                "fields": {"dimensionIds": ["dim"], "metricIds": ["met"]},
            },
        ),
        [
            "component kpi_total_tokens spec.table is not id, fixedLayout, or"
            " the spec.scorecard block that chart_type 'SCORECARD' reads"
        ],
        id="another_kind_with_generic_column_ids",
    ),
    pytest.param(
        # The block of a kind the chart had before, repeating its column IDs.
        _bqca_spec_block_edit(
            "chart_tokens_by_agent",
            "lineChart",
            lambda comp: comp["spec"]["barChart"],
        ),
        [
            "component chart_tokens_by_agent spec.lineChart is not id,"
            " fixedLayout, or the spec.barChart block that chart_type"
            " 'bar_chart' reads"
        ],
        id="stale_block_of_a_former_kind",
    ),
    pytest.param(
        # A kind that no BQCA chart_type maps to.
        _bqca_spec_block_edit(
            "chart_tokens_by_agent",
            "timeSeries",
            {
                "dataset": {
                    "datasourceId": BQCA_DATASOURCE_ID,
                    "columns": [
                        {
                            "id": "met",
                            "fieldName": "_total_tokens_",
                            "aggregation": "SUM",
                        }
                    ],
                },
                "fields": {"metricIds": ["met"]},
            },
        ),
        [
            "component chart_tokens_by_agent spec.timeSeries is not id,"
            " fixedLayout, or the spec.barChart block that chart_type"
            " 'bar_chart' reads"
        ],
        id="kind_no_chart_type_reads",
    ),
]


@pytest.mark.parametrize("edit, expected", BQCA_EXTRA_SPEC_BLOCK_CASES)
def test_validate_contracts_rejects_spec_blocks_the_chart_type_does_not_read(
    edit, expected
):
  """#check-can-fail: a component's spec holds no block its kind does not read.

  The column-ID checks read `spec.<kind>` alone, the block of the component's
  `chart_type`, so any other block of its spec must be rejected rather than
  left unchecked. Each case adds one, with a repeated, stale, or generic
  column ID, to the manifest and the bundle and re-pins both digests, so only
  that check can catch it.
  """
  assert _bqca_validate_repinned(edit) == expected


def test_bqca_derive_report_column_ids_lengthens_only_colliding_ids():
  """Two slots that share 8 hex digits both get longer IDs, up to 10 digits.

  The IDs depend only on the set of slots, and two slots that share their
  first 10 hex digits fail with both slots named.
  """
  validator = _load_dashboard_module("validate_contracts")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  slots = [
      (comp["component_id"], slot, column["fieldName"])
      for comp in _bqca_page_components(manifest)
      for slot, column in enumerate(_bqca_spec_columns(comp))
  ]
  assert len(slots) == 65
  derived = validator.bqca_derive_report_column_ids(slots)
  # No two of the 65 slots share 8 hex digits, so each has its 8-digit ID,
  # which is the ID the manifest gives it.
  assert derived == {slot: validator.bqca_qt_column_id(*slot) for slot in slots}
  assert [derived[slot] for slot in slots] == [
      column["id"]
      for comp in _bqca_page_components(manifest)
      for column in _bqca_spec_columns(comp)
  ]
  assert validator.bqca_derive_report_column_ids(reversed(slots)) == derived
  assert validator.bqca_derive_report_column_ids(slots + slots[:1]) == derived

  for total, inputs, total_id, input_id in (
      (
          ("cd-t0000166904", 0, "_total_tokens_"),
          ("cd-i0000005894", 0, "_input_tokens_"),
          "qt_d96d65ad4",
          "qt_d96d65ad9",
      ),
      (
          ("cd-t0000022648", 0, "_total_tokens_"),
          ("cd-i0000082636", 0, "_input_tokens_"),
          "qt_fed2087483",
          "qt_fed208748b",
      ),
  ):
    assert validator.bqca_qt_column_id(*total) == validator.bqca_qt_column_id(
        *inputs
    )
    for ordered in ([total, inputs, *slots], [*slots, inputs, total]):
      assert validator.bqca_derive_report_column_ids(ordered) == {
          **derived,
          total: total_id,
          inputs: input_id,
      }, total_id
    # A slot listed twice is one slot, so it collides with nothing.
    assert validator.bqca_derive_report_column_ids([total, total]) == {
        total: validator.bqca_qt_column_id(*total)
    }

  with pytest.raises(ValueError) as raised:
    validator.bqca_derive_report_column_ids(
        [
            ("cd-t0002954566", 0, "_total_tokens_"),
            *slots,
            ("cd-i0000212782", 0, "_input_tokens_"),
        ]
    )
  assert str(raised.value) == (
      "column slots cd-i0000212782:0:_input_tokens_,"
      " cd-t0002954566:0:_total_tokens_ share the first 10 hex digits"
      " '75f609f3f6' of their SHA-256, so no qt_ ID with at most 10 hex digits"
      " tells them apart"
  )


_BQCA_DROP = object()
BQCA_LIVE_PENDING = "bqca live_template_verification.pending_live_sync"
BQCA_LIVE_SYNCED = (
    (("verified_manifest_sha256",), lambda live: live["manifest_sha256"]),
    (
        ("verified_template_bundle_sha256",),
        lambda live: live["template_bundle_sha256"],
    ),
)


def _bqca_live_edit(*edits):
  """Return an edit of a binding's `live_template_verification` mapping.

  Each edit is a `(key path, value)` pair; a `_BQCA_DROP` value deletes the
  key, and a callable value is called with the mapping to get the value.
  """

  def edit(live):
    for path, value in edits:
      *parents, key = path
      node = live
      for parent in parents:
        node = node[parent]
      if value is _BQCA_DROP:
        del node[key]
      else:
        node[key] = value(live) if callable(value) else value

  return edit


def _bqca_pending_edit(key, value):
  """Return an edit of one key of `pending_live_sync`."""
  return _bqca_live_edit((("pending_live_sync", key), value))


def _bqca_pending_cases(key, message, values):
  """Return one case per `(id, value)` that must fail with `message`."""
  return [
      pytest.param(
          _bqca_pending_edit(key, value),
          [
              f"{BQCA_LIVE_PENDING}.{key}"
              f" {None if value is _BQCA_DROP else value!r} {message}"
          ],
          id=f"{key}_{case_id}",
      )
      for case_id, value in values
  ]


BQCA_ISSUE_PREFIX = (
    "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
)
# One case per rule of the pending live sync record: an edit of the binding's
# `live_template_verification` and the exact errors it must produce.
BQCA_PENDING_LIVE_SYNC_CASES = [
    pytest.param(_bqca_live_edit(), [], id="recorded"),
    pytest.param(
        _bqca_live_edit((("pending_live_sync",), _BQCA_DROP)),
        [
            f"{BQCA_LIVE_PENDING} is required while manifest_sha256 !="
            " verified_manifest_sha256 and template_bundle_sha256 !="
            " verified_template_bundle_sha256"
        ],
        id="missing_while_both_digests_differ",
    ),
    pytest.param(
        _bqca_live_edit(
            BQCA_LIVE_SYNCED[0], (("pending_live_sync",), _BQCA_DROP)
        ),
        [
            f"{BQCA_LIVE_PENDING} is required while template_bundle_sha256 !="
            " verified_template_bundle_sha256"
        ],
        id="missing_while_the_bundle_differs",
    ),
    pytest.param(
        _bqca_live_edit(
            BQCA_LIVE_SYNCED[1], (("pending_live_sync",), _BQCA_DROP)
        ),
        [
            f"{BQCA_LIVE_PENDING} is required while manifest_sha256 !="
            " verified_manifest_sha256"
        ],
        id="missing_while_the_manifest_differs",
    ),
    pytest.param(
        _bqca_live_edit(*BQCA_LIVE_SYNCED),
        [
            f"{BQCA_LIVE_PENDING} must be removed: manifest_sha256 and"
            " template_bundle_sha256 equal their verified digests"
        ],
        id="kept_after_the_sync",
    ),
    pytest.param(
        _bqca_live_edit(
            *BQCA_LIVE_SYNCED, (("pending_live_sync",), _BQCA_DROP)
        ),
        [],
        id="removed_after_the_sync",
    ),
    *(
        pytest.param(
            _bqca_live_edit(((key,), value)),
            [
                "bqca live_template_verification."
                f"{key} {None if value is _BQCA_DROP else value!r} is not a"
                " SHA-256 hex digest"
            ],
            id=f"{key}_{case_id}",
        )
        for key, case_id, value in (
            ("verified_manifest_sha256", "truncated", "a6b4effd"),
            (
                "verified_manifest_sha256",
                "uppercase",
                BQCA_LIVE_VERIFIED_MANIFEST_SHA256.upper(),
            ),
            ("verified_template_bundle_sha256", "missing", _BQCA_DROP),
        )
    ),
    pytest.param(
        _bqca_live_edit((("pending_live_sync",), "see the tracking issue")),
        [f"{BQCA_LIVE_PENDING} must be a mapping"],
        id="not_a_mapping",
    ),
    *_bqca_pending_cases(
        "reason",
        "must be a non-empty string",
        (("empty", ""), ("blank", "  "), ("missing", _BQCA_DROP)),
    ),
    *_bqca_pending_cases(
        "since_date",
        "is not a quoted YYYY-MM-DD date",
        (
            ("unquoted", datetime.date(2026, 10, 29)),
            ("impossible", "2026-13-01"),
            ("with_time", "2026-10-29T00:00"),
            ("basic_format", "20261029"),
            ("missing", _BQCA_DROP),
        ),
    ),
    *(
        pytest.param(
            _bqca_live_edit((("verified_date",), value)),
            [
                "bqca live_template_verification.verified_date"
                f" {None if value is _BQCA_DROP else value!r} is not a quoted"
                " YYYY-MM-DD date"
            ],
            id=f"verified_date_{case_id}",
        )
        for case_id, value in (
            ("unquoted", datetime.date(2026, 10, 9)),
            ("impossible", "2026-13-01"),
            ("with_time", "2026-10-09T00:00"),
            ("basic_format", "20261009"),
            ("missing", _BQCA_DROP),
        )
    ),
    pytest.param(
        _bqca_live_edit(
            *BQCA_LIVE_SYNCED,
            (("pending_live_sync",), _BQCA_DROP),
            (("verified_date",), "2026-02-30"),
        ),
        [
            "bqca live_template_verification.verified_date '2026-02-30' is not"
            " a quoted YYYY-MM-DD date"
        ],
        id="verified_date_invalid_after_sync",
    ),
    pytest.param(
        _bqca_live_edit((("verified_date",), "2099-01-01")),
        [
            "bqca live_template_verification.verified_date '2099-01-01' is in the future"
        ],
        id="verified_date_in_the_future",
    ),
    pytest.param(
        _bqca_live_edit(
            *BQCA_LIVE_SYNCED,
            (("pending_live_sync",), _BQCA_DROP),
            (
                ("verified_date",),
                max(
                    datetime.date.today(),
                    datetime.datetime.now(datetime.timezone.utc).date(),
                ).isoformat(),
            ),
        ),
        [],
        id="verified_date_today",
    ),
    pytest.param(
        _bqca_pending_edit("since_date", "2026-10-08"),
        [
            f"{BQCA_LIVE_PENDING}.since_date '2026-10-08' is before"
            " verified_date '2026-10-09'"
        ],
        id="since_date_before_verified_date",
    ),
    pytest.param(
        _bqca_pending_edit("since_date", "2026-10-09"),
        [],
        id="since_date_equal_to_verified_date",
    ),
    pytest.param(
        _bqca_pending_edit("since_date", "2099-01-01"),
        [f"{BQCA_LIVE_PENDING}.since_date '2099-01-01' is in the future"],
        id="since_date_in_the_future",
    ),
    pytest.param(
        _bqca_pending_edit(
            "since_date",
            max(
                datetime.date.today(),
                datetime.datetime.now(datetime.timezone.utc).date(),
            ).isoformat(),
        ),
        [],
        id="since_date_today",
    ),
    *_bqca_pending_cases(
        "tracking_issue",
        "is not the URL of an issue of this repository",
        (
            ("pull_request", f"{BQCA_ISSUE_PREFIX}/pull/559"),
            ("issue_zero", f"{BQCA_ISSUE_PREFIX}/issues/0"),
            (
                "other_repository",
                "https://github.com/GoogleCloudPlatform/other/issues/559",
            ),
            ("trailing_slash", f"{BQCA_LIVE_SYNC_ISSUE}/"),
            ("missing", _BQCA_DROP),
        ),
    ),
    *_bqca_pending_cases(
        "required_actions",
        "must be a non-empty list of non-empty strings",
        (
            ("empty", []),
            ("empty_string", [""]),
            ("blank_string", [" "]),
            ("not_a_string", ["Sync the template.", 1]),
            # A single word, so that only the list check can reject it: read
            # as a list of characters, a string with a space fails on the
            # blank one.
            ("a_string", "Resync."),
            ("missing", _BQCA_DROP),
        ),
    ),
]


@pytest.mark.parametrize("edit, expected", BQCA_PENDING_LIVE_SYNC_CASES)
def test_validate_contracts_requires_a_pending_live_sync_record(edit, expected):
  """#check-can-fail: an unverified live state is recorded, exactly.

  While the pinned manifest or bundle digest differs from the one the live
  template was verified against, `pending_live_sync` must give a reason, a
  quoted since-date, the tracking issue of this repository, and the required
  actions; once both match again it must be removed.
  """
  validator = _load_dashboard_module("validate_contracts")
  binding = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  edit(binding["live_template_verification"])
  assert validator.validate_bqca(binding_override=binding) == expected


BQCA_CONTRACT_FILES = (
    "bindings/bqca_report_template.yaml",
    "spec/bqca_chart_manifest.yaml",
    "spec/bqca_compatibility_profile.json",
    "spec/bqca_dashboard_template.json",
    "spec/bqca_product_contract.yaml",
    "sql/bqca_events_v1.template.sql",
)


def test_validate_contracts_cli_rejects_a_column_id_that_is_not_derived(
    tmp_path, monkeypatch, capsys
):
  """#check-can-fail: `--profile bqca` alone rejects a non-derived qt_ ID.

  A copy of the BQCA contracts gives the first scorecard's column a
  well-formed, unique qt_ ID in the manifest and the bundle, and re-pins both
  digests, so only the derivation check can fail.
  """
  validator = _load_dashboard_module("validate_contracts")
  for relative in BQCA_CONTRACT_FILES:
    (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DASHBOARD / relative, tmp_path / relative)
  monkeypatch.setattr(validator, "ROOT", tmp_path)
  assert validator.main(["--profile", "bqca"]) == 0
  assert capsys.readouterr().out == "ok: contracts validated (bqca)\n"

  binding_path = tmp_path / "bindings/bqca_report_template.yaml"
  binding_bytes = binding_path.read_bytes()
  # The column ID and its primaryMetricId; the manifest's top-level list
  # aliases its page components, and the bundle's repeats them.
  for relative, occurrences in (
      ("spec/bqca_chart_manifest.yaml", 2),
      ("spec/bqca_dashboard_template.json", 4),
  ):
    path = tmp_path / relative
    original = path.read_bytes()
    assert original.count(b"qt_d779815a") == occurrences, relative
    path.write_bytes(original.replace(b"qt_d779815a", b"qt_deadbeef"))
    old_digest = hashlib.sha256(original).hexdigest().encode()
    new_digest = hashlib.sha256(path.read_bytes()).hexdigest().encode()
    assert binding_bytes.count(old_digest) == 1, relative
    binding_bytes = binding_bytes.replace(old_digest, new_digest)
  binding_path.write_bytes(binding_bytes)

  assert validator.main(["--profile", "bqca"]) == 1
  assert capsys.readouterr().err.splitlines() == [
      f"ERROR: bqca {source} component {BQCA_KPI_TOTAL} column 0 id"
      " 'qt_deadbeef' is not its derived ID 'qt_d779815a'"
      for source in ("manifest", "bundle")
  ]


def _bqca_spec_role_ids(comp):
  """Return a component's (dimension IDs, metric IDs, sort clauses) in order."""
  fields = _bqca_spec_inner(comp)["fields"]
  if "primaryMetricId" in fields:
    return [], [fields["primaryMetricId"]], []
  if "primaryAxis" in fields:
    return (
        [fields["primaryAxis"]["fieldId"]],
        fields["metricSeries"]["metricIds"],
        fields["primaryAxis"]["sort"],
    )
  if "dimensionId" in fields:
    return [fields["dimensionId"]], [fields["metricId"]], fields["sort"]
  return fields["dimensionIds"], fields["metricIds"], fields["sort"]


def _render_bqca_live_report(
    manifest,
    *,
    names=None,
    source_fields=None,
    aggregations=None,
    concept_ids=None,
    overrides=None,
    prefix_references="",
    report_datasource_id=BQCA_DATASOURCE_ID,
    aliases=((BQCA_DATASOURCE_ALIAS, BQCA_DATASOURCE_ID),),
):
  """Render the manifest as a Looker Studio report textproto dump.

  The layout follows a saved live report: each component lists its dimension
  and its metric `concept_names` in `spec.<kind>.fields` order, each list
  under one `labeled_concepts` entry keyed `primary` (a scorecard's
  `dimensions` is empty), then one `sort` clause per manifest sort clause,
  then one `concept_defs` per column.

  `names`, `source_fields`, `aggregations` and `concept_ids` map
  `(component id, slot)` to the concept name, source field, aggregation or
  concept `id` to write instead of the manifest's; references follow a
  renamed concept. `overrides` maps a component id to the `datasource_id`,
  `dataset_id`, `dimensions` or `metrics` (lists of names), `role_entries`
  (mapping a role to `(key, names)` pairs, where `key=None` omits the `key`
  field), `sort` (`(sort_column, sort_dir)` pairs), `reverse_sort_order`
  (a string or list of strings), or `extra_property_lines` (raw textproto
  lines inside `component_property`) to write instead. `prefix_references`
  prepends a prefix (such as `'t0.'`) to every `concept_names` and
  `sort_column` value. A None `concept_ids`, `datasource_id`, `dataset_id`
  or `report_datasource_id` value omits that field. `aliases` lists the
  report's `(alias, datasource_id)` alias entries.
  """
  names = names or {}
  source_fields = source_fields or {}
  aggregations = aggregations or {}
  concept_ids = concept_ids or {}
  overrides = overrides or {}
  out = [
      "report_config {",
      "  shareable {",
      '    name: "BQCA \\342\\200\\224 Prompt & Response Logging"',
      "  }",
  ]
  for page in manifest["pages"]:
    out += ["  page {", f'    page_id: "{page["id"]}"', "    page {"]
    for comp in page["components"]:
      columns = _bqca_spec_columns(comp)
      concept = {
          column["id"]: names.get((comp["id"], slot), column["id"])
          for slot, column in enumerate(columns)
      }
      dimension_ids, metric_ids, clauses = _bqca_spec_role_ids(comp)
      default_rev = (
          ["false"] if comp["chart_type"] in ("bar_chart", "pie_chart") else []
      )
      own = {
          "datasource_id": comp["datasource_id"],
          "dataset_id": comp["datasource_id"],
          "dimensions": [concept[column_id] for column_id in dimension_ids],
          "metrics": [concept[column_id] for column_id in metric_ids],
          "role_entries": {},
          "sort": [
              (concept[clause["sortFieldId"]], clause["sortDir"])
              for clause in clauses
          ],
          "reverse_sort_order": default_rev,
          "extra_property_lines": [],
          **overrides.get(comp["id"], {}),
      }
      out.append("      component_config {")
      if own["datasource_id"] is not None:
        out.append(f'        datasource_id: "{own["datasource_id"]}"')
      out += [
          f'        component_id: "{comp["component_id"]}"',
          "        property_config {",
          "          component_property {",
          "            dataset {",
          "              dataset_type: DATASOURCE",
      ]
      if own["dataset_id"] is not None:
        out.append(f'              dataset_id: "{own["dataset_id"]}"')
      out.append("            }")
      for role in ("dimensions", "metrics"):
        out.append(f"            {role} {{")
        entries = own["role_entries"].get(
            role, [("primary", own[role])] if own[role] else []
        )
        for entry_key, entry_names in entries:
          out.append("              labeled_concepts {")
          if entry_key is not None:
            out.append(f'                key: "{entry_key}"')
          out += [
              "                value {",
              *(
                  f'                  concept_names: "{prefix_references}{n}"'
                  for n in entry_names
              ),
              "                }",
              "              }",
          ]
        out.append("            }")
      for sort_column, sort_dir in own["sort"]:
        out += [
            "            sort {",
            f"              sort_dir: {sort_dir}",
            f'              sort_column: "{prefix_references}{sort_column}"',
            "            }",
        ]
      rev_values = own["reverse_sort_order"]
      if isinstance(rev_values, str):
        rev_values = [rev_values]
      for rev_val in rev_values or []:
        out.append(f"            reverse_sort_order: {rev_val}")
      for extra_line in own["extra_property_lines"]:
        out.append(f"            {extra_line}")
      out += ["          }", "        }"]
      for slot, column in enumerate(columns):
        key = (comp["id"], slot)
        name = concept[column["id"]]
        concept_id = concept_ids.get(key, f"t0.{name}")
        field = source_fields.get(key, column["fieldName"])
        aggregation = aggregations.get(key, column.get("aggregation"))
        out.append("        concept_defs {")
        if concept_id is not None:
          out.append(f'          id: "{concept_id}"')
        out += [
            f'          name: "{name}"',
            '          namespace: "t0"',
            "          query_time_transformation {",
            "            data_transformation {",
            f'              source_field_name: "{field}"',
        ]
        if aggregation is not None:
          out.append(f"              aggregation: {aggregation}")
        out += ["            }", "          }", "        }"]
      out.append("      }")
    out += ["    }", "  }"]
  out.append("  report {")
  if report_datasource_id is not None:
    out.append(f'    datasource_id: "{report_datasource_id}"')
  out.append("    resource {")
  for alias, alias_datasource_id in aliases:
    out += [
        "      datasource_alias {",
        f'        datasource_id: "{alias_datasource_id}"',
        f'        alias: "{alias}"',
        "      }",
    ]
  out += ["    }", "  }", "}"]
  return "\n".join(out) + "\n"


def _render_bqca_live_report_with_copied_kpi(manifest, component_id, column_id):
  """Render the manifest plus a copy of its first scorecard.

  The copy gets `component_id` and one column named `column_id`, so its only
  concept repeats no other name in the report.
  """
  extended = json.loads(json.dumps(manifest))
  first_page = extended["pages"][0]["components"]
  copy = json.loads(json.dumps(first_page[0]))
  copy["component_id"] = component_id
  inner = _bqca_spec_inner(copy)
  inner["dataset"]["columns"][0]["id"] = column_id
  inner["fields"]["primaryMetricId"] = column_id
  first_page.append(copy)
  return _render_bqca_live_report(extended)


def _render_bqca_live_report_without_first_kpi(manifest):
  """Render the manifest without its first scorecard."""
  trimmed = json.loads(json.dumps(manifest))
  trimmed["pages"][0]["components"].pop(0)
  return _render_bqca_live_report(trimmed)


def _bqca_concepts_differ(where, live_only, manifest_only):
  """Return the live audit's error for a component whose concepts differ."""
  return (
      f"component {where} concept_defs (name, source_field_name,"
      " aggregation) differ from the manifest's (id, fieldName, aggregation):"
      f" live only {live_only}, manifest only {manifest_only}"
  )


BQCA_STALE_DATASOURCE_ID = "00000000-0000-4000-8000-000000000000"
BQCA_KPI_TOTAL = "kpi_total_tokens (cd-vWRq2EdpQyI)"
BQCA_KPI_INPUT = "kpi_input_tokens (cd-gQM6-qRztkI)"
BQCA_OVER_TIME = "chart_tokens_over_time (cd-kM4cFH3pweE)"
BQCA_OVER_TIME_METRICS = ["qt_af86847d", "qt_42018c0c", "qt_0db8b52b"]
BQCA_SQL_TABLE = "tbl_prompt_response_sql (cd-oUw97rpumWM)"
BQCA_SQL_TABLE_DIMENSIONS = [
    "qt_40f9b407",
    "qt_b684d2ed",
    "qt_54bd6219",
    "qt_7a28e588",
    "qt_800b7f96",
    "qt_bbf261e0",
    "qt_1ada8666",
    "qt_abb7920b",
    "qt_a04329d9",
    "qt_42c77379",
]
BQCA_SQL_TABLE_DIMENSIONS_SWAPPED = [
    "qt_b684d2ed",
    "qt_40f9b407",
    *BQCA_SQL_TABLE_DIMENSIONS[2:],
]

# One case per guard of `validate_live_report_textproto`: a function that
# renders the defective report from the manifest, and the exact errors it
# must produce, each without its "live report <path>: " prefix.
BQCA_LIVE_REPORT_DEFECTS = [
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            names={
                ("kpi_total_tokens", 0): "met",
                ("kpi_input_tokens", 0): "met",
            },
        ),
        [
            "component cd-vWRq2EdpQyI concept_defs name"
            f" 'met'{BQCA_NOT_QT_SUFFIX}",
            "component cd-gQM6-qRztkI concept_defs name"
            f" 'met'{BQCA_NOT_QT_SUFFIX}",
            "concept_defs name 'met' in component cd-gQM6-qRztkI repeats the"
            " one in cd-vWRq2EdpQyI; names must be unique across the report",
            _bqca_concepts_differ(
                BQCA_KPI_TOTAL,
                [("met", "_total_tokens_", "SUM")],
                [("qt_d779815a", "_total_tokens_", "SUM")],
            ),
            f"component {BQCA_KPI_TOTAL} metrics concept_names ['met'] !="
            " manifest metric IDs ['qt_d779815a']",
            _bqca_concepts_differ(
                BQCA_KPI_INPUT,
                [("met", "_input_tokens_", "SUM")],
                [("qt_c90b69a4", "_input_tokens_", "SUM")],
            ),
            f"component {BQCA_KPI_INPUT} metrics concept_names ['met'] !="
            " manifest metric IDs ['qt_c90b69a4']",
        ],
        id="generic_name_shared_by_two_components",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, concept_ids={("kpi_total_tokens", 0): "t1.qt_d779815a"}
        ),
        [
            "component cd-vWRq2EdpQyI concept_defs id 't1.qt_d779815a' is not"
            " 't0.qt_d779815a'"
        ],
        id="concept_id_in_another_namespace",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, concept_ids={("kpi_total_tokens", 0): None}
        ),
        [
            "component cd-vWRq2EdpQyI concept_defs id None is not"
            " 't0.qt_d779815a'"
        ],
        id="concept_id_missing",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, names={("chart_tokens_over_time", 2): "qt_af86847d"}
        ),
        [
            "component cd-kM4cFH3pweE repeats concept_defs name 'qt_af86847d'",
            _bqca_concepts_differ(
                BQCA_OVER_TIME,
                [("qt_af86847d", "_output_tokens_", "SUM")],
                [("qt_42018c0c", "_output_tokens_", "SUM")],
            ),
            f"component {BQCA_OVER_TIME} metrics concept_names ['qt_af86847d',"
            " 'qt_af86847d', 'qt_0db8b52b'] != manifest metric IDs"
            f" {BQCA_OVER_TIME_METRICS}",
        ],
        id="name_repeated_inside_a_component",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, names={("kpi_input_tokens", 0): "qt_d779815a"}
        ),
        [
            "concept_defs name 'qt_d779815a' in component cd-gQM6-qRztkI"
            " repeats the one in cd-vWRq2EdpQyI; names must be unique across"
            " the report",
            _bqca_concepts_differ(
                BQCA_KPI_INPUT,
                [("qt_d779815a", "_input_tokens_", "SUM")],
                [("qt_c90b69a4", "_input_tokens_", "SUM")],
            ),
            f"component {BQCA_KPI_INPUT} metrics concept_names"
            " ['qt_d779815a'] != manifest metric IDs ['qt_c90b69a4']",
        ],
        id="qt_name_repeated_by_another_component",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, overrides={"kpi_total_tokens": {"metrics": ["qt_deadbeef"]}}
        ),
        [
            "component cd-vWRq2EdpQyI concept_names 'qt_deadbeef' names none"
            " of its concept_defs",
            "component cd-vWRq2EdpQyI concept_defs name 'qt_d779815a' is not"
            " referenced by any concept_names or sort_column",
            f"component {BQCA_KPI_TOTAL} metrics concept_names"
            " ['qt_deadbeef'] != manifest metric IDs ['qt_d779815a']",
        ],
        id="concept_names_dangles",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {"sort": [("qt_deadbeef", "ASC")]}
            },
        ),
        [
            "component cd-kM4cFH3pweE sort_column 'qt_deadbeef' names none of"
            " its concept_defs",
            f"component {BQCA_OVER_TIME} sort_column ['qt_deadbeef'] !="
            " manifest sortFieldId ['qt_e7eb0560']",
        ],
        id="sort_column_dangles",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, overrides={"kpi_total_tokens": {"metrics": []}}
        ),
        [
            "component cd-vWRq2EdpQyI concept_defs name 'qt_d779815a' is not"
            " referenced by any concept_names or sort_column",
            f"component {BQCA_KPI_TOTAL} metrics concept_names [] != manifest"
            " metric IDs ['qt_d779815a']",
        ],
        id="concept_unreferenced",
    ),
    pytest.param(
        _render_bqca_live_report_without_first_kpi,
        [f"manifest component {BQCA_KPI_TOTAL} is missing"],
        id="manifest_component_missing",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report_with_copied_kpi(
            m, "cd-vWRq2EdpQyI", "qt_0d0d0d0d"
        ),
        ["component cd-vWRq2EdpQyI appears twice"],
        id="component_appears_twice",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report_with_copied_kpi(
            m, "cd-extra000000", "qt_0e0e0e0e"
        ),
        [
            "component cd-extra000000 defines concepts but is not in"
            " spec/bqca_chart_manifest.yaml"
        ],
        id="component_not_in_manifest",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, source_fields={("kpi_input_tokens", 0): "_output_tokens_"}
        ),
        [
            _bqca_concepts_differ(
                BQCA_KPI_INPUT,
                [("qt_c90b69a4", "_output_tokens_", "SUM")],
                [("qt_c90b69a4", "_input_tokens_", "SUM")],
            )
        ],
        id="concept_bound_to_another_field",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, aggregations={("kpi_total_tokens", 0): "AVG"}
        ),
        [
            _bqca_concepts_differ(
                BQCA_KPI_TOTAL,
                [("qt_d779815a", "_total_tokens_", "AVG")],
                [("qt_d779815a", "_total_tokens_", "SUM")],
            )
        ],
        id="concept_with_another_aggregation",
    ),
    pytest.param(
        # chart_tokens_over_time sums `_input_tokens_` in slot 1
        # (qt_af86847d) and `_output_tokens_` in slot 2 (qt_42018c0c); swap
        # the two concept names, whose references follow them.
        lambda m: _render_bqca_live_report(
            m,
            names={
                ("chart_tokens_over_time", 1): "qt_42018c0c",
                ("chart_tokens_over_time", 2): "qt_af86847d",
            },
        ),
        [
            _bqca_concepts_differ(
                BQCA_OVER_TIME,
                [
                    ("qt_42018c0c", "_input_tokens_", "SUM"),
                    ("qt_af86847d", "_output_tokens_", "SUM"),
                ],
                [
                    ("qt_42018c0c", "_output_tokens_", "SUM"),
                    ("qt_af86847d", "_input_tokens_", "SUM"),
                ],
            ),
            f"component {BQCA_OVER_TIME} metrics concept_names ['qt_42018c0c',"
            " 'qt_af86847d', 'qt_0db8b52b'] != manifest metric IDs"
            f" {BQCA_OVER_TIME_METRICS}",
        ],
        id="concept_names_swapped",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, names={("kpi_total_tokens", 0): "qt_0000beef"}
        ),
        [
            _bqca_concepts_differ(
                BQCA_KPI_TOTAL,
                [("qt_0000beef", "_total_tokens_", "SUM")],
                [("qt_d779815a", "_total_tokens_", "SUM")],
            ),
            f"component {BQCA_KPI_TOTAL} metrics concept_names"
            " ['qt_0000beef'] != manifest metric IDs ['qt_d779815a']",
        ],
        id="concept_renamed_to_an_unused_qt_id",
    ),
    pytest.param(
        # The concept_defs stay the manifest's; only the order in which the
        # chart lists its input and output token metrics changes.
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {
                    "metrics": ["qt_42018c0c", "qt_af86847d", "qt_0db8b52b"]
                }
            },
        ),
        [
            f"component {BQCA_OVER_TIME} metrics concept_names ['qt_42018c0c',"
            " 'qt_af86847d', 'qt_0db8b52b'] != manifest metric IDs"
            f" {BQCA_OVER_TIME_METRICS}"
        ],
        id="metric_references_reordered",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "tbl_prompt_response_sql": {
                    "dimensions": BQCA_SQL_TABLE_DIMENSIONS_SWAPPED
                }
            },
        ),
        [
            f"component {BQCA_SQL_TABLE} dimensions concept_names"
            f" {BQCA_SQL_TABLE_DIMENSIONS_SWAPPED} != manifest dimension IDs"
            f" {BQCA_SQL_TABLE_DIMENSIONS}"
        ],
        id="dimension_references_reordered",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {
                    "dimensions": ["qt_af86847d"],
                    "metrics": ["qt_e7eb0560", "qt_42018c0c", "qt_0db8b52b"],
                }
            },
        ),
        [
            f"component {BQCA_OVER_TIME} dimensions concept_names"
            " ['qt_af86847d'] != manifest dimension IDs ['qt_e7eb0560']",
            f"component {BQCA_OVER_TIME} metrics concept_names ['qt_e7eb0560',"
            " 'qt_42018c0c', 'qt_0db8b52b'] != manifest metric IDs"
            f" {BQCA_OVER_TIME_METRICS}",
        ],
        id="dimension_and_metric_trade_roles",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {"sort": [("qt_af86847d", "ASC")]}
            },
        ),
        [
            f"component {BQCA_OVER_TIME} sort_column ['qt_af86847d'] !="
            " manifest sortFieldId ['qt_e7eb0560']"
        ],
        id="sort_column_repointed",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {"sort": [("qt_e7eb0560", "DESC")]}
            },
        ),
        [
            f"component {BQCA_OVER_TIME} sort_dir ['DESC'] != manifest sortDir"
            " ['ASC']"
        ],
        id="sort_direction_flipped",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "kpi_total_tokens": {"datasource_id": BQCA_STALE_DATASOURCE_ID}
            },
        ),
        [
            f"component {BQCA_KPI_TOTAL} datasource_id"
            f" {BQCA_STALE_DATASOURCE_ID!r} != manifest {BQCA_DATASOURCE_ID!r}"
        ],
        id="component_datasource_stale",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, overrides={"kpi_total_tokens": {"datasource_id": None}}
        ),
        [
            f"component {BQCA_KPI_TOTAL} datasource_id None != manifest"
            f" {BQCA_DATASOURCE_ID!r}"
        ],
        id="component_datasource_missing",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "kpi_total_tokens": {"dataset_id": BQCA_STALE_DATASOURCE_ID}
            },
        ),
        [
            f"component {BQCA_KPI_TOTAL} dataset_id"
            f" {BQCA_STALE_DATASOURCE_ID!r} != manifest {BQCA_DATASOURCE_ID!r}"
        ],
        id="component_dataset_stale",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, overrides={"kpi_total_tokens": {"dataset_id": None}}
        ),
        [
            f"component {BQCA_KPI_TOTAL} dataset_id None != manifest"
            f" {BQCA_DATASOURCE_ID!r}"
        ],
        id="component_dataset_missing",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, report_datasource_id=BQCA_STALE_DATASOURCE_ID
        ),
        [
            f"report datasource_id {BQCA_STALE_DATASOURCE_ID!r} != binding"
            f" datasource_id {BQCA_DATASOURCE_ID!r}"
        ],
        id="report_datasource_stale",
    ),
    pytest.param(
        # Only the report-level datasource_id goes; `ds0` and every
        # component still name the binding's data source.
        lambda m: _render_bqca_live_report(m, report_datasource_id=None),
        [
            "report datasource_id is missing; the binding expects"
            f" {BQCA_DATASOURCE_ID!r}"
        ],
        id="report_datasource_missing",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(m, aliases=()),
        [
            "report resource has no data source alias"
            f" {BQCA_DATASOURCE_ALIAS!r}; the binding maps it to datasource_id"
            f" {BQCA_DATASOURCE_ID!r}"
        ],
        id="alias_missing",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m, aliases=(("ds1", BQCA_DATASOURCE_ID),)
        ),
        [
            "report resource has no data source alias"
            f" {BQCA_DATASOURCE_ALIAS!r}; the binding maps it to datasource_id"
            f" {BQCA_DATASOURCE_ID!r}"
        ],
        id="alias_renamed",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            aliases=((BQCA_DATASOURCE_ALIAS, BQCA_STALE_DATASOURCE_ID),),
        ),
        [
            f"data source alias {BQCA_DATASOURCE_ALIAS!r} points at"
            f" {BQCA_STALE_DATASOURCE_ID!r}, not binding datasource_id"
            f" {BQCA_DATASOURCE_ID!r}"
        ],
        id="alias_points_at_another_datasource",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={"kpi_total_tokens": {"metrics": ["t1.qt_d779815a"]}},
        ),
        [
            "component cd-vWRq2EdpQyI concept_names 't1.qt_d779815a' names none"
            " of its concept_defs",
            "component cd-vWRq2EdpQyI concept_defs name 'qt_d779815a' is not"
            " referenced by any concept_names or sort_column",
            f"component {BQCA_KPI_TOTAL} metrics concept_names"
            " ['t1.qt_d779815a'] != manifest metric IDs ['qt_d779815a']",
        ],
        id="concept_names_in_another_namespace",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {"sort": [("t1.qt_e7eb0560", "ASC")]}
            },
        ),
        [
            "component cd-kM4cFH3pweE sort_column 't1.qt_e7eb0560' names none"
            " of its concept_defs",
            f"component {BQCA_OVER_TIME} sort_column ['t1.qt_e7eb0560'] !="
            " manifest sortFieldId ['qt_e7eb0560']",
        ],
        id="sort_column_in_another_namespace",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "kpi_total_tokens": {
                    "role_entries": {
                        "metrics": [("drillDown", ["qt_d779815a"])]
                    }
                }
            },
        ),
        [
            f"component {BQCA_KPI_TOTAL} metrics labeled_concepts"
            " [('drillDown', ['qt_d779815a'])] != manifest"
            " [('primary', ['qt_d779815a'])]"
        ],
        id="labeled_concepts_wrong_key",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "kpi_total_tokens": {
                    "role_entries": {"metrics": [(None, ["qt_d779815a"])]}
                }
            },
        ),
        [
            f"component {BQCA_KPI_TOTAL} metrics labeled_concepts"
            " [('labeled_concepts', [('value', [('concept_names',"
            " 'qt_d779815a')])])] != manifest [('primary', ['qt_d779815a'])]"
        ],
        id="labeled_concepts_missing_key",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {
                    "role_entries": {
                        "metrics": [
                            ("primary", ["qt_af86847d"]),
                            ("primary", ["qt_42018c0c", "qt_0db8b52b"]),
                        ]
                    }
                }
            },
        ),
        [
            f"component {BQCA_OVER_TIME} metrics labeled_concepts"
            " [('primary', ['qt_af86847d']), ('primary', ['qt_42018c0c',"
            " 'qt_0db8b52b'])] != manifest [('primary', ['qt_af86847d',"
            " 'qt_42018c0c', 'qt_0db8b52b'])]"
        ],
        id="labeled_concepts_split_across_entries",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "kpi_total_tokens": {
                    "metrics": [],
                    "extra_property_lines": [
                        'secondary_sort { sort_column: "qt_d779815a" }'
                    ],
                }
            },
        ),
        [
            "component cd-vWRq2EdpQyI sort_column 'qt_d779815a' at"
            " component_property.secondary_sort.sort_column is not a reference"
            " the audit reads",
            "component cd-vWRq2EdpQyI concept_defs name 'qt_d779815a' is not"
            " referenced by any concept_names or sort_column",
            f"component {BQCA_KPI_TOTAL} metrics concept_names [] != manifest"
            " metric IDs ['qt_d779815a']",
        ],
        id="unread_reference_path_under_component_property",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={"chart_tokens_by_agent": {"reverse_sort_order": "true"}},
        ),
        [
            "component chart_tokens_by_agent (cd-kctXIOqrPyQ)"
            " reverse_sort_order True != manifest reverseSortOrder False"
        ],
        id="reverse_sort_order_true_when_manifest_false",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={"chart_tokens_by_agent": {"reverse_sort_order": "1"}},
        ),
        [
            "component chart_tokens_by_agent (cd-kctXIOqrPyQ)"
            " reverse_sort_order ['1'] is not a single true or false"
        ],
        id="reverse_sort_order_invalid_value",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_by_agent": {
                    "reverse_sort_order": ["false", "true"]
                }
            },
        ),
        [
            "component chart_tokens_by_agent (cd-kctXIOqrPyQ)"
            " reverse_sort_order ['false', 'true'] is not a single true or"
            " false"
        ],
        id="reverse_sort_order_repeated",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {
                    "sort": [],
                    "extra_property_lines": [
                        'sort { sort_dir: ASC sort_column: "qt_e7eb0560"'
                        " reverse_sort_order: true }"
                    ],
                }
            },
        ),
        [
            f"component {BQCA_OVER_TIME} sort_column [('sort', [('sort_dir',"
            " 'ASC'), ('sort_column', 'qt_e7eb0560'), ('reverse_sort_order',"
            " 'true')])] != manifest sortFieldId ['qt_e7eb0560']"
        ],
        id="sort_unexpected_field_or_reverse_sort_order",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {
                    "sort": [],
                    "extra_property_lines": [
                        'sort { sort_dir: ASC sort_column: "qt_e7eb0560"'
                        ' sort_column: "qt_af86847d" }'
                    ],
                }
            },
        ),
        [
            f"component {BQCA_OVER_TIME} sort_column [('sort', [('sort_dir',"
            " 'ASC'), ('sort_column', 'qt_e7eb0560'), ('sort_column',"
            " 'qt_af86847d')])] != manifest sortFieldId ['qt_e7eb0560']"
        ],
        id="sort_duplicate_field",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "chart_tokens_over_time": {
                    "sort": [],
                    "extra_property_lines": [
                        "sort { sort_dir: ASC sort_dir: DESC sort_column:"
                        ' "qt_e7eb0560" }'
                    ],
                }
            },
        ),
        [
            f"component {BQCA_OVER_TIME} sort_dir [('sort', [('sort_dir',"
            " 'ASC'), ('sort_dir', 'DESC'), ('sort_column', 'qt_e7eb0560')])]"
            " != manifest sortDir ['ASC']"
        ],
        id="sort_duplicate_dir",
    ),
    pytest.param(
        lambda m: _render_bqca_live_report(
            m,
            overrides={
                "kpi_total_tokens": {
                    "extra_property_lines": ['sort: "ignored"']
                }
            },
        ),
        [
            f"component {BQCA_KPI_TOTAL} sort_column [('sort', 'ignored')] !="
            " manifest sortFieldId []"
        ],
        id="sort_scalar_on_scorecard",
    ),
]


def test_live_report_textproto_audit_accepts_the_synced_report(
    tmp_path, capsys
):
  """`--live-report-textproto` passes a report that matches the contracts."""
  validator = _load_dashboard_module("validate_contracts")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )

  # The stdlib reader keeps repeated fields, decodes escapes, and joins
  # adjacent strings.
  assert validator.parse_report_textproto(
      'a: "x \\342\\200\\224 y" b { c: 1 } b < c: "2" "3" > d: [E1, E2];'
  ) == [
      ("a", "x \u2014 y"),
      ("b", [("c", "1")]),
      ("b", [("c", "23")]),
      ("d", "E1"),
      ("d", "E2"),
  ]

  # The rendered report has the saved report's reference layout: 21
  # scorecards with one metric each, 13 charts with 23 dimension and 21 more
  # metric references, and 13 sort clauses (one ASC).
  text = _render_bqca_live_report(manifest)
  assert text.count("concept_names:") == 23 + 42
  assert text.count('key: "primary"') == 21 + 2 * 13
  assert text.count("sort_dir: DESC") == 12
  assert text.count("sort_dir: ASC") == 1
  fixed = tmp_path / "fixed.txtpb"
  fixed.write_text(text, encoding="utf-8")
  assert validator.validate_live_report_textproto(fixed) == []
  assert (
      validator.main(
          ["--profile", "bqca", "--live-report-textproto", str(fixed)]
      )
      == 0
  )
  assert f"ok: live report validated ({fixed})" in capsys.readouterr().out

  # `t0.`-prefixed references (`concept_names: "t0.qt_..."` and
  # `sort_column: "t0.qt_..."`) are normalized to their `qt_` IDs and accepted.
  text_t0 = _render_bqca_live_report(manifest, prefix_references="t0.")
  assert text_t0.count('concept_names: "t0.qt_') == 23 + 42
  assert text_t0.count('sort_column: "t0.qt_') == 13
  fixed_t0 = tmp_path / "fixed_t0.txtpb"
  fixed_t0.write_text(text_t0, encoding="utf-8")
  assert validator.validate_live_report_textproto(fixed_t0) == []
  assert (
      validator.main(
          ["--profile", "bqca", "--live-report-textproto", str(fixed_t0)]
      )
      == 0
  )
  assert f"ok: live report validated ({fixed_t0})" in capsys.readouterr().out

  # A component whose manifest `reverseSortOrder` is True requires
  # `reverse_sort_order: true` in the live report and rejects `false`.
  manifest_rev = json.loads(json.dumps(manifest))
  for comp in _bqca_page_components(manifest_rev):
    if comp["id"] == "chart_tokens_by_agent":
      comp["spec"]["barChart"]["fields"]["primaryAxis"][
          "reverseSortOrder"
      ] = True
  rev_true = tmp_path / "rev_true.txtpb"
  rev_true.write_text(
      _render_bqca_live_report(
          manifest_rev,
          overrides={"chart_tokens_by_agent": {"reverse_sort_order": "true"}},
      ),
      encoding="utf-8",
  )
  assert (
      validator.validate_live_report_textproto(
          rev_true, manifest_override=manifest_rev
      )
      == []
  )
  assert validator.validate_live_report_textproto(
      fixed, manifest_override=manifest_rev
  ) == [
      f"live report {fixed}: component chart_tokens_by_agent (cd-kctXIOqrPyQ)"
      " reverse_sort_order False != manifest reverseSortOrder True"
  ]

  with pytest.raises(SystemExit) as adk_only:
    validator.main(["--profile", "adk", "--live-report-textproto", str(fixed)])
  assert adk_only.value.code == 2


@pytest.mark.parametrize("render, expected", BQCA_LIVE_REPORT_DEFECTS)
def test_live_report_textproto_audit_rejects_each_defect(
    render, expected, tmp_path, capsys
):
  """#check-can-fail: each live report defect yields exactly its errors."""
  validator = _load_dashboard_module("validate_contracts")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  path = tmp_path / "report.txtpb"
  path.write_text(render(manifest), encoding="utf-8")
  errors = [f"live report {path}: {message}" for message in expected]
  assert validator.validate_live_report_textproto(path) == errors
  assert (
      validator.main(
          ["--profile", "bqca", "--live-report-textproto", str(path)]
      )
      == 1
  )
  assert capsys.readouterr().err.splitlines() == [f"ERROR: {e}" for e in errors]


def test_live_report_textproto_audit_fails_closed_on_unreadable_input(
    tmp_path, capsys
):
  """#check-can-fail: text that is not protobuf text format, or no file."""
  validator = _load_dashboard_module("validate_contracts")
  truncated = tmp_path / "truncated.txtpb"
  truncated.write_text("report_config {\n  page {\n", encoding="utf-8")
  error = (
      f"live report {truncated}: cannot read it as protobuf text format:"
      " unterminated message: missing '}'"
  )
  assert validator.validate_live_report_textproto(truncated) == [error]
  assert (
      validator.main(
          ["--profile", "bqca", "--live-report-textproto", str(truncated)]
      )
      == 1
  )
  assert capsys.readouterr().err.splitlines() == [f"ERROR: {error}"]
  absent = tmp_path / "absent.txtpb"
  errors = validator.validate_live_report_textproto(absent)
  assert len(errors) == 1, errors
  assert errors[0].startswith(
      f"live report {absent}: cannot read it as protobuf text format: "
  ), errors


def test_live_report_textproto_reader_keeps_to_its_documented_subset(
    tmp_path, capsys
):
  """#check-can-fail: the reader's limits hold, and each fails closed.

  It reads 64 levels of nested messages and skips a leading byte order mark.
  It rejects a 65th level, an extension or `Any` field name, and a message
  list without a `:` as text it cannot read, without a traceback.
  """
  validator = _load_dashboard_module("validate_contracts")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )

  def nested(depth):
    return "a { " * depth + "} " * depth

  message = validator.parse_report_textproto(nested(64))
  for _ in range(64):
    ((name, message),) = message
    assert name == "a"
  assert message == []
  with pytest.raises(
      ValueError, match=r"^messages nest more than 64 levels deep$"
  ):
    validator.parse_report_textproto(nested(65))

  assert validator.parse_report_textproto("\ufeffa: 1") == [("a", "1")]
  with_bom = tmp_path / "with_bom.txtpb"
  with_bom.write_bytes(
      b"\xef\xbb\xbf" + _render_bqca_live_report(manifest).encode("utf-8")
  )
  assert validator.validate_live_report_textproto(with_bom) == []

  for name, text, reason in (
      ("deep", nested(2000), "messages nest more than 64 levels deep"),
      (
          "extension_name",
          "[type.googleapis.com/x.Y] { a: 1 }\n",
          "expected a field name, got '['",
      ),
      (
          "list_without_colon",
          "a [ { b: 1 }, { b: 2 } ]\n",
          "field 'a' needs ':' before its value",
      ),
  ):
    path = tmp_path / f"{name}.txtpb"
    path.write_text(text, encoding="utf-8")
    error = (
        f"live report {path}: cannot read it as protobuf text format: {reason}"
    )
    assert validator.validate_live_report_textproto(path) == [error], name
    assert (
        validator.main(
            ["--profile", "bqca", "--live-report-textproto", str(path)]
        )
        == 1
    ), name
    assert capsys.readouterr().err.splitlines() == [f"ERROR: {error}"], name


def test_live_report_textproto_audit_names_a_chart_type_it_cannot_read(
    tmp_path,
):
  """#check-can-fail: a manifest kind with unknown references is named.

  The audit does not compare the live roles of such a component with an
  empty or guessed set of manifest references.
  """
  validator = _load_dashboard_module("validate_contracts")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  path = tmp_path / "report.txtpb"
  path.write_text(_render_bqca_live_report(manifest), encoding="utf-8")
  for comp in _bqca_page_components(manifest):
    if comp["id"] == "kpi_total_tokens":
      comp["chart_type"] = "gauge"
  assert validator.validate_live_report_textproto(
      path, manifest_override=manifest
  ) == [
      f"live report {path}: {message}"
      for message in (
          _bqca_concepts_differ(
              BQCA_KPI_TOTAL, [("qt_d779815a", "_total_tokens_", "SUM")], []
          ),
          f"component {BQCA_KPI_TOTAL} has chart_type 'gauge', whose"
          " dimension, metric and sort references the audit cannot read",
      )
  ]


BQCA_LIVE_DUMP_FIXTURE = (
    ROOT / "tests/fixtures/bqca_live_report_42e79a1f_sanitized.textproto"
)
BQCA_LIVE_DUMP_REMAPPED_FIXTURE = (
    ROOT
    / "tests/fixtures/bqca_live_report_42e79a1f_sanitized_remapped.textproto"
)


def test_live_report_textproto_audit_on_a_sanitized_live_dump(capsys):
  """The audit's verdict on a cut of a real dump of the live template.

  The fixture keeps, from a dump saved before the live sync, every field the
  audit reads. Its 65 concepts keep their generic names, and its
  report-level data source is not the copy's own, so the audit fails with
  209 errors of known kinds. Its twin applies the two sync actions to the
  same text, renaming the 65 concepts and repointing the report-level data
  source and nothing else, and passes.
  """
  validator = _load_dashboard_module("validate_contracts")
  label = f"live report {BQCA_LIVE_DUMP_FIXTURE}: "
  errors = validator.validate_live_report_textproto(BQCA_LIVE_DUMP_FIXTURE)
  assert all(error.startswith(label) for error in errors), errors
  messages = [error[len(label) :] for error in errors]
  assert len(messages) == 209
  kinds = {
      # Every concept has a generic name (`met`, `dim`, `m0`, `d0`, ...),
      "is not a qt_ column ID": 65,
      # most of which an earlier component already uses,
      "names must be unique across the report": 49,
      # so no component has the manifest's concepts or references,
      "differ from the manifest's (id, fieldName, aggregation)": 34,
      "metrics concept_names": 34,
      "dimensions concept_names": 13,
      "sort_column [": 13,
      # while every sort direction already matches;
      "sort_dir [": 0,
      # and the report-level data source is the source report's.
      "report datasource_id": 1,
  }
  found = []
  for message in messages:
    matching = [kind for kind in kinds if kind in message]
    assert len(matching) == 1, message
    found.append(matching[0])
  assert {kind: found.count(kind) for kind in kinds} == kinds
  assert (
      f"report datasource_id {BQCA_STALE_DATASOURCE_ID!r} != binding"
      f" datasource_id {BQCA_DATASOURCE_ID!r}"
  ) in messages
  assert (
      validator.main(
          [
              "--profile",
              "bqca",
              "--live-report-textproto",
              str(BQCA_LIVE_DUMP_FIXTURE),
          ]
      )
      == 1
  )
  assert capsys.readouterr().err.splitlines() == [
      f"ERROR: {error}" for error in errors
  ]

  assert (
      validator.validate_live_report_textproto(BQCA_LIVE_DUMP_REMAPPED_FIXTURE)
      == []
  )
  assert (
      validator.main(
          [
              "--profile",
              "bqca",
              "--live-report-textproto",
              str(BQCA_LIVE_DUMP_REMAPPED_FIXTURE),
          ]
      )
      == 0
  )

  # The twin has the same fields in the same order; only the concept names
  # and IDs, the references to them, and the report-level data source differ.
  for fixture in (BQCA_LIVE_DUMP_FIXTURE, BQCA_LIVE_DUMP_REMAPPED_FIXTURE):
    assert (
        fixture.read_text(encoding="utf-8").count("reverse_sort_order: false")
        == 10
    )

  def leaves(message, path=()):
    for name, value in message:
      if isinstance(value, list):
        yield from leaves(value, path + (name,))
      else:
        yield path + (name,), value

  original, remapped = (
      leaves(
          validator.parse_report_textproto(fixture.read_text(encoding="utf-8"))
      )
      for fixture in (BQCA_LIVE_DUMP_FIXTURE, BQCA_LIVE_DUMP_REMAPPED_FIXTURE)
  )
  changed = {}
  for (path, value), (twin_path, twin_value) in zip(
      original, remapped, strict=True
  ):
    assert path == twin_path
    if value != twin_value:
      changed[path[-1]] = changed.get(path[-1], 0) + 1
  assert changed == {
      "name": 65,
      "id": 65,
      "concept_names": 65,
      "sort_column": 13,
      "datasource_id": 1,
  }


@pytest.mark.parametrize("profile", ["adk", "bqca"])
def test_hydrated_links_keep_the_template_data_source_fields(profile):
  """Every 1-click link keeps `refreshFields=false`, its only such parameter.

  Keeping the template's fields keeps the fields its charts bind to.
  """
  hydration = _load_hydration_module()
  report = yaml.safe_load(
      (DASHBOARD / hydration.PROFILES[profile]["report"]).read_text()
  )
  alias = report["data_source_alias"]
  link = hydration.build_link(
      "customer-project-123",
      "agent_analytics",
      "agent_events",
      "billing-project-123",
      "Customer report",
      profile=profile,
  )
  parameters = _link_parameters(link)
  assert parameters["c.reportId"] == [report["report_id"]]
  assert {
      key: value
      for key, value in parameters.items()
      if key.endswith(".refreshFields")
  } == {f"ds.{alias}.refreshFields": ["false"]}
  query = urllib.parse.urlparse(link).query.split("&")
  assert f"ds.{alias}.refreshFields=false" in query
  assert "refreshFields=true" not in link


def test_bqca_artifacts_never_name_event_types_bqca_does_not_log():
  for relative in (
      *BQCA_SQL_FILES,
      "tools/gen_bqca_events_tmpl.py",
      "tools/hydrate_dashboard.py",
      "bindings/bqca_report_template.yaml",
      "bindings/bqca_template_bindings.yaml",
      "spec/bqca_product_contract.yaml",
      "spec/bqca_chart_manifest.yaml",
      "spec/bqca_dashboard_template.json",
      "docs/index.html",
      "docs/bqca/index.html",
      "docs/app.mjs",
      "docs/configurator.mjs",
      "docs/report-config.mjs",
  ):
    text = (DASHBOARD / relative).read_text()
    for event_type in BQCA_UNLOGGED_EVENT_TYPES:
      assert event_type not in text, f"{relative} names {event_type}"


def test_bqca_deep_link_page_is_published_with_the_site():
  page = (DASHBOARD / "docs/index.html").read_text()
  bqca_page = (DASHBOARD / "docs/bqca/index.html").read_text()
  styles = (DASHBOARD / "docs/styles.css").read_text()

  assert bqca_page.startswith(
      "<!doctype html>\n<!-- Generated by tools/render_web_config.py from"
      " docs/index.html; do not edit. -->\n"
  )
  assert '<html lang="en" data-bqca-default-profile="bqca">' in bqca_page
  assert 'src="../app.mjs"' in bqca_page
  assert 'href="../styles.css"' in bqca_page
  assert 'href="../favicon.svg"' in bqca_page
  assert (
      'href="https://googlecloudplatform.github.io/'
      'BigQuery-Agent-Analytics-SDK/bqca/"' in bqca_page
  )
  assert 'id="profile-bqca" aria-pressed="true"' in bqca_page
  assert 'placeholder="my-project.my_dataset.bqca_prompt_response_logs"' in (
      bqca_page
  )
  for source in (page, bqca_page):
    assert "data-bqaa-app-initialized" not in source
  assert re.findall(r'\bid="([^"]+)"', bqca_page) == re.findall(
      r'\bid="([^"]+)"', page
  )

  # The BQCA hero, fact pills, CTA button, and notice describe the dedicated
  # 7-page tool-free BQCA Looker Studio template while keeping CLI flags out of
  # the primary hero lede and prominently warning about pending external access.
  assert "7-page tool-free BQCA Looker Studio dashboard" in bqca_page
  assert "34 BQCA-native charts &amp; KPIs" in bqca_page
  assert "7 tool-free report pages" in bqca_page
  assert "Self-Hosted Streamlit BQCA Dashboard" in bqca_page
  assert "Create my BQCA dashboard" in bqca_page
  hero_lede = bqca_page.split('<p class="lede" data-profile-only="bqca">', 1)[
      1
  ].split("</p>", 1)[0]
  assert "--custom-sql-out" not in hero_lede
  assert "sql/bqca_events_v1.sql.tmpl" not in hero_lede
  assert 'id="profile-adk" aria-pressed="true"' in page
  assert 'id="bqca-template-note"' in page
  note = page.split('id="bqca-template-note"', 1)[1].split("</aside>", 1)[0]
  assert 'data-profile-only="bqca" hidden>' in note
  assert (
      "Public link sharing is on; the outside-account copy check is pending"
      in note
  )
  assert "2026-10-09" in note
  assert "Dedicated 7-page tool-free BQCA Looker Studio template" in note
  assert '<details class="advanced-bqca-options">' in note
  assert 'id="bqca-column-id-note"' in note
  assert BQCA_LIVE_SYNC_ISSUE in note
  assert "legacy generic column IDs" in note
  assert "re-create any report created" in note
  assert BQCA_REPORT_ID in note
  assert "dashboards/looker_studio/sql/bqca_events_v1.sql.tmpl" in note
  assert "--profile bqca --custom-sql-out" in note
  assert "dashboards/streamlit" in note
  bqca_note_tag = bqca_page.split('id="bqca-template-note"', 1)[1]
  assert (
      bqca_note_tag.split(">", 1)[0]
      .rstrip()
      .endswith('data-profile-only="bqca"')
  ), "the /bqca/ page shows the disclosure before app.mjs runs"
  assert re.search(
      r"#form-status\s*\{[^}]*overflow-wrap:\s*anywhere", styles
  ), "P3-7: #form-status must wrap long identifiers on 375px viewports"


def test_bqca_hydration_link_names_and_defaults():
  hydration = _load_hydration_module()
  assert hydration.profile_default_table("adk") == "agent_events"
  assert hydration.profile_default_table("bqca") == BQCA_DEFAULT_TABLE
  assert (
      hydration.default_report_name("agent_analytics")
      == "BigQuery Agent Analytics — agent_analytics"
  )
  assert (
      hydration.default_report_name("ca_logs", "bqca")
      == "BigQuery Conversational Analytics (BQCA) — ca_logs"
  )

  adk_link = hydration.build_link(
      "customer-project-123",
      "agent_analytics",
      "agent_events",
      "billing-project-123",
      "Customer BQAA",
  )
  assert adk_link == hydration.build_link(
      "customer-project-123",
      "agent_analytics",
      "agent_events",
      "billing-project-123",
      "Customer BQAA",
      profile="adk",
  )
  assert _link_parameters(adk_link)["ds.ds230.datasourceName"] == [
      "BQAA Events — agent_analytics"
  ]

  parameters = _link_parameters(
      hydration.build_link(
          "customer-project-123",
          "ca_logs",
          BQCA_DEFAULT_TABLE,
          "billing-project-123",
          "Customer BQCA",
          profile="bqca",
      )
  )
  assert parameters == {
      "c.reportId": [BQCA_REPORT_ID],
      "c.mode": ["view"],
      "r.reportName": ["Customer BQCA"],
      "ds.ds0.datasourceName": ["BQCA Events — ca_logs"],
      "ds.ds0.billingProjectId": ["billing-project-123"],
      "ds.ds0.sqlReplace": [
          ",".join(
              [
                  SHARED_SENTINELS[0],
                  "customer-project-123",
                  SHARED_SENTINELS[1],
                  "ca_logs",
                  SHARED_SENTINELS[2],
                  BQCA_DEFAULT_TABLE,
              ]
          )
      ],
      "ds.ds0.refreshFields": ["false"],
  }
  with pytest.raises(ValueError, match="reserved template sentinel"):
    hydration.build_link(
        "xsentinelbqaaevents",
        "ca_logs",
        BQCA_DEFAULT_TABLE,
        "billing-project-123",
        "Customer BQCA",
        profile="bqca",
    )

  custom = hydration.custom_query_sql(
      "customer-project-123", "ca_logs", BQCA_DEFAULT_TABLE, "bqca"
  )
  assert custom.startswith(
      "-- Generated by tools/hydrate_dashboard.py --profile bqca from"
      " sql/bqca_events_v1.sql.tmpl for"
      " customer-project-123.ca_logs.bqca_prompt_response_logs.\n"
  )
  assert "{{" not in custom
  assert (
      custom.count(
          "FROM `customer-project-123.ca_logs.bqca_prompt_response_logs`"
      )
      == 1
  )
  assert "@DS_START_DATE" in custom
  assert hydration.custom_query_sql(
      "customer-project-123", "agent_analytics", "agent_events"
  ).startswith(
      "-- Generated by tools/hydrate_dashboard.py --profile adk from"
      " sql/events_v1.sql.tmpl"
  )
  assert (
      hydration.data_profile_sql(
          "customer-project-123", "agent_analytics", "agent_events", "adk"
      )
      is None
  )


def test_bqca_data_profile_summary_warns_on_empty_or_unattributed_data():
  hydration = _load_hydration_module()

  summary, warnings = hydration.summarize_data_profile(
      _profile_rows(events=40, attributed=30, fast_path=5)
  )
  assert summary[0].startswith(
      "BQCA data profile (last 30 days): 40 events (INVOCATION_STARTING=40,"
  )
  assert "data-agent attribution: 30 of 40 events (75.0%)" in summary[1]
  assert "fast-path events: 5" in summary[1]
  assert warnings == []

  _, warnings = hydration.summarize_data_profile(_profile_rows(0, 0))
  assert len(warnings) == 1
  assert "no BQCA Prompt & Response Logging events" in warnings[0]

  _, warnings = hydration.summarize_data_profile(_profile_rows(12, 0))
  assert len(warnings) == 1
  assert "data-agent-id" in warnings[0]


def test_bqca_hydration_gates_then_runs_a_capped_data_profile(
    monkeypatch, capsys, tmp_path
):
  hydration = _load_hydration_module()
  fake, calls = _fake_bq_query([[], _profile_rows(40, 30, fast_path=5)])
  monkeypatch.setattr(hydration, "bq_query", fake)
  sql_out = tmp_path / "bqca_custom_query.sql"

  assert (
      hydration.main(
          [
              "--project",
              "customer-project-123",
              "--dataset",
              "ca_logs",
              "--profile",
              "bqca",
              "--custom-sql-out",
              str(sql_out),
          ]
      )
      == 0
  )
  out, err = capsys.readouterr()

  # The shared structural gate runs uncapped first; the advisory data
  # profile follows under the default 10 GiB byte cap.
  assert [call["maximum_bytes_billed"] for call in calls] == [
      None,
      10 * 1024**3,
  ]
  assert calls[0]["sql"] == hydration.table_preflight_sql(
      "customer-project-123", "ca_logs", BQCA_DEFAULT_TABLE
  )
  assert calls[1]["sql"] == hydration.data_profile_sql(
      "customer-project-123", "ca_logs", BQCA_DEFAULT_TABLE
  )
  assert "{{" not in calls[1]["sql"]
  assert {(call["project"], call["location"]) for call in calls} == {
      ("customer-project-123", "US")
  }
  assert "BQCA preflight OK: base event table is compatible" in err
  assert "BQCA data profile (last 30 days): 40 events" in err
  assert "WARNING" not in err
  assert "NOTE: BQCA uses the dedicated 7-page tool-free BQCA template" in err
  assert f"(report {BQCA_REPORT_ID}, alias ds0)" in err
  assert "the outside-account copy check is pending (#515)" in err
  assert "allowlist" not in err
  assert "SECURITY: keep the new report private" in err

  parameters = _link_parameters(out.strip())
  assert parameters["r.reportName"] == [
      "BigQuery Conversational Analytics (BQCA) — ca_logs"
  ]
  assert parameters["ds.ds0.datasourceName"] == ["BQCA Events — ca_logs"]
  assert parameters["ds.ds0.sqlReplace"][0].split(",")[-1] == (
      BQCA_DEFAULT_TABLE
  )
  assert sql_out.read_text() == hydration.custom_query_sql(
      "customer-project-123", "ca_logs", BQCA_DEFAULT_TABLE, "bqca"
  )


def test_bqca_data_profile_failure_or_skip_never_blocks_the_link(
    monkeypatch, capsys
):
  hydration = _load_hydration_module()
  arguments = [
      "--project",
      "customer-project-123",
      "--dataset",
      "ca_logs",
      "--profile",
      "bqca",
      "--table",
      "logs",
  ]

  fake, calls = _fake_bq_query(
      [[], RuntimeError("BigQuery preflight failed: bytes billed exceeded")]
  )
  monkeypatch.setattr(hydration, "bq_query", fake)
  assert hydration.main([*arguments, "--maximum-bytes-billed", "1048576"]) == 0
  out, err = capsys.readouterr()
  assert calls[1]["maximum_bytes_billed"] == 1048576
  assert "WARNING: BQCA data profile not run" in err
  assert _link_parameters(out.strip())["ds.ds0.sqlReplace"][0].endswith(",logs")

  fake, calls = _fake_bq_query([[], _profile_rows(0, 0)])
  monkeypatch.setattr(hydration, "bq_query", fake)
  assert hydration.main(arguments) == 0
  out, err = capsys.readouterr()
  assert "WARNING: no BQCA Prompt & Response Logging events" in err
  assert out.startswith("https://lookerstudio.google.com/reporting/create?")

  fake, calls = _fake_bq_query([[]])
  monkeypatch.setattr(hydration, "bq_query", fake)
  assert hydration.main([*arguments, "--skip-data-profile"]) == 0
  out, err = capsys.readouterr()
  assert len(calls) == 1
  assert "BQCA data profile skipped" in err
  assert out.startswith("https://lookerstudio.google.com/reporting/create?")

  # The structural gate stays blocking: no data profile, no link.
  fake, calls = _fake_bq_query(
      [
          [
              {
                  "problem": "MISSING_COLUMN",
                  "column_name": "attributes",
                  "expected_type": "JSON",
                  "observed_type": None,
              }
          ]
      ]
  )
  monkeypatch.setattr(hydration, "bq_query", fake)
  assert hydration.main(arguments) == 1
  out, err = capsys.readouterr()
  assert len(calls) == 1
  assert out == ""
  assert "is not a compatible BQAA base table" in err


def test_adk_hydration_path_is_unchanged_by_the_bqca_profile(
    monkeypatch, capsys
):
  hydration = _load_hydration_module()
  fake, calls = _fake_bq_query([[]])
  monkeypatch.setattr(hydration, "bq_query", fake)

  assert (
      hydration.main(
          ["--project", "customer-project-123", "--dataset", "agent_analytics"]
      )
      == 0
  )
  out, err = capsys.readouterr()
  assert calls == [
      {
          "project": "customer-project-123",
          "location": "US",
          "sql": hydration.table_preflight_sql(
              "customer-project-123", "agent_analytics", "agent_events"
          ),
          "maximum_bytes_billed": None,
      }
  ]
  assert err.splitlines() == [
      "BQAA preflight OK: base event table is compatible; no views required",
      "SECURITY: keep the new report private until Resource > Manage added"
      " data sources > Edit shows Data credentials: Viewer",
  ]
  assert out == (
      hydration.build_link(
          "customer-project-123",
          "agent_analytics",
          "agent_events",
          "customer-project-123",
          "BigQuery Agent Analytics — agent_analytics",
      )
      + "\n"
  )


@pytest.mark.parametrize(
    "extra",
    [
        ["--maximum-bytes-billed", "0"],
        ["--maximum-bytes-billed", "-5"],
        ["--table", "bad,table"],
        ["--project", "xsentinelbqaaevents"],
    ],
)
def test_bqca_hydration_rejects_invalid_arguments_before_querying(
    monkeypatch, capsys, extra
):
  hydration = _load_hydration_module()
  fake, calls = _fake_bq_query([])
  monkeypatch.setattr(hydration, "bq_query", fake)
  arguments = [
      "--project",
      "customer-project-123",
      "--dataset",
      "ca_logs",
      "--profile",
      "bqca",
      *extra,
  ]
  assert hydration.main(arguments) == 2
  assert calls == []
  assert capsys.readouterr().err.startswith("ERROR: ")

  with pytest.raises(SystemExit):
    hydration.main([*arguments[:4], "--profile", "langchain"])
  assert calls == []


def test_bqca_profile_is_documented_for_contributors_and_users():
  readme = " ".join((DASHBOARD / "README.md").read_text().split())
  manual = " ".join((DASHBOARD / "USER_MANUAL.md").read_text().split())
  impl = " ".join(
      (DASHBOARD / "docs/dashboard-implementation.md").read_text().split()
  )
  for fragment in (
      "## BQCA Prompt & Response Logging profile",
      "BigQuery-Agent-Analytics-SDK/bqca/",
      "&profile=bqca",
      "--profile bqca",
      "--custom-sql-out",
      "--maximum-bytes-billed",
      "--skip-data-profile",
      "sql/bqca_events_v1.template.sql",
      "tools/gen_bqca_events_tmpl.py",
      "tools/render_web_config.py",
      "tools/validate_contracts.py",
      "BQCA uses a dedicated 7-page tool-free template",
      BQCA_REPORT_ID,
      "spec/bqca_product_contract.yaml",
      "spec/bqca_chart_manifest.yaml",
      "Offline Looker Studio report layout & datasource specification bundle",
      "PENDING_EXTERNAL_IDENTITY_CHECK",
      "all tool-usage pages, tool-latency series, and tool-error charts are omitted",
      "turns are keyed by `invocation_id`",
      "total turns (`invocation_id`), completed turns (`completed_turn_id`)",
      "Distinct Error Signatures",
  ):
    assert fragment in readme, f"README.md must document {fragment!r}"
  assert "session counts are turn counts" not in readme
  for fragment in (
      "## BQCA Prompt & Response Logging",
      "BigQuery-Agent-Analytics-SDK/bqca/",
      "&profile=bqca",
      "--profile bqca",
      "--skip-data-profile",
      "dedicated 7-page tool-free BQCA Looker Studio template",
      BQCA_REPORT_ID,
      "turns are keyed by `invocation_id`",
      "Distinct Error Signatures",
      "Event-Level Prompt, Response & Extracted SQL",
  ):
    assert fragment in manual, f"USER_MANUAL.md must document {fragment!r}"
  assert "session counts are turn counts" not in manual
  for fragment in (
      "## Dedicated 7-page BQCA Prompt & Response Logging template",
      BQCA_REPORT_ID,
      BQCA_DATASOURCE_ID,
      "spec/bqca_product_contract.yaml",
      "spec/bqca_chart_manifest.yaml",
      "tools/validate_contracts.py --profile bqca",
      "dashboards/streamlit/",
  ):
    assert (
        fragment in impl
    ), f"docs/dashboard-implementation.md must document {fragment!r}"
  for relative, text in (("README.md", readme), ("USER_MANUAL.md", manual)):
    for event_type in BQCA_UNLOGGED_EVENT_TYPES:
      assert event_type not in text, f"{relative} names {event_type}"


def test_staleness_parser_rejects_malformed_or_nested_attestations(capsys):
  """P2-5 & N5 & M08: stdlib attestation parser rejects missing keys, missing files (rc=2), and unverified PASSING status."""
  spec = importlib.util.spec_from_file_location(
      "check_external_access_staleness",
      ROOT / "scripts" / "check_external_access_staleness.py",
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)

  # Nested 6-space `status:` inside `controls:` must not shadow the 2-space
  # `status:` under `external_access_verification:`.
  nested_yaml = (
      "external_access_verification:\n"
      "  controls:\n"
      "    - method: check\n"
      "      status: CHILD_STATUS_MUST_BE_IGNORED\n"
      '  next_due_date: "2026-11-08"\n'
      "  status: PENDING_EXTERNAL_IDENTITY_CHECK\n"
      "  tracking_issue: GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#515\n"
      "known_live_issues: []\n"
  )
  assert module.read_attestation_fields(nested_yaml) == {
      "next_due_date": "2026-11-08",
      "status": "PENDING_EXTERNAL_IDENTITY_CHECK",
      "tracking_issue": "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#515",
  }

  # M08: status: PASSING needs both link_access: PUBLIC and a dated
  # link_access_verified_date. The second case is the BQCA template's current
  # state: the owner turned on public link sharing but no canary has passed.
  for link_access, verified_date in (
      ("PRIVATE", '"2026-10-09"'),
      ("PUBLIC", "null"),
  ):
    with pytest.raises(SystemExit, match="link_access: PUBLIC"):
      module.read_attestation_fields(
          f"link_access: {link_access}\n"
          "external_access_verification:\n"
          "  controls:\n"
          "    - method: external_identity_link_access_check\n"
          f"      link_access_verified_date: {verified_date}\n"
          '  next_due_date: "2026-11-08"\n'
          "  status: PASSING\n"
          "  tracking_issue:"
          " GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#515\n"
      )

  # N5: missing --attestation-path returns exit code 2 with stderr message and no traceback.
  rc = module.main(
      ["--attestation-path", "/tmp/nonexistent_attestation_515.yaml"]
  )
  captured = capsys.readouterr()
  assert rc == 2
  assert "ERROR: attestation file not found:" in captured.err

  # N5: workflow issue title and body cover both ADK (#445) and BQCA (#515).
  wf_text = (
      ROOT / ".github" / "workflows" / "external-access-staleness.yml"
  ).read_text()
  assert 'title="External access attestation overdue"' in wf_text
  assert "bqca_report_template.yaml (#515)" in wf_text

  # Missing `external_access_verification:` section raises SystemExit.
  with pytest.raises(SystemExit, match="external_access_verification"):
    module.read_attestation_fields("report_id: 123\n")

  # Missing required 2-space key (`tracking_issue`) raises SystemExit.
  with pytest.raises(SystemExit, match="tracking_issue"):
    module.read_attestation_fields(
        "external_access_verification:\n"
        '  next_due_date: "2026-11-08"\n'
        "  status: FAILING\n"
        "known_live_issues: []\n"
    )


def test_bqca_events_sql_semantic_turn_and_error_edge_cases():
  """P2-5 & N1/R3-1 & R3-N2 & N4 & R4-1..R4-5: verify Looker Studio BQCA SQL turn-grain, deduplication, error, and final-SQL semantics."""
  logical = (DASHBOARD / "sql/bqca_events_v1.sql.tmpl").read_text()
  contract = yaml.safe_load(
      (DASHBOARD / "spec/bqca_product_contract.yaml").read_text()
  )
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  styles_css = (DASHBOARD / "docs/styles.css").read_text()

  # (1) Multi-turn single-session_id & blank/NULL invocation_id turns (R4-1, R4-2):
  # trace_resolved inherits any non-blank invocation_id on the same trace_id
  # before falling back to trace_id -> session_id -> timestamp while preserving
  # NULL when invocation_id IS NULL, and turn_partition_key isolates NULL
  # invocation_id rows via SHA256 digest.
  assert "WITH trace_resolved AS (" in logical
  assert (
      "IF(\n"
      "      invocation_id IS NULL,\n"
      "      NULL,\n"
      "      COALESCE(\n"
      "        NULLIF(TRIM(invocation_id), ''),\n"
      "        IF(\n"
      "          NULLIF(TRIM(trace_id), '') IS NULL,\n"
      "          NULL,\n"
      "          FIRST_VALUE(NULLIF(TRIM(invocation_id), '') IGNORE NULLS) OVER (\n"
      "            PARTITION BY NULLIF(TRIM(trace_id), '')\n"
      "            ORDER BY timestamp ASC, event_type ASC, NULLIF(TRIM(invocation_id), '') ASC\n"
      "            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING\n"
      "          )\n"
      "        ),\n"
      "        NULLIF(TRIM(trace_id), ''),\n"
      "        NULLIF(TRIM(session_id), ''),\n"
      "        CAST(timestamp AS STRING)\n"
      "      )\n"
      "    ) AS turn_id"
  ) in logical
  assert (
      "event_type = 'INVOCATION_COMPLETED' AND turn_id IS NOT NULL," in logical
  )
  assert (
      "PARTITION BY\n"
      "          turn_id,\n"
      "          IF(event_type = 'INVOCATION_COMPLETED', 1, 0)" in logical
  )
  assert "TO_HEX(\n          SHA256(" in logical
  assert "'__null_invocation__:'" in logical
  assert (
      "IF(raw_turn_complete_rn = 1, turn_id, NULL) AS completed_turn_id"
      in logical
  )

  # R4-3, R4-N1, P3-2: raw_conversation_id, raw_data_agent_id, and
  # raw_explicit_persona trim whitespace before NULLIF(..., '').
  for json_path in (
      '$.session_metadata.state."conversation-id"',
      '$.session_metadata.state."data-agent-id"',
      "$.session_metadata.state.custom_labels.persona",
  ):
    assert (
        f"NULLIF(\n      TRIM(\n        JSON_VALUE(attributes, '{json_path}')\n      ),\n      ''\n    )"
        in logical
        or f"NULLIF(\n      TRIM(\n        JSON_VALUE(\n          attributes, '{json_path}'\n        )\n      ),\n      ''\n    )"
        in logical
    ), json_path

  # R4-5: #bqca-template-note body font size is 1rem (16px).
  assert (
      "#bqca-template-note {\n"
      "  margin: 0 0 16px;\n"
      "  padding: 13px 15px;\n"
      "  border-radius: 10px;\n"
      "  font-size: 1rem;\n"
      "  line-height: 1.5;"
  ) in styles_css

  comp_by_id = {c["id"]: c for c in manifest["components"]}
  assert comp_by_id["kpi_total_sessions"]["field"] == "invocation_id"
  assert comp_by_id["kpi_total_sessions"]["aggregation"] == "COUNT_DISTINCT"
  assert comp_by_id["kpi_turn_completes"]["field"] == "completed_turn_id"
  assert comp_by_id["kpi_turn_completes"]["aggregation"] == "COUNT_DISTINCT"
  assert (
      comp_by_id["kpi_distinct_errors"]["label"] == "Distinct Error Signatures"
  )
  assert (
      comp_by_id["kpi_distinct_errors"]["title"] == "Distinct Error Signatures"
  )
  for turn_chart_id in (
      "chart_turns_by_agent",
      "chart_turns_by_persona",
      "chart_persona_breakdown",
      "chart_fast_path_pie",
  ):
    assert (
        "invocation_id" in comp_by_id[turn_chart_id]["metrics"]
    ), turn_chart_id

  # (2) Turn-grain fast_path and data_agent_id/conversation_id/persona propagation
  # across all events in a non-null turn while short-circuiting turn_id IS NULL.
  assert (
      "IF(\n"
      "      turn_id IS NULL,\n"
      "      raw_fast_path,\n"
      "      LOGICAL_OR(raw_fast_path) OVER (\n"
      "        PARTITION BY turn_partition_key\n"
      "      )\n"
      "    ) AS fast_path"
  ) in logical
  for attr_col in (
      "raw_data_agent_id",
      "raw_conversation_id",
      "raw_explicit_persona",
      "raw_email_persona",
  ):
    assert (
        f"FIRST_VALUE({attr_col} IGNORE NULLS) OVER (\n"
        "          PARTITION BY turn_partition_key" in logical
        or f"FIRST_VALUE({attr_col} IGNORE NULLS) OVER (\n"
        "        PARTITION BY turn_partition_key" in logical
    ), attr_col

  # (3) 3-condition error_message synthesis: status='ERROR' with NULL error_message
  # or *_ERROR events synthesize '[<event_type>: status=<status>]' so Looker Studio
  # COUNT(error_message) and COUNT_DISTINCT(error_message) count all 3 conditions,
  # and chart_errors_by_agent is a bar_chart on error_message COUNT.
  assert (
      "IF(\n"
      "      raw_is_error,\n"
      "      COALESCE(\n"
      "        NULLIF(TRIM(error_message), ''),\n"
      "        CONCAT(\n"
      "          '[',\n"
      "          IFNULL(NULLIF(TRIM(event_type), ''), 'UNKNOWN_EVENT'),\n"
      "          ': status=',\n"
      "          IFNULL(NULLIF(TRIM(status), ''), 'ERROR'),\n"
      "          ']'\n"
      "        )\n"
      "      ),\n"
      "      NULL\n"
      "    ) AS error_message"
  ) in logical
  assert comp_by_id["chart_errors_by_agent"]["chart_type"] == "bar_chart"
  assert comp_by_id["chart_errors_by_agent"]["dimensions"] == ["data_agent_id"]
  assert comp_by_id["chart_errors_by_agent"]["metrics"] == ["error_message"]

  # (4) Accepted draft followed by workflow-nudged final AGENT_RESPONSE:
  # raw_agent_response_rn orders AGENT_RESPONSE events by timestamp DESC, span_id DESC
  # within each turn and extracted_sql only populates on raw_agent_response_rn = 1,
  # while raw_agent_response_rn is excluded from the outer projection.
  assert (
      "ORDER BY timestamp DESC, span_id DESC\n"
      "      ),\n"
      "      NULL\n"
      "    ) AS raw_agent_response_rn"
  ) in logical
  assert (
      "IF(\n"
      "    event_type = 'AGENT_RESPONSE' AND raw_agent_response_rn = 1,\n"
      "    REGEXP_EXTRACT(agent_response_text, r'(?is)```sql\\s*(.*?)"
  ) in logical
  assert "SELECT\n  * EXCEPT (raw_agent_response_rn)," in logical
  assert len(contract["pages"]) == 7


def test_bqca_json_template_bundle_and_compatibility_profile_parity():
  """Verify the portable Looker Studio JSON template bundle, BQCA compatibility profile, and M03/M04/M07/M08/M12/M14/M15/M16/R4-N2/N8 mutation guards."""
  validator = _load_dashboard_module("validate_contracts")
  adk_compat = json.loads(
      (DASHBOARD / "spec/compatibility_profile.json").read_text()
  )
  bqca_compat = json.loads(
      (DASHBOARD / "spec/bqca_compatibility_profile.json").read_text()
  )
  bundle = json.loads(
      (DASHBOARD / "spec/bqca_dashboard_template.json").read_text()
  )
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text()
  )
  contract = yaml.safe_load(
      (DASHBOARD / "spec/bqca_product_contract.yaml").read_text()
  )
  binding = yaml.safe_load(
      (DASHBOARD / "bindings/bqca_report_template.yaml").read_text()
  )
  sql_text = (DASHBOARD / "sql/bqca_events_v1.template.sql").read_text()

  # 1. Compatibility profile parity
  assert bqca_compat["source_object"] == BQCA_DEFAULT_TABLE
  assert bqca_compat["source_object_type"] == "BASE TABLE"
  assert bqca_compat["generated_views_required"] is False
  assert bqca_compat["required_columns"] == adk_compat["required_columns"]
  assert len(bqca_compat["required_columns"]) == 15
  assert tuple(bqca_compat["allowed_event_types"]) == BQCA_EVENT_TYPES
  assert tuple(bqca_compat["excluded_event_types"]) == BQCA_UNLOGGED_EVENT_TYPES

  # 2. JSON template bundle structure & parity with bqca_chart_manifest.yaml
  assert bundle["report_id"] == BQCA_REPORT_ID
  assert bundle["datasource_id"] == BQCA_DATASOURCE_ID
  assert bundle["data_source_alias"] == BQCA_DATASOURCE_ALIAS
  assert bundle["page_count"] == 7
  assert bundle["scorecard_count"] == 21
  assert bundle["chart_count"] == 13
  assert bundle["total_component_count"] == 34
  assert len(bundle["pages"]) == 7
  assert len(bundle["components"]) == 34
  assert bundle["datasource"]["field_count"] == 41
  assert bundle["datasource"]["fields"] == manifest["datasource"]["fields"]
  assert any(
      f["name"] == "_completed_turn_id_" for f in bundle["datasource"]["fields"]
  )
  assert (
      contract["surface"]["dashboard_template_bundle"]
      == "spec/bqca_dashboard_template.json"
  )
  assert (
      contract["surface"]["compatibility_profile"]
      == "spec/bqca_compatibility_profile.json"
  )

  # 3. Mutation detection on bundle (title, label/field/aggregation M12, and N8 grid_position)
  tampered_bundle = json.loads(json.dumps(bundle))
  tampered_bundle["pages"][0]["components"][0]["title"] = "Tampered Title"
  bundle_errors = validator.validate_bqca(bundle_override=tampered_bundle)
  assert any("kpi_total_tokens" in e for e in bundle_errors), bundle_errors
  assert any(
      "template_bundle_sha256" in e for e in bundle_errors
  ), bundle_errors

  # M12: mutating scorecard field/aggregation/label on bundle is caught even if sha is ignored
  tampered_m12 = json.loads(json.dumps(bundle))
  tampered_m12["pages"][0]["components"][0]["aggregation"] = "AVG"
  m12_errors = validator.validate_bqca(bundle_override=tampered_m12)
  assert any(
      "kpi_total_tokens" in e and "'aggregation'" in e for e in m12_errors
  ), m12_errors

  # N8: overlapping grid_position cells or out-of-bounds columns are caught
  tampered_grid = json.loads(json.dumps(bundle))
  tampered_grid["pages"][0]["components"][1]["grid_position"] = dict(
      tampered_grid["pages"][0]["components"][0]["grid_position"]
  )
  grid_errors = validator.validate_bqca(bundle_override=tampered_grid)
  assert any(
      "grid cell" in e and "overlap" in e for e in grid_errors
  ), grid_errors

  # M03, M04, M07, M14, M15, M16, R4-N2, r4-22, r4-23, r4-24, r3-08, R4-1..R4-3:
  # mutating SQL semantic expressions is caught by validate_bqca even when all
  # SHA-256 digests are re-pinned to the mutated SQL and bundle.
  def _validate_with_repinned_sql_sha(mutated_sql: str) -> list[str]:
    new_sql_sha = hashlib.sha256(mutated_sql.encode("utf-8")).hexdigest()
    repinned_bundle = json.loads(json.dumps(bundle))
    repinned_bundle["datasource"]["sql_sha256"] = new_sql_sha
    new_bundle_sha = hashlib.sha256(
        (json.dumps(repinned_bundle, indent=2) + "\n").encode("utf-8")
    ).hexdigest()
    repinned_binding = json.loads(json.dumps(binding))
    repinned_binding["reviewed_template_sql"]["sha256"] = new_sql_sha
    repinned_binding["live_template_verification"][
        "repository_sql_sha256"
    ] = new_sql_sha
    repinned_binding["live_template_verification"][
        "template_bundle_sha256"
    ] = new_bundle_sha
    return validator.validate_bqca(
        sql_override=mutated_sql,
        bundle_override=repinned_bundle,
        binding_override=repinned_binding,
    )

  for mut_label, old_frag, new_frag in (
      (
          "fast_path attribute extraction",
          "LOWER(JSON_VALUE(attributes, '$.fast_path')) = 'true'",
          "LOWER(JSON_VALUE(attributes, '$.wrong_fast_path')) = 'true'",
      ),
      (
          "turn_latency_ms deduplication gate",
          "IF(\n      raw_turn_complete_rn = 1,\n      SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64),\n      NULL\n    ) AS turn_latency_ms",
          "SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64) AS turn_latency_ms",
      ),
      (
          "data_agent_id turn-window propagation",
          "FIRST_VALUE(raw_data_agent_id IGNORE NULLS) OVER (",
          "LAST_VALUE(raw_data_agent_id IGNORE NULLS) OVER (",
      ),
      (
          "M14 (raw_turn_complete_rn >= 1)",
          "raw_turn_complete_rn = 1",
          "raw_turn_complete_rn >= 1",
      ),
      (
          "M15 (DESC NULLS LAST -> ASC NULLS LAST)",
          "DESC NULLS LAST",
          "ASC NULLS LAST",
      ),
      (
          "M16 (drop trace_id fallback)",
          "NULLIF(TRIM(trace_id), ''),\n        NULLIF(TRIM(session_id), '')",
          "NULLIF(TRIM(session_id), '')",
      ),
      (
          "R4-N2a (completed_turn_id NULL when rn=1)",
          "IF(raw_turn_complete_rn = 1, turn_id, NULL) AS completed_turn_id",
          "IF(raw_turn_complete_rn = 1, NULL, turn_id) AS completed_turn_id",
      ),
      (
          "R4-N2b (turn_latency_ms 0.0 when rn=1)",
          "IF(\n      raw_turn_complete_rn = 1,\n      SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64),\n      NULL\n    ) AS turn_latency_ms",
          "IF(raw_turn_complete_rn = 1, 0.0, SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64)) AS turn_latency_ms",
      ),
      (
          "r4-22 (comment-spoofed completed_turn_id gate)",
          "IF(raw_turn_complete_rn = 1, turn_id, NULL) AS completed_turn_id",
          "-- IF(raw_turn_complete_rn = 1, turn_id, NULL) AS completed_turn_id\n    IF(TRUE, turn_id, NULL) AS completed_turn_id",
      ),
      (
          "r4-23 (comment-spoofed turn_latency_ms gate)",
          "IF(\n      raw_turn_complete_rn = 1,\n      SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64),\n      NULL\n    ) AS turn_latency_ms",
          "-- IF( raw_turn_complete_rn = 1, SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64), NULL ) AS turn_latency_ms\n    IF(TRUE, SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64), NULL) AS turn_latency_ms",
      ),
      (
          "R4-1 (drop per-trace invocation_id inheritance)",
          "FIRST_VALUE(NULLIF(TRIM(invocation_id), '') IGNORE NULLS)",
          "FIRST_VALUE(NULL IGNORE NULLS)",
      ),
      (
          "R4-2 (drop turn_id IS NOT NULL on raw_turn_complete_rn)",
          "event_type = 'INVOCATION_COMPLETED' AND turn_id IS NOT NULL",
          "event_type = 'INVOCATION_COMPLETED'",
      ),
      (
          "R4-3 (drop TRIM on raw_data_agent_id)",
          "TRIM(\n        JSON_VALUE(attributes, '$.session_metadata.state.\"data-agent-id\"')\n      )",
          "JSON_VALUE(attributes, '$.session_metadata.state.\"data-agent-id\"')",
      ),
      (
          "r4-24 (drop ENDS_WITH _ERROR from raw_is_error)",
          "OR ENDS_WITH(UPPER(TRIM(IFNULL(event_type, ''))), '_ERROR')",
          "OR FALSE",
      ),
      (
          "R4-N2c (partition raw_turn_complete_rn by session_id instead of"
          " turn_id)",
          "PARTITION BY\n          turn_id,\n          IF(event_type ="
          " 'INVOCATION_COMPLETED', 1, 0)",
          "PARTITION BY\n          session_id,\n          IF(event_type ="
          " 'INVOCATION_COMPLETED', 1, 0)",
      ),
  ):
    mut_sql = sql_text.replace(old_frag, new_frag)
    assert mut_sql != sql_text, mut_label
    sql_mut_errors = _validate_with_repinned_sql_sha(mut_sql)
    assert sql_mut_errors, (mut_label, "expected errors with repinned SHA-256")
    assert any("behavioral oracle" in e for e in sql_mut_errors), (
        mut_label,
        sql_mut_errors,
    )

  # r3-08: dropping SHA256 payload digest from null-invocation partition key is caught
  r3_08_sql = re.sub(
      r"TO_HEX\(\s*SHA256\(\s*CONCAT\(.*?error_message,\s*''\)\s*\)\s*\)\s*\)",
      "'static_no_hash'",
      sql_text,
      flags=re.DOTALL,
  )
  assert r3_08_sql != sql_text
  r3_08_errors = _validate_with_repinned_sql_sha(r3_08_sql)
  assert any("behavioral oracle" in e for e in r3_08_errors), r3_08_errors

  # M08: setting external_access_verification.status = "PASSING" fails while
  # the copy canary is unverified, even though link_access is PUBLIC.
  tampered_binding = json.loads(json.dumps(binding))
  tampered_binding["external_access_verification"]["status"] = "PASSING"
  m08_errors = validator.validate_bqca(binding_override=tampered_binding)
  assert any(
      "external_access_verification.status is PASSING" in e for e in m08_errors
  ), m08_errors
  # Once both controls pass and the incident is resolved, PASSING validates,
  # and still requires link_access: PUBLIC.
  verified_binding = json.loads(json.dumps(tampered_binding))
  for control in verified_binding["external_access_verification"]["controls"]:
    if control["method"] == "permissions_api_link_role_check":
      control["last_result"] = "LINK_VIEWER_ALLUSERS_PRESENT"
    else:
      control["last_result"] = "PASSED"
      control["link_access_verified_date"] = "2026-10-09"
  for issue in verified_binding["known_live_issues"]:
    issue["status"] = "RESOLVED"
  assert validator.validate_bqca(binding_override=verified_binding) == []
  verified_binding["link_access"] = "PRIVATE"
  assert any(
      "external_access_verification.status is PASSING" in e
      for e in validator.validate_bqca(binding_override=verified_binding)
  )

  tampered_compat = json.loads(json.dumps(bqca_compat))
  tampered_compat["allowed_event_types"].append("TOOL_COMPLETED")
  compat_errors = validator.validate_bqca(compat_override=tampered_compat)
  assert any("allowed_event_types" in e for e in compat_errors), compat_errors
