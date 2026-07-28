-- Panel: Trace detail for the selected session (Sessions & Traces row).
-- Queries the raw table so every event type appears in one timeline.
-- - The session_id variable is interpolated with the sqlstring formatter below,
--   so Grafana SQL-escapes the value (injection-safe).
-- - session_id is selected explicitly: when the session filter is left on All,
--   or several sessions are picked, the timeline interleaves events from
--   different sessions and the column is the only way to tell them apart.
-- - COALESCE(model, model_version): `model` exists on LLM_REQUEST attributes,
--   `model_version` on LLM_RESPONSE attributes — one column covers both.
-- - ORDER BY ... DESC decides *which* 500 rows survive the LIMIT, and that is
--   the part a user cannot recover from. With the session filter on All the
--   ascending order returned the oldest 500 events in the range, so a busy
--   window showed the first few minutes and nothing since. Reading one pinned
--   session as a chronological timeline is still available: the panel is a
--   Grafana table, so clicking the `timestamp` header re-sorts the returned
--   rows client-side.
SELECT
  timestamp,
  session_id,
  event_type,
  agent,
  invocation_id,
  span_id,
  parent_span_id,
  status,
  COALESCE(
    JSON_VALUE(attributes, '$.model'),
    JSON_VALUE(attributes, '$.model_version')
  ) AS model,
  JSON_VALUE(content, '$.tool') AS tool_name,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms,
  error_message
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${event_type:sqlstring}]) OR event_type IN UNNEST(ARRAY<STRING>[${event_type:sqlstring}]))
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
ORDER BY timestamp DESC
LIMIT 500
