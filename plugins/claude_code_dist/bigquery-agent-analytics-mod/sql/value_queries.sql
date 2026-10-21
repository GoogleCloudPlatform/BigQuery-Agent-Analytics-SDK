-- Copyright 2026 Google LLC
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- The questions docs/value.md answers, one query each: a developer (V1, V2),
-- a team lead or administrator (V3 to V6) and a platform team (V7).
-- Placeholders as in sample_queries.sql: {PROJECT}, {DATASET}, {TABLE} and
-- the parameters @start and @end, which
-- bigquery-agent-analytics-skill/scripts/run_bq.py fills and binds. It runs
-- with your Application Default Credentials, not the mod's settings, and
-- stops a query that would bill more than --max-gb (1 GB by default). Pass
-- one query at a time:
--
--   python3 bigquery-agent-analytics-skill/scripts/run_bq.py \
--     --project my-gcp-project --dataset agent_analytics --table agent_events \
--     --start 2026-10-01 --end 2026-10-31 --max-gb 1 "<one query below>"
--
-- Add --dry-run to see the bytes a query would scan first. Every query reads
-- the raw table, so it needs read access to it (an administrator, or your
-- own sandbox); readers without it use the views in <dataset>_reporting.
--
-- Every query reads only this mod's rows (attributes.writer.plugin), except
-- V7, which sets them beside ADK agents' on purpose, and keeps one row per
-- event_id: a row resent after an ambiguous timeout, or by a replay, has the
-- same event_id. Rows without an event_id (ADK's) are all kept.
-- tests/queries.live.py runs each one on a synthetic fixture.

-- V1. Developer: my sessions at a glance. Per session: when it started, how
--     long it was open, its prompts, turns, model requests, tool calls, the
--     tool calls that failed, the tokens recorded, and the responses that
--     recorded none (their tokens are unknown, not zero). Add
--     AND user_id = 'you' for yours (user_id is the login name the machine
--     reported).
WITH events AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  session_id,
  ANY_VALUE(user_id) AS user_id,
  MIN(timestamp) AS started_at,
  ROUND(TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) / 60, 1) AS minutes_open,
  COUNTIF(event_type = 'USER_MESSAGE_RECEIVED') AS prompts,
  COUNTIF(event_type = 'INVOCATION_STARTING') AS turns,
  COUNTIF(event_type = 'LLM_RESPONSE') AS model_requests,
  COUNTIF(event_type = 'TOOL_COMPLETED') AS tool_calls,
  COUNTIF(event_type = 'TOOL_COMPLETED' AND status = 'ERROR') AS failed_tool_calls,
  SUM(IF(event_type = 'LLM_RESPONSE', SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.total_tokens') AS INT64), 0)) AS tokens,
  COUNTIF(event_type = 'LLM_RESPONSE' AND JSON_VALUE(attributes, '$.usage_metadata.total_tokens') IS NULL) AS responses_without_usage
FROM events
GROUP BY session_id
ORDER BY started_at DESC;

-- V2. Developer: where each turn's time went. The turn's total (NULL when
--     no end of turn was recorded), the time in model requests, answered or
--     failed, and in tool calls (a permission prompt's wait is part of its
--     tool call's), and the tool calls that asked for permission. A request
--     or a tool call is its rows on one span, its start and its end, and
--     its time is its end's duration. A part is NULL when one of its
--     requests or calls has no time: it recorded no duration, or its end is
--     not in the table by the end of the window (it ended later, or the row
--     was lost). Unknown, not zero; a turn with no requests, or no tool
--     calls, has 0 for that part. A turn with a row in the window is taken
--     whole, its rows looked up from a day before the window, so one that
--     began before it keeps its earlier calls. whole_turn is FALSE when the
--     turn's start row is not among them: it began more than a day before
--     the window, the mod was loaded mid-turn, or the row was lost. Its
--     earlier calls may be missing, so its model and tool time and its
--     permission prompts are NULL, and started_at comes from its end row
--     (its end less its total; NULL without one). The parts can add up to
--     more than the turn: a tool call can run beside a model request.
WITH events AS (
  SELECT
    *,
    timestamp >= @start AS in_window
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN TIMESTAMP_SUB(@start, INTERVAL 1 DAY) AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
    AND invocation_id IS NOT NULL
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
),
turn_rows AS (
  SELECT *
  FROM events
  WHERE TRUE
  QUALIFY LOGICAL_OR(in_window) OVER (PARTITION BY session_id, invocation_id)
),
calls AS (
  SELECT
    session_id,
    invocation_id,
    LOGICAL_OR(STARTS_WITH(event_type, 'LLM_')) AS model,
    MAX(IF(event_type IN ('LLM_RESPONSE', 'LLM_ERROR', 'TOOL_COMPLETED'),
      SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), NULL)) AS ms
  FROM turn_rows
  WHERE event_type IN ('LLM_REQUEST', 'LLM_RESPONSE', 'LLM_ERROR', 'TOOL_STARTING', 'TOOL_COMPLETED')
  GROUP BY session_id, invocation_id, IFNULL(span_id, event_id)
),
parts AS (
  SELECT
    session_id,
    invocation_id,
    IF(LOGICAL_OR(model AND ms IS NULL), NULL, IFNULL(SUM(IF(model, ms, NULL)), 0)) AS model_ms,
    IF(LOGICAL_OR(NOT model AND ms IS NULL), NULL, IFNULL(SUM(IF(model, NULL, ms)), 0)) AS tool_ms
  FROM calls
  GROUP BY session_id, invocation_id
),
turns AS (
  SELECT
    session_id,
    invocation_id,
    LOGICAL_OR(event_type = 'INVOCATION_STARTING') AS whole_turn,
    MIN(timestamp) AS first_row_at,
    MAX(IF(event_type IN ('INVOCATION_COMPLETED', 'INVOCATION_ERROR'), timestamp, NULL)) AS ended_at,
    MAX(IF(event_type IN ('INVOCATION_COMPLETED', 'INVOCATION_ERROR'), SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), NULL)) AS turn_ms,
    COUNTIF(event_type = 'TOOL_COMPLETED' AND JSON_VALUE(attributes, '$.permission_requested') = 'true') AS permission_prompts
  FROM turn_rows
  GROUP BY session_id, invocation_id
)
SELECT
  t.session_id,
  t.invocation_id,
  IF(t.whole_turn, t.first_row_at, TIMESTAMP_SUB(t.ended_at, INTERVAL t.turn_ms MILLISECOND)) AS started_at,
  t.whole_turn,
  t.turn_ms,
  IF(NOT t.whole_turn, NULL, IF(p.invocation_id IS NULL, 0, p.model_ms)) AS model_ms,
  IF(NOT t.whole_turn, NULL, IF(p.invocation_id IS NULL, 0, p.tool_ms)) AS tool_ms,
  IF(t.whole_turn, t.permission_prompts, NULL) AS permission_prompts
FROM turns AS t
LEFT JOIN parts AS p
  ON p.invocation_id = t.invocation_id AND p.session_id IS NOT DISTINCT FROM t.session_id
ORDER BY started_at DESC;

-- V3. Team lead: adoption against a roster. Per calendar day of the
--     window: the people enrolled (a table of their login names that you
--     keep, team_roster, with one user_id column), how many of them have
--     rows, their sessions and turns, and the sessions from no one on the
--     roster or with no user_id. A person with no rows is "not seen", not
--     zero use: they may not have used the agent, or their sessions may not
--     be recorded. A day with no rows at all is listed too, every enrolled
--     person not seen, and no_rows_that_day says so: absent telemetry, not
--     proof that nobody worked. Under impersonation leave
--     userIdFromCredentials off, or every row carries the service account
--     and the roster matches no one.
WITH events AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
),
roster AS (
  SELECT DISTINCT user_id FROM `{PROJECT}.{DATASET}.team_roster`
),
days AS (
  SELECT day FROM UNNEST(GENERATE_DATE_ARRAY(DATE(@start), DATE(@end))) AS day
),
daily AS (
  SELECT
    DATE(e.timestamp) AS day,
    COUNT(*) AS rows_that_day,
    COUNT(DISTINCT IF(r.user_id IS NOT NULL, e.user_id, NULL)) AS enrolled_seen,
    COUNT(DISTINCT IF(r.user_id IS NOT NULL, e.session_id, NULL)) AS enrolled_sessions,
    COUNTIF(r.user_id IS NOT NULL AND e.event_type = 'INVOCATION_STARTING') AS enrolled_turns,
    COUNT(DISTINCT IF(r.user_id IS NULL, e.session_id, NULL)) AS other_sessions
  FROM events AS e
  LEFT JOIN roster AS r ON e.user_id = r.user_id
  GROUP BY day
)
SELECT
  d.day,
  (SELECT COUNT(*) FROM roster) AS enrolled,
  IFNULL(a.enrolled_seen, 0) AS enrolled_seen,
  (SELECT COUNT(*) FROM roster) - IFNULL(a.enrolled_seen, 0) AS enrolled_not_seen,
  IFNULL(a.enrolled_sessions, 0) AS enrolled_sessions,
  IFNULL(a.enrolled_turns, 0) AS enrolled_turns,
  IFNULL(a.other_sessions, 0) AS other_sessions,
  a.day IS NULL AS no_rows_that_day
