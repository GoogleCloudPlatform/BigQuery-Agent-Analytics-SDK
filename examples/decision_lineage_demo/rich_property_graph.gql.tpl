-- Copyright 2026 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License").
--
-- Demo presentation graph. This is intentionally richer than the SDK's
-- canonical `agent_context_graph`: it promotes campaign runs, decision
-- types, candidate statuses, and rejection reasons into first-class
-- nodes so BigQuery Studio has a denser visual story to render.

CREATE OR REPLACE PROPERTY GRAPH
  `__PROJECT_ID__.__DATASET_ID__.__RICH_GRAPH_NAME__`
  NODE TABLES (
    `__PROJECT_ID__.__DATASET_ID__.campaign_runs` AS CampaignRun
      KEY (session_id)
      LABEL CampaignRun
      PROPERTIES (
        campaign,
        brand,
        brief,
        run_order
      ),
    `__PROJECT_ID__.__DATASET_ID__.rich_agent_steps` AS AgentStep
      KEY (span_id)
      LABEL AgentStep
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
    `__PROJECT_ID__.__DATASET_ID__.extracted_biz_nodes` AS MediaEntity
      KEY (biz_node_id)
      LABEL MediaEntity
      PROPERTIES (
        node_type,
        node_value,
        confidence,
        session_id,
        span_id,
        artifact_uri
      ),
    `__PROJECT_ID__.__DATASET_ID__.decision_points` AS PlanningDecision
      KEY (decision_id)
      LABEL PlanningDecision
      PROPERTIES (
        session_id,
        span_id,
        decision_type,
        description
      ),
    `__PROJECT_ID__.__DATASET_ID__.rich_decision_types` AS DecisionCategory
      KEY (decision_type_id)
      LABEL DecisionCategory
      PROPERTIES (
        decision_type_key,
        decision_type,
        decision_count
      ),
    `__PROJECT_ID__.__DATASET_ID__.candidates` AS DecisionOption
      KEY (candidate_id)
      LABEL DecisionOption
      PROPERTIES (
        decision_id,
        session_id,
        name,
        score,
        status,
        rejection_rationale
      ),
    `__PROJECT_ID__.__DATASET_ID__.rich_candidate_statuses` AS OptionOutcome
      KEY (status_id)
      LABEL OptionOutcome
      PROPERTIES (
        status,
        candidate_count
      ),
    `__PROJECT_ID__.__DATASET_ID__.rich_rejection_reasons` AS DropReason
      KEY (reason_id)
      LABEL DropReason
      PROPERTIES (
        rejection_rationale,
        reason_excerpt,
        candidate_count
      )
  )
  EDGE TABLES (
    `__PROJECT_ID__.__DATASET_ID__.rich_campaign_span_edges` AS CampaignActivity
      KEY (edge_id)
      SOURCE KEY (session_id) REFERENCES CampaignRun (session_id)
      DESTINATION KEY (span_id) REFERENCES AgentStep (span_id)
      LABEL CampaignActivity
      PROPERTIES (
        event_type,
        timestamp
      ),

    `__PROJECT_ID__.__DATASET_ID__.rich_agent_steps` AS NextStep
      KEY (span_id)
      SOURCE KEY (parent_span_id) REFERENCES AgentStep (span_id)
      DESTINATION KEY (span_id) REFERENCES AgentStep (span_id)
      LABEL NextStep,

    `__PROJECT_ID__.__DATASET_ID__.context_cross_links` AS ConsideredEntity
      KEY (link_id)
      SOURCE KEY (span_id) REFERENCES AgentStep (span_id)
      DESTINATION KEY (biz_node_id) REFERENCES MediaEntity (biz_node_id)
      LABEL ConsideredEntity
      PROPERTIES (
        artifact_uri,
        link_type,
        created_at
      ),

    `__PROJECT_ID__.__DATASET_ID__.made_decision_edges` AS DecidedAt
      KEY (edge_id)
      SOURCE KEY (span_id) REFERENCES AgentStep (span_id)
      DESTINATION KEY (decision_id) REFERENCES PlanningDecision (decision_id)
      LABEL DecidedAt,

    `__PROJECT_ID__.__DATASET_ID__.rich_campaign_decision_edges` AS CampaignDecision
      KEY (edge_id)
      SOURCE KEY (session_id) REFERENCES CampaignRun (session_id)
      DESTINATION KEY (decision_id) REFERENCES PlanningDecision (decision_id)
      LABEL CampaignDecision,

    `__PROJECT_ID__.__DATASET_ID__.rich_decision_type_edges` AS InCategory
      KEY (edge_id)
      SOURCE KEY (decision_id) REFERENCES PlanningDecision (decision_id)
      DESTINATION KEY (decision_type_id) REFERENCES DecisionCategory (decision_type_id)
      LABEL InCategory,

    `__PROJECT_ID__.__DATASET_ID__.candidate_edges` AS WeighedOption
      KEY (edge_id)
      SOURCE KEY (decision_id) REFERENCES PlanningDecision (decision_id)
      DESTINATION KEY (candidate_id) REFERENCES DecisionOption (candidate_id)
      LABEL WeighedOption
      PROPERTIES (
        edge_type,
        rejection_rationale,
        created_at
      ),

    `__PROJECT_ID__.__DATASET_ID__.rich_candidate_status_edges` AS HasOutcome
      KEY (edge_id)
      SOURCE KEY (candidate_id) REFERENCES DecisionOption (candidate_id)
      DESTINATION KEY (status_id) REFERENCES OptionOutcome (status_id)
      LABEL HasOutcome,

    `__PROJECT_ID__.__DATASET_ID__.rich_candidate_reason_edges` AS RejectedBecause
      KEY (edge_id)
      SOURCE KEY (candidate_id) REFERENCES DecisionOption (candidate_id)
      DESTINATION KEY (reason_id) REFERENCES DropReason (reason_id)
      LABEL RejectedBecause
  );
