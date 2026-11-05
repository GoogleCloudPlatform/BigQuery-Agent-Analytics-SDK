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

"""Generate the PBIR report definition for the BQAA Power BI dashboard.

Produces all files under ``BQAA_Agent_Analytics.Report/definition/`` and
the theme ``StaticResources/RegisteredResources/BQAA.json`` from the Looker
Studio contracts and the ``report_visuals.yaml`` spec.
"""

import copy
import json
import math
import os
import os.path
import re

import yaml

# The pbip_common module is loaded via importlib by the test suite; for
# standalone use we fall back to a direct import.
try:
  from dashboards.power_bi.tools import pbip_common  # noqa: F401
except ImportError:
  pass

# When loaded via importlib.util.spec_from_file_location, pbip_common
# is not on sys.modules under its dotted name.  The test suite passes
# it explicitly.  We use a late-binding pattern: functions access
# pbip_common through a module reference that can be swapped.

# Schema URL constants (spec §5.8)
_VC_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/visualContainer/2.13.0/schema.json"
)
_VC_SCHEMA_V20 = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/visualContainer/2.0.0/schema.json"
)
_PAGE_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/page/2.1.0/schema.json"
)
_PAGES_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/pagesMetadata/1.0.0/schema.json"
)
_REPORT_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/report/3.3.0/schema.json"
)
_VERSION_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/versionMetadata/1.0.0/schema.json"
)

_REPORT_DIR = "BQAA_Agent_Analytics.Report"
_DEF_DIR = os.path.join(_REPORT_DIR, "definition")
_PAGES_DIR = os.path.join(_DEF_DIR, "pages")
_THEME_DIR = os.path.join(_REPORT_DIR, "StaticResources", "RegisteredResources")


def _slug(page_name):
  """Return the folder slug for a page (same as page name)."""
  return page_name


def _col_expr(entity, prop):
  """Build a Column query expression."""
  return {
      "Column": {
          "Expression": {"SourceRef": {"Entity": entity}},
          "Property": prop,
      }
  }


def _measure_expr(entity, prop):
  """Build a Measure query expression."""
  return {
      "Measure": {
          "Expression": {"SourceRef": {"Entity": entity}},
          "Property": prop,
      }
  }


def _projection(entity, prop, kind="Column", display_name=None):
  """Build a RoleProjection for a column or measure."""
  if kind == "Measure":
    field = _measure_expr(entity, prop)
    qref = f"{entity}.{prop}"
  else:
    field = _col_expr(entity, prop)
    qref = f"{entity}.{prop}"
  proj = {
      "field": field,
      "queryRef": qref,
      "nativeQueryRef": prop,
  }
  if display_name is not None:
    proj["displayName"] = display_name
  return proj


def _title_objects(title_text, show=True):
  """Build the title formatting objects for a visual."""
  escaped = title_text.replace("'", "''")
  return {
      "title": [
          {
              "properties": {
                  "show": {
                      "expr": {
                          "Literal": {"Value": "true" if show else "false"}
                      }
                  },
                  "text": {"expr": {"Literal": {"Value": f"'{escaped}'"}}},
              }
          }
      ]
  }


def _annotations(chart_id, is_parity=True):
  """Build visual annotations for chart id and oracle query."""
  anns = [{"name": "bqaa.chart_id", "value": chart_id}]
  if is_parity:
    anns.append(
        {
            "name": "bqaa.oracle_query",
            "value": f"oracle/queries/{chart_id}.sql",
        }
    )
  return anns


def _build_card_visual(vdef, model_spec):
  """Build a cardVisual visual.json dict.

  The first measure is the headline value and carries the chart title as its
  display name; any further measure (the period-over-period change) is labelled
  "vs previous period".
  """
  measures = vdef.get("measures", [])
  title = vdef.get("title")
  # Use events_v1 as the entity for all measures
  entity = "events_v1"
  data_role = {
      "projections": [
          _projection(
              entity,
              m,
              kind="Measure",
              display_name=title if i == 0 else "vs previous period",
          )
          for i, m in enumerate(measures)
      ]
  }
  visual = {
      "visualType": "cardVisual",
      "query": {"queryState": {"Data": data_role}},
      "visualContainerObjects": _title_objects(vdef["title"], show=False),
  }
  return visual


