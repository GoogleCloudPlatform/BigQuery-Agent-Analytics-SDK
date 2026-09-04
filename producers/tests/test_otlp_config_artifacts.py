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

"""Tests for the #324 admin config generation (signal + privacy tiers).

The tier semantics under test come from issue #324 and the receiver design
doc: `baseline` must never enable prompt text / raw bodies / tool content,
`replay` must require an explicit acknowledgement, and Codex must never be
promised content capture (no supported raw-body path); Codex exporter
shapes are the ones verified live against CODEX_MIN_VERSION (#317).
"""

import json

import pytest

from bigquery_agent_analytics_tracing.otlp import config_artifacts as ca

_ENDPOINT = "https://receiver.example.com"


def _spec(**kw):
  defaults = dict(endpoint=_ENDPOINT)
  defaults.update(kw)
  return ca.BootstrapSpec(**defaults)


# --------------------------------------------------------------------------
# Spec defaults + validation
# --------------------------------------------------------------------------


def test_spec_defaults_are_the_enterprise_defaults():
  spec = _spec()
  assert spec.signals == ("logs", "metrics")
  assert spec.privacy == "baseline"


def test_spec_rejects_unknown_privacy_tier():
  with pytest.raises(ValueError, match="privacy"):
    _spec(privacy="everything")


def test_spec_rejects_unknown_signal():
  with pytest.raises(ValueError, match="signal"):
    _spec(signals=("logs", "vibes"))


def test_spec_rejects_partial_signal_subsets():
  # Issue #324 defines exactly two supported tiers; a logs-only config would
  # silently still enable the metrics exporter, so reject it outright.
  with pytest.raises(ValueError, match="signal tier"):
    _spec(signals=("logs",))
  with pytest.raises(ValueError, match="signal tier"):
    _spec(signals=("logs", "traces"))


def test_spec_accepts_both_supported_tiers_in_any_order():
  assert _spec(signals=("metrics", "logs")).signals == ("metrics", "logs")
  assert _spec(signals=("traces", "logs", "metrics")).privacy == "baseline"


def test_replay_requires_explicit_acknowledgement():
  with pytest.raises(ValueError, match="acknowledge_content_logging"):
    _spec(privacy="replay")


def test_replay_allowed_with_acknowledgement():
  spec = _spec(privacy="replay", acknowledge_content_logging=True)
  assert spec.privacy == "replay"


# --------------------------------------------------------------------------
# Claude Code managed settings
# --------------------------------------------------------------------------


def test_claude_baseline_matches_issue_324_contract():
  env = ca.claude_code_managed_settings(_spec())["env"]
  assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
  assert env["OTEL_LOGS_EXPORTER"] == "otlp"
  assert env["OTEL_METRICS_EXPORTER"] == "otlp"
  assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
  assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == _ENDPOINT
  # Deterministic provenance header rides with auth (W3C comma list).
  assert env["OTEL_EXPORTER_OTLP_HEADERS"] == (
      "Authorization=Bearer <token>,x-bqaa-source-product=claude_code"
  )


def test_claude_baseline_never_enables_content():
  env = ca.claude_code_managed_settings(_spec())["env"]
  assert "OTEL_LOG_USER_PROMPTS" not in env
  assert "OTEL_LOG_TOOL_DETAILS" not in env
  assert "OTEL_TRACES_EXPORTER" not in env
  assert "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA" not in env


def test_claude_security_audit_adds_tool_details_only():
  base = ca.claude_code_managed_settings(_spec())["env"]
  audit = ca.claude_code_managed_settings(_spec(privacy="security-audit"))[
      "env"
  ]
  assert audit["OTEL_LOG_TOOL_DETAILS"] == "1"
  assert "OTEL_LOG_USER_PROMPTS" not in audit
  assert {k: v for k, v in audit.items() if k != "OTEL_LOG_TOOL_DETAILS"} == (
      base
  )


def test_claude_replay_adds_prompt_capture_and_tool_details():
  env = ca.claude_code_managed_settings(
      _spec(privacy="replay", acknowledge_content_logging=True)
  )["env"]
  assert env["OTEL_LOG_USER_PROMPTS"] == "1"
  assert env["OTEL_LOG_TOOL_DETAILS"] == "1"


def test_claude_traces_tier_enables_trace_exporter_and_beta_flag():
  # Claude Code tracing needs the enhanced-telemetry beta flag in addition to
  # the exporter (docs + receiver design doc); exporter-only config looks
  # enabled but emits no spans.
  env = ca.claude_code_managed_settings(
      _spec(signals=("logs", "metrics", "traces"))
  )["env"]
  assert env["OTEL_TRACES_EXPORTER"] == "otlp"
  assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"


