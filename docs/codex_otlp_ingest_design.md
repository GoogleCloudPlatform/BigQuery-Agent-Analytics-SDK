# Codex (CLI/IDE) → BigQuery OTLP Ingest — OTel-Native Implementation Design

**Status:** Design / blocked on #316 receiver contract
**Issue:** [#317](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/317)
**Depends on:** [#316 OTel-native receiver](otlp_receiver_design.md) — native `otel_*` tables, envelope v1, `otel_schema_version`, DLQ/replay
**Date:** 2026-06-26 · **Supersedes** the earlier `agent_events`-projection-first draft.

---

## 1. Dependency gate & what the pivot simplifies

PR implementation **must not start until #316's OTel-native contract is final**:
the native tables, Pub/Sub envelope v1, per-signal idempotency, and
`otel_schema_version`. Codex reuses all of them — Codex speaks standard OTLP, so
its telemetry lands in the **same native tables** as Claude:

- logs → `otel_logs`
- metrics → the five `otel_metric_*` tables
- traces → `otel_spans` (only when #316's trace receiver is enabled)

**What the OTel-native pivot makes moot:** the earlier "mirror #316's metric JSON
row shape in the Codex crosswalk" concern is gone — Claude and Codex metrics land
in the *same physical* `otel_metric_*` tables, so the shape **cannot drift** and
there is **no Codex-specific metric view**. `bqaa_metrics` reads both uniformly.
Only the **log/event** projection is Codex-specific (event names aren't
isomorphic with Claude's). Codex binds to a **minimum `otel_schema_version`** and
fails fast if #316's schema is older.

Remaining Codex-specific open items:
1. **Tested** `otel.metrics_exporter` TOML (not prose).
2. A concrete **`CODEX_MIN_VERSION`** for the version-pinned surface matrix.
3. Codex log/event → BQAA projection crosswalk + queryable redaction state.

---

## 2. Three independent exporter keys (the central rollout fact)

| Key | Signal | Default | To reach our collector |
|-----|--------|---------|------------------------|
| `otel.exporter` | logs | `none` | `otlp-http` / `otlp-grpc` |
| `otel.metrics_exporter` | metrics | **`statsig`** | `otlp-http` / `otlp-grpc` |
| `otel.trace_exporter` | traces | `none` | `otlp-http` / `otlp-grpc` |

`otel.exporter = otlp-http` routes **logs only**; metrics stay on OpenAI's
internal `statsig` until `otel.metrics_exporter` is explicitly set. `[analytics]
enabled` (anonymous → OpenAI) is **independent** of `[otel]` — documented
separately so customers don't conflate them.

---

## 3. Config fixtures (tested, `~/.codex/config.toml`)

> **Verify, don't ship as prose:** the rendered config reference spells out
> endpoint/protocol/header subkeys for `otel.exporter` and `otel.trace_exporter`,
> but **not** definitively for `otel.metrics_exporter`. Each fixture below is
> validated end-to-end against `CODEX_MIN_VERSION` before docs ship; if the
> metrics subkey shape differs, the example would silently keep metrics on
> `statsig`.

- **3a logs-only:** `exporter = { otlp-http = { endpoint=".../v1/logs", protocol="binary", headers={...} } }`
- **3b logs+metrics:** + `metrics_exporter = { otlp-http = { endpoint=".../v1/metrics", ... } }`  *(shape to verify)*
- **3c logs+metrics+traces:** + `trace_exporter = { otlp-http = { endpoint=".../v1/traces", ... } }`

gRPC variants use `otlp-grpc = { endpoint=".../:4317", headers={...} }`.

**Enterprise rollout:** `[otel]` is **ignored in project-local
`.codex/config.toml`** (verbatim in the ignore list with `model_provider`,
`profile`, …); the documented path is **user-level `~/.codex/config.toml`**.
MDM/managed config for `[otel]` is **"verify"**, not asserted.

---

## 4. P0 boundary — logs + metrics; traces config-only

- **P0 is logs + metrics** (matches the issue AC). Metrics ingest already exists
  in #316; the only Codex unknown is the `metrics_exporter` TOML shape.
- **Gate task (PR 1 / pre-PR spike):** verify the `metrics_exporter` shape
  end-to-end against `CODEX_MIN_VERSION`. **If verified →** ship logs + metrics,
  fixture 3b lands tested. **If not →** the *only* condition that descopes P0 to
  logs, and it requires an **explicit issue-AC update** — never a silent mismatch.
- **Trace boundary:** #316 keeps trace landing fast-follow, so **P0 tests 3a +
  3b only**; **3c is config-shape-validated but its end-to-end BigQuery landing
  is deferred** until #316's `otel_spans` receiver exists (Codex can emit traces,
  but they have nowhere to land yet).
- **Honesty caveat:** even with `metrics_exporter` set, coverage is
  surface-dependent (see §5).

### `CODEX_MIN_VERSION` is concrete, not a placeholder

Provisional floor **`0.105.0`** (the version #12913 was reported against),
**resolved to the exact verified version at gate-task time; PR 1 must not merge
with it unresolved.** Every fixture/surface test **asserts and records `codex
--version`** so a later Codex release can't silently invalidate a frozen result.

---

## 5. Version-pinned surface matrix

#12913 was reported against `codex-cli 0.105.0` and **closed "fixed next
release"** — so the matrix is a verification task, not a permanent claim:

| Surface | logs | metrics | traces | (observed 0.105.0) |
|---------|------|---------|--------|--------------------|
| `codex` (interactive) | ✅ | ✅ | ✅ | |
| `codex exec` | ✅ | ❌ | ✅ | re-verify on min version |
| `codex mcp-server` | ❌ | ❌ | ❌ | no OTel SDK init — re-verify |
| IDE / cloud | verify | verify | verify | no doc claim they honor local `[otel]` |

---

## 6. Codex preservation + BQAA log/event projection

Native `otel_logs` preserves all Codex event names, resource/scope attrs,
trace/span ids, and redaction/snippet attributes **regardless of allowlist**. The
Codex-specific BQAA projection allowlist (into `agent_events_otlp`):

| Codex event | projected `event_type` |
|---|---|
| `codex.conversation_starts` | `session_start` |
| `codex.api_request` | `api_request` |
| `codex.sse_event` | `api_stream` |
| `codex.user_prompt` | `user_prompt` (length-only unless `log_user_prompt=true`) |
| `codex.tool_decision` | `tool_decision` |
| `codex.tool_result` | `tool_result` (duration/success **+ output snippet**) |
| `codex.websocket_request` / `_event` | `transport` or `otlp.unknown` |

Privacy state in the projection (and queryable natively): `log_user_prompt`
(default `false`), `content_redaction_state`, `snippet_truncation_state`,
`raw_or_unknown_payload`. **No blanket "tool content redacted" claim** —
`codex.tool_result` carries an output snippet with no documented length cap.
Cloud/app surfaces stay "verify / no claim."

---

## 7. P0 / fast-follow

**P0:** Codex logs + metrics into #316's native `otel_logs` / `otel_metric_*`
tables (binding a min `otel_schema_version`); tested 3a + 3b fixtures against a
concrete `CODEX_MIN_VERSION`; 3c config-validated (landing deferred);
version-pinned surface matrix; Codex log/event → `agent_events_otlp` projection +
queryable redaction/snippet state; rollout note (project-ignore, user-level
config, `[analytics]` ≠ `[otel]`).

**Fast-follow / verify:** trace data landing (with #316 trace receiver);
MDM/managed config; IDE/cloud surfaces; metrics **only if** the gate task fails
verification (with the issue AC updated to match).
