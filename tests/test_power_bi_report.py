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

"""Tests for the Power BI PBIR report generator (Slice S2).

Per spec.md §7.5, S2 tests cover:
- 8 pages, 37 + 9 + 40 slicers + 8 headings + 1 table
- titles, ids <= 50
- NoFilter set (exactly the F4 pairs)
- sync groups
- geometry/containment
- theme wiring
- Literal escaping
- determinism (two runs produce identical output)
"""

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POWER_BI = ROOT / "dashboards" / "power_bi"
TOOLS = POWER_BI / "tools"

_SCHEMA_VC = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/visualContainer/2.13.0/schema.json"
)
_SCHEMA_PAGE = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/page/2.1.0/schema.json"
)
_SCHEMA_REPORT = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/report/3.3.0/schema.json"
)
_SCHEMA_PAGES = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/pagesMetadata/1.0.0/schema.json"
)
_SCHEMA_VERSION = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/versionMetadata/1.0.0/schema.json"
)


def _load_module(name):
  module_path = TOOLS / f"{name}.py"
  spec = importlib.util.spec_from_file_location(f"power_bi_{name}", module_path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.fixture(scope="session")
def common():
  return _load_module("pbip_common")


@pytest.fixture(scope="session")
def report_mod():
  return _load_module("pbip_report")


@pytest.fixture(scope="session")
def contracts(common):
  return common.load_contracts()


@pytest.fixture(scope="session")
def visuals_spec(common):
  return common.load_spec("report_visuals")


@pytest.fixture(scope="session")
def generated_files(contracts, visuals_spec, report_mod):
  return report_mod.build_report(contracts, visuals_spec)


# --- Page structure ---

EXPECTED_PAGES = [
    ("token-consumption", "Token Consumption", 720),
    ("agent-and-sessions", "Agent & Sessions", 1464),
    ("tool-usage", "Tool Usage", 720),
    ("llm-interactions", "LLM Interactions", 720),
    ("user-analytics", "User Analytics", 784),
    ("latency", "Latency", 1184),
    ("errors", "Errors", 720),
    ("trace-inspector", "Trace Inspector", 720),
]


def test_pages_json(generated_files):
  """pages.json has 8 pages in order with activePageName."""
  pj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/pages/pages.json"]
  )
  assert pj["$schema"] == _SCHEMA_PAGES
  assert pj["pageOrder"] == [p[0] for p in EXPECTED_PAGES]
  assert pj["activePageName"] == "token-consumption"


def test_page_count(generated_files):
  """Exactly 8 page.json files."""
  pages = [
      p for p in generated_files if p.endswith("/page.json") and "/pages/" in p
  ]
  assert len(pages) == 8


def test_page_heights(generated_files):
  """Page heights match spec §5.1."""
  for name, _, height in EXPECTED_PAGES:
    pj = json.loads(
        generated_files[
            f"BQAA_Agent_Analytics.Report/definition/pages/{name}" "/page.json"
        ]
    )
    assert (
        pj["height"] >= height
    ), f"Page {name} height {pj['height']} < spec {height}"


def test_page_display_names(generated_files):
  """Page display names match spec §5.1."""
  for name, display, _ in EXPECTED_PAGES:
    pj = json.loads(
        generated_files[
            f"BQAA_Agent_Analytics.Report/definition/pages/{name}" "/page.json"
        ]
    )
    assert pj["displayName"] == display
    assert pj["name"] == name
    assert pj["displayOption"] == "FitToWidth"
    assert pj["width"] == 1280


def test_page_annotations(generated_files):
  """Each page carries bqaa.looker_page_id annotation."""
  for name, _, _ in EXPECTED_PAGES:
    pj = json.loads(
        generated_files[
            f"BQAA_Agent_Analytics.Report/definition/pages/{name}" "/page.json"
        ]
    )
    ann = pj.get("annotations", [])
    ids = [a["name"] for a in ann]
    assert "bqaa.looker_page_id" in ids


# --- Visual counts ---


def test_total_visual_count(generated_files):
  """104 visuals total.

  37 parity + 40 slicers + 8 headings + 9 non-data + 1 table, plus the 8
  per-page Reset filters buttons and the Trace Inspector Back button.
  """
  visuals = [p for p in generated_files if p.endswith("/visual.json")]
  assert len(visuals) == 104


