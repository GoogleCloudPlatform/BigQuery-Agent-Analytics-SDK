# Spec: failed and passed session lists on EvaluationReport (#576)

- Intent: `intent/576-evaluation-report-session-lists.md`
- Plan: `plan/576-evaluation-report-session-lists.md`
- Base: `main` at `674973b0`

## Decision

`passed_sessions` and `failed_sessions` stay integer counts. Two new
read-only properties return the matching `SessionScore` rows:

| Name | Kind | Type | Value |
|---|---|---|---|
| `passed_sessions` | field, unchanged | `int` | number of passing evaluation units |
| `failed_sessions` | field, unchanged | `int` | number of failing evaluation units |
| `passed_session_scores` | property, new | `list[SessionScore]` | the `session_scores` rows whose `passed` is true |
| `failed_session_scores` | property, new | `list[SessionScore]` | the `session_scores` rows whose `passed` is false |

The names extend `session_scores`: same element type, filtered by outcome.
There are no deprecations and no aliases.

Rejected alternatives:

- **Make `failed_sessions` a list.** This breaks every caller listed in the
  intent and changes the field's JSON type from number to array.
- **Deprecate, then flip.** New count names now, lists under the old names
  later. Every caller and JSON consumer would migrate twice for a change
  that only adds data.
- **`pydantic.computed_field`.** It would add both lists to `model_dump()`,
  so every serialized report would contain each row twice.

## API contract

These properties are on `EvaluationReport`, which is importable from
`bigquery_agent_analytics`, `bigquery_agent_analytics.evaluators` and
`bigquery_agent_analytics.system_evaluator`.

1. **Filter.** `failed_session_scores` returns the rows of `session_scores`
   whose `passed` is falsy, in `session_scores` order.
   `passed_session_scores` returns the rows whose `passed` is truthy, in the
   same order. Truthiness matches how the SDK counts: `_build_report`
   counts truthy `passed` as passed and everything else as failed.
2. **Same rows.** The elements are the `SessionScore` objects from
   `session_scores`, not copies. They keep every field (`session_id`,
   `scores`, `passed`, `details`, `llm_feedback`) and filter the same way
   as `session_scores` rows.
3. **Partition.** Every row of `session_scores` is in exactly one of the two
   lists, so their lengths add up to `len(session_scores)`.
4. **Live, and a new list each time.** Each access reads the current
   `session_scores`, so adding or changing a row there shows up on the next
   access. Each access returns a new list, so changing a returned list does
   not change the report.
5. **Rows, not counts.** The properties never read `passed_sessions` or
   `failed_sessions`. A report built from counts alone, with an empty
   `session_scores`, returns two empty lists.
6. **Read-only.** Neither property has a setter, so assignment raises
   `AttributeError`, as it does for `pass_rate`.
7. **Not serialized.** `model_dump()`, `model_dump_json()`,
   `model_json_schema()` and `serialize(report)` are unchanged. So are their
   outputs: CLI `--format json`, Remote Function replies and the streaming
   `report_json` column. `session_scores` is still serialized, so
   `EvaluationReport.model_validate(serialize(report))` yields the same two
   lists.
8. **Not constructor arguments.** The two names are not fields. If passed to
   `EvaluationReport(...)`, pydantic's default `extra="ignore"` drops them,
   as it does any unknown keyword today.

### Counts and lists

Every report the SDK builds satisfies:

```
report.passed_sessions == len(report.passed_session_scores)
report.failed_sessions == len(report.failed_session_scores)
```

That covers `_build_report`, which every `Client.evaluate()` path and the
multi-criterion merge use, and `_apply_strict_mode` for `strict=True`. In
strict mode, a row marked failed for empty scores, with
`details["parse_error"] = True`, is in `failed_session_scores`.

A caller who edits the counts or the rows afterwards can make them
disagree. The notebook that recomputes counts over evaluated sessions does
this. The lists then follow the rows.

## Compatibility

- **Additive.** No field, type, default, signature, export, serialized key
  or JSON schema changes. Code that reads, compares, formats, assigns or
  serializes `passed_sessions` and `failed_sessions` behaves as before.
- **Cost.** Each access is O(n) in `len(session_scores)`. A caller that
  reads a list many times can bind it to a local variable first.
- **Versions.** Python 3.10–3.14 and pydantic v2, as today.

## Docs

No doc or example says `failed_sessions` is anything but a count, and the
counts stay correct wherever they are read, so nothing is rewritten. The
only additions:

- `SDK.md`, "Batch Evaluation via the Client" (section 3): a short snippet
  that reads the failed rows after `client.evaluate(...)`.
- `CHANGELOG.md`: an entry under `[Unreleased]`, `### Added`.
- Docstrings on both properties. The `EvaluationReport` class docstring
  points to them.

## Done when

1. The new tests in the plan fail on `674973b0` with `AttributeError` on the
   new names, and pass with the change.
2. The focused and area suites in the plan pass.
3. `pytest --tb=short -q`, the full suite as CI runs it, passes.
   `bash autoformat.sh` with CI's tool versions (`pyink>=24.3.0,<26`,
   `isort>=5.0`) leaves no diff.
4. A PR against `main` says `Closes #576`.
