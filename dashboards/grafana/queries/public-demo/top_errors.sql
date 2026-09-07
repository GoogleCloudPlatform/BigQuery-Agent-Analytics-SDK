-- Panel: Top error messages (Overview row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  error_message,
  COUNT(*) AS errors,
  COUNT(DISTINCT session_id) AS sessions,
  COUNT(DISTINCT agent) AS agents,
  MAX(timestamp) AS last_seen
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.agent_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
  AND error_message IS NOT NULL
GROUP BY error_message
HAVING COUNT(*) > 0
ORDER BY errors DESC
LIMIT 50
