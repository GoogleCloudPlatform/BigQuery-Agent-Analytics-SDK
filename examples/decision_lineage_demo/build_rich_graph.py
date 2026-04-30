# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Build the richer demo presentation graph.

``build_graph.py`` builds the SDK's canonical decision graph:

  TechNode, BizNode, DecisionPoint, CandidateNode

This script adds demo-only presentation tables and creates
``rich_agent_context_graph`` with extra labels that make the BigQuery
Studio visualization read like the talk track:

  CampaignRun, AgentStep, MediaEntity, PlanningDecision,
  DecisionCategory, DecisionOption, OptionOutcome, DropReason

The derived tables are deterministic SQL/load-job projections over the
seven SDK backing tables. No new AI calls run here.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from campaigns import CAMPAIGN_BRIEFS
from dotenv import load_dotenv
from google.cloud import bigquery

_SCRIPT_DIR = Path(__file__).resolve().parent
_ENV_PATH = _SCRIPT_DIR / ".env"
if _ENV_PATH.exists():
  load_dotenv(dotenv_path=_ENV_PATH)

PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_ID = os.getenv("DATASET_ID", "decision_lineage_rich_demo")
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")

RICH_GRAPH_NAME = "rich_agent_context_graph"

_DISTINCT_SESSIONS_QUERY = """\
SELECT
  session_id,
  COUNT(*) AS event_count,
  MIN(timestamp) AS first_event_at
FROM `{project}.{dataset}.{table}`
WHERE session_id IS NOT NULL
GROUP BY session_id
ORDER BY first_event_at
"""

_CREATE_CAMPAIGN_RUNS_TABLE = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.campaign_runs` (
  session_id STRING,
  campaign STRING,
  brand STRING,
  brief STRING,
  run_order INT64,
  event_count INT64
)
"""

_CREATE_RICH_DECISION_TYPES = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_decision_types` AS
SELECT
  TO_HEX(SHA256(LOWER(TRIM(decision_type)))) AS decision_type_id,
  LOWER(TRIM(decision_type)) AS decision_type_key,
  ANY_VALUE(decision_type) AS decision_type,
  COUNT(*) AS decision_count
FROM `{project}.{dataset}.decision_points`
WHERE decision_type IS NOT NULL AND TRIM(decision_type) != ''
GROUP BY decision_type_id, decision_type_key
"""

_CREATE_RICH_CANDIDATE_STATUSES = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_candidate_statuses` AS
SELECT
  status AS status_id,
  status,
  COUNT(*) AS candidate_count
FROM `{project}.{dataset}.candidates`
WHERE status IS NOT NULL AND TRIM(status) != ''
GROUP BY status_id, status
"""

_CREATE_RICH_REJECTION_REASONS = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_rejection_reasons` AS
SELECT
  TO_HEX(SHA256(TRIM(rejection_rationale))) AS reason_id,
  TRIM(rejection_rationale) AS rejection_rationale,
  SUBSTR(TRIM(rejection_rationale), 1, 120) AS reason_excerpt,
  COUNT(*) AS candidate_count
FROM `{project}.{dataset}.candidates`
WHERE status = 'DROPPED'
  AND rejection_rationale IS NOT NULL
  AND TRIM(rejection_rationale) != ''
GROUP BY reason_id, rejection_rationale, reason_excerpt
"""

_CREATE_RICH_AGENT_STEPS = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_agent_steps` AS
SELECT
  span_id,
  parent_span_id,
  event_type,
  agent,
  timestamp,
  session_id,
  invocation_id,
  content,
  latency_ms,
  status,
  error_message
FROM `{project}.{dataset}.{table}`
WHERE span_id IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY span_id
  ORDER BY timestamp DESC, event_type DESC
) = 1
"""

_CREATE_RICH_CAMPAIGN_SPAN_EDGES = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_campaign_span_edges` AS
SELECT
  CONCAT(session_id, ':span:', span_id) AS edge_id,
  session_id,
  span_id,
  event_type,
  timestamp
FROM `{project}.{dataset}.rich_agent_steps`
WHERE session_id IS NOT NULL
  AND span_id IS NOT NULL
"""

_CREATE_RICH_CAMPAIGN_DECISION_EDGES = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_campaign_decision_edges` AS
SELECT
  CONCAT(session_id, ':campaign_decision:', decision_id) AS edge_id,
  session_id,
  decision_id
FROM `{project}.{dataset}.decision_points`
WHERE session_id IS NOT NULL
  AND decision_id IS NOT NULL
"""

_CREATE_RICH_DECISION_TYPE_EDGES = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_decision_type_edges` AS
SELECT
  CONCAT(decision_id, ':type:', TO_HEX(SHA256(LOWER(TRIM(decision_type))))) AS edge_id,
  decision_id,
  TO_HEX(SHA256(LOWER(TRIM(decision_type)))) AS decision_type_id
FROM `{project}.{dataset}.decision_points`
WHERE decision_id IS NOT NULL
  AND decision_type IS NOT NULL
  AND TRIM(decision_type) != ''
"""

_CREATE_RICH_CANDIDATE_STATUS_EDGES = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_candidate_status_edges` AS
SELECT
  CONCAT(candidate_id, ':status:', status) AS edge_id,
  candidate_id,
  status AS status_id
FROM `{project}.{dataset}.candidates`
WHERE candidate_id IS NOT NULL
  AND status IS NOT NULL
  AND TRIM(status) != ''
"""

