# Intent: failed and passed session lists on EvaluationReport (#576)

- Issue: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/576
- Base: `main` at `674973b0`
- Spec: `spec/576-evaluation-report-session-lists.md`
- Plan: `plan/576-evaluation-report-session-lists.md`

## Problem

After `client.evaluate(...)`, a user who wants to look into the sessions
that failed reads `report.failed_sessions` and gets an `int`, the number
of failed sessions. The per-session data is in `report.session_scores`,
mixed with the passing rows, so every caller writes the same filter:

```python
failed = [s for s in report.session_scores if not s.passed]
```

The SDK's own CLI does this (`_emit_evaluate_failures` in `cli.py`), and so
does the BQCA customer notebook.

## Why

Looking at the failures is the usual next step after an evaluation run,
and the report already holds the rows. It should hand over the failed rows,
and the passed ones, as `SessionScore` objects that the caller can filter
further, the same way as `session_scores`.

## Constraints

- **Backward compatible.** `passed_sessions` and `failed_sessions` are
  `int` fields, and callers rely on that:
  - arithmetic: `pass_rate` divides by them;
  - comparison: the streaming evaluator checks `report.failed_sessions == 0`;
  - formatting: `summary()` and the examples' metric dicts;
  - assignment: the BQCA customer notebook overwrites both counts;
  - serialized JSON: CLI `--format json`, Remote Function replies, and the
    streaming results table's `report_json` column.

  None of these may change.
- **Serialized output stays the same.** Adding the lists to the JSON would
  repeat every `session_scores` row.
- **Lists and counts agree** on every report the SDK builds, including after
  `strict=True` marks unparseable sessions as failed.
- **Hermetic unit tests** that fail on `main` and pass after the change.

## Non-goals

- Changing the type or meaning of `passed_sessions` / `failed_sessions`, or
  deprecating them.
- Changing `summary()`, `pass_rate`, `CategoricalEvaluationReport`, or the
  EvalBench `failed_sessions()` function and view, which share the name but
  are unrelated.
- Checking that stored counts match `session_scores`. Callers may build
  count-only reports or recompute the counts themselves.
- Rewriting the examples, notebooks or the CLI that read counts or filter
  `session_scores` by hand. They stay correct.
