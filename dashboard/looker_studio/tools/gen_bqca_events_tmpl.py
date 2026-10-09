#!/usr/bin/env python3
"""Generate the BQCA Prompt & Response Logging Looker Studio query.

BigQuery Conversational Analytics (BQCA) writes its Prompt & Response Logging
rows through the BigQuery Agent Analytics plugin, so the table has the BQAA
base schema. This query is the BQCA counterpart of ``events_v1.sql.tmpl``: one
flat custom-query projection over that table, restricted to the nine event
types BQCA logs, with the canonical extractions for data-agent attribution,
persona, fast path, errors, tokens, prompts, responses, and SQL.

Extraction notes (the facts behind each expression):

* Attribution reads session state, never ``agent``, ``user_id``, or
  ``session_id``: ``agent`` is always the root agent and every turn is a new
  session, so ``data_agent_id`` and ``conversation_id`` come from
  ``attributes.session_metadata.state`` and are propagated across all rows of
  the same turn (partitioned by ``turn_partition_key``).
* ``persona`` prefers the explicit ``custom_labels.persona`` label, then the
  local part of ``user_id`` only when ``user_id`` is a well-formed email
  (optionally followed by a ``:``-delimited memory suffix). Unresolved callers
  write ``''`` and older rows carry opaque IDs, so neither may become a
  persona; the data agent is the next fallback, then ``'unattributed'``,
  propagated across all rows of the turn.
* ``fast_path`` is propagated across all rows of the turn via
  ``LOGICAL_OR(raw_fast_path) OVER (PARTITION BY turn_partition_key)`` so turn
  counts and turn latency grouped by ``fast_path_label`` reflect turn-grain
  fast-path execution even when ``$.fast_path`` is logged on only one event.
* ``is_error`` uses three conditions (``status``, ``error_message``, and an
  ``_ERROR`` event type), never ``status = 'ERROR'`` alone. When ``is_error``
  is true and ``error_message`` is null or blank, ``error_message`` is
  synthesized as ``[EVENT_TYPE: status=STATUS]`` so ``COUNT(error_message)``
  and ``COUNT_DISTINCT(error_message)`` include every 3-condition error row.
* ``completed_turn_id`` emits ``turn_id`` on the deduplicated
  ``INVOCATION_COMPLETED`` row regardless of whether ``latency_ms.total_ms``
  is present, while ``turn_latency_ms`` stays ``FLOAT64`` (null when latency
  is absent).
* ``agent_response_text`` joins every markdown part of ``content.response`` in
  order. Rejected drafts are suppressed before logging; a turn logs a second
  AGENT_RESPONSE only when an earlier accepted draft is discarded by a
  workflow nudge, so ``extracted_sql`` is extracted only from the final
  ``AGENT_RESPONSE`` row of each turn (``raw_agent_response_rn = 1``).
* ``model_name`` falls back to ``attributes.model_version``, the model field
  BQCA logs on LLM_RESPONSE.
* Token counts read ``attributes.usage_metadata`` and the plugin's
  ``content.usage`` fallback on LLM_RESPONSE rows only.
* ``similar_queries_count`` counts the suggestions on an EMBEDDING_SUGGESTION
  row. The current writer logs ``{reason, suggested_columns}``, so the
  suggested-column count (``$.suggested_columns``) is checked first before
  ``$.similar_queries_count`` and ``$.suggestions`` fallbacks;
  ``embedding_suggestion_reason`` exposes ``reason``.

Regenerating must be byte-identical (CI asserts no drift).
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "sql/bqca_events_v1.sql.tmpl"

# The only event types a BQCA query may read, in canonical order.
BQCA_EVENT_TYPES = (
    "INVOCATION_STARTING",
    "USER_MESSAGE_RECEIVED",
    "AGENT_RESPONSE",
    "INVOCATION_COMPLETED",
    "LLM_RESPONSE",
    "EMBEDDING_SUGGESTION",
    "INVOCATION_ERROR",
    "AGENT_ERROR",
    "LLM_ERROR",
)

HEADER = """\
-- bqca_events_v1.sql.tmpl — stable BQCA Prompt & Response Logging schema.
--
-- One flat Looker Studio custom-query projection over a BQAA event table
-- written by BigQuery Conversational Analytics. The three logical
-- placeholders (PROJECT, DATASET, TABLE) are rendered to executable sentinel
-- bindings by tools/render_template.py --profile bqca; for your own data
-- source, replace them with your project, dataset, and table IDs.
--
-- Only the nine event types BQCA logs are read. Attribution uses session
-- state, never the agent, user_id, or session_id columns: agent is always the
-- root agent and every turn is a new session.
--
-- Date-range parameters must be enabled on the Looker Studio data source.
-- Timezone is UTC; @DS_START_DATE/@DS_END_DATE arrive as YYYYMMDD strings,
-- with the end date inclusive.
"""

BODY = r"""WITH raw_events AS (
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
    content,
    attributes,
    latency_ms,
    status,
    error_message,
    is_truncated,
    IF(
      invocation_id IS NULL,
      NULL,
      IFNULL(NULLIF(TRIM(invocation_id), ''), CAST(timestamp AS STRING))
    ) AS turn_id,
    IF(
      invocation_id IS NULL,
      CONCAT(
        '__null_inv_',
        CAST(timestamp AS STRING),
        '_',
        IFNULL(span_id, ''),
        '_',
        IFNULL(event_type, '')
      ),
      IFNULL(NULLIF(TRIM(invocation_id), ''), CAST(timestamp AS STRING))
    ) AS turn_partition_key,
    NULLIF(
      JSON_VALUE(attributes, '$.session_metadata.state."conversation-id"'),
      ''
    ) AS raw_conversation_id,
    NULLIF(
      JSON_VALUE(attributes, '$.session_metadata.state."data-agent-id"'),
      ''
    ) AS raw_data_agent_id,
    NULLIF(
      JSON_VALUE(
        attributes, '$.session_metadata.state.custom_labels.persona'
      ),
      ''
    ) AS raw_explicit_persona,
    NULLIF(
      REGEXP_EXTRACT(
        TRIM(user_id),
        r'^([A-Za-z0-9._%+-]+)@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?::|$)'
      ),
      ''
    ) AS raw_email_persona,
    IFNULL(
      LOWER(JSON_VALUE(attributes, '$.fast_path')) = 'true',
      FALSE
    ) AS raw_fast_path,
    (
      IFNULL(UPPER(TRIM(status)) = 'ERROR', FALSE)
      OR NULLIF(TRIM(error_message), '') IS NOT NULL
      OR ENDS_WITH(UPPER(TRIM(IFNULL(event_type, ''))), '_ERROR')
    ) AS raw_is_error,
    IF(
      event_type = 'INVOCATION_COMPLETED' AND invocation_id IS NOT NULL,
      ROW_NUMBER() OVER (
        PARTITION BY
          IF(
            invocation_id IS NULL,
            CONCAT(
              '__null_inv_',
              CAST(timestamp AS STRING),
              '_',
              IFNULL(span_id, '')
            ),
            IFNULL(NULLIF(TRIM(invocation_id), ''), CAST(timestamp AS STRING))
          ),
          IF(event_type = 'INVOCATION_COMPLETED', 1, 0)
        ORDER BY
          SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64) DESC NULLS LAST,
          timestamp DESC,
          span_id DESC
      ),
      NULL
    ) AS raw_turn_complete_rn,
    IF(
      event_type = 'AGENT_RESPONSE',
      ROW_NUMBER() OVER (
        PARTITION BY
          IF(
            invocation_id IS NULL,
            CONCAT(
              '__null_inv_',
              CAST(timestamp AS STRING),
              '_',
              IFNULL(span_id, '')
            ),
            IFNULL(NULLIF(TRIM(invocation_id), ''), CAST(timestamp AS STRING))
          ),
          IF(event_type = 'AGENT_RESPONSE', 1, 0)
        ORDER BY timestamp DESC, span_id DESC
      ),
      NULL
    ) AS raw_agent_response_rn
  FROM `{{PROJECT}}.{{DATASET}}.{{TABLE}}`
  WHERE timestamp >= TIMESTAMP(
          PARSE_DATE('%Y%m%d', @DS_START_DATE), 'UTC'
        )
    AND timestamp < TIMESTAMP(
          DATE_ADD(
            PARSE_DATE('%Y%m%d', @DS_END_DATE),
            INTERVAL 1 DAY
          ),
          'UTC'
        )
    AND event_type IN (
      'INVOCATION_STARTING',
      'USER_MESSAGE_RECEIVED',
      'AGENT_RESPONSE',
      'INVOCATION_COMPLETED',
      'LLM_RESPONSE',
      'EMBEDDING_SUGGESTION',
      'INVOCATION_ERROR',
      'AGENT_ERROR',
      'LLM_ERROR'
    )
),
bqca_fields AS (
  SELECT
    timestamp,
    DATE(timestamp, 'UTC') AS event_date,
    TIMESTAMP_TRUNC(timestamp, HOUR, 'UTC') AS event_hour,
    event_type,
    agent,
    COALESCE(NULLIF(TRIM(session_id), ''), turn_id) AS session_id,
    turn_id AS invocation_id,
    user_id,
    trace_id,
    span_id,
    parent_span_id,
    COALESCE(
      FIRST_VALUE(raw_data_agent_id IGNORE NULLS) OVER (
        PARTITION BY turn_partition_key
        ORDER BY timestamp ASC, event_type ASC, span_id ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
      ),
      'unattributed'
    ) AS data_agent_id,
    FIRST_VALUE(raw_conversation_id IGNORE NULLS) OVER (
      PARTITION BY turn_partition_key
      ORDER BY timestamp ASC, event_type ASC, span_id ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    ) AS conversation_id,
    COALESCE(
      FIRST_VALUE(raw_explicit_persona IGNORE NULLS) OVER (
        PARTITION BY turn_partition_key
        ORDER BY timestamp ASC, event_type ASC, span_id ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
      ),
      FIRST_VALUE(raw_email_persona IGNORE NULLS) OVER (
        PARTITION BY turn_partition_key
        ORDER BY timestamp ASC, event_type ASC, span_id ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
      ),
      FIRST_VALUE(raw_data_agent_id IGNORE NULLS) OVER (
        PARTITION BY turn_partition_key
        ORDER BY timestamp ASC, event_type ASC, span_id ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
      ),
      'unattributed'
    ) AS persona,
    LOGICAL_OR(raw_fast_path) OVER (
      PARTITION BY turn_partition_key
    ) AS fast_path,
    status,
    IF(
      raw_is_error,
      COALESCE(
        NULLIF(TRIM(error_message), ''),
        CONCAT(
          '[',
          IFNULL(NULLIF(TRIM(event_type), ''), 'UNKNOWN_EVENT'),
          ': status=',
          IFNULL(NULLIF(TRIM(status), ''), 'ERROR'),
          ']'
        )
      ),
      NULL
    ) AS error_message,
    raw_is_error AS is_error,
    is_truncated,
    event_type = 'INVOCATION_STARTING' AS is_turn_start,
    IFNULL(raw_turn_complete_rn = 1, FALSE) AS is_turn_complete,
    IF(raw_turn_complete_rn = 1, turn_id, NULL) AS completed_turn_id,
    SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64)
      AS total_latency_ms,
    IF(
      event_type = 'LLM_RESPONSE',
      SAFE_CAST(
        JSON_VALUE(latency_ms, '$.time_to_first_token_ms') AS FLOAT64
      ),
      NULL
    ) AS ttft_ms,
    IF(
      raw_turn_complete_rn = 1,
      SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64),
      NULL
    ) AS turn_latency_ms,
    IF(
      event_type = 'LLM_RESPONSE',
      SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64),
      NULL
    ) AS llm_latency_ms,
    IF(
      event_type = 'LLM_RESPONSE',
      COALESCE(
        NULLIF(JSON_VALUE(attributes, '$.model'), ''),
        NULLIF(JSON_VALUE(attributes, '$.model_version'), '')
      ),
      NULL
    ) AS model_name,
    IF(
      event_type = 'LLM_RESPONSE',
      NULLIF(JSON_VALUE(attributes, '$.model_version'), ''),
      NULL
    ) AS model_version,
    IF(
      event_type = 'LLM_RESPONSE',
      COALESCE(
        SAFE_CAST(
          JSON_VALUE(
            attributes, '$.usage_metadata.prompt_token_count'
          ) AS INT64
        ),
        SAFE_CAST(
          JSON_VALUE(
            attributes, '$.usage_metadata.prompt_tokens'
          ) AS INT64
        ),
        SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64)
      ),
      NULL
    ) AS input_tokens,
    IF(
      event_type = 'LLM_RESPONSE',
      COALESCE(
        SAFE_CAST(
          JSON_VALUE(
            attributes, '$.usage_metadata.candidates_token_count'
          ) AS INT64
        ),
        SAFE_CAST(
          JSON_VALUE(
            attributes, '$.usage_metadata.completion_tokens'
          ) AS INT64
        ),
        SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64)
      ),
      NULL
    ) AS output_tokens,
    IF(
      event_type = 'LLM_RESPONSE',
      SAFE_CAST(
        JSON_VALUE(
          attributes, '$.usage_metadata.thoughts_token_count'
        ) AS INT64
      ),
      NULL
    ) AS thoughts_tokens,
    IF(
      event_type = 'LLM_RESPONSE',
      SAFE_CAST(
        JSON_VALUE(
          attributes, '$.usage_metadata.cached_content_token_count'
        ) AS INT64
      ),
      NULL
    ) AS cached_tokens,
    IF(
      event_type = 'LLM_RESPONSE',
      COALESCE(
        SAFE_CAST(
          JSON_VALUE(
            attributes, '$.usage_metadata.total_token_count'
          ) AS INT64
        ),
        SAFE_CAST(
          JSON_VALUE(
            attributes, '$.usage_metadata.total_tokens'
          ) AS INT64
        ),
        SAFE_CAST(JSON_VALUE(content, '$.usage.total') AS INT64)
      ),
      NULL
    ) AS total_tokens,
    IF(
      event_type IN ('USER_MESSAGE_RECEIVED', 'INVOCATION_STARTING'),
      COALESCE(
        NULLIF(JSON_VALUE(content, '$.text_summary'), ''),
        NULLIF(JSON_VALUE(content, '$.prompt'), ''),
        NULLIF(
          ARRAY_TO_STRING(
            ARRAY(
              SELECT JSON_VALUE(p, '$.text')
              FROM UNNEST(JSON_QUERY_ARRAY(content, '$.parts')) AS p
                WITH OFFSET AS o
              WHERE JSON_VALUE(p, '$.text') IS NOT NULL
              ORDER BY o
            ),
            '\n'
          ),
          ''
        ),
        NULLIF(JSON_VALUE(content, '$.parts[0].text'), '')
      ),
      NULL
    ) AS user_prompt_text,
    IF(
      event_type IN ('AGENT_RESPONSE', 'INVOCATION_COMPLETED'),
      COALESCE(
        NULLIF(JSON_VALUE(content, '$.text_summary'), ''),
        NULLIF(
          ARRAY_TO_STRING(
            ARRAY(
              SELECT JSON_VALUE(part, '$.markdown')
              FROM UNNEST(JSON_QUERY_ARRAY(content, '$.response.parts')) AS part
                WITH OFFSET AS part_offset
              WHERE JSON_VALUE(part, '$.markdown') IS NOT NULL
              ORDER BY part_offset
            ),
            '\n\n'
          ),
          ''
        ),
        NULLIF(TRIM(JSON_VALUE(content, '$.response.clarifying_question')), ''),
        NULLIF(JSON_VALUE(content, '$.parts[0].text'), ''),
        JSON_VALUE(content, '$.response')
      ),
      NULL
    ) AS agent_response_text,
    IF(
      event_type = 'EMBEDDING_SUGGESTION',
      COALESCE(
        ARRAY_LENGTH(JSON_QUERY_ARRAY(content, '$.suggested_columns')),
        SAFE_CAST(JSON_VALUE(content, '$.similar_queries_count') AS INT64),
        ARRAY_LENGTH(JSON_QUERY_ARRAY(content, '$.suggestions')),
        0
      ),
      NULL
    ) AS similar_queries_count,
    IF(
      event_type = 'EMBEDDING_SUGGESTION',
      NULLIF(JSON_VALUE(content, '$.reason'), ''),
      NULL
    ) AS embedding_suggestion_reason,
    raw_agent_response_rn
  FROM raw_events
)
SELECT
  * EXCEPT (raw_agent_response_rn),
  IF(fast_path, 'fast_path', 'standard_nl2sql') AS fast_path_label,
  IFNULL(similar_queries_count > 0, FALSE) AS is_embedding_hit,
  IF(
    event_type = 'AGENT_RESPONSE' AND raw_agent_response_rn = 1,
    REGEXP_EXTRACT(agent_response_text, r'(?is)```sql\s*(.*?)\s*```'),
    NULL
  ) AS extracted_sql,
  SUBSTR(
    COALESCE(user_prompt_text, agent_response_text, error_message, ''),
    1,
    2000
  ) AS summary_text
