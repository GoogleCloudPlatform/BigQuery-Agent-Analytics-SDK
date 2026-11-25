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
-- Layer-3 scoped `AI.GENERATE` contradiction verification over `top_k`
-- candidate turns (`UNNEST(@candidate_keys)`). Reads only compact
-- `_ai_index_trajectory` strings (0 bytes of raw `trace` JSON) with
-- `thinking_level = "LOW"` on `gemini-4-hydrogen` for low cost and latency.
--
-- Substitution contract: replace the PROJECT_ID, DATASET_ID, EVAL_TURNS_TABLE,
-- ENDPOINT, and CONNECTION_ARG placeholders in the query body below literally
-- (do not use Python str.format, because model_params JSON contains {}).
-- The CONNECTION_ARG token expands to either a single-line
-- `connection_id => '<conn>', ` clause or an empty string.
-- Required parameters:
--   @candidate_keys ARRAY<STRUCT<session_id STRING, invocation_id STRING>>
--   @top_k INT64 (recommended <= 20)

WITH
  TargetKeys AS (
    SELECT
      k.session_id,
      k.invocation_id,
      ord
    FROM UNNEST(@candidate_keys) AS k WITH OFFSET AS ord
  ),
  CandidateRows AS (
    SELECT
      k.ord,
      t.session_id,
      t.invocation_id,
      t.turn_index,
      t.root_agent,
      t.has_error,
      t.error_summary,
      t._ai_index_trajectory,
      AI.GENERATE(
        prompt => CONCAT(
          'You are an agent trace verification auditor. Inspect this compact agent turn trajectory and determine whether a silent contradiction occurred (for example, a tool returned an error or failed status while the final AGENT response claimed success or omitted the failure).\n',
          'Return pattern_label as one of: SILENT_CONTRADICTION, TOOL_LOOP, EXACT_ERROR, TOKEN_BLOAT, HEALTHY.\n',
          'Trajectory:\n',
          COALESCE(t._ai_index_trajectory, '')
        ),
        endpoint => '{ENDPOINT}',
        {CONNECTION_ARG}model_params => JSON '{"generation_config": {"temperature": 0.0, "max_output_tokens": 1024, "thinking_config": {"thinking_level": "LOW"}}}',
        output_schema => 'is_silent_contradiction BOOL, pattern_label STRING, confidence FLOAT64, evidence_snippet STRING'
      ) AS ai_result
    FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}` AS t
    INNER JOIN TargetKeys AS k
      ON t.session_id = k.session_id
      AND t.invocation_id = k.invocation_id
    WHERE t._ai_index_trajectory IS NOT NULL
    ORDER BY k.ord ASC
    LIMIT @top_k
  )
SELECT
  session_id,
  invocation_id,
  turn_index,
  root_agent,
  has_error,
  error_summary,
  ai_result.is_silent_contradiction AS is_silent_contradiction,
  ai_result.pattern_label AS pattern_label,
  ai_result.confidence AS confidence,
  ai_result.evidence_snippet AS evidence_snippet,
  ai_result.status AS ai_generate_status
FROM CandidateRows
ORDER BY ord ASC;
