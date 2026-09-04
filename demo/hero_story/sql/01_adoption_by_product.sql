-- Q1: Who is using Claude Code vs Codex — sessions and active users?
-- Expected: one row per product from THIS demo run; Claude carries user
-- identity in log attributes; Codex sessions count via conversation starts.
-- user_id is HASHED for forwardable evidence (redaction rules).
SELECT
  l.source_product,
  COUNT(DISTINCT JSON_VALUE(l.log_attributes, '$."session.id"')) AS claude_sessions,
  COUNTIF(JSON_VALUE(l.log_attributes, '$."event.name"') = 'codex.conversation_starts') AS codex_conversations,
  COUNT(DISTINCT TO_HEX(SHA256(JSON_VALUE(l.log_attributes, '$."user.id"')))) AS distinct_users_hashed,
  COUNT(*) AS log_events
FROM `${dataset}.otel_logs_dedup` l
WHERE l.ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @window_hours HOUR)
  AND JSON_VALUE(l.resource_attributes, '$.env') = @demo_run_id
GROUP BY l.source_product
ORDER BY l.source_product;
