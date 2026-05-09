# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Materialize the auditor's joint context graph.

Reads the caller and receiver SDK-extracted graph backing tables
(populated by ``build_org_graphs.py``) and creates redacted
projection tables in ``<AUDITOR_DATASET>``, then issues the
``joint_property_graph.gql`` rendered by ``render_queries.sh``.

All projections use ``CREATE OR REPLACE TABLE ... AS SELECT ...`` so
re-runs are idempotent. This avoids the streaming-buffer / duplicate
key class of failure the decision-lineage demo hit in PR #99.

Auditor projections (Phase 1):

  - ``caller_campaign_runs`` — projection of caller demo metadata,
    renames ``session_id`` -> ``caller_session_id`` to match the
    graph DDL's ``KEY (caller_session_id)``.
  - ``remote_agent_invocations`` — one row per caller-side
    ``A2A_INTERACTION``. Carries lineage IDs only (``a2a_task_id``,
    ``a2a_context_id``, optional ``receiver_session_id_from_response``);
    drops raw ``a2a_request`` / ``a2a_response`` / ``content``.
  - ``receiver_runs`` — receiver-side session roots derived from
    ``GROUP BY session_id`` over the receiver ``agent_events``.
  - ``receiver_planning_decisions`` — projection of receiver
    ``decision_points``.
  - ``receiver_decision_options`` — projection of receiver
    ``candidates`` (carries ``rejection_rationale`` as a property,
    NULL for SELECTED options, non-NULL for DROPPED).
  - ``joint_a2a_edges`` — stitch table joining
    ``remote_agent_invocations.a2a_context_id`` to
    ``receiver_runs.receiver_session_id``.

