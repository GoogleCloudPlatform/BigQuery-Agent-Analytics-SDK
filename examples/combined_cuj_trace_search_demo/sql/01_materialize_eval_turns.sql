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
--
-- Materialize turn-level rows (`eval_turns`) from raw ADK `agent_events` (v2).
-- Replicates the 22 output columns of ADK's `eval_turn_query_v1.sql` and
-- `eval_view_ctes_v1.sql` (`has_error`, `root_agent`, `has_tool_loop`,
-- `eval_ready`, and `trace` JSON; intentionally no `status` or `root_agent_name`
-- output columns), plus three derived demo columns: `error_summary`,
-- `raw_trace_bytes`, and `_ai_index_trajectory` (initialized to NULL and
-- populated deterministically before autonomous embedding generation).
--
-- Substitution contract: replace the PROJECT_ID, DATASET_ID, EVENTS_TABLE,
-- and EVAL_TURNS_TABLE placeholders with validated BigQuery identifiers
-- containing no backticks, backslashes, or control characters. No other
-- brace placeholders exist in this template, so Python str.format and literal
-- token replacement produce identical SQL.

CREATE OR REPLACE TABLE `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
PARTITION BY DATE(turn_start_timestamp)
CLUSTER BY root_agent, has_error, session_id
OPTIONS(labels=[('bqaa_demo', 'combined_cuj_trace_search')]) AS
WITH
  RawEvents AS (
    SELECT
      timestamp,
      event_id,
      event_type,
      agent,
      session_id,
      invocation_id,
      user_id,
      content,
      content_parts,
      attributes,
      latency_ms,
      status,
      error_message,
      is_truncated
    FROM
      `{PROJECT_ID}.{DATASET_ID}.{EVENTS_TABLE}`
    WHERE
      session_id IS NOT NULL
      AND invocation_id IS NOT NULL
  ),
  SessionEvents AS (
    SELECT
      *
    FROM
      RawEvents
    WHERE
      JSON_VALUE(attributes, '$.adk.parent_session_id') IS NULL
  ),
  TypedEvents AS (
    SELECT
      *,
      COALESCE(
        agent, JSON_VALUE(attributes, '$.root_agent_name'), 'root_agent'
      ) AS author,
      JSON_VALUE(content, '$.tool') AS tool_name,
      COALESCE(JSON_VALUE(content, '$.tool'), '') IN (
        'transfer_to_agent',
        'adk_request_credential',
        'adk_request_confirmation',
        'adk_request_input'
      ) AS is_framework_tool,
      TO_JSON_STRING(JSON_QUERY(content, '$.args')) AS tool_args_json,
      CASE
        WHEN event_type != 'USER_MESSAGE_RECEIVED' THEN NULL
        WHEN COALESCE(ARRAY_LENGTH(content_parts), 0) > 0 THEN (
          SELECT
            STRING_AGG(p.text, '\n' ORDER BY p.part_index)
          FROM
            UNNEST(content_parts) AS p
          WHERE
            p.mime_type = 'text/plain'
            AND COALESCE(p.storage_mode, 'INLINE') = 'INLINE'
            AND COALESCE(
              p.part_attributes, TO_JSON_STRING(JSON_OBJECT())
            ) = TO_JSON_STRING(JSON_OBJECT())
            AND p.text IS NOT NULL
            AND p.text NOT IN ('', '[BINARY DATA]', '[UPLOAD FAILED]')
        )
        WHEN NOT REGEXP_CONTAINS(
          COALESCE(
            JSON_VALUE(content, '$.text_summary'),
            JSON_VALUE(content, '$'),
            ''
          ),
          r'^Function response: '
        ) THEN NULLIF(
          COALESCE(
            JSON_VALUE(content, '$.text_summary'), JSON_VALUE(content, '$')
          ),
          ''
        )
        ELSE NULL
      END AS message_text,
      (
        event_type = 'USER_MESSAGE_RECEIVED'
        AND (
          REGEXP_CONTAINS(
            COALESCE(
              JSON_VALUE(content, '$.text_summary'),
              JSON_VALUE(content, '$'),
              ''
            ),
            r'^Function response: '
          )
          OR EXISTS(
            SELECT 1
            FROM UNNEST(content_parts) AS p
            WHERE
              JSON_QUERY(p.part_attributes, '$.function_response')
              IS NOT NULL
          )
        )
      ) AS is_function_response,
      IF(
        event_type = 'AGENT_RESPONSE',
        COALESCE(
          IF(
            JSON_TYPE(JSON_QUERY(content, '$.text_summary')) = 'string'
              AND TRIM(JSON_VALUE(content, '$.text_summary')) != '',
            JSON_VALUE(content, '$.text_summary'),
            NULL
          ),
          IF(
            TRIM(JSON_VALUE(content, '$.response')) != '',
            JSON_VALUE(content, '$.response'),
            NULL
          ),
          IF(
            JSON_TYPE(JSON_QUERY(content, '$.response'))
              IN ('object', 'array'),
            TO_JSON_STRING(JSON_QUERY(content, '$.response')),
            NULL
          ),
          IF(
            JSON_TYPE(content) = 'string'
              AND TRIM(JSON_VALUE(content, '$')) != ''
              AND JSON_VALUE(content, '$') NOT IN (
                '[FORMATTER_FAILED]',
                '[FORMATTING FAILED]',
                '[REDACTED]',
                '[CONTENT_PARSE_FAILED]',
                '[UNPARSEABLE_JSON_BLOB]',
                '[UNSUPPORTED_OBJECT]',
                '[REDACTED_SENSITIVE_TEXT]'
              ),
            JSON_VALUE(content, '$'),
            NULL
          )
        ),
        NULL
      ) AS formatted_response,
      (
        event_type = 'LLM_RESPONSE'
        AND (
          JSON_VALUE(attributes, '$.finish_reason') IS NOT NULL
          OR JSON_VALUE(attributes, '$.cache_type') IS NOT NULL
        )
      ) AS is_final_llm_response,
      SAFE_CAST(JSON_VALUE(content, '$.usage.total') AS INT64) AS usage_total,
      SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64)
        AS usage_prompt,
      SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64)
        AS usage_completion,
      SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms,
      COALESCE(
        NULLIF(TRIM(error_message), ''),
        IF(
          event_type = 'TOOL_COMPLETED',
          COALESCE(
            NULLIF(TRIM(JSON_VALUE(content, '$.result.error_code')), ''),
            NULLIF(TRIM(JSON_VALUE(content, '$.result.error')), '')
          ),
          NULL
        )
      ) AS error_token
    FROM
      SessionEvents
  ),
  ResponseEvents AS (
    SELECT
      *,
      (
        event_type = 'TOOL_STARTING'
        AND NULLIF(tool_name, '') IS NOT NULL
        AND NOT is_framework_tool
      ) AS is_tool_call,
      COUNTIF(
        event_type IN (
          'HITL_CREDENTIAL_REQUEST',
          'HITL_CONFIRMATION_REQUEST',
          'HITL_INPUT_REQUEST'
        )
      ) OVER (
        PARTITION BY session_id, invocation_id
        ORDER BY timestamp, event_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) AS hitl_request_count,
      CASE
        WHEN
          REGEXP_CONTAINS(formatted_response, r"(?s)^text: '.*'$")
          AND NOT REGEXP_CONTAINS(
            formatted_response,
            r"' [|] (?:call: |resp: |Executable code |Code execution result )"
          )
          AND NOT REGEXP_CONTAINS(
            formatted_response, r"' [|] other(?: [|] |$)"
          )
          THEN IF(
            TRIM(
              REGEXP_REPLACE(
                REGEXP_REPLACE(formatted_response, r"^text: '|'$", ''),
                r"' [|] text: '",
                CHR(10)
              )
            ) != ''
              AND REGEXP_REPLACE(
                REGEXP_REPLACE(formatted_response, r"^text: '|'$", ''),
                r"' [|] text: '",
                CHR(10)
              ) NOT IN (
                '[FORMATTER_FAILED]',
                '[FORMATTING FAILED]',
                '[REDACTED]',
                '[CONTENT_PARSE_FAILED]',
                '[UNPARSEABLE_JSON_BLOB]',
                '[UNSUPPORTED_OBJECT]',
                '[REDACTED_SENSITIVE_TEXT]'
              ),
            REGEXP_REPLACE(
              REGEXP_REPLACE(formatted_response, r"^text: '|'$", ''),
              r"' [|] text: '",
              CHR(10)
            ),
            NULL
          )
        WHEN
          NOT REGEXP_CONTAINS(
            formatted_response,
            r'^(?:call: |resp: |Executable code |Code execution result )'
          )
          AND NOT REGEXP_CONTAINS(
            formatted_response, r"^(?:None$|text: '|other(?: [|] |$))"
          )
          AND TRIM(formatted_response) != ''
          AND formatted_response NOT IN (
            '[FORMATTER_FAILED]',
            '[FORMATTING FAILED]',
            '[REDACTED]',
            '[CONTENT_PARSE_FAILED]',
            '[UNPARSEABLE_JSON_BLOB]',
            '[UNSUPPORTED_OBJECT]',
            '[REDACTED_SENSITIVE_TEXT]'
          )
          THEN formatted_response
        ELSE NULL
      END AS response_text
    FROM
      TypedEvents
  ),
  AnnotatedEvents AS (
    SELECT
      *,
      LOGICAL_OR(is_final_llm_response) OVER (
        PARTITION BY session_id, invocation_id
      ) AS turn_has_final_llm_response,
      ROW_NUMBER() OVER (
        PARTITION BY session_id, invocation_id, is_tool_call
        ORDER BY timestamp, event_id
      ) - ROW_NUMBER() OVER (
        PARTITION BY
          session_id,
          invocation_id,
          is_tool_call,
          tool_name,
          tool_args_json,
          hitl_request_count
        ORDER BY timestamp, event_id
      ) AS tool_call_island
    FROM
      ResponseEvents
  ),
  TraceEvents AS (
    SELECT
      session_id,
      invocation_id,
      timestamp,
      event_id,
      event_type,
      user_id,
      agent,
      JSON_VALUE(attributes, '$.root_agent_name') AS root_agent_name,
      status,
      error_message,
      error_token,
      is_truncated,
      message_text,
      response_text,
      is_function_response,
      is_tool_call,
      is_framework_tool,
      IF(is_tool_call, tool_name, NULL) AS invoked_tool_name,
      IF(
        is_tool_call,
        COUNT(*) OVER (
          PARTITION BY
            session_id,
            invocation_id,
            is_tool_call,
            tool_name,
            tool_args_json,
            tool_call_island
        ),
        NULL
      ) AS tool_call_run_length,
      (
        event_type = 'LLM_RESPONSE'
        AND (is_final_llm_response OR NOT turn_has_final_llm_response)
      ) AS is_usage_row,
      usage_total,
      usage_prompt,
      usage_completion,
      total_ms,
      CASE
        WHEN event_type = 'USER_MESSAGE_RECEIVED' THEN 1
        WHEN
          event_type IN (
            'TOOL_STARTING',
            'HITL_CREDENTIAL_REQUEST',
            'HITL_CONFIRMATION_REQUEST',
            'HITL_INPUT_REQUEST'
          )
          THEN 2
        WHEN event_type = 'AGENT_TRANSFER' THEN 3
        WHEN
          event_type IN (
            'TOOL_COMPLETED',
            'TOOL_ERROR',
            'HITL_CREDENTIAL_REQUEST_COMPLETED',
            'HITL_CONFIRMATION_REQUEST_COMPLETED',
            'HITL_INPUT_REQUEST_COMPLETED'
          )
          THEN 4
        ELSE 5
      END AS event_seq,
      CASE event_type
        WHEN 'USER_MESSAGE_RECEIVED' THEN 'user'
        WHEN 'AGENT_TRANSFER'
          THEN COALESCE(JSON_VALUE(content, '$.from_agent'), author)
        ELSE author
      END AS event_author,
      CASE
        WHEN event_type = 'USER_MESSAGE_RECEIVED' AND message_text IS NOT NULL
          THEN JSON_OBJECT(
            'role', 'user',
            'parts', JSON_ARRAY(JSON_OBJECT('text', message_text))
          )
        WHEN
          is_tool_call
          OR event_type IN (
            'HITL_CREDENTIAL_REQUEST',
            'HITL_CONFIRMATION_REQUEST',
            'HITL_INPUT_REQUEST'
          )
          THEN JSON_OBJECT(
            'role', 'model',
            'parts', JSON_ARRAY(
              JSON_OBJECT(
                'function_call', JSON_OBJECT(
                  'name', COALESCE(tool_name, ''),
                  'args', IF(
                    JSON_TYPE(JSON_QUERY(content, '$.args')) = 'object',
                    JSON_QUERY(content, '$.args'),
                    JSON_OBJECT()
                  )
                )
              )
            )
          )
        WHEN event_type = 'AGENT_TRANSFER'
          THEN JSON_OBJECT(
            'role', 'model',
            'parts', JSON_ARRAY(
              JSON_OBJECT(
                'function_call', JSON_OBJECT(
                  'name', 'transfer_to_agent',
                  'args', JSON_OBJECT(
                    'agent_name',
                    COALESCE(JSON_VALUE(content, '$.to_agent'), '')
                  )
                )
              )
            )
          )
        WHEN
          (
            event_type = 'TOOL_COMPLETED'
            AND NULLIF(tool_name, '') IS NOT NULL
            AND NOT is_framework_tool
          )
          OR event_type IN (
            'HITL_CREDENTIAL_REQUEST_COMPLETED',
            'HITL_CONFIRMATION_REQUEST_COMPLETED',
            'HITL_INPUT_REQUEST_COMPLETED'
          )
          THEN JSON_OBJECT(
            'role', 'user',
            'parts', JSON_ARRAY(
              JSON_OBJECT(
                'function_response', JSON_OBJECT(
                  'name', COALESCE(tool_name, ''),
                  'response', IF(
                    JSON_TYPE(JSON_QUERY(content, '$.result')) = 'object',
                    JSON_QUERY(content, '$.result'),
                    JSON_OBJECT('result', JSON_QUERY(content, '$.result'))
                  )
                )
              )
            )
          )
        WHEN
          event_type = 'TOOL_ERROR'
          AND NULLIF(tool_name, '') IS NOT NULL
          AND NOT is_framework_tool
          THEN JSON_OBJECT(
            'role', 'user',
            'parts', JSON_ARRAY(
              JSON_OBJECT(
                'function_response', JSON_OBJECT(
                  'name', tool_name,
                  'response', JSON_OBJECT(
                    'error',
                    COALESCE(NULLIF(error_message, ''), 'Tool execution failed')
                  )
                )
              )
            )
          )
        WHEN event_type = 'AGENT_RESPONSE' AND response_text IS NOT NULL
          THEN JSON_OBJECT(
            'role', 'model',
            'parts', JSON_ARRAY(JSON_OBJECT('text', response_text))
          )
        ELSE NULL
      END AS event_content
    FROM
      AnnotatedEvents
  ),
  TurnRollup AS (
    SELECT
      session_id,
      invocation_id,
      ARRAY_AGG(
        user_id IGNORE NULLS ORDER BY timestamp LIMIT 1
      )[SAFE_OFFSET(0)] AS user_id,
      ARRAY_AGG(
        root_agent_name IGNORE NULLS ORDER BY timestamp LIMIT 1
      )[SAFE_OFFSET(0)] AS first_root_agent_name,
      ARRAY_AGG(
        agent IGNORE NULLS ORDER BY timestamp LIMIT 1
      )[SAFE_OFFSET(0)] AS first_agent,
      MIN(timestamp) AS turn_start_timestamp,
      MAX(timestamp) AS turn_end_timestamp,
      COUNTIF(is_tool_call) AS tool_call_count,
      COUNTIF(event_type = 'TOOL_ERROR' AND NOT is_framework_tool)
        AS tool_error_count,
      COUNTIF(event_type = 'LLM_ERROR') AS llm_error_count,
      COUNTIF(event_type = 'AGENT_ERROR') AS agent_error_count,
      COALESCE(
        LOGICAL_OR(
          status = 'ERROR'
          OR (COALESCE(status, '') != 'OK' AND error_message IS NOT NULL)
          OR (event_type = 'LLM_RESPONSE' AND error_message IS NOT NULL)
        ),
        FALSE
      ) AS has_error,
      LOGICAL_OR(event_content IS NOT NULL AND COALESCE(is_truncated, FALSE))
        AS has_truncated_content,
      COALESCE(MAX(tool_call_run_length), 0) >= 3 AS has_tool_loop,
      COALESCE(MAX(tool_call_run_length), 0)
        AS max_consecutive_identical_tool_calls,
      COALESCE(
        ARRAY_AGG(
          DISTINCT invoked_tool_name IGNORE NULLS ORDER BY invoked_tool_name
        ),
        ARRAY<STRING>[]
      ) AS distinct_tools_used,
      COALESCE(SUM(IF(is_usage_row, usage_prompt, NULL)), 0)
        AS total_prompt_tokens,
      COALESCE(SUM(IF(is_usage_row, usage_completion, NULL)), 0)
        AS total_completion_tokens,
      COALESCE(SUM(IF(is_usage_row, usage_total, NULL)), 0) AS total_tokens,
      COALESCE(
        MAX(IF(event_type = 'INVOCATION_COMPLETED', total_ms, NULL)),
        MAX(IF(event_type = 'AGENT_COMPLETED', total_ms, NULL))
      ) AS turn_latency_ms,
      COALESCE(
        LOGICAL_OR(event_type = 'INVOCATION_COMPLETED'), FALSE
      )
      AND COALESCE(
        LOGICAL_OR(event_type = 'AGENT_RESPONSE' AND response_text IS NOT NULL),
        FALSE
      ) AS is_complete,
      COALESCE(
        LOGICAL_OR(
          event_type = 'USER_MESSAGE_RECEIVED' AND message_text IS NOT NULL
        ),
        FALSE
      ) AS has_user_message,
      NULLIF(
        ARRAY_TO_STRING(
          ARRAY_AGG(DISTINCT error_token IGNORE NULLS ORDER BY error_token),
          '; '
        ),
        ''
      ) AS error_summary,
      ARRAY_AGG(
        IF(
          event_content IS NULL,
          NULL,
          JSON_OBJECT(
            'author', event_author,
            'content', event_content,
            'event_time',
            FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', timestamp)
          )
        )
        IGNORE NULLS
        ORDER BY timestamp, event_seq, event_id
      ) AS events
    FROM
      TraceEvents
    GROUP BY
      session_id,
      invocation_id
  ),
  Turns AS (
    SELECT
      *,
      COALESCE(first_root_agent_name, first_agent) AS root_agent,
      ROW_NUMBER() OVER (
        PARTITION BY session_id
        ORDER BY turn_start_timestamp, invocation_id
      ) - 1 AS turn_index,
      IF(
        COALESCE(ARRAY_LENGTH(events), 0) > 0, TO_JSON(events), JSON_ARRAY()
      ) AS events_json,
      (
        has_user_message
        AND is_complete
        AND NOT has_truncated_content
        AND COALESCE(ARRAY_LENGTH(events), 0) > 0
      ) AS eval_ready
    FROM
      TurnRollup
  ),
  AgentRows AS (
    SELECT
      session_id,
      invocation_id,
      agent AS agent_id,
      timestamp,
      IF(
        event_type = 'AGENT_STARTING',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'),
          IF(
            REGEXP_CONTAINS(
              JSON_VALUE(content, '$'),
              r'^(?:<(?:function|bound method) |<.+ at 0x[0-9a-fA-F]+>|functools\.partial\()'
            ),
            NULL,
            JSON_VALUE(content, '$')
          ),
          JSON_VALUE(attributes, '$.static_instruction')
        ),
        NULL
      ) AS instruction,
      IF(
        event_type = 'AGENT_STARTING',
        JSON_VALUE(attributes, '$.agent_description'),
        NULL
      ) AS description,
      IF(
        event_type = 'AGENT_STARTING'
        AND JSON_TYPE(JSON_QUERY(attributes, '$.sub_agents')) = 'array',
        JSON_QUERY(attributes, '$.sub_agents'),
        NULL
      ) AS sub_agents,
      IF(
        event_type = 'LLM_REQUEST'
        AND JSON_TYPE(JSON_QUERY(attributes, '$.tools')) = 'array',
        JSON_QUERY(attributes, '$.tools'),
        NULL
      ) AS tool_declarations
    FROM
      SessionEvents
    WHERE
      agent IS NOT NULL
  ),
  AgentLatest AS (
    SELECT
      session_id,
      invocation_id,
      agent_id,
      ARRAY_AGG(
        instruction IGNORE NULLS ORDER BY timestamp DESC LIMIT 1
      )[SAFE_OFFSET(0)] AS instruction,
      ARRAY_AGG(
        description IGNORE NULLS ORDER BY timestamp DESC LIMIT 1
      )[SAFE_OFFSET(0)] AS description,
      ARRAY_AGG(
        sub_agents IGNORE NULLS ORDER BY timestamp DESC LIMIT 1
      )[SAFE_OFFSET(0)] AS sub_agents,
      ARRAY_AGG(
        tool_declarations IGNORE NULLS ORDER BY timestamp DESC LIMIT 1
      )[SAFE_OFFSET(0)] AS tool_declarations
    FROM
      AgentRows
    GROUP BY
      session_id,
      invocation_id,
      agent_id
  ),
  AgentToolDeclarations AS (
    SELECT
      session_id,
      invocation_id,
      agent_id,
      ARRAY_AGG(
        IF(
          JSON_TYPE(JSON_QUERY(declaration, '$.parameters')) = 'object',
          JSON_OBJECT(
            'name', JSON_VALUE(declaration, '$.name'),
            'description',
            COALESCE(JSON_VALUE(declaration, '$.description'), ''),
            'parameters_json_schema', JSON_QUERY(declaration, '$.parameters')
          ),
          JSON_OBJECT(
            'name', JSON_VALUE(declaration, '$.name'),
            'description',
            COALESCE(JSON_VALUE(declaration, '$.description'), '')
          )
        )
        ORDER BY declaration_offset
      ) AS function_declarations
    FROM
      AgentLatest
    INNER JOIN
      UNNEST(JSON_QUERY_ARRAY(tool_declarations)) AS declaration
      WITH OFFSET AS declaration_offset
    WHERE
      JSON_VALUE(declaration, '$.name') IS NOT NULL
    GROUP BY
      session_id,
      invocation_id,
      agent_id
  ),
  SessionAgentPrompts AS (
    SELECT
      session_id,
      agent AS agent_id,
      ARRAY_AGG(
        IF(
          JSON_TYPE(JSON_QUERY(content, '$.system_prompt')) = 'string'
            AND TRIM(JSON_VALUE(content, '$.system_prompt')) != '',
          JSON_VALUE(content, '$.system_prompt'),
          NULL
        )
        IGNORE NULLS
        ORDER BY timestamp, event_id
        LIMIT 1
      )[SAFE_OFFSET(0)] AS logged_system_prompt
    FROM
      SessionEvents
    WHERE
      event_type = 'LLM_REQUEST'
      AND agent IS NOT NULL
    GROUP BY
      session_id,
      agent
  ),
  AgentConfigs AS (
    SELECT
      AgentLatest.session_id,
      AgentLatest.invocation_id,
      AgentLatest.agent_id,
      JSON_OBJECT(
        'agent_id', AgentLatest.agent_id,
        'instruction',
        COALESCE(
          NULLIF(AgentLatest.instruction, ''),
          SessionAgentPrompts.logged_system_prompt,
          ''
        ),
        'description', COALESCE(AgentLatest.description, ''),
        'sub_agents', COALESCE(AgentLatest.sub_agents, JSON_ARRAY()),
        'tools', IF(
          AgentToolDeclarations.function_declarations IS NULL,
          JSON_ARRAY(),
          JSON_ARRAY(
            JSON_OBJECT(
              'function_declarations',
              TO_JSON(AgentToolDeclarations.function_declarations)
            )
          )
        )
      ) AS agent_config
    FROM
      AgentLatest
    LEFT JOIN
      SessionAgentPrompts
      USING (session_id, agent_id)
    LEFT JOIN
      AgentToolDeclarations
      USING (session_id, invocation_id, agent_id)
  ),
  AgentMaps AS (
    SELECT
      session_id,
      invocation_id,
      JSON_OBJECT(
        ARRAY_AGG(agent_id ORDER BY agent_id),
        ARRAY_AGG(agent_config ORDER BY agent_id)
      ) AS agents
    FROM
      AgentConfigs
    GROUP BY
      session_id,
      invocation_id
  ),
  EvalTurns AS (
    SELECT
      t.session_id,
      t.invocation_id,
      t.turn_index,
      t.user_id,
      t.root_agent,
      t.turn_start_timestamp,
      t.turn_end_timestamp,
      t.tool_call_count,
      t.tool_error_count,
      t.llm_error_count,
      t.agent_error_count,
      t.has_error,
      t.has_truncated_content,
      t.has_tool_loop,
      t.max_consecutive_identical_tool_calls,
      t.distinct_tools_used,
      t.total_prompt_tokens,
      t.total_completion_tokens,
      t.total_tokens,
      t.turn_latency_ms,
      t.eval_ready,
      JSON_OBJECT(
        'agents', COALESCE(m.agents, JSON_OBJECT()),
        'turns', JSON_ARRAY(
          JSON_OBJECT(
            'turn_index', 0,
            'turn_id', t.invocation_id,
            'events', t.events_json
          )
        )
      ) AS trace,
      t.error_summary
    FROM
      Turns AS t
    LEFT JOIN
      AgentMaps AS m
      ON m.session_id = t.session_id AND m.invocation_id = t.invocation_id
  )
SELECT
  session_id,
  invocation_id,
  turn_index,
  user_id,
  root_agent,
  turn_start_timestamp,
  turn_end_timestamp,
  tool_call_count,
  tool_error_count,
  llm_error_count,
  agent_error_count,
  has_error,
  has_truncated_content,
  has_tool_loop,
  max_consecutive_identical_tool_calls,
  distinct_tools_used,
  total_prompt_tokens,
  total_completion_tokens,
  total_tokens,
  turn_latency_ms,
  eval_ready,
  trace,
  error_summary,
  BYTE_LENGTH(TO_JSON_STRING(trace)) AS raw_trace_bytes,
  CAST(NULL AS STRING) AS _ai_index_trajectory
FROM
  EvalTurns;
