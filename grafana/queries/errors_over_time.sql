-- Panel: Errors over time, one series per event_type (Overview row).
-- NOTE: UPPER(status) = 'ERROR' is the casing-safe predicate; the SDK's
-- ERROR_SQL_PREDICATE and seed_events.py disagree on status casing.
SELECT
  $__timeGroup(timestamp, $__interval) AS time,
  event_type,
  COUNT(*) AS errors
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND agent IN (${agent:sqlstring})
  AND UPPER(status) = 'ERROR'
GROUP BY time, event_type
ORDER BY time
