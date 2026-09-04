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

  def __init__(self, existing=(), transfer_listing=None, failing=()):
    self.existing = set(existing)  # resource kinds whose describe succeeds
    self.transfer_listing = transfer_listing  # bq ls --transfer_config JSON
    self.failing = tuple(failing)  # substrings whose try_run fails
    self.calls = []  # (argv tuple, input_text)

  image_digest = "sha256:" + "e" * 64  # answered by 'run revisions describe'

  def _canned(self, argv):
    joined = " ".join(argv)
    if "projects describe" in joined:
      return "123456"
    if "latestCreatedRevisionName" in joined:
      return "rev-00001"
    if "run revisions describe" in joined:
      return f"us-docker.pkg.dev/x/y/z@{self.image_digest}"
    if "run services describe" in joined:
      return (
          _RECEIVER_URL if bootstrap.RECEIVER_SVC in joined else _CONSUMER_URL
      )
    return ""

  def run(self, argv, input_text=None):
    import subprocess

    self.calls.append((tuple(argv), input_text))
    joined = " ".join(argv)
    if any(f in joined for f in self.failing):
      raise subprocess.CalledProcessError(1, list(argv), stderr="boom")
    if "--transfer_config" in joined and "ls" in argv:
      return self.transfer_listing or ""
    return self._canned(argv)

  def try_run(self, argv, input_text=None):
    self.calls.append((tuple(argv), input_text))
    joined = " ".join(argv)
    if any(f in joined for f in self.failing):
      return None
    if "--transfer_config" in joined and "ls" in argv:
      return self.transfer_listing
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
      # Legacy source-build path: these tests predate the released-image
      # default (issue #349) and exercise the Cloud Build sequence.
      build_from_source=True,
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
  ddl_calls = [
      inp
      for _, inp in r.find("bq", "query", "--use_legacy_sql=false")
      if inp and "CREATE TABLE" in inp
  ]
  [ddl_sql] = ddl_calls
  assert "CREATE TABLE IF NOT EXISTS `agent_analytics.otel_logs`" in ddl_sql
  assert "otel_spans" not in ddl_sql  # spans gated off by default


def test_bootstrap_traces_signal_enables_spans_everywhere(tmp_path):
  r = FakeRunner()
  bootstrap.run_bootstrap(
      _settings(tmp_path, signals=("logs", "metrics", "traces")),
      r,
      echo=lambda *_: None,
  )
  [ddl_sql] = [
      inp
      for _, inp in r.find("bq", "query", "--use_legacy_sql=false")
      if inp and "CREATE TABLE" in inp
  ]
  assert "otel_spans" in ddl_sql
  receiver = " ".join(r.find("run deploy", "receiver")[0][0])
  consumer = " ".join(r.find("run deploy", "consumer")[0][0])
  assert "BQAA_OTLP_ENABLE_TRACES=1" in receiver
  assert "BQAA_OTLP_ENABLE_TRACES=1" in consumer


def test_consumer_gunicorn_args_use_factory_call_syntax(tmp_path):
  # '--factory' is a uvicorn flag, not gunicorn: the consumer container
  # exits 2 on startup with "unrecognized arguments: --factory" (hit live
  # during the #324 e2e — first real Cloud Run deploy of this image).
  # gunicorn >= 20.1 calls factories via the 'module:callable()' syntax.
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  argv = r.find("run deploy", "consumer")[0][0]
  consumer = " ".join(argv)
  assert "--factory" not in consumer
  # gcloud argparse treats a space-separated --args value that begins with
  # '-' as another flag ("--args: expected one argument"); it must be the
  # single-token --args=... form.
  [args_token] = [a for a in argv if a.startswith("--args")]
  assert args_token.startswith("--args=--bind")
  assert args_token.endswith("make_push_app_from_env()")


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
  sub = " ".join(r.find("subscriptions create", "bqaa-otlp-sub ")[0][0])
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
  # Display name is dataset-specific so multiple datasets can coexist.
  assert "agent_analytics" in [a for a in argv if "--display_name" in a][0]


def test_bootstrap_refreshes_stale_merge_sql_instead_of_skipping(tmp_path):
  # Re-running bootstrap after a crosswalk/MERGE change must converge the
  # scheduled query to the current SQL, not leave the stale version.
  listing = json.dumps(
      [
          {
              "name": "projects/1/locations/us/transferConfigs/42",
              "displayName": "bqaa_agent_events_otlp_merge",
              "params": {
                  "query": "MERGE `agent_analytics.agent_events_otlp` T -- OLD"
              },
          }
      ]
  )
  r = FakeRunner(transfer_listing=listing)
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  # Real bq rejects `ls --transfer_config` without --transfer_location; a
  # failing listing would silently take the create path forever.
  [(ls_argv, _)] = r.find("--transfer_config", " ls ")
  assert "--transfer_location=US" in ls_argv
  assert not r.find("--transfer_config", "mk")
  [(argv, _)] = r.find("--transfer_config", "update")
  joined = " ".join(argv)
  assert "projects/1/locations/us/transferConfigs/42" in joined
  assert "MERGE `agent_analytics.agent_events_otlp`" in joined
  assert "-- OLD" not in joined


