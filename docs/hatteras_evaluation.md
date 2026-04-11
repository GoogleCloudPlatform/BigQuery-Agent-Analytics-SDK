# Hatteras-Style Categorical Evaluation Design

## Summary

Add a new categorical evaluation surface to the SDK so users can classify
agent sessions into user-defined categories directly against traces stored in
BigQuery, without relying on an external service.

This should be implemented as a new categorical evaluation subsystem, not as
an overload of the existing numeric `CodeEvaluator` / `LLMAsJudge` report
path.

The goal is to support Hatteras-like functionality inside the SDK:

- user-defined categorical metrics
- BigQuery-native batch execution when possible
- real-time or near-real-time categorical evaluation as the end-state
- Gemini API fallback when BigQuery-native execution is unavailable
- optional persistence of classification results back to BigQuery

## Why This Is Worth Adding

Today the SDK supports two major evaluation modes:

- deterministic numeric scoring via `CodeEvaluator`
- semantic numeric scoring via `LLMAsJudge`

What is missing is a first-class way to answer questions like:

- What kind of issue is this conversation about?
- Was the user frustrated, satisfied, or neutral?
- Did the agent behavior fall into one of several reviewed quality buckets?
- Which business-defined category best describes the session outcome?

That capability is useful for:

- privacy-reviewed or business-reviewed categorical taxonomies
- operational dashboards and routing
- continuous quality monitoring
- post-hoc labeling for analytics, evaluation, and experimentation

## Design Principles

1. Keep the current `Client.evaluate()` contract focused on numeric
   evaluation.
2. Add categorical evaluation as a parallel API surface, not a hidden
   extension of numeric reports.
3. Prefer one model call per session that returns all requested metric
   classifications.
4. Use structured output and strict category validation.
5. Support both:
   - BigQuery-native execution via `AI.GENERATE`
   - Gemini API fallback for environments where BigQuery-native execution is
     unavailable or undesired
6. Make persistence optional in phase 1, but mandatory in the final
   real-time architecture.

## Non-Goals

This design is not proposing:

- a full clone of an external Hatteras service
- a replacement for `CodeEvaluator`
- a replacement for `LLMAsJudge`
- a new remote function or Python UDF surface in the first phase
- real-time ingestion-time classification in phase 1

Phase 1 should be batch or near-real-time evaluation over existing traces in
BigQuery.

## Proposed SDK Surface

### New configuration models

```python
from typing import Optional

from pydantic import BaseModel
from pydantic import Field


class CategoricalMetricCategory(BaseModel):
    name: str = Field(description="Category label.")
    definition: str = Field(description="What this category means.")


class CategoricalMetricDefinition(BaseModel):
    name: str = Field(description="Metric name.")
    definition: str = Field(description="What this metric measures.")
    categories: list[CategoricalMetricCategory] = Field(
        description="Allowed categories for this metric.",
    )
    required: bool = Field(
        default=True,
        description="Whether this metric must be classified.",
    )


class CategoricalEvaluationConfig(BaseModel):
    metrics: list[CategoricalMetricDefinition] = Field(
        description="Metrics to evaluate.",
    )
    endpoint: str = Field(
        default="gemini-2.5-flash",
        description="Model endpoint for classification.",
    )
    temperature: float = Field(
        default=0.0,
        description="Sampling temperature.",
    )
    persist_results: bool = Field(
        default=False,
        description="Write results to BigQuery.",
    )
    results_table: Optional[str] = Field(
        default=None,
        description="Destination table for results.",
    )
    include_justification: bool = Field(
        default=True,
        description="Include justification in output.",
    )
    prompt_version: Optional[str] = Field(
        default=None,
        description="Tracks prompt version for reproducibility.",
    )
```

### New result models

```python
from datetime import datetime
from datetime import timezone
from typing import Any, Optional

from pydantic import BaseModel
from pydantic import Field


class CategoricalMetricResult(BaseModel):
    metric_name: str
    category: Optional[str] = None
    passed_validation: bool = True
    justification: Optional[str] = None
    raw_response: Optional[str] = None
    parse_error: bool = False


class CategoricalSessionResult(BaseModel):
    session_id: str
    metrics: list[CategoricalMetricResult] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class CategoricalEvaluationReport(BaseModel):
    dataset: str
    evaluator_name: str = "categorical_evaluator"
    total_sessions: int = 0
    category_distributions: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description="Maps metric_name → {category → count}.",
    )
    details: dict[str, Any] = Field(default_factory=dict)
    session_results: list[CategoricalSessionResult] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
```