def test_parity_visual_count(generated_files):
  """37 parity visuals, each named by chart id."""
  parity_ids = set()
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    name = vc["name"]
    if (
        name
        not in (
            "page-heading",
            "slicer-date",
            "slicer-agent",
            "slicer-user-id",
            "slicer-trace-id",
            "slicer-span-id",
            "trace-inspector-table",
            "reset-filters",
            "drillthrough-back",
        )
        and "nondata" not in name
    ):
      parity_ids.add(name)
  assert len(parity_ids) == 37


def test_slicer_count(generated_files):
  """40 slicers: 5 per page × 8 pages."""
  slicer_count = 0
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    if vc["name"].startswith("slicer-"):
      slicer_count += 1
  assert slicer_count == 40


def test_heading_count(generated_files):
  """8 page headings, one per page."""
  heading_count = 0
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    if vc["name"] == "page-heading":
      heading_count += 1
  assert heading_count == 8


def test_non_data_count(generated_files):
  """9 non-data elements."""
  nondata_count = 0
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    if "nondata" in vc["name"]:
      nondata_count += 1
  assert nondata_count == 9


def test_trace_inspector_table(generated_files):
  """trace-inspector-table with 15 columns sorted by timestamp desc (D12)."""
  ti_path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "trace-inspector/visuals/trace-inspector-table/visual.json"
  )
  assert ti_path in generated_files
  ti = json.loads(generated_files[ti_path])
  assert ti["visual"]["visualType"] == "tableEx"
  cols = ti["visual"]["query"]["queryState"]["Values"]["projections"]
  assert len(cols) == 15
  col_names = [c["queryRef"] for c in cols]
  assert col_names == [
      "events_v1.timestamp",
      "events_v1.event_type",
      "events_v1.agent",
      "events_v1.user_id",
      "events_v1.session_id",
      "events_v1.trace_id",
      "events_v1.span_id",
      "events_v1.status",
      "events_v1.model_version",
      "events_v1.tool_completed_name",
      "events_v1.tool_error_name",
      "events_v1.usage_total_tokens",
      "events_v1.llm_total_ms",
      "events_v1.tool_completed_total_ms",
      "events_v1.error_message",
  ]
  # Every column is a plain (non-summarized) column, one row per event.
  assert all("Column" in c["field"] for c in cols)
  sort = ti["visual"]["query"]["sortDefinition"]["sort"][0]
  assert sort["direction"] == "Descending"


# --- Names and IDs ---


def test_visual_names_length(generated_files):
  """All visual names are <= 50 characters (schema constraint)."""
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    assert (
        len(vc["name"]) <= 50
    ), f"Visual name '{vc['name']}' in {path} exceeds 50 chars"


def test_visual_names_unique_per_page(generated_files):
  """Visual names are unique per page."""
  from collections import Counter

  page_names = {}
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    # Path: BQAA_.../definition/pages/<page>/visuals/<visual>/visual.json
    parts = path.split("/")
    page = parts[parts.index("pages") + 1]
    vc = json.loads(content)
    if page not in page_names:
      page_names[page] = []
    page_names[page].append(vc["name"])
  for page, names in page_names.items():
    dupes = [n for n, c in Counter(names).items() if c > 1]
    assert not dupes, f"Duplicate visual names on {page}: {dupes}"


# --- Titles ---


def test_parity_visual_titles(generated_files, visuals_spec):
  """All 37 parity visuals have the product title from the spec."""
  for vdef in visuals_spec["parity_visuals"]:
    path = (
        f"BQAA_Agent_Analytics.Report/definition/pages/"
        f"{vdef['page']}/visuals/{vdef['id']}/visual.json"
    )
    assert path in generated_files, f"Missing {path}"
    vc = json.loads(generated_files[path])
    title_objs = vc["visual"].get("visualContainerObjects", {}).get("title", [])
    assert title_objs, f"No title for {vdef['id']}"
    text_expr = title_objs[0]["properties"]["text"]["expr"]
    expected = vdef["title"].replace("'", "''")
    assert text_expr["Literal"]["Value"] == f"'{expected}'"
    if vdef["visual_type"] == "cardVisual":
      projs = (
          vc["visual"]
          .get("query", {})
          .get("queryState", {})
          .get("Data", {})
          .get("projections", [])
      )
      assert projs and projs[0].get("displayName") == vdef["title"]


# --- Listener matrix (F4) ---


