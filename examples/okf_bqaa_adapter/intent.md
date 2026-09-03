# Intent — live ADK + BQAA observe trace → derived OKF adapter

**Label: derived/demo, observer-only, nothing attested. Not #435.**

## Why this example exists

A real ADK agent (`okf_rfc_observe_agent`, `gemini-3.8-flash`, Vertex
location `global`) answers one finance question while the
`BigQueryAgentAnalyticsPlugin` streams its events into
`test-project-0728-467323.okf_rfc_demo.agent_events`. The agent's tools
return retrieve-shaped and receipt-shaped payloads (`okf-context:retrieve`,
`okf-context:attested-computation`). Those `agent_events` rows are the
**only** input to the adapter in this directory, which projects them into a
derived OKF v0.2 bundle plus its identity chain
(observation_id / snapshot_id / publication_id).

The point being demonstrated: an observer that sees nothing but BQAA
`agent_events` can still derive a governed, hashable context bundle. It never
reads the authored `cymbal-finance-core` bundle, never executes SQL, and never
sees the principal, query text, parameter values, bundle paths, or
`concept_version_id`.

## What is the source of truth

- `fixtures/live_observe_agent_events.json` — the committed BigQuery export
  of the live session (rows parsed from the `agent_events` JSON columns).
- `fixtures/live.json` — session_id, trace_id, agent, model, dataset, table,
  project, Vertex location, ran_at.

These two files are the demo proof and the adapter's default input.
Reviewers can re-query the session in BigQuery and compare.

`fixtures/synthetic/bqaa-germany.json` (if present) is a **SYNTHETIC**
hashing regression against the pinned JS identities from the github.io
prototype. It is not the demo and never the default input.

## Posture

- One-way, observer-only. `agent_events` → derived bundle. Nothing is written
  back to BigQuery, Dataplex, kcmd, or any catalog in this slice.
- derived/demo. Every emitted stub says "Derived from BQAA observation, not
  authored." Every identity is new for the live trace; the germany
  publication_id is not force-matched.
- Nothing attested. The receipt tool does not execute anything and reports
  verdict `UNVERIFIABLE` with reason `no-execution`.
- Fail closed. Unknown `context_ref` lookups raise. Traces that are not
  retrieve-shaped (for example the earlier consume session
  `04fa3d56-f2f1-413e-8c2b-ec116835af84`, which used a stub echo tool) are
  rejected by `--session`.

## Later, not this PR

- github.io becomes a viewer of this committed run.
- A consume agent resolves `context_ref` through `lookup.py`.
- Catalog / Dataplex publication of the derived bundle.
- Nothing here touches issue #435, EvalBench, or the Week 0 preregistration.
  No merge pressure: this is a reviewable example, not a gate.
