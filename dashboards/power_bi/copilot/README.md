# BQAA Copilot preview (prompt-to-dashboard)

A browser-only preview of a prompt-driven dashboard for BigQuery Agent
Analytics. Type what you want to see, such as "p95 LLM latency by model for the
last 7 days" or "which tools fail most", and the page builds a dashboard page
with KPI cards, cross-filtering charts, a written summary, and the BigQuery SQL
behind it. It reads the same `agent_events` table as the
[Power BI dashboard](../README.md) and uses the same page themes: activity,
tokens, tool usage, latency, and errors.

This is an exploration of the authoring experience, not a replacement for the
PBIP dashboard. It does not need a Power BI license.

## What it is and isn't

- **Prompts are matched by keyword rules in your browser.** There is no
  language model behind the Copilot pane, and nothing is sent to a server.
  Prompts that match a theme get that theme's page; anything else gets a page
  built from the metric and dimension the prompt names.
- **It doesn't connect to BigQuery.** It starts with a synthetic sample of
  `agent_events` rows. Use **Import data** to load your own rows, and
  **BigQuery SQL** to copy the queries for a page and run them yourself.
- **Numbers are close to, not identical to, BigQuery.** P95 values use a
  nearest-rank percentile, while the generated SQL uses `APPROX_QUANTILES`.

## Run it

Serve the folder over HTTP from the repository root:

```bash
python3 -m http.server 8800 --directory dashboards/power_bi/copilot
```

Then open <http://localhost:8800/>.

## Load your own data

Export rows from your `agent_events` table and paste or upload them in
**Import data**. JSON arrays, JSON Lines, and CSV all work, including the
output of:

```bash
bq query --use_legacy_sql=false --format=json --max_rows=5000 \
  'SELECT * EXCEPT(content_parts)
   FROM `your-project.your_dataset.agent_events`
   WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
   ORDER BY timestamp DESC'
```

The importer reads `latency_ms`, `attributes.usage_metadata`, and
`content.tool` whether they arrive as JSON objects or JSON strings, and
accepts `timestamp` as ISO 8601 or epoch seconds. Rows without a parseable
timestamp are skipped and counted. The data stays in the browser tab.

## Metrics

Every metric is defined once, with the SQL it compiles to. The **Schema and
metric definitions** view lists them all. The main ones:

| Metric | Definition |
| :-- | :-- |
| LLM calls | `LLM_RESPONSE` events |
| Tool calls | `TOOL_COMPLETED` and `TOOL_ERROR` events |
| Error rate | `LLM_ERROR` and `TOOL_ERROR` events as a share of all LLM and tool calls |
| LLM latency, time to first token | `latency_ms.total_ms` and `latency_ms.time_to_first_token_ms` on `LLM_RESPONSE` |
| Tool latency | `latency_ms.total_ms` on `TOOL_COMPLETED` |
| Cache hit rate | `cached_content_token_count` as a share of `prompt_token_count` |

KPI cards compare the selected window with the window of the same length just
before it. When the earlier window has no data, the change shows `--`.

## Generated SQL

The SQL dialog renders one multi-statement query: a `CREATE TEMP TABLE` that
extracts the JSON columns once, then one `SELECT` for the KPI cards and one per
visual. The table ID must match `project.dataset.table` using the same
identifier rules as the PBIP parameters. Filter values are written as escaped
string literals.

The SQL window is relative to the current time, while the dashboard's window
ends at the newest date in the loaded data, so the two can cover different days
on older data.

## Files

| File | Purpose |
| :-- | :-- |
| `index.html` | Page layout |
| `styles.css` | Light and dark themes |
| `app.js` | Sample data, metrics, prompt rules, charts, SQL, and import |

`tests/test_power_bi_copilot.py` checks the files statically and, when Node.js
is available, runs `tests/power_bi_copilot_smoke.js` against `app.js` with a
minimal DOM stub.
