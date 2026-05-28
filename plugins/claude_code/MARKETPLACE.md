# Claude Code marketplace submission

Reference for submitting `bigquery-agent-analytics-tracing` to the
[Claude Code plugin marketplace](https://docs.claude.com/en/docs/claude-code/plugins).
Keep updated whenever metadata or install model changes.

## Pre-submission checklist

- [ ] `.claude-plugin/plugin.json` fields are final
  - [ ] `name` matches the PyPI distribution: `bigquery-agent-analytics-tracing`
  - [ ] `description` is under one sentence and accurate
  - [ ] `version` matches the `tracing-vX.Y.Z` release tag (the build
        script stamps this at release time; manual edits will be
        overwritten)
  - [ ] `author.name = "Google LLC"`
  - [ ] `repository` points at the GoogleCloudPlatform repo
  - [ ] `homepage` (if set) resolves to a public docs page
  - [ ] `license` is `Apache-2.0`
  - [ ] `keywords` cover the discoverability terms users will search
        for: `bigquery`, `agent-analytics`, `observability`, `tracing`,
        `claude-code`
  - [ ] All nine hooks declared with `${CLAUDE_PLUGIN_ROOT}/hooks/<name>.sh`
- [ ] `/bqaa-setup` slash command discoverable via `commands/bqaa-setup.md`
- [ ] `hooks/common.sh` and `scripts/run_setup_check.sh` are executable
      (`chmod +x`) in source control
- [ ] Plugin tarball produced by the release pipeline contains:
  - [ ] `.claude-plugin/plugin.json` (version stamped)
  - [ ] `hooks/` — all nine `*.sh` files + `common.sh`
  - [ ] `commands/bqaa-setup.md`
  - [ ] `scripts/run_setup_check.sh`
  - [ ] `vendor/bigquery_agent_analytics_tracing/` (full package source)
  - [ ] `vendor/bigquery_agent_analytics_tracing-X.Y.Z.dist-info/METADATA`
        (so `importlib.metadata.version()` resolves correctly)
- [ ] Tarball excludes `__pycache__`, `*.pyc`, `.gitignore`

## Runtime dependency model

Users **do not** need to `pip install bigquery-agent-analytics-tracing`.
The plugin vendors its own copy of the package. They DO need:

| Dep | Required? | Used for |
|---|---|---|
| `google-cloud-bigquery` | yes | both writer paths |
| `google-cloud-bigquery-storage` | optional | Storage Write API (lower latency, recommended for production) |
| `pyarrow` | optional | Storage Write API row encoding |

`BQAA_PYTHON` (or `python3` if unset) is the interpreter the hooks
exec. The `/bqaa-setup` command checks all of this and prints the
exact `pip install ...` line for any missing dep.

## BigQuery IAM requirements

The service account or user credentials the plugin's `BQAA_PYTHON`
will use (via ADC) needs:

| Role | Why |
|---|---|
| `roles/bigquery.dataEditor` on the destination dataset | write rows into `agent_events` |
| `roles/bigquery.user` on the project | run queries, list datasets, etc. |
| `roles/bigquery.metadataViewer` on the destination dataset | (only when `BQAA_AUTO_CREATE_TABLE=true`) read schema before deciding to create |

The plugin emits rows under the credentials' identity — there is no
per-user impersonation. Set `BQAA_AGENT_NAME` to disambiguate
multiple producers writing to the same dataset.

## Install path (pre-marketplace)

See [`README.md`](README.md#installing-the-plugin) for the curl-and-extract
recipe. Until marketplace listing is live this is the official path.

## Manual verification before each marketplace bump

Run on a clean environment (no `bigquery-agent-analytics-tracing` wheel
on `BQAA_PYTHON`):

0. Make sure no prior BQAA Claude Code plugin is active in the test
   session — `/plugin list` should show no `bigquery-agent-analytics-tracing`
   entry from any marketplace. Disable/uninstall any duplicates before
   proceeding, or you'll see doubled hook rows in `agent_events` and
   not be able to attribute them cleanly. Running multiple BQAA
   tracing plugins at once duplicates every hook write.
1. Download the release tarball for the version you're submitting:
   ```bash
   VERSION=X.Y.Z
   curl -L \
     "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases/download/tracing-v${VERSION}/bigquery-agent-analytics-tracing-claude-code-${VERSION}.tar.gz" \
     | tar -xz -C ~/.claude/plugins
   ```
2. Configure Claude Code to load the plugin per the
   [Claude Code plugins docs](https://docs.claude.com/en/docs/claude-code/plugins).
3. Inside a Claude Code session:
   - Run `/bqaa-setup` — expect a "READY" status (after configuring env
     vars + installing runtime deps as instructed).
   - Submit a real prompt. Confirm a row lands in
     `${BQAA_PROJECT_ID}.${BQAA_DATASET}.agent_events`:
     ```sql
     SELECT event_type, attributes.writer.version, attributes.writer.label
     FROM `${BQAA_PROJECT_ID}.${BQAA_DATASET}.agent_events`
     ORDER BY timestamp DESC
     LIMIT 5
     ```
   - `attributes.writer.version` and `attributes.writer.label` must
     reflect the version you just installed — NOT `0.0.0+local`. If
     they do, the vendored `.dist-info` is missing or stale.
4. Confirm `BQAA_TRACE_ENABLED=false` cleanly disables emission with
   no errors in the agent log.

## Submission process

1. Cut a `tracing-vX.Y.Z` release per [`producers/RELEASING.md`](../../producers/RELEASING.md).
2. Run the manual verification above against the published tarball.
3. Submit the plugin to the marketplace per
   [Claude Code's marketplace submission docs](https://docs.claude.com/en/docs/claude-code/plugins).
   Point the submission at the GitHub release URL.
4. Update this file's checklist completion status.
5. After acceptance, update [`README.md`](README.md) to remove the
   "until marketplace listing is live" hedge and point at the
   marketplace listing.
