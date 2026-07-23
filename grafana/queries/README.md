# Grafana Panel Queries — Source of Truth

The `.sql` files in this directory are the **canonical source of truth** for
every panel and query template variable in `grafana/bqaa-dashboard.json`.
The dashboard JSON embeds a copy of each query (Grafana has no "include SQL
from file" mechanism), so:

> **If you change a query, change it here first, then paste the updated SQL
> into the matching panel in `bqaa-dashboard.json`.** A PR that touches one
> without the other should be treated as incomplete.

## Conventions

- **Templating placeholders.** The files use Grafana template-variable syntax:
  - `${project}`, `${dataset}`, `${table}` — BigQuery location.
  - `${view_prefix}` — prefix applied by `ViewManager`.
  - `${agent:sqlstring}`, `${session_id:sqlstring}` — variables safely escaped by Grafana to prevent SQL injection.
  - `$__timeFilter(...)` — Grafana time range macros.

- **The `All` agent sentinel.** The `agent` variable's "All" option uses the custom value `'___ALL___'`. Because of how Grafana handles multi-select interpolation, queries pair every agent filter with this sentinel using BigQuery array syntax to prevent injection and empty-array crashes:
  `('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))`.

- **Error predicate.** An event is classified as an error if it meets any of the following conditions (used across overview and session queries):
  ```sql
  ENDS_WITH(event_type, '_ERROR')
    OR error_message IS NOT NULL
    OR UPPER(status) = 'ERROR'
  ```

- **Typed views.** Queries use typed views (e.g. `${view_prefix}llm_responses`) instead of parsing JSON from the raw `agent_events` table whenever possible.

- **Missing telemetry.** Token sums degrade to `0` (`IFNULL(..., 0)`); latency aggregates stay `NULL` so charts show gaps instead of fake zeros.

## File → Panel map

|             File              |                        Panel (dashboard row)                      |
| ----------------------------- | ----------------------------------------------------------------- |
| `overview_totals.sql`         | Sessions / Events / Error rate / Avg LLM latency stats (Overview) |
| `events_over_time.sql`        | Events over time (Overview)                                       |
| `errors_over_time.sql`        | Errors over time (Overview)                                       |
| `llm_tokens_over_time.sql`    | Token usage over time (LLM & FinOps)                              |
| `llm_latency_percentiles.sql` | LLM latency p50/p95 + TTFT (LLM & FinOps)                         |
| `tokens_by_model.sql`         | Tokens by model (LLM & FinOps)                                    |
| `estimated_cost.sql`          | Estimated cost (placeholder rates) + Total tokens (LLM & FinOps)  |
| `tool_usage.sql`              | Tool invocations by tool (Tools)                                  |
| `tool_latency.sql`            | Tool latency by tool (Tools)                                      |
| `tool_errors.sql`             | Tool errors (Tools)                                               |
| `recent_sessions.sql`         | Recent sessions (Sessions & Traces)                               |
| `trace_detail.sql`            | Trace detail for `$session_id` (Sessions & Traces)                |
| `var_agent.sql`               | Agent template variable                                           |
| `var_session_id.sql`          | Session template variable                                         |

## Adding a New Panel (CI Synchronization)

The CI synchronization script uses an explicit `PANEL_QUERIES` dictionary
that maps Grafana panel IDs to `.sql` files. This explicit mapping serves as
an integration test: it checks that every query file is wired to the
intended panel in the dashboard.

Panels that reuse another panel's result set (the `-- Dashboard --`
datasource) are not query-backed, so they live in a separate
`DASHBOARD_DATA_PANEL_SOURCES` mapping instead of `PANEL_QUERIES`. That
mapping records `{panel_id: source_panel_id}` and the script verifies each
such panel points at its source rather than at a `.sql` file.

When adding a panel:

1. Build the panel in Grafana and save the dashboard.
2. Note the integer `id` Grafana assigned to the panel in the dashboard JSON.
3. Save the query in a new file, such as `queries/new_feature.sql`.
4. Explicitly add `id: "new_feature.sql"` to `PANEL_QUERIES` in the CI
   synchronization script.
