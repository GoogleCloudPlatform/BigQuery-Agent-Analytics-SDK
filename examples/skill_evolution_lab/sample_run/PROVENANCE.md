# Provenance — U6 workaround removal and this recording (#360, design #384)

This file is the durable evidence for the two staged cleanup states (AE7,
AE8) and the recording committed in this directory (AE9). Scratch datasets
were unique, labeled, carried a 7-day default table expiration, and never
referenced the standing `bqaa_hero_demo_20260708` dataset.

## AE7 — shared table, API judge (the #359-only state)

| Field | Value |
| --- | --- |
| Commit (clean tree) | `5dad0b5` |
| UTC window | 2026-07-28 18:04:04 → 18:12:39 (first/last persisted row) |
| Project / dataset | `agent-skill-lab-01` / `u6_ae7_20260728_180346` (label `u6:ae7`) |
| Table / run label | `agent_events` (only table) / `lab_20260728_180402_39256516193` |
| Command | `DATASET_ID=u6_ae7_… ./run_e2e_demo.sh --agent-model gemini-3.1-flash-lite` (one round) |
| Models | agent `gemini-3.1-flash-lite` (Vertex `global`); analyst `gemini-3.1-pro-preview` (`global`); judge `gemini-2.5-flash` @ `us-central1` (API judge — AE7 state) |
| Result | exit 0; V0 test 28.6% (20/70), V1 test 100.0% (70/70) — in-scope rates (70 = 55 single-turn + 15 corrections; overall rates use 80 incl. the 10 out-of-scope) |
| Persisted checks | slices v0_evolve 168r/68s, v0_test 216r/80s, v1_test 382r/80s — one run label, `app=skill-evolution-lab` everywhere, zero foreign rows; all 80 held-out ids reused across V0/V1 in ONE table with no span mixing (201 live span-tree lines, no `custom_tags.seeded`) |
| Teardown | deleted after evidence capture (see below) |

## AE8 — server-side golden-grounded judge (the #358 cleanup on the AE7 substrate)

Pre-flight smoke (against AE7 data): `--limit 2` server-side scoring returned
`execution_mode: ai_generate` with scoped selection — BigQuery `AI.GENERATE`
judging works in-project.

**Attempt 1 — failed, evidence retained.** Commit `9c0272b`, dataset
`u6_ae8_20260728_181934` (retained until diagnosis, then explicitly deleted —
the 7-day policy is a default *table* expiration and never had to fire). Exit 1 in
STEP 3: an unretried `429 RESOURCE_EXHAUSTED` from the embedding model inside
`match_golden_qa → _embed_texts`. Fix: commit `6d9c65d` (bounded per-batch
retry on 429/503, unit-tested). A subsequent attempt used a new dataset, per
the design.

**Attempt 2 — the gate run and the sole AE9 artifact source.**

| Field | Value |
| --- | --- |
| Commit (clean tree) | `6d9c65d` |
| UTC times | run start 2026-07-28 18:33:56, wall 14m03s (end ≈ 18:47:59, incl. post-persist compare/gate/restore); first → last persisted row 18:33:57 → 18:45:43 |
| Project / dataset | `agent-skill-lab-01` / `u6_ae8_20260728_183340` (label `u6:ae8`) |
| Table / run label | `agent_events` (only table) / `lab_20260728_183356_39969610161` |
| Command | `DATASET_ID=u6_ae8_… ./run_e2e_demo.sh --agent-model gemini-3.1-flash-lite --rounds 2` |
| Models | agent `gemini-3.1-flash-lite` (Vertex `global` — unchanged from the previous sample; the material migration is hybrid→server-side judging and per-slice→shared storage); analyst `gemini-3.1-pro-preview`; judge `gemini-2.5-flash` @ `us-central1`, server-side (`execution_mode: ai_generate` on all 5 scoring passes) |
| Score bounds | `--app-name skill-evolution-lab --label run=… --label slice=… --time-period 24h --limit 500` (cap attested by the logged command + report metadata; population stayed far below it) |
| Result | exit 0; V0 test 30.0% (21/70) → V1 98.6% (69/70) — in-scope rates (70 = 55 single-turn + 15 corrections); V2 95.7% in-scope — gate refused V2 on the overall rate (93.8% ≤ 98.8%, denominator 80 incl. the 10 out-of-scope), V1 kept |
| Persisted checks | 5 slices under exactly one run label (v0_evolve 166r/68s, v0_test 220r/80s, v1_evolve 256r/68s, v1_test 338r/80s, v2_test 332r/80s); `app=skill-evolution-lab` uniform; zero foreign rows; all 80 held-out ids reused across v0/v1/v2 test passes in ONE shared table with no event/transcript/context mixing; judge-context text absent from logs (U5 redaction) |
| Teardown | deleted after evidence capture (see below) |

