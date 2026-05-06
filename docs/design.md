# BigQuery Agent Analytics SDK — Design Document

**Version:** 0.3.0
**Status:** Living document
**Last Updated:** 2026-03-31
**License:** Apache-2.0

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [System Context & Data Flow](#2-system-context--data-flow)
3. [Architecture Overview](#3-architecture-overview)
4. [Module Design](#4-module-design)
5. [Data Model](#5-data-model)
6. [Query Architecture](#6-query-architecture)
7. [Evaluation Framework](#7-evaluation-framework)
8. [LLM Execution Strategy](#8-llm-execution-strategy)
9. [Async/Sync Boundary Design](#9-asyncsync-boundary-design)
10. [Extensibility & Plugin Points](#10-extensibility--plugin-points)
11. [Error Handling Philosophy](#11-error-handling-philosophy)
12. [Testing Strategy](#12-testing-strategy)
13. [Security Considerations](#13-security-considerations)
14. [Future Directions](#14-future-directions)

---

## 1. Introduction

### 1.1 Purpose

The BigQuery Agent Analytics SDK is the **consumption layer** for agent observability at scale. It reads agent execution traces stored in BigQuery and provides a Python toolkit for trace reconstruction, deterministic and semantic evaluation, drift detection, insights generation, anomaly detection, and long-horizon agent memory.

### 1.2 Problem Statement

AI agents built on the [Agent Development Kit (ADK)](https://github.com/google/adk-python) generate rich telemetry — LLM calls, tool invocations, state changes, errors, and latency data — that is logged to BigQuery via the [BigQuery Agent Analytics Plugin](https://github.com/google/adk-python/blob/main/src/google/adk/plugins/bigquery_agent_analytics_plugin.py). However, raw event rows in BigQuery are insufficient for:

- **Debugging**: Reconstructing the causal chain of events within a session
- **Evaluation**: Systematically assessing agent quality against deterministic and semantic criteria
- **Monitoring**: Detecting production drift, anomalies, and regression
- **Curation**: Building and managing evaluation datasets with lifecycle governance

This SDK bridges the gap between raw telemetry and actionable agent analytics.

### 1.3 Design Principles

| Principle | Manifestation |
|-----------|---------------|
| **BigQuery-native** | Pushes computation to BigQuery via SQL wherever possible; minimizes data transfer |
| **Graceful degradation** | Optional dependencies (`google-genai`, `bigframes`) degrade features, never crash |
| **Async-first, sync-friendly** | Core pipeline is async for concurrency; `Client` exposes sync methods for simplicity |
| **Zero infrastructure** | No additional services needed — BigQuery is the only backend |
| **Injection over inheritance** | BigQuery client is injectable for testing; no subclass-based extension |
| **Builder over config** | Fluent method chaining over complex configuration objects |

### 1.4 Relationship to ADK

```
┌──────────────────────────────────────────────────────────┐
│                   Agent Runtime (ADK)                     │
│                                                          │
│  LlmAgent ─── Runner ─── SessionService                 │
│                  │                                       │
│         BigQueryAgentAnalyticsPlugin                     │
│           (production layer)                             │
│                  │                                       │
│    BQ Storage Write API ─── PyArrow serialization        │
└─────────────────┬────────────────────────────────────────┘
                  │  Writes events
                  ▼
┌──────────────────────────────────────────────────────────┐
│              BigQuery (agent_events)                      │
│                                                          │
│  Partitioned by DATE(timestamp)                          │
│  Clustered by event_type, agent, user_id                 │
└─────────────────┬────────────────────────────────────────┘
                  │  Reads events
                  ▼
┌──────────────────────────────────────────────────────────┐
│        BigQuery Agent Analytics SDK                      │
│              (consumption layer)                         │
│                                                          │
│  Client ─── Trace ─── Evaluators ─── Insights            │
│  Memory ─── AI/ML ─── Feedback ─── EvalSuite             │
└──────────────────────────────────────────────────────────┘
```

The SDK does **not** write agent events to BigQuery. That responsibility belongs entirely to the ADK plugin. The SDK reads event data, and optionally writes derived artifacts (evaluation results, embeddings indexes) to separate tables.

---

## 2. System Context & Data Flow

### 2.1 Producer: ADK Plugin

The `BigQueryAgentAnalyticsPlugin` (part of `google-adk`, not this SDK) captures agent lifecycle events and writes them to BigQuery asynchronously:

- **Transport**: BigQuery Storage Write API with PyArrow serialization for high-throughput, low-latency writes
- **Batching**: Configurable `batch_size` and `batch_flush_interval` with async queue (`queue_max_size` up to 10,000)
- **Tracing**: OpenTelemetry integration populates `trace_id`, `span_id`, `parent_span_id` when a `TracerProvider` is configured; falls back to internal UUIDs otherwise
- **Content handling**: `HybridContentParser` + `GCSOffloader` offload large payloads (>500KB default) to GCS, storing `ObjectRef` in `content_parts`
- **Safety**: `_safe_callback` decorator wraps every callback to swallow plugin errors, preventing observability failures from affecting agent execution
- **Filtering**: `event_allowlist` / `event_denylist` for selective logging; `content_formatter` for redaction

**Event types logged:**

| Category | Events |
|----------|--------|
| User interaction | `USER_MESSAGE_RECEIVED` |
| Agent lifecycle | `AGENT_STARTING`, `AGENT_COMPLETED`, `INVOCATION_STARTING`, `INVOCATION_COMPLETED` |
| LLM operations | `LLM_REQUEST`, `LLM_RESPONSE`, `LLM_ERROR` |
| Tool operations | `TOOL_STARTING`, `TOOL_COMPLETED`, `TOOL_ERROR` |
| State management | `STATE_DELTA` |

### 2.2 Consumer: This SDK

The SDK reads events through the standard BigQuery client library (`google-cloud-bigquery`) using parameterized SQL queries. It performs the following operations:

```
                    BigQuery
                       │
           ┌───────────┼───────────────────────┐
           │           │                       │
     Standard SQL   AI.GENERATE          ML.DETECT_ANOMALIES
     (trace, eval)  (LLM judge,          (ARIMA, Autoencoder)
                     facets, insights)
           │           │                       │
           ▼           ▼                       ▼
      ┌─────────────────────────────────────────────┐
      │          SDK Python Processing               │
      │                                             │
      │  Trace reconstruction ─── DAG tree building │
      │  Score computation ─── Report aggregation   │
      │  Trajectory matching ─── Drift analysis     │
      │  Context selection ─── Memory ranking       │
      └─────────────────────────────────────────────┘
```

### 2.3 End-to-End Lifecycle

As demonstrated in the [e2e demo](../examples/e2e_demo.py):

**Phase 1 — Trace Generation:**
1. ADK `LlmAgent` with tools is created and wired to a `Runner`
2. `BigQueryAgentAnalyticsPlugin` is attached as a plugin
3. User messages are sent through `runner.run_async()`
4. Plugin captures every event callback and writes to BigQuery
5. `plugin.flush()` ensures all buffered events are written

**Phase 2 — Evaluation:**
1. `Client.get_trace()` retrieves all events for a session
2. `CodeEvaluator` preset factories assess latency, turn count, error rate, token efficiency
3. `LLMAsJudge.correctness()` performs semantic evaluation via BigQuery `AI.GENERATE`
4. `BigQueryTraceEvaluator.evaluate_session()` performs trajectory matching against golden tool sequences

**Phase 3 — Insights:**
1. `Client.insights()` triggers the multi-stage pipeline
2. Session metadata is aggregated from BigQuery
3. Per-session facets are extracted via `AI.GENERATE` with structured output
4. Seven analysis prompts generate specialized reports
5. Executive summary synthesizes all findings

---

## 3. Architecture Overview

### 3.1 Module Dependency Graph

```
                         ┌─────────┐
                         │ Client  │  (entry point)
                         └────┬────┘
                ┌─────────┬───┴───┬──────────┐
                ▼         ▼       ▼          ▼
           ┌────────┐ ┌──────┐ ┌────────┐ ┌──────────┐
           │ trace  │ │eval- │ │feedback│ │ insights │
           │        │ │uators│ │        │ │          │
           └────────┘ └──┬───┘ └────────┘ └────┬─────┘
                         │                      │
                    ┌────┘                      │
                    ▼                           ▼
            ┌──────────────┐            ┌────────────┐
            │grader_pipeline│           │ evaluators │
            └──────────────┘            └────────────┘

   ┌────────────────┐    ┌────────────┐    ┌──────────────┐
   │trace_evaluator │    │ eval_suite │    │eval_validator│
   └───────┬────────┘    └──────┬─────┘    └──────┬───────┘
           │                    │                  │
           ▼                    │                  ▼
   ┌──────────────┐             │           ┌──────────┐
   │ multi_trial  │             └──────────►│eval_suite│
   └──────────────┘

   ┌──────────────────┐  ┌───────────────────┐  ┌─────────────────────┐
   │ memory_service   │  │ ai_ml_integration │  │bigframes_evaluator  │
   │ (requires ADK)   │  │                   │  │(requires bigframes) │
   └──────────────────┘  └───────────────────┘  └─────────────────────┘

   ┌──────────────────┐  ┌───────────────────┐  ┌──────────────────────┐
   │event_semantics   │  │     views         │  │   context_graph      │
   │(canonical helpers│  │(per-event BQ views│  │(Property Graph, GQL, │
   └──────────────────┘  └───────────────────┘  │ world-change detect) │
                                                └──────────────────────┘

   ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
   │ categorical_evaluator│  │ ontology_* (6 modules)│  │      cli         │
   │ categorical_views    │  │ (YAML → AI.GENERATE → │  │ (Typer commands) │
   │ (label evaluation)   │  │  tables → PG → GQL)   │  │                  │
   └──────────────────────┘  └──────────────────────┘  └──────────────────┘

   ┌──────────────────┐  ┌───────────────────┐
   │ udf_kernels      │  │ serialization     │
   │ udf_sql_templates│  │ formatter         │
   └──────────────────┘  └───────────────────┘
```

### 3.2 Module Categorization

| Layer | Modules | Responsibility |
|-------|---------|----------------|
| **Entry Point** | `client.py` | High-level sync API, BigQuery query orchestration |
| **Core Data** | `trace.py` | Trace/Span reconstruction, DAG rendering, filtering |
| **Evaluation Engine** | `evaluators.py`, `trace_evaluator.py`, `multi_trial.py`, `grader_pipeline.py` | Deterministic metrics, LLM-as-judge, trajectory matching, multi-trial statistics, grader composition |
| **Categorical Evaluation** | `categorical_evaluator.py`, `categorical_views.py` | User-defined categorical classification with AI.GENERATE + Gemini fallback, dashboard views with dedup |
| **Eval Governance** | `eval_suite.py`, `eval_validator.py` | Task lifecycle management, static quality validation |
| **Feedback & Insights** | `feedback.py`, `insights.py` | Drift detection, question distribution, multi-stage analysis pipeline |
| **AI/ML** | `ai_ml_integration.py`, `bigframes_evaluator.py` | BigQuery AI.GENERATE, embeddings, anomaly detection, DataFrame API |
| **Memory** | `memory_service.py` | Cross-session context retrieval, semantic search, user profiling |
| **Context Graph (V2/V3)** | `context_graph.py` | Property Graph linking traces to business entities, BizNode extraction via AI.GENERATE, GQL traversal, world-change detection, decision semantics |
| **Ontology Graph (V4)** | `ontology_models.py`, `ontology_schema_compiler.py`, `ontology_graph.py`, `ontology_materializer.py`, `ontology_property_graph.py`, `ontology_orchestrator.py` | Configuration-driven graph pipeline: YAML spec loading, AI extraction, physical table routing, Property Graph DDL transpilation, GQL showcase |
| **CLI** | `cli.py` | Command-line interface: traces, evaluation, categorical evaluation, insights, drift, views, ontology pipeline |
| **Interfaces** | `serialization.py`, `formatter.py` | JSON serialization for CLI/Remote Function boundaries, output formatting (JSON/text/table) |
| **UDF Kernels** | `udf_kernels.py`, `udf_sql_templates.py` | Pure analytical kernels for BigQuery Python UDFs, SQL templates for UDF registration |
| **Utilities** | `event_semantics.py`, `views.py` | Canonical event type predicates, per-event-type BigQuery view management |
| **Package** | `__init__.py` | Graceful optional import aggregation |

### 3.3 Key Design Decisions

**Decision 1: BigQuery as the sole backend.**
All data lives in BigQuery. The SDK does not introduce Redis, PostgreSQL, or any other storage. This keeps the operational footprint minimal — if you have BigQuery, you have everything you need.

**Decision 2: SQL-first computation.**
Aggregations, filtering, joins, and even LLM evaluation (via `AI.GENERATE`) are pushed down to BigQuery. Python-side processing is reserved for tasks that cannot be expressed in SQL: tree reconstruction, trajectory matching algorithms, report formatting, and metric composition.

**Decision 3: Three-tier LLM execution.**
LLM-based evaluation can run via (1) BigQuery `AI.GENERATE`, (2) legacy BigQuery ML `ML.GENERATE_TEXT`, or (3) the Gemini API directly. This maximizes compatibility across different GCP configurations.

**Decision 4: Composition over inheritance.**
The `GraderPipeline` composes `CodeEvaluator`, `LLMAsJudge`, and custom functions via a builder pattern rather than requiring them to share a common base class. The `BigQueryMemoryService` composes four internal services rather than extending a single monolithic class.

---

## 4. Module Design

### 4.1 `client.py` — SDK Entry Point

The `Client` class is the primary interface for users who want a batteries-included experience. It encapsulates BigQuery connection management and provides synchronous methods that internally orchestrate async evaluation pipelines.

**Constructor parameters:**

```python
Client(
    project_id: str,              # GCP project
    dataset_id: str,              # BigQuery dataset
    table_id: str = "agent_events",
    location: str | None = None,  # BQ location; None lets the client auto-detect
    gcs_bucket_name: str | None,  # For GCS-offloaded payload access
    verify_schema: bool = True,   # Schema validation on init
    endpoint: str | None,         # AI.GENERATE endpoint
    connection_id: str | None,    # BQ connection for AI functions
    bq_client = None,             # Injectable for testing
)
```

**Key methods:**

| Method | Returns | SQL Template | Description |
|--------|---------|-------------|-------------|
| `get_trace(session_id)` | `Trace` | `_SESSION_EVENTS_QUERY` | Fetches all events for a session, constructs Span tree |
| `list_traces(filter_criteria)` | `list[Trace]` | `_LIST_SESSIONS_QUERY` + per-session fetch | Discovers sessions matching `TraceFilter`, fetches each |
| `evaluate(evaluator, filters)` | `EvaluationReport` | `SESSION_SUMMARY_QUERY` or `AI_GENERATE_JUDGE_BATCH_QUERY` | Runs code or LLM evaluation over matching sessions |
| `drift_detection(golden_dataset, filters)` | `DriftReport` | Production + golden question queries | Compares golden vs. production question coverage |
| `insights(filters, config)` | `InsightsReport` | Multi-stage pipeline (5 SQL templates) | Generates comprehensive analysis report |
| `deep_analysis(filters, configuration)` | `QuestionDistribution` | Frequency or semantic grouping queries | Question distribution analysis |

**Sync-to-async bridge:**

```python
def evaluate(self, evaluator, filters=None):
    # ... build SQL ...
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(self._run_evaluation(...))
    finally:
        loop.close()
```

This pattern allows the `Client` to be used in Jupyter notebooks, synchronous scripts, and async applications alike.

**Legacy model detection:**

```python
def _is_legacy_model_ref(self, ref: str) -> bool:
    return ref.count(".") >= 2  # e.g., "project.dataset.model_name"
```

This heuristic determines whether to use `AI.GENERATE` (modern endpoint name like `"gemini-2.5-flash"`) or `ML.GENERATE_TEXT` (fully-qualified BQML model reference like `"project.dataset.gemini_model"`).

### 4.2 `trace.py` — Trace Reconstruction

**Core data structures:**

```python
@dataclass
class Span:
    event_type: str
    agent: str | None
    timestamp: datetime
    content: Any               # Parsed JSON payload
    span_id: str | None
    parent_span_id: str | None
    invocation_id: str | None
    latency_ms: float | None
    status: str | None
    error_message: str | None
    attributes: dict | None
    children: list[Span]       # Populated by tree builder
```

**`Span.from_bigquery_row(row)` factory:**

Handles polymorphic content extraction:
1. Attempts `JSON.loads(row.get("content", "{}"))` for JSON columns
2. Falls back to string content
3. Parses `latency_ms` from JSON (`{"total_ms": ...}`) or direct numeric
4. Parses `attributes` from JSON string or dict

**`Trace` class:**

```python
@dataclass
class Trace:
    session_id: str
    spans: list[Span]
    _roots: list[Span]         # Top-level spans (no parent)
```

**DAG reconstruction algorithm** (`_build_tree`):

Two-pass O(n) algorithm:
1. **Index pass**: Build `{span_id: span}` lookup dictionary
2. **Link pass**: For each span, if `parent_span_id` exists in index, append to parent's `children`; otherwise add to `_roots` list

This produces a forest of trees rooted at top-level events (typically `USER_MESSAGE_RECEIVED` or `INVOCATION_STARTING`).

**`render()` output:**

Recursive DFS tree rendering with Unicode box-drawing characters:

```
Session: sess-001 (12 events, 3420ms)
├── USER_MESSAGE_RECEIVED: "What is the weather?"
│   └── AGENT_STARTING: weather_agent
│       ├── LLM_REQUEST → LLM_RESPONSE (320ms)
│       ├── TOOL_STARTING: get_weather(city="NYC")
│       │   └── TOOL_COMPLETED: {"temp": 72} (1200ms)
│       └── AGENT_COMPLETED: "The weather is 72F."
```

**`TraceFilter` dataclass:**

Converts filter criteria to parameterized SQL:

```python
@dataclass
class TraceFilter:
    start_time: datetime | None
    end_time: datetime | None
    agent_id: str | None
    user_id: str | None
    session_ids: list[str] | None
    has_error: bool | None
    min_latency_ms: float | None
    max_latency_ms: float | None
    event_types: list[str] | None

    def to_sql_conditions(self) -> tuple[str, list[QueryParameter]]:
        # Returns (WHERE clause fragment, parameter list)
```

Each field generates a separate `AND` condition with a corresponding `bigquery.ScalarQueryParameter` or `bigquery.ArrayQueryParameter`. This is the **only** dynamic SQL in the SDK — everything else uses static templates.

### 4.3 `evaluators.py` — Code & LLM Evaluation

This module contains two evaluator classes and the SQL templates that power batch evaluation.

#### 4.3.1 `CodeEvaluator`

Deterministic evaluation using code-defined metric functions.

**Internal storage:**

```python
_metrics: list[dict]  # [{"name": str, "fn": Callable[[dict], float], "threshold": float}]
```

Each metric function receives a session summary dict (aggregated from BigQuery) and returns a score in `[0.0, 1.0]`.

**Pre-built factory methods** (Python path — raw-budget binary gates):

Since v0.2.2 the Python prebuilts compare the observed metric directly
against the user-supplied budget: `score = 1.0 if observed <= budget
else 0.0`, with `threshold = 1.0`. A session fails iff the observed
value strictly exceeds the budget.

| Factory | Observed Value | Pass Condition |
|---------|----------------|----------------|
| `latency(threshold_ms)` | `avg_latency_ms` | `observed <= threshold_ms` |
| `turn_count(max_turns)` | `turn_count` | `observed <= max_turns` |
| `error_rate(max_error_rate)` | `tool_errors / tool_calls` (0 when no calls) | `observed <= max_error_rate` |
| `token_efficiency(max_tokens)` | `total_tokens` | `observed <= max_tokens` |
| `context_cache_hit_rate(min_hit_rate)` | `cached_tokens / input_tokens` when cache telemetry exists | `observed >= min_hit_rate` |
| `ttft(threshold_ms)` | `avg_ttft_ms` | `observed <= threshold_ms` |
| `cost_per_session(max_cost_usd, ...)` | `(input_tokens/1K)*input_rate + (output_tokens/1K)*output_rate` | `observed <= max_cost_usd` |

`context_cache_hit_rate()` treats missing cache telemetry as unknown,
not as a cache miss. Older plugin rows without
`usage_metadata.cached_content_token_count` report
`cache_state="no_cache_telemetry"` and pass by default unless
`fail_on_missing_telemetry=True` is set.

Unlike the other prebuilt gates, `aggregate_scores` for this metric is
the average metric score rather than the fraction of sessions that
passed. That score is the observed cache hit rate for telemetry-bearing
sessions, while cache-blind sessions contribute `1.0` by default because
they pass as unknown. In mixed telemetry pipelines, the rate is also
diluted by input tokens from LLM events that do not report cache
metadata.

> **Prior to v0.2.2** these factories used a normalized score
> `1.0 - min(observed / budget, 1.0)` with a `0.5` pass cutoff, which
> effectively fired every gate at roughly half the budget the user
> typed (e.g. `latency(threshold_ms=5000)` failed at `avg_latency_ms >
> 2500`). See `CHANGELOG.md` for the migration note.

**SQL-native UDF path** (`udf_kernels.score_*`, used by
`udf_sql_templates.py`):

Unchanged — keeps the normalized `1.0 - min(observed / budget, 1.0)`
score because BigQuery SQL callers (e.g. scorecard dashboards) may
already interpret the normalized value. The divergence from the Python
path is intentional: Python prebuilts are binary CI gates; SQL UDFs
are normalized metrics for analytical workloads.

**`evaluate_session(session_summary) -> SessionScore`:**

Iterates all metrics, computes each score, determines `passed = all(score >= threshold for each metric)`.

**SQL template** (`SESSION_SUMMARY_QUERY`):

Aggregates per-session statistics from raw events:
- `COUNT(*)` as event_count
- `COUNT(CASE event_type = 'TOOL_STARTING')` as tool_calls
- `COUNT(CASE status = 'ERROR')` as tool_errors
- `AVG(JSON_VALUE(latency_ms, '$.total_ms'))` as avg_latency_ms
- `SUM(JSON_VALUE(content, '$.usage.total'))` as total_tokens
- Turn count from `USER_MESSAGE_RECEIVED` events

#### 4.3.2 `LLMAsJudge`

Semantic evaluation using an LLM as the scoring engine.

**Internal storage:**

```python
_criteria: list[dict]  # [{"name": str, "prompt_template": str, "score_key": str, "threshold": float}]
```

Prompt templates use `{trace_text}` and `{final_response}` placeholders.

**Pre-built factory methods:**

| Factory | Evaluates | Score Key |
|---------|-----------|-----------|
| `correctness(threshold)` | Factual accuracy and relevance | `correctness` |
| `hallucination(threshold)` | Unsupported claims in response | `hallucination` |
| `sentiment(threshold)` | Interaction tone and helpfulness | `sentiment` |

**`evaluate_session(trace_text, final_response) -> SessionScore`** (async):

For each criterion:
1. Format prompt template with `trace_text` and `final_response`
2. Call LLM (via `google-genai` API)
3. Parse JSON response to extract numeric score
4. Normalize to `[0.0, 1.0]` (divide by 10)

**SQL template** (`AI_GENERATE_JUDGE_BATCH_QUERY`):

Performs batch evaluation entirely within BigQuery:

```sql
WITH session_traces AS (
    SELECT session_id,
           STRING_AGG(... ORDER BY timestamp) AS trace_text,
           ARRAY_REVERSE(ARRAY_AGG(... ORDER BY timestamp))[SAFE_OFFSET(0)] AS final_response
    FROM `{table}` WHERE {where}
    GROUP BY session_id
)
SELECT session_id, trace_text, final_response,
    AI.GENERATE(
        CONCAT(@judge_prompt, '\n\nTrace:\n', trace_text, '\n\nResponse:\n', final_response),
        endpoint => @endpoint,
        output_schema => STRUCT<score INT64, justification STRING>(...)
    ).*
FROM session_traces
```

This avoids transferring trace data out of BigQuery for evaluation.

### 4.4 `trace_evaluator.py` — Trajectory Matching & Replay

#### 4.4.1 `BigQueryTraceEvaluator`

Evaluates agent behavior against expected tool-call trajectories.

**`evaluate_session()` flow:**

```
1. Fetch trace from BigQuery (_SESSION_TRACE_QUERY)
2. Parse into SessionTrace (tool_calls, events, final_response)
3. Extract actual ToolCall sequence
4. Compute trajectory score (based on MatchType)
5. Compute step efficiency
6. Optionally run LLM-as-judge on response quality
7. Determine pass/fail against thresholds
8. Return EvaluationResult
```

**`evaluate_batch()` flow:**

```
1. Create asyncio.Semaphore(concurrency)
2. For each task in eval_dataset:
   - Acquire semaphore
   - evaluate_session(task)
   - Release semaphore
3. Gather all results
```

#### 4.4.2 `TrajectoryMetrics` (static methods)

Three matching algorithms:

**`compute_exact_match(actual, expected) -> float`:**
- Requires identical sequence length and order
- Each position: compares `tool_name` equality; if match and both have `args`, compares args equality
- Score = matching_positions / max(len(actual), len(expected))

**`compute_in_order_match(actual, expected) -> float`:**
- Expected tools must appear in order within actual sequence
- Extra tools between expected steps are allowed
- Uses greedy forward scan: for each expected step, scan forward in actual
- Score = matched_expected / len(expected)

**`compute_any_order_match(actual, expected) -> float`:**
- All expected tools must be present, order doesn't matter
- Uses set-like matching with `tool_name` comparison
- Score = matched_expected / len(expected)

**`compute_step_efficiency(actual_steps, expected_steps) -> float`:**
- Measures whether agent used minimal steps
- Score = min(expected / actual, 1.0) if actual > 0, else 0.0
- Penalizes extra steps, doesn't reward fewer

#### 4.4.3 `TraceReplayRunner`

Deterministic replay for debugging and comparison:

- **`replay_session(session_id, replay_mode, step_callback)`**: Fetches trace, replays events in order. Modes: `"full"` (all events), `"step"` (with callback per event), `"tool_only"` (only tool events)
- **`compare_replays(session_a, session_b)`**: Replays both sessions, diffs tool sequences and response similarity

### 4.5 `multi_trial.py` — Statistical Evaluation

Agents are non-deterministic. A single evaluation run is not statistically meaningful. `TrialRunner` addresses this.

**`TrialRunner(evaluator, num_trials, concurrency)`:**

Runs N trials of the same evaluation task with bounded concurrency via `asyncio.Semaphore`.

**Key metrics:**

```python
def compute_pass_at_k(n: int, c: int) -> float:
    """Probability at least 1 of k trials passes.

    Formula: 1 - C(n-c, k) / C(n, k)
    where n=total trials, c=passed trials, k=n
    """
    if c == n: return 1.0
    if c == 0: return 0.0
    return 1.0 - math.comb(n - c, n) / math.comb(n, n)

def compute_pass_pow_k(n: int, c: int) -> float:
    """Probability all k trials pass.

    Formula: (c/n)^n
    """
    return (c / n) ** n if n > 0 else 0.0
```

**`MultiTrialReport`:**

```python
class MultiTrialReport(BaseModel):
    session_id: str
    num_trials: int
    num_passed: int
    pass_at_k: float
    pass_pow_k: float
    per_trial_pass_rate: float
    mean_scores: dict[str, float]
    score_std_dev: dict[str, float]
    trial_results: list[TrialResult]
```

### 4.6 `grader_pipeline.py` — Grader Composition

Combines heterogeneous evaluators into a unified verdict using a strategy pattern.

**Architecture:**

```
                    ┌──────────────────┐
                    │  GraderPipeline  │
                    │                  │
                    │  strategy: ──────┼──► ScoringStrategy
                    │  graders: ───────┼──► list[_GraderEntry]
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        CodeEvaluator   LLMAsJudge    Custom Fn
        (sync)          (async)        (sync)
              │              │              │
              ▼              ▼              ▼
         GraderResult   GraderResult   GraderResult
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                    ┌────────────────┐
                    │ScoringStrategy │
                    │  .aggregate()  │
                    └───────┬────────┘
                            ▼
                    AggregateVerdict
```

**Scoring strategies:**

| Strategy | Aggregation | Pass Condition |
|----------|-------------|----------------|
| `WeightedStrategy(weights, threshold)` | Weighted average of grader scores | Average >= threshold |
| `BinaryStrategy()` | Average of all scores | ALL graders must pass individually |
| `MajorityStrategy()` | Average of all scores | Majority (>50%) of graders must pass |

### 4.7 `eval_suite.py` — Eval Lifecycle Management

Manages collections of evaluation tasks with lifecycle governance.

**Task lifecycle:**

```
                 add_task()
                     │
                     ▼
              ┌──────────────┐
              │  CAPABILITY   │  ◄── New tasks start here
              │  (active dev) │
              └──────┬───────┘
                     │  auto_graduate() or graduate_to_regression()
                     │  (requires consistent passing over threshold_runs)
                     ▼
              ┌──────────────┐
              │  REGRESSION   │  ◄── Stable, must-pass tests
              │  (locked)     │
              └──────────────┘
```

**Health monitoring (`check_health()`):**

Detects:
- **Balance issues**: Positive/negative case ratio outside 30-70%
- **Saturation**: Tasks at 100% pass rate (may need difficulty increase)
- **Missing expectations**: Tasks without `expected_trajectory` AND `expected_response`

**`auto_graduate(pass_history, threshold_runs=10)`:**

A task graduates to REGRESSION if `all(pass_history[task_id][-threshold_runs:])` — i.e., it has passed all of the last N runs.

### 4.8 `eval_validator.py` — Static Validation

Five static checks that run without executing any evaluations:

| Check | Detects | Severity |
|-------|---------|----------|
| `check_ambiguity` | Tasks missing both expected trajectory and response | warning |
| `check_balance` | Positive/negative ratio outside 30-70% | warning |
| `check_threshold_consistency` | Thresholds at 0.0 (always pass) or 1.0 (require perfection) | warning |
| `check_duplicate_sessions` | Multiple tasks using the same session_id | info |
| `check_saturation` | Tasks at 100% pass rate (last 5+ runs) | info |

### 4.9 `feedback.py` — Drift Detection

#### Drift Detection (`compute_drift`)

Compares golden (curated Q&A) against production traffic:

1. Load golden questions from dedicated BigQuery table
2. Load production questions from `USER_MESSAGE_RECEIVED` events (with `TraceFilter`)
3. Perform keyword overlap matching: `set(q.lower().strip())`
4. Report coverage %, covered/uncovered/new questions

#### Question Distribution (`compute_question_distribution`)

Four analysis modes:

| Mode | Approach |
|------|----------|
| `frequently_asked` | SQL `GROUP BY question ORDER BY COUNT(*) DESC` |
| `frequently_unanswered` | Join with error sessions, count unanswered |
| `auto_group_using_semantics` | `AI.GENERATE` classifies each question into categories |
| `custom` | `AI.GENERATE` with user-provided category list |

### 4.10 `insights.py` — Multi-Stage Analysis Pipeline

The most complex module. Implements a six-stage pipeline:

```
Stage 1: Session Metadata Extraction
    │  SQL: _SESSION_METADATA_QUERY
    │  → list[SessionMetadata]
    ▼
Stage 2: Session Facet Extraction
    │  SQL: _AI_GENERATE_FACET_EXTRACTION_QUERY (or legacy/API fallback)
    │  → list[SessionFacet]
    │
    │  Per-session structured analysis:
    │    goal_categories, outcome, satisfaction, friction_types,
    │    session_type, agent_effectiveness, primary_success,
    │    key_topics, summary
    ▼
Stage 3: Aggregation
    │  Python: aggregate_facets()
    │  → AggregatedInsights (counters, distributions, averages)
    ▼
Stage 4: Multi-Prompt Analysis
    │  7 specialized prompts via AI.GENERATE or API:
    │    task_areas, interaction_patterns, what_works_well,
    │    friction_analysis, tool_usage, suggestions, trends
    │  → list[AnalysisSection]
    ▼
Stage 5: Executive Summary
    │  Synthesizes all 7 sections into 4-6 sentence overview
    │  → str
    ▼
Stage 6: Report Assembly
    → InsightsReport
```

**Facet extraction prompt design:**

The facet extraction prompt constrains LLM output to predefined vocabularies:
- `GOAL_CATEGORIES`: 14 categories (question_answering, data_retrieval, task_automation, ...)
- `OUTCOMES`: 5 levels (success, partial_success, failure, abandoned, unclear)
- `SATISFACTION_LEVELS`: 6 levels
- `FRICTION_TYPES`: 12 types (tool_error, slow_response, wrong_answer, ...)
- `SESSION_TYPES`: 5 types

When using `AI.GENERATE`, structured `output_schema` enforces type safety. When using the API fallback, `parse_facet_response()` validates against these vocabularies and applies defaults for invalid values.

**Analysis context computation (`build_analysis_context()`):**

Computes derived statistics from aggregated facets and metadata to feed into analysis prompts:
- Success goals vs. failure goals (by category)
- Tool error rates, underused tools (used in <=5% of sessions)
- Low success rate goals (<50%)
- Time range of analyzed data

### 4.11 `memory_service.py` — Long-Horizon Memory

Implements the ADK `BaseMemoryService` interface, enabling agents to access cross-session context stored in BigQuery.

**Composed sub-services:**

```
BigQueryMemoryService (ADK BaseMemoryService)
    ├── BigQuerySessionMemory    — Recent session context retrieval
    ├── BigQueryEpisodicMemory   — Semantic similarity search
    ├── ContextManager           — Token-budget-aware memory selection
    └── UserProfileBuilder       — User behavior profiling via LLM
```

**Semantic search strategy:**

```
1. Try vector similarity search (if embedding_model configured)
   - Generate query embedding via genai.embed_content()
   - Run ML.DISTANCE(embedding, @query_embedding, 'COSINE')
   - Return top-k by distance ASC

2. Fall back to keyword overlap search
   - Fetch recent USER_MESSAGE_RECEIVED events
   - Score: |query_words ∩ message_words| / |query_words|
   - Return top-k by score DESC
```

**Context selection algorithm (`ContextManager.select_relevant_context()`):**

```python
score = relevance_weight * relevance + recency_weight * recency

# relevance: word overlap between current task and memory content
# recency: exponential decay with 24-hour half-life: 2^(-age_hours/24)

# Greedy selection within token budget:
for memory in sorted_by_score_desc:
    tokens = len(memory_text) // 4 + 10  # rough estimate
    if current_tokens + tokens <= max_context_tokens:
        select(memory)
```

### 4.12 `ai_ml_integration.py` — BigQuery AI/ML

Direct wrappers around BigQuery's native ML capabilities:

| Class | BigQuery Feature | Use Case |
|-------|-----------------|----------|
| `BigQueryAIClient` | `AI.GENERATE` | Text generation, trace analysis |
| `EmbeddingSearchClient` | `AI.EMBED` (scalar); legacy `ML.GENERATE_EMBEDDING` | Semantic search over traces |
| `AnomalyDetector` | `AI.DETECT_ANOMALIES`, `AI.FORECAST` (TimesFM); legacy `ML.DETECT_ANOMALIES`, `ML.FORECAST` (ARIMA_PLUS); `ML.DETECT_ANOMALIES` (AUTOENCODER) | Latency anomalies, latency forecasting, behavioral anomalies |
| `BatchEvaluator` | `AI.GENERATE` with `output_schema` | Batch session evaluation with persistence |

**AI.FORECAST latency (primary — no model training needed):**

```sql
SELECT *
FROM AI.FORECAST(
  (SELECT TIMESTAMP_TRUNC(timestamp, HOUR) AS hour,
          AVG(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency
   FROM `{table}`
   WHERE event_type = 'LLM_RESPONSE'
     AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @training_days DAY)
   GROUP BY hour),
  horizon => 24,
  confidence_level => 0.95,
  timestamp_col => 'hour',
  data_col => 'avg_latency'
)
```

**Legacy ARIMA latency model (requires `use_legacy_anomaly_model=True`):**

```sql
CREATE OR REPLACE MODEL `{dataset}.latency_anomaly_model`
OPTIONS(
    model_type = 'ARIMA_PLUS',
    time_series_timestamp_col = 'hour',
    time_series_data_col = 'avg_latency',
    auto_arima = TRUE,
    data_frequency = 'HOURLY'
) AS
SELECT TIMESTAMP_TRUNC(timestamp, HOUR) AS hour,
       AVG(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64)) AS avg_latency
FROM `{table}`
WHERE event_type = 'LLM_RESPONSE'
  AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @training_days DAY)
GROUP BY hour
```

**Autoencoder behavioral model:**

```sql
CREATE OR REPLACE MODEL `{dataset}.behavior_anomaly_model`
OPTIONS(
    model_type = 'AUTOENCODER',
    activation_fn = 'RELU',
    hidden_units = [16, 8, 16],  -- symmetric encoder-decoder
    l2_reg = 0.0001,
    learn_rate = 0.001
) AS
SELECT total_events, tool_calls, tool_errors, llm_calls,
       avg_latency, session_duration
FROM `{dataset}.session_features`
```

Detection uses `STRUCT(0.01 AS contamination)` as the anomaly threshold, flagging ~1% of sessions as anomalous.

### 4.13 `bigframes_evaluator.py` — DataFrame API

Notebook-friendly alternative using BigFrames (pandas-compatible API backed by BigQuery):

```python
# Instead of SQL strings, uses BigFrames operations:
df = bpd.read_gbq(query)
df["prompt"] = prompt_prefix + df["trace_text"]
result = bbq.ai.generate(
    df["prompt"],
    endpoint=endpoint,
    output_schema={"score": "INT64", "justification": "STRING"}
)
```

Returns `bigframes.DataFrame` that can be displayed directly in Jupyter notebooks.

### 4.14 `context_graph.py` — Property Graph & World-Change Detection

Builds a BigQuery Property Graph that cross-links technical execution traces to business-domain entities. The module is organized around a 4-pillar architecture:

**Pillar 1: TechNodes** — The existing `agent_events` table serves as the technical graph. Each row is a node with `span_id`, `parent_span_id`, `event_type`, etc.

**Pillar 2: BizNodes** — Business entities extracted from trace payloads via `AI.GENERATE` with structured `output_schema`. Stored in `context_graph_biz_nodes` with composite key `biz_node_id = span_id:node_type:node_value`.

**Pillar 3: Caused edges** — Implicit edges from TechNode to BizNode via the shared `span_id` foreign key.

**Pillar 4: Evaluated cross-links** — Explicit edges stored in `context_graph_cross_links`, connecting BizNodes to their evaluation context. Carries `artifact_uri` and `link_type`.

**Key design decisions:**

| Decision | Rationale |
|----------|-----------|
| `output_schema` in AI.GENERATE | Forces structured JSON output; eliminates post-hoc parsing failures |
| Composite `biz_node_id` | Prevents key collisions when the same span produces multiple entities |
| MERGE with DELETE | `WHEN NOT MATCHED BY SOURCE ... DELETE` cleans stale nodes on re-extraction |
| Fail-closed world-change | Query or callback errors → `check_failed=True, is_safe_to_approve=False` |
| Legacy endpoint rejection | `project.dataset.model` refs raise `ValueError` instead of silently producing bad URLs |

**Key methods on `ContextGraphManager`:**

| Method | Description |
|--------|-------------|
| `build_context_graph(session_ids)` | End-to-end pipeline: extract → cross-link → create graph |
| `extract_biz_nodes(session_ids)` | Extract business entities via AI.GENERATE or client-side |
| `store_biz_nodes(nodes)` | Persist BizNodes to BigQuery |
| `create_cross_links(session_ids)` | Create Evaluated edges between BizNodes and TechNodes |
| `create_property_graph()` | Execute `CREATE PROPERTY GRAPH` DDL |
| `detect_world_changes(session_id, fn)` | Check if business entities have drifted since evaluation |
| `reconstruct_trace_gql(session_id)` | GQL-based trace reconstruction with quantified-path traversal |
| `explain_decision(event_type, entity)` | Get the reasoning chain for a specific decision |
| `get_biz_nodes_for_session(session_id)` | Read BizNodes for a session |

### 4.15 `categorical_evaluator.py` — Categorical Evaluation

Classifies agent sessions into user-defined categories (e.g., `GOOD / NEEDS_IMPROVEMENT / CRITICAL`) using BigQuery `AI.GENERATE` with automatic Gemini API fallback.

**Key design decisions:**

| Decision | Rationale |
|----------|-----------|
| BigQuery-first with Gemini fallback | `AI.GENERATE` processes batches server-side; Gemini API provides universal access when BigQuery ML is unavailable |
| `execution_mode` tracking | Every result records whether it used `bq_ai_generate`, `api_fallback`, or `api_direct`, enabling operational monitoring |
| Configurable `prompt_version` | Allows A/B testing of prompt strategies with per-version result tracking |
| Persistence to `categorical_results` | Append-only streaming insert; dedup is handled at the view layer |

**Key classes:**

- `CategoricalEvaluationConfig` — metric definitions with categories, thresholds, and prompt templates
- `CategoricalEvaluator` — orchestrates batch evaluation with `evaluate()` and `evaluate_and_persist()`

### 4.16 `categorical_views.py` — Dashboard Views

Provides pre-aggregated BigQuery views over the `categorical_results` table with deduplication at read time.

**Views:**

| View | Purpose |
|------|---------|
| `categorical_results_latest` | Base dedup view using `ROW_NUMBER() OVER (PARTITION BY session_id, metric_name, prompt_version)` |
| `categorical_daily_counts` | Daily category distribution by metric |
| `categorical_hourly_counts` | Rolling hourly counts for near-real-time dashboards |
| `categorical_operational_metrics` | Parse error rate + fallback rate by day and endpoint |

All downstream views query from the dedup base, not the raw append-only table.

### 4.17 Ontology Graph Modules (V4) — Configuration-Driven Graph Pipeline

Six modules that together implement a fully YAML-driven graph extraction and materialization pipeline. See [Ontology Graph V4 Design](ontology_graph_v4_design.md) for the full specification.

**Module responsibilities:**

| Module | Class / Function | Role |
|--------|-----------------|------|
| `ontology_models.py` | `GraphSpec`, `EntitySpec`, `load_graph_spec()` | YAML parsing, `{{ env }}` resolution, Pydantic validation |
| `ontology_schema_compiler.py` | `compile_extraction_prompt()`, `compile_output_schema()` | Deterministic prompt + JSON schema generation from spec |
| `ontology_graph.py` | `OntologyGraphManager` | `AI.GENERATE` extraction → `ExtractedGraph` hydration |
| `ontology_materializer.py` | `OntologyMaterializer` | Table creation + streaming insert with delete-then-insert idempotency |
| `ontology_property_graph.py` | `OntologyPropertyGraphCompiler` | YAML → `CREATE PROPERTY GRAPH` DDL transpilation |
| `ontology_orchestrator.py` | `build_ontology_graph()`, `compile_showcase_gql()` | One-shot pipeline + GQL query generation |

**Key design decisions:**

| Decision | Rationale |
|----------|-----------|
| YAML over code | Domain experts define ontologies without Python; spec changes don't require redeployment |
| Label-only inheritance | `extends` adds graph labels, not property/binding inheritance — keeps resolution deterministic |
| Session-scoped node identity | `session_id` in every KEY prevents cross-session entity collisions |
| All columns in PROPERTIES | BigQuery Property Graph KEY columns are not queryable in GQL unless also listed in PROPERTIES |

### 4.18 `cli.py` — Command-Line Interface

Typer-based CLI exposing SDK functionality for CI/CD pipelines, cron jobs, and ad-hoc use.

**Command groups:**

| Command | Description |
|---------|-------------|
| `doctor` | Validate BigQuery connectivity and table schema |
| `traces list` / `traces get` | List and inspect agent traces |
| `evaluate` | Run code or LLM evaluation |
| `categorical-eval` | Run categorical evaluation with optional persistence |
| `categorical-views` | Create dashboard views over categorical results |
| `insights` | Generate multi-stage analysis reports |
| `drift` | Compare production traces against golden datasets |
| `distribution` | Analyze question distribution patterns |
| `views create-all` | Create per-event-type BigQuery views |
| `ontology-build` | Run the full ontology graph pipeline |
| `ontology-showcase-gql` | Generate GQL traversal queries from spec |

All commands support `--format` (json/text/table) output via the `formatter.py` module.

### 4.19 `udf_kernels.py` — BigQuery Python UDF Kernels

Pure analytical functions designed to run inside BigQuery Python UDFs. These kernels are deterministic, side-effect free, and depend only on the Python standard library.

**Available kernels:** `classify_event_family`, `extract_tool_outcome`, `compute_latency_bucket`, `is_error_event`, `extract_response_text`.

`udf_sql_templates.py` generates the `CREATE FUNCTION` SQL for registering these kernels as BigQuery UDFs.

---

## 5. Data Model

### 5.1 BigQuery Table Schema (`agent_events`)

The canonical schema written by the ADK plugin and read by this SDK:

| Column | Type | Mode | Description |
|--------|------|------|-------------|
| `timestamp` | TIMESTAMP | REQUIRED | UTC event creation time (microsecond precision) |
| `event_type` | STRING | NULLABLE | Event category (e.g., `LLM_REQUEST`, `TOOL_COMPLETED`) |
| `agent` | STRING | NULLABLE | Agent name |
| `session_id` | STRING | NULLABLE | Persistent conversation thread identifier |
| `invocation_id` | STRING | NULLABLE | Single execution turn identifier |
| `user_id` | STRING | NULLABLE | User identifier |
| `trace_id` | STRING | NULLABLE | OpenTelemetry trace ID (32-char hex) |
| `span_id` | STRING | NULLABLE | OpenTelemetry span ID (16-char hex) |
| `parent_span_id` | STRING | NULLABLE | Parent span ID for hierarchy reconstruction |
| `content` | JSON | NULLABLE | Polymorphic event payload |
| `content_parts` | RECORD | REPEATED | Multimodal segments (text, image, GCS refs) |
| `attributes` | JSON | NULLABLE | Metadata: model info, token usage, custom tags |
| `latency_ms` | JSON | NULLABLE | Performance metrics (`total_ms`, `time_to_first_token_ms`) |
| `status` | STRING | NULLABLE | `OK` or `ERROR` |
| `error_message` | STRING | NULLABLE | Exception message when status is ERROR |
| `is_truncated` | BOOLEAN | NULLABLE | True if content exceeded 10MB cell limit |

**Partitioning:** `PARTITION BY DATE(timestamp)`
**Clustering:** `CLUSTER BY event_type, agent, user_id`

### 5.2 Content Payload Structures

The `content` JSON column is polymorphic — its structure depends on `event_type`:

```
USER_MESSAGE_RECEIVED:
  {"text_summary": "What is the weather in NYC?"}

AGENT_STARTING:
  "You are a helpful assistant..."  (system prompt as string)

LLM_REQUEST:
  {"system_prompt": "...", "prompt": [{"role": "user", "content": "..."}]}

LLM_RESPONSE:
  {"response": "The weather is...", "usage": {"completion": 19, "prompt": 10129, "total": 10148}}

TOOL_STARTING:
  {"tool": "get_weather", "args": {"city": "NYC"}}

TOOL_COMPLETED:
  {"tool": "get_weather", "result": {"temp": 72, "condition": "sunny"}}

TOOL_ERROR / LLM_ERROR:
  (content may be present; error_message column has the error)

STATE_DELTA:
  (attributes.state_delta has the state change)
```

### 5.3 Content Parts (Multimodal)

The `content_parts` REPEATED RECORD stores multimodal content segments:

```sql
ARRAY<STRUCT<
    mime_type STRING,          -- "text/plain", "image/png", etc.
    uri STRING,                -- Direct URI if applicable
    object_ref STRUCT<         -- GCS offloaded reference
        uri STRING,            -- "gs://bucket/path/file.txt"
        version STRING,
        authorizer STRING,     -- BQ connection for signed URL generation
        details JSON
    >,
    text STRING,               -- Inline text content
    part_index INT64,          -- Position in original content
    part_attributes STRING,    -- Additional metadata
    storage_mode STRING        -- "INLINE" or "GCS_REFERENCE"
>>
```

### 5.4 SDK-Created Tables

The SDK may create additional tables for derived data:

| Table | Created By | Purpose |
|-------|-----------|---------|
| `trace_embeddings` | `EmbeddingSearchClient.build_embeddings_index()` | Vector embeddings for semantic search |
| `session_features` | `AnomalyDetector.train_behavior_model()` | Aggregated session features for autoencoder |
| `session_evaluations` | `BatchEvaluator.store_evaluation_results()` | Persisted evaluation scores |
| `latency_arima_model` | `AnomalyDetector.train_latency_model()` | ARIMA time-series model |
| `behavior_autoencoder_model` | `AnomalyDetector.train_behavior_model()` | Autoencoder anomaly detection model |
| `context_graph_biz_nodes` | `ContextGraphManager.extract_biz_nodes()` | Business entities extracted from traces via AI.GENERATE |
| `context_graph_cross_links` | `ContextGraphManager.create_cross_links()` | Cross-link edges connecting BizNodes to TechNodes |
| `agent_context_graph` | `ContextGraphManager.create_property_graph()` | BigQuery Property Graph (DDL) with TechNode, BizNode, Caused, Evaluated |

### 5.5 Pydantic Data Models

Key SDK data models (all Pydantic v2 `BaseModel`):

```
SessionScore
├── session_id: str
├── scores: dict[str, float]    # metric_name -> normalized score [0,1]
├── passed: bool
└── llm_feedback: dict | None

EvaluationReport
├── evaluator_name: str
├── session_scores: list[SessionScore]
├── aggregate_scores: dict[str, float]  # normalized metrics only
├── details: dict[str, Any]             # operational metadata (parse_errors, etc.)
├── pass_rate: float                    # computed property
└── summary() -> str

EvaluationResult
├── session_id: str
├── eval_status: EvalStatus     # PASSED | FAILED | ERROR
├── scores: dict[str, float]
├── overall_score: float
├── details: dict
└── error: str | None

InsightsReport
├── session_facets: list[SessionFacet]
├── session_metadata: list[SessionMetadata]
├── aggregated: AggregatedInsights
├── analysis_sections: list[AnalysisSection]
├── executive_summary: str
└── summary() -> str

BizNode (dataclass)
├── span_id: str
├── session_id: str
├── node_type: str               # "Product", "Targeting", "Campaign", "Budget"
├── node_value: str
├── confidence: float            # 0.0-1.0
├── evaluated_at: datetime | None
├── artifact_uri: str | None     # GCS URI for persisted artifacts
└── metadata: dict

WorldChangeReport (Pydantic)
├── session_id: str
├── alerts: list[WorldChangeAlert]
├── total_entities_checked: int
├── stale_entities: int
├── is_safe_to_approve: bool
├── check_failed: bool           # fail-closed on query/callback errors
├── checked_at: datetime
└── summary() -> str
```

---

## 6. Query Architecture

### 6.1 Parameterized Query Pattern

All queries follow this pattern to prevent SQL injection:

```python
query = """
SELECT session_id, COUNT(*) AS event_count
FROM `{project}.{dataset}.{table}`
WHERE {where_clause}
GROUP BY session_id
"""

# Table references: Python string formatting (not user-controlled)
formatted = query.format(
    project=self.project_id,
    dataset=self.dataset_id,
    table=self.table_id,
    where_clause=where_sql,
)

# User-controlled values: BigQuery parameters
job_config = bigquery.QueryJobConfig(
    query_parameters=[
        bigquery.ScalarQueryParameter("agent_id", "STRING", agent_id),
        bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
    ]
)

results = client.query(formatted, job_config=job_config)
```

**Important distinction:**
- Table names (`project.dataset.table`) are interpolated via Python f-strings — these come from constructor parameters, not user input
- Filter values (`agent_id`, `session_ids`, timestamps) use BigQuery `@param` syntax with typed `QueryParameter` objects

### 6.2 SQL Template Inventory

| Module | Template | Purpose |
|--------|----------|---------|
| `client.py` | `_SESSION_EVENTS_QUERY` | Fetch all events for a session |
| `client.py` | `_LIST_SESSIONS_QUERY` | Discover sessions matching filter |
| `evaluators.py` | `SESSION_SUMMARY_QUERY` | Aggregate session metrics for code evaluation |
| `evaluators.py` | `AI_GENERATE_JUDGE_BATCH_QUERY` | Batch LLM-as-judge via AI.GENERATE |
| `evaluators.py` | `LLM_JUDGE_BATCH_QUERY` | Legacy batch evaluation via ML.GENERATE_TEXT |
| `trace_evaluator.py` | `_SESSION_TRACE_QUERY` | Fetch trace for trajectory matching |
| `insights.py` | `_SESSION_METADATA_QUERY` | Aggregate session metadata |
| `insights.py` | `_SESSION_TRANSCRIPT_QUERY` | Build session transcripts |
| `insights.py` | `_AI_GENERATE_FACET_EXTRACTION_QUERY` | Extract structured facets via AI.GENERATE |
| `insights.py` | `_AI_GENERATE_ANALYSIS_QUERY` | Run analysis prompt via AI.GENERATE |
| `insights.py` | `_LEGACY_FACET_EXTRACTION_QUERY` | Legacy facet extraction via ML.GENERATE_TEXT |
| `insights.py` | `_LEGACY_ANALYSIS_QUERY` | Legacy analysis via ML.GENERATE_TEXT |
| `feedback.py` | `_PRODUCTION_QUESTIONS_QUERY` | Extract production questions |
| `feedback.py` | `_GOLDEN_QUESTIONS_QUERY` | Load golden dataset questions |
| `feedback.py` | `_SEMANTIC_DRIFT_QUERY` | Embedding-based drift detection |
| `feedback.py` | `_FREQUENTLY_ASKED_QUERY` | Question frequency analysis |
| `feedback.py` | `_FREQUENTLY_UNANSWERED_QUERY` | Unanswered question analysis |
| `feedback.py` | `_AI_GENERATE_SEMANTIC_GROUPING_QUERY` | Question classification via AI.GENERATE |
| `feedback.py` | `_LEGACY_SEMANTIC_GROUPING_QUERY` | Legacy question classification |
| `memory_service.py` | `_RECENT_CONTEXT_QUERY` | Recent session history for a user |
| `memory_service.py` | `_SIMILARITY_SEARCH_QUERY` | Keyword-based memory search |
| `memory_service.py` | `_VECTOR_SEARCH_QUERY` | Vector similarity memory search |
| `memory_service.py` | `_USER_STATS_QUERY` | User statistics for profile building |
| `memory_service.py` | `_USER_MESSAGES_QUERY` | Recent user messages for profiling |
| `ai_ml_integration.py` | `_AI_GENERATE_QUERY` | Direct text generation |
| `ai_ml_integration.py` | `_VECTOR_SEARCH_QUERY` | Embedding vector search |
| `ai_ml_integration.py` | `_CREATE_EMBEDDINGS_TABLE_QUERY` | Create embeddings table DDL |
| `ai_ml_integration.py` | `_INDEX_EMBEDDINGS_QUERY` | Build/refresh embeddings index |
| `ai_ml_integration.py` | `_CREATE_LATENCY_MODEL_QUERY` | ARIMA model training DDL |
| `ai_ml_integration.py` | `_DETECT_LATENCY_ANOMALIES_QUERY` | ARIMA anomaly detection |
| `ai_ml_integration.py` | `_CREATE_BEHAVIOR_MODEL_QUERY` | Autoencoder model training DDL |
| `ai_ml_integration.py` | `_BATCH_EVALUATION_QUERY` | Batch evaluation via AI.GENERATE |

---

## 7. Evaluation Framework

### 7.1 Evaluation Taxonomy

```
Evaluation
├── Deterministic (CodeEvaluator)
│   ├── Latency
│   ├── Turn count
│   ├── Error rate
│   ├── Token efficiency
│   ├── Cost per session
│   └── Custom metric functions
│
├── Semantic (LLMAsJudge)
│   ├── Correctness
│   ├── Hallucination
│   ├── Sentiment
│   └── Custom criteria with prompt templates
│
├── Trajectory (BigQueryTraceEvaluator)
│   ├── Exact match
│   ├── In-order match
│   ├── Any-order match
│   └── Step efficiency
│
├── Composite (GraderPipeline)
│   ├── Weighted average
│   ├── Binary (all-pass)
│   └── Majority vote
│
└── Statistical (TrialRunner)
    ├── pass@k
    ├── pass^k
    └── Per-trial pass rate
```

### 7.2 Score Normalization Convention

All evaluation scores in the SDK are normalized to `[0.0, 1.0]`:

| Source | Raw Range | Normalization |
|--------|-----------|---------------|
| Code metrics | Already [0, 1] | None needed |
| LLM judge (API) | Integer 1-10 | Divide by 10.0 |
| LLM judge (AI.GENERATE) | INT64 1-10 | Divide by 10.0 |
| Trajectory match | Already [0, 1] | None needed |
| Step efficiency | Already [0, 1] | None needed |
| Anomaly severity | Already [0, 1] | None needed |

### 7.3 Evaluation Execution Modes

| Mode | Evaluator | Where Computation Runs |
|------|-----------|----------------------|
| Single session (sync) | `CodeEvaluator.evaluate_session()` | Python |
| Single session (async) | `LLMAsJudge.evaluate_session()` | Gemini API |
| Batch via Client | `Client.evaluate()` | BigQuery (SQL + AI.GENERATE) |
| Trajectory matching | `BigQueryTraceEvaluator.evaluate_session()` | BigQuery (fetch) + Python (matching) |
| Multi-trial | `TrialRunner.run_trials()` | BigQuery (fetch) + Python (N iterations) |
| Pipeline | `GraderPipeline.evaluate()` | Mixed (code=Python, LLM=API/BQ) |
| DataFrame | `BigFramesEvaluator.evaluate_sessions()` | BigQuery (BigFrames + AI.GENERATE) |

---

## 8. LLM Execution Strategy

### 8.1 Three-Tier Fallback

The SDK supports three paths for LLM-based operations, selected automatically:

```
Tier 1: AI.GENERATE (preferred)
    ├── Modern BigQuery SQL function
    ├── Endpoint: model name (e.g., "gemini-2.5-flash")
    ├── Supports output_schema for typed structured output
    ├── Zero data movement — computation runs in BigQuery
    └── No pre-created model required

Tier 2: ML.GENERATE_TEXT (legacy)
    ├── Classic BigQuery ML function
    ├── Endpoint: fully-qualified model ref (e.g., "project.dataset.model_name")
    ├── Requires pre-created BQML remote model
    ├── Output is unstructured text (requires JSON parsing)
    └── Detected by: endpoint.count(".") >= 2

Tier 3: Gemini API (fallback)
    ├── Direct API call via google-genai library
    ├── Used when BQ AI functions unavailable
    ├── Requires google-genai optional dependency
    ├── Data transfers to/from API endpoint
    └── Used by: insights API fallback, memory service, grader pipeline
```

### 8.2 AI.GENERATE Structured Output

When using Tier 1, the SDK leverages `output_schema` for type-safe LLM output:

```sql
AI.GENERATE(
    (prompt_text),
    endpoint => 'gemini-2.5-flash',
    output_schema => STRUCT<
        score INT64,
        justification STRING
    >(...),
    temperature => 0.3
)
```

This eliminates JSON parsing fragility — BigQuery returns typed columns directly.

### 8.3 JSON Parsing Strategy (for Tier 2 & 3)

When structured output is not available, multi-strategy JSON extraction is used:

```python
def _parse_json_from_text(text: str) -> dict | None:
    # Strategy 1: Extract from markdown code fence
    match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return json.loads(match.group(1))

    # Strategy 2: Find JSON object in raw text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    # Strategy 3: Regex extraction of specific fields
    # ... pattern-specific extraction ...

    return None
```

---

## 9. Async/Sync Boundary Design

### 9.1 Design Rationale

The SDK faces a tension: BigQuery operations are I/O-bound (network calls) and benefit from async concurrency, but many users (especially in notebooks) expect synchronous APIs.

**Solution:** Async-first internals with synchronous `Client` wrapper.

### 9.2 Boundary Locations

```
Synchronous (user-facing):
├── Client.get_trace()
├── Client.list_traces()
├── Client.evaluate()
├── Client.drift_detection()
├── Client.insights()
├── Client.deep_analysis()
├── CodeEvaluator.evaluate_session()
├── EvalSuite.*
├── EvalValidator.*
└── BigFramesEvaluator.*

Async (internal / advanced users):
├── LLMAsJudge.evaluate_session()
├── BigQueryTraceEvaluator.evaluate_session()
├── BigQueryTraceEvaluator.evaluate_batch()
├── TrialRunner.run_trials()
├── TrialRunner.run_trials_batch()
├── GraderPipeline.evaluate()
├── BigQueryMemoryService.search_memory()
├── BigQueryMemoryService.get_session_context()
├── compute_drift()
├── compute_question_distribution()
├── All ai_ml_integration functions
└── All insights pipeline stages
```

### 9.3 Event Loop Management

```python
# Client sync-to-async bridge pattern:
def evaluate(self, evaluator, filters=None):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            self._run_evaluation(evaluator, filters)
        )
    finally:
        loop.close()

# BigQuery call in async context (blocking I/O wrapped in executor):
async def _execute_query(self, query, params):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: self.client.query(query, job_config=config).result()
    )
```

### 9.4 Concurrency Control

`TrialRunner` and `BigQueryTraceEvaluator.evaluate_batch()` use `asyncio.Semaphore` for bounded concurrency:

```python
semaphore = asyncio.Semaphore(concurrency)

async def _run_one(task):
    async with semaphore:
        return await evaluator.evaluate_session(**task)

results = await asyncio.gather(*[_run_one(t) for t in tasks])
```

---

## 10. Extensibility & Plugin Points

### 10.1 Custom Metrics (CodeEvaluator)

```python
evaluator = CodeEvaluator(name="custom").add_metric(
    name="business_metric",
    fn=lambda session: your_scoring_logic(session),
    threshold=0.7,
)
```

The metric function receives the full session summary dict and returns `float` in `[0, 1]`.

### 10.2 Custom Judge Criteria (LLMAsJudge)

```python
judge = LLMAsJudge(name="custom").add_criterion(
    name="domain_accuracy",
    prompt_template="Evaluate accuracy...\n{trace_text}\n{final_response}",
    score_key="accuracy",
    threshold=0.8,
)
```

### 10.3 Custom Graders (GraderPipeline)

```python
def my_grader(context: dict) -> GraderResult:
    # context has: session_summary, trace_text, final_response
    return GraderResult(
        grader_name="my_grader",
        scores={"metric": 0.9},
        passed=True,
    )

pipeline.add_custom_grader("my_grader", my_grader)
```

### 10.4 Custom Analysis Prompts (Insights)

```python
config = InsightsConfig(
    analysis_prompts=["custom_prompt_1", "custom_prompt_2"],
)
```

### 10.5 BigQuery Client Injection

Every class that uses BigQuery accepts an optional client parameter:

```python
Client(project_id="...", dataset_id="...", bq_client=custom_client)
BigQueryTraceEvaluator(..., bq_client=mock_client)
BigQueryAIClient(..., client=mock_client)
```

---

## 11. Error Handling Philosophy

### 11.1 Guiding Principles

1. **Never crash on observability failures.** The SDK is an analytics tool — it should degrade gracefully, not block the user.
2. **Log warnings, return defaults.** When a component fails (BQ query, LLM call, JSON parsing), log at WARNING level and return a safe default (empty list, zero score, etc.).
3. **Individual failures don't poison batches.** A single session evaluation failure in a batch produces a failed result entry; the rest of the batch continues.

### 11.2 Error Handling by Layer

| Layer | Strategy | Default |
|-------|----------|---------|
| BigQuery query failure | Catch `Exception`, log WARNING | Empty result set |
| Schema verification failure | Log WARNING, continue | No enforcement |
| LLM call failure | Catch `Exception`, log WARNING | Score 0.0, `passed=False` |
| JSON parse failure | Multi-strategy parser, then default | `SessionFacet` with default values |
| Import error (optional deps) | Catch `ImportError` in `__init__.py` | Feature unavailable, DEBUG log |
| Single metric failure | Catch in metric loop | Score 0.0 for that metric |
| Single grader failure | Catch in grader loop | Failed `GraderResult`, continue |
| Facet validation error | Clamp/default invalid values | Validated `SessionFacet` |

### 11.3 Facet Validation Example

When AI.GENERATE returns values outside the expected vocabulary:

```python
def parse_facet_from_ai_generate_row(session_id, row):
    outcome = row.get("outcome", "unclear")
    if outcome not in OUTCOMES:
        outcome = "unclear"  # Default to safe value

    effectiveness = row.get("agent_effectiveness", 5.0)
    effectiveness = max(1.0, min(10.0, effectiveness))  # Clamp to [1, 10]

    key_topics = row.get("key_topics", [])[:5]  # Truncate to max 5
    summary = (row.get("summary", "") or "")[:200]  # Truncate to 200 chars
```

---

## 12. Testing Strategy

### 12.1 Test Architecture

All tests mock BigQuery — no GCP credentials or live BigQuery access is needed.

```
tests/
├── test_sdk_client.py              # Client integration tests
├── test_sdk_evaluators.py          # CodeEvaluator + LLMAsJudge
├── test_sdk_trace.py               # Trace/Span reconstruction
├── test_sdk_feedback.py            # Drift detection
├── test_sdk_insights.py            # Insights pipeline
├── test_trace_evaluator.py         # Trajectory matching
├── test_multi_trial.py             # Multi-trial runner
├── test_grader_pipeline.py         # Grader composition
├── test_eval_suite.py              # Eval suite lifecycle
├── test_eval_validator.py          # Static validation
├── test_memory_service.py          # Memory service
├── test_ai_ml_integration.py       # AI/ML integration
├── test_bigframes_evaluator.py     # BigFrames evaluator
├── test_categorical_evaluator.py   # Categorical evaluation engine
├── test_categorical_views.py       # Dashboard view SQL generation
├── test_context_graph.py           # Context Graph V2/V3
├── test_ontology_models.py         # YAML spec parsing + validation
├── test_ontology_schema_compiler.py# Schema + prompt compilation
├── test_ontology_graph.py          # Ontology extraction + hydration
├── test_ontology_materializer.py   # Table creation + materialization
├── test_ontology_property_graph.py # DDL transpilation
├── test_ontology_orchestrator.py   # End-to-end orchestrator + GQL
├── test_cli.py                     # CLI command tests
├── test_event_semantics.py         # Event semantic layer
├── test_views.py                   # BigQuery view management
├── test_formatter.py               # Output formatting
├── test_serialization.py           # JSON serialization
├── test_udf_kernels.py             # UDF kernel functions
└── test_udf_sql_templates.py       # UDF SQL generation
```

> **1297 tests** as of v0.3.0, all running without GCP credentials.

### 12.2 Mock Strategy

**BigQuery client mocking via dependency injection:**

```python
mock_bq = MagicMock()
mock_job = MagicMock()
mock_job.result.return_value = [mock_row_1, mock_row_2]
mock_bq.query.return_value = mock_job

client = Client(
    project_id="test-project",
    dataset_id="test-dataset",
    verify_schema=False,    # Skip schema verification query
    bq_client=mock_bq,
)
```

**Mock row protocol:**

BigQuery rows must implement a dict-like interface. Mock rows are constructed with:

```python
def _make_mock_row(data: dict):
    row = MagicMock()
    row.__iter__ = Mock(return_value=iter(data.keys()))
    row.get = data.get
    row.keys = data.keys
    row.values = data.values
    row.items = data.items
    row.__getitem__ = data.__getitem__
    return row
```

### 12.3 Async Test Support

`pytest-asyncio` with `asyncio_mode = "auto"` enables async tests without decorators:

```python
class TestTraceEvaluator:
    async def test_evaluate_session(self):
        # No @pytest.mark.asyncio needed
        result = await evaluator.evaluate_session(...)
        assert result.eval_status == EvalStatus.PASSED
```

---

## 13. Security Considerations

### 13.1 SQL Injection Prevention

- **Table references**: Interpolated from constructor parameters (developer-controlled, not user input)
- **Filter values**: Always use `bigquery.QueryParameter` objects with typed parameters
- **Dynamic WHERE clauses**: Generated by `TraceFilter.to_sql_conditions()` which only produces parameterized conditions

### 13.2 Content Safety

- The ADK plugin supports `content_formatter` for redaction before logging
- The SDK reads content as-is; redaction is the producer's responsibility
- `event_allowlist` / `event_denylist` on the plugin side controls what gets logged

### 13.3 Authentication

- Uses Google Cloud Application Default Credentials (ADC)
- No credential storage or management in the SDK
- IAM roles required: `bigquery.jobUser` (project), `bigquery.dataReader` (table)

### 13.4 BigFrames SQL Construction Note

`bigframes_evaluator.py` constructs SQL with inline f-string interpolation for session IDs rather than parameterized queries. This is a known limitation — the values come from the SDK's own query results, not user input, but it deviates from the parameterized query pattern used elsewhere.

---

## 14. Future Directions

### 14.1 Implemented Since v0.1 Alpha

The following capabilities have been delivered since the original design:

**Context Graph V2/V3 (v0.2):**
- 4-Pillar Property Graph: TechNode + BizNode + Caused edges + Evaluated cross-links
- BizNode extraction via `AI.GENERATE` with structured `output_schema`
- GQL trace reconstruction replacing recursive CTEs with quantified-path traversal
- World-change detection with fail-closed semantics for HITL safety
- Decision Semantics: DecisionPoint + Candidate node types with rejection rationale tracking

**Ontology Graph V4 (v0.3):**
- Configuration-driven graph pipeline: YAML spec → AI extraction → materialization → Property Graph
- Fully generic — any business ontology can be modeled without code changes
- `build_ontology_graph()` one-shot orchestrator and `compile_showcase_gql()` query generator
- See [Ontology Graph V4 Design](ontology_graph_v4_design.md)

**Categorical Evaluation (v0.3):**
- User-defined categorical classification with configurable categories and prompt templates
- BigQuery `AI.GENERATE` with automatic Gemini API fallback
- Persistent result storage with append-only writes and dedup-at-read views
- Dashboard views: daily/hourly counts, operational metrics (parse error rate, fallback rate)
- CLI exposure via `categorical-eval` and `categorical-views` commands

**CLI & Interfaces (v0.2):**
- Typer-based CLI with 12+ commands covering traces, evaluation, insights, drift, views, ontology
- JSON/text/table output formatting
- Serialization layer for Remote Function boundaries

**Python UDF Kernels (v0.2):**
- Pure analytical kernels for BigQuery Python UDFs
- SQL template generation for UDF registration

### 14.2 Open Evolution Paths

**Streaming / near-real-time evaluation:**
The batch evaluation model works for post-hoc analysis. Micro-batch evaluation (e.g., `TraceFilter(last="5m")` + cron) provides near-real-time coverage. True streaming evaluation via BigQuery subscriptions or Pub/Sub remains a future option.

**Cross-session entity resolution:**
The Ontology Graph V4 creates session-scoped nodes by design. Deduplicating business entities across sessions (e.g., "Yahoo Homepage" appearing in multiple campaigns) would enable cross-session graph analytics.

**Graph-based anomaly detection:**
GQL pattern matching over Property Graphs could detect unusual execution paths (e.g., a tool call sequence that never appeared in golden datasets).

**Embedding-based drift detection:**
The SQL template for semantic drift (`_SEMANTIC_DRIFT_QUERY`) exists but is not wired into the execution path. Activating vector-based drift detection would improve coverage detection accuracy.

**Multi-agent trace support:**
Multi-agent orchestration (where an agent delegates to sub-agents) would benefit from cross-session trace correlation and agent-specific evaluation.

**Cost attribution:**
Deeper cost attribution by tool, agent, and prompt version would enable cost optimization at the component level beyond the current per-session estimates.
