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

"""Tests for the Power BI dashboard: drift, parity, schema, docs, CI guard.

Mirrors tests/test_looker_studio_dashboard.py for the Power BI (PBIP) port.
Schema-dependent tests run only when BQAA_POWER_BI_SCHEMAS_DIR is set; a guard
test verifies the CI workflow cannot silently skip them.
"""

import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POWER_BI = ROOT / "dashboards" / "power_bi"
LOOKER = ROOT / "dashboards" / "looker_studio"


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------


def _load_tool_module(name):
  module_path = POWER_BI / "tools" / f"{name}.py"
  if not module_path.exists():
    pytest.skip(f"tool module not found: {module_path}")
  spec = importlib.util.spec_from_file_location(f"power_bi_{name}", module_path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _power_bi_generated():
  """Return True if the generated Power BI project exists."""
  return (POWER_BI / "BQAA_Agent_Analytics.SemanticModel").exists()


def _skip_if_not_generated():
  if not _power_bi_generated():
    pytest.skip(
        "Power BI generated files not present yet " "(S1/S2 not merged)"
    )


# ---------------------------------------------------------------------------
# Drift tests (§7.5 S3)
# ---------------------------------------------------------------------------


class TestDrift:
  """Generator --check must pass on the committed generated files."""

  def test_check_passes_on_clean_tree(self):
    _skip_if_not_generated()
    gen_path = POWER_BI / "tools" / "generate_pbip.py"
    if not gen_path.exists():
      pytest.skip("generate_pbip.py not found")
    result = subprocess.run(
        [sys.executable, str(gen_path), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"--check failed:\n{result.stdout}\n{result.stderr}"

  def test_check_detects_drift_on_modified_copy(self, tmp_path):
    """A deliberately broken copy in tmp_path must make check_generated_files return drift."""
    _skip_if_not_generated()
    import shutil

    gen_mod = _load_tool_module("generate_pbip")
    assert gen_mod.check_generated_files(power_bi_root=POWER_BI) == []

    tmp_copy_dir = tmp_path / "power_bi"
    shutil.copytree(POWER_BI, tmp_copy_dir)
    report_path = (
        tmp_copy_dir
        / "BQAA_Agent_Analytics.Report"
        / "definition"
        / "report.json"
    )
    broken_obj = json.loads(report_path.read_text())
    broken_obj["pages"] = []
    report_path.write_text(json.dumps(broken_obj, indent=2) + "\n")

    diffs = gen_mod.check_generated_files(power_bi_root=tmp_copy_dir)
    assert (
        diffs
    ), "check_generated_files should return drift errors on a modified copy"
    assert any("report.json" in d for d in diffs)


# ---------------------------------------------------------------------------
# Parity tests (§7.4)
# ---------------------------------------------------------------------------


class TestParity:
  """Chart manifest ↔ visuals ↔ measures ↔ oracle parity."""

  def test_manifest_has_37_charts(self):
    manifest = _load_manifest()
    charts = manifest.get("charts", [])
    assert (
        len(charts) == 37
    ), f"Expected 37 charts in manifest, got {len(charts)}"

  def test_37_unique_product_titles(self):
    manifest = _load_manifest()
    product = _load_product_contract()
    titles = set()
    for chart in manifest.get("charts", []):
      title = _get_product_title(product, chart["id"])
      if title:
        titles.add(title)
    assert (
        len(titles) == 37
    ), f"Expected 37 unique product titles, got {len(titles)}"

  def test_all_chart_ids_under_50_chars(self):
    manifest = _load_manifest()
    for chart in manifest.get("charts", []):
      assert (
          len(chart["id"]) <= 50
      ), f"Chart id '{chart['id']}' exceeds 50 characters"

  def test_oracle_files_exist(self):
    manifest = _load_manifest()
    oracle_dir = LOOKER / "oracle" / "queries"
    for chart in manifest.get("charts", []):
      oracle_path = oracle_dir / f"{chart['id']}.sql"
      assert (
          oracle_path.exists()
      ), f"Oracle file missing for chart '{chart['id']}'"

  def test_visual_names_match_manifest_ids(self):
    _skip_if_not_generated()
    manifest = _load_manifest()
    manifest_ids = {c["id"] for c in manifest.get("charts", [])}
    # Collect all visual names from report pages.
    visual_names = _collect_visual_names()
    # Non-data visual ids from spec.
    non_data_ids = {
        "usage-nondata-1",
        "usage-nondata-2",
        "usage-nondata-3",
        "usage-nondata-4",
        "performance-nondata-1",
        "performance-nondata-2",
        "performance-nondata-3",
        "performance-nondata-4",
        "performance-nondata-5",
    }
    # Also slicer/heading/header-button/table visual names.
    non_parity = non_data_ids | {
        "page-heading",
        "slicer-date",
        "slicer-agent",
        "slicer-user-id",
        "slicer-trace-id",
        "slicer-span-id",
        "reset-filters",
        "drillthrough-back",
        "trace-inspector-table",
    }
    parity_visuals = visual_names - non_parity
    assert parity_visuals == manifest_ids, (
        f"Parity visual mismatch.\n"
        f"Missing: {manifest_ids - parity_visuals}\n"
        f"Extra: {parity_visuals - manifest_ids}"
    )

  def test_visual_names_unique_per_page(self):
    _skip_if_not_generated()
    pages_dir = (
        POWER_BI / "BQAA_Agent_Analytics.Report" / "definition" / "pages"
    )
    if not pages_dir.exists():
      pytest.skip("Report pages not generated")
    for page_dir in sorted(pages_dir.iterdir()):
      if not page_dir.is_dir():
        continue
      visuals_dir = page_dir / "visuals"
      if not visuals_dir.exists():
        continue
      names = set()
      for vdir in visuals_dir.iterdir():
        if not vdir.is_dir():
          continue
        visual_json = vdir / "visual.json"
        if visual_json.exists():
          vobj = json.loads(visual_json.read_text())
          vname = vobj.get("name", vdir.name)
          assert vname not in names, (
              f"Duplicate visual name '{vname}' on page " f"'{page_dir.name}'"
          )
          names.add(vname)

  def test_visual_names_under_50_chars(self):
    _skip_if_not_generated()
    visual_names = _collect_visual_names()
    for vname in visual_names:
      assert len(vname) <= 50, f"Visual name '{vname}' exceeds 50 characters"

  def test_no_distinctcount_only_no_blank(self):
    """TMDL must use DISTINCTCOUNTNOBLANK, never DISTINCTCOUNT."""
    _skip_if_not_generated()
    model_dir = POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    events_path = model_dir / "tables" / "events_v1.tmdl"
    if not events_path.exists():
      pytest.skip("events_v1.tmdl not found")
    text = events_path.read_text()
    assert (
        "DISTINCTCOUNT(" not in text
    ), "Found DISTINCTCOUNT( — must use DISTINCTCOUNTNOBLANK"

  def test_distinctcountnoblank_present(self):
    """At least one DISTINCTCOUNTNOBLANK must appear."""
    _skip_if_not_generated()
    model_dir = POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    events_path = model_dir / "tables" / "events_v1.tmdl"
    if not events_path.exists():
      pytest.skip("events_v1.tmdl not found")
    text = events_path.read_text()
    assert "DISTINCTCOUNTNOBLANK" in text

  def test_29_event_columns_present(self):
    """All 29 events_v1 columns are really declared, bound and typed.

    Reads the actual `column` blocks (`///` description lines are skipped), so
    a comment can never stand in for a declaration.
    """
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    model_dir = POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    events_path = model_dir / "tables" / "events_v1.tmdl"
    if not events_path.exists():
      pytest.skip("events_v1.tmdl not found")
    text = events_path.read_text()
    expected_columns = [
        "timestamp",
        "event_type",
        "agent",
        "session_id",
        "invocation_id",
        "user_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "status",
        "error_message",
        "is_truncated",
        "usage_prompt_tokens",
        "usage_completion_tokens",
        "usage_total_tokens",
        "llm_total_ms",
        "ttft_ms",
        "model_version",
        "tool_completed_name",
        "tool_completed_origin",
        "tool_completed_total_ms",
        "tool_error_name",
        "tool_error_origin",
        "tool_error_total_ms",
        "event_date",
        "event_pk",
        "llm_response_pk",
        "tool_completed_pk",
        "tool_error_pk",
    ]
    columns, parse_errors = val_mod._parse_tmdl_columns(text, "events_v1.tmdl")
    assert not parse_errors, parse_errors
    declared = [col["name"] for col in columns]
    assert len(declared) == len(expected_columns) == 29
    assert sorted(declared) == sorted(expected_columns)
    for col in columns:
      assert col.get("sourceColumn") == col["name"], col
      assert col.get("dataType") in val_mod.VALID_DATA_TYPES, col
    assert val_mod._check_events_columns(text) == []

  def test_50_measures_present(self):
    """All 50 measures are really declared in events_v1.tmdl.

    Reads the actual `measure` declarations (`///` description lines are
    skipped), so a description naming a measure cannot satisfy the check.
    """
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    model_dir = POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    events_path = model_dir / "tables" / "events_v1.tmdl"
    if not events_path.exists():
      pytest.skip("events_v1.tmdl not found")
    text = events_path.read_text()
    expected_measures = [
        "Total Tokens",
        "Total Traces",
        "Total Sessions",
        "Total Users",
        "Total Events",
        "LLM Calls",
        "Tool Completion Events",
        "Distinct Tool Invocations",
        "Tool Errors",
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
        "Traces for Agents With Tokens",
        "KPI Total Tokens",
        "KPI Total Tokens Prior",
        "KPI Total Tokens Change %",
        "KPI Total Traces",
        "KPI Total Traces Prior",
        "KPI Total Traces Change %",
        "KPI Total Sessions",
        "KPI Total Sessions Prior",
        "KPI Total Sessions Change %",
        "KPI Total LLM Calls",
        "KPI Total LLM Calls Prior",
        "KPI Total LLM Calls Change %",
        "KPI Total Users",
        "KPI Total Users Prior",
        "KPI Total Users Change %",
        "KPI Tool Errors",
        "KPI Tool Errors Prior",
        "KPI Tool Errors Change %",
        "Token Usage by Agent",
        "Top 5 Users by Token Consumption",
        "Top 5 Users by Traces",
        "Traces by Agent",
        "Top 5 Agents by Sessions",
        "Tool Invocations",
        "Tool Completions by Agent",
        "Top 5 Users by Sessions",
        "Top 5 Users by Events",
        "Top 5 Agents by Tool Errors",
        "Top 5 Tools by Errors",
        "Top 5 Agents by LLM Calls",
    ]
    declared = [m["name"] for m in val_mod._parse_tmdl_measures(text)]
    assert len(declared) == len(set(declared)) == len(expected_measures) == 50
    assert sorted(declared) == sorted(expected_measures)

  def test_measure_count_is_50(self):
    """Count of measure declarations must be exactly 50."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    model_dir = POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    events_path = model_dir / "tables" / "events_v1.tmdl"
    if not events_path.exists():
      pytest.skip("events_v1.tmdl not found")
    text = events_path.read_text()
    measure_re = re.compile(r"^\s*measure\s+", re.MULTILINE)
    count = len(measure_re.findall(text))
    assert count == 50, f"Expected 50 measures, found {count}"
    assert len(val_mod._parse_tmdl_measures(text)) == count

  def test_no_uuid4_calls(self):
    """uuid.uuid4 must never be called in any tool."""
    tools_dir = POWER_BI / "tools"
    if not tools_dir.exists():
      pytest.skip("tools directory not found")
    for py_file in tools_dir.glob("*.py"):
      text = py_file.read_text()
      assert "uuid4" not in text, f"uuid4 found in {py_file.name}"

  def test_tmdl_no_semicolon_in_sql(self):
    """I8: no ; (or other M-escape hazard) in the SQL expressions.tmdl builds.

    Scans the literals of the real `EventsSelectLines` list and `EventsSql`
    expression.  The test first proves it found the 128 SELECT lines, so it
    cannot pass by matching nothing.
    """
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    model_dir = POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    expr_path = model_dir / "expressions.tmdl"
    if not expr_path.exists():
      pytest.skip("expressions.tmdl not found")
    text = expr_path.read_text()
    select = re.search(r"\bEventsSelectLines\s*=\s*\{", text)
    assert select is not None, "EventsSelectLines not found"
    literals, problem = val_mod._m_string_list(text, select.end())
    assert problem is None, problem
    assert len(literals) == 128
    assert literals[0] == "SELECT"
    assert not [lit for lit in literals if ";" in lit]
    assert val_mod._check_events_sql(text) == []

  def test_listener_matrix_nofilter(self):
    """visualInteractions NoFilter set must match F4 exactly.

    Each page's NoFilter pairs should only reference visuals on that
    page. The 4 F4 exception charts that do NOT listen to User ID are:
    usage-top-5-users-with-most-tokens-consumption and
    usage-token-usage-split-by-agent (on token-consumption page), and
    usage-top-5-users-with-most-traces and usage-traces-split-by-agent
    (on agent-and-sessions page).
    """
    _skip_if_not_generated()
    pages_dir = (
        POWER_BI / "BQAA_Agent_Analytics.Report" / "definition" / "pages"
    )
    if not pages_dir.exists():
      pytest.skip("Report pages not generated")
    # F4 exceptions: these 4 charts do NOT listen to User ID.
    non_uid = {
        "usage-top-5-users-with-most-tokens-consumption",
        "usage-token-usage-split-by-agent",
        "usage-top-5-users-with-most-traces",
        "usage-traces-split-by-agent",
    }
    # Expected NoFilter pairs, keyed by (page, source, target).
    # token-consumption has the 2 token exception charts.
    # agent-and-sessions has the 2 trace exception charts.
    expected_nf = {
        (
            "token-consumption",
            "slicer-user-id",
            "usage-top-5-users-with-most-tokens-consumption",
        ),
        (
            "token-consumption",
            "slicer-user-id",
            "usage-token-usage-split-by-agent",
        ),
        (
            "agent-and-sessions",
            "slicer-user-id",
            "usage-top-5-users-with-most-traces",
        ),
        (
            "agent-and-sessions",
            "slicer-user-id",
            "usage-traces-split-by-agent",
        ),
    }
    actual_nf = set()
    for page_dir in sorted(pages_dir.iterdir()):
      if not page_dir.is_dir():
        continue
      page_json = page_dir / "page.json"
      if not page_json.exists():
        continue
      page_obj = json.loads(page_json.read_text())
      # Collect visual names on this page.
      visuals_dir = page_dir / "visuals"
      page_visuals = set()
      if visuals_dir.exists():
        for vd in visuals_dir.iterdir():
          if vd.is_dir():
            page_visuals.add(vd.name)
      interactions = page_obj.get("visualInteractions", [])
      for i in interactions:
        if i.get("type") == "NoFilter":
          target = i.get("target", "")
          # Only count pairs where the target visual exists
          # on this page (per-page NoFilter set).
          if target in page_visuals:
            actual_nf.add((page_dir.name, i.get("source"), target))
    assert actual_nf == expected_nf, (
        f"NoFilter mismatch.\n"
        f"Expected: {expected_nf}\n"
        f"Actual: {actual_nf}"
    )

  def test_report_window_filter_hidden_and_locked(self):
    """Report window filter must be hidden and locked."""
    _skip_if_not_generated()
    report_json = (
        POWER_BI / "BQAA_Agent_Analytics.Report" / "definition" / "report.json"
    )
    if not report_json.exists():
      pytest.skip("report.json not found")
    obj = json.loads(report_json.read_text())
    filters = obj.get("filterConfig", {}).get("filters", [])
    window_filter = None
    for f in filters:
      if f.get("name") == "bqaa-report-window":
        window_filter = f
        break
    assert (
        window_filter is not None
    ), "Report window filter 'bqaa-report-window' not found"
    assert (
        window_filter.get("isHiddenInViewMode") is True
    ), "Window filter must be hidden"
    assert (
        window_filter.get("isLockedInViewMode") is True
    ), "Window filter must be locked"

  def test_slicer_sync_groups(self):
    """Each page must have all 5 slicer sync groups."""
    _skip_if_not_generated()
    pages_dir = (
        POWER_BI / "BQAA_Agent_Analytics.Report" / "definition" / "pages"
    )
    if not pages_dir.exists():
      pytest.skip("Report pages not generated")
    expected_groups = {
        "bqaa-date",
        "bqaa-agent",
        "bqaa-user-id",
        "bqaa-trace-id",
        "bqaa-span-id",
    }
    for page_dir in sorted(pages_dir.iterdir()):
      if not page_dir.is_dir():
        continue
      visuals_dir = page_dir / "visuals"
      if not visuals_dir.exists():
        continue
      found_groups = set()
      for vdir in visuals_dir.iterdir():
        if not vdir.is_dir():
          continue
        vjson = vdir / "visual.json"
        if not vjson.exists():
          continue
        vobj = json.loads(vjson.read_text())
        sync = vobj.get("visual", {}).get("syncGroup", {})
        group = sync.get("groupName", "")
        if group:
          found_groups.add(group)
      assert found_groups == expected_groups, (
          f"Page '{page_dir.name}': sync groups "
          f"{found_groups} != {expected_groups}"
      )

  def test_eight_pages_present(self):
    """Exactly 8 pages must be present in the report."""
    _skip_if_not_generated()
    pages_dir = (
        POWER_BI / "BQAA_Agent_Analytics.Report" / "definition" / "pages"
    )
    if not pages_dir.exists():
      pytest.skip("Report pages not generated")
    page_dirs = [d for d in pages_dir.iterdir() if d.is_dir()]
    assert len(page_dirs) == 8, f"Expected 8 pages, found {len(page_dirs)}"
    expected_names = {
        "token-consumption",
        "agent-and-sessions",
        "tool-usage",
        "llm-interactions",
        "user-analytics",
        "latency",
        "errors",
        "trace-inspector",
    }
    actual_names = {d.name for d in page_dirs}
    assert actual_names == expected_names

  def test_trace_inspector_columns(self):
    """Trace Inspector table must have exactly 7 columns."""
    _skip_if_not_generated()
    ti_page = (
        POWER_BI
        / "BQAA_Agent_Analytics.Report"
        / "definition"
        / "pages"
        / "trace-inspector"
    )
    if not ti_page.exists():
      pytest.skip("Trace Inspector page not generated")
    visuals_dir = ti_page / "visuals"
    if not visuals_dir.exists():
      pytest.skip("Trace Inspector visuals not generated")
    found_table = False
    for vdir in visuals_dir.iterdir():
      if not vdir.is_dir():
        continue
      vjson = vdir / "visual.json"
      if not vjson.exists():
        continue
      vobj = json.loads(vjson.read_text())
      if vobj.get("name") == "trace-inspector-table":
        found_table = True
        assert (
            vobj.get("visual", {}).get("visualType") == "tableEx"
        ), "Trace Inspector should be tableEx"
    assert found_table, "trace-inspector-table not found"

  def test_no_overlaps_per_page(self):
    """No two visual rects should overlap on any page."""
    _skip_if_not_generated()
    pages_dir = (
        POWER_BI / "BQAA_Agent_Analytics.Report" / "definition" / "pages"
    )
    if not pages_dir.exists():
      pytest.skip("Report pages not generated")
    for page_dir in sorted(pages_dir.iterdir()):
      if not page_dir.is_dir():
        continue
      visuals_dir = page_dir / "visuals"
      if not visuals_dir.exists():
        continue
      rects = []
      for vdir in visuals_dir.iterdir():
        if not vdir.is_dir():
          continue
        vjson = vdir / "visual.json"
        if not vjson.exists():
          continue
        vobj = json.loads(vjson.read_text())
        pos = vobj.get("position", vobj.get("visual", {}).get("position", {}))
        x = pos.get("x")
        y = pos.get("y")
        w = pos.get("width")
        h = pos.get("height")
        if all(v is not None for v in [x, y, w, h]):
          rects.append((x, y, w, h, vobj.get("name")))
      # Check pairwise non-overlap.
      for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
          x1, y1, w1, h1, n1 = rects[i]
          x2, y2, w2, h2, n2 = rects[j]
          if x1 < x2 + w2 and x1 + w1 > x2 and y1 < y2 + h2 and y1 + h1 > y2:
            pytest.fail(
                f"Overlap on page '{page_dir.name}': "
                f"{n1}({x1},{y1},{w1}x{h1}) and "
                f"{n2}({x2},{y2},{w2}x{h2})"
            )


# ---------------------------------------------------------------------------
# Check-can-fail tests (#check-can-fail)
# ---------------------------------------------------------------------------


class TestCheckCanFail:
  """Each validator check must fail on a deliberately broken copy."""

  def test_distinctcount_detected(self, tmp_path):
    """Replacing DISTINCTCOUNTNOBLANK with DISTINCTCOUNT must fail validate_tmdl."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_model = tmp_path / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    shutil.copytree(
        POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition",
        tmp_model,
    )
    events_path = tmp_model / "tables" / "events_v1.tmdl"
    events_path.write_text(
        events_path.read_text().replace("DISTINCTCOUNTNOBLANK", "DISTINCTCOUNT")
    )
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any("DISTINCTCOUNT(" in e for e in errors), errors

  def test_dropped_visual_detected(self, tmp_path):
    """Dropping a visual from a page must break validate_parity."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_report = tmp_path / "BQAA_Agent_Analytics.Report"
    shutil.copytree(POWER_BI / "BQAA_Agent_Analytics.Report", tmp_report)
    dropped = (
        tmp_report
        / "definition"
        / "pages"
        / "token-consumption"
        / "visuals"
        / "usage-total-tokens"
    )
    shutil.rmtree(dropped)
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert any("Missing parity visuals" in e for e in errors), errors

  def test_renamed_measure_detected(self, tmp_path):
    """Renaming a measure must break validate_tmdl."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_model = tmp_path / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    shutil.copytree(
        POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition",
        tmp_model,
    )
    events_path = tmp_model / "tables" / "events_v1.tmdl"
    events_path.write_text(
        events_path.read_text().replace(
            "measure 'Total Tokens'", "measure 'Renamed Tokens'"
        )
    )
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any("missing measure 'Total Tokens'" in e for e in errors), errors

  def test_overlapping_rect_detected(self, tmp_path):
    """An overlapping visual rect in tmp_path must be caught by validate_parity."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_report = tmp_path / "BQAA_Agent_Analytics.Report"
    shutil.copytree(POWER_BI / "BQAA_Agent_Analytics.Report", tmp_report)
    vjson_path = (
        tmp_report
        / "definition"
        / "pages"
        / "token-consumption"
        / "visuals"
        / "usage-total-tokens"
        / "visual.json"
    )
    vobj = json.loads(vjson_path.read_text())
    vobj["position"]["x"] = 0
    vobj["position"]["y"] = 0
    vobj["position"]["width"] = 1280
    vobj["position"]["height"] = 720
    vjson_path.write_text(json.dumps(vobj, indent=2) + "\n")
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert any("Overlap on page" in e for e in errors), errors

  # -- Header buttons, drill-through and theme reference (D12, D17) ----------

  @staticmethod
  def _copy_report(tmp_path):
    import shutil

    tmp_report = tmp_path / "BQAA_Agent_Analytics.Report"
    shutil.copytree(POWER_BI / "BQAA_Agent_Analytics.Report", tmp_report)
    return tmp_report

  @staticmethod
  def _rewrite_json(path, mutate):
    obj = json.loads(path.read_text())
    mutate(obj)
    path.write_text(json.dumps(obj, indent=2) + "\n")

  def test_ux_controls_pass_on_the_generated_report(self):
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    report = POWER_BI / "BQAA_Agent_Analytics.Report"
    assert val_mod._check_ux_controls(report) == []

  def test_dropped_reset_filters_button_detected(self, tmp_path):
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    pages = self._copy_report(tmp_path) / "definition" / "pages"
    shutil.rmtree(pages / "errors" / "visuals" / "reset-filters")
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert "Page 'errors': missing header button 'reset-filters'" in errors

  def test_reset_filters_button_must_clear_all_slicers(self, tmp_path):
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    pages = self._copy_report(tmp_path) / "definition" / "pages"
    path = pages / "latency" / "visuals" / "reset-filters" / "visual.json"
    path.write_text(
        path.read_text().replace("'ClearAllSlicers'", "'ApplyAllSlicers'")
    )
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert any(
        "header button 'reset-filters' must be an actionButton with action"
        " 'ClearAllSlicers'" in e
        for e in errors
    ), errors

  def test_back_button_outside_the_drillthrough_page_detected(self, tmp_path):
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    pages = self._copy_report(tmp_path) / "definition" / "pages"
    shutil.copytree(
        pages / "trace-inspector" / "visuals" / "drillthrough-back",
        pages / "errors" / "visuals" / "drillthrough-back",
    )
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert (
        "Page 'errors': unexpected header button 'drillthrough-back'" in errors
    )

  def test_missing_drillthrough_binding_detected(self, tmp_path):
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    pages = self._copy_report(tmp_path) / "definition" / "pages"
    self._rewrite_json(
        pages / "trace-inspector" / "page.json",
        lambda page: page.pop("pageBinding"),
    )
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert (
        "Page 'trace-inspector': pageBinding must have type 'Drillthrough'"
        in errors
    )

  def test_drillthrough_filter_must_be_created_by_drillthrough(self, tmp_path):
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    pages = self._copy_report(tmp_path) / "definition" / "pages"

    def _user_filter(page):
      page["filterConfig"]["filters"][1]["howCreated"] = "User"

    self._rewrite_json(pages / "trace-inspector" / "page.json", _user_filter)
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert any(
        "must have howCreated 'Drillthrough'" in e for e in errors
    ), errors

  def test_dropped_drillthrough_field_detected(self, tmp_path):
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    pages = self._copy_report(tmp_path) / "definition" / "pages"
    self._rewrite_json(
        pages / "trace-inspector" / "page.json",
        lambda page: page["pageBinding"]["parameters"].pop(),
    )
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert any("drill-through fields" in e for e in errors), errors

  def test_unresolvable_custom_theme_detected(self, tmp_path):
    """The display name "BQAA" does not resolve the registered theme."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    report = self._copy_report(tmp_path)

    def _display_name(rj):
      rj["themeCollection"]["customTheme"]["name"] = "BQAA"

    self._rewrite_json(report / "definition" / "report.json", _display_name)
    errors = val_mod.validate_parity(power_bi_root=tmp_path)
    assert any(
        "customTheme name 'BQAA' is not a registered CustomTheme resource" in e
        for e in errors
    ), errors

  # -- Real declarations, not description lines (F8) -------------------------

  @staticmethod
  def _copy_model_definition(tmp_path):
    import shutil

    tmp_model = tmp_path / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    shutil.copytree(
        POWER_BI / "BQAA_Agent_Analytics.SemanticModel" / "definition",
        tmp_model,
    )
    return tmp_model

  @staticmethod
  def _edit_once(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)}x"
    path.write_text(text.replace(old, new))

  def test_wrong_source_column_detected(self, tmp_path):
    """Editing only `sourceColumn:` fails although the `///` line still agrees."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    tmp_model = self._copy_model_definition(tmp_path)
    events_path = tmp_model / "tables" / "events_v1.tmdl"
    self._edit_once(
        events_path,
        "\t\tsourceColumn: agent\n",
        "\t\tsourceColumn: agent_renamed\n",
    )
    # A substring check would still be satisfied by the description line.
    assert "sourceColumn=agent dataType=string" in events_path.read_text()
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any(
        "column 'agent' has sourceColumn 'agent_renamed'" in e for e in errors
    ), errors

  @pytest.mark.parametrize(
      "new_type, expected",
      [
          ("notatype", "column 'event_type' has invalid dataType 'notatype'"),
          (
              "int64",
              "column 'event_type' has dataType 'int64', expected 'string'",
          ),
      ],
  )
  def test_wrong_data_type_detected(self, tmp_path, new_type, expected):
    """Editing only `dataType:` fails (invalid and valid-but-wrong types)."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    tmp_model = self._copy_model_definition(tmp_path)
    events_path = tmp_model / "tables" / "events_v1.tmdl"
    self._edit_once(
        events_path,
        "\tcolumn event_type\n\t\tdataType: string\n",
        f"\tcolumn event_type\n\t\tdataType: {new_type}\n",
    )
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any(expected in e for e in errors), errors

  def test_description_line_cannot_stand_in_for_a_column(self, tmp_path):
    """A `///` line naming a column is not a declaration of that column."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    tmp_model = self._copy_model_definition(tmp_path)
    events_path = tmp_model / "tables" / "events_v1.tmdl"
    self._edit_once(events_path, "\tcolumn agent\n", "\tcolumn agent_renamed\n")
    assert "/// Column agent " in events_path.read_text()
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any("missing column 'agent'" in e for e in errors), errors
    assert any("unexpected column 'agent_renamed'" in e for e in errors), errors

  def test_description_line_cannot_stand_in_for_a_measure(self, tmp_path):
    """A `///` line naming a measure is not a declaration of that measure."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    tmp_model = self._copy_model_definition(tmp_path)
    events_path = tmp_model / "tables" / "events_v1.tmdl"
    self._edit_once(
        events_path,
        "\tmeasure 'Total Events' = COUNTROWS('events_v1')\n",
        "\tmeasure 'Total Events Renamed' = COUNTROWS('events_v1')\n",
    )
    assert "/// measure Total Events " in events_path.read_text()
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any("missing measure 'Total Events'" in e for e in errors), errors
    assert any(
        "unexpected measure 'Total Events Renamed'" in e for e in errors
    ), errors

  @pytest.mark.parametrize(
      "literal, expected",
      [
          ('"SELECT;",', "contains ';'"),
          ('"SELECT ""x""",', "contains a double quote"),
          ('"SELECT #(lf)",', "unescaped '#('"),
      ],
      ids=["semicolon", "double-quote", "m-escape-sequence"],
  )
  def test_events_sql_hazards_detected(self, tmp_path, literal, expected):
    """I8: the real EventsSelectLines literals are scanned, not a lookalike."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    tmp_model = self._copy_model_definition(tmp_path)
    self._edit_once(
        tmp_model / "expressions.tmdl",
        '\t\t\t\t"SELECT",\n',
        f"\t\t\t\t{literal}\n",
    )
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any(expected in e for e in errors), errors

  @pytest.mark.parametrize(
      "old_frag, new_frag, expected",
      [
          (
              'AnchorSql = "SELECT MAX(',
              'AnchorSql = "SELECT; MAX(',
              "AnchorSql literal 1 contains ';'",
          ),
          (
              'AnchorSql = "SELECT MAX(',
              'AnchorSql = "SELECT ""x"" MAX(',
              "AnchorSql literal 1 contains a double quote",
          ),
          (
              'AnchorSql = "SELECT MAX(',
              'AnchorSql = "SELECT #(lf) MAX(',
              "AnchorSql literal 1 contains unescaped '#('",
          ),
          (
              "\"SELECT DATE(MAX(timestamp), 'UTC') AS newest_date FROM \"",
              "\"SELECT; DATE(MAX(timestamp), 'UTC') AS newest_date FROM \"",
              "FallbackAnchor literal 1 contains ';'",
          ),
      ],
  )
  def test_anchor_and_fallback_sql_hazards_detected(
      self, tmp_path, old_frag, new_frag, expected
  ):
    """I8: AnchorSql and FallbackAnchor literals are scanned for SQL hazards."""
    _skip_if_not_generated()
    val_mod = _load_tool_module("validate_pbip")
    tmp_model = self._copy_model_definition(tmp_path)
    self._edit_once(tmp_model / "expressions.tmdl", old_frag, new_frag)
    errors = val_mod.validate_tmdl(power_bi_root=tmp_path)
    assert any(expected in e for e in errors), errors

  # -- Oracle contract drift and missing specs (F4, F5, F13) -----------------

  def test_oracle_edit_without_regeneration_detected(self, tmp_path):
    """An edited cited oracle query is reported until the model is regenerated."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    gen_mod = _load_tool_module("generate_pbip")
    assert val_mod.validate_parity(power_bi_root=POWER_BI) == []
    assert gen_mod.check_generated_files(power_bi_root=POWER_BI) == []

    tmp_looker = tmp_path / "looker_studio"
    shutil.copytree(LOOKER, tmp_looker)
    query_path = tmp_looker / "oracle" / "queries" / "usage-total-traces.sql"
    sql = query_path.read_text()
    assert sql.count("INTERVAL 1 DAY") == 2
    query_path.write_text(sql.replace("INTERVAL 1 DAY", "INTERVAL 2 DAY"))

    errors = val_mod.validate_parity(
        power_bi_root=POWER_BI, looker_root=tmp_looker
    )
    assert any(
        "measure 'KPI Total Traces' oracle digest" in e for e in errors
    ), errors
    diffs = gen_mod.check_generated_files(
        power_bi_root=POWER_BI, looker_root=tmp_looker
    )
    assert any("events_v1.tmdl" in d for d in diffs), diffs

  def test_removed_oracle_alias_detected(self, tmp_path):
    """A measure whose oracle_column is not an output alias is reported."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_looker = tmp_path / "looker_studio"
    shutil.copytree(LOOKER, tmp_looker)
    query_path = tmp_looker / "oracle" / "queries" / "usage-total-tokens.sql"
    sql = query_path.read_text()
    alias_line = "  prev.value AS pop_total_tokens_prior,\n"
    assert sql.count(alias_line) == 1
    query_path.write_text(sql.replace(alias_line, ""))
    errors = val_mod.validate_parity(
        power_bi_root=POWER_BI, looker_root=tmp_looker
    )
    assert any(
        "oracle_column 'pop_total_tokens_prior' is not an output alias" in e
        for e in errors
    ), errors

  def test_oracle_distinct_count_column_mismatch_detected(self, tmp_path):
    """Mutating COUNT(DISTINCT session_id) to trace_id in oracle SQL fails parity."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_looker = tmp_path / "looker_studio"
    shutil.copytree(LOOKER, tmp_looker)
    query_path = tmp_looker / "oracle" / "queries" / "usage-total-sessions.sql"
    sql = query_path.read_text(encoding="utf-8")
    assert sql.count("COUNT(DISTINCT session_id)") == 2
    query_path.write_text(
        sql.replace("COUNT(DISTINCT session_id)", "COUNT(DISTINCT trace_id)"),
        encoding="utf-8",
    )
    errors = val_mod.validate_parity(
        power_bi_root=POWER_BI, looker_root=tmp_looker
    )
    assert any(
        "counts distinct 'session_id', but cited oracle query"
        " 'oracle/queries/usage-total-sessions.sql' does not contain"
        " COUNT(DISTINCT ... session_id)" in e
        for e in errors
    ), errors

  def test_top_n_limit_mismatch_detected(self, tmp_path):
    """A source manifest limit that disagrees with the oracle LIMIT fails.

    The visual-side cases (top_n above the source rows, top_n different from
    the source LIMIT, the pinned D14 exception) are covered below.
    """
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_looker = tmp_path / "looker_studio"
    shutil.copytree(LOOKER, tmp_looker)
    manifest_path = tmp_looker / "spec" / "chart_manifest.yaml"
    manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    target = next(
        c
        for c in manifest_data["charts"]
        if c["id"] == "usage-top-5-agents-by-llm-calls"
    )
    assert target["limit"] == 500
    # Mutate manifest limit to disagree with oracle LIMIT (500).
    target["limit"] = 5
    manifest_path.write_text(
        yaml.safe_dump(manifest_data, sort_keys=False), encoding="utf-8"
    )
    errors = val_mod.validate_parity(
        power_bi_root=POWER_BI, looker_root=tmp_looker
    )
    assert any(
        "Top-N limit mismatch for 'usage-top-5-agents-by-llm-calls':"
        " source manifest limit=5 and oracle LIMIT=500" in e
        for e in errors
    ), errors

  # -- Row caps, bar counts and comparison types (README D13 and D14) -------

  @staticmethod
  def _set_entry_field(path, list_key, entry_id, field, value):
    """Sets (or, for None, deletes) one field of one entry of a YAML list."""
    key = "name" if list_key == "measures" else "id"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = next(e for e in data[list_key] if e[key] == entry_id)
    assert field in entry, (entry_id, field)
    if value is None:
      del entry[field]
    else:
      entry[field] = value
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

  @staticmethod
  def _set_final_limit(sql_path, old, new):
    sql = sql_path.read_text(encoding="utf-8")
    assert sql.rstrip().endswith(f"LIMIT {old}"), sql[-60:]
    head, _, tail = sql.rpartition(f"LIMIT {old}")
    sql_path.write_text(f"{head}LIMIT {new}{tail}", encoding="utf-8")

  def test_unmodified_copy_passes_parity_and_tmdl_checks(self, tmp_path):
    """Control for the mutations below: the untouched copy is clean."""
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    val_mod = _load_tool_module("validate_pbip")
    assert val_mod.validate_parity(power_bi_root=pbi, looker_root=looker) == []
    assert val_mod.validate_tmdl(power_bi_root=pbi, looker_root=looker) == []

  @pytest.mark.parametrize(
      "chart_id,top_n,message",
      [
          # The pinned D14 chart draws five bars over its 500-row source ...
          (
              "usage-top-5-agents-by-llm-calls",
              501,
              "report_visuals top_n=501 exceeds the available source rows"
              " (oracle LIMIT=500)",
          ),
          (
              "usage-top-5-agents-by-llm-calls",
              500,
              "report_visuals top_n=500 and oracle LIMIT=500 must stay"
              " top_n=5 over a source LIMIT=500 (README known difference D14)",
          ),
          (
              "usage-top-5-agents-by-llm-calls",
              4,
              "report_visuals top_n=4 and oracle LIMIT=500 must stay top_n=5"
              " over a source LIMIT=500 (README known difference D14)",
          ),
          # ... and every other Top-N visual draws exactly its source LIMIT.
          (
              "usage-top-5-users-by-session",
              3,
              "report_visuals top_n=3 must equal the source LIMIT=5",
          ),
          (
              "usage-top-5-users-by-session",
              6,
              "report_visuals top_n=6 exceeds the available source rows"
              " (oracle LIMIT=5)",
          ),
          (
              "usage-tool-invocations",
              5,
              "report_visuals top_n=5 must equal the source LIMIT=10",
          ),
      ],
  )
  def test_visual_top_n_drift_detected(
      self, tmp_path, chart_id, top_n, message
  ):
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    val_mod = _load_tool_module("validate_pbip")
    self._set_entry_field(
        pbi / "spec" / "report_visuals.yaml",
        "parity_visuals",
        chart_id,
        "top_n",
        top_n,
    )
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    expected = f"Top-N limit mismatch for '{chart_id}': {message}"
    assert any(expected in e for e in errors), errors

  def test_source_cut_to_the_bar_count_detected(self, tmp_path):
    """Rewriting the 500-row source to five rows fails even when consistent."""
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    val_mod = _load_tool_module("validate_pbip")
    cid = "usage-top-5-agents-by-llm-calls"
    self._set_entry_field(
        looker / "spec" / "chart_manifest.yaml", "charts", cid, "limit", 5
    )
    self._set_final_limit(looker / "oracle" / "queries" / f"{cid}.sql", 500, 5)
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    assert any(
        f"Top-N limit mismatch for '{cid}': report_visuals top_n=5 and oracle"
        " LIMIT=5 must stay top_n=5 over a source LIMIT=500 (README known"
        " difference D14)" in e
        for e in errors
    ), errors

  @pytest.mark.parametrize(
      "chart_id,side,message",
      [
          (
              "usage-tool-calls-over-time",
              "manifest",
              "manifest limit=4000, oracle LIMIT=5000",
          ),
          (
              "usage-tool-calls-over-time",
              "oracle",
              "manifest limit=5000, oracle LIMIT=4000",
          ),
          (
              "usage-llm-call-trends",
              "manifest",
              "manifest limit=4000, oracle LIMIT=5000",
          ),
      ],
  )
  def test_source_limit_drift_detected_for_a_chart_without_top_n(
      self, tmp_path, chart_id, side, message
  ):
    """Either half of a non-Top-N source row cap cannot drift on its own."""
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    val_mod = _load_tool_module("validate_pbip")
    if side == "manifest":
      self._set_entry_field(
          looker / "spec" / "chart_manifest.yaml",
          "charts",
          chart_id,
          "limit",
          4000,
      )
    else:
      self._set_final_limit(
          looker / "oracle" / "queries" / f"{chart_id}.sql", 5000, 4000
      )
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    expected = f"Source limit mismatch for '{chart_id}': {message}"
    assert any(expected in e for e in errors), errors

  @pytest.mark.parametrize(
      "measure,value,message",
      [
          ("KPI Total Tokens", "float", "'float', expected 'int'"),
          ("Top 5 Agents by LLM Calls", "float", "'float', expected 'int'"),
          ("Avg LLM Latency (ms)", "int", "'int', expected 'float'"),
          ("KPI Total Users Change %", "int", "'int', expected 'float'"),
          ("Total Events", None, "None, expected 'int'"),
      ],
  )
  def test_comparison_type_drift_in_the_spec_detected(
      self, tmp_path, measure, value, message
  ):
    """The repository validator pins which measures compare as floats."""
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    val_mod = _load_tool_module("validate_pbip")
    self._set_entry_field(
        pbi / "spec" / "semantic_model.yaml",
        "measures",
        measure,
        "comparison_type",
        value,
    )
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    expected = f"semantic_model.yaml: measure '{measure}' has comparison_type {message}"
    assert expected in errors, errors

  _KPI_TOKENS = (
      "\tmeasure 'KPI Total Tokens' = [Total Tokens]\n\t\tformatString: #,0\n"
  )
  _AVG_LLM = (
      "\tmeasure 'Avg LLM Latency (ms)' = AVERAGE('events_v1'[llm_total_ms])\n"
      "\t\tformatString: #,0.0\n"
  )

  @pytest.mark.parametrize(
      "old,new,measure,message",
      [
          (
              _KPI_TOKENS,
              _KPI_TOKENS.replace("#,0\n", "#,0.0\n"),
              "KPI Total Tokens",
              "is an exact count or sum and must use formatString '#,0',"
              " found '#,0.0'",
          ),
          (
              _KPI_TOKENS,
              _KPI_TOKENS.replace("#,0\n", "+0.0%;-0.0%;0.0%\n"),
              "KPI Total Tokens",
              "is an exact count or sum and must use formatString '#,0',"
              " found '+0.0%;-0.0%;0.0%'",
          ),
          (
              _AVG_LLM,
              _AVG_LLM.replace("#,0.0\n", "#,0\n"),
              "Avg LLM Latency (ms)",
              "is compared as a float and must use one of ['#,0.0', '#,0.00',"
              " '+0.0%;-0.0%;0.0%'], found '#,0'",
          ),
          (
              _KPI_TOKENS,
              _KPI_TOKENS + "\t\tformatString: #,0.0\n",
              "KPI Total Tokens",
              "must declare exactly one formatString, found ['#,0', '#,0.0']",
          ),
          (
              _KPI_TOKENS,
              _KPI_TOKENS.replace("\t\tformatString: #,0\n", ""),
              "KPI Total Tokens",
              "must declare exactly one formatString, found []",
          ),
      ],
  )
  def test_measure_format_string_drift_detected(
      self, tmp_path, old, new, measure, message
  ):
    """A display-format edit in the TMDL cannot loosen an exact count."""
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    val_mod = _load_tool_module("validate_pbip")
    events_path = (
        pbi
        / "BQAA_Agent_Analytics.SemanticModel"
        / "definition"
        / "tables"
        / "events_v1.tmdl"
    )
    text = events_path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    text = text.replace(old, new)
    events_path.write_text(text, encoding="utf-8")
    line = next(
        i
        for i, raw in enumerate(text.splitlines(), 1)
        if raw.startswith(f"\tmeasure '{measure}' = ")
    )
    errors = val_mod.validate_tmdl(power_bi_root=pbi, looker_root=looker)
    assert (
        f"events_v1.tmdl:{line}: measure '{measure}' {message}" in errors
    ), errors

  def test_kpi_prior_missing_coalesce_detected(self, tmp_path):
    """Dropping COALESCE from a count-based KPI * Prior measure fails validate_parity."""
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    val_mod = _load_tool_module("validate_pbip")
    events_path = (
        pbi
        / "BQAA_Agent_Analytics.SemanticModel"
        / "definition"
        / "tables"
        / "events_v1.tmdl"
    )
    self._edit_once(
        events_path,
        "CALCULATE(COALESCE([Total Traces], 0), REMOVEFILTERS('Date')",
        "CALCULATE([Total Traces], REMOVEFILTERS('Date')",
    )
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    assert any(
        "'KPI Total Traces Prior' must use CALCULATE(COALESCE(..., 0), ...)"
        in e
        for e in errors
    ), errors

  @pytest.mark.parametrize(
      "missing", ["semantic_model.yaml", "report_visuals.yaml"]
  )
  def test_missing_spec_is_not_silently_skipped(self, tmp_path, missing):
    """Deleting an input spec fails the sync check instead of skipping its half."""
    import shutil

    gen_mod = _load_tool_module("generate_pbip")
    tmp_pbi = tmp_path / "power_bi"
    shutil.copytree(POWER_BI / "spec", tmp_pbi / "spec")
    (tmp_pbi / "spec" / missing).unlink()

    assert gen_mod.check_generated_files(power_bi_root=tmp_pbi) == [
        f"MISSING: spec/{missing}"
    ]
    with pytest.raises(FileNotFoundError, match="Missing required spec"):
      gen_mod.generate_all(power_bi_root=tmp_pbi)
    with pytest.raises(FileNotFoundError, match="Missing required spec"):
      gen_mod.write_generated_files(power_bi_root=tmp_pbi)
    # Nothing was written or deleted.
    assert sorted(p.name for p in tmp_pbi.iterdir()) == ["spec"]


