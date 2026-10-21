# Moving to this marketplace

For people who installed the mod from the marketplace it came from
(`bigquery-agent-analytics-plugin`, in the
`haiyuan-eng-google/bigquery_agent_analytics_skill` repository), or who use
the settings-hook tracing plugin, `bigquery-agent-analytics-tracing`.

Run one tracer per table. The mod and the settings-hook tracing plugin
write the same events, so with both on, every event is written twice.

## From the earlier marketplace

1. See what you have: `claude plugin list`.
2. Remove the earlier install, so only one copy of the mod is installed:

   ```bash
   claude plugin uninstall bigquery-agent-analytics-mod@bigquery-agent-analytics-plugin
   ```

3. Install from this marketplace with the same options (the README's
   [Install](README.md#install)):

   ```bash
   claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
     --sparse .claude-plugin plugins/claude_code_dist
   claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
     --config project=my-gcp-project --config dataset=agent_analytics
   ```

   Rows the earlier install left on disk (`~/.bqaa-mod/dead-letter`) are
   sent by this one: the folder and its format are the same.
4. Check: `/bqaa-status` names your table, with no `notice` line, and
   `/plugin` lists one mod.

## From the settings-hook tracing plugin

Turn it off for the table the mod writes to, from whichever marketplace it
came from:

```bash
claude plugin disable bigquery-agent-analytics-tracing@bqaa-tracing
claude plugin disable bigquery-agent-analytics-tracing@bigquery-agent-analytics-plugin
```

Hooks pasted into your own `~/.claude/settings.json` that run the tracer's
scripts have no plugin to disable: take them out.

## With managed settings

A policy that turned on the mod from the earlier marketplace names its
plugin id. Move it to `bigquery-agent-analytics-mod@bqaa-tracing`, and set
the earlier ids to `false` in `enabledPlugins`, which also stops them from
being installed again:

```json
"enabledPlugins": {
  "bigquery-agent-analytics-mod@bqaa-tracing": true,
  "bigquery-agent-analytics-mod@bigquery-agent-analytics-plugin": false,
  "bigquery-agent-analytics-tracing@bigquery-agent-analytics-plugin": false,
  "bigquery-agent-analytics-tracing@bqaa-tracing": false
}
```

Move the policy's `pluginConfigs` key to the new id too: options under the
old id no longer apply.

## The earlier location

In the earlier repository, only the mod's folder moves: it is replaced by a
pointer to this one. Its settings-hook tracing plugin and its analysis
skill stay where they are.
