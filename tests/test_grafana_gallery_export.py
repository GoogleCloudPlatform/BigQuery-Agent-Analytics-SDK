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
grafana.com listing. Nothing regenerates it, so these tests fail when the trial
changes without a new export, and when the export breaks a gallery rule:

1. Panels match the trial: ids, types, titles, descriptions, layout, data
   sources, queries, transformations, units, data links and field overrides.
2. Variables match the trial, with the six constants resolved through their
   VAR_* inputs, and so do the dashboard-level settings.
3. Export-only rules: no top-level id, no __requires entry with an empty
   version, VAR_* inputs that carry the placeholder defaults, and one DS_*
   input that binds only through the datasource variable, so every BigQuery
   reference keeps ${datasource}.

Grafana's schema migration rewrites some display defaults on export (the base
threshold step, min/max on Event error rate, hideFrom.tooltip on Tokens by
model), so field overrides are compared by matcher and property id, and data
links by value.
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
GALLERY = json.loads(GALLERY_PATH.read_text(encoding="utf-8"))
BIGQUERY_PLUGIN = "grafana-bigquery-datasource"
BUILT_IN_UIDS = {"-- Dashboard --", "-- Grafana --"}
# The sanitized constant values the gallery inputs must carry.
PLACEHOLDERS = {
    "project": "my-project",
    "dataset": "agent_analytics",
    "table": "agent_events",
    "view_prefix": "adk_",
    "price_per_1m_input_tokens": "1.25",
    "price_per_1m_output_tokens": "5.00",
}
PANEL_FIELDS = (
    "type",
    "title",
    "description",
    "gridPos",
    "datasource",
    "targets",
    "transformations",
    "links",
    "collapsed",
    "repeat",
    "maxDataPoints",
    "interval",
    "timeFrom",
    "timeShift",
)
FIELD_DEFAULTS = ("unit", "decimals", "noValue", "displayName", "links")
VARIABLE_FIELDS = (
    "type",
    "label",
    "description",
    "datasource",
    "query",
    "definition",
    "regex",
    "multi",
    "includeAll",
    "allValue",
    "refresh",
)
# The exporter drops these when they hold Grafana's default.
VARIABLE_DEFAULTS = {"hide": 0, "skipUrlSync": False, "sort": 0}
DASHBOARD_FIELDS = (
    "uid",
    "title",
    "description",
    "tags",
    "time",
    "timepicker",
    "timezone",
    "refresh",
    "links",
    "annotations",
    "graphTooltip",
    "editable",
    "fiscalYearStartMonth",
)


def _walk(panels):
  for panel in panels:
    yield panel
    yield from _walk(panel.get("panels", []))


def _panels(dashboard):
  return {panel["id"]: panel for panel in _walk(dashboard["panels"])}


def _variables(dashboard):
  return {v["name"]: v for v in dashboard["templating"]["list"]}


def _inputs():
  return {item["name"]: item for item in GALLERY.get("__inputs", [])}


def _datasource_input():
  (item,) = [i for i in _inputs().values() if i["type"] == "datasource"]
  return item


def _resolve_inputs(node):
  """Replaces ${VAR_*} input references with the inputs' values."""
  if isinstance(node, dict):
    return {key: _resolve_inputs(value) for key, value in node.items()}
  if isinstance(node, list):
    return [_resolve_inputs(value) for value in node]
  if isinstance(node, str):
    for name, item in _inputs().items():
      if item["type"] == "constant":
        node = node.replace("${" + name + "}", item["value"])
  return node


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


def _override_shape(panel):
  return [
      (
          override["matcher"],
          [prop["id"] for prop in override.get("properties", [])],
      )
      for override in panel.get("fieldConfig", {}).get("overrides", [])
  ]


def _override_links(panel):
  return [
      prop["value"]
      for override in panel.get("fieldConfig", {}).get("overrides", [])
      for prop in override.get("properties", [])
      if prop["id"] == "links"
  ]


TRIAL_PANELS = _panels(TRIAL)
GALLERY_PANELS = _panels(GALLERY)