# ---------------------------------------------------------------------------
# Source contract: the Looker Studio snapshot is the baseline the port is
# compared with, so a Power BI change must not rewrite it to agree with the
# port (README known difference D14).
# ---------------------------------------------------------------------------

_LLM_AGENTS_ID = "usage-top-5-agents-by-llm-calls"
_MANIFEST_REL = "dashboards/looker_studio/spec/chart_manifest.yaml"
_LLM_AGENTS_ORACLE_REL = (
    f"dashboards/looker_studio/oracle/queries/{_LLM_AGENTS_ID}.sql"
)


def _manifest_chart(text, chart_id):
  return next(c for c in yaml.safe_load(text)["charts"] if c["id"] == chart_id)


def _final_sql_limit(text):
  limits = re.findall(r"(?m)^LIMIT (\d+)\s*$", text)
  assert limits, "the oracle query has no final LIMIT"
  return int(limits[-1])


def _git(*args):
  return subprocess.run(
      ["git", "-C", str(ROOT), *args],
      capture_output=True,
      text=True,
      check=False,
  )


def _main_merge_base():
  """Returns the merge base of HEAD and the main branch, or skips the test."""
  try:
    shallow = _git("rev-parse", "--is-shallow-repository")
  except FileNotFoundError:
    pytest.skip("git is not installed")
  if shallow.returncode != 0:
    pytest.skip("not a git checkout")
  if shallow.stdout.strip() != "false":
    pytest.skip("shallow clone: the main branch history is not available")
  for ref in ("upstream/main", "origin/main", "main"):
    base = _git("merge-base", "HEAD", ref)
    if base.returncode == 0 and base.stdout.strip():
      return base.stdout.strip()
  pytest.skip("no main branch to compare with")


