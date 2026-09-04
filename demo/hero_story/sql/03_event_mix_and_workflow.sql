-- Q3: What are the agents actually doing?
-- Uses the BQAA projection (agent_events_otlp), scoped to the demo run via
-- an idempotency-key join back to native logs (the projection intentionally
-- carries log attributes, not resource attributes).
-- Expected: codex.* and claude_code.* event types side by side.
SELECT
  p.source_product,
  p.event_type,
  COUNT(*) AS events
FROM `${dataset}.agent_events_otlp` p
JOIN `${dataset}.otel_logs_dedup` l USING (idempotency_key)
WHERE p.timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(l.resource_attributes, '$.env') = @demo_run_id
GROUP BY 1, 2
ORDER BY p.source_product, events DESC;
