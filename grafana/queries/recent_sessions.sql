-- Panel: Recent sessions (Sessions & Traces row).
-- Session-level rollup over the raw agent_events table.
-- All four filters apply, but agent, user_id and event_type only decide *which
-- sessions are listed*: each gets its own LOGICAL_OR(...) in the HAVING over
-- the GROUP BY, and the three are ANDed. A session is kept when it contains a
-- match for each filter somewhere in the window — the matches need not be the
-- same event. Applying them in the outer WHERE would drop events before the
-- GROUP BY, so every rollup below would describe a filtered slice of the
-- session while still being labelled as the session itself.
-- HAVING rather than a `session_id IN (SELECT ...)` subquery: the subquery
-- expresses the same predicate but reads the table a second time, and BigQuery
-- bills bytes scanned per reference rather than collapsing the two reads.
-- Consequently the per-session rollup columns below cover every event the
-- session has in the window, including events the Agent / User / Event Type
-- filters excluded, so they will not sum to the Overview stats.
-- session_id IS NOT NULL matches var_session_id.sql: events written without a
-- session would otherwise collapse into one NULL row that names no session and
-- that the Session dropdown cannot select for the trace panel below.
-- The LIMIT caps the table at the 250 most recently active sessions in the
-- window. Truncated older sessions are reachable by narrowing the dashboard's
-- time range, the same escape hatch var_session_id.sql documents.
-- session_users_in_window is a STRING_AGG rather than ANY_VALUE: nothing in the
-- schema stops two user_ids from sharing a session, and ANY_VALUE would silently
-- name one of them. It reads like COUNT(DISTINCT agent) below — the column is
-- there to show when a session is not the single-user thing it was assumed to
-- be, so it lists every user rather than picking one.
-- The token columns read $.usage.prompt / $.usage.completion straight off the
-- raw content payload: this panel groups the raw table, so it cannot use the
-- typed ${view_prefix}llm_responses view that exposes the same two extractions
-- to the LLM & FinOps panels as usage_prompt_tokens / usage_completion_tokens.
-- They are named input/output to match the price_per_1m_input_tokens /
-- price_per_1m_output_tokens variables that price them. Only LLM_RESPONSE
-- carries a usage block, and the IF() says so explicitly rather than leaning on
-- the path being absent from every other event type's content — the view these
-- numbers mirror is scoped to LLM_RESPONSE, and this sum has to scope itself.
-- Like the counts, they cover the whole session in the window and so will not
-- tie out against Token usage over time whenever a filter is active.
SELECT
  session_id,
  STRING_AGG(DISTINCT user_id, ', ' ORDER BY user_id) AS session_users_in_window,
  MIN(timestamp) AS started_in_window_at,
  MAX(timestamp) AS last_event_in_window_at,
  TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) AS duration_in_window_s,
  COUNT(DISTINCT agent) AS session_agents_in_window,
  COUNT(*) AS session_events_in_window,
  COUNTIF(ENDS_WITH(event_type, '_ERROR') OR error_message IS NOT NULL OR UPPER(status) = 'ERROR') AS session_errors_in_window,
  IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',
    CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64), NULL)), 0)
    AS session_input_tokens_in_window,
  IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',
    CAST(JSON_VALUE(content, '$.usage.completion') AS INT64), NULL)), 0)
    AS session_output_tokens_in_window
FROM `${project}.${dataset}.${table}`
WHERE $__timeFilter(timestamp)
  AND session_id IS NOT NULL
  AND ('___ALL___' IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]) OR session_id IN UNNEST(ARRAY<STRING>[${session_id:sqlstring}]))
GROUP BY session_id
HAVING LOGICAL_OR('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))
  AND LOGICAL_OR('___ALL___' IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]) OR user_id IN UNNEST(ARRAY<STRING>[${user_id:sqlstring}]))
  AND LOGICAL_OR('___ALL___' IN UNNEST(ARRAY<STRING>[${event_type:sqlstring}]) OR event_type IN UNNEST(ARRAY<STRING>[${event_type:sqlstring}]))
ORDER BY last_event_in_window_at DESC
LIMIT 250
