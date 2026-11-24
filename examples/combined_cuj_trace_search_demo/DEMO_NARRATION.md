# Combined CUJ Demo Narration & 1080p@24fps Video Screenplay

**Audience:** Engineering leaders, platform architects, and AI observability teams evaluating BigQuery as a unified warehouse for agent trace search, cost-aware evaluation, and regression suite generation.

**Target Surface:** Self-contained Adaptive Workbench HTML (`reports/run_<timestamp>/showcase.html`, or `--demo-fixture` preview), recorded cleanly at `1080p@24fps`.

> **Ground Truth:** Quantitative figures below match the unified verified live BigQuery + Vertex AI + Gemini Decisions API run (`US` multi-region, `gemini-4-hydrogen` in `global` with `thinking_level="LOW"`, and `gemini-decision-1` `L1_DECISIONS_API` with `0` output tokens, session batch `20261124_200850`) recorded in `VERIFICATION.md`, with 4-arm `/1K` evaluation cost, recall, and latency explicitly marked as analytical projections (`MODELED_PROJECTION`).

---

## 30-Second Elevator Pitch

> "Teams love logging ADK traces to BigQuery for IAM and Row-Level Security, but evaluating raw multi-megabyte trace JSON with frontier judges breaks the bank and overflows embedding windows. In this demo, a zero-LLM deterministic extractor shrinks raw traces by `93.3%` (`13,256 B -> 887 B`, averaging `222` tokens) so zero turns overflow `2,048` tokens, indexes them with BigQuery `SEARCH INDEX` and autonomous `AI.EMBED`, routes simple lookups for `$0` in `0 ms` and behavioral queries via `gemini-decision-1` (`0` output tokens, `0.998`–`0.999` confidence), verifies silent contradictions only on `top_k` hits with `AI.GENERATE`, and graduates discovered failure cohorts straight into a balanced `6`-task `EvalSuite` backed by 100% real BigQuery session IDs—projecting a `99.57%` eval spend drop (`$16.69 -> $0.07` per `1K` evals)."

---

## 7-Step Interactive Walkthrough (`s01`–`s07`)

| Step | Scene Title | Target DOM Selector(s) | Recommended `zoom_scale` | Visual Action |
| :--- | :--- | :--- | :---: | :--- |
| `s01` | Overview & KPI Strip | `#tab-overview`, `#overview` | `1.0` | Scroll to header (showing `L1 Router: gemini-decision-1 (Decisions API)`) + KPI strip + 5-stage SVG pipeline |
| `s02` | S1 vs S2 vs S3 Ablation Lab | `#tab-ablation-lab`, `#btn-s1`, `#btn-s2`, `#btn-s3` | `1.15` | Click `#btn-s1` (`8/8` red `>2,048` bars), `#btn-s2`, then `#btn-s3` (`0/8` overflow, `222` avg tokens, trailing error preserved) |
| `s03` | Search Studio: Layer 0 Exact Rule | `#tab-search-studio`, `#query-chip-1` | `1.15` | Click `#query-chip-1` (`CARD_EXPIRED`) -> `L0_RULE`, `0 ms`, `$0`, `8,634 B` vs `88,194 B` raw baseline (`10.2x` smaller) |
| `s04` | Search Studio: Layer 1 Semantic & Hybrid RRF + Lazy Trace | `#query-chip-3`, `#query-chip-5`, `#search-results-table`, `#expand-trace-btn` | `1.20` | Click `#query-chip-3` (`L1_DECISIONS_API`, `0` decode tokens, `AI.SEARCH` `rerank_score`), then `#query-chip-5` (`HYBRID` RRF), then click `#expand-trace-btn` |
| `s05` | Modeled 8-Lever Eval Cost Cascade | `#tab-cost-cascade`, `#lever-toggles`, `#cascade-table` | `1.15` | Toggle levers in `#lever-toggles` and highlight modeled `Arm 1` (`$16.69`) vs `Arm 4` (`$0.07`) savings in `#cascade-table` |
| `s06` | Continuous Pattern Discovery & Scoped `AI.GENERATE` | `#tab-patterns-eval`, `#patterns-eval` | `1.15` | Inspect `SILENT_CONTRADICTION_CANDIDATE`, `EXACT_ERROR_SPIKE`, `TOOL_LOOP`, and `TOKEN_BLOAT` cohorts + `conf=1.0` `AI.GENERATE` badges |
| `s07` | `EvalSuite` Export & Health Check | `#export-eval-btn` | `1.10` | Click `#export-eval-btn` to reveal balanced `6`-task `EvalSuite` JSON (`balance_ratio=0.33`, `0` warnings) |

---

## Scene-by-Scene Spoken Narration

