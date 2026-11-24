# Verification Ledger — Combined CUJ Trace Search Demo

This file records verified hermetic/offline invariants alongside the unified live BigQuery + Vertex AI + Gemini Decisions API execution measurements (`20261124_200850`, `US` multi-region, `global` Vertex AI `gemini-4-hydrogen` with `thinking_level="LOW"`, and `gemini-decision-1` `L1_DECISIONS_API` with `0` output tokens) synced across the CLI runner and `combined_cuj_trace_search_demo.ipynb`.

## 1. Hermetic & Offline Test Suites

| Check | Scope | Status | Observed Result |
| :--- | :--- | :--- | :--- |
| Root combined test wrapper | `tests/examples/test_combined_cuj_trace_search_demo.py` | PASS | `3 passed` (runs all 3 demo test suites in isolated subprocesses) |
| Deterministic `S1`/`S2`/`S3` extractor & ADK tools | `tests/test_trajectory_extractor_and_agent.py` | PASS | Verifies `S1 > S2 > S3`, `S3 < 2,048` tokens on `25 KB` payloads, trailing error preservation, `0` LLM calls, key-aware secret scrubbing |
| Router, SQL contract & Pattern-to-Eval | `tests/test_router_sql_and_patterns.py` | PASS | Verifies `L1_DECISIONS_API` SDK & REST parity (`0` output tokens, fail-closed blocking/malformed/redirect checks, `NaN`/`Inf` & `0`-token guards), `load_safe_env` caller-override precedence, package-export ADK SQL resolver, pure-ASCII SQL, `has_error`/`root_agent` schema, quoted `IN`/`NEAR` keywords, `AI.SEARCH` & `VERIFIER_ERROR` handling, `< 5,000`-row `IVF` skip, `bqaa_demo` ownership checks, `EvalSuite.check_health()` |
| HTML showcase (`0600`, ASCII code, XSS), E2E `--offline`, `.ipynb`, & OSS hygiene | `tests/test_showcase_html_and_hygiene.py` | PASS | Verifies `0600` upfront artifact permissions, `(Decisions API)` L1 router badge, self-contained HTML without altering caller dir permissions, `run_e2e_demo.sh --offline` exit `0`, `nbclient` offline execution, and `0` OSS hygiene violations |
| Repo CI formatting (`isort` + `pyink`) | `bash autoformat.sh` | PASS | Clean check (`0` diffs) |

## 2. Offline End-to-End Run (`./run_e2e_demo.sh --offline`)

- **Command:** `PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 ./run_e2e_demo.sh --offline`
- **Status:** PASS (`exit 0`; produces `0600`-permissioned `live_demo_results.json`, `search_results.json`, `pattern_eval_report.json`, `eval_dataset_generated.json`, and `showcase.html`).

## 3. Live BigQuery + Vertex AI + Decisions API Run (`reports/final_live_run/` & `combined_cuj_trace_search_demo.ipynb`)

### 3.1 Live 8-Scenario ADK + `S1`/`S2`/`S3` Extraction Ablation (`US`, `gemini-4-hydrogen` / `global`)

- **Run Timestamp:** `2026-11-24T20:13:50.172279+00:00` (session batch `20261124_200850`, `mock_live=false`, `8` live ADK turns materialized into `eval_turns` via `adk_build_eval_turn_query_sql`)
- **Ingest Summarization LLM Cost:** `$0.00` (100% deterministic Python/regex extraction)
- **Aggregate Byte Reduction (`S1 -> S3`):** `106,049 B -> 7,096 B` (**`93.3%` reduction**)

| Metric | `S1` (Raw `trace` JSON) | `S2` (Subfields Only) | `S3` (Outcome-First + Regex/Head-Tail Cap) |
| :--- | ---: | ---: | ---: |
| Average payload size | `13,256 B` (`3,314` est. tokens) | `3,023 B` (`756` est. tokens) | **`887 B` (`222` est. tokens)** |
| Turns exceeding `2,048`-token embedding cap | `8 / 8` | `1 / 8` | **`0 / 8`** |
| Bloated outlier (`scn_05_bloated_audit_blob`) | `25,066 B` (`6,267` est. tokens) | `15,209 B` (`3,803` est. tokens) | **`1,045 B` (`262` est. tokens)** — preserves `AUDIT_SYNC_WARNING` & `CHECKSUM_MISMATCH` |
| Silent contradiction (`sess_03_silent_contradiction_refund_20261124_200850`) | `11,477 B` (`2,869` est. tokens) | `930 B` (`233` est. tokens) | **`709 B` (`178` est. tokens)** (`-93.8%`) |

### 3.2 Live BigQuery Dual-Index & 4-Layer Search Query Job Stats

Measured via live BigQuery query job statistics (`QueryJob.total_bytes_processed` & `slot_millis`):

