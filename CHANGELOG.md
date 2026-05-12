# Changelog

All notable changes to `bigquery-agent-analytics` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Ontology runtime reader** in
  ``bigquery_agent_analytics.ontology_runtime`` and
  [`docs/ontology_runtime_reader.md`](docs/ontology_runtime_reader.md).
  Issue [#58](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/58)
  reader follow-on to PR #92's concept-index emission.
  Public surface:
  * ``OntologyRuntime`` — façade that loads
    ``Ontology + Binding`` (from YAML files or in-memory
    models) plus an optional :class:`ConceptIndexLookup`
    wired to the emitted BigQuery table. Read-only
    accessors over entities / relationships / synonyms /
    annotations / SKOS schemes / notations / labels;
    provenance properties (``compile_fingerprint`` /
    ``compile_id``) computed locally.
  * ``EntityResolver`` Protocol + two reference
    implementations: ``ExactEntityResolver`` (in-memory
    match on ``entity_name``, no BQ roundtrip) and
    ``LabelSynonymResolver`` (BQ-backed match against the
    concept-index ``label`` / ``synonym`` / ``notation``
    rows, re-ranked by label-kind priority
    ``name > pref > alt > hidden > synonym > notation``).
    **No embedding / LLM / fuzzy in this slice** — explicit
    non-goals; future PRs can implement the Protocol
    without touching the runtime surface.
  * ``ConceptIndexLookup`` — BigQuery-backed accessor that
    is **fingerprint-strict**. Three trust points: eager
    ``verify()`` at construction (compares the table's
    ``__meta`` row against the locally-computed
    ``compile_fingerprint(ontology_fp, binding_fp,
    compiler_version)``); explicit ``verify()`` method for
    re-checks before long batches; per-query
    ``WHERE compile_fingerprint = @expected_fp`` as defense
    in depth so stale rows can't surface even mid-flight.
    Three lookup methods: ``lookup_by_label`` (with
    label-kind / language / case-insensitive filters),
    ``lookup_by_entity_name``, ``lookup_by_notation``.
    Stable failure codes: ``FingerprintMismatchError``,
    ``MetaTableMissingError``, ``MetaTableEmptyError``,
    ``MetaTableMultipleRowsError`` — all subclass
    ``ConceptIndexError``.
  CI suite (55 cases) uses in-memory fake BQ clients;
  live test (gated behind ``BQAA_RUN_LIVE_ONTOLOGY_RUNTIME_TESTS=1``)
  emits a real concept-index via PR #92's path, attaches
  the runtime, runs resolver queries against the live
  table, asserts provenance, drops the tables on the way
  out. Closes the last feature dependency for #107's
  four-guarantee notebook (the resolve beat).
- **Compiled-extractor rollout guide** at
  [`docs/extractor_compilation_rollout_guide.md`](docs/extractor_compilation_rollout_guide.md).
  Operational playbook for the Phase C pipeline (issue
  [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75))
  stitching the five stages — Compile, Publish, Sync, Wire,
  Revalidate — into one flow. Treats Publish/Sync as the
  remote-runtime path; local / co-located deployments can
  shortcut to Compile → Wire → Revalidate. Worked BKA
  example uses Python snippets for the non-CLI stages
  (``measure_compile``, ``publish_bundles_to_bq``,
  ``sync_bundles_from_bq``,
  ``OntologyGraphManager.from_bundles_root``) and the real
  ``bqaa-revalidate-extractors`` shell invocation only
  where a CLI actually exists. Documents the **four trust
  gates** across the pipeline — the compile-time smoke gate
  inside ``compile_extractor`` (``load_callable_from_source``
  + ``run_smoke_test``, not ``load_bundle`` itself: there's
  no manifest at compile time) plus three real
  ``load_bundle`` runs at pre-publish, post-sync, and
  runtime-startup discovery — so the trust model is one
  mental model across the pipeline.
  Includes a failure-recovery playbook keyed on the stable
  failure codes each stage emits.
- **``--events-bq-query-file`` for ``bqaa-revalidate-extractors``**
  (issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  CLI follow-up). The CLI now accepts a BigQuery event
  source in addition to ``--events-jsonl``; the two are
  mutually exclusive and exactly one must be supplied.
  Contract: the SQL must produce exactly one column named
  ``event_json`` (STRING) per row, containing a JSON-encoded
  event dict — same shape ``--events-jsonl`` consumes
  line-by-line. The CLI does NOT auto-shape
  ``bigquery.Row`` objects; the query writer controls
  projection via ``TO_JSON_STRING(STRUCT(...))``. Row-level
  errors (missing column, non-string value, malformed JSON,
  non-dict decode) surface as exit 2 with the 0-based row
  index named so an operator can find the offender with
  ``LIMIT N OFFSET row_index``. BigQuery-side exceptions
  (auth, syntax, table-not-found, permission) are caught and
  surfaced with type + message — no traceback escapes.
  ``--bq-project`` is optional: the BigQuery client falls
  back to Application Default Credentials / environment for
  project inference; if both are absent the CLI exits 2 with
  ``Set --bq-project explicitly`` rather than confusing the
  operator with a downstream API error. ``--bq-location``
  defaults to ``US``. Client construction is centralized
  behind ``_make_bq_client(project, location)`` so unit
  tests inject in-memory fakes via ``monkeypatch.setattr``
  rather than wiring through every call site. CI tests
  (11 new cases) cover the happy path, ADC inference,
  no-project-anywhere, query exceptions, every row-shape
  failure mode, the mutex on event sources (both / neither),
  and the empty-SQL-file edge. Live BQ test
  (``tests/test_extractor_compilation_cli_revalidate_bq_live.py``)
  is gated behind ``BQAA_RUN_LIVE_BQ_REVALIDATE_TESTS=1``;
  it creates a temp table, inserts two ``event_json`` rows,
  runs the CLI, asserts the report is written with both
  events as compiled_unchanged + parity_matches, deletes
  the table on the way out.
- **``bqaa-revalidate-extractors`` CLI** in
  `bigquery_agent_analytics.extractor_compilation.cli_revalidate`
  and
  [`docs/extractor_compilation_revalidate_cli.md`](docs/extractor_compilation_revalidate_cli.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  follow-up to Milestone C2.d — operationalizes
  ``revalidate_compiled_extractors`` so ops can run periodic
  revalidation without writing Python. **Local inputs
  only** this round; ``--events-bq-query`` lands in a
  follow-up so the auth/location/pagination surface gets
  isolated from the operational-loop contract.
  Flags: ``--bundles-root`` (auto-detects the fingerprint
  from the first bundle's manifest; mixed fingerprints
  fail-closed), ``--events-jsonl`` (one event per line,
  malformed lines abort with line number), ``--reference-
  extractors-module`` (dotted path; module exposes
  ``EXTRACTORS: dict[str, callable]``, ``RESOLVED_GRAPH``
  from ``resolve(ontology, binding)``, and optionally
  ``SPEC`` — the CLI carries no ontology/binding flags
  because the reference module owns the validator-input
  contract), ``--thresholds-json`` (optional; JSON object
  with any subset of ``RevalidationThresholds`` fields,
  bounds-checked via the existing ``__post_init__``),
  ``--report-out`` (combined JSON of the raw
  ``RevalidationReport`` plus the ``ThresholdCheckResult``).
  Exit codes are deliberately narrow: ``0`` pass / ``1``
  threshold violation (report still written) / ``2``
  usage-or-input error (report not written). Wired through
  ``pyproject.toml [project.scripts]`` so
  ``pip install`` exposes the binary.
- **BigQuery-table bundle mirror** in
  `bigquery_agent_analytics.extractor_compilation.bq_bundle_mirror`
  and
  [`docs/extractor_compilation_bq_bundle_mirror.md`](docs/extractor_compilation_bq_bundle_mirror.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR C2.c.3 — publishes compiled bundles to a BigQuery
  table and syncs them back into a local directory for
  C2.a's existing loader. Runtime path stays
  ``sync_bundles_from_bq → discover_bundles →
  from_bundles_root``; the mirror is a utility, not a
  runtime loader. Public surface:
  ``publish_bundles_to_bq(bundle_root, store,
  bundle_fingerprint_allowlist=None)`` and
  ``sync_bundles_from_bq(store, dest_dir,
  bundle_fingerprint_allowlist=None)``. Both call
  :func:`load_bundle` as a gate — publish refuses bundles
  that wouldn't load at the runtime; sync refuses bundles
  whose reconstruction the loader rejects, scrubbing any
  partial directory it wrote. Sync writes each
  fingerprint to a side-by-side **staging directory** and
  runs ``load_bundle`` on the staged copy before performing
  a **staged replace** of the target (the rmtree+move pair
  is not strictly atomic — a crash between the two leaves
  the bundle absent on disk, recoverable by re-sync — but
  the load-bundle-failure direction *is* atomic, so a bad
  mirror row never destroys a previously-good local
  bundle).
  Strict bundle-shape check: the table stores exactly two
  rows per fingerprint (``manifest.json`` + the manifest's
  ``module_filename``); ``unexpected_file`` codes reject
  anything else. The manifest's own ``module_filename`` is
  shape-checked at sync (bare filename — no separators, no
  ``..``, no NUL); a path-separator value surfaces as
  ``manifest_row_unreadable`` instead of raising
  ``FileNotFoundError`` at the write step.
  ``invalid_bundle_path`` rejects traversal / absolute /
  backslash / NUL paths before writing to disk.
  ``duplicate_row`` rejects two rows sharing the same
  ``(fingerprint, bundle_path)`` (BigQuery has no unique
  constraint; the mirror enforces uniqueness at sync).
  ``duplicate_fingerprint`` rejects publish-side cases
  where two subdirs of ``bundle_root`` claim the same
  manifest fingerprint — neither is published, so the
  table can't end up with logical duplicates.
  ``malformed_row`` rejects rows with wrong field types.
  Idempotent republish via DELETE+INSERT in
  ``BigQueryBundleStore.publish_rows`` —
  re-publishing the same fingerprint replaces the prior
  rows rather than accumulating duplicates. The DELETE +
  ``insert_rows_json`` are NOT a single atomic
  transaction; a transient INSERT failure leaves rows
  missing until the caller re-runs publish (recoverable;
  documented in the class docstring).
  ``publish_rows`` also raises ``ValueError`` on duplicate
  ``(fingerprint, bundle_path)`` input pairs as defense in
  depth.
  ``BundleStore`` is a Protocol so tests can pass in-memory
  fakes; ``BigQueryBundleStore`` is the concrete
  implementation wrapping ``google.cloud.bigquery``.
  ``BUNDLE_MIRROR_TABLE_SCHEMA`` is exported for callers
  who need to create the table themselves (or
  ``BigQueryBundleStore.ensure_table()`` does it
  idempotently). Failure codes are stable strings;
  per-bundle problems land in ``failures`` instead of
  raising. Store exceptions (BQ-side: network, auth, table
  missing) propagate. Out of scope: GCS-backed signed-URL
  fetch, caching / TTL, garbage collection, multi-region
  replication.
- **Revalidation harness for compiled structured extractors**
  in
  `bigquery_agent_analytics.extractor_compilation.revalidation`
  and
  [`docs/extractor_compilation_revalidation.md`](docs/extractor_compilation_revalidation.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR C2.d — turns the compiled path from "works in tests" into
  "keeps proving itself after rollout." Public surface:
  ``revalidate_compiled_extractors(events,
  compiled_extractors, reference_extractors, resolved_graph,
  ...)`` drives ``run_with_fallback`` (with a no-op fallback)
  over a batch of events AND calls the reference extractor
  directly, aggregating the per-event outcomes into a
  ``RevalidationReport`` with **two orthogonal dimensions**:
  (1) runtime decision — per-event-type ``EventTypeCounts``
  plus totals for ``compiled_unchanged`` /
  ``compiled_filtered`` / ``fallback_for_event``, with
  ``compiled_path_faults`` split out so bundle bugs (the
  wrapper's ``compiled_exception`` audit field covers
  exceptions, wrong return type, and malformed result
  internals) are distinguishable from ontology drift; (2)
  agreement against reference — ``parity_matches`` /
  ``parity_divergences`` / ``parity_not_checked`` using a
  three-comparator parity check: ``_compare_nodes`` and
  ``_compare_span_handling`` from ``measurement.py`` plus
  ``_compare_edges`` in ``revalidation.py`` (same edge_id
  set with matching relationship_name / endpoints / property-
  set per shared edge; duplicate edge_ids on either side
  reported as a divergence rather than silently collapsed by
  dict keying, since #76 doesn't enforce edge-id
  uniqueness). The parity dimension catches **schema-valid
  but semantically wrong** outputs the validator would
  silently accept. **Every failure mode on the reference
  side becomes a parity divergence, never a batch abort**:
  exceptions, non-``StructuredExtractionResult`` returns
  (including ``None``), and comparator crashes all funnel
  into the divergence channel with a descriptive string. Headline KPIs:
  ``compiled_unchanged_rate`` (schema safety) and
  ``parity_match_rate`` (semantic agreement; denominator
  excludes ``parity_not_checked`` so wrapper-filtered events
  don't conflate with wrong-output events). Sample
  divergences are capped (per-dimension, independently) at 10
  by default. Skipped events (event_types without a compiled
  or reference extractor, malformed events) are counted
  separately from the rate denominators.
  ``check_thresholds(report, RevalidationThresholds(...))``
  evaluates the same report against policy gates;
  ``RevalidationThresholds`` validates rates are in
  ``[0, 1]`` (and rejects NaN / bool) at construction so a
  typo like ``max_fallback_for_event_rate=5`` fails loud
  instead of silently disabling the gate. Multiple
  thresholds all evaluated (no short-circuit), violations as
  human-readable strings naming the failed rate and bound.
  ``RevalidationReport.to_json()`` is deterministic for
  persistence + cross-run diffing. Out of scope (deferred):
  scheduled / cron orchestration, BigQuery / disk
  persistence, CLI binary, sampling strategy, auto-fix
  workflows.
- **Orchestrator call-site swap for compiled structured
  extractors** in
  `bigquery_agent_analytics.ontology_graph.OntologyGraphManager`
  and
  [`docs/extractor_compilation_orchestrator_swap.md`](docs/extractor_compilation_orchestrator_swap.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR C2.c.2 — the actual call-site swap that puts compiled
  extractors on the runtime path. New classmethod
  ``OntologyGraphManager.from_bundles_root(*, project_id,
  dataset_id, ontology, binding, bundles_root,
  expected_fingerprint, fallback_extractors, ...)`` builds the
  C2.c.1 registry adapter internally and constructs a manager
  whose ``extractors`` dict is the wrapped registry — so the
  existing ``run_structured_extractors`` call inside
  ``extract_graph`` picks up compiled-with-fallback behavior
  automatically. Manager exposes ``manager.runtime_registry:
  WrappedRegistry | None`` as the audit handle (non-``None``
  when bundle-wired; cross-reference
  ``runtime_registry.bundles_without_fallback`` /
  ``fallbacks_without_bundle`` /  ``discovery.failures`` for
  rollout-coverage telemetry). The existing ``__init__`` and
  ``from_ontology_binding`` paths are unchanged — direct-
  constructor callers leave ``runtime_registry = None`` and
  back-compat holds by construction. Compiled-only event_types
  without a matching fallback are NOT registered (fail-closed
  per C2.b's safety contract). Out of scope (deferred): BQ-
  table mirror (C2.c.3), revalidation harness (C2.d),
  ``AI.GENERATE`` fallback adapter.
- **Runtime extractor-registry adapter for compiled structured
  extractors** in
  `bigquery_agent_analytics.extractor_compilation.runtime_registry`
  and
  [`docs/extractor_compilation_runtime_registry.md`](docs/extractor_compilation_runtime_registry.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR C2.c.1 — the adapter that glues C2.a's
  ``discover_bundles`` and C2.b's ``run_with_fallback`` into one
  call. Public surface:
  ``build_runtime_extractor_registry(*, bundles_root,
  expected_fingerprint, fallback_extractors, resolved_graph,
  event_type_allowlist, on_outcome)`` returning
  ``WrappedRegistry(extractors, discovery,
  bundles_without_fallback, fallbacks_without_bundle)``. The
  ``extractors`` dict is ready to pass straight into the
  existing ``run_structured_extractors`` hook. Wiring matrix:
  compiled+fallback → wrapped closure that calls
  ``run_with_fallback``; fallback-only → original callable
  registered unchanged; **compiled-only → skipped and recorded
  in ``bundles_without_fallback``** (C2's safety contract
  requires a fallback; fail-closed default). The inverse
  ``fallbacks_without_bundle`` audit surface records every
  event_type whose fallback has *no usable compiled registry
  entry* — that includes "bundle never built" and "bundle
  exists but discovery rejected it" (fingerprint mismatch,
  collision, ``manifest_unreadable``); rollout telemetry that
  wants to distinguish those cases should cross-reference
  ``discovery.failures``. ``fallback_extractors`` values are
  validated to be callable at build time (rejects ``None`` /
  non-callable with a ``TypeError`` naming the offending
  event_type) so misconfiguration surfaces immediately rather
  than silently in ``run_structured_extractors``.
  ``event_type_allowlist`` filters both candidate pools. The
  ``on_outcome`` callback fires on every wrapped invocation
  including ``compiled_unchanged`` (denominator metric for
  compiled-vs-fallback rate analysis); callback exceptions
  propagate. **This PR ships the adapter, not the orchestrator
  call-site swap** — that's C2.c.2. Out of scope (deferred):
  BQ mirror (C2.c.3), revalidation harness (C2.d),
  ``AI.GENERATE`` fallback adapter.
- **Runtime fallback wiring for compiled structured extractors**
  in
  `bigquery_agent_analytics.extractor_compilation.runtime_fallback`
  and
  [`docs/extractor_compilation_runtime_fallback.md`](docs/extractor_compilation_runtime_fallback.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR C2.b — the runtime safety net for compiled extractors.
  Public surface: ``run_with_fallback(*, event, spec,
  resolved_graph, compiled_extractor, fallback_extractor)``
  returning ``FallbackOutcome`` with ``decision`` ∈
  ``{\"compiled_unchanged\", \"compiled_filtered\",
  \"fallback_for_event\"}``. Validates the compiled extractor's
  output via #76's ``validate_extracted_graph`` and routes by
  failure scope: per-element failures (NODE / EDGE / FIELD with
  pinpointable ``node_id`` / ``edge_id``) drop the offending
  elements with orphan cleanup AND downgrade the event's
  ``span_id`` from ``fully_handled_span_ids`` to
  ``partially_handled_span_ids`` so the AI transcript still sees
  the source span and can recover the dropped facts. EVENT-
  scope failures, compiled-extractor exceptions, wrong return
  types, and unpinpointable failures all trigger
  ``fallback_for_event``. The wrapper does not validate the
  fallback output and does not catch fallback exceptions —
  fallback is the trusted runtime baseline (handwritten
  extractor or ``AI.GENERATE``). Out of scope (deferred to
  C2.c/d): orchestrator call-site swap, BQ-table mirror,
  revalidation harness.
- **Bundle loader + minimal runtime discovery for compiled
  structured extractors** in
  `bigquery_agent_analytics.extractor_compilation.bundle_loader`
  and
  [`docs/extractor_compilation_bundle_loader.md`](docs/extractor_compilation_bundle_loader.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR C2.a — the trust boundary between on-disk compiled bundles
  and the runtime that's about to import + execute them. Public
  surface: ``load_bundle(bundle_dir, *, expected_fingerprint,
  expected_event_types)`` and ``discover_bundles(parent_dir, *,
  expected_fingerprint, event_type_allowlist)`` returning
  ``LoadedBundle`` / ``LoadFailure`` / ``DiscoveryResult``.
  Stable ``LoadFailure`` codes — ``manifest_missing`` /
  ``manifest_unreadable`` / ``fingerprint_mismatch`` /
  ``event_types_mismatch`` / ``module_not_found`` /
  ``import_failed`` (catches both ``Exception`` and
  ``BaseException`` so a malicious or buggy bundle can't tear
  down the loading process) / ``function_not_found`` /
  ``function_signature_mismatch`` / ``event_type_collision``.
  The loader never raises; every failure surfaces as a
  structured record. The fingerprint check runs *before* module
  import, so a bundle with a wrong fingerprint can't side-effect
  via a broken module. Multi-event bundles register the same
  callable under each declared event_type. Discovery fails
  closed on event-type collisions: dropped from the registry,
  one ``LoadFailure`` per claimant, other event_types from the
  same bundles still register if unique. Out of scope (deferred
  to C2.b/c/d): per-field/node/edge fallback through #76's
  validator, BQ-table mirror, ontology-graph call-site swap,
  revalidation harness.
- **Compile-and-measure utility + BKA-decision end-to-end proof**
  in `bigquery_agent_analytics.extractor_compilation.measurement`
  and
  [`docs/extractor_compilation_bka_measurement.md`](docs/extractor_compilation_bka_measurement.md).
  Issue [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
  PR 4c — wraps ``compile_with_llm`` with a parity check against
  a known-good *reference* extractor. Public surface:
  ``measure_compile(...)`` returning ``CompileMeasurement`` (a
  JSON-serializable record covering loop outcome, bundle
  fingerprint, per-attempt failure codes, per-axis parity counts,
  and audit fields like model_name / source / sample_session_ids
  / captured_at). Loop failure is captured in the record rather
  than raised — callers route on ``ok`` / ``parity_ok``. The
  first concrete consumer is ``extract_bka_decision_event``;
  ``measure_compile`` itself is generic so future extractor
  baselines can reuse the parity logic. CI path (deterministic
  fake LLM client) is merge-blocking and runs without an API key;
  gated live path
  (``BQAA_RUN_LIVE_TESTS=1`` + ``BQAA_RUN_LIVE_LLM_COMPILE_TESTS=1``)
  exercises the same pipeline against real ``agent_events`` rows
  and a real Gemini model, regenerating the checked-in
  measurement artifact at
  ``tests/fixtures_extractor_compilation/bka_decision_measurement_report.json``.
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
