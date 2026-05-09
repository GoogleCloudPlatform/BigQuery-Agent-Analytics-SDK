-- Copyright 2026 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Five paste-and-run blocks for the BigQuery Studio walkthrough.
-- Render concrete project/dataset values via ./render_queries.sh.
--
--   Block 1 — Stitch health and coverage.
--   Block 2 — End-to-end A2A path (one row per remote call).
--   Block 3 — Remote governance rejections.
--   Block 4 — Right-to-explanation for one specific campaign.
--   Block 5 — Redaction proof.

-- ============================================================
-- Block 1 — Stitch health and coverage.
-- Confirms each A2A_INTERACTION row carries a context_id, counts
-- how many also carry the (diagnostic) receiver_session_id echoed
-- back in the A2A response metadata, and reports stitch coverage:
-- if `stitched_edges` is less than `a2a_calls`, some remote calls
-- have no matching receiver session (the join in joint_a2a_edges
-- found no row), and downstream traversals will silently miss
-- those calls.
-- ============================================================
WITH ri AS (
  SELECT
    COUNT(*)                                                  AS a2a_calls,
    COUNTIF(a2a_context_id IS NOT NULL)                       AS calls_with_context_id,
    COUNTIF(receiver_session_id_from_response IS NOT NULL)    AS calls_with_receiver_echo
  FROM `__PROJECT_ID__.__AUDITOR_DATASET_ID__.remote_agent_invocations`
),
edges AS (
  SELECT COUNT(*) AS stitched_edges
  FROM `__PROJECT_ID__.__AUDITOR_DATASET_ID__.joint_a2a_edges`
)
SELECT
  ri.a2a_calls,
  ri.calls_with_context_id,
  ri.calls_with_receiver_echo,
  edges.stitched_edges,
  ri.a2a_calls - edges.stitched_edges AS unstitched_calls
FROM ri, edges;


-- ============================================================
-- Block 2 — End-to-end A2A path.
-- One row per (campaign, remote A2A call, receiver session). The
-- HandledBy edge resolves caller a2a_context_id to the receiver's
-- session_id via joint_a2a_edges.
-- ============================================================
GRAPH `__PROJECT_ID__.__AUDITOR_DATASET_ID__.a2a_joint_context_graph`
MATCH (campaign:CallerCampaignRun)
      -[:DelegatedVia]->(remote:RemoteAgentInvocation)
      -[:HandledBy]->(receiver:ReceiverAgentRun)
RETURN
  campaign.caller_session_id,
  campaign.campaign,
  remote.a2a_context_id,
  remote.a2a_task_id,
  receiver.receiver_session_id,
  receiver.event_count
LIMIT 20;


-- ============================================================
-- Block 3 — Remote governance rejections.
-- Walks caller -> receiver -> planning decision -> dropped
-- candidate, surfacing the rejection rationale for every option
-- the receiver dropped.
-- ============================================================
GRAPH `__PROJECT_ID__.__AUDITOR_DATASET_ID__.a2a_joint_context_graph`
MATCH (remote:RemoteAgentInvocation)-[:HandledBy]->(receiver:ReceiverAgentRun)
      -[:ReceiverMadeDecision]->(decision:ReceiverPlanningDecision)
      -[:ReceiverWeighedOption]->(option:ReceiverDecisionOption)
WHERE option.status = 'DROPPED'
RETURN
  remote.a2a_context_id,
  decision.decision_type,
  option.name,
  option.score,
  option.rejection_rationale
ORDER BY option.score ASC
LIMIT 20;


-- ============================================================
-- Block 4 — Right-to-explanation for one specific campaign.
-- Both SELECTED and DROPPED options appear because
-- rejection_rationale is a property on every ReceiverDecisionOption
-- (NULL for selected, non-null for dropped).
-- Replace the @caller_session parameter with one of the rows
-- returned by Block 2.
-- ============================================================
GRAPH `__PROJECT_ID__.__AUDITOR_DATASET_ID__.a2a_joint_context_graph`
MATCH (campaign:CallerCampaignRun)
      -[:DelegatedVia]->(remote:RemoteAgentInvocation)
      -[:HandledBy]->(receiver:ReceiverAgentRun)
      -[:ReceiverMadeDecision]->(decision:ReceiverPlanningDecision)
      -[:ReceiverWeighedOption]->(option:ReceiverDecisionOption)
WHERE campaign.caller_session_id = '__DEMO_CALLER_SESSION_ID__'
RETURN
  campaign.campaign,
  remote.a2a_context_id,
  decision.decision_type,
  option.name,
  option.score,
  option.status,
  option.rejection_rationale AS rationale
ORDER BY option.status DESC, option.score DESC;


-- ============================================================
-- Block 5 — Redaction proof.
-- Auditor-facing projection tables expose lineage ids and
-- outcomes, NOT raw a2a_request / a2a_response / full content.
-- Expected: zero rows containing those columns.
-- ============================================================
SELECT
  table_name,
  column_name
FROM `__PROJECT_ID__.__AUDITOR_DATASET_ID__.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN (
  'caller_campaign_runs',
  'remote_agent_invocations',
  'receiver_runs',
  'receiver_planning_decisions',
  'receiver_decision_options',
  'joint_a2a_edges'
)
  AND column_name IN ('a2a_request', 'a2a_response', 'content')
ORDER BY table_name, column_name;
