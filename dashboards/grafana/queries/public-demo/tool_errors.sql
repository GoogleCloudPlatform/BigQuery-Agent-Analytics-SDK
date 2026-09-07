-- Panel: Tool errors (Tools row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_tool_errors`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()

UNION ALL

SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_tool_completions`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
  AND (error_message IS NOT NULL OR UPPER(status) = 'ERROR')
ORDER BY timestamp DESC
LIMIT 100
