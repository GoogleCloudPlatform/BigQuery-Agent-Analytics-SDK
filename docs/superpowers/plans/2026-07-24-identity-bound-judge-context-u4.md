# Identity-Bound Judge Context (U4) Implementation Plan

> Issue scope: #358 U4, sequenced by #361 after merged #359 U2/U3.
> This unit binds trusted per-trace judge context through categorical
> AI.GENERATE, retry, and Gemini API fallback. U5 remains responsible for
> persistence/results schema, views, reports, and the quality-report caller.

## Contract

- `Client.evaluate_categorical()` accepts an optional mapping whose keys are
  immutable `ResolvedTraceSelector` values or legacy session-id strings and
  whose values are trusted judge-context strings.
- A non-empty mapping resolves the evaluated population to exact U2
  identity/scope selectors before any model call. Legacy string keys are
  accepted only when that session id names exactly one resolved trace in the
  evaluated population; ambiguity raises `AmbiguousSessionError`.
- The resolved population is deduplicated by selector. Unmapped traces still
  run without extra context. Context keys outside the filtered population are
  ignored so callers may safely pass a superset mapping.
- A non-empty mapping bypasses AI.CLASSIFY, because AI.CLASSIFY cannot accept
  per-row context. The report records that reason even when justification was
  disabled.
- The same selector-keyed context is used by BigQuery AI.GENERATE, its
  parse/NULL retry, and full Gemini API fallback. Context is sent only as query
  parameter/model prompt data and is never interpolated into SQL, logged,
  persisted, or placed in job labels.
- Empty resolved work constructs a typed empty array parameter in helper-level
  coverage and returns without a model call.
- The no-context path remains byte-for-byte compatible in behavior and query
  shape.

## Task 1: Add identity-bound input primitives and transcript parity

**Files**

- Modify: `src/bigquery_agent_analytics/categorical_evaluator.py`
- Test: `tests/test_categorical_evaluator.py`

**Test-first scenarios**

1. Format a materialized `Trace` into the same event/agent/content-priority
   transcript used by the existing SQL (`text_summary`, `response`, first
   artifact text, `tool`, empty).
2. Build a stable selector key from all intrinsic identity fields plus the
   exact scope signature; two reused session ids must produce different keys.
3. Build an `ARRAY<STRUCT<evaluation_key STRING, session_id STRING,
   transcript STRING, judge_context STRING>>` query parameter for non-empty
   inputs and, critically, with the same explicit struct type for an empty
   list.
4. Render an identity-bound AI.GENERATE query that reads only
   `UNNEST(@evaluation_inputs)`, includes nullable context in the prompt, and
   does not interpolate context or rescan/group the events table.

**Verification**

```bash
uv run pytest -q tests/test_categorical_evaluator.py -k "IdentityBound or EvaluationInput or TraceTranscript"
```

## Task 2: Resolve and normalize the context population

**Files**

- Modify: `src/bigquery_agent_analytics/client.py`
- Test: `tests/test_categorical_evaluator.py`
- Test: `tests/test_sdk_client.py`

**Test-first scenarios**

1. Two traces with the same session id but different resolved selectors receive
   their own exact-selector contexts.
2. A legacy session-id key resolves when exactly one trace matches and raises
   `AmbiguousSessionError` with executable U2 candidates when two match.
3. Exact-selector and legacy aliases deduplicate to one evaluation input;
   conflicting values for the same resolved selector fail closed.
4. Duplicate traces from a caller/test seam deduplicate before the BigQuery
   struct parameter is constructed.
5. Unmapped traces are retained with `judge_context=None`; out-of-population
   mapping entries are ignored.
6. Invalid key/value types fail before a model query.
7. Empty filtered populations return without calling AI.CLASSIFY,
   AI.GENERATE, or the API.

**Verification**

```bash
uv run pytest -q tests/test_categorical_evaluator.py tests/test_sdk_client.py -k "context or Context or identity_bound"
```

## Task 3: Carry context through AI.GENERATE and retries

**Files**

- Modify: `src/bigquery_agent_analytics/client.py`
- Modify: `src/bigquery_agent_analytics/categorical_evaluator.py`
- Test: `tests/test_categorical_evaluator.py`
- Test: `tests/test_client_labels.py`

**Test-first scenarios**

1. AI.GENERATE receives the typed selector/transcript/context struct parameter
   and no raw context appears in SQL or job labels.
2. Results for a reused session retain reserved `user_id`,
   `root_agent_name`, and `scope_signature` attribution in `details`.
3. A NULL/unparseable result retries by internal selector key, not session id;
   two colliding session ids cannot overwrite each other.
4. Mixed mapped/unmapped retries preserve the corresponding context exactly.
5. Existing telemetry labels stay unchanged and never contain context.

**Verification**

```bash
uv run pytest -q tests/test_categorical_evaluator.py tests/test_client_labels.py -k "context or Context or retry"
```

## Task 4: Carry context through full API fallback and record mode decisions

**Files**

- Modify: `src/bigquery_agent_analytics/client.py`
- Modify: `src/bigquery_agent_analytics/categorical_evaluator.py`
- Test: `tests/test_categorical_evaluator.py`
- Test: `tests/test_sdk_client.py`

**Test-first scenarios**

1. `classify_sessions_via_api()` preserves legacy session-key behavior without
   selector metadata.
2. With selector metadata, colliding session ids each receive only their own
   context and result attribution.
3. Full fallback reuses the already-resolved transcripts/contexts and performs
   no second transcript query.
4. `include_justification=False` plus non-empty context starts at
   AI.GENERATE, never AI.CLASSIFY, and records a stable
   `classify_skip_reason`.
5. AI.GENERATE failure followed by API success produces the same identity
   population and context binding.

**Verification**

```bash
uv run pytest -q tests/test_categorical_evaluator.py tests/test_sdk_client.py -k "context or Context or fallback or classify"
```

## Task 5: Public documentation and release-boundary guidance

**Files**

- Modify: `src/bigquery_agent_analytics/client.py`
- Modify: `src/bigquery_agent_analytics/categorical_evaluator.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: relevant docstring/API tests discovered during implementation

**Content**

- Explain selector-keyed and legacy-key behavior, ambiguity, mixed mappings,
  and AI.CLASSIFY bypass.
- State explicitly that context is trusted evaluator material subject to the
  same governance as evaluation prompts; it must not contain untrusted
  conversation instructions unless the caller deliberately accepts prompt
  influence.
- Record that context is query-parameter/model input only and is not persisted
  by U4.
- Record the release gate: U4 is not released before U5 completes identity-safe
  persistence/report propagation.
- Do not modify quality-report callers, result-table schemas, persistence
  writers, views, or report cardinality in this unit; those are U5.

## Final verification

```bash
uv run pytest -q tests/test_categorical_evaluator.py tests/test_sdk_client.py tests/test_client_labels.py
uv run pytest -q
uv run autoformat
git diff --check
uv run pytest -q tests/test_trace_identity_bigquery_live.py
```

For BigQuery grammar, dry-run both non-empty and explicitly typed empty
`@evaluation_inputs` parameters against the generated identity-bound query.
The opt-in live collision fixture must prove two identities sharing a session
receive different context through AI.GENERATE/fallback before #358 closes.
