-- Variable: user_id dropdown options.
-- Lists every user active in the time range, most recently active first.
-- The LIMIT is a guard against user_id's cardinality: a consumer-facing
-- deployment can touch far more users in a day than Grafana's dropdown can
-- render, and the search box runs entirely in the frontend over the options it
-- was given, so an oversized list degrades into an unusable picker. Capping at
-- the 1000 most recently active users keeps the variable renderable.
-- Truncated older users are still reachable: narrow the dashboard's time range
-- so the window they fall in holds fewer than 1000 users, or type the user ID
-- straight into the picker, which accepts custom values.
SELECT user_id
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND user_id IS NOT NULL
GROUP BY user_id
ORDER BY MAX(timestamp) DESC
LIMIT 1000
