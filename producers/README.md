# bigquery-agent-analytics-tracing

Producer packages and tracing adapters that emit rows into the canonical
BQAA `agent_events` schema consumed by the
[`bigquery-agent-analytics`](../) SDK.

## Install

```bash
# Core writer + drainer.
pip install bigquery-agent-analytics-tracing

# Storage Write API path (lower-latency, recommended for production).
pip install "bigquery-agent-analytics-tracing[storage-write]"
```

For dev work in this repo:

```bash
cd producers
pip install -e ".[dev]"
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

## Quickstart (Claude Code plugin)

If you run Claude Code, the
[Claude Code plugin artifact](../plugins/claude_code/) wires up all
nine hooks for you. Until marketplace submission lands, install from
a GitHub release tarball:

```bash
# Pick the version that matches the wheel you have installed.
VERSION=$(python -c "from bigquery_agent_analytics_tracing import __version__; print(__version__)")
mkdir -p ~/.claude/plugins
curl -L \
  "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases/download/tracing-v${VERSION}/bigquery-agent-analytics-tracing-claude-code-${VERSION}.tar.gz" \
  | tar -xz -C ~/.claude/plugins
```

The plugin tarball vendors its own copy of the tracing package, so
the `BQAA_PYTHON` interpreter does **not** need
`bigquery-agent-analytics-tracing` installed — only its runtime deps
(`google-cloud-bigquery` always; `google-cloud-bigquery-storage` +
`pyarrow` for the Storage Write path). See the
[plugin README](../plugins/claude_code/README.md) for the full runtime
model.

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
