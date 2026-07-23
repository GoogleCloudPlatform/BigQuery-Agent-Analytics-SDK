-- Panel: Tool invocations by tool (Tools row).
-- Uses the typed ${view_prefix}tool_starts view so failed invocations are
-- included in the volume (${view_prefix}tool_completions would drop them).
SELECT
  tool_name,
  COUNT(*) AS invocations
FROM `${project}.${dataset}.${view_prefix}tool_starts`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
GROUP BY tool_name
ORDER BY invocations DESC
