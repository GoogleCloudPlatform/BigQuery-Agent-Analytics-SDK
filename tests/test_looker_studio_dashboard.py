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
