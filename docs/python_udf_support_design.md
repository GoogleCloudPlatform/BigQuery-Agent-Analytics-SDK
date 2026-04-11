# Python UDF Support Design

**Status:** Proposal
**Parent PRD:** [Unified Analytics Interface](prd_unified_analytics_interface.md)
**Related:** [Remote Function Rationale](remote_function_rationale.md)
**Date:** 2026-03-14

---

## 1. Executive Summary

This document proposes adding a **second SQL integration path** for the
BigQuery Agent Analytics SDK based on **BigQuery Python UDFs**, while
**retaining the existing Remote Function path** as the primary
full-fidelity interface.

This is the right product shape.

It is not the right engineering move to replace Remote Function with
Python UDF. The two mechanisms solve different problems:

- **Remote Function** is the right fit for the current multiplexed
  `agent_analytics(operation STRING, params JSON) RETURNS JSON` contract.
- **Python UDF** is the right fit for **typed, scalar, row-level or
  vectorized analytical kernels** that run directly inside BigQuery.

The recommended approach is:

1. Keep **Remote Function** for the broad SDK surface area.
2. Add **Python UDF** as an **independent, complementary interface**.
3. Reuse SDK logic only where it can be expressed as **pure Python kernels**
   over typed inputs.
4. Do **not** attempt to force the current JSON-RPC-like remote function
   contract into Python UDF as the primary design.

---

## 2. Why an Independent Python UDF Path Is a Good Approach

Yes, keeping both is a good approach, but only if the two paths are
deliberately separated.

That separation matters because the platform contracts are different:

- Python UDFs are **Preview / Pre-GA**.
- Python UDFs implement a **scalar function in Python**.
- Python UDFs do **not support `JSON`** types.
- Python UDFs can use third-party packages and can access external services
  only when created with a **Cloud resource connection**.
- Python UDFs run on **BigQuery-managed resources**, which removes Cloud
  Functions / Cloud Run deployment from the architecture.

Official references:

- BigQuery Python UDFs:
  `https://docs.cloud.google.com/bigquery/docs/user-defined-functions-python`
- BigQuery UDFs overview:
  `https://docs.cloud.google.com/bigquery/docs/user-defined-functions`
- BigQuery Remote Functions:
  `https://docs.cloud.google.com/bigquery/docs/remote-functions`

The product decision is therefore:

- **Remote Function** remains the best interface for the current
  operation-dispatch design.
- **Python UDF** should be added only for the subset of SDK capabilities that
  naturally map to typed UDF signatures and in-engine execution.

---

## 3. Platform Constraints That Shape the Design

The design has to respect the current BigQuery Python UDF contract.

### 3.1 Confirmed Python UDF constraints

From the current BigQuery Python UDF documentation:

- Runtime is limited to `python-3.11`
- Python UDFs are **persistent only**; temporary Python UDFs are not supported
- Python UDFs cannot be used in **materialized views**
- Python UDF query results are not cached
- `JSON`, `RANGE`, `INTERVAL`, and `GEOGRAPHY` types are not supported
- Network access is blocked unless the UDF is created with `WITH CONNECTION`
- PyPI packages and Cloud Storage Python libraries are supported
- Vectorized UDFs are documented for Python but `OPTIONS(vectorized = true)`
  is not yet supported in the Python UDF preview (only JavaScript UDFs)

### 3.2 Consequences for this SDK

These constraints drive several design decisions:

1. The current Remote Function API cannot be ported as-is.
   Reason: it depends on `JSON` input/output and a multiplexed
   operation-dispatch contract.

2. Full `Client` reuse is not the right goal.
   Reason: `Client` is built around issuing BigQuery jobs, loading trace rows,
   and assembling rich Python objects such as `Trace`,
   [`EvaluationReport`](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/evaluators.py),
   and [`InsightsReport`](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/insights.py).

3. Python UDF support should be built around **small analytical kernels**,
   not around a UDF that internally reimplements the whole SDK client.

