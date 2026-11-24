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

"""Keeps the Grafana gallery export in step with the efficiency trial.

dashboards/grafana/gallery/bqaa-dashboard-efficiency-trial.gallery.json is the
trial dashboard as Grafana's "Export for sharing externally" writes it, for the
grafana.com listing. Nothing regenerates it, so these tests diff the two files
and fail on any difference that the export does not explain:

1. Panels, variables and dashboard settings must be identical, except for the
   rewrites allowed below: defaults Grafana fills in (FILLED_DEFAULTS, with
   their exact values), schema-migration changes (_panel_change_allowed), and
   the export's own changes (constants and the data source become __inputs
   references, query variables lose their saved state).
2. Export-only rules: no top-level id; __requires lists only Grafana 12.3.4 or
   later and the BigQuery plugin version that run_local.py pins; the six VAR_*
   inputs carry the placeholder defaults; the datasource input is exactly
   DS_BIGQUERY, labelled "BigQuery", and binds only through the datasource
   variable, so every BigQuery reference keeps ${datasource}.
3. Sanitization: no saved variable state beyond those references, and no URLs,
   credentials or hosts anywhere in the file.
"""

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAFANA = ROOT / "dashboards" / "grafana"
TRIAL_PATH = GRAFANA / "bqaa-dashboard-efficiency-trial.json"
GALLERY_PATH = (
    GRAFANA / "gallery" / "bqaa-dashboard-efficiency-trial.gallery.json"
)
TRIAL = json.loads(TRIAL_PATH.read_text(encoding="utf-8"))
GALLERY_TEXT = GALLERY_PATH.read_text(encoding="utf-8")
GALLERY = json.loads(GALLERY_TEXT)
BIGQUERY_PLUGIN = "grafana-bigquery-datasource"
BUILT_IN_UIDS = {"-- Dashboard --", "-- Grafana --"}
# The exporter names and labels the input after the exporting data source, so
# anything else would publish that instance's data source name.
DATASOURCE_INPUT = {
    "name": "DS_BIGQUERY",
    "label": "BigQuery",
    "description": "",
    "type": "datasource",
    "pluginId": BIGQUERY_PLUGIN,
    "pluginName": "Google BigQuery",
}
# Grafana 12.3.0 to 12.3.3 cannot make this export (grafana/grafana#113319).
MIN_GRAFANA = (12, 3, 4)
# The sanitized constant values the gallery inputs must carry.
PLACEHOLDERS = {
    "project": "my-project",
    "dataset": "agent_analytics",
    "table": "agent_events",
    "view_prefix": "adk_",
    "price_per_1m_input_tokens": "1.25",
    "price_per_1m_output_tokens": "5.00",
}
# Keys the exporter drops when they hold Grafana's default.
VARIABLE_DEFAULTS = {"hide": 0, "skipUrlSync": False, "sort": 0}
DASHBOARD_DEFAULTS = {"liveNow": False}
# Keys the exporter adds at the top level, with the only values allowed.
DASHBOARD_ADDITIONS = {"__elements": {}, "preload": False, "weekStart": ""}
# Defaults Grafana 12.3 writes into panels on export, with their values. A
# re-export from another Grafana version may need this table updated.
_DEFAULTS = ("fieldConfig", "defaults")
_CUSTOM = _DEFAULTS + ("custom",)
FILLED_DEFAULTS = {
    _DEFAULTS + ("mappings",): [],
    _DEFAULTS
    + ("thresholds",): {
        "mode": "absolute",
        "steps": [
            {"color": "green", "value": 0},
            {"color": "red", "value": 80},
        ],
    },
    _CUSTOM + ("axisBorderShow",): False,
    _CUSTOM + ("axisCenteredZero",): False,
    _CUSTOM + ("axisColorMode",): "text",
    _CUSTOM + ("axisLabel",): "",
    _CUSTOM + ("axisPlacement",): "auto",
    _CUSTOM + ("barAlignment",): 0,
    _CUSTOM + ("barWidthFactor",): 0.6,
    _CUSTOM + ("footer",): {"reducers": []},
    _CUSTOM + ("hideFrom",): {"legend": False, "tooltip": False, "viz": False},
    _CUSTOM + ("insertNulls",): False,
    _CUSTOM + ("inspect",): False,
    _CUSTOM + ("lineInterpolation",): "linear",
    _CUSTOM + ("pointSize",): 5,
    _CUSTOM + ("scaleDistribution",): {"type": "linear"},
    _CUSTOM + ("showValues",): False,
    _CUSTOM + ("thresholdsStyle",): {"mode": "off"},
    ("options", "barRadius"): 0,
    ("options", "fullHighlight"): False,
    ("options", "legend", "calcs"): [],
    ("options", "percentChangeColorMode"): "standard",
    ("options", "showPercentChange"): False,
    ("options", "stacking"): "none",
    ("options", "tooltip", "hideZeros"): False,
    ("options", "wideLayout"): True,
    ("options", "xTickLabelSpacing"): 0,
}
GRAFANA_VERSION = next(
    (
        r.get("version")
        for r in GALLERY.get("__requires", [])
        if r.get("id") == "grafana"
    ),
    None,
)