### New client method

```python
report = client.evaluate_categorical(
    config=config,
    filters=TraceFilter.from_cli_args(last="24h"),
    dataset="agent_events",
)
```

This should be a separate method, not an overload of `Client.evaluate()`,
because the current numeric report shape (`aggregate_scores`,
`SessionScore.scores`) is not a clean fit for label-valued results.

## Execution Model

### Preferred path: BigQuery-native batch classification

Use the same design direction already used elsewhere in the SDK:

- SQL builds one transcript per session
- `AI.GENERATE` performs the classification
- structured output is returned in typed columns
- the SDK validates categories against the configured allowlist

The key design choice is:

one model call per session should return all metric classifications

instead of:

one model call per metric per session

because the latter is slower, more expensive, and harder to reason about
operationally.

### Example output shape

Conceptually, the model should return a structure like:

```text
results ARRAY<STRUCT<
  metric_name STRING,
  category STRING,
  justification STRING
>>
```

If `AI.GENERATE` cannot directly express the final nested schema cleanly,
phase 1 can use a constrained string/JSON output plus strict SDK-side
validation. But the target should be structured output, not substring
parsing.

### Concrete `output_schema` decision

Phase 1 uses a single STRING column containing a JSON array of metric
results rather than attempting a fully typed `ARRAY<STRUCT<...>>`
`output_schema`:

```text
output_schema => 'classifications STRING'
```

This is a conservative design choice, not a proven platform limitation.
`AI.GENERATE`'s `output_schema` supports typed arrays (the SDK's
`insights.py` already uses `ARRAY<STRING>` columns like
`goal_categories` and `friction_types`), but the multi-metric
classification payload — an array of structs with heterogeneous fields —
has not been validated against `output_schema` yet. Starting with a JSON
STRING envelope keeps phase 1 unblocked while that validation happens.

The SDK parses the JSON string and validates each entry's `category` value
against the configured allowlist.

### Endpoint precedence

When `config.endpoint` is set, it overrides `Client.endpoint`. This
matches the existing SDK pattern where evaluator-level config takes
precedence over client defaults.

## Prompting and Validation

Each metric definition supplies:

- metric name
- metric definition
- allowed categories
- category definitions

The prompt should instruct the model to:

- classify the full session for every configured metric
- choose exactly one category from the allowed set for each metric
- optionally return a short justification
- return no extra categories or free-form labels

The SDK should then:

- normalize category names
- validate that each category is in the configured allowlist
- mark invalid or missing outputs with `parse_error=True`
- store raw output in `raw_response` for debugging

The implementation should explicitly avoid substring matching like
`if cat in response`, because that is too weak for production categorical
evaluation.

## BigQuery-Native SQL Shape

The first implementation should follow the same transcript-building pattern
already used by `LLMAsJudge` and `insights`:

1. Filter sessions with `TraceFilter`
2. Build one ordered transcript per `session_id`
3. Call `AI.GENERATE` once per session
4. Read typed output columns or parse a constrained JSON/string envelope
5. Convert rows into `CategoricalSessionResult`

This keeps the execution model aligned with the existing SDK design instead of
introducing a separate trace-by-trace Python-only path.

### Concrete SQL template

```sql
WITH session_transcripts AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type,
        COALESCE(CONCAT(' [', agent, ']'), ''),
        ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'),
          JSON_VALUE(content, '$.response'),
          JSON_VALUE(content, '$.tool'),
          ''
        )
      ),
      '\n' ORDER BY timestamp
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE {where}
  GROUP BY session_id
  HAVING LENGTH(transcript) > 10
  LIMIT @trace_limit
)
SELECT
  session_id,
  transcript,
  (AI.GENERATE(
    CONCAT(
      @categorical_prompt,
      '\n\nTranscript:\n', transcript
    ),
    endpoint => '{endpoint}',
    model_params => JSON '{"generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024}}',
    output_schema => 'classifications STRING'
  )).classifications AS classifications
FROM session_transcripts
```

