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

"""Tests for the ``bqaa-otel`` admin CLI (#324 PR1: ``config`` only)."""

import json

import pytest

from bigquery_agent_analytics_tracing.otlp import cli


def _run(argv):
  return cli.main(argv)


def test_config_writes_artifacts_to_out_dir(tmp_path):
  rc = _run(
      [
          "config",
          "--endpoint",
          "https://receiver.example.com",
          "--source",
          "claude-code,codex",
          "--out",
          str(tmp_path),
      ]
  )
  assert rc == 0
  settings = json.loads(
      (tmp_path / "claude-code.managed-settings.json").read_text()
  )
  assert settings["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
  assert (tmp_path / "codex.config.toml").exists()


def test_config_defaults_to_baseline_and_logs_metrics(tmp_path):
  _run(
      [
          "config",
          "--endpoint",
          "https://receiver.example.com",
          "--source",
          "claude-code",
          "--out",
          str(tmp_path),
      ]
  )
  env = json.loads(
      (tmp_path / "claude-code.managed-settings.json").read_text()
  )["env"]
  assert "OTEL_LOG_USER_PROMPTS" not in env
  assert "OTEL_TRACES_EXPORTER" not in env


def test_config_replay_requires_ack_flag(tmp_path, capsys):
  rc = _run(
      [
          "config",
          "--endpoint",
          "https://receiver.example.com",
          "--source",
          "claude-code",
          "--privacy",
          "replay",
          "--out",
          str(tmp_path),
      ]
  )
  assert rc != 0
  err = capsys.readouterr().err
  assert "--i-understand-content-logging" in err
  assert not (tmp_path / "claude-code.managed-settings.json").exists()


def test_config_replay_with_ack_flag_warns_and_writes(tmp_path, capsys):
  rc = _run(
      [
          "config",
          "--endpoint",
          "https://receiver.example.com",
          "--source",
          "claude-code",
          "--privacy",
          "replay",
          "--i-understand-content-logging",
          "--out",
          str(tmp_path),
      ]
  )
  assert rc == 0
  assert "content logging" in capsys.readouterr().err.lower()
  env = json.loads(
      (tmp_path / "claude-code.managed-settings.json").read_text()
  )["env"]
  assert env["OTEL_LOG_USER_PROMPTS"] == "1"


def test_config_traces_signal_tier(tmp_path):
  _run(
      [
          "config",
          "--endpoint",
          "https://receiver.example.com",
          "--source",
          "claude-code",
          "--signals",
          "logs,metrics,traces",
          "--out",
          str(tmp_path),
      ]
  )
  env = json.loads(
      (tmp_path / "claude-code.managed-settings.json").read_text()
  )["env"]
  assert env["OTEL_TRACES_EXPORTER"] == "otlp"


def test_config_prints_next_admin_action(tmp_path, capsys):
  _run(
      [
          "config",
          "--endpoint",
          "https://receiver.example.com",
          "--source",
          "claude-code",
          "--out",
          str(tmp_path),
      ]
  )
  out = capsys.readouterr().out
  # Issue #324: print the exact next admin action.
  assert "managed settings" in out.lower()


def test_console_script_is_registered():
  import pathlib

  try:
    import tomllib
  except ImportError:  # Python < 3.11 — the dev extra ships tomli
    import tomli as tomllib

  # cli.py -> otlp -> bigquery_agent_analytics_tracing -> src -> producers
  pyproject = pathlib.Path(cli.__file__).parents[3] / "pyproject.toml"
  scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
  assert (
      scripts["bqaa-otel"] == "bigquery_agent_analytics_tracing.otlp.cli:main"
  )
