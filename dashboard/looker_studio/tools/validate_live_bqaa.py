#!/usr/bin/env python3
"""Execute every dashboard query contract on a live BQAA dataset.

This is a read-only compatibility smoke test, not parity certification. The
optional receipt records query hashes, row counts, and BigQuery job IDs, but
never query results, source identifiers, prompts, user IDs, traces, or errors.
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import uuid

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FILTER_PARAMS = (
    "filter_agent",
    "filter_user_id",
    "filter_trace_id",
    "filter_span_id",
    "filter_tool_name",
)
def require_identifier(label: str, value: str, pattern: re.Pattern) -> str:
  if not pattern.fullmatch(value):
    raise ValueError(f"invalid {label}: {value!r}")
  return value


def sha256_file(path: str | pathlib.Path) -> str:
  return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def window_for(chart: dict, scenario_end: str) -> tuple[str, str]:
  days = 7 if chart["source_dashboard"] == "performance" else 14
  end = datetime.date.fromisoformat(scenario_end)
  start = end - datetime.timedelta(days=days - 1)
  return start.isoformat(), end.isoformat()


def run_query(
    sql_path: pathlib.Path,
    source_project: str,
    billing_project: str,
    dataset: str,
    table: str,
    location: str,
    start: str,
    end: str,
) -> tuple[list[dict], str]:
  sql = (
      sql_path.read_text()
      .replace("{{PROJECT}}", source_project)
      .replace("{{DATASET}}", dataset)
      .replace("{{TABLE}}", table)
  )
  job_id = f"looker_studio_smoke_{uuid.uuid4().hex}"
  command = [
      "bq",
      f"--project_id={billing_project}",
      f"--location={location}",
      "query",
      "--nouse_legacy_sql",
      "--format=json",
      "--max_rows=100000",
      f"--job_id={job_id}",
      f"--parameter=start_date:DATE:{start}",
      f"--parameter=end_date:DATE:{end}",
  ]
  for parameter in FILTER_PARAMS:
    command.append(f"--parameter={parameter}:ARRAY<STRING>:[]")
  process = subprocess.run(
      command,
      input=sql,
      capture_output=True,
      text=True,
      check=False,
  )
  if process.returncode:
    raise RuntimeError(f"{sql_path.name}: {process.stderr.strip()[:400]}")
  result = json.loads(process.stdout or "[]")
  if not isinstance(result, list):
    raise RuntimeError(f"{sql_path.name}: expected a JSON row list")
  return result, job_id


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--project", required=True)
  parser.add_argument(
      "--billing-project",
      help="BigQuery billing project (default: --project)",
  )
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--table", default="agent_events")
  parser.add_argument("--location", default="US")
  parser.add_argument(
      "--end-date",
      required=True,
      help="Inclusive YYYY-MM-DD report end date",
  )
  parser.add_argument(
      "--spec",
      default=str(ROOT / "spec/chart_manifest.yaml"),
  )
  parser.add_argument("--output", help="Optional sanitized JSON receipt")
  args = parser.parse_args()
  billing_project = args.billing_project or args.project

  try:
    require_identifier("project ID", args.project, PROJECT_RE)
    require_identifier("billing project ID", billing_project, PROJECT_RE)
    require_identifier("dataset ID", args.dataset, ID_RE)
    require_identifier("table ID", args.table, ID_RE)
    require_identifier("location", args.location, LOCATION_RE)
  except ValueError as exc:
    raise SystemExit(str(exc)) from exc

  datetime.date.fromisoformat(args.end_date)
  manifest = yaml.safe_load(pathlib.Path(args.spec).read_text())
  charts = manifest["charts"]
  if len(charts) != 37:
    raise SystemExit(f"expected 37 charts, found {len(charts)}")

  preflight = (
      (ROOT / "sql/preflight.sql.tmpl")
      .read_text()
      .replace("{{PROJECT}}", args.project)
      .replace("{{DATASET}}", args.dataset)
      .replace("{{TABLE}}", args.table)
  )
  preflight_process = subprocess.run(
      [
          "bq",
          f"--project_id={billing_project}",
          f"--location={args.location}",
          "query",
          "--nouse_legacy_sql",
          "--format=json",
          "--max_rows=10000",
      ],
      input=preflight,
      capture_output=True,
      text=True,
      check=False,
  )
  if preflight_process.returncode:
    raise SystemExit(
        f"preflight execution failed: {preflight_process.stderr[:400]}"
    )
  problems = json.loads(preflight_process.stdout or "[]")
  if problems:
    raise SystemExit(
        f"preflight found {len(problems)} incompatible source objects"
    )

  executed = []
  failures = []
  for chart in charts:
    start, end = window_for(chart, args.end_date)
    query_path = ROOT / chart["oracle_query"]
    try:
      rows, job_id = run_query(
          query_path,
          args.project,
          billing_project,
          args.dataset,
          args.table,
          args.location,
          start,
          end,
      )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
      failures.append(f"{chart['id']}: {exc}")
      print(f"FAIL {chart['id']}", file=sys.stderr)
      continue
    executed.append(
        {
            "chart_id": chart["id"],
            "query_sha256": sha256_file(query_path),
            "row_count": len(rows),
            "job_id": job_id,
            "window_days": (
                7 if chart["source_dashboard"] == "performance" else 14
            ),
        }
    )
    print(f"OK {chart['id']}: {len(rows)} rows [{job_id}]", flush=True)

  receipt = {
      "kind": "sanitized-live-bqaa-smoke-test",
      "semantic_scope": (
          "read-only execution/compatibility; not fixture parity certification"
      ),
      "executed_at_utc": datetime.datetime.now(
          datetime.timezone.utc
      ).isoformat(),
      "report_end_date": args.end_date,
      "source_object_type": "BASE TABLE",
      "generated_views_required": False,
      "spec_sha256": sha256_file(args.spec),
      "charts_expected": 37,
      "charts_succeeded": len(executed),
      "charts_failed": len(failures),
      "charts": executed,
      "pass": not failures and len(executed) == 37,
  }
  if args.output:
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(f"sanitized receipt: {output}")
  if failures:
    for failure in failures:
      print(f"FAIL: {failure}", file=sys.stderr)
    return 1
  print("live BQAA validation OK: 37/37 oracle queries executed")
  return 0


if __name__ == "__main__":
  sys.exit(main())
