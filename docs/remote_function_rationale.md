# Why the SDK Needs a BigQuery Remote Function Interface

**Status:** Proposal
**Parent PRD:** [Unified Analytics Interface](prd_unified_analytics_interface.md)
**Date:** 2026-03-06

---

## 1. The Problem

The BigQuery Agent Analytics SDK has 16+ analytical capabilities — trace
retrieval, latency evaluation, LLM-as-judge scoring, drift detection,
insights extraction — but every one requires writing Python code. This
locks out the largest user group in the data ecosystem: **SQL analysts**.

```
Today:
  Analyst → "I need agent quality metrics in my dashboard"
          → Files ticket to Python engineer
          → Engineer writes notebook, exports CSV
          → Analyst imports into Looker
          → 3-day turnaround, stale by arrival

With Remote Function:
  Analyst → SELECT agent_analytics('analyze', JSON'{"session_id":"s1"}')
          → Scheduled query materializes nightly
          → Looker reads table directly
          → 10-minute setup, always fresh
```

### Who is blocked today

| User | What they need | Why Python-only blocks them |
|------|---------------|---------------------------|
| **Data analysts** | Agent quality dashboards in Looker | Cannot run Python in BigQuery console |
| **BI engineers** | Scheduled quality reports | Cannot add Python dependencies to scheduled queries |
| **Data scientists** | Batch evaluation of 10K sessions | SDK works per-session; SQL can fan out natively |
| **On-call SREs** | Ad-hoc investigation in BigQuery | Switching to a notebook during an incident adds friction |

---

## 2. The Solution: One Multiplexed Remote Function

Deploy the SDK as a Cloud Function, register it as a BigQuery Remote
Function, and expose all operations through a single SQL-callable endpoint:

```sql
agent_analytics(operation STRING, params JSON) RETURNS JSON
```

### Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────┐
│   BigQuery      │────▶│  Cloud Function       │────▶│  BigQuery   │
│   SELECT fn()   │     │  (SDK Core)           │     │  (queries)  │
│                 │◀────│  handle_request()      │◀────│             │
│   Returns JSON  │     └──────────────────────┘     └─────────────┘
└─────────────────┘
```

BigQuery sends batched rows via HTTP POST. Each row is an
`[operation, params_json]` tuple. The function dispatches to the
appropriate SDK method and returns JSON results. BigQuery handles
retry (HTTP 408/429/5xx) and batching automatically.

### Supported Operations

| Operation | What it does | Example params |
|-----------|-------------|----------------|
| `analyze` | Trace metrics (spans, errors, latency) | `{"session_id": "s1"}` |
| `evaluate` | Code-based evaluation (latency, error rate) | `{"session_id": "s1", "metric": "latency", "threshold": 5000}` |
| `judge` | LLM-as-judge scoring | `{"session_id": "s1", "criterion": "correctness"}` |
| `insights` | Intent/outcome/friction extraction | `{"session_id": "s1"}` |
| `drift` | Golden-set coverage analysis | `{"golden_dataset": "golden_qs", "agent_filter": "bot"}` |

---

## 3. Concrete User Journeys

### Journey 1: Nightly Quality Dashboard

Priya (data analyst) creates a scheduled query — no Python needed:

```sql
-- Runs nightly, materializes agent quality scores
CREATE OR REPLACE TABLE `project.analytics.daily_quality` AS
WITH recent AS (
  SELECT DISTINCT session_id, MIN(timestamp) AS ts, ANY_VALUE(agent) AS agent
  FROM `project.analytics.agent_events`
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
  GROUP BY session_id
)
SELECT
  session_id, ts, agent,
  JSON_VALUE(r, '$.error_count') AS error_count,
  CAST(JSON_VALUE(r, '$.avg_latency_ms') AS FLOAT64) AS avg_latency_ms,
  JSON_VALUE(r, '$.tool_call_count') AS tool_calls
FROM recent,
UNNEST([`project.analytics.agent_analytics`('analyze',
  JSON_OBJECT('session_id', session_id))]) AS r;
```

She connects Looker to `daily_quality` — dashboard shows latency trends,
error rates, and tool usage by agent. Total setup: **~30 minutes**.

### Journey 2: LLM-as-Judge at Scale

Score every session for correctness, surface the worst ones for review:

```sql
SELECT session_id,
  CAST(JSON_VALUE(j, '$.score') AS FLOAT64) AS score,
  JSON_VALUE(j, '$.feedback') AS feedback
FROM (
  SELECT DISTINCT session_id
  FROM `project.analytics.agent_events`
  WHERE agent = 'support_bot' AND timestamp >= '2026-03-01'
) s,
UNNEST([`project.analytics.agent_analytics`('judge',
  JSON_OBJECT('session_id', s.session_id, 'criterion', 'correctness'))]) AS j
WHERE CAST(JSON_VALUE(j, '$.score') AS FLOAT64) < 0.7
ORDER BY score ASC;
```

### Journey 3: Weekly Drift Alert

Scheduled query detects when production questions drift from the golden set:

```sql
SELECT
  JSON_VALUE(d, '$.coverage_percentage') AS coverage_pct,
  JSON_QUERY(d, '$.uncovered_questions') AS gaps
