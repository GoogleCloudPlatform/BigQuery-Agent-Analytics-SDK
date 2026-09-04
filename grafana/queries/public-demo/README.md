# Public Demo Panel Queries

The `.sql` files here are the **canonical source of truth** for every panel in
`grafana/bqaa-public-demo.json` — one file per panel, embedded as a copy in the
dashboard JSON.

> **Edit the query here first, then paste it into the matching panel in
> `bqaa-public-demo.json`.** A PR that touches one without the other fails CI.

## Why the queries live in `.sql` files

Two reasons, both about keeping things simple:

- **Maintainability.** SQL is edited, reviewed, and diffed as SQL — in a real
  file with syntax highlighting — instead of as an escaped one-line string
  buried in a large JSON document.
- **Straightforward CI check.** `scripts/check_grafana_queries_sync.py` compares
  each file to the panel's query as **plain text**. Keep it that way: do not add
  anything to these files that CI would have to interpret.

> **CI scope.** The lint checks three things: drift between a `.sql` file and
> the dashboard JSON, SQL that would run unbounded or uninterpolated for an
> anonymous viewer, and real project identifiers shipping in place of the
> placeholders. It compares strings, so it catches honest mistakes, not
> deliberate ones — `WHERE <bound> OR TRUE` still reads as bounded. That is a
> known gap: such a query must still fail review. Formatting, style, and edge
> cases are review's job, not CI's.

## Conventions for every file in this directory

- **No variables, no macros.** The demo pins `templating.list` to `[]`. No
  `${...}` placeholders, no Grafana time-group or time-range macros — nothing
  expands them for an anonymous viewer, so they would reach BigQuery verbatim.
- **Hourly buckets via `TIMESTAMP_TRUNC`.** BigQuery's native function, not a
  time-group macro.
- **Half-open 72-hour window in every table-scan branch.** `timestamp >=
  TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR) AND timestamp <
  CURRENT_TIMESTAMP()`. CI counts the uncommented lines carrying each half
  across the whole file and requires that total to equal the number of
  backticked placeholder table paths. That count is global, not positional: it
  catches a new `UNION` arm that scans a table without adding a bound, but two
  copies in one arm and none in another balance out. Keeping each bound in the
  branch it belongs to is on review, not on CI.
- **One standalone query per stat panel.** No panel reads another panel's result
  via the `-- Dashboard --` datasource; each stat pays for its own scan. See
  [`../../README.md`](../../README.md) on setting a BigQuery Custom Quota.
- **Literal table placeholders.** Files read
  `` `YOUR_PROJECT_ID.YOUR_DATASET_ID.<table>` ``; replace both before importing.
- **`HAVING COUNT(*) > 0` on scalar stats.** Without it, an empty range reports a
  confident `0` instead of "No matching data".
- **Missing telemetry.** Token sums degrade to `0` via `IFNULL`; latency
  aggregates stay `NULL` so charts show gaps rather than a fake 0 ms.

The shared error predicate is:

```sql
ENDS_WITH(event_type, '_ERROR') OR error_message IS NOT NULL OR UPPER(status) = 'ERROR'
```

`top_errors.sql` keeps only the `error_message IS NOT NULL` arm, since it groups
by message text.

## File → panel map

| File                           | Panel (demo row)                | Notes                                                                     |
| ------------------------------ | ------------------------------- | ------------------------------------------------------------------------- |
| `overview_sessions.sql`        | Sessions (Overview)             | `COUNT(DISTINCT session_id)`.                                             |
| `overview_events.sql`          | Events (Overview)               | Raw `agent_events` row count.                                             |
| `overview_error_rate.sql`      | Error rate (Overview)           | `SAFE_DIVIDE`, so a zero denominator yields `NULL`, not an error.         |
| `overview_avg_llm_latency.sql` | Avg LLM latency (Overview)      | Scalar companion to the percentiles panel.                                |
| `events_over_time.sql`         | Events over time (Overview)     | One series per `event_type`.                                              |
| `errors_over_time.sql`         | Errors over time (Overview)     | Same error predicate, one series per `event_type`.                        |
| `events_by_agent.sql`          | Events by agent (Overview)      | Raw table, so every event type counts; `agent` is nullable → `unknown`.   |
| `top_errors.sql`               | Top error messages (Overview)   | Narrowed to non-NULL `error_message`; capped at 50.                       |
| `llm_tokens_over_time.sql`     | Token usage over time (FinOps)  | Typed `adk_llm_responses` view.                                           |
| `llm_latency_percentiles.sql`  | LLM latency p50 / p95 / TTFT    | `APPROX_QUANTILES`; NULLs preserved.                                      |
| `tokens_by_model.sql`          | Tokens by model (FinOps)        | `model_version` is response-side; missing → `unknown`.                    |
| `estimated_cost.sql`           | Estimated cost (FinOps)         | Rates are inlined USD-per-1M literals; edit to match your models.         |
| `total_tokens.sql`             | Total tokens (FinOps)           | Provider-reported `usage.total`, so it can exceed prompt + completion.    |
| `llm_calls_total.sql`          | LLM calls (FinOps)              | Distinct `trace_id`/`span_id`, so streaming chunks count as one call.     |
| `tool_usage.sql`               | Tool invocations by tool        | Uses `adk_tool_starts` so failed invocations still count.                 |
| `tool_latency.sql`             | Tool latency (Tools)            | No `IFNULL`, so averages are not skewed.                                  |
| `tool_errors.sql`              | Tool errors (Tools)             | `UNION ALL` keeps both records when one failure logs two events.          |
| `recent_sessions.sql`          | Recent sessions (Sessions)      | Session rollup over the raw table; capped at 250.                         |

There is deliberately no `trace_detail.sql`: the public build exposes no
per-session event timeline.
