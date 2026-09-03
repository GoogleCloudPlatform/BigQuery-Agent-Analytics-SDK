# Spec — `examples/okf_bqaa_adapter/`

**Label: derived/demo, observer-only, nothing attested. Not #435.**

Adapter version: `okf-bqaa-adapter:v0` (port of the github.io `adapter.js`).

## 1. Observe agent contract (`observe_agent.py`)

| Item | Value |
|------|-------|
| Agent name | `okf_rfc_observe_agent` |
| Model | `DEMO_MODEL_ID`, default `gemini-3.8-flash` |
| Vertex | `GOOGLE_GENAI_USE_VERTEXAI=True`, `GOOGLE_CLOUD_LOCATION` default `global` |
| Plugin | `BigQueryAgentAnalyticsPlugin(project_id, dataset_id, table_id="agent_events", location="US", config=BigQueryLoggerConfig(batch_size=1, shutdown_timeout=20.0, max_content_length=64*1024))` |
| Project | `GOOGLE_CLOUD_PROJECT`, default `test-project-0728-467323` |
| Dataset | `OKF_DEMO_DATASET`, default `okf_rfc_demo` (created if missing, location US) |
| Runner | `InMemoryRunner`; `create_session` before `run_async`; `runner.close()` then plugin shutdown |
| Prompt | Multi-turn: 10–12 related questions (Germany / France / UK / prior quarter / trust / excluded metric / policy / backing tables / active-customer definition / region roll-up / what would make it attested / excluded items). First question stays the Germany line. Same `session_id` for every turn. Keep going until a BQ query for that session returns **>= 100** rows. |

Fail closed before constructing the agent: if the model id contains `2.5`,
`3.5`, or `flash-latest`, raise `SystemExit` with a clear message.

### Tools (observer-only; never read authored `cymbal-finance-core`)

Both tools read only a small in-process observed catalog (titles, types,
ranks, one exclusion, one link) labelled derived/demo. The plugin logs
`TOOL_COMPLETED` with `content = {tool, result, tool_origin}` and does **not**
set `attributes.tool.kind`, so the OKF envelope lives on the tool return
value:

`okf_retrieve_context(mode="current", token_budget=8000)` returns
```
{kind: "okf-context:retrieve", context_ref: "okf:env-observe#<opaque>",
 item_count, okf: {publication_id, profile_contract_version: "okf-context/1",
 mode, items: [{rank, type, title}], excluded: [{type, title, reason}],
 links: [{from, to, rel}]}, label: "derived/demo, observer-only"}
```
`publication_id` is a sha256 pin of the in-process demo catalog, not an
authored publication.

`okf_run_attested_computation(context_ref)` executes nothing and returns
```
{kind: "okf-context:attested-computation", context_ref, okf: {verdict:
 "UNVERIFIABLE", verdict_reason: "no-execution; observer-only demo, nothing
 attested", receipt_id: "rcpt-observe-noexec", runtime:
 "bigquery-named-parameters", parameter_schema: [region STRING, quarter_start
 DATE, quarter_end DATE], receipt_fields: [verdict, verdict_reason,
 receipt_id]}, label: "derived/demo, observer-only, not attested"}
```
If `context_ref` is not a **non-empty exact equal** of the envelope issued
by the retrieve tool (prefix / suffix matches do not bind), the receipt is
refused (`verdict: REFUSED`, no receipt_id) instead of fabricated.

Never on tool payloads: `concept_version_id`, `bundle_path`, `source_path`,
`principal`, `user_id`, `query_text`, `sql`, `parameter_values`,
`destination_table`.

Agent instruction: call `okf_retrieve_context` first, then
`okf_run_attested_computation` with the returned `context_ref`, then answer.
Cite only `context_ref`. Never print SQL, principal, paths, or
`concept_version_id`.

## 2. Live export

After shutdown, query `agent_events` for the session and write:

- `fixtures/live_observe_agent_events.json`
  ```
  {_fixture, label, project, dataset, table: "<project>.<dataset>.agent_events",
   writer: {plugin, label, mode}, agent: {name, framework, model},
   session_id, trace_id, exported_at, events: [row, ...]}
  ```
  Each `row` keeps the BigQuery columns `timestamp` (ISO 8601 Z),
  `event_type`, `agent`, `session_id`, `invocation_id`, `user_id`,
  `trace_id`, `span_id`, `parent_span_id`, `content`, `attributes`,
  `latency_ms`, `status`, `error_message`, `is_truncated`. JSON columns are
  parsed into objects. `content_parts` is dropped.
- `fixtures/live.json` — `{session_id, trace_id, agent, model, dataset,
  table, project, vertex_location, ran_at, label}`.
- `fixtures/live_identities.json` — identities computed from the export at
  run time (pinned by tests).

