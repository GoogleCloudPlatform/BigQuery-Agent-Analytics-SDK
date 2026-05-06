# Implementation Plan: Remote Function & CLI Interface

**Parent PRD:** [Unified Analytics Interface](prd_unified_analytics_interface.md)
**Remote Function Rationale:** [Why Remote Function](remote_function_rationale.md)
**Date:** 2026-03-12
**SDK Version:** 0.2.0

---

## 0. Repo Validation Summary

Validated against commit `1d05770` (main branch, 2026-03-12). All 5 proposed
remote function operations and all 9 CLI commands have backing SDK methods.
Key findings incorporated into this plan:

| Finding | Impact | Resolution |
|---------|--------|------------|
| `Trace`/`Span` dataclasses fail `json.dumps()` | Blocks RF + CLI | Phase 1: Add `.to_dict()` with datetime→ISO 8601 |
| Pydantic `.model_dump()` preserves raw `datetime` | Blocks RF + CLI | Phase 1: Use `.model_dump(mode="json")` everywhere |
| `_run_sync()` already handles async/sync boundary | No new work | Phase 3: Reuse directly in Cloud Function |
| CLI `distribution` maps to SDK `deep_analysis()` | Naming alias | Phase 2: CLI aliases the command name |
| No `deploy/` directory or CLI entry point | Expected | Phase 2 + 3 deliverables |

---

## 1. Phase 1: Serialization & Filter Layer (Week 1)

### 1.1 Uniform Serialization Module

**New file:** `src/bigquery_agent_analytics/serialization.py`

Provides a single `serialize()` function that handles all SDK return types:

```python
"""Uniform JSON serialization for CLI and Remote Function boundaries."""

import dataclasses
from datetime import date, datetime


def serialize(obj):
    """Convert any SDK return type to a json.dumps()-safe dict.

    Handles three cases:
    1. Pydantic BaseModel → .model_dump(mode="json")
    2. dataclass (Trace, Span, etc.) → recursive dict with datetime→isoformat
    3. dict/list/primitive → pass through with datetime conversion
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _dataclass_to_dict(obj)
    if isinstance(obj, list):
        return [serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def _dataclass_to_dict(obj):
    result = {}
    for f in dataclasses.fields(obj):
        val = getattr(obj, f.name)
        result[f.name] = serialize(val)
    return result
```

**Files changed:**
- `src/bigquery_agent_analytics/serialization.py` (new, ~40 lines)
- `src/bigquery_agent_analytics/__init__.py` (export `serialize`)

**Tests:**
- `tests/test_serialization.py` (new, ~120 lines)
- Test `serialize(Trace(...))` produces valid `json.dumps()` output
- Test `serialize(EvaluationReport(...))` converts `created_at` to ISO string
- Test `serialize(InsightsReport(...))` handles nested `SessionMetadata` datetimes
- Test `serialize(DriftReport(...))`, `serialize(QuestionDistribution(...))`
- Test `serialize(dict)` with embedded datetime values
- Test `serialize([Trace(...), Trace(...)])` for list returns
- Test round-trip: `json.loads(json.dumps(serialize(obj)))` for every return type

**Exit criterion:** `json.dumps(serialize(result))` succeeds for every `Client`
public method return type.

### 1.2 TraceFilter CLI Factory

**File changed:** `src/bigquery_agent_analytics/trace.py`

Add class method on `TraceFilter`:

```python
@classmethod
def from_cli_args(
    cls,
    last: str | None = None,        # "1h", "24h", "7d", "30d"
    agent_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    has_error: bool | None = None,
    limit: int = 100,
) -> "TraceFilter":
    """Build TraceFilter from CLI-style arguments.

    Parses --last time windows (e.g. '1h' → start_time = now - 1 hour).
    Also used by Remote Function dispatch to parse params JSON.
    """
```

Time window parser supports: `Xm` (minutes), `Xh` (hours), `Xd` (days).

**Tests:**
- `tests/test_trace_filter_factory.py` (new, ~80 lines)
- Test `from_cli_args(last="1h")` sets `start_time` ~1 hour ago
- Test `from_cli_args(agent_id="bot")` sets `agent_id`
- Test `from_cli_args(last="7d", agent_id="bot", limit=50)` combines all
- Test invalid `last` value raises `ValueError`
- Test `from_cli_args()` with no args returns default filter

