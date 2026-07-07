-- Q0 (operator, not leadership): Is the pipeline healthy RIGHT NOW?
-- Boundary: @demo_run_id rides the `env` resource attribute (both products);
-- @window_hours bounds the scan. ${dataset} is substituted by run_queries.sh.
-- Expected: one row per surface; healthy = fresh_rows > 0 for logs/metrics/
-- spans and dead_letters = 0 (a zero here is an ANSWER, not an empty result).
SELECT 'otel_logs' AS surface,
       COUNT(*) AS fresh_rows,
       MAX(ingest_time) AS newest
FROM `${dataset}.otel_logs`
WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'otel_metrics(all)', COUNT(*), MAX(ingest_time)
FROM `${dataset}.bqaa_metrics` m
JOIN `${dataset}.otel_metric_sum` s USING (idempotency_key)
WHERE s.ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(s.resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'otel_spans', COUNT(*), MAX(ingest_time)
FROM `${dataset}.otel_spans`
WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'dead_letters(any run)', COUNT(*), MAX(received_at)
FROM `${dataset}.otlp_dead_letter`
WHERE received_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
ORDER BY surface;
