-- Panel: Error rate stat (Overview row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  SAFE_DIVIDE(
    COUNTIF(ENDS_WITH(event_type, '_ERROR') OR error_message IS NOT NULL OR UPPER(status) = 'ERROR'),
    COUNT(*)
  ) AS error_rate
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.agent_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
HAVING COUNT(*) > 0
