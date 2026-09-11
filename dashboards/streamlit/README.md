# Streamlit Dashboard for BigQuery Agent Analytics

Interactive dashboard for monitoring, diagnosing, and evaluating AI agent traces in Google BigQuery.

---

## 1. Install

Install dependencies with the `dashboards` extra:

```bash
pip install '.[streamlit]'
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

Launch the Streamlit dashboard:

```bash
streamlit run dashboards/streamlit/app.py
```

---

## 5. Sidebar Controls

The sidebar provides runtime controls and guardrails for query execution:

* **Time range selector**: Snaps query execution to discrete sliding windows (e.g., Last 15 minutes, Last 24 hours, Last 7 days, Last 30 days) with bucketed intervals.
* **Per-query scan cap guardrail (`maximum_bytes_billed`)**: Sets a strict byte limit on every BigQuery job. A free dry-run preflight validates query scan size before execution, preventing queries from running if they exceed the selected cap rather than billing for unexpected costs.
* **Token pricing defaults**: Configures input and output token rates for estimated cost calculations (defaults to \$1.25 / 1M input tokens and \$5.00 / 1M output tokens). Note that costs are derived from token counts rather than recorded billing telemetry.