def test_no_filter_set(generated_files):
  """visualInteractions NoFilter pairs are page-scoped (spec §5.4, F4).

  token-consumption gets the 2 token visuals, agent-and-sessions gets
  the 2 trace visuals, all other 6 pages get none.  4 total across all
  pages.
  """
  page_expected = {
      "token-consumption": {
          ("slicer-user-id", "usage-top-5-users-with-most-tokens-consumption"),
          ("slicer-user-id", "usage-token-usage-split-by-agent"),
      },
      "agent-and-sessions": {
          ("slicer-user-id", "usage-top-5-users-with-most-traces"),
          ("slicer-user-id", "usage-traces-split-by-agent"),
      },
  }
  total = 0
  for path, content in generated_files.items():
    if not path.endswith("/page.json"):
      continue
    parts = path.split("/")
    page = parts[parts.index("pages") + 1]
    pj = json.loads(content)
    actual = {
        (vi["source"], vi["target"])
        for vi in pj.get("visualInteractions", [])
        if vi["type"] == "NoFilter"
    }
    total += len(actual)
    if page in page_expected:
      assert actual == page_expected[page], (
          f"NoFilter set mismatch on {page}: " f"{actual ^ page_expected[page]}"
      )
    else:
      assert (
          actual == set()
      ), f"Page {page} should have no NoFilter entries, got {actual}"
  assert total == 4, f"Expected 4 total NoFilter entries, got {total}"


# --- Sync groups ---


def test_slicer_sync_groups(generated_files):
  """Each slicer has the correct sync group."""
  expected_groups = {
      "slicer-date": "bqaa-date",
      "slicer-agent": "bqaa-agent",
      "slicer-user-id": "bqaa-user-id",
      "slicer-trace-id": "bqaa-trace-id",
      "slicer-span-id": "bqaa-span-id",
  }
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    name = vc["name"]
    if name not in expected_groups:
      continue
    sync = vc["visual"].get("syncGroup", {})
    assert sync.get("groupName") == expected_groups[name]
    assert sync.get("fieldChanges") is True
    assert sync.get("filterChanges") is True


def test_sync_group_consistency(generated_files):
  """The same slicer name across pages has the same sync group."""
  from collections import defaultdict

  group_map = defaultdict(set)
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    name = vc["name"]
    sync = vc["visual"].get("syncGroup")
    if sync:
      group_map[name].add(sync["groupName"])
  for name, groups in group_map.items():
    assert (
        len(groups) == 1
    ), f"Slicer {name} has inconsistent sync groups: {groups}"


# --- Geometry / containment ---


def test_no_overlaps(generated_files):
  """No two visuals on the same page overlap."""
  from collections import defaultdict

  page_rects = defaultdict(list)
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    parts = path.split("/")
    page = parts[parts.index("pages") + 1]
    vc = json.loads(content)
    pos = vc["position"]
    rect = (
        pos["x"],
        pos["y"],
        pos["x"] + pos["width"],
        pos["y"] + pos["height"],
    )
    page_rects[page].append((vc["name"], rect))
  for page, visuals in page_rects.items():
    for i in range(len(visuals)):
      for j in range(i + 1, len(visuals)):
        n1, (x1, y1, x2, y2) = visuals[i]
        n2, (a1, b1, a2, b2) = visuals[j]
        # Check overlap (allowing touching edges)
        if x1 < a2 and a1 < x2 and y1 < b2 and b1 < y2:
          pytest.fail(
              f"Overlap on {page}: {n1}({x1},{y1},{x2},{y2})"
              f" vs {n2}({a1},{b1},{a2},{b2})"
          )


def test_containment(generated_files):
  """y + height <= page height - 24 for every visual."""
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    parts = path.split("/")
    page = parts[parts.index("pages") + 1]
    vc = json.loads(content)
    pos = vc["position"]
    page_json = json.loads(
        generated_files[
            f"BQAA_Agent_Analytics.Report/definition/pages" f"/{page}/page.json"
        ]
    )
    ph = page_json["height"]
    assert pos["y"] + pos["height"] <= ph - 24, (
        f"Visual {vc['name']} on {page}: "
        f"y+h={pos['y']+pos['height']} > {ph - 24}"
    )
    assert pos["x"] + pos["width"] <= 1280, (
        f"Visual {vc['name']} on {page}: " f"x+w={pos['x']+pos['width']} > 1280"
    )


# --- Theme ---


def test_theme_json(generated_files, visuals_spec):
  """Theme JSON has the correct structure."""
  theme_path = (
      "BQAA_Agent_Analytics.Report/StaticResources/"
      "RegisteredResources/BQAA.json"
  )
  assert theme_path in generated_files
  theme = json.loads(generated_files[theme_path])
  assert theme["name"] == "BQAA"
  assert theme["dataColors"] == [
      "#4285F4",
      "#EA4335",
      "#FBBC04",
      "#34A853",
      "#A142F4",
      "#24C1E0",
      "#FA903E",
  ]
  assert theme["foreground"] == "#202124"
  assert theme["background"] == "#FFFFFF"


