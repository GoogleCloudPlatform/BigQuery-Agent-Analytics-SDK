-- Panel: Tool invocations by tool (Tools row).
-- Uses the typed ${view_prefix}tool_completions view.
SELECT
  tool_name,
  COUNT(*) AS invocations
FROM `${project}.${dataset}.${view_prefix}tool_completions`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
GROUP BY tool_name
ORDER BY invocations DESC
