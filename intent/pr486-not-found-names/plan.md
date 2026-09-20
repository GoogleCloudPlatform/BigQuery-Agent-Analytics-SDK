# Plan — PR #486 residual P2

1. Rewrite `Client._not_found_names_source` in `src/bigquery_agent_analytics/client.py` to extract+compare complete Table/Dataset tokens with case respect (`import re`).
2. Add negative tests in `TestEventsTableNotFound` (`tests/test_sdk_client.py`):
   - `test_table_prefix_collision_is_not_translated` (`agent_events_archive`)
   - `test_dataset_prefix_collision_is_not_translated` (`ds_archive`)
   - `test_case_distinct_table_name_is_not_translated` (`AGENT_EVENTS`)
3. Keep existing exact-name / missing-dataset / job-not-found tests green.
4. Run focused pytest: `pytest tests/test_sdk_client.py::TestEventsTableNotFound -q`
5. Commit + push to `fix/485-readme-and-not-found` (no Co-Authored-By: Claude; no merge).
6. Reply on review thread #discussion_r4052121395 that the P2 is fixed (do not resolve the thread).
