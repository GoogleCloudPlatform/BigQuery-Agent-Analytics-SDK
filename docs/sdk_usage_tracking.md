# SDK Usage Tracking via `INFORMATION_SCHEMA.JOBS`

Every BigQuery job the SDK submits is labeled. Those labels land in
BigQuery's native `INFORMATION_SCHEMA.JOBS` views, so you can attribute
spend and usage back to the SDK without running a separate telemetry
pipeline.

This document is the operator cookbook: what labels exist, how to read
them, and ready-to-run SQL.

---

## Label schema

Applied by the SDK to every query job (`QueryJobConfig.labels`) and
load job (`LoadJobConfig.labels`) it submits.

| Key                | Value                                      | Scope |
| ------------------ | ------------------------------------------ | ----- |
| `sdk`              | constant `bigquery-agent-analytics`        | every SDK job |
| `sdk_version`      | `__version__`, BQ-safe (e.g. `0-4-0`)      | every SDK job |
| `sdk_surface`      | `python` \| `cli` \| `remote-function`     | every SDK job |
| `sdk_feature`      | `trace-read` \| `eval-code` \| `eval-llm-judge` \| `eval-categorical` \| `insights` \| `drift` \| `memory` \| `context-graph` \| `ontology-build` \| `ontology-gql` \| `views` \| `ai-ml` \| `feedback` | per-call site |
| `sdk_ai_function`  | `ai-generate` \| `ai-embed` \| `ai-classify` \| `ai-forecast` \| `ai-detect-anomalies` \| `ml-generate-text` \| `ml-generate-embedding` \| `ml-detect-anomalies` \| `ml-forecast` | AI/ML invocations only |

**Reserved namespace.** All `sdk*` keys are managed by the SDK. If a
caller pre-sets any of these on a `QueryJobConfig.labels` dict passed
to the SDK, the SDK overrides them and logs a one-shot `WARNING`. This
keeps telemetry trustworthy. Non-`sdk*` user labels (e.g.
`team=search`) are preserved unchanged and show up alongside the SDK
labels in `INFORMATION_SCHEMA` — useful for joining SDK spend against
your own cost-center dimensions.

**Privacy.** SDK labels never contain `user_id`, `session_id`,
`trace_id`, or any trace-extracted value. `INFORMATION_SCHEMA.JOBS` is
readable by anyone with `bigquery.jobs.listAll`; the SDK enforces the
`[a-z0-9_-]{1,63}` label-value format that BigQuery itself requires,
which also rejects most PII shapes (emails, UUIDs with dashes only
pass, etc. — avoid adding trace-derived values to any custom labels
you set).

**Out of scope.** Streaming inserts via `insert_rows_json` /
`tabledata.insertAll` are **not** jobs, do not support labels, and do
not appear in `INFORMATION_SCHEMA.JOBS`. To observe those, use Cloud
Audit Logs.

---

## Prerequisites

- Read access to `INFORMATION_SCHEMA.JOBS_BY_PROJECT` or
  `INFORMATION_SCHEMA.JOBS_BY_ORGANIZATION` — typically `bigquery.jobs.listAll`
  plus appropriate dataset/organization IAM.
- Replace `region-us` in the queries below with your BigQuery region
  (e.g. `region-eu`, `region-asia-northeast1`). The region is the
  BigQuery **multi-region or location** of the dataset where jobs run.

---

## Queries

### 1. Feature adoption over the last 30 days

Which SDK features are being used, from which surface, and how much
do they cost?

```sql
SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_feature') AS feature,
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_surface') AS surface,
  COUNT(*) AS jobs,
  SUM(total_bytes_billed) / POW(2, 40) AS tib_billed,
  SUM(TIMESTAMP_DIFF(end_time, start_time, MILLISECOND)) / 1000.0 / 60
    AS total_minutes
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'sdk')
GROUP BY feature, surface
ORDER BY jobs DESC;
```

### 2. AI/ML function cost breakdown

Where is your `AI.GENERATE` / `AI.EMBED` / `AI.FORECAST` spend going?

