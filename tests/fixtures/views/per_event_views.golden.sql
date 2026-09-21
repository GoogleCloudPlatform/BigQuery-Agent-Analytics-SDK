-- USER_MESSAGE_RECEIVED
CREATE OR REPLACE VIEW `test-project.analytics.adk_user_messages` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated
FROM `test-project.analytics.agent_events`
WHERE event_type = 'USER_MESSAGE_RECEIVED'

-- LLM_REQUEST
CREATE OR REPLACE VIEW `test-project.analytics.adk_llm_requests` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(attributes, '$.model') AS model,
  content AS request_content,
  JSON_QUERY(attributes, '$.llm_config') AS llm_config,
  JSON_QUERY(attributes, '$.tools') AS tools
FROM `test-project.analytics.agent_events`
WHERE event_type = 'LLM_REQUEST'

-- LLM_RESPONSE
CREATE OR REPLACE VIEW `test-project.analytics.adk_llm_responses` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_QUERY(content, '$.response') AS response,
  CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64) AS usage_prompt_tokens,
  CAST(JSON_VALUE(content, '$.usage.completion') AS INT64) AS usage_completion_tokens,
  CAST(JSON_VALUE(content, '$.usage.total') AS INT64) AS usage_total_tokens,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms,
  CAST(JSON_VALUE(
    latency_ms, '$.time_to_first_token_ms'
  ) AS INT64) AS ttft_ms,
  JSON_VALUE(attributes, '$.model_version') AS model_version,
  JSON_QUERY(attributes, '$.usage_metadata') AS usage_metadata
FROM `test-project.analytics.agent_events`
WHERE event_type = 'LLM_RESPONSE'

-- LLM_ERROR
CREATE OR REPLACE VIEW `test-project.analytics.adk_llm_errors` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms
FROM `test-project.analytics.agent_events`
WHERE event_type = 'LLM_ERROR'

-- TOOL_STARTING
CREATE OR REPLACE VIEW `test-project.analytics.adk_tool_starts` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args,
  JSON_VALUE(content, '$.tool_origin') AS tool_origin
FROM `test-project.analytics.agent_events`
WHERE event_type = 'TOOL_STARTING'

-- TOOL_COMPLETED
CREATE OR REPLACE VIEW `test-project.analytics.adk_tool_completions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result,
  JSON_VALUE(content, '$.tool_origin') AS tool_origin,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms,
  -- ADK 2.0 long-running pair keys (#199/#293). Null on ordinary
  -- (non-long-running) completions; set on the resume row that pairs
  -- with a TOOL_PAUSED. `pause_orphan` is reserved for the pause
  -- registry (#206) — the producer emits it null until that lands.
  JSON_VALUE(attributes, '$.adk.function_call_id') AS function_call_id,
  JSON_VALUE(attributes, '$.adk.pause_kind') AS pause_kind,
  CAST(JSON_VALUE(attributes, '$.adk.pause_orphan') AS BOOL) AS pause_orphan
FROM `test-project.analytics.agent_events`
WHERE event_type = 'TOOL_COMPLETED'

-- TOOL_ERROR
CREATE OR REPLACE VIEW `test-project.analytics.adk_tool_errors` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args,
  JSON_VALUE(content, '$.tool_origin') AS tool_origin,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms
FROM `test-project.analytics.agent_events`
WHERE event_type = 'TOOL_ERROR'

-- AGENT_STARTING
CREATE OR REPLACE VIEW `test-project.analytics.adk_agent_starts` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.text_summary') AS agent_instruction
FROM `test-project.analytics.agent_events`
WHERE event_type = 'AGENT_STARTING'

-- AGENT_COMPLETED
CREATE OR REPLACE VIEW `test-project.analytics.adk_agent_completions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms
FROM `test-project.analytics.agent_events`
WHERE event_type = 'AGENT_COMPLETED'

-- INVOCATION_STARTING
CREATE OR REPLACE VIEW `test-project.analytics.adk_invocation_starts` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated
FROM `test-project.analytics.agent_events`
WHERE event_type = 'INVOCATION_STARTING'

