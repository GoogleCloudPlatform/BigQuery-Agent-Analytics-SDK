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

"""``bqaa-otel teardown`` — inventory-driven, dry-run-default cleanup.

Issue #349 contract, TDD-ported from the proven hero-demo teardown:

- Consumes the ``inventory.json`` that ``bootstrap --execute`` writes;
  without one, reconstructs it from the deterministic package constants
  (the DTS config name is queried — its id is opaque).
- The human-provided ``--project``/``--dataset`` must MATCH the
  inventory; every resource name must match the ``bqaa`` allowlist
  patterns. A poisoned inventory is refused, never obeyed.
- Dry-run by default: prints the exact command for every deletion.
- Verification is the authority on success: existence probes that PASS
  only on a known not-found response — an auth/API error is
  UNVERIFIABLE and fails the run (a probe error must never masquerade
  as "gone").
- Mode-aware via the inventory: default released-image mode records no
  customer AR repo, so none is expected or deleted.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import subprocess
from typing import Callable

from . import bootstrap

_NOT_FOUND_RE = re.compile(
    r"not.?found|does not exist|was not found|no such", re.IGNORECASE
)

# Pattern contract for every deletable resource name.
_ALLOWLIST = {
    "cloud_run_services": r"bqaa-otlp-.*",
    "pubsub_topics": r"bqaa-otlp.*",
    "pubsub_subscriptions": r"bqaa-otlp.*",
    "secret": r"bqaa-otlp-.*",
    "service_accounts": r"bqaa-otlp-.*",
    "ar_repo": r"bqaa",
    "dts_display_name": rf"{bootstrap.MERGE_DISPLAY_NAME}(_\w+)?",
}


@dataclasses.dataclass(frozen=True)
class TeardownSettings:
  project: str
  dataset: str
  region: str = "us-central1"
  bq_location: str = "US"
  inventory: pathlib.Path | None = None
  confirm: bool = False
  dataset_only: bool = False

  def __post_init__(self):
    if not re.fullmatch(r"[a-z0-9.:-]+", self.project, re.IGNORECASE):
      raise ValueError(f"invalid GCP project id {self.project!r}")
    if not re.fullmatch(r"\w+", self.dataset, re.ASCII):
      raise ValueError(f"invalid BigQuery dataset id {self.dataset!r}")


@dataclasses.dataclass(frozen=True)
class VerifyCheck:
  name: str
  ok: bool
  message: str


@dataclasses.dataclass(frozen=True)
class TeardownReport:
  dry_run: bool
  checks: tuple[VerifyCheck, ...]

  @property
  def ok(self) -> bool:
    return all(c.ok for c in self.checks)


def _reconstructed_inventory(s: TeardownSettings) -> dict:
  """Inventory from the deterministic package constants (no file needed)."""
  return {
      "mode": "reconstructed",
      "project": s.project,
      "dataset": s.dataset,
      "region": s.region,
      "bq_location": s.bq_location,
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
      "dts_display_name": f"{bootstrap.MERGE_DISPLAY_NAME}_{s.dataset}",
      # No ar_repo: reconstruction must not assume a source-build deploy;
      # released-image mode creates none.
  }


def _load_inventory(s: TeardownSettings) -> dict:
  if s.inventory is None:
    return _reconstructed_inventory(s)
  inventory = json.loads(s.inventory.read_text())
  for key in ("project", "dataset"):
    if inventory.get(key) != getattr(s, key):
      raise ValueError(
          f"inventory {key} {inventory.get(key)!r} does not match the"
          f" requested {key} {getattr(s, key)!r} — refusing (wrong"
          " inventory file?)"
      )
  return inventory


def _guard_allowlist(inventory: dict) -> None:
  for key, pattern in _ALLOWLIST.items():
    if key not in inventory:
      continue
    values = inventory[key]
    if isinstance(values, str):
      values = [values]
    for value in values:
      if not re.fullmatch(pattern, value):
        raise ValueError(
            f"{key} value {value!r} does not match allowlist pattern"
            f" {pattern!r} — refusing the inventory"
        )


def run_teardown(
    settings: TeardownSettings,
    runner,
    *,
    echo: Callable[..., None] = print,
) -> TeardownReport:
  s = settings
  inventory = _load_inventory(s)
  _guard_allowlist(inventory)
  proj = ["--project", s.project]
  bq = ["bq", f"--project_id={s.project}"]

  deletions: list[tuple[str, list[str]]] = []

  # --- dataset-scoped tier ---------------------------------------------------
  # Dry run executes NOTHING (not even read-only listings — the plan must
  # work offline); the opaque DTS config name is queried at --confirm time.
  if s.confirm:
    listing = runner.try_run(
        [
            *bq,
            f"--location={s.bq_location}",
            "ls",
            "--transfer_config",
            f"--transfer_location={s.bq_location}",
            "--format=json",
        ]
    )
    if listing is None:
      # Hard-fail BEFORE anything destructive: if the DTS listing is
      # unreadable we cannot find the scheduled MERGE, and deleting the
      # rest would leave it running (and billing) against a dead dataset.
      raise RuntimeError(
          "cannot list DTS scheduled queries (bq ls --transfer_config"
          " failed) — refusing to start the destructive teardown; fix"
          " access to BigQuery Data Transfer and re-run"
      )
    dts_name = bootstrap._find_merge_config(listing, s.dataset)
    if dts_name:
      deletions.append(
          (
              f"DTS scheduled MERGE ({dts_name})",
              [
                  *bq,
                  f"--location={s.bq_location}",
                  "rm",
                  "-f",
                  "--transfer_config",
                  dts_name,
              ],
          )
      )
  else:
    deletions.append(
        (
            f"DTS scheduled MERGE for {s.dataset} (name queried at --confirm"
            " time; legacy unsuffixed configs matched by query text)",
            [
                *bq,
                f"--location={s.bq_location}",
                "rm",
                "-f",
                "--transfer_config",
                "<queried-config-name>",
            ],
        )
    )
  deletions.append(
      (
          f"BigQuery dataset {s.project}:{s.dataset} (contains telemetry)",
          [*bq, "rm", "-r", "-f", "--dataset", f"{s.project}:{s.dataset}"],
      )
  )

  # --- pipeline tier (skipped with --dataset-only) ---------------------------
  consumer_sa = f"{bootstrap.CONSUMER_SVC}@{s.project}.iam.gserviceaccount.com"
  if not s.dataset_only:
    for svc in inventory["cloud_run_services"]:
      deletions.append(
          (
              f"Cloud Run service {svc}",
              [
                  "gcloud",
                  "run",
                  "services",
                  "delete",
                  svc,
                  *proj,
                  "--region",
                  s.region,
              ],
          )
      )
    for sub in inventory["pubsub_subscriptions"]:
      deletions.append(
          (
              f"subscription {sub}",
              ["gcloud", "pubsub", "subscriptions", "delete", sub, *proj],
          )
      )
    for topic in inventory["pubsub_topics"]:
      deletions.append(
          (
              f"topic {topic}",
              ["gcloud", "pubsub", "topics", "delete", topic, *proj],
          )
      )
    deletions.append(
        (
            f"secret {inventory['secret']}",
            ["gcloud", "secrets", "delete", inventory["secret"], *proj],
        )
    )
    if "ar_repo" in inventory:
      # Present only for source-build deploys; released-image mode
      # creates no customer repo and must not touch one.
      deletions.append(
          (
              f"Artifact Registry repo {inventory['ar_repo']}",
              [
                  "gcloud",
                  "artifacts",
                  "repositories",
                  "delete",
                  inventory["ar_repo"],
                  *proj,
                  "--location",
                  s.region,
              ],
          )
      )
    deletions.append(
        (
            f"project jobUser binding for {consumer_sa}",
            [
                "gcloud",
                "projects",
                "remove-iam-policy-binding",
                s.project,
                "--member",
                f"serviceAccount:{consumer_sa}",
                "--role",
                "roles/bigquery.jobUser",
            ],
        )
    )
    for sa in inventory["service_accounts"]:
      email = f"{sa}@{s.project}.iam.gserviceaccount.com"
      deletions.append(
          (
              f"service account {email}",
              ["gcloud", "iam", "service-accounts", "delete", email, *proj],
          )
      )

  if not s.confirm:
    echo(
        f"Teardown plan (project={s.project} dataset={s.dataset},"
        f" mode={inventory.get('mode', 'unknown')})"
    )
    echo("DRY RUN — re-run with --confirm to execute.")
    for description, argv in deletions:
      echo(f"WOULD DELETE  {description}")
      echo(f"              $ {' '.join(argv)}")
    return TeardownReport(dry_run=True, checks=())

  for description, argv in deletions:
    echo(f"DELETE  {description}")
    try:
      runner.run(argv)
    except subprocess.CalledProcessError as exc:
      # Visible, never silent — verification below is the authority.
      stderr = (exc.stderr or "").strip().splitlines()
      echo(
          f"        delete failed (verification will decide):"
          f" {stderr[0] if stderr else 'unknown error'}"
      )

  # --- verification: existence probes for EVERY resource class --------------
  checks: list[VerifyCheck] = []

  def gone(name: str, argv: list[str]) -> None:
    try:
      runner.run(argv)
    except subprocess.CalledProcessError as exc:
      stderr = (exc.stderr or "").strip()
      if _NOT_FOUND_RE.search(stderr):
        checks.append(VerifyCheck(name, True, f"PASS  {name} is gone"))
      else:
        checks.append(
            VerifyCheck(
                name,
                False,
                f"FAIL  {name} UNVERIFIABLE: {stderr.splitlines()[0][:100] if stderr else 'probe failed'}",
            )
        )
      return
    checks.append(VerifyCheck(name, False, f"FAIL  {name} STILL EXISTS"))

  post_listing = runner.try_run(
      [
          *bq,
          f"--location={s.bq_location}",
          "ls",
          "--transfer_config",
          f"--transfer_location={s.bq_location}",
          "--format=json",
      ]
  )
  if post_listing is None:
    checks.append(
        VerifyCheck(
            "dts", False, "FAIL  DTS listing UNVERIFIABLE (listing failed)"
        )
    )
  elif bootstrap._find_merge_config(post_listing, s.dataset):
    checks.append(
        VerifyCheck(
            "dts",
            False,
            "FAIL  DTS scheduled MERGE STILL EXISTS (bills every 15 min)",
        )
    )
  else:
    checks.append(
        VerifyCheck(
            "dts",
            True,
            "PASS  no DTS scheduled MERGE remains (incl. legacy unsuffixed)",
        )
    )
  gone(
      f"dataset {s.dataset}",
      [*bq, "show", "--dataset", f"{s.project}:{s.dataset}"],
  )

  if not s.dataset_only:
    for svc in inventory["cloud_run_services"]:
      gone(
          f"Cloud Run service {svc}",
          [
              "gcloud",
              "run",
              "services",
              "describe",
              svc,
              *proj,
              "--region",
              s.region,
          ],
      )
    for sub in inventory["pubsub_subscriptions"]:
      gone(
          f"subscription {sub}",
          ["gcloud", "pubsub", "subscriptions", "describe", sub, *proj],
      )
    for topic in inventory["pubsub_topics"]:
      gone(
          f"topic {topic}",
          ["gcloud", "pubsub", "topics", "describe", topic, *proj],
      )
    gone(
        f"secret {inventory['secret']}",
        ["gcloud", "secrets", "describe", inventory["secret"], *proj],
    )
    if "ar_repo" in inventory:
      gone(
          f"Artifact Registry repo {inventory['ar_repo']}",
          [
              "gcloud",
              "artifacts",
              "repositories",
              "describe",
              inventory["ar_repo"],
              *proj,
              "--location",
              s.region,
          ],
      )
    for sa in inventory["service_accounts"]:
      email = f"{sa}@{s.project}.iam.gserviceaccount.com"
      gone(
          f"service account {email}",
          ["gcloud", "iam", "service-accounts", "describe", email, *proj],
      )
    binding = runner.try_run(
        [
            "gcloud",
            "projects",
            "get-iam-policy",
            s.project,
            "--flatten=bindings[].members",
            f"--filter=bindings.role:roles/bigquery.jobUser AND"
            f" bindings.members:serviceAccount:{consumer_sa}",
            "--format=value(bindings.role)",
        ]
    )
    if binding is None:
      checks.append(
          VerifyCheck(
              "iam", False, "FAIL  IAM policy UNVERIFIABLE (read failed)"
          )
      )
    elif binding.strip():
      checks.append(
          VerifyCheck(
              "iam",
              False,
              f"FAIL  project jobUser binding for {consumer_sa} STILL EXISTS",
          )
      )
    else:
      checks.append(
          VerifyCheck(
              "iam",
              True,
              f"PASS  project jobUser binding for {consumer_sa} is gone",
          )
      )

  for check in checks:
    echo(check.message)
  echo("")
  ok = all(c.ok for c in checks)
  echo(
      "Teardown verified clean."
      if ok
      else "Verification found remaining or unverifiable resources."
  )
  return TeardownReport(dry_run=False, checks=tuple(checks))
