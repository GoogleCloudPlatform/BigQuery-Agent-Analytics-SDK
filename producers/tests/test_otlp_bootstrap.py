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

"""Tests for ``bqaa-otel bootstrap`` orchestration (#324 PR2).

The orchestration absorbs ``deploy/otlp_receiver/setup.sh``: same gcloud/bq
sequence, but driven through an injectable runner so the steps are testable
without GCP and so plan mode (the default) can render the commands without
executing anything.
"""

import json

import pytest

from bigquery_agent_analytics_tracing.otlp import bootstrap

_RECEIVER_URL = "https://bqaa-otlp-receiver-x.run.app"
_CONSUMER_URL = "https://bqaa-otlp-consumer-x.run.app"


class FakeRunner:
  """Records every command; answers describe/access with canned output."""

  def __init__(self, existing=()):
    self.existing = set(existing)  # resource kinds whose describe succeeds
    self.calls = []  # (argv tuple, input_text)

  def _canned(self, argv):
    joined = " ".join(argv)
    if "projects describe" in joined:
      return "123456"
    if "run services describe" in joined:
      return (
          _RECEIVER_URL if bootstrap.RECEIVER_SVC in joined else _CONSUMER_URL
      )
    if "secrets versions access" in joined:
      return "tok-from-secret"
    return ""

  def run(self, argv, input_text=None):
    self.calls.append((tuple(argv), input_text))
    return self._canned(argv)

  def try_run(self, argv, input_text=None):
    self.calls.append((tuple(argv), input_text))
    joined = " ".join(argv)
    if "describe" in joined or " ls " in f" {joined} ":
      for kind in self.existing:
        if kind in joined:
          return self._canned(argv)
      return None
    return self._canned(argv)

  # -- assertion helpers ---------------------------------------------------

  def joined(self):
    return [" ".join(argv) for argv, _ in self.calls]

  def find(self, *needles):
    return [
        (argv, inp)
        for argv, inp in self.calls
        if all(n in " ".join(argv) for n in needles)
    ]


def _settings(tmp_path, **kw):
  defaults = dict(
      project="my-proj",
      dataset="agent_analytics",
      region="us-central1",
      bq_location="US",
      out_dir=tmp_path / "artifacts",
  )
  defaults.update(kw)
  return bootstrap.BootstrapSettings(**defaults)


# --------------------------------------------------------------------------
# Execute: the setup.sh sequence
# --------------------------------------------------------------------------


def test_bootstrap_runs_the_full_deploy_sequence(tmp_path):
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  joined = r.joined()
  for expected in (
      "gcloud services enable",
      "bq --project_id=my-proj --location=US mk -f --dataset",
      "gcloud secrets create",
      "gcloud pubsub topics create bqaa-otlp",
      "gcloud pubsub topics create bqaa-otlp-dlq",
      "gcloud builds submit",
      "gcloud run deploy bqaa-otlp-receiver",
      "gcloud run deploy bqaa-otlp-consumer",
      "gcloud pubsub subscriptions create bqaa-otlp-sub",
  ):
    assert any(expected in c for c in joined), f"missing: {expected}"


def test_bootstrap_creates_schema_from_ddl_module(tmp_path):
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  [(argv, ddl_sql)] = r.find("bq", "query", "--use_legacy_sql=false")
  assert "CREATE TABLE IF NOT EXISTS `agent_analytics.otel_logs`" in ddl_sql
  assert "otel_spans" not in ddl_sql  # spans gated off by default


def test_bootstrap_traces_signal_enables_spans_everywhere(tmp_path):
  r = FakeRunner()
  bootstrap.run_bootstrap(
      _settings(tmp_path, signals=("logs", "metrics", "traces")),
      r,
      echo=lambda *_: None,
  )
  [(_, ddl_sql)] = r.find("bq", "query", "--use_legacy_sql=false")
  assert "otel_spans" in ddl_sql
  receiver = " ".join(r.find("run deploy", "receiver")[0][0])
  consumer = " ".join(r.find("run deploy", "consumer")[0][0])
  assert "BQAA_OTLP_ENABLE_TRACES=1" in receiver
  assert "BQAA_OTLP_ENABLE_TRACES=1" in consumer


def test_bootstrap_default_signals_keep_traces_off(tmp_path):
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  receiver = " ".join(r.find("run deploy", "receiver")[0][0])
  assert "BQAA_OTLP_ENABLE_TRACES=0" in receiver


def test_bootstrap_existing_secret_is_not_recreated(tmp_path):
  r = FakeRunner(existing=("secrets describe bqaa-otlp-token",))
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  assert not r.find("secrets", "create")


def test_bootstrap_subscription_has_dlq_and_oidc_push(tmp_path):
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  sub = " ".join(r.find("subscriptions create")[0][0])
  assert "--dead-letter-topic bqaa-otlp-dlq" in sub
  assert f"--push-endpoint {_CONSUMER_URL}/" in sub
  assert "--push-auth-service-account" in sub


def test_bootstrap_registers_scheduled_merge(tmp_path):
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  [(argv, _)] = r.find("--transfer_config", "mk")
  joined = " ".join(argv)
  assert "scheduled_query" in joined
  assert "MERGE `agent_analytics.agent_events_otlp`" in joined


def test_bootstrap_writes_artifacts_with_deployed_endpoint(tmp_path):
  r = FakeRunner()
  result = bootstrap.run_bootstrap(
      _settings(tmp_path, sources=("claude-code", "codex")),
      r,
      echo=lambda *_: None,
  )
  assert result.receiver_url == _RECEIVER_URL
  env = json.loads(
      (tmp_path / "artifacts" / "claude-code.managed-settings.json").read_text()
  )["env"]
  assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == _RECEIVER_URL
  # The bearer token is NOT embedded by default — artifacts are meant to be
  # committed/distributed; the summary points at the secret instead.
  assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer <token>"
  assert (tmp_path / "artifacts" / "codex.config.toml").exists()


def test_bootstrap_summary_prints_endpoints_token_and_smoke(tmp_path):
  lines = []
  bootstrap.run_bootstrap(
      _settings(tmp_path),
      FakeRunner(),
      echo=lambda *a: lines.append(" ".join(map(str, a))),
  )
  text = "\n".join(lines)
  assert f"{_RECEIVER_URL}/v1/logs" in text
  assert (
      "gcloud secrets versions access latest --secret=bqaa-otlp-token" in text
  )
  assert "test_otlp_e2e.py" in text


def test_bootstrap_replay_requires_acknowledgement(tmp_path):
  with pytest.raises(ValueError, match="acknowledge_content_logging"):
    _settings(tmp_path, privacy="replay")


# --------------------------------------------------------------------------
# Plan mode (the default)
# --------------------------------------------------------------------------


def test_render_plan_lists_commands_without_running(tmp_path):
  plan = bootstrap.render_plan(_settings(tmp_path))
  assert "gcloud run deploy bqaa-otlp-receiver" in plan
  assert "gcloud pubsub subscriptions create" in plan
  assert "--execute" in plan  # tells the admin how to apply
  # Long DDL/MERGE bodies are summarized, not dumped.
  assert "CREATE TABLE IF NOT EXISTS" not in plan


def test_render_plan_marks_capture_placeholders(tmp_path):
  plan = bootstrap.render_plan(_settings(tmp_path))
  assert "<receiver-url>" in plan or "status.url" in plan
