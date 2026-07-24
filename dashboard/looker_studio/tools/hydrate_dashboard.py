#!/usr/bin/env python3
"""Validate a BQAA installation and emit its Looker Studio creation URL.

The dashboard reads the BQAA-generated views, not the raw event table. The
table ID is accepted as a structural BQAA installation anchor; the view prefix
selects the generated reporting views used by the report.
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
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

BQAA_TABLE_COLUMNS = {
    "timestamp": "TIMESTAMP",
    "event_type": "STRING",
    "agent": "STRING",
    "session_id": "STRING",
    "invocation_id": "STRING",
    "user_id": "STRING",
    "trace_id": "STRING",
    "span_id": "STRING",
    "parent_span_id": "STRING",
    "content": "JSON",
    "attributes": "JSON",
    "latency_ms": "JSON",
    "status": "STRING",
    "error_message": "STRING",
    "is_truncated": "BOOL",
}


def require_identifier(label: str, value: str, pattern: re.Pattern) -> str:
  if not pattern.fullmatch(value):
    raise ValueError(f"invalid {label}: {value!r}")
  return value


def load_yaml(path: pathlib.Path) -> dict:
  value = yaml.safe_load(path.read_text())
  if not isinstance(value, dict):
    raise ValueError(f"{path}: expected a mapping")
  return value


def reject_sentinel_collisions(
    values: dict[str, str], sentinels: dict[str, str]
) -> None:
  """Reject inputs that sequential sqlReplace could mutate a second time."""
  reserved = tuple(sentinels.values())
  if not reserved or not all(
      isinstance(value, str) and value for value in reserved
  ):
    raise ValueError("template bindings contain invalid sentinel values")
  for label, value in values.items():
    if any(sentinel in value for sentinel in reserved):
      raise ValueError(f"{label} contains a reserved template sentinel")


def bq_query(project: str, location: str, sql: str) -> list[dict]:
  proc = subprocess.run(
      [
          "bq",
          f"--project_id={project}",
          f"--location={location}",
          "query",
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


def table_preflight_sql(project: str, dataset: str, table: str) -> str:
  required = ",\n    ".join(
      f"STRUCT('{name}' AS column_name, '{kind}' AS data_type)"
      for name, kind in BQAA_TABLE_COLUMNS.items()
  )
  return f"""\
WITH required AS (
  SELECT * FROM UNNEST([
    {required}
  ])
),
actual AS (
  SELECT column_name, data_type
  FROM `{project}.{dataset}`.INFORMATION_SCHEMA.COLUMNS
  WHERE table_name = '{table}'
),
object AS (
  SELECT table_type
  FROM `{project}.{dataset}`.INFORMATION_SCHEMA.TABLES
  WHERE table_name = '{table}'
)
SELECT
  '{table}' AS table_name,
  r.column_name,
  r.data_type AS expected_type,
  a.data_type AS observed_type,
  o.table_type AS observed_object_type,
  CASE
    WHEN o.table_type IS NULL THEN 'MISSING_TABLE'
    WHEN o.table_type != 'BASE TABLE' THEN 'WRONG_OBJECT_TYPE'
    WHEN a.column_name IS NULL THEN 'MISSING_COLUMN'
    ELSE 'TYPE_MISMATCH'
  END AS problem
FROM required r
CROSS JOIN (SELECT 1) anchor
LEFT JOIN object o ON TRUE
LEFT JOIN actual a USING (column_name)
WHERE o.table_type IS NULL
   OR o.table_type != 'BASE TABLE'
   OR a.column_name IS NULL
   OR a.data_type != r.data_type
ORDER BY r.column_name
"""


def view_preflight_sql(project: str, dataset: str, prefix: str) -> str:
  template = (ROOT / "sql/preflight.sql.tmpl").read_text()
  return (
      template.replace("{{PROJECT}}", project)
      .replace("{{DATASET}}", dataset)
      .replace("{{VIEW_PREFIX}}", prefix)
  )


def build_link(
    project: str,
    dataset: str,
    prefix: str,
    billing_project: str,
    report_name: str,
) -> str:
  report = load_yaml(ROOT / "bindings/report_template.yaml")
  bindings = load_yaml(ROOT / "bindings/template_bindings.yaml")
  sentinels = bindings["placeholders"]
  reject_sentinel_collisions(
      {
          "project ID": project,
          "dataset ID": dataset,
          "view prefix": prefix,
          "billing project ID": billing_project,
          "report name": report_name,
      },
      sentinels,
  )
  alias = report["data_source_alias"]
  sql_replace = ",".join(
      [
          sentinels["PROJECT"],
          project,
          sentinels["DATASET"],
          dataset,
          sentinels["VIEW_PREFIX"],
          prefix,
      ]
  )
  params = {
      "c.reportId": report["report_id"],
      "c.mode": "view",
      "r.reportName": report_name,
      f"ds.{alias}.datasourceName": f"BQAA Events — {dataset}",
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


def main() -> int:
  parser = argparse.ArgumentParser(
      description=(
          "Validate a BQAA table and its generated views, then create a "
          "configured Looker Studio dashboard link."
      )
  )
  parser.add_argument("--project", required=True)
  parser.add_argument("--dataset", required=True)
  parser.add_argument(
      "--table",
      default="agent_events",
      help="BQAA base table used for structural validation (default: agent_events)",
  )
  parser.add_argument(
      "--prefix",
      default="v",
      help="BQAA generated-view prefix (default: v)",
  )
  parser.add_argument(
      "--billing-project",
      help="BigQuery billing project (default: --project)",
  )
  parser.add_argument("--location", default="US")
  parser.add_argument("--report-name")
  args = parser.parse_args()

  try:
    project = require_identifier("project ID", args.project, PROJECT_RE)
    dataset = require_identifier("dataset ID", args.dataset, ID_RE)
    table = require_identifier("table ID", args.table, ID_RE)
    prefix = require_identifier("view prefix", args.prefix, PREFIX_RE)
    billing = require_identifier(
        "billing project ID",
        args.billing_project or project,
        PROJECT_RE,
    )
    location = require_identifier("location", args.location, LOCATION_RE)
    bindings = load_yaml(ROOT / "bindings/template_bindings.yaml")
    reject_sentinel_collisions(
        {
            "project ID": project,
            "dataset ID": dataset,
            "table ID": table,
            "view prefix": prefix,
            "billing project ID": billing,
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

  try:
    view_problems = bq_query(
        billing, location, view_preflight_sql(project, dataset, prefix)
    )
  except (OSError, RuntimeError, json.JSONDecodeError) as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    return 1
  if view_problems:
    print(
        f"ERROR: {len(view_problems)} generated-view compatibility "
        "problem(s); run the BQAA plugin with create_views=True",
        file=sys.stderr,
    )
    for problem in view_problems[:20]:
      print(
          "  - "
          f"{problem.get('problem')}: "
          f"{problem.get('view_name')}."
          f"{problem.get('required_column')}",
          file=sys.stderr,
      )
    return 1

  report_name = args.report_name or f"BigQuery Agent Analytics — {dataset}"
  link = build_link(project, dataset, prefix, billing, report_name)
  print(
      "BQAA preflight OK: base table and 15 generated views are compatible",
      file=sys.stderr,
  )
  print(link)
  return 0


if __name__ == "__main__":
  sys.exit(main())