_CREATE_RICH_CANDIDATE_REASON_EDGES = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.rich_candidate_reason_edges` AS
SELECT
  CONCAT(candidate_id, ':reason:', TO_HEX(SHA256(TRIM(rejection_rationale)))) AS edge_id,
  candidate_id,
  TO_HEX(SHA256(TRIM(rejection_rationale))) AS reason_id
FROM `{project}.{dataset}.candidates`
WHERE status = 'DROPPED'
  AND candidate_id IS NOT NULL
  AND rejection_rationale IS NOT NULL
  AND TRIM(rejection_rationale) != ''
"""


def _format(sql: str) -> str:
  return sql.format(
      project=PROJECT_ID,
      dataset=DATASET_ID,
      table=TABLE_ID,
      graph=RICH_GRAPH_NAME,
  )


def _run_query(client: bigquery.Client, sql: str, label: str) -> None:
  print(f"  - {label}")
  job = client.query(_format(sql))
  job.result()


def _fetch_sessions(client: bigquery.Client) -> list[tuple[str, int]]:
  rows = client.query(_format(_DISTINCT_SESSIONS_QUERY)).result()
  return [(row.session_id, int(row.event_count)) for row in rows]


def _campaign_runs_has_rows(client: bigquery.Client) -> bool:
  query = """\
SELECT COUNT(*) AS n
FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES`
WHERE table_name = 'campaign_runs'
""".format(
      project=PROJECT_ID, dataset=DATASET_ID
  )
  rows = list(client.query(query).result())
  if not rows or not rows[0].n:
    return False

  count_query = """\
SELECT COUNT(*) AS n
FROM `{project}.{dataset}.campaign_runs`
""".format(
      project=PROJECT_ID, dataset=DATASET_ID
  )
  count_rows = list(client.query(count_query).result())
  return bool(count_rows and count_rows[0].n)


def _campaign_rows(sessions: list[tuple[str, int]]) -> list[dict[str, object]]:
  rows: list[dict[str, object]] = []
  for idx, (session_id, event_count) in enumerate(sessions):
    brief = CAMPAIGN_BRIEFS[idx % len(CAMPAIGN_BRIEFS)]
    brand = brief.campaign.split()[0]
    rows.append(
        {
            "session_id": session_id,
            "campaign": brief.campaign,
            "brand": brand,
            "brief": brief.brief,
            "run_order": idx + 1,
            "event_count": event_count,
        }
    )
  return rows


def _write_campaign_runs(
    client: bigquery.Client, sessions: list[tuple[str, int]]
) -> None:
  if _campaign_runs_has_rows(client):
    print("  - campaign_runs (existing from run_agent.py)")
    return

  print("  - campaign_runs")
  client.query(_format(_CREATE_CAMPAIGN_RUNS_TABLE)).result()
  table_ref = f"{PROJECT_ID}.{DATASET_ID}.campaign_runs"
  job_config = bigquery.LoadJobConfig(
      write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
      source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
  )
  job = client.load_table_from_json(
      _campaign_rows(sessions), table_ref, job_config=job_config
  )
  job.result()


def _create_rich_graph(client: bigquery.Client) -> None:
  print(f"  - {RICH_GRAPH_NAME}")
  template = (_SCRIPT_DIR / "rich_property_graph.gql.tpl").read_text(
      encoding="utf-8"
  )
  sql = (
      template.replace("__PROJECT_ID__", PROJECT_ID or "")
      .replace("__DATASET_ID__", DATASET_ID)
      .replace("__RICH_GRAPH_NAME__", RICH_GRAPH_NAME)
  )
  client.query(sql).result()


def main() -> int:
  if not PROJECT_ID:
    print("ERROR: PROJECT_ID not set. Run ./setup.sh first.", file=sys.stderr)
    return 2

  print(f"Project : {PROJECT_ID}")
  print(f"Dataset : {DATASET_ID}")
  print(f"Graph   : {RICH_GRAPH_NAME}")
  print()

  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)
  sessions = _fetch_sessions(client)
  if not sessions:
    print(
        f"ERROR: no sessions found in {DATASET_ID}.{TABLE_ID}. "
        "Run run_agent.py and build_graph.py first.",
        file=sys.stderr,
    )
    return 1

  print("Building rich demo tables:")
  _write_campaign_runs(client, sessions)
  for label, sql in (
      ("rich_decision_types", _CREATE_RICH_DECISION_TYPES),
      ("rich_candidate_statuses", _CREATE_RICH_CANDIDATE_STATUSES),
      ("rich_rejection_reasons", _CREATE_RICH_REJECTION_REASONS),
      ("rich_agent_steps", _CREATE_RICH_AGENT_STEPS),
      ("rich_campaign_span_edges", _CREATE_RICH_CAMPAIGN_SPAN_EDGES),
      ("rich_campaign_decision_edges", _CREATE_RICH_CAMPAIGN_DECISION_EDGES),
      ("rich_decision_type_edges", _CREATE_RICH_DECISION_TYPE_EDGES),
      ("rich_candidate_status_edges", _CREATE_RICH_CANDIDATE_STATUS_EDGES),
      ("rich_candidate_reason_edges", _CREATE_RICH_CANDIDATE_REASON_EDGES),
  ):
    _run_query(client, sql, label)

  print("Creating rich property graph:")
  _create_rich_graph(client)
  print()
  print(
      f"Rich graph: {PROJECT_ID}.{DATASET_ID}.{RICH_GRAPH_NAME} "
      f"({len(sessions)} campaign runs)"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
