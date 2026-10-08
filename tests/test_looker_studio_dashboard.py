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
  assert "path: dashboard/looker_studio/docs" in workflow
  assert "pages: write" in workflow
  assert "id-token: write" in workflow
  assert (
      "actions/deploy-pages@"
      "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
  )


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
BQCA_REPORT_ID = "1ffb0888-20ea-451f-aeb8-69fc37973335"
BQCA_DATASOURCE_ID = "4f17a2b4-f79a-4a52-aaa1-5f65e49ca1cc"
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
      "IFNULL(UPPER(status) = 'ERROR', FALSE)\n"
      "      OR error_message IS NOT NULL\n"
      "      OR ENDS_WITH(event_type, '_ERROR')"
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

  # R2-P2-3: is_turn_complete and turn_latency_ms deduplicate multiple
  # INVOCATION_COMPLETED rows per non-blank invocation_id via ROW_NUMBER().
  assert logical.count("NULLIF(TRIM(invocation_id), '') IS NOT NULL") == 2
  assert (
      logical.count("PARTITION BY NULLIF(TRIM(invocation_id), ''), event_type")
      == 2
  )
  assert (
      len(
          re.findall(
              r"ORDER BY\s+SAFE_CAST\(JSON_VALUE\(latency_ms,"
              r" '\$\.total_ms'\) AS FLOAT64\) DESC NULLS LAST,\s+timestamp"
              r" DESC,\s+span_id",
              logical,
          )
      )
      == 2
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
  rendered = (DASHBOARD / "sql/bqca_events_v1.template.sql").read_bytes()
  digest = hashlib.sha256(rendered).hexdigest()
  pages_digest = validator.canonical_pages_sha256(contract["pages"])
  manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()

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

  # The live evidence names the exact SQL, pages, and manifest it was gathered for.
  assert bqca["reviewed_template_sql"]["sha256"] == digest
  evidence = bqca["live_template_verification"]
  assert evidence == {
      "verified_date": "2026-10-08",
      "repository_sql_sha256": digest,
      "pages_sha256": pages_digest,
      "manifest_sha256": manifest_digest,
      "method": [
          "data_studio_web_service_publish_datasource",
          "data_studio_web_service_get_block_datasource",
          "lego_midtier_service_execute_query",
          "lego_midtier_service_render_dashboard_pdf",
      ],
      "publish_datasource": {
          "health": "HEALTHY",
          "versions": {
              "hydrated_bqca_table": 1791494305139,
              "canonical_sentinel_table": 1791494402606,
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
      "last_attested_date": evidence["verified_date"],
      "evidence": (
          "bindings/bqca_report_template.yaml#live_template_verification"
      ),
  }

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
  assert manifest["datasource"]["field_count"] == 40
  assert len(manifest["datasource"]["fields"]) == 40
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


def test_bqca_artifacts_never_name_event_types_bqca_does_not_log():
  for relative in (
      *BQCA_SQL_FILES,
      "tools/gen_bqca_events_tmpl.py",
      "tools/hydrate_dashboard.py",
      "bindings/bqca_report_template.yaml",
      "bindings/bqca_template_bindings.yaml",
      "spec/bqca_product_contract.yaml",
      "spec/bqca_chart_manifest.yaml",
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
  assert "Template not yet publicly shared for external accounts" in note
  assert "Dedicated 7-page tool-free BQCA Looker Studio template" in note
  assert '<details class="advanced-bqca-options">' in note
  assert BQCA_REPORT_ID in note
  assert "dashboard/looker_studio/sql/bqca_events_v1.sql.tmpl" in note
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
      "all tool-usage pages, tool-latency series, and tool-error charts are omitted",
      "session counts are turn counts",
  ):
    assert fragment in readme, f"README.md must document {fragment!r}"
  for fragment in (
      "## BQCA Prompt & Response Logging",
      "BigQuery-Agent-Analytics-SDK/bqca/",
      "&profile=bqca",
      "--profile bqca",
      "--skip-data-profile",
      "dedicated 7-page tool-free BQCA Looker Studio template",
      BQCA_REPORT_ID,
      "session counts are turn counts",
  ):
    assert fragment in manual, f"USER_MANUAL.md must document {fragment!r}"
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
