-- Panel: Recent sessions (Sessions & Traces row).
-- Session-level rollup over the raw agent_events table.
SELECT
  session_id,
  MIN(timestamp) AS started_in_window_at,
  MAX(timestamp) AS last_event_in_window_at,
  TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) AS duration_in_window_s,
  COUNT(DISTINCT agent) AS agents_in_window,
  COUNT(*) AS events_in_window,
  COUNTIF(ENDS_WITH(event_type, '_ERROR') OR error_message IS NOT NULL OR UPPER(status) = 'ERROR') AS errors_in_window
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
GROUP BY session_id
ORDER BY started_in_window_at DESC
LIMIT 50
