-- Panel: Overview stats (Sessions, Events, Error rate, Avg LLM latency).
-- One row, four columns; each Grafana stat panel picks one field.
-- NOTE: UPPER(status) = 'ERROR' is used instead of the SDK's
-- ERROR_SQL_PREDICATE because seed_events.py emits lowercase statuses.
SELECT
  COUNT(DISTINCT e.session_id) AS sessions,
  COUNT(*) AS events,
  SAFE_DIVIDE(
    COUNTIF(UPPER(e.status) = 'ERROR'),
    COUNT(*)
  ) AS error_rate,
  (
    SELECT AVG(r.total_ms)
    FROM `${project}.${dataset}.${view_prefix}llm_responses` AS r
    WHERE $__timeFilter(r.timestamp)
      AND r.agent IN (${agent:sqlstring})
  ) AS avg_llm_latency_ms
FROM `${project}.${dataset}.${table}` AS e
WHERE $__timeFilter(e.timestamp)
  AND e.agent IN (${agent:sqlstring})
