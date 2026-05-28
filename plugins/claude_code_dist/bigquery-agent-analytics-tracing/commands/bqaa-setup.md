---
description: Check BQAA tracing setup — env vars, Python runtime deps, vendored package import. Advisory only; never mutates the environment.
---

# /bqaa-setup

Run the BQAA tracing setup check and report what (if anything) needs fixing
before agent events can land in BigQuery.

## What to do

1. Invoke the wrapper script:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_setup_check.sh"
   ```

   The wrapper sets `PYTHONPATH` to the vendored package root and runs
   `python -m bigquery_agent_analytics_tracing.setup_check`. Exit code is
   `0` when the runtime is ready, `1` when something required is missing
   or unconfigured.

2. Surface the script's output to the user verbatim. The check is designed
   to be copy-pasteable — it already prints the exact `pip install ...`
   command for any missing dep and the exact `export BQAA_PROJECT_ID=...`
   line for any missing env var.

3. **Do not** modify the user's shell rc files, install packages on their
   behalf, or run any GCP API calls in this command. This is advisory
   only — if the user wants a guided install, they can run the suggested
   commands themselves.

## What the check covers

- `BQAA_PROJECT_ID` — required. Falls back to `GCP_PROJECT_ID` /
  `GOOGLE_CLOUD_PROJECT` if unset.
- `BQAA_DATASET` — required. Falls back to `BQ_DATASET` if unset.
- `BQAA_PYTHON` — the interpreter the hooks will exec; defaults to the
  current `python3`. The check verifies it can import the tracing
  package and required runtime deps.
- `google-cloud-bigquery` — always required (both writer paths use it).
- `google-cloud-bigquery-storage` + `pyarrow` — required only for the
  lower-latency Storage Write path; absence is reported as advisory
  (the drainer falls back to `insert_rows_json`).

## What the check does NOT do

- Create a BigQuery dataset or table.
- Grant IAM permissions.
- Install pip packages.
- Edit shell rc files or `~/.claude/settings.json`.

For the bootstrap that actually does those things, see
[`plugins/claude_code/MARKETPLACE.md`](../MARKETPLACE.md#bigquery-iam-requirements)
for the IAM model and the manual `gcloud` / `bq` commands.