FROM UNNEST([`project.analytics.agent_analytics`('drift',
  JSON_OBJECT('golden_dataset', 'project.analytics.golden_questions',
              'agent_filter', 'support_bot',
              'start_date', '2026-03-01', 'end_date', '2026-03-06'))]) AS d;
```

If coverage drops below 85%, a BigQuery alert triggers a Slack notification.

---

## 4. Deployment & Security

### One-Time Setup (Platform Engineer)

```bash
# 1. Deploy Cloud Function
gcloud functions deploy bq-agent-analytics \
  --gen2 --runtime python312 --region us-central1 \
  --entry-point handle_request --source ./deploy/remote_function/ \
  --trigger-http --no-allow-unauthenticated

# 2. Create CLOUD_RESOURCE connection
bq mk --connection --location=US --connection_type=CLOUD_RESOURCE \
  --project_id=PROJECT analytics-conn

# 3. Grant connection SA → Cloud Run Invoker
CONNECTION_SA=$(bq show --connection --format=json PROJECT.us.analytics-conn \
  | jq -r '.cloudResource.serviceAccountId')
gcloud functions add-invoker-policy-binding bq-agent-analytics \
  --region=us-central1 --member="serviceAccount:${CONNECTION_SA}"

# 4. Register the remote function
bq query --use_legacy_sql=false '
CREATE FUNCTION `PROJECT.analytics.agent_analytics`(
  operation STRING, params JSON
) RETURNS JSON
REMOTE WITH CONNECTION `PROJECT.us.analytics-conn`
OPTIONS (
  endpoint = "https://us-central1-PROJECT.cloudfunctions.net/bq-agent-analytics",
  max_batching_rows = 50
)'
```

### Least-Privilege IAM

| Service Account | Roles | Scope |
|----------------|-------|-------|
| Cloud Function runtime SA | `bigquery.dataViewer`, `bigquery.jobUser` | Analytics dataset + project |
| BQ Connection SA (auto-created) | `run.invoker` | The Cloud Function |

The Cloud Function SA only needs read access to `agent_events`. It cannot
write data or modify schemas.

### API Contract

- **Versioning:** Responses include `_version: "1.0"`. v1.x is additive-only.
- **Partial failure:** If 1 of 5 batched rows fails, the other 4 succeed.
  Failed rows return `{"_error": {"code": "SESSION_NOT_FOUND", ...}}`.
- **Idempotency:** All read-only operations (`analyze`, `evaluate`) are
  naturally idempotent. `judge` is non-deterministic (LLM output varies).

---

## 5. Cost & Performance

### Cost per 1K Sessions

| Component | Cost | Notes |
|-----------|------|-------|
| BigQuery scan | ~$0.03 | ~6 MB/session, $6.25/TB on-demand |
| Cloud Function | ~$0.01 | 256 MB, ~500ms/invocation |
| LLM calls (`judge` only) | ~$1.25 | Gemini 2.5 Flash, ~600 tokens/call |

A nightly evaluation of 1K sessions costs **< $0.05** without LLM,
**~$1.30** with LLM-as-judge. Partitioning `agent_events` by timestamp
reduces scan cost by 90%+.

### Performance Targets

| Metric | Target |
|--------|--------|
| `analyze` p95 latency | < 5 seconds |
| `evaluate` p95 latency | < 5 seconds |
| `judge` p95 latency | < 15 seconds (LLM-bound) |
| Cold start | < 3 seconds (Cloud Function gen2) |
| Availability | 99.5% (Cloud Function + BQ connection) |

### Why Not Just Use Continuous Queries?

Continuous Queries (`APPENDS()` + `AI.GENERATE_TEXT`) are excellent for
real-time per-row processing, but they **cannot** use `JOIN`, `GROUP BY`,
aggregations, or user-defined remote functions. The SDK's analytical
operations — trace assembly, drift detection, batch evaluation — require
cross-row logic that only Remote Functions can provide in SQL.

| Need | Remote Function | Continuous Query |
|------|----------------|-----------------|
| Aggregate session metrics | Yes | No (no GROUP BY) |
| Compare against golden set | Yes (JOIN) | No (no JOIN) |
| Batch evaluate 10K sessions | Yes | No (per-row only) |
| Real-time error classification | No (batch) | Yes |

The two paths are complementary, not competing.

---

## Summary

| Without Remote Function | With Remote Function |
|------------------------|---------------------|
| SQL analysts file tickets for Python engineers | SQL analysts self-serve with `SELECT agent_analytics(...)` |
| Dashboard data is days stale | Scheduled queries materialize nightly |
| Batch evaluation requires notebooks | `SELECT ... FROM sessions` fans out natively |
| Drift detection is a manual Python script | Scheduled query alerts automatically |
| 3-day turnaround for quality reports | 30-minute setup, always fresh |

The Remote Function is the bridge between the SDK's analytical depth and
BigQuery's reach. It turns every SQL user into an agent analytics user.