### 1.3 Format Output Layer

**New file:** `src/bigquery_agent_analytics/formatter.py`

```python
"""Output formatting for CLI and Remote Function responses."""

import json

from .serialization import serialize


def format_output(obj, fmt: str = "json") -> str:
    """Format an SDK result for output.

    Args:
        obj: Any SDK return type (Trace, EvaluationReport, dict, etc.)
        fmt: "json", "text", or "table"
    """
```

- `json`: `json.dumps(serialize(obj), indent=2)`
- `text`: Calls `.summary()` if available, else pretty-prints key fields
- `table`: Simple columnar format for list-like results (traces, scores)

**Files changed:**
- `src/bigquery_agent_analytics/formatter.py` (new, ~80 lines)
- `src/bigquery_agent_analytics/__init__.py` (export `format_output`)

**Tests:**
- `tests/test_formatter.py` (new, ~60 lines)

---

## 2. Phase 2: CLI MVP (Weeks 2–3)

### 2.1 Dependencies & Entry Point

**File changed:** `pyproject.toml`

```toml
[project.optional-dependencies]
cli = [
  "typer>=0.9.0",
]

[project.scripts]
bq-agent-sdk = "bigquery_agent_analytics.cli:app"
```

### 2.2 CLI Module

**New file:** `src/bigquery_agent_analytics/cli.py`

| Command | SDK Method | Return Type | Serialization |
|---------|-----------|-------------|---------------|
| `doctor` | `Client.doctor()` | `dict` | `serialize(dict)` |
| `get-trace` | `Client.get_session_trace()` | `Trace` | `serialize(trace)` via `.to_dict()` |
| `list-traces` | `Client.list_traces()` | `list[Trace]` | `serialize(traces)` |
| `evaluate` | `Client.evaluate()` | `EvaluationReport` | `.model_dump(mode="json")` |
| `insights` | `Client.insights()` | `InsightsReport` | `.model_dump(mode="json")` |
| `drift` | `Client.drift_detection()` | `DriftReport` | `.model_dump(mode="json")` |
| `distribution` | `Client.deep_analysis()` | `QuestionDistribution` | `.model_dump(mode="json")` |
| `hitl-metrics` | `Client.hitl_metrics()` | `dict` | `serialize(dict)` |
| `views create-all` | `ViewManager.create_all_views()` | `dict[str, str]` | direct |

**v1.0 MVP commands:** `doctor`, `get-trace`, `evaluate`
**v1.1 commands:** `insights`, `drift`, `distribution`, `hitl-metrics`,
`list-traces`, `views`

### 2.3 Global Options

All commands share:
```
--project-id TEXT       [env: BQ_AGENT_PROJECT]
--dataset-id TEXT       [env: BQ_AGENT_DATASET]
--table-id TEXT         [default: agent_events]
--location TEXT         [default: us-central1]
--format TEXT           json|text|table [default: json]
--quiet                 Suppress non-essential output
```

### 2.4 Evaluate Command Detail

```
bq-agent-sdk evaluate [OPTIONS]
  --evaluator TEXT      latency|error_rate|turn_count|token_efficiency|
                        context_cache_hit_rate|ttft|cost|llm-judge
  --threshold FLOAT
  --criterion TEXT      correctness|hallucination|sentiment|custom
  --custom-prompt TEXT
  --agent-id TEXT
  --last TEXT           1h|24h|7d|30d
  --limit INT           [default: 100]
  --exit-code           Return 1 on evaluation failure
  --fail-on-missing-cache-telemetry
```

