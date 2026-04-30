-- Copyright 2026 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License").
--
-- BigQuery Studio query bundle for the decision-lineage demo.
--
-- Open BigQuery Studio, paste each block (delimited by the "=="
-- headers) into a new query tab, and run. Block 2 returns paths and
-- renders as an interactive graph diagram.
--
-- The graph is `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`.
-- `build_graph.py` first creates the SDK's canonical
-- `agent_context_graph`; `build_rich_graph.py` then derives demo
-- presentation nodes (CampaignRun, DecisionType, CandidateStatus,
-- RejectionReason) and creates this richer graph.
--
-- Cross-session blocks (1, 4, 4b, 5) span every session in the
-- dataset. Per-session blocks (2, 3) are scoped to the first
-- session run_agent.py created — `__SESSION_ID__`. Replace it with
-- a different session id (the build output prints them all) to
-- visualize / audit a different campaign run.

-- ====================================================================
-- Block 1: Portfolio inventory — what did the SDK extract?
--
-- Counts every node label across every session. CampaignRun and
-- TechNode are deterministic. BizNode / DecisionPoint / CandidateNode
-- come from AI.GENERATE. DecisionType / CandidateStatus /
-- RejectionReason are SQL-only demo projections over those extracted
-- rows.
-- ====================================================================

-- 1a. CampaignRuns (one per campaign brief)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:CampaignRun)
RETURN COUNT(n) AS campaignRuns;

-- 1b. TechNodes (every span the BQ AA Plugin wrote)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:TechNode)
RETURN COUNT(n) AS techNodes;

-- 1c. BizNodes (entities AI.GENERATE found across all sessions)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:BizNode)
RETURN COUNT(n) AS bizNodes;

-- 1d. DecisionPoints (decisions across all sessions)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:DecisionPoint)
RETURN COUNT(n) AS decisions;

-- 1e. CandidateNodes (every option considered, SELECTED and DROPPED)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:CandidateNode)
RETURN COUNT(n) AS candidates;

-- 1f. DecisionTypes (normalized labels AI.GENERATE assigned)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:DecisionType)
RETURN COUNT(n) AS decisionTypes;

-- 1g. CandidateStatuses (normally SELECTED and DROPPED)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:CandidateStatus)
RETURN COUNT(n) AS candidateStatuses;

-- 1h. RejectionReasons (distinct dropped-candidate rationales)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:RejectionReason)
RETURN COUNT(n) AS rejectionReasons;

-- 1i. DecisionPoints per campaign run (sanity check: ~5 per session)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:DecisionPoint)
RETURN n.session_id, COUNT(n) AS decisions
GROUP BY n.session_id
ORDER BY decisions DESC;

-- ====================================================================
-- Block 2: Visualize ONE session's reasoning
--
-- Returns a richer campaign-level path:
-- CampaignRun -> DecisionPoint -> CandidateNode -> CandidateStatus.
-- This promotes the accepted/rejected state into visible graph nodes
-- instead of hiding it only as a CandidateNode property.
--
-- To visualize a different session, swap '__SESSION_ID__' with any
-- other id Block 1i returned.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH p =
  (cr:CampaignRun)-[:CampaignDecision]->(dp:DecisionPoint)
    -[:CandidateEdge]->(c:CandidateNode)-[:HasCandidateStatus]->(st:CandidateStatus)
WHERE cr.session_id = '__SESSION_ID__'
RETURN p;

-- Optional: dropped-candidate rationale fan-out for the same campaign.
-- This second path keeps the main visualization readable while still
-- making rejection reasons first-class nodes when the audience asks
-- "why did it reject those options?"
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH p =
  (cr:CampaignRun)-[:CampaignDecision]->(dp:DecisionPoint)
    -[:CandidateEdge]->(c:CandidateNode)-[:RejectedBecause]->(rr:RejectionReason)
WHERE cr.session_id = '__SESSION_ID__'
RETURN p;

-- ====================================================================
-- Block 3: EU-audit traversal for ONE session
--
-- Same shape the SDK ships as `mgr.get_eu_audit_gql(session_id)`:
-- one row per (decision, candidate) the SDK extracted for the
-- session, with rejection rationale on dropped ones.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
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
-- DISTINCT collapses graph-traversal duplicates that arise when
-- the underlying decision_points / candidates tables have multiple
-- rows for the same key (e.g. legacy data from before the
-- store_decision_points dedupe fix).
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH
  (dp:DecisionPoint)-[ce:CandidateEdge]->(cand:CandidateNode)
WHERE ce.edge_type = 'DROPPED_CANDIDATE'
RETURN DISTINCT
  dp.session_id,
  dp.decision_type,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.rejection_rationale
ORDER BY dp.session_id, dp.decision_type, cand.score DESC;

-- ====================================================================
-- Block 4b: Dropped-candidate roll-up by decision_type
--
-- Same predicate as Block 4 but aggregated: COUNT(cand) and
-- AVG(cand.score) per decision_type across every session. The
-- portfolio metric product teams want to track.
--
-- This count is correct as long as the backing `candidates` table
-- has one row per `candidate_id` (the property graph KEY). The
-- SDK enforces this at write time via _dedupe_rows_by_key in
-- store_decision_points; if you suspect legacy duplicate-key data
-- (run before that fix landed), reseat the table with:
--   CREATE OR REPLACE TABLE T AS SELECT * EXCEPT(rn) FROM
--     (SELECT *, ROW_NUMBER() OVER (PARTITION BY candidate_id) AS rn FROM T)
--     WHERE rn = 1;
-- and re-apply rich_property_graph.gql.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
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
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
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
