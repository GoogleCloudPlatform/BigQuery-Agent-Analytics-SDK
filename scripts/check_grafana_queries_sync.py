#!/usr/bin/env python3
"""Check that canonical Grafana SQL matches the dashboard's embedded queries."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
import re
import sys
from typing import Any, NamedTuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = REPOSITORY_ROOT / "grafana" / "bqaa-dashboard.json"
QUERIES_DIRECTORY = REPOSITORY_ROOT / "grafana" / "queries"
PUBLIC_DEMO_DASHBOARD_PATH = (
    REPOSITORY_ROOT / "grafana" / "bqaa-public-demo.json"
)
PUBLIC_DEMO_QUERIES_DIRECTORY = QUERIES_DIRECTORY / "public-demo"

BIGQUERY_DATASOURCE_TYPE = "grafana-bigquery-datasource"
DASHBOARD_DATASOURCE_UID = "-- Dashboard --"
# The public demo ships as an importable dashboard: the BigQuery data source is
# declared once in __inputs and every panel wires itself to that input's uid.
PUBLIC_DEMO_DATASOURCE_INPUT = "DS_BIGQUERY"
PUBLIC_DEMO_DATASOURCE = {
    "type": BIGQUERY_DATASOURCE_TYPE,
    "uid": "${" + PUBLIC_DEMO_DATASOURCE_INPUT + "}",
}

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
    21: "events_by_agent.sql",
    22: "top_errors.sql",
    23: "llm_calls_total.sql",
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

# The public demo keeps the interactive build's panel ids so the two dashboards
# stay comparable, but every panel carries its own query: Dashboard-data panels
# resolve in the browser and render empty for an anonymous viewer, so panels
# 3-5 and 20 embed standalone SQL here instead of reusing panels 2 and 12.
# Panel 19 (Trace detail) is absent by design — it would expose a raw event
# timeline — so this map has 18 entries against the interactive build's 15.
PUBLIC_DEMO_PANEL_QUERIES = {
    2: "overview_sessions.sql",
    3: "overview_events.sql",
    4: "overview_error_rate.sql",
    5: "overview_avg_llm_latency.sql",
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
    20: "total_tokens.sql",
    21: "events_by_agent.sql",
    22: "top_errors.sql",
    23: "llm_calls_total.sql",
}
# Dashboard-level settings a public build must keep. A viewer who can edit the
# dashboard, widen the time window, or pin a fast refresh can enlarge the
# BigQuery scan, and a non-empty templating.list would leave variables that the
# public build's SQL never interpolates.
PUBLIC_DEMO_PROPERTIES = {
    "editable": False,
    "refresh": "",
    "time": {"from": "now-72h", "to": "now"},
    "timepicker.hidden": True,
    "timepicker.refresh_intervals": [],
    "templating.list": [],
}
# Panel 19 (Trace detail) renders a raw event timeline, so the public build
# must not carry it at all, under any data source.
PUBLIC_DEMO_EXCLUDED_PANEL = 19
# The public build has no time picker, so each query freezes its own half-open
# window. Both sides are required: with only the lower bound, a future-dated
# event is reported before it happens and future partitions stay eligible for
# the scan, so "Last 72 hours" would not be the truth.
PUBLIC_DEMO_TIME_PREDICATES = (
    "timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)",
    "timestamp < CURRENT_TIMESTAMP()",
)
# Grafana interpolation syntax: variables, macros and the legacy variable
# form. The public build interpolates nothing, so its SQL must run as written.
PUBLIC_DEMO_FORBIDDEN_SYNTAX = ("${", "$__", "[[")
# The public SQL ships with placeholders instead of a real table, and they must
# stay inside a backticked path so no half-substituted identifier can leak.
PUBLIC_DEMO_TABLE_PLACEHOLDER = "YOUR_PROJECT_ID.YOUR_DATASET_ID"
# Backticks are optional in BigQuery whenever the identifiers need no escaping,
# so `FROM project.dataset.table` names a real table without ever touching one.
# Match the dotted path after FROM or JOIN so the foreign-path check below sees
# the unquoted form too. Public demo queries are SELECT-only, so those two
# keywords cover every table reference they can make. The capture starts at an
# identifier character, so its caller strips backticks first: that is what makes
# a mixed `project`.`dataset`.`table` — dots outside the quotes, invisible to a
# search for a dotted path between two backticks — read as the bare form here.
UNQUOTED_TABLE_PATH = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+)", re.IGNORECASE
)


def normalize(query: str) -> str:
  """Ignore only harmless whitespace at the beginning and end."""

  return query.strip()


def fail(message: str) -> None:
  print(f"ERROR: {message}", file=sys.stderr)


def repository_path(path: Path) -> str:
  """Render a path relative to the repository root for use in messages."""

  return path.relative_to(REPOSITORY_ROOT).as_posix()


def lookup(dashboard: dict[str, Any], dotted_key: str) -> Any:
  """Read a top-level or one-level-nested dashboard property.

  Returns None when an intermediate value is missing or is not an object, so a
  malformed dashboard reports a property mismatch rather than raising.
  """

  value: Any = dashboard
  for key in dotted_key.split("."):
    if not isinstance(value, dict):
      return None
    value = value.get(key)
  return value


def check_unmapped_queries(
    directory: Path, mapped_files: set[str], map_names: str
) -> int:
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

  directory_label = repository_path(directory)
  try:
    sql_files = {path.name for path in directory.glob("*.sql")}
  except OSError as error:
    fail(f"cannot list canonical queries in {directory}: {error}")
    return 1

  unmapped = sorted(sql_files - mapped_files)
  if not unmapped:
    return 0
  border = "  " + "*" * 64
  title = ("* ERROR: unmapped SQL files in " + directory_label + "/").ljust(63)
  body = (
      f"  The .sql file(s) below are missing from {map_names}, so they are\n"
      "  never validated against the dashboard. Do NOT rely on this script to\n"
      "  catch every drift until a query map is updated to cover them:\n"
      + "".join(f"    - {name}\n" for name in unmapped)
  )
  print(
      f"\n{border}\n  {title}*\n{border}\n{body}{border}",
      file=sys.stderr,
  )
  return len(unmapped)


def read_canonical_queries(
    directory: Path, filenames: set[str]
) -> tuple[dict[str, str], int]:
  """Read every mapped canonical .sql file once, up front.

  Both the panel comparison and the allValue check need this text, so reading
  it here keeps each file to a single read instead of one read per comparison.

  Returns the file contents keyed by filename plus the number of unreadable
  files; a filename missing from the mapping has already been reported.
  """

  queries: dict[str, str] = {}
  errors = 0
  for filename in sorted(filenames):
    path = directory / filename
    try:
      queries[filename] = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
      fail(f"cannot read canonical query {repository_path(path)}: {error}")
      errors += 1
  return queries, errors


def load_dashboard(path: Path) -> dict[str, Any]:
  try:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as error:
    raise ValueError(f"cannot load valid JSON from {path}: {error}") from error

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


def collect_panels(dashboard: dict[str, Any]) -> tuple[dict[int, Any], int]:
  """Index every panel by id, reporting malformed and duplicated entries."""

  panels: dict[int, dict[str, Any]] = {}
  errors = 0
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
  return panels, errors


def check_sql_matches(
    embedded_query: str,
    canonical_query: str,
    source_label: str,
    description: str,
) -> int:
  """Compare embedded SQL with its canonical text, diffing any drift."""

  if normalize(embedded_query) == normalize(canonical_query):
    return 0
  fail(f"{description} has drifted from {source_label}")
  diff = difflib.unified_diff(
      normalize(canonical_query).splitlines(),
      normalize(embedded_query).splitlines(),
      fromfile=source_label,
      tofile=description,
      lineterm="",
  )
  print("\n".join(diff), file=sys.stderr)
  return 1


def check_panel_queries(
    panels: dict[int, Any],
    panel_queries: dict[int, str],
    directory: Path,
    canonical_queries: dict[str, str],
) -> int:
  """Verify each mapped panel embeds exactly its canonical query, verbatim."""

  errors = 0
  for panel_id, filename in panel_queries.items():
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
    target = targets[0]
    embedded_query = target.get("rawSql") if isinstance(target, dict) else None
    if not isinstance(embedded_query, str):
      fail(f"panel id {panel_id} has no string rawSql for {filename}")
      errors += 1
      continue
    canonical_query = canonical_queries.get(filename)
    if canonical_query is None:
      # read_canonical_queries already reported the read failure.
      continue
    errors += check_sql_matches(
        embedded_query,
        canonical_query,
        repository_path(directory / filename),
        f"dashboard panel {panel_id} rawSql",
    )
  return errors


def check_bigquery_panels_mapped(
    panels: dict[int, Any], panel_queries: dict[int, str], map_name: str
) -> int:
  """Reverse check: every panel that queries BigQuery must be in the map.

  The forward check only visits mapped panel ids, so a panel added to the JSON
  and forgotten in the map would never be compared against a canonical file.
  Every panel object is inspected regardless of its declared type: a query
  target still runs when the panel calls itself a row.
  """

  errors = 0
  for panel_id, panel in panels.items():
    targets = panel.get("targets", [])
    if not isinstance(targets, list):
      continue
    if any(
        isinstance(target, dict) and isinstance(target.get("rawSql"), str)
        for target in targets
    ):
      if panel_id not in panel_queries:
        fail(
            f"panel id {panel_id} queries BigQuery but is missing from "
            f"{map_name}"
        )
        errors += 1
  return errors


def active_sql(query: str) -> str:
  """Return the query with `--` and `#` lines and `/* */` blocks stripped.

  A commented-out line still satisfies a plain substring search, so the public
  demo's SQL policy is checked against this comment-free view instead: a
  70-hour predicate with the required 72-hour one parked in a comment above it
  must not pass. BigQuery honours `#` as a line comment alongside `--`, so a
  bound parked behind either one is stripped the same way. Neither strip knows
  about string literals, so a `/*`, `--` or `#` inside quotes would read as a
  comment start; no shipped query has one.
  """

  query = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
  return "\n".join(
      re.split(r"--|#", line, maxsplit=1)[0] for line in query.splitlines()
  )


def check_public_demo_queries(canonical_queries: dict[str, str]) -> int:
  """Lint the shipped public demo SQL against its documented conventions.

  These queries run for anonymous viewers with no time picker and no template
  variables, so each one has to bound its own scan, interpolate nothing, and
  name its table through the shipped placeholders only.

  Works on comment-stripped text: it catches an edit that drops a bound, leaves
  a variable in, or pastes a real table path, not SQL written to slip past it
  (`WHERE <bound> OR TRUE` still reads as bounded). Review of these files, not
  this script, clears the public dashboard.
  """

  errors = 0
  for filename, query in sorted(canonical_queries.items()):
    label = repository_path(PUBLIC_DEMO_QUERIES_DIRECTORY / filename)
    executable = active_sql(query)

    for syntax in PUBLIC_DEMO_FORBIDDEN_SYNTAX:
      if syntax in executable:
        fail(
            f"public demo query {label} must not use Grafana interpolation "
            f"syntax {syntax!r}"
        )
        errors += 1

    # Every file names exactly one backticked placeholder path per table it
    # scans: tool_errors.sql UNIONs two, the other seventeen read one. That
    # count is therefore the number of branches each time bound must appear in,
    # which is what stops a new UNION arm from shipping unbounded.
    table_scans = executable.count("`" + PUBLIC_DEMO_TABLE_PLACEHOLDER + ".")
    if not table_scans or table_scans != executable.count(
        PUBLIC_DEMO_TABLE_PLACEHOLDER
    ):
      fail(
          f"public demo query {label} must name every table as "
          f"`{PUBLIC_DEMO_TABLE_PLACEHOLDER}.<table>`"
      )
      errors += 1
      continue

    # The count check above passes as long as the placeholders are backticked;
    # it says nothing about a second path alongside them, quoted or not. A
    # UNION arm naming a real project would otherwise ship in a query anonymous
    # viewers run.
    candidate_paths = re.findall(r"`([^`]*\.[^`]*)`", executable)
    candidate_paths += UNQUOTED_TABLE_PATH.findall(executable.replace("`", ""))
    foreign_paths = sorted(
        {
            path
            for path in candidate_paths
            if not path.startswith(PUBLIC_DEMO_TABLE_PLACEHOLDER + ".")
        }
    )
    if foreign_paths:
      fail(
          f"public demo query {label} names {foreign_paths} as a table path: "
          "every dotted path, backticked or not, must start with "
          f"{PUBLIC_DEMO_TABLE_PLACEHOLDER + '.'!r}"
      )
      errors += 1
      continue

    for predicate in PUBLIC_DEMO_TIME_PREDICATES:
      found = sum(predicate in line for line in executable.splitlines())
      if found != table_scans:
        fail(
            f"public demo query {label} scans {table_scans} table(s) but "
            f"carries {found} uncommented {predicate!r} predicate(s): every "
            "table-scan branch must freeze the same half-open 72-hour window"
        )
        errors += 1
  return errors


def find_dashboard_data_panels(panels: dict[int, Any]) -> list[int]:
  """Return the ids of panels wired to Grafana's Dashboard data source.

  Such a panel reuses another panel's result instead of querying BigQuery. The
  two builds allow different sets of them, so each caller decides which ids are
  acceptable.
  """

  return sorted(
      panel_id
      for panel_id, panel in panels.items()
      if lookup(panel, "datasource.uid") == DASHBOARD_DATASOURCE_UID
  )


class DashboardCheck(NamedTuple):
  """What the shared pipeline found, for the caller's own checks to reuse."""

  errors: int
  dashboard: dict[str, Any] | None
  panels: dict[int, Any]
  canonical_queries: dict[str, str]