def test_report_theme_collection(generated_files):
  """report.json has themeCollection with customTheme."""
  rj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/report.json"]
  )
  tc = rj["themeCollection"]
  # The name must be the RegisteredResources item name ("BQAA.json"), not the
  # theme's display name; otherwise the theme does not resolve.
  assert tc["customTheme"]["name"] == "BQAA.json"
  assert tc["customTheme"]["type"] == "RegisteredResources"
  versions = tc["customTheme"]["reportVersionAtImport"]
  assert versions["visual"] == "2.13.0"
  assert versions["page"] == "2.1.0"
  assert versions["report"] == "3.3.0"


def test_report_resource_packages(generated_files):
  """report.json has resourcePackages with BQAA.json."""
  rj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/report.json"]
  )
  rp = rj["resourcePackages"]
  assert len(rp) == 1
  assert rp[0]["name"] == "RegisteredResources"
  assert rp[0]["type"] == "RegisteredResources"
  items = rp[0]["items"]
  assert len(items) == 1
  assert items[0]["name"] == "BQAA.json"
  assert items[0]["path"] == "BQAA.json"
  assert items[0]["type"] == "CustomTheme"


# --- Report filters ---


def test_report_window_filter(generated_files):
  """Report window filter is hidden and locked."""
  rj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/report.json"]
  )
  filters = rj["filterConfig"]["filters"]
  window_filter = next(f for f in filters if f["name"] == "bqaa-report-window")
  assert window_filter["isHiddenInViewMode"] is True
  assert window_filter["isLockedInViewMode"] is True
  assert window_filter["type"] == "Categorical"


def test_report_filter_count(generated_files):
  """3 report-level filters: window, session-id, model-version."""
  rj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/report.json"]
  )
  filters = rj["filterConfig"]["filters"]
  assert len(filters) == 3
  names = [f["name"] for f in filters]
  assert "bqaa-report-window" in names
  assert "bqaa-session-id" in names
  assert "bqaa-model-version" in names


# --- Literal escaping ---


def test_literal_escaping(generated_files):
  """Titles with apostrophes use doubled single quotes in Literal values."""
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    title_objs = (
        vc.get("visual", {}).get("visualContainerObjects", {}).get("title", [])
    )
    for to in title_objs:
      text = to.get("properties", {}).get("text", {})
      expr = text.get("expr", {})
      literal = expr.get("Literal", {})
      value = literal.get("Value", "")
      if value.startswith("'") and value.endswith("'"):
        inner = value[1:-1]
        # No unescaped single quotes (should be doubled)
        assert "''" not in inner.replace(
            "''", ""
        ), f"Unescaped quote in {path}: {value}"


# --- Determinism ---


def test_determinism(contracts, visuals_spec, report_mod):
  """Two consecutive build_report calls produce identical output."""
  files1 = report_mod.build_report(contracts, visuals_spec)
  files2 = report_mod.build_report(contracts, visuals_spec)
  assert set(files1.keys()) == set(files2.keys())
  for key in files1:
    assert files1[key] == files2[key], f"Determinism failed for {key}"


# --- Annotations ---


def test_parity_visual_annotations(generated_files, visuals_spec):
  """Each parity visual has bqaa.chart_id and bqaa.oracle_query annotations."""
  for vdef in visuals_spec["parity_visuals"]:
    path = (
        f"BQAA_Agent_Analytics.Report/definition/pages/"
        f"{vdef['page']}/visuals/{vdef['id']}/visual.json"
    )
    vc = json.loads(generated_files[path])
    anns = vc.get("annotations", [])
    ann_names = [a["name"] for a in anns]
    assert "bqaa.chart_id" in ann_names
    assert "bqaa.oracle_query" in ann_names
    chart_id = next(a["value"] for a in anns if a["name"] == "bqaa.chart_id")
    assert chart_id == vdef["id"]


# --- Schema URLs ---


def test_version_json_schema(generated_files):
  """version.json has the correct schema URL and version."""
  vj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/version.json"]
  )
  assert vj["$schema"] == _SCHEMA_VERSION
  assert vj["version"] == "2.0.0"


def test_report_json_schema(generated_files):
  """report.json has the correct schema URL."""
  rj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/report.json"]
  )
  assert rj["$schema"] == _SCHEMA_REPORT


