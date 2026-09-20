# Spec — complete Table/Dataset token match (case-preserving)

## Behavior
1. Parse BigQuery `NotFound` messages for complete resource tokens after `Table ` / `Dataset ` (colon or dotted forms: `project:dataset.table`, `project.dataset.table`, `project:dataset`, `project.dataset`).
2. Compare tokens to the queried `{project}{sep}{dataset}.{table}` / `{project}{sep}{dataset}` with **exact** string equality (no `.lower()`, no substring/`in`).
3. Return true only on exact table or dataset token match → translate to `EventsTableNotFoundError`.
4. Otherwise re-raise the original `NotFound` (identity preserved), including:
   - job / job-location NotFound
   - prefix-longer resources (`…agent_events_archive`, `…ds_archive`)
   - case-distinct table names (`AGENT_EVENTS` ≠ `agent_events`)

## Non-goals
- Change exception type hierarchy, message template, or structured metadata forwarding.
- Merge the PR; dual-LGTM (Astra + Muse) is a separate parent step.
