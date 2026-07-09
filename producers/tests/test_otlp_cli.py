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


# --------------------------------------------------------------------------
# bootstrap subcommand (PR2)
# --------------------------------------------------------------------------


def test_bootstrap_default_is_plan_mode(tmp_path, capsys, monkeypatch):
  from bigquery_agent_analytics_tracing.otlp import bootstrap

  def _boom(*a, **k):
    raise AssertionError("plan mode must not execute")

  monkeypatch.setattr(bootstrap.SubprocessRunner, "run", _boom)
  rc = _run(
      [
          "bootstrap",
          "--project",
          "my-proj",
          "--build-from-source",
          "--out",
          str(tmp_path),
      ]
  )
  assert rc == 0
  out = capsys.readouterr().out
  assert "--execute" in out
  assert "gcloud run deploy bqaa-otlp-receiver" in out


def test_bootstrap_execute_invokes_run_bootstrap(tmp_path, monkeypatch):
  import pathlib as _pathlib

  from bigquery_agent_analytics_tracing.otlp import bootstrap

  # --execute refuses to run outside the repo root (Cloud Build context).
  monkeypatch.chdir(_pathlib.Path(bootstrap.__file__).parents[4])
  seen = {}

  def _fake_run_bootstrap(settings, runner, **kw):
    seen["settings"] = settings
    seen["runner"] = runner
    return bootstrap.BootstrapResult("https://r", "https://c", ())

  monkeypatch.setattr(bootstrap, "run_bootstrap", _fake_run_bootstrap)
  rc = _run(
      [
          "bootstrap",
          "--project",
          "my-proj",
          "--dataset",
          "ds1",
          "--signals",
          "logs,metrics,traces",
          "--build-from-source",
          "--out",
          str(tmp_path),
          "--execute",
      ]
  )
  assert rc == 0
  assert seen["settings"].project == "my-proj"
  assert seen["settings"].dataset == "ds1"
  assert seen["settings"].enable_spans is True
  assert isinstance(seen["runner"], bootstrap.SubprocessRunner)


def test_bootstrap_replay_requires_ack_flag(capsys):
  rc = _run(
      [
          "bootstrap",
          "--project",
          "my-proj",
          "--privacy",
          "replay",
      ]
  )
  assert rc != 0
  assert "--i-understand-content-logging" in capsys.readouterr().err


# --------------------------------------------------------------------------
# bootstrap failure modes (#331 full review)
# --------------------------------------------------------------------------


def test_bootstrap_execute_surfaces_failed_step_stderr(monkeypatch, capsys):
  import subprocess

  from bigquery_agent_analytics_tracing.otlp import bootstrap

  def _boom(*a, **k):
    raise subprocess.CalledProcessError(
        1, ["gcloud", "run", "deploy"], stderr="PERMISSION_DENIED: nope"
    )

  monkeypatch.setattr(bootstrap, "run_bootstrap", _boom)
  monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)
  rc = _run(["bootstrap", "--project", "p", "--build-from-source", "--execute"])
  assert rc == 1
  err = capsys.readouterr().err
  assert "gcloud run deploy" in err
  assert "PERMISSION_DENIED: nope" in err


def test_bootstrap_execute_requires_repo_root(monkeypatch, tmp_path, capsys):
  from bigquery_agent_analytics_tracing.otlp import bootstrap

  def _never(*a, **k):
    raise AssertionError("must not deploy without the Dockerfile present")

  monkeypatch.setattr(bootstrap, "run_bootstrap", _never)
  monkeypatch.chdir(tmp_path)  # no deploy/otlp_receiver/Dockerfile here
  rc = _run(["bootstrap", "--project", "p", "--build-from-source", "--execute"])
  assert rc == 2
  assert "repository root" in capsys.readouterr().err


def test_bootstrap_dev_checkout_requires_image_choice(capsys):
  # Development checkout: no released image is embedded, so the CLI must
  # fail actionably BEFORE planning or mutating anything (#349).
  rc = _run(["bootstrap", "--project", "p"])
  assert rc == 2
  err = capsys.readouterr().err
  assert "--image" in err and "--build-from-source" in err