def _build_bar_visual(vdef, model_spec):
  """Build a clusteredBarChart visual.json dict."""
  entity = "events_v1"
  cat_field = vdef.get("category", "")
  parts = cat_field.split(".")
  cat_entity = parts[0] if len(parts) > 1 else entity
  cat_prop = parts[-1]

  cat_proj = _projection(cat_entity, cat_prop, kind="Column")
  measure_proj = _projection(entity, vdef["measures"][0], kind="Measure")

  visual = {
      "visualType": "clusteredBarChart",
      "query": {
          "queryState": {
              "Category": {"projections": [cat_proj]},
              "Y": {"projections": [measure_proj]},
          },
          "sortDefinition": {
              "sort": [
                  {
                      "field": _measure_expr(entity, vdef["measures"][0]),
                      "direction": "Descending",
                  }
              ],
              "isDefaultSort": False,
          },
      },
      "visualContainerObjects": _title_objects(vdef["title"]),
  }
  return visual


def _build_area_visual(vdef, model_spec):
  """Build an areaChart visual.json dict."""
  entity = "events_v1"
  axis_field = vdef.get("axis", "Date.Date")
  parts = axis_field.split(".")
  axis_entity = parts[0]
  axis_prop = parts[1]

  axis_proj = _projection(axis_entity, axis_prop, kind="Column")
  measure_proj = _projection(entity, vdef["measures"][0], kind="Measure")

  query_state = {
      "Category": {"projections": [axis_proj]},
      "Y": {"projections": [measure_proj]},
  }

  # Series (pivot): one overlapping area per series value, not a stack
  series_field = vdef.get("series")
  if series_field:
    sparts = series_field.split(".")
    s_entity = sparts[0]
    s_prop = sparts[1]
    series_proj = _projection(s_entity, s_prop, kind="Column")
    query_state["Series"] = {"projections": [series_proj]}

  visual = {
      "visualType": "areaChart",
      "query": {"queryState": query_state},
      "visualContainerObjects": _title_objects(vdef["title"]),
  }
  return visual


def _build_slicer_visual(slicer_def):
  """Build a slicer visual.json dict."""
  field = slicer_def["field"]
  parts = field.split(".")
  entity = parts[0]
  prop = parts[1]

  proj = _projection(entity, prop, kind="Column")
  mode = slicer_def.get("mode", "Dropdown")

  objects = {
      "data": [
          {
              "properties": {
                  "mode": {"expr": {"Literal": {"Value": f"'{mode}'"}}}
              }
          }
      ],
      "header": [
          {
              "properties": {
                  "show": {"expr": {"Literal": {"Value": "true"}}},
                  "text": {
                      "expr": {
                          "Literal": {
                              "Value": f"'{slicer_def['display_name']}'"
                          }
                      }
                  },
              }
          }
      ],
  }

  visual = {
      "visualType": "slicer",
      "query": {"queryState": {"Values": {"projections": [proj]}}},
      "objects": objects,
      "syncGroup": {
          "groupName": slicer_def["sync_group"],
          "fieldChanges": True,
          "filterChanges": True,
      },
  }
  return visual


def _build_textbox_visual(ndef):
  """Build a textbox visual.json dict."""
  content = ndef.get("content", {})
  ctype = content.get("type", "p")
  text = content.get("text", "")
  align = content.get("align", "center")
  escaped = text.replace("'", "''")

  if ctype == "spacer":
    paragraphs = [{"textRuns": [{"value": ""}]}]
  elif ctype in ("h1", "h2"):
    font_size = "24pt" if ctype == "h1" else "18pt"
    font_weight = "bold"
    runs = [
        {
            "value": text,
            "textStyle": {
                "fontSize": font_size,
                "fontWeight": font_weight,
            },
        }
    ]
    para = {"textRuns": runs}
    if align:
      para["horizontalAlignment"] = align.capitalize()
    paragraphs = [para]
    para_text = content.get("paragraph")
    if para_text:
      paragraphs.append(
          {
              "textRuns": [{"value": para_text}],
              "horizontalAlignment": align.capitalize() if align else None,
          }
      )
      # Remove None values
      paragraphs[-1] = {
          k: v for k, v in paragraphs[-1].items() if v is not None
      }
  else:
    paragraphs = [{"textRuns": [{"value": text}]}]

  return {
      "visualType": "textbox",
      "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]},
  }