def test_visual_schema_url(generated_files):
  """All visual.json files have the visualContainer 2.13.0 schema URL."""
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    assert (
        vc["$schema"] == _SCHEMA_VC
    ), f"Wrong schema in {path}: {vc['$schema']}"


def test_page_schema_url(generated_files):
  """All page.json files have the page 2.1.0 schema URL."""
  for path, content in generated_files.items():
    if not path.endswith("/page.json"):
      continue
    pj = json.loads(content)
    assert pj["$schema"] == _SCHEMA_PAGE


# --- Visual type mapping ---


def test_card_visual_type(generated_files):
  """Card visuals use cardVisual with Data role."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "token-consumption/visuals/usage-total-tokens/visual.json"
  )
  vc = json.loads(generated_files[path])
  assert vc["visual"]["visualType"] == "cardVisual"
  assert "Data" in vc["visual"]["query"]["queryState"]


def test_bar_visual_type(generated_files):
  """Bar visuals use clusteredBarChart with Category and Y roles."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "token-consumption/visuals/"
      "usage-token-usage-split-by-agent/visual.json"
  )
  vc = json.loads(generated_files[path])
  assert vc["visual"]["visualType"] == "clusteredBarChart"
  qs = vc["visual"]["query"]["queryState"]
  assert "Category" in qs
  assert "Y" in qs


def test_area_visual_type(generated_files):
  """Area visuals use areaChart with Category and Y roles."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "token-consumption/visuals/"
      "usage-total-tokens-consumption-over-the-time/visual.json"
  )
  vc = json.loads(generated_files[path])
  assert vc["visual"]["visualType"] == "areaChart"
  qs = vc["visual"]["query"]["queryState"]
  assert "Category" in qs
  assert "Y" in qs


def test_tool_calls_line_chart_with_series(generated_files):
  """usage-tool-calls-over-time is a lineChart with a Series role (D15)."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "tool-usage/visuals/"
      "usage-tool-calls-over-time/visual.json"
  )
  vc = json.loads(generated_files[path])
  assert vc["visual"]["visualType"] == "lineChart"
  qs = vc["visual"]["query"]["queryState"]
  assert "Series" in qs


def test_slicer_visual_type(generated_files):
  """Slicers use slicer visual type with Values role."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "token-consumption/visuals/slicer-date/visual.json"
  )
  vc = json.loads(generated_files[path])
  assert vc["visual"]["visualType"] == "slicer"
  assert "Values" in vc["visual"]["query"]["queryState"]


def test_textbox_visual_type(generated_files):
  """Non-data textboxes use textbox visual type."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "agent-and-sessions/visuals/usage-nondata-1/visual.json"
  )
  vc = json.loads(generated_files[path])
  assert vc["visual"]["visualType"] == "textbox"
  paragraphs = vc["visual"]["objects"]["general"][0]["properties"]["paragraphs"]
  assert len(paragraphs) > 0


