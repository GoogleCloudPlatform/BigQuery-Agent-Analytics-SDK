-- Q0 (operator, not leadership): Is the pipeline healthy RIGHT NOW?
-- Boundary: logs/metrics/spans are RUN-scoped (@demo_run_id rides the `env`
-- resource attribute for both products, @window_hours bounds the scan);
-- dead-letter health is deliberately DEPLOYMENT-scoped — dead letters may
-- lack run attribution, and ingestion health is a property of the pipeline,
-- not of one run. ${dataset} is substituted by run_queries.sh.
-- Reads the *_dedup views so retries/replays never inflate the counts.
-- Expected: one row per surface; healthy = fresh_rows > 0 for logs/metrics/
-- spans and dead_letters = 0 (a zero here is an ANSWER, not an empty result).
WITH metric_rows AS (
  SELECT ingest_time, resource_attributes FROM `${dataset}.otel_metric_sum_dedup`
  UNION ALL SELECT ingest_time, resource_attributes FROM `${dataset}.otel_metric_gauge_dedup`
  UNION ALL SELECT ingest_time, resource_attributes FROM `${dataset}.otel_metric_histogram_dedup`
  UNION ALL SELECT ingest_time, resource_attributes FROM `${dataset}.otel_metric_exponential_histogram_dedup`
  UNION ALL SELECT ingest_time, resource_attributes FROM `${dataset}.otel_metric_summary_dedup`
)
SELECT 'otel_logs' AS surface,
       COUNT(*) AS fresh_rows,
       MAX(ingest_time) AS newest
FROM `${dataset}.otel_logs_dedup`
WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'otel_metrics(all five)', COUNT(*), MAX(ingest_time)
FROM metric_rows
WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'otel_spans', COUNT(*), MAX(ingest_time)
FROM `${dataset}.otel_spans_dedup`
WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'dead_letters(deployment-wide)', COUNT(*), MAX(received_at)
FROM `${dataset}.otlp_dead_letter`
WHERE received_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
ORDER BY surface;
