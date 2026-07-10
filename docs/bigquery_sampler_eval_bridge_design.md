# BigQuerySampler — OTel/BQAA Telemetry → ADK Eval Bridge Design

**Status:** Design v2 / pre-implementation — synchronized with the frozen [#318](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/318) contract (P0a/P0b split, trust invariants, promotion governance)
**Issue (product + acceptance contract):** [#318](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/318)
**Upstream ADK RFC (usage/budget/cancellation seam):** [google/adk-python#6357](https://github.com/google/adk-python/issues/6357)
**Consumes (P0b, when available):** [#316](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/316)/[#317](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/317) deduplicated OTel views + BQAA projections
**Upstream reference:** `google/adk-python` `optimization/{sampler,data_types,local_eval_sampler}.py`
**Date:** 2026-06-26 · v2 sync 2026-07-10

---

## 0. Delivery boundary (P0a / P0b)

**P0a proves the sampler and trust contract on a curated corpus** — no production
prompt logging required:

- `BigQuerySampler` on the `LocalEvalSampler` pattern (`perform_inference → evaluate`).
- A stable `optimizer_examples` snapshot populated from an existing eval/golden
  corpus or another approved content-bearing source.
- Candidate execution, deterministic score coverage, failure floors, the
  candidate-versus-incumbent promotion gate, append-only attempt audit, and the
  dry-run approval/canary handoff.
- P0a optimizers: **`SimplePromptOptimizer` and `GEPARootAgentPromptOptimizer`
  only.** `GEPARootAgentOptimizer` (root/skill surface, ADK 2.4.0+) is
  capability-gated fast-follow.

**P0b automates telemetry mining** after the bridge is proven: scheduled
detection over deduplicated OTel views and BQAA projections into
`flywheel_candidates`, eligibility/curation, representativeness checks, and
materialization into `optimizer_examples`.

**Fast-follow:** LangGraph adapter; sub-agent/tool-definition optimization; live
side-effectful tool replay; weighted multi-metric recipes and calibration
machinery for reference-free quality metrics; `bq://` resolver; root/skill
optimizer instrumentation; demo consolidation after P0a fixes the canonical API.

---

## 1. Framing — pattern from upstream, contracts owned here

`LocalEvalSampler` (`Sampler[UnstructuredSamplingResult]`) wraps `LocalEvalService`
(`perform_inference → evaluate`) and returns `UnstructuredSamplingResult(scores,
data)`. That's the **pattern**. This repo owns: the BigQuery→ADK eval bridge, the
scoring policy, replay metadata, holdout governance, the attempt audit, and the
promotion gate. ADK owns (per [google/adk-python#6357](https://github.com/google/adk-python/issues/6357)):
run-scoped optimizer usage accounting, budgets, cooperative cancellation, and
capability declarations.

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

P0a composition (prompt-only optimizers; the run context is the #6357 seam):

```python
optimizer = GEPARootAgentPromptOptimizer(...)   # or SimplePromptOptimizer
sampler = BigQuerySampler(...)
result = await optimizer.optimize(agent, sampler, run_context=run_context)
```

`candidate` is a **modified `Agent`** → historical telemetry alone cannot score
it; scoring runs the candidate through an ADK runner/eval path. No optimizer in
the verified matrix modifies sub-agent prompts.

---

## 2. ADK compatibility tiers (two floors, dual preflight probe)

> BQAA retains a functional compatibility floor of `google-adk>=1.31.1`.
> Governed optimization and automated promotion require the first ADK release
> containing run-context usage, budget, and cancellation enforcement
> ([google/adk-python#6357](https://github.com/google/adk-python/issues/6357)).
> On earlier compatible ADK versions, evaluation may remain available, but the
> adapter must report `unsupported_adk_capability` and reject promotion;
> `optimize()` on such versions requires an explicit ungoverned-mode opt-in
> recorded in the attempt. P0a is production-complete only when its governed
> path is tested against the enforcement-capable ADK floor.

- **Functional floor (`google-adk>=1.31.1`):** sampler, scoring, gate, and audit
  all work. Optimizer-owned spend is unobservable by construction; attempts
  record `usage_observability: unsupported`.
- **Governed-promotion floor (first release with the #6357 seam):** verified
  usage, enforced call/token budgets, cooperative cancellation.
- **Dual preflight probe:** (a) does `google.adk.optimization.sampler` import at
  all (actionable unsupported-version error before any BigQuery work or attempt
  creation); (b) does the run-context API exist *and* what capabilities does the
  chosen optimizer instance actually report. Absence of the run-context API maps
  to conservative unsupported capabilities — the functional tier — never to an
  error.
- **Below-floor `optimize()` is explicit, never implicit:** it requires
  `ungoverned_mode=true`, persisted on the attempt row, so an ungoverned run can
  never be mistaken for a budget-governed one in the audit trail.

---

## 3. Telemetry source (P0b) — deduplicated views first, raw tables as provenance

Candidate detection reads deduplicated views, **not raw tables**:

- **OTel-native (#316/#317):** `*_dedup` views over `otel_logs` / the five
  `otel_metric_*` tables / gated `otel_spans`, plus `agent_events_otlp`. Bind a
  **minimum `otel_schema_version`** — #318 mining SQL references #316 shapes, so
  a schema bump is a versioned, fail-fast dependency.
- **BQAA projections / legacy:** `bqaa_metrics`, existing `agent_events` rows.
- Either source works independently; a deployment never requires both.

Raw OTel tables remain **provenance sources**: every curated example carries
`telemetry_provenance` (`trace_id`, `span_id`, log/metric-point refs, native
table + `otel_schema_version`, #316 `idempotency_key` / `source_position`, or
BQAA projection refs) so audit refs point at exact native rows.

### 3.1 The replay/privacy precondition (P0b's sharpest cross-cutting risk)

With `OTEL_LOG_USER_PROMPTS` off (the default) and Codex `log_user_prompt=false`,
the prompt in `otel_logs` is `<REDACTED>` / length-only — **you cannot
reconstruct `input_payload` from default-privacy telemetry.** Consequences:

- Replayable mined examples require content-bearing telemetry at capture time —
  a documented deployment precondition, policy-governed and opt-in.
- Examples without a reconstructable input are `eligibility.runnable=false`
  (`skip_reason="content_redacted"`) and never become optimizer IDs (§5.1).
- A default-privacy deployment yields near-zero *mined* runnable examples;
  detection and outcome signals still work. **P0a is unaffected** — its corpus
  is curated, which is exactly why the P0a/P0b split exists.

---

## 4. Three tables, three jobs

| Table | Phase | Owner job | Role |
|-------|-------|-----------|------|
| `optimizer_examples` | P0a | curation job/snapshot | **stable, immutable** runnable eval cases + split assignment; only rows that passed replay/policy/identity eligibility |
| `flywheel_attempts` | P0a | wrapper | append-only record of every optimizer/candidate attempt and promotion decision (see §7) |
| `flywheel_candidates` | P0b | scheduled detection | raw mined candidates including ineligible/excluded rows + reasons; volatile; full eligibility funnel |

This package owns DDLs/migrations, partitioning/clustering, retention, upgrade
checks, IAM, and teardown for all three. `get_train/validation_example_ids` read
`optimizer_examples` filtered to `eligibility.runnable=true` from an **immutable
run snapshot** — churn cannot change a run mid-flight, and the scores-coverage
invariant (§5.1) holds by construction.

**Identity:** rows and examples use tenant/application/run-aware composite
identity plus #316 native-row identity. Repeated mining and retries are
idempotent; reused session ids never merge producers or runs (trust invariant 6).

---

## 5. The BigQuery→ADK eval bridge

Each `optimizer_examples` row is **executable**, not just descriptive:

```jsonc
{
  "example_id": "stable-uuid", "split": "train|validation|promotion_holdout",
  "input_payload": { /* curated (P0a) or reconstructed (P0b, §3.1) user content */ },
  "app_name": "nl2sql_agent",
  "eval_recipe": { "primary": {"name":"sql_execution_success","type":"reference_grounded",
                               "reference":"expected_result_ref","on_missing":0.0},
                   "floors": [{"name":"no_side_effects","type":"deterministic_veto"}],
                   "advisory": [{"name":"helpfulness","type":"reference_free"}],
                   "on_run_failure": 0.0 },
  "replay": { "tools":[...], "auth":{...}, "side_effects":"isolated",
              "fixtures":{...}, "determinism":{"temperature":0,"seed":42,"clock":"frozen:..."}, "timeout_s":120 },
  "telemetry_provenance": { ... },   // §3; null provenance for curated P0a rows
  "labels": {...}, "eligibility": { "runnable": true, "skip_reason": null }
}
```

Replay executes in the required sandbox: isolated non-production credentials,
fixtures/recorded results or approved read-only tool adapters, network egress and
side-effectful tools denied by default. An unsafe or incomplete replay contract
fails eligibility **before** optimizer invocation.

### 5.1 Scores-coverage invariant (do not omit requested IDs)

`result.scores` **must contain a float for every requested ID** — ADK/GEPA index
`scores[example_id]` directly; a missing key crashes the run
([google/adk-python#6004](https://github.com/google/adk-python/issues/6004) tracks
the upstream behavior; this sampler satisfies the stronger invariant regardless):

1. **Eligibility filtered at ID-listing time (§4)** — non-runnable examples never
   become optimizer IDs.
2. **Run-time failure floor** — an example that fails *during* scoring gets
   `eval_recipe.on_run_failure` (default `0.0`).
3. **Post-condition:** `set(scores) == set(requested_ids)` before return — this
   holds on every path, including budget-exceeded aborts (§7.2).

### 5.2 Run-failure metadata is NOT gated on `capture_full_eval_data`

On a run failure, regardless of the flag: (a) emit minimal
`.data[id] = {"run_failed":true,"reason":"..."}`, and (b) write the failure to
`flywheel_attempts` as a backstop. Only heavy payload (inputs, trajectory, tool
outputs, per-metric detail) stays gated behind `capture_full_eval_data=True`.
A floor `0.0` must never be indistinguishable from a genuine metric `0.0`.

---

## 6. Scoring policy — reference-grounded primary; deterministic floors; advisory extras

- **P0a has one versioned, independently reference-grounded primary
  quality/correctness scorer.** The expected answer, authoritative tool result,
  explicit fixture outcome, or independently authored behavioral predicate must
  actually reach the evaluator, and the receipt proves which evidence was used.
- **Deterministic observed-execution, safety, and cost constraints may be
  independent promotion floors or vetoes** without any calibration program —
  e.g. span-level behavioral predicates ("a tool call occurs in the
  post-correction segment") computed from real traces, never inferred from judge
  classifications (trust invariant 7).
- **Reference-free quality metrics are advisory in P0a** and cannot affect
  promotion eligibility or ranking. Calibration machinery is fast-follow.
- Missing *metric* → `on_missing`; *run failure* → `on_run_failure`; neither
  omits the key. Evaluator code/SQL hash, dependencies, inputs, raw per-example
  outcomes, and aggregation inputs are persisted per attempt.
- SQL evaluators are parameterized, read-only, versioned, restricted to approved
  views/templates, with dataset allowlists, dry-run validation, timeouts and
  maximum-bytes-billed. Every query uses timestamp partition bounds.

---

## 7. Orchestration: usage, budgets, watchdog, gate, audit, handoff

The wrapper runs `await optimizer.optimize(initial_agent, sampler,
run_context=ctx)` and owns everything below.

### 7.1 Usage accounting (two ledgers, honest coverage)

- **Optimizer-owned calls:** the #6357 `OptimizationRunContext` ledger —
  logical-call events, provider-reported tokens, coverage
  (`verified`/`partial`/`unreported`, never zero-coerced).
- **Candidate/evaluator calls:** owned by this sampler's Runner/eval path.
- `flywheel_attempts` records both ledgers, BigQuery bytes processed/billed,
  elapsed time, usage-observability coverage, and — when a price catalog is
  configured — estimated cost with catalog provenance. Total usage is labeled
  `verified` only when **both** paths are observable. Each optimizer adapter
  declares whether its internal calls are observable; a policy requiring
  verified total usage rejects partially observable paths at preflight.

### 7.2 Budgets and the two-stage wall-clock watchdog

- Call/token ceilings ride the run context (#6357): overshoot commits the final
  call, then terminates per `on_budget_exceeded: raise | return_partial`
  (default `raise`); `return_partial` results carry
  `terminal_status="budget_exceeded"` and are **never promotion-eligible**.
  Budget exhaustion records `budget_exceeded`, makes the attempt ineligible, and
  assigns the declared failure floor + metadata to every unprocessed requested
  ID whenever a sampler result is returned (§5.1 holds).
- **Wall-clock is BQAA-owned, two-stage — a single `asyncio.timeout()` around
  `optimize()` would reverse the intended ordering:**
  1. a **soft deadline** timer calls `ctx.request_cancel("deadline")` — the
     cooperative window;
  2. a **bounded grace period** lets in-flight calls settle and the GEPA worker
     exit at the next boundary;
  3. **native task cancellation** (`asyncio.timeout` / `task.cancel()`) as the
     hard backstop only if the grace period expires.
  The attempt records `deadline_exceeded` with the stage that fired.

### 7.3 Promotion gate and holdout governance

- Candidate and incumbent run on the **same immutable held-out IDs and
  fixtures** in the same attempt (never against a stored historical incumbent
  score — baselines drift). Exact ID/count equality, complete coverage, minimum
  paired improvement, critical-slice non-regression floors, and
  **tie-keeps-incumbent** are enforced before approval handoff.
- Sealed holdout: optimizer identities receive no holdout inputs/outcomes;
  detailed results are audit-restricted; attempts per holdout generation are
  capped. **In P0a, budget exhaustion without a rotation source blocks further
  promotion attempts until an operator refreshes the corpus — the holdout is
  never silently reused.**

### 7.4 Incumbent CAS handoff

The handoff binds the approved candidate digest to the evaluated incumbent
digest or registry generation. Immediately before promotion, the downstream
actor **atomically compare-and-sets** its canonical incumbent (a
`flywheel_incumbents` record with a generation counter); a mismatch records
`stale_incumbent` and requires fresh evaluation. A downstream system without an
atomic CAS receives an advisory, non-promotable handoff. For skill candidates,
the winner is mirrored into Skill Registry as a new immutable revision **after**
the CAS succeeds — the registry is the immutable artifact store, not the CAS
authority (no documented conditional-update precondition today).

### 7.5 Audit

Every attempt writes `flywheel_attempts`: rejected candidates, failed runs,
exclusions, source refs, corpus snapshot, candidate/incumbent digests,
evaluator/fixture/model/config versions, per-example results, both usage
ledgers, budget/watchdog status, `ungoverned_mode` flag, approval state, and
canary handoff state — plus a content-addressed run manifest reproducible
independently of mutable source tables. P0a never auto-applies a candidate;
approval records approver identity, role separation, expiry, and exact digest.
The optimizer cannot approve its own output.

---

## 8. End-to-end sequence (P0a reference journey)

```text
 1. bootstrap: create/validate tables, IAM, budgets, schema versions
 2. preflight: probe ADK optimization API + run-context capability;
    resolve tier (governed | functional+ungoverned-opt-in | reject)
 3. snapshot: freeze optimizer_examples run snapshot (train/validation/holdout)
 4. attempt row: open flywheel_attempts entry (ids, config, digests, tier)
 5. optimize: optimizer.optimize(agent, BigQuerySampler, run_context)
    with call/token budgets + two-stage watchdog armed
 6. evaluate: candidate AND incumbent on the same sealed holdout snapshot
 7. gate: coverage == exact, paired improvement >= min, slice floors hold,
    tie -> incumbent kept
 8. receipt: content-addressed manifest persisted; both usage ledgers final
 9. handoff: approval_required with candidate + expected-incumbent digests
10. promote (downstream): atomic CAS on flywheel_incumbents;
    mismatch -> stale_incumbent -> re-evaluate; skill winners mirrored to
    Skill Registry post-CAS
```

The reference journey must demonstrate an accepted and a rejected candidate, a
run failure without missing coverage, repeated-run idempotency, and full
decision reconstruction from persisted artifacts alone.
