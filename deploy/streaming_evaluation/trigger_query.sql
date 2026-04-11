-- Scheduled overlap scan: agent_events -> streaming evaluation worker
--
-- Placeholders:
--   PROJECT      - GCP project id
--   DATASET      - BigQuery dataset id
--   SOURCE_TABLE - source table to scan
--   SCAN_START   - overlap window lower bound (TIMESTAMP literal or parameter)
--   SCAN_END     - overlap window upper bound (TIMESTAMP literal or parameter)

SELECT
  session_id,
  trace_id,
  span_id,
  event_type,
  status,
  error_message,
  timestamp AS trigger_timestamp,
  CASE
    WHEN event_type = 'AGENT_COMPLETED' THEN 'session_terminal'
    ELSE 'error_event'
  END AS trigger_kind
FROM `PROJECT.DATASET.SOURCE_TABLE`
WHERE timestamp >= SCAN_START
  AND timestamp < SCAN_END
  AND session_id IS NOT NULL
  AND (
    event_type = 'AGENT_COMPLETED'
    OR event_type = 'TOOL_ERROR'
    OR (
      status = 'ERROR'
      AND error_message IS NOT NULL
    )
  )
ORDER BY trigger_timestamp ASC;
