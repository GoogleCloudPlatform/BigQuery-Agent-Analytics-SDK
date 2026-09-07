-- Panel: Tool latency by tool (Tools row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  tool_name,
  COUNT(*) AS completions,
  AVG(total_ms) AS avg_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_ms
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_tool_completions`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
GROUP BY tool_name
ORDER BY p95_ms DESC
