# Grafana Panel Queries: Source of Truth

The `.sql` files in this directory are the **canonical source of truth** for
every panel and query template variable in `grafana/bqaa-dashboard.json`.
The dashboard JSON embeds a copy of each query (Grafana has no "include SQL
from file" mechanism), so:

> **If you change a query, change it here first, then paste the updated SQL
> into the matching panel in `bqaa-dashboard.json`.** A PR that touches one
> without the other should be treated as incomplete.

**This applies to SQL text only.** For *which filters a panel honors*, the
canonical statement is the **Variables** table in
[`../README.md`](../README.md#variables). Update that table when a filter's
scope changes. This file, the `-- ` headers in the `.sql` files, and the
dashboard's row titles and tooltips explain the *rationale* and should point at
that table rather than restate it. `scripts/check_grafana_queries_sync.py` only
diffs SQL text, so keep the restatements few.

## Conventions

- **Templating placeholders.** The files use Grafana template-variable syntax:
  - `${project}`, `${dataset}`, `${table}`: BigQuery location.
  - `${view_prefix}`: prefix applied by `ViewManager`.
  - `${agent:sqlstring}`, `${session_id:sqlstring}`: variables safely escaped by
    Grafana to prevent SQL injection.
  - `${price_per_1m_input_tokens}`, `${price_per_1m_output_tokens}`: per-1M-token
    USD rates for `estimated_cost.sql`. Interpolated raw, without `:sqlstring`,
    because they are numbers in arithmetic rather than string literals. They are
    `constant` variables with `skipUrlSync`, like `${project}` and `${dataset}`,
    so only someone who can already edit the dashboard can change them. A URL
    cannot. Operator-facing docs:
    [Cost variables](../README.md#cost-variables).
  - `$__timeFilter(...)`: Grafana time range macros.

- **The `All` agent sentinel.** The `agent` variable's "All" option uses the
  custom value `'___ALL___'`. Because of how Grafana handles multi-select
  interpolation, queries pair every agent filter with this sentinel using
  BigQuery array syntax to prevent injection and empty-array crashes:
  `('___ALL___' IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]) OR agent IN UNNEST(ARRAY<STRING>[${agent:sqlstring}]))`.

- **Error predicate.** An event counts as an error if any of these hold (used
  across overview and session queries):
  ```sql
  ENDS_WITH(event_type, '_ERROR')
    OR error_message IS NOT NULL
    OR UPPER(status) = 'ERROR'
  ```
  `top_errors.sql` is the one deliberate narrowing: it groups by the message
  text, so it keeps only the `error_message IS NOT NULL` arm — an error that
  recorded no message has no string to group under. Its counts are therefore a
  subset of `errors_over_time.sql`'s, and its header says so.

- **Typed views.** Queries use typed views (e.g. `${view_prefix}llm_responses`)
  instead of parsing JSON from the raw `agent_events` table whenever possible.

- **Event Type scoping.** Two rules govern which queries carry an
  `${event_type:sqlstring}` clause; the per-panel outcome lives in the
  [Variables table](../README.md#variables), and each exempt file states its own
  reason in its header comment.
  1. A view-backed query is already scoped (each typed view holds exactly one
     event type), so adding the filter would blank the panel for every selection
     that did not name that view's own event type. `llm_calls_total.sql` is the
     limiting case: its `COUNT(*)` over `${view_prefix}llm_responses` *is* the
     LLM_RESPONSE count, so the filter would only ever subtract from it.
  2. A query that counts *errors* is exempt even when it reads the raw
     `${table}`, because errors carry their own `*_ERROR` event types: honoring
     the filter would report zero errors (or a 0% error rate) whenever the
     selection named some other event type.

- **Recent sessions report whole sessions.** `recent_sessions.sql` honors all
  four filters, but they only decide *which* sessions are listed. Each listed
  session's duration, user list, agent count, event count, error count and token
  sums then cover the whole session, including events the filters exclude, so
  they will not agree with the Overview stats. The table is capped at the 250
  most recently active sessions in the range. It is also the one panel that
  reads `$.usage.prompt` / `$.usage.completion` out of the raw `content` payload
  rather than through `${view_prefix}llm_responses`: it groups the raw
  `${table}` and cannot join the view without paying for a second scan.

- **Missing telemetry.** Token sums degrade to `0` (`IFNULL(..., 0)`); latency
  aggregates stay `NULL` so charts show gaps instead of fake zeros.

## File → Panel map

|             File              |                        Panel (dashboard row)                      |
| ----------------------------- | ----------------------------------------------------------------- |
| `overview_totals.sql`         | Sessions / Events / Error rate / Avg LLM latency stats (Overview) |
| `events_over_time.sql`        | Events over time (Overview)                                       |
| `errors_over_time.sql`        | Errors over time (Overview)                                       |
| `events_by_agent.sql`         | Events by agent (Overview)                                        |
| `top_errors.sql`              | Top error messages (Overview)                                     |
| `llm_tokens_over_time.sql`    | Token usage over time (LLM & FinOps)                              |
| `llm_latency_percentiles.sql` | LLM latency (p50 / p95 / TTFT) (LLM & FinOps)                      |
| `tokens_by_model.sql`         | Tokens by model (LLM & FinOps)                                    |
| `estimated_cost.sql`          | Estimated cost (placeholder rates) + Total tokens (LLM & FinOps)  |
| `llm_calls_total.sql`         | LLM calls (LLM & FinOps)                                          |
| `tool_usage.sql`              | Tool invocations by tool (Tools & Execution)                      |
| `tool_latency.sql`            | Tool latency (Tools & Execution)                                  |
| `tool_errors.sql`             | Tool errors (Tools & Execution)                                   |
| `recent_sessions.sql`         | Recent sessions (Sessions & Traces)                               |
| `trace_detail.sql`            | Trace detail — `${session_id:text}` (Sessions & Traces)           |
| `var_agent.sql`               | Agent template variable (capped at 1000)                          |
| `var_user_id.sql`             | User ID template variable (capped at 1000)                        |
| `var_event_type.sql`          | Event Type template variable (uncapped)                           |
| `var_session_id.sql`          | Session template variable (no cascade, capped at 1000)            |

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
