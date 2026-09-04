-- Q6: When a product ships a NEW event type tomorrow, do we lose it?
-- No: native otel_logs preserves every record regardless of the projection
-- allowlist. This surfaces records whose semantic event name is absent or
-- unmapped — still fully queryable, before any code changes.
-- Expected: a status row (healthy zero is an answer) plus any examples.
WITH unmapped AS (
  SELECT
    source_product,
    COALESCE(JSON_VALUE(log_attributes, '$."event.name"'), event_name, '(none)') AS raw_event,
    COUNT(*) AS records
  FROM `${dataset}.otel_logs_dedup`
  WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
    AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
    AND JSON_VALUE(log_attributes, '$."event.name"') IS NULL
  GROUP BY 1, 2
)
SELECT 'records_without_semantic_event_name' AS check_name,
       CAST(COALESCE(SUM(records), 0) AS STRING) AS value,
       'preserved natively; queryable without code changes' AS meaning
FROM unmapped
UNION ALL
SELECT CONCAT('example: ', source_product), CONCAT(raw_event, ' x', CAST(records AS STRING)), 'raw OTLP eventName retained'
FROM unmapped
ORDER BY check_name
LIMIT 10;
