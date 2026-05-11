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

"""Tools the analyst agent uses to query the joint A2A context graph.

Each tool wraps one of the four headline audit questions the demo
already supports via BigQuery Studio (`bq_studio_queries.gql.tpl`
Blocks 1-4). The analyst agent reasons in natural language and
picks a tool; the tool runs a parameterized query against the
auditor projection tables and joint property graph, returns a
small structured result, and never exposes raw A2A request /
response payloads.

`<P>.<AUDITOR>.a2a_joint_context_graph` is the graph. Its node and
edge tables are populated by `build_joint_graph.py` from the
caller and receiver `agent_events` tables earlier in the runbook.

All tools accept no credentials directly; they use ADC via
`google.cloud.bigquery.Client`. The dataset references come from
module-level constants resolved from `.env`.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv
from google.api_core import exceptions as gax_exceptions
import google.auth
from google.cloud import bigquery

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_HERE)
_env_path = os.path.join(_DEMO_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
AUDITOR_DATASET_ID = os.getenv("AUDITOR_DATASET_ID", "a2a_auditor_demo")

# Row caps protect the LLM context window against pathological
# audit datasets (e.g. thousands of dropped options). Operators can
# override either through env vars when they truly want a wider
# scan; the defaults stay tight to keep the demo readable.
_LIST_LIMIT = int(os.getenv("ANALYST_LIST_LIMIT", "25"))
_REJECTIONS_LIMIT = int(os.getenv("ANALYST_REJECTIONS_LIMIT", "30"))
# audit_campaign() walks N options × M decisions for one campaign;
# at the receiver-prompt contract (3 candidates × 1 decision per
# call) this is ~3 rows per A2A call. A misconfigured receiver
# can produce many more, hence the cap.
_AUDIT_LIMIT = int(os.getenv("ANALYST_AUDIT_LIMIT", "50"))

_GRAPH = f"`{PROJECT_ID}.{AUDITOR_DATASET_ID}.a2a_joint_context_graph`"


def _client() -> bigquery.Client:
  return bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)


def stitch_health() -> dict[str, Any]:
  """Audit-stack health gate (Block 1 in the BQ Studio walkthrough).

  Returns:
      Dict with five keys:
        - a2a_calls (int): number of remote A2A delegations the
          caller recorded in the current campaign run.
        - calls_with_context_id (int): subset that carry an
          a2a_context_id (should equal a2a_calls).
        - calls_with_receiver_echo (int): subset whose response
          carried a diagnostic adk_session_id echo.
        - stitched_edges (int): number of (remote_invocation,
          receiver_session) pairs the auditor join found.
        - unstitched_calls (int): a2a_calls - stitched_edges; the
          failure signal.
  """
  client = _client()
  q = f"""
    WITH ri AS (
      SELECT
        COUNT(*)                                                  AS a2a_calls,
        COUNTIF(a2a_context_id IS NOT NULL)                       AS calls_with_context_id,
        COUNTIF(receiver_session_id_from_response IS NOT NULL)    AS calls_with_receiver_echo
      FROM `{PROJECT_ID}.{AUDITOR_DATASET_ID}.remote_agent_invocations`
    ),
    edges AS (
      SELECT COUNT(*) AS stitched_edges
      FROM `{PROJECT_ID}.{AUDITOR_DATASET_ID}.joint_a2a_edges`
    )
    SELECT
      ri.a2a_calls,
      ri.calls_with_context_id,
      ri.calls_with_receiver_echo,
      edges.stitched_edges,
      ri.a2a_calls - edges.stitched_edges AS unstitched_calls
    FROM ri, edges
  """
  try:
    row = list(client.query(q).result())[0]
  except gax_exceptions.NotFound:
    return {
        "error": (
            "Auditor projections not found. Run build_joint_graph.py first."
        ),
    }
  return {
      "a2a_calls": int(row["a2a_calls"]),
      "calls_with_context_id": int(row["calls_with_context_id"]),
      "calls_with_receiver_echo": int(row["calls_with_receiver_echo"]),
      "stitched_edges": int(row["stitched_edges"]),
      "unstitched_calls": int(row["unstitched_calls"]),
  }


def list_campaigns() -> dict[str, Any]:
  """Lists every caller CampaignRun visible to the auditor graph.

  Returns:
      Dict with two keys:
        - count (int): number of rows.
        - campaigns (list[dict]): one entry per campaign with
          caller_session_id, campaign, brand, run_order, and
          event_count.
  """
  client = _client()
  q = f"""
    SELECT
      caller_session_id,
      campaign,
      brand,
      run_order,
      event_count
    FROM `{PROJECT_ID}.{AUDITOR_DATASET_ID}.caller_campaign_runs`
    ORDER BY run_order
    LIMIT {_LIST_LIMIT}
  """
  try:
    rows = list(client.query(q).result())
  except gax_exceptions.NotFound:
    return {
        "count": 0,
        "campaigns": [],
        "error": (
            "caller_campaign_runs not found. Run build_joint_graph.py first."
        ),
    }
  campaigns = [
      {
          "caller_session_id": str(r["caller_session_id"]),
          "campaign": str(r["campaign"]),
          "brand": str(r["brand"]),
          "run_order": int(r["run_order"]),
          "event_count": int(r["event_count"]),
      }
      for r in rows
  ]
  return {"count": len(campaigns), "campaigns": campaigns}


def audit_campaign(caller_session_id: str) -> dict[str, Any]:
  """Right-to-explanation audit path for one caller campaign.

  Walks the graph:

      CallerCampaignRun -[:DelegatedVia]-> RemoteAgentInvocation
        -[:HandledBy]-> ReceiverAgentRun
        -[:ReceiverMadeDecision]-> ReceiverPlanningDecision
        -[:ReceiverWeighedOption]-> ReceiverDecisionOption

  Both selected and dropped options appear because
  `rejection_rationale` is a property on every
  `ReceiverDecisionOption` (NULL for SELECTED, non-NULL for DROPPED).

  Args:
      caller_session_id: The caller session id for the campaign
          under audit. Use `list_campaigns()` to look this up if
          the user provided only a campaign or brand name.

  Returns:
      Dict with:
        - caller_session_id (str): echo of input.
        - count (int): number of option rows returned.
        - options (list[dict]): each entry has campaign,
          a2a_context_id, decision_type, option_name, score,
          status (SELECTED | DROPPED), and rationale (the dropped
          option's rejection reason, or null for selected options).
  """
  client = _client()
  q = f"""
    GRAPH {_GRAPH}
    MATCH (campaign:CallerCampaignRun)
          -[:DelegatedVia]->(remote:RemoteAgentInvocation)
          -[:HandledBy]->(receiver:ReceiverAgentRun)
          -[:ReceiverMadeDecision]->(decision:ReceiverPlanningDecision)
          -[:ReceiverWeighedOption]->(option:ReceiverDecisionOption)
    WHERE campaign.caller_session_id = @caller_session
    RETURN
      campaign.campaign AS campaign,
      remote.a2a_context_id AS a2a_context_id,
      decision.decision_type AS decision_type,
      option.name AS option_name,
      option.score AS score,
      option.status AS status,
      option.rejection_rationale AS rationale
    ORDER BY option.status DESC, option.score DESC
    LIMIT {_AUDIT_LIMIT}
  """
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "caller_session", "STRING", caller_session_id
          ),
      ],
  )
  try:
    rows = list(client.query(q, job_config=job_config).result())
  except gax_exceptions.NotFound:
    return {
        "caller_session_id": caller_session_id,
        "count": 0,
        "options": [],
        "error": (
            "Joint property graph not found. Run build_joint_graph.py first."
        ),
    }
  if not rows:
    return {
        "caller_session_id": caller_session_id,
        "count": 0,
        "options": [],
        "note": (
            "No options found for that caller_session_id. Use "
            "list_campaigns() to confirm the session id exists."
        ),
    }
  options = [
      {
          "campaign": str(r["campaign"]),
          "a2a_context_id": (
              str(r["a2a_context_id"]) if r["a2a_context_id"] else None
          ),
          "decision_type": str(r["decision_type"]),
          "option_name": str(r["option_name"]),
          "score": float(r["score"]) if r["score"] is not None else None,
          "status": str(r["status"]),
          "rationale": (
              str(r["rationale"]) if r["rationale"] is not None else None
          ),
      }
      for r in rows
  ]
  return {
      "caller_session_id": caller_session_id,
      "count": len(options),
      "options": options,
  }


def find_governance_rejections(
    decision_type: Optional[str] = None,
    max_score: Optional[float] = None,
) -> dict[str, Any]:
  """Portfolio-level scan of every DROPPED option in the joint graph.

  Args:
      decision_type: Optional case-insensitive substring filter on
          the decision label (e.g. ``"audience"`` to match
          ``"Audience risk review"``). Pass ``None`` to include
          all decision types.
      max_score: Optional upper bound on option score. Pass
          ``None`` to include all dropped options regardless of
          score.

  Returns:
      Dict with:
        - count (int): rows returned (capped by
          ANALYST_REJECTIONS_LIMIT, default 30).
        - filters (dict): echo of the applied filter values.
        - rejections (list[dict]): each entry has
          caller_session_id, campaign, brand, a2a_context_id,
          decision_type, option_name, score, and rationale. Campaign
          identity is included so the analyst can attribute each
          rejection back to a specific caller campaign in
          portfolio-level questions.
  """
  filters = {"decision_type": decision_type, "max_score": max_score}
  where = ["option.status = 'DROPPED'"]
  params: list[bigquery.ScalarQueryParameter] = []
  if decision_type is not None:
    where.append(
        "LOWER(decision.decision_type) LIKE CONCAT('%', LOWER(@dtype), '%')"
    )
    params.append(
        bigquery.ScalarQueryParameter("dtype", "STRING", decision_type)
    )
  if max_score is not None:
    where.append("option.score <= @max_score")
    params.append(
        bigquery.ScalarQueryParameter("max_score", "FLOAT64", float(max_score))
    )

  where_clause = " AND ".join(where)
  # Walk back to CallerCampaignRun so the analyst can attribute each
  # rejection to a specific campaign (caller_session_id + campaign
  # + brand). The earlier shape started from RemoteAgentInvocation,
  # which made portfolio-level answers ambiguous about which
  # campaign each rejection belonged to.
  q = f"""
    GRAPH {_GRAPH}
    MATCH (campaign:CallerCampaignRun)
          -[:DelegatedVia]->(remote:RemoteAgentInvocation)
          -[:HandledBy]->(receiver:ReceiverAgentRun)
          -[:ReceiverMadeDecision]->(decision:ReceiverPlanningDecision)
          -[:ReceiverWeighedOption]->(option:ReceiverDecisionOption)
    WHERE {where_clause}
    RETURN
      campaign.caller_session_id AS caller_session_id,
      campaign.campaign AS campaign,
      campaign.brand AS brand,
      remote.a2a_context_id AS a2a_context_id,
      decision.decision_type AS decision_type,
      option.name AS option_name,
      option.score AS score,
      option.rejection_rationale AS rationale
    ORDER BY option.score ASC
    LIMIT {_REJECTIONS_LIMIT}
  """
  job_config = bigquery.QueryJobConfig(query_parameters=params)
  client = _client()
  try:
    rows = list(client.query(q, job_config=job_config).result())
  except gax_exceptions.NotFound:
    return {
        "count": 0,
        "filters": filters,
        "rejections": [],
        "error": (
            "Joint property graph not found. Run build_joint_graph.py first."
        ),
    }
  rejections = [
      {
          "caller_session_id": str(r["caller_session_id"]),
          "campaign": str(r["campaign"]),
          "brand": str(r["brand"]) if r["brand"] is not None else None,
          "a2a_context_id": (
              str(r["a2a_context_id"]) if r["a2a_context_id"] else None
          ),
          "decision_type": str(r["decision_type"]),
          "option_name": str(r["option_name"]),
          "score": float(r["score"]) if r["score"] is not None else None,
          "rationale": (
              str(r["rationale"]) if r["rationale"] is not None else None
          ),
      }
      for r in rows
  ]
  return {
      "count": len(rejections),
      "filters": filters,
      "rejections": rejections,
  }


ANALYST_TOOLS = [
    stitch_health,
    list_campaigns,
    audit_campaign,
    find_governance_rejections,
]
