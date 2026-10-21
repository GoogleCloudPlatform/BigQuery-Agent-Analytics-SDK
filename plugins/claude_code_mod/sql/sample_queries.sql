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

-- Sample queries over rows written by the bigquery-agent-analytics-mod.
--
-- Placeholders follow the repo's analysis skill: {PROJECT}, {DATASET},
-- {TABLE}, and the query parameters @start, @end and @trace_id, which
-- bigquery-agent-analytics-skill/scripts/run_bq.py fills and binds. Pass
-- one query at a time:
--
--   python bigquery-agent-analytics-skill/scripts/run_bq.py --dry-run \
--     --start 2026-10-01 --end 2026-10-13 "<one query below>"
--
-- Every query reads only this mod's rows (attributes.writer.plugin: a table
-- can also hold the settings-hook tracer's or ADK agents' rows) and keeps
-- one row per event_id: a row resent after an ambiguous timeout or by a
-- backfill carries the same event_id (the ADK docs use the same QUALIFY for
-- its dedup). tests/queries.live.py runs each one on a synthetic fixture.

-- 1. Rows this mod wrote, deduplicated.
SELECT *
FROM `{PROJECT}.{DATASET}.{TABLE}`
WHERE timestamp BETWEEN @start AND @end
  AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1;

-- 2. Tokens per session and model, with the prompt-cache share.
WITH responses AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE event_type = 'LLM_RESPONSE'
    AND timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  session_id,
  JSON_VALUE(attributes, '$.model') AS model,
  COUNT(*) AS model_requests,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.input_tokens') AS INT64)) AS input_tokens,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_read_input_tokens') AS INT64)) AS cache_read_tokens,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_creation_input_tokens') AS INT64)) AS cache_write_tokens,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.output_tokens') AS INT64)) AS output_tokens,
  ROUND(SAFE_DIVIDE(
    SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_read_input_tokens') AS INT64)),
    SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_tokens') AS INT64))), 3) AS cache_read_share
FROM responses
GROUP BY session_id, model
ORDER BY output_tokens DESC;

-- 3. Model request latency by model: total and time to first token.
WITH responses AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE event_type = 'LLM_RESPONSE'
    AND timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  JSON_VALUE(attributes, '$.model') AS model,
  COUNT(*) AS requests,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100)[OFFSET(50)] AS p50_total_ms,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100)[OFFSET(95)] AS p95_total_ms,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.time_to_first_token_ms') AS INT64), 100)[OFFSET(50)] AS p50_ttft_ms
FROM responses
GROUP BY model
ORDER BY requests DESC;

-- 4. Tool failure rate and latency. A permission prompt's wait is inside
--    total_ms, so the share of calls that prompted is listed beside it.
--    Each refused call is counted once, by where the refusal came from: the
--    person at the prompt, a hook, a permission rule
--    (permission_decision_source), or another source (refused_other: one
--    the host did not give, which the mod records as unknown, or one not
--    named here); the four add up to refused. A call that a rule blocks
--    before the host records it leaves no row.
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
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_VALUE(content, '$.tool_origin') AS tool_origin,
  COUNT(*) AS calls,
  COUNTIF(status = 'ERROR') AS failures,
  ROUND(COUNTIF(status = 'ERROR') / COUNT(*) * 100, 2) AS fail_rate_pct,
  COUNTIF(refusal IS NOT NULL) AS refused,
  COUNTIF(refusal = 'prompt') AS rejected_at_prompt,
  COUNTIF(refusal = 'hook') AS denied_by_hook,
  COUNTIF(refusal = 'rule') AS denied_by_rule,
  COUNTIF(refusal = 'other') AS refused_other,
  COUNTIF(JSON_VALUE(attributes, '$.permission_requested') = 'true') AS asked_permission,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100)[OFFSET(95)] AS p95_ms
FROM tools
GROUP BY tool_name, tool_origin
ORDER BY failures DESC, calls DESC;

