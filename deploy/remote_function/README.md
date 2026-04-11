# Remote Function Deployment

Deploy the SDK as a BigQuery Remote Function so you can call it from SQL.

## What It Does

Exposes the SDK as a Cloud Function (gen2) behind a BigQuery remote function.
Once registered, you can run SDK operations — trace analysis, evaluation, and
more — directly from BigQuery SQL.

## Prerequisites

- `gcloud` CLI authenticated with sufficient permissions
- Cloud Functions API and Cloud Build API enabled
- A BigQuery dataset to host the function

## Deploy

```bash
cd deploy/remote_function
./deploy.sh PROJECT [FUNCTION_REGION] [DATASET] [BQ_LOCATION]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `PROJECT` | *required* | GCP project ID |
| `FUNCTION_REGION` | `us-central1` | Cloud Function region |
| `DATASET` | `agent_analytics` | BigQuery dataset for the function |
| `BQ_LOCATION` | `US` | BigQuery dataset location (must match the dataset) |

The script builds an SDK wheel from the repo, stages a deployment bundle, deploys
the Cloud Function, creates a BigQuery CLOUD_RESOURCE connection, and grants the
invoker role.

## Register the Function

After deployment, register the remote function in BigQuery. Replace the
placeholders in `register.sql` and run it, or copy the DDL printed by
`deploy.sh`.

```sql
CREATE OR REPLACE FUNCTION `PROJECT.DATASET.agent_analytics`(
  operation STRING, params JSON
) RETURNS JSON
REMOTE WITH CONNECTION `PROJECT.BQ_LOCATION.analytics-conn`
OPTIONS (
  endpoint = 'https://FUNCTION_REGION-PROJECT.cloudfunctions.net/bq-agent-analytics',
  max_batching_rows = 50
);
```

## Usage

```sql
SELECT `my-project.agent_analytics.agent_analytics`(
  'analyze', JSON '{"session_id": "s1"}'
);

SELECT `my-project.agent_analytics.agent_analytics`(
  'evaluate', JSON '{"metric": "latency"}'
);
```

## Files

| File | Description |
|------|-------------|
| `deploy.sh` | End-to-end deploy script (build, stage, deploy, connect) |
| `main.py` | Cloud Function entry point |
| `dispatch.py` | Request routing and SDK dispatch |
| `register.sql` | DDL template for registering the remote function |
