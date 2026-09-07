-- Panel: Recent sessions (Sessions row).
-- Public-demo build; see README.md. Unlike the interactive build there are no
-- HAVING LOGICAL_OR guards, so every rollup covers the session's whole window.
SELECT
  session_id,
  STRING_AGG(DISTINCT user_id, ', ' ORDER BY user_id) AS session_users_in_window,
  MIN(timestamp) AS started_in_window_at,
  MAX(timestamp) AS last_event_in_window_at,
  TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) AS duration_in_window_s,
  COUNT(DISTINCT agent) AS session_agents_in_window,
  COUNT(*) AS session_events_in_window,
  COUNTIF(ENDS_WITH(event_type, '_ERROR') OR error_message IS NOT NULL OR UPPER(status) = 'ERROR') AS session_errors_in_window,
  IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',
    SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64), NULL)), 0)
    AS session_input_tokens_in_window,
  IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',
    SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64), NULL)), 0)
    AS session_output_tokens_in_window
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.agent_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
  AND session_id IS NOT NULL
GROUP BY session_id
ORDER BY last_event_in_window_at DESC
LIMIT 250