def test_button_visual_type(generated_files):
  """Non-data buttons use actionButton visual type with PageNavigation."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "user-analytics/visuals/usage-nondata-3/visual.json"
  )
  vc = json.loads(generated_files[path])
  assert vc["visual"]["visualType"] == "actionButton"
  link = vc["visual"]["visualContainerObjects"]["visualLink"][0]
  assert link["properties"]["type"]["expr"]["Literal"]["Value"] == (
      "'PageNavigation'"
  )
  assert (
      link["properties"]["navigationSection"]["expr"]["Literal"]["Value"]
      == "'latency'"
  )


# --- Period-over-period KPI scorecards and the Top-5 LLM chart ---

_KPI_CARDS = {
    "usage-total-tokens": ("KPI Total Tokens", "KPI Total Tokens Change %"),
    "usage-total-traces": ("KPI Total Traces", "KPI Total Traces Change %"),
    "usage-total-sessions": (
        "KPI Total Sessions",
        "KPI Total Sessions Change %",
    ),
    "usage-total-calls": (
        "KPI Total LLM Calls",
        "KPI Total LLM Calls Change %",
    ),
    "usage-total-users": ("KPI Total Users", "KPI Total Users Change %"),
    "performance-total-errors": ("KPI Tool Errors", "KPI Tool Errors Change %"),
}


def _visual_path(vdef):
  return (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      f"{vdef['page']}/visuals/{vdef['id']}/visual.json"
  )


def _spec_visual(visuals_spec, chart_id):
  return next(v for v in visuals_spec["parity_visuals"] if v["id"] == chart_id)


def _card_projections(files, vdef):
  vc = json.loads(files[_visual_path(vdef)])
  return vc["visual"]["query"]["queryState"]["Data"]["projections"]


@pytest.mark.parametrize("chart_id", sorted(_KPI_CARDS))
def test_kpi_scorecards_bind_the_value_and_the_change_percent(
    generated_files, visuals_spec, chart_id
):
  """Each scorecard shows the headline value and its change vs the prior period."""
  current, change = _KPI_CARDS[chart_id]
  vdef = _spec_visual(visuals_spec, chart_id)
  assert vdef["visual_type"] == "cardVisual"
  assert vdef["measures"] == [current, change]

  projs = _card_projections(generated_files, vdef)
  assert [p["nativeQueryRef"] for p in projs] == [current, change]
  assert [p["queryRef"] for p in projs] == [
      f"events_v1.{current}",
      f"events_v1.{change}",
  ]
  assert projs[0]["displayName"] == vdef["title"]
  assert projs[1]["displayName"] == "vs previous period"
  for proj in projs:
    measure = proj["field"]["Measure"]
    assert measure["Expression"]["SourceRef"]["Entity"] == "events_v1"
    assert measure["Property"] == proj["nativeQueryRef"]


def test_only_the_kpi_scorecards_carry_a_change_callout(
    generated_files, visuals_spec
):
  with_callout = set()
  for vdef in visuals_spec["parity_visuals"]:
    if vdef["visual_type"] != "cardVisual":
      continue
    projs = _card_projections(generated_files, vdef)
    assert len(projs) == len(vdef["measures"]), vdef["id"]
    if len(projs) == 2:
      with_callout.add(vdef["id"])
    else:
      assert len(projs) == 1, vdef["id"]
  assert with_callout == set(_KPI_CARDS)


def test_card_projections_follow_the_spec_measures(
    report_mod, contracts, visuals_spec
):
  """Dropping the change measure from the spec drops the callout (can fail)."""
  mutated = copy.deepcopy(visuals_spec)
  vdef = _spec_visual(mutated, "usage-total-tokens")
  vdef["measures"] = ["KPI Total Tokens"]
  files = report_mod.build_report(contracts, mutated)
  projs = _card_projections(files, vdef)
  assert [p["nativeQueryRef"] for p in projs] == ["KPI Total Tokens"]
  assert projs[0]["displayName"] == vdef["title"]


def test_top_5_agents_by_llm_calls_binds_the_top_five_measure(
    generated_files, visuals_spec
):
  vdef = _spec_visual(visuals_spec, "usage-top-5-agents-by-llm-calls")
  assert vdef["visual_type"] == "clusteredBarChart"
  assert vdef["top_n"] == 5
  assert vdef["measures"] == ["Top 5 Agents by LLM Calls"]

  vc = json.loads(generated_files[_visual_path(vdef)])
  state = vc["visual"]["query"]["queryState"]
  assert [p["nativeQueryRef"] for p in state["Y"]["projections"]] == [
      "Top 5 Agents by LLM Calls"
  ]
  assert [p["nativeQueryRef"] for p in state["Category"]["projections"]] == [
      "agent"
  ]


# --- UX enhancements (README D12, D15-D17) ---

_PAGE_NAMES = [p[0] for p in EXPECTED_PAGES]
_REPORT_PAGES = "BQAA_Agent_Analytics.Report/definition/pages"
_LINE_CHARTS = {
    "usage-tool-calls-over-time",
    "performance-tool-latency-trend",
    "performance-llm-latency-trend",
}
_DRILLTHROUGH_FIELDS = [
    ("Date", "Date"),
    ("events_v1", "agent"),
    ("events_v1", "user_id"),
    ("events_v1", "tool_completed_name"),
    ("events_v1", "tool_error_name"),
]


def _lit(value):
  return {"expr": {"Literal": {"Value": value}}}


def _solid(color):
  return {"solid": {"color": _lit(f"'{color}'")}}


def _visual(files, page, name):
  return json.loads(files[f"{_REPORT_PAGES}/{page}/visuals/{name}/visual.json"])


def _page(files, page):
  return json.loads(files[f"{_REPORT_PAGES}/{page}/page.json"])


def _props(objects, name, selector=None):
  """Return the properties of the `name` object entry with this selector."""
  for entry in objects.get(name, []):
    if entry.get("selector") == selector:
      return entry["properties"]
  raise AssertionError(f"no {name!r} entry with selector {selector!r}")


def _parity(visuals_spec, visual_type):
  return [
      v
      for v in visuals_spec["parity_visuals"]
      if v["visual_type"] == visual_type
  ]


def test_custom_theme_name_matches_its_registered_resource(generated_files):
  """A1: the customTheme must name a registered CustomTheme item."""
  rj = json.loads(
      generated_files["BQAA_Agent_Analytics.Report/definition/report.json"]
  )
  items = {
      item["name"]
      for pkg in rj["resourcePackages"]
      if pkg["type"] == "RegisteredResources"
      for item in pkg["items"]
      if item["type"] == "CustomTheme"
  }
  assert rj["themeCollection"]["customTheme"]["name"] in items


@pytest.mark.parametrize(
    "page,name",
    [
        ("user-analytics", "usage-nondata-3"),
        ("latency", "performance-nondata-3"),
    ],
)
def test_navigation_buttons_show_their_fill_colour(generated_files, page, name):
  """A2: a button fill is off by default, and a colour needs solid.color."""
  objects = _visual(generated_files, page, name)["visual"]["objects"]
  fill = _props(objects, "fill", {"id": "default"})
  assert fill["show"] == _lit("true")
  assert fill["fillColor"] == _solid("#E52592")
  assert fill["transparency"] == _lit("0D")
  text = _props(objects, "text", {"id": "default"})
  assert text["fontColor"] == _solid("#FFFFFF")
  assert text["bold"] == _lit("true")
  assert _props(objects, "outline", {"id": "default"})["show"] == _lit("false")


def test_latency_cards_show_exact_millisecond_values(
    generated_files, visuals_spec
):
  """A3: latency cards show 4,512 rather than the auto-abbreviated 4.5K."""
  latency_cards = [
      v for v in _parity(visuals_spec, "cardVisual") if v["page"] == "latency"
  ]
  assert len(latency_cards) == 10
  for vdef in latency_cards:
    objects = json.loads(generated_files[_visual_path(vdef)])["visual"][
        "objects"
    ]
    value = _props(objects, "value", {"id": "default"})
    assert value["labelDisplayUnits"] == _lit("1D"), vdef["id"]
    assert value["labelPrecision"] == _lit("0L"), vdef["id"]
  # KPI scorecards keep Power BI's automatic display units (39M, 14K).
  for chart_id in _KPI_CARDS:
    vdef = _spec_visual(visuals_spec, chart_id)
    objects = json.loads(generated_files[_visual_path(vdef)])["visual"][
        "objects"
    ]
    for entry in objects.get("value", []):
      assert "labelDisplayUnits" not in entry["properties"], chart_id


def test_chart_titles_are_dark_and_bold(generated_files, visuals_spec):
  """B1: visible chart titles use the theme foreground, 12pt, bold."""
  for vdef in visuals_spec["parity_visuals"]:
    if vdef["visual_type"] == "cardVisual":
      continue  # Card titles are hidden; the callout label is the title.
    vc = json.loads(generated_files[_visual_path(vdef)])
    title = vc["visual"]["visualContainerObjects"]["title"][0]["properties"]
    assert title["show"] == _lit("true"), vdef["id"]
    assert title["fontColor"] == _solid("#202124"), vdef["id"]
    assert title["fontSize"] == _lit("12D"), vdef["id"]
    assert title["bold"] == _lit("true"), vdef["id"]


def test_bar_charts_show_data_labels(generated_files, visuals_spec):
  """B2: every bar shows its value without hovering."""
  bars = _parity(visuals_spec, "clusteredBarChart")
  assert len(bars) == 12
  for vdef in bars:
    objects = json.loads(generated_files[_visual_path(vdef)])["visual"][
        "objects"
    ]
    assert _props(objects, "labels")["show"] == _lit("true"), vdef["id"]


def test_tool_calls_and_latency_trends_are_line_charts(
    generated_files, visuals_spec
):
  """B3/B4: overlapping series and non-zero baselines read better as lines."""
  assert {v["id"] for v in _parity(visuals_spec, "lineChart")} == _LINE_CHARTS
  for vdef in _parity(visuals_spec, "lineChart"):
    vc = json.loads(generated_files[_visual_path(vdef)])
    assert vc["visual"]["visualType"] == "lineChart"
    qs = vc["visual"]["query"]["queryState"]
    assert [p["queryRef"] for p in qs["Category"]["projections"]] == [
        "Date.Date"
    ]
    assert [p["nativeQueryRef"] for p in qs["Y"]["projections"]] == vdef[
        "measures"
    ]


def test_area_charts_start_their_value_axis_at_zero(
    generated_files, visuals_spec
):
  """B4: an area's fill encodes magnitude, so its axis must start at zero."""
  areas = _parity(visuals_spec, "areaChart")
  assert len(areas) == 6
  for vdef in areas:
    objects = json.loads(generated_files[_visual_path(vdef)])["visual"][
        "objects"
    ]
    assert _props(objects, "valueAxis")["start"] == _lit("0D"), vdef["id"]


