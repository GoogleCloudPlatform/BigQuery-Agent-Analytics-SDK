#!/usr/bin/env python3
"""Validate a BQAA installation and emit its Looker Studio creation URL.

The dashboard reads one BQAA agent-events table and does not require the
optional generated analytics views.
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
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


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
  template = (ROOT / "sql/preflight.sql.tmpl").read_text()
  return (
      template.replace("{{PROJECT}}", project)
      .replace("{{DATASET}}", dataset)
      .replace("{{TABLE}}", table)
  )


def build_link(
    project: str,
    dataset: str,
    table: str,
    billing_project: str,
    report_name: str,
) -> str:
  report = load_yaml(ROOT / "bindings/report_template.yaml")
  bindings = load_yaml(ROOT / "bindings/template_bindings.yaml")
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
          "Validate a BQAA event table, then create a configured Looker "
          "Studio dashboard link."
      )
  )
  parser.add_argument("--project", required=True)
  parser.add_argument("--dataset", required=True)
  parser.add_argument(
      "--table",
      default="agent_events",
      help="BQAA event table queried by the dashboard (default: agent_events)",
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
    billing = require_identifier(
        "billing project ID",
        args.billing_project or project,
        PROJECT_RE,
    )
    location = require_identifier("location", args.location, LOCATION_RE)
    bindings = load_yaml(ROOT / "bindings/template_bindings.yaml")
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

  report_name = args.report_name or f"BigQuery Agent Analytics — {dataset}"
  link = build_link(project, dataset, table, billing, report_name)
  print(
      "BQAA preflight OK: base event table is compatible; no views required",
      file=sys.stderr,
  )
  print(link)
  return 0


if __name__ == "__main__":
  sys.exit(main())
