# Week 0 — Real Pilot Partner (frozen)

**Status:** frozen by the #435 Week 0 freeze PR. This is the REAL partner
record, not the example pack (`examples/evalbench_week0_full_idea.md`, which
stays illustrative). The six-week clock has **not** started; it starts only
when the first Week 1 snapshot job is kicked, not at merge of this freeze.

Machine-readable copy: `examples/fixtures/week0_real_partner.json`
(`example: false`).

## Partner

**Google Cloud BigQuery Agent Analytics (this SDK / BQAA).** The pilot is
self-hosted: the ADK `support_agent` traces in
`test-project-0728-467323.bqaa_e2e_real.agent_events`, imported as EvalBench
job `mvp-e2e-real-traces`.

## Relationship to SANA

AgentForensics is **SANA-adjacent published work — not a SANA fork and not a
named collaboration with SANA authors**. SANA is LakeQA + KramaBench on the
Strands runtime, with a seven-category failure taxonomy (task/planning,
wrong source, execution/computation, incomplete evidence, turn-waste,
finalization, tool blockers). This pilot is ADK+EvalBench on widget-stock
support — a different benchmark and a different runtime — so it is
**not duplicating** published work on LakeQA or KramaBench. Taxonomy v0.1 starts
from SANA's seven categories per Week 0 item 1 (see
`docs/week0_g1_taxonomy.md`).

## Runtime and route (recorded as real, already true)

ADK plugin → BQAA `agent_events` → EvalBench-hosted. The partner's benchmark
runs are EvalBench-hosted, so the **EvalBench-only MVP route is correct and
D1 does not need re-decision**.

## Pilot-benchmark rubric (recorded as real)

The predeclared rubric selected the widget-stock support benchmark on
session `7e352c34-4c1c-4395-acd5-fb3c8f215346` (`7e352c34`): sibling gold
session `ab7535a5` answered "There are 0 widgets in stock." for the same
prompt, and `goal_completion` is 0 for the failed session vs 1 for the gold
against a definable threshold. Machine-readable copy:
`examples/fixtures/week0_real_rubric.json`.