### `s01` — Overview & Pipeline (`0:00–0:30`)
**Action:** Open `showcase.html` at `#overview` (`zoom_scale: 1.0`).
> "Everything starts inside BigQuery. Our ADK customer-operations agent streams every turn through `BigQueryAgentAnalyticsPlugin` into `agent_events`, then materializes a 25-column `eval_turns` table (22 core `v_eval_turn` columns plus 3 demo extraction and summary columns). Look at the top KPI strip: deterministic extraction cuts trajectory size by `93.3%` at zero ingest LLM cost, Step-1 keyword searches cut scan bytes `10.2x` (`8,634 B` vs `88,194 B` raw `trace`), and our modeled eight-lever cascade projects a drop in evaluation cost from `$16.69` down to `$0.07` per thousand turns."

### `s02` — Deterministic Trajectory Extraction Ablation (`0:30–1:15`)
**Action:** Click `#tab-ablation-lab`, then step `#btn-s1` -> `#btn-s2` -> `#btn-s3` (`zoom_scale: 1.15`).
> "Why does naive raw-JSON embedding fail? Click `S1`: all `8` of `8` turns average `13,256 B` (`3,314` estimated tokens) and blow past the `2,048`-token embedding cap on repeated system instructions and tool schemas. `S2` strips agent boilerplate down to `3,023 B` (`756` tokens), which helps—until our `scn_05` bloated audit dump still hits `15,209 B` (`3,803` tokens). Now click `S3`: our pure-Python extractor puts `[OUTCOME: ...]` first, strips HTML noise, and head/tail-clamps payloads so `scn_05` drops to `1,045 B` (`262` tokens) while preserving `AUDIT_SYNC_WARNING` and `CHECKSUM_MISMATCH`—zero of eight turns overflow."

### `s03` — Search Studio: Layer 0 Exact Rule (`1:15–1:45`)
**Action:** Click `#tab-search-studio`, then `#query-chip-1` (`zoom_scale: 1.15`).
> "When an engineer searches for an exact error constant like `CARD_EXPIRED`, Layer 0 deterministic rules match `UPPER_SNAKE_CASE` in `0.0 ms` for `$0.00`, scanning only `8,634 B` (`10.2x` less than the `88,194 B` raw `trace` table) with zero bytes of raw `trace` JSON read."

### `s04` — Search Studio: Layer 1 Semantic, Hybrid RRF & Lazy Trace (`1:45–2:30`)
**Action:** Click `#query-chip-3`, then `#query-chip-5`, then `#expand-trace-btn` (`zoom_scale: 1.20`).
> "Conceptual queries like 'agent claimed refund succeeded even though tool failed' hit our Layer 1 router—powered by `gemini-decision-1` (`L1_DECISIONS_API`, `0` output tokens, `0.998`–`0.999` confidence, `$0.000046` per call) with Vertex `gemini-4-hydrogen` (`thinking_level="LOW"`) as fallback—and execute `AI.SEARCH` over `_ai_index_trajectory` sorted by `rerank_score DESC`, putting `sess_03` (`rerank=0.3822`, `dist=0.4460`) right at the top. Mixed queries like `Q5` combine keyword and semantic rankings via Reciprocal Rank Fusion. Only when a human drills into a specific turn does Step 2 lazy-load the full `trace` JSON."

### `s05` — Modeled 8-Lever Eval Cost Cascade (`2:30–3:10`)
**Action:** Click `#tab-cost-cascade`, toggle checkboxes in `#lever-toggles` (`zoom_scale: 1.15`).
> "Eval cost is a design choice, not a fixed tax. Parameterized from our live `S1` and `S3` byte counts under uniform `$0.30` input and `$2.50` output per-million judge pricing, stacking code prefilters, turn scoping, `S3` trimming, decision routing, `thinking_level="LOW"`, and top-K cascade escalation projects a `99.57%` cost drop—from `$16.69` (`3,764,125` tokens) per thousand evals in Arm 1 down to `$0.07` (`92,426` tokens) in Arm 4."

### `s06` & `s07` — Pattern Discovery & `EvalSuite` Export (`3:10–4:00`)
**Action:** Click `#tab-patterns-eval`, inspect the 4 discovered cohorts, then click `#export-eval-btn` (`zoom_scale: 1.10`).
> "Finally, scheduled SQL surfaces all `4` cohorts automatically: `2` `SILENT_CONTRADICTION_CANDIDATE` turns (`sess_03` verified at `1.0` confidence by scoped `AI.GENERATE`, plus `sess_08`), `2` `EXACT_ERROR_SPIKE` turns (`sess_02` and `sess_07`), `1` four-call `DEADLINE_EXCEEDED` `TOOL_LOOP` (`sess_04`), and `1` `TOKEN_BLOAT` outlier (`sess_05`). `pattern_to_eval.py` exports a balanced `6`-task `bigquery_agent_analytics.EvalSuite` (`2` positive capability + `4` negative regression across all `4` cohorts using 100% real BigQuery session IDs, `balance_ratio=0.333`, `0` health warnings) ready for CI regression gates."
