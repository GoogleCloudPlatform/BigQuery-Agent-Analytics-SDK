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

- **Endpoints:** `<url>/v1/logs`, `<url>/v1/metrics` (`/v1/traces` is gated —
  set `ENABLE_SPANS=1` to wire it; span landing is deferred).
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
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <token>"
  }
}
```

### Codex (user-level `~/.codex/config.toml`)

`[otel]` is ignored in project-local config, so set it at user level. **Logs-only
until [#317](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/317)
verifies the metrics config shape** (Codex metrics default to `statsig`, and the
exact `metrics_exporter` endpoint block is version-specific):

```toml
[otel]
environment = "prod"
exporter = "otlp-http"
metrics_exporter = "none"    # PENDING #317: enable once the metrics shape is verified
trace_exporter = "none"
log_user_prompt = false

[otel.exporter."otlp-http"]
endpoint = "https://<receiver-url>/v1/logs"
protocol = "binary"
headers = { "Authorization" = "Bearer ${BQAA_OTLP_TOKEN}" }
```

> Do not set `metrics_exporter = "otlp-http"` from this doc: the endpoint block
> shape for metrics is verified per Codex version in
> [#317](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/317).
> Full guided config generation is
> [#324](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/324).

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
