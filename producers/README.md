# bigquery-agent-analytics-tracing

Producer packages and tracing adapters that emit rows into the canonical
BQAA `agent_events` schema consumed by the
[`bigquery-agent-analytics`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK) SDK.

## Install

```bash
# Core writer + drainer + the `bqaa-otel` enterprise admin CLI
# (pipx recommended for the CLI-only enterprise-admin use case).
pip install bigquery-agent-analytics-tracing

# Storage Write API path (lower-latency, recommended for production).
pip install "bigquery-agent-analytics-tracing[storage-write]"
```

Enterprise admins deploying the OTel-native receiver: see
[the deployment guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/deploy/otlp_receiver/README.md) —
`bqaa-otel bootstrap` deploys the released, digest-pinned receiver image
with no repository checkout.

For dev work in this repo:

```bash
cd producers
pip install -e ".[dev,receiver]"
```

## Quickstart (library)

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

## Quickstart (Claude Code plugins & mods)

For coding-agent tracing in Claude Code, official plugins are maintained and published from the dedicated marketplace repository [`GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins):

- **`bigquery-agent-analytics-mod`** (recommended for Claude Code v2.1.296+): An in-process TypeScript mod that records turns, model requests (with token usage and latency), and tool executions from the background into BigQuery with zero interactive latency.
- **`bigquery-agent-analytics-tracing`**: Hook scripts (bash) that call the Python tracing package and stream events to BigQuery.

Install via the `bqaa-tracing` marketplace:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=my-gcp-project --config dataset=agent_analytics
```

Or for the hook-based tracer:

```bash
claude plugin install bigquery-agent-analytics-tracing@bqaa-tracing
```

See the [BigQuery-Agent-Analytics-Plugins repository](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-Plugins) for complete setup, configuration, and migration guides.

The plugin vendors its own copy of the tracing package, so the
`BQAA_PYTHON` interpreter does **not** need
`bigquery-agent-analytics-tracing` installed — only its runtime deps
(`google-cloud-bigquery` always; `google-cloud-bigquery-storage` +
`pyarrow` for the Storage Write path).

## Releases

Releases are cut by pushing a `tracing-vX.Y.Z` tag whose version
matches `pyproject.toml`. Full runbook in [`RELEASING.md`](RELEASING.md).
The release pipeline (`.github/workflows/release-tracing.yml`) builds
and publishes the wheel + sdist to TestPyPI then PyPI via Trusted
Publishing, and attaches the wheel, sdist, and Claude Code plugin
tarball to the GitHub release for that tag.

## Roadmap

Producer adapters tracked under
[#229](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/229)
and the publish/plugin milestone under
[#234](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/234).