@pytest.mark.parametrize("chart_id", sorted(_KPI_CARDS))
def test_kpi_cards_stack_and_explain_a_blank_change(
    generated_files, visuals_spec, chart_id
):
  """B5/C1: callouts stack vertically; a blank Change % reads 'No prior data'."""
  _, change = _KPI_CARDS[chart_id]
  vdef = _spec_visual(visuals_spec, chart_id)
  objects = json.loads(generated_files[_visual_path(vdef)])["visual"]["objects"]
  layout = _props(objects, "layout", {"id": "default"})
  assert layout["orientation"] == _lit("1D")
  blank = _props(objects, "value", {"metadata": f"events_v1.{change}"})
  assert blank["showBlankAs"] == _lit("'No prior data'")
  # Only the change callout is relabelled, never the headline value.
  for entry in objects["value"]:
    if "showBlankAs" in entry["properties"]:
      assert entry["selector"] == {"metadata": f"events_v1.{change}"}


def test_dropdown_slicers_have_a_search_box(generated_files):
  """C2: Agent, User ID, Trace ID and Span ID slicers are searchable."""
  searchable = 0
  for path, content in generated_files.items():
    if not path.endswith("/visual.json"):
      continue
    vc = json.loads(content)
    if vc["visual"]["visualType"] != "slicer":
      continue
    objects = vc["visual"]["objects"]
    if objects["data"][0]["properties"]["mode"] == _lit("'Dropdown'"):
      assert _props(objects, "general")["selfFilterEnabled"] == _lit(
          "true"
      ), path
      searchable += 1
    else:
      assert "general" not in objects, path
  assert searchable == 32  # 4 dropdown slicers x 8 pages


