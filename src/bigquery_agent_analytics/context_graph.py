# Copyright 2025 Google LLC
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

"""Context Graph: Property Graph for Agent Trace + Business Entity linking.

This module provides the "System of Reasoning" for enterprise agents by
cross-linking the **Technical Graph** (execution lineage from the ADK
BigQuery Agent Analytics Plugin) with a **Business Graph** (domain
entities extracted via ``AI.GENERATE``).

Key capabilities:

- **Business entity extraction** — Use ``AI.GENERATE`` to extract
  structured entities (e.g. Products, Targeting segments, Campaigns)
  from unstructured agent payloads. The model is prompted to return
  a JSON array; the SQL strips markdown fences and JSON_EXTRACT_ARRAY
  parses each entity.
- **Property Graph DDL** — Generate ``CREATE PROPERTY GRAPH`` DDL
  that formalizes Tech nodes, Biz nodes, ``CAUSED`` edges (parent→child
  span linkage), and ``EVALUATED`` cross-links.
- **GQL traversal** — Quantified-path GQL queries to answer "Why was
  X selected?" by tracing causal chains from a decision back to the
  business inputs.
- **World Change detection** — Compare business entities evaluated at
  agent-execution time against current availability to detect stale
  context in long-running A2A tasks.
- **Decision Semantics** — Model agent decision points with candidates,
  scores, selection/rejection status, and rejection rationale for
  EU audit compliance.

Example usage::

    from bigquery_agent_analytics.context_graph import ContextGraphManager

    cgm = ContextGraphManager(
        project_id="my-project",
        dataset_id="agent_analytics",
    )

    # Extract business entities from agent traces
    biz_nodes = cgm.extract_biz_nodes(session_ids=["sess-1"])

    # Generate Property Graph DDL
    ddl = cgm.get_property_graph_ddl(graph_name="my_context_graph")

    # Traverse reasoning chains via GQL
    chain = cgm.explain_decision(
        decision_event_type="HITL_CONFIRMATION_REQUEST_COMPLETED",
        biz_entity="Yahoo Homepage",
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
import logging
from typing import Any, Optional

from google.cloud import bigquery
from pydantic import BaseModel
from pydantic import Field

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Data Models                                                          #
# ------------------------------------------------------------------ #


@dataclass
class BizNode:
  """A business-domain entity extracted from agent traces.

  Attributes:
      span_id: The span from which this entity was extracted.
      session_id: Session that produced this entity.
      node_type: Entity category (e.g. "Product", "Targeting",
          "Campaign", "Budget").
      node_value: Entity value (e.g. "Yahoo Homepage",
          "Millennials", "$8,000").
      confidence: Extraction confidence score (0.0-1.0).
      metadata: Additional extraction metadata.
  """

  span_id: str
  session_id: str
  node_type: str
  node_value: str
  confidence: float = 1.0
  evaluated_at: Optional[datetime] = None
  artifact_uri: Optional[str] = None
  metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionPoint:
  """A decision point identified in an agent trace.

  Represents a moment where the agent evaluated multiple candidates
  and selected or rejected them based on scores and criteria.

  Attributes:
      decision_id: Unique identifier for this decision point.
      session_id: Session that contains this decision.
      span_id: The span where the decision was made.
      decision_type: Category of decision (e.g. "audience_selection",
          "placement_selection", "budget_allocation").
      description: Human-readable description of the decision.
      timestamp: When the decision was made.
      metadata: Additional decision metadata.
  """

  decision_id: str
  session_id: str
  span_id: str
  decision_type: str
  description: str = ""
  timestamp: Optional[datetime] = None
  metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
  """A candidate option evaluated at a decision point.

  Attributes:
      candidate_id: Unique identifier for this candidate.
      decision_id: The decision point this candidate belongs to.
      session_id: Session containing this candidate.
      name: Candidate name/label.
      score: Evaluation score (0.0-1.0).
      status: "SELECTED" or "DROPPED".
      rejection_rationale: Why the candidate was dropped (required for
          DROPPED candidates, supports EU audit compliance).
      properties: Additional candidate properties (e.g. reach, cost).
  """

  candidate_id: str
  decision_id: str
  session_id: str
  name: str
  score: float = 0.0
  status: str = "SELECTED"
  rejection_rationale: Optional[str] = None
  properties: dict[str, Any] = field(default_factory=dict)


class WorldChangeAlert(BaseModel):
  """An alert indicating a business entity has changed since evaluation.

  Attributes:
      biz_node: The business entity that changed.
      original_state: State at the time the agent evaluated it.
      current_state: Current state.
      drift_type: Type of drift (e.g. "unavailable",
          "price_changed", "inventory_depleted").
      severity: Drift severity (0.0-1.0).
      recommendation: Suggested action.
  """

  biz_node: str = Field(description="The business entity that changed.")
  original_state: str = Field(description="State when the agent evaluated it.")
  current_state: str = Field(description="Current state.")
  drift_type: str = Field(description="Type of drift detected.")
  severity: float = Field(description="Drift severity (0.0-1.0).")
  recommendation: str = Field(
      default="Review before approving.",
      description="Suggested action.",
  )


class WorldChangeReport(BaseModel):
  """Report on world-state drift for a long-running agent task.

  Attributes:
      session_id: The session under review.
      alerts: List of detected world changes.
      total_entities_checked: Number of entities checked.
      stale_entities: Number of entities that drifted.
      is_safe_to_approve: Whether the context is still valid.
      checked_at: When the check was performed.
  """

  session_id: str = Field(description="Session under review.")
  alerts: list[WorldChangeAlert] = Field(default_factory=list)
  total_entities_checked: int = Field(default=0)
  stale_entities: int = Field(default=0)
  is_safe_to_approve: bool = Field(default=True)
  check_failed: bool = Field(
      default=False,
      description=(
          "True when the underlying query or state check could "
          "not complete.  When True, is_safe_to_approve=False "
          "and the report should NOT be used for HITL approval."
      ),
  )
  checked_at: datetime = Field(
      default_factory=lambda: datetime.now(timezone.utc)
  )

  model_config = {"arbitrary_types_allowed": True}

  def summary(self) -> str:
    """Returns a human-readable summary."""
    lines = [
        f"World Change Report — Session: {self.session_id}",
        f"  Entities checked : {self.total_entities_checked}",
        f"  Stale entities   : {self.stale_entities}",
        f"  Safe to approve  : {self.is_safe_to_approve}",
    ]
    if self.check_failed:
      lines.append("  CHECK FAILED     : query or state check error")
    for alert in self.alerts:
      lines.append(
          f"  [{alert.drift_type}] {alert.biz_node}: "
          f"{alert.original_state} -> {alert.current_state} "
          f"(severity={alert.severity:.2f})"
      )
    return "\n".join(lines)


class ContextGraphConfig(BaseModel):
  """Configuration for the Context Graph.

  Attributes:
      biz_nodes_table: Table name for extracted business nodes.
      cross_links_table: Table name for cross-link edges.
      graph_name: Name for the Property Graph.
      endpoint: AI.GENERATE endpoint for entity extraction.
      entity_types: Domain-specific entity types to extract.
      max_hops: Maximum causal hops for GQL traversal.
  """

  biz_nodes_table: str = Field(default="extracted_biz_nodes")
  cross_links_table: str = Field(default="context_cross_links")
  decision_points_table: str = Field(default="decision_points")
  candidates_table: str = Field(default="candidates")
  made_decision_edges_table: str = Field(default="made_decision_edges")
  candidate_edges_table: str = Field(default="candidate_edges")
  graph_name: str = Field(default="agent_context_graph")
  endpoint: str = Field(default="gemini-2.5-flash")
  entity_types: list[str] = Field(
      default_factory=lambda: [
          "Product",
          "Targeting",
          "Campaign",
          "Budget",
          "Audience",
          "Creative",
          "Placement",
      ]
  )
  max_hops: int = Field(default=20)


# ------------------------------------------------------------------ #
# SQL Templates                                                        #
# ------------------------------------------------------------------ #

_EXTRACT_BIZ_NODES_QUERY = """\
MERGE `{project}.{dataset}.{biz_table}` AS target
USING (
  SELECT
    CONCAT(base.span_id, ':', JSON_EXTRACT_SCALAR(entity, '$.entity_type'),
           ':', JSON_EXTRACT_SCALAR(entity, '$.entity_value')
    ) AS biz_node_id,
    base.span_id,
    base.session_id,
    JSON_EXTRACT_SCALAR(entity, '$.entity_type') AS node_type,
    JSON_EXTRACT_SCALAR(entity, '$.entity_value') AS node_value,
    CAST(
      COALESCE(JSON_EXTRACT_SCALAR(entity, '$.confidence'), '1.0')
      AS FLOAT64
    ) AS confidence,
    -- Persisted artifact URI from content_parts[].object_ref.uri
    (SELECT JSON_EXTRACT_SCALAR(cp, '$.object_ref.uri')
     FROM UNNEST(JSON_EXTRACT_ARRAY(TO_JSON_STRING(base.content_parts)))
       AS cp WITH OFFSET
     WHERE JSON_EXTRACT_SCALAR(cp, '$.object_ref.uri') IS NOT NULL
     ORDER BY OFFSET LIMIT 1
    ) AS artifact_uri
  FROM `{project}.{dataset}.{table}` AS base,
  UNNEST(JSON_EXTRACT_ARRAY(
    -- Strip markdown code fences (```json ... ```) from LLM output
    REGEXP_REPLACE(
      REGEXP_REPLACE(
        AI.GENERATE(
          CONCAT(
            'Extract business entities from this agent payload. ',
            'Entity types: {entity_types}. ',
            'Return a JSON array of objects with entity_type, ',
            'entity_value, and confidence (0-1).',
            '\\n\\nPayload:\\n',
            COALESCE(
              JSON_EXTRACT_SCALAR(base.content, '$.text_summary'),
              JSON_EXTRACT_SCALAR(base.content, '$.response'),
              JSON_EXTRACT_SCALAR(base.content, '$.text'),
              TO_JSON_STRING(base.content)
            )
          ),
          endpoint => '{endpoint}'
        ).result,
        r'^```(?:json)?\\s*', ''),
      r'\\s*```$', '')
  )) AS entity
  WHERE base.session_id IN UNNEST(@session_ids)
    AND base.event_type IN (
      'USER_MESSAGE_RECEIVED',
      'LLM_RESPONSE',
      'TOOL_COMPLETED',
      'AGENT_COMPLETED'
    )
    AND base.content IS NOT NULL
) AS source
ON target.biz_node_id = source.biz_node_id
WHEN MATCHED THEN
  UPDATE SET confidence = source.confidence,
             artifact_uri = source.artifact_uri
