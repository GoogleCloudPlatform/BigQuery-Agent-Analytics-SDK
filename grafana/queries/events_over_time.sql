-- Panel: Events over time, one series per event_type (Overview row).
SELECT
  $__timeGroup(timestamp, $__interval) AS time,
  event_type,
  COUNT(*) AS events
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
GROUP BY time, event_type
ORDER BY time