def test_bootstrap_second_dataset_gets_its_own_merge(tmp_path):
  # A transfer config for another dataset must not swallow this dataset's
  # projection job.
  listing = json.dumps(
      [
          {
              "name": "projects/1/locations/us/transferConfigs/42",
              "displayName": "bqaa_agent_events_otlp_merge",
              "params": {
                  "query": "MERGE `agent_analytics.agent_events_otlp` T"
              },
          }
      ]
  )
  r = FakeRunner(transfer_listing=listing)
  bootstrap.run_bootstrap(
      _settings(tmp_path, dataset="ds2"), r, echo=lambda *_: None
  )
  assert not r.find("--transfer_config", "update")
  [(argv, _)] = r.find("--transfer_config", "mk")
  joined = " ".join(argv)
  assert "MERGE `ds2.agent_events_otlp`" in joined
  assert "ds2" in [a for a in argv if "--display_name" in a][0]


def test_bootstrap_listing_failure_aborts_instead_of_duplicating(tmp_path):
  # "Can't list" is not "doesn't exist": a transient bq ls failure must abort
  # (visible error), never take the create path — DTS display names are not
  # unique, so blind mk mints a duplicate scheduled MERGE on every flaky run.
  import subprocess

  import pytest

  r = FakeRunner(failing=("--transfer_config",))
  with pytest.raises(subprocess.CalledProcessError):
    bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  assert not r.find("--transfer_config", "mk")


def test_scheduled_merge_runs_as_consumer_service_account(tmp_path):
  # Without --service_account_name, DTS pins the transfer config to the
  # invoking admin's personal OAuth — the projection job dies when they leave.
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  [(argv, _)] = r.find("--transfer_config", "mk")
  sa = "bqaa-otlp-consumer@my-proj.iam.gserviceaccount.com"
  assert f"--service_account_name={sa}" in argv


def test_scheduled_merge_update_also_pins_service_account(tmp_path):
  listing = json.dumps(
      [
          {
              "name": "projects/1/locations/us/transferConfigs/42",
              "displayName": "bqaa_agent_events_otlp_merge",
              "params": {
                  "query": "MERGE `agent_analytics.agent_events_otlp` T"
              },
          }
      ]
  )
  r = FakeRunner(transfer_listing=listing)
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  [(argv, _)] = r.find("--transfer_config", "update")
  sa = "bqaa-otlp-consumer@my-proj.iam.gserviceaccount.com"
  assert f"--service_account_name={sa}" in argv
  # bq only forwards service_account_name on update when update_credentials
  # is set (checked in bq 2.1.28 source) — without it the existing config
  # silently keeps the old admin OAuth credential.
  assert "--update_credentials" in argv


def test_bootstrap_creates_dlq_retention_subscription(tmp_path):
  # Pub/Sub discards messages published to a topic with no subscription: the
  # DLQ topic needs a retention subscription or dead letters evaporate.
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  [(argv, _)] = r.find("subscriptions create", "bqaa-otlp-dlq-sub")
  joined = " ".join(argv)
  assert "--topic bqaa-otlp-dlq" in joined
  assert "--message-retention-duration" in joined
  assert "--expiration-period never" in joined


def test_bootstrap_scopes_data_editor_to_the_dataset(tmp_path):
  # jobUser needs project scope, but write access must be dataset-scoped: a
  # project-wide grant lets a compromised consumer SA modify every dataset
  # in the project. The GA mechanism is a WRITER entry in the dataset access
  # list — `bq add-iam-policy-binding --dataset` requires allowlisting and
  # fails on normal projects (hit live during the #324 e2e).
  r = FakeRunner()
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  sa = "bqaa-otlp-consumer@my-proj.iam.gserviceaccount.com"
  # No project-level dataEditor grant, no allowlisted preview API.
  assert not r.find("gcloud projects add-iam-policy-binding", "dataEditor")
  assert not r.find("add-iam-policy-binding", "--dataset")
  # jobUser stays project-level.
  [(job_argv, _)] = r.find("gcloud projects add-iam-policy-binding", "jobUser")
  assert f"serviceAccount:{sa}" in " ".join(job_argv)
  # dataEditor lands via GA BigQuery DCL (GRANT is idempotent), piped over
  # the same bq query stdin path the DDL uses ('bq update --source
  # /dev/stdin' fails too: bq requires a regular file, stdin is a pipe).
  [grant_sql] = [
      inp
      for _, inp in r.find("bq", "query", "--use_legacy_sql=false")
      if inp and inp.lstrip().startswith("GRANT")
  ]
  assert "`roles/bigquery.dataEditor`" in grant_sql
  assert "ON SCHEMA `my-proj.agent_analytics`" in grant_sql
  assert f'"serviceAccount:{sa}"' in grant_sql


