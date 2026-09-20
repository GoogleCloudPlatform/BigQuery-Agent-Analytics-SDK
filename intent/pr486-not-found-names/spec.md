# Spec — complete Table/Dataset token match (case-preserving, spaces OK)

## Behavior
1. Parse BigQuery `NotFound` messages for complete resource tokens after `Table ` / `Dataset ` by capturing until the case-insensitive terminator ` was not found` (colon or dotted forms: `project:dataset.table`, `project.dataset.table`, `project:dataset`, `project.dataset`, including spaces inside the identifier).
2. Compare tokens to the queried `{project}{sep}{dataset}.{table}` / `{project}{sep}{dataset}` with **exact** string equality (no `.lower()`, no substring/`in`).
3. Return true only on exact table or dataset token match → translate to `EventsTableNotFoundError`.
4. Otherwise re-raise the original `NotFound` (identity preserved), including:
   - job / job-location NotFound
   - prefix-longer resources (`…agent_events_archive`, `…ds_archive`)
   - space-suffix resources (`…agent_events archive` when configured is `agent_events`)
   - case-distinct table names (`AGENT_EVENTS` ≠ `agent_events`)
   - ambiguous formats that lack the ` was not found` terminator

## Non-goals
- Change exception type hierarchy, message template, or structured metadata forwarding.
- Merge the PR; dual-LGTM (Astra alone for this residual re-review; Muse already APPROVE'd as COMMENT) is a separate parent step.
