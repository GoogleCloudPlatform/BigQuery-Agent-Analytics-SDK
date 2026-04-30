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
    `__PROJECT_ID__.__DATASET_ID__.agent_events` AS TechNode
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
    `__PROJECT_ID__.__DATASET_ID__.extracted_biz_nodes` AS BizNode
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
    `__PROJECT_ID__.__DATASET_ID__.decision_points` AS DecisionPoint
      KEY (decision_id)
      LABEL DecisionPoint
      PROPERTIES (
        session_id,
        span_id,
        decision_type,
        description
      ),
    `__PROJECT_ID__.__DATASET_ID__.rich_decision_types` AS DecisionType
      KEY (decision_type_id)
      LABEL DecisionType
      PROPERTIES (
        decision_type_key,
        decision_type,
        decision_count
      ),
    `__PROJECT_ID__.__DATASET_ID__.candidates` AS CandidateNode
      KEY (candidate_id)
      LABEL CandidateNode
      PROPERTIES (
        decision_id,
        session_id,
        name,
        score,
        status,
        rejection_rationale
      ),
    `__PROJECT_ID__.__DATASET_ID__.rich_candidate_statuses` AS CandidateStatus
      KEY (status_id)
      LABEL CandidateStatus
      PROPERTIES (
        status,
        candidate_count
      ),
    `__PROJECT_ID__.__DATASET_ID__.rich_rejection_reasons` AS RejectionReason
      KEY (reason_id)
      LABEL RejectionReason
      PROPERTIES (
        rejection_rationale,
        reason_excerpt,
        candidate_count
      )
  )
  EDGE TABLES (
    `__PROJECT_ID__.__DATASET_ID__.rich_campaign_span_edges` AS CampaignSpan
      KEY (edge_id)
      SOURCE KEY (session_id) REFERENCES CampaignRun (session_id)
      DESTINATION KEY (span_id) REFERENCES TechNode (span_id)
      LABEL CampaignSpan
      PROPERTIES (
        event_type,
        timestamp
      ),

    `__PROJECT_ID__.__DATASET_ID__.agent_events` AS Caused
      KEY (span_id)
      SOURCE KEY (parent_span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (span_id) REFERENCES TechNode (span_id)
      LABEL Caused,

    `__PROJECT_ID__.__DATASET_ID__.context_cross_links` AS Evaluated
      KEY (link_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (biz_node_id) REFERENCES BizNode (biz_node_id)
      LABEL Evaluated
      PROPERTIES (
        artifact_uri,
        link_type,
        created_at
      ),

    `__PROJECT_ID__.__DATASET_ID__.made_decision_edges` AS MadeDecision
      KEY (edge_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      LABEL MadeDecision,

    `__PROJECT_ID__.__DATASET_ID__.rich_campaign_decision_edges` AS CampaignDecision
      KEY (edge_id)
      SOURCE KEY (session_id) REFERENCES CampaignRun (session_id)
      DESTINATION KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      LABEL CampaignDecision,

    `__PROJECT_ID__.__DATASET_ID__.rich_decision_type_edges` AS HasDecisionType
      KEY (edge_id)
      SOURCE KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      DESTINATION KEY (decision_type_id) REFERENCES DecisionType (decision_type_id)
      LABEL HasDecisionType,

    `__PROJECT_ID__.__DATASET_ID__.candidate_edges` AS CandidateEdge
      KEY (edge_id)
      SOURCE KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      DESTINATION KEY (candidate_id) REFERENCES CandidateNode (candidate_id)
      LABEL CandidateEdge
      PROPERTIES (
        edge_type,
        rejection_rationale,
        created_at
      ),

    `__PROJECT_ID__.__DATASET_ID__.rich_candidate_status_edges` AS HasCandidateStatus
      KEY (edge_id)
      SOURCE KEY (candidate_id) REFERENCES CandidateNode (candidate_id)
      DESTINATION KEY (status_id) REFERENCES CandidateStatus (status_id)
      LABEL HasCandidateStatus,

    `__PROJECT_ID__.__DATASET_ID__.rich_candidate_reason_edges` AS RejectedBecause
      KEY (edge_id)
      SOURCE KEY (candidate_id) REFERENCES CandidateNode (candidate_id)
      DESTINATION KEY (reason_id) REFERENCES RejectionReason (reason_id)
      LABEL RejectedBecause
  );
