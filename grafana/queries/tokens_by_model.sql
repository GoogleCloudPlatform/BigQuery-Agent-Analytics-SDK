-- Panel: Tokens by model (LLM & FinOps row).
-- model_version is the response-side attribute exposed by the
-- ${view_prefix}llm_responses view; rows without it group under "unknown".
SELECT
  IFNULL(model_version, 'unknown') AS model,
  IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
  IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens,
  COUNT(*) AS responses
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
GROUP BY model
ORDER BY total_tokens DESC
