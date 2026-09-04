# Week 0 — D4 Boundary Memo (frozen, fail-closed)

**Status:** frozen by the #435 Week 0 freeze PR. This is the REAL D4
boundary for exactly this pilot's data, not the example pack. The six-week
clock has **not** started; it starts only when the
first Week 1 snapshot job is kicked. Machine-readable copy:
`examples/fixtures/week0_real_d4_memo.json` (`example: false`).

## Scope — this pilot data only

- Project: `test-project-0728-467323`
- Datasets: `bqaa_e2e_real`, `bqaa_evalbench_mvp_demo`,
  `bqaa_evalbench_mvp_mirror`
- Derived artifacts: `failed_sessions` rows and taxonomy labels computed
  from them

## Named report consumers

- **Hai-Yuan Cao** (`caohy1988` / `haiyuan-eng-google`)

No other consumer is named. **Fail-closed:** if a consumer is not named in
this memo, access is denied.

## Per-user grants and notebook/export policy (text only, no IAM API)

No additional per-user grants and no notebook/export access beyond
Hai-Yuan Cao until a later D4 amendment. This policy is recorded as text in
this governed memo; **no IAM API calls** are made by the freeze, and none
are needed until an amendment names a new consumer.

## What synthetic / fixture / example runs can prove

The `--fixture` path, synthetic data, and the example pack validate
**ingestion, taxonomy mechanics, and stability ONLY** — they can **never**
produce a Part II funding recommendation.

## Standing restrictions under this memo

- No new live judge calls.
- No new BigQuery jobs.
- No labeler access expansion.
- The stop/go memo is itself a governed artifact inside this D4 boundary.
