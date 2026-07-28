-- Panel: Overview stats (Sessions, Events, Error rate, Avg LLM latency).
-- One row, four columns; each Grafana stat panel picks one field.
-- NOTE: The avg_llm_latency_ms subquery performs an extra full scan of llm_responses.
-- This is an intentional UX decision to provide a top-level scalar metric alongside
-- the dedicated time-series panel below.
-- The event_type filter is intentionally NOT applied anywhere in this query.
-- Errors arrive as their own event types (LLM_ERROR, TOOL_ERROR, ...), so a
-- selection like LLM_RESPONSE would exclude every error row and report a 0%
-- error rate when errors did occur — the same exemption errors_over_time.sql
-- makes. The llm_responses subquery is exempt for a second reason: that view
-- already holds exactly one event type, so filtering it would blank the metric
-- for every non-LLM selection.
-- HAVING COUNT(*) > 0 keeps the no-data contract: an unaggregated SELECT over
-- aggregates always emits one row, so an empty filter intersection would
-- otherwise report a confident "0 sessions, 0 events, 0% error rate" instead of
-- letting the stat panels fall back to their "No matching data" text.
SELECT
  COUNT(DISTINCT e.session_id) AS sessions,
  COUNT(*) AS events,
  SAFE_DIVIDE(
    COUNTIF(ENDS_WITH(e.event_type, '_ERROR') OR e.error_message IS NOT NULL OR UPPER(e.status) = 'ERROR'),
    COUNT(*)
  ) AS error_rate,
  (
    SELECT AVG(r.total_ms)
    FROM `${project}.${dataset}.${view_prefix}llm_responses` AS r
    WHERE $__timeFilter(r.timestamp)
      AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR r.agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
      AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR r.user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
      AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR r.session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
  ) AS avg_llm_latency_ms
FROM `${project}.${dataset}.${table}` AS e
WHERE $__timeFilter(e.timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR e.agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR e.user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR e.session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
HAVING COUNT(*) > 0
