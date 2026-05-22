# bigquery-agent-analytics-tracing

Producer packages and tracing adapters that emit rows into the canonical
BQAA `agent_events` schema consumed by the
[`bigquery-agent-analytics`](../) SDK.

This first cut ships only the shared writer and async drainer. Producer
adapters (OpenAI Agents SDK, Codex CLI wrapper, Claude Code plugin) land in
follow-up PRs tracked under
[#229](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/229).

## Install

```bash
pip install bigquery-agent-analytics-tracing
pip install "bigquery-agent-analytics-tracing[storage-write]"  # BigQuery Storage Write API path
```

## Quickstart

```python
from bigquery_agent_analytics_tracing import (
    BQAAConfig,
    BigQueryAgentAnalyticsLogger,
)

logger = BigQueryAgentAnalyticsLogger(
    BQAAConfig(project_id="my-project", dataset="agent_analytics", dry_run=True)
)
logger.log_event(
    event_type="STATE_DELTA",
    content={"hello": "world"},
)
```

See the full architecture and roadmap in
[#229](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/229).
