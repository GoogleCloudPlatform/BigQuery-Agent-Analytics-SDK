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

-- Add identity/scope pins when they are already known.
SELECT `my-project.agent_analytics.agent_analytics`(
  'analyze', JSON '{
    "session_id": "s1",
    "user_id": "user-42",
    "root_agent_name": "support_agent",
    "experiment_id": "exp-7",
    "custom_labels": {"run": "v1"}
  }'
);

-- Exact retry: carry candidates[0].selector through as JSON.
WITH initial AS (
  SELECT `my-project.agent_analytics.agent_analytics`(
    'analyze', JSON '{"session_id": "s1"}'
  ) AS result
)
SELECT `my-project.agent_analytics.agent_analytics`(
  'analyze',
  JSON_OBJECT(
    'selector',
    JSON_QUERY(result, '$._error.details.candidates[0].selector')
  )
)
FROM initial;

SELECT `my-project.agent_analytics.agent_analytics`(
  'evaluate', JSON '{"metric": "latency"}'
);
```

`session_id` is a conversation identifier, so an unpinned `analyze` request can
match multiple identities or scopes. That row returns
`_error.code = "AmbiguousSessionError"` and the retry-ready payload under
`_error.details`; it does not choose one candidate implicitly. Preserve JSON
`null` values in the selected selector because they pin SQL `NULL`, while an
absent field is unpinned.

The printable error message is redacted, but the structured selector includes
user, root-agent, experiment, label, and scope-signature metadata. Treat it as
sensitive. Event content and judge context are not included.

## Files

| File | Description |
|------|-------------|
| `deploy.sh` | End-to-end deploy script (build, stage, deploy, connect) |
| `main.py` | Cloud Function entry point |
| `dispatch.py` | Request routing and SDK dispatch |
| `register.sql` | DDL template for registering the remote function |
