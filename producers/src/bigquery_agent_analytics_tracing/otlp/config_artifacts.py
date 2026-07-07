# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Telemetry-source config generation for the #324 admin bootstrap.

Pure functions: a validated :class:`BootstrapSpec` (signal tier + privacy
tier + receiver coordinates) in, deployable artifacts out. No GCP calls —
the orchestration that deploys the receiver and fills in the real endpoint
/ token lives in the ``bqaa-otel bootstrap`` command (PR 2).

Tier semantics (issue #324 + the receiver design doc):

* ``baseline`` (default) — logs + metrics only. No prompt text, no raw API
  bodies, no tool output content.
* ``security-audit`` — adds tool/MCP/Bash decision detail
  (``OTEL_LOG_TOOL_DETAILS``) where the product exposes that control.
* ``replay`` — adds prompt capture (``OTEL_LOG_USER_PROMPTS``); requires an
  explicit acknowledgement because prompt text can contain full conversation
  history. Raw API *bodies* are a separate deferred fast-follow and are not
  promised here. Codex is never offered replay: it documents no supported
  raw request/response body path.

Codex exporter config is version-specific: the shapes here were verified
live against ``CODEX_MIN_VERSION`` (#317) — logs + metrics are P0, traces
generate only for the traces signal tier.
"""

from __future__ import annotations

import dataclasses
import json

PRIVACY_TIERS = ("baseline", "security-audit", "replay")
SIGNALS = ("logs", "metrics", "traces")
# Issue #324 defines exactly two signal tiers. Partial subsets are rejected
# rather than partially honored (a "logs-only" config would still enable the
# metrics exporter, which is worse than an early error).
SIGNAL_TIERS = (
    frozenset(("logs", "metrics")),
    frozenset(("logs", "metrics", "traces")),
)
SOURCES = ("claude-code", "codex")

_TOKEN_PLACEHOLDER = "<token>"

# Concrete minimum Codex version the config shapes below were verified
# against, live (#317): inline-table exporters, no env-var expansion in
# headers, event.name log-attribute convention.
CODEX_MIN_VERSION = "0.142.5"


@dataclasses.dataclass(frozen=True)
class BootstrapSpec:
  """Validated admin choices that drive config generation."""

  endpoint: str
  signals: tuple[str, ...] = ("logs", "metrics")
  privacy: str = "baseline"
  token: str | None = None
  resource_attributes: dict[str, str] | None = None
  acknowledge_content_logging: bool = False

  def __post_init__(self):
    if self.privacy not in PRIVACY_TIERS:
      raise ValueError(
          f"unknown privacy tier {self.privacy!r}; expected one of"
          f" {PRIVACY_TIERS}"
      )
    for s in self.signals:
      if s not in SIGNALS:
        raise ValueError(f"unknown signal {s!r}; expected one of {SIGNALS}")
    if frozenset(self.signals) not in SIGNAL_TIERS:
      raise ValueError(
          f"unsupported signal tier {','.join(self.signals)!r}; expected"
          " 'logs,metrics' or 'logs,metrics,traces'"
      )
    if self.privacy == "replay" and not self.acknowledge_content_logging:
      raise ValueError(
          "privacy tier 'replay' enables prompt/content logging and"
          " requires acknowledge_content_logging=True"
          " (--i-understand-content-logging)"
      )

  @property
  def bearer(self) -> str:
    return self.token if self.token is not None else _TOKEN_PLACEHOLDER


def claude_code_managed_settings(spec: BootstrapSpec) -> dict:
  """Claude Code managed-settings JSON body for the chosen tiers."""
  env = {
      "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
      "OTEL_LOGS_EXPORTER": "otlp",
      "OTEL_METRICS_EXPORTER": "otlp",
      "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
      "OTEL_EXPORTER_OTLP_ENDPOINT": spec.endpoint,
      "OTEL_EXPORTER_OTLP_HEADERS": (
          f"Authorization=Bearer {spec.bearer}"
          ",x-bqaa-source-product=claude_code"
      ),
  }
  if "traces" in spec.signals:
    # Tracing needs the enhanced-telemetry beta flag in addition to the
    # exporter; exporter-only config looks enabled but emits no spans.
    env["OTEL_TRACES_EXPORTER"] = "otlp"
    env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
  if spec.privacy in ("security-audit", "replay"):
    env["OTEL_LOG_TOOL_DETAILS"] = "1"
  if spec.privacy == "replay":
    env["OTEL_LOG_USER_PROMPTS"] = "1"
  if spec.resource_attributes:
    env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(
        f"{k}={v}" for k, v in spec.resource_attributes.items()
    )
  return {"env": env}


def _codex_exporter(spec: BootstrapSpec, path: str) -> str:
  """One verified inline-table exporter block (codex >= CODEX_MIN_VERSION).

  The token is embedded: codex does not expand ``${ENV}`` references in
  config headers (verified live — the literal header is sent and the
  receiver rejects it with 401). The provenance header keeps
  ``source_product`` deterministic on a multi-product receiver.
  """
  return (
      "{ otlp-http = { "
      f'endpoint = "{spec.endpoint}{path}", protocol = "binary", '
      f'headers = {{ "Authorization" = "Bearer {spec.bearer}", '
      '"x-bqaa-source-product" = "codex" } } }'
  )


def codex_config_toml(spec: BootstrapSpec) -> str:
  """Codex ``~/.codex/config.toml`` template for the chosen tiers.

  Logs + metrics are P0 (both shapes verified live against
  ``CODEX_MIN_VERSION``); traces are the optional observability tier —
  span/trace structure, never transcript replay. Prompt capture is never
  enabled: Codex documents no supported raw request/response body path,
  so replay is not offered.
  """
  lines = [
      "# Generated by bqaa-otel (issue #324/#317). User-level config —",
      "# [otel] is ignored in project-local config.toml.",
      f"# Shapes verified against codex-cli {CODEX_MIN_VERSION}.",
      "# Fill in the bearer token: codex does NOT expand ${ENV} refs in",
      "# headers, so this file holds the literal secret — do not commit it.",
  ]
  if spec.privacy == "replay":
    lines += [
        "# Replay tier requested: NOT offered for Codex — Codex documents",
        "# no supported raw request/response body path, so prompt capture",
        "# stays off.",
    ]
  lines += [
      "[otel]",
      'environment = "prod"',
      f"exporter = {_codex_exporter(spec, '/v1/logs')}",
      f"metrics_exporter = {_codex_exporter(spec, '/v1/metrics')}",
  ]
  if "traces" in spec.signals:
    lines += [
        "# Observability traces: span/trace structure only, not replay.",
        f"trace_exporter = {_codex_exporter(spec, '/v1/traces')}",
    ]
  else:
    lines.append('trace_exporter = "none"')
  lines.append("log_user_prompt = false")
  return "\n".join(lines) + "\n"


def endpoint_managed_guidance(spec: BootstrapSpec, source: str) -> str:
  """MDM deployment guidance for the generated managed-settings artifact."""
  del spec  # guidance is tier-independent; the JSON artifact carries tiers
  return f"""# Deploying {source} managed settings

Two distribution paths (issue #324):

## Server-managed settings (recommended)

There is **no admin API** for server-managed settings: an Owner/Primary
Owner pastes the generated `{source}.managed-settings.json` contents into
the Claude admin console (Settings -> Claude Code -> managed settings).

## Endpoint-managed settings (MDM / OS policy)

Deploy `{source}.managed-settings.json` to each machine as
`managed-settings.json`:

* **macOS** — install to
  `/Library/Application Support/ClaudeCode/managed-settings.json` via your
  MDM (e.g. a Jamf/Kandji package or a configuration profile that writes
  the file). A LaunchDaemon is not required; the file is read on startup.
* **Windows** — install to
  `C:\\ProgramData\\ClaudeCode\\managed-settings.json` via Intune
  (Win32 app or PowerShell script deployment) or Group Policy file
  preferences.

The bearer token in the file grants write access to your telemetry
endpoint — treat the artifact as a secret and prefer your MDM's secure
file distribution.
"""


def render_artifacts(
    spec: BootstrapSpec, sources: tuple[str, ...]
) -> dict[str, str]:
  """Render ``{filename: content}`` for the requested telemetry sources."""
  for source in sources:
    if source not in SOURCES:
      raise ValueError(f"unknown source {source!r}; expected one of {SOURCES}")
  artifacts: dict[str, str] = {}
  if "claude-code" in sources:
    artifacts["claude-code.managed-settings.json"] = (
        json.dumps(claude_code_managed_settings(spec), indent=2) + "\n"
    )
    artifacts["claude-code.endpoint-managed.md"] = endpoint_managed_guidance(
        spec, "claude-code"
    )
  if "codex" in sources:
    artifacts["codex.config.toml"] = codex_config_toml(spec)
  return artifacts