-- 5. One line per turn: the prompt, how long it took, how many model
--    requests and tool calls it made, its tokens and how it ended.
WITH events AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  session_id,
  invocation_id,
  MIN(timestamp) AS started_at,
  STRING_AGG(IF(event_type = 'USER_MESSAGE_RECEIVED', JSON_VALUE(content, '$.text_summary'), NULL), ' / ' ORDER BY timestamp) AS prompt,
  MAX(IF(event_type IN ('INVOCATION_COMPLETED', 'INVOCATION_ERROR'), CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), NULL)) AS turn_ms,
  COUNTIF(event_type = 'LLM_RESPONSE') AS model_requests,
  COUNTIF(event_type = 'TOOL_COMPLETED') AS tool_calls,
  COUNTIF(event_type = 'AGENT_STARTING') AS subagents,
  MAX(IF(event_type IN ('INVOCATION_COMPLETED', 'INVOCATION_ERROR'), CAST(JSON_VALUE(attributes, '$.usage_metadata.total_tokens') AS INT64), NULL)) AS turn_tokens,
  MAX(IF(event_type IN ('INVOCATION_COMPLETED', 'INVOCATION_ERROR'), JSON_VALUE(attributes, '$.finish_reason'), NULL)) AS finish_reason,
  COUNTIF(status = 'ERROR') AS error_rows
FROM events
WHERE invocation_id IS NOT NULL
GROUP BY session_id, invocation_id
ORDER BY started_at DESC;

-- 6. The span tree of one turn (pass --trace-id to run_bq.py): each row
--    with its parent, so subagents and the tools each request asked for
--    nest under it.
SELECT
  child.timestamp,
  child.event_type,
  child.agent,
  child.span_id,
  child.parent_span_id,
  parent.event_type AS parent_event_type,
  COALESCE(JSON_VALUE(child.content, '$.tool'), JSON_VALUE(child.attributes, '$.model')) AS detail,
  CAST(JSON_VALUE(child.latency_ms, '$.total_ms') AS INT64) AS total_ms,
  child.status
FROM `{PROJECT}.{DATASET}.{TABLE}` AS child
LEFT JOIN (
  SELECT span_id, trace_id, ANY_VALUE(event_type) AS event_type
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN @start AND @end
    AND trace_id = @trace_id
    AND event_type IN ('INVOCATION_STARTING', 'LLM_REQUEST', 'AGENT_STARTING')
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  GROUP BY span_id, trace_id
) AS parent
  ON child.parent_span_id = parent.span_id AND child.trace_id = parent.trace_id
WHERE child.timestamp BETWEEN @start AND @end
  AND child.trace_id = @trace_id
  AND JSON_VALUE(child.attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
QUALIFY child.event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY child.event_id ORDER BY child.timestamp) = 1
ORDER BY child.timestamp;

-- 7. Subagents: how often each type ran, for how long, and what it cost.
WITH agents AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE event_type IN ('AGENT_COMPLETED', 'AGENT_ERROR')
    AND timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  agent AS subagent_type,
  COUNT(*) AS runs,
  COUNTIF(status = 'ERROR') AS failed_runs,
  ROUND(AVG(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64)) / 1000, 1) AS avg_seconds,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.total_tokens') AS INT64)) AS total_tokens
FROM agents
GROUP BY subagent_type
ORDER BY runs DESC;

-- 8. Is the mod healthy? Rows per day and version, truncation and errors.
WITH events AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  DATE(timestamp) AS day,
  JSON_VALUE(attributes, '$.writer.label') AS writer,
  COUNT(DISTINCT session_id) AS sessions,
  COUNT(*) AS rows_written,
  COUNTIF(is_truncated) AS truncated_rows,
  COUNTIF(status = 'ERROR') AS error_rows
FROM events
GROUP BY day, writer
ORDER BY day DESC;