`AI.GENERATE` is a scalar function: the first positional argument is the
prompt, and it returns a STRUCT containing the `output_schema` fields
plus `full_response` and `status`. The `model_params` JSON must conform
to the Gemini `GenerateContent` request body format.

## Real-Time Evaluation and Dashboard End-State

Real-time evaluation is an important product goal for this design. The final
system should support not only offline or scheduled batch classification, but
also a near-real-time pipeline that produces continuously updating
categorical metrics for dashboards and alerting.

The key point is that phase 1 does not need to deliver true streaming
classification, but the architecture should be shaped so the later real-time
path does not require a redesign of the metric definitions, result schema, or
validation logic.

### End-state goal

After the full implementation is complete, the SDK should support:

- categorical evaluation of recent sessions with low operational delay
- persisted session-level categorical results in BigQuery
- dashboard-friendly tables or views for category trends, distributions, and
  error counts
- alerting or monitoring built on top of persisted categorical outputs

### Recommended architecture

The real-time path should be built as a pipeline with clear separation of
responsibilities:

1. agent events land in the canonical `agent_events` table
2. a transcript-building step produces session-ready or recent-session-ready
   inputs
3. a categorical evaluation step classifies those sessions using the same
   metric definitions as the batch path
4. validated results are written to a durable BigQuery results table
5. dashboard queries or views read from the persisted results table instead of
   calling the model live on every dashboard refresh

This matters because dashboards should read from stable persisted outputs, not
invoke LLM evaluation inline. Inline evaluation would be slower, more
expensive, and harder to audit.

### Real-time delivery options

The implementation can support multiple execution modes over time:

- scheduled micro-batch evaluation over the last N minutes of sessions
- event-driven evaluation triggered after session completion
- hybrid mode where batch backfills and real-time classification share the
  same parsing and persistence code

The recommended first real-time milestone is scheduled micro-batch execution,
because it is operationally simpler than true per-event streaming and still
good enough for dashboards that refresh every few minutes.

### Dashboard support

Dashboard support should be treated as a first-class outcome of this design,
not a side effect.

The persisted results model should support:

- category distribution over time
- breakdown by agent, environment, experiment, or user segment
- top failure or escalation categories
- recent-session monitoring panels
- drill-down from aggregate counts to example sessions

To support that, the results table should be paired with stable views or
example queries such as:

- daily category counts by metric
- rolling 1-hour category counts
- per-agent category distribution
- parse error rate and model fallback rate

### Required invariants for the real-time path

To make batch and real-time coexist cleanly, both paths should share:

- the same metric-definition schema
- the same prompt-building logic
- the same category validation rules
- the same persisted results schema
- the same execution metadata fields, especially `execution_mode`,
  `prompt_version`, `endpoint`, and `created_at`

If those invariants hold, dashboards and downstream consumers do not need to
care whether a classification came from batch execution or a real-time path.

### Suggested final-phase deliverables

The final state of this work should include:

- real-time or near-real-time categorical evaluation pipeline
- durable BigQuery table for categorical evaluation results
- dashboard-oriented example SQL or views
- documentation for recommended dashboard patterns
- operational metrics for fallback rate, parse error rate, and evaluation
  latency

This is the right end-state because the value of categorical evaluation is not
just producing labels. The value is making those labels continuously usable
for monitoring, reporting, and decision-making.

## Fallback Path

When BigQuery-native execution is unavailable or fails, fall back to Gemini
API execution.

The fallback path should:

- reuse the same metric definitions
- reuse the same strict validation logic
- record execution mode in `report.details`
- preserve as much result shape parity as possible

Example detail fields:

- `execution_mode`: `"ai_generate" | "api_fallback"`
- `endpoint`
- `parse_errors`
- `parse_error_rate`
- `persisted`

## Persistence Design

Persistence should be optional but designed in phase 1.

### Suggested results table schema

- `session_id STRING`
- `metric_name STRING`
- `category STRING`
- `justification STRING`
- `passed_validation BOOL`
- `parse_error BOOL`
- `raw_response STRING`
- `endpoint STRING`
- `execution_mode STRING`
- `prompt_version STRING`
- `created_at TIMESTAMP`

