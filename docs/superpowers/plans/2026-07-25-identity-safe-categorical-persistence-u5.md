# Identity-Safe Categorical Persistence (U5) Implementation Plan

> Issue scope: #358 U5, sequenced by #361 after merged U4.
> This unit carries U2/U4 identity and judge-context provenance through
> categorical results, persistence, views, and quality reports. U6 remains
> responsible for removing the demo workarounds after this unit lands.

## Contract

- Every identity-resolved categorical result may carry its immutable
  `TraceIdentity`, exact `TraceScope`, whether trusted judge context was
  applied, the SDK-defined context-source enum, and its execution mode.
- Persistence uses an additive, rollback-safe migration in strict
  schema → writer → view order. Historical rows remain untouched.
- New rows contain nullable identity dimensions, a canonical scope key, a
  versioned identity key, and provenance. They never contain judge context,
  expected answers, caller-controlled source strings, or context
  fingerprints.
- The latest-results view deduplicates new rows by the versioned identity key.
  A legacy session with exactly one post-migration identity is superseded by
  that identity; legacy sessions with zero or multiple post-migration
  identities retain a namespaced `legacy:<session_id>` lane.
- The quality-report BigQuery path binds golden answers by exact resolved
  selector, not session id, and correlates golden metadata to the same
  identity-safe result.
- Retry/fallback reconciliation may replace only the failed resolved identity.
  In-memory, serialized report, persisted table, and dashboard view
  cardinality agree for colliding session ids.

## Task 1: Add typed result identity and provenance

**Files**

- Modify: `src/bigquery_agent_analytics/categorical_evaluator.py`
- Modify: `src/bigquery_agent_analytics/client.py`
- Test: `tests/test_categorical_evaluator.py`
- Test: `tests/test_sdk_client.py`

**Test-first scenarios**

1. AI.GENERATE and Gemini API results for two selectors sharing a session id
   retain distinct `TraceIdentity`/`TraceScope` values.
2. Context-applied and context-source values are assigned from the normalized
   evaluation input, not model output or caller-provided result details.
3. Retry replacement matches the full resolved selector and replaces only the
   failed identity.
4. AI.CLASSIFY, contextual AI.GENERATE, and API fallback expose distinct,
   stable execution provenance.
5. Legacy session-only results remain constructible and serializable.

**Verification**

```bash
uv run pytest -q tests/test_categorical_evaluator.py tests/test_sdk_client.py -k "identity or provenance or context or retry"
```

## Task 2: Migrate the table before enabling the writer

**Files**

- Modify: `src/bigquery_agent_analytics/categorical_evaluator.py`
- Modify: `src/bigquery_agent_analytics/client.py`
- Test: `tests/test_categorical_evaluator.py`
- Test: `tests/test_sdk_client.py`

**Test-first scenarios**

1. Flattened rows include nullable identity fields, canonical `scope_key`,
   deterministic versioned `identity_key`, context provenance, and execution
   mode.
2. Raw expected answers and judge context never appear in flattened rows.
3. Table creation plus every `ADD COLUMN IF NOT EXISTS` migration runs before
   `insert_rows_json`; rerunning the migration is harmless.
4. Contextual persistence, previously gated in U4, succeeds only after the
   schema sequence completes.
5. A schema migration failure performs no insert and records a persistence
   failure without leaking context.

**Verification**

```bash
uv run pytest -q tests/test_categorical_evaluator.py tests/test_sdk_client.py -k "persist or flatten or migration"
```

## Task 3: Make latest-result views migration- and collision-safe

**Files**

- Modify: `src/bigquery_agent_analytics/categorical_views.py`
- Test: `tests/test_categorical_views.py`

**Test-first scenarios**

1. Two post-migration identities sharing a session id remain two latest rows.
2. A legacy-only session deduplicates within `legacy:<session_id>`.
3. When exactly one post-migration identity exists, its latest row supersedes
   legacy rows for that session and prompt version.
4. When multiple post-migration identities exist, the ambiguous legacy lane
   remains separate and never merges into either identity.
5. Prompt-version and metric partitioning remain intact and helper columns do
   not escape the base view.

**Verification**

```bash
uv run pytest -q tests/test_categorical_views.py
```

## Task 4: Bind quality-report golden context by exact selector

**Files**

- Modify: `scripts/quality_report.py`
- Test: `tests/test_quality_report_helpers.py`
- Test: `tests/test_sdk_client.py`

**Test-first scenarios**

1. Two resolved traces sharing a session id independently match golden Q&A and
   produce two selector-keyed contexts.
2. `run_evaluation()` passes those contexts to
   `Client.evaluate_categorical()` with the SDK-defined golden-answer source.
3. JSON/report enrichment attaches each golden match to the corresponding
   identity-attributed result; unattributed collisions fail closed.
4. Legacy unique-session report inputs retain their existing behavior.
5. Persisted categorical rows record only provenance and never golden text.

**Verification**

```bash
uv run pytest -q tests/test_quality_report_helpers.py tests/test_sdk_client.py -k "golden or report or context"
```

## Task 5: Document the migration and verify the full chain

**Files**

- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: relevant API docstrings
- Test: all focused U4/U5 tests

**Content**

- Document the additive schema, rollback behavior, legacy straddle semantics,
  identity-safe report cardinality, and the no-raw-context guarantee.
- State the schema → writer → view deployment order and that historical rows
  are not backfilled.
- Record that U5 satisfies the remaining #358 persistence/report gate and
  unlocks U6/#360.

**Final verification**

```bash
uv run pytest -q tests/test_categorical_evaluator.py tests/test_sdk_client.py tests/test_client_labels.py tests/test_categorical_views.py tests/test_quality_report_helpers.py
uv run pytest -q
uv run autoformat
git diff --check
uv run pytest -q tests/test_trace_identity_bigquery_live.py
```

Before closing #358, run the live collision fixture with persistence enabled
and compare the in-memory result count, inserted-row identity keys, and latest
view cardinality for two identities sharing one session id.
