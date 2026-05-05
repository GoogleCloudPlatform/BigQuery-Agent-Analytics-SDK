# Changelog

All notable changes to `bigquery-agent-analytics` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`bq-agent-sdk binding-validate` CLI** — pre-flight validator that
  checks whether a binding YAML's referenced BigQuery tables
  physically exist with the columns and types the binding requires,
  before extraction wastes ``AI.GENERATE`` tokens. Emits a structured
  JSON report (failures + warnings) and exits 0 / 1 / 2. Supports
  `--strict` to escalate `KEY_COLUMN_NULLABLE` warnings to hard
  failures. See [issue #105](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/105)
  and `docs/ontology/binding-validation.md`.
- **`bq-agent-sdk ontology-build --validate-binding` and
  `--validate-binding-strict`** opt-in flags. Run the binding
  pre-flight before phase 2 (extraction). On any failure, the build
  short-circuits before any `AI.GENERATE` call fires; default-mode
  warnings print to stderr but don't block. The two flags are
  mutually exclusive; both incompatible with the deprecated
  `--spec-path` form because the validator needs the unresolved
  `Ontology` + `Binding` pair.
- **`bq-agent-sdk ontology-build --location`** — BigQuery location
  (e.g. `US`, `EU`) threaded through to `build_ontology_graph()`.
  The Python API has supported `location` since 0.2.3; this adds
  the matching CLI flag.
- **`validate_binding_against_bigquery(...)` Python API** in
  `bigquery_agent_analytics.binding_validation`. Same surface the
  CLI calls: takes `Ontology` + `Binding` + `bq_client`, returns a
  `BindingValidationReport` with `failures` + `warnings` lists and
  an `ok` property. Issue [#105](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/105).
- **`validate_extracted_graph(spec, graph)` Python API** in
  `bigquery_agent_analytics.graph_validation` — ontology-aware
  post-extraction validator that checks an `ExtractedGraph` against
  a `ResolvedGraph`. Returns a `ValidationReport` with typed
  failures classified by `FallbackScope` (`FIELD` / `NODE` /
  `EDGE`) so downstream consumers (notably the compiled-extractor
  runtime in #75) know the smallest safe unit of replacement.
  Twelve failure codes ship: `unknown_entity`, `missing_node_id`,
  `duplicate_node_id`, `missing_key`, `key_mismatch`,
  `unknown_property`, `type_mismatch`, `unsupported_type`,
  `unknown_relationship`, `unresolved_endpoint`,
  `wrong_endpoint_entity`, `missing_endpoint_key`. `EVENT` scope is
  reserved for #75 C2.
  See [issue #76](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/76)
  and `docs/ontology/validation.md`.
- **`validate_extracted_graph_from_ontology(ontology, binding,
  graph)`** — adapter for callers holding upstream
  `Ontology` + `Binding` instead of a `ResolvedGraph`. Resolves
  internally then delegates.
- **Runtime-target decision recorded for compiled structured
  extractors** in
  [`docs/extractor_compilation_runtime_target.md`](docs/extractor_compilation_runtime_target.md).
  Settles issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  P0.2: Phase 1 emits plain Python and runs client-side via the
  existing `run_structured_extractors()` hook in
  `structured_extraction.py:198`. No SQL/UDF translation layer or
  Remote Function deploy surface is taken on for Phase 1; Phase 2
  re-opens the choice for the session-aggregated `AI.GENERATE`
  tier with Option C (SQL / Python UDF) as the primary candidate.
  Unblocks the compile-harness PR.

## [0.2.3] - 2026-04-27

### Fixed

- **LLM-as-Judge AI.GENERATE path now executes against current
  BigQuery.** Earlier versions emitted a table-valued
  ``FROM session_traces, AI.GENERATE(...) AS result`` shape with
  ``output_schema`` and a flat ``model_params`` dict. Current
  ``AI.GENERATE`` is a scalar function that returns a STRUCT;
  the table-valued form raises ``Table-valued function not found``
  and the flat ``model_params`` raises ``does not conform to the
  GenerateContent request body``. Mocked unit tests passed because
  they bypassed real query execution. The SDK now renders a
  ``SELECT AI.GENERATE(...).score, ...`` query with a
  ``generationConfig``-wrapped ``model_params`` and ``output_schema``
  on the scalar form, runs against live BigQuery, and unwraps the
  returned struct's ``score`` / ``justification`` / ``status``
  fields.
- **LLM-as-Judge AI.GENERATE / ML.GENERATE_TEXT now uses the full
  Python prompt template.** Previously both BQ-native paths sent
  only ``prompt_template.split('{trace_text}')[0]`` to BigQuery,
  silently dropping every instruction that followed the
  placeholders — including the per-criterion output-format spec
  the judge model needs to score consistently with the
  API-fallback path. The two BQ paths and the Python API path now
  produce comparable scores against the same prompt.

### Added

- ``evaluators.render_ai_generate_judge_query(...)`` is the new
  entry point that builds the AI.GENERATE batch SQL.
  ``connection_id`` is optional — when omitted the call uses
  end-user credentials; when supplied it inlines the
  ``connection_id =>`` argument so callers can route through a
  service-account-owned connection when their environment
  requires it.
- ``Client.connection_id`` already existed; it is now plumbed
  through to ``_ai_generate_judge`` so a connection set at client
  construction propagates to the judge SQL automatically.
- Live BigQuery integration tests for the LLM-judge AI.GENERATE
  path (``tests/test_ai_generate_judge_live.py``). Skipped by
  default; opt in with ``BQAA_RUN_LIVE_TESTS=1`` plus
  ``PROJECT_ID`` / ``DATASET_ID``. Three tests cover SQL parse
  acceptance, expected result-schema column names, and the
  ``connection_id`` escape hatch when
  ``BQAA_AI_GENERATE_CONNECTION_ID`` is set. Catches the class of
  mock-divergence bug that let the prior broken template ship.
- ``EvaluationReport.details["execution_mode"]`` is now populated
  for LLM-as-Judge runs with one of ``ai_generate``,
  ``ml_generate_text``, ``api_fallback``, or ``no_op`` — matching
  the value space the categorical evaluator already exposes. When
  an earlier tier raised before a later tier succeeded,
  ``details["fallback_reason"]`` carries the chained exception
  messages in attempt order, so CI and dashboards can audit which
  path actually ran.
- ``evaluators.split_judge_prompt_template(prompt_template)`` is
  the helper the SQL paths use to safely substitute the template
  into ``CONCAT()``; exposed publicly for downstream code that
  needs the same shape.
- ``bq-agent-sdk evaluate --exit-code`` FAIL lines now carry a
  bounded ``feedback="…"`` snippet drawn from
  ``SessionScore.llm_feedback`` for LLM-judge failures. The
  snippet collapses internal whitespace to a single space,
  truncates to 120 characters with an ellipsis, and is omitted
  entirely for code-based metrics (which leave ``llm_feedback``
  empty). CI logs now explain *why* the judge said the session
  failed without forcing the reader to chase the JSON output.

### Changed

- ``--strict`` help text and ``SDK.md §4`` clarified to match shipped
  behavior. ``--strict`` is a *visibility* knob — it stamps
  ``details['parse_error']=True`` on AI.GENERATE/ML.GENERATE_TEXT
  judge rows whose ``scores`` dict is empty, and adds a report-level
  ``parse_errors`` counter. It does **not** flip any session's
  pass/fail outcome: both BQ-native judge methods compute ``passed``
  as ``bool(scores) and all(...)``, so empty-scores rows already
  fail without the flag. API-fallback parse errors coerce to
  ``score=0.0``, so they fail as low-score failures rather than
  parse errors. For pass/fail-only CI consumers ``--strict`` is a
  no-op; reach for it when a dashboard needs to tell "no parseable
  score" apart from "low score."

## [0.2.2] - 2026-04-24

### Changed (breaking)

- **Prebuilt `CodeEvaluator` gates now compare raw observed values
  directly against the user-supplied budget.** `CodeEvaluator.latency`,
  `.turn_count`, `.error_rate`, `.token_efficiency`, `.ttft`, and
  `.cost_per_session` return `1.0` when the observed metric is within
  budget and `0.0` otherwise. The previous implementation scored sessions
  on a normalized `1.0 - (observed / budget)` scale against a `0.5` pass
  cutoff, which effectively fired every gate at roughly half the budget
  the user typed (e.g. `latency(threshold_ms=5000)` failed sessions at
  `avg_latency_ms > 2500`). Users relying on the old sub-budget fail
  behavior should lower their budgets to match their intent.
- The scheduled streaming evaluator (`streaming_observability_v1`) uses
  the same raw-budget gate semantics for consistency with the prebuilt
  `CodeEvaluator` factories.

### Added

- `CodeEvaluator.add_metric` accepts `observed_key`, `observed_fn`, and
  `budget` arguments that flow into `SessionScore.details[f"metric_{name}"]`
  for downstream reporting. The CLI uses these to emit readable failure
  lines without re-running the scorer.
- `bq-agent-sdk evaluate --exit-code` now prints a per-session failure
  summary on stderr before exiting non-zero. Each line names the
  session_id, failing metric, observed value, and the budget it blew
  through. Output is capped at the first 10 failing sessions to keep
  CI logs scannable.
- `bq-agent-sdk categorical-eval` gains `--exit-code`,
  `--min-pass-rate`, and `--pass-category METRIC=CATEGORY`
  (repeatable) flags. Declare which classification counts as passing
  per metric, set a minimum pass rate across the run, and fail CI when
  any metric falls below it. Multiple pass categories per metric are
  OR'd together (e.g. `--pass-category tone=positive --pass-category
  tone=neutral`). Missing metric names warn on stderr without failing
  the run so configuration mistakes are visible in CI logs.

## [0.2.1]

- See `git log` for prior changes.
