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

-- Sessions, model requests and tokens per day and user, from the
-- LLM_RESPONSE rows this mod wrote (attributes.writer.plugin), one row per
-- event_id. Sessions, requests and tokens all count the same rows. `user_id`
-- is BQAA_USER_ID or the login name, or with the userIdFromCredentials
-- option the verified account of the token. setup.sh fills in {PROJECT},
-- {DATASET}, {TABLE} and {VIEWS_DATASET}.
--
-- Filter on `day`: duplicates are matched within one timestamp, so a filter
-- on the date still reaches the table's day partitions.
CREATE OR REPLACE VIEW `{PROJECT}.{VIEWS_DATASET}.daily_usage_by_user`
OPTIONS (description = 'Sessions, model requests and tokens per day and user, from the LLM_RESPONSE rows the bigquery-agent-analytics-mod wrote, one per event_id.')
AS
WITH responses AS (
  SELECT
    timestamp,
    user_id,
    session_id,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.input_tokens') AS INT64) AS input_tokens,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_read_input_tokens') AS INT64) AS cache_read_input_tokens,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_creation_input_tokens') AS INT64) AS cache_creation_input_tokens,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.output_tokens') AS INT64) AS output_tokens,
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.total_tokens') AS INT64) AS total_tokens
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE event_type = 'LLM_RESPONSE'
    -- This mod's rows only. The table can be shared (ADK agents, the
    -- settings-hook plugin), and other writers can name their token counts
    -- differently, so counting their requests here would not match the
    -- token totals.
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  -- A row sent twice (after an ambiguous timeout, or by a replay) has the
  -- same event_id and timestamp: keep one.
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY timestamp, event_id) = 1
)
SELECT
  DATE(timestamp) AS day,
  COALESCE(user_id, '(unknown)') AS user_id,
  COUNT(DISTINCT session_id) AS sessions,
  COUNT(*) AS model_requests,
  SUM(input_tokens) AS input_tokens,
  SUM(cache_read_input_tokens) AS cache_read_input_tokens,
  SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
  SUM(output_tokens) AS output_tokens,
  SUM(total_tokens) AS total_tokens
FROM responses
GROUP BY day, user_id;
