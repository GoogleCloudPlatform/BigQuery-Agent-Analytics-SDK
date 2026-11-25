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
-- Continuous pattern discovery over materialized `eval_turns` metadata and
-- compact `_ai_index_trajectory` strings (0 bytes of raw `trace` JSON read).
--
-- Surfaces four actionable regression/cost cohorts per `root_agent`:
--   1. SILENT_CONTRADICTION_CANDIDATE: Extracts the final `AGENT:` utterance
--      line first (never negating `SEARCH` over the full trajectory, because
--      the `[OUTCOME: ...]` header and `TOOL: ... -> ERROR:` lines contain error
--      words and nullable `SEARCH` can return NULL) and flags turns with an
--      error where `agent_text` lacks failure/refusal markers.
--   2. TOOL_LOOP: `has_tool_loop = TRUE` (>= 3 consecutive identical tool calls).
--   3. EXACT_ERROR_SPIKE: Turns with errors where the agent properly reported
--      the failure.
--   4. TOKEN_BLOAT: Clean turns whose `raw_trace_bytes` or `total_tokens` exceed
--      configured bloat thresholds.
--
-- Substitution contract: replace the PROJECT_ID, DATASET_ID, and
-- EVAL_TURNS_TABLE placeholders with validated BigQuery identifiers.
-- Required parameters:
--   @lookback_days INT64, @token_bloat_bytes INT64, @token_bloat_tokens INT64

WITH
  TurnSignals AS (
    SELECT
      session_id,
      invocation_id,
      turn_index,
      turn_start_timestamp,
      COALESCE(root_agent, 'unknown_agent') AS root_agent,
      COALESCE(has_error, FALSE) AS has_error,
      COALESCE(has_tool_loop, FALSE) AS has_tool_loop,
      COALESCE(max_consecutive_identical_tool_calls, 0)
        AS max_consecutive_identical_tool_calls,
      COALESCE(total_tokens, 0) AS total_tokens,
      COALESCE(raw_trace_bytes, 0) AS raw_trace_bytes,
      error_summary,
      _ai_index_trajectory,
      (
        COALESCE(has_error, FALSE)
        OR NULLIF(TRIM(error_summary), '') IS NOT NULL
      ) AS effective_has_error,
      COALESCE(
        ARRAY_REVERSE(
          REGEXP_EXTRACT_ALL(
            COALESCE(_ai_index_trajectory, ''),
            r'(?m)^AGENT:\s*(.+)$'
          )
        )[SAFE_OFFSET(0)],
        COALESCE(
          REGEXP_EXTRACT(
            COALESCE(_ai_index_trajectory, ''),
            r'(?m)^AGENT:\s*(.+)$'
          ),
          ''
        )
      ) AS agent_text
    FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
    WHERE turn_start_timestamp >= TIMESTAMP_SUB(
      CURRENT_TIMESTAMP(),
      INTERVAL @lookback_days DAY
    )
      AND NOT STARTS_WITH(session_id, 'scale_synth_')
  ),
  ClassifiedTurns AS (
    SELECT
      *,
      (
        effective_has_error
        AND agent_text != ''
        AND NOT REGEXP_CONTAINS(
          LOWER(agent_text),
          r"(?:\b(?:error|sorry|failed|unable|expired|cannot|declined|denied|restricted|unauthorized|unfortunately|dispatched|rejected)\b|\bcould\s+not\b|\bcouldn't\b|\bcan't\b)"
        )
      ) AS is_silent_contradiction_candidate,
      CASE
        WHEN has_tool_loop
          THEN 'TOOL_LOOP'
        WHEN effective_has_error
          AND agent_text != ''
          AND NOT REGEXP_CONTAINS(
            LOWER(agent_text),
            r"(?:\b(?:error|sorry|failed|unable|expired|cannot|declined|denied|restricted|unauthorized|unfortunately|dispatched|rejected)\b|\bcould\s+not\b|\bcouldn't\b|\bcan't\b)"
          )
          THEN 'SILENT_CONTRADICTION_CANDIDATE'
        WHEN effective_has_error
          THEN 'EXACT_ERROR_SPIKE'
        WHEN raw_trace_bytes >= @token_bloat_bytes
          OR total_tokens >= @token_bloat_tokens
          THEN 'TOKEN_BLOAT'
        ELSE 'HEALTHY'
      END AS pattern_class
    FROM TurnSignals
  )
SELECT
  CONCAT(root_agent, ':', pattern_class) AS pattern_id,
  root_agent,
  pattern_class,
  COUNT(*) AS affected_turns,
  SUM(total_tokens) AS total_tokens_spent,
  COUNTIF(has_tool_loop) AS tool_loop_turns,
  COUNTIF(is_silent_contradiction_candidate) AS silent_contradiction_candidates,
  ARRAY_AGG(
    STRUCT(
      session_id,
      invocation_id,
      turn_index,
      has_error,
      has_tool_loop,
      error_summary,
      total_tokens,
      raw_trace_bytes,
      _ai_index_trajectory
    )
    ORDER BY total_tokens DESC, turn_start_timestamp DESC
    LIMIT 5
  ) AS sample_turns
FROM ClassifiedTurns
WHERE pattern_class != 'HEALTHY'
GROUP BY root_agent, pattern_class
ORDER BY affected_turns DESC, total_tokens_spent DESC;