## Shared U6 evidence — synthetic bounds probe (once per PR)

Run in the populated AE8 scratch dataset via the opt-in live test
`tests/test_trace_identity_bigquery_live.py::TestU6BoundsProbeLive`
(`BQAA_U6_PROBE_DATASET=u6_ae8_20260728_183340`): a dedicated non-demo label
received 3 recent traces, 1 same-label 48-hour-old trace, and 1 recent
foreign-label trace; a read-only selector with that label + a `24h` window +
`limit=2` returned exactly 2 recent correctly-labeled resolved traces and
neither sentinel. **PASSED** (1 passed, 17.45s) — label, time, and limit
enforcement proven without judging 500 synthetic sessions.

## AE9 — this recording

- Source: the AE8 attempt-2 gate run above (`runs/20260728_183356_gemini_3_1_flash_lite`),
  copied wholesale, with two mechanical sanitations: `run.log` path prefixes
  removed, and trailing console-padding whitespace stripped from `run.log` and
  three consolidation candidate files (cosmetic; every hashed artifact except
  `run.log` is byte-identical to the run output, and `run.log`'s hash below is
  post-sanitation). Artifact commit SHA: see the commit that introduced this
  file.
- Headline (RESULT.md): V0 35.0% (28/80) → V1 98.8% (79/80) overall;
  corrections 0/15 → 15/15 with parroted sub-trajectories 12 → 0;
  out-of-scope 7/10 → 10/10. Round 2: V2 95.7% — strict-win gate kept V1.
- Key artifact hashes (SHA-256):
  - `v1_skill.md` `e321f05f4e1e32b59d061a46c203a3f31c6fad0d6cb09934ad08e0d0aa247db4`
  - `v2_skill.md` `5b50d15c52e4a49cb3bc8801fed7f226d4bbadd5e659414e88135c3101d65114`
  - `RESULT.md` `6562e95ba73a4917ebea515ab31102dccb9908005ebdf8a8040aec198607fdc7`
  - `RESULT_ROUND2.md` `491db36d8ddbe4f584dbb12799443335829b383bd170c0db5ee0d4994894db30`
  - `run.log` `c29d955fb5b637dfa3d942851ab43e3a0dd59c7c9a35565fa6db57bbd61e28fc`
  - `v1_test_report.json` `f95fcef046bc3ba43e42e09b2f5a01ead55038d4d45e79481542c6eda4e8065b`
- Gates at the artifact commit: `bash -n run_e2e_demo.sh`; focused
  quality-report / compare-runs / skill-evolution suites; full offline suite;
  formatter + whitespace checks; exact-80 held-out population per pass
  (missing sessions fail, strays excluded, ties keep the incumbent —
  enforced by `compare_runs.py --questions --gate` during the run).
- `gist_update: published` — canonical Gist
  `evekhm/01c4673dddd68c1fd13383948eb7de35`, revision
  `ac58c73475652769f0cbda8e86f0f30055f625b6`, prepared from the AE9 artifact
  commit `c143f83` with sample_run links pinned to that SHA; SHA-256 of the
  published markdown:
  `3c9b3ba5fd04951d30c40ee24ac4266f3f3a707e9e5845c2929a63b6daacfb1a`.
  Post-publication addendum: revision `c72af8eb` (2026-07-28T23:43Z) changed
  three plugin-doc links (`adk.dev/integrations/…` → `adk.dev/observability/…`);
  the gist HEAD therefore drifts from the hash above by exactly those lines.