Redaction is a *convention* enforced by these views, not an
IAM-enforced control. In a single-project demo, anyone with
project-level access can SELECT * from the underlying caller and
receiver datasets directly. Production cross-org redaction
enforcement is a separate working group.
"""

from __future__ import annotations

import os
import subprocess
import sys

from dotenv import load_dotenv
from google.api_core import exceptions as gax_exceptions
import google.auth
from google.cloud import bigquery

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_HERE, ".env")
if os.path.exists(_ENV_PATH):
  load_dotenv(dotenv_path=_ENV_PATH)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
CALLER_DATASET_ID = os.getenv("CALLER_DATASET_ID", "a2a_caller_demo")
CALLER_TABLE_ID = os.getenv("CALLER_TABLE_ID", "agent_events")
RECEIVER_DATASET_ID = os.getenv("RECEIVER_DATASET_ID", "a2a_receiver_demo")
RECEIVER_TABLE_ID = os.getenv("RECEIVER_TABLE_ID", "agent_events")
AUDITOR_DATASET_ID = os.getenv("AUDITOR_DATASET_ID", "a2a_auditor_demo")

_RENDERED_GRAPH_DDL_PATH = os.path.join(_HERE, "joint_property_graph.gql")
_RENDER_SCRIPT_PATH = os.path.join(_HERE, "render_queries.sh")


_PROJECTIONS: list[tuple[str, str]] = [
    (
        "caller_campaign_runs",
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.caller_campaign_runs` AS
SELECT
  session_id AS caller_session_id,
  campaign,
  brand,
  brief,
  run_order,
  event_count
FROM `{project}.{caller}.campaign_runs`
""",
    ),
    (
        "remote_agent_invocations",
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.remote_agent_invocations` AS
SELECT
  TO_HEX(SHA256(CONCAT(session_id, ':', span_id))) AS remote_invocation_id,
  session_id AS caller_session_id,
  span_id AS caller_span_id,
  JSON_VALUE(attributes, '$.a2a_metadata."a2a:task_id"')    AS a2a_task_id,
  JSON_VALUE(attributes, '$.a2a_metadata."a2a:context_id"') AS a2a_context_id,
  COALESCE(
    JSON_VALUE(content, '$.metadata.adk_session_id'),
    JSON_VALUE(attributes, '$.a2a_metadata."a2a:response".metadata.adk_session_id')
  ) AS receiver_session_id_from_response,
  timestamp
FROM `{project}.{caller}.{caller_table}`
WHERE event_type = 'A2A_INTERACTION'
""",
    ),
    (
        "receiver_runs",
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.receiver_runs` AS
SELECT
  session_id AS receiver_session_id,
  MIN(timestamp) AS started_at,
  MAX(timestamp) AS ended_at,
  COUNT(*) AS event_count,
  COUNTIF(event_type = 'AGENT_COMPLETED') AS completed
FROM `{project}.{receiver}.{receiver_table}`
WHERE session_id IS NOT NULL
GROUP BY receiver_session_id
""",
    ),
    (
        "receiver_planning_decisions",
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.receiver_planning_decisions` AS
SELECT
  decision_id,
  session_id,
  span_id,
  decision_type,
  description
FROM `{project}.{receiver}.decision_points`
""",
    ),
    (
        "receiver_decision_options",
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.receiver_decision_options` AS
SELECT
  candidate_id,
  decision_id,
  session_id,
  name,
  score,
  status,
  rejection_rationale
FROM `{project}.{receiver}.candidates`
""",
    ),
    (
        "joint_a2a_edges",
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.joint_a2a_edges` AS
SELECT
  TO_HEX(SHA256(CONCAT(r.remote_invocation_id, ':', rr.receiver_session_id)))
    AS edge_id,
  r.remote_invocation_id,
  rr.receiver_session_id,
  r.a2a_context_id,
  r.a2a_task_id
FROM `{project}.{auditor}.remote_agent_invocations` AS r
JOIN `{project}.{auditor}.receiver_runs` AS rr
  ON r.a2a_context_id = rr.receiver_session_id
""",
    ),
]


def _materialize_projections(client: bigquery.Client) -> int:
  for name, sql in _PROJECTIONS:
    rendered = sql.format(
        project=PROJECT_ID,
        caller=CALLER_DATASET_ID,
        caller_table=CALLER_TABLE_ID,
        receiver=RECEIVER_DATASET_ID,
        receiver_table=RECEIVER_TABLE_ID,
        auditor=AUDITOR_DATASET_ID,
    )
    print(f"  materializing {name}...")
    try:
      client.query(rendered).result()
    except gax_exceptions.NotFound as exc:
      print(
          f"ERROR: {name} CTAS failed because a source table is "
          f"missing: {exc}. Re-run build_org_graphs.py first.",
          file=sys.stderr,
      )
      return 1
  return 0


def _render_graph_ddl() -> int:
  if not os.path.exists(_RENDER_SCRIPT_PATH):
    print(
        f"ERROR: render script {_RENDER_SCRIPT_PATH} not found.",
        file=sys.stderr,
    )
    return 1
  result = subprocess.run(
      ["bash", _RENDER_SCRIPT_PATH], check=False, capture_output=True, text=True
  )
  if result.returncode != 0:
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    print(
        f"ERROR: render_queries.sh failed (exit {result.returncode}).",
        file=sys.stderr,
    )
    return 1
  print(result.stdout.strip())
  return 0


def _create_property_graph(client: bigquery.Client) -> int:
  if not os.path.exists(_RENDERED_GRAPH_DDL_PATH):
    print(
        "ERROR: rendered joint_property_graph.gql not found at "
        f"{_RENDERED_GRAPH_DDL_PATH}; render_queries.sh did not "
        "produce it.",
        file=sys.stderr,
    )
    return 1
  with open(_RENDERED_GRAPH_DDL_PATH, encoding="utf-8") as f:
    ddl = f.read()
  print("  issuing CREATE OR REPLACE PROPERTY GRAPH...")
  try:
    client.query(ddl).result()
  except gax_exceptions.GoogleAPIError as exc:
    print(
        f"ERROR: joint property graph DDL failed: {exc}",
        file=sys.stderr,
    )
    return 1
  return 0


def _verify_graph(client: bigquery.Client) -> int:
  """Smoke-check: end-to-end traversal returns at least one row."""
  q = f"""
    GRAPH `{PROJECT_ID}.{AUDITOR_DATASET_ID}.a2a_joint_context_graph`
    MATCH (campaign:CallerCampaignRun)
          -[:DelegatedVia]->(remote:RemoteAgentInvocation)
          -[:HandledBy]->(receiver:ReceiverAgentRun)
    RETURN
      campaign.campaign,
      remote.a2a_context_id,
      receiver.receiver_session_id
    LIMIT 5
  """
  try:
    rows = list(client.query(q).result())
  except gax_exceptions.GoogleAPIError as exc:
    print(
        f"ERROR: traversal smoke query failed: {exc}",
        file=sys.stderr,
    )
    return 1
  if not rows:
    print(
        "ERROR: traversal smoke returned zero rows. Check that "
        "joint_a2a_edges has matching pairs (see Block 1 in "
        "bq_studio_queries.gql).",
        file=sys.stderr,
    )
    return 1
  print(f"  traversal smoke: {len(rows)} row(s):")
  for row in rows:
    print(
        f"    campaign={row['campaign']!r} "
        f"context={row['a2a_context_id']!r} "
        f"receiver_session={row['receiver_session_id']!r}"
    )
  return 0


def main() -> int:
  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)

  print(f"Materializing auditor projections in {AUDITOR_DATASET_ID}...")
  rc = _materialize_projections(client)
  if rc != 0:
    return rc

  print("Rendering joint_property_graph.gql from template...")
  rc = _render_graph_ddl()
  if rc != 0:
    return rc

  print("Creating joint property graph...")
  rc = _create_property_graph(client)
  if rc != 0:
    return rc

  print("Verifying joint graph traversal...")
  rc = _verify_graph(client)
  if rc != 0:
    return rc

  graph_ref = f"{PROJECT_ID}.{AUDITOR_DATASET_ID}.a2a_joint_context_graph"
  print()
  print(f"OK — joint property graph ready at `{graph_ref}`.")
  print(
      "Open BigQuery Studio and paste blocks from "
      f"{os.path.join(_HERE, 'bq_studio_queries.gql')}"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
