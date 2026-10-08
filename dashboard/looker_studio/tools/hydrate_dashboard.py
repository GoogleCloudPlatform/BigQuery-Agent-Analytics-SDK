#!/usr/bin/env python3
"""Validate a BQAA installation and emit its Looker Studio creation URL.

The dashboard reads one BQAA agent-events table and does not require the
optional generated analytics views.

``--profile bqca`` targets a BigQuery Conversational Analytics (BQCA) Prompt
& Response Logging table. BQCA writes the BQAA base-table schema, so the same
structural preflight gates it; an advisory, byte-capped 30-day data profile
over the nine BQCA event types then reports whether the table actually holds
BQCA logs with data-agent attribution. ``--custom-sql-out`` writes the
profile's reporting query bound to your table for a Looker Studio custom-query
data source.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import urllib.parse

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Per-profile files. Both profiles share the structural preflight because
# BQCA writes the BQAA base-table schema; only BQCA has a data profile.
PROFILES = {
    "adk": {
        "report": "bindings/report_template.yaml",
        "bindings": "bindings/template_bindings.yaml",
        "events_sql": "sql/events_v1.sql.tmpl",
        "data_profile_sql": None,
        "default_table": "agent_events",
        "label": "BQAA",
    },
    "bqca": {
        "report": "bindings/bqca_report_template.yaml",
        "bindings": "bindings/bqca_template_bindings.yaml",
        "events_sql": "sql/bqca_events_v1.sql.tmpl",
        "data_profile_sql": "sql/bqca_preflight.sql.tmpl",
        "default_table": None,  # bindings/bqca_report_template.yaml
        "label": "BQCA",
    },
}
# The advisory BQCA data profile reads three columns over 30 days; the cap
# turns an unexpectedly large scan into a warning instead of a surprise bill.
DEFAULT_PROFILE_MAX_BYTES = 10 * 1024**3


def require_identifier(label: str, value: str, pattern: re.Pattern) -> str:
  if not pattern.fullmatch(value):
    raise ValueError(f"invalid {label}: {value!r}")
  return value


def load_yaml(path: pathlib.Path) -> dict:
  value = yaml.safe_load(path.read_text())
  if not isinstance(value, dict):
    raise ValueError(f"{path}: expected a mapping")
  return value


def profile_default_table(profile: str = "adk") -> str:
  configured = PROFILES[profile]["default_table"]
  if configured:
    return configured
  return load_yaml(ROOT / PROFILES[profile]["report"])["default_table"]


def reject_sentinel_collisions(
    replacements: dict[str, str], sentinels: dict[str, str]
) -> None:
  """Reject only values that a later sqlReplace pair would mutate.

  A replacement may safely equal or contain its own sentinel because that
  pair has already consumed the input when the replacement text is inserted.
  It may also contain an earlier sentinel. A later sentinel is unsafe because
  the subsequent pair would rewrite part of the inserted identifier.
  """
  order = ("PROJECT", "DATASET", "TABLE")
  if set(replacements) != set(order):
    raise ValueError("replacement values do not match template bindings")
  reserved = tuple(sentinels.get(name) for name in order)
  if not all(isinstance(value, str) and value for value in reserved):
    raise ValueError("template bindings contain invalid sentinel values")
  if len(set(reserved)) != len(reserved):
    raise ValueError("template bindings contain duplicate sentinel values")
  for index, name in enumerate(order):
    value = replacements[name]
    if any(sentinel in value for sentinel in reserved[index + 1 :]):
      raise ValueError(
          f"{name.lower()} contains a later reserved template sentinel"
      )


def bq_query(
    project: str,
    location: str,
    sql: str,
    maximum_bytes_billed: int | None = None,
) -> list[dict]:
  query_flags = ["query"]
  if maximum_bytes_billed is not None:
    query_flags.append(f"--maximum_bytes_billed={maximum_bytes_billed}")
  proc = subprocess.run(
      [
          "bq",
          f"--project_id={project}",
          f"--location={location}",
          *query_flags,
          "--nouse_legacy_sql",
          "--format=json",
          "--max_rows=10000",
      ],
      input=sql,
      capture_output=True,
      text=True,
  )
  if proc.returncode:
    diagnostic = (proc.stderr or proc.stdout).strip()[:800]
    raise RuntimeError(f"BigQuery preflight failed: {diagnostic}")
  result = json.loads(proc.stdout or "[]")
  if not isinstance(result, list):
    raise RuntimeError("BigQuery preflight returned a non-list result")
  return result


def _bind(template_path: str, project: str, dataset: str, table: str) -> str:
  template = (ROOT / template_path).read_text()
  return (
      template.replace("{{PROJECT}}", project)
      .replace("{{DATASET}}", dataset)
      .replace("{{TABLE}}", table)
  )


def table_preflight_sql(project: str, dataset: str, table: str) -> str:
  return _bind("sql/preflight.sql.tmpl", project, dataset, table)


def data_profile_sql(
    project: str, dataset: str, table: str, profile: str = "bqca"
) -> str | None:
  """Return the profile's advisory data-profile query, if it has one."""
  template = PROFILES[profile]["data_profile_sql"]
  return _bind(template, project, dataset, table) if template else None


