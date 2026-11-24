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
-- Dual-index BigQuery DDL setup on materialized `eval_turns`:
--   Step 1: Autonomous embedding column (`AI.EMBED` with `embeddinggemma-300m`)
--   Step 2: Full-text `SEARCH INDEX` (`LOG_ANALYZER`, column-level granularity)
--           Note: In production BigQuery, search indexes populate once a base
--           table reaches >= 10 GiB and rewrite queries when >= 100 files
--           survive partition pruning, and smaller tables fall back cleanly to scan.
--   Step 3: Unpartitioned `IVF` `VECTOR INDEX` with `STORING` covering Step-1
--           columns (no `PARTITION BY`, as `IVF` + generated embedding columns
--           reject partitioned vector indexes, and BigQuery requires >= 5,000 rows in the
--           base table, so Python runner gates execution on `COUNT(*) >= 5000`).
--
-- Substitution contract: replace the PROJECT_ID, DATASET_ID, and
-- EVAL_TURNS_TABLE placeholders with validated BigQuery identifiers. Statements
-- are separated by semicolons so callers can execute Steps 1-3 individually.

-- Step 1: Autonomous embedding column on compact `_ai_index_trajectory`
ALTER TABLE `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}`
  ADD COLUMN IF NOT EXISTS _ai_index_trajectory_embedding
    STRUCT<result ARRAY<FLOAT64>, status STRING>
    GENERATED ALWAYS AS (
      AI.EMBED(
        _ai_index_trajectory,
        model => 'embeddinggemma-300m',
        task_type => 'RETRIEVAL_DOCUMENT'
      )
    ) STORED
    OPTIONS (asynchronous = TRUE);

-- Step 2: Search index over compact trajectory and exact-lookup metadata
CREATE SEARCH INDEX IF NOT EXISTS idx_turns_search
  ON `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}` (
    _ai_index_trajectory,
    session_id,
    invocation_id,
    error_summary,
    root_agent
  )
  OPTIONS (
    analyzer = 'LOG_ANALYZER',
    default_index_column_granularity = 'COLUMN'
  );

-- Step 3: Unpartitioned IVF vector index with STORING (run when row_count >= 5000)
CREATE VECTOR INDEX IF NOT EXISTS idx_turns_vec
  ON `{PROJECT_ID}.{DATASET_ID}.{EVAL_TURNS_TABLE}` (
    _ai_index_trajectory_embedding
  )
  STORING (
    session_id,
    invocation_id,
    turn_index,
    turn_start_timestamp,
    root_agent,
    has_error,
    turn_latency_ms,
    error_summary,
    _ai_index_trajectory
  )
  OPTIONS (
    index_type = 'IVF',
    distance_type = 'COSINE'
  );
