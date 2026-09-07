-- Variable: agent dropdown options.
-- Lists every agent active in the time range, alphabetically.
-- The LIMIT is a defensive cap, not an expected truncation: agent cardinality
-- is normally a handful of names, so 1000 only bites on a runaway deployment
-- that would otherwise hand Grafana more options than its frontend can render.
-- Truncation drops the alphabetically last agents; narrow the dashboard's time
-- range so the window holds fewer than 1000 agents to bring them back, or type
-- the agent name straight into the picker, which accepts custom values.
SELECT DISTINCT agent
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND agent IS NOT NULL
ORDER BY agent
LIMIT 1000
