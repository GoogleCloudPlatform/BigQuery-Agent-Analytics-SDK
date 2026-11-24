# Verification Ledger — Combined CUJ Trace Search Demo

This file records verified hermetic/offline invariants alongside the unified live BigQuery + Vertex AI execution measurements (`20261124_174855`, `US` multi-region, `global` Vertex AI `gemini-4-hydrogen` with `thinking_level="LOW"`) synced across the CLI runner and `combined_cuj_trace_search_demo.ipynb`.

## 1. Hermetic & Offline Test Suites

| Check | Scope | Status | Observed Result |
| :--- | :--- | :--- | :--- |
| Root combined test wrapper | `tests/examples/test_combined_cuj_trace_search_demo.py` | PASS | `3 passed` (runs all 3 demo test suites in isolated subprocesses) |
| Deterministic `S1`/`S2`/`S3` extractor & ADK tools | `tests/test_trajectory_extractor_and_agent.py` | PASS | Verifies `S1 > S2 > S3`, `S3 < 2,048` tokens on `25 KB` payloads, trailing error preservation, `0` LLM calls, key-aware secret scrubbing |
| Router, SQL contract & Pattern-to-Eval | `tests/test_router_sql_and_patterns.py` | PASS | Verifies package-export ADK SQL resolver, pure-ASCII SQL, `has_error`/`root_agent` schema, quoted `IN`/`NEAR` keywords, `AI.SEARCH` & `VERIFIER_ERROR` handling, `< 5,000`-row `IVF` skip, `bqaa_demo` ownership checks, `EvalSuite.check_health()` |
| HTML showcase (`0600`, ASCII code, XSS), E2E `--offline`, `.ipynb`, & OSS hygiene | `tests/test_showcase_html_and_hygiene.py` | PASS | Verifies `0600` upfront artifact permissions, self-contained HTML without altering caller dir permissions, `run_e2e_demo.sh --offline` exit `0`, `nbclient` offline execution, and `0` OSS hygiene violations |
| Repo CI formatting (`isort` + `pyink`) | `bash autoformat.sh` | PASS | Clean check (`0` diffs) |

## 2. Offline End-to-End Run (`./run_e2e_demo.sh --offline`)

- **Command:** `PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 ./run_e2e_demo.sh --offline`
- **Status:** PASS (`exit 0`; produces `0600`-permissioned `live_demo_results.json`, `search_results.json`, `pattern_eval_report.json`, `eval_dataset_generated.json`, and `showcase.html`).

## 3. Live BigQuery + Vertex AI Run (`reports/final_live_run/` & `combined_cuj_trace_search_demo.ipynb`)

### 3.1 Live 8-Scenario ADK + `S1`/`S2`/`S3` Extraction Ablation (`US`, `gemini-4-hydrogen` / `global`)

- **Run Timestamp:** `2026-11-24T17:52:13.597262+00:00` (session batch `20261124_174855`, `mock_live=false`, `mock_events_count=0`, `8` live ADK turns materialized into `eval_turns` via `adk_build_eval_turn_query_sql`)
- **Ingest Summarization LLM Cost:** `$0.00` (100% deterministic Python/regex extraction)
- **Aggregate Byte Reduction (`S1 -> S3`):** `106,185 B -> 7,218 B` (**`93.2%` reduction**)

| Metric | `S1` (Raw `trace` JSON) | `S2` (Subfields Only) | `S3` (Outcome-First + Regex/Head-Tail Cap) |
| :--- | ---: | ---: | ---: |
| Average payload size | `13,273 B` (`3,318` est. tokens) | `3,018 B` (`755` est. tokens) | **`902 B` (`226` est. tokens)** |
| Turns exceeding `2,048`-token embedding cap | `8 / 8` | `1 / 8` | **`0 / 8`** |
| Bloated outlier (`scn_05_bloated_audit_blob`) | `24,996 B` (`6,249` est. tokens) | `15,139 B` (`3,785` est. tokens) | **`977 B` (`245` est. tokens)** — preserves `AUDIT_SYNC_WARNING` & `CHECKSUM_MISMATCH` |
| Silent contradiction (`sess_03_silent_contradiction_refund`) | `11,457 B` (`2,864` est. tokens) | `910 B` (`228` est. tokens) | **`689 B` (`173` est. tokens)** (`-94.0%`) |

### 3.2 Live BigQuery Dual-Index & 4-Layer Search Query Job Stats

Measured via live BigQuery query job statistics (`QueryJob.total_bytes_processed` & `slot_millis`):

