-- Panel: LLM latency percentiles + time-to-first-token (LLM & FinOps row).
-- Latency aggregates deliberately stay NULL when telemetry is missing so
-- the chart shows gaps instead of fake zeros.
SELECT
  $__timeGroup(timestamp, $__interval) AS time,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_total_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_total_ms,
  APPROX_QUANTILES(ttft_ms, 100)[OFFSET(50)] AS p50_ttft_ms
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
GROUP BY time
ORDER BY time
