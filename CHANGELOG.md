# Changelog

All notable changes to `bigquery-agent-analytics` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **LLM-as-Judge AI.GENERATE / ML.GENERATE_TEXT now uses the full Python
  prompt template.** Previously both BQ-native paths sent only
  ``prompt_template.split('{trace_text}')[0]`` to BigQuery, silently
  dropping every instruction that followed the placeholders — including
  the per-criterion output-format spec the judge model needs to score
  consistently with the API-fallback path. The two BQ paths and the
  Python API path now produce comparable scores against the same prompt.

### Added

- ``EvaluationReport.details["execution_mode"]`` is now populated for
  LLM-as-Judge runs with one of ``ai_generate``, ``ml_generate_text``,
  ``api_fallback``, or ``no_op`` — matching the value space the
  categorical evaluator already exposes. When an earlier tier raised
  before a later tier succeeded, ``details["fallback_reason"]`` carries
  the chained exception messages in attempt order, so CI and dashboards
  can audit which path actually ran.
- ``evaluators.split_judge_prompt_template(prompt_template)`` is the
  helper the SQL paths use to safely substitute the template into
  ``CONCAT()``; exposed publicly for downstream code that needs the
  same shape.
- ``bq-agent-sdk evaluate --exit-code`` FAIL lines now carry a bounded
  ``feedback="…"`` snippet drawn from ``SessionScore.llm_feedback`` for
  LLM-judge failures. The snippet collapses internal whitespace to a
  single space, truncates to 120 characters with an ellipsis, and is
  omitted entirely for code-based metrics (which leave
  ``llm_feedback`` empty). CI logs now explain *why* the judge said
  the session failed without forcing the reader to chase the JSON
  output.

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
