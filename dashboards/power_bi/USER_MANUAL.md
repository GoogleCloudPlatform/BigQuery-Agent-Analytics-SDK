# BigQuery Agent Analytics Dashboard — Power BI User Manual

This guide is for people who **use** the dashboard: you run agents that
log to BigQuery through the
[ADK BigQuery Agent Analytics plugin](https://adk.dev/observability/
bigquery-agent-analytics/), and you want charts in Power BI. You do not
need to write SQL or read the rest of this repository. If you want to
change or validate the dashboard itself, see the
[contributor README](README.md) instead.

**What you get:** your own copy of an 8-page Power BI dashboard — token
consumption, sessions, tool usage, LLM calls, user analytics, latency,
tool errors, and a trace inspector — built on the `agent_events` table
your agents already write. Your copy is private, reads your data with
your credentials, and bills your project.

---

## Before you start

You need four things:

1. **A BQAA table with data in it.** If your agents run with the ADK
   BigQuery Agent Analytics plugin, this table already exists — it is
   the table the plugin writes to, normally named `agent_events`.
2. **A Google account that can read that table _and_ run BigQuery jobs**
   in the project that will pay for queries.
3. **A Power BI license.** A per-user Premium (PPU) license or a Fabric
   capacity is required to deploy and refresh the semantic model.
4. **The generated Power BI project.** Clone this repository and run
   the generator (see the [README](README.md)).

---

## Deploy your dashboard

### Option A — 1-Click Web Configurator & `.zip` Template Download (Fastest, No Clone Required)

1. Open the [**Power BI Web Configurator**](https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/power-bi/) (the hosted page goes live after merge to `main`, because `.github/workflows/looker-studio-pages.yml` publishes from `main` only; before then, or for local use, serve over HTTP via `python3 -m http.server 8799 --directory dashboards/power_bi` and open `http://localhost:8799/docs/index.html`, because browsers block ES module imports such as `./configurator.mjs` over `file://`) — you can also explore all 8 dashboard pages immediately in the interactive **8-Page Dashboard Tour** on that page.
2. Paste your `project.dataset.table` (or click **Use public demo fixture** to try `test-project-0728-467323.bqaa_fixture_adk_1_27_0.agent_events`).
3. Click **Download pre-configured PBIP (.zip)** to download `BQAA_Agent_Analytics_<project>_<dataset>_<table>.zip` with your BigQuery parameters already baked into `expressions.tmdl`.
4. Extract the `.zip` and open `BQAA_Agent_Analytics.pbip` in Power BI Desktop (Developer Mode enabled), or deploy to a Power BI / Fabric workspace.

To share your settings with a teammate, click **Copy setup link**. The link keeps them in the URL fragment (the part of the address after `#`), which browsers never send to the server in HTTP requests, so share copied links deliberately.

You can also generate the same pre-configured `.zip` bundle from the CLI:

```bash
python dashboards/power_bi/tools/generate_pbip.py \
  --output-zip BQAA_Agent_Analytics.zip \
  --project your-gcp-project \
  --dataset bq_agent_analytics \
  --table agent_events
```

### Option B — Generate and Deploy from a Repository Clone

#### Step 1 — Generate the project

```bash
python dashboards/power_bi/tools/generate_pbip.py --write
```

This writes the TMDL semantic model and PBIR report under
`dashboards/power_bi/`.

#### Step 2 — Deploy to a workspace

Use the deploy helper with a Fabric bearer token:

```bash
python dashboards/power_bi/tools/deploy_fabric.py \
  --workspace-id <workspace-guid> \
  --token-file /path/to/fabric.token \
  --project-dir dashboards/power_bi \
  --display-name "BQAA Agent Analytics"
```

Or, if your tenant supports Git integration, connect the workspace to
this repository and sync.

### Step 3 — Set BigQuery credentials

In Power BI Service, open the semantic model, go to **Settings → Data
source credentials → Edit credentials**, choose **Google BigQuery →
OAuth2**, sign in with a Google account that can read your BQAA table,
and set the privacy level to **Organizational**.

### Step 4 — Refresh

Click **Refresh now** on the semantic model. The first refresh runs
the anchor metadata query and the Import-mode BigQuery scan, then loads
the data into the VertiPaq engine.