def _build_button_visual(ndef):
  """Build an actionButton visual.json dict."""
  text = ndef.get("button_text", "")
  escaped = text.replace("'", "''")
  target = ndef.get("target_page", "")

  fill_color = ndef.get("fill_color", "#E52592")

  return {
      "visualType": "actionButton",
      "objects": {
          "text": [
              {
                  "properties": {
                      "show": {"expr": {"Literal": {"Value": "true"}}}
                  }
              },
              {
                  "properties": {
                      "text": {"expr": {"Literal": {"Value": f"'{escaped}'"}}}
                  },
                  "selector": {"id": "default"},
              },
          ],
          "fill": [
              {
                  "properties": {
                      "fillColor": {
                          "expr": {"Literal": {"Value": f"'{fill_color}'"}}
                      }
                  },
                  "selector": {"id": "default"},
              }
          ],
      },
      "visualContainerObjects": {
          "visualLink": [
              {
                  "properties": {
                      "show": {"expr": {"Literal": {"Value": "true"}}},
                      "type": {
                          "expr": {"Literal": {"Value": "'PageNavigation'"}}
                      },
                      "navigationSection": {
                          "expr": {"Literal": {"Value": f"'{target}'"}}
                      },
                  }
              }
          ]
      },
  }


def _build_table_visual(ti_def):
  """Build a tableEx visual for the Trace Inspector."""
  cols = ti_def["columns"]
  projections = []
  for col_path in cols:
    parts = col_path.split(".")
    entity = parts[0]
    prop = parts[1]
    projections.append(_projection(entity, prop, kind="Column"))

  sort_def = None
  if ti_def.get("sort"):
    s = ti_def["sort"]
    sparts = s["field"].split(".")
    s_entity = sparts[0]
    s_prop = sparts[1]
    sort_def = {
        "sort": [
            {
                "field": _col_expr(s_entity, s_prop),
                "direction": s["direction"],
            }
        ],
        "isDefaultSort": False,
    }

  visual = {
      "visualType": "tableEx",
      "query": {"queryState": {"Values": {"projections": projections}}},
  }
  if sort_def:
    visual["query"]["sortDefinition"] = sort_def
  return visual


def _build_visual_container(name, rect, z, tab_order, visual, annotations=None):
  """Wrap a visual definition in a visual container."""
  vc = {
      "$schema": _VC_SCHEMA,
      "name": name,
      "position": {
          "x": rect["x"],
          "y": rect["y"],
          "z": z,
          "height": rect["height"],
          "width": rect["width"],
          "tabOrder": tab_order,
      },
      "visual": visual,
  }
  if annotations:
    vc["annotations"] = annotations
  return vc


def _build_page_heading(page_display_name):
  """Build the page-heading textbox visual."""
  escaped = page_display_name.replace("'", "''")
  visual = {
      "visualType": "textbox",
      "objects": {
          "general": [
              {
                  "properties": {
                      "paragraphs": [
                          {
                              "textRuns": [
                                  {
                                      "value": page_display_name,
                                      "textStyle": {
                                          "fontSize": "18pt",
                                          "fontWeight": "bold",
                                      },
                                  }
                              ]
                          }
                      ]
                  }
              }
          ]
      },
  }
  return visual


def _build_report_filter(filter_def):
  """Build a report-level filter from the spec."""
  field = filter_def["field"]
  parts = field.split(".")
  entity = parts[0]
  prop = parts[1]

  result = {
      "name": filter_def["name"],
      "field": _col_expr(entity, prop),
      "type": filter_def["type"],
      "howCreated": "User",
  }

  if filter_def.get("hidden"):
    result["isHiddenInViewMode"] = True
  if filter_def.get("locked"):
    result["isLockedInViewMode"] = True

  if filter_def.get("values"):
    result["filter"] = {
        "Version": 2,
        "From": [{"Name": "a", "Entity": entity, "Type": 0}],
        "Where": [
            {
                "Condition": {
                    "In": {
                        "Expressions": [_col_expr_from_source("a", prop)],
                        "Values": [
                            [{"Literal": {"Value": f"'{v}'"}}]
                            for v in filter_def["values"]
                        ],
                    }
                }
            }
        ],
    }

  return result


def _col_expr_from_source(source, prop):
  """Build a Column expression referencing a From-source alias."""
  return {
      "Column": {
          "Expression": {"SourceRef": {"Source": source}},
          "Property": prop,
      }
  }


def _theme_json(spec):
  """Build the theme JSON dict."""
  t = spec["theme"]
  theme = {
      "name": t["name"],
      "dataColors": list(t["data_colors"]),
      "foreground": t["foreground"],
      "foregroundNeutralSecondary": t["foreground_neutral_secondary"],
      "background": t["background"],
      "backgroundLight": t["background_light"],
      "tableAccent": t["table_accent"],
      "bad": t["bad"],
  }
  return theme