def custom_query_sql(
    project: str, dataset: str, table: str, profile: str = "adk"
) -> str:
  """Return the profile's reporting query bound to one validated table."""
  source = PROFILES[profile]["events_sql"]
  return (
      f"-- Generated by tools/hydrate_dashboard.py --profile {profile} from"
      f" {source} for {project}.{dataset}.{table}.\n"
      + _bind(source, project, dataset, table)
  )


def build_link(
    project: str,
    dataset: str,
    table: str,
    billing_project: str,
    report_name: str,
    profile: str = "adk",
) -> str:
  files = PROFILES[profile]
  report = load_yaml(ROOT / files["report"])
  bindings = load_yaml(ROOT / files["bindings"])
  sentinels = bindings["placeholders"]
  reject_sentinel_collisions(
      {
          "PROJECT": project,
          "DATASET": dataset,
          "TABLE": table,
      },
      sentinels,
  )
  alias = report["data_source_alias"]
  datasource_prefix = report.get("datasource_name_prefix", files["label"])
  sql_replace = ",".join(
      [
          sentinels["PROJECT"],
          project,
          sentinels["DATASET"],
          dataset,
          sentinels["TABLE"],
          table,
      ]
  )
  params = {
      "c.reportId": report["report_id"],
      "c.mode": "view",
      "r.reportName": report_name,
      f"ds.{alias}.datasourceName": f"{datasource_prefix} Events — {dataset}",
      f"ds.{alias}.billingProjectId": billing_project,
      f"ds.{alias}.sqlReplace": sql_replace,
      # Every supported installation yields the same stable 30-field schema.
      # Keeping fields preserves the report's calculated field types.
      f"ds.{alias}.refreshFields": "false",
  }
  return (
      "https://lookerstudio.google.com/reporting/create?"
      + urllib.parse.urlencode(params)
  )


def default_report_name(dataset: str, profile: str = "adk") -> str:
  if profile == "adk":
    return f"BigQuery Agent Analytics — {dataset}"
  report = load_yaml(ROOT / PROFILES[profile]["report"])
  return f"{report['default_report_name']} — {dataset}"


