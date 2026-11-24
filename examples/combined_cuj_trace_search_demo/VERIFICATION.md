# Verification Ledger — Combined CUJ Trace Search Demo

This file records verified hermetic/offline invariants alongside the unified live BigQuery + Vertex AI execution measurements (`20261124_094624`, `US` multi-region, `us-central1` ADK agent on `gemini-2.5-flash`) synced across the CLI runner and `combined_cuj_trace_search_demo.ipynb`.

## 1. Hermetic & Offline Test Suites

| Check | Scope | Status | Observed Result |
| :--- | :--- | :--- | :--- |
| Root combined test wrapper | `tests/examples/test_combined_cuj_trace_search_demo.py` | PASS | `3 passed` (runs all 3 demo test suites in isolated subprocesses) |
| Deterministic `S1`/`S2`/`S3` extractor & ADK tools | `tests/test_trajectory_extractor_and_agent.py` | PASS | Verifies `S1 > S2 > S3`, `S3 < 2,048` tokens on `25 KB` payloads, trailing error preservation, `0` LLM calls, secret scrubbing |
| Router, SQL contract & Pattern-to-Eval | `tests/test_router_sql_and_patterns.py` | PASS | Verifies pure-ASCII SQL, `has_error`/`root_agent` schema, `AI.SEARCH` signature, `< 5,000`-row `IVF` skip, `EvalSuite.check_health()` |
| HTML showcase (`0600`, ASCII code, XSS), E2E `--offline`, `.ipynb`, & OSS hygiene | `tests/test_showcase_html_and_hygiene.py` | PASS | Verifies `0600` self-contained HTML, `run_e2e_demo.sh --offline` exit `0`, `nbclient` 12-cell execution, and `0` OSS hygiene violations |
| Repo CI formatting (`isort` + `pyink`) | `bash autoformat.sh` | PASS | Clean check (`0` diffs) |

## 2. Offline End-to-End Run (`./run_e2e_demo.sh --offline`)

- **Command:** `PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 ./run_e2e_demo.sh --offline`
- **Status:** PASS (`exit 0`; produces `live_demo_results.json`, `search_results.json`, `pattern_eval_report.json`, `eval_dataset_generated.json`, and `0600` `showcase.html`).

## 3. Live BigQuery + Vertex AI Run (`reports/final_live_run/` & `combined_cuj_trace_search_demo.ipynb`)

### 3.1 Live 8-Scenario ADK + `S1`/`S2`/`S3` Extraction Ablation (`US`, `gemini-2.5-flash`)

- **Run Timestamp:** `2026-11-24T09:47:43Z` (session batch `20261124_094624`, `mock_live=false`, `mock_events_count=0`, `8` live ADK turns materialized into `eval_turns` via `adk_build_eval_turn_query_sql`)
- **Ingest Summarization LLM Cost:** `$0.00` (100% deterministic Python/regex extraction)
- **Aggregate Byte Reduction (`S1 -> S3`):** `104,265 B -> 5,871 B` (**`94.4%` reduction**)

| Metric | `S1` (Raw `trace` JSON) | `S2` (Subfields Only) | `S3` (Outcome-First + Regex/Head-Tail Cap) |
| :--- | ---: | ---: | ---: |
| Average payload size | `13,033 B` (`3,258` est. tokens) | `2,841 B` (`710` est. tokens) | **`734 B` (`184` est. tokens)** |
| Turns exceeding `2,048`-token embedding cap | `8 / 8` | `1 / 8` | **`0 / 8`** |
| Bloated outlier (`scn_05_bloated_audit_blob`) | `24,792 B` (`6,198` est. tokens) | `14,935 B` (`3,734` est. tokens) | **`773 B` (`194` est. tokens)** — preserves `AUDIT_SYNC_WARNING` & `CHECKSUM_MISMATCH` |
| Silent contradiction (`sess_03_silent_contradiction_refund`) | `11,314 B` (`2,829` est. tokens) | `910 B` (`228` est. tokens) | **`688 B` (`172` est. tokens)** (`-93.9%`) |

### 3.2 Live BigQuery Dual-Index & 4-Layer Search Query Job Stats

Measured via live BigQuery query job statistics (`QueryJob.total_bytes_processed` & `slot_millis`):

