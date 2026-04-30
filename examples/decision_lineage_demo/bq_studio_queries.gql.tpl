-- Copyright 2026 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License").
--
-- BigQuery Studio query bundle for the decision-lineage demo.
--
-- Open BigQuery Studio, paste each block (delimited by the "=="
-- headers) into a new query tab, and run. Block 2 returns paths and
-- renders as an interactive graph diagram.
--
-- The graph is `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`,
-- built by `build_graph.py` calling
-- `ContextGraphManager.build_context_graph(use_ai_generate=True,
-- include_decisions=True)` against every session captured by
-- `run_agent.py`.
--
-- Cross-session blocks (1, 4, 4b, 5) span every session in the
-- dataset. Per-session blocks (2, 3) are scoped to the first
-- session run_agent.py created — `__SESSION_ID__`. Replace it with
-- a different session id (the build output prints them all) to
-- visualize / audit a different campaign run.

-- ====================================================================
-- Block 1: Portfolio inventory — what did the SDK extract?
--
-- Counts every node label across every session. The TechNode count
-- is deterministic (= total spans the BQ AA Plugin wrote). BizNode /
-- DecisionPoint / CandidateNode counts come from AI.GENERATE and
-- vary run-to-run; the demo cares that each is non-zero and roughly
-- proportional to the session count.
-- ====================================================================

-- 1a. TechNodes (every span the BQ AA Plugin wrote)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:TechNode)
RETURN COUNT(n) AS techNodes;

-- 1b. BizNodes (entities AI.GENERATE found across all sessions)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:BizNode)
RETURN COUNT(n) AS bizNodes;

-- 1c. DecisionPoints (decisions across all sessions)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:DecisionPoint)
RETURN COUNT(n) AS decisions;

-- 1d. CandidateNodes (every option considered, SELECTED and DROPPED)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:CandidateNode)
RETURN COUNT(n) AS candidates;

-- 1e. DecisionPoints per session (sanity check: ~5 per session)
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH (n:DecisionPoint)
RETURN n.session_id, COUNT(n) AS decisions
GROUP BY n.session_id
ORDER BY decisions DESC;

-- ====================================================================
-- Block 2: Visualize ONE session's reasoning
--
-- Returns paths from each span that made a decision, through the
-- decision, out to its candidates — all scoped to a single session
-- so the graph diagram stays readable. BigQuery Studio renders this
-- as an interactive graph.
--
-- To visualize a different session, swap '__SESSION_ID__' with any
-- other id Block 1e returned.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH p =
  (s:TechNode)-[:MadeDecision]->(dp:DecisionPoint)-[:CandidateEdge]->(c:CandidateNode)
WHERE dp.session_id = '__SESSION_ID__'
RETURN p;

-- ====================================================================
-- Block 3: EU-audit traversal for ONE session
--
-- Same shape the SDK ships as `mgr.get_eu_audit_gql(session_id)`:
-- one row per (decision, candidate) the SDK extracted for the
-- session, with rejection rationale on dropped ones.
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
-- Block 4: Dropped candidates — detail view across the portfolio
--
-- One row per dropped candidate across every session. Same shape
-- the SDK ships as `mgr.get_dropped_candidates_gql()`, with the
-- session_id filter dropped so it spans the portfolio.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH
  (dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)
WHERE ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  dp.session_id,
  dp.decision_type,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.rejection_rationale
ORDER BY dp.session_id, dp.decision_type, cand.score DESC;

-- ====================================================================
-- Block 4b: Dropped-candidate roll-up by decision_type
--
-- Same predicate as Block 4 but aggregated: COUNT(c) and AVG(score)
-- per decision_type across every session. The portfolio metric
-- product teams want to track.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH
  (dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)
WHERE ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  dp.decision_type,
  COUNT(cand) AS dropped_count,
  AVG(cand.score) AS avg_dropped_score
GROUP BY dp.decision_type
ORDER BY dropped_count DESC;

-- ====================================================================
-- Block 5: Sessions whose top dropped candidate was within 0.05 of
-- the SELECTED one — "close calls" worth a second look
--
-- Joins each decision's SELECTED candidate to its DROPPED candidates
-- and flags the cases where the agent only narrowly preferred the
-- chosen option. Works at the portfolio level by including session
-- and decision context.
-- ====================================================================
-- DISTINCT collapses the cartesian product when a DecisionPoint
-- has multiple SELECTED or DROPPED candidates that bind separately
-- in the two MATCH legs above; without it the same close-call tuple
-- repeats once per (sel, drop) pairing on the same DecisionPoint.
GRAPH `__PROJECT_ID__.__DATASET_ID__.agent_context_graph`
MATCH
  (dp:DecisionPoint)-[:CandidateEdge]->(sel:CandidateNode),
  (dp)-[:CandidateEdge]->(drop:CandidateNode)
WHERE sel.status = 'SELECTED'
  AND drop.status = 'DROPPED'
  AND sel.score - drop.score < 0.05
RETURN DISTINCT
  dp.session_id,
  dp.decision_type,
  sel.name AS selected_name,
  sel.score AS selected_score,
  drop.name AS dropped_name,
  drop.score AS dropped_score,
  drop.rejection_rationale
ORDER BY sel.score - drop.score ASC;