FROM bqca_fields
"""

BASE_TABLE = "FROM `{{PROJECT}}.{{DATASET}}.{{TABLE}}`"
EVENT_FILTER_RE = re.compile(r"AND event_type IN \(([^)]*)\)")


def generate() -> str:
  """Return the deterministic BQCA reporting query."""
  sql = HEADER + BODY
  if sql.count(BASE_TABLE) != 1:
    raise ValueError("the BQCA query must scan the base table exactly once")
  filters = EVENT_FILTER_RE.findall(sql)
  if len(filters) != 1:
    raise ValueError("the BQCA query must have exactly one event filter")
  listed = tuple(re.findall(r"'([A-Z_]+)'", filters[0]))
  if listed != BQCA_EVENT_TYPES:
    raise ValueError(f"event filter {listed} is not the BQCA allowlist")
  return sql


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
  parser.add_argument(
      "--check",
      action="store_true",
      help="fail if --output differs from a fresh generation; write nothing",
  )
  args = parser.parse_args(argv)
  sql = generate()
  if args.check:
    current = args.output.read_text() if args.output.exists() else None
    if current != sql:
      print(
          f"DRIFT: {args.output} is stale; rerun without --check",
          file=sys.stderr,
      )
      return 1
    print(f"ok {args.output}")
    return 0
  args.output.write_text(sql)
  print(f"wrote {args.output} (one base-table scan, BQCA allowlist only)")
  return 0


if __name__ == "__main__":
  sys.exit(main())