WHEN NOT MATCHED BY TARGET THEN
  INSERT (biz_node_id, span_id, session_id, node_type, node_value,
          confidence, artifact_uri)
  VALUES (source.biz_node_id, source.span_id, source.session_id,
          source.node_type, source.node_value, source.confidence,
          source.artifact_uri)
WHEN NOT MATCHED BY SOURCE
  AND target.session_id IN UNNEST(@session_ids) THEN
  DELETE
"""

_EXTRACT_BIZ_NODES_SIMPLE_QUERY = """\
SELECT
  base.span_id,
  base.session_id,
  base.event_type,
  COALESCE(
    JSON_EXTRACT_SCALAR(base.content, '$.text_summary'),
    JSON_EXTRACT_SCALAR(base.content, '$.response'),
    JSON_EXTRACT_SCALAR(base.content, '$.text'),
    TO_JSON_STRING(base.content)
  ) AS payload_text
FROM `{project}.{dataset}.{table}` AS base
WHERE base.session_id IN UNNEST(@session_ids)
  AND base.event_type IN (
    'USER_MESSAGE_RECEIVED',
    'LLM_RESPONSE',
    'TOOL_COMPLETED',
    'AGENT_COMPLETED'
  )
  AND base.content IS NOT NULL
ORDER BY base.timestamp ASC
"""

_CREATE_BIZ_NODES_TABLE_QUERY = """\
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{biz_table}` (
  biz_node_id STRING,
  span_id STRING,
  session_id STRING,
  node_type STRING,
  node_value STRING,
  confidence FLOAT64,
  artifact_uri STRING
)
"""

_DELETE_BIZ_NODES_FOR_SESSIONS_QUERY = """\
DELETE FROM `{project}.{dataset}.{biz_table}`
WHERE session_id IN UNNEST(@session_ids)
"""

_INSERT_BIZ_NODES_QUERY = """\
INSERT INTO `{project}.{dataset}.{biz_table}`
  (biz_node_id, span_id, session_id, node_type, node_value, confidence)
VALUES
  {values}
"""

_CREATE_CROSS_LINKS_TABLE_QUERY = """\
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{cross_links_table}` (
  link_id STRING,
  span_id STRING,
  session_id STRING,
  biz_node_id STRING,
  node_value STRING,
  link_type STRING,
  artifact_uri STRING,
  created_at TIMESTAMP
)
"""

_DELETE_CROSS_LINKS_FOR_SESSIONS_QUERY = """\
DELETE FROM `{project}.{dataset}.{cross_links_table}`
WHERE session_id IN UNNEST(@session_ids)
"""

_INSERT_CROSS_LINKS_QUERY = """\
INSERT INTO `{project}.{dataset}.{cross_links_table}`
  (link_id, span_id, session_id, biz_node_id, node_value, link_type,
   artifact_uri, created_at)
SELECT
  b.biz_node_id AS link_id,
  b.span_id,
  b.session_id,
  b.biz_node_id,
  b.node_value,
  'EVALUATED' AS link_type,
  b.artifact_uri,
  CURRENT_TIMESTAMP() AS created_at
FROM `{project}.{dataset}.{biz_table}` b
WHERE b.session_id IN UNNEST(@session_ids)
"""

_CREATE_DECISION_POINTS_TABLE_QUERY = """\
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{decision_points_table}` (
  decision_id STRING,
  session_id STRING,
  span_id STRING,
  decision_type STRING,
  description STRING
)
"""

_CREATE_CANDIDATES_TABLE_QUERY = """\
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{candidates_table}` (
  candidate_id STRING,
  decision_id STRING,
  session_id STRING,
  name STRING,
  score FLOAT64,
  status STRING,
  rejection_rationale STRING
)
"""

_CREATE_MADE_DECISION_EDGES_TABLE_QUERY = """\
CREATE TABLE IF NOT EXISTS
  `{project}.{dataset}.{made_decision_edges_table}` (
  edge_id STRING,
  span_id STRING,
  decision_id STRING,
  created_at TIMESTAMP
)
"""

_CREATE_CANDIDATE_EDGES_TABLE_QUERY = """\
CREATE TABLE IF NOT EXISTS
  `{project}.{dataset}.{candidate_edges_table}` (
  edge_id STRING,
  decision_id STRING,
  candidate_id STRING,
  edge_type STRING,
  rejection_rationale STRING,
  created_at TIMESTAMP
)
"""

_EXTRACT_DECISION_POINTS_QUERY = """\
SELECT
  base.span_id,
  base.session_id,
  base.event_type,
  COALESCE(
    JSON_EXTRACT_SCALAR(base.content, '$.text_summary'),
    JSON_EXTRACT_SCALAR(base.content, '$.response'),
    JSON_EXTRACT_SCALAR(base.content, '$.text'),
    TO_JSON_STRING(base.content)
  ) AS payload_text
FROM `{project}.{dataset}.{table}` AS base
WHERE base.session_id IN UNNEST(@session_ids)
  AND base.event_type IN (
    'LLM_RESPONSE',
    'TOOL_COMPLETED',
    'AGENT_COMPLETED',
    'HITL_CONFIRMATION_REQUEST_COMPLETED'
  )
  AND base.content IS NOT NULL
ORDER BY base.timestamp ASC
"""

_EXTRACT_DECISION_POINTS_AI_QUERY = """\
SELECT
  base.span_id,
  base.session_id,
  TO_JSON_STRING(
    AI.GENERATE(
      CONCAT(
        'Extract agent decision points from the payload. ',
        'A decision point is present only when the payload shows the ',
        'agent evaluated multiple candidates/options and selected or ',
        'rejected them. Return zero decisions if no such decision is ',
        'present. Preserve candidate names and rejection rationale text ',
        'from the payload. Use status SELECTED or DROPPED. Use null ',
        'rejection_rationale for selected candidates. Scores must be ',
        'FLOAT64 values between 0 and 1 when present, otherwise 0.0.',
        '\\n\\nPayload:\\n',
        COALESCE(
          JSON_EXTRACT_SCALAR(base.content, '$.text_summary'),
          JSON_EXTRACT_SCALAR(base.content, '$.response'),
          JSON_EXTRACT_SCALAR(base.content, '$.text'),
          TO_JSON_STRING(base.content)
        )
      ),
      endpoint => '{endpoint}',
      model_params => JSON '{{"generationConfig": {{"temperature": 0.0, "topP": 0.1, "maxOutputTokens": 2048}}}}',
      output_schema => 'decisions ARRAY<STRUCT<decision_type STRING, description STRING, candidates ARRAY<STRUCT<name STRING, score FLOAT64, status STRING, rejection_rationale STRING>>>>'
    ).decisions
  ) AS decisions_json
FROM `{project}.{dataset}.{table}` AS base
WHERE base.session_id IN UNNEST(@session_ids)
  AND base.event_type IN (
    'LLM_RESPONSE',
    'TOOL_COMPLETED',
    'AGENT_COMPLETED',
    'HITL_CONFIRMATION_REQUEST_COMPLETED'
  )
  AND base.content IS NOT NULL
ORDER BY base.timestamp ASC
"""

_DELETE_DECISION_POINTS_FOR_SESSIONS_QUERY = """\
DELETE FROM `{project}.{dataset}.{decision_points_table}`
WHERE session_id IN UNNEST(@session_ids)
"""

_DELETE_CANDIDATES_FOR_SESSIONS_QUERY = """\
DELETE FROM `{project}.{dataset}.{candidates_table}`
WHERE session_id IN UNNEST(@session_ids)
"""

_DELETE_MADE_DECISION_EDGES_FOR_SESSIONS_QUERY = """\
DELETE FROM `{project}.{dataset}.{made_decision_edges_table}`
WHERE decision_id IN (
  SELECT decision_id
  FROM `{project}.{dataset}.{decision_points_table}`
  WHERE session_id IN UNNEST(@session_ids)
)
"""

_DELETE_CANDIDATE_EDGES_FOR_SESSIONS_QUERY = """\
DELETE FROM `{project}.{dataset}.{candidate_edges_table}`
WHERE decision_id IN (
  SELECT decision_id
  FROM `{project}.{dataset}.{decision_points_table}`
  WHERE session_id IN UNNEST(@session_ids)
)
"""

_INSERT_MADE_DECISION_EDGES_QUERY = """\
INSERT INTO `{project}.{dataset}.{made_decision_edges_table}`
  (edge_id, span_id, decision_id, created_at)
