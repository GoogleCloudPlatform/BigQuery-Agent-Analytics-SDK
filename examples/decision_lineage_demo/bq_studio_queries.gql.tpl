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
-- presentation nodes (CampaignRun, DecisionCategory, OptionOutcome,
-- DropReason) and creates this richer graph.
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
-- AgentStep are deterministic. MediaEntity / PlanningDecision / DecisionOption
-- come from AI.GENERATE. DecisionCategory / OptionOutcome /
-- DropReason are SQL-only demo projections over those extracted
-- rows.
-- ====================================================================

-- 1a. CampaignRuns (one per campaign brief)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:CampaignRun)
RETURN COUNT(n) AS campaignRuns;

-- 1b. AgentSteps (every span the BQ AA Plugin wrote)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:AgentStep)
RETURN COUNT(n) AS agentSteps;

-- 1c. MediaEntities (entities AI.GENERATE found across all sessions)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:MediaEntity)
RETURN COUNT(n) AS mediaEntities;

-- 1d. PlanningDecisions (decisions across all sessions)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:PlanningDecision)
RETURN COUNT(n) AS decisions;

-- 1e. DecisionOptions (every option considered, SELECTED and DROPPED)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:DecisionOption)
RETURN COUNT(n) AS decisionOptions;

-- 1f. DecisionCategories (normalized labels AI.GENERATE assigned)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:DecisionCategory)
RETURN COUNT(n) AS decisionCategories;

-- 1g. OptionOutcomes (normally SELECTED and DROPPED)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:OptionOutcome)
RETURN COUNT(n) AS optionOutcomes;

-- 1h. DropReasons (distinct dropped-candidate rationales)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:DropReason)
RETURN COUNT(n) AS dropReasons;

-- 1i. PlanningDecisions per campaign run (sanity check: ~5 per session)
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH (n:PlanningDecision)
RETURN n.session_id, COUNT(n) AS decisions
GROUP BY n.session_id
ORDER BY decisions DESC;

-- ====================================================================
-- Block 2: Visualize ONE session's reasoning
--
-- Returns a richer campaign-level path:
-- CampaignRun -> PlanningDecision -> DecisionOption -> OptionOutcome.
-- This promotes the accepted/rejected state into visible graph nodes
-- instead of hiding it only as a DecisionOption property.
--
-- To visualize a different session, swap '__SESSION_ID__' with any
-- other id Block 1i returned.
-- ====================================================================
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH p =
  (cr:CampaignRun)-[:CampaignDecision]->(dp:PlanningDecision)
    -[:WeighedOption]->(c:DecisionOption)-[:HasOutcome]->(st:OptionOutcome)
WHERE cr.session_id = '__SESSION_ID__'
RETURN p;

-- Optional: dropped-candidate rationale fan-out for the same campaign.
-- This second path keeps the main visualization readable while still
-- making drop reasons first-class nodes when the audience asks
-- "why did it reject those options?"
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH p =
  (cr:CampaignRun)-[:CampaignDecision]->(dp:PlanningDecision)
    -[:WeighedOption]->(c:DecisionOption)-[:RejectedBecause]->(rr:DropReason)
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
  (step:AgentStep)-[md:DecidedAt]->(dp:PlanningDecision)
    -[ce:WeighedOption]->(cand:DecisionOption)
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
  (dp:PlanningDecision)-[ce:WeighedOption]->(cand:DecisionOption)
WHERE ce.edge_type = 'DROPPED_CANDIDATE'
RETURN DISTINCT
  dp.session_id,
  dp.decision_type,
  cand.name AS candidate_name,
  cand.score AS candidate_score,
  cand.rejection_rationale
ORDER BY dp.session_id, dp.decision_type, cand.score DESC;

-- ====================================================================
-- Block 4b: Dropped-candidate roll-up by decision category
--
-- Same predicate as Block 4 but aggregated: COUNT(cand) and
-- AVG(cand.score) per DecisionCategory across every session. The
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
  (dp:PlanningDecision)-[:InCategory]->(cat:DecisionCategory),
  (dp)-[ce:WeighedOption]->(cand:DecisionOption)
WHERE ce.edge_type = 'DROPPED_CANDIDATE'
RETURN
  cat.decision_type,
  COUNT(cand) AS dropped_count,
  AVG(cand.score) AS avg_dropped_score
GROUP BY cat.decision_type
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
-- DISTINCT collapses the cartesian product when a PlanningDecision
-- has multiple SELECTED or DROPPED candidates that bind separately
-- in the two MATCH legs above; without it the same close-call tuple
-- repeats once per (sel, drop) pairing on the same PlanningDecision.
GRAPH `__PROJECT_ID__.__DATASET_ID__.rich_agent_context_graph`
MATCH
  (dp:PlanningDecision)-[:WeighedOption]->(sel:DecisionOption),
  (dp)-[:WeighedOption]->(drop:DecisionOption)
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
