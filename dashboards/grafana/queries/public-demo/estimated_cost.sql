-- Panel: "Estimated Cost" stat (LLM & FinOps row).
-- Public-demo build; see README.md. The rates are the inlined USD-per-1M
-- literals below, not dashboard variables — edit them to your models'.
SELECT
  IFNULL(SUM(usage_prompt_tokens), 0) / 1e6 * 1.25    -- $ / 1M input tokens
    + IFNULL(SUM(usage_completion_tokens), 0) / 1e6 * 5.00  -- $ / 1M output tokens
    AS estimated_cost_usd
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
HAVING COUNT(*) > 0
