# AI coding telemetry, in our own warehouse — demo summary

<!-- Regenerate per run from evidence/; aggregates and hashes only (see
     evidence/template.md redaction rules). Fields marked <…> are filled
     by scripts/run_queries.sh from this run's SQL outputs. -->

**What we showed.** Claude Code and Codex telemetry from real developer
sessions, landing in a BigQuery dataset **we own**, queryable with plain
SQL, with privacy enforced by configuration.

**Architecture (one line).** Developer machines export OpenTelemetry →
an authenticated Cloud Run receiver → Pub/Sub (with a retained dead-letter
queue) → BigQuery — every component inside our own GCP project; no
third-party analytics service in the path.

**How much work it was.** One command deployed the pipeline
(`bqaa-otel bootstrap … --execute`); two generated config artifacts enabled
the sources. Fresh-project install: ≤30 minutes including the container
build.

## Numbers from this run (`DEMO_RUN_ID: <run_id>`, window: <n>h)

| Question | Result |
|---|---|
| Products reporting | `claude_code`, `codex` — one schema, provenance-tagged |
| Sessions / conversations | <claude_sessions> Claude, <codex_conversations> Codex |
| Distinct users (hashed) | <distinct_users_hashed> |
| Tokens (measured) | <total_tokens> — est. $<est_cost_usd> *(estimate; editable rates, as-of <rate_as_of>)* |
| Agent latency | <span_name> p95 = <p95_ms> ms (from real spans) |
| Prompt content in warehouse | **PASS — none found** (exact-string scan of all content-bearing columns; baseline tier) |
| Replay without explicit acknowledgement | **Refused** (exit 2 — content capture cannot be enabled by accident) |
| Dead letters | <dead_letter_count> (transport DLQ retained + replayable) |

## Why it matters

- **Adoption & ROI**: usage and token spend per team, per product, in SQL —
  joinable with everything else in the warehouse.
- **Governance**: privacy tiers are configuration with an explicit
  acknowledgement gate; the telemetry itself proves what was (not) captured.
- **Future-proof**: new event types a vendor ships tomorrow are preserved
  natively and queryable before any code changes.

**Versions**: Claude Code <claude_version>, Codex <codex_version>
(verified ≥ 0.142.5), BQAA <bqaa_commit>. Full record: `evidence/`.
