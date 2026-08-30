# AgentForensics MVP — Execution & Implementation Plan (FINAL v4)

**Status:** plan of record. MVP-first. The six-week clock has **not** started;
it starts only when every Week 0 item below clears.

**Tracking:**
[issue #435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)
(AgentForensics MVP) and
[issue #97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
(EvalBench import contract). This text is the v4 plan finalized on #435 after
three rounds of review; the issue body points here.

**Code already landed (engineering slices, independent of Week 0):**

- [PR #451](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/451)
  — **merged** — `materialize()`: immutable, versioned EvalBench snapshots
  plus the failed-session contract (W0.4). Reference: `docs/evalbench.md`.
- [PR #452](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/452)
  — open — `failed_sessions` view and the version-pinned consumer.
- [PR #453](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/453)
  — open — `evalbench-score` CLI wrapping `Client.evaluate`.

Those slices build the Week 1–2 substrate; they do not start the clock and do
not touch the partner job, the D4 boundary, taxonomy content, or live traces.

A mechanical taxonomy *scaffold* also exists
(`src/bigquery_agent_analytics/failure_taxonomy.py`): it maps the landed
failed-session flags (`process_failed` / `missing_completion` /
`score_failed`) to a versioned config (`0.0.0-scaffold`, `g1_frozen: false`)
with an empty D2 dialects slot. The failed-session consumer now uses it:
`failed_sessions` / `bq-agent-sdk evalbench-failed-sessions` attach the
mechanical categories to each session row as `taxonomy_categories`, computed
in Python from the row's flags. It is **not** G1 — no category names are
frozen, the six-week clock has still not started, and the partner job, D4
boundary, and live-trace ingestion remain parked.

---

## Preface — what was verified before acceptance

This plan finalizes after three rounds of review. The claims new in the last
round were verified before being accepted: **KramaBench, LakeQA, and
Spider 2.0 appear nowhere in this repo** (grep across `*.py`/`*.md`); the
**SANA paper is real and is exactly adjacent work** — LakeQA + a converted
KramaBench, a seven-category failure taxonomy (task/planning, wrong source,
execution/computation, incomplete evidence, turn-waste, finalization, tool
blockers), a two-stage LLM audit, and the **Strands** runtime, which is
neither ADK nor EvalBench; the shipped `categorical_results` DDL matches the
reviewer's description (identity, validation state, endpoint, execution mode,
prompt version — and **no run id column**); and the EvalBench reader does
three sequential source reads with no snapshot consistency. All accepted.

---

## Week 0 — the pre-clock gate (P0)

The six-week clock does not start until all of these clear. They are cheap,
and every one of them changes what the six weeks build:

1. **Name the partner and its relationship to SANA.** If AgentForensics is or
   builds on SANA, taxonomy v0.1 starts from SANA's seven categories, not a
   blank page. If it is not, the RFC gains a sentence on why this isn't
   duplicating published work on the same two benchmarks. Either way the
   partner joins this thread — a collaboration plan with one org in it is
   planning in a vacuum.
2. **Confirm the pilot job's runtime and route.** SANA's harness is Strands.
   If the partner's benchmark runs are not EvalBench-hosted, the
   EvalBench-only MVP route is wrong and D1 gets re-decided *before* any
   clock starts — not discovered at week 6.
3. **Select the pilot benchmark by a predeclared rubric**, not import
   convenience: collaborator relevance, failure-mode coverage, **score
   availability and threshold-definability**, ground-truth depth (KramaBench
   ships step-level checks; LakeQA has gold sources — this decides how much
   of RFC question 5 is answerable), then trace fidelity.
4. **Approve the D4 boundary** for exactly this data — now including the
   **named report consumers** (the collaborators get exemplar and drill-down
   access), per-user grants, notebook/export restrictions, and the stop/go
   memo itself as governed artifacts. Fail-closed: no clearance → the pilot
   runs on pre-redacted real traces or pauses; a fully **synthetic run
   validates ingestion, taxonomy mechanics, and stability only** and can
   never produce a Part II funding recommendation.
5. **Freeze the preregistration doc**: every floor, margin, and decision rule
   below, including the value-gate rubric and the noisy-small-n localization
   rule. Nothing in it changes after week 1.

## The MVP (six weeks + one reserved revision week)

**Hypothesis (unchanged):** for one scored benchmark on the EvalBench route,
BQAA can produce a stable, human-usable failure-category breakdown that
*demonstrably changes* collaborators' next debugging action — and, only if
they measurably need it, event-boundary evidence.

| Week | Deliverable | Exit criterion |
|---|---|---|
| 1–2 | **Source-consistent immutable snapshot**: require a completed/immutable source-job signal or one BigQuery snapshot timestamp across the `results`/`scores`/`configs` reads; record row counts and content fingerprints in the manifest — a changed fingerprint mints a new version, never a silent `v1` reproduction. `failed_sessions` view per the W0.4 contract. | **≥100 reproducible failed sessions**, pre-partitioned and disjoint: `P-tax` (40–60, taxonomy study), `P-dev` (localization headroom/development), `P-blind` (sealed final evaluation — frozen now, untouched until week 6), `P-ex` (notebook exemplars). |
| 2–3 | Taxonomy v0.1 (seeded per week-0 item 1) + `unknown`; **two stability replicates** — named honestly: same judge model, so they measure consistency, not validity; validity comes from the labeler study. Results land in **`categorical_results`** with an added immutable **`evaluation_run_id`** binding source-snapshot version, taxonomy/prompt hashes, judge endpoint/model/execution mode, redaction-policy version, and D4 approval — no parallel four-column side table. A run is complete only when every expected row is validated. Session-level notebook ships. | Replicate agreement ≥80%; non-`unknown` coverage ≥80%. A miss invokes the **reserved revision week** (one revision, fresh labels, re-gate); a second miss **ends the MVP** with the analysis as the deliverable. |
| 3–4 | Blinded two-labeler study on a **random** `P-tax` sample. **Counterfactual value study**: investigations **randomly assigned** from the failed set (not self-selected); each collaborator records the intended next action *before* opening the report and the action *after*; a **non-investigator adjudicates** whether the report changed, materially narrowed, or accelerated the action. | Human-human κ and classifier-vs-adjudicated κ: **point ≥0.6 and 95% CI lower bound ≥0.45** (decision rule, not reporting). Value gate: ≥ the preregistered fraction of investigations where the report *changed or narrowed* the action, adjudicated — a stated preference or unverified "acceptance" counts for nothing. |
| 4 | **Localization go/no-go.** Need must be measurable: a majority of participating collaborators, backed by recorded investigations where the category report could not select a next action and boundary evidence would have. Headroom: the **total** first-error baseline (an output for every failed session) measured on `P-dev` against the preregistered margin. | Build only if classification passed, need is evidenced, and headroom exists. **New explicit branch:** classification passes but the value gate fails → no localization, no Part II productization funding; keep the analysis and investigate why the report didn't change actions. |
| 4–5 | Internal boundary prototype: chronological, coverage-preserving overlapping windows; `boundary_id` + `target_kind: imported_event`; all tuning on `P-dev` only. **Judge-integrity hardening ships here**: trace text delimited as untrusted data in the prompt, schema-validated labels, evidence bound to rendered-window substrings, escaped notebook rendering, and instruction-bearing/malicious-markup traces in the MVP test set (the shipped evaluator concatenates raw trace after the prompt — two replicates repeat the same injection failure, so this is not optional). | Every failed session in scope gets `localized`, `unlocalized`, or `missing_step`; window-selection misses are outcomes; no metric conditions on successful localizations only. |
| 6 | Score **once** on `P-blind`; write the stop/go memo (quality with CIs, counterfactual value results, cost/latency vs the week-0 budget, error analysis, funding recommendation). | Preregistered decision rules: coverage = `localized / all P-blind failed sessions` ≥70%; paired hit@1 uplift over the total baseline with **CI lower bound >0**; point uplift ≥ +10pp; absolute hit@1 ≥ the preregistered floor. Point-clears-but-CI-spans-zero resolves per the week-0 rule, not week-6 judgment. |
| 7 | *(Reserved)* taxonomy revision slot only — used or returned. | — |

**Labeler ledger (complete, inside the D4 approval):** `P-tax` 40–60 × 2 +
adjudication; **30–50 sealed boundary labels** (the most expensive per-item
task in the plan — previously missing from the ledger) + calibration;
counterfactual-study adjudication. ≈2 labeler-weeks in the MVP. Collaborator
availability is a named critical-path dependency: if investigation volume
slips, the value gate re-scopes to completed investigations with the
threshold applied proportionally — it does not silently pass.

## Part II — staged funding, no aggregate estimate

The "~10 weeks / ~16 total" framing is withdrawn — route work is explicitly
unsized, so a bottom-up number would be fiction. Each stage is sized when the
previous stage's gate passes, and the wave roster is named **after** week 6,
not week 1:

- **Stage A** — durable #97 import contract (manifested versions, both CLIs,
  multi-benchmark hardening).
- **Stage B** — second-benchmark **transfer study**: does the taxonomy
  travel? SANA's own evidence says LakeQA is search-bottlenecked and
  KramaBench analysis-bottlenecked — one taxonomy may not transfer, and this
  is where that's measured, on a disjoint corpus with the same
  preregistration discipline.
- **Stage C** — additional ingestion routes, each sized on its own: ADK
  plugin; OTLP (receiver `KNOWN_PRODUCTS` allowlist + provenance/auth + score
  mapping + fidelity tests); Strands if the partner needs it (likely, per
  SANA).
- **Stage D** — public APIs (`localize_failures()`), #429 promotion,
  `categorical_views` expansion, dashboard, corpus publication, external
  claims — each behind its predecessor's validation gate. Reference binding
  by exact scenario-id join throughout; `match_golden_qa` only for genuinely
  fuzzy text.

#436 stays parked until the taxonomy freezes (end of a passed Stage B, not
before).

## Standing invariants (unchanged from v3, restated once)

D4 precedes any real-trace judge call or labeler access and covers derived
data; evaluator fails closed. Immutable versioned imports everywhere. Random
samples for prevalence, stratified only for diagnostics. Sealed sets frozen
before tuning and used once. `returncode == 0` means completed, not passed.
Stopping is a success mode of the plan, not a failure of it.

---

This document is the plan of record. Week-0 items remain human-gated. Further
plan edits happen in this file, not in issue comments.
