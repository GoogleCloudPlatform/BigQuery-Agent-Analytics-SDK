-- Copyright 2026 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License").
--
-- Standalone DDL — recreates `agent_context_graph` from the seven
-- backing tables already present in the dataset. Use this when:
--
--   * The agent + extraction pipeline has already run (so
--     `agent_events`, `extracted_biz_nodes`, `context_cross_links`,
--     `decision_points`, `candidates`, `made_decision_edges`, and
--     `candidate_edges` all exist).
--   * You dropped the property graph but want to keep the data.
--   * You're moving / cloning the dataset to another project and
--     want to recreate the graph layer there.
--
-- The DDL is byte-identical to what
-- `ContextGraphManager.get_decision_property_graph_ddl()` emits at
-- the SDK default config; `setup.sh` renders this template into
-- `property_graph.gql` with your project and dataset inlined, but
-- you can also paste it directly into BigQuery Studio after
-- substituting `__PROJECT_ID__` and `__DATASET_ID__` by hand.
--
-- Idempotent: `CREATE OR REPLACE PROPERTY GRAPH` is a single atomic
-- DDL.

CREATE OR REPLACE PROPERTY GRAPH
  `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
  NODE TABLES (
    -- Technical execution nodes (spans from the BQ AA Plugin).
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
    -- Business domain nodes (entities AI.GENERATE extracted).
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
    -- Decision-point nodes (one row per decision the agent made,
    -- as extracted by AI.GENERATE).
    `__PROJECT_ID__.__DATASET_ID__.decision_points` AS DecisionPoint
      KEY (decision_id)
      LABEL DecisionPoint
      PROPERTIES (
        session_id,
        span_id,
        decision_type,
        description
      ),
    -- Candidate nodes (every option weighed at every decision).
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
      )
  )
  EDGE TABLES (
    -- Causal lineage between spans (parent_span_id -> span_id).
    `__PROJECT_ID__.__DATASET_ID__.agent_events` AS Caused
      KEY (span_id)
      SOURCE KEY (parent_span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (span_id) REFERENCES TechNode (span_id)
      LABEL Caused,

    -- Cross-link from a span to the BizNode it evaluated /
    -- produced.
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

    -- TechNode -> DecisionPoint (the span that produced the
    -- decision).
    `__PROJECT_ID__.__DATASET_ID__.made_decision_edges` AS MadeDecision
      KEY (edge_id)
      SOURCE KEY (span_id) REFERENCES TechNode (span_id)
      DESTINATION KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      LABEL MadeDecision,

    -- DecisionPoint -> CandidateNode (selected or dropped).
    `__PROJECT_ID__.__DATASET_ID__.candidate_edges` AS CandidateEdge
      KEY (edge_id)
      SOURCE KEY (decision_id) REFERENCES DecisionPoint (decision_id)
      DESTINATION KEY (candidate_id) REFERENCES CandidateNode (candidate_id)
      LABEL CandidateEdge
      PROPERTIES (
        edge_type,
        rejection_rationale,
        created_at
      )
  );
