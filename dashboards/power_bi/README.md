# BigQuery Agent Analytics — Power BI Dashboard

A Power BI dashboard generated as a [PBIP project](https://learn.microsoft.com
/power-bi/developer/projects/projects-pbip) (TMDL semantic model + PBIR
report), porting the 8-page / 37-chart
[Looker Studio dashboard](../looker_studio/README.md) to Power BI. Built
directly on the `agent_events` table populated by the
[ADK BigQuery Agent Analytics plugin](https://adk.dev/observability/
bigquery-agent-analytics/).

**Just want to use the dashboard?** Read the
[User Manual](USER_MANUAL.md) — prerequisites, setup, page guide, and
troubleshooting, written for dashboard users.

## Overview

The project lives under `dashboards/power_bi/` and is generated from the
existing Looker Studio contracts (`dashboards/looker_studio/spec/
chart_manifest.yaml`, `dashboards/looker_studio/spec/product_contract.yaml`,
`dashboards/looker_studio/sql/events_v1.sql.tmpl`, and the oracle queries).
The generator (`tools/generate_pbip.py`) produces a deterministic,
text-based Power BI project that is reviewable in plain diffs. A
`--check` drift gate keeps the Power BI port from silently diverging
when the Looker Studio contracts change.

### What you get

An 8-page Power BI report — Token Consumption, Agent & Sessions, Tool
Usage, LLM Interactions, User Analytics, Latency, Errors, and a Trace
Inspector — with 37 parity charts, 9 non-data elements, 40 synced
slicers, and 50 DAX measures that match the 37 oracle SQL queries.

### One BigQuery scan per refresh

The semantic model uses Import mode with a single
`GoogleBigQuery.Database` + `Value.NativeQuery` scan that reuses the
`events_v1` SELECT list verbatim. No new metric SQL is authored — all
metrics are DAX measures computed in the VertiPaq engine.

## Prerequisites

1. **A BQAA table with data in it.** If your agents run with the ADK
   BigQuery Agent Analytics plugin, this table already exists (normally
   named `agent_events`).
2. **A Google account that can read that table and run BigQuery jobs**
   in the project that will pay for queries.
3. **A Power BI license.** A per-user Premium (PPU) license or a
   Fabric capacity is required to deploy and refresh the semantic model.
4. **Python 3.10–3.14** for the generator and validator.

## Parameters

The semantic model exposes 7 M parameters that control the BigQuery
connection and date window. Defaults ship the public fixture table.

| Parameter | Default | Validation |
| :-- | :-- | :-- |
| `BQAA_ProjectId` | `test-project-0728-467323` | `^[a-z][a-z0-9-]{4,28}[a-z0-9]$` |
| `BQAA_DatasetId` | `bqaa_fixture_adk_1_27_0` | `^[A-Za-z_][A-Za-z0-9_]{0,1023}$` |
| `BQAA_TableId` | `agent_events` | `^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$` |
| `BQAA_BillingProjectId` | `test-project-0728-467323` | Same as `BQAA_ProjectId` |
| `BQAA_WindowEndDate` | `auto` | `auto` or a real calendar date in strict `YYYY-MM-DD` form |
| `BQAA_LookbackDays` | `90` | Integer 1–366 |
| `BQAA_UseStorageApi` | `true` | Logical; set `false` if the account lacks `bigquery.readsessions.*` |

### Trust model

Only semantic-model owners/editors can change M parameters (Settings →
Parameters). Report viewers cannot; URL filters act on loaded data only.
Validation protects against misconfiguration and pasted values rather
than a privilege boundary, because model editors can already author
arbitrary M. The model must not bind these parameters as dynamic M
query parameters.

### Deliberate strictness

The identifier regexes reject some BigQuery identifiers (domain-scoped
projects like `example.com:p` and table ids with Unicode spaces). We
keep the repository's existing contract for one consistent fail-closed
rule.

The generator, deploy helper, and live validator match the whole value
rather than a prefix, so a trailing newline is rejected in project,
dataset, table, and GUID inputs, like the M `Only(...)` checks do. An
impossible date such as `2026-02-30` is rejected for
`BQAA_WindowEndDate`.

## SQL substitution contract

The `BQAA_Config` M expression builds the BigQuery SQL from validated
parameter bindings. This mirrors the Grafana Textbox variable risk
documented in `dashboards/grafana/README.md`.

### Placeholders

The generator reuses the `events_v1` SELECT list verbatim from
`dashboards/looker_studio/sql/events_v1.sql.tmpl`. The SELECT…FROM block
is embedded as M text literals (one per line) so TMDL indentation does
not alter SQL whitespace.

### SELECT reuse

The events query is the verbatim `events_v1` SELECT list plus a WHERE
clause. No other part of the template is modified.

### WHERE replacement

The template's `WHERE timestamp >= @start_date AND timestamp < @end_date`
placeholder predicate is replaced by M-computed constant DATE literals:

```
WHERE timestamp >= TIMESTAMP(DATE '<load-start>', 'UTC')
  AND timestamp < TIMESTAMP(DATE_ADD(DATE '<anchor>', INTERVAL 1 DAY), 'UTC')
```

M computes `load-start` and `anchor` from the parameter values, so
BigQuery prunes partitions to the 2L-day load range.

### M escaping rules

The generated M text literals escape:
- `"` → `""` (M string escaping)
- `#(` → `#(#)(` (M interpolated expression escaping)

### Constant DATE literals

Dates are rendered by M from `date` values via
`Date.ToText(d, [Format="yyyy-MM-dd", Culture="en-US"])`, producing only
digits and `-`. They are never spliced as raw parameter text.

### No semicolons

The generator strips the template header comment (which contains `UTC;`)
and asserts the generated SQL contains no `;`, `"`, or `#` outside M
escaping.

## Date window

### Default behavior (`BQAA_WindowEndDate = auto`)

M runs a metadata query against `INFORMATION_SCHEMA.PARTITIONS` to find
the newest non-empty partition. If a streaming buffer exists
(`__UNPARTITIONED__` rows), the anchor is `max(partition_date,
CURRENT_DATE('UTC'))`. If no usable partition exists (unpartitioned or
month/year partitioned table), it falls back to
`DATE(MAX(timestamp), 'UTC')` from the table. If the table is empty, it
uses today (UTC).

### Explicit window

Set `BQAA_WindowEndDate` to a `YYYY-MM-DD` date to skip the metadata
query entirely. The anchor is that date.

### Load range

With `BQAA_LookbackDays = L` (default 90), the current window is
`[anchor − (L−1), anchor]`, the prior window is
`[anchor − (2L−1), anchor − L]`, and the load range covers both:
`[anchor − (2L−1), anchor]`.

### Locked report filter

A report-level filter `Date[Window] = "Current"` is hidden and locked.
Viewers can narrow the date range inside the current window but cannot
leave it. This ensures the prior window (for the `KPI * Prior` and
`KPI * Change %` period-over-period measures) is always inside the loaded
range.

## Deploy

### Option 0 — 1-click Web Configurator or CLI `.zip` packager

Open the browser-based **Power BI Web Configurator** (`dashboards/power_bi/docs/index.html`, published to `/power-bi/` on GitHub Pages) to enter your `project.dataset.table` ID, preview all 8 dashboard pages interactively, and download a pre-configured `BQAA_Agent_Analytics_<project>_<dataset>_<table>.zip` archive with zero server-side processing.

Or generate the same pre-configured `.zip` archive from the CLI:

```bash
python dashboards/power_bi/tools/generate_pbip.py \
  --output-zip BQAA_Agent_Analytics.zip \
  --project <your-gcp-project> \
  --dataset <your_dataset> \
  --table <your_table>
```

### Option 1 — Fabric REST API

Use `tools/deploy_fabric.py` with a delegated bearer token (obtain one
with any Microsoft-supported tool; the script contains no client IDs):

```bash
python dashboards/power_bi/tools/deploy_fabric.py \
  --workspace-id <workspace-guid> \
  --token-file /path/to/fabric.token \
  --project-dir dashboards/power_bi \
  --display-name "BQAA Agent Analytics"
```

The script creates the semantic model and report via the Fabric REST
API with TMDL and PBIR definitions, rewriting `definition.pbir` from
`byPath` to `byConnection` in memory. Use `--dry-run` to list parts
without network access. Use `--update` to update existing items.

Existing items are found by display name across every page of the
workspace listing. The script stops with an error, instead of creating a
duplicate, when the listing fails or when two items share the display
name. The report connection is always rewritten with the semantic model
id, which an update keeps even when the service answers with an empty
body.

### Option 2 — Fabric trial

If the REST API rejects a PPU workspace, start a 60-day Fabric trial
from `app.powerbi.com → Account manager → Start trial`, create or
assign a Trial workspace, and rerun the deploy helper with the trial
workspace id.

### Option 3 — Git integration

If a tenant admin enables the GitHub Git provider, connect the
workspace to this repository, branch `feat/power-bi-dashboard`, folder
`dashboards/power_bi`, and sync Git → workspace.

### After deploy

Set BigQuery credentials on the semantic model:
**Workspace → semantic model → ⋯ → Settings → Data source credentials →
Edit credentials → Google BigQuery → Sign in**.

**OAuth2** (default): choose Authentication method `OAuth2`, Privacy level
`Organizational`, then Sign in with a Google account that can read the
BigQuery table and run jobs in the billing project.

