-- Copyright 2026 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Phase 1 joint property graph for the A2A joint-lineage demo.
--
-- 5 nodes, 4 edges — intentionally small. The graph spans BOTH the
-- caller's and the receiver's SDK-extracted graph backing tables via
-- the auditor projections in `<AUDITOR_DATASET>` (created by
-- `build_joint_graph.py`). The headline traversal:
--
--   CallerCampaignRun
--     -[:DelegatedVia]-> RemoteAgentInvocation
--     -[:HandledBy]->     ReceiverAgentRun
--     -[:ReceiverMadeDecision]-> ReceiverPlanningDecision
--     -[:ReceiverWeighedOption]-> ReceiverDecisionOption
--
-- `rejection_rationale` lives on `ReceiverDecisionOption` as a
-- property in Phase 1; a separate `ReceiverDropReason` node is a
-- future expansion. `OPTIONAL MATCH` is not needed here because
-- selected options also carry the property (NULL for SELECTED).
--
-- Two physical tables (`receiver_planning_decisions`,
-- `receiver_decision_options`) intentionally back BOTH a node label
-- and an edge label. BigQuery permits this table-reuse pattern; it
-- keeps the first joint graph smaller than introducing dedicated
-- edge tables. See DATA_LINEAGE.md for the per-table mapping.

CREATE OR REPLACE PROPERTY GRAPH
  `__PROJECT_ID__.__AUDITOR_DATASET_ID__.a2a_joint_context_graph`
  NODE TABLES (
    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.caller_campaign_runs` AS CallerCampaignRun
      KEY (caller_session_id)
      LABEL CallerCampaignRun
      PROPERTIES (
        caller_session_id,
        campaign,
        brand,
        brief,
        run_order,
        event_count
      ),

    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.remote_agent_invocations` AS RemoteAgentInvocation
      KEY (remote_invocation_id)
      LABEL RemoteAgentInvocation
      PROPERTIES (
        remote_invocation_id,
        caller_session_id,
        caller_span_id,
        a2a_task_id,
        a2a_context_id,
        receiver_session_id_from_response,
        timestamp
      ),

    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.receiver_runs` AS ReceiverAgentRun
      KEY (receiver_session_id)
      LABEL ReceiverAgentRun
      PROPERTIES (
        receiver_session_id,
        started_at,
        ended_at,
        event_count,
        completed
      ),

    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.receiver_planning_decisions` AS ReceiverPlanningDecision
      KEY (decision_id)
      LABEL ReceiverPlanningDecision
      PROPERTIES (
        decision_id,
        session_id,
        span_id,
        decision_type,
        description
      ),

    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.receiver_decision_options` AS ReceiverDecisionOption
      KEY (candidate_id)
      LABEL ReceiverDecisionOption
      PROPERTIES (
        candidate_id,
        decision_id,
        session_id,
        name,
        score,
        status,
        rejection_rationale
      )
  )
  EDGE TABLES (
    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.remote_agent_invocations` AS DelegatedVia
      KEY (remote_invocation_id)
      SOURCE KEY (caller_session_id) REFERENCES CallerCampaignRun (caller_session_id)
      DESTINATION KEY (remote_invocation_id) REFERENCES RemoteAgentInvocation (remote_invocation_id)
      LABEL DelegatedVia
      PROPERTIES (
        a2a_task_id,
        a2a_context_id,
        timestamp
      ),

    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.joint_a2a_edges` AS HandledBy
      KEY (edge_id)
      SOURCE KEY (remote_invocation_id) REFERENCES RemoteAgentInvocation (remote_invocation_id)
      DESTINATION KEY (receiver_session_id) REFERENCES ReceiverAgentRun (receiver_session_id)
      LABEL HandledBy
      PROPERTIES (
        a2a_context_id,
        a2a_task_id
      ),

    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.receiver_planning_decisions` AS ReceiverMadeDecision
      KEY (decision_id)
      SOURCE KEY (session_id) REFERENCES ReceiverAgentRun (receiver_session_id)
      DESTINATION KEY (decision_id) REFERENCES ReceiverPlanningDecision (decision_id)
      LABEL ReceiverMadeDecision,

    `__PROJECT_ID__.__AUDITOR_DATASET_ID__.receiver_decision_options` AS ReceiverWeighedOption
      KEY (candidate_id)
      SOURCE KEY (decision_id) REFERENCES ReceiverPlanningDecision (decision_id)
      DESTINATION KEY (candidate_id) REFERENCES ReceiverDecisionOption (candidate_id)
      LABEL ReceiverWeighedOption
      PROPERTIES (
        status,
        score,
        rejection_rationale
      )
  );
