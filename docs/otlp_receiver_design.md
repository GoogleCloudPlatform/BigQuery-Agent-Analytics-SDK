# Claude Code → BigQuery OTLP Receiver — OTel-Native Implementation Design

**Status:** Design / pre-implementation (architecture breakdown ready)
**Issue:** [#316](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/316)
**Downstream dependents:** [#317](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/317) (reuses native tables + envelope), [#318](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/318) (mines them)
**Related schema:** `producers/src/bigquery_agent_analytics_tracing/schema.py` (`agent_events`, now a *projection target*)
**Date:** 2026-06-26 · **Supersedes** the earlier `agent_events`-as-landing draft.

---

## 1. Architectural inversion (what changed and why)

Source of truth is **OTel-native BigQuery tables**, not BQAA's `agent_events`.
The receiver lands logs / metric points / (gated) spans in observability-native
tables that mirror the OTLP proto; **BQAA `agent_events` becomes a projection**
layered on top for existing SDK/dashboard compatibility.

This dissolves the tensions the earlier `agent_events`-landing design kept
hitting: no provenance-columns-vs-canonical-schema fight, no metrics-as-rows-vs-
JSON decision, and unknown events (`api_refusal`, body events) stay first-class
queryable instead of becoming `otlp.unknown` rows.

Pipeline unchanged: **Cloud Run (OTLP ingest) → Pub/Sub → BigQuery Storage Write
API**, Terraform-deployed into the customer project.

---

## 2. Decision 1 — Schema lineage: ClickHouse `otel_*`, BigQuery-adapted, versioned

**There is no canonical OTLP→BigQuery schema** (the OTel Collector's
`googlecloudexporter` targets Cloud Logging/Monitoring/Trace, not raw BigQuery;
the contrib BigQuery-exporter request was closed *not planned*). The only
OTLP-faithful, community-maintained named layout is the **ClickHouse exporter
schema** (`opentelemetry-collector-contrib/exporter/clickhouseexporter`). We
**adopt its structure and column vocabulary, translate ClickHouse types to
BigQuery, and version it** (`otel_schema_version`, a table label + a versioned
DDL file) — because we own compatibility (no upstream standard to track) and
OTLP itself adds fields over time.

Two structural decisions inherited from ClickHouse (both independently validated
by the OTLP proto `oneof` and OTel-Arrow):

- **Metrics split into five per-type tables**, not one wide table.
- **Low-cardinality identity promoted to top-level typed columns** (timestamps,
  ids, `service_name`, `metric_name`, `severity_number`) for partition/cluster
  keys; heterogeneous attribute bags stay in JSON.

### 2.1 BigQuery type translation

| ClickHouse | BigQuery | Used for |
|---|---|---|
| `Map(LowCardinality(String), String)` | **`JSON`** | resource / scope / log / span / metric-point attributes |
| `Nested(...)` | **`ARRAY<STRUCT<...>>`** | span `events` / `links`, metric `exemplars` |
| `Array(T)` | `ARRAY<INT64>` / `ARRAY<FLOAT64>` | histogram `bucket_counts` / `explicit_bounds` |
| `DateTime64(9)` | `TIMESTAMP` | event/observed/start/end times |
| `ORDER BY (...)` | `PARTITION BY` + `CLUSTER BY` | see §2.4 |

**Why JSON for attribute bags** (Decision 2, folded in): BigQuery native `JSON`
gives schema-on-read for the open `AnyValue` attribute space, preserves value
typing (string/int/double/bool/array), and bills only the scanned JSON paths
(Google's benchmark: 18.28 GB → 514 MB, ~97% reduction vs a `STRING` blob). It
matches Google's own move of Cloud Logging → Log Analytics from rigid RECORDs to
native JSON. `ARRAY<STRUCT<key,value>>` is rejected (flattens typing); raw
`STRING` is rejected (whole-row scan).

### 2.2 Native tables (P0: logs + metrics; spans gated)

```
otel_logs                      -- one row per OTLP log record
otel_metric_sum                -- NumberDataPoint + aggregation_temporality + is_monotonic
otel_metric_gauge              -- NumberDataPoint
otel_metric_histogram          -- count/sum/min/max + bucket_counts[] + explicit_bounds[]
otel_metric_exponential_histogram  -- scale/zero_count/zero_threshold + pos/neg buckets
otel_metric_summary            -- quantile_values[]
otel_spans              (trace-gated) -- + events[]/links[]
otlp_dead_letter               -- malformed/failed records + replay metadata
```

`otel_logs` columns (representative): `timestamp`, `observed_timestamp`,
`trace_id`, `span_id`, `trace_flags`, `severity_text`, `severity_number`,
`service_name` (promoted), `body` (JSON), `resource_attributes` (JSON),
`scope_name`, `scope_version`, `scope_attributes` (JSON), `log_attributes`
(JSON), `event_name`, plus receiver metadata `source_product`, `source_signal`,
`idempotency_key`, `source_position` (STRUCT, **required** — same shape as the
envelope, §4), `ingest_time`, `raw_preservation` (STRUCT), `otel_schema_version`.
Metric tables carry the same `idempotency_key` + `source_position` receiver
metadata.

Metric tables share a common prefix (`resource_attributes`, `scope_*`,
`service_name`, `metric_name`, `metric_description`, `unit`, `attributes` JSON,
`start_timestamp`, `time_timestamp`, `flags`) + a per-type tail. **Exemplars**
on sum/gauge/histogram/exp-histogram are
`ARRAY<STRUCT<time_timestamp TIMESTAMP, value FLOAT64, span_id STRING,
trace_id STRING, filtered_attributes JSON>>` — the documented metric→trace
correlation key. (Summary has no exemplars/temporality, per proto.)

### 2.3 Resource attributes: denormalized per row (no dimension table)

Resource attributes are stored **per row as JSON** with `service.name` promoted
to a column — matching ClickHouse (`ResourceAttributes Map` + `ServiceName`) and
Google's denormalization guidance. BigQuery Capacitor RLE/dictionary-encodes the
repetition once rows are clustered by `service_name`. A normalized hash-keyed
resource table is **not** P0 (forces a JOIN on every query); revisit only if
measured resource payloads are large and compression underperforms.

### 2.4 Partitioning & clustering

- **Partition** daily by `DATE(timestamp)` (event time, not ingestion time);
  **require a partition filter**; set partition expiration (retention knob).
- **Cluster** (≤4, leftmost-filtered first):
  - `otel_logs` / `otel_spans`: `service_name, severity_number/span_name, trace_id`
  - metric tables: `service_name, metric_name`
- Mind the **4,000-partitions-per-job** cap on historical backfills (chunk loads).

---

## 3. Decision 3 — Projection is a scheduled-MERGE table, not a view

The earlier draft made `agent_events_otlp` a view doing crosswalk + read-time
dedup (`QUALIFY ROW_NUMBER()`). **That cannot be an incremental materialized
view** — BigQuery MVs categorically forbid window functions (and self-joins,
`UNION ALL`, UDFs). Grounded options:

| Mechanism | Window-fn dedup | Auto-rewrite | Freshness | Read cost |
|---|---|---|---|---|
| Plain logical view + `QUALIFY` | yes | n/a | always | full scan + sort **every read** |
| Incremental MV | **no** | yes | auto | cheapest (but can't express dedup) |
| Non-incremental MV (`max_staleness` 30 m–3 d) | yes | no | stale | full re-exec on refresh |
| **Scheduled `MERGE` → curated table** | yes | n/a | ≥5 min | cheap, predictable |

**Decision:** native `otel_*` tables stay **append-only** (default Storage Write
stream, at-least-once). The **BQAA projection `agent_events_otlp` is a curated
table built by a scheduled `MERGE`** (5–15 min) that does dedup + crosswalk once;
dashboards/SDK read the curated table. This is the one place a compaction job is
justified — and the research shows it's effectively *required*, because the
projection's dedup+crosswalk exceeds MV limits.

For ad-hoc correctness on the raw native tables, ship thin **dedup views**
(plain logical views, `QUALIFY ROW_NUMBER() OVER (PARTITION BY idempotency_key
ORDER BY ingest_time DESC) = 1`) — acknowledged as scan-heavy, for low-volume
exploration; high-volume reads go through the curated projection.

### 3.1 Schema-parity contract test

Existing SDK/dashboards now depend on `agent_events_otlp` faithfully reproducing
`agent_events`. **A contract test must assert `agent_events_otlp`'s column set
and types are a superset of `producers/.../schema.py`'s `agent_events`.** Drift
in the canonical schema must fail this test, not silently break SDK queries.

---

## 4. Decision 4 — Pub/Sub envelope v1 (OTel-native) + per-signal idempotency

One message per decoded **OTLP record / metric data point / span**:

```jsonc
{
  "envelope_version": "1",
  "otel_schema_version": "1",
  "idempotency_key": "<per-signal hash, §4.1>",
  "ingest_time": "2026-06-26T20:44:17Z",
  "source": { "product": "claude_code", "signal": "log|metric|span", "client_version": "2.1.172" },
  "source_position": {                              // REQUIRED — stable, replay-invariant; feeds idempotency (§4.1)
    "raw_otlp_request_hash": "<sha256 of original OTLP request bytes>",
    "resource_index": 0, "scope_index": 0,
    "record_index": 0,                              // log record index (logs)
    "metric_index": 0, "data_point_index": 0        // metric + data point index (metrics)
  },
  "otlp": { "resource_attributes": {...}, "scope": {"name":"com.anthropic.claude_code","version":"..."} },
  "record": { /* decoded log record / metric point (typed) / span */ },
  "raw_preservation": { "policy": "decoded_only", "raw_b64": null },
  "parse_error": null,
  "delivery": { "attempt": 1, "dlq": false }
}
```

### 4.1 Per-signal idempotency keys (records have no natural id)

OTLP logs and metric points carry **no unique id**, so the key is a deterministic
content hash, stored as **`TO_HEX(SHA256(...))`** over a **canonicalized** payload
(sorted attribute keys, fixed timestamp precision). `FARM_FINGERPRINT` is used
**only** as an optional clustering/sharding helper, never as the canonical
identity (64-bit, collision-prone for an identity key).

`source_position` (the §4 envelope/native-row field) is a **required** input to
the log and metric keys — it is what distinguishes two legitimately identical log
records or metric points in the same OTLP request, and it is **replay-invariant**
(a deterministic function of the original request bytes, preserved through DLQ/
replay):

- **spans:** `trace_id || span_id` (natural key — no hash; `source_position`
  still stored for audit).
- **logs:** `sha256(resource_id | scope | observed_time | severity | body | sorted_attrs | source_position)`.
- **metric points:** `sha256(resource_id | scope | metric_name | sorted_attrs | start_ts | time_ts | temporality | value | source_position)`.

**No `ingest_time` in the key.** `ingest_time` changes on replay and across
receiver retries, which would *break* the exact retry/replay collapse the key
exists to provide — `source_position` provides the export-distinctness instead.
Default Storage Write stream is at-least-once (Google's recommended high-throughput
path); dedup happens at projection-build / read time on this key (the documented
BigQuery streaming-dedup pattern).

### 4.2 Parse failures → `otlp_dead_letter` (never analytics tables)

Decode/write failures are **published, not dropped**, with `record: null` and a
populated `parse_error` (`stage`, `reason`, `raw_b64`). They land in
`otlp_dead_letter` (own DDL; partitioned, short retention) + a DLQ topic — never
in `otel_*`. **DLQ keying:** carry the same `source_position` (request hash +
indices) where decoding got far enough to compute it; for whole-request failures
that never decoded, key the dead-letter row from `raw_otlp_request_hash + parse
stage`. Replay re-publishes `raw_b64` to the main topic; because `source_position`
is replay-invariant, the idempotency key reproduces exactly and dedup at
projection time makes replay safe (append-only).

---

## 5. BQAA projections & crosswalk

- **`agent_events_otlp`** (scheduled-MERGE curated table, §3): maps selected
  `otel_logs` / `otel_spans` records into the `agent_events` shape. Provenance
  (`source_product`, `source_signal`, `source_event_name`, `crosswalk_version`)
  are **projection columns**, derived — not physical columns on `agent_events`,
  so **no `agent_events` migration**.
- **`bqaa_metrics`** / dashboard views: read from the five `otel_metric_*`
  tables (cost, tokens, tool activity, sessions, failures), respecting metric
  temporality (§6).
- **Allowlist controls projection only.** `api_refusal`, `api_request_body`,
  `api_response_body`, `plugin_loaded`, websocket, etc. are preserved natively in
  `otel_logs` regardless; the allowlist decides which get first-class BQAA rows.
  Unknown events remain queryable natively (may project to `otlp.unknown`).
  Note `api_refusal` fires on `stop_reason:"refusal"` and does **not** trigger
  `api_error` — count it separately.

---

## 6. Signal-specific contracts (preserve natively, interpret in projection)

- **Metrics temporality preserved.** Claude defaults to **delta**; native tables
  store `aggregation_temporality` + start/end timestamps verbatim. P0 must not
  silently SUM/last-value delta as cumulative; derived dashboard views **declare
  their temporality assumption** (or require cumulative input). In-pipeline
  delta→cumulative is fast-follow, not a storage prerequisite.
- **Raw API bodies, two modes.** `=1` inline (≤60 KB) → store in `otel_logs`
  body/attributes with row-level truncation metadata. `file:<dir>` → only a
  **source-local `body_ref` path** arrives; store as a non-dereferenceable
  reference (never a fetchable URI). Off by default in deploy docs (enabling
  implies prompt+tool+content exposure). GCS-uploader sidecar is fast-follow.
- **Identity is auth-dependent.** Preserve OTel identity/resource attrs as
  emitted; direct API key / Bedrock / Vertex / Foundry populate only `user.id` +
  `session.id`. `OTEL_RESOURCE_ATTRIBUTES` (`enduser.id`) is the injection path;
  the `agent_events_otlp` projection maps `user.id`→`user_id` and documents
  account/org sparsity.
- **Per-request attribution is redaction-gated.** Preserve `model`/`agent.name`/
  `skill.name`/`plugin.name`/`mcp_*` plus the privacy-gate state so a real
  `"custom"` is distinguishable from a redacted third-party name
  (`OTEL_LOG_TOOL_DETAILS`).
- **Cardinality knobs preserved.** `prompt.id` / `workspace.host_paths` never on
  metrics; metric↔event joins use `session.id` / `tool_use_id`.
- **Tracing two-tier, gated.** Basic (`CLAUDE_CODE_ENABLE_TELEMETRY` +
  `…ENHANCED_TELEMETRY_BETA`) vs detailed (`ENABLE_BETA_TRACING_DETAILED` +
  separate `BETA_TRACING_ENDPOINT`). P0 may wire the `otel_spans` receiver behind
  a flag; trace **data landing** is not required for the logs+metrics milestone.
- **Subprocess caveat:** Claude Code does not pass `OTEL_*` to spawned
  subprocesses; the receiver won't see subprocess-originated telemetry.

---

## 7. P0 / fast-follow boundary

**P0:** OTLP/gRPC + HTTP-protobuf on `/v1/logs` + `/v1/metrics` (HTTP/JSON out);
auth via Secret Manager + Cloud Run injection + rotation; **native `otel_logs` +
five `otel_metric_*` tables** (`otel_spans` behind the trace flag), versioned
`otel_schema_version`, JSON attributes + promoted cluster columns, daily
partition + clustering; envelope v1 + per-signal idempotency; append-only writer
+ read-time dedup views; **`agent_events_otlp` scheduled-MERGE projection +
schema-parity test** + `bqaa_metrics`; malformed→`otlp_dead_letter` + DLQ +
replay; metric-temporality preservation; raw-body two-mode; retention/cardinality
+ partition/cluster.

**All five `otel_metric_*` DDLs + write paths are created and conformance-tested
in P0 — no metric-type table is deferred.** Claude currently emits mostly
sums/gauges, so histogram / exponential-histogram / summary tables are validated
via **synthetic** OTLP fixtures (write-path + empty-table conformance), not by
waiting for Claude to emit them. This keeps the five-table contract stable so
#317/#318 can bind to it now.

Conformance fixtures: valid log; **a valid metric for each of the five point
types** (histogram/exp-histogram/summary synthetic); malformed→dead-letter;
unauthenticated; idempotency replay (key stable across replay, §4.1); DLQ replay;
regional write; privacy-gate states; **projection correctness + schema-parity**.

**Fast-follow:** trace data landing (two-tier gating); GCS uploader for raw-body
files; in-pipeline delta→cumulative aggregation; normalized resource table (only
if measured).