-- INVOCATION_COMPLETED
CREATE OR REPLACE VIEW `test-project.analytics.adk_invocation_completions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated
FROM `test-project.analytics.agent_events`
WHERE event_type = 'INVOCATION_COMPLETED'

-- STATE_DELTA
CREATE OR REPLACE VIEW `test-project.analytics.adk_state_deltas` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_QUERY(attributes, '$.state_delta') AS state_delta
FROM `test-project.analytics.agent_events`
WHERE event_type = 'STATE_DELTA'

-- HITL_CREDENTIAL_REQUEST
CREATE OR REPLACE VIEW `test-project.analytics.adk_hitl_credential_requests` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args
FROM `test-project.analytics.agent_events`
WHERE event_type = 'HITL_CREDENTIAL_REQUEST'

-- HITL_CONFIRMATION_REQUEST
CREATE OR REPLACE VIEW `test-project.analytics.adk_hitl_confirmation_requests` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args
FROM `test-project.analytics.agent_events`
WHERE event_type = 'HITL_CONFIRMATION_REQUEST'

-- HITL_INPUT_REQUEST
CREATE OR REPLACE VIEW `test-project.analytics.adk_hitl_input_requests` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args
FROM `test-project.analytics.agent_events`
WHERE event_type = 'HITL_INPUT_REQUEST'

-- HITL_CREDENTIAL_REQUEST_COMPLETED
CREATE OR REPLACE VIEW `test-project.analytics.adk_hitl_credential_completions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result
FROM `test-project.analytics.agent_events`
WHERE event_type = 'HITL_CREDENTIAL_REQUEST_COMPLETED'

-- HITL_CONFIRMATION_REQUEST_COMPLETED
CREATE OR REPLACE VIEW `test-project.analytics.adk_hitl_confirmation_completions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result
FROM `test-project.analytics.agent_events`
WHERE event_type = 'HITL_CONFIRMATION_REQUEST_COMPLETED'

-- HITL_INPUT_REQUEST_COMPLETED
CREATE OR REPLACE VIEW `test-project.analytics.adk_hitl_input_completions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result
FROM `test-project.analytics.agent_events`
WHERE event_type = 'HITL_INPUT_REQUEST_COMPLETED'

-- A2A_INTERACTION
CREATE OR REPLACE VIEW `test-project.analytics.adk_a2a_interactions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(
    attributes, '$.a2a_metadata."a2a:task_id"'
  ) AS a2a_task_id,
  JSON_VALUE(
    attributes, '$.a2a_metadata."a2a:context_id"'
  ) AS a2a_context_id,
  JSON_QUERY(
    attributes, '$.a2a_metadata."a2a:request"'
  ) AS a2a_request,
  JSON_QUERY(
    attributes, '$.a2a_metadata."a2a:response"'
  ) AS a2a_response,
  COALESCE(
    JSON_VALUE(content, '$.metadata.adk_session_id'),
    JSON_VALUE(
      attributes,
      '$.a2a_metadata."a2a:response".metadata.adk_session_id'
    )
  ) AS receiver_session_id
FROM `test-project.analytics.agent_events`
WHERE event_type = 'A2A_INTERACTION'

-- AGENT_TRANSFER
CREATE OR REPLACE VIEW `test-project.analytics.adk_agent_transfers` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.from_agent') AS from_agent,
  JSON_VALUE(content, '$.to_agent') AS to_agent
FROM `test-project.analytics.agent_events`
WHERE event_type = 'AGENT_TRANSFER'

-- EVENT_COMPACTION
CREATE OR REPLACE VIEW `test-project.analytics.adk_event_compactions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  TIMESTAMP_MICROS(CAST(
    CAST(JSON_VALUE(content, '$.start_timestamp') AS FLOAT64) * 1000000 AS INT64
  )) AS start_timestamp,
  TIMESTAMP_MICROS(CAST(
    CAST(JSON_VALUE(content, '$.end_timestamp') AS FLOAT64) * 1000000 AS INT64
  )) AS end_timestamp,
  JSON_VALUE(content, '$.compacted_content') AS compacted_content
FROM `test-project.analytics.agent_events`
WHERE event_type = 'EVENT_COMPACTION'

-- AGENT_STATE_CHECKPOINT
CREATE OR REPLACE VIEW `test-project.analytics.adk_agent_state_checkpoints` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_QUERY(content, '$.agent_state') AS agent_state,
  JSON_TYPE(JSON_QUERY(content, '$.agent_state')) AS agent_state_type,
  CAST(JSON_VALUE(content, '$.end_of_agent') AS BOOL) AS end_of_agent
FROM `test-project.analytics.agent_events`
WHERE event_type = 'AGENT_STATE_CHECKPOINT'

-- TOOL_PAUSED
CREATE OR REPLACE VIEW `test-project.analytics.adk_tool_pauses` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args,
  JSON_VALUE(attributes, '$.adk.function_call_id') AS function_call_id,
  JSON_VALUE(attributes, '$.adk.pause_kind') AS pause_kind
FROM `test-project.analytics.agent_events`
WHERE event_type = 'TOOL_PAUSED'

-- WORKFLOW_NODE_STARTING
CREATE OR REPLACE VIEW `test-project.analytics.adk_workflow_node_starts` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated
FROM `test-project.analytics.agent_events`
WHERE event_type = 'WORKFLOW_NODE_STARTING'

-- WORKFLOW_NODE_COMPLETED
CREATE OR REPLACE VIEW `test-project.analytics.adk_workflow_node_completions` AS
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated
FROM `test-project.analytics.agent_events`
WHERE event_type = 'WORKFLOW_NODE_COMPLETED'

