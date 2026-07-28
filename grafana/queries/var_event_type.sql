SELECT DISTINCT event_type FROM `${project}.${dataset}.${table}` WHERE event_type IS NOT NULL AND $__timeFilter(timestamp) ORDER BY event_type
