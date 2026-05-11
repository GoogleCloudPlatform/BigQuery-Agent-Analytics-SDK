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

Redaction is a *convention* enforced by these projection tables,
not an IAM-enforced control. In a single-project demo, anyone with
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

# Scoped receiver-extraction acceptance thresholds. Match the
# defaults in build_org_graphs.py so the auditor surface for a
# default 3-campaign demo (3 decisions × 3 candidates) is
# consistent with the per-org gate. Override either via env.
MIN_SCOPED_DECISIONS = int(os.getenv("DEMO_MIN_RECEIVER_DECISIONS", "3"))
MIN_SCOPED_CANDIDATES = int(os.getenv("DEMO_MIN_RECEIVER_CANDIDATES", "9"))

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
        # Filter to A2A_INTERACTION rows whose caller session is in
        # the current caller_campaign_runs. Without this join, a
        # rerun without ./reset.sh (the README's "skip reset if
        # iterating" path) leaves stale A2A_INTERACTION rows in the
        # caller agent_events table while run_caller_agent.py
        # WRITE_TRUNCATEs campaign_runs to only the current run.
        # The auditor projection would then carry orphaned remote
        # invocations whose CallerCampaignRun source vanished, and
        # Block 2's traversal would silently drop them.
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.remote_agent_invocations` AS
SELECT
  TO_HEX(SHA256(CONCAT(ev.session_id, ':', ev.span_id))) AS remote_invocation_id,
  ev.session_id AS caller_session_id,
  ev.span_id AS caller_span_id,
  JSON_VALUE(ev.attributes, '$.a2a_metadata."a2a:task_id"')    AS a2a_task_id,
  JSON_VALUE(ev.attributes, '$.a2a_metadata."a2a:context_id"') AS a2a_context_id,
  COALESCE(
    JSON_VALUE(ev.content, '$.metadata.adk_session_id'),
    JSON_VALUE(ev.attributes, '$.a2a_metadata."a2a:response".metadata.adk_session_id')
  ) AS receiver_session_id_from_response,
  ev.timestamp
FROM `{project}.{caller}.{caller_table}` AS ev
JOIN `{project}.{auditor}.caller_campaign_runs` AS ccr
  ON ev.session_id = ccr.caller_session_id
WHERE ev.event_type = 'A2A_INTERACTION'
""",
    ),
    (
        "receiver_runs",
        # Scope receiver_runs to sessions referenced by the current
        # remote_agent_invocations. Without this, no-reset reruns
        # (and the smoke_receiver session) leave stale receiver
        # session rows visible in the BQ Studio Explorer and in
        # any GQL traversal that starts from ReceiverAgentRun. The
        # curated Blocks 2-4 start from RemoteAgentInvocation and
        # are mostly protected, but the auditor table itself
        # should reflect only sessions matched to current caller
        # campaigns.
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.receiver_runs` AS
SELECT
  ev.session_id AS receiver_session_id,
  MIN(ev.timestamp) AS started_at,
  MAX(ev.timestamp) AS ended_at,
  COUNT(*) AS event_count,
  COUNTIF(ev.event_type = 'AGENT_COMPLETED') AS completed
FROM `{project}.{receiver}.{receiver_table}` AS ev
WHERE ev.session_id IS NOT NULL
  AND ev.session_id IN (
    SELECT DISTINCT a2a_context_id
    FROM `{project}.{auditor}.remote_agent_invocations`
    WHERE a2a_context_id IS NOT NULL
  )