class TestSourceContract:
  """The port is compared with the Looker Studio source; it never edits it."""

  def test_llm_agents_draws_five_bars_over_its_500_row_source(self):
    manifest = _manifest_chart(
        (ROOT / _MANIFEST_REL).read_text(encoding="utf-8"), _LLM_AGENTS_ID
    )
    oracle = (ROOT / _LLM_AGENTS_ORACLE_REL).read_text(encoding="utf-8")
    visuals = yaml.safe_load(
        (POWER_BI / "spec" / "report_visuals.yaml").read_text(encoding="utf-8")
    )["parity_visuals"]
    visual = next(v for v in visuals if v["id"] == _LLM_AGENTS_ID)
    # The source tile keeps its 500-row cap.  Its column_limit caps pivot
    # columns, and the tile has neither a pivot nor a displayed-row limit.
    assert (manifest["limit"], _final_sql_limit(oracle)) == (500, 500)
    assert manifest["pivots"] == []
    assert manifest["style"]["limit_displayed_rows"] is False
    # The port draws five bars over it, as one pinned, documented exception.
    assert visual["top_n"] == 5
    val_mod = _load_tool_module("validate_pbip")
    assert val_mod.TOP_N_BELOW_SOURCE_LIMIT == {_LLM_AGENTS_ID: (5, 500)}
    readme = " ".join(
        (POWER_BI / "README.md").read_text(encoding="utf-8").split()
    )
    assert (
        "| D14 | Top 5 Agents by LLM Calls | Bar tile titled Top 5 over up to"
        " 500 agents (`limit: 500`) |" in readme
    )

  def test_d14_states_the_tie_break_rather_than_kept_ties(self):
    """TOPN(5) breaks a fifth-place tie on the agent; it never keeps both."""
    measures = yaml.safe_load(
        (POWER_BI / "spec" / "semantic_model.yaml").read_text(encoding="utf-8")
    )["measures"]
    measure = next(m for m in measures if m.get("chart_id") == _LLM_AGENTS_ID)
    assert measure["top_n"] == 5
    assert measure["dax"].startswith("VAR _top = TOPN(5, ")
    assert "[LLM Calls], DESC, 'events_v1'[agent], ASC)" in measure["dax"]
    readme = " ".join(
        (POWER_BI / "README.md").read_text(encoding="utf-8").split()
    )
    assert "ties at fifth place kept" not in readme
    assert (
        "Top five agents (`TOPN(5)`, with agent ascending as the secondary"
        " tie-break); source and oracle keep `LIMIT 500`" in readme
    )
    # The live check tolerates a different member only at a tied cutoff.
    assert (
        "accepts a different member at the tied cutoff when the value"
        " sequence is identical" in readme
    )

  def test_llm_agents_source_matches_the_main_branch(self):
    """The source row cap is main's, not a rewrite that agrees with the port."""
    base = _main_merge_base()

    def at_base(rel):
      shown = _git("show", f"{base}:{rel}")
      if shown.returncode != 0:
        pytest.skip(f"{rel} does not exist at the merge base {base}")
      return shown.stdout

    base_manifest = _manifest_chart(at_base(_MANIFEST_REL), _LLM_AGENTS_ID)
    base_oracle = at_base(_LLM_AGENTS_ORACLE_REL)
    manifest = _manifest_chart(
        (ROOT / _MANIFEST_REL).read_text(encoding="utf-8"), _LLM_AGENTS_ID
    )
    oracle = (ROOT / _LLM_AGENTS_ORACLE_REL).read_text(encoding="utf-8")
    assert (manifest["limit"], _final_sql_limit(oracle)) == (
        base_manifest["limit"],
        _final_sql_limit(base_oracle),
    )
    assert manifest == base_manifest
    assert oracle == base_oracle


