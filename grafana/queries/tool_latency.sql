-- Panel: Tool latency by tool (Tools row).
-- Latency stays NULL when missing (no IFNULL) so averages aren't skewed.
SELECT
  tool_name,
  COUNT(*) AS invocations,
  AVG(total_ms) AS avg_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_ms
FROM `${project}.${dataset}.${view_prefix}tool_completions`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
GROUP BY tool_name
ORDER BY p95_ms DESC
