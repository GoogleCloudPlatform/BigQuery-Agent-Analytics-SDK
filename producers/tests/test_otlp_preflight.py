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

"""bqaa-otel bootstrap --preflight (issue #349 PR 3).

Mode-aware readiness checks that fail fast with actionable messages,
BEFORE anything mutates. Default prebuilt-image mode checks no build
permissions and requires no product CLIs (a platform admin deploying
infra does not run Claude/Codex on their machine).
"""

import subprocess

from bigquery_agent_analytics_tracing.otlp import preflight


class ScriptedRunner:
  """Answers commands from a substring->response map; '!' raises."""

  def __init__(self, responses=()):
    self.responses = dict(responses)
    self.calls = []

  def _answer(self, argv):
    joined = " ".join(argv)
    self.calls.append(joined)
    for needle, response in self.responses.items():
      if needle in joined:
        if response == "!":
          raise subprocess.CalledProcessError(1, list(argv), stderr="boom")
        return response
    return "ok"

  def run(self, argv, input_text=None):
    return self._answer(argv)

  def try_run(self, argv, input_text=None):
    try:
      return self._answer(argv)
    except subprocess.CalledProcessError:
      return None


def _grants_all(permissions):
  return {"permissions": list(permissions)}


def _run(mode="released", runner=None, http_post=None, check_products=False):
  return preflight.run_preflight(
      project="my-proj",
      dataset="ds1",
      image_mode=mode,
      runner=runner or ScriptedRunner({"billingEnabled": "True"}),
      http_post=http_post
      or (lambda url, body, token: _grants_all(body["permissions"])),
      check_products=check_products,
      echo=lambda *_: None,
  )


class TestModeAwarePermissions:

  def test_default_mode_never_asks_for_build_permissions(self):
    asked = {}

    def capture(url, body, token):
      asked["permissions"] = body["permissions"]
      return _grants_all(body["permissions"])

    _run(mode="released", http_post=capture)
    assert "cloudbuild.builds.create" not in asked["permissions"]
    assert "artifactregistry.repositories.create" not in asked["permissions"]
    assert "run.services.create" in asked["permissions"]
    assert "bigquery.transfers.update" in asked["permissions"]

  def test_source_mode_adds_build_permissions(self):
    asked = {}

    def capture(url, body, token):
      asked["permissions"] = body["permissions"]
      return _grants_all(body["permissions"])

    _run(mode="source", http_post=capture)
    assert "cloudbuild.builds.create" in asked["permissions"]
    assert "artifactregistry.repositories.create" in asked["permissions"]

  def test_missing_permissions_are_named_and_fail(self):
    def deny_transfers(url, body, token):
      return _grants_all(
          p for p in body["permissions"] if p != "bigquery.transfers.update"
      )

    report = _run(http_post=deny_transfers)
    assert not report.ok
    failed = "\n".join(c.message for c in report.checks if not c.ok)
    assert "bigquery.transfers.update" in failed


class TestNoProductClis:

  def test_default_checks_do_not_touch_product_clis(self):
    r = ScriptedRunner({"billingEnabled": "True"})
    _run(runner=r)
    joined = "\n".join(r.calls)
    assert "claude" not in joined
    assert "codex" not in joined

  def test_check_products_opt_in_probes_both(self):
    r = ScriptedRunner({"billingEnabled": "True"})
    _run(runner=r, check_products=True)
    joined = "\n".join(r.calls)
    assert "claude --version" in joined
    assert "codex --version" in joined


class TestOrgPolicy:

  def test_unreadable_org_policy_is_warn_not_fail(self):
    r = ScriptedRunner({"billingEnabled": "True", "org-policies describe": "!"})
    report = _run(runner=r)
    warned = [c for c in report.checks if c.status == "WARN"]
    assert any("allUsers" in c.message for c in warned)
    assert report.ok  # WARN alone must not fail preflight

  def test_domain_restriction_enforced_fails(self):
    r = ScriptedRunner(
        {
            "billingEnabled": "True",
            "org-policies describe": (
                '{"listPolicy": {"allowedValues": ["C0abc"]}}'
            ),
        }
    )
    report = _run(runner=r)
    assert not report.ok


class TestHardFailures:

  def test_billing_disabled_fails_with_actionable_message(self):
    r = ScriptedRunner({"billingEnabled": "False"})
    report = _run(runner=r)
    assert not report.ok
    failed = [c for c in report.checks if c.status == "FAIL"]
    assert any("billing" in c.message.lower() for c in failed)

  def test_inaccessible_project_fails(self):
    r = ScriptedRunner({"billingEnabled": "True", "projects describe": "!"})
    report = _run(runner=r)
    assert not report.ok
