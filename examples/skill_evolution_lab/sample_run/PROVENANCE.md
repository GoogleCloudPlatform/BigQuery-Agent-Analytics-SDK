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
| Result | exit 0; V0 test 28.6% (20/70), V1 test 100.0% (70/70) |
| Persisted checks | slices v0_evolve 168r/68s, v0_test 216r/80s, v1_test 382r/80s — one run label, `app=skill-evolution-lab` everywhere, zero foreign rows; all 80 held-out ids reused across V0/V1 in ONE table with no span mixing (201 live span-tree lines, no `custom_tags.seeded`) |
| Teardown | deleted after evidence capture (see below) |

## AE8 — server-side golden-grounded judge (the #358 cleanup on the AE7 substrate)

Pre-flight smoke (against AE7 data): `--limit 2` server-side scoring returned
`execution_mode: ai_generate` with scoped selection — BigQuery `AI.GENERATE`
judging works in-project.

**Attempt 1 — failed, evidence retained.** Commit `9c0272b`, dataset
`u6_ae8_20260728_181934` (retained until diagnosis, then expired). Exit 1 in
STEP 3: an unretried `429 RESOURCE_EXHAUSTED` from the embedding model inside
`match_golden_qa → _embed_texts`. Fix: commit `6d9c65d` (bounded per-batch
retry on 429/503, unit-tested). A subsequent attempt used a new dataset, per
the design.

**Attempt 2 — the gate run and the sole AE9 artifact source.**

| Field | Value |
| --- | --- |
| Commit (clean tree) | `6d9c65d` |
| UTC window | 2026-07-28 18:33:57 → 18:45:43 (14m03s wall) |
| Project / dataset | `agent-skill-lab-01` / `u6_ae8_20260728_183340` (label `u6:ae8`) |
| Table / run label | `agent_events` (only table) / `lab_20260728_183356_39969610161` |
| Command | `DATASET_ID=u6_ae8_… ./run_e2e_demo.sh --agent-model gemini-3.1-flash-lite --rounds 2` |
| Models | agent `gemini-3.1-flash-lite` (Vertex `global` — unchanged from the previous sample; the material migration is hybrid→server-side judging and per-slice→shared storage); analyst `gemini-3.1-pro-preview`; judge `gemini-2.5-flash` @ `us-central1`, server-side (`execution_mode: ai_generate` on all 5 scoring passes) |
| Score bounds | `--app-name skill-evolution-lab --label run=… --label slice=… --time-period 24h --limit 500` (cap attested by the logged command + report metadata; population stayed far below it) |
| Result | exit 0; V0 test 30.0% (21/70) → V1 98.6% (69/70); V2 95.7% — gate refused V2 (overall 93.8% ≤ 98.8%), V1 kept |
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
  copied wholesale; `run.log` path-sanitized only. Artifact commit SHA: see the
  commit that introduced this file.
- Headline (RESULT.md): V0 35.0% (28/80) → V1 98.8% (79/80) overall;
  corrections 0/15 → 15/15 with parroted sub-trajectories 12 → 0;
  out-of-scope 7/10 → 10/10. Round 2: V2 95.7% — strict-win gate kept V1.
- Key artifact hashes (SHA-256):
  - `v1_skill.md` `e321f05f4e1e32b59d061a46c203a3f31c6fad0d6cb09934ad08e0d0aa247db4`
  - `v2_skill.md` `5b50d15c52e4a49cb3bc8801fed7f226d4bbadd5e659414e88135c3101d65114`
  - `RESULT.md` `6562e95ba73a4917ebea515ab31102dccb9908005ebdf8a8040aec198607fdc7`
  - `RESULT_ROUND2.md` `491db36d8ddbe4f584dbb12799443335829b383bd170c0db5ee0d4994894db30`
  - `run.log` `8cd46b9205c48c38869483e530e24f8c09efaedfa058940b897c9f2e3440834e`
  - `v1_test_report.json` `f95fcef046bc3ba43e42e09b2f5a01ead55038d4d45e79481542c6eda4e8065b`
- Gates at the artifact commit: `bash -n run_e2e_demo.sh`; focused
  quality-report / compare-runs / skill-evolution suites; full offline suite;
  formatter + whitespace checks; exact-80 held-out population per pass
  (missing sessions fail, strays excluded, ties keep the incumbent —
  enforced by `compare_runs.py --questions --gate` during the run).
- `gist_update`: PENDING — recorded here once the canonical Gist revision is
  published (or prepared-but-blocked) after the gates pass.
- Scratch teardown: `u6_ae7_20260728_180346` DELETED, `u6_ae8_20260728_181934`
  (failed attempt) DELETED, `u6_ae8_20260728_183340` DELETED — all after the
  evidence above was captured; recorded at teardown time.
