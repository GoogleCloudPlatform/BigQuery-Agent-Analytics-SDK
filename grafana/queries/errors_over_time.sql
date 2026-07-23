-- Panel: Errors over time, one series per event_type (Overview row).
SELECT
  $__timeGroup(timestamp, $__interval) AS time,
  event_type,
  COUNT(*) AS errors
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND (ENDS_WITH(event_type, '_ERROR') OR error_message IS NOT NULL OR UPPER(status) = 'ERROR')
GROUP BY time, event_type
ORDER BY time