def check_dashboard_sync(
    dashboard_path: Path,
    queries_directory: Path,
    panel_queries: dict[int, str],
    panel_map_name: str,
    mapped_files: set[str] | None = None,
    map_names: str | None = None,
) -> DashboardCheck:
  """Run the checks both builds share: unmapped files through panel SQL sync.

  Covers the unmapped-file scan, the dashboard load, panel indexing, the
  reverse BigQuery-panel mapping and the panel-versus-file comparison. Callers
  add the policy that applies only to their build and reuse the returned
  dashboard, panels and canonical query text instead of re-reading them.

  mapped_files and map_names default to the panel map alone; the interactive
  dashboard widens them to cover its template-variable queries too.
  """

  if mapped_files is None:
    mapped_files = set(panel_queries.values())
  errors = check_unmapped_queries(
      queries_directory, mapped_files, map_names or panel_map_name
  )

  try:
    dashboard = load_dashboard(dashboard_path)
  except ValueError as error:
    fail(str(error))
    return DashboardCheck(errors + 1, None, {}, {})

  canonical_queries, read_errors = read_canonical_queries(
      queries_directory, mapped_files
  )
  errors += read_errors

  panels, panel_errors = collect_panels(dashboard)
  errors += panel_errors
  errors += check_bigquery_panels_mapped(panels, panel_queries, panel_map_name)
  errors += check_panel_queries(
      panels, panel_queries, queries_directory, canonical_queries
  )
  return DashboardCheck(errors, dashboard, panels, canonical_queries)