4. Complex outputs should use either:
   - typed scalar return values, or
   - `STRING` returns carrying JSON text when structured output is necessary.

5. Session-level analytics will often require a **SQL + UDF split**:
   SQL does the scan and aggregation; Python UDF does the reusable scoring or
   parsing logic.

---

## 4. Product Positioning

The SDK should expose two SQL-native paths:

| Path | Best for | Contract shape | Infra model |
|------|----------|----------------|-------------|
| Remote Function | Full SDK access from SQL | Multiplexed, JSON-based | Cloud Function + BQ connection |
| Python UDF | Direct typed functions in BigQuery | Scalar / vectorized typed UDFs | BigQuery-managed runtime |

### 4.1 Recommendation

Use this positioning:

- **Remote Function** = broadest capability coverage
- **Python UDF** = lowest operational friction for deterministic kernels

This avoids a false choice. It also gives users a clean progression:

1. Start with Python UDFs for lightweight scoring and canonical event logic.
2. Move to Remote Function when they need richer objects, broader method
   coverage, or JSON-based multi-operation dispatch.

---

## 5. What Part of the SDK Should Python UDF Support?

The goal is not "all SDK features in Python UDF." That is not realistic under
current BigQuery constraints.

The right goal is:

> Support as much of the SDK as possible by extracting the subset of logic that
> is deterministic, pure-Python, typed, and meaningful at row or batch level.

### 5.1 Strong fit: deterministic analytical kernels

These parts of the SDK map well to Python UDFs:

| SDK area | Current source | Python UDF fit | Design |
|----------|----------------|----------------|--------|
| Error detection | [event_semantics.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/event_semantics.py) | Strong | `BOOL` helpers such as `is_error_event` |
| Tool outcome classification | [event_semantics.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/event_semantics.py) | Strong | `STRING` helpers such as `tool_outcome` |
| Response text extraction | [event_semantics.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/event_semantics.py) | Good | parse-wrapper plus `STRING` extraction from a JSON-formatted `STRING` payload |
| Latency scoring | [evaluators.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/evaluators.py) | Strong | `FLOAT64` score kernel |
| Turn-count scoring | [evaluators.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/evaluators.py) | Strong | `FLOAT64` score kernel |
| Error-rate scoring | [evaluators.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/evaluators.py) | Strong | `FLOAT64` score kernel |
| TTFT scoring | [evaluators.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/evaluators.py) | Strong | `FLOAT64` score kernel |
| Cost scoring | [evaluators.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/evaluators.py) | Strong | wider `FLOAT64` score kernel over token and pricing inputs |

These kernels are exactly the kind of logic that benefits from direct SQL
invocation with no external deployment surface.

### 5.2 Moderate fit: session-level scoring via SQL pre-aggregation

These capabilities can be supported, but not as a single "call the SDK"
primitive:

| SDK area | Python UDF fit | Required redesign |
|----------|----------------|-------------------|
| `Client.evaluate(CodeEvaluator, filters)` | Partial | SQL builds per-session summaries first; UDF computes scores from summary fields |
| `Client.deep_analysis()` / question distribution | Partial | SQL does grouping / embeddings / top-k; UDF can help with categorization or normalization |
| `Client.drift_detection()` | Partial | SQL computes set logic; UDF may help with text normalization or thresholding |
| `Client.insights()` | Partial | Best split into SQL extraction + optional UDF post-processing; not a direct port |

### 5.3 Weak fit or out of scope

These SDK capabilities should remain Remote Function or Python-library-only:

