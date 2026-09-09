-- Panel: LLM calls (LLM & FinOps row).
-- Public-demo build; see README.md. Counted per span, not per row, so a
-- streaming installation does not report chunks as calls; rows with no span key
-- are added back one-per-row because CONCAT of a NULL is NULL.
SELECT
  COUNT(DISTINCT CONCAT(trace_id, '|', span_id))
    + COUNTIF(trace_id IS NULL OR span_id IS NULL) AS llm_calls
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_llm_responses`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
HAVING COUNT(*) > 0