# ---------------------------------------------------------------------------
# Reviewed oracle lock (spec/oracle_lock.json): the committed record of the
# oracle SQL the DAX measures were last reconciled with.  Regenerating the
# model must never move it; only --accept-oracle-changes does.
# ---------------------------------------------------------------------------

_LOCK_REL = "spec/oracle_lock.json"
_TRACES_QUERY = "oracle/queries/usage-total-traces.sql"


def _copy_dashboards(tmp_path):
  """Copies both dashboard trees into a throwaway repo layout.

  The tools find their inputs relative to their own location, so the CLI can
  be run from the copy without touching the checkout.
  """
  import shutil

  ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
  dashboards = tmp_path / "dashboards"
  shutil.copytree(POWER_BI, dashboards / "power_bi", ignore=ignore)
  shutil.copytree(LOOKER, dashboards / "looker_studio", ignore=ignore)
  return dashboards / "power_bi", dashboards / "looker_studio"


def _edit_traces_query(looker_root):
  """Changes the traces oracle date window, as a reviewer would object to."""
  query_path = looker_root / _TRACES_QUERY
  sql = query_path.read_text(encoding="utf-8")
  assert sql.count("INTERVAL 1 DAY") == 2
  query_path.write_text(
      sql.replace("INTERVAL 1 DAY", "INTERVAL 2 DAY"),
      encoding="utf-8",
  )


def _tree_digest(root):
  digest = hashlib.sha256()
  for path in sorted(root.rglob("*")):
    if path.is_file() and "__pycache__" not in path.parts:
      digest.update(path.relative_to(root).as_posix().encode("utf-8"))
      digest.update(b"\0")
      digest.update(path.read_bytes())
      digest.update(b"\0")
  return digest.hexdigest()


def _run_generator(power_bi_root, *args):
  return subprocess.run(
      [
          sys.executable,
          str(power_bi_root / "tools" / "generate_pbip.py"),
          *args,
      ],
      cwd=str(power_bi_root.parent.parent),
      capture_output=True,
      text=True,
      env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
      check=False,
  )