def check_main_dashboard() -> int:
  """Check the interactive dashboard against grafana/queries/*.sql.

  On top of the shared sync, the interactive build owns the template variables:
  their SQL must match its canonical file, and the panels that reuse another
  panel's result must stay wired to the source panel they are mapped to.
  """

  check = check_dashboard_sync(
      DASHBOARD_PATH,
      QUERIES_DIRECTORY,
      PANEL_QUERIES,
      "PANEL_QUERIES",
      set(PANEL_QUERIES.values()) | set(TEMPLATE_VARIABLE_QUERIES.values()),
      "PANEL_QUERIES or TEMPLATE_VARIABLE_QUERIES",
  )
  errors = check.errors
  if check.dashboard is None:
    return errors

  templating = check.dashboard.get("templating", {})
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
        or datasource.get("type") != BIGQUERY_DATASOURCE_TYPE
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
    # Grafana keeps two copies of a query variable's SQL. Require them to be
    # byte-identical: whitespace-only drift between the copies still means the
    # JSON was hand-edited in one place and not the other.
    if definition != raw_sql:
      fail(
          f"template variable {variable_name} definition has drifted from "
          "query.rawSql"
      )
      errors += 1
    canonical_query = check.canonical_queries.get(filename)
    if canonical_query is None:
      # read_canonical_queries already reported the read failure.
      continue
    errors += check_sql_matches(
        raw_sql,
        canonical_query,
        repository_path(QUERIES_DIRECTORY / filename),
        f"dashboard variable {variable_name} query.rawSql",
    )

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
    for filename in PANEL_QUERIES.values():
      canonical_query = check.canonical_queries.get(filename)
      if canonical_query is None:
        continue
      if interpolation in canonical_query and all_value not in canonical_query:
        fail(
            f"canonical query {filename} uses {interpolation} but does not "
            f"contain the variable's allValue sentinel {all_value!r}"
        )
        errors += 1

  for panel_id in find_dashboard_data_panels(check.panels):
    if panel_id not in DASHBOARD_DATA_PANEL_SOURCES:
      fail(
          f"panel id {panel_id} uses the Dashboard datasource but is missing "
          "from DASHBOARD_DATA_PANEL_SOURCES"
      )
      errors += 1

  for panel_id, source_panel_id in DASHBOARD_DATA_PANEL_SOURCES.items():
    panel = check.panels.get(panel_id, {})
    targets = panel.get("targets")
    has_single_target = isinstance(targets, list) and len(targets) == 1
    target = targets[0] if has_single_target else None
    if (
        lookup(panel, "datasource.uid") != DASHBOARD_DATASOURCE_UID
        or not isinstance(target, dict)
        or target.get("panelId") != source_panel_id
        or lookup(target, "datasource.uid") != DASHBOARD_DATASOURCE_UID
    ):
      fail(
          f"panel id {panel_id} must use Dashboard data from panel "
          f"{source_panel_id}"
      )
      errors += 1

  return errors


