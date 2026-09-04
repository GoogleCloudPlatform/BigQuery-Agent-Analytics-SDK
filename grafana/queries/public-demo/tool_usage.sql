-- Panel: Tool invocations by tool (Tools row).
-- Public-demo build; conventions shared by every file here are in README.md.
SELECT
  tool_name,
  COUNT(*) AS invocations
FROM `YOUR_PROJECT_ID.YOUR_DATASET_ID.adk_tool_starts`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR)
  AND timestamp < CURRENT_TIMESTAMP()
GROUP BY tool_name
ORDER BY invocations DESC
