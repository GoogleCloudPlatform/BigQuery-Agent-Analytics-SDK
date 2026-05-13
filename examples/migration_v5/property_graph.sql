CREATE OR REPLACE PROPERTY GRAPH `test-project-0728-467323.migration_v5_demo.mako_demo_graph`
  NODE TABLES (
    `test-project-0728-467323.migration_v5_demo.agent_session` AS agent_session
      KEY (id)
      LABEL AgentSession PROPERTIES (id, session_id),
    `test-project-0728-467323.migration_v5_demo.candidate` AS candidate
      KEY (id)
      LABEL Candidate PROPERTIES (id),
    `test-project-0728-467323.migration_v5_demo.context_snapshot` AS context_snapshot
      KEY (id)
      LABEL ContextSnapshot PROPERTIES (id, snapshot_payload, snapshot_timestamp),
    `test-project-0728-467323.migration_v5_demo.decision_execution` AS decision_execution
      KEY (id)
      LABEL DecisionExecution PROPERTIES (id, business_entity_id, latency_ms, span_id, trace_id),
    `test-project-0728-467323.migration_v5_demo.decision_point` AS decision_point
      KEY (id)
      LABEL DecisionPoint PROPERTIES (id, reversibility),
    `test-project-0728-467323.migration_v5_demo.selection_outcome` AS selection_outcome
      KEY (id)
      LABEL SelectionOutcome PROPERTIES (id)
  )
  EDGE TABLES (
    `test-project-0728-467323.migration_v5_demo.at_context_snapshot` AS at_context_snapshot
      SOURCE KEY (decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      DESTINATION KEY (context_snapshot_id) REFERENCES `test-project-0728-467323.migration_v5_demo.context_snapshot` (id)
      LABEL atContextSnapshot,
    `test-project-0728-467323.migration_v5_demo.evaluates_candidate` AS evaluates_candidate
      SOURCE KEY (decision_point_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_point` (id)
      DESTINATION KEY (candidate_id) REFERENCES `test-project-0728-467323.migration_v5_demo.candidate` (id)
      LABEL evaluatesCandidate,
    `test-project-0728-467323.migration_v5_demo.evolved_from` AS evolved_from
      SOURCE KEY (src_decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      DESTINATION KEY (dst_decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      LABEL evolvedFrom,
    `test-project-0728-467323.migration_v5_demo.executed_at_decision_point` AS executed_at_decision_point
      SOURCE KEY (decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      DESTINATION KEY (decision_point_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_point` (id)
      LABEL executedAtDecisionPoint,
    `test-project-0728-467323.migration_v5_demo.has_selection_outcome` AS has_selection_outcome
      SOURCE KEY (decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      DESTINATION KEY (selection_outcome_id) REFERENCES `test-project-0728-467323.migration_v5_demo.selection_outcome` (id)
      LABEL hasSelectionOutcome,
    `test-project-0728-467323.migration_v5_demo.part_of_session` AS part_of_session
      SOURCE KEY (decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      DESTINATION KEY (session_id) REFERENCES `test-project-0728-467323.migration_v5_demo.agent_session` (id)
      LABEL partOfSession,
    `test-project-0728-467323.migration_v5_demo.rejected_candidate` AS rejected_candidate
      SOURCE KEY (selection_outcome_id) REFERENCES `test-project-0728-467323.migration_v5_demo.selection_outcome` (id)
      DESTINATION KEY (candidate_id) REFERENCES `test-project-0728-467323.migration_v5_demo.candidate` (id)
      LABEL rejectedCandidate,
    `test-project-0728-467323.migration_v5_demo.selected_candidate` AS selected_candidate
      SOURCE KEY (selection_outcome_id) REFERENCES `test-project-0728-467323.migration_v5_demo.selection_outcome` (id)
      DESTINATION KEY (candidate_id) REFERENCES `test-project-0728-467323.migration_v5_demo.candidate` (id)
      LABEL selectedCandidate,
    `test-project-0728-467323.migration_v5_demo.superseded_by` AS superseded_by
      SOURCE KEY (src_decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      DESTINATION KEY (dst_decision_execution_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_execution` (id)
      LABEL supersededBy
  );