Export gate (nonzero exit, no files written on failure): parsed rows must
contain an OK `TOOL_COMPLETED` with `content.result.kind ==
"okf-context:retrieve"` and one with `"okf-context:attested-computation"`
whose `context_ref` binds to the retrieve envelope, plus an `LLM_REQUEST`
whose `attributes.model` is the run model, **and `len(events) >= 100`**.
A 15-row smoke is rejected. Do not fake, duplicate, or pad events.
germany JSON remains synthetic hashing-only.

The run prints `PROJECT DATASET MODEL SESSION TRACE` at the end.

## 3. Adapter contracts (`adapter.py`, stdlib only)

- `observe(trace) -> dict` — pulls only what an observer may see. Tool kind
  is read from `attributes.tool.kind` (fixture shape) **or**
  `content.result.kind` (live plugin shape); the OKF envelope from
  `attributes.okf` or `content.result.okf`; `context_ref` likewise. The
  user question comes from `LLM_REQUEST.content.text`,
  `USER_MESSAGE_RECEIVED.content.text_summary`, or the first user entry of
  `LLM_REQUEST.content.prompt`.
- `require_retrieve_shaped(trace)` — raises `NotRetrieveShapedError` unless
  both kinds are present with status OK **and** retrieve/receipt
  `context_ref`s are non-empty exact equals (`refs_bound`).
- `adapt(trace) -> {observation, constants, files, docs, bundle_key}` — port
  of `stubDoc` / `logDoc`. Constants: `bundle_key=bqaa-derived-cymbal-demo`,
  `source_uri=bqaa://<table>?session_id=<sid>`,
  `revision=bqaa-trace:<trace_id>`,
  `deployment_key=cymbal-finance-prod/eu/bqaa-derived-demo`,
  `compiler_semantics_version=okf-context-compiler:v0.1`,
  `profile_contract_version=okf-context/1`,
  `adapter_version=okf-bqaa-adapter:v0`.
- `compute_identities(files, constants, manifests)` — PROFILE.md rules:
  canonical CBOR, domain-separated SHA-256, NFC, canon:v1 text, frontmatter
  split. Returns `observation_id`, `snapshot_id`, `publication_id`,
  `source_manifest_hash`, `manifest_hashes`, `file_sha256`,
  `concept_version_ids` (all `sha256:` hex ids except `file_sha256`).
- `project(result, identities, out_dir)` — writes `bundle/<path>.md`,
  `identities.json`, `observation.json`, `mapping.json`
  (`{context_ref -> publication_id}` for the retrieve envelope and the receipt
  ref). No Dataplex, kcmd, or Catalog writes.
- Compile manifests live in `fixtures/manifests/*.json` (compile semantics,
  not adapter input).
- Live identities are new. The germany triple is never force-matched.

## 4. Lookup contract (`lookup.py`, stdlib only)

- `NEVER_EMIT = ["concept_version_id", "bundle_path", "source_path",
  "principal", "user_id", "query_text", "sql", "parameter_values",
  "destination_table"]`
- `lookup(context_ref, mapping_path) -> {context_ref, publication_id,
  label: "derived/demo"}`
- Unknown `context_ref` raises `UnknownContextRefError`; the CLI exits 2.
- `never_emit_violations(payload) -> list[str]` deep-scans keys; must be
  empty for every lookup result.

## 5. CLI (`run.py`)

```
python examples/okf_bqaa_adapter/run.py                 # adapt committed live export
python examples/okf_bqaa_adapter/run.py --live          # run observe agent, export, adapt
python examples/okf_bqaa_adapter/run.py --session ID    # read BQ; fail closed if not retrieve-shaped
python examples/okf_bqaa_adapter/run.py --lookup REF    # resolve via <out>/mapping.json
python examples/okf_bqaa_adapter/run.py --out DIR       # default examples/okf_bqaa_adapter/out
```
`--session 04fa3d56-f2f1-413e-8c2b-ec116835af84` (the consume session with
the stub echo tool) must exit nonzero at the retrieve-shaped check.
Google imports happen only on the `--live` / `--session` paths.

## 6. Tests (`tests/examples/test_okf_bqaa_adapter.py`, hermetic)

No GCP, no `google.adk` / `google.cloud` imports on the default path.
1. Committed live export has **>= 100** events, both kinds, model
   `gemini-3.8-flash`, a session_id.
2. `adapt` + `compute_identities` on the live export yield `sha256:` ids and
   match `fixtures/live_identities.json`.
3. `lookup(known ref)` from the live observation works.
4. `lookup(unknown)` raises `UnknownContextRefError`.
5. `never_emit_violations(lookup result) == []`.
6. `require_retrieve_shaped` rejects a consume-shaped (stub echo) trace.
7. Optional, labelled SYNTHETIC: germany fixture identities equal the pinned
   JS triple. The JS germany receipt uses a `#2` suffix; that is hashing-only
   and is NOT the live bind rule. Live traces require exact-equal
   `context_ref`s (`refs_bound`).

## 7. Out of scope

Dataplex / kcmd / Catalog writes; reading or writing authored
`cymbal-finance-core`; issue #435, EvalBench, Week 0; the github.io viewer;
changes under `src/`; rewriting OKF v0.2 core.