| Run Scale | `index_status` | Path A (`KEYWORD`) Step-1 Bytes | Path B (`AI.SEARCH` `rerank_score DESC`) Step-1 Bytes | Path C (`HYBRID` RRF) Step-1 Bytes | Raw `trace` Baseline Bytes | Transparent Index Notes |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| **8-row live run** | `ai_embed_column: OK` (`8/8` ready, `warnings: []`), `search_index: SCAN_FALLBACK_BELOW_10GIB`, `vector_index: SKIPPED_BELOW_5000_ROWS` | **`8,634 B`** (`-90.2%`, **`10.2x` reduction** vs baseline) | `57,802 B` (`-34.5%`) | `57,802 B` (`-34.5%`) | `88,194 B` | `SEARCH INDEX` reports `BASE_TABLE_TOO_SMALL` below `10 GiB`; `IVF` cleanly skipped below `5,000` rows; `--no-rerank` (`PATH_B_AI_SEARCH_NO_RERANK` & `PATH_C_HYBRID_RRF_NO_RERANK`) verified live via `L1_DECISIONS_API` |
| **5,091-row scale run (`--with-vector-index-scale`)** | `ai_embed_column: OK` (`1,000/5,091` initial async ready, `warnings: ["Partial embeddings ready..."]`), `search_index: SCAN_FALLBACK_BELOW_10GIB`, `vector_index: CREATED` (`IVF`) | **`1,966,485 B`** (`1.97 MB`, `-94.7%`, **`18.7x` reduction** vs baseline) | `14,258,485 B` (`14.26 MB`, `-61.3%`) | `14,258,485 B` (`14.26 MB`, `-61.3%`) | `36,828,878 B` (`36.83 MB`) | `CREATE VECTOR INDEX ... IVF` succeeded live; immediate post-DDL queries emit a `Partial embeddings ready` warning and show `PENDING_INDEX_CREATION` during async `IVF` build |

### 3.3 Query Router, Scoped `AI.GENERATE`, Pattern Discovery, Modeled Cost Cascade & Notebook

| Dimension | Result |
| :--- | :--- |
| **Layer 0 Deterministic Routing** (`q1_exact_error`, `q2_quoted_phrase`, `q5_hybrid_mixed`) | **Measured Live:** `L0_RULE`, **`0.0 ms`**, `0` tokens, **`$0.00`** (`q1` `8,634 B`, top=`sess_06`, L3 `conf=0.99`; `q2` `8,634 B`, top=`sess_03`, L3 `conf=1.0`; `q5` `HYBRID` top=`sess_02`: `rrf=0.0323`, `rerank=0.2022`, `dist=0.3443`) |
| **Layer 1 Classifier** (`q3_nl_contradiction`, `q4_nl_retry_loop`) | **Measured Live:** `L1_DECISIONS_API` (`model="gemini-decision-1"`, **`0` output tokens**, `$0.000046`/call): `q3` `conf=0.9976` (`in_tok=128`), top=`sess_03_silent_contradiction_refund_20261124_200850` (`dist=0.4460`, `rerank=0.3822`, L3 `conf=1.0`); `q4` `conf=0.9991` (`in_tok=127`), top=`sess_04_deadline_retry_loop_20261124_200850` (`dist=0.4980`, `rerank=0.3476`); `--no-rerank` (`PATH_B_AI_SEARCH_NO_RERANK` & `PATH_C_HYBRID_RRF_NO_RERANK`) also verified live via `L1_DECISIONS_API` (`q3` `dist=0.4481`, `q4` `dist=0.4951`) |
| **Layer 3 Scoped `AI.GENERATE` Contradiction Verifier** | **Measured Live:** Verified silent contradiction on `sess_03_silent_contradiction_refund_20261124_200850` (`SILENT_CONTRADICTION`, `confidence=1.0`) and accurately classified `sess_06` (`conf=0.99`) without scanning unranked turns |
| **Pattern Discovery (`sql/05_pattern_discovery.sql`)** | **Measured Live:** `4` cohorts discovered: `SILENT_CONTRADICTION_CANDIDATE` (`2` turns, `26,227` tok: `sess_08`, `sess_03`), `EXACT_ERROR_SPIKE` (`2` turns, `13,305` tok: `sess_02`, `sess_07`), `TOOL_LOOP` (`1` turn, `16,398` tok: `sess_04`), `TOKEN_BLOAT` (`1` turn, `14,235` tok: `sess_05`) |
| **`EvalSuite.check_health()`** | **Measured Live:** **`6` total tasks** (`2` capability positive / `4` regression negative covering all `4` cohorts using `100%` real BigQuery `session_id`s), `balance_ratio=0.333`, `warnings=[]` |
| **8-Lever Cost Cascade (`tokens` / `cost per 1K evals`, `MODELED_PROJECTION`)** | **Modeled Projection** (parameterized from live `S1`/`S3` byte counts under uniform `$0.30/1M` in, `$2.50/1M` out judge pricing for Arms 2–4 vs `$3.00/$15.00` Frontier Arm 1; recall `0.88 -> 0.98` & p50 latencies `2,400 -> 145 ms` are analytical projections): `Arm 1` `3,764,125` tok / `$16.6924` (`$16.69`) -> `Arm 2` `401,375` tok / `$0.5164` (`$0.52`, `-96.91%`) -> `Arm 3` `247,756` tok / `$0.2475` (`$0.25`, `-98.52%`) -> **`Arm 4` `92,426` tok / `$0.0725` (`$0.07`, `-99.57%`)** |
| **`combined_cuj_trace_search_demo.ipynb` Live Run (`DEMO_MOCK_LIVE=false`)** | **Measured Live:** Cleanly executed all code cells synced to `reports/final_live_run/` with repo-relative paths and masked project display outputs |
