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

"""``bqaa-otel bootstrap --preflight`` — mode-aware readiness checks.

Productized from the hero-demo ``preflight.sh`` (which caught real gaps
three times); issue #349 contract:

- Default prebuilt-image mode checks NO build permissions (the customer
  project never builds or hosts the image) and requires NO product CLIs
  (a platform admin deploying infra does not run Claude/Codex locally;
  ``check_products=True`` opts those probes back in).
- Every failure carries the remediation, not just the error.
- The permission probe uses the Resource Manager ``testIamPermissions``
  REST call: gcloud has no such subcommand (learned live), and the call
  returns exactly the subset of tested permissions the caller holds.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from typing import Callable
import urllib.request

# What bootstrap ACTUALLY exercises — creates plus the setIamPolicy/
# actAs/DTS surface where real deploys have failed mid-run.
_BASE_PERMISSIONS = (
    "serviceusage.services.enable",
    "run.services.create",
    "run.services.setIamPolicy",
    "pubsub.topics.create",
    "pubsub.topics.setIamPolicy",
    "pubsub.subscriptions.create",
    "pubsub.subscriptions.update",
    "pubsub.subscriptions.setIamPolicy",
    "bigquery.datasets.create",
    "bigquery.jobs.create",
    "bigquery.transfers.update",
    "secretmanager.secrets.create",
    "secretmanager.versions.add",
    "secretmanager.secrets.setIamPolicy",
    "iam.serviceAccounts.create",
    "iam.serviceAccounts.setIamPolicy",
    "iam.serviceAccounts.actAs",
    "resourcemanager.projects.setIamPolicy",
)
_SOURCE_BUILD_PERMISSIONS = (
    "cloudbuild.builds.create",
    "artifactregistry.repositories.create",
)

_ORG_POLICY_CONSTRAINT = "constraints/iam.allowedPolicyMemberDomains"


@dataclasses.dataclass(frozen=True)
class Check:
  name: str
  status: str  # OK | WARN | FAIL
  message: str

  @property
  def ok(self) -> bool:
    return self.status != "FAIL"


@dataclasses.dataclass(frozen=True)
class PreflightReport:
  checks: tuple[Check, ...]

  @property
  def ok(self) -> bool:
    return all(c.ok for c in self.checks)


def _default_http_post(url: str, body: dict, token: str) -> dict:
  request = urllib.request.Request(
      url,
      data=json.dumps(body).encode(),
      headers={
          "Authorization": f"Bearer {token}",
          "Content-Type": "application/json",
      },
      method="POST",
  )
  with urllib.request.urlopen(request, timeout=30) as response:
    return json.loads(response.read().decode())


def required_permissions(image_mode: str) -> tuple[str, ...]:
  """The permission set for this deploy mode ('source' adds build perms)."""
  if image_mode == "source":
    return _BASE_PERMISSIONS + _SOURCE_BUILD_PERMISSIONS
  return _BASE_PERMISSIONS


def run_preflight(
    *,
    project: str,
    dataset: str,
    image_mode: str,
    runner,
    http_post: Callable[[str, dict, str], dict] | None = None,
    check_products: bool = False,
    echo: Callable[..., None] = print,
) -> PreflightReport:
  """Run every check; mutate nothing; return the aggregated report."""
  http_post = http_post or _default_http_post
  checks: list[Check] = []

  def add(name: str, status: str, message: str) -> None:
    checks.append(Check(name, status, message))
    echo(f"{status:<5} {message}")

  # --- local CLIs the deploy itself shells out to --------------------------
  for cli, probe in (
      ("gcloud", ["gcloud", "version"]),
      ("bq", ["bq", "version"]),
  ):
    if runner.try_run(probe) is None:
      add(cli, "FAIL", f"{cli} missing — install the Google Cloud SDK")
    else:
      add(cli, "OK", f"{cli} present")

  # Product CLIs are OPT-IN: infra deployment does not need them.
  if check_products:
    for cli in ("claude", "codex"):
      if runner.try_run([cli, "--version"]) is None:
        add(
            cli,
            "FAIL",
            f"{cli} CLI missing — required only because --check-products"
            " was requested (validation sessions)",
        )
      else:
        add(cli, "OK", f"{cli} CLI present")

  # --- auth / project / billing --------------------------------------------
  account = runner.try_run(
      [
          "gcloud",
          "auth",
          "list",
          "--filter=status:ACTIVE",
          "--format=value(account)",
      ]
  )
  if not account:
    add("auth", "FAIL", "no active gcloud account — run: gcloud auth login")
  else:
    add("auth", "OK", f"gcloud authenticated ({account.splitlines()[0]})")

  if (
      runner.try_run(
          [
              "gcloud",
              "projects",
              "describe",
              project,
              "--format=value(projectId)",
          ]
      )
      is None
  ):
    add(
        "project",
        "FAIL",
        f"project {project!r} not accessible — check the id and your"
        " permissions",
    )
  else:
    add("project", "OK", f"project {project} accessible")

  billing = runner.try_run(
      [
          "gcloud",
          "billing",
          "projects",
          "describe",
          project,
          "--format=value(billingEnabled)",
      ]
  )
  if billing is not None and "True" in billing:
    add("billing", "OK", "billing enabled")
  else:
    add(
        "billing",
        "FAIL",
        "billing not enabled (or not visible) — link a billing account;"
        " Cloud Run/DTS refuse without it",
    )

  # --- permission probe (mode-aware, testIamPermissions REST) --------------
  required = required_permissions(image_mode)
  token = runner.try_run(["gcloud", "auth", "print-access-token"])
  granted: set[str] = set()
  probe_error = None
  if token:
    try:
      response = http_post(
          "https://cloudresourcemanager.googleapis.com/v1/projects/"
          f"{project}:testIamPermissions",
          {"permissions": list(required)},
          token.strip(),
      )
      granted = set(response.get("permissions", ()))
    except Exception as exc:  # noqa: BLE001 - any probe failure is a FAIL
      probe_error = str(exc)
  else:
    probe_error = "could not mint an access token"
  missing = [p for p in required if p not in granted]
  if probe_error:
    add("permissions", "FAIL", f"permission probe failed: {probe_error}")
  elif missing:
    add(
        "permissions",
        "FAIL",
        f"missing deploy permissions ({len(granted)}/{len(required)}):"
        f" {', '.join(missing)} — grant roles covering these before"
        " deploying",
    )
  else:
    add(
        "permissions",
        "OK",
        f"deploy permissions present ({len(required)}/{len(required)},"
        f" {image_mode} mode)",
    )

  # --- org policy: the receiver needs an allUsers invoker grant ------------
  policy_json = runner.try_run(
      [
          "gcloud",
          "resource-manager",
          "org-policies",
          "describe",
          _ORG_POLICY_CONSTRAINT,
          "--project",
          project,
          "--effective",
          "--format=json",
      ]
  )
  if policy_json is None:
    add(
        "org-policy",
        "WARN",
        "org policy iam.allowedPolicyMemberDomains not readable — cannot"
        " verify allUsers is permitted; if domain-restricted sharing is"
        " enforced, the Cloud Run deploy fails at the invoker grant",
    )
  else:
    try:
      list_policy = json.loads(policy_json).get("listPolicy", {})
    except ValueError:
      list_policy = {}
    if (
        list_policy.get("allowedValues")
        or list_policy.get("allValues") == "DENY"
    ):
      add(
          "org-policy",
          "FAIL",
          "domain-restricted sharing is enforced"
          " (iam.allowedPolicyMemberDomains) — the receiver needs an"
          " allUsers invoker grant; get a policy exception or use a"
          " project where it is allowed",
      )
    else:
      add("org-policy", "OK", "org policy permits allUsers invoker grants")

  # --- dataset state (informational) ----------------------------------------
  if (
      runner.try_run(
          [
              "bq",
              f"--project_id={project}",
              "show",
              "--dataset",
              f"{project}:{dataset}",
          ]
      )
      is None
  ):
    add(
        "dataset",
        "OK",
        f"dataset {dataset} does not exist yet (will be created)",
    )
  else:
    add("dataset", "OK", f"dataset {dataset} exists (bootstrap converges)")

  report = PreflightReport(tuple(checks))
  fails = sum(1 for c in checks if c.status == "FAIL")
  warns = sum(1 for c in checks if c.status == "WARN")
  echo("")
  echo(
      f"{len(checks) - fails - warns} ok, {warns} warnings, {fails} failed"
      + ("" if report.ok else " — do not deploy yet")
  )
  return report
