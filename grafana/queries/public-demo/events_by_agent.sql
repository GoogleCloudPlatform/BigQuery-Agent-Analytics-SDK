-- Panel: Events by agent (Overview row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  IFNULL(agent, 'unknown') AS agent_name,
  COUNT(*) AS events
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.agent_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
GROUP BY agent_name
HAVING COUNT(*) > 0
ORDER BY events DESC