def check_public_demo_dashboard() -> int:
  """Check the public demo build against grafana/queries/public-demo/*.sql.

  The public build is shared with anonymous viewers, so on top of the same
  bidirectional SQL sync its queries must satisfy the public SQL policy, its
  panels must stay aligned with their interactive counterparts and exclude the
  trace-detail panel, and it must keep the dashboard-level settings that bound
  what a viewer can make BigQuery scan and wire its data source through the
  import input.
  """

  check = check_dashboard_sync(
      PUBLIC_DEMO_DASHBOARD_PATH,
      PUBLIC_DEMO_QUERIES_DIRECTORY,
      PUBLIC_DEMO_PANEL_QUERIES,
      "PUBLIC_DEMO_PANEL_QUERIES",
  )
  errors = check.errors + check_public_demo_queries(check.canonical_queries)

  # Every public panel keeps the id of the interactive panel it was derived
  # from, whether that panel embeds SQL or reuses another panel's result.
  unaligned = sorted(
      set(PUBLIC_DEMO_PANEL_QUERIES)
      - set(PANEL_QUERIES)
      - set(DASHBOARD_DATA_PANEL_SOURCES)
  )
  if unaligned:
    fail(
        f"public demo panel id(s) {unaligned} have no counterpart in "
        "PANEL_QUERIES or DASHBOARD_DATA_PANEL_SOURCES"
    )
    errors += 1

  if check.dashboard is None:
    return errors

  if PUBLIC_DEMO_EXCLUDED_PANEL in check.panels:
    fail(
        f"public demo panel id {PUBLIC_DEMO_EXCLUDED_PANEL} (trace detail) "
        "exposes a raw event timeline and must be absent from the public build"
    )
    errors += 1

  for dotted_key, expected in PUBLIC_DEMO_PROPERTIES.items():
    actual = lookup(check.dashboard, dotted_key)
    # Compare types too: Python treats False == 0 and True == 1, so an
    # integer written where a boolean belongs would otherwise pass.
    if actual != expected or type(actual) is not type(expected):
      fail(
          f"public demo dashboard.{dotted_key} must be {expected!r}, "
          f"found {actual!r}"
      )
      errors += 1

  # The BigQuery data source is declared once as an import input; every panel
  # below is then checked to wire itself to that declaration.
  inputs = check.dashboard.get("__inputs")
  declarations = [
      entry
      for entry in (inputs if isinstance(inputs, list) else [])
      if isinstance(entry, dict)
      and entry.get("name") == PUBLIC_DEMO_DATASOURCE_INPUT
      and entry.get("type") == "datasource"
      and entry.get("pluginId") == BIGQUERY_DATASOURCE_TYPE
  ]
  if len(declarations) != 1:
    fail(
        "public demo dashboard.__inputs must declare exactly one "
        f"{PUBLIC_DEMO_DATASOURCE_INPUT} datasource input with pluginId "
        f"{BIGQUERY_DATASOURCE_TYPE}"
    )
    errors += 1

  # Dashboard-data panels resolve in the browser against another panel's
  # result, which never runs for an anonymous viewer, so the public build
  # allows none at all.
  for panel_id in find_dashboard_data_panels(check.panels):
    fail(
        f"public demo panel id {panel_id} uses the Dashboard datasource, "
        "which renders empty for anonymous viewers"
    )
    errors += 1

  for panel_id in PUBLIC_DEMO_PANEL_QUERIES:
    panel = check.panels.get(panel_id)
    if panel is None:
      # check_panel_queries already reported the missing panel.
      continue
    targets = panel.get("targets")
    target = targets[0] if isinstance(targets, list) and targets else {}
    if (
        panel.get("datasource") != PUBLIC_DEMO_DATASOURCE
        or not isinstance(target, dict)
        or target.get("datasource") != PUBLIC_DEMO_DATASOURCE
    ):
      fail(
          f"public demo panel id {panel_id} and its target must both use "
          f"datasource {PUBLIC_DEMO_DATASOURCE!r}"
      )
      errors += 1

  return errors