This table can later support:

- dashboarding
- auditing
- experiment comparison
- downstream aggregation and drift analysis
- real-time monitoring views

## Relationship to Existing SDK Components

The implementation lives in a new module:
`src/bigquery_agent_analytics/categorical_evaluator.py`. It does not
belong in `evaluators.py`, which is focused on numeric scoring contracts.

This proposal should reuse and align with current SDK patterns:

- `TraceFilter` for session selection
- `Client.get_session_trace()` transcript semantics
- BigQuery-native `AI.GENERATE` execution patterns already used in `insights`
  and other modules
- Gemini API fallback patterns already used by `LLMAsJudge`

This proposal should not try to force categorical labels into:

- `SessionScore.scores: dict[str, float]`
- `EvaluationReport.aggregate_scores: dict[str, float]`

Those numeric contracts should remain numeric.

## Suggested Implementation Phases

### Phase 1: Models + BigQuery-native execution (working backend)

- Add Pydantic config, metric, and result models in
  `categorical_evaluator.py`
- Add `Client.evaluate_categorical(...)` entry point
- Build session-transcript SQL and `AI.GENERATE` query template
- Return one classification payload per session for all metrics
- Add strict validation and parsing helpers (category allowlist check,
  JSON envelope parsing)
- Add unit tests for metric definition validation and result parsing
- Add integration tests for typed row parsing and invalid-category
  handling

### Phase 2: Gemini API fallback

- Add API execution path
- Preserve report shape parity with the BigQuery-native path
- Add tests for fallback behavior and partial failures

### Phase 3: Persistence

- Add result table DDL/template
- Add `persist_results=True` flow
- Add export/query helpers if needed

### Phase 4: CLI exposure

- Optional CLI wrapper after the Python API stabilizes
- Keep this out of the initial implementation scope

### Phase 5: Real-time evaluation and dashboard support

- Add scheduled micro-batch or event-driven execution for recent sessions
- Reuse the same metric definitions and validation logic as the batch path
- Persist real-time classifications into the same results schema
- Add dashboard-oriented views or example SQL
- Document recommended dashboard and alerting patterns

## Open Questions

1. ~~Can `AI.GENERATE` express the desired multi-metric structured output
   directly, or should phase 1 use a constrained JSON/string envelope?~~
   **Decided:** phase 1 uses a JSON STRING envelope
   (`output_schema => 'classifications STRING'`). This is a conservative
   choice while `ARRAY<STRUCT<...>>` support in `output_schema` is
   validated; see the Concrete `output_schema` decision section.
2. ~~Should one session produce exactly one result row, or should results be
   flattened to one row per `(session_id, metric_name)` during
   parsing/persistence?~~
   **Decided:** flatten to one row per `(session_id, metric_name)` for
   persistence. This matches the results table schema shown in the
   Persistence Design section.
3. Should justifications be stored by default, or optional because of privacy
   or storage concerns?
4. Should the first release support only session-level classification, or also
   last-turn-only classification?
5. ~~Should categorical evaluation live in `evaluators.py`, or in a new module
   such as `categorical_evaluators.py` to avoid conflating numeric and
   label-based contracts?~~
   **Decided:** new module `categorical_evaluator.py`. See the Relationship
   to Existing SDK Components section.

## Acceptance Criteria

- Users can define one or more categorical metrics with explicit allowed
  categories.
- Users can run categorical evaluation over filtered sessions in BigQuery.
- The SDK validates model output against the configured category set.
- Invalid or missing outputs are surfaced explicitly as parse errors.
- Results are returned in a dedicated categorical report type.
- BigQuery-native execution is the preferred path.
- Gemini API fallback works when BigQuery-native execution is unavailable.
- Optional persistence path is clearly defined.

## Why This Approach Fits the SDK

The important design choices are:

- categorical evaluation is a new API surface, not a hidden extension of
  numeric evaluation
- execution is batch-first, not one LLM call per metric per trace
- parsing is strict and schema-driven, not substring-based
- result models are label-oriented, not forced into numeric score containers

That makes the feature much more likely to integrate cleanly with the current
SDK and to scale operationally.
