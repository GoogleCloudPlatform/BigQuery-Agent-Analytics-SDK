-- Panel: Avg LLM latency stat (Overview row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  AVG(total_ms) AS avg_llm_latency_ms
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
HAVING COUNT(*) > 0