def test_bootstrap_rejects_bogus_source(capsys):
  rc = _run(["bootstrap", "--project", "p", "--source", "cursor"])
  assert rc == 2
  assert "source" in capsys.readouterr().err


# --------------------------------------------------------------------------
# verify subcommand (PR3)
# --------------------------------------------------------------------------


def _fake_results(*, fail=False, warn=False):
  from bigquery_agent_analytics_tracing.otlp import verify

  results = [verify.CheckResult("endpoint reachable", True, "200")]
  if warn:
    results.append(
        verify.CheckResult("recent rows in otel_logs", False, "0", True)
    )
  if fail:
    results.append(verify.CheckResult("tables and views exist", False, "gone"))
  return results


def test_verify_green_exits_zero(capsys, monkeypatch):
  from bigquery_agent_analytics_tracing.otlp import verify

  monkeypatch.setattr(
      verify, "run_verify", lambda *a, **k: _fake_results(warn=True)
  )
  monkeypatch.setattr(verify, "make_query_rows", lambda project: lambda q: [])
  rc = _run(
      [
          "verify",
          "--endpoint",
          "https://r",
          "--token",
          "tok",
          "--project",
          "p",
          "--dataset",
          "ds",
      ]
  )
  assert rc == 0
  out = capsys.readouterr().out
  assert "OK" in out and "WARN" in out


def test_verify_failure_exits_nonzero(capsys, monkeypatch):
  from bigquery_agent_analytics_tracing.otlp import verify

  monkeypatch.setattr(
      verify, "run_verify", lambda *a, **k: _fake_results(fail=True)
  )
  monkeypatch.setattr(verify, "make_query_rows", lambda project: lambda q: [])
  rc = _run(
      [
          "verify",
          "--endpoint",
          "https://r",
          "--token",
          "tok",
          "--project",
          "p",
          "--dataset",
          "ds",
      ]
  )
  assert rc == 1
  assert "FAIL" in capsys.readouterr().out


def test_verify_smoke_flag_also_runs_smoke(monkeypatch):
  from bigquery_agent_analytics_tracing.otlp import verify

  called = {}
  monkeypatch.setattr(verify, "run_verify", lambda *a, **k: _fake_results())
  monkeypatch.setattr(
      verify,
      "run_smoke",
      lambda *a, **k: called.setdefault("smoke", True) and _fake_results(),
  )
  monkeypatch.setattr(verify, "make_query_rows", lambda project: lambda q: [])
  rc = _run(
      [
          "verify",
          "--endpoint",
          "https://r",
          "--token",
          "tok",
          "--project",
          "p",
          "--dataset",
          "ds",
          "--smoke",
      ]
  )
  assert rc == 0
  assert called.get("smoke")


def test_verify_token_falls_back_to_env(monkeypatch):
  from bigquery_agent_analytics_tracing.otlp import verify

  seen = {}

  def _record(settings, **kw):
    seen["token"] = settings.token
    return _fake_results()

  monkeypatch.setattr(verify, "run_verify", _record)
  monkeypatch.setattr(verify, "make_query_rows", lambda project: lambda q: [])
  monkeypatch.setenv("BQAA_OTLP_TOKEN", "env-tok")
  rc = _run(
      ["verify", "--endpoint", "https://r", "--project", "p", "--dataset", "ds"]
  )
  assert rc == 0
  assert seen["token"] == "env-tok"


# --------------------------------------------------------------------------
# verify hardening (#332 full review)
# --------------------------------------------------------------------------


def test_verify_missing_token_exits_two(monkeypatch, capsys):
  monkeypatch.delenv("BQAA_OTLP_TOKEN", raising=False)
  rc = _run(
      ["verify", "--endpoint", "https://r", "--project", "p", "--dataset", "d"]
  )
  assert rc == 2
  assert "token" in capsys.readouterr().err.lower()


