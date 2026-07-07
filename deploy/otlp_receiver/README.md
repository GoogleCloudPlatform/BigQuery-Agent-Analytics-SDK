# OTel-native OTLP receiver — enterprise admin setup

Deploys the [#316](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/316)
OTel-native receiver: Claude Code / Codex OpenTelemetry logs + metrics land in
**OTel-native BigQuery tables** in your own project, with a BQAA
`agent_events_otlp` projection on top.

```
Claude Code / Codex --OTLP--> Cloud Run receiver --> Pub/Sub --> consumer --> BigQuery
                                                        └── DLQ --> otlp_dead_letter
```

## Deploy

```bash
# Print the plan (runs nothing):
PYTHONPATH=producers/src python3 -m bigquery_agent_analytics_tracing.otlp.cli \
  bootstrap --project my-proj --dataset agent_analytics --region us-central1

# Apply it:
PYTHONPATH=producers/src python3 -m bigquery_agent_analytics_tracing.otlp.cli \
  bootstrap --project my-proj --dataset agent_analytics --region us-central1 --execute
```

(`bqaa-otel bootstrap ...` once the producers package is installed;
`setup.sh` is a thin wrapper over the same command with the historical
env-var interface: `PROJECT=my-proj bash deploy/otlp_receiver/setup.sh`.)

This creates: the native tables + `*_dedup` views + `agent_events_otlp` +
`bqaa_metrics` (DDL generated from the schema package; `gen_schema_sql.py`
prints the same bundle standalone), Pub/Sub topics/subscription + DLQ, a
Secret Manager bearer token, the Cloud Run receiver + consumer, and the
scheduled `MERGE`. It prints the receiver URL, how to read the token, and
writes the ready-to-distribute Claude Code / Codex config artifacts
(`--source claude-code,codex --signals ... --privacy ...` — see
`bqaa-otel config --help` for generating artifacts against an existing
deployment).

- **Endpoints:** `<url>/v1/logs`, `<url>/v1/metrics`, and — when the
  deployment's signal tier includes traces (`--signals logs,metrics,traces`
  / `ENABLE_SPANS=1`) — `<url>/v1/traces`, landing spans in `otel_spans`.
- **Auth:** bearer token — `Authorization: Bearer <token>`. The receiver rejects
  unauthenticated requests with `401`.
- **Protocol:** OTLP/HTTP `http/protobuf` is the recommended enterprise default.

## Configure the telemetry source

### Claude Code (server-managed settings JSON)

There is no admin API; an Owner/Primary Owner pastes this into managed settings
(or deploys it via MDM). `baseline` privacy — no prompt text / raw bodies / tool
content:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://<receiver-url>",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <token>,x-bqaa-source-product=claude_code"
  }
}
```

### Codex (user-level `~/.codex/config.toml`)

Shapes verified live against **codex-cli 0.142.5**
([#317](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/317)).
Rollout notes:

- `[otel]` is **ignored in project-local** `.codex/config.toml` — set it at
  user level (`~/.codex/config.toml`). MDM/managed distribution: verify for
  your fleet; there is no documented guarantee.
- `[analytics]` (anonymous → OpenAI) is independent of `[otel]` — enabling
  one does not enable the other.
- Codex metrics default to `statsig`; metrics only reach your receiver with
  `metrics_exporter` explicitly set as below.
- Codex does **not** expand `${ENV}` references in config headers (verified
  live) — the file holds the literal bearer token; do not commit it.
- The `x-bqaa-source-product` header keeps `source_product` provenance
  deterministic on a multi-product receiver.

```toml
[otel]
environment = "prod"
exporter = { otlp-http = { endpoint = "https://<receiver-url>/v1/logs", protocol = "binary", headers = { "Authorization" = "Bearer <token>", "x-bqaa-source-product" = "codex" } } }
metrics_exporter = { otlp-http = { endpoint = "https://<receiver-url>/v1/metrics", protocol = "binary", headers = { "Authorization" = "Bearer <token>", "x-bqaa-source-product" = "codex" } } }
trace_exporter = "none"   # optional observability tier: same shape with /v1/traces
log_user_prompt = false
```

Logs-only pilots: omit `metrics_exporter` (it defaults to `statsig`, which
does not reach your receiver). **Codex traces are observability traces —
span/trace structure and timing, not replay.** Codex documents no
Claude-Code-style raw request/response body path, so replay/full-content
capture remains unsupported for Codex.

## Privacy tiers

The receiver stores whatever the source emits; **content is controlled at the
source** via these env vars/config. Signal tier (logs/metrics/**traces**) is
independent of content tier.

| Tier | What is captured | How |
|------|------------------|-----|
| `baseline` (default) | logs + metrics, no prompt/tool/raw content | the settings above |
| `security-audit` | + tool/MCP/Bash decision detail | Claude: add `"OTEL_LOG_TOOL_DETAILS": "1"`; Codex: documented tool-decision/result metadata only |
| `replay` | + prompt text / raw API bodies (flywheel input) | Claude: documented raw-body controls — **opt-in only**. Codex: **not offered** — Codex documents no supported raw request/response body path |

**Traces are not replay.** A span is structure/timing, not the transcript. Codex
traces (`otel.trace_exporter`) are observability only.

## Verify

```bash
# Read-only health checks: endpoint reachability + auth enforcement,
# table/view existence, recent rows, dead-letter health.
BQAA_OTLP_TOKEN=<token> PYTHONPATH=producers/src python3 -m \
  bigquery_agent_analytics_tracing.otlp.cli verify \
  --endpoint <url> --project <proj> --dataset <dataset>

# Add --smoke to also send synthetic OTLP logs+metrics and follow them into
# the native tables, dedup views, and the agent_events_otlp projection.
```

The full pytest e2e (same payloads as `--smoke`, plus protobuf-path and
dead-letter round-trips) remains available:

```bash
BQAA_OTLP_ENDPOINT=<url> BQAA_OTLP_TOKEN=<token> \
  BQAA_PROJECT=<proj> BQAA_DATASET=<dataset> \
  python -m pytest producers/tests/test_otlp_e2e.py -v
```
