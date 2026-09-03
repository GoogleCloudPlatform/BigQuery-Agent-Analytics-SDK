# Live ADK + BQAA observe trace → derived OKF adapter

**derived/demo · observer-only · nothing attested · not #435 · do not merge
as a gate**

A real ADK agent (`okf_rfc_observe_agent`) answers one finance question while
the `BigQueryAgentAnalyticsPlugin` streams its events into BigQuery. The
committed export of that session is the only input to a small Python adapter
that projects the observer's view (titles, types, ranks, one exclusion, one
edge, one unattested receipt) into a derived OKF v0.2 bundle and its identity
chain. The authored `cymbal-finance-core` bundle is never read or written.

## Live run on record

| Field | Value |
|-------|-------|
| session_id | `1e6dfed7-27ce-4c4d-b2e7-c45de7c241d1` |
| trace_id | `e-51cdf04f-7c34-48b7-8a3f-f2315073bfc1` |
| table | `test-project-0728-467323.okf_rfc_demo.agent_events` |
| model | `gemini-3.8-flash` (Vertex, location `global`) |
| agent | `okf_rfc_observe_agent` |
| ran_at | see `fixtures/live.json` |
| context_ref | `okf:env-observe#674153c572f6` |
| receipt | `rcpt-observe-noexec`, verdict `UNVERIFIABLE` (no-execution) |

`fixtures/live_observe_agent_events.json` holds the rows read back from that
table (JSON columns parsed, `content_parts` dropped). `fixtures/live.json`
is the run metadata. `fixtures/live_identities.json` pins the derived
observation / snapshot / publication ids for that export.

## Run

```bash
# adapt the committed live export (stdlib only, no GCP)
python examples/okf_bqaa_adapter/run.py

# resolve a context_ref through the fail-closed lookup (uses <out>/mapping.json)
python examples/okf_bqaa_adapter/run.py --lookup 'okf:env-observe#674153c572f6'

# run the ADK observe agent live, export, then adapt (needs ADC + google.adk)
export GOOGLE_CLOUD_PROJECT=test-project-0728-467323
export GOOGLE_CLOUD_LOCATION=global        # us-central1 404s gemini-3.8-flash
export GOOGLE_GENAI_USE_VERTEXAI=True
export DEMO_MODEL_ID=gemini-3.8-flash
python examples/okf_bqaa_adapter/run.py --live

# read an existing session from BigQuery; fails closed if not retrieve-shaped
python examples/okf_bqaa_adapter/run.py --session 04fa3d56-f2f1-413e-8c2b-ec116835af84
# -> FAIL_CLOSED not retrieve-shaped (that session used a stub echo tool)
```

Output goes to `--out` (default `examples/okf_bqaa_adapter/out/`, ignored by
git): `bundle/*.md`, `identities.json`, `observation.json`, `mapping.json`.

## What the pieces do

- `observe_agent.py` — the live producer. Two observer-only tools return the
  OKF envelope on their result (`kind`, `context_ref`, `okf`); the plugin
  logs it under `content.result`. Refuses `2.5` / `3.5` / `flash-latest`
  model ids before building the agent. Exports the session after shutdown
  and fails the run if the rows are not retrieve + receipt shaped.
- `adapter.py` — `observe` / `adapt` / `compute_identities` / `project`,
  a port of the github.io `adapter.js` with stdlib PROFILE.md hashing
  (canonical CBOR, domain-separated SHA-256). Adapter version
  `okf-bqaa-adapter:v0`.
- `lookup.py` — fail-closed `context_ref → publication_id` resolver over
  `mapping.json`, plus the never-emit deep scan.
- `fixtures/manifests/` — compile manifests (compile semantics, not adapter
  input).
- `fixtures/synthetic/` — **SYNTHETIC** germany trace + pinned JS
  identities. Hashing regression only; never the demo.

## Tests

```bash
pytest tests/examples/test_okf_bqaa_adapter.py -q
```

Hermetic: no GCP, no `google.adk` / `google.cloud` imports on the default
path. Tests read the committed live export, check both OKF kinds and the
model, recompute and pin the identities, exercise the lookup (known works,
unknown raises), assert the never-emit scan is empty, and reject a
consume-shaped trace.

## Not in this slice

No Dataplex / kcmd / Catalog writes. No read of authored
`cymbal-finance-core`. No github.io viewer (later it points at this run).
Nothing under `src/`. Not issue #435. See `intent.md`, `spec.md`, `plan.md`.
