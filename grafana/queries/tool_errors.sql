-- Panel: Tool errors (Tools row).
-- Combines the dedicated TOOL_ERROR view with any error-status completions.
-- NOTE: UPPER(status) = 'ERROR' is the casing-safe predicate (see
-- queries/README.md).
-- UNION ALL intentionally preserves both telemetry records when one logical
-- failure emits a TOOL_ERROR and an error-status TOOL_COMPLETED event.
SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM `${project}.${dataset}.${view_prefix}tool_errors`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})

UNION ALL

SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM `${project}.${dataset}.${view_prefix}tool_completions`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
  AND UPPER(status) = 'ERROR'
ORDER BY timestamp DESC
LIMIT 100
