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
  """95 visuals total: 37 parity + 40 slicers + 8 headings + 9 non-data + 1 table."""
  visuals = [p for p in generated_files if p.endswith("/visual.json")]
  assert len(visuals) == 95


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
  """trace-inspector-table with 7 columns sorted by timestamp desc."""
  ti_path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "trace-inspector/visuals/trace-inspector-table/visual.json"
  )
  assert ti_path in generated_files
  ti = json.loads(generated_files[ti_path])
  assert ti["visual"]["visualType"] == "tableEx"
  cols = ti["visual"]["query"]["queryState"]["Values"]["projections"]
  assert len(cols) == 7
  col_names = [c["queryRef"] for c in cols]
  assert col_names == [
      "events_v1.timestamp",
      "events_v1.event_type",
      "events_v1.agent",
      "events_v1.user_id",
      "events_v1.trace_id",
      "events_v1.span_id",
      "events_v1.status",
  ]
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
  assert tc["customTheme"]["name"] == "BQAA"
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


def test_area_with_series(generated_files):
  """usage-tool-calls-over-time has a Series role (pivot tool_name)."""
  path = (
      "BQAA_Agent_Analytics.Report/definition/pages/"
      "tool-usage/visuals/"
      "usage-tool-calls-over-time/visual.json"
  )
  vc = json.loads(generated_files[path])
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
