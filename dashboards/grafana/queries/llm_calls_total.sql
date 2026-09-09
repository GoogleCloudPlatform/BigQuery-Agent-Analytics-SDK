-- Panel: LLM calls (LLM & FinOps row).
-- Number of distinct LLM calls in the window: the call volume the token,
-- latency and cost panels in this row are averaging and summing over.
-- Counted per span, not per row: a streaming installation records one
-- LLM_RESPONSE per chunk, so COUNT(*) would report chunks as calls. This is the
-- same distinct-span definition the Looker Studio build uses for "LLM calls".
-- trace_id and span_id are nullable, and CONCAT of a NULL is NULL, so rows that
-- carry no span key are added back one-per-row rather than silently dropped.
-- No event_type filter: the llm_responses view is already scoped to a single
-- event type, so every row here is an LLM_RESPONSE by construction and adding
-- the filter would blank the stat for every non-LLM selection.
-- HAVING COUNT(*) > 0 keeps the no-data contract: an unaggregated SELECT over an
-- aggregate always emits one row, so an empty filter intersection would
-- otherwise report a confident "0 calls" instead of letting the stat panel fall
-- back to its "No matching data" text.
SELECT
  COUNT(DISTINCT CONCAT(trace_id, '|', span_id))
    + COUNTIF(trace_id IS NULL OR span_id IS NULL) AS llm_calls
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
HAVING COUNT(*) > 0
