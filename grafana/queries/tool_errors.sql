-- Panel: Tool errors (Tools row).
-- Combines the dedicated TOOL_ERROR view with any error-status completions.
-- UNION ALL intentionally preserves both telemetry records when one logical
-- failure emits a TOOL_ERROR and an error-status TOOL_COMPLETED event.
-- No event_type filter: both views are already scoped to a single event
-- type each, so filtering them would blank the panel for every non-tool
-- selection.
SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM `${project}.${dataset}.${view_prefix}tool_errors`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))

UNION ALL

SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM `${project}.${dataset}.${view_prefix}tool_completions`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND (error_message IS NOT NULL OR UPPER(status) = 'ERROR')
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
ORDER BY timestamp DESC
LIMIT 100