def test_gallery_has_the_trial_panels_in_order():
  assert list(GALLERY_PANELS) == list(TRIAL_PANELS)


@pytest.mark.parametrize("panel_id", list(TRIAL_PANELS))
def test_gallery_panel_matches_the_trial(panel_id):
  trial = TRIAL_PANELS[panel_id]
  gallery = GALLERY_PANELS[panel_id]
  for field in PANEL_FIELDS:
    assert gallery.get(field) == trial.get(field), field
  trial_defaults = trial.get("fieldConfig", {}).get("defaults", {})
  gallery_defaults = gallery.get("fieldConfig", {}).get("defaults", {})
  for field in FIELD_DEFAULTS:
    assert gallery_defaults.get(field) == trial_defaults.get(field), field
  assert _override_shape(gallery) == _override_shape(trial)
  assert _override_links(gallery) == _override_links(trial)


def test_gallery_has_the_trial_variables_in_order():
  assert list(_variables(GALLERY)) == list(_variables(TRIAL))


@pytest.mark.parametrize("name", list(_variables(TRIAL)))
def test_gallery_variable_matches_the_trial(name):
  trial = _variables(TRIAL)[name]
  gallery = _resolve_inputs(_variables(GALLERY)[name])
  for field in VARIABLE_FIELDS:
    assert gallery.get(field) == trial.get(field), field
  for field, default in VARIABLE_DEFAULTS.items():
    assert gallery.get(field, default) == trial.get(field, default), field


@pytest.mark.parametrize("field", DASHBOARD_FIELDS)
def test_gallery_dashboard_setting_matches_the_trial(field):
  assert GALLERY.get(field) == TRIAL.get(field)


def test_gallery_has_no_instance_fields():
  assert "id" not in GALLERY
  assert [r for r in GALLERY["__requires"] if r.get("version") == ""] == []
  assert {"grafana", BIGQUERY_PLUGIN} <= {
      r["id"] for r in GALLERY["__requires"]
  }


def test_gallery_constants_are_placeholder_inputs():
  constants = {
      name: variable
      for name, variable in _variables(GALLERY).items()
      if variable["type"] == "constant"
  }
  assert set(constants) == set(PLACEHOLDERS)
  inputs = _inputs()
  expected = {f"VAR_{name.upper()}" for name in PLACEHOLDERS}
  expected.add(_datasource_input()["name"])
  assert set(inputs) == expected
  for name, value in PLACEHOLDERS.items():
    ref = f"VAR_{name.upper()}"
    assert inputs[ref]["type"] == "constant"
    assert inputs[ref]["value"] == value
    assert constants[name]["query"] == "${" + ref + "}"
    assert constants[name]["hide"] == 2
    assert constants[name]["skipUrlSync"] is True
    # The trial holds the same placeholder, so no real value is exported.
    assert _variables(TRIAL)[name]["query"] == value


def test_gallery_datasource_input_binds_only_through_the_variable():
  item = _datasource_input()
  assert item["name"].startswith("DS_")
  assert item["pluginId"] == BIGQUERY_PLUGIN
  reference = "${" + item["name"] + "}"
  datasource = _variables(GALLERY)["datasource"]
  assert datasource["type"] == "datasource"
  assert datasource["query"] == BIGQUERY_PLUGIN
  assert datasource["current"]["value"] == reference
  assert GALLERY_PATH.read_text(encoding="utf-8").count(reference) == 1


def test_gallery_keeps_every_runtime_datasource_reference():
  gallery = sorted(_datasource_uids(GALLERY))
  assert gallery == sorted(_datasource_uids(TRIAL))
  assert set(gallery) <= {"${datasource}"} | BUILT_IN_UIDS


def test_gallery_holds_no_credentials_or_hosts():
  text = GALLERY_PATH.read_text(encoding="utf-8")
  for needle in (
      "private_key",
      "PRIVATE KEY",
      "client_email",
      "googleusercontent",
      "localhost",
      "127.0.0.1",
  ):
    assert needle not in text
  assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
