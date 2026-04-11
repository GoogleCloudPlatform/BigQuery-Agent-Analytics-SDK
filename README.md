# BigQuery Agent Analytics SDK

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13-blue)](pyproject.toml)
[![CI](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/actions/workflows/ci.yml/badge.svg)](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/actions/workflows/ci.yml)

An open-source Python SDK for analyzing, evaluating, and curating agent traces
stored in BigQuery. Built on top of the
[Agent Development Kit (ADK)](https://github.com/google/adk-python), it provides
a consumption-layer toolkit for agent observability at scale.

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
- Observability dashboards (SQL and BigFrames)

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
- Context Graph — property graph linking traces to business entities with GQL traversal
- YAML-driven ontology extraction and materialization
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

## Quick Start

```python
from bigquery_agent_analytics import Client

client = Client(project_id="my-project", dataset_id="analytics")
trace = client.get_trace("trace-abc-123")
trace.render()
```

See [SDK.md](SDK.md) for the full API walkthrough with code examples for every
feature.

## Documentation

| Resource | Description |
|----------|-------------|
| [SDK Feature Reference](SDK.md) | Complete API walkthrough with working code examples |
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
│   ├── evaluators.py              # CodeEvaluator + LLMAsJudge
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
│   ├── context_graph.py           # Property Graph: BizNode extraction, GQL
│   └── memory_service.py          # Long-horizon agent memory
│
├── Ontology
│   ├── ontology_models.py         # Pydantic models for ontology schema
│   ├── ontology_schema_compiler.py# YAML → compiled schema
│   ├── ontology_graph.py          # Ontology graph construction
│   ├── ontology_materializer.py   # Graph materialization to BigQuery
│   ├── ontology_property_graph.py # Property graph operations
│   └── ontology_orchestrator.py   # End-to-end ontology pipeline
│
└── CLI & Deploy
    ├── cli.py                     # CLI entry point (bq-agent-sdk)
    ├── udf_kernels.py             # Python UDF scoring kernels
    └── udf_sql_templates.py       # UDF SQL generation
```

## Related Projects

- [Google ADK](https://github.com/google/adk-python) — Agent Development Kit
  for building AI agents
- [ADK BigQuery Trace Exporter](https://github.com/google/adk-python/tree/main/contributing/extensions/bigquery_trace_exporter) —
  ADK plugin that writes agent traces to BigQuery
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

## Disclaimer

This is not an officially supported Google product. This SDK is intended to
demonstrate patterns for analyzing agent traces in BigQuery and is provided
as-is.
