-- Panel: Total tokens stat (LLM & FinOps row).
-- Public-demo build; see README.md. Reports the provider's usage.total, so it
-- can exceed prompt + completion when cached or reasoning tokens are billed.
SELECT
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
HAVING COUNT(*) > 0
