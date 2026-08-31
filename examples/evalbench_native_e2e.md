# Native freeze e2e: team demo on the widget-stock session, no EvalBench tables

This is a live walkthrough for the team
([#463](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/463),
parent
[#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)):
one real failed session, told as a story a presenter reads aloud. A
customer asked a support agent how many widgets are in stock, the agent
went silent, and BQAA's **native** snapshot path — the
[PR #464](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/464)
`bq-agent-sdk evalbench-native-import` writer — finds that session
straight from production `agent_events` and names the failure with the
frozen G1 taxonomy. No EvalBench `configs`/`results`/`scores` tables are
read anywhere in the path. A teammate should understand the failure in
60–90 seconds of reading the output, then see how BQAA names it.

Run it, then use this file as speaker notes:

```bash
bash examples/evalbench_native_e2e.sh --fixture
```

`--fixture` is the only mode: offline, no BigQuery, no live judge, exit
`0`. Any other invocation prints one line saying so and exits `2`
without running anything (there is no `--synth` here, and the
`EVALBENCH_FIXTURE` environment variable is not read).

The Week 0 freeze (partner, D4 boundary, G1 taxonomy v0.1.0 —
`docs/week0_*.md`) already landed; this demo leans on it but does not
re-teach it. This is the native-path analog of the adapter-path freeze
demo
([PR #462](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/462)):
same session, same six acts, different entrance — the snapshot starts
from native `agent_events`, not from EvalBench tables. The
`evalbench-import` adapter
([#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97))
stays as an optional on-ramp; this demo never calls it. **The six-week
clock has NOT started.** It starts only when the first Week 1 snapshot
job is kicked, not at this demo.

## The session (same protagonist as the merged MVP e2e demo)

Real trace living in
`test-project-0728-467323.bqaa_e2e_real.agent_events` (spoken about, not
queried live), snapshotted as native job `mvp-e2e-real-traces`:

| | |
|---|---|
| agent | `support_agent` |
| system prompt | terse support agent; use tools for inventory/tickets; one-sentence answers |
| user | `real-user-0` |
| asked | `How many widgets are in stock?` |
| session_id | `7e352c34-4c1c-4395-acd5-fb3c8f215346` |
| eval_id | `7e352c34` (first 8 chars of session_id) |

## The six acts, in banner order

1. **The customer asked. The agent went silent.** Introduce the session:
   who the agent is, what the customer asked, the session and eval ids.
   No jargon yet — this is a support ticket that went unanswered. No
   "EvalBench" in the customer sentence.

2. **What the trace shows.** The events live in production
   `agent_events` — the source of truth. `USER_MESSAGE_RECEIVED` →
   `INVOCATION_STARTING` → `AGENT_STARTING` → silence. No
   `check_inventory` tool call, no `LLM_RESPONSE`, no `AGENT_COMPLETED`.
   Sibling session `ab7535a5` answered *"There are 0 widgets in
   stock."* — the agent **can** do this; this session just never did.
   Land the human problem: a stock question with no answer.

3. **Snapshot the failure straight from agent_events.** Step 1:
   `evalbench-native-import` — the exact PR #464 invocation
   (`--source-table … agent_events`, `--session-id`, `--location US`,
   `--snapshot-at`, `--import-version v1`,
   `--min-score goal_completion=1.0`), with sample output shaped like
   the real command: `status: imported`, 27 events + 7 score rows under
   `import_version v1`, events/scores/manifest tables and the
   `evalbench_failed_sessions` view on
   `test-project-0728-467323.bqaa`. **Say the loud line:** the source is
   production `agent_events`, read-only; EvalBench
   `configs`/`results`/`scores` are not read. The only synthesized value
   is the deterministic `goal_completion` score — 1.0 iff the session
   logged `AGENT_COMPLETED` (*completed*, not *passed*).

4. **failed_sessions finds the one that never answered.** Step 2:
   `evalbench-failed-sessions --format json` (JSON, not the table
   format — the table omits `taxonomy_categories`). 1 of 7 sessions
   failed: the mechanical flags `process_failed`, `missing_completion`,
   and `score_failed` are all true, `failing_scores` shows
   `goal_completion 0.0`, and the same row carries the frozen labels:

   ```json
   "taxonomy_categories": ["task/planning", "finalization", "tool blockers"]
   ```

   Read them in plain English, not as a taxonomy lecture:

   - `task/planning` — never decided to look up stock
   - `tool blockers` — never called `check_inventory`
   - `finalization` — never produced an answer

   Give the import identity as one string a teammate can paste into a
   ticket: `evalbench-native-import:mvp-e2e-real-traces:v1:7e352c34`
   (feature : job_id : import_version : eval_id). The published rows
   keep the **real** ADK `session_id`, so the trace in act 2 and this
   row are the same object. One short trust note here (not its own act):
   this is our real BQAA ADK pilot (not SANA/Strands). Fail-closed D4:
   Hai-Yuan Cao is the only named report consumer; a fixture can never
   produce a funding rec. Clock not started.

5. **A live judge would miss this.** Step 3: `evalbench-score` — the
   correctness judge scored the unanswered session 1.0 with
   `llm_feedback` null and `pass_rate` 1.0 over the 7 pinned sessions,
   because there was nothing to judge. That is why `failed_sessions`,
   not the judge, is the denominator.

6. **Punchline.**

   > This widget-stock session failed because the agent never answered
   > (goal_completion=0.0). G1 names it task/planning, tool blockers,
   > and finalization — it never planned the lookup, never called
   > check_inventory, never finished. Next debugging action: inspect why
   > the trace died after AGENT_STARTING before the inventory tool.

   Then the native point, in one breath: **we did not need EvalBench
   tables — native `agent_events` was enough.**

## Related

- [`examples/evalbench_mvp_e2e.md`](evalbench_mvp_e2e.md) — the merged
  MVP e2e on the same session (adapter import → failed-sessions → score,
  plus `--synth` and live modes).
- [`examples/evalbench_week0_full_idea.md`](evalbench_week0_full_idea.md)
  — the EXAMPLE Week 0 pack (Acme, pre-freeze), which stays
  illustrative.
- [`docs/evalbench.md`](../docs/evalbench.md) — data flow, the
  failed-session contract (W0.4), judge scoring, CLI.
- [`docs/week0_partner.md`](../docs/week0_partner.md),
  [`docs/week0_d4_memo.md`](../docs/week0_d4_memo.md),
  [`docs/week0_g1_taxonomy.md`](../docs/week0_g1_taxonomy.md) — the
  frozen Week 0 record this demo leans on.
- [PR #464](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/464)
  — the native `agent_events` snapshot writer this demo narrates
  (`native_events.NativeAgentEventsRun` /
  `bq-agent-sdk evalbench-native-import`).
- [PR #462](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/462)
  — the adapter-path freeze demo this one mirrors.
- Issues
  [#463](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/463)
  (native writer / EvalBench-adapter exit ramp),
  [#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)
  (EvalBench import bridge / AgentForensics MVP), and
  [#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
  (the adapter on-ramp, which stays).
