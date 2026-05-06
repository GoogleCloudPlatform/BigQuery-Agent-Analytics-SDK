# Changelog

All notable changes to `bigquery-agent-analytics` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Retry-on-gate-failure orchestrator for compiled structured
  extractors** in
  `bigquery_agent_analytics.extractor_compilation.retry_loop` and
  [`docs/extractor_compilation_retry_loop.md`](docs/extractor_compilation_retry_loop.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR 4b.2.2.c.2 — wires the resolver prompt + parser + renderer
  + ``compile_extractor`` + diagnostic builders into a single
  loop that retries the LLM with structured feedback on every
  gate failure. Public surface:
  ``compile_with_llm(extraction_rule, event_schema, llm_client,
  compile_source, max_attempts)`` returning
  ``RetryCompileResult`` (``ok`` / ``manifest`` / ``bundle_dir``
  / ``attempts`` / ``reason``); ``AttemptRecord`` with one
  failure channel populated per failed iteration
  (``plan_parse_error`` / ``render_error`` / ``compile_result``)
  so telemetry can route on field name; ``build_retry_prompt(*,
  original_prompt, prior_response, diagnostic)`` as the pure
  prompt-stitching function. ``max_attempts=1`` runs once with
  no retry; values below 1 raise ``ValueError``. LLM-client
  exceptions (auth / quota / network) propagate unchanged so the
  loop never silently retries non-gate failures.
  ``compile_source`` is a caller-supplied closure
  ``(plan, source) -> CompileResult`` that wraps
  ``compile_extractor`` with the per-call inputs (sample events,
  spec, parent bundle dir, fingerprint inputs, etc.) — keeps the
  loop signature narrow and makes the loop trivially testable
  with stubs. End-to-end test in
  ``tests/test_extractor_compilation_retry_loop.py`` wires the
  real ``compile_extractor`` through the loop to prove the
  parser/renderer/compiler stack lines up.
- **Diagnostic builders for compiled-extractor retry feedback** in
  `bigquery_agent_analytics.extractor_compilation.diagnostics` and
  [`docs/extractor_compilation_diagnostics.md`](docs/extractor_compilation_diagnostics.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR 4b.2.2.c.1 — turns each compile-gate failure into a string
  the LLM can act on, ready for embedding in retry prompts.
  Public surface: ``build_plan_parse_diagnostic(error)`` /
  ``build_ast_diagnostic(report)`` / ``build_smoke_diagnostic(
  report)`` /
  ``build_compile_result_diagnostic(result)`` plus a
  ``build_gate_diagnostic(kind, payload)`` dispatcher
  (``kind ∈ {"parse", "ast", "smoke", "compile"}``).
  ``build_compile_result_diagnostic`` covers the top-level
  ``CompileResult`` envelope, including the
  ``invalid_identifier`` / ``invalid_event_types`` /
  ``load_error`` failure modes that don't surface through any
  single gate's report (e.g., the LLM emits a structurally-valid
  plan with the wrong ``event_type`` — parser and AST pass, but
  ``compile_extractor`` rejects it for missing sample coverage).
  Output is **actionable** (each per-failure entry carries the
  stable failure ``code`` plus a dotted ``path`` or source line
  so the LLM can grep its own response), **bounded** (each
  section capped at the first ten entries with a truncation
  summary; multi-line tracebacks reduced to their last
  informative line), and **deterministic** (same input report →
  byte-identical output). PR 4b.2.2.c.2 uses these to build
  retry prompts; this PR ships the diagnostic format on its own
  so the wording can be locked down before the retry loop
  depends on it.
- **LLM-driven plan resolver for compiled structured extractors**
  in
  `bigquery_agent_analytics.extractor_compilation.plan_resolver`
  and
  [`docs/extractor_compilation_plan_resolver.md`](docs/extractor_compilation_plan_resolver.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR 4b.2.2.b — wraps an injectable LLM client to map a raw
  ``(extraction_rule, event_schema)`` pair into a
  ``ResolvedExtractorPlan``. Public surface:
  ``LLMClient`` ``Protocol`` with one method
  (``generate_json(prompt, schema) -> dict``);
  ``build_resolution_prompt(rule, schema)`` producing the
  deterministic prompt (sort-keyed JSON throughout, embeds the
  exported JSON Schema, instructs the LLM to use only paths that
  exist in the schema, use Python-identifier-shaped names, omit
  uncertain optional fields rather than invent them);
  ``PlanResolver(llm_client).resolve(rule, schema)`` doing
  prompt → LLM call → ``parse_resolved_extractor_plan_json``.
  ``PlanParseError`` and any exception the LLM client raises
  propagate unchanged so PR 4b.2.2.c can layer typed retry on
  top. **Adapter-free** — no ``google-genai`` import; concrete
  provider adapters and retry orchestration land in PR 4b.2.2.c
  / PR 4c. Tests use fake ``LLMClient`` implementations with
  pre-canned responses; no real LLM calls.
- **JSON-to-plan parser for compiled structured extractors** in
  `bigquery_agent_analytics.extractor_compilation.plan_parser` and
  [`docs/extractor_compilation_plan_parser.md`](docs/extractor_compilation_plan_parser.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR 4b.2.2.a — turns a JSON payload (string or already-parsed
  dict) into a ``ResolvedExtractorPlan`` ready for 4b.2.1's
  ``render_extractor_source``. Public surface:
  ``parse_resolved_extractor_plan_json(payload)`` returning a
  validated plan, plus ``PlanParseError`` carrying a stable
  ``code``, dotted ``path``, and human-readable ``message``.
  Stable failure codes: ``invalid_json``, ``wrong_root_type``,
  ``missing_required_field``, ``unknown_field``, ``wrong_type``,
  ``empty_string``, ``empty_path``, ``invalid_identifier``,
  ``duplicate_property_name``, ``invalid_plan``. Also exports
  ``RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA`` — a Draft-2020-12 JSON
  Schema dict with ``additionalProperties: false`` that PR
  4b.2.2.b will hand directly to the LLM client's structured-
  output mode (Gemini's ``response_schema``, etc.) so the LLM is
  constrained to emit *structurally valid* JSON. (Schema-passing
  payloads can still fail parser semantic checks — Python-
  identifier shape, function-name keyword exclusion, duplicate
  property names — which aren't expressible in plain JSON Schema
  and stay parser-only.)
  **No LLM call lives here** — the parser is the deterministic
  boundary every LLM-emitted plan must cross. PR 4b.2.2.b owns
  the prompt and the LLM step that produces this JSON. Locked
  down by a golden BKA fixture
  (``tests/fixtures_extractor_compilation/plan_bka_decision.json``)
  whose parsed plan renders + compiles end-to-end through 4b.2.1
  + 4b.1, plus 38 schema and semantic rejection cases and 8
  schema-conformance cases (55 total).
- **Deterministic source generator for compiled structured
  extractors** in
  `bigquery_agent_analytics.extractor_compilation.template_renderer`
  and
  [`docs/extractor_compilation_template_renderer.md`](docs/extractor_compilation_template_renderer.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR 4b.2.1 — turns a pre-resolved
  ``ResolvedExtractorPlan`` into a Python source string that 4b.1's
  ``compile_extractor`` runs through every gate (AST allowlist,
  smoke runner, #76 validator). Public surface:
  ``FieldMapping`` / ``SpanHandlingRule`` /
  ``ResolvedExtractorPlan`` dataclasses + ``render_extractor_source(plan)
  -> str``. The renderer is the deterministic boundary the LLM
  step in PR 4b.2.2 will plug into; **no LLM call lives here**.
  Generated source carries a top-of-function ``event_type``
  guard that returns an empty result when the incoming event
  doesn't match the plan's declared type, layered with the
  orchestrator's manifest-driven dispatch so a plan/manifest
  mismatch can't silently attach an extractor to the wrong
  event type. Output otherwise matches
  ``extract_bka_decision_event``'s runtime behavior on the BKA
  fixture's sample events. Exercised end-to-end by 39 unit
  tests covering plan validation, the AST gate, the subprocess
  smoke runner, plan-shape variations (no property fields, no
  span handling, single-step paths, deep traversal paths,
  non-dict intermediates at every depth-3 traversal site), and
  wrong-event-type rejection.
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
- **Compile-time scaffolding for structured-extractor compilation**
  in `bigquery_agent_analytics.extractor_compilation` and
  [`docs/extractor_compilation_scaffolding.md`](docs/extractor_compilation_scaffolding.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR 4b.1 — the deterministic contract layer the LLM-driven
  template fill (PR 4b.2) plugs into. Public surface:
  `compute_fingerprint(...)` over the #75 input tuple,
  `Manifest` with JSON round-trip, `validate_source(...)` returning
  an `AstReport` with stable failure codes (`syntax_error`,
  `disallowed_import`, `disallowed_name`, `disallowed_attribute`,
  `disallowed_async`, `disallowed_generator`, `disallowed_class`,
  `disallowed_scope`, `disallowed_decorator`, `disallowed_default`,
  `disallowed_while`, `disallowed_for_iter`, `disallowed_raise`,
  `disallowed_try`, `disallowed_with`, `disallowed_match`,
  `disallowed_call`, `disallowed_method`, `disallowed_lambda`,
  `disallowed_shadowing`, `top_level_side_effect`) — per-module symbol
  allowlist, no `import x`, no wildcards, no dunder aliases, no
  decorators, no non-constant defaults, no halt/escape constructs.
  `run_smoke_test(...)` returning a `SmokeTestReport` gated on the
  #76 `validate_extracted_graph` validator plus return-shape
  checks (catches `BaseException`, rejects wrong return types,
  requires at least one non-empty result by default).
  `compile_extractor(...) -> CompileResult` runs the end-to-end
  pipeline through a sibling staging directory and atomically
  replaces the target on success — failed re-compiles leave any
  pre-existing valid bundle untouched, and a second compile on
  identical inputs is a cache hit (`result.cache_hit is True`,
  no rewrite). `module_name` / `function_name` are validated as
  Python identifiers up front, so path-traversal-shaped names
  fail before the harness touches the filesystem. **No LLM call
  lives here** — that's PR 4b.2. Runtime loader / orchestrator
  integration is deferred to C2 per the runtime-target RFC.
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
