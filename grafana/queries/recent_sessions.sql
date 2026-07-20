-- Panel: Recent sessions (Sessions & Traces row).
-- Session-level rollup over the raw agent_events table.
-- NOTE: UPPER(status) = 'ERROR' is the casing-safe predicate (see
-- queries/README.md).
SELECT
  session_id,
  MIN(timestamp) AS started_at,
  MAX(timestamp) AS last_event_at,
  TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) AS duration_s,
  COUNT(DISTINCT agent) AS agents,
  COUNT(*) AS events,
  COUNTIF(UPPER(status) = 'ERROR') AS errors
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
GROUP BY session_id
ORDER BY started_at DESC
LIMIT 50
