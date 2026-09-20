# Plan — PR #486 residual P2

## Slice 1 (landed) — prefix / case tokens
1. Rewrite `Client._not_found_names_source` to extract+compare complete Table/Dataset tokens with case respect (`import re`).
2. Negative tests: `agent_events_archive`, `ds_archive`, `AGENT_EVENTS`.
3. Keep exact-name / missing-dataset / job-not-found green.

## Slice 2 (this residual) — spaces inside identifiers
1. Prefer capturing the full resource after `Table` / `Dataset` until ` was not found` (case-insensitive) so spaces inside identifiers are preserved; drop `[^\s,;]+` truncation.
2. Keep exact equality against expected_tables / expected_datasets (case-preserving).
3. Add:
   - `test_table_name_with_spaces_suffix_is_not_translated` (`agent_events archive` vs configured `agent_events`)
   - `test_table_name_with_spaces_exact_is_translated` (configured `agent_events archive` exact match)
4. Keep job NotFound / prefix / case-distinct negatives green.
5. `pytest tests/test_sdk_client.py::TestEventsTableNotFound -q`
6. pyink format; commit + push to `fix/485-readme-and-not-found` (no Co-Authored-By: Claude; no merge).
7. Reply on #discussion_r4057920520 (do not resolve). Astra-only blind re-review of this P2 close (do not kick Muse/Fable as reviewer).
