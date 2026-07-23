SELECT DISTINCT agent FROM `${project}.${dataset}.${table}` WHERE agent IS NOT NULL AND $__timeFilter(timestamp) ORDER BY agent
