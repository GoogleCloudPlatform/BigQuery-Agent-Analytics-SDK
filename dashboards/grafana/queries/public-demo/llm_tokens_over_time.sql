-- Panel: Token usage over time (LLM & FinOps row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  TIMESTAMP_TRUNC(timestamp, HOUR) AS time,
  IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
  IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
GROUP BY time
ORDER BY time