def _theme_collection():
  """Build the themeCollection for report.json (spec §5.7)."""
  return {
      "customTheme": {
          "name": "BQAA",
          "reportVersionAtImport": {
              "visual": "2.13.0",
              "page": "2.1.0",
              "report": "3.3.0",
          },
          "type": "RegisteredResources",
      }
  }


def _resource_packages():
  """Build the resourcePackages for report.json (spec §5.7)."""
  return [
      {
          "name": "RegisteredResources",
          "type": "RegisteredResources",
          "items": [
              {
                  "name": "BQAA.json",
                  "path": "BQAA.json",
                  "type": "CustomTheme",
              }
          ],
      }
  ]


def _listener_matrix(visuals_spec, page_name=None):
  """Compute the visualInteractions NoFilter set (spec §5.4, F4).

  F4: User ID listener = 33/37. The 4 exceptions are:
  usage-top-5-users-with-most-tokens-consumption,
  usage-token-usage-split-by-agent,
  usage-top-5-users-with-most-traces,
  usage-traces-split-by-agent.

  These 4 charts do NOT listen to slicer-user-id, so each gets a
  NoFilter entry from slicer-user-id.  When *page_name* is given,
  only entries whose target visual is on that page are returned.
  """
  no_filter = []
  for vdef in visuals_spec["parity_visuals"]:
    if not vdef.get("listens_user_id", True):
      if page_name is not None and vdef["page"] != page_name:
        continue
      no_filter.append(
          {
              "source": "slicer-user-id",
              "target": vdef["id"],
              "type": "NoFilter",
          }
      )
  return no_filter


def _collect_visuals_for_page(page_name, visuals_spec, model_spec, common):
  """Collect all visual containers for a page, sorted by z/tabOrder."""
  visuals = []

  # Page heading
  heading_rect = common.HEADER_RECTS["page-heading"]
  heading_visual = _build_page_heading(
      _page_display_name(page_name, visuals_spec)
  )
  visuals.append(
      _build_visual_container(
          "page-heading",
          heading_rect,
          1000,
          1000,
          heading_visual,
      )
  )

  # Slicers (5 per page)
  for i, sdef in enumerate(visuals_spec["slicers"]):
    sname = sdef["name"]
    srect = common.HEADER_RECTS[sname]
    svisual = _build_slicer_visual(sdef)
    visuals.append(
        _build_visual_container(sname, srect, 2000 + i, 2000 + i, svisual)
    )

  # Body visuals: parity charts, non-data, trace inspector
  body_visuals = []
  idx = 0

  # Parity visuals on this page
  for vdef in visuals_spec["parity_visuals"]:
    if vdef["page"] != page_name:
      continue
    rect = common.canvas_rect(vdef["geometry"])
    vtype = vdef["visual_type"]
    if vtype == "cardVisual":
      visual = _build_card_visual(vdef, model_spec)
    elif vtype == "clusteredBarChart":
      visual = _build_bar_visual(vdef, model_spec)
    elif vtype == "areaChart":
      visual = _build_area_visual(vdef, model_spec)
    else:
      continue
    annotations = _annotations(vdef["id"])
    vc = _build_visual_container(
        vdef["id"],
        rect,
        3000 + idx,
        3000 + idx,
        visual,
        annotations=annotations,
    )
    body_visuals.append((vdef["geometry"], vc))
    idx += 1

  # Non-data elements on this page
  for ndef in visuals_spec.get("non_data_elements", []):
    if ndef["page"] != page_name:
      continue
    rect = common.canvas_rect(ndef["geometry"])
    vt = ndef["visual_type"]
    if vt == "textbox":
      visual = _build_textbox_visual(ndef)
    elif vt == "actionButton":
      visual = _build_button_visual(ndef)
    else:
      continue
    vc = _build_visual_container(
        ndef["id"],
        rect,
        3000 + idx,
        3000 + idx,
        visual,
        annotations=_annotations(ndef["id"], is_parity=False),
    )
    body_visuals.append((ndef["geometry"], vc))
    idx += 1

  # Trace inspector table
  ti = visuals_spec.get("trace_inspector")
  if ti and ti["page"] == page_name:
    rect = common.canvas_rect(ti["geometry"])
    visual = _build_table_visual(ti)
    vc = _build_visual_container(
        ti["name"],
        rect,
        3000 + idx,
        3000 + idx,
        visual,
        annotations=_annotations(ti["name"], is_parity=False),
    )
    body_visuals.append((ti["geometry"], vc))
    idx += 1

  # Sort body visuals by (row, col) for z/tabOrder
  body_visuals.sort(key=lambda x: (x[0]["row"], x[0]["col"]))
  for i, (geom, vc) in enumerate(body_visuals):
    vc["position"]["z"] = 4000 + i
    vc["position"]["tabOrder"] = 4000 + i
    visuals.append(vc)

  return visuals