Dispatch logic:
```python
# Map CLI --evaluator to SDK factory
EVALUATOR_FACTORIES = {
    "latency": lambda t: CodeEvaluator.latency(threshold_ms=t),
    "error_rate": lambda t: CodeEvaluator.error_rate(max_error_rate=t),
    "turn_count": lambda t: CodeEvaluator.turn_count(max_turns=int(t)),
    "token_efficiency": lambda t: CodeEvaluator.token_efficiency(max_tokens=int(t)),
    "ttft": lambda t: CodeEvaluator.ttft(threshold_ms=t),
    "cost": lambda t: CodeEvaluator.cost_per_session(max_cost_usd=t),
    "llm-judge": None,  # special handling
}
# context_cache_hit_rate is special-cased so callers can pass
# fail_on_missing_telemetry in addition to threshold/min_hit_rate.
```

### 2.5 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success / evaluation passed |
| 1 | Evaluation failed (pass_rate below threshold) |
| 2 | Infrastructure error (BQ connection, missing table, etc.) |

### 2.6 Tests

**New file:** `tests/test_cli.py` (~300 lines)
- Mock `Client` construction and all SDK methods
- Test each v1.0 command produces valid JSON output
- Test `--exit-code` returns 1 on failure, 0 on pass
- Test `--format=text` calls `.summary()`
- Test `--last=1h` correctly parsed
- Test env var fallback for `--project-id`
- Test error handling (missing required args, BQ connection failure)

### 2.7 Files Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| `src/bigquery_agent_analytics/cli.py` | new | ~350 |
| `tests/test_cli.py` | new | ~300 |
| `pyproject.toml` | edit | +8 |

---

## 3. Phase 3: Remote Function (Weeks 4–5)

### 3.1 Directory Structure

```
deploy/remote_function/
├── main.py             # functions-framework entry point
├── requirements.txt    # SDK + functions-framework
├── deploy.sh           # gcloud deployment script
├── register.sql        # CREATE FUNCTION DDL template
└── README.md           # Deployment guide
```

### 3.2 Entry Point (`main.py`)

