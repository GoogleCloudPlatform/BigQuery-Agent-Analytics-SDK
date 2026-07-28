-- Panel: "Estimated Cost" stat (LLM & FinOps row).
-- Costs are DERIVED, not telemetry: the SDK records token counts only.
-- The rates come from the price_per_1m_input_tokens /
-- price_per_1m_output_tokens
-- dashboard variables, whose defaults are placeholders — edit them in
-- Dashboard settings > Variables to match the pricing of the model(s) you
-- actually run, rather than editing this file. Both variables are read as
-- USD per 1,000,000 tokens, which is the unit model price lists publish.
-- The panel is intentionally labeled "Estimated Cost" for this reason;
-- token counts are the primary FinOps signal.
-- The two variables are `constant`s with skipUrlSync, so a crafted dashboard
-- URL cannot substitute arbitrary text into the arithmetic below.
-- No event_type filter: the llm_responses view is already scoped to a
-- single event type, so filtering it would blank the panel for every
-- non-LLM selection.
-- HAVING COUNT(*) > 0 keeps the no-data contract: an unaggregated SELECT over
-- aggregates always emits one row, so an empty filter intersection would
-- otherwise report a confident zero cost / zero tokens instead of letting the
-- panels fall back to their "No matching data" text.
SELECT
  IFNULL(SUM(usage_prompt_tokens), 0) / 1e6 * ${price_per_1m_input_tokens}    -- $ / 1M input tokens
    + IFNULL(SUM(usage_completion_tokens), 0) / 1e6 * ${price_per_1m_output_tokens}  -- $ / 1M output tokens
    AS estimated_cost_usd,
  -- NOTE: This column is consumed dynamically by the dependent 'Total tokens' panel
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
HAVING COUNT(*) > 0