---

## Parameters

You can change the BigQuery connection and date window without editing
any code. In Power BI Service, open the semantic model and go to
**Settings → Parameters**.

| Parameter | What it does |
| :-- | :-- |
| `BQAA_ProjectId` | The GCP project containing your BQAA table |
| `BQAA_DatasetId` | The BigQuery dataset |
| `BQAA_TableId` | The BigQuery table (normally `agent_events`) |
| `BQAA_BillingProjectId` | The project that pays for queries |
| `BQAA_WindowEndDate` | `auto` (newest partition) or a `YYYY-MM-DD` date |
| `BQAA_LookbackDays` | How many days of data to load (default 90) |
| `BQAA_UseStorageApi` | Use the BigQuery Storage Read API (default `true`) |

After changing a parameter, click **Apply** and then refresh the model.

---

## Page guide

### 1. Token Consumption

Total tokens consumed over time, top users by token consumption, and
token usage split by agent. A scorecard shows the current total tokens.

### 2. Agent & Sessions

Total traces, trace volume over time, top users by traces, traces by
agent, total sessions, session volume over time, and top agents by
sessions. Scorecards show total traces and total sessions.

### 3. Tool Usage

Tool calls over time (broken down by tool name), tool invocations, and tool
completions by agent.

### 4. LLM Interactions

Total LLM calls, LLM call volume over time, and top agents by LLM
calls. A scorecard shows total LLM calls.

### 5. User Analytics

Total users, user growth over time, top users by sessions, and top
users by events. A navigation button links to the Latency page.

### 6. Latency

Average tool latency, tool latency over time, percentile cards
(P50–P99) for tool and LLM latency, average LLM latency, and LLM
latency over time. A navigation button links to the Token Consumption
page.

### 7. Errors

Total tool errors, tool errors over time, top agents by tool errors,
and top tools by errors. A scorecard shows total tool errors.

### 8. Trace Inspector

A table showing all events (timestamp, event type, agent, user, trace,
span, status), sorted by timestamp descending. Filter by date, agent,
user, trace ID, or span ID using the slicers at the top.

---

## Using the dashboard

### Slicers

Each page has five slicers at the top: Date, Agent, User ID, Trace ID,
and Span ID. The slicers sync across pages — selecting an agent on one
page applies the same filter on all pages.

### Period-over-period

Each KPI card (Total Tokens, Total Traces, Total Sessions, LLM Calls,
Total Users, or Tool Errors) shows the current value and, as a second
callout labelled "vs previous period", the percentage change from the
prior window (the `KPI * Change %` measure). The prior window is the same
number of days immediately before the current selection. The
`KPI * Prior` measures stay in the semantic model if you want the prior
value itself.

### Date range

The Date slicer is limited to the current window (set by
`BQAA_LookbackDays` and `BQAA_WindowEndDate`). You can narrow the range
within the window but cannot go beyond it. This ensures the
period-over-period calculation always has data.

### Cross-filtering

Clicking a bar in a bar chart filters the other charts on the page
(except the four charts that do not listen to the User ID slicer: Top
5 Users by Token Consumption, Token Usage by Agent, Top 5 Users by
Traces, and Traces by Agent).

---

## Troubleshooting

### Refresh fails with `BQAA.InvalidParameter`

One of the M parameters failed validation. Check that the project,
dataset, and table IDs match the allowed character sets (see the
[README](README.md#parameters)).

### Refresh fails with a BigQuery permission error

The account used for BigQuery credentials needs `bigquery.dataViewer`
on the table and `bigquery.jobUser` in the billing project.

### Refresh fails with a Storage API error

Set `BQAA_UseStorageApi = false` in the model parameters and refresh
again.

### Charts show no data

Verify that the date window overlaps with your data. The default
`auto` mode finds the newest partition; if the table is empty or all
partitions are in the streaming buffer, it falls back to today. Try
setting `BQAA_WindowEndDate` to an explicit date that has data.

### Performance is slow

The Import-mode model scans BigQuery once per refresh, loading 2× the
lookback days of data. For large tables, reduce `BQAA_LookbackDays`
or set an explicit `BQAA_WindowEndDate` to skip the metadata query.
