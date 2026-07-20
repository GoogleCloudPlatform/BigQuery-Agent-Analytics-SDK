-- Panel: Token usage over time (LLM & FinOps row).
-- Uses the typed ${view_prefix}llm_responses view. Missing usage telemetry
-- degrades to 0 for sums (IFNULL) rather than dropping the bucket.
SELECT
  $__timeGroup(timestamp, $__interval) AS time,
  SUM(IFNULL(usage_prompt_tokens, 0)) AS prompt_tokens,
  SUM(IFNULL(usage_completion_tokens, 0)) AS completion_tokens,
  SUM(IFNULL(usage_total_tokens, 0)) AS total_tokens
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
GROUP BY time
ORDER BY time