**Service account** (for `@google.com` accounts that block third-party
OAuth2 on Power BI's client ID): create or use a GCP service account
with `roles/bigquery.dataViewer` on the dataset and
`roles/bigquery.jobUser` + `roles/bigquery.readSessionUser` on the
project. Generate a JSON key in GCP Console → IAM & Admin → Service
accounts → Keys, then in the Power BI credential dialog choose
Authentication method `Basic` (or `Service account` if shown), enter the
service account email as the User name and the single-line JSON key as
the Password. Delete the local key file afterwards.

## Screenshots

| Page | Screenshot |
| :-- | :-- |
| Token Consumption | [01-token-consumption.png](screenshots/01-token-consumption.png) |
| Agent & Sessions | [02-agent-and-sessions.png](screenshots/02-agent-and-sessions.png) |
| Tool Usage | [03-tool-usage.png](screenshots/03-tool-usage.png) |
| LLM Interactions | [04-llm-interactions.png](screenshots/04-llm-interactions.png) |
| User Analytics | [05-user-analytics.png](screenshots/05-user-analytics.png) |
| Latency | [06-latency.png](screenshots/06-latency.png) |
| Errors | [07-errors.png](screenshots/07-errors.png) |
| Trace Inspector | [08-trace-inspector.png](screenshots/08-trace-inspector.png) |

## Live check

Use `tools/validate_live_powerbi.py` to cross-check all 37 charts
against the oracle SQL:

```bash
python dashboards/power_bi/tools/validate_live_powerbi.py \
  --workspace-id <ws> --dataset-id <model-id> \
  --pbi-token-file /path/to/pbi.token \
  --project <bq-project> --billing-project <bq-project> \
  --dataset <bq-dataset> --table agent_events \
  --location US --window default --window 2026-07-03:2026-07-16 \
  --output receipt.json
```

The validator runs each oracle SQL via `bq` and each DAX query via the
Power BI `executeQueries` API, then compares under exact-count,
float-tolerance, NULL≡BLANK, and tie-aware Top-N rules. The six KPI
scorecards are compared on both the value and its `Change %`.

`--window default` (also used when `--window` is omitted) is the loaded
Current window, read from the model's Date table, so the oracle and the
DAX query cover the window the report shows. If that lookup fails, the
validator warns and uses the fixture window, and the receipt records
which one it used in `window_details[].source`. An explicit
`YYYY-MM-DD:YYYY-MM-DD` window is validated before any query runs.

The run fails closed. A DAX error (response, result, or table level), a
value that is not a finite number, an off-by-one count, or more Top-N rows
than requested is a `FAIL`. A chart that is empty on both sides is
`EMPTY`, because nothing was compared. Pass = every chart `PASS` in every
requested window (`charts_pass == charts_expected`, which is 37 times the
number of windows) with at least one oracle row returned. Use
`--allow-empty` only for a deliberately empty window; the receipt records
it as `allow_empty`.

## Intentional differences from Looker Studio

| # | Area | Looker Studio | Power BI port | Why |
| :-- | :-- | :-- | :-- | :-- |
| D1 | Default window | Rolling 90 days incl. today | 90 days ending at newest partition (`auto`) | Today-relative is empty on the fixture |
| D2 | Date range freedom | Any range | Slicer limited to current window (locked filter) | Exact PoP for every reachable state |
| D3 | String identity | BigQuery case-sensitive, byte order | VertiPaq case-insensitive, culture-aware | Engine property; tie-aware validation |
| D4 | NULL-metric categories | SQL may list a NULL category | Not drawn | Power BI omits blank rows |
| D5 | Scorecard comparison | Comparison arrow | Card with the current value and a second callout bound to `KPI * Change %`, labelled `vs previous period`; `KPI * Prior` stays in the model | Native visual; a number, not an arrow |
| D6 | Layout | Absolute live positions | 1280-px canvas, header band, manifest 24-col geometry | Manifest geometry is complete |
| D7 | Controls | Filter bar + report date control | 5 synced page slicers + Filters-pane cards | Power BI idiom |
| D8 | Nav buttons | Link to separate dashboards | Navigate to report pages | Single report |
| D9 | Font | Inter | Segoe UI (Power BI default) | Availability |
| D10 | Freshness | Live query per view | Import, as of last refresh | Import mode |
| D11 | Cost shape | Per-view queries | Per refresh: 2 metadata + one pruned scan | One Import scan |
| D12 | Trace Inspector | Table, 7 fields | Same 7 fields, newest first | Parity |

## Schema credit

The PBIR JSON schemas used for offline validation are from
[microsoft/json-schemas](https://github.com/microsoft/json-schemas) at
commit `eecc60d35b9ab18b884d35c50abe7d09d2753cdf`, MIT-licensed. The
schemas are not vendored; the hash lock in
`spec/pbir_schemas.lock.json` records the 13 closure files (208,520
bytes) with their SHA-256 hashes. `validate_pbip.py --fetch-schemas`
downloads and verifies them at CI time.

Generated files (TMDL, PBIR JSON, `.platform`) are covered by the
repository license and carry no third-party content.
