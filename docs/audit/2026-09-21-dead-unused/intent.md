# Intent — nightly audit: dead / unused code (2026-09-21)

Baseline: `614826bb3515a1d4226ad7b456aa2273f93c3f5c` (main, v0.5.3).

Remove code in `src/bigquery_agent_analytics` that nothing references:
private helpers with zero call sites and imports that are never used in
the importing module. No behavior change, no public API change.