def test_bootstrap_dlq_subscription_failure_is_not_swallowed(tmp_path):
  # A DLQ retention subscription that fails to create must abort the run:
  # silently continuing reports success while dead letters keep evaporating.
  import subprocess

  import pytest

  r = FakeRunner(failing=("bqaa-otlp-dlq-sub",))
  with pytest.raises(subprocess.CalledProcessError):
    bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)


def test_bootstrap_converges_existing_dlq_subscription(tmp_path):
  # An existing DLQ subscription gets its retention/expiration converged
  # rather than skipped (it may predate the retention settings).
  r = FakeRunner(existing=("subscriptions describe bqaa-otlp-dlq-sub",))
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  assert not r.find("subscriptions create", "bqaa-otlp-dlq-sub")
  [(argv, _)] = r.find("subscriptions update", "bqaa-otlp-dlq-sub")
  joined = " ".join(argv)
  assert "--message-retention-duration" in joined
  assert "--expiration-period never" in joined


def test_find_merge_config_survives_garbage_listing():
  assert bootstrap._find_merge_config("not json at all", "ds") is None
  assert (
      bootstrap._find_merge_config(
          json.dumps([{"displayName": "x", "params": None}]), "ds"
      )
      is None
  )


def test_bootstrap_rejects_non_identifier_project_and_dataset(tmp_path):
  # project/dataset feed backtick-quoted SQL identifiers (DDL, the DCL
  # GRANT, the scheduled MERGE) run under admin credentials: a
  # backtick-bearing value breaks out of the quoting and appends SQL.
  import pytest

  with pytest.raises(ValueError, match="dataset"):
    _settings(tmp_path, dataset="ds` ; DROP TABLE x;--")
  with pytest.raises(ValueError, match="project"):
    _settings(tmp_path, project="p`.hax")
  # Legitimate ids pass, incl. legacy domain-scoped projects.
  ok = _settings(
      tmp_path, project="domain.com:my-proj-1", dataset="agent_analytics_2"
  )
  assert ok.dataset == "agent_analytics_2"


def test_bootstrap_rejects_unknown_or_empty_sources(tmp_path):
  import pytest

  with pytest.raises(ValueError, match="source"):
    _settings(tmp_path, sources=("cursor",))
  with pytest.raises(ValueError, match="source"):
    _settings(tmp_path, sources=())


def test_bootstrap_repairs_existing_subscription_with_dlq_flags(tmp_path):
  # An existing (possibly drifted / pre-DLQ) subscription must be updated
  # with the same DLQ + deadline settings the create path uses.
  r = FakeRunner(failing=("subscriptions create bqaa-otlp-sub ",))
  bootstrap.run_bootstrap(_settings(tmp_path), r, echo=lambda *_: None)
  [(argv, _)] = r.find("subscriptions update", "bqaa-otlp-sub ")
  joined = " ".join(argv)
  assert "--push-endpoint" in joined
  assert "--push-auth-service-account" in joined
  assert "--dead-letter-topic bqaa-otlp-dlq" in joined
  assert "--max-delivery-attempts 5" in joined
  assert "--ack-deadline 60" in joined


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
  assert env["OTEL_EXPORTER_OTLP_HEADERS"].startswith(
      "Authorization=Bearer <token>,"
  )
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
  # Captured values that feed later commands render as placeholders.
  assert "<consumer-url>" in plan
  assert "<project-number>" in plan
  # Args with spaces are shell-quoted so a copied plan line runs unchanged.
  assert "'--schedule=every 15 minutes'" in plan


# --------------------------------------------------------------------------
# SubprocessRunner (the only code that touches real subprocesses)
# --------------------------------------------------------------------------


def test_subprocess_runner_returns_stripped_stdout_and_raises():
  import subprocess

  import pytest

  r = bootstrap.SubprocessRunner(echo=lambda *_: None)
  assert r.run(["python3", "-c", "print(' hi ')"]) == "hi"
  with pytest.raises(subprocess.CalledProcessError):
    r.run(["python3", "-c", "import sys; sys.exit(3)"])
  assert r.try_run(["python3", "-c", "import sys; sys.exit(3)"]) is None


def test_subprocess_runner_never_inherits_the_tty():
  # A command that prompts must see EOF (devnull), not block on stdin while
  # its prompt is invisible in the captured pipe.
  r = bootstrap.SubprocessRunner(echo=lambda *_: None)
  out = r.run(["python3", "-c", "import sys; print(repr(sys.stdin.read()))"])
  assert out == "''"


def test_hardened_argv_makes_clis_non_interactive():
  assert bootstrap._hardened_argv(["gcloud", "x", "y"]) == [
      "gcloud",
      "x",
      "y",
      "--quiet",
  ]
  assert bootstrap._hardened_argv(["bq", "--project_id=p", "ls"]) == [
      "bq",
      "--headless",
      "--project_id=p",
      "ls",
  ]
  assert bootstrap._hardened_argv(["docker", "ps"]) == ["docker", "ps"]