def test_trace_inspector_is_a_drillthrough_target(generated_files):
  """C3: right-click a bar or a day -> Drill through -> Trace Inspector."""
  pj = _page(generated_files, "trace-inspector")
  binding = pj["pageBinding"]
  assert binding["type"] == "Drillthrough"
  filters = {f["name"]: f for f in pj["filterConfig"]["filters"]}
  fields = []
  for param in binding["parameters"]:
    flt = filters[param["boundFilter"]]
    assert flt["howCreated"] == "Drillthrough"
    assert flt["type"] == "Categorical"
    assert flt["field"] == param["fieldExpr"]
    column = flt["field"]["Column"]
    fields.append(
        (column["Expression"]["SourceRef"]["Entity"], column["Property"])
    )
  assert fields == _DRILLTHROUGH_FIELDS
  assert len(filters) == len(_DRILLTHROUGH_FIELDS)
  names = [
      binding["name"],
      *[p["name"] for p in binding["parameters"]],
      *filters,
  ]
  assert len(names) == len(set(names))


def test_only_the_trace_inspector_is_a_drillthrough_target(generated_files):
  for page in _PAGE_NAMES:
    pj = _page(generated_files, page)
    assert ("pageBinding" in pj) == (page == "trace-inspector"), page
    assert ("filterConfig" in pj) == (page == "trace-inspector"), page


def test_trace_inspector_has_a_back_button(generated_files):
  """C3: drill-through targets need a way back to the source page."""
  vc = _visual(generated_files, "trace-inspector", "drillthrough-back")
  assert vc["visual"]["visualType"] == "actionButton"
  link = vc["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
  assert link["show"] == _lit("true")
  assert link["type"] == _lit("'Back'")
  for page in _PAGE_NAMES:
    if page != "trace-inspector":
      assert (
          f"{_REPORT_PAGES}/{page}/visuals/drillthrough-back/visual.json"
          not in generated_files
      )


def test_every_page_has_a_reset_filters_button(generated_files):
  """C5: one click clears every (synced) slicer."""
  for page in _PAGE_NAMES:
    vc = _visual(generated_files, page, "reset-filters")
    assert vc["visual"]["visualType"] == "actionButton"
    link = vc["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
    assert link["show"] == _lit("true")
    assert link["type"] == _lit("'ClearAllSlicers'")
    text = _props(vc["visual"]["objects"], "text", {"id": "default"})
    assert text["text"] == _lit("'Reset filters'")
    # The button sits in the header band, above the slicer row.
    assert vc["position"]["y"] + vc["position"]["height"] <= 56