GROUP BY receiver_session_id
""",
    ),
    (
        "receiver_planning_decisions",
        # Scope to the receiver sessions retained in receiver_runs
        # so stale extractions (including those from smoke_receiver)
        # don't surface in the auditor projection.
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.receiver_planning_decisions` AS
SELECT
  dp.decision_id,
  dp.session_id,
  dp.span_id,
  dp.decision_type,
  dp.description
FROM `{project}.{receiver}.decision_points` AS dp
WHERE dp.session_id IN (
  SELECT receiver_session_id FROM `{project}.{auditor}.receiver_runs`
)
""",
    ),
    (
        "receiver_decision_options",
        # Same scoping rationale as receiver_planning_decisions.
        """\
CREATE OR REPLACE TABLE `{project}.{auditor}.receiver_decision_options` AS
SELECT
  c.candidate_id,
  c.decision_id,
  c.session_id,
  c.name,
  c.score,
  c.status,
  c.rejection_rationale
FROM `{project}.{receiver}.candidates` AS c
WHERE c.session_id IN (
  SELECT receiver_session_id FROM `{project}.{auditor}.receiver_runs`
)
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
  """Verify stitch coverage and traversal end-to-end.

  Four checks:

  1. Stitch coverage — every row in ``remote_agent_invocations``
     must have a matching row in ``joint_a2a_edges``. A "≥1 row
     traversal" check would silently pass on a partially broken
     auditor graph (3 remote calls, 1 stitched edge, downstream
     traversal returns the one stitched campaign and looks fine).
     This gate fails if any remote call is unstitched, with a
     pointer at Block 1 in ``bq_studio_queries.gql`` for diagnosis.
  2. Aggregate receiver-extraction coverage — scoped
     ``receiver_planning_decisions`` and ``receiver_decision_options``
     must meet the same thresholds ``build_org_graphs.py`` uses
     (defaults 3 / 9). Catches no-reset reruns where the unscoped
     receiver dataset satisfies the per-org gate but the scope
     chain dropped most rows.
  3. Per-receiver-session coverage — every stitched receiver
     session must have ≥1 decision and ≥3 candidates (the per-call
     receiver-prompt contract). Catches the case where (2) passes
     because some sessions over-produce while others have zero,
     leaving Block 4 empty for one specific campaign even though
     the aggregate count is fine.
  4. End-to-end traversal — walks all the way to
     ``ReceiverDecisionOption`` so a broken scope chain or a
     KEY/REFERENCES drift in the property graph DDL produces zero
     rows even when (1)-(3) pass.
  """
  coverage_q = f"""
    SELECT
      (SELECT COUNT(*) FROM
        `{PROJECT_ID}.{AUDITOR_DATASET_ID}.remote_agent_invocations`)
        AS a2a_calls,
      (SELECT COUNT(*) FROM
        `{PROJECT_ID}.{AUDITOR_DATASET_ID}.joint_a2a_edges`)
        AS stitched_edges
  """
  try:
    coverage_row = list(client.query(coverage_q).result())[0]
  except gax_exceptions.GoogleAPIError as exc:
    print(
        f"ERROR: stitch-coverage query failed: {exc}",
        file=sys.stderr,
    )
    return 1
  a2a_calls = int(coverage_row["a2a_calls"])
  stitched_edges = int(coverage_row["stitched_edges"])
  print(
      f"  stitch coverage: a2a_calls={a2a_calls}, "
      f"stitched_edges={stitched_edges}"
  )
  if a2a_calls == 0:
    print(
        "ERROR: zero remote_agent_invocations rows. The caller has "
        "no A2A_INTERACTION events; verify run_caller_agent.py "
        "produced them and that build_org_graphs.py ran.",
        file=sys.stderr,
    )
    return 1
  if stitched_edges != a2a_calls:
    unstitched = a2a_calls - stitched_edges
    print(
        f"ERROR: stitch coverage incomplete — {unstitched} of "
        f"{a2a_calls} remote calls have no matching receiver "
        "session. Run Block 1 in bq_studio_queries.gql to inspect; "
        "the most likely cause is the receiver session service "
        "rewriting session ids so caller.a2a_context_id no longer "
        "equals receiver.session_id.",
        file=sys.stderr,
    )
    return 1
  print("  stitch coverage OK — every remote call has a matching edge.")

  # Receiver-extraction coverage gate. Mirrors the thresholds in
  # build_org_graphs.py (≥3 decisions, ≥9 candidates for the
  # default 3-campaign demo) but runs against the *scoped* auditor
  # tables, so empty scoped projections are caught here even when
  # the unscoped receiver dataset satisfied build_org_graphs.py.
  scope_q = f"""
    SELECT
      (SELECT COUNT(*) FROM
        `{PROJECT_ID}.{AUDITOR_DATASET_ID}.receiver_planning_decisions`)
        AS scoped_decisions,
      (SELECT COUNT(*) FROM
        `{PROJECT_ID}.{AUDITOR_DATASET_ID}.receiver_decision_options`)
        AS scoped_candidates
  """
  try:
    scope_row = list(client.query(scope_q).result())[0]
  except gax_exceptions.GoogleAPIError as exc:
    print(
        f"ERROR: receiver-scope query failed: {exc}",
        file=sys.stderr,
    )
    return 1
  scoped_decisions = int(scope_row["scoped_decisions"])
  scoped_candidates = int(scope_row["scoped_candidates"])
  print(
      f"  receiver scope: scoped_decisions={scoped_decisions} "
      f"(min {MIN_SCOPED_DECISIONS}), "
      f"scoped_candidates={scoped_candidates} "
      f"(min {MIN_SCOPED_CANDIDATES})"
  )
  if (
      scoped_decisions < MIN_SCOPED_DECISIONS
      or scoped_candidates < MIN_SCOPED_CANDIDATES
  ):
    print(
        f"ERROR: scoped receiver-extraction coverage below threshold "
        f"({scoped_decisions} < {MIN_SCOPED_DECISIONS} decisions or "
        f"{scoped_candidates} < {MIN_SCOPED_CANDIDATES} candidates). "
        "build_org_graphs.py validates the full receiver dataset, "
        "but the auditor scope chain may have dropped most rows — "
        "no-reset reruns where receiver session ids changed will "
        "satisfy the global gate while leaving the scoped surface "
        "thin. Re-run after `./reset.sh && ./setup.sh`, or check "
        "that the receiver session service is honoring caller "
        "context_ids. Override the threshold via "
        "DEMO_MIN_RECEIVER_DECISIONS / DEMO_MIN_RECEIVER_CANDIDATES "
        "if intentionally running fewer campaigns.",
        file=sys.stderr,
    )
    return 1
  print("  receiver-scope OK (aggregate).")

  # Per-receiver-session coverage: every stitched receiver session
  # must have ≥1 retained decision and ≥3 candidates *traversable
  # through those retained decisions* (matching the receiver prompt
  # contract — one Audience-risk-review decision per A2A call with
  # three options weighed).
  #
  # The candidate join chains through rpd.decision_id rather than
  # joining options directly to the session — that's what the
  # property graph actually traverses
  # (ReceiverAgentRun -> ReceiverPlanningDecision ->
  # ReceiverDecisionOption). A direct session join would count
  # candidate rows whose decision_id is NOT retained in
  # receiver_planning_decisions; those rows would never appear in
  # Block 4's traversal, so counting them would let the gate pass
  # with an empty Block 4 result.
  per_session_q = f"""
    SELECT
      jae.receiver_session_id,
      COUNT(DISTINCT rpd.decision_id)  AS decision_count,
      COUNT(DISTINCT rdo.candidate_id) AS candidate_count
    FROM `{PROJECT_ID}.{AUDITOR_DATASET_ID}.joint_a2a_edges` AS jae
    LEFT JOIN
      `{PROJECT_ID}.{AUDITOR_DATASET_ID}.receiver_planning_decisions`
        AS rpd
      ON jae.receiver_session_id = rpd.session_id
    LEFT JOIN
      `{PROJECT_ID}.{AUDITOR_DATASET_ID}.receiver_decision_options`
        AS rdo
      ON rpd.decision_id = rdo.decision_id
    GROUP BY jae.receiver_session_id
    HAVING decision_count < 1 OR candidate_count < 3
  """
  try:
    bad_sessions = list(client.query(per_session_q).result())
  except gax_exceptions.GoogleAPIError as exc:
    print(
        f"ERROR: per-receiver-session coverage query failed: {exc}",
        file=sys.stderr,
    )
    return 1
  if bad_sessions:
    summary = ", ".join(
        f"{r['receiver_session_id']}(d={r['decision_count']},"
        f"c={r['candidate_count']})"
        for r in bad_sessions[:5]
    )
    print(
        f"ERROR: {len(bad_sessions)} stitched receiver session(s) "
        "lack the per-session contract (≥1 decision, ≥3 candidates "
        f"per call). Failing sessions (first 5): {summary}. The "
        "receiver prompt likely produced an unparseable response "
        "for these calls; inspect the receiver agent_events for "
        "those sessions and tighten receiver_agent/prompts.py if "
        "the LLM_RESPONSE text doesn't match the SELECTED|DROPPED "
        "shape.",
        file=sys.stderr,
    )
    return 1
  print("  receiver-scope OK (per-session).")

  # Headline traversal goes all the way to ReceiverDecisionOption
  # so a broken scope chain (empty receiver_planning_decisions or
  # receiver_decision_options) produces zero rows here even when
  # stitch coverage passed.
  traversal_q = f"""
    GRAPH `{PROJECT_ID}.{AUDITOR_DATASET_ID}.a2a_joint_context_graph`
    MATCH (campaign:CallerCampaignRun)
          -[:DelegatedVia]->(remote:RemoteAgentInvocation)
          -[:HandledBy]->(receiver:ReceiverAgentRun)
          -[:ReceiverMadeDecision]->(decision:ReceiverPlanningDecision)
          -[:ReceiverWeighedOption]->(option:ReceiverDecisionOption)
    RETURN
      campaign.campaign,
      remote.a2a_context_id,
      receiver.receiver_session_id,
      decision.decision_type,
      option.status
    LIMIT 5
  """
  try:
    rows = list(client.query(traversal_q).result())
  except gax_exceptions.GoogleAPIError as exc:
    print(
        f"ERROR: traversal smoke query failed: {exc}",
        file=sys.stderr,
    )
    return 1
  if not rows:
    print(
        "ERROR: end-to-end traversal returned zero rows even though "
        "stitch and receiver-scope coverage passed. This shouldn't "
        "happen unless the property graph DDL itself is broken; "
        "re-render and re-create.",
        file=sys.stderr,
    )
    return 1
  print(f"  traversal smoke: {len(rows)} row(s):")
  for row in rows:
    print(
        f"    campaign={row['campaign']!r} "
        f"context={row['a2a_context_id']!r} "
        f"receiver_session={row['receiver_session_id']!r} "
        f"decision={row['decision_type']!r} "
        f"option_status={row['status']!r}"
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
