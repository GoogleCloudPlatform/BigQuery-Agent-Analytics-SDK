-- Panel: Tokens by model (LLM & FinOps row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  IFNULL(model_version, 'unknown') AS model,
  IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
  IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens,
  COUNT(*) AS responses
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
GROUP BY model
ORDER BY total_tokens DESC
