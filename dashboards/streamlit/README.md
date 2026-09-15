# Streamlit Dashboard for BigQuery Agent Analytics

Interactive dashboard for monitoring, diagnosing, and evaluating AI agent traces in Google BigQuery.

---

## 1. Install

Install dependencies with the `streamlit` extra:

```bash
pip install -e '.[streamlit]'
```

---

## 2. Configure

Copy the sample environment file and configure your parameters:

```bash
cp dashboards/streamlit/.env.sample dashboards/streamlit/.env
```

Parameters configured in `dashboards/streamlit/.env`:

| Variable                         | Description                              | Default                            |
| :------------------------------- | :--------------------------------------- | :--------------------------------- |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to service account JSON key file    | `dashboards/streamlit/sa-key.json` |
| `BQ_PROJECT_ID`                  | Google Cloud project ID                  | `my-gcp-project`                   |
| `BQ_DATASET_ID`                  | BigQuery dataset containing agent events | `agent_analytics`                  |
| `BQ_TABLE_ID`                    | Raw agent events table                   | `agent_events`                     |
| `BQ_VIEW_PREFIX`                 | Prefix for typed analytical views        | `adk_`                             |
| `STREAMLIT_LAZY_TABS`            | Only query data for active tab           | `true`                             |

---

## 3. Prerequisites & Typed Views

The dashboard queries typed analytical views (`adk_llm_responses`, `adk_tool_starts`, etc.) created over the raw events table.

### IAM Roles

Ensure the query identity (service account or `gcloud auth application-default login`) has:
* **`roles/bigquery.jobUser`** on the Google Cloud project to run query jobs.
* **`roles/bigquery.dataViewer`** on the dataset to read tables and views.

### Create Views

Generate the typed analytical views matching your project, dataset, and events table:

```bash
bq-agent-sdk views create-all \
  --project-id YOUR_PROJECT \
  --dataset-id YOUR_DATASET \
  --table-id YOUR_TABLE
```

> **Note:** Views default to the `adk_` prefix. If you specify a custom prefix with `--prefix`, set `BQ_VIEW_PREFIX` in `.env` (or in the dashboard sidebar) to match.

---

## 4. Run

Run from the dashboard directory (recommended, automatically applies `.streamlit/config.toml`):

```bash
cd dashboards/streamlit
streamlit run app.py
```

Or run from the repository root with explicit localhost binding:

```bash
streamlit run dashboards/streamlit/app.py --server.address 127.0.0.1
```

### Deployment & Access Control

> **Note:**
> * `.streamlit/config.toml` configures loopback binding (`127.0.0.1`), headless mode, and disables telemetry when running from `dashboards/streamlit/`.
> * When launching from the repository root, pass `--server.address 127.0.0.1` to ensure the dashboard binds only to localhost.
> * If remote access is needed, front it with an authenticating reverse proxy or IAP.
> * Every viewer shares the server's BigQuery identity.
> * The per-query scan cap is configurable in the sidebar.

---

## 5. Sidebar Controls

The sidebar provides runtime controls and guardrails for query execution:

* **Time range selector**: Snaps query execution to discrete sliding windows (Last 1 hour, Last 6 hours, Last 24 hours, Last 3 days, Last 7 days, Last 30 days) with bucketed intervals.
* **Per-query scan cap guardrail (`maximum_bytes_billed`)**: Sets a strict byte limit on every BigQuery job. A free dry-run preflight validates query scan size before execution, preventing queries from running if they exceed the selected cap rather than billing for unexpected costs.
* **Token pricing defaults**: Configures input and output token rates for estimated cost calculations (defaults to \$1.25 / 1M input tokens and \$5.00 / 1M output tokens). Note that costs are derived from token counts rather than recorded billing telemetry.
* **Streaming Buffer Scans**: In scenarios where on-demand compute pricing is used and the queried data reside in BigQuery's streaming buffer, BigQuery does not charge for bytes scanned from the streaming buffer.
