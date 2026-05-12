-- Copyright 2026 Google LLC
--
-- User-authored property graph for the migration v5 demo.
--
-- Beat 1 ("you own the graph definition", issue #104):
--   the platform team owns this DDL; the SDK populates the
--   base tables via ``ontology-build --skip-property-graph``
--   and never re-issues ``CREATE OR REPLACE PROPERTY GRAPH``.
--
-- Replace the ``__DATASET__`` placeholder with the demo's
-- runtime dataset (``migration_v5_demo_<hex>``) at apply
-- time. The notebook's Beat 1 cell does this substitution.

CREATE OR REPLACE PROPERTY GRAPH __DATASET__.mako_demo_graph
  NODE TABLES (
    __DATASET__.agent_session
      KEY (id)
      LABEL AgentSession PROPERTIES (id, session_id, started_at),

    __DATASET__.decision_point
      KEY (id)
      LABEL DecisionPoint PROPERTIES (id, decision_type, decided_at),

    __DATASET__.candidate
      KEY (id)
      LABEL Candidate PROPERTIES (id, candidate_label, score),

    __DATASET__.selection_outcome
      KEY (id)
      LABEL SelectionOutcome
      PROPERTIES (id, selected_candidate_id, rationale),

    __DATASET__.context_snapshot
      KEY (id)
      LABEL ContextSnapshot
      PROPERTIES (id, snapshot_payload, snapshot_timestamp)
  )
  EDGE TABLES (
    __DATASET__.contains_decision_point
      SOURCE KEY (session_id) REFERENCES __DATASET__.agent_session (id)
      DESTINATION KEY (decision_point_id) REFERENCES __DATASET__.decision_point (id)
      LABEL containsDecisionPoint,

    __DATASET__.has_candidate
      SOURCE KEY (decision_point_id) REFERENCES __DATASET__.decision_point (id)
      DESTINATION KEY (candidate_id) REFERENCES __DATASET__.candidate (id)
      LABEL hasCandidate,

    __DATASET__.has_outcome
      SOURCE KEY (decision_point_id) REFERENCES __DATASET__.decision_point (id)
      DESTINATION KEY (outcome_id) REFERENCES __DATASET__.selection_outcome (id)
      LABEL hasOutcome,

    __DATASET__.has_context
      SOURCE KEY (decision_point_id) REFERENCES __DATASET__.decision_point (id)
      DESTINATION KEY (context_id) REFERENCES __DATASET__.context_snapshot (id)
      LABEL hasContext
  );
