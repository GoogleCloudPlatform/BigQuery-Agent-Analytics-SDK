-- Panel: Overview stats (Sessions, Events, Error rate, Avg LLM latency).
-- One row, four columns; each Grafana stat panel picks one field.
-- NOTE: The avg_llm_latency_ms subquery performs an extra full scan of llm_responses.
-- This is an intentional UX decision to provide a top-level scalar metric alongside
-- the dedicated time-series panel below.
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
  ) AS avg_llm_latency_ms
FROM `${project}.${dataset}.${table}` AS e
WHERE $__timeFilter(e.timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR e.agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