FROM days AS d
LEFT JOIN daily AS a USING (day)
ORDER BY d.day DESC;

-- V4. Team lead: estimated cost per session and model. Tokens split the
--     way they are priced (uncached input, cache reads, cache writes,
--     output) times the rates you put in `rates`: USD per million tokens,
--     one row per model id as the model column shows it, with the date and
--     source of the rates. An estimate, not a bill: a model with no rate
--     row has no cost (NULL), not zero; nor has a session and model with a
--     response that recorded no usage (responses_without_usage counts
--     them), as its tokens are unknown. Discounts and other charges are
--     not counted.
WITH rates AS (
  -- rates: begin
  SELECT * FROM UNNEST([
    STRUCT('your-model-id' AS model, DATE '2026-01-01' AS rates_as_of, 'where these rates come from' AS rates_source,
           0.0 AS input_usd, 0.0 AS cache_read_usd, 0.0 AS cache_write_usd, 0.0 AS output_usd)
  ])
  -- rates: end
),
responses AS (
  SELECT
    session_id,
    JSON_VALUE(attributes, '$.model') AS model,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.input_tokens') AS INT64) AS input_tokens,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_read_input_tokens') AS INT64) AS cache_read_tokens,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_creation_input_tokens') AS INT64) AS cache_write_tokens,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.output_tokens') AS INT64) AS output_tokens
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE event_type = 'LLM_RESPONSE'
    AND timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  r.session_id,
  r.model,
  COUNT(*) AS model_requests,
  SUM(IFNULL(r.input_tokens, 0)) AS input_tokens,
  SUM(IFNULL(r.cache_read_tokens, 0)) AS cache_read_tokens,
  SUM(IFNULL(r.cache_write_tokens, 0)) AS cache_write_tokens,
  SUM(IFNULL(r.output_tokens, 0)) AS output_tokens,
  COUNTIF(r.input_tokens IS NULL OR r.output_tokens IS NULL OR r.cache_read_tokens IS NULL OR r.cache_write_tokens IS NULL) AS responses_without_usage,
  ANY_VALUE(p.rates_as_of) AS rates_as_of,
  IF(COUNTIF(r.input_tokens IS NULL OR r.output_tokens IS NULL OR r.cache_read_tokens IS NULL OR r.cache_write_tokens IS NULL) > 0, NULL,
    ROUND((SUM(r.input_tokens) * ANY_VALUE(p.input_usd)
      + SUM(r.cache_read_tokens) * ANY_VALUE(p.cache_read_usd)
      + SUM(r.cache_write_tokens) * ANY_VALUE(p.cache_write_usd)
      + SUM(r.output_tokens) * ANY_VALUE(p.output_usd)) / 1e6, 6)) AS estimated_usd
