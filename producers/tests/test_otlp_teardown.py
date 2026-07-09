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

"""bqaa-otel teardown (issue #349 PR 3).

Consumes the inventory bootstrap --execute writes; dry-run by default;
existence-verified (a probe error must never masquerade as "gone");
mode-aware via the inventory (default released-image mode records no
customer AR repo, so teardown neither expects nor deletes one).
"""

import json
import subprocess

import pytest

from bigquery_agent_analytics_tracing.otlp import bootstrap
from bigquery_agent_analytics_tracing.otlp import teardown

NOT_FOUND = "NOT_FOUND: resource does not exist"
DENIED = "PERMISSION_DENIED: caller lacks access"


class ProbeRunner:
  """Deletes succeed; existence probes answer from a substring map.

  Probe map values: "gone" -> raises with a not-found stderr,
  "denied" -> raises with a permission stderr, anything else -> exists.
  """

  def __init__(self, probes=(), transfer_listing="[]"):
    self.probes = dict(probes)
    self.transfer_listing = transfer_listing
    self.calls = []

  def run(self, argv, input_text=None):
    joined = " ".join(argv)
    self.calls.append(joined)
    if "--transfer_config" in joined and "ls" in argv:
      return self.transfer_listing
    if "get-iam-policy" in joined:
      # Reading the project policy SUCCEEDS whether or not the binding
      # exists; an empty answer means "binding gone".
      for needle, state in self.probes.items():
        if needle in joined and state == "denied":
          raise subprocess.CalledProcessError(1, list(argv), stderr=DENIED)
      return ""
    if "describe" in joined or " show " in f" {joined} ":
      for needle, state in self.probes.items():
        if needle in joined:
          if state == "gone":
            raise subprocess.CalledProcessError(1, list(argv), stderr=NOT_FOUND)
          if state == "denied":
            raise subprocess.CalledProcessError(1, list(argv), stderr=DENIED)
          return state
      raise subprocess.CalledProcessError(1, list(argv), stderr=NOT_FOUND)
    return ""

  def try_run(self, argv, input_text=None):
    try:
      return self.run(argv, input_text)
    except subprocess.CalledProcessError:
      return None


def _inventory(tmp_path, **overrides):
  inv = {
      "mode": "released",
      "image": "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver:0.2.0",
      "project": "my-proj",
      "dataset": "ds1",
      "region": "us-central1",
      "bq_location": "US",
      "cloud_run_services": [bootstrap.RECEIVER_SVC, bootstrap.CONSUMER_SVC],
      "pubsub_topics": [bootstrap.MAIN_TOPIC, bootstrap.DLQ_TOPIC],
      "pubsub_subscriptions": [
          bootstrap.SUBSCRIPTION,
          bootstrap.DLQ_SUBSCRIPTION,
      ],
      "secret": bootstrap.SECRET,
      "service_accounts": [
          bootstrap.RECEIVER_SVC,
          bootstrap.CONSUMER_SVC,
          "bqaa-otlp-push",
      ],
      "dts_display_name": f"{bootstrap.MERGE_DISPLAY_NAME}_ds1",
  }
  inv.update(overrides)
  path = tmp_path / "inventory.json"
  path.write_text(json.dumps(inv))
  return path


def _settings(tmp_path, **kw):
  # Lazily default the inventory: an eager _inventory() call would
  # overwrite a custom one the test already wrote to the same path.
  if "inventory" not in kw:
    kw["inventory"] = _inventory(tmp_path)
  defaults = dict(project="my-proj", dataset="ds1")
  defaults.update(kw)
  return teardown.TeardownSettings(**defaults)


class TestSafety:

  def test_dry_run_is_the_default_and_deletes_nothing(self, tmp_path):
    r = ProbeRunner()
    report = teardown.run_teardown(_settings(tmp_path), r, echo=lambda *_: None)
    assert report.dry_run
    joined = "\n".join(r.calls)
    assert "delete" not in joined
    assert "rm" not in joined.split()

  def test_dry_run_plan_names_every_deletion(self, tmp_path, capsys):
    lines = []
    teardown.run_teardown(_settings(tmp_path), ProbeRunner(), echo=lines.append)
    plan = "\n".join(lines)
    assert bootstrap.RECEIVER_SVC in plan
    assert bootstrap.SECRET in plan
    assert "my-proj:ds1" in plan

  def test_inventory_project_dataset_must_match_args(self, tmp_path):
    with pytest.raises(ValueError, match="inventory"):
      teardown.run_teardown(
          _settings(tmp_path, dataset="other_ds"),
          ProbeRunner(),
          echo=lambda *_: None,
      )

  def test_non_bqaa_resource_names_in_inventory_are_refused(self, tmp_path):
    path = _inventory(tmp_path, secret="prod-payments-key")
    with pytest.raises(ValueError, match="allowlist"):
      teardown.run_teardown(
          _settings(tmp_path, inventory=path),
          ProbeRunner(),
          echo=lambda *_: None,
      )

  def test_default_mode_inventory_never_touches_an_ar_repo(self, tmp_path):
    r = ProbeRunner()
    teardown.run_teardown(
        _settings(tmp_path, confirm=True), r, echo=lambda *_: None
    )
    assert not any("artifacts repositories" in c for c in r.calls)

  def test_source_mode_inventory_includes_the_ar_repo(self, tmp_path):
    path = _inventory(tmp_path, mode="source", ar_repo=bootstrap.AR_REPO)
    r = ProbeRunner()
    teardown.run_teardown(
        _settings(tmp_path, inventory=path, confirm=True),
        r,
        echo=lambda *_: None,
    )
    deletes = [c for c in r.calls if "repositories delete" in c]
    assert len(deletes) == 1 and bootstrap.AR_REPO in deletes[0]


