-- Panel: Trace detail for the selected $session_id (Sessions & Traces row).
-- Queries the raw table so every event type appears in one timeline.
-- - ${session_id:sqlstring} lets Grafana SQL-escape the value (injection-safe).
-- - COALESCE(model, model_version): `model` exists on LLM_REQUEST attributes,
--   `model_version` on LLM_RESPONSE attributes — one column covers both.
SELECT
  timestamp,
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
WHERE session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}])
  AND $__timeFilter(timestamp)
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
ORDER BY timestamp
LIMIT 500
