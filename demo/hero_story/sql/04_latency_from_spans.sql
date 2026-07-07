-- Q4: How fast are the agents — p50/p95 from REAL spans, per product?
-- Traces tier only. These are observability spans (structure + timing),
-- not transcripts. Expected: a few marquee span names per product
-- (claude_code.llm_request / interaction; codex run_turn / stream_request).
SELECT
  source_product,
  span_name,
  COUNT(*) AS spans,
  APPROX_QUANTILES(TIMESTAMP_DIFF(end_timestamp, timestamp, MILLISECOND), 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(TIMESTAMP_DIFF(end_timestamp, timestamp, MILLISECOND), 100)[OFFSET(95)] AS p95_ms
FROM `${dataset}.otel_spans_dedup`
WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
  AND end_timestamp IS NOT NULL
GROUP BY 1, 2
HAVING COUNT(*) >= 2 OR span_name IN ('claude_code.llm_request', 'claude_code.interaction', 'run_turn', 'stream_request')
ORDER BY source_product, spans DESC
LIMIT 20;
