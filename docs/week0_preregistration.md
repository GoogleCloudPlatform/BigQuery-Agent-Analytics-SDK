# Week 0 — Preregistration (sealed)

**Banner:** sealed 2026-09-02 2:05 PM PT by Hai-Yuan Cao (`caohy1988` /
`haiyuan-eng-google`). These numbers are the plan of record (FINAL v4,
`docs/agentforensics_mvp_plan.md`) plus the three Week-0 numbers the
freeze-candidate still left as placeholders. **The six-week clock has **not** started** — this is not a clock start and not Week 1 execution; the clock
starts only when the first Week 1 snapshot job is kicked. Machine-readable
copy: `examples/fixtures/week0_real_preregistration.json` (`example: false`,
`sealed: true`, `clock_started: false`).

## Floors (from v4, now sealed)

| Floor | Value |
|---|---|
| Replicate agreement | ≥80% |
| Non-`unknown` coverage | ≥80% |
| Human-human / classifier-vs-adjudicated κ (point) | ≥0.6 |
| κ 95% CI lower bound | ≥0.45 |
| Localization coverage | ≥70% |
| Paired hit@1 uplift CI lower bound | >0 |
| Paired hit@1 point uplift | ≥ +10pp |
| Value gate | ≥50% of completed, adjudicated counterfactual investigations |
| Absolute hit@1 | no separate floor (CI >0 and +10pp are the hit@1 gates) |

## Decision rules (sealed)

- **Reserved revision week:** one taxonomy revision slot (week 7); a
  stability miss invokes it once with fresh labels and a re-gate; a second
  miss ends the MVP with the analysis as the deliverable.
- **Value gate:** at least 50% of randomly assigned investigations where
  the report changed or materially narrowed the next action, adjudicated by
  a non-investigator; a stated preference or unverified acceptance counts
  for nothing. If volume slips, apply 50% to completed investigations only.
- **Noisy-small-n localization:** if the point estimate clears but the 95%
  CI spans zero, the gate **fails**. Do not localize and do not claim
  uplift. No metric conditions on successful localizations only. Week-6
  judgment may not override this.
- **Sealed sets:** `P-blind` is frozen before any tuning and scored exactly
  once at week 6.

## What this does not do

Partner, D4, and G1 were already frozen. This commit is **preregistration
execution** only. It does not kick a Week 1 snapshot, does not amend D4
(still: no new BigQuery jobs, no new live judge calls), and does not merge
presenter/do-not-merge PRs.