class TestOracleLock:
  """An edited oracle query fails every gate until a person accepts it."""

  def test_clean_tree_matches_the_lock(self):
    _skip_if_not_generated()
    gen_mod = _load_tool_module("generate_pbip")
    val_mod = _load_tool_module("validate_pbip")
    assert gen_mod.oracle_lock_problems(POWER_BI, LOOKER) == []
    assert val_mod.validate_parity(power_bi_root=POWER_BI) == []

  def test_edited_oracle_query_is_reported_by_the_check_and_the_validator(
      self, tmp_path
  ):
    _skip_if_not_generated()
    gen_mod = _load_tool_module("generate_pbip")
    val_mod = _load_tool_module("validate_pbip")
    pbi, looker = _copy_dashboards(tmp_path)
    # Control: the copy is clean until the query is edited.
    assert gen_mod.check_generated_files(pbi, looker) == []
    assert val_mod.validate_parity(power_bi_root=pbi, looker_root=looker) == []

    _edit_traces_query(looker)
    expected = f"ORACLE LOCK: {_TRACES_QUERY} changed since it was reviewed"
    diffs = gen_mod.check_generated_files(pbi, looker)
    assert diffs[0].startswith(expected), diffs
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    assert any(e.startswith(expected) for e in errors), errors

  def test_regenerating_the_model_does_not_clear_the_lock(self, tmp_path):
    """Regenerating every file from the edited SQL must still be reported.

    The per-measure digest follows the SQL on every regeneration, so on its
    own it only proves the model was regenerated.  The lock is what records
    that a person looked at the new SQL.
    """
    _skip_if_not_generated()
    gen_mod = _load_tool_module("generate_pbip")
    val_mod = _load_tool_module("validate_pbip")
    pbi, looker = _copy_dashboards(tmp_path)
    _edit_traces_query(looker)

    regenerated = gen_mod.generate_all(pbi, looker, enforce_oracle_lock=False)
    for rel_path, text in regenerated.items():
      target = pbi / rel_path
      target.parent.mkdir(parents=True, exist_ok=True)
      with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    diffs = gen_mod.check_generated_files(pbi, looker)
    assert diffs, "regenerated files must not be enough to pass the check"
    # Every generated file is now in sync; only the lock disagrees.
    assert all(d.startswith("ORACLE LOCK:") for d in diffs), diffs
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    assert any(e.startswith("ORACLE LOCK:") for e in errors), errors

  def test_generation_refuses_an_edited_query_and_writes_nothing(
      self, tmp_path
  ):
    _skip_if_not_generated()
    gen_mod = _load_tool_module("generate_pbip")
    pbi, looker = _copy_dashboards(tmp_path)
    _edit_traces_query(looker)
    before = _tree_digest(pbi)
    oracle_lock_error = gen_mod.pbip_common.OracleLockError

    with pytest.raises(oracle_lock_error, match="--accept-oracle-changes"):
      gen_mod.generate_all(pbi, looker)
    with pytest.raises(oracle_lock_error, match="ORACLE LOCK"):
      gen_mod.write_generated_files(pbi, looker)
    out_zip = tmp_path / "out" / "custom.zip"
    with pytest.raises(oracle_lock_error, match="ORACLE LOCK"):
      gen_mod.write_pbip_zip(
          out_zip, project="abcde-1", power_bi_root=pbi, looker_root=looker
      )
    assert _tree_digest(pbi) == before
    assert not out_zip.parent.exists()

  def test_accepting_the_change_rewrites_the_lock_and_the_annotations(
      self, tmp_path
  ):
    _skip_if_not_generated()
    common = _load_tool_module("pbip_common")
    gen_mod = _load_tool_module("generate_pbip")
    val_mod = _load_tool_module("validate_pbip")
    pbi, looker = _copy_dashboards(tmp_path)
    locked_before = common.read_oracle_lock(POWER_BI / _LOCK_REL)
    events_path = (
        pbi
        / "BQAA_Agent_Analytics.SemanticModel"
        / "definition"
        / "tables"
        / "events_v1.tmdl"
    )
    events_before = events_path.read_text(encoding="utf-8")

    _edit_traces_query(looker)
    gen_mod.write_generated_files(pbi, looker, accept_oracle=True)

    assert gen_mod.check_generated_files(pbi, looker) == []
    assert val_mod.validate_parity(power_bi_root=pbi, looker_root=looker) == []
    locked_after = common.read_oracle_lock(pbi / _LOCK_REL)
    assert sorted(locked_after) == sorted(locked_before)
    assert {q for q in locked_after if locked_after[q] != locked_before[q]} == {
        _TRACES_QUERY
    }
    # The derived per-measure annotations followed the accepted SQL.
    assert events_path.read_text(encoding="utf-8") != events_before

  def test_missing_lock_is_reported_and_recreated_only_on_request(
      self, tmp_path
  ):
    _skip_if_not_generated()
    gen_mod = _load_tool_module("generate_pbip")
    val_mod = _load_tool_module("validate_pbip")
    pbi, looker = _copy_dashboards(tmp_path)
    committed = (pbi / _LOCK_REL).read_bytes()
    (pbi / _LOCK_REL).unlink()

    assert gen_mod.check_generated_files(pbi, looker) == [
        f"MISSING: {_LOCK_REL}"
    ]
    errors = val_mod.validate_parity(power_bi_root=pbi, looker_root=looker)
    assert f"MISSING: {_LOCK_REL}" in errors, errors
    with pytest.raises(ValueError, match=f"MISSING: {_LOCK_REL}"):
      gen_mod.write_generated_files(pbi, looker)
    assert not (pbi / _LOCK_REL).exists()

    gen_mod.write_generated_files(pbi, looker, accept_oracle=True)
    assert (pbi / _LOCK_REL).read_bytes() == committed
    assert gen_mod.check_generated_files(pbi, looker) == []

  @pytest.mark.parametrize(
      "text",
      [
          "{",
          "[]",
          '{"version": 2, "algorithm": "sha256", "oracle_queries": {}}',
          '{"version": 1, "algorithm": "sha256", "oracle_queries": {}}',
          '{"version": 1, "algorithm": "sha256",'
          ' "oracle_queries": {"oracle/queries/a.sql": "not-a-digest"}}',
      ],
  )
  def test_unusable_lock_is_reported(self, tmp_path, text):
    _skip_if_not_generated()
    gen_mod = _load_tool_module("generate_pbip")
    pbi, looker = _copy_dashboards(tmp_path)
    (pbi / _LOCK_REL).write_text(text, encoding="utf-8")
    diffs = gen_mod.check_generated_files(pbi, looker)
    assert len(diffs) == 1, diffs
    assert diffs[0].startswith(f"ORACLE LOCK: {_LOCK_REL} is unusable: ")

  def test_lock_entries_must_match_the_cited_queries_exactly(self, tmp_path):
    _skip_if_not_generated()
    gen_mod = _load_tool_module("generate_pbip")
    pbi, looker = _copy_dashboards(tmp_path)
    lock = json.loads((pbi / _LOCK_REL).read_text(encoding="utf-8"))
    del lock["oracle_queries"][_TRACES_QUERY]
    lock["oracle_queries"]["oracle/queries/retired.sql"] = "0" * 64
    (pbi / _LOCK_REL).write_text(json.dumps(lock), encoding="utf-8")

    diffs = gen_mod.check_generated_files(pbi, looker)
    assert diffs == [
        f"ORACLE LOCK: {_TRACES_QUERY} is not recorded in {_LOCK_REL}",
        f"ORACLE LOCK: {_LOCK_REL} records oracle/queries/retired.sql,"
        " which no chart or measure cites",
    ]

  def test_cli_check_write_and_accept_flow(self, tmp_path):
    _skip_if_not_generated()
    pbi, looker = _copy_dashboards(tmp_path)
    clean = _run_generator(pbi, "--check")
    assert clean.returncode == 0, clean.stderr
    _edit_traces_query(looker)

    check = _run_generator(pbi, "--check")
    assert check.returncode == 1
    assert "PBIP drift detected" in check.stderr
    assert (
        f"ORACLE LOCK: {_TRACES_QUERY} changed since it was reviewed"
        in check.stderr
    )
    assert "generate_pbip.py --write --accept-oracle-changes" in check.stderr

    before = _tree_digest(pbi)
    write = _run_generator(pbi, "--write")
    assert write.returncode == 1
    assert "ORACLE LOCK" in write.stderr
    assert "--accept-oracle-changes" in write.stderr
    assert _tree_digest(pbi) == before

    accept = _run_generator(pbi, "--write", "--accept-oracle-changes")
    assert accept.returncode == 0, accept.stderr
    assert f"Re-recorded {_LOCK_REL} (1 change(s))." in accept.stdout
    assert _TRACES_QUERY in accept.stdout
    after = _run_generator(pbi, "--check")
    assert after.returncode == 0, after.stderr

  @pytest.mark.parametrize(
      "mode", [["--check"], ["--print-sql", "--window-end", "2026-07-16"]]
  )
  def test_accept_flag_is_only_valid_with_write(self, mode):
    proc = subprocess.run(
        [
            sys.executable,
            str(POWER_BI / "tools" / "generate_pbip.py"),
            *mode,
            "--accept-oracle-changes",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert "--accept-oracle-changes is only valid with --write" in proc.stderr

  def test_readme_describes_the_reviewed_lock(self):
    readme = (POWER_BI / "README.md").read_text(encoding="utf-8")
    paragraphs = [
        " ".join(paragraph.split()) for paragraph in readme.split("\n\n")
    ]
    (lock_doc,) = [p for p in paragraphs if "spec/oracle_lock.json" in p]
    for gate in (
        "--check",
        "--write",
        "--output-zip",
        "validate_pbip.py",
        "--accept-oracle-changes",
    ):
      assert gate in lock_doc, gate
    assert "Regeneration alone never moves the lock" in lock_doc


# ---------------------------------------------------------------------------
# Schema lock integrity tests
# ---------------------------------------------------------------------------


class TestSchemaLock:
  """Verify the schema lock file is well-formed."""

  def test_lock_file_exists(self):
    lock_path = POWER_BI / "spec" / "pbir_schemas.lock.json"
    assert lock_path.exists(), "pbir_schemas.lock.json not found"

  def test_lock_has_13_files(self):
    lock_path = POWER_BI / "spec" / "pbir_schemas.lock.json"
    lock = json.loads(lock_path.read_text())
    assert len(lock["files"]) == 13

  def test_lock_total_bytes(self):
    lock_path = POWER_BI / "spec" / "pbir_schemas.lock.json"
    lock = json.loads(lock_path.read_text())
    total = sum(f["bytes"] for f in lock["files"])
    assert total == 208520

  def test_lock_commit(self):
    lock_path = POWER_BI / "spec" / "pbir_schemas.lock.json"
    lock = json.loads(lock_path.read_text())
    assert lock["commit"] == ("eecc60d35b9ab18b884d35c50abe7d09d2753cdf")

  def test_each_file_has_sha256_and_bytes(self):
    lock_path = POWER_BI / "spec" / "pbir_schemas.lock.json"
    lock = json.loads(lock_path.read_text())
    for f in lock["files"]:
      assert "sha256" in f, f"Missing sha256 for {f['path']}"
      assert "bytes" in f, f"Missing bytes for {f['path']}"
      assert len(f["sha256"]) == 64, f"Invalid sha256 length for {f['path']}"

  def test_lock_base_url_contains_commit(self):
    lock_path = POWER_BI / "spec" / "pbir_schemas.lock.json"
    lock = json.loads(lock_path.read_text())
    assert lock["commit"] in lock["base_url"]


# ---------------------------------------------------------------------------
# Schema validation tests (only when BQAA_POWER_BI_SCHEMAS_DIR is set)
# ---------------------------------------------------------------------------

SCHEMAS_DIR = os.environ.get("BQAA_POWER_BI_SCHEMAS_DIR")


class TestSchemaValidation:
  """JSON schema validation against pinned schemas."""

  def test_schema_tests_cannot_silently_skip_in_ci(self):
    """Guard: the CI workflow must fetch schemas and run these tests.

    Mirrors test_browser_gate_cannot_silently_skip_in_ci: parses the
    workflow YAML and asserts it fetches schemas, exports the env var,
    and runs the Power BI test files.
    """
    workflow_path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    assert workflow_path.exists(), "power-bi-sync-check.yml not found"
    text = workflow_path.read_text()
    # Must fetch schemas.
    assert "--fetch-schemas" in text, "Workflow must fetch schemas"
    # Must validate against schemas.
    assert "--schemas-dir" in text, "Workflow must validate against schemas"
    # Must export BQAA_POWER_BI_SCHEMAS_DIR.
    assert (
        "BQAA_POWER_BI_SCHEMAS_DIR" in text
    ), "Workflow must set BQAA_POWER_BI_SCHEMAS_DIR"
    # Must run the test files.
    assert "test_power_bi" in text, "Workflow must run Power BI tests"

  @pytest.mark.skipif(
      SCHEMAS_DIR is None,
      reason="BQAA_POWER_BI_SCHEMAS_DIR not set",
  )
  def test_all_json_files_valid_against_schemas(self):
    _skip_if_not_generated()
    val_path = POWER_BI / "tools" / "validate_pbip.py"
    if not val_path.exists():
      pytest.skip("validate_pbip.py not found")
    result = subprocess.run(
        [sys.executable, str(val_path), "--schemas-dir", SCHEMAS_DIR],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Schema validation failed:\n{result.stderr}"

  @pytest.mark.skipif(
      SCHEMAS_DIR is None,
      reason="BQAA_POWER_BI_SCHEMAS_DIR not set",
  )
  def test_validator_detects_wrong_schema_url(self, tmp_path):
    """A JSON file with an unknown $schema must be rejected by validate_json_schemas."""
    _skip_if_not_generated()
    import shutil

    val_mod = _load_tool_module("validate_pbip")
    tmp_report = tmp_path / "BQAA_Agent_Analytics.Report"
    shutil.copytree(POWER_BI / "BQAA_Agent_Analytics.Report", tmp_report)
    report_json = tmp_report / "definition" / "report.json"
    obj = json.loads(report_json.read_text())
    obj["$schema"] = "https://bogus.example.com/schema"
    report_json.write_text(json.dumps(obj, indent=2) + "\n")
    errors = val_mod.validate_json_schemas(tmp_path, SCHEMAS_DIR)
    assert any("not in pinned lock" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Documentation tests
# ---------------------------------------------------------------------------


class TestDocumentation:
  """Verify README and USER_MANUAL cover required topics."""

  def test_readme_exists(self):
    assert (
        POWER_BI / "README.md"
    ).exists(), "dashboards/power_bi/README.md not found"

  def test_user_manual_exists(self):
    assert (
        POWER_BI / "USER_MANUAL.md"
    ).exists(), "dashboards/power_bi/USER_MANUAL.md not found"

  def test_readme_documents_sql_substitution_contract(self):
    """README must document the SQL substitution contract."""
    text = (POWER_BI / "README.md").read_text().lower()
    required_topics = [
        "sql substitution",
        "placeholder",
        "escaping",
        '""',
        "trust model",
    ]
    for topic in required_topics:
      assert (
          topic.lower() in text
      ), f"README missing SQL substitution topic: '{topic}'"

  def test_readme_documents_parameter_validation(self):
    """README must document parameter validation and trust model."""
    text = (POWER_BI / "README.md").read_text().lower()
    assert "parameter" in text
    assert "validation" in text or "validate" in text
    assert "trust" in text

  def test_readme_documents_divergences(self):
    """README must document intentional divergences D1-D12."""
    text = (POWER_BI / "README.md").read_text()
    # Check for divergence references.
    for d in ["D1", "D2", "D3", "D4"]:
      assert d in text, f"README missing divergence {d}"

  def test_readme_documents_schema_credit(self):
    """README must credit the MIT-licensed Microsoft schemas."""
    text = (POWER_BI / "README.md").read_text()
    assert "microsoft" in text.lower()
    assert "MIT" in text or "mit" in text

  def test_readme_documents_date_window(self):
    """README must document the date window design."""
    text = (POWER_BI / "README.md").read_text().lower()
    assert "window" in text
    assert "partition" in text or "anchor" in text

  def test_readme_documents_deploy(self):
    """README must document deployment options."""
    text = (POWER_BI / "README.md").read_text().lower()
    assert "deploy" in text
    assert "fabric" in text or "rest" in text

  def test_readme_no_vendor_names(self):
    """README must not contain agent/model/vendor names."""
    text = (POWER_BI / "README.md").read_text()
    for name in _FORBIDDEN_VENDOR_NAMES():
      assert (
          name.lower() not in text.lower()
      ), f"README contains forbidden name: '{name}'"

  def test_user_manual_no_vendor_names(self):
    """USER_MANUAL must not contain agent/model/vendor names."""
    text = (POWER_BI / "USER_MANUAL.md").read_text()
    for name in _FORBIDDEN_VENDOR_NAMES():
      assert (
          name.lower() not in text.lower()
      ), f"USER_MANUAL contains forbidden name: '{name}'"

  def test_docs_state_the_real_measure_count(self):
    """README and the web configurator quote the generated measure count."""
    pbip_model = _load_tool_module("pbip_model")
    count = len(pbip_model.MEASURE_NAMES)
    assert count == 50
    readme = (POWER_BI / "README.md").read_text()
    index_html = (POWER_BI / "docs" / "index.html").read_text()
    assert f"{count} DAX measures" in readme
    assert f"{count} DAX measures" in index_html
    for stale in ("49 DAX measures", "49 measures"):
      assert stale not in readme
      assert stale not in index_html

  def test_docs_describe_kpi_change_callout_and_live_gate(self):
    """The docs describe what the six KPI cards and the live validator do."""
    readme = (POWER_BI / "README.md").read_text()
    manual = (POWER_BI / "USER_MANUAL.md").read_text()
    assert "vs previous period" in readme
    assert "vs previous period" in manual
    assert "--allow-empty" in readme
    assert "fails closed" in readme

  def test_dashboards_readme_has_power_bi_row(self):
    """dashboards/README.md must have a Power BI row."""
    text = (ROOT / "dashboards" / "README.md").read_text()
    assert "Power BI" in text, "dashboards/README.md missing Power BI row"
    assert (
        "power_bi/README.md" in text
    ), "dashboards/README.md must link to power_bi/README.md"

  def test_pages_links_say_the_hosted_page_goes_live_after_merge(self):
    """The hosted configurator is published from main only, so it 404s first."""
    workflow = (
        ROOT / ".github" / "workflows" / "looker-studio-pages.yml"
    ).read_text(encoding="utf-8")
    assert "branches: [main]" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    url = (
        "https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK"
        "/power-bi/"
    )
    for path in (
        POWER_BI / "README.md",
        POWER_BI / "USER_MANUAL.md",
        ROOT / "dashboards" / "README.md",
    ):
      text = " ".join(path.read_text(encoding="utf-8").split())
      assert text.count(url) == 1, path
      # The qualifier and the local alternative follow the link itself.
      after = text.split(url, 1)[1][:400]
      assert "after merge to `main`" in after, path
      assert "over HTTP" in after or "over local HTTP" in after, path

  def test_tool_series_are_not_described_as_stacked(self):
    """Tool Calls Over Time draws one line per tool, so no doc says stacked."""
    visual = json.loads(
        (
            POWER_BI
            / "BQAA_Agent_Analytics.Report"
            / "definition"
            / "pages"
            / "tool-usage"
            / "visuals"
            / "usage-tool-calls-over-time"
            / "visual.json"
        ).read_text(encoding="utf-8")
    )
    assert visual["visual"]["visualType"] == "lineChart"
    for rel in (
        "README.md",
        "USER_MANUAL.md",
        "docs/index.html",
        "docs/configurator.mjs",
        "docs/pbip-bundle.js",
    ):
      text = (POWER_BI / rel).read_text(encoding="utf-8")
      assert "stacked by tool name" not in text, rel
    for rel in ("USER_MANUAL.md", "docs/configurator.mjs"):
      text = (POWER_BI / rel).read_text(encoding="utf-8")
      assert "broken down by tool name" in text, rel

  def test_readme_says_a_card_is_never_empty(self):
    """Only a grouped visual can be EMPTY; a card must return one row."""
    readme = " ".join(
        (POWER_BI / "README.md").read_text(encoding="utf-8").split()
    )
    assert (
        "A grouped visual (area, line, or bar chart) that is empty on both"
        " sides is `EMPTY`, because nothing was compared." in readme
    )
    assert (
        "A `cardVisual` is never `EMPTY`: its DAX query must return exactly"
        " one row, and a missing oracle row fails it" in readme
    )
    assert "A chart that is empty on both sides is `EMPTY`" not in readme

  def test_preview_heading_names_its_capture_revision(self):
    """The static preview says which revision it shows and that it is static."""
    html = (POWER_BI / "docs" / "index.html").read_text(encoding="utf-8")
    match = re.search(
        r'<h2 id="preview-heading" data-capture-revision="([0-9a-f]{7,40})">'
        r"(.*?)</h2>",
        html,
        re.DOTALL,
    )
    assert match, "the preview heading names no capture revision"
    revision = match.group(1)
    heading = " ".join(match.group(2).split())
    captured = re.search(
        rf"static capture at {revision} on (\d{{4}}-\d{{2}}-\d{{2}});", heading
    )
    assert captured, heading
    assert "not a live check of the current package" in heading
    # The configurator never rewrites the heading.
    configurator = (POWER_BI / "docs" / "configurator.mjs").read_text(
        encoding="utf-8"
    )
    assert "preview-heading" not in configurator
    # Where the capture revision is in this clone's history, the label carries
    # its commit date and the screenshots are unchanged since it, so a
    # recapture must update the label.
    try:
      known = _git("cat-file", "-e", f"{revision}^{{commit}}").returncode == 0
    except FileNotFoundError:
      known = False
    if known and (
        _git("merge-base", "--is-ancestor", revision, "HEAD").returncode == 0
    ):
      shown = _git("show", "-s", "--format=%cs", revision)
      assert shown.stdout.strip() == captured.group(1)
      changed = _git(
          "diff",
          "--name-only",
          revision,
          "--",
          "dashboards/power_bi/screenshots",
      )
      assert changed.returncode == 0, changed.stderr
      assert changed.stdout == ""

  def test_web_configurator_files_and_js_pkzip(self, tmp_path):
    """Verify dashboards/power_bi/docs/ configurator files and pure-JS PKZIP builder."""
    import zipfile

    docs_dir = POWER_BI / "docs"
    for fname in (
        "index.html",
        "styles.css",
        "configurator.mjs",
        "pbip-bundle.js",
        "favicon.svg",
    ):
      fpath = docs_dir / fname
      assert fpath.is_file(), f"Missing {fpath}"
      text = fpath.read_text(encoding="utf-8")
      assert (
          "Apache License, Version 2.0" in text
      ), f"{fname} missing Apache header"
      if fname != "pbip-bundle.js":
        for vname in _FORBIDDEN_VENDOR_NAMES():
          assert (
              vname.lower() not in text.lower()
          ), f"{fname} contains forbidden name '{vname}'"

    # Security guard: no open iframe / ?embed= parameter framing.
    index_text = (docs_dir / "index.html").read_text(encoding="utf-8")
    mjs_text = (docs_dir / "configurator.mjs").read_text(encoding="utf-8")
    assert "<iframe" not in index_text.lower()
    assert "pbi-live-iframe" not in index_text
    assert "pbi-live-iframe" not in mjs_text
    assert "validatePublishToWebUrl" not in mjs_text
    assert 'params.get("embed")' not in mjs_text

    node_bin = _node_binary()

    out_zip = tmp_path / "browser_out.zip"
    script = f"""
import fs from "node:fs";
import vm from "node:vm";
import {{
  buildCustomPbipBundle,
  buildPkZip,
}} from {(docs_dir / "configurator.mjs").as_uri()!r};

const sandbox = {{ window: {{}}, Object }};
vm.runInNewContext(fs.readFileSync({str(docs_dir / "pbip-bundle.js")!r}, "utf8"), sandbox);
const bundle = sandbox.window.BQAA_PBIP_BUNDLE;
const custom = buildCustomPbipBundle(bundle, {{
  tableId: "`my-org-proj-99:prod_telemetry.agent_events`;",
  billingProject: "my-billing-proj-99",
  windowEndDate: "2026-07-16",
  lookbackDays: 14,
  useStorageApi: false,
}});
const zipBytes = buildPkZip(custom, "BQAA_Agent_Analytics");
fs.writeFileSync({str(out_zip)!r}, zipBytes);
"""
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node test failed:\n{proc.stderr}"
    with zipfile.ZipFile(out_zip, "r") as zf:
      assert zf.testzip() is None
      names = zf.namelist()
      assert len(names) == 127
      expr_text = zf.read(
          "BQAA_Agent_Analytics/BQAA_Agent_Analytics.SemanticModel/definition/expressions.tmdl"
      ).decode("utf-8")
      assert 'expression BQAA_ProjectId = "my-org-proj-99" meta' in expr_text
      assert 'expression BQAA_DatasetId = "prod_telemetry" meta' in expr_text
      assert 'expression BQAA_TableId = "agent_events" meta' in expr_text
      assert (
          'expression BQAA_BillingProjectId = "my-billing-proj-99" meta'
          in expr_text
      )
      assert 'expression BQAA_WindowEndDate = "2026-07-16" meta' in expr_text
      assert "expression BQAA_LookbackDays = 14 meta" in expr_text
      assert "expression BQAA_UseStorageApi = false meta" in expr_text


# ---------------------------------------------------------------------------
# Web configurator: the shipped configurator.mjs and pbip-bundle.js run under
# Node, so these tests exercise what the browser runs (no re-implementation).
# ---------------------------------------------------------------------------

_EXPRESSIONS_KEY = (
    "BQAA_Agent_Analytics.SemanticModel/definition/expressions.tmdl"
)
_BQAA_PARAMETERS = (
    "BQAA_ProjectId",
    "BQAA_DatasetId",
    "BQAA_TableId",
    "BQAA_BillingProjectId",
    "BQAA_WindowEndDate",
    "BQAA_LookbackDays",
    "BQAA_UseStorageApi",
)
_CUSTOM_CONFIG = {
    "tableId": "my-org-proj-99.prod_telemetry.agent_events",
    "billingProject": "my-billing-proj-99",
    "windowEndDate": "2026-07-16",
    "lookbackDays": 14,
    "useStorageApi": False,
}
_CUSTOM_IDENTIFIERS = (
    "my-org-proj-99",
    "prod_telemetry",
    "agent_events",
    "my-billing-proj-99",
)

# Loads the real module and the real bundle.  The module is imported before
# globalThis.window exists, so its browser-only auto-start stays inactive.
_NODE_PRELUDE = r"""
import crypto from "node:crypto";
import fs from "node:fs";
import vm from "node:vm";
import * as cfg from "__CONFIGURATOR_URI__";

const sandbox = { window: {}, Object };
vm.runInNewContext(fs.readFileSync("__BUNDLE_PATH__", "utf8"), sandbox);
const bundle = sandbox.window.BQAA_PBIP_BUNDLE;
const EXPR_KEY =
  "BQAA_Agent_Analytics.SemanticModel/definition/expressions.tmdl";
const emit = (value) => process.stdout.write(JSON.stringify(value));
const attempt = (fn) => {
  try {
    return { ok: true, value: fn() };
  } catch (err) {
    return { ok: false, message: String(err && err.message) };
  }
};
const sha256 = (bytes) =>
  crypto.createHash("sha256").update(bytes).digest("hex");
"""

# A minimal DOM: just enough surface for initPowerBIConfigurator to run its
# real event handlers, with the clipboard and Blob downloads captured.
_NODE_PAGE = r"""
function makeElement(id) {
  const listeners = {};
  const attributes = {};
  const el = {
    id,
    value: "",
    checked: false,
    disabled: false,
    textContent: "",
    dataset: {},
    children: [],
    clicks: 0,
    removed: false,
    addEventListener(type, fn) {
      (listeners[type] ??= []).push(fn);
    },
    setAttribute(name, value) {
      attributes[name] = String(value);
    },
    removeAttribute(name) {
      delete attributes[name];
    },
    getAttribute(name) {
      return attributes[name] ?? null;
    },
    appendChild(child) {
      el.children.push(child);
      return child;
    },
    querySelectorAll() {
      return [];
    },
    remove() {
      el.removed = true;
    },
    click() {
      el.clicks += 1;
    },
    async fire(type) {
      for (const fn of listeners[type] ?? []) {
        await fn({ type, target: el });
      }
    },
  };
  return el;
}

const PAGE_IDS = [
  "configurator", "table-id", "billing-project", "window-end-date",
  "lookback-days", "use-storage-api", "use-demo-fixture", "download-pbip",
  "copy-link", "form-status", "cli-zip-cmd", "fabric-deploy-cmd",
  "table-id-error", "billing-project-error", "window-end-date-error",
  "lookback-days-error",
];

function mountPage(href, pageBundle = bundle) {
  const els = Object.fromEntries(PAGE_IDS.map((id) => [id, makeElement(id)]));
  els["window-end-date"].value = "auto";
  els["lookback-days"].value = "90";
  els["use-storage-api"].checked = true;
  const page = { els, blobs: [], copied: [], anchors: [] };
  const doc = {
    getElementById: (id) => els[id] ?? null,
    createElement: () => {
      const created = makeElement("created");
      page.anchors.push(created);
      return created;
    },
    body: makeElement("body"),
  };
  globalThis.location = new URL(href);
  globalThis.window = { BQAA_PBIP_BUNDLE: pageBundle };
  for (const [name, value] of [
    ["createObjectURL", (blob) => `blob:test/${page.blobs.push(blob)}`],
    ["revokeObjectURL", () => {}],
  ]) {
    Object.defineProperty(URL, name, { configurable: true, writable: true, value });
  }
  globalThis.setTimeout = () => ({ unref() {} });
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    writable: true,
    value: { clipboard: { writeText: async (text) => page.copied.push(text) } },
  });
  cfg.initPowerBIConfigurator(doc);
  return page;
}

async function fillForm(page, form) {
  const { els } = page;
  els["table-id"].value = form.table;
  els["billing-project"].value = form.billing ?? "";
  els["window-end-date"].value = form.windowEnd ?? "auto";
  els["lookback-days"].value = String(form.lookback ?? 90);
  els["use-storage-api"].checked = form.storageApi ?? true;
  await els["table-id"].fire("input");
}

const fields = (page) => ({
  table: page.els["table-id"].value,
  billing: page.els["billing-project"].value,
  windowEnd: page.els["window-end-date"].value,
  lookback: page.els["lookback-days"].value,
  storageApi: page.els["use-storage-api"].checked,
  downloadDisabled: page.els["download-pbip"].disabled,
  status: page.els["form-status"].textContent,
  statusKind: page.els["form-status"].dataset.kind,
});
"""


def _node_binary():
  import glob
  import shutil

  node_bin = shutil.which("node")
  if not node_bin:
    nvm_nodes = sorted(
        glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/node"))
        + glob.glob("/usr/local/nvm/versions/node/*/bin/node")
    )
    if nvm_nodes:
      node_bin = nvm_nodes[-1]
  assert (
      node_bin is not None
  ), "node binary required for the configurator.mjs tests"
  return node_bin


def _run_node(body, *, data=None, page=False):
  """Runs `body` after the prelude and returns the JSON it emitted."""
  docs_dir = POWER_BI / "docs"
  prelude = _NODE_PRELUDE.replace(
      "__CONFIGURATOR_URI__", (docs_dir / "configurator.mjs").as_uri()
  ).replace('"__BUNDLE_PATH__"', json.dumps(str(docs_dir / "pbip-bundle.js")))
  script = "\n".join(
      [
          prelude,
          f"const data = {json.dumps(data)};",
          _NODE_PAGE if page else "",
          body,
      ]
  )
  proc = subprocess.run(
      [_node_binary(), "--input-type=module", "-e", script],
      capture_output=True,
      text=True,
      check=False,
      timeout=120,
  )
  assert proc.returncode == 0, f"Node script failed:\n{proc.stderr}"
  return json.loads(proc.stdout)


def _without_declaration(text, name):
  lines = text.split("\n")
  idx = next(
      i
      for i, line in enumerate(lines)
      if line.startswith(f"expression {name} = ")
  )
  return "\n".join(lines[:idx] + lines[idx + 1 :])


def _with_duplicate_declaration(text, name):
  lines = text.split("\n")
  idx = next(
      i
      for i, line in enumerate(lines)
      if line.startswith(f"expression {name} = ")
  )
  return "\n".join(lines[: idx + 1] + [lines[idx]] + lines[idx + 1 :])


class TestWebConfigurator:
  """The browser packager and the setup link, as a visitor would use them."""

  # -- Duplicate or missing declarations in the bundled expressions.tmdl -----

  def test_rewrite_rejects_a_missing_or_duplicated_declaration(self):
    """Each BQAA_* parameter must be declared exactly once, as in Python."""
    gen_mod = _load_tool_module("generate_pbip")
    out = _run_node(
        r"""
const base = bundle[EXPR_KEY];
const lines = base.split("\n");
const cases = {};
for (const name of data.names) {
  const idx = lines.findIndex((l) => l.startsWith(`expression ${name} = `));
  const dropped = [...lines.slice(0, idx), ...lines.slice(idx + 1)].join("\n");
  const doubled = [
    ...lines.slice(0, idx + 1), lines[idx], ...lines.slice(idx + 1),
  ].join("\n");
  const packaged = (text) =>
    Object.keys(
      cfg.buildCustomPbipBundle({ ...bundle, [EXPR_KEY]: text }, data.config),
    ).length;
  cases[name] = {
    declared: idx >= 0,
    dropped: attempt(() => cfg.rewriteExpressionsTmdl(dropped, data.config)),
    doubled: attempt(() => cfg.rewriteExpressionsTmdl(doubled, data.config)),
    droppedBundle: attempt(() => packaged(dropped)),
    doubledBundle: attempt(() => packaged(doubled)),
  };
}
emit({ base, cases, intact: attempt(() => cfg.buildCustomPbipBundle(bundle, data.config)[EXPR_KEY].length) });
""",
        data={"names": list(_BQAA_PARAMETERS), "config": _CUSTOM_CONFIG},
    )

    assert out["intact"]["ok"] is True
    python_kwargs = {
        "project": "my-org-proj-99",
        "dataset": "prod_telemetry",
        "table": "agent_events",
        "billing_project": "my-billing-proj-99",
        "window_end": "2026-07-16",
        "lookback_days": 14,
        "use_storage_api": False,
    }
    for name in _BQAA_PARAMETERS:
      case = out["cases"][name]
      assert case["declared"], f"{name} is not declared in the bundle"
      for key, count, text in (
          ("dropped", 0, _without_declaration(out["base"], name)),
          ("doubled", 2, _with_duplicate_declaration(out["base"], name)),
      ):
        message = (
            f"Expected exactly 1 declaration for {name} in expressions.tmdl,"
            f" found {count}"
        )
        # Rewriting and the full packaging path both refuse the bundle ...
        assert case[key] == {"ok": False, "message": message}, (name, key)
        assert case[f"{key}Bundle"] == {"ok": False, "message": message}, (
            name,
            key,
        )
        # ... with the same words the Python generator uses.
        with pytest.raises(ValueError) as python_error:
          gen_mod.rewrite_expressions_parameters(text, **python_kwargs)
        assert str(python_error.value) == message

  # Duplicate declarations a hand edit could leave behind.  None carries the
  # `meta [IsParameterQuery=true, ...]` annotation the rewrite itself matches.
  # Object names are case-insensitive, so `{lower}` and `{upper}` spell the
  # same parameter.
  _DUPLICATE_FORMS = {
      "metadata_free": 'expression {name} = "stale"',
      "quoted_name": "expression '{name}' = \"stale\"",
      "multiline": 'expression {name} =\n\t\t"stale"',
      "indented_no_spaces": '  expression {name}="stale"',
      "tab_separated": 'expression\t{name}\t= "stale"',
      "lower_case_name": 'expression {lower} = "stale"',
      "quoted_upper_case_name": "expression '{upper}' = \"stale\"",
  }
  # Lines that only look like a declaration.
  _NOT_DECLARATIONS = (
      '/// expression BQAA_ProjectId = "stale"\n'
      '// expression BQAA_ProjectId = "stale"\n'
      "\t/// expression 'BQAA_DatasetId' = \"stale\"\n"
      'expression BQAA_ProjectIdBackup = "stale"\n'
      'expression XBQAA_TableId = "stale"\n'
      'expression BQAA_TableIds = "stale"'
  )
  _PYTHON_KWARGS = {
      "project": "my-org-proj-99",
      "dataset": "prod_telemetry",
      "table": "agent_events",
      "billing_project": "my-billing-proj-99",
      "window_end": "2026-07-16",
      "lookback_days": 14,
      "use_storage_api": False,
  }

  def test_rewrite_rejects_a_duplicate_declaration_without_metadata(self):
    """Every duplicate form fails in the browser and in Python, for all seven."""
    gen_mod = _load_tool_module("generate_pbip")
    out = _run_node(
        r"""
const base = bundle[EXPR_KEY];
const results = {};
for (const name of data.names) {
  for (const [form, template] of Object.entries(data.forms)) {
    const duplicate = template
      .replaceAll("{name}", name)
      .replaceAll("{lower}", name.toLowerCase())
      .replaceAll("{upper}", name.toUpperCase());
    for (const [where, text] of [
      ["appended", `${base}\n${duplicate}\n`],
      ["prepended", `${duplicate}\n\n${base}`],
    ]) {
      results[`${name}|${form}|${where}`] = {
        rewrite: attempt(() => cfg.rewriteExpressionsTmdl(text, data.config)),
        bundle: attempt(() =>
          Object.keys(
            cfg.buildCustomPbipBundle({ ...bundle, [EXPR_KEY]: text }, data.config),
          ).length,
        ),
      };
    }
  }
}
const noisy = `${base}\n${data.noise}\n`;
emit({
  base,
  results,
  noisy: attempt(() => cfg.rewriteExpressionsTmdl(noisy, data.config)),
});
""",
        data={
            "names": list(_BQAA_PARAMETERS),
            "forms": self._DUPLICATE_FORMS,
            "noise": self._NOT_DECLARATIONS,
            "config": _CUSTOM_CONFIG,
        },
    )
    base = out["base"]
    assert len(out["results"]) == len(_BQAA_PARAMETERS) * 7 * 2
    for key, result in out["results"].items():
      name, form, where = key.split("|")
      message = (
          f"Expected exactly 1 declaration for {name} in expressions.tmdl,"
          " found 2"
      )
      # The rewrite and the full packaging path both refuse the bundle ...
      assert result["rewrite"] == {"ok": False, "message": message}, key
      assert result["bundle"] == {"ok": False, "message": message}, key
      # ... and so does the Python generator, with the same words.
      duplicate = self._DUPLICATE_FORMS[form].format(
          name=name, lower=name.lower(), upper=name.upper()
      )
      text = (
          f"{base}\n{duplicate}\n"
          if where == "appended"
          else f"{duplicate}\n\n{base}"
      )
      with pytest.raises(ValueError) as python_error:
        gen_mod.rewrite_expressions_parameters(text, **self._PYTHON_KWARGS)
      assert str(python_error.value) == message, key
    # Control: comments and similar names are not declarations, and both
    # packagers rewrite around them identically.
    noisy = f"{base}\n{self._NOT_DECLARATIONS}\n"
    assert out["noisy"]["ok"] is True, out["noisy"]
    assert out["noisy"]["value"] == gen_mod.rewrite_expressions_parameters(
        noisy, **self._PYTHON_KWARGS
    )
    assert out["noisy"]["value"].endswith(f"{self._NOT_DECLARATIONS}\n")
    assert (
        out["noisy"]["value"].count(
            'expression BQAA_ProjectId = "my-org-proj-99" meta'
        )
        == 1
    )

  # Every separator Python's str.splitlines() breaks on.  buildPkZip turns a
  # lone CR into a line break, so a declaration after one is a real line.
  _LINE_SEPARATORS = (
      "\r",
      "\v",
      "\f",
      "\x1c",
      "\x1d",
      "\x1e",
      "\x85",
      "\u2028",
      "\u2029",
  )

  @staticmethod
  def _fullwidth(text):
    return "".join(
        chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text
    )

  def _normalization_cases(self, base):
    """Duplicates and controls that only a normalizing guard reads alike.

    Each case is a (prefix, body, suffix) spec, where `body` names the
    template the prefix and suffix wrap: the bundled one ("base"), or that
    template without its `///` doc comments ("bare").  Specs keep the Node
    command line short; both languages compose the same text from them.
    """
    bom = "\ufeff"
    rejected = {}
    for name in _BQAA_PARAMETERS:
      canonical = next(
          line
          for line in base.split("\n")
          if line.startswith(f"expression {name} = ")
      )
      # A file-start byte-order mark in front of a first-line duplicate.
      rejected[f"{name}|bom|metadata_free"] = (
          f'{bom}expression {name} = "stale"\n',
          "base",
          "",
      )
      rejected[f"{name}|bom|quoted_upper_case"] = (
          f"{bom}expression '{name.upper()}' = \"stale\"\n",
          "base",
          "",
      )
      rejected[f"{name}|bom|annotated"] = (f"{bom}{canonical}\n", "base", "")
      # A duplicate after a separator that follows a comment.
      for index, separator in enumerate(self._LINE_SEPARATORS):
        rejected[f"{name}|after_comment|{index}"] = (
            "",
            "base",
            f'\n// note{separator}expression {name} = "stale"\n',
        )
      # Width and other compatibility forms of the name and the spaces.
      rejected[f"{name}|compat|fullwidth"] = (
          "",
          "base",
          f'\nexpression {self._fullwidth(name)} = "stale"\n',
      )
      rejected[f"{name}|compat|quoted_fullwidth_lower"] = (
          "",
          "base",
          f"\nexpression '{self._fullwidth(name.lower())}' = \"stale\"\n",
      )
      rejected[f"{name}|compat|unicode_spaces"] = (
          "",
          "base",
          f'\nexpression\u00a0{name}\u3000= "stale"\n',
      )
    # Letters that Unicode relates to ASCII ones: KELVIN SIGN and LONG S.
    for label, name, variant in (
        ("kelvin_sign", "BQAA_LookbackDays", "BQAA_Loo\u212abackDays"),
        ("long_s", "BQAA_UseStorageApi", "BQAA_U\u017feStorageApi"),
        ("long_s", "BQAA_DatasetId", "BQAA_Data\u017fetId"),
    ):
      rejected[f"{name}|compat|{label}"] = (
          "",
          "base",
          f'\nexpression {variant} = "stale"\n',
      )
    comment = '// expression BQAA_ProjectId = "stale"'
    similar = '\n// note\rexpression BQAA_ProjectIdBackup = "x"\n'
    # Accepted spec, and the spec whose rewrite it must equal.
    accepted = {
        "bom_only": ((bom, "base", ""), ("", "base", "")),
        "bom_before_first_declaration": ((bom, "bare", ""), ("", "bare", "")),
        "bom_before_comment": (
            (f"{bom}{comment}\n", "base", ""),
            (f"{comment}\n", "base", ""),
        ),
        "similar_name_after_cr": (("", "base", similar), ("", "base", similar)),
    }
    return rejected, accepted

  def test_rewrite_normalizes_bom_separators_and_name_forms_like_python(self):
    """Both packagers read a declaration the same way, so both refuse it."""
    gen_mod = _load_tool_module("generate_pbip")
    base = (POWER_BI / _EXPRESSIONS_KEY).read_text(encoding="utf-8")
    assert base.isascii() and "\r" not in base
    bodies = {
        "base": base,
        "bare": "\n".join(
            line for line in base.split("\n") if not line.startswith("///")
        ),
    }
    assert bodies["bare"].startswith("expression BQAA_ProjectId = ")

    def compose(spec):
      prefix, body, suffix = spec
      return f"{prefix}{bodies[body]}{suffix}"

    rejected, accepted = self._normalization_cases(base)
    assert len(rejected) == len(_BQAA_PARAMETERS) * (3 + 9 + 3) + 3
    specs = dict(rejected)
    specs.update({key: spec for key, (spec, _) in accepted.items()})
    out = _run_node(
        r"""
const base = bundle[EXPR_KEY];
const bodies = {
  base,
  bare: base.split("\n").filter((l) => !l.startsWith("///")).join("\n"),
};
const results = {};
for (const [key, [prefix, body, suffix]] of Object.entries(data.specs)) {
  const text = prefix + bodies[body] + suffix;
  results[key] = {
    rewrite: attempt(() => cfg.rewriteExpressionsTmdl(text, data.config)),
    bundle: attempt(() =>
      cfg.buildCustomPbipBundle({ ...bundle, [EXPR_KEY]: text }, data.config)[
        EXPR_KEY
      ],
    ),
  };
}
emit({ bodies, results });
""",
        data={"specs": specs, "config": _CUSTOM_CONFIG},
    )
    # Both languages composed their texts from the same template.
    assert out["bodies"] == bodies
    assert set(out["results"]) == set(specs)
    for key, spec in rejected.items():
      name = key.split("|")[0]
      message = (
          f"Expected exactly 1 declaration for {name} in expressions.tmdl,"
          " found 2"
      )
      result = out["results"][key]
      assert result["rewrite"] == {"ok": False, "message": message}, key
      assert result["bundle"] == {"ok": False, "message": message}, key
      with pytest.raises(ValueError) as python_error:
        gen_mod.rewrite_expressions_parameters(
            compose(spec), **self._PYTHON_KWARGS
        )
      assert str(python_error.value) == message, key
    for key, (spec, clean_spec) in accepted.items():
      expected = gen_mod.rewrite_expressions_parameters(
          compose(clean_spec), **self._PYTHON_KWARGS
      )
      assert (
          gen_mod.rewrite_expressions_parameters(
              compose(spec), **self._PYTHON_KWARGS
          )
          == expected
      ), key
      result = out["results"][key]
      assert result["rewrite"] == {"ok": True, "value": expected}, key
      assert result["bundle"] == {"ok": True, "value": expected}, key
      assert not expected.startswith("\ufeff"), key

  @pytest.mark.parametrize(
      "config,python_kwargs",
      [
          (
              _CUSTOM_CONFIG,
              {
                  "project": "my-org-proj-99",
                  "dataset": "prod_telemetry",
                  "table": "agent_events",
                  "billing_project": "my-billing-proj-99",
                  "window_end": "2026-07-16",
                  "lookback_days": 14,
                  "use_storage_api": False,
              },
          ),
          (
              {"tableId": "my-org-proj-99.prod_telemetry.agent_events"},
              {
                  "project": "my-org-proj-99",
                  "dataset": "prod_telemetry",
                  "table": "agent_events",
              },
          ),
          (
              {"tableId": "`my-org-proj-99:prod_telemetry.agent_events`;"},
              {
                  "project": "my-org-proj-99",
                  "dataset": "prod_telemetry",
                  "table": "agent_events",
              },
          ),
          (
              {
                  "tableId": "abcde-1.ds_1.tbl-1",
                  "billingProject": "other-billing-1",
                  "windowEndDate": "auto",
                  "lookbackDays": 366,
                  "useStorageApi": True,
              },
              {
                  "project": "abcde-1",
                  "dataset": "ds_1",
                  "table": "tbl-1",
                  "billing_project": "other-billing-1",
                  "window_end": "auto",
                  "lookback_days": 366,
                  "use_storage_api": True,
              },
          ),
          (
              {
                  "tableId": "abcde-1.ds_1.tbl-1",
                  "windowEndDate": "2028-02-29",
                  "lookbackDays": 1,
              },
              {
                  "project": "abcde-1",
                  "dataset": "ds_1",
                  "table": "tbl-1",
                  "window_end": "2028-02-29",
                  "lookback_days": 1,
              },
          ),
      ],
  )
  def test_browser_rewrite_matches_the_python_generator(
      self, config, python_kwargs
  ):
    gen_mod = _load_tool_module("generate_pbip")
    out = _run_node(
        r"""
const base = bundle[EXPR_KEY];
emit({
  base,
  rewritten: cfg.rewriteExpressionsTmdl(base, data.config),
  viaBundle: cfg.buildCustomPbipBundle(bundle, data.config)[EXPR_KEY],
});
""",
        data={"config": config},
    )
    expected = gen_mod.rewrite_expressions_parameters(
        out["base"], **python_kwargs
    )
    assert out["rewritten"] == expected
    assert out["viaBundle"] == expected
    assert out["rewritten"] != out["base"] or config["tableId"] == (
        "my-org-proj-99.prod_telemetry.agent_events"
    )

  def test_download_refuses_a_bundle_with_a_duplicated_declaration(self):
    """The Download button reports the malformed bundle and ships no ZIP."""
    gen_mod = _load_tool_module("generate_pbip")
    del gen_mod  # Only used to fail fast if the generator cannot be loaded.
    out = _run_node(
        r"""
const lines = bundle[EXPR_KEY].split("\n");
const idx = lines.findIndex((l) => l.startsWith("expression BQAA_LookbackDays = "));
const doubled = [
  ...lines.slice(0, idx + 1), lines[idx], ...lines.slice(idx + 1),
].join("\n");

const good = mountPage(data.href);
await fillForm(good, data.form);
await good.els["download-pbip"].fire("click");

const bad = mountPage(data.href, { ...bundle, [EXPR_KEY]: doubled });
await fillForm(bad, data.form);
const before = fields(bad);
await bad.els["download-pbip"].fire("click");
emit({
  good: { blobs: good.blobs.length, anchors: good.anchors.length, ...fields(good) },
  badBefore: before,
  bad: { blobs: bad.blobs.length, anchors: bad.anchors.length, ...fields(bad) },
});
""",
        data={
            "href": "http://127.0.0.1/power-bi/",
            "form": {"table": "my-org-proj-99.prod_telemetry.agent_events"},
        },
        page=True,
    )
    assert out["good"]["blobs"] == 1
    assert out["good"]["statusKind"] == "ready"
    assert out["good"]["status"].startswith("Downloaded BQAA_Agent_Analytics_")
    # Control: the page is ready to download before the click.
    assert out["badBefore"]["statusKind"] == "ready"
    assert out["badBefore"]["downloadDisabled"] is False
    assert out["bad"]["blobs"] == 0
    assert out["bad"]["anchors"] == 0
    assert out["bad"]["statusKind"] == "error"
    assert out["bad"]["status"] == (
        "Expected exactly 1 declaration for BQAA_LookbackDays in"
        " expressions.tmdl, found 2"
    )

  @pytest.mark.parametrize(
      "prefix,suffix",
      [
          pytest.param(
              "", "\nexpression bqaa_lookbackdays = 7\n", id="lower_case"
          ),
          pytest.param(
              "",
              "\nexpression 'BQAA_LOOKBACKDAYS' = 7\n",
              id="quoted_upper_case",
          ),
          pytest.param(
              "\ufeffexpression BQAA_LookbackDays = 7\n",
              "",
              id="bom_first_line",
          ),
      ],
  )
  def test_download_refuses_case_variant_and_bom_duplicates(
      self, prefix, suffix
  ):
    """The whole download path refuses them; a lone leading BOM is fine."""
    out = _run_node(
        r"""
const template = bundle[EXPR_KEY];
const good = mountPage(data.href, { ...bundle, [EXPR_KEY]: "\uFEFF" + template });
await fillForm(good, data.form);
await good.els["download-pbip"].fire("click");

const bad = mountPage(data.href, {
  ...bundle,
  [EXPR_KEY]: data.prefix + template + data.suffix,
});
await fillForm(bad, data.form);
const before = fields(bad);
await bad.els["download-pbip"].fire("click");
emit({
  good: { blobs: good.blobs.length, ...fields(good) },
  badBefore: before,
  bad: { blobs: bad.blobs.length, anchors: bad.anchors.length, ...fields(bad) },
});
""",
        data={
            "href": "http://127.0.0.1/power-bi/",
            "form": {"table": "my-org-proj-99.prod_telemetry.agent_events"},
            "prefix": prefix,
            "suffix": suffix,
        },
        page=True,
    )
    # Control: a template that only gains a byte-order mark still downloads.
    assert out["good"]["blobs"] == 1
    assert out["good"]["statusKind"] == "ready"
    assert out["good"]["status"].startswith("Downloaded BQAA_Agent_Analytics_")
    assert out["badBefore"]["statusKind"] == "ready"
    assert out["badBefore"]["downloadDisabled"] is False
    assert out["bad"]["blobs"] == 0
    assert out["bad"]["anchors"] == 0
    assert out["bad"]["statusKind"] == "error"
    assert out["bad"]["status"] == (
        "Expected exactly 1 declaration for BQAA_LookbackDays in"
        " expressions.tmdl, found 2"
    )

  # -- Setup links keep the configuration out of anything sent to a server ---

  def test_setup_link_puts_the_configuration_in_the_fragment_only(self):
    out = _run_node(
        r"""
emit({
  full: cfg.buildSetupUrl(
    data.config,
    "http://127.0.0.1:8123/power-bi/?stale=1&project=old-project-1#old-fragment",
  ),
  minimal: cfg.buildSetupUrl(
    { tableId: "abcde-1.ds_1.tbl-1" },
    "https://example.test/power-bi/?x=1#y",
  ),
});
""",
        data={"config": _CUSTOM_CONFIG},
    )
    full = urllib.parse.urlsplit(out["full"])
    assert (full.scheme, full.netloc, full.path) == (
        "http",
        "127.0.0.1:8123",
        "/power-bi/",
    )
    assert full.query == ""
    assert urllib.parse.parse_qs(full.fragment, strict_parsing=True) == {
        "project": ["my-org-proj-99"],
        "dataset": ["prod_telemetry"],
        "table": ["agent_events"],
        "billingProject": ["my-billing-proj-99"],
        "windowEndDate": ["2026-07-16"],
        "lookbackDays": ["14"],
        "useStorageApi": ["false"],
    }
    request_part = out["full"].split("#", 1)[0]
    for identifier in _CUSTOM_IDENTIFIERS:
      assert identifier not in request_part
      assert identifier in full.fragment
    assert "old-project-1" not in out["full"]

    minimal = urllib.parse.urlsplit(out["minimal"])
    assert minimal.query == ""
    assert urllib.parse.parse_qs(minimal.fragment, strict_parsing=True) == {
        "project": ["abcde-1"],
        "dataset": ["ds_1"],
        "table": ["tbl-1"],
    }

  def test_copied_setup_link_never_sends_the_configuration_to_the_server(self):
    """A loopback server records the request target a browser would send."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):

      def do_GET(self):  # pylint: disable=invalid-name
        seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

      def log_message(self, *args):
        del args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
      page_url = f"http://127.0.0.1:{server.server_address[1]}/power-bi/"
      out = _run_node(
          r"""
const page = mountPage(data.href);
await fillForm(page, data.form);
await page.els["copy-link"].fire("click");
emit({ copied: page.copied, status: page.els["form-status"].textContent });
""",
          data={
              "href": page_url,
              "form": {
                  "table": "my-org-proj-99.prod_telemetry.agent_events",
                  "billing": "my-billing-proj-99",
                  "windowEnd": "2026-07-16",
                  "lookback": 14,
                  "storageApi": False,
              },
          },
          page=True,
      )
      assert len(out["copied"]) == 1
      copied = out["copied"][0]
      assert copied.startswith(page_url + "#")
      assert urllib.request.Request(copied).selector == "/power-bi/"

      # Bypass any proxy settings: the request must reach the loopback server.
      opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
      with opener.open(copied, timeout=10) as response:
        assert response.status == 200
        response.read()
    finally:
      server.shutdown()
      server.server_close()
      thread.join(timeout=10)

    assert seen == ["/power-bi/"]
    for identifier in _CUSTOM_IDENTIFIERS:
      assert identifier not in seen[0]
    assert "never send" in out["status"]

  def test_setup_link_round_trips_through_the_page_to_an_identical_zip(self):
    out = _run_node(
        r"""
const first = mountPage(data.href);
await fillForm(first, data.form);
await first.els["copy-link"].fire("click");
const copiedUrl = first.copied[0];

const second = mountPage(copiedUrl);
const hydrated = fields(second);
await second.els["download-pbip"].fire("click");
const pageZip = new Uint8Array(await second.blobs[0].arrayBuffer());
const direct = cfg.buildPkZip(
  cfg.buildCustomPbipBundle(bundle, data.config),
  "BQAA_Agent_Analytics",
);
emit({
  copiedUrl,
  hydrated,
  blobs: second.blobs.length,
  blobType: second.blobs[0].type,
  downloadName: second.anchors[0].download,
  pageZipSha: sha256(pageZip),
  directZipSha: sha256(direct),
  zipBytes: pageZip.length,
});
""",
        data={
            "href": "http://127.0.0.1:8123/power-bi/?stale=1#old",
            "config": _CUSTOM_CONFIG,
            "form": {
                "table": "my-org-proj-99.prod_telemetry.agent_events",
                "billing": "my-billing-proj-99",
                "windowEnd": "2026-07-16",
                "lookback": 14,
                "storageApi": False,
            },
        },
        page=True,
    )
    assert urllib.parse.urlsplit(out["copiedUrl"]).query == ""
    hydrated = out["hydrated"]
    assert hydrated["table"] == "my-org-proj-99.prod_telemetry.agent_events"
    assert hydrated["billing"] == "my-billing-proj-99"
    assert hydrated["windowEnd"] == "2026-07-16"
    assert hydrated["lookback"] == "14"
    assert hydrated["storageApi"] is False
    assert hydrated["downloadDisabled"] is False
    assert hydrated["statusKind"] == "ready"
    assert out["blobs"] == 1
    assert out["blobType"] == "application/zip"
    assert out["downloadName"] == (
        "BQAA_Agent_Analytics_my-org-proj-99_prod_telemetry_agent_events.zip"
    )
    assert out["zipBytes"] > 0
    assert out["pageZipSha"] == out["directZipSha"]

  def test_query_string_setup_links_are_not_read(self):
    """Old-style ?project=...&dataset=... links no longer configure the page."""
    out = _run_node(
        r"""
emit({
  legacy: fields(mountPage(data.legacy)),
  fragment: fields(mountPage(data.fragment)),
  tableId: fields(mountPage(data.tableId)),
  invalid: fields(mountPage(data.invalid)),
});
""",
        data={
            "legacy": (
                "http://127.0.0.1/power-bi/?project=my-org-proj-99"
                "&dataset=prod_telemetry&table=agent_events"
                "&billingProject=other-billing-1&windowEndDate=2026-07-16"
                "&lookbackDays=14&useStorageApi=false"
            ),
            "fragment": (
                "http://127.0.0.1/power-bi/#project=my-org-proj-99"
                "&dataset=prod_telemetry&table=agent_events"
                "&billingProject=other-billing-1&windowEndDate=2026-07-16"
                "&lookbackDays=14&useStorageApi=false"
            ),
            "tableId": (
                "http://127.0.0.1/power-bi/"
                "#tableId=my-org-proj-99.prod_telemetry.agent_events"
            ),
            "invalid": (
                "http://127.0.0.1/power-bi/#project=BAD_PROJECT"
                "&dataset=prod_telemetry&table=agent_events"
            ),
        },
        page=True,
    )
    legacy = out["legacy"]
    assert legacy["table"] == ""
    assert legacy["billing"] == ""
    assert legacy["windowEnd"] == "auto"
    assert legacy["lookback"] == "90"
    assert legacy["storageApi"] is True
    assert legacy["downloadDisabled"] is True

    # Control: the same settings in the fragment do configure the page.
    fragment = out["fragment"]
    assert fragment["table"] == "my-org-proj-99.prod_telemetry.agent_events"
    assert fragment["billing"] == "other-billing-1"
    assert fragment["windowEnd"] == "2026-07-16"
    assert fragment["lookback"] == "14"
    assert fragment["storageApi"] is False
    assert fragment["downloadDisabled"] is False

    assert out["tableId"]["table"] == (
        "my-org-proj-99.prod_telemetry.agent_events"
    )
    assert out["tableId"]["downloadDisabled"] is False
    # A hydrated link still goes through validation.
    assert out["invalid"]["statusKind"] == "error"
    assert out["invalid"]["downloadDisabled"] is True

  def test_docs_say_where_the_setup_link_keeps_its_settings(self):
    docs_dir = POWER_BI / "docs"
    index_text = " ".join(
        (docs_dir / "index.html").read_text(encoding="utf-8").split()
    )
    assert "after <code>#</code>" in index_text
    assert "browsers never send in HTTP requests to the host" in index_text
    assert "share copied links deliberately" in index_text
    assert "never leave your browser" not in index_text
    manual = " ".join(
        (POWER_BI / "USER_MANUAL.md").read_text(encoding="utf-8").split()
    )
    assert "Copy setup link" in manual
    assert "browsers never send to the server in HTTP requests" in manual
    assert "share copied links deliberately" in manual
    assert "http.server 8799" in manual
    readme = " ".join(
        (POWER_BI / "README.md").read_text(encoding="utf-8").split()
    )
    assert "Copy setup link" in readme
    assert "fragment" in readme
    assert "share copied links deliberately" in readme
    assert "http.server 8799" in readme


# ---------------------------------------------------------------------------
# CI workflow tests
# ---------------------------------------------------------------------------


class TestCIWorkflow:
  """Verify the CI workflow file."""

  def test_workflow_exists(self):
    path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    assert path.exists()

  def test_workflow_has_apache_header(self):
    path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    text = path.read_text()
    assert "Apache License" in text
    assert "Copyright 2026 Google LLC" in text

  def test_workflow_actions_pinned_by_sha(self):
    path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    text = path.read_text()
    # Check that checkout and setup-python are pinned by SHA.
    assert "actions/checkout@" in text
    assert "actions/setup-python@" in text
    # Verify SHA pins (not version tags like @v4).
    checkout_match = re.search(r"actions/checkout@([0-9a-f]{40})", text)
    assert checkout_match, "checkout not pinned by SHA"
    python_match = re.search(r"actions/setup-python@([0-9a-f]{40})", text)
    assert python_match, "setup-python not pinned by SHA"

  def test_workflow_permissions_read_only(self):
    path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    text = path.read_text()
    assert "permissions:" in text
    assert "contents: read" in text

  def test_workflow_triggers_on_correct_paths(self):
    path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    text = path.read_text()
    assert "dashboards/power_bi/**" in text
    assert "dashboards/looker_studio/spec/**" in text
    assert "tests/test_power_bi_*.py" in text

  @staticmethod
  def _workflow_doc():
    path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    return yaml.safe_load(path.read_text())

  def test_workflow_runs_in_the_merge_queue(self):
    """`merge_group` is a trigger, so a merge queue runs this check."""
    doc = self._workflow_doc()
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), triggers
    assert "merge_group" in triggers, sorted(map(str, triggers))
    assert triggers["merge_group"] == {"types": ["checks_requested"]}
    assert "push" in triggers and "pull_request" in triggers

  @staticmethod
  def _install_requirements(run_text):
    """Quoted requirement specifiers of a `pip install` command."""
    assert "pip install" in run_text, run_text
    return re.findall(r'"([^"]+)"', run_text)

  @staticmethod
  def _is_exact_pin(requirement):
    return re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9]+(\.[0-9]+)*", requirement)

  def test_workflow_pins_python_dependencies_exactly(self):
    """Validation dependencies change only through a repository diff."""
    steps = self._workflow_doc()["jobs"]["power-bi-sync"]["steps"]
    installs = [s["run"] for s in steps if "pip install" in s.get("run", "")]
    assert len(installs) == 1, installs
    requirements = self._install_requirements(installs[0])
    assert [r.split("==")[0] for r in requirements] == [
        "pyyaml",
        "jsonschema",
        "pytest",
    ]
    assert all(self._is_exact_pin(r) for r in requirements), requirements

  @pytest.mark.parametrize(
      "requirement",
      [
          "pyyaml>=6.0",
          "jsonschema~=4.18",
          "pytest",
          "pytest==8.*",
          "pyyaml==6.0.2; python_version>'3'",
          "pytest===8.3.4",
      ],
  )
  def test_exact_pin_check_rejects_ranges(self, requirement):
    """The exact-pin check can fail: ranges, wildcards and markers are rejected."""
    assert not self._is_exact_pin(requirement)

  def test_workflow_install_line_rejects_open_ended_ranges(self):
    """The requirement parser sees the three specifiers of an unpinned line."""
    old = 'pip install "pyyaml>=6.0" "jsonschema>=4.18" "pytest>=7.0"'
    requirements = self._install_requirements(old)
    assert requirements == ["pyyaml>=6.0", "jsonschema>=4.18", "pytest>=7.0"]
    assert not any(self._is_exact_pin(r) for r in requirements)

  @pytest.mark.parametrize(
      "rel",
      [
          "dashboards/power_bi/spec/semantic_model.yaml",
          "dashboards/power_bi/spec/report_visuals.yaml",
          "dashboards/power_bi/spec/pbir_schemas.lock.json",
          ".github/workflows/power-bi-sync-check.yml",
      ],
  )
  def test_hand_written_inputs_pass_whitespace_hygiene(self, rel):
    """No trailing whitespace and exactly one newline at EOF (git diff --check)."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert text.endswith("\n"), f"{rel} must end with a newline"
    assert not text.endswith("\n\n"), f"{rel} has a blank line at EOF"
    bad = [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if line != line.rstrip()
    ]
    assert not bad, f"{rel}: trailing whitespace on lines {bad}"

  @pytest.mark.parametrize(
      "text, ok",
      [
          ("a: 1\n", True),
          ("a: 1", False),
          ("a: 1\n\n", False),
          ("a: 1 \n", False),
      ],
  )
  def test_whitespace_hygiene_rules_can_fail(self, text, ok):
    """The EOF/whitespace rules reject a missing newline, a blank EOF line, or padding."""
    passes = (
        text.endswith("\n")
        and not text.endswith("\n\n")
        and all(line == line.rstrip() for line in text.splitlines())
    )
    assert passes is ok

  def test_workflow_no_vendor_names(self):
    path = ROOT / ".github" / "workflows" / "power-bi-sync-check.yml"
    text = path.read_text()
    for name in _FORBIDDEN_VENDOR_NAMES():
      assert name.lower() not in text.lower()

  def test_no_internal_review_or_vault_shorthand_tags(self):
    """Tracked Power BI files and tests must not contain internal shorthand IDs."""
    patterns = (
        re.compile(r"#6\d\d\b"),
        re.compile(r"#ugc-" + r"host-embed\b"),
        re.compile(r"\bR\d+-(?:F|P|NIT)\d*\b"),
    )
    candidates = list(POWER_BI.rglob("*")) + list(
        (ROOT / "tests").glob("test_power_bi_*.py")
    )
    for path in candidates:
      if not path.is_file() or path.suffix.lower() in (".png", ".ico", ".zip"):
        continue
      try:
        text = path.read_text(encoding="utf-8")
      except UnicodeDecodeError:
        continue
      for pat in patterns:
        match = pat.search(text)
        assert match is None, (
            f"{path.relative_to(ROOT)} contains internal shorthand tag "
            f"{match.group(0)!r}"
        )

  # -- GitHub Pages staging (looker-studio-pages.yml) -------------------------

  _PAGES_STAGE_STEP = (
      "Stage Power BI configurator and screenshots under /power-bi"
  )

  @classmethod
  def _pages_stage_script(cls):
    path = ROOT / ".github" / "workflows" / "looker-studio-pages.yml"
    jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
    steps = jobs["deploy"]["steps"]
    stage = [s for s in steps if s.get("name") == cls._PAGES_STAGE_STEP]
    assert len(stage) == 1, [s.get("name") for s in steps]
    return stage[0]["run"]

  @pytest.mark.parametrize("site_has_screenshots", [True, False])
  def test_pages_staging_keeps_site_files_it_does_not_own(
      self, tmp_path, site_has_screenshots
  ):
    """Staging adds the Power BI page and images; the site's files survive."""
    import shutil

    site = tmp_path / "dashboards" / "looker_studio" / "docs"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<p>site</p>\n", encoding="utf-8")
    keep = site / "screenshots" / "keep.txt"
    if site_has_screenshots:
      keep.parent.mkdir()
      keep.write_text("site asset\n", encoding="utf-8")
    stale = site / "power-bi" / "stale.txt"
    stale.parent.mkdir()
    stale.write_text("previous build\n", encoding="utf-8")
    pbi = tmp_path / "dashboards" / "power_bi"
    shutil.copytree(POWER_BI / "docs", pbi / "docs")
    shutil.copytree(POWER_BI / "screenshots", pbi / "screenshots")

    script = self._pages_stage_script()
    for _ in range(2):  # A re-run stages the same tree.
      proc = subprocess.run(
          ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
          cwd=tmp_path,
          capture_output=True,
          text=True,
          check=False,
      )
      assert proc.returncode == 0, proc.stderr

    assert (site / "index.html").read_text(encoding="utf-8") == "<p>site</p>\n"
    if site_has_screenshots:
      assert keep.read_text(encoding="utf-8") == "site asset\n"
    # The step owns /power-bi only, so a stale file there is replaced.
    assert not stale.exists()
    page = site / "power-bi"
    assert (page / "index.html").read_bytes() == (
        POWER_BI / "docs" / "index.html"
    ).read_bytes()
    # Every preview image the page references resolves on the staged site to
    # the same bytes it resolves to when dashboards/power_bi is served locally.
    refs = set()
    for name in ("index.html", "configurator.mjs"):
      refs.update(
          re.findall(
              r"[\"'](\.\./screenshots/[^\"']+)[\"']",
              (page / name).read_text(encoding="utf-8"),
          )
      )
    assert len(refs) == 8, sorted(refs)
    for ref in sorted(refs):
      staged = (page / ref).resolve()
      assert staged.is_relative_to(site.resolve()), ref
      local = (POWER_BI / "docs" / ref).resolve()
      assert staged.read_bytes() == local.read_bytes(), ref


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _FORBIDDEN_VENDOR_NAMES():
  """Names that must never appear in docs, code, or commits.

  Decoded at call time to avoid embedding literal vendor names in source.
  """
  encoded = bytes.fromhex(
      "636c6175646520616e7468726f706963206f707573"
      "206661626c6520736f6e6e65742067656d696e69"
      "2062617269756d20636172626f6e206172676f6e"
      "20687964726f67656e20676c6d207a756d616b69"
      "20666c617368206a6574736b69"
  )
  return encoded.decode("ascii").split()


def _load_manifest():
  path = LOOKER / "spec" / "chart_manifest.yaml"
  with open(path, "r", encoding="utf-8") as f:
    return yaml.safe_load(f)


def _load_product_contract():
  path = LOOKER / "spec" / "product_contract.yaml"
  with open(path, "r", encoding="utf-8") as f:
    return yaml.safe_load(f)


def _get_product_title(product, chart_id):
  charts = product.get("charts", {})
  if isinstance(charts, list):
    for c in charts:
      if c.get("id") == chart_id:
        return c.get("title")
  elif isinstance(charts, dict):
    info = charts.get(chart_id, {})
    return info.get("title")
  return None


def _collect_visual_names():
  """Collect all visual names from the generated report."""
  pages_dir = POWER_BI / "BQAA_Agent_Analytics.Report" / "definition" / "pages"
  names = set()
  if not pages_dir.exists():
    return names
  for page_dir in pages_dir.iterdir():
    if not page_dir.is_dir():
      continue
    visuals_dir = page_dir / "visuals"
    if not visuals_dir.exists():
      continue
    for vdir in visuals_dir.iterdir():
      if not vdir.is_dir():
        continue
      vjson = vdir / "visual.json"
      if vjson.exists():
        vobj = json.loads(vjson.read_text())
        names.add(vobj.get("name", vdir.name))
  return names
