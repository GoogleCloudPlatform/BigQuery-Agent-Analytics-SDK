-- Panel: Tool latency by tool (Tools row).
-- Latency stays NULL when missing (no IFNULL) so averages aren't skewed.
-- No event_type filter: the view is already scoped to a single event type,
-- so filtering it would blank the panel for every non-tool selection.
SELECT
  tool_name,
  COUNT(*) AS completions,
  AVG(total_ms) AS avg_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_ms
FROM `${project}.${dataset}.${view_prefix}tool_completions`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
GROUP BY tool_name
ORDER BY p95_ms DESC
