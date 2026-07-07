-- Q2: Where is usage going by team — and what does it roughly cost?
-- Tokens are MEASURED. Dollars are an ESTIMATE from the rates CTE below:
-- edit the rates for your contract; the as_of date prints in the output.
-- Expected: >=1 row per (product, team) with token totals; est_cost_usd is
-- NULL when no rate matches (never silently zero).
WITH rates AS (
  -- source: EDIT ME (contract pricing); blended $/1M tokens, as_of below
  SELECT 'codex' AS source_product, 5.00 AS usd_per_million_tokens, DATE '2026-07-07' AS as_of
  UNION ALL
  SELECT 'claude_code', 6.00, DATE '2026-07-07'
),
tokens AS (
  SELECT
    s.source_product,
    COALESCE(JSON_VALUE(s.resource_attributes, '$.department'), 'unattributed') AS team,
    SUM(CAST(m.value AS FLOAT64)) AS total_tokens
  FROM `${dataset}.bqaa_metrics` m
  JOIN `${dataset}.otel_metric_sum` s USING (idempotency_key)
  WHERE m.metric_name IN ('codex.turn.token_usage', 'claude_code.token.usage')
    AND s.ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
    AND JSON_VALUE(s.resource_attributes, '$.env') = @demo_run_id
  GROUP BY 1, 2
)
SELECT
  t.source_product,
  t.team,
  t.total_tokens,
  ROUND(t.total_tokens / 1e6 * r.usd_per_million_tokens, 4) AS est_cost_usd,
  r.as_of AS rate_as_of
FROM tokens t
LEFT JOIN rates r USING (source_product)
ORDER BY t.total_tokens DESC;