def main() -> int:
  """
  Validates the integrity and synchronization of the Grafana dashboard queries.

  This CI script strictly enforces these key conditions, for the interactive
  dashboard against grafana/queries/ and for the public demo build against
  grafana/queries/public-demo/:
  1. Drift Prevention: The 'rawSql' inside the JSON dashboard exactly matches
     the canonical '.sql' files in the queries directory (printing unified diffs on failure).
  2. Unmapped File Detection: Every '.sql' file in the queries directory is
     explicitly registered in the PANEL_QUERIES dictionary.
  3. Reverse Mapping Detection: Every BigQuery panel in the JSON dashboard
     is explicitly registered in the PANEL_QUERIES dictionary.
  4. Dashboard Datasource Validation: Every panel querying the '-- Dashboard --'
     datasource is explicitly registered in DASHBOARD_DATA_PANEL_SOURCES, and
     the public demo build carries no such panel at all.
  5. Template Variable Reverse Mapping: Every BigQuery query variable in the
     dashboard is explicitly registered in TEMPLATE_VARIABLE_QUERIES.
  6. Template Variable Synchronization: Query variables match their canonical
     files and their definition and query.rawSql copies match each other.
  7. Public Demo Safety: The public build keeps the settings that bound an
     anonymous viewer's BigQuery scan, and declares and wires the BigQuery
     data source through its import input.
  8. Public Demo SQL Policy: Every public query bounds its own scan with both
     sides of the hard half-open 72-hour window, outside any comment and as
     many times over the file as it scans tables, interpolates no Grafana
     variable or macro, and
     names its tables through backticked placeholder paths only. Its panels map
     onto interactive counterparts and exclude the trace-detail panel.
  """
  errors = check_main_dashboard()
  errors += check_public_demo_dashboard()

  if errors:
    print(
        f"Grafana query sync check failed with {errors} error(s).",
        file=sys.stderr,
    )
    return 1
  interactive_queries = len(PANEL_QUERIES) + len(TEMPLATE_VARIABLE_QUERIES)
  print(
      "Grafana dashboard JSON is valid and "
      f"{interactive_queries + len(PUBLIC_DEMO_PANEL_QUERIES)} queries "
      f"are in sync ({interactive_queries} interactive, "
      f"{len(PUBLIC_DEMO_PANEL_QUERIES)} public demo)."
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