```sql
SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_ai_function')
    AS ai_function,
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_feature') AS feature,
  COUNT(*) AS jobs,
  SUM(total_bytes_billed) / POW(2, 40) AS tib_billed,
  AVG(TIMESTAMP_DIFF(end_time, start_time, MILLISECOND)) AS avg_ms
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND EXISTS (
    SELECT 1 FROM UNNEST(labels) WHERE key = 'sdk_ai_function'
  )
GROUP BY ai_function, feature
ORDER BY tib_billed DESC;
```

### 3. Slowest feature per day (p50 / p95 latency)

Which features are degrading or have runaway outliers?

```sql
SELECT
  DATE(creation_time) AS day,
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_feature') AS feature,
  COUNT(*) AS jobs,
  APPROX_QUANTILES(
    TIMESTAMP_DIFF(end_time, start_time, MILLISECOND), 100
  )[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(
    TIMESTAMP_DIFF(end_time, start_time, MILLISECOND), 100
  )[OFFSET(95)] AS p95_ms
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'sdk')
  AND state = 'DONE'
GROUP BY day, feature
HAVING jobs >= 5
ORDER BY day DESC, p95_ms DESC;
```

### 4. Version adoption after a release

How many jobs are still on the old version after you cut a new one?

```sql
SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_version') AS sdk_version,
  DATE(creation_time) AS day,
  COUNT(*) AS jobs
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'sdk')
GROUP BY sdk_version, day
ORDER BY day DESC, jobs DESC;
```

### 5. Surface attribution (who is calling the SDK?)

Split spend across direct Python users, CLI invocations, and the
deployed remote-function runtime.

```sql
SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_surface') AS surface,
  COUNT(*) AS jobs,
  SUM(total_bytes_billed) / POW(2, 40) AS tib_billed
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'sdk')
GROUP BY surface
ORDER BY tib_billed DESC;
```

### 6. Errors by feature

Are any SDK features failing disproportionately?

```sql
SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_feature') AS feature,
  error_result.reason AS reason,
  COUNT(*) AS failed_jobs
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'sdk')
  AND state = 'DONE'
  AND error_result.reason IS NOT NULL
GROUP BY feature, reason
ORDER BY failed_jobs DESC;
```

### 7. Custom caller labels joined with SDK labels

If your callers add their own labels (e.g. `team=search`,
`env=prod`) before handing a `QueryJobConfig` to the SDK, those
survive and co-exist with the SDK's labels. You can slice SDK usage
by your own cost-center dimensions:

```sql
SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'team') AS team,
  (SELECT value FROM UNNEST(labels) WHERE key = 'sdk_feature') AS feature,
  COUNT(*) AS jobs,
  SUM(total_bytes_billed) / POW(2, 40) AS tib_billed
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'sdk')
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'team')
GROUP BY team, feature
ORDER BY tib_billed DESC;
```

---

## Opting in and out

### By default: opt-in

Constructing the SDK the normal way gets you labels on every job:

```python
from bigquery_agent_analytics import Client

# sdk_surface defaults to "python"; bq_client is lazily built via
# make_bq_client, which returns a LabeledBigQueryClient.
client = Client(project_id="my-proj", dataset_id="analytics")
```

### Explicitly construct the labeled client

If you need your own `google.cloud.bigquery.Client` configuration
(custom `client_info`, `default_query_job_config`, transport, etc.)
but still want SDK labels, use `make_bq_client`:

```python
from bigquery_agent_analytics import make_bq_client, Client

bq = make_bq_client(project="my-proj", location="US", sdk_surface="python")
# ... mutate bq.default_query_job_config, etc., if you want.

client = Client(project_id="my-proj", dataset_id="analytics", bq_client=bq)
```

### Pass your own client — labels are NOT applied

If you pass a vanilla `bigquery.Client` to `Client(bq_client=...)`,
the SDK honors it as-is (no reconstruction, so your
`default_query_job_config` and other settings survive) and logs a
one-shot `WARNING` noting that SDK labels will not be applied:

```python
from google.cloud import bigquery
from bigquery_agent_analytics import Client

client = Client(
    project_id="my-proj",
    dataset_id="analytics",
    bq_client=bigquery.Client(project="my-proj"),
    # Jobs from this Client will NOT carry sdk_* labels.
    # The SDK logs one WARNING explaining how to opt in.
)
```

---

## Related

- See `SDK.md` for the full consumption-layer API reference.
- See [issue #52 on GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK][issue-52]
  for the design discussion and rollout history.

[issue-52]: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/52