def summarize_data_profile(rows: list[dict]) -> tuple[list[str], list[str]]:
  """Return (summary lines, warning lines) for BQCA data-profile rows."""

  def count(row: dict, key: str) -> int:
    return int(float(row.get(key) or 0))

  total = sum(count(row, "events_30d") for row in rows)
  attributed = sum(count(row, "events_with_data_agent_id") for row in rows)
  fast_path = sum(count(row, "fast_path_events") for row in rows)
  per_type = ", ".join(
      f"{row.get('event_type')}={count(row, 'events_30d')}" for row in rows
  )
  coverage = attributed / total if total else 0.0
  summary = [
      f"BQCA data profile (last 30 days): {total} events ({per_type})",
      f"  data-agent attribution: {attributed} of {total} events"
      f" ({coverage:.1%}); fast-path events: {fast_path}",
  ]
  warnings = []
  if not total:
    warnings.append(
        "WARNING: no BQCA Prompt & Response Logging events in the last 30"
        " days; the dashboard stays empty until your data agents log to this"
        " table. Check the table ID or use --profile adk for ADK agents."
    )
  elif not attributed:
    warnings.append(
        "WARNING: no event carries a data-agent ID in"
        ' attributes.session_metadata.state."data-agent-id"; per-agent'
        " breakdowns in the BQCA custom query will read 'unattributed'."
    )
  return summary, warnings


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description=(
          "Validate a BQAA event table, then create a configured Looker "
          "Studio dashboard link."
      )
  )
  parser.add_argument("--project", required=True)
  parser.add_argument("--dataset", required=True)
  parser.add_argument(
      "--profile",
      choices=tuple(PROFILES),
      default="adk",
      help=(
          "dashboard surface: adk (ADK agents, default) or bqca (BigQuery "
          "Conversational Analytics Prompt & Response Logging)"
      ),
  )
  parser.add_argument(
      "--table",
      help=(
          "BQAA event table queried by the dashboard (default: agent_events;"
          " bqca_prompt_response_logs with --profile bqca)"
      ),
  )
  parser.add_argument(
      "--billing-project",
      help="BigQuery billing project (default: --project)",
  )
  parser.add_argument("--location", default="US")
  parser.add_argument("--report-name")
  parser.add_argument(
      "--custom-sql-out",
      type=pathlib.Path,
      help=(
          "also write the profile's reporting query, bound to this table, for"
          " a Looker Studio custom-query data source"
      ),
  )
  parser.add_argument(
      "--maximum-bytes-billed",
      type=int,
      default=DEFAULT_PROFILE_MAX_BYTES,
      help=(
          "byte cap for the advisory BQCA data profile (default: 10 GiB);"
          " a larger scan is skipped with a warning"
      ),
  )
  parser.add_argument(
      "--skip-data-profile",
      action="store_true",
      help="skip the advisory BQCA data profile (no table data is read)",
  )
  args = parser.parse_args(argv)
  profile = args.profile

  try:
    project = require_identifier("project ID", args.project, PROJECT_RE)
    dataset = require_identifier("dataset ID", args.dataset, DATASET_RE)
    table = require_identifier(
        "table ID", args.table or profile_default_table(profile), TABLE_RE
    )
    billing = require_identifier(
        "billing project ID",
        args.billing_project or project,
        PROJECT_RE,
    )
    location = require_identifier("location", args.location, LOCATION_RE)
    if args.maximum_bytes_billed <= 0:
      raise ValueError("--maximum-bytes-billed must be positive")
    bindings = load_yaml(ROOT / PROFILES[profile]["bindings"])
    reject_sentinel_collisions(
        {
            "PROJECT": project,
            "DATASET": dataset,
            "TABLE": table,
        },
        bindings["placeholders"],
    )
  except ValueError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    return 2

  try:
    table_problems = bq_query(
        billing, location, table_preflight_sql(project, dataset, table)
    )
  except (OSError, RuntimeError, json.JSONDecodeError) as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    return 1
  if table_problems:
    print(
        f"ERROR: {project}.{dataset}.{table} is not a compatible BQAA "
        f"base table ({len(table_problems)} problem(s))",
        file=sys.stderr,
    )
    for problem in table_problems[:20]:
      print(
          "  - "
          f"{problem.get('problem')}: "
          f"{problem.get('column_name')} "
          f"(expected {problem.get('expected_type')}, "
          f"observed {problem.get('observed_type')})",
          file=sys.stderr,
      )
    return 1

  label = PROFILES[profile]["label"]
  print(
      f"{label} preflight OK: base event table is compatible; no views"
      " required",
      file=sys.stderr,
  )
  profile_sql = data_profile_sql(project, dataset, table, profile)
  if profile_sql and args.skip_data_profile:
    print("BQCA data profile skipped (--skip-data-profile)", file=sys.stderr)
  elif profile_sql:
    try:
      rows = bq_query(
          billing,
          location,
          profile_sql,
          maximum_bytes_billed=args.maximum_bytes_billed,
      )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
      # Advisory only: the structural gate above already passed.
      print(f"WARNING: BQCA data profile not run: {exc}", file=sys.stderr)
    else:
      summary, warnings = summarize_data_profile(rows)
      for line in summary + warnings:
        print(line, file=sys.stderr)

  if args.custom_sql_out:
    try:
      args.custom_sql_out.write_text(
          custom_query_sql(project, dataset, table, profile)
      )
    except OSError as exc:
      print(f"ERROR: {exc}", file=sys.stderr)
      return 1
    print(
        f"Wrote the {label} custom query to {args.custom_sql_out}; enable"
        " date range parameters on that data source",
        file=sys.stderr,
    )

  report_name = args.report_name or default_report_name(dataset, profile)
  link = build_link(
      project, dataset, table, billing, report_name, profile=profile
  )
  if profile == "bqca":
    print(
        "NOTE: BQCA uses the dedicated 7-page tool-free BQCA template"
        f" (report {load_yaml(ROOT / PROFILES['bqca']['report'])['report_id']},"
        " alias ds0) over sql/bqca_events_v1.template.sql",
        file=sys.stderr,
    )
  print(
      "SECURITY: keep the new report private until Resource > Manage added "
      "data sources > Edit shows Data credentials: Viewer",
      file=sys.stderr,
  )
  print(link)
  return 0


if __name__ == "__main__":
  sys.exit(main())
