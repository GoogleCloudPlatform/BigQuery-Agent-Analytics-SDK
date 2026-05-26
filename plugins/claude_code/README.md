# BigQuery Agent Analytics — Claude Code plugin

Native Claude Code plugin that streams hook events to the BigQuery
Agent Analytics `agent_events` table.

## Layout

```
plugins/claude_code/
├── .claude-plugin/
│   └── plugin.json          # native Claude Code manifest
├── hooks/
│   ├── common.sh            # shared hook entry (PYTHONPATH + python -m)
│   └── <hook>.sh            # one per Claude Code hook
└── vendor/                  # generated at build time (git-ignored)
    └── bigquery_agent_analytics_tracing/
                             # vendored package source — copied from
                             # producers/src/ by build_claude_plugin.py
```

`vendor/` is never source-controlled. The build script
[`producers/scripts/build_claude_plugin.py`](../../producers/scripts/build_claude_plugin.py)
copies the canonical package source into `vendor/` and stamps the
resolved version into `.claude-plugin/plugin.json` at release time —
so the wheel and the plugin always ship the same code.

## Runtime model

1. Claude Code fires a hook (e.g. `PreToolUse`) per the manifest.
2. The matching `hooks/<hook>.sh` execs `hooks/common.sh <HookName>`.
3. `common.sh` prepends `${CLAUDE_PLUGIN_ROOT}/vendor` to `PYTHONPATH`
   and execs
   `python -m bigquery_agent_analytics_tracing.claude_code <HookName>`.
4. The hook process writes one spool envelope and spawns the drainer
   subprocess. The drainer inherits the same `PYTHONPATH` via
   `os.environ.copy()` so the vendored package resolves on the spawned
   process too — no separate wheel install required on `BQAA_PYTHON`.

## Required runtime deps on `BQAA_PYTHON`

The plugin does **not** require installing `bigquery-agent-analytics-tracing`
itself, but the Python under `BQAA_PYTHON` still needs:

- `google-cloud-bigquery` — required for both writer paths.
- `google-cloud-bigquery-storage` + `pyarrow` — required only for the
  Storage Write API path; the drainer falls back to `insert_rows_json`
  when these are unavailable.

A future `/bqaa-setup` slash command (planned in #234 step 5) will
surface a single `pip install ...` line for missing deps.

## Installing the plugin

Until the plugin is submitted to the Claude Code marketplace, install
from a GitHub release tarball:

```bash
# Pick a released version from
# https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases
VERSION=0.1.0.dev0

mkdir -p ~/.claude/plugins
curl -L \
  "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases/download/tracing-v${VERSION}/bigquery-agent-analytics-tracing-claude-code-${VERSION}.tar.gz" \
  | tar -xz -C ~/.claude/plugins

# Configure BigQuery destination + runtime deps (one-time).
export BQAA_PROJECT_ID=your-gcp-project
export BQAA_DATASET=agent_analytics
pip install google-cloud-bigquery               # always
pip install google-cloud-bigquery-storage pyarrow  # Storage Write path
```

Then point Claude Code at the unpacked plugin directory per the
[Claude Code plugins docs](https://docs.claude.com/en/docs/claude-code/plugins).

Marketplace submission is tracked in
[#234 step 5](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/234).

## Building the artifact

```bash
cd producers
python scripts/build_claude_plugin.py
# → plugins/claude_code/vendor/bigquery_agent_analytics_tracing/
# → producers/dist/bigquery-agent-analytics-tracing-claude-code-X.Y.Z.tar.gz
```

CI builds the same tarball on every release tag (see
[producers/RELEASING.md](../../producers/RELEASING.md)). On regular PRs
the plugin tarball is uploaded as a workflow artifact via the
`producers-ci.yml` `build` job.
