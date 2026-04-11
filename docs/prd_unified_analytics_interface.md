# PRD: Unified Analytics Interface for BigQuery Agent Analytics SDK

**Status:** Draft
**Issue:** [#22](https://github.com/haiyuan-eng-google/BigQuery-Agent-Analytics-SDK/issues/22)
**Author:** SDK Product Team
**Date:** 2026-03-06

---

## 1. Validation & Rationale

### 1.1 Is This a Necessary Update?

**Yes.** The SDK today is a Python-library-only toolkit with 16+ analytical
capabilities (trace retrieval, evaluation, drift detection, insights, context
graphs, etc.), but every one of them requires a user to write Python code and
import the library. This creates three gaps:

| Gap | Who is Affected | Why It Matters |
|-----|----------------|----------------|
| **SQL-native analytics** | Data analysts, BI engineers, Looker/Data Studio users | Cannot run `SELECT analyze(session_id) FROM traces` — must leave BigQuery to run Python |
| **Agent self-diagnostics** | Autonomous AI agents (ADK, LangChain, CrewAI) | Agents cannot inspect their own performance without generating complex SQL; CLIs are the natural LLM tool interface |
| **Automation & CI/CD** | Platform engineers, SRE teams | No scriptable CLI for cron-based eval runs, alerting pipelines, or `git bisect`-style regression checks |

### 1.2 Current State

```
┌──────────────────────────────────────────────────┐
│           BigQuery Agent Analytics SDK           │
│                                                  │
│  Client.get_trace()      Client.evaluate()       │
│  Client.insights()       Client.drift_detection()│
│  Client.doctor()         Client.deep_analysis()  │
│  Client.hitl_metrics()   Client.context_graph()  │
│  ViewManager             BigQueryTraceEvaluator   │
│  TrialRunner             GraderPipeline           │
│  EvalSuite               EvalValidator            │
│  BigQueryMemoryService   BigQueryAIClient         │
│                                                  │
│  ACCESS: Python import ONLY                      │
└──────────────────────────────────────────────────┘
```

### 1.3 Proposed State

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                          Shared Core (Python)                                │
│     Client, evaluators, insights, feedback, trace, context_graph             │
├───────────────┬────────────────────┬────────────────────┬────────────────────┤
│ Python Lib    │ BQ Remote Function │ Continuous Query   │ CLI               │
│ (existing)    │ (Path A — Batch)   │ (Path A' — Stream) │ (Path B — Agent)  │
│               │                    │                    │                   │
│ import Client │ SELECT fn(...)     │ APPENDS() +        │ $ bq-agent-sdk    │
│ Notebooks     │ Scheduled queries  │ AI.GENERATE_TEXT   │   evaluate ...    │
│ Python apps   │ Looker, dashboards │ → BQ / Pub/Sub /   │   insights ...    │
│               │                    │   Bigtable / Spanner│                   │
└───────────────┴────────────────────┴────────────────────┴────────────────────┘
```

### 1.4 MVP Scope

**v1.0 = CLI with `evaluate`, `get-trace`, `doctor`, and `--exit-code`.**

This is the smallest cut that unblocks all three personas. Remote Function
(Path A) and Continuous Query (Path A') ship in v1.1/v1.2. See §8.1 for
the full MVP definition, feature-to-version mapping, and rationale.

---

## 2. User Personas

### Persona A: Priya — Data Analyst (Remote Function Path)

- Works in BigQuery console and Looker daily
- Comfortable with SQL, not with Python notebooks
- Needs to build dashboards showing agent quality metrics
- Wants to run evaluation and insights directly inside scheduled SQL queries

### Persona B: AgentX — Autonomous AI Agent (CLI Path)

- An ADK-based agent deployed in production
- Has tool-calling capability (can invoke shell commands)
- Needs to check its own latency, error rates, and drift before responding
- Must minimize token overhead — CLI commands are cheaper than SQL generation

### Persona C: Marcus — Platform Engineer (CLI Path)

- Manages agent fleet in CI/CD pipelines
- Needs nightly eval runs with pass/fail gates
- Wants `bq-agent-sdk evaluate ... --exit-code` in GitHub Actions
- Pipes output to monitoring systems (Datadog, PagerDuty)

---

## 3. Path A: BigQuery Remote Function Interface

### 3.1 Overview

Deploy the SDK's analytical logic as a Google Cloud Function (or Cloud Run
service), register it as a BigQuery Remote Function, and let SQL users call
SDK features directly from `SELECT` statements.

### 3.2 Supported Operations

All operations go through a single multiplexed function:
`agent_analytics(operation STRING, params JSON) RETURNS JSON`

| Operation | SDK Method | Params (JSON keys) | Output |
|-----------|-----------|---------------------|--------|
| `analyze` | `Client.get_session_trace()` + metrics | `session_id` | JSON with span count, error count, latency, tool calls |
| `evaluate` | `CodeEvaluator` | `session_id`, `metric`, `threshold` | JSON with passed, score, details |
| `judge` | `LLMAsJudge` | `session_id`, `criterion` | JSON with score, feedback |
| `insights` | Facet extraction | `session_id` | JSON with intent, outcome, friction |
| `drift` | Drift detection | `golden_dataset`, `agent_filter`, `start_date`, `end_date` | JSON with coverage, gaps |

### 3.3 Critical User Journeys (CUJ)

#### CUJ-A1: Priya Builds an Agent Quality Dashboard

**Goal:** Create a Looker dashboard showing per-session quality scores
updated nightly.

**Journey:**

```
Step 1: Platform team deploys SDK as Cloud Function
        $ gcloud functions deploy bq-agent-analytics \
            --runtime python312 \
            --entry-point handle_request \
            --source ./deploy/remote_function/

Step 2: Platform team registers a single multiplexed Remote Function
        CREATE FUNCTION `project.analytics.agent_analytics`(
          operation STRING,
          params JSON
        ) RETURNS JSON
        REMOTE WITH CONNECTION `project.us.analytics-conn`
        OPTIONS (
          endpoint = 'https://us-central1-project.cloudfunctions.net/bq-agent-analytics'
        );

Step 3: Priya writes a scheduled query (no Python needed)
        -- Nightly materialization of agent quality scores
        CREATE OR REPLACE TABLE `project.analytics.daily_quality` AS
        WITH recent AS (
          SELECT DISTINCT session_id, MIN(timestamp) AS timestamp,
                 ANY_VALUE(agent) AS agent
          FROM `project.analytics.agent_events`
          WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
          GROUP BY session_id
        )
        SELECT
          session_id,
          timestamp,
          agent,
          JSON_VALUE(result, '$.error_count') AS error_count,
          CAST(JSON_VALUE(result, '$.avg_latency_ms') AS FLOAT64) AS avg_latency_ms,
          JSON_VALUE(result, '$.tool_call_count') AS tool_calls
        FROM recent,
        UNNEST([
          `project.analytics.agent_analytics`(
            'analyze',
            JSON_OBJECT('session_id', session_id)
          )
        ]) AS result;

Step 4: Priya connects Looker to `daily_quality` table
        → Dashboard shows latency trends, error rates, tool usage by agent
```

**End-to-End Example — Batch Evaluation via SQL:**

```sql
-- Evaluate all sessions from last 24h for latency compliance
WITH recent_sessions AS (
  SELECT DISTINCT session_id
  FROM `myproject.analytics.agent_events`
  WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
)
SELECT
  s.session_id,
  JSON_VALUE(result, '$.passed') AS passed,
  JSON_VALUE(result, '$.score') AS latency_score,
  JSON_VALUE(result, '$.details') AS details
FROM recent_sessions s,
UNNEST([
  `myproject.analytics.agent_analytics`(
    'evaluate',
    JSON_OBJECT(
      'session_id', s.session_id,
      'metric', 'latency',
      'threshold', 5000
    )
  )
]) AS result;
```

**Result:**

| session_id | passed | latency_score | details |
|-----------|--------|--------------|---------|
| sess-001 | true | 0.85 | avg_latency_ms=2340, max=4200 |
| sess-002 | false | 0.32 | avg_latency_ms=7800, max=12400 |
| sess-003 | true | 0.91 | avg_latency_ms=1850, max=3100 |

---

#### CUJ-A2: Priya Runs LLM-as-Judge at Scale

**Goal:** Score all sessions for correctness using AI, directly in SQL.

```sql
-- Judge correctness of every session from the "support_bot" agent
SELECT
  session_id,
  CAST(JSON_VALUE(judgment, '$.score') AS FLOAT64) AS correctness_score,
  JSON_VALUE(judgment, '$.passed') AS passed,
  JSON_VALUE(judgment, '$.feedback') AS llm_feedback
FROM (
  SELECT DISTINCT session_id
  FROM `myproject.analytics.agent_events`
  WHERE agent = 'support_bot'
    AND timestamp >= '2026-03-01'
) sessions,
UNNEST([
  `myproject.analytics.agent_analytics`(
    'judge',
    JSON_OBJECT(
      'session_id', sessions.session_id,
      'criterion', 'correctness'
    )
  )
]) AS judgment
WHERE CAST(JSON_VALUE(judgment, '$.score') AS FLOAT64) < 0.7
ORDER BY correctness_score ASC;
```

**Result:** Surfaces the lowest-quality sessions for human review — no Python
required.

---

#### CUJ-A3: Priya Creates a Drift Alert

**Goal:** Scheduled query that alerts when production questions drift from
golden set.

```sql
-- Weekly drift check: compare production vs golden questions
SELECT
  JSON_VALUE(drift_result, '$.coverage_percentage') AS coverage_pct,
  JSON_VALUE(drift_result, '$.total_golden') AS golden_count,
  JSON_VALUE(drift_result, '$.total_production') AS prod_count,
  JSON_QUERY(drift_result, '$.uncovered_questions') AS gaps
FROM UNNEST([
  `myproject.analytics.agent_analytics`(
    'drift',
    JSON_OBJECT(
      'golden_dataset', 'myproject.analytics.golden_questions',
      'agent_filter', 'support_bot',
      'start_date', '2026-03-01',
      'end_date', '2026-03-06'
    )
  )
]) AS drift_result;
```

---

### 3.4 Remote Function Technical Design

> Reference:
> [BigQuery Remote Functions](https://cloud.google.com/bigquery/docs/remote-functions)

#### Prerequisites

| Requirement | Detail |
|-------------|--------|
| **Connection** | A `CLOUD_RESOURCE` connection (`bq mk --connection --connection_type=CLOUD_RESOURCE`) |
| **IAM — Creator** | `bigquery.routines.create` on dataset + `bigquery.connections.delegate` on connection |
| **IAM — Invoker** | `bigquery.routines.get` on dataset + `bigquery.connections.use` on connection |
| **Service account** | The connection's auto-created SA needs **Cloud Run Invoker** role on the Cloud Function / Cloud Run service |
| **Return types** | `BOOL`, `BYTES`, `NUMERIC`, `STRING`, `DATE`, `DATETIME`, `TIME`, `TIMESTAMP`, `JSON`. **Not supported:** `ARRAY`, `STRUCT`, `INTERVAL`, `GEOGRAPHY` |

#### Deployment Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────┐
│   BigQuery      │────▶│  Cloud Function /     │────▶│  BigQuery   │
│   SELECT fn()   │     │  Cloud Run            │     │  (queries)  │
│                 │◀────│  + SDK Core            │◀────│             │
│   Returns JSON  │     │  handle_request()      │     │             │
└─────────────────┘     └──────────────────────┘     └─────────────┘
```

#### Request/Response Contract

BigQuery sends batched rows as HTTP POST requests. Each element in `calls`
represents one row's arguments. BigQuery automatically determines batch
size but respects the `max_batching_rows` limit. Retries happen on HTTP
408, 429, 500, 503, 504. Results are never cached (assumed non-deterministic).

**Request** (POST from BigQuery):
```json
{
  "requestId": "124ab1c",
  "caller": "//bigquery.googleapis.com/projects/myproject/jobs/bqjob_r1234_00001",
  "sessionUser": "analyst@company.com",
  "userDefinedContext": {
    "project_id": "myproject",
    "dataset_id": "analytics"
  },
  "calls": [
    ["evaluate", "{\"session_id\":\"sess-001\",\"metric\":\"latency\",\"threshold\":5000}"],
    ["evaluate", "{\"session_id\":\"sess-002\",\"metric\":\"latency\",\"threshold\":5000}"]
  ]
}
```

**Response** (HTTP 200):
```json
{
  "replies": [
    "{\"passed\": true, \"score\": 0.85, \"details\": \"avg=2340ms\"}",
    "{\"passed\": false, \"score\": 0.32, \"details\": \"avg=7800ms\"}"
  ]
}
```

**Error response** (non-retryable 4xx):
```json
{
  "errorMessage": "Unknown operation: foo (max 1KB)"
}
```

#### API Versioning & Stability Contract

The remote function response includes a `_version` field for forward
compatibility. Clients should ignore unknown fields.

```json
{
  "replies": [
    "{\"_version\":\"1.0\",\"passed\":true,\"score\":0.85,\"details\":\"avg=2340ms\"}"
  ]
}
```

**Version guarantees:**
- **v1.x:** Additive changes only (new fields, new operations). Existing
  fields and operations are never removed or renamed.
- **v2.0:** Breaking changes require a new Cloud Function endpoint and
  new `CREATE FUNCTION` registration.

#### Error Codes

Every per-row error in `replies` uses a structured error object instead of
a result:

```json
{
  "replies": [
    "{\"passed\":true,\"score\":0.85}",
    "{\"_error\":{\"code\":\"SESSION_NOT_FOUND\",\"message\":\"No events for session sess-999\"}}"
  ]
}
```

| Error Code | HTTP | Retryable | Description |
|-----------|------|-----------|-------------|
| `INVALID_OPERATION` | 400 | No | Unknown operation string |
| `INVALID_PARAMS` | 400 | No | Missing or malformed params JSON |
| `SESSION_NOT_FOUND` | 200* | No | No events found for session_id |
| `EVALUATION_FAILED` | 200* | No | Evaluator raised an exception |
| `UPSTREAM_TIMEOUT` | 200* | Yes | BigQuery query timed out |
| `INTERNAL_ERROR` | 200* | Yes | Unexpected SDK error |

*\*Per-row errors return HTTP 200 with error in `replies[i]` so that
other rows in the batch succeed. Batch-level errors (all rows fail)
return HTTP 400 with top-level `errorMessage`.*

#### Partial Failure Semantics

BigQuery batches multiple rows into a single HTTP request. The function
processes each row independently:

- If row 2 of 5 fails, rows 1, 3, 4, 5 return valid results. Row 2
  returns a `_error` object in its `replies` slot.
- BigQuery surfaces the `_error` JSON as the column value — the caller
  can filter with `JSON_VALUE(result, '$._error.code') IS NOT NULL`.
- Only if **all** rows fail does the function return HTTP 400.

#### Idempotency Guidance

BigQuery may retry requests on transient errors (HTTP 408/429/5xx).
The function should be safe to call multiple times with the same input:

- `analyze` and `evaluate` are naturally idempotent (read-only queries).
- `judge` calls AI.GENERATE which may return slightly different scores —
  this is acceptable (non-deterministic by nature).
- The `requestId` field can be used for deduplication if the function
  performs any write operations in the future.

#### CREATE FUNCTION DDL

```sql
-- 1. Create CLOUD_RESOURCE connection (one-time)
--    Console: Explorer → +Add → External Connection → "Vertex AI remote models,
--             remote functions, BigLake and Spanner"
--    CLI:
--    bq mk --connection --location=US --project_id=myproject \
--           --connection_type=CLOUD_RESOURCE analytics-conn

-- 2. Grant Cloud Run Invoker to the connection's service account
--    (find the SA in connection details → IAM → add role)

-- 3. Register the remote function
CREATE FUNCTION `myproject.analytics.agent_analytics`(
  operation STRING,
  params JSON
) RETURNS JSON
REMOTE WITH CONNECTION `myproject.us.analytics-conn`
OPTIONS (
  endpoint = 'https://us-central1-myproject.cloudfunctions.net/bq-agent-analytics',
  user_defined_context = [
    ("project_id", "myproject"),
    ("dataset_id", "analytics")
  ],
  max_batching_rows = 50
);
```

#### Entry Point (`deploy/remote_function/main.py`)

A single multiplexed Cloud Function handles all operations. BigQuery sends
`(operation STRING, params JSON)` tuples; the function dispatches to the
appropriate SDK method and returns JSON results.

```python
import functions_framework
import json
import os
from flask import jsonify
from bigquery_agent_analytics import Client, CodeEvaluator, LLMAsJudge, TraceFilter

# Initialized once per cold start. Config comes from userDefinedContext
# (forwarded by BigQuery) or environment variables as fallback.
_client = None

def _get_client(context: dict) -> Client:
    global _client
    if _client is None:
        _client = Client(
            project_id=context.get("project_id", os.environ["PROJECT_ID"]),
            dataset_id=context.get("dataset_id", os.environ["DATASET_ID"]),
        )
    return _client

@functions_framework.http
def handle_request(request):
    """Entry point called by BigQuery Remote Function framework.

    BigQuery batches rows into `calls`. Each call is [operation, params_json].
    The `replies` array must have the same length as `calls`.
    """
    try:
        body = request.get_json()
        context = body.get("userDefinedContext", {})
        client = _get_client(context)
        replies = []
        for call in body["calls"]:
            operation, params_raw = call[0], call[1]
            params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
            result = _dispatch(client, operation, params)
            replies.append(json.dumps(result))
        return jsonify({"replies": replies})
    except Exception as e:
        return jsonify({"errorMessage": str(e)[:1024]}), 400

def _dispatch(client, operation, params):
    if operation == "analyze":
        trace = client.get_session_trace(params["session_id"])
        return {
            "span_count": len(trace.spans),
            "error_count": len(trace.error_spans),
            "avg_latency_ms": trace.total_latency_ms,
            "tool_call_count": len(trace.tool_calls),
            "final_response": trace.final_response,
        }
    elif operation == "evaluate":
        evaluator = CodeEvaluator.latency(threshold_ms=params["threshold"])
        report = client.evaluate(evaluator=evaluator,
            filters=TraceFilter(session_ids=[params["session_id"]]))
        return report.details[0] if report.details else {}
    elif operation == "judge":
        judge = getattr(LLMAsJudge, params["criterion"])()
        report = client.evaluate(evaluator=judge,
            filters=TraceFilter(session_ids=[params["session_id"]]))
        return report.details[0] if report.details else {}
    elif operation == "drift":
        report = client.drift_detection(
            golden_dataset=params["golden_dataset"],
            filters=TraceFilter(
                agent_id=params.get("agent_filter"),
                start_time=params.get("start_date"),
                end_time=params.get("end_date"),
            ))
        return report.model_dump()
    else:
        raise ValueError(f"Unknown operation: {operation}")
```

**Deploy:**
```bash
gcloud functions deploy bq-agent-analytics \
    --gen2 \
    --runtime python312 \
    --region us-central1 \
    --entry-point handle_request \
    --source ./deploy/remote_function/ \
    --trigger-http \
    --no-allow-unauthenticated \
    --set-env-vars PROJECT_ID=myproject,DATASET_ID=analytics
```

---

## 3A. Path A-bis: Continuous Queries for Real-Time Analytics

### 3A.1 Overview

> Reference:
> [BigQuery Continuous Queries](https://cloud.google.com/bigquery/docs/continuous-queries-introduction),
> [Create Continuous Queries](https://cloud.google.com/bigquery/docs/continuous-queries)

BigQuery **continuous queries** run SQL continuously against new data as it
arrives, using the `APPENDS()` table function. They process each row as it
is ingested and write results to BigQuery tables, Pub/Sub, Bigtable, or
Spanner — enabling real-time, event-driven analytics over agent traces.

#### Key Capabilities and Constraints

| Aspect | Detail |
|--------|--------|
| **Trigger** | Automatically fires on new rows via `APPENDS(TABLE ..., start_timestamp)` |
| **Destinations** | `INSERT INTO` (BigQuery table), `EXPORT DATA` (Pub/Sub, Bigtable, Spanner) |
| **AI functions** | `AI.GENERATE_TEXT` and `ML.GENERATE_TEXT` — supported for remote models (Gemini, etc.). `AI.GENERATE_TABLE` — **not supported** in continuous queries. `ML.UNDERSTAND_TEXT`, `ML.TRANSLATE` — supported. See [supported AI functions in CQ](https://cloud.google.com/bigquery/docs/continuous-queries-introduction#supported_statements). |
| **Remote functions** | **Not supported** — continuous queries cannot call user-defined remote functions (`CREATE FUNCTION ... REMOTE`) because remote functions are UDF routines, and CQ disallows all UDFs |
| **SQL restrictions** | No `JOIN`, `GROUP BY`, `DISTINCT`, aggregates, window functions, `ORDER BY`, `LIMIT` |
| **Execution** | `bq query --continuous=true` or API `"continuous": true` — not a DDL statement |
| **Reservation** | Requires Enterprise / Enterprise Plus edition with `CONTINUOUS` job type assignment (max 500 slots) |
| **Max runtime** | 2 days (user account), 150 days (service account) |
| **Processing model** | Stateless per-row; no cross-row state. Idle queries consume ~1 slot |

#### Why This Matters for the SDK

The ADK plugin writes events to the `agent_events` table via the BigQuery
Storage Write API. A continuous query can monitor this table in real-time
and apply `AI.GENERATE_TEXT` — the same LLM evaluation engine the SDK
uses — to score, classify, or flag every session as events arrive. **No
Cloud Function deployment needed; pure SQL.**

This creates a third analytics path:

```
Path A:  Remote Function   → batch SQL analytics via Cloud Function
Path A': Continuous Query   → real-time streaming analytics via AI.GENERATE_TEXT
Path B:  CLI               → agent self-diagnostics and CI/CD
```

### 3A.2 Critical User Journeys (CUJ)

#### CUJ-A4: Priya Builds Real-Time Error Alerting with Continuous Query + AI.GENERATE_TEXT

**Goal:** Every time an agent session ends with an error, automatically
classify the failure root cause using Gemini and push an alert to Pub/Sub
(which routes to Slack/PagerDuty). No Cloud Function, no cron — pure
streaming SQL.

**Architecture:**

```
┌──────────────┐    ┌─────────────────────────┐    ┌────────────────┐
│  ADK Plugin  │───▶│  agent_events table      │───▶│  Continuous    │
│  (writes     │    │  (BigQuery)              │    │  Query + AI.   │
│   events)    │    │                          │    │  GENERATE_TEXT │
└──────────────┘    └─────────────────────────┘    └──────┬─────────┘
                                                          │
                                          ┌───────────────┼───────────────┐
                                          ▼               ▼               ▼
                                   ┌────────────┐ ┌────────────┐ ┌──────────────┐
                                   │ error_      │ │ Pub/Sub    │ │ Bigtable     │
                                   │ analysis    │ │ (→ Slack)  │ │ (low-latency │
                                   │ table       │ │            │ │  dashboard)  │
                                   └────────────┘ └────────────┘ └──────────────┘
```

**Step 1: Create the AI model endpoint for evaluation**

```sql
-- Create a remote model pointing to Gemini (one-time setup)
CREATE OR REPLACE MODEL `myproject.analytics.gemini_flash`
REMOTE WITH CONNECTION `myproject.us.analytics-conn`
OPTIONS (
  endpoint = 'gemini-2.5-flash'
);
```

**Step 2: Create destination table for analysis results**

```sql
CREATE TABLE IF NOT EXISTS `myproject.analytics.realtime_error_analysis` (
  session_id STRING,
  agent STRING,
  event_type STRING,
  error_message STRING,
  timestamp TIMESTAMP,
  root_cause STRING,
  severity STRING,
  suggested_fix STRING,
  analyzed_at TIMESTAMP
);
```

**Step 3: Launch continuous query (real-time error analysis)**

```bash
bq query --project_id=myproject --use_legacy_sql=false \
  --continuous=true \
  --connection_property=service_account=analytics-cq@myproject.iam.gserviceaccount.com \
  '
INSERT INTO `myproject.analytics.realtime_error_analysis`
SELECT
  base.session_id,
  base.agent,
  base.event_type,
  base.error_message,
  base.timestamp,
  JSON_VALUE(analysis.ml_generate_text_llm_result, "$.root_cause") AS root_cause,
  JSON_VALUE(analysis.ml_generate_text_llm_result, "$.severity") AS severity,
  JSON_VALUE(analysis.ml_generate_text_llm_result, "$.suggested_fix") AS suggested_fix,
  CURRENT_TIMESTAMP() AS analyzed_at
FROM
  AI.GENERATE_TEXT(
    MODEL `myproject.analytics.gemini_flash`,
    (
      SELECT
        session_id,
        agent,
        event_type,
        error_message,
        timestamp,
        CONCAT(
          "Analyze this agent error and return JSON with keys: ",
          "root_cause (one of: tool_timeout, invalid_input, api_failure, ",
          "model_error, permission_denied, rate_limit, unknown), ",
          "severity (critical, high, medium, low), ",
          "suggested_fix (one sentence). ",
          "Agent: ", COALESCE(agent, "unknown"),
          " | Event: ", event_type,
          " | Error: ", COALESCE(error_message, "no message")
        ) AS prompt
      FROM
        APPENDS(
          TABLE `myproject.analytics.agent_events`,
          CURRENT_TIMESTAMP() - INTERVAL 10 MINUTE
        )
      WHERE
        ENDS_WITH(event_type, "_ERROR")
        OR error_message IS NOT NULL
        OR status = "ERROR"
    ),
    STRUCT(100 AS max_output_tokens, 0.1 AS temperature)
  ) AS analysis
'
```

**What happens at runtime:**

```
14:00:01  Plugin writes TOOL_ERROR for sess-042
14:00:02  Continuous query detects new error row via APPENDS()
14:00:03  AI.GENERATE_TEXT classifies: root_cause=tool_timeout, severity=high
14:00:03  Result inserted into realtime_error_analysis table
          → Looker dashboard updates in real-time
          → (Optional) Second continuous query exports to Pub/Sub → Slack
```

**Step 4 (Optional): Chain a second continuous query to Pub/Sub for alerts**

```bash
bq query --project_id=myproject --use_legacy_sql=false \
  --continuous=true \
  --connection_property=service_account=analytics-cq@myproject.iam.gserviceaccount.com \
  '
EXPORT DATA
  OPTIONS (
    format = "CLOUD_PUBSUB",
    uri = "https://pubsub.googleapis.com/projects/myproject/topics/agent-error-alerts"
  )
AS (
  SELECT
    TO_JSON_STRING(
      STRUCT(
        session_id,
        agent,
        root_cause,
        severity,
        error_message,
        suggested_fix
      )
    ) AS message,
    TO_JSON(
      STRUCT(
        severity AS severity,
        agent AS agent
      )
    ) AS _ATTRIBUTES
  FROM
    APPENDS(
      TABLE `myproject.analytics.realtime_error_analysis`,
      CURRENT_TIMESTAMP() - INTERVAL 10 MINUTE
    )
  WHERE severity IN ("critical", "high")
)
'
```

**End-to-end result:** Every high-severity agent error is automatically
analyzed by Gemini within seconds of occurrence, stored in a queryable
table, and routed to Slack/PagerDuty via Pub/Sub — all with zero
application code.

---

#### CUJ-A5: Priya Streams Session Quality Scores to Bigtable for Low-Latency Dashboards

**Goal:** Score every completed agent session in real-time and write results
to Bigtable for sub-millisecond dashboard reads.

```bash
bq query --project_id=myproject --use_legacy_sql=false \
  --continuous=true \
  --connection_property=service_account=analytics-cq@myproject.iam.gserviceaccount.com \
  '
EXPORT DATA
  OPTIONS (
    format = "CLOUD_BIGTABLE",
    overwrite = TRUE,
    uri = "https://bigtable.googleapis.com/projects/myproject/instances/agent-metrics/tables/session-scores"
  )
AS (
  SELECT
    CONCAT(base.session_id, "#", CAST(base.timestamp AS STRING)) AS rowkey,
    STRUCT(
      base.session_id,
      base.agent,
      base.timestamp,
      JSON_VALUE(analysis.ml_generate_text_llm_result, "$.quality_score") AS quality_score,
      JSON_VALUE(analysis.ml_generate_text_llm_result, "$.outcome") AS outcome,
      JSON_VALUE(analysis.ml_generate_text_llm_result, "$.summary") AS summary
    ) AS metrics
  FROM
    AI.GENERATE_TEXT(
      MODEL `myproject.analytics.gemini_flash`,
      (
        SELECT
          session_id,
          agent,
          timestamp,
          CONCAT(
            "Score this completed agent session. Return JSON with: ",
            "quality_score (0.0-1.0), outcome (success/partial/failure), ",
            "summary (one sentence). ",
            "Agent: ", COALESCE(agent, "unknown"),
            " | Response: ", COALESCE(
              JSON_VALUE(content, "$.response"),
              JSON_VALUE(content, "$.text_summary"),
              "no response"
            )
          ) AS prompt
        FROM
          APPENDS(
            TABLE `myproject.analytics.agent_events`,
            CURRENT_TIMESTAMP() - INTERVAL 10 MINUTE
          )
        WHERE event_type = "AGENT_COMPLETED"
      ),
      STRUCT(80 AS max_output_tokens, 0.1 AS temperature)
    ) AS analysis
)
'
```

### 3A.3 When to Use Which Path

| Scenario | Path | Why |
|----------|------|-----|
| Nightly batch evaluation of all sessions | **Remote Function** (Path A) | Needs JOINs, GROUP BY, aggregation — not supported in continuous queries |
| Real-time error classification as events arrive | **Continuous Query** (Path A') | Stateless per-row processing; AI.GENERATE_TEXT on each error; no deployment needed |
| Dashboard with sub-second latency | **Continuous Query → Bigtable** | EXPORT DATA to Bigtable for low-latency reads |
| Alert on critical errors via Slack/PagerDuty | **Continuous Query → Pub/Sub** | EXPORT DATA to Pub/Sub with severity-based attributes |
| Agent self-diagnostic before responding | **CLI** (Path B) | Agent calls `bq-agent-sdk evaluate` as a tool |
| CI/CD gate blocking deployment | **CLI** (Path B) | `--exit-code` in GitHub Actions |
| Ad-hoc drift analysis comparing golden set | **Remote Function** (Path A) | Needs cross-table comparison (JOIN with golden set) |
| Semantic session clustering | **Remote Function** (Path A) | Needs aggregation and embedding distance (GROUP BY) |

---

## 4. Path B: CLI Interface (`bq-agent-sdk`)

### 4.1 Overview

A command-line tool that wraps the SDK's Python API, designed for two primary
consumers:

1. **AI agents** that invoke CLI commands as tools (low token overhead)
2. **Platform engineers** who script evaluation pipelines

#### 4.1.1 Why CLI Instead of MCP for Agent Tool Integration

Recent research and community benchmarks demonstrate that CLI tools
significantly outperform MCP (Model Context Protocol) servers when used as
AI agent tools. The `bq-agent-sdk` CLI is designed with these findings in mind.

**Token Efficiency: 35x Reduction**

| Approach | Context Tokens | Notes |
|----------|---------------|-------|
| MCP server schema (single service) | ~55,000 | Full tool definitions loaded at session start |
| MCP server schema (3 services stacked) | ~150,000+ | Each server adds its full schema |
| CLI command (`bq-agent-sdk evaluate --help`) | ~4,150 | Loaded just-in-time, only when needed |
| CLI manifest (all commands summarized) | ~100 | One-line descriptions; agent drills into `--help` on demand |

A community benchmark measured a **Token Efficiency Score of 202 for CLI vs
152 for MCP** on identical tasks, with CLI achieving **28% higher task
completion rate**. MCP schema pre-loading consumed up to **40% of the
available context window** before any actual work began.

**LLMs Are CLI-Native**

Large language models are trained on billions of terminal interactions from
public code repositories, documentation, and Stack Overflow. Commands like
`git`, `curl`, `jq`, `kubectl`, and `gcloud` are deeply embedded in their
training data. When an agent sees `bq-agent-sdk evaluate --evaluator=latency
--threshold=5000 --format=json`, it can infer behavior from structural
similarity to tools it has seen millions of times — no schema pre-loading
required.

**Unix Composability**

CLI tools compose naturally via pipes and shell scripting, enabling workflows
that are difficult or impossible with MCP:

```bash
# Evaluate → filter failures → alert (three tools, zero MCP schemas)
bq-agent-sdk evaluate --last=1h --format=json \
  | jq '.failed_sessions[]' \
  | xargs -I{} bq-agent-sdk get-trace --session-id={} --format=json \
  | jq '{session: .session_id, errors: .errors}' \
  | curl -X POST "$SLACK_WEBHOOK" -d @-
```

**When MCP Is Still Appropriate**

MCP remains valuable for: (a) structured validation where type-safe schemas
prevent malformed requests, (b) multi-tenant environments with dynamic tool
discovery, (c) services without CLI equivalents (e.g., browser automation,
GUI testing), and (d) tool discovery in large catalogs. For the SDK's
well-defined analytics operations, CLI is the simpler and more efficient path.

#### 4.1.2 CLI Design Principles for Agent Consumption

The `bq-agent-sdk` CLI follows five principles that maximize its effectiveness
as an AI agent tool:

1. **Structured JSON output by default** — `--format=json` is the default.
   Every command returns a parseable JSON object. Agents never need to parse
   free-form text.

2. **Just-in-time `--help`** — Instead of pre-loading a 55K-token schema,
   agents call `bq-agent-sdk --help` (~100 tokens) to discover commands, then
   `bq-agent-sdk evaluate --help` (~200 tokens) to learn a specific command's
   options. Total context cost: ~300 tokens vs ~55,000 for MCP.

3. **Lightweight manifest** — A one-line-per-command manifest can be embedded
   in an agent's system prompt at ~100 tokens total:
   ```
   bq-agent-sdk doctor    — health check
   bq-agent-sdk evaluate  — run evaluations (latency, errors, LLM judge)
   bq-agent-sdk get-trace — retrieve session trace
   bq-agent-sdk drift     — drift detection against golden set
   bq-agent-sdk insights  — generate analytics report
   bq-agent-sdk views     — manage BigQuery views
   ```

4. **Machine-friendly exit codes** — `--exit-code` returns 0/1 for pass/fail,
   enabling direct use in conditionals (`if bq-agent-sdk evaluate ...;
   then ...`).

5. **Environment variable fallbacks** — Global options like `--project-id` and
   `--dataset-id` can be set via `BQ_AGENT_PROJECT` and `BQ_AGENT_DATASET`,
   reducing per-invocation token cost for agents that call the CLI repeatedly.

### 4.2 Command Structure

```
bq-agent-sdk [GLOBAL OPTIONS] <command> [COMMAND OPTIONS]

Global Options:
  --project-id TEXT       GCP project ID [env: BQ_AGENT_PROJECT]
  --dataset-id TEXT       BigQuery dataset [env: BQ_AGENT_DATASET]
  --table-id TEXT         Events table [default: agent_events]
  --location TEXT         BQ location [default: us-central1]
  --endpoint TEXT         AI.GENERATE endpoint
  --connection-id TEXT    BQ connection ID
  --format TEXT           Output format: json|text|table [default: json]
  --quiet                 Suppress non-essential output

Commands:
  doctor                  Run diagnostic health check
  get-trace               Retrieve and render a trace
  list-traces             List recent traces with filters
  evaluate                Run code-based or LLM evaluation
  insights                Generate insights report
  drift                   Run drift detection against golden set
  distribution            Analyze question distribution
  hitl-metrics            Show HITL interaction metrics
  views                   Manage per-event-type BigQuery views
```

### 4.3 Critical User Journeys (CUJ)

#### CUJ-B1: AgentX Checks Its Own Latency Before Responding

**Context:** An ADK agent has a `before_agent_callback` that shells out to
the CLI to check recent performance. If latency is high, it adjusts its
strategy (e.g., skips expensive tool calls).

**Agent's Tool Definition (ADK tool-calling schema):**
```json
{
  "name": "check_agent_performance",
  "description": "Check this agent's recent latency and error rate",
  "parameters": {
    "type": "object",
    "properties": {
      "session_count": {"type": "integer", "default": 10},
      "metric": {"type": "string", "enum": ["latency", "error_rate", "all"]}
    }
  }
}
```

**Agent Invocation (what the LLM generates):**
```bash
bq-agent-sdk evaluate \
  --project-id=myproject \
  --dataset-id=analytics \
  --agent-id=support_bot \
  --last=1h \
  --evaluator=latency \
  --threshold=5000 \
  --format=json
```

**CLI Output (consumed by the agent):**
```json
{
  "evaluator": "latency",
  "threshold_ms": 5000,
  "total_sessions": 10,
  "passed": 7,
  "failed": 3,
  "pass_rate": 0.70,
  "aggregate_scores": {
    "avg_latency_ms": 3200,
    "max_latency_ms": 8400,
    "p95_latency_ms": 6100
  },
  "failed_sessions": ["sess-042", "sess-047", "sess-051"]
}
```

**Agent's Decision Logic:**
```
IF pass_rate < 0.8:
    → Switch to lighter model (gemini-flash instead of gemini-pro)
    → Skip optional enrichment tool calls
    → Add disclaimer: "Response may be less detailed due to system load"
```

**End-to-End Flow:**
```
User → "What's the refund policy for order #1234?"
       │
       ▼
AgentX (before responding):
  1. Calls: bq-agent-sdk evaluate --agent-id=support_bot --last=1h --evaluator=latency --threshold=5000
  2. Sees: pass_rate=0.70, avg_latency=3200ms
  3. Decides: latency is borderline, use lighter model
  4. Calls: bq-agent-sdk evaluate --agent-id=support_bot --last=1h --evaluator=error_rate --threshold=0.1
  5. Sees: pass_rate=0.95, error_rate=0.05
  6. Decides: errors are fine, proceed normally
       │
       ▼
AgentX → "The refund policy for order #1234 is..."
```

---

#### CUJ-B2: AgentX Retrieves a Past Session for Context

**Context:** A user returns and references a previous conversation. The agent
retrieves the old session trace to understand context.

**Agent Invocation:**
```bash
bq-agent-sdk get-trace \
  --project-id=myproject \
  --dataset-id=analytics \
  --session-id=sess-previous-abc \
  --format=json \
  --quiet
```

**CLI Output:**
```json
{
  "trace_id": "trace-abc-123",
  "session_id": "sess-previous-abc",
  "user_id": "user-42",
  "total_latency_ms": 4500,
  "span_count": 12,
  "tool_calls": [
    {"tool_name": "lookup_order", "args": {"order_id": "1234"}, "status": "OK"},
    {"tool_name": "check_refund_eligibility", "args": {"order_id": "1234"}, "status": "OK"}
  ],
  "final_response": "Your order #1234 is eligible for a full refund...",
  "errors": []
}
```

**Agent uses this context:** "I can see from your previous conversation that
we confirmed order #1234 is eligible for a refund. Let me process that now."

---

#### CUJ-B3: Marcus Runs Nightly Eval in CI/CD

**Goal:** GitHub Actions workflow that gates deployment on evaluation pass rate.

**`.github/workflows/nightly-eval.yml`:**
```yaml
name: Nightly Agent Evaluation
on:
  schedule:
    - cron: '0 2 * * *'  # 2 AM daily

jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}

      - name: Install SDK
        run: pip install bigquery-agent-analytics-sdk[cli]

      - name: Run latency evaluation
        run: |
          bq-agent-sdk evaluate \
            --project-id=${{ vars.GCP_PROJECT }} \
            --dataset-id=analytics \
            --agent-id=support_bot \
            --last=24h \
            --evaluator=latency \
            --threshold=5000 \
            --format=json \
            --exit-code \
          > eval_latency.json

      - name: Run error rate evaluation
        run: |
          bq-agent-sdk evaluate \
            --project-id=${{ vars.GCP_PROJECT }} \
            --dataset-id=analytics \
            --agent-id=support_bot \
            --last=24h \
            --evaluator=error_rate \
            --threshold=0.05 \
            --format=json \
            --exit-code \
          > eval_errors.json

      - name: Run correctness judge
        run: |
          bq-agent-sdk evaluate \
            --project-id=${{ vars.GCP_PROJECT }} \
            --dataset-id=analytics \
            --agent-id=support_bot \
            --last=24h \
            --evaluator=llm-judge \
            --criterion=correctness \
            --threshold=0.7 \
            --format=json \
            --exit-code \
          > eval_correctness.json

      - name: Run drift detection
        run: |
          bq-agent-sdk drift \
            --project-id=${{ vars.GCP_PROJECT }} \
            --dataset-id=analytics \
            --golden-dataset=golden_questions \
            --agent-id=support_bot \
            --last=24h \
            --min-coverage=0.85 \
            --exit-code \
          > drift_report.json

      - name: Generate insights summary
        if: always()
        run: |
          bq-agent-sdk insights \
            --project-id=${{ vars.GCP_PROJECT }} \
            --dataset-id=analytics \
            --agent-id=support_bot \
            --last=24h \
            --max-sessions=50 \
            --format=text \
          > insights_summary.txt

      - name: Upload reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: eval-reports
          path: |
            eval_*.json
            drift_report.json
            insights_summary.txt
```

**Key behavior:** `--exit-code` makes the command return exit code 1 when
evaluation fails, causing the CI step to fail and blocking deployment.

---

#### CUJ-B4: Marcus Pipes CLI Output to Monitoring

**Goal:** Feed evaluation results into Slack alerts and Datadog metrics.

```bash
#!/bin/bash
# cron-eval.sh — runs every hour

RESULT=$(bq-agent-sdk evaluate \
  --project-id=myproject \
  --dataset-id=analytics \
  --agent-id=support_bot \
  --last=1h \
  --evaluator=latency \
  --threshold=5000 \
  --format=json)

PASS_RATE=$(echo "$RESULT" | jq -r '.pass_rate')
AVG_LATENCY=$(echo "$RESULT" | jq -r '.aggregate_scores.avg_latency_ms')

# Send to Datadog
curl -X POST "https://api.datadoghq.com/api/v1/series" \
  -H "DD-API-KEY: ${DD_API_KEY}" \
  -d "{
    \"series\": [{
      \"metric\": \"agent.latency.pass_rate\",
      \"points\": [[$(date +%s), $PASS_RATE]],
      \"tags\": [\"agent:support_bot\"]
    }, {
      \"metric\": \"agent.latency.avg_ms\",
      \"points\": [[$(date +%s), $AVG_LATENCY]],
      \"tags\": [\"agent:support_bot\"]
    }]
  }"

# Alert Slack if pass rate drops
if (( $(echo "$PASS_RATE < 0.8" | bc -l) )); then
  curl -X POST "$SLACK_WEBHOOK" \
    -d "{\"text\": \"⚠️ Agent latency pass rate dropped to ${PASS_RATE} (threshold: 0.80). Avg: ${AVG_LATENCY}ms\"}"
fi
```

---

#### CUJ-B5: AgentX Performs Self-Correction Loop

**Context:** An agent notices repeated errors in a session and uses the CLI
to diagnose and adapt in real-time.

**Conversation Flow:**
```
Turn 1: User asks complex multi-step question
Turn 2: Agent calls tool → TOOL_ERROR
Turn 3: Agent calls tool again → TOOL_ERROR
Turn 4: Agent invokes self-diagnostic:

  $ bq-agent-sdk get-trace \
      --project-id=myproject \
      --dataset-id=analytics \
      --session-id=current-session-456 \
      --format=json \
      --quiet

  Output:
  {
    "errors": [
      {"event_type": "TOOL_ERROR", "tool": "database_query",
       "error_message": "Connection timeout after 30s"},
      {"event_type": "TOOL_ERROR", "tool": "database_query",
       "error_message": "Connection timeout after 30s"}
    ],
    "error_count": 2,
    "tool_calls": [
      {"tool_name": "database_query", "status": "ERROR"},
      {"tool_name": "database_query", "status": "ERROR"}
    ]
  }

Turn 5: Agent recognizes "database_query" tool is timing out
         → Switches to cached data source
         → Tells user: "I'm experiencing delays with the live database.
           Let me check the cached data instead."

Turn 6: Agent calls fallback tool → success → responds with answer
```

---

#### CUJ-B6: Marcus Runs Doctor Check Before Deployment

**Goal:** Validate SDK configuration and table health before deploying a new
agent version.

```bash
$ bq-agent-sdk doctor \
    --project-id=myproject \
    --dataset-id=analytics \
    --format=text

╔══════════════════════════════════════════════════╗
║         Agent Analytics Health Check             ║
╠══════════════════════════════════════════════════╣
║ Table: myproject.analytics.agent_events          ║
║ Schema: ✓ OK (16/16 required columns present)   ║
║                                                  ║
║ Event Coverage (last 24h):                       ║
║   USER_MESSAGE_RECEIVED    1,234                 ║
║   LLM_REQUEST              2,456                 ║
║   LLM_RESPONSE             2,450                 ║
║   TOOL_STARTING              890                 ║
║   TOOL_COMPLETED             875                 ║
║   TOOL_ERROR                  15                 ║
║   AGENT_STARTING             620                 ║
║   AGENT_COMPLETED            618                 ║
║   HITL_CONFIRMATION_REQ       42                 ║
║   STATE_DELTA                310                 ║
║                                                  ║
║ AI.GENERATE_TEXT: ✓ (gemini-2.5-flash)            ║
║ Connection:  ✓ us-central1.analytics-conn        ║
║                                                  ║
║ Warnings:                                        ║
║   ⚠ 2 AGENT_STARTING events without matching    ║
║     AGENT_COMPLETED (possible timeout)           ║
║   ⚠ TOOL_ERROR rate: 1.7% (15/890)              ║
╚══════════════════════════════════════════════════╝
```

---

#### CUJ-B7: Marcus Creates BigQuery Views via CLI

```bash
# Create all per-event-type views
$ bq-agent-sdk views create-all \
    --project-id=myproject \
    --dataset-id=analytics \
    --prefix=adk_

Created 18 views:
  ✓ adk_llm_requests
  ✓ adk_llm_responses
  ✓ adk_llm_errors
  ✓ adk_tool_starts
  ✓ adk_tool_completions
  ✓ adk_tool_errors
  ✓ adk_user_messages
  ✓ adk_agent_starts
  ✓ adk_agent_completions
  ✓ adk_invocation_starts
  ✓ adk_invocation_completions
  ✓ adk_state_deltas
  ✓ adk_hitl_credential_requests
  ✓ adk_hitl_confirmation_requests
  ✓ adk_hitl_input_requests
  ✓ adk_hitl_credential_completions
  ✓ adk_hitl_confirmation_completions
  ✓ adk_hitl_input_completions

# Create a single view
$ bq-agent-sdk views create LLM_RESPONSE \
    --project-id=myproject \
    --dataset-id=analytics
```

---

### 4.4 CLI Command Reference (Detailed)

#### `bq-agent-sdk get-trace`

```
Usage: bq-agent-sdk get-trace [OPTIONS]

  Retrieve and display a single trace or session.

Options:
  --trace-id TEXT       Retrieve by trace ID
  --session-id TEXT     Retrieve by session ID
  --render              Print hierarchical DAG tree [default: false]
  --format TEXT         json | text | tree [default: json]
```

**Examples:**
```bash
# JSON output for agent consumption
bq-agent-sdk get-trace --session-id=sess-001 --format=json

# Tree rendering for human debugging
bq-agent-sdk get-trace --trace-id=trace-abc --render --format=tree
```

---

#### `bq-agent-sdk list-traces`

```
Usage: bq-agent-sdk list-traces [OPTIONS]

  List recent traces matching filter criteria.

Options:
  --agent-id TEXT       Filter by agent name
  --user-id TEXT        Filter by user ID
  --session-ids TEXT    Comma-separated session IDs
  --last TEXT           Time window: 1h, 24h, 7d, 30d
  --start-time TEXT     ISO8601 start time
  --end-time TEXT       ISO8601 end time
  --has-error           Only sessions with errors
  --no-error            Only sessions without errors
  --min-latency INT     Minimum latency (ms)
  --max-latency INT     Maximum latency (ms)
  --event-types TEXT    Comma-separated event types
  --limit INT           Max traces [default: 20]
  --format TEXT         json | text | table [default: json]
```

**Example:**
```bash
# List error sessions from last hour
bq-agent-sdk list-traces \
  --agent-id=support_bot \
  --last=1h \
  --has-error \
  --format=table

SESSION_ID        SPANS  ERRORS  LATENCY_MS  STARTED_AT
sess-042          15     2       8400        2026-03-06T14:23:00Z
sess-047          8      1       6100        2026-03-06T14:45:00Z
sess-051          22     3       12400       2026-03-06T15:02:00Z
```

---

#### `bq-agent-sdk evaluate`

```
Usage: bq-agent-sdk evaluate [OPTIONS]

  Run code-based or LLM evaluation over traces.

Options:
  --evaluator TEXT      Evaluator type:
                          latency, error_rate, turn_count,
                          token_efficiency, cost,
                          llm-judge
  --threshold FLOAT     Pass/fail threshold
  --criterion TEXT      LLM judge criterion:
                          correctness, hallucination,
                          sentiment, custom
  --custom-prompt TEXT  Custom LLM judge prompt (with --criterion=custom)
  --agent-id TEXT       Filter by agent
  --last TEXT           Time window
  --start-time TEXT     ISO8601 start
  --end-time TEXT       ISO8601 end
  --limit INT           Max sessions [default: 100]
  --exit-code           Return exit code 1 on failure
  --format TEXT         json | text [default: json]
```

**Examples:**
```bash
# Code-based latency check (agent tool call)
bq-agent-sdk evaluate --evaluator=latency --threshold=5000 --agent-id=bot --last=1h

# LLM correctness judge (CI pipeline)
bq-agent-sdk evaluate --evaluator=llm-judge --criterion=correctness \
  --threshold=0.7 --last=24h --exit-code

# Custom LLM judge with user-defined prompt
bq-agent-sdk evaluate --evaluator=llm-judge --criterion=custom \
  --custom-prompt="Rate how well the agent handled PII. Score 0-1." \
  --threshold=0.9 --last=24h
```

---

#### `bq-agent-sdk insights`

```
Usage: bq-agent-sdk insights [OPTIONS]

  Generate comprehensive agent insights report.

Options:
  --agent-id TEXT       Filter by agent
  --last TEXT           Time window
  --max-sessions INT    Max sessions to analyze [default: 50]
  --format TEXT         json | text [default: json]
```

**Example:**
```bash
bq-agent-sdk insights --agent-id=support_bot --last=24h --format=text

══════════════════════════════════════════════════
          Agent Insights — support_bot
══════════════════════════════════════════════════
Sessions analyzed: 48 / 1,234 total

Goal Distribution:
  question_answering    62%  (30 sessions)
  task_automation       25%  (12 sessions)
  data_retrieval        13%  (6 sessions)

Outcome Distribution:
  success               78%
  partial_success       12%
  failure                8%
  abandoned              2%

Top Friction Points:
  1. high_latency         15 sessions (31%)
  2. too_many_tool_calls   8 sessions (17%)
  3. repetitive_responses  4 sessions  (8%)

Executive Summary:
  The support_bot agent shows strong overall performance
  with 78% success rate. Primary area for improvement is
  latency — 31% of sessions experienced high latency,
  particularly in multi-tool workflows. Consider caching
  frequently-accessed data or parallelizing tool calls.
══════════════════════════════════════════════════
```

---

#### `bq-agent-sdk drift`

```
Usage: bq-agent-sdk drift [OPTIONS]

  Run drift detection against a golden question set.

Options:
  --golden-dataset TEXT     Golden questions table (required)
  --agent-id TEXT           Filter by agent
  --last TEXT               Time window
  --embedding-model TEXT    Model for semantic matching
  --min-coverage FLOAT      Minimum coverage to pass [default: 0.0]
  --exit-code               Return exit code 1 if below min-coverage
  --format TEXT             json | text [default: json]
```

---

#### `bq-agent-sdk distribution`

```
Usage: bq-agent-sdk distribution [OPTIONS]

  Analyze question distribution patterns.

Options:
  --mode TEXT           Analysis mode:
                          frequently_asked, frequently_unanswered,
                          auto_group_using_semantics, custom
  --categories TEXT     Comma-separated custom categories (with --mode=custom)
  --top-k INT           Top items per category [default: 10]
  --agent-id TEXT       Filter by agent
  --last TEXT           Time window
  --format TEXT         json | text [default: json]
```

---

## 5. Security & IAM

### 5.1 Least-Privilege IAM Roles

Each path requires a distinct service account with minimum permissions.
**Never use the default Compute Engine or App Engine service account.**

#### Remote Function Runtime SA (`bq-analytics-fn@PROJECT.iam`)

This SA runs the Cloud Function / Cloud Run service.

```bash
# Create SA
gcloud iam service-accounts create bq-analytics-fn \
  --display-name="BQ Agent Analytics Remote Function"

# Grant minimum roles
# 1. Read agent_events table
gcloud projects add-iam-policy-binding PROJECT \
  --member="serviceAccount:bq-analytics-fn@PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataViewer" \
  --condition="expression=resource.name.startsWith('projects/PROJECT/datasets/analytics'),title=analytics-only"

# 2. Run BigQuery jobs (for SDK queries)
gcloud projects add-iam-policy-binding PROJECT \
  --member="serviceAccount:bq-analytics-fn@PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

| Role | Resource | Why |
|------|----------|-----|
| `roles/bigquery.dataViewer` | `analytics` dataset | Read `agent_events` and golden tables |
| `roles/bigquery.jobUser` | Project | Execute BQ queries from within Cloud Function |

#### BigQuery Connection SA (auto-created)

When you create a `CLOUD_RESOURCE` connection, BigQuery auto-creates a SA.
Grant it the invoker role on the Cloud Function.

```bash
# Find the connection's SA
CONNECTION_SA=$(bq show --connection --format=json PROJECT.us.analytics-conn \
  | jq -r '.cloudResource.serviceAccountId')

# Grant Cloud Run Invoker so BigQuery can call the function
gcloud functions add-invoker-policy-binding bq-agent-analytics \
  --region=us-central1 \
  --member="serviceAccount:${CONNECTION_SA}"
```

| Role | Resource | Why |
|------|----------|-----|
| `roles/run.invoker` | Cloud Function / Cloud Run | Allow BigQuery to invoke the remote function |

#### Continuous Query SA (`analytics-cq@PROJECT.iam`)

```bash
gcloud iam service-accounts create analytics-cq \
  --display-name="BQ Continuous Query SA"

# Read source, write destination, use AI models
gcloud projects add-iam-policy-binding PROJECT \
  --member="serviceAccount:analytics-cq@PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor" \
  --condition="expression=resource.name.startsWith('projects/PROJECT/datasets/analytics'),title=analytics-dataset"

gcloud projects add-iam-policy-binding PROJECT \
  --member="serviceAccount:analytics-cq@PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"

gcloud projects add-iam-policy-binding PROJECT \
  --member="serviceAccount:analytics-cq@PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.connectionUser"
```

| Role | Resource | Why |
|------|----------|-----|
| `roles/bigquery.dataEditor` | `analytics` dataset | Read `agent_events`, write analysis tables |
| `roles/bigquery.jobUser` | Project | Run continuous query jobs |
| `roles/bigquery.connectionUser` | Connection | Invoke AI.GENERATE_TEXT via remote model connection |

### 5.2 CLI Authentication

The CLI uses Application Default Credentials (ADC). No service account
key files.

```bash
# Interactive login (developer workstation)
gcloud auth application-default login

# Service account (CI/CD)
gcloud auth activate-service-account --key-file=sa-key.json
# or: use Workload Identity Federation (recommended for GitHub Actions)
```

Required user/SA permissions for CLI:
- `roles/bigquery.dataViewer` on the analytics dataset
- `roles/bigquery.jobUser` on the project

---

## 6. Cost Model

### 6.1 Per-1K Sessions Cost Estimate

Costs assume US multi-region, on-demand pricing (March 2026).

| Component | Per 1K Sessions | Assumptions |
|-----------|----------------|-------------|
| **BigQuery scan** (CLI / Remote Fn) | ~$0.03 | ~6 MB scanned per session × $6.25/TB |
| **Cloud Function invocation** (Remote Fn) | ~$0.01 | 1K invocations × $0.40/million + 256MB × 500ms |
| **AI.GENERATE_TEXT** (Continuous Query) | ~$1.25 | 1K calls × ~500 input tokens × $0.075/1M + ~100 output tokens × $0.30/1M (Gemini 2.5 Flash) |
| **BigQuery continuous query slots** | ~$0.50/hr idle | 1 slot minimum × Enterprise edition pricing; billed per reservation |
| **Pub/Sub export** (alerting) | ~$0.004 | 1K messages × $40/million |
| **Bigtable export** | ~$0.01 | 1K writes × $0.01/100K rows (depends on instance) |

### 6.2 Monthly Cost Scenarios

| Scenario | Volume | Estimated Monthly Cost |
|----------|--------|----------------------|
| **Small** (dev/test) | 10K sessions, CLI only | < $1 (BQ scan only) |
| **Medium** (production) | 100K sessions, CLI + Remote Fn + nightly eval | ~$15 (BQ scan + Cloud Function) |
| **Large** (streaming) | 500K sessions, all paths + Continuous Query | ~$700 (dominated by CQ reservation + AI.GENERATE_TEXT) |

### 6.3 Cost Optimization

- **Partitioning:** `agent_events` table should be partitioned by `timestamp`
  (ingestion-time or column). All queries use `--last` / `WHERE timestamp >=`
  which prunes partitions, reducing scan cost by 90%+.
- **Materialized views:** Cache `daily_quality` table to avoid re-scanning
  raw events.
- **AI.GENERATE_TEXT batching:** Continuous queries process rows as they
  arrive; no additional batching optimization needed.
- **Slot reservations:** For continuous queries, a FLEX reservation (per-minute
  billing) is cheaper than on-demand for sustained workloads.

---

## 7. Per-Path SLOs & Operational Runbook

### 7.1 Service-Level Objectives

| Path | Metric | Target | Measurement |
|------|--------|--------|-------------|
| **CLI** | Command latency (p95) | < 10s for `evaluate` (≤ 100 sessions) | CLI timing output (`--verbose`) |
| **CLI** | Availability | 99.9% (bounded by BigQuery SLA) | BQ job success rate |
| **CLI** | Max error rate | < 1% of CLI invocations fail due to SDK bugs (vs infra) | Error log classification |
| **Remote Function** | Response latency (p95) | < 5s for `analyze`, < 15s for `judge` | Cloud Monitoring function latency |
| **Remote Function** | Availability | 99.5% (Cloud Function + BQ connection) | Cloud Monitoring uptime check |
| **Remote Function** | Max error rate | < 2% of calls return non-retryable errors | `errorMessage` rate in BQ audit logs |
| **Remote Function** | Cold start | < 3s (Cloud Function gen2) | Cloud Monitoring cold start metric |
| **Continuous Query** | Processing latency | < 30s from event ingestion to analysis row written | `analyzed_at - timestamp` delta |
| **Continuous Query** | Availability | 99% (bounded by Enterprise reservation) | `INFORMATION_SCHEMA.JOBS` status |

### 7.2 Retry Behavior

| Path | Retry Strategy |
|------|----------------|
| **CLI** | No automatic retry. User re-runs command. `--exit-code` returns 2 for infra errors (vs 1 for eval failure). |
| **Remote Function** | BigQuery automatically retries on HTTP 408, 429, 500, 503, 504. The Cloud Function must be **idempotent** for a given `(requestId, call_index)` pair. Non-retryable errors return HTTP 400. |
| **Continuous Query** | BigQuery restarts failed continuous queries automatically. If a row fails AI.GENERATE_TEXT, the row is skipped (no dead-letter). Monitor via `INFORMATION_SCHEMA.JOBS`. |

### 7.3 Operational Runbook: "What Happens When It Fails"

#### Remote Function Failures

| Failure | Symptom | Diagnosis | Resolution |
|---------|---------|-----------|------------|
| **Cold start timeout** | First query after idle returns timeout | Cloud Monitoring → Function latency spike | Set `--min-instances=1` in deployment; increase timeout to 120s |
| **Batch too large** | HTTP 413 or OOM | Cloud Monitoring → memory usage | Reduce `max_batching_rows` in CREATE FUNCTION DDL (default 50 → 10) |
| **SDK query fails** | `errorMessage` in response | Cloud Function logs → BQ error | Check SA permissions (§5.1); verify `agent_events` table exists |
| **Quota exhaustion** | HTTP 429 from Cloud Function | Cloud quotas dashboard | Request quota increase; add `max_batching_rows` limit |
| **Connection SA expired** | "Permission denied" in BQ | Connection details → SA status | Re-grant `roles/run.invoker` to connection SA |

#### Continuous Query Failures

| Failure | Symptom | Diagnosis | Resolution |
|---------|---------|-----------|------------|
| **AI.GENERATE_TEXT quota** | Query pauses / slows | `INFORMATION_SCHEMA.JOBS` → error message | Increase Vertex AI quota; reduce `max_output_tokens` |
| **Reservation exhausted** | Query queued, not processing | Reservation monitor → slot utilization | Add FLEX slots or reduce concurrent CQ count |
| **Query exceeds 2-day limit** | Query stops | `INFORMATION_SCHEMA.JOBS` → end_time | Use service account (150-day limit); set up auto-restart cron |
| **Destination table schema mismatch** | Insert fails | CQ error log → schema error | ALTER TABLE to add new columns; restart CQ |
| **Pub/Sub topic deleted** | EXPORT DATA fails | CQ error log | Recreate topic; restart CQ |

#### CLI Failures

| Failure | Symptom | Diagnosis | Resolution |
|---------|---------|-----------|------------|
| **ADC not configured** | "Could not automatically determine credentials" | `gcloud auth list` | Run `gcloud auth application-default login` |
| **Dataset not found** | "Not found: Dataset" | Verify `--dataset-id` | Check project/dataset spelling; verify IAM access |
| **Timeout on large evaluation** | Command hangs > 60s | BQ job duration in console | Add `--limit=50` to reduce session count; use `--last=1h` |

### 7.4 Alerting Recommendations

```bash
# Cloud Monitoring alert policy for Remote Function errors
gcloud monitoring policies create \
  --display-name="BQ Analytics Remote Fn Error Rate" \
  --condition-filter='resource.type="cloud_function" AND metric.type="cloudfunctions.googleapis.com/function/execution_count" AND metric.labels.status!="ok"' \
  --condition-threshold-value=0.02 \
  --condition-threshold-comparison=COMPARISON_GT \
  --notification-channels=CHANNEL_ID

# BigQuery INFORMATION_SCHEMA query for continuous query health
SELECT
  job_id,
  state,
  error_result.reason AS error_reason,
  creation_time,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), creation_time, HOUR) AS running_hours
FROM `region-us`.INFORMATION_SCHEMA.JOBS
WHERE job_type = 'QUERY'
  AND configuration.query.continuous = TRUE
  AND creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
ORDER BY creation_time DESC;
```

---

## 8. Implementation Roadmap

### 8.1 MVP Definition (v1.0)

**v1.0 ships CLI + `evaluate` + `get-trace` + `--exit-code`.** This is the
minimum surface that unblocks all three personas:

| Feature | Persona Unblocked | Why MVP |
|---------|-------------------|---------|
| `bq-agent-sdk evaluate` | Marcus (CI/CD), AgentX (self-diagnostic) | Core value: "is my agent healthy?" |
| `bq-agent-sdk get-trace` | AgentX (context retrieval) | Enables self-correction loop (CUJ-B5) |
| `--exit-code` | Marcus (CI/CD gate) | Blocks deploy on eval failure |
| `--format=json` | AgentX (machine consumption) | Structured output for agent tool use |
| `bq-agent-sdk doctor` | Marcus (pre-deploy check) | Validates setup before first eval |

**v1.1 (post-MVP):**

| Feature | Target |
|---------|--------|
| Remote Function (`agent_analytics()`) | v1.1 — requires Cloud Function deployment infra |
| `bq-agent-sdk insights`, `drift`, `distribution` | v1.1 — higher-level analytics |
| `bq-agent-sdk views`, `hitl-metrics`, `list-traces` | v1.1 — convenience commands |
| Continuous Query templates | v1.2 — requires Enterprise reservation |

### 8.2 Phases

#### Phase 1: Core Refactoring (1 week) — *v1.0*

- [ ] Extract filter-building helpers (`--last`, `--agent-id`, etc.) into
      shared utility that constructs `TraceFilter` from CLI args or remote
      function params
- [ ] Add uniform response serialization layer for both interface boundaries
      (CLI and Remote Function):
      - **Dataclass returns** (`Trace`, `Span`, `ContentPart`, `ObjectRef`):
        Add `Trace.to_dict()` / `Span.to_dict()` methods that recursively
        convert nested dataclasses and `datetime` fields to JSON-safe dicts
        (ISO 8601 strings for datetimes)
      - **Pydantic returns** (`EvaluationReport`, `InsightsReport`,
        `DriftReport`, `QuestionDistribution`, `WorldChangeReport`): Use
        `.model_dump(mode="json")` or `.model_dump_json()` — **not** plain
        `.model_dump()`, which preserves raw `datetime` objects that fail
        `json.dumps()`. Affected fields include `EvaluationReport.created_at`,
        `InsightsReport.created_at`, `SessionMetadata.start_time`/`end_time`,
        etc.
      - **dict returns** (`doctor()`, `hitl_metrics()`): Verify datetime values
        are converted to ISO 8601 strings before output
- [ ] Add `--format` output formatting layer (JSON, text table, tree)

**Exit criteria:**
- `json.dumps(serialize(result))` succeeds for every `Client` public method
  return type — including `Trace`, `EvaluationReport`, `InsightsReport`,
  `DriftReport`, and `QuestionDistribution` (unit test for each)
- `TraceFilter.from_cli_args()` parses `--last=1h`, `--agent-id=X`,
  `--session-id=Y` (unit test coverage ≥ 90%)
- Format layer renders JSON / text / table for a sample trace (snapshot test)

#### Phase 2: CLI MVP (`bq-agent-sdk`) (2 weeks) — *v1.0*

- [ ] Add `typer` dependency (optional `[cli]` extra in `pyproject.toml`)
- [ ] Implement CLI entry point in `pyproject.toml` `[project.scripts]`
- [ ] Implement v1.0 commands: `doctor`, `get-trace`, `evaluate`
- [ ] Add `--exit-code` support for CI/CD integration
- [ ] Add `--last` time window parser (`1h`, `24h`, `7d`, `30d`)
- [ ] Write CLI integration tests (mock BQ client, ≥ 85% line coverage)
- [ ] Write quickstart guide with copy-paste examples

**Exit criteria:**
- `pip install bigquery-agent-analytics-sdk[cli]` → `bq-agent-sdk --help`
  works (smoke test in CI)
- `bq-agent-sdk evaluate --last=1h --evaluator=latency --threshold=5000
  --exit-code` returns exit code 1 on failure (integration test)
- `bq-agent-sdk get-trace --session-id=X --format=json` returns valid JSON
  (integration test)
- Quickstart guide timed walkthrough completes in < 10 minutes
- Sample GitHub Actions workflow passes with mock credentials

#### Phase 3: Remote Function (2 weeks) — *v1.1*

- [ ] Create `deploy/remote_function/` directory with:
      - `main.py` (functions-framework entry point)
      - `requirements.txt`
      - `deploy.sh` (gcloud deployment script)
      - `register.sql` (CREATE FUNCTION DDL templates)
- [ ] Implement dispatch for: `analyze`, `evaluate`, `judge`, `insights`,
      `drift`
- [ ] Use the uniform serialization layer from Phase 1 in `_dispatch()` to
      convert all SDK return types to JSON-safe dicts before building the
      reply array (see Phase 1 serialization policy)
- [ ] Reuse the existing `_run_sync()` bridge (`client.py:247`) for sync
      wrappers — `insights()`, `drift_detection()`, and `deep_analysis()`
      already route through it, so the Cloud Function entry point can call
      these sync methods directly without async/sync boundary concerns
- [ ] Add Terraform/gcloud deployment automation
- [ ] Write integration tests with BigQuery Remote Function simulator
- [ ] Document deployment guide with IAM prerequisites (see §5)

**Exit criteria:**
- `deploy.sh` deploys to a test project and `SELECT agent_analytics('analyze',
  JSON'{"session_id":"test"}')` returns valid JSON (end-to-end test)
- Partial failure in a batch (1 of 5 calls errors) returns per-row error,
  not batch-level 400 (integration test)
- All 5 operations return `json.dumps()`-safe responses (no raw `datetime`
  objects, no dataclass instances) — verified by integration test
- `deploy/remote_function/README.md` includes IAM roles, cost estimate,
  and troubleshooting guide
- Remote Function p95 latency < 5s for `analyze` operation (load test with
  50 concurrent calls)

#### Phase 4: CLI v1.1 Commands + Continuous Query Templates (2 weeks) — *v1.1/v1.2*

- [ ] Implement remaining CLI commands: `insights`, `drift`, `distribution`,
      `hitl-metrics`, `list-traces`, `views`
- [ ] Document LLM tool-calling schema for agent integration
- [ ] Create `deploy/continuous_queries/` directory with:
      - `realtime_error_analysis.sql` — AI.GENERATE_TEXT error classification
      - `session_scoring.sql` — per-session quality scoring
      - `pubsub_alerting.sql` — critical error → Pub/Sub export
      - `bigtable_dashboard.sql` — session metrics → Bigtable
      - `setup_reservation.md` — Enterprise reservation guide
- [ ] Document AI.GENERATE_TEXT prompt templates aligned with SDK evaluation
      criteria (correctness, hallucination, sentiment)
- [ ] Add backfill guide (FOR SYSTEM_TIME AS OF → APPENDS handoff)
- [ ] Document continuous query monitoring via INFORMATION_SCHEMA.JOBS

**Exit criteria:**
- All 9 CLI commands pass integration tests (mock BQ client)
- Each continuous query template runs without syntax errors in BigQuery
  dry-run mode (`--dry_run` flag)
- Continuous query monitoring query returns job status for a running
  template

#### Phase 5: Documentation, Polish & Pilot (1 week) — *v1.0 GA*

- [ ] Update SDK.md with CLI, Remote Function, and Continuous Query sections
- [ ] Add `examples/cli_agent_tool.py` — example ADK agent using CLI as tool
- [ ] Add `examples/ci_eval_pipeline.sh` — example CI/CD script
- [ ] Add `examples/remote_function_dashboard.sql` — example Looker queries
- [ ] Add `examples/continuous_query_alerting.sql` — real-time error alerting
- [ ] Update README.md with new interfaces
- [ ] Run design-partner pilot (see §10)

**Exit criteria:**
- All examples run without errors against a test dataset
- Pilot partners complete assigned CUJs within time targets (see §10.2)
- Zero unresolved P0/P1 bugs from pilot feedback

### 8.3 Migration / Onboarding Path

**Python-only user today → CLI in 15 minutes → Remote Function in 1 day**

#### Step 1: CLI (15 minutes)

```bash
# Install with CLI extra
pip install bigquery-agent-analytics-sdk[cli]

# Set environment variables (avoid repeating in every command)
export BQ_AGENT_PROJECT=myproject
export BQ_AGENT_DATASET=analytics

# Health check
bq-agent-sdk doctor

# First evaluation
bq-agent-sdk evaluate --evaluator=latency --threshold=5000 --last=1h

# Retrieve a specific trace
bq-agent-sdk get-trace --session-id=sess-001 --format=json
```

#### Step 2: CI/CD gate (30 minutes)

```yaml
# Add to .github/workflows/agent-eval.yml
- name: Install SDK
  run: pip install bigquery-agent-analytics-sdk[cli]

- name: Gate on latency
  run: bq-agent-sdk evaluate --evaluator=latency --threshold=5000 --last=24h --exit-code
```

#### Step 3: Remote Function (1 day)

```bash
# Clone and deploy
git clone https://github.com/haiyuan-eng-google/BigQuery-Agent-Analytics-SDK.git
cd BigQuery-Agent-Analytics-SDK/deploy/remote_function
./deploy.sh --project=myproject --region=us-central1

# Register in BigQuery (copy-paste from register.sql)
bq query --use_legacy_sql=false < register.sql

# First SQL evaluation
bq query --use_legacy_sql=false \
  "SELECT \`myproject.analytics.agent_analytics\`('analyze', JSON'{\"session_id\":\"sess-001\"}')"
```

#### Step 4: Continuous Query (optional, requires Enterprise edition)

```bash
# Deploy real-time error alerting template
bq query --use_legacy_sql=false --continuous=true \
  < deploy/continuous_queries/realtime_error_analysis.sql
```

---

## 9. Success Metrics

### 9.1 Adoption Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Time-to-first-value (CLI)** | First CLI eval run in < 10 minutes from `pip install` | Timed walkthrough in quickstart guide |
| **Time-to-first-value (Remote Fn)** | First SQL `agent_analytics()` call in < 30 minutes from repo clone | Timed deploy + query walkthrough |
| CLI adoption | 20% of SDK users use CLI within 3 months | PyPI download stats for `[cli]` extra |
| Remote Function deployments | 10 production deployments within 6 months | Deployment telemetry |
| Agent tool integration | 5 agents use CLI for self-diagnostics | Community feedback / GitHub issues |
| CI/CD integration | 3 orgs use `--exit-code` in pipelines | Community feedback |
| Token savings for agents | 35x fewer tokens vs MCP schema loading (~4K vs ~145K); 60% fewer vs SQL generation | Benchmarked comparison against MCP baseline (see §4.1.1) |

### 9.2 User Outcome Metrics

| Outcome | Target | How Measured |
|---------|--------|--------------|
| **Mean time to detect agent regression** | < 15 minutes (from event to alert) | Continuous Query → Pub/Sub pipeline latency; CLI cron interval |
| **MTTR reduction for agent incidents** | 30% reduction vs manual investigation | Before/after comparison during pilot (see §10) |
| **Eval pipeline setup time** | < 1 hour for full CI/CD gate (evaluate + drift + exit-code) | Pilot partner timed walkthrough |
| **SQL analyst unblocked** | Priya-persona can build dashboard without filing Python ticket | Pilot partner interview |
| **Agent self-correction rate** | Agents using CLI self-diagnostics resolve 50% of tool failures without human intervention | Session trace analysis (self-correction loop detected) |

---

## 10. Design-Partner Validation Plan

### 10.1 Pilot Partners

| Partner Profile | Persona | Focus Area | v1 Feature Set |
|----------------|---------|------------|----------------|
| **Analyst team** (1 data analyst / BI engineer) | Priya | Remote Function + Looker dashboard | `evaluate`, `analyze` via SQL |
| **Agent team** (1 agent developer) | AgentX | CLI as agent tool for self-diagnostics | `evaluate`, `get-trace` via CLI |
| **Platform team** (1 SRE / DevOps engineer) | Marcus | CI/CD pipeline with `--exit-code` | `evaluate`, `drift` via CLI + GitHub Actions |

### 10.2 Pilot Success Criteria

| Criterion | Measurement | Pass Threshold |
|-----------|-------------|----------------|
| Time-to-first-eval (CLI) | Timed from `pip install` to first `evaluate` output | < 10 minutes |
| Time-to-first-eval (Remote Fn) | Timed from repo clone to first SQL `agent_analytics()` result | < 30 minutes |
| Task completion without help | Pilot user completes assigned CUJ without asking SDK team questions | 2 out of 3 users |
| Token overhead acceptable | Agent pilot measures context tokens consumed by CLI tool calls | < 5,000 tokens per CLI invocation |
| CI/CD gate works end-to-end | Platform pilot configures `--exit-code` in real pipeline, blocks on failure | Blocks deploy on eval failure |
| No P0 bugs | Pilot users report zero data-loss or incorrect evaluation results | 0 P0 bugs |

### 10.3 Pilot Protocol

1. **Week 1:** Onboard 3 pilot partners; provide quickstart guide + 30-min walkthrough
2. **Weeks 2–3:** Partners use their assigned path independently; SDK team collects:
   - Setup friction (where did they get stuck?)
   - Feature gaps (what did they need that was missing?)
   - Bug reports (severity-tagged)
3. **Week 4:** Debrief interviews; collect NPS score (0–10) and written feedback
4. **GA Decision Gate:** Proceed to GA if:
   - All 3 pilots achieve time-to-first-eval targets
   - NPS ≥ 7 for 2 out of 3 partners
   - Zero unresolved P0/P1 bugs
   - Documentation rated "sufficient" or better by all 3

---

## 11. Non-Goals (Out of Scope)

- **Web UI / dashboard** — Use Looker/Data Studio with remote functions instead
- **Custom real-time processing** — The SDK does not build a streaming
  pipeline. Real-time analytics are handled via BigQuery Continuous Query
  templates (Path A', §3A) which use native `AI.GENERATE_TEXT` — the SDK
  provides SQL templates but no custom runtime
- **Agent framework integration** — The CLI is framework-agnostic; specific ADK
  tool wrappers are a separate effort
- **Multi-cloud** — BigQuery-only for now

---

## 12. Open Questions

1. **CLI framework:** `click` vs `typer` — typer has better auto-generated
   help and type inference, but click has broader ecosystem support.
   **Recommendation:** `typer` (better developer experience, auto-complete,
   and its auto-generated `--help` output is concise enough for LLM
   just-in-time consumption — see §4.1.2).

2. **Authentication:** Should the CLI handle `gcloud auth` automatically, or
   require users to have Application Default Credentials configured?
   **Recommendation:** Require ADC; add `bq-agent-sdk auth check` command.

3. **Remote function granularity:** ~~One function per operation vs one
   multiplexed function?~~
   **Decided:** One multiplexed function `agent_analytics(operation, params)`
   (simpler deployment, single connection). All CUJs and examples in this
   document use this approach.

4. **Versioning:** Should the CLI version be tied to the SDK version?
   **Recommendation:** Yes, single version number for all interfaces.