def _page_display_name(page_name, visuals_spec):
  """Get the display name for a page."""
  for p in visuals_spec["pages"]:
    if p["name"] == page_name:
      return p["display_name"]
  return page_name


def _page_heights(visuals_spec, common):
  """Compute page heights from body rects + spec override."""
  heights = {}
  for p in visuals_spec["pages"]:
    pname = p["name"]
    # Collect body rects for this page
    rects = []
    for vdef in visuals_spec["parity_visuals"]:
      if vdef["page"] == pname:
        rects.append(common.canvas_rect(vdef["geometry"]))
    for ndef in visuals_spec.get("non_data_elements", []):
      if ndef["page"] == pname:
        rects.append(common.canvas_rect(ndef["geometry"]))
    ti = visuals_spec.get("trace_inspector")
    if ti and ti["page"] == pname:
      rects.append(common.canvas_rect(ti["geometry"]))
    # Include header rects
    for hr in common.HEADER_RECTS.values():
      rects.append(hr)
    computed = common.page_height(rects)
    # Use the spec override if it's larger (spec heights are
    # the normative values from spec §5.1)
    spec_h = p.get("height", computed)
    heights[pname] = max(spec_h, computed)
  return heights


def build_report(contracts, visuals_spec, model_spec=None):
  """Build the full report definition.

  Args:
    contracts: Looker Studio contracts (from pbip_common.load_contracts).
    visuals_spec: The report_visuals.yaml dict (from
      pbip_common.load_spec("report_visuals")).
    model_spec: The semantic_model.yaml dict (from
      pbip_common.load_spec("semantic_model")). Optional; used for
      measure name validation.

  Returns:
    A dict mapping relative file paths to file content strings.
  """
  # Import pbip_common dynamically
  import importlib.util

  common_path = os.path.join(
      os.path.dirname(os.path.abspath(__file__)),
      "pbip_common.py",
  )
  _mod_spec = importlib.util.spec_from_file_location("pbip_common", common_path)
  common = importlib.util.module_from_spec(_mod_spec)
  _mod_spec.loader.exec_module(common)

  files = {}

  # version.json
  files[os.path.join(_DEF_DIR, "version.json")] = common.dump_json(
      {
          "$schema": _VERSION_SCHEMA,
          "version": "2.0.0",
      }
  )

  # report.json
  report_json = {
      "$schema": _REPORT_SCHEMA,
      "themeCollection": _theme_collection(),
      "resourcePackages": _resource_packages(),
      "filterConfig": {
          "filters": [
              _build_report_filter(f) for f in visuals_spec["report_filters"]
          ]
      },
  }
  files[os.path.join(_DEF_DIR, "report.json")] = common.dump_json(report_json)

  # pages.json
  page_order = [p["name"] for p in visuals_spec["pages"]]
  pages_json = {
      "$schema": _PAGES_SCHEMA,
      "pageOrder": page_order,
      "activePageName": "token-consumption",
  }
  files[os.path.join(_PAGES_DIR, "pages.json")] = common.dump_json(pages_json)

  # Per-page page.json and visual.json files
  page_heights = _page_heights(visuals_spec, common)

  for p in visuals_spec["pages"]:
    pname = p["name"]
    pslug = _slug(pname)
    pdir = os.path.join(_PAGES_DIR, pslug)

    # page.json
    page_no_filter = _listener_matrix(visuals_spec, page_name=pname)
    page_json = {
        "$schema": _PAGE_SCHEMA,
        "name": pname,
        "displayName": p["display_name"],
        "displayOption": "FitToWidth",
        "height": page_heights[pname],
        "width": 1280,
        "annotations": [
            {
                "name": "bqaa.looker_page_id",
                "value": p["looker_page_id"],
            }
        ],
    }
    if page_no_filter:
      page_json["visualInteractions"] = page_no_filter
    files[os.path.join(pdir, "page.json")] = common.dump_json(page_json)

    # Visuals
    visuals = _collect_visuals_for_page(pname, visuals_spec, model_spec, common)
    for vc in visuals:
      vname = vc["name"]
      vdir = os.path.join(pdir, "visuals", vname)
      files[os.path.join(vdir, "visual.json")] = common.dump_json(vc)

  # Theme JSON
  files[os.path.join(_THEME_DIR, "BQAA.json")] = common.dump_json(
      _theme_json(visuals_spec)
  )

  return files
