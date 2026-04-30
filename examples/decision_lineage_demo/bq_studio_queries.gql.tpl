-- Copyright 2026 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License").
--
-- BigQuery Studio query bundle for the decision-lineage demo.
--
-- Open BigQuery Studio, paste each block (delimited by the
-- "==" headers) into a new query tab, and run. Block 2 returns
-- paths and renders as an interactive graph diagram.
--
-- The graph is `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
-- and was built by `build_graph.py` calling
-- `ContextGraphManager.build_context_graph(use_ai_generate=True,
-- include_decisions=True)` against the seeded traces.

-- ====================================================================
-- Block 1: What did the SDK extract?
--
-- Four tiny GQL queries — run each, confirm non-zero rows. These
-- prove AI.GENERATE actually populated the decision tables before
-- you start the visual demo.
-- ====================================================================

-- 1a. TechNodes (agent execution spans, written by the BQ AA Plugin)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:TechNode)
WHERE n.session_id = '__SESSION_ID__'
RETURN COUNT(n) AS techNodes;

-- 1b. BizNodes (business entities AI.GENERATE found in the traces)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:BizNode)
WHERE n.session_id = '__SESSION_ID__'
RETURN COUNT(n) AS bizNodes;

-- 1c. DecisionPoints (moments where the agent picked between options)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:DecisionPoint)
WHERE n.session_id = '__SESSION_ID__'
RETURN COUNT(n) AS decisions;

-- 1d. CandidateNodes (every option considered, SELECTED and DROPPED)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:CandidateNode)
WHERE n.session_id = '__SESSION_ID__'
RETURN COUNT(n) AS candidates;

-- ====================================================================
-- Block 2: Visualize the agent's reasoning
--
-- Returns paths from each span that made a decision, through the
-- decision, out to every candidate. BigQuery Studio renders this as
-- an interactive graph — DROPPED candidates show up as branches off
-- the decision node alongside the SELECTED one.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH p =
  (s:TechNode)-[:MadeDecision]->(dp:DecisionPoint)-[:CandidateEdge]->(c:CandidateNode)
WHERE dp.session_id = '__SESSION_ID__'
RETURN p;

-- ====================================================================
-- Block 3: EU-audit traversal
--
-- The same query the SDK ships as `mgr.get_eu_audit_gql(session_id)`.
-- For one session, returns every decision with every candidate and
-- the rejection rationale on dropped ones. This is the "show your
-- work" panel a compliance reviewer would run.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH
  (step:TechNode)-[md:MadeDecision]->(dp:DecisionPoint)
    -[ce:CandidateEdge]->(cand:CandidateNode)
WHERE dp.session_id = '__SESSION_ID__'
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
ORDER BY dp.decision_id, cand.score DESC;

-- ====================================================================
-- Block 4: Dropped candidates — detail view
--
-- Filters Block 3 down to just the rejections, ordered by decision and
-- score. One row per dropped candidate. The same shape the SDK ships
-- as `mgr.get_dropped_candidates_gql()`. Drop the session_id filter
-- and replace it with a date-range filter on the underlying
-- agent_events table to fan this out across every agent run.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH
  (dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)
WHERE dp.session_id = '__SESSION_ID__'
  AND ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  dp.decision_id,
  dp.decision_type,
  dp.description AS decision_description,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.rejection_rationale
ORDER BY dp.decision_id, cand.score DESC;

-- ====================================================================
-- Block 4b (optional): Dropped-candidate roll-up by decision type
--
-- Same predicate as Block 4, but aggregates: COUNT(c) and AVG(score)
-- per decision_type. Useful for portfolio-level metrics across many
-- sessions — drop the session_id filter and replace with a date range.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH
  (dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)
WHERE dp.session_id = '__SESSION_ID__'
  AND ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  dp.decision_type,
  COUNT(cand) AS dropped_count,
  AVG(cand.score) AS avg_dropped_score
GROUP BY dp.decision_type
ORDER BY dropped_count DESC;
