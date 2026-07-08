# Reference rehearsal transcript — 2026-07-08 (redacted)

Run id `hero20260708000541` · dataset `bqaa_hero_demo_20260708` · fresh-project
chain, first attempt, no manual intervention. Redaction rules from
`evidence/template.md` applied (no tokens, emails, or full receiver URLs).

Versions: Claude Code 2.1.203 · codex-cli 0.142.5 · BQAA c3d7334 ·
gcloud 559.0.0 · bq 2.1.28 · signals logs,metrics,traces · privacy baseline.

## reset_demo.sh (preflight → bootstrap → inventory → sessions)

```
==> Preflight (fails fast; nothing has been mutated)
13 ok, 0 warnings, 0 failed
Preflight green — safe to bootstrap/present.
==> Bootstrap plan (mutates nothing)
==> Bootstrap execute (fresh install ~15 min incl. Cloud Build; converges on re-run)
==> Enabling APIs
==> Ensuring Artifact Registry repo 'bqaa' exists
==> Creating BigQuery dataset (US) + native schema
==> Ensuring the bearer token secret exists
==> Ensuring service accounts exist
==> Ensuring Pub/Sub topics + DLQ retention subscription exist
==> Granting least-privilege IAM
==> Building image
==> Deploying the OTLP receiver (Cloud Run)
==> Deploying the Pub/Sub push consumer (Cloud Run HTTP service)
==> Ensuring the push subscription (OIDC) with DLQ
==> Registering the scheduled MERGE into agent_events_otlp
==> Generating telemetry-source config artifacts
==> Done. Receiver: https://bqaa-…run.app
==> Recording the resource inventory (feeds teardown.sh)
==> Deterministic demo sessions (both products, per-product landing gates)
==> Claude Code session (baseline privacy, traces tier)
==> Codex session (isolated CODEX_HOME; your real ~/.codex is untouched)
==> Waiting for BOTH products to land (logs, spans, tokens; up to ~4 min)
  poll 1: claude_code logs,spans,tokens=37,2,30075 | codex logs,spans,tokens=19,439,41379
DEMO_RUN_ID=hero20260708000541   (persisted to evidence/DEMO_RUN_ID)
Demo is ready. Next: scripts/run_queries.sh   (teardown: scripts/teardown.sh)
```

Per-product landing gates green on the FIRST poll — both products, all
three surfaces.

## verify --smoke (pipeline proof)

```
OK    endpoint auth enforced: unauthenticated POST https://bqaa-…run.app/v1/logs -> 401 (want 401)
OK    endpoint reachable: authenticated POST https://bqaa-…run.app/v1/logs -> 200 (want 200; decode-only probe — use --smoke for the full path)
OK    tables and views exist: all present
OK    recent rows in otel_logs: 56 rows in the last 24h
OK    recent rows in bqaa_metrics: 87 rows in the last 24h
OK    dead-letter health: 0 dead-lettered records in the last 24h
OK    smoke send /v1/logs: POST /v1/logs -> 200
OK    smoke send /v1/metrics: POST /v1/metrics -> 200
OK    smoke send /v1/traces: POST /v1/traces -> 200
OK    smoke row in otel_logs: 1 rows (waited up to 150s)
OK    smoke point in otel_metric_gauge: 1 rows (waited up to 150s)
OK    smoke point in bqaa_metrics view: 1 rows (waited up to 150s)
OK    smoke span in otel_spans: 1 rows (waited up to 150s)
OK    smoke event projected into agent_events_otlp: event_type='claude_code.user_prompt'

All checks passed.
```

## SQL pack highlights (full CSVs regenerated per run in evidence/sql/)

```
token_usage_and_estimated_cost_by_team

governance_privacy_dlq
```

## teardown --dataset-only --confirm (previous e2e dataset, real telemetry)

```
Teardown plan from <inventory-path>/demo_resources.json (project=<project-id> dataset=otlp_e2e_324 location=US)

--- dataset-scoped ---
DELETE  DTS scheduled MERGE (projects/<project-number>/locations/us/transferConfigs/<transfer-config-id>)
DELETE  BigQuery dataset <project-id>:otlp_e2e_324 (contains real telemetry)

--- post-teardown verification (existence checks, not exit codes) ---
PASS  no DTS scheduled MERGE remains for otlp_e2e_324 (incl. legacy unsuffixed)
PASS  dataset otlp_e2e_324 (real telemetry) is gone

Teardown verified clean.
```

The destructive path is existence-verified: a failed delete cannot report
clean, and the scheduled job (the one resource that bills forever) is
provably gone.
