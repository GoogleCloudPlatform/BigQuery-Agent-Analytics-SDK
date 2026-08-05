-- Panel: LLM calls (LLM & FinOps row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  COUNT(*) AS llm_calls
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
HAVING COUNT(*) > 0
