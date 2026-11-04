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

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

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
    # Also slicer/heading/table visual names.
    non_parity = non_data_ids | {
        "page-heading",
        "slicer-date",
        "slicer-agent",
        "slicer-user-id",
        "slicer-trace-id",
        "slicer-span-id",
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
    assert sql.count("COUNT(DISTINCT trace_id)") == 2
    query_path.write_text(
        sql.replace("COUNT(DISTINCT trace_id)", "COUNT(DISTINCT session_id)")
    )

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
    """README must document the SQL substitution contract (#625)."""
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

  def test_web_configurator_files_and_js_pkzip(self, tmp_path):
    """Verify dashboards/power_bi/docs/ configurator files and pure-JS PKZIP builder."""
    import shutil
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

    # Security guard: no open iframe / ?embed= parameter framing (#ugc-host-embed).
    index_text = (docs_dir / "index.html").read_text(encoding="utf-8")
    mjs_text = (docs_dir / "configurator.mjs").read_text(encoding="utf-8")
    assert "<iframe" not in index_text.lower()
    assert "pbi-live-iframe" not in index_text
    assert "pbi-live-iframe" not in mjs_text
    assert "validatePublishToWebUrl" not in mjs_text
    assert 'params.get("embed")' not in mjs_text

    import glob

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
    ), "node binary required for configurator.mjs PKZIP test"

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
      assert len(names) == 118
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