```python
"""BigQuery Remote Function entry point.

Dispatches BigQuery Remote Function calls to SDK methods.
BigQuery sends batched requests as JSON with a `calls` array;
each element is [operation, params_json]. We return a `replies`
array of the same length.
"""

import json
import os

import functions_framework
from flask import jsonify

from bigquery_agent_analytics import Client, serialize
from bigquery_agent_analytics import CodeEvaluator, LLMAsJudge
from bigquery_agent_analytics import TraceFilter


@functions_framework.http
def handle_request(request):
    """HTTP entry point for BigQuery Remote Function."""
    body = request.get_json(silent=True)
    if not body or "calls" not in body:
        return jsonify({"errorMessage": "Missing 'calls' array"}), 400

    # Config from user_defined_context or env vars
    udc = body.get("userDefinedContext", {})
    project_id = udc.get("project_id", os.environ.get("BQ_AGENT_PROJECT"))
    dataset_id = udc.get("dataset_id", os.environ.get("BQ_AGENT_DATASET"))

    if not project_id or not dataset_id:
        return jsonify({
            "errorMessage": "project_id and dataset_id required"
        }), 400

    client = Client(project_id=project_id, dataset_id=dataset_id)

    replies = []
    for call in body["calls"]:
        try:
            operation, params_json = call[0], call[1]
            params = json.loads(params_json) if isinstance(
                params_json, str
            ) else params_json
            result = _dispatch(client, operation, params)
            result["_version"] = "1.0"
            replies.append(result)
        except Exception as e:
            replies.append({
                "_error": {
                    "code": type(e).__name__,
                    "message": str(e),
                },
                "_version": "1.0",
            })

    return jsonify({"replies": replies})


def _dispatch(client, operation, params):
    """Route operation to SDK method, return JSON-safe dict."""
    if operation == "analyze":
        trace = client.get_session_trace(params["session_id"])
        return serialize(trace)

    elif operation == "evaluate":
        evaluator = _build_evaluator(params)
        filters = TraceFilter.from_cli_args(
            session_id=params.get("session_id"),
            agent_id=params.get("agent_filter"),
            last=params.get("last"),
        )
        report = client.evaluate(evaluator=evaluator, filters=filters)
        return serialize(report)

    elif operation == "judge":
        judge = _build_judge(params)
        filters = TraceFilter.from_cli_args(
            session_id=params.get("session_id"),
            agent_id=params.get("agent_filter"),
            last=params.get("last"),
        )
        report = client.evaluate(evaluator=judge, filters=filters)
        return serialize(report)

    elif operation == "insights":
        filters = TraceFilter.from_cli_args(
            session_id=params.get("session_id"),
            agent_id=params.get("agent_filter"),
            last=params.get("last"),
        )
        report = client.insights(filters=filters)
        return serialize(report)

    elif operation == "drift":
        filters = TraceFilter.from_cli_args(
            agent_id=params.get("agent_filter"),
            last=params.get("last"),
        )
        report = client.drift_detection(
            golden_dataset=params["golden_dataset"],
            filters=filters,
        )
        return serialize(report)

    else:
        raise ValueError(f"Unknown operation: {operation}")


def _bool_param(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _build_evaluator(params):
    """Build CodeEvaluator from params dict."""
    metric = params.get("metric", "latency")
    threshold = params.get("threshold")
    fail_on_missing_telemetry = _bool_param(
        params.get("fail_on_missing_telemetry", False)
    )
    factories = {
        "latency": lambda t: CodeEvaluator.latency(threshold_ms=t),
        "error_rate": lambda t: CodeEvaluator.error_rate(max_error_rate=t),
        "turn_count": lambda t: CodeEvaluator.turn_count(max_turns=int(t)),
        "token_efficiency": lambda t: CodeEvaluator.token_efficiency(
            max_tokens=int(t)
        ),
        "ttft": lambda t: CodeEvaluator.ttft(threshold_ms=t),
        "cost": lambda t: CodeEvaluator.cost_per_session(max_cost_usd=t),
    }
    factories_default = {
        "latency": CodeEvaluator.latency,
        "error_rate": CodeEvaluator.error_rate,
        "turn_count": CodeEvaluator.turn_count,
        "token_efficiency": CodeEvaluator.token_efficiency,
        "ttft": CodeEvaluator.ttft,
        "cost": CodeEvaluator.cost_per_session,
    }
    if metric == "context_cache_hit_rate":
        kwargs = {"fail_on_missing_telemetry": fail_on_missing_telemetry}
        if threshold is not None:
            kwargs["min_hit_rate"] = threshold
        return CodeEvaluator.context_cache_hit_rate(**kwargs)
    if metric not in factories:
        raise ValueError(f"Unknown metric: {metric}")
    if threshold is not None:
        return factories[metric](threshold)
    return factories_default[metric]()


def _build_judge(params):
    """Build LLMAsJudge from params dict."""
    criterion = params.get("criterion", "correctness")
    threshold = params.get("threshold", 0.5)
    factories = {
        "correctness": lambda t: LLMAsJudge.correctness(threshold=t),
        "hallucination": lambda t: LLMAsJudge.hallucination(threshold=t),
        "sentiment": lambda t: LLMAsJudge.sentiment(threshold=t),
    }
    factory = factories.get(criterion)
    if not factory:
        raise ValueError(f"Unknown criterion: {criterion}")
    return factory(threshold)
```

**Key design decisions:**
- `_dispatch()` calls sync SDK methods directly — `insights()`,
  `drift_detection()`, and `deep_analysis()` internally use `_run_sync()`
  (`client.py:247`) which already handles the async/sync boundary safely
- `serialize()` from Phase 1 handles all return type → JSON conversion
- Each failed call produces a per-row `_error` (partial failure semantics)
- Config from `userDefinedContext` with env var fallback

### 3.3 Requirements

```
# deploy/remote_function/requirements.txt
functions-framework==3.*
bigquery-agent-analytics[llm]>=0.2.0
```

### 3.4 Deployment Script