- Scratch teardown: `u6_ae7_20260728_180346`, `u6_ae8_20260728_181934`
  (failed attempt), and `u6_ae8_20260728_183340` all DELETED after the
  evidence above was captured and the Gist revision verified.
  **Retention gap, disclosed (review P1):** the raw SQL outputs behind the
  gate-day aggregate rows were captured in the operator session but not all
  committed before teardown. The retained gate-day outputs and a full
  re-verification on a seeded replica of this exact recording are in the
  appendix below.

## Appendix — retained and re-verified query evidence (2026-07-29)

**Retained from the gate day (2026-07-28, original scratch datasets).**
AE7 (`u6_ae7_…0346`, run label `lab_…180402_39256516193`):

```text
run_label,slice,app,rows_n,sessions_n
lab_20260728_180402_39256516193,v0_evolve,skill-evolution-lab,168,68
lab_20260728_180402_39256516193,v0_test,skill-evolution-lab,216,80
lab_20260728_180402_39256516193,v1_test,skill-evolution-lab,382,80

v0_test_ids,v1_test_ids,reused_ids,first_row,last_row
80,80,80,2026-07-28 18:04:04,2026-07-28 18:12:39
```

AE8 (`u6_ae8_…3340`, run label `lab_…183356_39969610161`):

```text
run_label,slice,app,rows_n,sessions_n
lab_20260728_183356_39969610161,v0_evolve,skill-evolution-lab,166,68
lab_20260728_183356_39969610161,v0_test,skill-evolution-lab,220,80
lab_20260728_183356_39969610161,v1_evolve,skill-evolution-lab,256,68
lab_20260728_183356_39969610161,v1_test,skill-evolution-lab,338,80
lab_20260728_183356_39969610161,v2_test,skill-evolution-lab,332,80

reused_all3,first_row,last_row
80,2026-07-28 18:33:57,2026-07-28 18:45:43
```

**Re-verified on a seeded replica (2026-07-29).** The five committed
traffic/report pairs were seeded into fresh expiring scratch
`agent-skill-lab-01.u6_evid_20260729_022933` (7-day expiration, label
`u6:evidence`) via `run_agent.py --seed-bigquery <traffic> --seed-report
<report>` under the original run/slice labels — the replica reproduced the
gate-day row counts exactly (166/220/256/338/332). Captured outputs:

```text
== per-slice cardinalities
slice,rows_n,sessions,users,root_agents,scope_payloads
v0_evolve,166,68,1,1,1
v0_test,220,80,1,1,1
v1_evolve,256,68,1,1,1
v1_test,338,80,1,1,1
v2_test,332,80,1,1,1

== foreign-row exclusion (rows outside app or run label)
foreign_rows,total_rows
0,1312

== reused held-out ids across v0/v1/v2 test slices
reused_in_all_3
80

== SDK isolation probe (reused id t28_benefits, present in all 3 test passes)
bare get_session_trace('t28_benefits') -> AmbiguousSessionError,
  candidates=3, retry_dimensions=['custom_labels', 'scope_signature']
scoped (v0_test): 1 trace, spans=2, scope_labels=v0_test
scoped (v1_test): 1 trace, spans=6, scope_labels=v1_test
scoped (v2_test): 1 trace, spans=4, scope_labels=v2_test

== combined U2/U3 live suite + strengthened U6 bounds probe
tests/test_trace_identity_bigquery_live.py: 39 passed in 74.31s
  (BQAA_U6_PROBE_DATASET=u6_evid_20260729_022933; the probe now asserts
   the 24h predicate uncapped, the label-only read that must include the
   48h sentinel, and the capped limit=2 read)

== redaction (committed run.log)
occurrences of the golden-context prefix "EXPECTED ANSWER FOR THIS
QUESTION": 0  (the demo does not persist evaluator rows; the U5 redaction
contract is covered by the offline suite's categorical persistence tests)
```

The replica dataset was deleted after this capture.