def test_verify_rejects_plain_http_for_remote_hosts(monkeypatch, capsys):
  monkeypatch.setenv("BQAA_OTLP_TOKEN", "tok")
  rc = _run(
      [
          "verify",
          "--endpoint",
          "http://receiver.example.com",
          "--project",
          "p",
          "--dataset",
          "d",
      ]
  )
  assert rc == 2
  assert "https" in capsys.readouterr().err.lower()


def test_verify_rejects_mixed_case_http_scheme(monkeypatch, capsys):
  # URL schemes are case-insensitive: HTTP:// must not bypass the guard.
  monkeypatch.setenv("BQAA_OTLP_TOKEN", "tok")
  rc = _run(
      [
          "verify",
          "--endpoint",
          "HTTP://receiver.example.com",
          "--project",
          "p",
          "--dataset",
          "d",
      ]
  )
  assert rc == 2
  assert "https" in capsys.readouterr().err.lower()


def test_verify_allows_http_for_localhost(monkeypatch):
  from bigquery_agent_analytics_tracing.otlp import verify

  monkeypatch.setenv("BQAA_OTLP_TOKEN", "tok")
  monkeypatch.setattr(verify, "run_verify", lambda *a, **k: _fake_results())
  monkeypatch.setattr(verify, "make_query_rows", lambda project: lambda q: [])
  rc = _run(
      [
          "verify",
          "--endpoint",
          "http://127.0.0.1:9999",
          "--project",
          "p",
          "--dataset",
          "d",
      ]
  )
  assert rc == 0


def test_verify_invalid_signals_exit_two(monkeypatch, capsys):
  monkeypatch.setenv("BQAA_OTLP_TOKEN", "tok")
  rc = _run(
      [
          "verify",
          "--endpoint",
          "https://r",
          "--project",
          "p",
          "--dataset",
          "d",
          "--signals",
          "logs,metrics,trace",
      ]
  )
  assert rc == 2
  assert "signal" in capsys.readouterr().err.lower()


def test_verify_plumbs_settings_and_timeout(monkeypatch):
  from bigquery_agent_analytics_tracing.otlp import verify

  monkeypatch.setenv("BQAA_OTLP_TOKEN", "tok")
  seen = {}

  def record_verify(settings, **kw):
    seen["settings"] = settings
    return _fake_results()

  def record_smoke(settings, **kw):
    seen["timeout_s"] = kw.get("timeout_s")
    return _fake_results()

  monkeypatch.setattr(verify, "run_verify", record_verify)
  monkeypatch.setattr(verify, "run_smoke", record_smoke)
  monkeypatch.setattr(verify, "make_query_rows", lambda project: lambda q: [])
  rc = _run(
      [
          "verify",
          "--endpoint",
          "https://r",
          "--project",
          "p",
          "--dataset",
          "d",
          "--signals",
          "logs,metrics,traces",
          "--recent-hours",
          "6",
          "--timeout",
          "9",
          "--smoke",
      ]
  )
  assert rc == 0
  assert seen["settings"].signals == ("logs", "metrics", "traces")
  assert seen["settings"].recent_hours == 6
  assert seen["timeout_s"] == 9


def test_verify_rejects_schemeless_endpoint(monkeypatch, capsys):
  monkeypatch.setenv("BQAA_OTLP_TOKEN", "tok")
  rc = _run(
      [
          "verify",
          "--endpoint",
          "receiver.example.com",
          "--project",
          "p",
          "--dataset",
          "d",
      ]
  )
  assert rc == 2
  assert "http" in capsys.readouterr().err.lower()


def test_verify_malformed_ipv6_endpoint_exits_cleanly(monkeypatch, capsys):
  monkeypatch.setenv("BQAA_OTLP_TOKEN", "tok")
  rc = _run(
      [
          "verify",
          "--endpoint",
          "http://[::1",
          "--project",
          "p",
          "--dataset",
          "d",
      ]
  )
  assert rc == 2
  assert "endpoint" in capsys.readouterr().err.lower()
