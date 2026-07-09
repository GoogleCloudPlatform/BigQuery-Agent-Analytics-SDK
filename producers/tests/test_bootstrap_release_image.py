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

"""Released-image wiring in bootstrap (issue #349 PR 2).

Default customer path: the wheel embeds the released public image
(pinned by digest) and bootstrap deploys it with ZERO Cloud Build /
customer Artifact Registry footprint. `--image` overrides explicitly
(the TestPyPI gate deploys the staging image this way);
`--build-from-source` keeps the legacy repo-checkout path.
"""

import json
import pathlib
import sys

import pytest

from bigquery_agent_analytics_tracing.otlp import _release
from bigquery_agent_analytics_tracing.otlp import bootstrap

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_otlp_bootstrap import _settings
from test_otlp_bootstrap import FakeRunner

RELEASED = (
    "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver:0.2.0@sha256:"
    + "e" * 64
)
STAGING = (
    "us-docker.pkg.dev/bqaa-releases/bqaa-staging/otlp-receiver:"
    "0.2.0-candidate.99@sha256:" + "e" * 64
)


def _run(settings, runner=None):
  r = runner or FakeRunner()
  bootstrap.run_bootstrap(settings, r, echo=lambda *_: None)
  return r


def _deploy_images(r):
  return [
      c[c.index("--image") + 1]
      for c, _ in r.calls
      if "deploy" in c and "--image" in c
  ]


class TestReleasedDefault:

  def test_packaged_release_image_is_the_default(self, tmp_path, monkeypatch):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", RELEASED)
    r = _run(_settings(tmp_path, build_from_source=False))
    assert _deploy_images(r) == [RELEASED, RELEASED]

  def test_prebuilt_mode_has_zero_build_footprint(self, tmp_path, monkeypatch):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", RELEASED)
    r = _run(_settings(tmp_path, build_from_source=False))
    joined = [" ".join(c) for c, _ in r.calls]
    assert not any("builds submit" in j for j in joined)
    assert not any("artifacts repositories" in j for j in joined)
    enable = next(j for j in joined if "services enable" in j)
    assert "cloudbuild" not in enable
    assert "artifactregistry" not in enable

  def test_dev_checkout_requires_explicit_choice(self, tmp_path, monkeypatch):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", None)
    with pytest.raises(ValueError, match="--image.*--build-from-source"):
      _run(_settings(tmp_path, build_from_source=False))


class TestExplicitOverride:

  def test_image_override_wins_over_packaged_default(
      self, tmp_path, monkeypatch
  ):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", RELEASED)
    r = _run(_settings(tmp_path, build_from_source=False, image=STAGING))
    assert _deploy_images(r) == [STAGING, STAGING]

  def test_plan_output_names_the_image(self, tmp_path, monkeypatch):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", None)
    plan = bootstrap.render_plan(
        _settings(tmp_path, build_from_source=False, image=STAGING)
    )
    assert "bqaa-staging" in plan
    assert "builds submit" not in plan

  @pytest.mark.parametrize(
      "bad",
      [
          "us-docker.pkg.dev/p/r/otlp-receiver",  # no tag
          "us-docker.pkg.dev/p/r/otlp-receiver:latest",  # never 'latest'
          "us-docker.pkg.dev/p/r/otlp-receiver:0.2.0@sha256:short",
          "bad image`with:injection",
      ],
  )
  def test_malformed_image_rejected(self, tmp_path, bad):
    with pytest.raises(ValueError):
      _settings(tmp_path, build_from_source=False, image=bad)

  def test_image_and_build_from_source_are_mutually_exclusive(self, tmp_path):
    with pytest.raises(ValueError, match="mutually exclusive"):
      _settings(tmp_path, build_from_source=True, image=STAGING)


class TestSourceBuildPath:

  def test_build_from_source_keeps_legacy_behavior(self, tmp_path):
    r = _run(_settings(tmp_path))  # helper default: build_from_source=True
    joined = [" ".join(c) for c, _ in r.calls]
    assert any("builds submit" in j for j in joined)
    enable = next(j for j in joined if "services enable" in j)
    assert "cloudbuild" in enable and "artifactregistry" in enable


class TestDigestAssertion:

  def test_digest_pinned_deploy_asserts_deployed_digest(
      self, tmp_path, monkeypatch
  ):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", RELEASED)
    r = FakeRunner()
    r.image_digest = "sha256:" + "e" * 64
    _run(_settings(tmp_path, build_from_source=False), r)
    joined = [" ".join(c) for c, _ in r.calls]
    assert any("run revisions describe" in j for j in joined)

  def test_digest_mismatch_fails_the_bootstrap(self, tmp_path, monkeypatch):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", RELEASED)
    r = FakeRunner()
    r.image_digest = "sha256:" + "f" * 64
    with pytest.raises(RuntimeError, match="digest"):
      _run(_settings(tmp_path, build_from_source=False), r)

  def test_source_build_skips_digest_assertion(self, tmp_path):
    r = _run(_settings(tmp_path))
    joined = [" ".join(c) for c, _ in r.calls]
    assert not any("run revisions describe" in j for j in joined)


class TestInventory:

  def test_inventory_records_the_deployed_image_and_resources(
      self, tmp_path, monkeypatch
  ):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", RELEASED)
    _run(_settings(tmp_path, build_from_source=False))
    inv = json.loads((tmp_path / "artifacts/inventory.json").read_text())
    assert inv["image"] == RELEASED
    assert inv["mode"] == "released"
    assert inv["project"] == "my-proj"
    assert bootstrap.RECEIVER_SVC in inv["cloud_run_services"]
    assert bootstrap.DLQ_SUBSCRIPTION in inv["pubsub_subscriptions"]
    assert inv["dts_display_name"].endswith(inv["dataset"])
    # Default released-image mode creates no customer AR repo: teardown
    # must neither expect nor delete one.
    assert "ar_repo" not in inv

  def test_source_mode_inventory_includes_the_ar_repo(self, tmp_path):
    _run(_settings(tmp_path))
    inv = json.loads((tmp_path / "artifacts/inventory.json").read_text())
    assert inv["mode"] == "source"
    assert inv["ar_repo"] == bootstrap.AR_REPO

  def test_explicit_override_mode_is_recorded(self, tmp_path, monkeypatch):
    monkeypatch.setattr(_release, "RELEASE_IMAGE", None)
    _run(_settings(tmp_path, build_from_source=False, image=STAGING))
    inv = json.loads((tmp_path / "artifacts/inventory.json").read_text())
    assert inv["mode"] == "explicit"
    assert inv["image"] == STAGING
