# Post-freeze Week 0 e2e: the real freeze on the widget-stock session

The Week 0 freeze landed for real
([#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)):
the pilot partner is frozen in
[`docs/week0_partner.md`](../docs/week0_partner.md), the fail-closed D4
boundary in [`docs/week0_d4_memo.md`](../docs/week0_d4_memo.md), and the
G1 taxonomy v0.1.0 in
[`docs/week0_g1_taxonomy.md`](../docs/week0_g1_taxonomy.md). This demo
replays that freeze as one recordable story on the same widget-stock
failed session as the merged MVP e2e demo
([`examples/evalbench_mvp_e2e.md`](evalbench_mvp_e2e.md)).

**This is the REAL freeze demo, not the EXAMPLE Acme pack.** The Week 0
example pack ([`examples/evalbench_week0_full_idea.md`](evalbench_week0_full_idea.md))
remains illustrative — its partner is fictional and its fixtures keep
`example: true` / `g1_frozen: false`. Everything printed by this demo is
the frozen record.

**The six-week clock has NOT started.** It starts only when the first
Week 1 snapshot job is kicked, not at the freeze and not at this demo.

```bash
bash examples/evalbench_week0_freeze_e2e.sh --fixture
```

`--fixture` is the only mode: offline, no BigQuery, no live judge, exit
`0`. Any other invocation prints one line saying so and exits `2`
without running anything (there is no `--synth` here, and the
`EVALBENCH_FIXTURE` environment variable is not read).

## The protagonist (same session as the MVP e2e demo)

A terse support agent was asked how many widgets are in stock and never
answered. Real trace from `bqaa_e2e_real.agent_events` (project
`test-project-0728-467323`), EvalBench job `mvp-e2e-real-traces`:

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

## The story, in order

The fixture prints these under `=== ... ===` banners, in this order:

1. **Partner: Google Cloud BQAA (this SDK).** The real pilot partner is
   Google Cloud BigQuery Agent Analytics itself — self-hosted: the ADK
   `support_agent` traces in
   `test-project-0728-467323.bqaa_e2e_real.agent_events`, imported as
   EvalBench job `mvp-e2e-real-traces`. AgentForensics is SANA-adjacent
   published work, **not a SANA fork** and not a named collaboration with
   SANA authors; SANA is LakeQA + KramaBench on Strands, this pilot is
   ADK+EvalBench on widget-stock support, so it is not duplicating those
   benchmarks. Frozen record: `docs/week0_partner.md`.

2. **D4: fail-closed memo for this pilot.** The boundary covers exactly
   this pilot's data — project `test-project-0728-467323`, datasets
   `bqaa_e2e_real`, `bqaa_evalbench_mvp_demo`,
   `bqaa_evalbench_mvp_mirror` — with one named report consumer,
   Hai-Yuan Cao (`caohy1988` / `haiyuan-eng-google`). Unnamed means
   denied. `--fixture` validates ingestion, taxonomy mechanics, and
   stability ONLY; it can never produce a Part II funding
   recommendation. Frozen record: `docs/week0_d4_memo.md`.

3. **G1 freeze: taxonomy v0.1.0.** Production `failure_taxonomy.py` is
   frozen — `taxonomy_version: 0.1.0`, `g1_frozen: true`,
   `clock_started: false`. Mechanical mapping until the labeler study:
   `missing_completion` → `finalization`, `process_failed` →
   `tool blockers`, `score_failed` → `task/planning`, with names
   returned in frozen order (the SANA-neighborhood seven, then
   `unknown`), not flag order. Freezing G1 is not a clock start. Frozen
   record: `docs/week0_g1_taxonomy.md`.

4. **The session**, then **What happened** — the widget-stock trace
   above, same as the MVP e2e demo.

5. **Step 1: `evalbench-import`** — sample output shaped like the real
   command: `status: imported`, 7 scenarios → 27 events + 7 score rows,
   `failed_sessions_view` pinned to `v1`.

6. **Step 2: `evalbench-failed-sessions --format json`** — the one
   failed row of 7 (the W0.4 denominator), with the mechanical flags
   still true **and** the frozen labels attached:

   ```json
   "taxonomy_categories": ["task/planning", "finalization", "tool blockers"]
   ```

   JSON, not the table format, because the table historically omitted
   `taxonomy_categories`.

7. **Step 3: `evalbench-score`** — the same W0.4 punch as the MVP demo:
   the imported `goal_completion` is 0.0, yet the correctness judge
   scored the unanswered session 1.0 with `llm_feedback` null (there was
   no answer to judge). That is why `failed_sessions`, not the judge, is
   the W0.4 denominator.

8. **Punchline:**

   > This widget-stock session failed because the agent never answered;
   > goal_completion=0.0. G1 frozen labels are task/planning,
   > finalization, tool blockers.

## Related

- [`examples/evalbench_mvp_e2e.md`](evalbench_mvp_e2e.md) — the merged
  MVP e2e on the same session (import → failed-sessions → score, plus
  `--synth` and live modes).
- [`examples/evalbench_week0_full_idea.md`](evalbench_week0_full_idea.md)
  — the EXAMPLE Week 0 pack (Acme, pre-freeze), which stays
  illustrative.
- [`docs/evalbench.md`](../docs/evalbench.md) — data flow, the
  failed-session contract (W0.4), judge scoring, CLI.
- [`docs/week0_partner.md`](../docs/week0_partner.md),
  [`docs/week0_d4_memo.md`](../docs/week0_d4_memo.md),
  [`docs/week0_g1_taxonomy.md`](../docs/week0_g1_taxonomy.md) — the
  frozen Week 0 record this demo replays.
- Issues
  [#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)
  (EvalBench import bridge / AgentForensics MVP) and
  [#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
  (LLM-judge scoring of EvalBench runs).
