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

## 3. Run

Launch the Streamlit dashboard:

```bash
streamlit run dashboards/streamlit/app.py
```
