# Plan: failed and passed session lists on EvaluationReport (#576)

- Intent: `intent/576-evaluation-report-session-lists.md`
- Spec: `spec/576-evaluation-report-session-lists.md`
- Base: `main` at `674973b0`, branch `fix/576-evaluation-report-session-lists`

## Steps

1. **Commit the notes.** Commit the intent, spec and this plan before any
   code.
2. **RED, unit tests.** Add `TestEvaluationReportSessionLists` to
   `tests/test_system_evaluator.py`. It builds one report by hand with five
   `SessionScore` rows. Ids are out of sorted order, so losing report order
   shows up:

   | Test | Asserts | Catches |
   |---|---|---|
   | `test_failed_session_scores_lists_failed_rows_in_order` | ids `["sess-9", "sess-2"]` | inverted or wrong predicate; sorted or reordered output |
   | `test_passed_session_scores_lists_passed_rows_in_order` | ids `["sess-5", "sess-1", "sess-7"]` | same, for the passed side |
   | `test_session_lists_hold_the_report_rows` | element `is` the `session_scores` row; filtering on `details` and `llm_feedback` works | copied rows (`model_copy`) or rows rebuilt without some fields |
   | `test_session_lists_follow_later_changes_to_session_scores` | after appending a failed row and flipping a passed one, both lists change | `functools.cached_property`, or lists stored when the report is built |
   | `test_count_only_report_has_empty_session_lists` | counts 7/3 with no rows give `[]`, `[]` | rows invented from counts, or a consistency check that raises |
   | `test_session_lists_stay_out_of_serialized_report` | neither name is in `serialize(report)`; a report rebuilt from it gives the same failed ids | `computed_field` or a stored field, which repeats rows in JSON |
   | `test_session_lists_leave_json_schema_unchanged` (validation and serialization modes; added in review round 1) | the schema `description` is still `Aggregate report from an evaluation run.`; neither name is in the schema properties; both counts are `integer` | an edited class docstring, which pydantic copies into the schema; `computed_field`, which appears in the serialization schema |

3. **RED, end-to-end tests.** In `tests/test_sdk_client.py`, both use a
   mocked BigQuery client:
   - `TestClientEvaluate.test_evaluate_lists_failed_and_passed_session_scores`:
     `client.evaluate(SystemEvaluator.latency(threshold_ms=5000))` over one
     fast session (1500 ms) and one slow session (8000 ms). The failed list
     is exactly `["s2"]` and carries `details["metric_latency"]` (observed
     8000.0, budget 5000). The passed list is `["s1"]`, and each count equals
     its list's length.
   - `TestStrictMode.test_strict_mode_failed_session_scores_include_parse_errors`:
     uses the existing strict fixture (judge returns empty scores for `s2`).
     `failed_session_scores` is exactly `["s2"]` with
     `details["parse_error"] is True`, and `passed_session_scores` is
     `["s1"]`.
4. **Confirm RED.** Run the eight new tests on `674973b0` and record that
   each fails with `AttributeError` on the new names.
5. **GREEN.** Add the two `@property` methods to `EvaluationReport` in
   `src/bigquery_agent_analytics/system_evaluator.py`, with docstrings that
   name the counts and say the lists are not serialized. Leave the class
   docstring unchanged, because pydantic copies it into the JSON schema's
   `description`. Use the truthy predicate that `_build_report` uses.
6. **Run the new tests**, then the focused and area suites:
   ```
   pytest -q tests/test_system_evaluator.py tests/test_sdk_client.py \
     tests/test_serialization.py tests/test_formatter.py tests/test_cli.py \
     tests/test_cli_fail_line_injection.py tests/test_streaming_evaluation.py \
     tests/test_remote_function.py tests/test_pr16_fixes.py \
     tests/test_pr17_fixes.py tests/test_pr19_fixes.py \
     tests/test_pr123_client_regressions.py tests/test_pr123_legacy_regressions.py \
     tests/test_categorical_evaluator.py tests/test_quality_report_helpers.py \
     tests/test_cli_evalbench_score.py
   ```
   These are the suites that build, merge, print or serialize
   `EvaluationReport`.
7. **Docs.**
   - `SDK.md`, section 3 "Batch Evaluation via the Client": a few lines on
     reading `failed_session_scores` after `client.evaluate(...)`.
   - `CHANGELOG.md`: `[Unreleased]`, `### Added`.
8. **Format.** Run `bash autoformat.sh` with CI's versions (`pyink>=24.3.0,<26`,
   `isort>=5.0`), then
   `git diff --exit-code src/ tests/ examples/ scripts/ dashboards/`.
9. **Full suite.** Run `pytest --tb=short -q`, the CI command, with `TMUX`
   and `TMUX_PANE` unset.
10. **Scrub, then ship.** Before each commit and before the push, scrub the
    diff for vendor or tool names, co-author trailers, absolute local
    paths and secrets, and run a secret scan (`gitleaks` if installed,
    otherwise `rg` for key-shaped tokens). Push to `origin` and open the PR
    against upstream `main` with `Closes #576`.

## Risks

- **Name confusion.** `failed_sessions` (count) and `failed_session_scores`
  (rows) sit side by side. The `_session_scores` suffix ties the new names to
  `session_scores`, and both docstrings name the matching count.
- **Counts and rows can disagree** on caller-edited reports, for example
  the notebook that recomputes counts. The spec says the lists follow the
  rows, and the docstrings say so too.
- **Per-access cost** is O(n). This is fine at evaluation-report sizes, and
  the spec says to bind the list to a local for repeated reads.
- **SDLC notes in the repo.** `.gitignore` and the CI "Format check" reject
  files named `intent.md`, `spec.md` and `plan.md`; the comment says working
  notes live outside the repo. This PR's notes are named
  `intent/576-….md`, `spec/576-….md` and `plan/576-….md`, as requested, so
  they pass the check. Maintainers may still ask to drop them before merge,
  and dropping them is a one-commit change.

## Review round 1

Both blind reviews found that the first head, `49a28e5`, changed
`EvaluationReport.model_json_schema()["description"]`, because pydantic
copies the class docstring into the schema. That broke spec item 7.

The fix:

- Restore the one-line class docstring. The details stay in the property
  docstrings and `SDK.md`.
- Add `test_session_lists_leave_json_schema_unchanged`, which runs in both
  schema modes.

The new test fails on `49a28e5` and passes after the fix. It passes on
`674973b0` by design, because it pins that schema.
