-- Variable: session_id dropdown options (independent filter).
-- This variable intentionally does not cascade from any other filter: no agent,
-- user_id, or event_type clause appears below, so the list is a global scan for
-- every session active in the time range, most recently active first.
-- Cascading would make an upstream filter change drop the active session out of
-- this list, and Grafana would then silently re-select a different session (or,
-- with multi-select, prune the user's selection down to whatever survived). We
-- prefer the dashboard to show "No Data" for contradictory filter selections
-- rather than forcefully changing the user's selected sessions behind their
-- back.
-- Trade-off: the dropdown lists sessions that may not match the current Agent /
-- User ID / Event Type selections, so picking one can legitimately render every
-- panel empty. That is the intended, recoverable outcome — widen the other
-- filters to bring the data back.
-- The LIMIT is a guard against session_id's cardinality, which is unbounded in
-- a way that agent, user_id and event_type are not: a busy deployment can open
-- hundreds of thousands of sessions in a single day, and handing that many
-- options to Grafana exhausts the frontend rather than producing a usable
-- dropdown. Capping at the 1000 most recently active sessions keeps the
-- variable renderable. Truncated older sessions are still reachable: narrow the
-- dashboard's time range so the window they fall in holds fewer than 1000
-- sessions, and they move back into the list.
SELECT session_id
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND session_id IS NOT NULL
GROUP BY session_id
ORDER BY MAX(timestamp) DESC
LIMIT 1000