class TestConfirmAndVerify:

  def test_confirm_deletes_and_verifies_every_class(self, tmp_path):
    r = ProbeRunner()  # all probes answer not-found after deletion
    report = teardown.run_teardown(
        _settings(tmp_path, confirm=True), r, echo=lambda *_: None
    )
    assert not report.dry_run
    assert report.ok
    joined = "\n".join(r.calls)
    assert "run services delete" in joined
    assert "secrets delete" in joined
    assert "topics delete" in joined
    assert "subscriptions delete" in joined
    assert "service-accounts delete" in joined
    assert "remove-iam-policy-binding" in joined

  def test_permission_error_is_unverifiable_not_gone(self, tmp_path):
    r = ProbeRunner(probes={bootstrap.SECRET: "denied"})
    report = teardown.run_teardown(
        _settings(tmp_path, confirm=True), r, echo=lambda *_: None
    )
    assert not report.ok
    failed = "\n".join(c.message for c in report.checks if not c.ok)
    assert "UNVERIFIABLE" in failed or "unverifiable" in failed

  def test_surviving_resource_fails_verification(self, tmp_path):
    r = ProbeRunner(probes={bootstrap.SECRET: "still-here"})
    report = teardown.run_teardown(
        _settings(tmp_path, confirm=True), r, echo=lambda *_: None
    )
    assert not report.ok

  def test_legacy_unsuffixed_dts_config_is_found_and_deleted(self, tmp_path):
    listing = json.dumps(
        [
            {
                "name": "projects/1/locations/us/transferConfigs/abc",
                "displayName": bootstrap.MERGE_DISPLAY_NAME,
                "params": {"query": "MERGE `ds1.agent_events_otlp` t ..."},
            }
        ]
    )
    r = ProbeRunner(transfer_listing=listing)
    teardown.run_teardown(
        _settings(tmp_path, confirm=True), r, echo=lambda *_: None
    )
    assert any("--transfer_config" in c and "rm" in c.split() for c in r.calls)

  def test_dataset_only_skips_the_pipeline_tier(self, tmp_path):
    r = ProbeRunner()
    teardown.run_teardown(
        _settings(tmp_path, confirm=True, dataset_only=True),
        r,
        echo=lambda *_: None,
    )
    joined = "\n".join(r.calls)
    assert "run services delete" not in joined
    assert "secrets delete" not in joined
    assert any("rm" in c.split() and "--dataset" in c for c in r.calls)


class TestLiveReconstruction:

  def test_missing_inventory_reconstructs_from_constants(self, tmp_path):
    settings = teardown.TeardownSettings(
        project="my-proj", dataset="ds1", inventory=None
    )
    lines = []
    report = teardown.run_teardown(settings, ProbeRunner(), echo=lines.append)
    assert report.dry_run
    plan = "\n".join(lines)
    # The reconstructed plan covers every deterministic resource class...
    assert bootstrap.SECRET in plan
    assert bootstrap.RECEIVER_SVC in plan
    assert bootstrap.DLQ_SUBSCRIPTION in plan
    assert "my-proj:ds1" in plan
    # ...and never assumes a source-build AR repo.
    assert "repositories delete" not in plan


class TestDtsListingGate:

  def test_unreadable_dts_listing_refuses_before_any_deletion(self, tmp_path):
    class ListingFailsRunner(ProbeRunner):

      def run(self, argv, input_text=None):
        joined = " ".join(argv)
        if "--transfer_config" in joined and "ls" in argv:
          self.calls.append(joined)
          raise subprocess.CalledProcessError(1, list(argv), stderr="boom")
        return super().run(argv, input_text)

    r = ListingFailsRunner()
    with pytest.raises(RuntimeError, match="DTS"):
      teardown.run_teardown(
          _settings(tmp_path, confirm=True), r, echo=lambda *_: None
      )
    # The refusal happened BEFORE anything destructive ran: the scheduled
    # MERGE must never survive while the rest of the deployment is gone.
    assert not any("delete" in c for c in r.calls)
    assert not any("rm" in c.split() for c in r.calls)
