-- Panel: "Estimated Cost" stat (LLM & FinOps row).
-- Costs are DERIVED, not telemetry: the SDK records token counts only.
-- The per-1M-token rates below are placeholders — adjust them to the
-- pricing of the model(s) you actually run. The panel is intentionally
-- labeled "Estimated Cost" for this reason; token counts are the
-- primary FinOps signal.
SELECT
  SUM(IFNULL(usage_prompt_tokens, 0)) / 1e6 * 1.25    -- $ / 1M prompt tokens
    + SUM(IFNULL(usage_completion_tokens, 0)) / 1e6 * 5.00  -- $ / 1M completion tokens
    AS estimated_cost_usd,
  SUM(IFNULL(usage_total_tokens, 0)) AS total_tokens
FROM `${project}.${dataset}.${view_prefix}llm_responses`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
