-- Panel: Events over time, one series per event_type (Overview row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  TIMESTAMP_TRUNC(timestamp, HOUR) AS time,
  event_type,
  COUNT(*) AS events
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.agent_events`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
GROUP BY time, event_type
ORDER BY time