```bash
# deploy/remote_function/deploy.sh
#!/usr/bin/env bash
set -euo pipefail

PROJECT="${1:?Usage: deploy.sh PROJECT [REGION] [DATASET]}"
REGION="${2:-us-central1}"
DATASET="${3:-agent_analytics}"

echo "==> Deploying Cloud Function..."
gcloud functions deploy bq-agent-analytics \
  --gen2 --runtime python312 --region "$REGION" \
  --entry-point handle_request \
  --source "$(dirname "$0")" \
  --trigger-http --no-allow-unauthenticated \
  --set-env-vars "BQ_AGENT_PROJECT=$PROJECT,BQ_AGENT_DATASET=$DATASET" \
  --memory 512MB --timeout 120s --min-instances 0

echo "==> Creating CLOUD_RESOURCE connection..."
bq mk --connection --location=US --connection_type=CLOUD_RESOURCE \
  --project_id="$PROJECT" analytics-conn 2>/dev/null || true

echo "==> Granting invoker role to connection SA..."
CONNECTION_SA=$(bq show --connection --format=json \
  "$PROJECT.us.analytics-conn" | jq -r '.cloudResource.serviceAccountId')
gcloud functions add-invoker-policy-binding bq-agent-analytics \
  --region="$REGION" --member="serviceAccount:${CONNECTION_SA}"

echo "==> Done. Register the function with:"
echo "    bq query --use_legacy_sql=false < register.sql"
```

### 3.5 Registration DDL

```sql
-- deploy/remote_function/register.sql
CREATE OR REPLACE FUNCTION `PROJECT.DATASET.agent_analytics`(
  operation STRING, params JSON
) RETURNS JSON
REMOTE WITH CONNECTION `PROJECT.us.analytics-conn`
OPTIONS (
  endpoint = "https://REGION-PROJECT.cloudfunctions.net/bq-agent-analytics",
  max_batching_rows = 50
);
```

### 3.6 Tests

**New file:** `tests/test_remote_function.py` (~250 lines)

- Mock `Client` and test `handle_request()` with sample batched payloads
- Test each operation returns `_version: "1.0"`
- Test partial failure: 1 bad session_id in batch of 3 → 2 successes + 1 error
- Test missing `calls` → 400
- Test missing `project_id`/`dataset_id` → 400
- Test unknown operation → per-row error
- Test all returns are `json.dumps()`-safe (no datetime objects)
- Test `userDefinedContext` config parsing
- Test env var fallback

### 3.7 Files Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| `deploy/remote_function/main.py` | new | ~160 |
| `deploy/remote_function/requirements.txt` | new | ~3 |
| `deploy/remote_function/deploy.sh` | new | ~30 |
| `deploy/remote_function/register.sql` | new | ~10 |
| `deploy/remote_function/README.md` | new | ~100 |
| `tests/test_remote_function.py` | new | ~250 |

---

## 4. Phase 4: CLI v1.1 + Continuous Query Templates (Weeks 6–7)

### 4.1 Remaining CLI Commands

| Command | SDK Method | Notes |
|---------|-----------|-------|
| `insights` | `Client.insights()` | `--max-sessions`, `--agent-id`, `--last` |
| `drift` | `Client.drift_detection()` | `--golden-dataset` required |
| `distribution` | `Client.deep_analysis()` | **Aliases** SDK method name |
| `hitl-metrics` | `Client.hitl_metrics()` | |
| `list-traces` | `Client.list_traces()` | Filter options mirror `TraceFilter` |
| `views create-all` | `ViewManager.create_all_views()` | `--prefix` option |
| `views create` | `ViewManager.create_view()` | Takes event type arg |

### 4.2 Continuous Query Templates

```
deploy/continuous_queries/
├── realtime_error_analysis.sql
├── session_scoring.sql
├── pubsub_alerting.sql
├── bigtable_dashboard.sql
└── setup_reservation.md
```

These are parameterized SQL files (not SDK code). Users substitute
`PROJECT`, `DATASET`, `CONNECTION` placeholders and run via `bq query
--continuous=true`.

---

## 5. Phase 5: Documentation & Pilot (Week 8)

### 5.1 Documentation Updates

| File | Changes |
|------|---------|
| `SDK.md` | Add CLI, Remote Function, and Continuous Query sections |
| `README.md` | Add quick-start for CLI and Remote Function |

### 5.2 Examples

| File | Description |
|------|-------------|
| `examples/cli_agent_tool.py` | ADK agent using CLI for self-diagnostics |
| `examples/ci_eval_pipeline.sh` | GitHub Actions evaluation script |
| `examples/remote_function_dashboard.sql` | Looker query examples |
| `examples/continuous_query_alerting.sql` | Real-time error alerting |

