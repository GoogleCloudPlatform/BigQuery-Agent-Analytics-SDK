-- Panel: LLM latency percentiles + time-to-first-token (LLM & FinOps row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  TIMESTAMP_TRUNC(timestamp, HOUR) AS time,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_total_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_total_ms,
  APPROX_QUANTILES(ttft_ms, 100)[OFFSET(50)] AS p50_ttft_ms
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
GROUP BY time
ORDER BY time
