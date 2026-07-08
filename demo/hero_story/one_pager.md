# AI coding telemetry, in our own warehouse — demo summary

<!-- Filled from the reference rehearsal (2026-07-08). Regenerate per run
     from evidence/; aggregates and hashes only (see evidence/template.md
     redaction rules). -->

**What we showed.** Claude Code and Codex telemetry from real developer
sessions, landing in a BigQuery dataset **we own**, queryable with plain
SQL, with privacy enforced by configuration.

**Architecture (one line).** Developer machines export OpenTelemetry →
an authenticated Cloud Run receiver → Pub/Sub (with a retained dead-letter
queue) → BigQuery — every component inside our own GCP project; no
third-party analytics service in the path.

**How much work it was.** One command deployed the pipeline
(`bqaa-otel bootstrap … --execute`); two generated config artifacts enabled
the sources. The reference rehearsal ran the whole fresh-project chain —
preflight, deploy, sessions, verification — in under 25 minutes, first try.

## Numbers from the reference run (`hero20260708000541`, 24h window)

| Question | Result |
|---|---|
| Products reporting | `claude_code`, `codex` — one schema, provenance-tagged |
| Sessions / conversations | 1 Claude session, 1 Codex conversation (scripted demo sessions) |
| Distinct users (hashed) | 1 |
| Tokens (measured) | 30,075 Claude + 41,379 Codex — est. $0.18 + $0.21 *(estimates; editable rates, as-of 2026-07-07)* |
| Agent latency | `claude_code.llm_request` 7.1s; `codex.handle_responses` p95 = 112 ms (from real spans) |
| Prompt content in warehouse | **PASS — none found** (exact scripted-prompt scan of all content-bearing columns; baseline tier) |
| Replay without explicit acknowledgement | **Refused** (exit 2 — content capture cannot be enabled by accident) |
| Dead letters | 0 (transport DLQ retained + replayable) |
| Teardown | exercised for real on the *previous* demo dataset (not this reference run's): dataset + scheduled job deleted and **existence-verified** gone |

## Why it matters

- **Adoption & ROI**: usage and token spend per team, per product, in SQL —
  joinable with everything else in the warehouse.
- **Governance**: privacy tiers are configuration with an explicit
  acknowledgement gate; the telemetry itself proves what was (not) captured.
- **Future-proof**: new event types a vendor ships tomorrow are preserved
  natively and queryable before any code changes.

## Recommended path (exec-reviewed)

Pilot 20–50 engineers on `logs,metrics,traces` at **baseline** privacy with
a required `department`/`cost_center`/`env` taxonomy, retention policy, and
dead-letter/freshness/token alerts. Before broad rollout: token-rotation
runbook, private ingress review, MDM-only config distribution, and a
monthly compatibility smoke (product telemetry drifts — verified shapes are
version-pinned). Content-bearing modes stay off until security review.

**Versions**: Claude Code 2.1.203, Codex 0.142.5 (verified minimum), BQAA
`c3d7334`. Full record: `evidence/` from run `hero20260708000541`.
