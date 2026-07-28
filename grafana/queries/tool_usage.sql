-- Panel: Tool invocations by tool (Tools row).
-- Uses the typed tool_starts view so failed invocations are included in the
-- volume (the tool_completions view would drop them).
-- No event_type filter: the view is already scoped to a single event type,
-- so filtering it would blank the panel for every non-tool selection.
SELECT
  tool_name,
  COUNT(*) AS invocations
FROM `${project}.${dataset}.${view_prefix}tool_starts`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
GROUP BY tool_name
ORDER BY invocations DESC