SELECT
  CONCAT(dp.span_id, ':MADE_DECISION:', dp.decision_id) AS edge_id,
  dp.span_id,
  dp.decision_id,
  CURRENT_TIMESTAMP() AS created_at
FROM `{project}.{dataset}.{decision_points_table}` dp
WHERE dp.session_id IN UNNEST(@session_ids)
"""

_INSERT_CANDIDATE_EDGES_QUERY = """\
INSERT INTO `{project}.{dataset}.{candidate_edges_table}`
  (edge_id, decision_id, candidate_id, edge_type,
   rejection_rationale, created_at)
SELECT
  CONCAT(c.decision_id, ':', c.status, ':', c.candidate_id) AS edge_id,
  c.decision_id,
  c.candidate_id,
  CASE c.status
    WHEN 'SELECTED' THEN 'SELECTED_CANDIDATE'
    ELSE 'DROPPED_CANDIDATE'
  END AS edge_type,
  c.rejection_rationale,
  CURRENT_TIMESTAMP() AS created_at
FROM `{project}.{dataset}.{candidates_table}` c
WHERE c.session_id IN UNNEST(@session_ids)
"""

_DECISION_POINTS_FOR_SESSION_QUERY = """\
SELECT
  dp.decision_id,
  dp.session_id,
  dp.span_id,
  dp.decision_type,
  dp.description
FROM `{project}.{dataset}.{decision_points_table}` dp
WHERE dp.session_id = @session_id
"""

_CANDIDATES_FOR_DECISION_QUERY = """\
SELECT
  c.candidate_id,
  c.decision_id,
  c.session_id,
  c.name,
  c.score,
  c.status,
  c.rejection_rationale
FROM `{project}.{dataset}.{candidates_table}` c
WHERE c.decision_id = @decision_id
ORDER BY c.score DESC
"""

_CANDIDATES_FOR_SESSION_QUERY = """\
SELECT
  c.candidate_id,
  c.decision_id,
  c.session_id,
  c.name,
  c.score,
  c.status,
  c.rejection_rationale