| SDK area | Why Python UDF is a poor fit |
|----------|------------------------------|
| `Client.get_trace()` / `get_session_trace()` | Rich trace reconstruction, nested objects, and event-row loading do not map cleanly to scalar UDFs |
| `Client.list_traces()` | Returns collections, not scalar computation kernels |
| `Client.doctor()` | Environment and schema diagnostics are not a natural UDF workload |
| `ViewManager` | DDL management is not a UDF responsibility |
| `ContextGraphManager` / GQL / audit export | Multi-table graph workflows and property graph DDL are outside UDF scope |
| Memory service | Stateful retrieval and semantic search are not good scalar UDF targets |
| Trial runner / eval suite / grader pipeline | Orchestration constructs, not in-query scalar kernels |

---

## 6. Recommended Design Principle

The Python UDF path should not depend on instantiating the full
[`Client`](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/client.py)
inside the UDF body.

Instead, the SDK should expose a new internal layer:

```text
bigquery_agent_analytics/
  client.py               # BigQuery job orchestration
  evaluators.py           # existing evaluator logic
  event_semantics.py      # existing canonical predicates
  udf_kernels.py          # new: pure functions reused by Python UDFs
  udf_serialization.py    # new: STRING envelope helpers if needed
```

### 6.1 Why this layering is better

It keeps the contracts clear:

- `Client` remains the orchestration layer
- Remote Function remains the broad SQL bridge
- Python UDFs reuse only pure business logic that is stable and typed

That is maintainable. Reusing the entire client inside a Python UDF is not.

### 6.2 Extraction work is real, not free

The current evaluator score math is not implemented as standalone top-level
functions today. It lives inside factory-method closures such as
`CodeEvaluator.latency()` and `CodeEvaluator.error_rate()` in
[evaluators.py](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/evaluators.py).

That means the first implementation step is a deliberate refactor:

1. extract each `_score` closure into a top-level pure function in
   `udf_kernels.py`
2. update the existing evaluator factories to call those shared functions
3. add parity tests proving exact behavior matches the current implementation

Those parity tests must cover the existing edge cases, including the current
"missing or non-positive input returns `1.0`" behavior.

---

## 7. Proposed Python UDF API Surface

The recommended API surface is a **family of typed functions**, not a single
multiplexed UDF.

### 7.1 Tier 1: canonical event semantics

These should be the first Python UDFs because they are simple, stable, and
useful across many queries.

```sql
CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_is_error_event`(
  event_type STRING,
  error_message STRING,
  status STRING
) RETURNS BOOL
LANGUAGE python
...

CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_tool_outcome`(
  event_type STRING,
  status STRING
) RETURNS STRING
LANGUAGE python
...

CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_extract_response_text`(
  content_json STRING
) RETURNS STRING
LANGUAGE python
...
```

`bqaa_extract_response_text` is not a direct lift of the current helper.
The existing
[`extract_response_text()`](/Users/haiyuancao/BigQuery-Agent-Analytics-SDK/src/bigquery_agent_analytics/event_semantics.py)
accepts a parsed Python `dict`. The Python UDF version therefore needs a thin
parse wrapper that accepts `STRING`, calls `json.loads()`, then delegates to
the shared extraction helper.

### 7.2 Tier 2: code-evaluator score kernels

These should map directly to the existing `CodeEvaluator` math:

```sql
CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_score_latency`(
  avg_latency_ms FLOAT64,
  threshold_ms FLOAT64
) RETURNS FLOAT64
LANGUAGE python
...

CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_score_error_rate`(
  tool_calls INT64,
  tool_errors INT64,
  max_error_rate FLOAT64
) RETURNS FLOAT64
LANGUAGE python
...

CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_score_turn_count`(
  turn_count INT64,
  max_turns INT64
) RETURNS FLOAT64
LANGUAGE python
...

CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_score_cost`(
  input_tokens INT64,
  output_tokens INT64,
  max_cost_usd FLOAT64,
  input_cost_per_1k FLOAT64,
  output_cost_per_1k FLOAT64
) RETURNS FLOAT64
LANGUAGE python
...
```

`bqaa_score_cost` is the widest deterministic kernel in the first batch. It
cannot reuse the simpler two- or three-argument signatures used by the other
scoring functions because the current `cost_per_session()` evaluator depends on
both token counts and pricing parameters.

These kernels let users express session evaluation in plain SQL:

```sql
WITH session_summary AS (
  SELECT
    session_id,
    AVG(latency_ms) AS avg_latency_ms,
    COUNTIF(event_type = 'TOOL_STARTING') AS tool_calls,
    COUNTIF(event_type = 'TOOL_ERROR') AS tool_errors,
    COUNTIF(event_type = 'USER_MESSAGE_RECEIVED') AS turn_count
  FROM `PROJECT.DATASET.agent_events`
  GROUP BY session_id
)
SELECT
  session_id,
  `PROJECT.UDF_DATASET.bqaa_score_latency`(avg_latency_ms, 5000.0) AS latency,
  `PROJECT.UDF_DATASET.bqaa_score_error_rate`(
    tool_calls, tool_errors, 0.1
  ) AS error_rate,
  `PROJECT.UDF_DATASET.bqaa_score_turn_count`(turn_count, 10) AS turn_score
FROM session_summary;
```

### 7.3 Tier 3: vectorized scoring envelopes

Where row-wise scalar UDFs become too slow or repetitive, add vectorized Python
UDFs that accept a batch of typed columns and return one score column.

This is the right place to support:

- batch latency scoring
- batch cost scoring
- batch normalized labels from free-text fields

### 7.4 Tier 4: optional STRING-envelope functions

For outputs that need richer structure, allow a separate family of UDFs that
return `STRING` containing JSON text:

```sql
CREATE FUNCTION `PROJECT.UDF_DATASET.bqaa_eval_summary_json`(
  avg_latency_ms FLOAT64,
  tool_calls INT64,
  tool_errors INT64,
  turn_count INT64,
  threshold_ms FLOAT64,
  max_error_rate FLOAT64,
  max_turns INT64
) RETURNS STRING
LANGUAGE python
...
```

This is acceptable as a secondary pattern, but it should not become the
primary API style.

---

## 8. What Not to Do

These anti-patterns should be explicitly avoided.

### 8.1 Do not build a Python UDF clone of the Remote Function

Avoid:

```sql
-- Not recommended
CREATE FUNCTION fn(operation STRING, params STRING) RETURNS STRING
```

Why this is the wrong default:

- it recreates a JSON-RPC envelope without JSON type support
- it loses type safety
- it is harder to document and optimize
- it hides which inputs are actually required
- it becomes a second dispatch surface to maintain

### 8.2 Do not call BigQuery recursively from inside the UDF as the main model

The Python UDF should not become a wrapper that:

1. accepts a session id
2. creates a BigQuery client
3. submits another BigQuery job
4. waits for the result
5. returns a string

That would be operationally fragile, hard to reason about, and contrary to the
point of in-engine computation.

### 8.3 Do not promise full SDK parity

Python UDF should be documented as a **subset interface**. If the docs imply
"everything the SDK can do is now callable directly as a Python UDF," the docs
will overpromise and drift.

---

## 9. Proposed Repository Layout

If this proposal is accepted, the implementation should add a separate deploy
surface:

```text
deploy/
  remote_function/
  python_udf/
    register.sql
    generate_sql.py
    README.md
examples/
  python_udf_evaluation.sql
  python_udf_event_semantics.sql
src/bigquery_agent_analytics/
  udf_kernels.py
  udf_sql_templates.py
tests/
  test_udf_kernels.py
  test_udf_sql_generation.py
docs/
  python_udf_support_design.md
