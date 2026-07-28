#!/usr/bin/env python3
"""Check that canonical Grafana SQL matches the dashboard's embedded queries."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
import sys
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = REPOSITORY_ROOT / "grafana" / "bqaa-dashboard.json"
QUERIES_DIRECTORY = REPOSITORY_ROOT / "grafana" / "queries"

# Panel IDs are stable across title and layout changes. Only panel 2 embeds the
# overview query; panels 3-5 consume its result through Grafana's Dashboard data
# source and are checked separately below. Likewise, panel 12 embeds the cost
# query and panel 20 (Total tokens) reuses its result, so it adds no BigQuery
# load.
PANEL_QUERIES = {
    2: "overview_totals.sql",
    6: "events_over_time.sql",
    7: "errors_over_time.sql",
    9: "llm_tokens_over_time.sql",
    10: "llm_latency_percentiles.sql",
    11: "tokens_by_model.sql",
    12: "estimated_cost.sql",
    14: "tool_usage.sql",
    15: "tool_latency.sql",
    16: "tool_errors.sql",
    18: "recent_sessions.sql",
    19: "trace_detail.sql",
}
TEMPLATE_VARIABLE_QUERIES = {
    "agent": "var_agent.sql",
    "session_id": "var_session_id.sql",
    "user_id": "var_user_id.sql",
    "event_type": "var_event_type.sql",
}
# Panels that consume another panel's result via Grafana's Dashboard data
# source, mapped to the panel id they draw from. These add no BigQuery load, so
# they carry no embedded query and are validated by reference instead.
DASHBOARD_DATA_PANEL_SOURCES = {
    3: 2,
    4: 2,
    5: 2,
    20: 12,
}


def normalize(query: str) -> str:
  """Ignore only harmless whitespace at the beginning and end."""

  return query.strip()


def fail(message: str) -> None:
  print(f"ERROR: {message}", file=sys.stderr)


def check_unmapped_queries() -> int:
  """Fail if the queries directory holds .sql files this check never sees.

  The explicit panel and template-variable maps decide which .sql files get
  compared against the dashboard. A new .sql file that nobody adds to either
  map is therefore invisible to this check. Diff the on-disk files against the
  mapped files directly so a mismatch is surfaced loudly rather than silently
  passing.
  Comparing counts alone is unsound: deleting one mapped file while adding one
  unmapped file keeps the counts equal but leaves an unmapped file uncaught.

  Returns the number of unmapped .sql files found.
  """

  try:
    sql_files = {path.name for path in QUERIES_DIRECTORY.glob("*.sql")}
  except OSError as error:
    fail(f"cannot list canonical queries in {QUERIES_DIRECTORY}: {error}")
    return 1

  mapped_files = set(PANEL_QUERIES.values()) | set(
      TEMPLATE_VARIABLE_QUERIES.values()
  )
  unmapped = sorted(sql_files - mapped_files)
  if not unmapped:
    return 0
  print(
      "\n"
      "  ****************************************************************\n"
      "  * ERROR: unmapped SQL files in grafana/queries/                *\n"
      "  ****************************************************************\n"
      f"  Found {len(sql_files)} .sql file(s) on disk but the query maps cover\n"
      f"  only {len(mapped_files)} unique file(s). This check compares ONLY\n"
      "  the files listed in PANEL_QUERIES or TEMPLATE_VARIABLE_QUERIES, so\n"
      "  files missing from both are never validated against the dashboard.\n"
      "\n"
      "  Do NOT rely on this script to catch every drift automatically\n"
      "  until a query map is updated to cover the files below:\n"
      + "".join(f"    - {name}\n" for name in unmapped)
      + "  ****************************************************************",
      file=sys.stderr,
  )
  return len(unmapped)


def load_dashboard() -> dict[str, Any]:
  try:
    dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as error:
    raise ValueError(
        f"cannot load valid JSON from {DASHBOARD_PATH}: {error}"
    ) from error

  # Minimal schema validation for the fields this check relies on. This keeps
  # the workflow dependency-free while producing useful corruption errors.
  if not isinstance(dashboard, dict):
    raise ValueError("dashboard root must be a JSON object")
  if not isinstance(dashboard.get("title"), str):
    raise ValueError("dashboard.title must be a string")
  if not isinstance(dashboard.get("schemaVersion"), int):
    raise ValueError("dashboard.schemaVersion must be an integer")
  if not isinstance(dashboard.get("panels"), list):
    raise ValueError("dashboard.panels must be an array")
  return dashboard


def flatten_panels(panels: list[Any]) -> list[Any]:
  """Return all panels, including panels nested inside collapsed rows."""

  flattened: list[Any] = []
  for panel in panels:
    flattened.append(panel)
    if isinstance(panel, dict):
      nested_panels = panel.get("panels", [])
      if isinstance(nested_panels, list):
        flattened.extend(flatten_panels(nested_panels))
  return flattened


def main() -> int:
  """
  Validates the integrity and synchronization of the Grafana dashboard queries.

  This CI script strictly enforces six key conditions:
  1. Drift Prevention: The 'rawSql' inside the JSON dashboard exactly matches
     the canonical '.sql' files in the queries directory (printing unified diffs on failure).
  2. Unmapped File Detection: Every '.sql' file in the queries directory is
     explicitly registered in the PANEL_QUERIES dictionary.
  3. Reverse Mapping Detection: Every BigQuery panel in the JSON dashboard
     is explicitly registered in the PANEL_QUERIES dictionary.
  4. Dashboard Datasource Validation: Every panel querying the '-- Dashboard --'
     datasource is explicitly registered in DASHBOARD_DATA_PANEL_SOURCES.
  5. Template Variable Reverse Mapping: Every BigQuery query variable in the
     dashboard is explicitly registered in TEMPLATE_VARIABLE_QUERIES.
  6. Template Variable Synchronization: Query variables match their canonical
     files and their definition and query.rawSql copies match each other.
  """
  errors = check_unmapped_queries()

  try:
    dashboard = load_dashboard()
  except ValueError as error:
    fail(str(error))
    return 1

  panels: dict[int, dict[str, Any]] = {}
  for panel in flatten_panels(dashboard["panels"]):
    if not isinstance(panel, dict) or not isinstance(panel.get("id"), int):
      fail("each dashboard panel must be an object with an integer id")
      errors += 1
      continue
    panel_id = panel["id"]
    if panel_id in panels:
      fail(f"duplicate panel id {panel_id}")
      errors += 1
    panels[panel_id] = panel

  templating = dashboard.get("templating", {})
  variables = templating.get("list", []) if isinstance(templating, dict) else []
  if not isinstance(variables, list):
    fail("dashboard.templating.list must be an array")
    errors += 1
    variables = []
  variables_by_name = {
      variable.get("name"): variable
      for variable in variables
      if isinstance(variable, dict) and isinstance(variable.get("name"), str)
  }

  for variable in variables:
    if not isinstance(variable, dict) or variable.get("type") != "query":
      continue
    datasource = variable.get("datasource")
    if (
        not isinstance(datasource, dict)
        or datasource.get("type") != "grafana-bigquery-datasource"
    ):
      continue
    variable_name = variable.get("name")
    if not isinstance(variable_name, str):
      fail("each BigQuery query variable must have a string name")
      errors += 1
    elif variable_name not in TEMPLATE_VARIABLE_QUERIES:
      fail(
          f"BigQuery query variable {variable_name} is missing from "
          "TEMPLATE_VARIABLE_QUERIES"
      )
      errors += 1

  for variable_name, filename in TEMPLATE_VARIABLE_QUERIES.items():
    variable = variables_by_name.get(variable_name)
    if variable is None:
      fail(f"missing template variable {variable_name} for {filename}")
      errors += 1
      continue
    definition = variable.get("definition")
    query = variable.get("query")
    raw_sql = query.get("rawSql") if isinstance(query, dict) else None
    if not isinstance(definition, str) or not isinstance(raw_sql, str):
      fail(
          f"template variable {variable_name} must have string definition "
          "and query.rawSql values"
      )
      errors += 1
      continue
    if normalize(definition) != normalize(raw_sql):
      fail(
          f"template variable {variable_name} definition has drifted from "
          "query.rawSql"
      )
      errors += 1
    try:
      canonical_query = (QUERIES_DIRECTORY / filename).read_text(
          encoding="utf-8"
      )
    except OSError as error:
      fail(f"cannot read canonical query {filename}: {error}")
      errors += 1
      continue
    if normalize(raw_sql) != normalize(canonical_query):
      fail(
          f"template variable {variable_name} query has drifted from "
          f"grafana/queries/{filename}"
      )
      diff = difflib.unified_diff(
          normalize(canonical_query).splitlines(),
          normalize(raw_sql).splitlines(),
          fromfile=f"grafana/queries/{filename}",
          tofile=f"dashboard variable {variable_name} query.rawSql",
          lineterm="",
      )
      print("\n".join(diff), file=sys.stderr)
      errors += 1

  for variable_name, variable in variables_by_name.items():
    all_value = variable.get("allValue")
    if all_value is None:
      continue
    if not isinstance(all_value, str) or not all_value:
      fail(
          f"template variable {variable_name} allValue must be a "
          "non-empty string when set"
      )
      errors += 1
      continue
    interpolation = "${" + variable_name + ":sqlstring}"
    sentinel = all_value
    for filename in PANEL_QUERIES.values():
      try:
        canonical_query = (QUERIES_DIRECTORY / filename).read_text(
            encoding="utf-8"
        )
      except OSError:
        # The canonical panel-query loop below reports the read failure.
        continue
      if interpolation in canonical_query and sentinel not in canonical_query:
        fail(
            f"canonical query {filename} uses {interpolation} but does not "
            f"contain the variable's allValue sentinel {all_value!r}"
        )
        errors += 1

  for panel_id, panel in panels.items():
    if panel.get("type") == "row":
      continue
    targets = panel.get("targets", [])
    if not isinstance(targets, list):
      continue
    if any(
        isinstance(target, dict) and isinstance(target.get("rawSql"), str)
        for target in targets
    ):
      if panel_id not in PANEL_QUERIES:
        fail(
            f"panel id {panel_id} queries BigQuery but is missing from "
            "PANEL_QUERIES"
        )
        errors += 1
    datasource = panel.get("datasource", {})
    if (
        isinstance(datasource, dict)
        and datasource.get("uid") == "-- Dashboard --"
        and panel_id not in DASHBOARD_DATA_PANEL_SOURCES
    ):
      fail(
          f"panel id {panel_id} uses the Dashboard datasource but is missing "
          "from DASHBOARD_DATA_PANEL_SOURCES"
      )
      errors += 1

  for panel_id, filename in PANEL_QUERIES.items():
    panel = panels.get(panel_id)
    if panel is None:
      fail(f"missing panel id {panel_id} for {filename}")
      errors += 1
      continue
    targets = panel.get("targets")
    if not isinstance(targets, list) or len(targets) != 1:
      fail(f"panel id {panel_id} must have exactly one query target")
      errors += 1
      continue
    embedded_query = targets[0].get("rawSql")
    if not isinstance(embedded_query, str):
      fail(f"panel id {panel_id} has no string rawSql for {filename}")
      errors += 1
      continue
    try:
      canonical_query = (QUERIES_DIRECTORY / filename).read_text(
          encoding="utf-8"
      )
    except OSError as error:
      fail(f"cannot read canonical query {filename}: {error}")
      errors += 1
      continue
    if normalize(embedded_query) != normalize(canonical_query):
      fail(
          f"panel id {panel_id} query has drifted from "
          f"grafana/queries/{filename}"
      )
      diff = difflib.unified_diff(
          normalize(canonical_query).splitlines(),
          normalize(embedded_query).splitlines(),
          fromfile=f"grafana/queries/{filename}",
          tofile=f"dashboard panel {panel_id} rawSql",
          lineterm="",
      )
      print("\n".join(diff), file=sys.stderr)
      errors += 1

  for panel_id, source_panel_id in DASHBOARD_DATA_PANEL_SOURCES.items():
    panel = panels.get(panel_id, {})
    targets = panel.get("targets", [])
    datasource = panel.get("datasource", {})
    if (
        datasource.get("uid") != "-- Dashboard --"
        or len(targets) != 1
        or targets[0].get("panelId") != source_panel_id
        or targets[0].get("datasource", {}).get("uid") != "-- Dashboard --"
    ):
      fail(
          f"panel id {panel_id} must use Dashboard data from panel "
          f"{source_panel_id}"
      )
      errors += 1

  if errors:
    print(
        f"Grafana query sync check failed with {errors} error(s).",
        file=sys.stderr,
    )
    return 1
  print(
      "Grafana dashboard JSON is valid and "
      f"{len(PANEL_QUERIES) + len(TEMPLATE_VARIABLE_QUERIES)} queries "
      "are in sync."
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