FROM `{project}.{dataset}.{candidates_table}` c
WHERE c.session_id = @session_id
ORDER BY c.decision_id, c.score DESC
"""

_PROPERTY_GRAPH_DDL = """\
CREATE OR REPLACE PROPERTY GRAPH `{project}.{dataset}.{graph_name}`
  NODE TABLES (
    -- Technical execution nodes (spans from ADK plugin)
    `{project}.{dataset}.{table}` AS TechNode
      KEY (span_id)
      LABEL TechNode
      PROPERTIES (
        event_type,
        agent,
        timestamp,
        session_id,
        invocation_id,
        content,
        latency_ms,
        status,
        error_message
      ),
    -- Business domain nodes (extracted entities, keyed by composite ID)
    `{project}.{dataset}.{biz_table}` AS BizNode
      KEY (biz_node_id)
      LABEL BizNode
      PROPERTIES (
        node_type,
        node_value,
        confidence,
        session_id,
        span_id,
        artifact_uri
      )
  )
  EDGE TABLES (
    -- Causal lineage: parent span -> child span
    `{project}.{dataset}.{table}` AS Caused
      KEY (span_id)
      SOURCE KEY (parent_span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (span_id) REFERENCES TechNode (span_id)
      LABEL Caused,

    -- Cross-link: technical event -> business entity it evaluated
    `{project}.{dataset}.{cross_links_table}` AS Evaluated
      KEY (link_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (biz_node_id) REFERENCES BizNode (biz_node_id)
      LABEL Evaluated
      PROPERTIES (
        artifact_uri,
        link_type,
        created_at
      )
  )
"""

_GQL_REASONING_CHAIN_QUERY = """\
GRAPH `{project}.{dataset}.{graph_name}`
MATCH
  (decision:TechNode)-[c:Caused]->{{1,{max_hops}}}(step:TechNode)
    -[e:Evaluated]->(biz:BizNode)
WHERE decision.event_type = @decision_event_type
  {biz_filter_clause}
RETURN
  TO_JSON(decision) AS decision_node,
  decision.span_id AS decision_span_id,
  decision.event_type AS decision_type,
  step.span_id AS reasoning_span_id,
  step.event_type AS step_type,
  step.agent AS step_agent,
  COALESCE(
    JSON_EXTRACT_SCALAR(step.content, '$.text_summary'),
    JSON_EXTRACT_SCALAR(step.content, '$.response'),
    ''
  ) AS reasoning_text,
  step.latency_ms AS step_latency_ms,
  biz.node_type AS entity_type,
  biz.node_value AS entity_value,
  biz.confidence AS entity_confidence,
  TO_JSON(step) AS step_node,
  TO_JSON(biz) AS biz_node
ORDER BY step.timestamp ASC
LIMIT @result_limit
"""

_GQL_FULL_CAUSAL_CHAIN_QUERY = """\
GRAPH `{project}.{dataset}.{graph_name}`
MATCH
  (root:TechNode)-[c:Caused]->{{1,{max_hops}}}(leaf:TechNode)
WHERE root.session_id = @session_id
  AND root.event_type = 'USER_MESSAGE_RECEIVED'
RETURN
  TO_JSON(root) AS root_node,
  root.span_id AS root_span_id,
  leaf.span_id AS leaf_span_id,
  leaf.event_type AS leaf_event_type,
  leaf.agent AS leaf_agent,
  COALESCE(
    JSON_EXTRACT_SCALAR(leaf.content, '$.text_summary'),
    JSON_EXTRACT_SCALAR(leaf.content, '$.response'),
    ''
  ) AS leaf_content,
  leaf.latency_ms AS leaf_latency_ms,
  TO_JSON(leaf) AS leaf_node,
  TO_JSON(c) AS edge
ORDER BY leaf.timestamp ASC
LIMIT @result_limit
"""

_GQL_TRACE_RECONSTRUCTION_QUERY = """\
GRAPH `{project}.{dataset}.{graph_name}`
MATCH
  (parent:TechNode)-[c:Caused]->(child:TechNode)
WHERE parent.session_id = @session_id
   OR child.session_id = @session_id
RETURN
  parent.span_id AS parent_span_id,
  parent.event_type AS parent_event_type,
  parent.agent AS parent_agent,
  parent.timestamp AS parent_timestamp,
  parent.session_id AS session_id,
  parent.invocation_id AS parent_invocation_id,
  parent.content AS parent_content,
  parent.latency_ms AS parent_latency_ms,
  parent.status AS parent_status,
  parent.error_message AS parent_error_message,
  child.span_id AS child_span_id,
  child.event_type AS child_event_type,
  child.agent AS child_agent,
  child.timestamp AS child_timestamp,
  child.invocation_id AS child_invocation_id,
  child.content AS child_content,
  child.latency_ms AS child_latency_ms,
  child.status AS child_status,
  child.error_message AS child_error_message
ORDER BY child.timestamp ASC
LIMIT @result_limit
"""

_BIZ_NODES_FOR_SESSION_QUERY = """\
SELECT
  biz_node_id,
  node_type,
  node_value,
  confidence,
  span_id,
  session_id,
  artifact_uri
FROM `{project}.{dataset}.{biz_table}`
WHERE session_id = @session_id
ORDER BY confidence DESC
"""

_WORLD_CHANGE_CHECK_QUERY = """\
SELECT
  b.node_type,
  b.node_value,
  b.confidence,
  b.span_id,
  e.timestamp AS evaluated_at
FROM `{project}.{dataset}.{biz_table}` b
JOIN `{project}.{dataset}.{table}` e
  ON b.span_id = e.span_id
WHERE b.session_id = @session_id
ORDER BY e.timestamp ASC
"""

# ------------------------------------------------------------------ #
# Decision Semantics: Extended Property Graph DDL                      #
# ------------------------------------------------------------------ #

_DECISION_PROPERTY_GRAPH_DDL = """\
CREATE OR REPLACE PROPERTY GRAPH `{project}.{dataset}.{graph_name}`
  NODE TABLES (
    -- Technical execution nodes (spans from ADK plugin)
    `{project}.{dataset}.{table}` AS TechNode
      KEY (span_id)
      LABEL TechNode
      PROPERTIES (
        event_type,
        agent,
        timestamp,
        session_id,
        invocation_id,
        content,
        latency_ms,
        status,
        error_message
      ),
    -- Business domain nodes (extracted entities)
    `{project}.{dataset}.{biz_table}` AS BizNode
      KEY (biz_node_id)
      LABEL BizNode
      PROPERTIES (
        node_type,
        node_value,
        confidence,
        session_id,
        span_id,
        artifact_uri
      ),
    -- Decision point nodes
    `{project}.{dataset}.{decision_points_table}` AS DecisionPoint
      KEY (decision_id)
      LABEL DecisionPoint
      PROPERTIES (
        session_id,
        span_id,
        decision_type,
        description
      ),
    -- Candidate nodes
    `{project}.{dataset}.{candidates_table}` AS CandidateNode
      KEY (candidate_id)
      LABEL CandidateNode
      PROPERTIES (
        decision_id,
        session_id,
        name,
        score,
        status,
        rejection_rationale
      )
  )
  EDGE TABLES (
    -- Causal lineage: parent span -> child span
    `{project}.{dataset}.{table}` AS Caused
      KEY (span_id)
      SOURCE KEY (parent_span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (span_id) REFERENCES TechNode (span_id)
      LABEL Caused,

    -- Cross-link: technical event -> business entity it evaluated
    `{project}.{dataset}.{cross_links_table}` AS Evaluated
      KEY (link_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (biz_node_id) REFERENCES BizNode (biz_node_id)
      LABEL Evaluated
      PROPERTIES (
        artifact_uri,
        link_type,
        created_at
      ),

    -- TechNode -> DecisionPoint (span that made the decision)
    `{project}.{dataset}.{made_decision_edges_table}` AS MadeDecision
      KEY (edge_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      LABEL MadeDecision,

    -- DecisionPoint -> CandidateNode (selected or dropped)
    `{project}.{dataset}.{candidate_edges_table}` AS CandidateEdge
      KEY (edge_id)
      SOURCE KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      DESTINATION KEY (candidate_id) REFERENCES CandidateNode (candidate_id)
      LABEL CandidateEdge
      PROPERTIES (
        edge_type,
        rejection_rationale,
        created_at
      )
  )
"""

# ------------------------------------------------------------------ #
# Decision Semantics: GQL Queries                                      #
# ------------------------------------------------------------------ #

_GQL_EU_AUDIT_QUERY = """\
GRAPH `{project}.{dataset}.{graph_name}`
MATCH
  (step:TechNode)-[md:MadeDecision]->(dp:DecisionPoint)
    -[ce:CandidateEdge]->(cand:CandidateNode)
WHERE dp.session_id = @session_id
  {decision_type_clause}
RETURN
  dp.decision_id,
  dp.decision_type,
  dp.description AS decision_description,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.status AS candidate_status,
  cand.rejection_rationale,
  ce.edge_type,
  step.span_id,
  step.event_type,
  step.agent
ORDER BY dp.decision_id, cand.score DESC
LIMIT @result_limit
"""

_GQL_DROPPED_CANDIDATES_QUERY = """\
GRAPH `{project}.{dataset}.{graph_name}`
MATCH
  (dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)
WHERE dp.session_id = @session_id
  AND ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  dp.decision_id,
  dp.decision_type,
  dp.description AS decision_description,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.rejection_rationale
ORDER BY dp.decision_id, cand.score DESC
LIMIT @result_limit
"""


def _dedupe_rows_by_key(
    rows: list[dict[str, Any]], key: str
) -> list[dict[str, Any]]:
  """Return ``rows`` with one entry per ``rows[i][key]``, last-wins.

  The property-graph DDL declares ``decision_id`` and
  ``candidate_id`` (and ``biz_node_id``) as ``KEY``, so the backing
  tables must have at most one row per key. This helper handles
  the **in-batch** duplicate case: ``AI.GENERATE`` can return
  multiple objects that the SDK maps to the same composite key
  (e.g. the same decision surfacing from two LLM_RESPONSE rows).

  The cross-batch / rerun case (a prior load is invisible to a
  subsequent ``DELETE``) is handled by writing through
  ``_append_rows_via_load_job`` — load jobs write to managed
  storage, not the streaming buffer.
  """
  seen: dict[Any, dict[str, Any]] = {}
  for row in rows:
    seen[row[key]] = row
  return list(seen.values())


# ------------------------------------------------------------------ #
# ContextGraphManager                                                  #
# ------------------------------------------------------------------ #


class ContextGraphManager:
  """Manages the Context Graph linking technical traces to business entities.

  This is the main entry point for building and querying the
  "System of Reasoning" Property Graph.

  Args:
      project_id: Google Cloud project ID.
      dataset_id: BigQuery dataset ID.
      table_id: Agent events table name.
      config: Optional context graph configuration.
      client: Optional BigQuery client instance.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      config: Optional[ContextGraphConfig] = None,
      client: Optional[bigquery.Client] = None,
      location: str = "US",
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self.config = config or ContextGraphConfig()
    self._client = client
    self._warned_unlabeled_client = False
    self.location = location

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id, location=self.location)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  def _resolve_endpoint(self) -> str:
    """Resolves the AI.GENERATE endpoint to a full Vertex AI URL.

    Short model names like ``gemini-2.5-flash`` work for older models,
    but newer models (Gemini 3.x+) require the full Vertex AI endpoint
    URL.  This method converts short names to full URLs when necessary.

    Raises:
        ValueError: If the endpoint looks like a legacy BQ ML model
            reference (``project.dataset.model``), which is not
            compatible with AI.GENERATE.
    """
    ep = self.config.endpoint
    if ep.startswith("https://"):
      return ep
    # Reject legacy BQ ML model refs (project.dataset.model)
    if ep.count(".") >= 2:
      raise ValueError(
          f"Legacy BQ ML model reference '{ep}' is not supported "
          f"for AI.GENERATE. Use a Vertex AI model name "
          f"(e.g. 'gemini-2.5-flash') or a full endpoint URL."
      )
    return (
        f"https://aiplatform.googleapis.com/v1/projects/"
        f"{self.project_id}/locations/global/publishers/google/"
        f"models/{ep}"
    )

  # -------------------------------------------------------------- #
  # Business Entity Extraction                                       #
  # -------------------------------------------------------------- #

  def extract_biz_nodes(
      self,
      session_ids: list[str],
      use_ai_generate: bool = True,
  ) -> list[BizNode]:
    """Extracts business entities from agent trace payloads.

    When *use_ai_generate* is True, runs the extraction as a
    BigQuery ``AI.GENERATE`` job that populates the biz nodes
    table directly.  When False, fetches payloads and returns
    them for client-side extraction.

    Args:
        session_ids: Sessions to extract entities from.
        use_ai_generate: Whether to use BigQuery AI.GENERATE
            (server-side) for extraction.

    Returns:
        List of extracted BizNode objects.
    """
    if use_ai_generate:
      return self._extract_via_ai_generate(session_ids)
    return self._extract_payloads_for_client(session_ids)

  def _ensure_biz_nodes_table(self) -> None:
    """Creates the biz_nodes table if it does not exist."""
    ddl = _CREATE_BIZ_NODES_TABLE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        biz_table=self.config.biz_nodes_table,
    )
    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(), feature="context-graph"
    )
    job = self.client.query(ddl, job_config=job_config)
    job.result()

  def _delete_biz_nodes_for_sessions(self, session_ids: list[str]) -> None:
    """Deletes existing BizNode rows for the given sessions.

    Mirrors ``_delete_decision_data_for_sessions``. Without this
    step ``store_biz_nodes`` is not rerun-idempotent: load-job
    appends would stack rows with duplicate ``biz_node_id`` keys,
    violating the ``BizNode KEY (biz_node_id)`` graph contract.

    Swallows "table does not exist" errors so the first run
    against an empty dataset is not a failure.
    """
    if not session_ids:
      return
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")
    delete_query = _DELETE_BIZ_NODES_FOR_SESSIONS_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        biz_table=self.config.biz_nodes_table,
    )
    try:
      job = self.client.query(delete_query, job_config=job_config)
      job.result()
    except Exception as e:  # pylint: disable=broad-except
      msg = str(e).lower()
      if "not found" in msg or "does not exist" in msg:
        logger.debug("biz_nodes table does not exist yet: %s", e)
      else:
        logger.warning("biz_nodes delete failed: %s", e)

  def _append_rows_via_load_job(
      self,
      table_ref: str,
      rows: list[dict[str, Any]],
      feature_label: str,
  ) -> bool:
    """Append ``rows`` to ``table_ref`` via a BigQuery load job.

    Load jobs write rows directly to managed storage, so a
    subsequent ``DELETE`` DML in the same Python session sees the
    rows and can evict them. The legacy streaming insert path
    (``insert_rows_json``) buffers writes for ~30 minutes and the
    DML cannot see buffered rows during that window — that gap is
    what produced duplicate-key data on rerun before this fix.

    Returns False (and logs a warning) on any load-job failure.
    """
    if not rows:
      return True
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")
    try:
      job = self.client.load_table_from_json(
          rows, table_ref, job_config=job_config
      )
      job.result()
      logger.info("Loaded %d %s into %s", len(rows), feature_label, table_ref)
      return True
    except Exception as e:  # pylint: disable=broad-except
      logger.warning(
          "Load job for %s failed (%s): %s", feature_label, table_ref, e
      )
      return False

  def _extract_via_ai_generate(self, session_ids: list[str]) -> list[BizNode]:
    """Server-side extraction using AI.GENERATE with MERGE upsert."""
    self._ensure_biz_nodes_table()

    entity_types_str = ", ".join(self.config.entity_types)
    query = _EXTRACT_BIZ_NODES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        biz_table=self.config.biz_nodes_table,
        endpoint=self._resolve_endpoint(),
        entity_types=entity_types_str,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="context-graph", ai_function="ai-generate"
    )

    try:
      job = self.client.query(query, job_config=job_config)
      job.result()
      logger.info(
          "AI.GENERATE extraction complete — results in %s.%s",
          self.dataset_id,
          self.config.biz_nodes_table,
      )
    except Exception as e:
      logger.warning("AI.GENERATE extraction failed: %s", e)
      return []

    return self._read_biz_nodes(session_ids)

  def _extract_payloads_for_client(
      self, session_ids: list[str]
  ) -> list[BizNode]:
    """Fetches payloads for client-side entity extraction."""
    query = _EXTRACT_BIZ_NODES_SIMPLE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      nodes = []
      for row in rows:
        nodes.append(
            BizNode(
                span_id=row.get("span_id", ""),
                session_id=row.get("session_id", ""),
                node_type="raw_payload",
                node_value=row.get("payload_text", ""),
            )
        )
      return nodes
    except Exception as e:
      logger.warning("Payload extraction failed: %s", e)
      return []

  def _read_biz_nodes(self, session_ids: list[str]) -> list[BizNode]:
    """Reads extracted biz nodes from the output table."""
    query = f"""\
    SELECT span_id, session_id, node_type, node_value, confidence,
           artifact_uri
    FROM `{self.project_id}.{self.dataset_id}.{self.config.biz_nodes_table}`
    WHERE session_id IN UNNEST(@session_ids)
    ORDER BY confidence DESC
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [
          BizNode(
              span_id=row.get("span_id", ""),
              session_id=row.get("session_id", ""),
              node_type=row.get("node_type", ""),
              node_value=row.get("node_value", ""),
              confidence=float(row.get("confidence", 1.0)),
              artifact_uri=row.get("artifact_uri"),
          )
          for row in rows
      ]
    except Exception as e:
      logger.warning("Failed to read biz nodes: %s", e)
      return []

  def store_biz_nodes(self, nodes: list[BizNode]) -> bool:
    """Stores pre-extracted business nodes into BigQuery.

    Use this when entities are extracted client-side (e.g. via
    the Gemini API directly) rather than through AI.GENERATE.

    Args:
        nodes: List of BizNode objects to store.

    Returns:
        True if successful.
    """
    if not nodes:
      return True

    create_query = _CREATE_BIZ_NODES_TABLE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        biz_table=self.config.biz_nodes_table,
    )

    try:
      job_config = with_sdk_labels(
          bigquery.QueryJobConfig(), feature="context-graph"
      )
      job = self.client.query(create_query, job_config=job_config)
      job.result()
    except Exception as e:
      logger.warning("Failed to create biz nodes table: %s", e)
      return False

    rows = [
        {
            "biz_node_id": f"{n.span_id}:{n.node_type}:{n.node_value}",
            "span_id": n.span_id,
            "session_id": n.session_id,
            "node_type": n.node_type,
            "node_value": n.node_value,
            "confidence": n.confidence,
            "artifact_uri": n.artifact_uri,
        }
        for n in nodes
    ]
    # The BizNode property-graph KEY is biz_node_id; dedupe before
    # the load so multiple BizNode objects mapping to the same
    # composite id never produce a duplicate row in the table.
    rows = _dedupe_rows_by_key(rows, "biz_node_id")

    # Idempotent rerun: delete prior BizNode rows for the sessions
    # we are about to load. Without this, a re-call of
    # store_biz_nodes() would stack appended rows with the same
    # biz_node_id and violate the BizNode KEY graph contract. The
    # delete + load pair must run in order; load_table_from_json
    # writes to managed storage so the just-issued DELETE is
    # visible by the time the load lands.
    session_ids = list({r["session_id"] for r in rows if r["session_id"]})
    self._delete_biz_nodes_for_sessions(session_ids)

    table_ref = (
        f"{self.project_id}.{self.dataset_id}" f".{self.config.biz_nodes_table}"
    )

    return self._append_rows_via_load_job(
        table_ref, rows, feature_label="biz nodes"
    )

  # -------------------------------------------------------------- #
  # Cross-Link Generation                                            #
  # -------------------------------------------------------------- #

  def create_cross_links(self, session_ids: list[str]) -> bool:
    """Creates EVALUATED edges linking TechNodes to BizNodes.

    Args:
        session_ids: Sessions to create cross-links for.

    Returns:
        True if successful.
    """
    create_query = _CREATE_CROSS_LINKS_TABLE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        cross_links_table=self.config.cross_links_table,
    )

    try:
      job_config = with_sdk_labels(
          bigquery.QueryJobConfig(), feature="context-graph"
      )
      job = self.client.query(create_query, job_config=job_config)
      job.result()
    except Exception as e:
      logger.warning("Failed to create cross-links table: %s", e)
      return False

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    # Delete existing cross-links for these sessions (idempotent)
    try:
      delete_query = _DELETE_CROSS_LINKS_FOR_SESSIONS_QUERY.format(
          project=self.project_id,
          dataset=self.dataset_id,
          cross_links_table=self.config.cross_links_table,
      )
      job = self.client.query(delete_query, job_config=job_config)
      job.result()
    except Exception as e:
      err_msg = str(e).lower()
      if "not found" in err_msg or "does not exist" in err_msg:
        logger.debug("Cross-links table does not exist yet: %s", e)
      else:
        logger.warning("Cross-links delete failed: %s", e)
        return False

    insert_query = _INSERT_CROSS_LINKS_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        biz_table=self.config.biz_nodes_table,
        cross_links_table=self.config.cross_links_table,
    )

    try:
      job = self.client.query(insert_query, job_config=job_config)
      job.result()
      logger.info("Cross-links created for %d sessions", len(session_ids))
      return True
    except Exception as e:
      logger.warning("Failed to create cross-links: %s", e)
      return False

  # -------------------------------------------------------------- #
  # Property Graph DDL                                               #
  # -------------------------------------------------------------- #

  def get_property_graph_ddl(
      self,
      graph_name: Optional[str] = None,
  ) -> str:
    """Returns the CREATE PROPERTY GRAPH DDL statement.

    Args:
        graph_name: Override the default graph name.

    Returns:
        The DDL SQL string.
    """
    name = graph_name or self.config.graph_name
    return _PROPERTY_GRAPH_DDL.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        biz_table=self.config.biz_nodes_table,
        cross_links_table=self.config.cross_links_table,
        graph_name=name,
    )

  def create_property_graph(
      self,
      graph_name: Optional[str] = None,
      include_decisions: bool = False,
  ) -> bool:
    """Creates the Property Graph in BigQuery.

    Args:
        graph_name: Override the default graph name.
        include_decisions: If True, uses the extended DDL with
            DecisionPoint and CandidateNode tables.

    Returns:
        True if successful.
    """
    if include_decisions:
      ddl = self.get_decision_property_graph_ddl(graph_name)
    else:
      ddl = self.get_property_graph_ddl(graph_name)
    try:
      job_config = with_sdk_labels(
          bigquery.QueryJobConfig(), feature="context-graph"
      )
      job = self.client.query(ddl, job_config=job_config)
      job.result()
      logger.info(
          "Property Graph '%s' created (decisions=%s)",
          graph_name or self.config.graph_name,
          include_decisions,
      )
      return True
    except Exception as e:
      logger.warning("Failed to create Property Graph: %s", e)
      return False

  # -------------------------------------------------------------- #
  # GQL Traversal                                                    #
  # -------------------------------------------------------------- #

  def get_reasoning_chain_gql(
      self,
      decision_event_type: str = "HITL_CONFIRMATION_REQUEST_COMPLETED",
      biz_entity: Optional[str] = None,
      graph_name: Optional[str] = None,
      max_hops: Optional[int] = None,
      result_limit: int = 100,
  ) -> str:
    """Returns a GQL query for reasoning chain traversal.

    Traces causal hops from a decision event back to the business
    entities that informed it.

    Args:
        decision_event_type: The terminal event type to trace from.
        biz_entity: Optional specific business entity to filter.
        graph_name: Override graph name.
        max_hops: Override max causal hops.
        result_limit: Maximum results to return.

    Returns:
        The GQL query string.
    """
    name = graph_name or self.config.graph_name
    hops = max_hops or self.config.max_hops

    biz_filter_clause = ""
    if biz_entity:
      biz_filter_clause = "AND biz.node_value = @biz_entity"

    return _GQL_REASONING_CHAIN_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        graph_name=name,
        max_hops=hops,
        biz_filter_clause=biz_filter_clause,
    )

  def explain_decision(
      self,
      decision_event_type: str = "HITL_CONFIRMATION_REQUEST_COMPLETED",
      biz_entity: Optional[str] = None,
      session_id: Optional[str] = None,
      decision_type: Optional[str] = None,
      include_dropped: bool = False,
      graph_name: Optional[str] = None,
      max_hops: Optional[int] = None,
      result_limit: int = 100,
  ) -> list[dict[str, Any]]:
    """Traverses the context graph to explain a decision.

    When *session_id* is provided, uses the EU audit GQL query
    that traverses TechNode→MadeDecision→DecisionPoint→CandidateEdge
    →CandidateNode, returning all candidates with scores, status,
    and rejection rationale.  The *decision_type* and
    *include_dropped* parameters filter the results.

    When *session_id* is not provided, falls back to the original
    BizNode reasoning-chain query.

    Args:
        decision_event_type: The terminal decision event type
            (used only in BizNode fallback path).
        biz_entity: Optional specific entity to explain
            (used only in BizNode fallback path).
        session_id: Session to query decision data for.
            When provided, uses the EU audit GQL path.
        decision_type: Optional filter for decision type
            (e.g. "audience_selection").
        include_dropped: Include dropped candidates in results.
        graph_name: Override graph name.
        max_hops: Override max causal hops.
        result_limit: Maximum results.

    Returns:
        List of dicts with decision and candidate details.
    """
    # Decision Semantics path: use EU audit GQL
    if session_id:
      return self._explain_decision_via_audit(
          session_id=session_id,
          decision_type=decision_type,
          include_dropped=include_dropped,
          graph_name=graph_name,
          max_hops=max_hops,
          result_limit=result_limit,
      )

    # BizNode fallback path: original reasoning chain
    return self._explain_decision_via_reasoning_chain(
        decision_event_type=decision_event_type,
        biz_entity=biz_entity,
        graph_name=graph_name,
        max_hops=max_hops,
        result_limit=result_limit,
    )

  def _explain_decision_via_audit(
      self,
      session_id: str,
      decision_type: Optional[str] = None,
      include_dropped: bool = False,
      graph_name: Optional[str] = None,
      max_hops: Optional[int] = None,
      result_limit: int = 100,
  ) -> list[dict[str, Any]]:
    """Explains decisions using the EU audit GQL path."""
    query = self.get_eu_audit_gql(
        session_id=session_id,
        decision_type=decision_type,
        graph_name=graph_name,
        max_hops=max_hops,
        result_limit=result_limit,
    )

    params = [
        bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
        bigquery.ScalarQueryParameter("result_limit", "INT64", result_limit),
    ]
    if decision_type:
      params.append(
          bigquery.ScalarQueryParameter(
              "decision_type", "STRING", decision_type
          )
      )

    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(query_parameters=params),
        feature="context-graph",
    )

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      results = [dict(row) for row in rows]
    except Exception as e:
      logger.warning("EU audit GQL query failed: %s", e)
      # Fall back to export_audit_trail for non-graph path
      return self.export_audit_trail(
          session_id,
          include_dropped=include_dropped,
      )

    if not include_dropped:
      results = [r for r in results if r.get("candidate_status") != "DROPPED"]

    return results

  def _explain_decision_via_reasoning_chain(
      self,
      decision_event_type: str,
      biz_entity: Optional[str] = None,
      graph_name: Optional[str] = None,
      max_hops: Optional[int] = None,
      result_limit: int = 100,
  ) -> list[dict[str, Any]]:
    """Explains decisions using the BizNode reasoning chain."""
    query = self.get_reasoning_chain_gql(
        decision_event_type=decision_event_type,
        biz_entity=biz_entity,
        graph_name=graph_name,
        max_hops=max_hops,
        result_limit=result_limit,
    )

    params = [
        bigquery.ScalarQueryParameter(
            "decision_event_type", "STRING", decision_event_type
        ),
        bigquery.ScalarQueryParameter("result_limit", "INT64", result_limit),
    ]
    if biz_entity:
      params.append(
          bigquery.ScalarQueryParameter("biz_entity", "STRING", biz_entity)
      )

    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(query_parameters=params),
        feature="context-graph",
    )

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [dict(row) for row in rows]
    except Exception as e:
      logger.warning("GQL reasoning chain query failed: %s", e)
      return []

  def get_causal_chain_gql(
      self,
      session_id: str,
      graph_name: Optional[str] = None,
      max_hops: Optional[int] = None,
      result_limit: int = 200,
  ) -> str:
    """Returns a GQL query for the full causal chain of a session.

    Args:
        session_id: Session to traverse.
        graph_name: Override graph name.
        max_hops: Override max hops.
        result_limit: Maximum results.

    Returns:
        The GQL query string.
    """
    name = graph_name or self.config.graph_name
    hops = max_hops or self.config.max_hops
    return _GQL_FULL_CAUSAL_CHAIN_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        graph_name=name,
        max_hops=hops,
    )

  def traverse_causal_chain(
      self,
      session_id: str,
      graph_name: Optional[str] = None,
      max_hops: Optional[int] = None,
      result_limit: int = 200,
  ) -> list[dict[str, Any]]:
    """Traverses the full causal chain for a session.

    Args:
        session_id: Session to traverse.
        graph_name: Override graph name.
        max_hops: Override max hops.
        result_limit: Maximum results.

    Returns:
        List of chain steps as dicts.
    """
    query = self.get_causal_chain_gql(
        session_id=session_id,
        graph_name=graph_name,
        max_hops=max_hops,
        result_limit=result_limit,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
            bigquery.ScalarQueryParameter(
                "result_limit", "INT64", result_limit
            ),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [dict(row) for row in rows]
    except Exception as e:
      logger.warning("GQL causal chain query failed: %s", e)
      return []

  # -------------------------------------------------------------- #
  # GQL Trace Reconstruction                                         #
  # -------------------------------------------------------------- #

  def reconstruct_trace_gql(
      self,
      session_id: str,
      graph_name: Optional[str] = None,
      result_limit: int = 1000,
  ) -> list[dict[str, Any]]:
    """Reconstructs a session trace using GQL graph traversal.

    This replaces the recursive CTE approach in ``trace.py`` with
    a native Property Graph ``MATCH`` query that walks the
    ``Caused`` edges to reconstruct the parent→child span tree.

    Args:
        session_id: Session to reconstruct.
        graph_name: Override graph name.
        result_limit: Maximum result rows.

    Returns:
        List of dicts with parent/child span pairs ordered by
        timestamp, suitable for building a Trace tree.
    """
    gname = graph_name or self.config.graph_name
    query = _GQL_TRACE_RECONSTRUCTION_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        graph_name=gname,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
            bigquery.ScalarQueryParameter(
                "result_limit", "INT64", result_limit
            ),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [dict(row) for row in rows]
    except Exception as e:
      logger.warning("GQL trace reconstruction failed: %s", e)
      return []

  # -------------------------------------------------------------- #
  # World Change Detection                                           #
  # -------------------------------------------------------------- #

  def get_biz_nodes_for_session(self, session_id: str) -> list[BizNode]:
    """Returns all business entities evaluated in a session.

    Args:
        session_id: Session to query.

    Returns:
        List of BizNode objects.
    """
    query = _BIZ_NODES_FOR_SESSION_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        biz_table=self.config.biz_nodes_table,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [
          BizNode(
              span_id=row.get("span_id", ""),
              session_id=row.get("session_id", ""),
              node_type=row.get("node_type", ""),
              node_value=row.get("node_value", ""),
              confidence=float(row.get("confidence", 1.0)),
              artifact_uri=row.get("artifact_uri"),
          )
          for row in rows
      ]
    except Exception as e:
      logger.warning(
          "Failed to get biz nodes for session %s: %s",
          session_id,
          e,
      )
      return []

  def _get_biz_nodes_with_timestamp(self, session_id: str) -> list[BizNode]:
    """Returns biz nodes with ``evaluated_at`` timestamp from the events table.

    Uses ``_WORLD_CHANGE_CHECK_QUERY`` which JOINs biz_nodes with
    agent_events to get the original evaluation timestamp.
    """
    query = _WORLD_CHANGE_CHECK_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        biz_table=self.config.biz_nodes_table,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [
          BizNode(
              span_id=row.get("span_id", ""),
              session_id=session_id,
              node_type=row.get("node_type", ""),
              node_value=row.get("node_value", ""),
              confidence=float(row.get("confidence", 1.0)),
              evaluated_at=row.get("evaluated_at"),
          )
          for row in rows
      ]
    except Exception as e:
      logger.warning(
          "Failed to get timestamped biz nodes for session %s: %s",
          session_id,
          e,
      )
      raise

  def detect_world_changes(
      self,
      session_id: str,
      current_state_fn: Any = None,
  ) -> WorldChangeReport:
    """Checks if business entities have drifted since evaluation.

    This implements the "World Change" detection pattern for
    long-running A2A tasks. Before a HITL approval is finalized,
    this method traverses the context graph to find the original
    BizNodes with their ``evaluated_at`` timestamps, then queries
    current availability via *current_state_fn* and reports drift.

    The callback receives a :class:`BizNode` whose ``evaluated_at``
    field contains the original evaluation timestamp, enabling
    temporal drift comparisons (e.g. "was this still available
    2 hours after evaluation?").

    Args:
        session_id: The session to check.
        current_state_fn: A callable that takes a BizNode and returns
            a dict with keys ``available`` (bool), ``current_value``
            (str), and optionally ``drift_type`` (str).  If None,
            no drift checks are performed and a safe report is
            returned with the entity count.

    Returns:
        WorldChangeReport with alerts for any detected drift.
        When the underlying query fails, ``check_failed`` is True
        and ``is_safe_to_approve`` is False (fail-closed).
    """
    try:
      nodes = self._get_biz_nodes_with_timestamp(session_id)
    except Exception:
      return WorldChangeReport(
          session_id=session_id,
          is_safe_to_approve=False,
          check_failed=True,
      )

    alerts: list[WorldChangeAlert] = []
    stale_count = 0
    callback_failures = 0

    for node in nodes:
      if current_state_fn is not None:
        try:
          state = current_state_fn(node)
        except Exception as e:
          logger.warning(
              "World state check failed for %s: %s",
              node.node_value,
              e,
          )
          callback_failures += 1
          continue

        if not state.get("available", True):
          stale_count += 1
          alerts.append(
              WorldChangeAlert(
                  biz_node=node.node_value,
                  original_state=f"{node.node_type}: {node.node_value}",
                  current_state=state.get("current_value", "unavailable"),
                  drift_type=state.get("drift_type", "unavailable"),
                  severity=state.get("severity", 0.8),
                  recommendation=state.get(
                      "recommendation",
                      "Review before approving.",
                  ),
              )
          )

    if callback_failures > 0:
      return WorldChangeReport(
          session_id=session_id,
          alerts=alerts,
          total_entities_checked=len(nodes),
          stale_entities=stale_count,
          is_safe_to_approve=False,
          check_failed=True,
      )

    return WorldChangeReport(
        session_id=session_id,
        alerts=alerts,
        total_entities_checked=len(nodes),
        stale_entities=stale_count,
        is_safe_to_approve=(stale_count == 0),
    )

  # -------------------------------------------------------------- #
  # Decision Semantics                                               #
  # -------------------------------------------------------------- #

  def _ensure_decision_tables(self) -> None:
    """Creates decision_points, candidates, and edge tables."""
    fmt = {
        "project": self.project_id,
        "dataset": self.dataset_id,
        "decision_points_table": self.config.decision_points_table,
        "candidates_table": self.config.candidates_table,
        "made_decision_edges_table": (self.config.made_decision_edges_table),
        "candidate_edges_table": self.config.candidate_edges_table,
    }
    for ddl_template in (
        _CREATE_DECISION_POINTS_TABLE_QUERY,
        _CREATE_CANDIDATES_TABLE_QUERY,
        _CREATE_MADE_DECISION_EDGES_TABLE_QUERY,
        _CREATE_CANDIDATE_EDGES_TABLE_QUERY,
    ):
      job_config = with_sdk_labels(
          bigquery.QueryJobConfig(), feature="context-graph"
      )
      job = self.client.query(ddl_template.format(**fmt), job_config=job_config)
      job.result()

  def extract_decision_points(
      self,
      session_ids: list[str],
      use_ai_generate: bool = True,
  ) -> tuple[list[DecisionPoint], list[Candidate]]:
    """Extracts decision points and candidates from agent traces.

    When *use_ai_generate* is True, uses BigQuery AI.GENERATE
    with a prompt-shaped JSON contract (no ``output_schema``,
    just instructions in the prompt) to extract structured
    decision data server-side, including candidates with scores,
    selection status, and rejection rationale. The result is a
    JSON array; the SQL strips markdown fences and the Python
    side parses it.

    When False, fetches payloads for client-side extraction;
    each payload becomes a DecisionPoint stub with no candidates.

    Args:
        session_ids: Sessions to extract decision points from.
        use_ai_generate: Whether to use server-side extraction.

    Returns:
        A tuple of (decision_points, candidates) lists.
    """
    if use_ai_generate:
      return self._extract_decisions_via_ai_generate(session_ids)
    return self._extract_decisions_for_client(session_ids)

  def _extract_decisions_via_ai_generate(
      self, session_ids: list[str]
  ) -> tuple[list[DecisionPoint], list[Candidate]]:
    """Server-side decision extraction using AI.GENERATE.

    The query uses AI.GENERATE's SQL-style ``output_schema`` to return
    a typed ``decisions`` array, then ``TO_JSON_STRING`` converts that
    typed output into the ``decisions_json`` text this method parses
    into ``DecisionPoint`` + ``Candidate`` records. This avoids the
    nondeterministic markdown-fenced JSON parsing path while preserving
    the existing Python parsing contract.
    """
    import json as _json

    query = _EXTRACT_DECISION_POINTS_AI_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        endpoint=self._resolve_endpoint(),
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="context-graph", ai_function="ai-generate"
    )

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
    except Exception as e:
      logger.warning("AI.GENERATE decision extraction failed: %s", e)
      return [], []

    all_dps: list[DecisionPoint] = []
    all_candidates: list[Candidate] = []

    for row in rows:
      span_id = row.get("span_id", "")
      session_id = row.get("session_id", "")
      raw_json = row.get("decisions_json", "")

      if not raw_json:
        continue

      try:
        decisions = _json.loads(raw_json)
      except (_json.JSONDecodeError, TypeError):
        logger.debug("Could not parse decisions JSON for span %s", span_id)
        continue

      if not isinstance(decisions, list):
        decisions = [decisions]

      for idx, dec in enumerate(decisions):
        decision_id = f"{session_id}:dp:{span_id}:{idx}"
        dp = DecisionPoint(
            decision_id=decision_id,
            session_id=session_id,
            span_id=span_id,
            decision_type=dec.get("decision_type", "unknown"),
            description=dec.get("description", ""),
        )
        all_dps.append(dp)

        for cidx, cand in enumerate(dec.get("candidates", [])):
          candidate_id = f"{decision_id}:c:{cidx}"
          status = cand.get("status", "SELECTED").upper()
          c = Candidate(
              candidate_id=candidate_id,
              decision_id=decision_id,
              session_id=session_id,
              name=cand.get("name", ""),
              score=float(cand.get("score", 0.0)),
              status=status,
              rejection_rationale=cand.get("rejection_rationale"),
          )
          all_candidates.append(c)

    logger.info(
        "AI.GENERATE extracted %d decision points, %d candidates",
        len(all_dps),
        len(all_candidates),
    )
    return all_dps, all_candidates

  def _extract_decisions_for_client(
      self, session_ids: list[str]
  ) -> tuple[list[DecisionPoint], list[Candidate]]:
    """Fetches payloads for client-side decision extraction."""
    query = _EXTRACT_DECISION_POINTS_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
    except Exception as e:
      logger.warning("Decision point extraction failed: %s", e)
      return [], []

    all_dps: list[DecisionPoint] = []
    for row in rows:
      span_id = row.get("span_id", "")
      session_id = row.get("session_id", "")
      payload = row.get("payload_text", "")
      if not payload:
        continue
      dp_idx = len(all_dps)
      decision_id = f"{session_id}:dp:{span_id}:{dp_idx}"
      all_dps.append(
          DecisionPoint(
              decision_id=decision_id,
              session_id=session_id,
              span_id=span_id,
              decision_type="raw_payload",
              description=payload[:200],
          )
      )

    return all_dps, []

  def store_decision_points(
      self,
      decision_points: list[DecisionPoint],
      candidates: list[Candidate],
  ) -> bool:
    """Stores pre-extracted decision points and candidates.

    This is idempotent: existing data for the same sessions is
    deleted before inserting new rows.

    Args:
        decision_points: List of DecisionPoint objects.
        candidates: List of Candidate objects.

    Returns:
        True if successful.
    """
    if not decision_points and not candidates:
      return True

    try:
      self._ensure_decision_tables()
    except Exception as e:
      logger.warning("Failed to create decision tables: %s", e)
      return False

    # Delete existing data for idempotency
    session_ids = list(
        {dp.session_id for dp in decision_points}
        | {c.session_id for c in candidates}
    )
    if session_ids:
      self._delete_decision_data_for_sessions(session_ids)

    if decision_points:
      dp_rows = [
          {
              "decision_id": dp.decision_id,
              "session_id": dp.session_id,
              "span_id": dp.span_id,
              "decision_type": dp.decision_type,
              "description": dp.description,
          }
          for dp in decision_points
      ]
      # The DecisionPoint KEY is decision_id; dedupe before the load.
      dp_rows = _dedupe_rows_by_key(dp_rows, "decision_id")
      dp_table = (
          f"{self.project_id}.{self.dataset_id}"
          f".{self.config.decision_points_table}"
      )
      if not self._append_rows_via_load_job(
          dp_table, dp_rows, feature_label="decision points"
      ):
        return False

    if candidates:
      cand_rows = [
          {
              "candidate_id": c.candidate_id,
              "decision_id": c.decision_id,
              "session_id": c.session_id,
              "name": c.name,
              "score": c.score,
              "status": c.status,
              "rejection_rationale": c.rejection_rationale,
          }
          for c in candidates
      ]
      # The CandidateNode KEY is candidate_id; dedupe before the load.
      cand_rows = _dedupe_rows_by_key(cand_rows, "candidate_id")
      cand_table = (
          f"{self.project_id}.{self.dataset_id}"
          f".{self.config.candidates_table}"
      )
      if not self._append_rows_via_load_job(
          cand_table, cand_rows, feature_label="candidates"
      ):
        return False

    logger.info(
        "Stored %d decision points, %d candidates "
        "(post-dedupe; some inputs may have shared a key)",
        len(decision_points),
        len(candidates),
    )
    return True

  def _delete_decision_data_for_sessions(
      self,
      session_ids: list[str],
  ) -> None:
    """Deletes existing decision data for the given sessions."""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")
    fmt = {
        "project": self.project_id,
        "dataset": self.dataset_id,
        "decision_points_table": self.config.decision_points_table,
        "candidates_table": self.config.candidates_table,
        "made_decision_edges_table": (self.config.made_decision_edges_table),
        "candidate_edges_table": self.config.candidate_edges_table,
    }
    for tmpl in (
        _DELETE_MADE_DECISION_EDGES_FOR_SESSIONS_QUERY,
        _DELETE_CANDIDATE_EDGES_FOR_SESSIONS_QUERY,
        _DELETE_CANDIDATES_FOR_SESSIONS_QUERY,
        _DELETE_DECISION_POINTS_FOR_SESSIONS_QUERY,
    ):
      try:
        job = self.client.query(tmpl.format(**fmt), job_config=job_config)
        job.result()
      except Exception as e:
        err_msg = str(e).lower()
        if "not found" in err_msg or "does not exist" in err_msg:
          logger.debug("Table does not exist yet: %s", e)
        else:
          logger.warning("Decision data delete failed: %s", e)

  def create_decision_edges(
      self,
      session_ids: list[str],
  ) -> bool:
    """Creates MadeDecision and CandidateEdge edges.

    This is idempotent: existing edges for the given sessions
    are deleted before inserting new ones.

    Args:
        session_ids: Sessions to create decision edges for.

    Returns:
        True if successful.
    """
    try:
      self._ensure_decision_tables()
    except Exception as e:
      logger.warning("Failed to create decision tables: %s", e)
      return False

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")

    fmt = {
        "project": self.project_id,
        "dataset": self.dataset_id,
        "decision_points_table": self.config.decision_points_table,
        "candidates_table": self.config.candidates_table,
        "made_decision_edges_table": (self.config.made_decision_edges_table),
        "candidate_edges_table": self.config.candidate_edges_table,
    }

    # Delete existing edges (idempotent)
    for del_tmpl in (
        _DELETE_MADE_DECISION_EDGES_FOR_SESSIONS_QUERY,
        _DELETE_CANDIDATE_EDGES_FOR_SESSIONS_QUERY,
    ):
      try:
        job = self.client.query(del_tmpl.format(**fmt), job_config=job_config)
        job.result()
      except Exception as e:
        err_msg = str(e).lower()
        if "not found" not in err_msg and "does not exist" not in err_msg:
          logger.warning("Edge delete failed: %s", e)
          return False

    # Insert MadeDecision edges
    try:
      job = self.client.query(
          _INSERT_MADE_DECISION_EDGES_QUERY.format(**fmt),
          job_config=job_config,
      )
      job.result()
    except Exception as e:
      logger.warning("Failed to insert MadeDecision edges: %s", e)
      return False

    # Insert CandidateEdge edges
    try:
      job = self.client.query(
          _INSERT_CANDIDATE_EDGES_QUERY.format(**fmt),
          job_config=job_config,
      )
      job.result()
    except Exception as e:
      logger.warning("Failed to insert CandidateEdge edges: %s", e)
      return False

    logger.info("Decision edges created for %d sessions", len(session_ids))
    return True

  def get_decision_property_graph_ddl(
      self,
      graph_name: Optional[str] = None,
  ) -> str:
    """Returns Property Graph DDL with Decision Semantics extension.

    This extends the base 4-pillar DDL with DecisionPoint and
    Candidate node tables and decision edge tables.

    Args:
        graph_name: Override the default graph name.

    Returns:
        The DDL SQL string.
    """
    name = graph_name or self.config.graph_name
    return _DECISION_PROPERTY_GRAPH_DDL.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        biz_table=self.config.biz_nodes_table,
        cross_links_table=self.config.cross_links_table,
        decision_points_table=self.config.decision_points_table,
        candidates_table=self.config.candidates_table,
        made_decision_edges_table=(self.config.made_decision_edges_table),
        candidate_edges_table=self.config.candidate_edges_table,
        graph_name=name,
    )

  def get_eu_audit_gql(
      self,
      session_id: Optional[str] = None,
      decision_type: Optional[str] = None,
      graph_name: Optional[str] = None,
      max_hops: Optional[int] = None,
      result_limit: int = 200,
  ) -> str:
    """Returns a GQL query for EU audit trail traversal.

    Traces decision points back through reasoning chains to find
    all candidates (selected and dropped) with rejection rationale.

    Args:
        session_id: Session to audit.
        decision_type: Optional filter for decision type.
        graph_name: Override graph name.
        max_hops: Override max causal hops.
        result_limit: Maximum results.

    Returns:
        The GQL query string.
    """
    name = graph_name or self.config.graph_name
    hops = max_hops or self.config.max_hops

    decision_type_clause = ""
    if decision_type:
      decision_type_clause = "AND dp.decision_type = @decision_type"

    return _GQL_EU_AUDIT_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        graph_name=name,
        max_hops=hops,
        decision_type_clause=decision_type_clause,
    )

  def get_dropped_candidates_gql(
      self,
      graph_name: Optional[str] = None,
      result_limit: int = 200,
  ) -> str:
    """Returns a GQL query for dropped candidates with rationale.

    Args:
        graph_name: Override graph name.
        result_limit: Maximum results.

    Returns:
        The GQL query string.
    """
    name = graph_name or self.config.graph_name
    return _GQL_DROPPED_CANDIDATES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        graph_name=name,
    )

  def get_decision_points_for_session(
      self,
      session_id: str,
  ) -> list[DecisionPoint]:
    """Returns all decision points for a session.

    Args:
        session_id: Session to query.

    Returns:
        List of DecisionPoint objects.
    """
    query = _DECISION_POINTS_FOR_SESSION_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        decision_points_table=self.config.decision_points_table,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")
    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [
          DecisionPoint(
              decision_id=row.get("decision_id", ""),
              session_id=row.get("session_id", ""),
              span_id=row.get("span_id", ""),
              decision_type=row.get("decision_type", ""),
              description=row.get("description", ""),
          )
          for row in rows
      ]
    except Exception as e:
      logger.warning(
          "Failed to get decision points for session %s: %s",
          session_id,
          e,
      )
      return []

  def get_candidates_for_decision(
      self,
      decision_id: str,
  ) -> list[Candidate]:
    """Returns all candidates for a decision point.

    Args:
        decision_id: The decision point to query.

    Returns:
        List of Candidate objects ordered by score descending.
    """
    query = _CANDIDATES_FOR_DECISION_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        candidates_table=self.config.candidates_table,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("decision_id", "STRING", decision_id),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="context-graph")
    try:
      job = self.client.query(query, job_config=job_config)
      rows = list(job.result())
      return [
          Candidate(
              candidate_id=row.get("candidate_id", ""),
              decision_id=row.get("decision_id", ""),
              session_id=row.get("session_id", ""),
              name=row.get("name", ""),
              score=float(row.get("score", 0.0)),
              status=row.get("status", ""),
              rejection_rationale=row.get("rejection_rationale"),
          )
          for row in rows
      ]
    except Exception as e:
      logger.warning(
          "Failed to get candidates for decision %s: %s",
          decision_id,
          e,
      )
      return []

  def export_audit_trail(
      self,
      session_id: str,
      include_dropped: bool = True,
      format: str = "dict",
  ) -> Any:
    """Exports a full audit trail for a session's decisions.

    Returns all decision points with their candidates, scores,
    status, and rejection rationale.

    Args:
        session_id: Session to export.
        include_dropped: If False, only returns selected candidates.
        format: Output format — ``"dict"`` (default) returns a list
            of dicts, ``"json"`` returns a JSON string.

    Returns:
        List of dicts (or JSON string if format="json") with
        decision and candidate details.
    """
    decision_points = self.get_decision_points_for_session(session_id)
    trail: list[dict[str, Any]] = []

    for dp in decision_points:
      candidates = self.get_candidates_for_decision(dp.decision_id)
      if not include_dropped:
        candidates = [c for c in candidates if c.status == "SELECTED"]

      trail.append(
          {
              "decision_id": dp.decision_id,
              "decision_type": dp.decision_type,
              "description": dp.description,
              "span_id": dp.span_id,
              "candidates": [
                  {
                      "candidate_id": c.candidate_id,
                      "name": c.name,
                      "score": c.score,
                      "status": c.status,
                      "rejection_rationale": c.rejection_rationale,
                  }
                  for c in candidates
              ],
          }
      )

    if format == "json":
      import json

      return json.dumps(trail, indent=2, default=str)
    return trail

  # -------------------------------------------------------------- #
  # Pipeline: End-to-End                                             #
  # -------------------------------------------------------------- #

  def build_context_graph(
      self,
      session_ids: list[str],
      graph_name: Optional[str] = None,
      use_ai_generate: bool = True,
      include_decisions: bool = False,
  ) -> dict[str, Any]:
    """End-to-end pipeline: extract, cross-link, and create graph.

    Runs all steps in sequence:
    1. Extract business entities from traces
    2. Create cross-link edges
    3. (Optional) Extract decision points and create decision edges
    4. Create the Property Graph

    Args:
        session_ids: Sessions to include.
        graph_name: Override graph name.
        use_ai_generate: Use AI.GENERATE for extraction.
        include_decisions: Also extract and store decision
            semantics (DecisionPoints, Candidates, edges).

    Returns:
        Dict with results of each step.
    """
    results: dict[str, Any] = {}

    # Step 1: Extract biz nodes
    nodes = self.extract_biz_nodes(session_ids, use_ai_generate=use_ai_generate)
    results["biz_nodes_count"] = len(nodes)
    results["biz_nodes"] = nodes

    # Step 2: Cross-links
    cross_link_ok = self.create_cross_links(session_ids)
    results["cross_links_created"] = cross_link_ok

    # Step 3: Decision Semantics (if enabled)
    if include_decisions:
      dps, cands = self.extract_decision_points(
          session_ids, use_ai_generate=use_ai_generate
      )
      results["decision_points_count"] = len(dps)
      if dps or cands:
        store_ok = self.store_decision_points(dps, cands)
        results["decision_points_stored"] = store_ok
      edges_ok = self.create_decision_edges(session_ids)
      results["decision_edges_created"] = edges_ok

    # Step 4: Property Graph
    graph_ok = self.create_property_graph(
        graph_name, include_decisions=include_decisions
    )
    results["property_graph_created"] = graph_ok

    return results
