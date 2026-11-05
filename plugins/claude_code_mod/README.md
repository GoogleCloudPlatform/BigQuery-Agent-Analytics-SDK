# BigQuery Agent Analytics — Coding-Agent Mod (Moved)

`bigquery-agent-analytics-mod` has moved to the dedicated plugins repository:
[`GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins)
([`plugins/claude_code_mod/`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins/tree/main/plugins/claude_code_mod)).

## Install from the canonical marketplace

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=YOUR_PROJECT_ID \
  --config dataset=agent_analytics
```

## Moving from the former SDK marketplace

If you previously added `bqaa-tracing` from `GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK`, remove that marketplace entry first and re-add it from `GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins`:

```bash
claude plugin uninstall bigquery-agent-analytics-mod@bqaa-tracing
claude plugin marketplace remove bqaa-tracing
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=YOUR_PROJECT_ID \
  --config dataset=agent_analytics
```

See the full documentation in [`GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins):

- [Mod README](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins/blob/main/plugins/claude_code_mod/README.md)
- [Quickstart](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins/blob/main/plugins/claude_code_mod/QUICKSTART.md)

Migration guides, by what you are moving from:

- This repository's `bqaa-tracing` marketplace: [Moving from the former SDK marketplace](#moving-from-the-former-sdk-marketplace), above.
- The earlier `bigquery-agent-analytics-plugin` marketplace, or the settings-hook tracing plugin: the Plugins repository's [migration guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins/blob/main/plugins/claude_code_mod/MIGRATION.md), which also covers managed settings. It does not yet cover this repository's marketplace.