def _diff(trial, gallery, path=()):
  """Yields (kind, path, trial value, gallery value) for every difference."""
  if isinstance(trial, dict) and isinstance(gallery, dict):
    for key in sorted(set(trial) | set(gallery)):
      sub = path + (key,)
      if key not in trial:
        yield "added", sub, None, gallery[key]
      elif key not in gallery:
        yield "removed", sub, trial[key], None
      else:
        yield from _diff(trial[key], gallery[key], sub)
  elif (
      isinstance(trial, list)
      and isinstance(gallery, list)
      and len(trial) == len(gallery)
  ):
    for index, (old, new) in enumerate(zip(trial, gallery)):
      yield from _diff(old, new, path + (index,))
  elif trial != gallery:
    yield "changed", path, trial, gallery


def _walk(panels):
  for panel in panels:
    yield panel
    yield from _walk(panel.get("panels", []))


def _panels(dashboard):
  return {
      panel["id"]: {
          key: value for key, value in panel.items() if key != "panels"
      }
      for panel in _walk(dashboard["panels"])
  }


def _variables(dashboard):
  return {v["name"]: v for v in dashboard["templating"]["list"]}


def _version(text):
  return tuple(
      int(part) for part in re.match(r"(\d+)\.(\d+)\.(\d+)", text).groups()
  )


def _plugin_pin():
  text = (GRAFANA / "run_local.py").read_text(encoding="utf-8")
  return re.search(r'^PLUGIN_VERSION = "([^"]+)"', text, re.M).group(1)


def _datasource_uids(node):
  if isinstance(node, dict):
    ref = node.get("datasource")
    if isinstance(ref, dict) and "uid" in ref:
      yield ref["uid"]
    for value in node.values():
      yield from _datasource_uids(value)
  elif isinstance(node, list):
    for value in node:
      yield from _datasource_uids(value)


def _panel_change_allowed(panel, kind, path, old, new):
  """True for the rewrites Grafana's export and schema migration make."""
  if kind == "added":
    if path in FILLED_DEFAULTS:
      return new == FILLED_DEFAULTS[path]
    if path == ("pluginVersion",):
      return new == GRAFANA_VERSION
    # A percentunit field is bounded to 0..1.
    if path in (
        ("fieldConfig", "defaults", "min"),
        ("fieldConfig", "defaults", "max"),
    ):
      unit = panel.get("fieldConfig", {}).get("defaults", {}).get("unit")
      return unit == "percentunit" and new == (0 if path[-1] == "min" else 1)
    return False
  # The base threshold step (-Infinity, saved as null) is written as 0 or
  # dropped.
  if path == ("fieldConfig", "defaults", "thresholds", "steps", 0, "value"):
    return old is None and (kind == "removed" or new == 0)
  # A hidden table footer moves into fieldConfig.defaults.custom.footer.
  if path == ("options", "footer"):
    return panel["type"] == "table" and kind == "removed" and not old["show"]
  # Series hidden from the viz are hidden from the tooltip too.
  if (
      kind == "changed"
      and len(path) == 7
      and path[:2] == ("fieldConfig", "overrides")
      and path[3] == "properties"
      and path[5:] == ("value", "tooltip")
  ):
    prop = panel["fieldConfig"]["overrides"][path[2]]["properties"][path[4]]
    return (
        prop["id"] == "custom.hideFrom"
        and prop["value"].get("viz") is True
        and old is False
        and new is True
    )
  return False


def _variable_change_allowed(variable, kind, path, old, new):
  """True for the export's own rewrites of a variable."""
  if kind == "removed" and len(path) == 1 and path[0] in VARIABLE_DEFAULTS:
    return old == VARIABLE_DEFAULTS[path[0]]
  if variable["type"] == "constant":
    ref = "${VAR_" + variable["name"].upper() + "}"
    if path in (
        ("query",),
        ("current", "text"),
        ("current", "value"),
        ("options", 0, "text"),
        ("options", 0, "value"),
    ):
      return kind == "changed" and new == ref
    if path == ("options", 0, "selected"):
      return new is False
  # The datasource variable gets the DS_* reference and query variables lose
  # their selection; the exact results are checked below.
  if variable["type"] == "datasource" and path[:1] == ("current",):
    return kind == "added"
  if variable["type"] == "query" and path[:1] == ("current",):
    return kind == "removed"
  return False


TRIAL_PANELS = _panels(TRIAL)
GALLERY_PANELS = _panels(GALLERY)
TRIAL_VARIABLES = _variables(TRIAL)
GALLERY_VARIABLES = _variables(GALLERY)
QUERY_VARIABLES = [
    n for n, v in GALLERY_VARIABLES.items() if v["type"] == "query"
]


def test_gallery_has_the_trial_panels_in_order():
  assert list(GALLERY_PANELS) == list(TRIAL_PANELS)


