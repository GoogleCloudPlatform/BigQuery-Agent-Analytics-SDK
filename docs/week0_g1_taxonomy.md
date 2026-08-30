# Week 0 — G1 Taxonomy Freeze (v0.1.0)

**Status:** frozen by the #435 Week 0 freeze PR, in production code:
`src/bigquery_agent_analytics/failure_taxonomy.py` now carries
`taxonomy_version: 0.1.0` / `g1_frozen: true`. **Freezing G1 is not a clock
start**: the six-week clock has **not** started and starts only when the
first Week 1 snapshot job is kicked. Machine-readable copy:
`examples/fixtures/week0_real_taxonomy.json` (`example: false`,
`g1_frozen: true`).

## Frozen vocabulary (names and spellings are stable)

The SANA-neighborhood seven, then `unknown`, in **frozen order**:

1. `task/planning`
2. `wrong source`
3. `execution/computation`
4. `incomplete evidence`
5. `turn-waste`
6. `finalization`
7. `tool blockers`
8. `unknown`

`unknown` is in the vocabulary as the residual bucket for the labeler
study. The mechanical mapper never emits it: a row with all flags false
returns `()`, not `("unknown",)`.

## Mechanical assignment until the labeler study

`categorize_failed_session` maps the three landed failed-session flags
(`MECHANICAL_FLAGS`) onto frozen names:

| Flag | Frozen category |
|---|---|
| `missing_completion` | `finalization` |
| `process_failed` | `tool blockers` |
| `score_failed` | `task/planning` |

When multiple flags trip, the returned names follow the **frozen order
above, not flag order**. The other four categories and `unknown` are never
emitted by the three-flag mapper; they become assignable when the labeler
study runs.

## The widget-stock pilot session

Session `7e352c34-4c1c-4395-acd5-fb3c8f215346` (`7e352c34`) trips all three
flags, so its `taxonomy_categories` are:

```
("task/planning", "finalization", "tool blockers")
```

## What did not change

- `dialects` stays `[]` (D2: one taxonomy, optional per-benchmark
  extension categories on the same core — still empty).
- The config keeps the #431 `{"metrics": [...]}` schema shape;
  `evaluation_rubrics.build_metrics` interprets it unchanged.
- `scaffold_taxonomy_config()` survives as a compatibility wrapper for
  `taxonomy_config()` (see CHANGELOG).
- No six-week execution starts here: no ≥100-session study, no labeler
  study, no live-trace ingestion job.
