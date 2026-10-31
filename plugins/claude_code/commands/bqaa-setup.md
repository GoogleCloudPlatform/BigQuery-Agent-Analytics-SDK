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

   The wrapper prepends the vendored package root to `PYTHONPATH`,
   dropping empty entries, and runs
   `bigquery_agent_analytics_tracing.setup_check` like `python -m`,
   without adding the working directory to `sys.path`. Non-empty
   `PYTHONPATH` entries you set are kept as given, so `.` or another
   relative entry can still import code from the working directory.
   Exit code is `0` when the runtime is ready, `1` when something
   required is missing or unconfigured.

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
- `BQAA_TRACE_ENABLED` — reported as `on` / `off`; on when unset. When
  off, the hooks emit nothing. Informational only; does not affect the
  exit code.
- `BQAA_DRY_RUN` — reported as `on` / `off`; off when unset. When
  `BQAA_DRY_RUN=true`, the hooks try to write events to `BQAA_LOG_FILE`
  instead of BigQuery, and the report prints that log path. The runtime
  drops dry-run events silently if it refuses the path (for example a
  read-only file, a symlink, or a file in a directory that your group or
  other users can write), so the report does not promise delivery. When
  `BQAA_LOG_FILE` is unset, the default log file lives under the temp
  directory, which each process resolves from its own `TMPDIR`;
  sandboxed shell commands can see a different `TMPDIR` than the hooks.
  A relative `BQAA_LOG_FILE` resolves against each hook's working
  directory, and `~` or `~/...` against each hook's `HOME`. A
  `~user/...` path instead expands through the account database, the
  same for every process; if that user does not exist, the runtime
  cannot expand it and discards dry-run events, and the report says so.
  Set `BQAA_LOG_FILE` to an absolute path for dry-run walkthroughs. In
  `--json`, `log_file_is_default: true` and `log_file_is_relative: true`
  mark `log_file` as a path the hooks may resolve differently. If
  `BQAA_LOG_FILE` is set but empty, dry-run events are discarded, and if
  `BQAA_TRACE_ENABLED` is off nothing is emitted; the report says so in
  both cases. Unset `BQAA_DRY_RUN` (or set it to `false`) before a live
  BigQuery smoke test. Informational only; does not affect the exit
  code.

Accepted values for both flags: only the word `true`, in any letter
case and with no surrounding whitespace, turns a flag on. Every other
set value turns it off, including `1`, `yes`, `on`, padded values such
as ` true`, and an empty value. The hooks, the runtime and this report
all apply the same rule. Upgrade note: earlier versions of this
plugin's hooks stopped at the shell gate for `BQAA_TRACE_ENABLED=TRUE`
or `True` and emitted nothing, so an existing uppercase setting now
starts emission. The sibling `claude_code_mod` plugin parses these
variables differently: it trims and lower-cases the value, turns
`BQAA_DRY_RUN` on for `true`, `1`, `yes` or `on`, and keeps tracing on
unless `BQAA_TRACE_ENABLED` is `false`, `0`, `no` or `off`.
- `google-cloud-bigquery` — always required (both writer paths use it).
- `google-cloud-bigquery-storage` + `pyarrow` — required only for the
  lower-latency Storage Write path; absence is reported as advisory
  (the drainer falls back to `insert_rows_json`).
- Numeric `BQAA_*` settings — `BQAA_MAX_CONTENT_LENGTH`,
  `BQAA_TRANSCRIPT_MAX_BYTES` and `BQAA_DRAIN_BATCH_SIZE` must be
  integers (so `5.0` and `1e3` are rejected); `BQAA_STATE_TTL_HOURS`,
  `BQAA_DRAIN_IDLE_SECONDS` and `BQAA_DRAIN_POLL_SECONDS` must parse as
  numbers, and the two `BQAA_DRAIN_*_SECONDS` timings must also be
  finite and above zero.
  If one is not, the hooks cannot load their configuration and emit no
  trace events. Each hook still exits 0. When tracing is enabled it prints the error
  to stderr and tries to append it to the configured log file (a
  logging failure does not stop the hook); when tracing is off it stays
  silent. The check reports `[INVALID]` with the variable name (not the
  value) and exits `1`.

## What the check does NOT do

- Create a BigQuery dataset or table.
- Grant IAM permissions.
- Install pip packages.
- Edit shell rc files or `~/.claude/settings.json`.

For the bootstrap that actually does those things, see
[`plugins/claude_code/MARKETPLACE.md`](../MARKETPLACE.md#bigquery-iam-requirements)
for the IAM model and the manual `gcloud` / `bq` commands.
