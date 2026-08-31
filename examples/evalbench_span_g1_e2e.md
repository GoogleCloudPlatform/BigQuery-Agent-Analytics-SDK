# Span-G1 e2e: which span died, on the widget-stock session

This is a live walkthrough for the team
([#466](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/466),
parent
[#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)):
one real failed session, told as a story a presenter reads aloud. A
customer asked a support agent how many widgets are in stock, the agent
went silent, session-level `failed_sessions` + the frozen G1 taxonomy
names the failure — and the
[PR #467](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/467)
`bigquery_agent_analytics.span_taxonomy` library localizes every frozen
category onto the real native `span_id` where the trace died:
`AGENT_STARTING` span `b7ad6b7169203331`, `target_kind="gap_after_span"`.
No EvalBench `configs`/`results`/`scores` tables are read anywhere in the
path. A teammate should understand the failure in 60–90 seconds of
reading the output, then see **which span to inspect**.

Run it, then use this file as speaker notes:

```bash
bash examples/evalbench_span_g1_e2e.sh --fixture
```

`--fixture` is the only mode: offline, no BigQuery, no live judge, exit
`0`. Any other invocation prints one line saying so and exits `2`
without running anything (there is no `--synth` here, and the
`EVALBENCH_FIXTURE` environment variable is not read).

This is the native-path analog of the native freeze demo
([PR #465](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/465)):
same session, same six-act shape, but the load-bearing new act consumes
[PR #467](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/467)
(the span taxonomy library) instead of PR #464 (the native writer, which
stays as already-landed context here). PR #467 is deliberately a **pure
library** — `label_failed_session_spans` / `label_native_run`, no CLI —
and this demo adds **no new CLI** around it; act 4 shows the library's
sample output as JSON. The `evalbench-import` adapter
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
| last existing span | `AGENT_STARTING`, span_id `b7ad6b7169203331` |

The trace/span ids are the fixture stand-ins pinned by
`tests/test_span_taxonomy.py`, in the OTel hex shape the ADK plugin
writes; the contract is that emitted span ids are always drawn **from**
`agent_events` rows, never invented by the attribution layer.

## The six acts, in banner order

1. **The customer asked. The agent went silent.** Introduce the session:
   who the agent is, what the customer asked, the session and eval ids.
   No jargon yet — this is a support ticket that went unanswered. No
   "EvalBench" in the customer sentence.

2. **What the trace shows.** The events live in production
   `agent_events` — the source of truth. `USER_MESSAGE_RECEIVED` →
   `INVOCATION_STARTING` → `AGENT_STARTING` → silence. **Name the last
   existing span out loud**: `AGENT_STARTING`, span_id
   `b7ad6b7169203331` — the whole demo lands on it. No `check_inventory`
   tool call, no `LLM_RESPONSE`, no `AGENT_COMPLETED`. Sibling session
   `ab7535a5` answered *"There are 0 widgets in stock."* — the agent
   **can** do this; this session just never did.

3. **Session-level G1 names the failure — the denominator, unchanged.**
   Already-landed context (PR #464's `evalbench-native-import`
   snapshotted this trace; not the new act): the `failed_sessions` row
   for import_version `v1` — 1 of 7 sessions failed, mechanical flags
   `process_failed` / `missing_completion` / `score_failed` all true,
   `failing_scores` `goal_completion 0.0`, and the frozen labels:

   ```json
   "taxonomy_categories": ["task/planning", "finalization", "tool blockers"]
   ```

   Give the import identity as one pasteable string:
   `evalbench-native-import:mvp-e2e-real-traces:v1:7e352c34`. **Say the
   loud line:** this is still the session-level denominator; span labels
   localize, they never classify a session or replace
   `failed_sessions` + G1. Taxonomy frozen at v0.1.0.

4. **Span-level G1 localizes it — which span died (PR #467).** The new
   act. `span_taxonomy` is a pure library, no CLI:
   `label_native_run(run)` over the PR #464 `NativeAgentEventsRun`, or
   `label_failed_session_spans(events, verdict, ...)` per session. Show
   the sample JSON: three `SpanFailureLabel` rows, one per tripped frozen
   category in frozen order, **all anchored to the same real span** —
   span_id `b7ad6b7169203331`, `target_kind="gap_after_span"` — with
   evidence stating that no `TOOL_STARTING` followed, `check_inventory`
   was never called (the completed sibling called it), and no
   `AGENT_COMPLETED` followed. Mention the RFC #435 Phase 2 tuple shape,
   `SpanFailureLabel.as_tuple()`:
   `(trace_id, span_id, failure_category, evidence, confidence)`;
   confidence 1.0 is `MECHANICAL_CONFIDENCE` — checkable facts, not
   judged probabilities. No synthetic span identifiers: the silence case
   is a gap marker anchored to the real last span; a row without a
   `span_id` fails closed. **Say the loud line:** the
   `AGENT_STARTING` → silence punchline is now an inspectable localized
   row on a real native `span_id`.

5. **A live judge would still miss this.** Keep the trap short:
   correctness 1.0, `llm_feedback` null, `pass_rate` 1.0 over the 7
   pinned sessions — nothing to judge. Then come straight back to the
   span row in act 4: *that* is the thing a teammate can paste into a
   ticket — category, evidence, and the exact span to open.

6. **Punchline.**

   > This widget-stock session failed because the agent never answered
   > (goal_completion=0.0). Session-level G1 still names it
   > task/planning, tool blockers, and finalization. Span-level G1
   > localizes all three to AGENT_STARTING span b7ad6b7169203331
   > (gap_after_span) — it died before check_inventory was ever called.
   > Next debugging action: inspect that span.

   Then the native point, in one breath: **we did not need EvalBench
   tables — native `agent_events` + `span_taxonomy` was enough.**

D4 stays fail-closed: Hai-Yuan Cao is the only named report consumer; a
fixture can never produce a funding rec. **The six-week clock has NOT
started.**

## Related

- [`examples/evalbench_mvp_e2e.md`](evalbench_mvp_e2e.md) — the merged
  MVP e2e on the same session.
- [`docs/evalbench.md`](../docs/evalbench.md) — data flow, the
  failed-session contract (W0.4), judge scoring, CLI.
- [`docs/week0_g1_taxonomy.md`](../docs/week0_g1_taxonomy.md) — the
  frozen G1 record the session-level labels come from.
- [PR #467](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/467)
  — the span taxonomy library this demo narrates
  (`span_taxonomy.label_failed_session_spans` / `label_native_run` /
  `SpanFailureLabel`).
- [PR #464](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/464)
  — the native `agent_events` snapshot writer (already-landed context in
  act 3).
- [PR #465](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/465)
  — the native freeze demo this one mirrors.
- Issues
  [#466](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/466)
  (span-level G1 on span_id),
  [#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)
  (EvalBench import bridge / AgentForensics MVP), and
  [#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
  (the adapter on-ramp, which stays).