def test_claude_token_is_embedded_when_provided():
  env = ca.claude_code_managed_settings(_spec(token="s3cret"))["env"]
  assert env["OTEL_EXPORTER_OTLP_HEADERS"].startswith(
      "Authorization=Bearer s3cret,"
  )


def test_claude_resource_attributes_rendered_as_kv_pairs():
  env = ca.claude_code_managed_settings(
      _spec(
          resource_attributes={
              "department": "engineering",
              "cost_center": "eng-123",
          }
      )
  )["env"]
  assert (
      env["OTEL_RESOURCE_ATTRIBUTES"]
      == "department=engineering,cost_center=eng-123"
  )


# --------------------------------------------------------------------------
# Codex config template
# --------------------------------------------------------------------------


def _toml():
  try:
    import tomllib
  except ImportError:  # Python < 3.11 — the dev extra ships tomli
    import tomli as tomllib
  return tomllib


def test_codex_p0_tier_generates_logs_and_metrics_exporters():
  # Shapes verified live against codex-cli 0.142.5 (#317 e2e): inline-table
  # exporters; metrics is P0 alongside logs. The token is EMBEDDED — codex
  # does not expand ${ENV} refs in config headers (verified live: the
  # literal header was sent and the receiver 401'd).
  otel = _toml().loads(ca.codex_config_toml(_spec()))["otel"]
  logs = otel["exporter"]["otlp-http"]
  metrics = otel["metrics_exporter"]["otlp-http"]
  assert logs["endpoint"] == f"{_ENDPOINT}/v1/logs"
  assert metrics["endpoint"] == f"{_ENDPOINT}/v1/metrics"
  for exporter in (logs, metrics):
    assert exporter["protocol"] == "binary"
    assert exporter["headers"]["Authorization"] == "Bearer <token>"
    # Deterministic provenance: explicit product header, not detection.
    assert exporter["headers"]["x-bqaa-source-product"] == "codex"
  assert otel["trace_exporter"] == "none"  # observability tier not selected
  assert otel["log_user_prompt"] is False
  assert ca.CODEX_MIN_VERSION in ca.codex_config_toml(_spec())


def test_codex_traces_tier_adds_trace_exporter():
  otel = _toml().loads(
      ca.codex_config_toml(_spec(signals=("logs", "metrics", "traces")))
  )["otel"]
  traces = otel["trace_exporter"]["otlp-http"]
  assert traces["endpoint"] == f"{_ENDPOINT}/v1/traces"
  assert traces["headers"]["x-bqaa-source-product"] == "codex"


def test_codex_token_is_embedded_when_provided():
  otel = _toml().loads(ca.codex_config_toml(_spec(token="s3cret")))["otel"]
  assert (
      otel["exporter"]["otlp-http"]["headers"]["Authorization"]
      == "Bearer s3cret"
  )


def test_codex_never_enables_prompt_capture_even_in_replay():
  toml = ca.codex_config_toml(
      _spec(privacy="replay", acknowledge_content_logging=True)
  )
  assert "log_user_prompt = false" in toml
  assert "not offered" in toml.lower() or "no supported" in toml.lower()


# --------------------------------------------------------------------------
# Artifact bundle
# --------------------------------------------------------------------------


def test_render_artifacts_covers_requested_sources():
  artifacts = ca.render_artifacts(_spec(), sources=("claude-code", "codex"))
  assert "claude-code.managed-settings.json" in artifacts
  assert "codex.config.toml" in artifacts
  # The managed-settings artifact is valid JSON with the env block.
  parsed = json.loads(artifacts["claude-code.managed-settings.json"])
  assert parsed["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


def test_render_artifacts_claude_only():
  artifacts = ca.render_artifacts(_spec(), sources=("claude-code",))
  assert "codex.config.toml" not in artifacts


def test_render_artifacts_rejects_unknown_source():
  with pytest.raises(ValueError, match="source"):
    ca.render_artifacts(_spec(), sources=("cursor",))


def test_render_artifacts_includes_endpoint_managed_guidance():
  artifacts = ca.render_artifacts(_spec(), sources=("claude-code",))
  guidance = artifacts["claude-code.endpoint-managed.md"]
  assert "managed-settings.json" in guidance
  assert "Intune" in guidance
  # Server-managed vs endpoint-managed boundary must be documented: there is
  # no admin API for server-managed settings.
  assert "no admin API" in guidance or "Owner" in guidance