```

### 9.1 `udf_kernels.py`

This module should contain only:

- pure functions
- typed inputs and outputs
- no BigQuery client creation
- no file IO
- no environment-dependent behavior

That gives one source of truth for both Python and SQL-facing implementations.

---

## 10. Coverage Matrix

This is the realistic support target.

| SDK capability | Remote Function | Python UDF | Recommended path |
|----------------|-----------------|------------|------------------|
| Trace reconstruction | Full | No | Remote Function / Python SDK |
| Deterministic score math | Full | Full | Both |
| Canonical event predicates | N/A | Full | Python UDF |
| Session-level eval from pre-aggregated SQL | Good | Good | Both |
| LLM judge | Good | Maybe, but not preferred | Remote Function or BigQuery SQL AI |
| Insights extraction | Good | Partial | Remote Function or SQL AI pipeline |
| Drift detection | Good | Partial | Remote Function plus SQL primitives |
| Context graph / GQL / audit | Full | No | Remote Function / Python SDK |
| View creation | No | No | CLI / Python SDK |
| Health diagnostics | No | No | CLI / Python SDK |

### 10.1 Important implication

Python UDF should be described as:

> "Fastest path for deterministic, typed analytics inside BigQuery."

Remote Function should still be described as:

> "Broadest SQL interface for SDK capabilities."

---

## 11. Recommended Rollout Plan

### Phase U1: design-safe core extraction

- Add `udf_kernels.py`
- Move reusable evaluator math into standalone pure functions
- Move reusable event semantic helpers into a UDF-safe layer
- Add unit tests proving parity with existing `CodeEvaluator` behavior

### Phase U2: Tier 1 and Tier 2 UDFs

- Register event semantics UDFs
- Register deterministic score UDFs
- Publish example SQL for session scoring
- Document region-replication guidance for utility datasets

Note: BigQuery UDFs are region-scoped. The BigQuery UDF docs recommend
maintaining UDFs in each region, or using dataset replication for utility
datasets.

### Phase U3: vectorized UDFs (deferred)

- **Deferred**: BigQuery Python UDF preview does not support
  `OPTIONS(vectorized = true)`. The option is currently only available
  for JavaScript UDFs. Vectorized Python UDFs will be added when
  BigQuery extends `vectorized` support to the Python runtime.
- When available: add batch-oriented UDFs where scalar calls become
  inefficient; benchmark against pure SQL expressions and Remote
  Function equivalents.

### Phase U4: selective structured `STRING` envelopes

- Add JSON-string return helpers only where the structure is worth the extra
  parsing overhead

---

## 12. Documentation Positioning

The docs should say this explicitly:

- Python UDF support is **complementary**
- Remote Function support remains **first-class**
- Python UDF support is aimed at **deterministic and typed kernels**
- Complex, orchestration-heavy, or JSON-heavy SDK features remain better served
  by Remote Function or direct Python usage

Recommended wording:

> The SDK supports two SQL-native execution paths. Use Remote Function for the
> broadest method coverage and JSON-shaped outputs. Use Python UDFs for direct
> in-BigQuery execution of deterministic analytical kernels with typed inputs.

---

## 13. Concrete Recommendation

Yes, adding independent Python UDF support is a good approach.

But only under these conditions:

1. Remote Function stays in place as the full-surface SQL bridge.
2. Python UDF support is positioned as a **parallel subset interface**.
3. The Python UDF API is **typed and function-family-based**, not a second
   JSON-RPC dispatcher.
4. The implementation reuses **pure kernels**, not the full `Client`
   orchestration layer.

If those constraints are followed, Python UDF support improves the SDK:

- less deployment overhead for deterministic SQL-native users
- better fit for direct BigQuery evaluation patterns
- no damage to the existing Remote Function design

If those constraints are not followed, Python UDF support will become a second,
worse Remote Function implementation.

---

## 14. Proposed Next Step

The next step should be a narrow implementation plan, not immediate broad
feature parity.

Recommended first cut:

1. Event semantics UDFs
2. Deterministic score kernels for `latency`, `error_rate`, `turn_count`,
   `ttft`, and `cost`
3. Example SQL showing session pre-aggregation plus UDF scoring
4. Benchmark and doc comparison versus Remote Function

That is the highest-confidence path to "support as much as possible" without
building the wrong abstraction.
