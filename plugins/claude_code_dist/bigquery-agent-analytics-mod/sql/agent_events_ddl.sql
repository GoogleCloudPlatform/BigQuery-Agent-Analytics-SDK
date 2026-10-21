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

-- BigQuery Agent Analytics `agent_events` table, schema version 2 (with
-- event_id). Same columns, partitioning and clustering as the manual DDL in
-- the ADK BigQuery Agent Analytics docs
-- (https://adk.dev/integrations/bigquery-agent-analytics/), so ADK agents,
-- the settings-hook tracing plugin and this mod can share one table.
--
-- Replace {PROJECT}, {DATASET} and {TABLE} first (the skill's
-- scripts/run_bq.py fills them from GCP_PROJECT_ID, BQ_DATASET and
-- BQ_TABLE), then run it with `bq query --use_legacy_sql=false` or in the
-- console.
--
-- The mod creates this table itself on first write when it is missing
-- (BQAA_AUTO_CREATE_TABLE, on by default); the dataset must exist.

CREATE TABLE IF NOT EXISTS `{PROJECT}.{DATASET}.{TABLE}` (
  timestamp TIMESTAMP NOT NULL OPTIONS (description = "The UTC time at which the event was logged."),
  event_id STRING OPTIONS (description = "Unique ID assigned before enqueue; also the insertAll insertId. The dedup key."),
  event_type STRING OPTIONS (description = "The type of event (e.g. 'LLM_REQUEST', 'TOOL_COMPLETED')."),
  agent STRING OPTIONS (description = "The agent, or subagent type, associated with the event."),
  session_id STRING OPTIONS (description = "The coding-agent session id."),
  invocation_id STRING OPTIONS (description = "The turn (invocation) id within the session."),
  user_id STRING OPTIONS (description = "The user associated with the session."),
  trace_id STRING OPTIONS (description = "32-char hex trace id, one per main-loop turn."),
  span_id STRING OPTIONS (description = "16-char hex span id for this operation."),
  parent_span_id STRING OPTIONS (description = "16-char hex span id of the immediate caller."),
  content JSON OPTIONS (description = "The event-specific payload."),
  content_parts ARRAY<STRUCT<
    mime_type STRING,
    uri STRING,
    object_ref STRUCT<
      uri STRING,
      version STRING,
      authorizer STRING,
      details JSON
    >,
    text STRING,
    part_index INT64,
    part_attributes STRING,
    storage_mode STRING
  >> OPTIONS (description = "Detailed content parts for multi-modal data. The mod writes none."),
  attributes JSON OPTIONS (description = "Metadata: model, usage_metadata, writer, session_metadata."),
  latency_ms JSON OPTIONS (description = "Latency measurements (total_ms, time_to_first_token_ms)."),
  status STRING OPTIONS (description = "'OK' or 'ERROR'."),
  error_message STRING OPTIONS (description = "Sanitized error or termination diagnostic."),
  is_truncated BOOLEAN OPTIONS (description = "True when content or metadata was truncated.")
)
PARTITION BY DATE(timestamp)
CLUSTER BY event_type, agent, user_id;

-- A table created by the settings-hook tracing plugin (schema version 1) has
-- no event_id column. The mod's rows still land there (insertAll runs with
-- ignoreUnknownValues), but BigQuery drops event_id; add the column to keep
-- the dedup key:
ALTER TABLE `{PROJECT}.{DATASET}.{TABLE}` ADD COLUMN IF NOT EXISTS event_id STRING;
