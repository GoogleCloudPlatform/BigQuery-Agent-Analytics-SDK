# Plan — `examples/okf_bqaa_adapter/`

**Label: derived/demo, observer-only, nothing attested. Not #435.**

Branch `feat/okf-bqaa-adapter` off `upstream/main @ 3a560f7`. PR head is the
`caohy1988` fork. Do not merge; reviewers comment only.

## Step 1 — artifacts commit (this commit)

`intent.md`, `spec.md`, `plan.md` only.
`docs(examples): intent/spec/plan for live ADK+BQAA OKF adapter`

## Step 2 — observe agent, live run, committed export

c0806b5's 15-row single-question export is superseded and is not the demo.

- `observe_agent.py`: multi-turn (10–12 related questions, more if needed)
  under **one session_id**. Each turn calls retrieve then
  attested-computation. `create_session` once before the first `run_async`.
  `fetch_session_rows` waits for a **stable** row count (do not stop on the
  first `INVOCATION_COMPLETED`). Export gate requires **>= 100** real rows,
  both OKF kinds, and `gemini-3.8-flash`.
- Run with `GOOGLE_CLOUD_LOCATION=global`, `DEMO_MODEL_ID=gemini-3.8-flash`,
  dataset `okf_rfc_demo`, using the `.venv` python that has `google.adk`.
- If count < 100: add turns in the same session, re-export; do not PR / do
  not treat the 15-row smoke as done. Do not fake, duplicate, or pad.
- If the run fails (auth, 404), retry with location `global`; do not fall
  back to germany.
- Overwrite `fixtures/live_observe_agent_events.json`, `fixtures/live.json`,
  `fixtures/live_identities.json`.

## Step 3 — adapter, hashing, lookup, CLI

- `adapter.py`: port `observe` (dual-shape kind detection), `stubDoc`,
  `logDoc`, `adapt`, `compute_identities` (from `derived_vectors.py`),
  `project`, `require_retrieve_shaped`.
- `fixtures/manifests/*.json` copied from the reference compile manifests.
- `lookup.py`: `lookup`, `UnknownContextRefError`, `never_emit_violations`.
- `run.py`: default = committed live export; `--live`, `--session`, `--out`,
  `--lookup`.
- Write `fixtures/live_identities.json` from the live export.
- Verify `--session 04fa3d56-f2f1-413e-8c2b-ec116835af84` fails closed.
- Optional: `fixtures/synthetic/bqaa-germany.json` + `identities.json`
  labelled SYNTHETIC; port stub text closely enough that the pinned triple
  matches, otherwise pin only the hashing helpers.

## Step 4 — tests

`tests/examples/test_okf_bqaa_adapter.py` (+ `__init__.py`), hermetic, per
spec §6. `pytest tests/examples/test_okf_bqaa_adapter.py -q`.

## Step 5 — README, autoformat, PR

- `README.md` in this directory; one row in `examples/README.md` Demo
  Bundles table.
- `bash autoformat.sh`; commit any format fixes.
- `git push -u fork feat/okf-bqaa-adapter`; open PR against
  `GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK` main. Do not merge or
  self-approve.

## Later slices (not this PR)

- Catalog / Dataplex write of the derived bundle.
- github.io viewer pointed at the committed live run.
- Consume agent using `lookup.py`.
