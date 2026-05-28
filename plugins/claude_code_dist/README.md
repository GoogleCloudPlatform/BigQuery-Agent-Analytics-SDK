# Claude Code plugin distribution

This directory holds the **built** plugin artifacts that the Claude Code
marketplace catalog at [`/.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json)
serves to users.

The catalog uses a **relative** plugin `source` pointing into this
directory. That means users must add the marketplace via the Git
source form (`/plugin marketplace add <owner>/<repo>`), **not** by
passing a direct URL to `marketplace.json` — Claude Code can only
resolve `./plugins/claude_code_dist/...` when the surrounding repo
checkout is available locally.

## Why this exists

The source of truth for the plugin lives at
[`plugins/claude_code/`](../claude_code/). That directory uses
`0.0.0+local` as a manifest placeholder and gitignores `vendor/` so the
canonical package source under `producers/src/` and the plugin tree
cannot drift.

The marketplace can't install from that form. It needs:

- A `plugin.json` stamped with a real version (not the placeholder).
- A populated `vendor/` directory.
- A `vendor/<pkg>-<version>.dist-info/METADATA` so
  `importlib.metadata.version()` resolves at runtime.

Those are exactly what
[`producers/scripts/build_claude_plugin.py`](../../producers/scripts/build_claude_plugin.py)
produces, and what the `tracing-vX.Y.Z` release tag attaches to its
GitHub release as `bigquery-agent-analytics-tracing-claude-code-X.Y.Z.tar.gz`.

This directory is **the expanded tarball, committed**, so the marketplace
catalog's relative-path `source` can serve it.

## Release routine for this directory

After each `tracing-vX.Y.Z` cut:

```bash
VERSION=X.Y.Z
WORK=$(mktemp -d)
curl -sSL \
  "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases/download/tracing-v${VERSION}/bigquery-agent-analytics-tracing-claude-code-${VERSION}.tar.gz" \
  -o "$WORK/plugin.tar.gz"
tar -xzf "$WORK/plugin.tar.gz" -C "$WORK"

rm -rf plugins/claude_code_dist/bigquery-agent-analytics-tracing
cp -R "$WORK/bigquery-agent-analytics-tracing-claude-code-${VERSION}" \
      plugins/claude_code_dist/bigquery-agent-analytics-tracing
```

Then bump `.claude-plugin/marketplace.json`'s `version` field and PR.

Manual for v0.1.0; automate in a follow-up PR (cross-repo workflow
trigger on `tracing-v*` tags from `release-tracing.yml`).

## Do not edit the contents directly

`plugins/claude_code_dist/bigquery-agent-analytics-tracing/` is a verbatim
copy of the released tarball. Edits there get overwritten on the next
release sync. Send patches to:

- [`producers/src/bigquery_agent_analytics_tracing/`](../../producers/src/bigquery_agent_analytics_tracing/) — for the vendored Python.
- [`plugins/claude_code/`](../claude_code/) — for the plugin manifest, hooks, slash commands, scripts, and docs.
