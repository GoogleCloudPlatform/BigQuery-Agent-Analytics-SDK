# BigQuery Agent Analytics SDK

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13%20|%203.14-blue)](pyproject.toml)
[![CI](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/actions/workflows/ci.yml/badge.svg)](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/actions/workflows/ci.yml)

An open-source Python SDK for analyzing, evaluating, and curating agent traces
stored in BigQuery. Built on top of the
[BigQuery Agent Analytics](https://adk.dev/observability/bigquery-agent-analytics/), it provides
a consumption-layer toolkit for agent observability, analysis, evaluation, and advanced capabilities like the Agent Context Graph — extracting decision traces from your agent's context graph — at scale.

## Overview

The BigQuery Agent Analytics SDK connects your AI agent telemetry in BigQuery to
a rich set of evaluation, observability, and analytics capabilities. It is
designed for ML engineers, data scientists, and platform teams who run agents in
production and need to understand agent behavior, measure quality, and detect
regressions — all through BigQuery SQL or Python.

## Key Features

**Observability**
- Trace reconstruction and DAG visualization
- Per-event-type BigQuery views
- Observability dashboards (Looker Studio, SQL, and BigFrames)

**Evaluation**
- Code-based metrics (latency, turn count, error rate, token efficiency, cost)
- LLM-as-Judge scoring (correctness, hallucination, sentiment)
- Trajectory matching (exact, in-order, any-order)
- Multi-trial evaluation with pass@k / pass^k
- Grader composition (weighted, binary, majority strategies)
- Eval suite lifecycle management with graduation and saturation detection
- Static quality validation (ambiguous tasks, class imbalance, suspicious thresholds)

**AI/ML Integration**
- BigQuery AI.GENERATE, AI.EMBED, AI.CLASSIFY
- Anomaly detection and latency forecasting
- Categorical (Hatteras-style) evaluation via BigFrames

**Advanced Analytics**
- Agent Context Graph — extract decision traces from your agent's context graph: the requests an agent handled, the options it weighed, and the outcomes it committed, materialized into a queryable BigQuery property graph (GQL traversal, scheduled refresh via `bqaa context-graph`)
- Long-horizon cross-session memory
- Multi-stage agent insights pipeline
- Drift detection for golden vs production question distributions

**CLI** (`bq-agent-sdk`)
- 12+ commands for diagnostics, evaluation, and CI/CD integration

**Deployment Surfaces**
- Remote Function (BigQuery SQL via Cloud Run)
- Python UDF scoring kernels
- Streaming evaluation (Cloud Scheduler + Cloud Run)
- Continuous query templates

**Usage Telemetry**
- Every job the SDK submits is labeled (`sdk`, `sdk_version`,
  `sdk_surface`, `sdk_feature`, and `sdk_ai_function` where relevant)
  so operators can attribute spend, latency, and adoption directly
  from `INFORMATION_SCHEMA.JOBS_BY_PROJECT`. No extra telemetry
  pipeline is required. See [docs/sdk_usage_tracking.md](docs/sdk_usage_tracking.md)
  for the label schema and ready-to-run tracking queries.

## Prerequisites

- Python 3.10+
- A Google Cloud project with BigQuery enabled
- Agent traces stored in BigQuery via the
  [ADK BigQuery Trace Exporter](https://github.com/google/adk-python/tree/main/contributing/extensions/bigquery_trace_exporter)

## Installation

```bash
pip install bigquery-agent-analytics
```

With optional LLM judge support:

```bash
pip install bigquery-agent-analytics[llm]
```

With BigFrames support:

```bash
pip install bigquery-agent-analytics[bigframes]
```

With LangSmith export support:

```bash
pip install bigquery-agent-analytics[langsmith]
```

## Quick Start

```python
from bigquery_agent_analytics import Client

client = Client(project_id="my-project", dataset_id="analytics")
trace = client.get_trace("trace-abc-123")
trace.render()
```

### Export traces to LangSmith

Export the standard ADK `agent_events` schema with Application Default
Credentials and LangSmith's standard environment variables:

```bash
export LANGSMITH_API_KEY=lsv2_...
export LANGSMITH_PROJECT=agent-production

bq-agent-sdk export langsmith \
  --source=my-project.analytics.agent_events \
  --since=2026-08-01T00:00:00Z
```

The CLI intentionally accepts the API key only through
`LANGSMITH_API_KEY`, keeping it out of shell history and process arguments.

The exporter reconstructs span parents and derives stable LangSmith UUIDs. It
treats created runs as immutable: replaying an overlapping window is an
idempotent no-op for existing run IDs while still creating previously unseen
IDs. Run IDs derive from the source identity and not the destination, so
exporting to a fresh LangSmith project reuses the same IDs and creates nothing.
To correct already-exported data, use a deliberately versioned `--source-id`.
For scheduled syncs, use `--incremental` with `--watermark-file=state.json`.
The JSON summary bounds row-level diagnostics with `--max-dropped-rows` and
reports the number omitted as `dropped_rows_truncated`.

LangSmith Cloud rejects runs whose `start_time` is more than 24 hours from now,
so an export covers recent traces rather than aged trace history. Run
`--incremental` on a schedule frequent enough to stay inside that window. See
[SDK.md](SDK.md#23-langsmith-export) for the exact error and its effect on
bounded backfills.

Custom schemas use a YAML mapping from LangSmith fields to source column or
nested paths. Unmapped columns remain in `extra.metadata`; payload values are
opaque and are never classified by event type. Optional fields omitted from a
custom mapping remain unmapped rather than inheriting ADK column names:

```yaml
fields:
  run_id: event_key
  trace_id: trace.key
  parent_run_id: parent_key
  name: kind
  start_time: occurred_at
  inputs: payload
```

```bash
bq-agent-sdk export langsmith \
  --source='SELECT * FROM `my-project.custom.events`' \
  --mapping=mapping.yaml --source-id=custom-events-v1
```

See [SDK.md](SDK.md#23-langsmith-export) for the Python API, incremental
watermark contract, filtering, and operational controls.

For session reads, `session_id` is a reusable conversation identifier rather
than a unique trace key. `client.get_session_trace()` resolves user, root agent,
experiment, and labels; if more than one candidate remains it raises
`AmbiguousSessionError` carrying structured candidates for an exact
`client.get_trace_by_selector()` retry. The same contract is used by GQL,
trajectory evaluation, the CLI, the Remote Function, and reports. See
[Identity-safe session resolution](SDK.md#resolve-a-session-safely).

Categorical evaluation can bind trusted per-trace judge context (for example,
a golden expected answer) to the same exact selector:

```python
from bigquery_agent_analytics import ResolvedTraceSelector, TraceFilter

filters = TraceFilter(limit=100)
traces = client.list_traces(filters)
context = {
    ResolvedTraceSelector(trace.identity, trace.scope): expected_answer(trace)
    for trace in traces
}
report = client.evaluate_categorical(
    config,
    filters=filters,
    per_session_context=context,
)
```

Legacy string keys are accepted only when the transcript-eligible evaluated
`session_id` is unambiguous; eligibility is applied before that ambiguity
check, so exact selector keys are recommended whenever session IDs may be
reused. Otherwise `AmbiguousSessionError` fails before any model call.
Context is trusted evaluator material, sent as a query parameter/model prompt
through AI.GENERATE, retry, and API fallback. It is never interpolated into
SQL, logged, persisted, or placed in job labels. Apply the same data-governance
policy you use for evaluation prompts.

When `persist_results=True`, categorical results use an additive, nullable
identity/provenance schema; existing historical rows are not backfilled.
Deploy or roll back safely in this order: **schema, then writer, then views**.
The latest-results view keeps identities distinct even when they share a
`session_id`. During a legacy/schema straddle, a sole typed identity supersedes
matching legacy metric/prompt rows; zero or multiple typed identities leave
legacy rows in their separate `legacy:<session_id>` lane. Trusted judge or
golden-answer context — including any model echo — is never persisted; only
SDK-owned context provenance is. This U5 migration completes #358's remaining
persistence/report gate and unlocks U6/#360.

See [SDK.md](SDK.md) for the full API walkthrough with code examples for every
feature.

### Try it: extract decision traces (Agent Context Graph, ~10 minutes)

Deploy a context graph, seed sample agent events, extract the decision
traces, and query one in GQL — entirely from your terminal:

```bash
export PROJECT_ID="your-project" DATASET="agent_analytics_demo"
gcloud config set project "$PROJECT_ID"
bq --location=US mk --dataset "$PROJECT_ID:$DATASET"

# 1. Deploy the context graph (one-time DDL: tables, then the property graph).
cd examples/context_graph/codelab
envsubst < table_ddl.sql      | bq query --use_legacy_sql=false
envsubst < property_graph.sql | bq query --use_legacy_sql=false

# 2. Seed five sample agent sessions into agent_events.
bqaa seed-events --project-id "$PROJECT_ID" --dataset-id "$DATASET" --sessions 5

# 3. Extract decision traces from the deployed graph
#    (read back via INFORMATION_SCHEMA.PROPERTY_GRAPHS — no SQL file passed).
bqaa context-graph --project-id "$PROJECT_ID" --dataset-id "$DATASET" \
    --graph agent_decisions_graph --lookback-hours 24 --format json

# 4. Query a decision trace: what did the agent weigh, and how did it resolve?
bq query --use_legacy_sql=false "
SELECT * FROM GRAPH_TABLE(
  $DATASET.agent_decisions_graph
  MATCH (req:DecisionRequest)-[eo:evaluatesOption]->(opt:DecisionOption),
        (req)-[ri:resultedIn]->(out:DecisionOutcome)
  COLUMNS (req.request_text AS question, opt.option_label AS considered,
           out.status AS outcome, out.rationale AS rationale))"
```

Expect `"ok": true` with 5 sessions materialized, and fifteen GQL rows — three
options weighed per request, each with the committed outcome and rationale.
The [Agent Context Graph codelab](docs/codelabs/periodic_materialization.md) is the
guided version of these steps (plus backfill and production scheduling), and
[`examples/context_graph/`](examples/context_graph/) is the worked example
with a runnable ADK agent.

## Documentation

| Resource | Description |
|----------|-------------|
| [SDK Feature Reference](SDK.md) | Complete API walkthrough with working code examples |
| [Looker Studio Dashboard](dashboard/looker_studio/README.md) | Published 37-chart BQAA observability template with project/dataset/table configurator |
| [Dashboard User Manual](dashboard/looker_studio/USER_MANUAL.md) | End-user guide to the Looker Studio dashboard: setup in three steps, page guide, sharing, troubleshooting |
| [Agent Context Graph Codelab](docs/codelabs/periodic_materialization.md) | Extract decision traces from your agent's context graph, end to end (~35 min) |
| [Scheduled Deploy Runbook](docs/guides/scheduled-context-graph-deploy.md) | Keep the context graph fresh on a Cloud Run + Cloud Scheduler cron |
| [Design Documents](docs/README.md) | Architecture decisions and design rationale |
| [Examples](examples/README.md) | Notebooks, SQL scripts, and demos |
| [Deployment Guides](deploy/README.md) | Four deployment surfaces for Google Cloud |

## Architecture

```
src/bigquery_agent_analytics/
│
├── Core
│   ├── client.py                  # High-level SDK client
│   ├── trace.py                   # Trace reconstruction & visualization
│   ├── views.py                   # Per-event-type BigQuery view management
│   ├── event_semantics.py         # Canonical event type helpers & predicates
│   ├── serialization.py           # Uniform serialization layer
│   └── formatter.py               # Output formatting (json/text/table)
│
├── Evaluation
│   ├── evaluators.py              # SystemEvaluator + LLMAsJudge
│   ├── trace_evaluator.py         # Trajectory matching & replay
│   ├── multi_trial.py             # Multi-trial runner + pass@k
│   ├── grader_pipeline.py         # Grader composition pipeline
│   ├── eval_suite.py              # Eval suite lifecycle management
│   └── eval_validator.py          # Static validation checks
│
├── AI/ML
│   ├── ai_ml_integration.py       # BigQuery AI/ML capabilities
│   ├── bigframes_evaluator.py     # BigFrames DataFrame evaluator
│   ├── categorical_evaluator.py   # Hatteras categorical evaluation
│   └── categorical_views.py       # Categorical metric views
│
├── Analytics
│   ├── insights.py                # Multi-stage insights pipeline
│   ├── feedback.py                # Drift detection & question distribution
│   └── memory_service.py          # Long-horizon agent memory
│
├── Export
│   └── export/
│       ├── __init__.py             # Stable public export API
│       ├── cli.py                  # bq-agent-sdk export command group
│       └── langsmith.py            # Schema-agnostic LangSmith connector
│
├── Agent Context Graph
│   ├── context_graph.py           # Decision-trace extraction & GQL traversal
│   ├── materialize_window.py      # Scheduled materialization (bqaa context-graph)
│   └── property_graph_spec.py     # Derive the spec from your deployed property graph
│
└── CLI & Deploy
    ├── cli.py                     # CLI entry point (bq-agent-sdk)
    ├── udf_kernels.py             # Python UDF scoring kernels
    └── udf_sql_templates.py       # UDF SQL generation
```

## Related Projects

- [Google ADK](https://github.com/google/adk-python) — Agent Development Kit
  for building AI agents
- [BigQuery](https://cloud.google.com/bigquery) — Google Cloud analytics data
  warehouse
- [BigQuery AI Functions](https://cloud.google.com/bigquery/docs/ai-application-overview) —
  AI.GENERATE, AI.EMBED, AI.CLASSIFY, and more

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Format code
pyink --config pyproject.toml src/ tests/
isort src/ tests/
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