| Run Scale | `index_status` | Path A (`KEYWORD`) Step-1 Bytes | Path B (`AI.SEARCH` `rerank_score DESC`) Step-1 Bytes | Path C (`HYBRID` RRF) Step-1 Bytes | Raw `trace` Baseline Bytes | Transparent Index Notes |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| **8-row live run** | `ai_embed_column: OK` (`8/8` ready, `warnings: []`), `search_index: SCAN_FALLBACK_BELOW_10GIB`, `vector_index: SKIPPED_BELOW_5000_ROWS` | **`8,759 B`** (`-90.1%`, **`10.1x` reduction** vs baseline) | `57,927 B` (`-34.4%`) | `57,927 B` (`-34.4%`) | `88,331 B` | `SEARCH INDEX` reports `BASE_TABLE_TOO_SMALL` below `10 GiB`; `IVF` cleanly skipped below `5,000` rows; `--no-rerank` (`distance ASC`) verified live |
| **5,074-row scale run (`--with-vector-index-scale`)** | `ai_embed_column: OK` (`2,000/5,074` initial async ready, `warnings: ["Partial embeddings ready..."]`), `search_index: SCAN_FALLBACK_BELOW_10GIB`, `vector_index: CREATED` (`IVF`) | **`1,948,168 B`** (`1.95 MB`, `-94.7%`, **`18.8x` reduction** vs baseline) | `14,240,168 B` (`14.24 MB`, `-61.1%`) | `14,240,168 B` (`14.24 MB`, `-61.1%`) | `36,642,883 B` | `CREATE VECTOR INDEX ... IVF` succeeded live; immediate post-DDL queries emit a `Partial embeddings ready` warning and show `PENDING_INDEX_CREATION` during async `IVF` build |

### 3.3 Query Router, Scoped `AI.GENERATE`, Pattern Discovery, Modeled Cost Cascade & Notebook

| Dimension | Result |
| :--- | :--- |
| **Layer 0 Deterministic Routing** (`q1_exact_error`, `q2_quoted_phrase`, `q5_hybrid_mixed`) | **Measured Live:** `L0_RULE`, **`0.0 ms`**, `0` tokens, **`$0.00`** (`q2` matched `sess_03` with `0` false hits; `q5` `HYBRID` top hit `sess_03`: `rrf=0.0325`, `rerank=0.1822`, `dist=0.3594`, L3 `conf=0.99`) |
| **Layer 1 Classifier** (`q3_nl_contradiction`, `q4_nl_retry_loop`) | **Measured Live:** `L1_VERTEX_HYDROGEN` (`gemini-4-hydrogen`, `thinking_level="LOW"`, `global`): `q3` **`2,534.82 ms`**, `$0.000457`, `conf=0.98`, top=`sess_03` (`rerank=0.3815`, `dist=0.4407`, L3 `conf=1.0`); `q4` **`1,511.00 ms`**, `$0.000434`, `conf=0.95`, top=`sess_04` (`rerank=0.3175`, `dist=0.4747`, L3 `conf=0.95`) |
| **Layer 3 Scoped `AI.GENERATE` Contradiction Verifier** | **Measured Live:** Caught silent contradiction on `sess_03_silent_contradiction_refund` (`SILENT_CONTRADICTION`, `confidence=1.0`); correctly kept `sess_08` (`EXACT_ERROR`, `confidence=0.99` when the model accurately declined post-dispatch cancellation) and `sess_01`/`sess_06` (`HEALTHY`) non-contradictory |
| **Pattern Discovery (`sql/05_pattern_discovery.sql`)** | **Measured Live:** `4` cohorts discovered: `EXACT_ERROR_SPIKE` (`3` turns, `25,916` tok), `TOOL_LOOP` (`1` turn, `16,615` tok), `TOKEN_BLOAT` (`1` turn, `14,443` tok), `SILENT_CONTRADICTION_CANDIDATE` (`1` turn, `11,353` tok) |
| **`EvalSuite.check_health()`** | **Measured Live:** **`6` total tasks** (`2` capability positive / `4` regression negative covering all `4` cohorts using `100%` real BigQuery `session_id`s), `balance_ratio=0.333`, `warnings=[]` |
| **8-Lever Cost Cascade (`tokens` / `cost per 1K evals`, `MODELED_PROJECTION`)** | **Modeled Projection** (parameterized from live `S1`/`S3` byte counts under uniform `$0.30/1M` in, `$2.50/1M` out judge pricing for Arms 2–4 vs `$3.00/$15.00` Frontier Arm 1; recall `0.88 -> 0.98` & p50 latencies `2,400 -> 145 ms` are analytical projections): `Arm 1` `3,768,375` tok / `$16.7051` (`$16.71`) -> `Arm 2` `405,125` tok / `$0.5175` (`$0.52`, `-96.90%`) -> `Arm 3` `249,819` tok / `$0.2481` (`$0.25`, `-98.51%`) -> **`Arm 4` `93,246` tok / `$0.0728` (`$0.07`, `-99.56%`)** |
| **`combined_cuj_trace_search_demo.ipynb` Live Run (`DEMO_MOCK_LIVE=false`)** | **Measured Live:** Cleanly executed all code cells synced to `reports/final_live_run/` with repo-relative paths and masked project display outputs |
