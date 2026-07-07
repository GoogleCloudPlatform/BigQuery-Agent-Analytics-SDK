-- Q5: Is privacy holding, and is ingestion clean?
-- The privacy proof is EXACT and scoped: it searches for the scripted
-- prompt string (@scripted_prompt) across the content-bearing columns of
-- this warehouse. PASS means: this configuration wrote no prompt content
-- into THIS telemetry warehouse — not a claim about other channels.
-- Expected: three status rows, all PASS/0 for a baseline-tier run.
SELECT 'prompt_text_in_logs' AS check_name,
       IF(COUNTIF(
            STRPOS(COALESCE(TO_JSON_STRING(body), ''), @scripted_prompt) > 0
            OR STRPOS(COALESCE(TO_JSON_STRING(log_attributes), ''), @scripted_prompt) > 0
          ) = 0, 'PASS', 'FAIL') AS status,
       COUNT(*) AS rows_scanned
FROM `${dataset}.otel_logs_dedup`
WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'prompt_text_in_projection',
       IF(COUNTIF(
            STRPOS(COALESCE(TO_JSON_STRING(p.content), ''), @scripted_prompt) > 0
          ) = 0, 'PASS', 'FAIL'),
       COUNT(*)
FROM `${dataset}.agent_events_otlp` p
JOIN `${dataset}.otel_logs_dedup` l USING (idempotency_key)
WHERE p.timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(l.resource_attributes, '$.env') = @demo_run_id
UNION ALL
SELECT 'dead_letter_count',
       IF(COUNT(*) = 0, 'PASS', 'INSPECT (replayable raw_b64 preserved)'),
       COUNT(*)
FROM `${dataset}.otlp_dead_letter`
WHERE received_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR);