@pytest.mark.parametrize("panel_id", list(TRIAL_PANELS))
def test_gallery_panel_matches_the_trial(panel_id):
  trial = TRIAL_PANELS[panel_id]
  unexplained = [
      (kind, path, old, new)
      for kind, path, old, new in _diff(trial, GALLERY_PANELS[panel_id])
      if not _panel_change_allowed(trial, kind, path, old, new)
  ]
  assert unexplained == []


def test_gallery_has_the_trial_variables_in_order():
  assert list(GALLERY_VARIABLES) == list(TRIAL_VARIABLES)
  assert set(GALLERY["templating"]) == set(TRIAL["templating"]) == {"list"}


@pytest.mark.parametrize("name", list(TRIAL_VARIABLES))
def test_gallery_variable_matches_the_trial(name):
  trial = TRIAL_VARIABLES[name]
  unexplained = [
      (kind, path, old, new)
      for kind, path, old, new in _diff(trial, GALLERY_VARIABLES[name])
      if not _variable_change_allowed(trial, kind, path, old, new)
  ]
  assert unexplained == []


def test_gallery_dashboard_settings_match_the_trial():
  skip = {"panels", "templating", "__inputs", "__requires"}
  trial = {k: v for k, v in TRIAL.items() if k not in skip}
  gallery = {k: v for k, v in GALLERY.items() if k not in skip}
  unexplained = []
  for kind, path, old, new in _diff(trial, gallery):
    key = path[0]
    if len(path) == 1 and kind == "removed" and key in DASHBOARD_DEFAULTS:
      if old == DASHBOARD_DEFAULTS[key]:
        continue
    if len(path) == 1 and kind == "added" and key in DASHBOARD_ADDITIONS:
      if new == DASHBOARD_ADDITIONS[key]:
        continue
    if path == ("schemaVersion",) and kind == "changed" and new >= old:
      continue
    unexplained.append((kind, path, old, new))
  assert unexplained == []
  assert "id" not in GALLERY


def test_gallery_requires_the_tested_versions():
  requires = {item["id"]: item for item in GALLERY["__requires"]}
  assert len(GALLERY["__requires"]) == len(requires) == 2
  grafana = requires["grafana"]
  assert (grafana["type"], grafana["name"]) == ("grafana", "Grafana")
  assert _version(grafana["version"]) >= MIN_GRAFANA
  plugin = requires[BIGQUERY_PLUGIN]
  assert (plugin["type"], plugin["name"]) == ("datasource", "Google BigQuery")
  assert plugin["version"] == _plugin_pin()


def test_gallery_constants_are_placeholder_inputs():
  inputs = {item["name"]: item for item in GALLERY["__inputs"]}
  constants = {
      name: variable
      for name, variable in GALLERY_VARIABLES.items()
      if variable["type"] == "constant"
  }
  assert set(constants) == set(PLACEHOLDERS)
  assert set(inputs) == {DATASOURCE_INPUT["name"]} | {
      f"VAR_{name.upper()}" for name in PLACEHOLDERS
  }
  for name, value in PLACEHOLDERS.items():
    ref = f"VAR_{name.upper()}"
    assert inputs[ref]["type"] == "constant"
    assert inputs[ref]["value"] == value
    variable = constants[name]
    assert variable["query"] == "${" + ref + "}"
    assert (
        variable["current"]["text"]
        == variable["current"]["value"]
        == "${" + ref + "}"
    )
    assert [(o["text"], o["value"]) for o in variable["options"]] == [
        ("${" + ref + "}", "${" + ref + "}")
    ]
    assert variable["hide"] == 2
    assert variable["skipUrlSync"] is True
    # The trial holds the same placeholder, so no real value is exported.
    assert TRIAL_VARIABLES[name]["query"] == value


def test_gallery_datasource_input_binds_only_through_the_variable():
  (item,) = [i for i in GALLERY["__inputs"] if i["type"] == "datasource"]
  assert item == DATASOURCE_INPUT
  datasource = GALLERY_VARIABLES["datasource"]
  assert datasource["type"] == "datasource"
  assert datasource["query"] == BIGQUERY_PLUGIN
  assert datasource["current"] == {
      "selected": True,
      "text": "",
      "value": "${DS_BIGQUERY}",
  }
  assert datasource.get("options", []) == []
  assert GALLERY_TEXT.count("${DS_BIGQUERY}") == 1


@pytest.mark.parametrize("name", QUERY_VARIABLES)
def test_gallery_query_variable_holds_no_saved_state(name):
  variable = GALLERY_VARIABLES[name]
  assert variable.get("current", {}) == {}
  assert variable.get("options", []) == []


def test_gallery_keeps_every_runtime_datasource_reference():
  gallery = sorted(_datasource_uids(GALLERY))
  assert gallery == sorted(_datasource_uids(TRIAL))
  assert set(gallery) <= {"${datasource}"} | BUILT_IN_UIDS


def test_gallery_holds_no_urls_credentials_or_hosts():
  for needle in (
      "private_key",
      "PRIVATE KEY",
      "client_email",
      "googleusercontent",
      "localhost",
      "127.0.0.1",
  ):
    assert needle not in GALLERY_TEXT
  assert not re.search(r"https?://", GALLERY_TEXT)
  assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", GALLERY_TEXT)
