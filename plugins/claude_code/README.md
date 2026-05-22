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

## Building the artifact

```bash
cd producers
python scripts/build_claude_plugin.py
# → plugins/claude_code/vendor/bigquery_agent_analytics_tracing/
# → producers/dist/bigquery-agent-analytics-tracing-claude-code-X.Y.Z.tar.gz
```

CI runs the same script and uploads the tarball as a workflow artifact.