---

## 6. Dependency Graph

```
Phase 1 (serialization + filter factory)
  │
  ├──→ Phase 2 (CLI MVP) ──→ Phase 4 (CLI v1.1)
  │                                │
  └──→ Phase 3 (Remote Function) ──┤
                                   │
                                   └──→ Phase 5 (docs + pilot)
```

Phase 2 and Phase 3 can run **in parallel** after Phase 1 completes.

---

## 7. Operation → SDK Method Reference

Complete mapping from interface operations to current SDK code:

| Operation | SDK Method | File:Line | Return Type | Serialization Strategy |
|-----------|-----------|-----------|-------------|----------------------|
| `analyze` | `Client.get_session_trace()` | `client.py` | `Trace` (dataclass) | `serialize()` → recursive `.to_dict()` |
| `evaluate` | `Client.evaluate(CodeEvaluator)` | `client.py` | `EvaluationReport` (Pydantic) | `.model_dump(mode="json")` |
| `judge` | `Client.evaluate(LLMAsJudge)` | `client.py` | `EvaluationReport` (Pydantic) | `.model_dump(mode="json")` |
| `insights` | `Client.insights()` | `client.py` | `InsightsReport` (Pydantic) | `.model_dump(mode="json")` |
| `drift` | `Client.drift_detection()` | `client.py` | `DriftReport` (Pydantic) | `.model_dump(mode="json")` |
| `distribution` | `Client.deep_analysis()` | `client.py` | `QuestionDistribution` (Pydantic) | `.model_dump(mode="json")` |
| `doctor` | `Client.doctor()` | `client.py` | `dict` | `serialize(dict)` |
| `hitl-metrics` | `Client.hitl_metrics()` | `client.py` | `dict` | `serialize(dict)` |
| `views` | `ViewManager.create_all_views()` | `views.py` | `dict[str, str]` | direct (str values) |

### Evaluator Factory Methods (already exist)

| CLI `--evaluator` | SDK Factory | File |
|-------------------|------------|------|
| `latency` | `CodeEvaluator.latency(threshold_ms)` | `evaluators.py` |
| `error_rate` | `CodeEvaluator.error_rate(max_error_rate)` | `evaluators.py` |
| `turn_count` | `CodeEvaluator.turn_count(max_turns)` | `evaluators.py` |
| `token_efficiency` | `CodeEvaluator.token_efficiency(max_tokens)` | `evaluators.py` |
| `context_cache_hit_rate` | `CodeEvaluator.context_cache_hit_rate(min_hit_rate)` | `evaluators.py` |
| `ttft` | `CodeEvaluator.ttft(threshold_ms)` | `evaluators.py` |
| `cost` | `CodeEvaluator.cost_per_session(max_cost_usd)` | `evaluators.py` |
| `llm-judge` | `LLMAsJudge.correctness/hallucination/sentiment(threshold)` | `evaluators.py` |

### SDK Capabilities NOT Exposed (v1.2+ candidates)

| SDK Feature | Class | Potential Operation |
|-------------|-------|-------------------|
| Context Graph | `ContextGraphManager` | `context_graph` |
| Trajectory Evaluation | `BigQueryTraceEvaluator` | `trajectory` |
| Multi-Trial | `TrialRunner` | `multi_trial` |
| Grader Pipeline | `GraderPipeline` | `grade` |
| Memory Service | `BigQueryMemoryService` | (separate interface) |
| Anomaly Detection & Forecasting | `AnomalyDetector` | `anomaly`, `forecast` |

---

## 8. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Cloud Function cold start > 3s | Medium | Latency SLO breach | `--min-instances=1` for production |
| `LLMAsJudge` timeout in batch | Medium | Partial failure | Per-row error handling; `max_batching_rows=10` for judge |
| `typer` version conflict with user deps | Low | CLI install failure | Optional `[cli]` extra isolates dependency |
| `Trace.to_dict()` missing edge cases | Medium | Serialization crash | Comprehensive test matrix in Phase 1 |
| `datetime` serialization regression | Medium | Silent JSON errors | CI test: `json.dumps(serialize(x))` for all types |
