# BigQuerySampler — OTel/BQAA Telemetry → ADK Eval Bridge Design

**Status:** Design / pre-implementation (proceeds independently of #316/#317)
**Issue:** [#318](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/318)
**Consumes (when available):** [#316](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/316)/[#317](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/317) native `otel_*` tables + BQAA projections
**Upstream reference:** `google/adk-python` `optimization/{sampler,data_types,local_eval_sampler}.py`
**Date:** 2026-06-26 · Updated for the OTel-native telemetry source.

---

## 1. Framing — pattern from upstream, contracts owned here

`LocalEvalSampler` (`Sampler[UnstructuredSamplingResult]`) wraps `LocalEvalService`
(`perform_inference → evaluate`) and returns `UnstructuredSamplingResult(scores,
data)`. That's the **pattern**. This issue owns: the BigQuery→ADK eval bridge,
the scoring recipe, replay metadata, and — new with the OTel-native pivot — the
**telemetry source contract and its privacy precondition**.

```python
from google.adk.optimization.sampler import Sampler          # __init__ is empty; import submodules
from google.adk.optimization.data_types import UnstructuredSamplingResult

class BigQuerySampler(Sampler[UnstructuredSamplingResult]):
    def get_train_example_ids(self) -> list[str]: ...
    def get_validation_example_ids(self) -> list[str]: ...
    async def sample_and_score(self, candidate: Agent,
        example_set="validation", batch=None, capture_full_eval_data=False
    ) -> UnstructuredSamplingResult: ...
```

`candidate` is a **modified `Agent`** → historical telemetry alone cannot score
it; scoring runs the candidate through an ADK runner/eval path. Works unchanged
across `SimplePromptOptimizer`, `GEPARootAgentPromptOptimizer`,
`GEPARootAgentOptimizer`. P0 optimizer target: **root instruction (+ root-level
`SkillToolset` skills with the newer optimizer); no optimizer touches sub-agents.**

---

## 2. Telemetry source — OTel-native first, BQAA views as compatibility

Candidate detection reads from BigQuery and **must not assume `agent_events` is
the only telemetry table**:

- **OTel-native (#316/#317):** `otel_logs`, the five `otel_metric_*` tables,
  gated `otel_spans`. Bind a **minimum `otel_schema_version`** (cross-issue
  contract): #318's mining SQL references #316's table shapes, so a schema bump
  in #316 must be a versioned, fail-fast dependency — not a silent break.
- **BQAA projections / legacy:** `agent_events_otlp`, `bqaa_metrics`, and
  existing `agent_events` hook/ADK rows.

Curated examples are built from native tables and/or BQAA views; each carries
**telemetry provenance** (`trace_id`, `span_id`, log/metric-point refs, native
table + `otel_schema_version`, or BQAA projection row refs) so the audit trail is
observability-native while BQAA semantics stay available.

---

## 3. Decision — the replay/privacy precondition (sharpest cross-cutting risk)

The OTel-native pivot makes telemetry the optimizer's input — but **#316's
privacy defaults strip exactly what replay needs.** With `OTEL_LOG_USER_PROMPTS`
off (the default) and Codex `log_user_prompt=false`, the prompt in `otel_logs` is
`<REDACTED>` / length-only. **You cannot reconstruct the input payload to re-run
a candidate from default-privacy telemetry.** Pinned consequences:

- **Replayable examples require content-bearing telemetry** (prompt/tool content
  logging enabled at *capture* time) — a documented deployment precondition for
  the flywheel, not an afterthought.
- **Examples without a reconstructable input are `eligibility.runnable=false`**
  (`skip_reason="content_redacted"`), so they never become optimizer IDs (§5.1).
- A deployment running privacy-default telemetry should expect **near-zero
  runnable examples** until content logging is enabled or inputs are supplied by
  another source (e.g. an eval set). The issue must say this plainly, or #318
  looks implementable but yields nothing to optimize.

This ties #316's privacy contract directly to #318's feasibility.

---

## 4. Two tables, two jobs

| Table | Owner job | Role |
|-------|-----------|------|
| `flywheel_candidates` | scheduled detection (~15 min) | raw mined failures from `otel_*` + BQAA views; volatile; carries telemetry refs |
| `optimizer_examples` | curation view/job | **stable, runnable** optimizer input; one row = one runnable eval case |

`get_train/validation_example_ids` read **`optimizer_examples` filtered to
`eligibility.runnable=true`** — so churn can't change a run mid-flight, and the
scores-coverage invariant (§5.1) holds by construction.

---

## 5. The BigQuery→ADK eval bridge

Each `optimizer_examples` row is **executable**, not just descriptive:

```jsonc
{
  "example_id": "stable-uuid", "split": "train|validation",
  "input_payload": { /* reconstructed user content — requires §3 content telemetry */ },
  "app_name": "nl2sql_agent",
  "eval_recipe": { "metrics": [{"name":"sql_execution_success","type":"bq_error_absent","on_missing":0.0}],
                   "primary": "sql_execution_success", "aggregation": "weighted",
                   "weights": {...}, "on_run_failure": 0.0 },
  "replay": { "tools":[...], "auth":{...}, "side_effects":"isolated",
              "fixtures":{...}, "determinism":{"temperature":0,"seed":42,"clock":"frozen:..."}, "timeout_s":120 },
  "telemetry_provenance": { "trace_id":"...", "span_ids":[...], "otel_table":"otel_logs",
                            "otel_schema_version":"1",
                            "idempotency_key":"...", "source_position":{...},  // #316 envelope/native-row fields → point to exact native log/metric rows
                            "bqaa_refs":[...] },
  "labels": {...}, "eligibility": { "runnable": true, "skip_reason": null }
}
```

### 5.1 Scores-coverage invariant (do not omit requested IDs)

`result.scores` **must contain a float for every requested ID** — ADK/GEPA index
`scores[example_id]` directly; a missing key crashes the run. Enforced by:

1. **Eligibility filtered at ID-listing time (§4)** — non-runnable examples
   (including §3 content-redacted ones) never become optimizer IDs.
2. **Run-time failure floor** — an example eligible at listing time that fails
   *during* scoring still gets `eval_recipe.on_run_failure` (default `0.0`).
3. **Post-condition:** `set(scores) == set(requested_ids)` before return.

### 5.2 Run-failure metadata is NOT gated on `capture_full_eval_data`

ADK optimizers routinely call `sample_and_score(..., capture_full_eval_data=
False)` (e.g. `SimplePromptOptimizer` training batches). So on a run failure,
**regardless of the flag**: (a) emit the **minimal** `.data[id] =
{"run_failed":true,"reason":"..."}`, and (b) write the failure to the **audit
table** as a backstop. Only the *heavy* payload (inputs, trajectory, tool
outputs, per-metric detail) stays gated behind `capture_full_eval_data=True`.
A floor `0.0` must never be indistinguishable from a genuine metric `0.0`.

---

## 6. SQL-derived scoring recipe

`scores` is `dict[str,float]`. Custom metrics (`sql_execution_success`, business
outcomes, trajectory/tool metrics) computed reference-free from BigQuery/replay,
then aggregated: `primary` (named metric only) or `weighted` (normalized weighted
sum). Per-metric detail always in `.data[id].metrics` when full eval data is
captured, and persisted to the audit table every attempt. Missing *metric* →
`on_missing` (distinct from a *run failure* → `on_run_failure`); neither omits
the key.

---

## 7. Audit & ownership

The wrapper runs `await optimizer.optimize(initial_agent, BigQuerySampler(...))`
and writes an audit row for **every** candidate (not just winners): telemetry
source refs, prompt delta, before/after scores, rejected candidates, failed runs,
evaluator/source-SQL versions + hash, train/validation IDs, model/config,
approval/canary status. **P0 does not auto-apply patches** — dry-run + approval/
canary handoff. This package owns DDL/migrations for `flywheel_candidates`,
`optimizer_examples`, and the audit table (partition/retention/IAM).

**P0:** `BigQuerySampler` on the `LocalEvalSampler` pattern; OTel-native +
BQAA telemetry source with min `otel_schema_version`; replay/privacy precondition;
eval bridge + scoring recipe + scores invariant + ungated run-failure metadata;
two-table split; persist-every-attempt audit; root-instruction(+root-skill)
target; framework-neutral candidate detection.

**Fast-follow:** LangGraph patch-apply/eval adapter; sub-agent/tool optimization;
live (non-recorded) tool replay; input reconstruction from non-telemetry sources.
