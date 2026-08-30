# EXAMPLE Week 0 scenario pack: the full AgentForensics idea on one failed session

**This is an EXAMPLE pack — every artifact in it is illustrative, not a
freeze.** No partner freeze, no G1 freeze, no preregistration freeze:
`g1_frozen` is **false** everywhere, the six-week clock has **not**
started, and no real partner is named (the example partner is *Acme
Retail Support*). Week 0 of
[`docs/agentforensics_mvp_plan.md`](../docs/agentforensics_mvp_plan.md)
remains human-gated.

> **Predates the G1 freeze
> ([#461](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/461)):**
> this walkthrough is the pre-freeze illustrative story. Production has
> since frozen taxonomy **v0.1.0** (`g1_frozen: true`) and
> `failed_sessions` now emits the frozen names (`task/planning`,
> `finalization`, `tool blockers`) in `taxonomy_categories` — not the
> mechanical flag ids shown below. Only this example pack keeps the
> pre-freeze behavior.

What it does show: all **five Week 0 human gates** of the plan of record
([#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435))
worked through as **one concrete story** on the widget-stock failed
session from the e2e demo — so a reader can see, end to end, what
AgentForensics is *for* before any human clears a gate for real.

```bash
bash examples/evalbench_week0_full_idea.sh --fixture
```

`--fixture` (or `EVALBENCH_FIXTURE=1`) is the only mode: offline, no
BigQuery, no network, no live judge, exit `0`. Any other invocation
prints one line saying so and exits `2` without running anything. The
facts printed under each banner come from the JSON files in
[`examples/fixtures/`](fixtures/) (`week0_example_*.json`), each of
which carries `"example": true`, `"g1_frozen": false`,
`"clock_started": false`.

## The protagonist (same session as the e2e demo)

A terse support agent was asked how many widgets are in stock and never
answered. Real trace from `bqaa_e2e_real.agent_events` (reference
project `test-project-0728-467323`), EvalBench job `mvp-e2e-real-traces`:

| | |
|---|---|
| agent | `support_agent` |
| user | `real-user-0` |
| prompt | `How many widgets are in stock?` |
| session_id | `7e352c34-4c1c-4395-acd5-fb3c8f215346` |
| eval_id / scenario_id | `7e352c34` |
| events | `USER_MESSAGE_RECEIVED` → `INVOCATION_STARTING` → `AGENT_STARTING`, then silence |
| what never happened | no `check_inventory` call, no `LLM_RESPONSE`, no `AGENT_COMPLETED` |
| score | `goal_completion` 0.0 vs threshold 1 |

Sibling session `ab7535a5` answered *"There are 0 widgets in stock."* —
the agent could do it; this session just never did.

## The five gates, as this session's story

The fixture prints these in order, each under an `=== ... ===` banner;
`tests/test_evalbench_week0_full_idea.py` asserts the same order.

1. **`EXAMPLE — Week 0 is not a freeze. Clock has not started.`** — the
   disclaimer first: everything below is illustrative, `g1_frozen:
   false`, `clock_started: false`.
2. **`EXAMPLE partner + SANA relationship`** — example partner **Acme
   Retail Support**. AgentForensics here is **SANA-adjacent, not a SANA
   fork**: SANA is LakeQA + KramaBench on the Strands runtime with seven
   categories (task/planning, wrong source, execution/computation,
   incomplete evidence, turn-waste, finalization, tool blockers); this
   example pilot is ADK+EvalBench on widget-stock support. Taxonomy v0.1
   EXAMPLE seeds from those seven because the failure modes overlap —
   and it is not duplicating LakeQA/KramaBench, which study different
   benchmarks on a different runtime.
3. **`EXAMPLE runtime + route`** — the pilot traces already exist: the
   ADK plugin logged `support_agent` into `bqaa_e2e_real.agent_events`,
   folded into EvalBench job `mvp-e2e-real-traces`. Runtime is ADK
   plugin → BQAA `agent_events` → EvalBench-hosted, so the
   EvalBench-only MVP route is **CORRECT** for this example and D1 does
   not need re-decision.
4. **`EXAMPLE pilot-benchmark rubric`** — the benchmark is selected by
   the predeclared rubric, not import convenience, and each criterion is
   *scored* in the fixture: collaborator relevance (retail support
   inventory questions — pass), failure-mode coverage (silent
   non-completion — pass), score availability / threshold-definability
   (`goal_completion` 0.0 vs 1.0, threshold 1 — pass), ground-truth
   depth (sibling `ab7535a5`'s gold answer — pass), then trace fidelity
   (the three real ADK events, then silence — pass).
5. **`EXAMPLE D4 boundary memo`** — fail-closed, with **example** report
   consumers ("Alex Rivera (example collaborator)", "Jordan Lee (example
   collaborator)" — not real people). A `--fixture`/synthetic run
   validates **ingestion, taxonomy mechanics, and stability ONLY** and
   can never produce a Part II funding recommendation; without clearance
   the pilot runs on pre-redacted reference traces
   (`test-project-0728-467323`) or pauses. The stop/go memo is itself a
   governed artifact.
6. **`EXAMPLE preregistration (not a week-1 freeze)`** — the plan's
   floors and decision rules copied as an example: replicate agreement
   ≥80%, non-`unknown` coverage ≥80%, κ point ≥0.6 with CI lower ≥0.45,
   localization coverage ≥70%, hit@1 CI lower >0 and point uplift
   ≥ +10pp, plus the value-gate rubric, the noisy-small-n localization
   rule, the sealed `P-blind` set, and the reserved revision week.

Then the through-line the gates exist to serve:

7. **`This agent was asked to check widget stock. Here is the session.`**
   — the protagonist, verbatim.
8. **`This session in failed_sessions (mechanical taxonomy_categories)`**
   — the slice-9 consumer attaches
   `taxonomy_categories: ["process_failed", "missing_completion",
   "score_failed"]` to the row: which gates tripped, mechanically, not
   why the agent failed.
9. **`EXAMPLE mapping of mechanical flags onto SANA-seeded names`** —
   the judgment layer the MVP would build, shown as an EXAMPLE (**not
   G1**): `missing_completion` → *finalization* (never finalized an
   answer), `process_failed` → *tool blockers* (never called
   `check_inventory`), plus *task/planning* as an overlapping seed. This
   mapping lives only in
   [`fixtures/week0_example_taxonomy_seed.json`](fixtures/week0_example_taxonomy_seed.json)
   — never in `src/bigquery_agent_analytics/failure_taxonomy.py`, whose
   scaffold config stays `0.0.0-scaffold`, `g1_frozen: false`.
10. **`Punchline`** — *This widget-stock session failed because the
    agent never answered; goal_completion=0.0.*

> **Post-[#461](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/461)
> note:** items 8–9 describe the pre-freeze scaffold and predate the G1
> freeze. Production `failure_taxonomy.py` is now G1-frozen at v0.1.0
> (`g1_frozen: true`) and emits the frozen category names
> (`task/planning`, `finalization`, `tool blockers`) in
> `taxonomy_categories`, not the mechanical flag ids.

## What this pack is NOT

- It does **not** clear any Week 0 gate — those remain human decisions.
- It does **not** start the six-week clock, the partner job, the real D4
  boundary, live-trace ingestion, or the labeler study.
- It does **not** freeze taxonomy names: the SANA-seeded names appear
  only in the example fixture, and the production scaffold module is
  untouched.

## Related

- [`docs/agentforensics_mvp_plan.md`](../docs/agentforensics_mvp_plan.md)
  — the plan of record whose Week 0 gates this pack illustrates.
- `examples/evalbench_mvp_e2e.md` / `.sh` (PR
  [#455](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/455))
  — the e2e demo this pack's session comes from.
- [`docs/evalbench.md`](../docs/evalbench.md) — the failed-session
  contract and CLI reference.
- Issue
  [#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)
  — AgentForensics MVP tracking.