FROM responses AS r
LEFT JOIN rates AS p USING (model)
GROUP BY r.session_id, r.model
ORDER BY r.session_id, r.model;

-- V5. Team lead: the tools people use most. Per tool: the people who used
--     it, its calls, and the share of calls that failed, asked for
--     permission, and were refused (refused_pct). Each refused call is also
--     counted once by where the refusal came from: the person at the prompt
--     (rejected or aborted), a hook, a permission rule
--     (permission_decision_source config), or another source
--     (refused_other_pct: one the host did not give, which the mod records
--     as unknown, or one not named here). Those four add up to refused_pct.
--     A call that a rule blocks before the host records it leaves no row.
WITH tools AS (
  SELECT
    *,
    CASE
      WHEN JSON_VALUE(attributes, '$.permission_decision') = 'reject'
        AND JSON_VALUE(attributes, '$.permission_decision_source') IN ('user_reject', 'user_abort') THEN 'prompt'
      WHEN JSON_VALUE(attributes, '$.denied') = 'true'
        OR (JSON_VALUE(attributes, '$.permission_decision') = 'reject' AND JSON_VALUE(attributes, '$.permission_decision_source') = 'hook') THEN 'hook'
      WHEN JSON_VALUE(attributes, '$.permission_decision') = 'reject' AND JSON_VALUE(attributes, '$.permission_decision_source') = 'config' THEN 'rule'
      WHEN JSON_VALUE(attributes, '$.permission_decision') = 'reject' THEN 'other'
    END AS refusal
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE event_type = 'TOOL_COMPLETED'
    AND timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  JSON_VALUE(content, '$.tool') AS tool,
  COUNT(DISTINCT user_id) AS people,
  COUNT(*) AS calls,
  ROUND(COUNTIF(status = 'ERROR') / COUNT(*) * 100, 1) AS error_pct,
  ROUND(COUNTIF(JSON_VALUE(attributes, '$.permission_requested') = 'true') / COUNT(*) * 100, 1) AS asked_pct,
  ROUND(COUNTIF(refusal IS NOT NULL) / COUNT(*) * 100, 1) AS refused_pct,
  ROUND(COUNTIF(refusal = 'prompt') / COUNT(*) * 100, 1) AS rejected_at_prompt_pct,
  ROUND(COUNTIF(refusal = 'hook') / COUNT(*) * 100, 1) AS denied_by_hook_pct,
  ROUND(COUNTIF(refusal = 'rule') / COUNT(*) * 100, 1) AS denied_by_rule_pct,
  ROUND(COUNTIF(refusal = 'other') / COUNT(*) * 100, 1) AS refused_other_pct
FROM tools
GROUP BY tool
ORDER BY calls DESC, tool;

-- V6. Team lead: errors over time. Per day, each count beside its own
--     denominator: turns and those that ended in an error, model requests
--     (answered or failed) and the failed ones, tool calls and the failed
--     ones, and the refused tool calls (refused_tool_calls), split by where
--     the refusal came from (as in V5) into rejected_at_prompt,
--     denied_by_hook, denied_by_rule and refused_other, which add up to it.
WITH events AS (
  SELECT
    *,
    CASE
      WHEN JSON_VALUE(attributes, '$.permission_decision') = 'reject'
        AND JSON_VALUE(attributes, '$.permission_decision_source') IN ('user_reject', 'user_abort') THEN 'prompt'
      WHEN JSON_VALUE(attributes, '$.denied') = 'true'
        OR (JSON_VALUE(attributes, '$.permission_decision') = 'reject' AND JSON_VALUE(attributes, '$.permission_decision_source') = 'hook') THEN 'hook'
      WHEN JSON_VALUE(attributes, '$.permission_decision') = 'reject' AND JSON_VALUE(attributes, '$.permission_decision_source') = 'config' THEN 'rule'
      WHEN JSON_VALUE(attributes, '$.permission_decision') = 'reject' THEN 'other'
    END AS refusal
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  DATE(timestamp) AS day,
  COUNTIF(event_type IN ('INVOCATION_COMPLETED', 'INVOCATION_ERROR')) AS turns,
  COUNTIF(event_type = 'INVOCATION_ERROR') AS failed_turns,
  COUNTIF(event_type IN ('LLM_RESPONSE', 'LLM_ERROR')) AS model_requests,
  COUNTIF(event_type = 'LLM_ERROR') AS failed_model_requests,
  COUNTIF(event_type = 'TOOL_COMPLETED') AS tool_calls,
  COUNTIF(event_type = 'TOOL_COMPLETED' AND status = 'ERROR') AS failed_tool_calls,
  COUNTIF(event_type = 'TOOL_COMPLETED' AND refusal IS NOT NULL) AS refused_tool_calls,
  COUNTIF(event_type = 'TOOL_COMPLETED' AND refusal = 'prompt') AS rejected_at_prompt,
  COUNTIF(event_type = 'TOOL_COMPLETED' AND refusal = 'hook') AS denied_by_hook,
  COUNTIF(event_type = 'TOOL_COMPLETED' AND refusal = 'rule') AS denied_by_rule,
  COUNTIF(event_type = 'TOOL_COMPLETED' AND refusal = 'other') AS refused_other
FROM events
GROUP BY day
ORDER BY day DESC;

-- V7. Platform team: the mod's coding-agent sessions beside ADK agents.
--     Both write the agent_events schema, so one table, or a UNION over
--     two, holds them both. The settings-hook tracer's rows are left out:
--     it writes one response per turn and per subagent run, with their
--     usage summed, so its model requests would count turns. Per producer
--     and agent: sessions, model requests, tokens and the share of rows in
--     error. Normalized: rows with no writer are named for ADK, and an ADK
--     response's tokens are read from total_token_count when total_tokens
--     is missing. Other columns differ by producer and are left out.
--     An ADK model call counts once, as one row: the responses on its
--     request's own span (each streamed chunk, and its final response),
--     with the largest token count among them, in error if any was. With
--     progressive streaming off, ADK also writes later finals under the
--     agent's span; they repeat their call's usage, and when several calls
--     share that span the rows cannot always tell which call wrote one. So
--     a later final counts in no call's requests or tokens, and as a row
--     only when it reports an error; later_finals says how many were left
--     out. A call whose own rows recorded no usage adds no tokens and is in
--     model_requests_without_usage. Requests are looked up from a day
--     before the window, so a call that began just before it is still
--     matched, and only rows inside the window are counted. A response
--     with no request found is counted by its span, or alone, and
--     model_requests_unmatched says how many requests were counted so.
--     When an agent's only rows in the window are later finals, it has no
--     rows to take a share of: its error_row_pct is NULL.
WITH events AS (
  SELECT
    *,
    timestamp >= @start AS in_window,
    JSON_VALUE(attributes, '$.writer.plugin') IS NULL AS adk
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN TIMESTAMP_SUB(@start, INTERVAL 1 DAY) AND @end
    AND IFNULL(JSON_VALUE(attributes, '$.writer.plugin'), 'bigquery-agent-analytics-mod') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
),
spans AS (
  SELECT
    *,
    span_id IS NOT NULL AND LOGICAL_OR(event_type = 'LLM_REQUEST') OVER (PARTITION BY adk, session_id, span_id) AS on_request_span,
    JSON_VALUE(attributes, '$.cache_type') IS NOT NULL OR JSON_VALUE(attributes, '$.finish_reason') IS NOT NULL AS final
  FROM events
),
closes AS (
  SELECT
    *,
    COUNTIF(event_type = 'LLM_RESPONSE' AND on_request_span AND final) OVER (
      PARTITION BY adk, session_id, IF(on_request_span, parent_span_id, span_id)
      ORDER BY timestamp, IF(on_request_span, 0, 1)
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS closed_so_far
  FROM spans
),
tagged AS (
  SELECT
    IFNULL(JSON_VALUE(attributes, '$.writer.plugin'), 'adk (no writer)') AS producer,
    agent,
    session_id,
    event_type,
    in_window,
    status = 'ERROR' AS failed,
    COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.total_tokens') AS INT64),
      SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.total_token_count') AS INT64)) AS tokens,
    adk AND event_type = 'LLM_RESPONSE' AND NOT on_request_span AND span_id IS NOT NULL AND closed_so_far > 0 AS later_final,
    IF(adk AND event_type = 'LLM_RESPONSE',
      CASE
        WHEN on_request_span THEN CONCAT('request ', span_id)
        WHEN span_id IS NOT NULL AND closed_so_far > 0 THEN NULL
        WHEN span_id IS NOT NULL THEN CONCAT('span ', span_id)
        ELSE GENERATE_UUID()
      END,
      NULL) AS call
  FROM closes
),
counted AS (
  SELECT producer, agent, session_id, IF(later_final, 'LLM_RESPONSE, later final', event_type) AS event_type, failed,
    IF(later_final, NULL, tokens) AS tokens, FALSE AS unmatched, later_final, NOT later_final OR failed AS a_row
  FROM tagged
  WHERE in_window AND call IS NULL
  UNION ALL
  SELECT producer, agent, session_id, 'LLM_RESPONSE', LOGICAL_OR(failed), MAX(tokens), NOT STARTS_WITH(call, 'request '), FALSE, TRUE
  FROM tagged
  WHERE in_window AND call IS NOT NULL
  GROUP BY producer, agent, session_id, call
)
SELECT
  producer,
  agent,
  COUNT(DISTINCT session_id) AS sessions,
  COUNTIF(event_type = 'LLM_RESPONSE') AS model_requests,
  COUNTIF(unmatched) AS model_requests_unmatched,
  COUNTIF(event_type = 'LLM_RESPONSE' AND tokens IS NULL) AS model_requests_without_usage,
  COUNTIF(later_final) AS later_finals,
  SUM(IF(event_type = 'LLM_RESPONSE', tokens, 0)) AS tokens,
  ROUND(SAFE_DIVIDE(COUNTIF(failed AND a_row), COUNTIF(a_row)) * 100, 1) AS error_row_pct
FROM counted
GROUP BY producer, agent
ORDER BY producer, agent;
