# Plan: insights model propagation (#577)

## Commits

1. **test** (8901812): `tests/test_insights_model_propagation.py` encodes
   acceptance A1 to A11. On 674973b: 17 failed, 9 passed. The 9 that pass are
   the guards A1, A7, A9 (endpoint half) and A11.
2. **docs**: this intent, spec and plan.
3. **fix**, implementing the spec:
   - `client.py`, new `Client._insights_api_model(text_model)`: S3.
   - `Client._run_insights`: compute the S3 model once; pass it to
     `_extract_facets`, to every `run_analysis_prompt(..., model=...)` and to
     `generate_executive_summary(report, model=...)` (S4).
   - `Client._extract_facets(table, session_ids, text_model, api_model)`: gate
     AI.GENERATE on the run model and pass it to the AI.GENERATE query (S1);
     call `extract_facets_via_api(transcripts, model=api_model)` (S4).
   - `Client._extract_facets_ai_generate(table, session_ids, endpoint)`: format
     the given endpoint, escaped as before.
   - `insights.py`: import `DEFAULT_ENDPOINT` from `.evaluators` (already
     imported from there, so no new cycle) and use it as the three helpers'
     `model` default (S5).
   - `Client.insights()` docstring for `text_model` (S6).

   The tests from commit 1 are not edited.

## Verification

- Focused: `pytest tests/test_insights_model_propagation.py`, all green.
- Subset: `test_sdk_insights.py`, `test_insights_sql_literals.py`,
  `test_client_table_id.py`, `test_client_project_dataset_ids.py`,
  `test_remote_function.py`, `test_sdk_client.py`, `test_pr16_fixes.py`,
  `test_cli.py`.
- The full suite if the machine has disk headroom; otherwise CI.
- `isort --check-only` and `pyink --check` on the changed Python files; the CI
  check for `intent.md` / `plan.md` / `spec.md` basenames passes, because
  these notes use the issue name.
- Scrub the diff and commit messages: no tool or assistant attribution, no
  co-author trailer, no local paths; secret scan.

## Rollback

Revert commit 3: the commit 1 tests go red again, and nothing else depends on
the change.

## Fix round 1 (review of 8cd6788)

Review found two problems:

1. **URL query and fragment.** URL extraction searched the whole URL for the
   last `/models/`, so a `/models/` in the query or fragment replaced the
   path's model, or named one for a URL whose path has none. The path now
   ends at the URL's first `?` or `#` before the search (spec S3, A13).
2. **Non-string models.** S8 promised that every non-string run model fails
   before the first facet query, but a list, tuple or dict fell through.
   `_run_insights` now raises `TypeError` before any query for a `text_model`
   that is neither a string nor `None`, and for a non-string client endpoint
   (spec S8, A14). This mirrors the `import_version` check in the same file.

The tests were added first; on 8cd6788 they gave 32 failed and 34 passed. The
34 are the existing 26 plus 8 ordinary-suffix guards. With the fix, 66 pass.
