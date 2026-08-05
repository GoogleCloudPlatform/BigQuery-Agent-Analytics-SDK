-- Panel: LLM calls (LLM & FinOps row).
-- Total number of LLM_RESPONSE events in the window: the call volume the token,
-- latency and cost panels in this row are averaging and summing over.
-- No event_type filter: the llm_responses view is already scoped to a single
-- event type, so COUNT(*) is the LLM_RESPONSE count by construction and adding
-- the filter would blank the stat for every non-LLM selection.
-- HAVING COUNT(*) > 0 keeps the no-data contract: an unaggregated SELECT over an
-- aggregate always emits one row, so an empty filter intersection would
-- otherwise report a confident "0 calls" instead of letting the stat panel fall
-- back to its "No matching data" text.
SELECT
  COUNT(*) AS llm_calls
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
HAVING COUNT(*) > 0
