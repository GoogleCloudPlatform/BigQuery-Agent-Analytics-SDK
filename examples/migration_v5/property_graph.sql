CREATE OR REPLACE PROPERTY GRAPH `test-project-0728-467323.migration_v5_demo_56f9179f.mako_demo_graph`
  NODE TABLES (
    `test-project-0728-467323.migration_v5_demo_56f9179f.agent_session` AS agent_session
      KEY (agent_session_id)
      LABEL AgentSession PROPERTIES (agent_session_id, session_id),
    `test-project-0728-467323.migration_v5_demo_56f9179f.candidate` AS candidate
      KEY (candidate_id)
      LABEL Candidate PROPERTIES (candidate_id),
    `test-project-0728-467323.migration_v5_demo_56f9179f.context_snapshot` AS context_snapshot
      KEY (context_snapshot_id)
      LABEL ContextSnapshot PROPERTIES (context_snapshot_id, snapshot_payload, snapshot_timestamp),
    `test-project-0728-467323.migration_v5_demo_56f9179f.decision_execution` AS decision_execution
      KEY (decision_execution_id)
      LABEL DecisionExecution PROPERTIES (decision_execution_id, business_entity_id, latency_ms, span_id, trace_id),
    `test-project-0728-467323.migration_v5_demo_56f9179f.decision_point` AS decision_point
      KEY (decision_point_id)
      LABEL DecisionPoint PROPERTIES (decision_point_id, reversibility),
    `test-project-0728-467323.migration_v5_demo_56f9179f.selection_outcome` AS selection_outcome
      KEY (selection_outcome_id)
      LABEL SelectionOutcome PROPERTIES (selection_outcome_id)
  )
  EDGE TABLES (
    `test-project-0728-467323.migration_v5_demo_56f9179f.at_context_snapshot` AS at_context_snapshot
      KEY (decision_execution_id, context_snapshot_id)
      SOURCE KEY (decision_execution_id) REFERENCES decision_execution (decision_execution_id)
      DESTINATION KEY (context_snapshot_id) REFERENCES context_snapshot (context_snapshot_id)
      LABEL atContextSnapshot,
    `test-project-0728-467323.migration_v5_demo_56f9179f.evaluates_candidate` AS evaluates_candidate
      KEY (decision_point_id, candidate_id)
      SOURCE KEY (decision_point_id) REFERENCES decision_point (decision_point_id)
      DESTINATION KEY (candidate_id) REFERENCES candidate (candidate_id)
      LABEL evaluatesCandidate,
    `test-project-0728-467323.migration_v5_demo_56f9179f.executed_at_decision_point` AS executed_at_decision_point
      KEY (decision_execution_id, decision_point_id)
      SOURCE KEY (decision_execution_id) REFERENCES decision_execution (decision_execution_id)
      DESTINATION KEY (decision_point_id) REFERENCES decision_point (decision_point_id)
      LABEL executedAtDecisionPoint,
    `test-project-0728-467323.migration_v5_demo_56f9179f.has_selection_outcome` AS has_selection_outcome
      KEY (decision_execution_id, selection_outcome_id)
      SOURCE KEY (decision_execution_id) REFERENCES decision_execution (decision_execution_id)
      DESTINATION KEY (selection_outcome_id) REFERENCES selection_outcome (selection_outcome_id)
      LABEL hasSelectionOutcome,
    `test-project-0728-467323.migration_v5_demo_56f9179f.part_of_session` AS part_of_session
      KEY (decision_execution_id, agent_session_id)
      SOURCE KEY (decision_execution_id) REFERENCES decision_execution (decision_execution_id)
      DESTINATION KEY (agent_session_id) REFERENCES agent_session (agent_session_id)
      LABEL partOfSession,
    `test-project-0728-467323.migration_v5_demo_56f9179f.rejected_candidate` AS rejected_candidate
      KEY (selection_outcome_id, candidate_id)
      SOURCE KEY (selection_outcome_id) REFERENCES selection_outcome (selection_outcome_id)
      DESTINATION KEY (candidate_id) REFERENCES candidate (candidate_id)
      LABEL rejectedCandidate,
    `test-project-0728-467323.migration_v5_demo_56f9179f.selected_candidate` AS selected_candidate
      KEY (selection_outcome_id, candidate_id)
      SOURCE KEY (selection_outcome_id) REFERENCES selection_outcome (selection_outcome_id)
      DESTINATION KEY (candidate_id) REFERENCES candidate (candidate_id)
      LABEL selectedCandidate
  );
