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
-- Layer-2 BigQuery retrieval templates (0 raw `trace` JSON bytes read in Step 1).
-- All Step-1 templates use `has_error` and `root_agent` (matching `v_eval_turn`).
--
-- Substitution contract: replace the PROJECT_ID, DATASET_ID, and
-- EVAL_TURNS_TABLE placeholders in each query body literally (do not use
-- Python str.format, because AI.SEARCH JSON options literals contain {}).
-- Required query parameters per statement:
--   @start_ts TIMESTAMP, @end_ts TIMESTAMP, @has_error BOOL (nullable),
--   @min_latency_ms INT64 (nullable), @search_query STRING,
--   @keyword_query STRING, @top_k INT64, and @candidate_k INT64 (for PATH_C);
--   and @session_id STRING, @invocation_id STRING for STEP2_LAZY_TRACE_HYDRATION.

-- [QUERY: PATH_A_KEYWORD]
SELECT
  session_id,
  invocation_id,
  turn_index,
  turn_start_timestamp,
  root_agent,
  has_error,
  turn_latency_ms,
  error_summary,
  _ai_index_trajectory
FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
  AND (@has_error IS NULL OR has_error = @has_error)
  AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
  AND SEARCH(
    (_ai_index_trajectory, session_id, invocation_id, error_summary, root_agent),
    @search_query
  )
ORDER BY turn_start_timestamp DESC, turn_index ASC, session_id ASC, invocation_id ASC
LIMIT @top_k;

-- [QUERY: PATH_A_METADATA_FILTER]
SELECT
  session_id,
  invocation_id,
  turn_index,
  turn_start_timestamp,
  root_agent,
  has_error,
  turn_latency_ms,
  error_summary,
  _ai_index_trajectory
FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
  AND (@has_error IS NULL OR has_error = @has_error)
  AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
ORDER BY turn_start_timestamp DESC, turn_index ASC, session_id ASC, invocation_id ASC
LIMIT @top_k;

-- [QUERY: PATH_B_AI_SEARCH_RERANK]
SELECT
  base.session_id,
  base.invocation_id,
  base.turn_index,
  base.turn_start_timestamp,
  base.root_agent,
  base.has_error,
  base.turn_latency_ms,
  base.error_summary,
  base._ai_index_trajectory,
  distance,
  rerank_score
FROM AI.SEARCH(
  (
    SELECT
      session_id,
      invocation_id,
      turn_index,
      turn_start_timestamp,
      root_agent,
      has_error,
      turn_latency_ms,
      error_summary,
      _ai_index_trajectory
    FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
    WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
      AND (@has_error IS NULL OR has_error = @has_error)
      AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
  ),
  '_ai_index_trajectory',
  @search_query,
  top_k => @top_k,
  distance_type => 'COSINE',
  rerank => TRUE,
  options => '{"fraction_lists_to_search": 0.02}'
)
ORDER BY rerank_score DESC, base.session_id ASC, base.invocation_id ASC;

-- [QUERY: PATH_B_AI_SEARCH_NO_RERANK]
SELECT
  base.session_id,
  base.invocation_id,
  base.turn_index,
  base.turn_start_timestamp,
  base.root_agent,
  base.has_error,
  base.turn_latency_ms,
  base.error_summary,
  base._ai_index_trajectory,
  distance
FROM AI.SEARCH(
  (
    SELECT
      session_id,
      invocation_id,
      turn_index,
      turn_start_timestamp,
      root_agent,
      has_error,
      turn_latency_ms,
      error_summary,
      _ai_index_trajectory
    FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
    WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
      AND (@has_error IS NULL OR has_error = @has_error)
      AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
  ),
  '_ai_index_trajectory',
  @search_query,
  top_k => @top_k,
  distance_type => 'COSINE',
  options => '{"fraction_lists_to_search": 0.02}'
)
ORDER BY distance ASC, base.session_id ASC, base.invocation_id ASC;

