# Changelog

All notable changes to `bigquery-agent-analytics` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
