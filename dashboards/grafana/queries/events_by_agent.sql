-- Panel: Events by agent (Overview row).
-- Event volume per agent over the raw ${table}, so every event type counts, not
-- just the ones a typed view exposes.
-- This query DOES honor the Event Type filter: neither exemption in
-- queries/README.md applies (it reads the raw table, and it does not count
-- errors), so with a selection active the bars read "events of the selected
-- type(s) per agent". It is the second panel in the Overview row to do so.
-- agent is nullable on the raw table; IFNULL groups those rows under "unknown"
-- rather than dropping them, so the bars still add up to the Events stat above
-- whenever the Event Type filter is on All.
-- The output column is agent_name, not agent, so the GROUP BY names the IFNULL
-- alias instead of resolving back to the raw nullable column.
-- HAVING COUNT(*) > 0 is a written-down contract rather than a live filter: the
-- GROUP BY already emits no rows when nothing matches, which is what lets the
-- panel fall back to its "No matching data" text. Stating it next to the
-- aggregate keeps a later edit that widens the input set (an outer join, or a
-- COUNTIF replacing the count) from silently drawing zero-height bars.
SELECT
  IFNULL(agent, 'unknown') AS agent_name,
  COUNT(*) AS events
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${event_type:sqlstring}]) OR event_type IN UNNEST(ARRAY<STRING>[${event_type:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
GROUP BY agent_name
HAVING COUNT(*) > 0
ORDER BY events DESC
