-- Panel: Token usage over time (LLM & FinOps row).
-- Uses the typed ${view_prefix}llm_responses view. Missing usage telemetry
-- degrades to 0 for sums (IFNULL) rather than dropping the bucket.
SELECT
  $__timeGroup(timestamp, $__interval) AS time,
  IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
  IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
GROUP BY time
ORDER BY time
