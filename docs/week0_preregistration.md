# Week 0 — Preregistration Freeze-Candidate

**Banner:** these numbers are the plan of record (FINAL v4,
`docs/agentforensics_mvp_plan.md`), copied here as the freeze-candidate.
**The six-week clock has NOT started** — this is not a clock start and not
Week 1 execution; the clock starts only when the first Week 1 snapshot job
is kicked. Nothing here invents a new number. Machine-readable copy:
`examples/fixtures/week0_real_preregistration.json` (`example: false`,
`clock_started: false`).

## Floors (from v4)

| Floor | Value |
|---|---|
| Replicate agreement | ≥80% |
| Non-`unknown` coverage | ≥80% |
| Human-human / classifier-vs-adjudicated κ (point) | ≥0.6 |
| κ 95% CI lower bound | ≥0.45 |
| Localization coverage | ≥70% |
| Paired hit@1 uplift CI lower bound | >0 |
| Paired hit@1 point uplift | ≥ +10pp |

## Decision rules (same as the v4 plan)

- **Reserved revision week:** one taxonomy revision slot (week 7); a
  stability miss invokes it once with fresh labels and a re-gate; a second
  miss ends the MVP with the analysis as the deliverable.
- **Value gate:** at least the preregistered fraction of randomly assigned
  investigations where the report changed or materially narrowed the next
  action, adjudicated by a non-investigator; a stated preference or
  unverified acceptance counts for nothing.
- **Noisy-small-n localization:** point-clears-but-CI-spans-zero resolves
  per the preregistered week-0 rule, not week-6 judgment; no metric
  conditions on successful localizations only.
- **Sealed sets:** `P-blind` is frozen before any tuning and scored exactly
  once at week 6.

## Remaining Week 0 item

With partner, D4, and G1 frozen, the remaining Week 0 item is
**preregistration execution** (adopting this freeze-candidate as the sealed
preregistration). That step, not this document, precedes any clock start.