-- [QUERY: PATH_B_SYNC_EMBED_FALLBACK]
WITH
  QueryEmbedding AS (
    SELECT
      AI.EMBED(
        @search_query,
        model => 'embeddinggemma-300m',
        task_type => 'RETRIEVAL_QUERY'
      ).result AS query_vec
  ),
  DocEmbeddings AS (
    SELECT
      t.session_id,
      t.invocation_id,
      t.turn_index,
      t.turn_start_timestamp,
      t.root_agent,
      t.has_error,
      t.turn_latency_ms,
      t.error_summary,
      t._ai_index_trajectory,
      COALESCE(
        IF(
          t._ai_index_trajectory_embedding IS NOT NULL
            AND ARRAY_LENGTH(t._ai_index_trajectory_embedding.result) > 0,
          t._ai_index_trajectory_embedding.result,
          NULL
        ),
        AI.EMBED(
          t._ai_index_trajectory,
          model => 'embeddinggemma-300m',
          task_type => 'RETRIEVAL_DOCUMENT'
        ).result
      ) AS doc_vec
    FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}` AS t
    WHERE t.turn_start_timestamp BETWEEN @start_ts AND @end_ts
      AND (@has_error IS NULL OR t.has_error = @has_error)
      AND (@min_latency_ms IS NULL OR t.turn_latency_ms > @min_latency_ms)
      AND t._ai_index_trajectory IS NOT NULL
  )
SELECT
  d.session_id,
  d.invocation_id,
  d.turn_index,
  d.turn_start_timestamp,
  d.root_agent,
  d.has_error,
  d.turn_latency_ms,
  d.error_summary,
  d._ai_index_trajectory,
  ML.DISTANCE(d.doc_vec, q.query_vec, 'COSINE') AS distance
FROM DocEmbeddings AS d
CROSS JOIN QueryEmbedding AS q
WHERE ARRAY_LENGTH(d.doc_vec) > 0
  AND ARRAY_LENGTH(q.query_vec) > 0
ORDER BY distance ASC, d.session_id ASC, d.invocation_id ASC
LIMIT @top_k;

-- [QUERY: PATH_C_HYBRID_RRF]
WITH
  KeywordRanked AS (
    SELECT
      session_id,
      invocation_id,
      turn_index,
      turn_start_timestamp,
      root_agent,
      has_error,
      turn_latency_ms,
      error_summary,
      _ai_index_trajectory,
      ROW_NUMBER() OVER (
        ORDER BY turn_start_timestamp DESC, turn_index ASC, session_id ASC, invocation_id ASC
      ) AS kw_rank
    FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
    WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
      AND (@has_error IS NULL OR has_error = @has_error)
      AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
      AND SEARCH(
        (_ai_index_trajectory, session_id, invocation_id, error_summary, root_agent),
        @keyword_query
      )
    ORDER BY kw_rank ASC
    LIMIT @candidate_k
  ),
  SemanticRanked AS (
    SELECT
      base.session_id,
      base.invocation_id,
      base.turn_index,
      base.turn_start_timestamp,
      base.root_agent,
      base.has_error,
      base.turn_latency_ms,
      base.error_summary,
      base._ai_index_trajectory,
      distance,
      rerank_score,
      ROW_NUMBER() OVER (
        ORDER BY rerank_score DESC, distance ASC, base.session_id ASC, base.invocation_id ASC
      ) AS sem_rank
    FROM AI.SEARCH(
      (
        SELECT
          session_id,
          invocation_id,
          turn_index,
          turn_start_timestamp,
          root_agent,
          has_error,
          turn_latency_ms,
          error_summary,
          _ai_index_trajectory
        FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
        WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
          AND (@has_error IS NULL OR has_error = @has_error)
          AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
      ),
      '_ai_index_trajectory',
      @search_query,
      top_k => @candidate_k,
      distance_type => 'COSINE',
      rerank => TRUE,
      options => '{"fraction_lists_to_search": 0.02}'
    )
  )
SELECT
  COALESCE(kw.session_id, sem.session_id) AS session_id,
  COALESCE(kw.invocation_id, sem.invocation_id) AS invocation_id,
  COALESCE(kw.turn_index, sem.turn_index) AS turn_index,
  COALESCE(kw.turn_start_timestamp, sem.turn_start_timestamp)
    AS turn_start_timestamp,
  COALESCE(kw.root_agent, sem.root_agent) AS root_agent,
  COALESCE(kw.has_error, sem.has_error) AS has_error,
  COALESCE(kw.turn_latency_ms, sem.turn_latency_ms) AS turn_latency_ms,
  COALESCE(kw.error_summary, sem.error_summary) AS error_summary,
  COALESCE(kw._ai_index_trajectory, sem._ai_index_trajectory)
    AS _ai_index_trajectory,
  sem.distance,
  sem.rerank_score,
  COALESCE(1.0 / (60 + kw.kw_rank), 0.0)
    + COALESCE(1.0 / (60 + sem.sem_rank), 0.0) AS rrf_score
FROM KeywordRanked AS kw
FULL OUTER JOIN SemanticRanked AS sem
  USING (session_id, invocation_id, turn_index)
ORDER BY rrf_score DESC, COALESCE(sem.distance, 1.0) ASC, session_id ASC, invocation_id ASC
LIMIT @top_k;

-- [QUERY: PATH_C_HYBRID_RRF_NO_RERANK]
WITH
  KeywordRanked AS (
    SELECT
      session_id,
      invocation_id,
      turn_index,
      turn_start_timestamp,
      root_agent,
      has_error,
      turn_latency_ms,
      error_summary,
      _ai_index_trajectory,
      ROW_NUMBER() OVER (
        ORDER BY turn_start_timestamp DESC, turn_index ASC, session_id ASC, invocation_id ASC
      ) AS kw_rank
    FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
    WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
      AND (@has_error IS NULL OR has_error = @has_error)
      AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
      AND SEARCH(
        (_ai_index_trajectory, session_id, invocation_id, error_summary, root_agent),
        @keyword_query
      )
    ORDER BY kw_rank ASC
    LIMIT @candidate_k
  ),
  SemanticRanked AS (
    SELECT
      base.session_id,
      base.invocation_id,
      base.turn_index,
      base.turn_start_timestamp,
      base.root_agent,
      base.has_error,
      base.turn_latency_ms,
      base.error_summary,
      base._ai_index_trajectory,
      distance,
      CAST(NULL AS FLOAT64) AS rerank_score,
      ROW_NUMBER() OVER (
        ORDER BY distance ASC, base.session_id ASC, base.invocation_id ASC
      ) AS sem_rank
    FROM AI.SEARCH(
      (
        SELECT
          session_id,
          invocation_id,
          turn_index,
          turn_start_timestamp,
          root_agent,
          has_error,
          turn_latency_ms,
          error_summary,
          _ai_index_trajectory
        FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
        WHERE turn_start_timestamp BETWEEN @start_ts AND @end_ts
          AND (@has_error IS NULL OR has_error = @has_error)
          AND (@min_latency_ms IS NULL OR turn_latency_ms > @min_latency_ms)
      ),
      '_ai_index_trajectory',
      @search_query,
      top_k => @candidate_k,
      distance_type => 'COSINE',
      rerank => FALSE,
      options => '{"fraction_lists_to_search": 0.02}'
    )
  )
SELECT
  COALESCE(kw.session_id, sem.session_id) AS session_id,
  COALESCE(kw.invocation_id, sem.invocation_id) AS invocation_id,
  COALESCE(kw.turn_index, sem.turn_index) AS turn_index,
  COALESCE(kw.turn_start_timestamp, sem.turn_start_timestamp)
    AS turn_start_timestamp,
  COALESCE(kw.root_agent, sem.root_agent) AS root_agent,
  COALESCE(kw.has_error, sem.has_error) AS has_error,
  COALESCE(kw.turn_latency_ms, sem.turn_latency_ms) AS turn_latency_ms,
  COALESCE(kw.error_summary, sem.error_summary) AS error_summary,
  COALESCE(kw._ai_index_trajectory, sem._ai_index_trajectory)
    AS _ai_index_trajectory,
  sem.distance,
  sem.rerank_score,
  COALESCE(1.0 / (60 + kw.kw_rank), 0.0)
    + COALESCE(1.0 / (60 + sem.sem_rank), 0.0) AS rrf_score
FROM KeywordRanked AS kw
FULL OUTER JOIN SemanticRanked AS sem
  USING (session_id, invocation_id, turn_index)
ORDER BY rrf_score DESC, COALESCE(sem.distance, 1.0) ASC, session_id ASC, invocation_id ASC
LIMIT @top_k;

-- [QUERY: STEP2_LAZY_TRACE_HYDRATION]
SELECT
  session_id,
  invocation_id,
  turn_index,
  root_agent,
  has_error,
  raw_trace_bytes,
  trace
FROM `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
WHERE session_id = @session_id
  AND invocation_id = @invocation_id
LIMIT 1;
