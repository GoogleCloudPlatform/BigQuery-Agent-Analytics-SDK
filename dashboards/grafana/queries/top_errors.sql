-- Panel: Top error messages (Overview row).
-- Ranks the distinct error_message strings in the window by how often they
-- occur, so the loudest failure mode is the first row.
-- Scope: only events carrying a non-NULL error_message. An error event that
-- recorded no message (event_type ending in _ERROR, or UPPER(status) = 'ERROR',
-- with error_message NULL) has no string to group under and is intentionally
-- absent, so these counts are a subset of what Errors over time charts.
-- No event_type filter: errors arrive as their own event types (LLM_ERROR,
-- TOOL_ERROR, ...), so honoring a selection like LLM_RESPONSE would report zero
-- errors when errors did occur — the same exemption errors_over_time.sql makes.
-- HAVING COUNT(*) > 0 is a written-down contract rather than a live filter: the
-- GROUP BY already emits no rows when nothing matches, which is what lets the
-- panel fall back to its "No errors in range" text. Stating it next to the
-- aggregate keeps a later edit that widens the input set (an outer join, or the
-- error predicate moved out of the WHERE into a COUNTIF) from silently listing
-- zero-count rows.
SELECT
  error_message,
  COUNT(*) AS errors,
  COUNT(DISTINCT session_id) AS sessions,
  COUNT(DISTINCT agent) AS agents,
  MAX(timestamp) AS last_seen
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND error_message IS NOT NULL
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
GROUP BY error_message
HAVING COUNT(*) > 0
ORDER BY errors DESC
LIMIT 50