| Run Scale | `index_status` | Path A (`KEYWORD`) Step-1 Bytes | Path B (`AI.SEARCH` `rerank_score DESC`) Step-1 Bytes | Path C (`HYBRID` RRF) Step-1 Bytes | Raw `trace` Baseline Bytes | Transparent Index Notes |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| **8-row live run** | `ai_embed_column: OK` (`8/8` ready), `search_index: SCAN_FALLBACK_BELOW_10GIB`, `vector_index: SKIPPED_BELOW_5000_ROWS` | **`7,409 B`** (`-91.5%`, **`11.7x` reduction** vs baseline; `48`–`92` `slot_ms`) | `56,577 B` (`-34.8%`; `1,383`–`2,880` `slot_ms`) | `56,577 B` (`-34.8%`; `3,808` `slot_ms`) | `86,765 B` | `SEARCH INDEX` reports `BASE_TABLE_TOO_SMALL` below `10 GiB`; `IVF` cleanly skipped below `5,000` rows |
| **5,032-row scale run (`--with-vector-index-scale`)** | `ai_embed_column: OK` (`2,000/5,032` initial async ready), `search_index: SCAN_FALLBACK_BELOW_10GIB`, `vector_index: CREATED` (`IVF`) | **`1,904,854 B`** (`1.90 MB`, `-94.7%`, **`19.0x` reduction** vs baseline) | `14,196,854 B` (`14.20 MB`, `-60.8%`) | `14,196,854 B` (`14.20 MB`, `-60.8%`) | `36,185,352 B` | `CREATE VECTOR INDEX ... IVF` succeeded live; immediate post-DDL queries show `PENDING_INDEX_CREATION` until background `IVF` build finishes |

### 3.3 Query Router, Scoped `AI.GENERATE`, Pattern Discovery, Cost Cascade & Notebook

| Dimension | Observed Live Result |
| :--- | :--- |
| **Layer 0 Deterministic Routing** (`q1_exact_error`, `q2_quoted_phrase`, `q5_hybrid_mixed`) | `L0_RULE`, **`0.0 ms`**, `0` tokens, **`$0.00`** (`q5` `HYBRID` top hit `sess_03_silent_contradiction_refund`: `rrf=0.0325`, `rerank=0.1776`, `dist=0.3575`, L3 `conf=1.0`) |
| **Layer 1 Classifier** (`q3_nl_contradiction`, `q4_nl_retry_loop`) | `L1_VERTEX_FLASH` (`gemini-2.5-flash`, `thinking_budget=0`, `conf=0.90`): `q3` **`2,140.33 ms`**, `$0.000116`, top=`sess_03` (`rerank=0.3742`, `dist=0.4422`, L3 `conf=1.0`); `q4` **`966.97 ms`**, `$0.000146`, top=`sess_04` (`rerank=0.6339`, `dist=0.4536`) |
| **Layer 3 Scoped `AI.GENERATE` Contradiction Verifier** | Detected silent contradictions (`is_silent_contradiction=True`, `confidence=1.0`) on `sess_03_silent_contradiction_refund` (`CARD_EXPIRED`) and `sess_08_silent_contradiction_cancellation` (`SHIPMENT_ALREADY_DISPATCHED`) |
| **Pattern Discovery (`sql/05_pattern_discovery.sql`)** | `4` cohorts discovered: `SILENT_CONTRADICTION_CANDIDATE` (`2` turns, `13,862` tokens), `EXACT_ERROR_SPIKE` (`2` turns, `10,760` tokens), `TOKEN_BLOAT` (`1` turn, `12,286` tokens), `TOOL_LOOP` (`1` turn, `11,366` tokens) |
| **`EvalSuite.check_health()`** | **`6` total tasks** (`2` capability positive / `4` regression negative covering all `4` cohorts using `100%` real BigQuery `session_id`s), `balance_ratio=0.333`, `warnings=[]` |
| **8-Lever Cost Cascade (`tokens` / `cost per 1K evals`)** | `Arm 1` `3,708,625` tok / `$16.5259` (`$16.53`) -> `Arm 2` `363,250` tok / `$0.5050` (`$0.51`, `-96.94%`) -> `Arm 3` `226,788` tok / `$0.2412` (`$0.24`, `-98.54%`) -> **`Arm 4` `84,086` tok / `$0.0434` (`$0.04`, `-99.74%`)** |
| **`combined_cuj_trace_search_demo.ipynb` Live Run (`DEMO_MOCK_LIVE=false`)** | Cleanly executed all `6` code cells (`execution_count` `1`..`6`) synced to `reports/final_live_run/` with repo-relative paths and masked project display outputs |
