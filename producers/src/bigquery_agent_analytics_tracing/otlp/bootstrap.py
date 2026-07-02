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

"""``bqaa-otel bootstrap`` — the #324 admin deploy orchestration.

Absorbs ``deploy/otlp_receiver/setup.sh`` (which now delegates here so the
deploy sequence has one source of truth): BigQuery schema + views generated
straight from the ``ddl``/``sql`` modules, Pub/Sub topics + OIDC push
subscription with DLQ, Secret Manager bearer token, least-privilege service
accounts, the Cloud Run receiver + consumer, the scheduled ``MERGE``, and —
new versus the shell script — the PR 1 config artifacts rendered against the
real deployed endpoint.

Commands run through an injectable runner so the sequence is unit-testable
and so plan mode (the CLI default) renders every command without executing
anything. The bearer token is never embedded in generated artifacts; the
summary prints the ``gcloud secrets versions access`` command instead.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import secrets as _secrets
import subprocess
from typing import Any, Callable, Protocol

from . import config_artifacts
from . import ddl
from . import sql

AR_REPO = "bqaa"
MAIN_TOPIC = "bqaa-otlp"
DLQ_TOPIC = "bqaa-otlp-dlq"
SUBSCRIPTION = "bqaa-otlp-sub"
SECRET = "bqaa-otlp-token"
RECEIVER_SVC = "bqaa-otlp-receiver"
CONSUMER_SVC = "bqaa-otlp-consumer"
MERGE_DISPLAY_NAME = "bqaa_agent_events_otlp_merge"

_APIS = (
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "bigquery.googleapis.com",
    "bigquerydatatransfer.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
)

_CONSUMER_GUNICORN_ARGS = (
    "--factory,--bind,0.0.0.0:8080,--workers,2,--threads,8,"
    "bigquery_agent_analytics_tracing.otlp.consumer:make_push_app_from_env"
)


class Runner(Protocol):
  """Executes one external command; see :class:`SubprocessRunner`."""

  def run(self, argv: list[str], input_text: str | None = None) -> str:
    """Run to completion; return stdout. Raises on failure."""

  def try_run(
      self, argv: list[str], input_text: str | None = None
  ) -> str | None:
    """Run; return stdout, or ``None`` on failure (probe / idempotent)."""


class SubprocessRunner:
  """Real runner: gcloud/bq via subprocess, echoing each command."""

  def __init__(self, echo: Callable[..., None] = print):
    self._echo = echo

  def run(self, argv: list[str], input_text: str | None = None) -> str:
    self._echo(f"$ {' '.join(argv)}")
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

  def try_run(
      self, argv: list[str], input_text: str | None = None
  ) -> str | None:
    try:
      return self.run(argv, input_text)
    except subprocess.CalledProcessError:
      return None


class _PlanRunner:
  """Records the would-run commands; existence probes take the create path."""

  _PLACEHOLDERS = (
      ("projects describe", "<project-number>"),
      (RECEIVER_SVC, "<receiver-url>"),
      (CONSUMER_SVC, "<consumer-url>"),
      ("secrets versions access", "<token>"),
  )

  def __init__(self):
    self.commands: list[tuple[tuple[str, ...], str | None]] = []

  def _canned(self, argv: list[str]) -> str:
    joined = " ".join(argv)
    for needle, placeholder in self._PLACEHOLDERS:
      if needle in joined:
        return placeholder
    return ""

  def run(self, argv: list[str], input_text: str | None = None) -> str:
    self.commands.append((tuple(argv), input_text))
    return self._canned(argv)

  def try_run(
      self, argv: list[str], input_text: str | None = None
  ) -> str | None:
    joined = " ".join(argv)
    if "describe" in joined or "--transfer_config" in joined:
      return None  # probe: assume the resource is missing → show creates
    self.commands.append((tuple(argv), input_text))
    return self._canned(argv)


@dataclasses.dataclass(frozen=True)
class BootstrapSettings:
  """Validated admin inputs for one bootstrap run."""

  project: str
  dataset: str = "agent_analytics"
  region: str = "us-central1"
  bq_location: str = "US"
  signals: tuple[str, ...] = ("logs", "metrics")
  privacy: str = "baseline"
  sources: tuple[str, ...] = ("claude-code",)
  source_product: str = "claude_code"
  resource_attributes: dict[str, str] | None = None
  acknowledge_content_logging: bool = False
  out_dir: pathlib.Path = pathlib.Path(".")

  def __post_init__(self):
    # Reuse the PR 1 tier validation (privacy/signals/replay ack) so the
    # gate can't drift between `config` and `bootstrap`.
    self._spec("<pending-deploy>")
    for source in self.sources:
      if source not in config_artifacts.SOURCES:
        raise ValueError(
            f"unknown source {source!r}; expected one of"
            f" {config_artifacts.SOURCES}"
        )

  def _spec(self, endpoint: str) -> config_artifacts.BootstrapSpec:
    return config_artifacts.BootstrapSpec(
        endpoint=endpoint,
        signals=self.signals,
        privacy=self.privacy,
        resource_attributes=self.resource_attributes,
        acknowledge_content_logging=self.acknowledge_content_logging,
    )

  @property
  def enable_spans(self) -> bool:
    return "traces" in self.signals


@dataclasses.dataclass(frozen=True)
class BootstrapResult:
  receiver_url: str
  consumer_url: str
  artifact_paths: tuple[pathlib.Path, ...]


def _image(s: BootstrapSettings) -> str:
  return f"{s.region}-docker.pkg.dev/{s.project}/{AR_REPO}/otlp-receiver:latest"


def _find_merge_config(listing: str | None, dataset: str) -> str | None:
  """Resource name of this dataset's scheduled-MERGE config, if one exists.

  Matches either the dataset-specific display name or a legacy
  ``bqaa_agent_events_otlp_merge`` config whose query targets this dataset
  (pre-#331 deployments used the unsuffixed name for every dataset).
  """
  if not listing:
    return None
  try:
    configs = json.loads(listing)
  except ValueError:
    return None
  for config in configs:
    display = config.get("displayName", "")
    query = (config.get("params") or {}).get("query", "")
    if display == f"{MERGE_DISPLAY_NAME}_{dataset}" or (
        display == MERGE_DISPLAY_NAME
        and f"`{dataset}.agent_events_otlp`" in query
    ):
      return config.get("name")
  return None


def run_bootstrap(
    settings: BootstrapSettings,
    runner: Runner,
    *,
    echo: Callable[..., None] = print,
    write_file: Callable[[pathlib.Path, str], None] | None = None,
) -> BootstrapResult:
  """Execute the full deploy sequence (setup.sh parity + config artifacts)."""
  s = settings
  proj = ["--project", s.project]
  spans = "1" if s.enable_spans else "0"

  if write_file is None:

    def write_file(path: pathlib.Path, content: str) -> None:
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(content)

  receiver_sa = f"{RECEIVER_SVC}@{s.project}.iam.gserviceaccount.com"
  consumer_sa = f"{CONSUMER_SVC}@{s.project}.iam.gserviceaccount.com"
  push_sa = f"bqaa-otlp-push@{s.project}.iam.gserviceaccount.com"

  echo("==> Enabling APIs")
  runner.run(["gcloud", "services", "enable", *proj, *_APIS])

  echo(f"==> Ensuring Artifact Registry repo {AR_REPO!r} exists")
  if (
      runner.try_run(
          [
              "gcloud",
              "artifacts",
              "repositories",
              "describe",
              AR_REPO,
              *proj,
              "--location",
              s.region,
          ]
      )
      is None
  ):
    runner.run(
        [
            "gcloud",
            "artifacts",
            "repositories",
            "create",
            AR_REPO,
            *proj,
            "--location",
            s.region,
            "--repository-format=docker",
        ]
    )

  echo(f"==> Creating BigQuery dataset ({s.bq_location}) + native schema")
  bq = ["bq", f"--project_id={s.project}", f"--location={s.bq_location}"]
  runner.run([*bq, "mk", "-f", "--dataset", f"{s.project}:{s.dataset}"])
  runner.run(
      [*bq, "query", "--use_legacy_sql=false"],
      input_text=ddl.create_all_sql(s.dataset, enable_spans=s.enable_spans),
  )

  echo("==> Creating the bearer token secret")
  if runner.try_run(["gcloud", "secrets", "describe", SECRET, *proj]) is None:
    runner.run(
        [
            "gcloud",
            "secrets",
            "create",
            SECRET,
            *proj,
            "--replication-policy=automatic",
            "--data-file=-",
        ],
        input_text=_secrets.token_hex(32),
    )

  echo("==> Creating service accounts")
  for sa_id in (RECEIVER_SVC, CONSUMER_SVC, "bqaa-otlp-push"):
    if (
        runner.try_run(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "describe",
                f"{sa_id}@{s.project}.iam.gserviceaccount.com",
                *proj,
            ]
        )
        is None
    ):
      runner.run(["gcloud", "iam", "service-accounts", "create", sa_id, *proj])

  echo("==> Creating Pub/Sub topics")
  for topic in (MAIN_TOPIC, DLQ_TOPIC):
    runner.try_run(["gcloud", "pubsub", "topics", "create", topic, *proj])

  echo("==> Granting least-privilege IAM")
  project_number = runner.run(
      [
          "gcloud",
          "projects",
          "describe",
          s.project,
          "--format=value(projectNumber)",
      ]
  )
  pubsub_agent = (
      f"service-{project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
  )
  runner.run(
      [
          "gcloud",
          "secrets",
          "add-iam-policy-binding",
          SECRET,
          *proj,
          "--member",
          f"serviceAccount:{receiver_sa}",
          "--role",
          "roles/secretmanager.secretAccessor",
      ]
  )
  runner.run(
      [
          "gcloud",
          "pubsub",
          "topics",
          "add-iam-policy-binding",
          MAIN_TOPIC,
          *proj,
          "--member",
          f"serviceAccount:{receiver_sa}",
          "--role",
          "roles/pubsub.publisher",
      ]
  )
  for role in ("roles/bigquery.dataEditor", "roles/bigquery.jobUser"):
    runner.run(
        [
            "gcloud",
            "projects",
            "add-iam-policy-binding",
            s.project,
            "--member",
            f"serviceAccount:{consumer_sa}",
            "--role",
            role,
        ]
    )
  runner.run(
      [
          "gcloud",
          "pubsub",
          "topics",
          "add-iam-policy-binding",
          DLQ_TOPIC,
          *proj,
          "--member",
          f"serviceAccount:{pubsub_agent}",
          "--role",
          "roles/pubsub.publisher",
      ]
  )
  runner.run(
      [
          "gcloud",
          "iam",
          "service-accounts",
          "add-iam-policy-binding",
          push_sa,
          *proj,
          "--member",
          f"serviceAccount:{pubsub_agent}",
          "--role",
          "roles/iam.serviceAccountTokenCreator",
      ]
  )

  echo("==> Building image")
  image = _image(s)
  build_config = (
      "steps:\n"
      "- name: gcr.io/cloud-builders/docker\n"
      f"  args: ['build','-f','deploy/otlp_receiver/Dockerfile',"
      f"'-t','{image}','.']\n"
      f"images: ['{image}']\n"
  )
  runner.run(
      ["gcloud", "builds", "submit", *proj, "--config=/dev/stdin", "."],
      input_text=build_config,
  )

  main_topic_path = f"projects/{s.project}/topics/{MAIN_TOPIC}"

  echo("==> Deploying the OTLP receiver (Cloud Run)")
  runner.run(
      [
          "gcloud",
          "run",
          "deploy",
          RECEIVER_SVC,
          *proj,
          "--region",
          s.region,
          "--image",
          image,
          "--allow-unauthenticated",
          "--service-account",
          receiver_sa,
          "--set-secrets",
          f"BQAA_OTLP_TOKEN={SECRET}:latest",
          "--set-env-vars",
          f"BQAA_OTLP_MAIN_TOPIC={main_topic_path},"
          f"BQAA_OTLP_SOURCE_PRODUCT={s.source_product},"
          f"BQAA_OTLP_ENABLE_TRACES={spans}",
      ]
  )

  echo("==> Deploying the Pub/Sub push consumer (Cloud Run HTTP service)")
  runner.run(
      [
          "gcloud",
          "run",
          "deploy",
          CONSUMER_SVC,
          *proj,
          "--region",
          s.region,
          "--image",
          image,
          "--no-allow-unauthenticated",
          "--service-account",
          consumer_sa,
          "--command",
          "gunicorn",
          "--args",
          _CONSUMER_GUNICORN_ARGS,
          "--set-env-vars",
          f"BQAA_PROJECT={s.project},BQAA_DATASET={s.dataset},"
          f"BQAA_OTLP_ENABLE_TRACES={spans}",
      ]
  )

  consumer_url = runner.run(
      [
          "gcloud",
          "run",
          "services",
          "describe",
          CONSUMER_SVC,
          *proj,
          "--region",
          s.region,
          "--format=value(status.url)",
      ]
  )
  runner.run(
      [
          "gcloud",
          "run",
          "services",
          "add-iam-policy-binding",
          CONSUMER_SVC,
          *proj,
          "--region",
          s.region,
          "--member",
          f"serviceAccount:{push_sa}",
          "--role",
          "roles/run.invoker",
      ]
  )

  echo("==> Creating the push subscription (OIDC) with DLQ")
  if (
      runner.try_run(
          [
              "gcloud",
              "pubsub",
              "subscriptions",
              "create",
              SUBSCRIPTION,
              *proj,
              "--topic",
              MAIN_TOPIC,
              "--push-endpoint",
              f"{consumer_url}/",
              "--push-auth-service-account",
              push_sa,
              "--dead-letter-topic",
              DLQ_TOPIC,
              "--max-delivery-attempts",
              "5",
              "--ack-deadline",
              "60",
          ]
      )
      is None
  ):
    # Repair path: converge every setting the create path applies, so a
    # drifted or pre-DLQ subscription is brought back to spec.
    runner.run(
        [
            "gcloud",
            "pubsub",
            "subscriptions",
            "update",
            SUBSCRIPTION,
            *proj,
            "--push-endpoint",
            f"{consumer_url}/",
            "--push-auth-service-account",
            push_sa,
            "--dead-letter-topic",
            DLQ_TOPIC,
            "--max-delivery-attempts",
            "5",
            "--ack-deadline",
            "60",
        ]
    )
  runner.run(
      [
          "gcloud",
          "pubsub",
          "subscriptions",
          "add-iam-policy-binding",
          SUBSCRIPTION,
          *proj,
          "--member",
          f"serviceAccount:{pubsub_agent}",
          "--role",
          "roles/pubsub.subscriber",
      ]
  )

  echo("==> Registering the scheduled MERGE into agent_events_otlp")
  merge_sql = sql.agent_events_otlp_merge_sql(dataset=s.dataset)
  params = json.dumps({"query": merge_sql})
  existing_name = _find_merge_config(
      runner.try_run([*bq, "ls", "--transfer_config", "--format=json"]),
      s.dataset,
  )
  if existing_name:
    # Converge: refresh the SQL so re-runs after crosswalk/MERGE changes
    # never leave a stale scheduled query behind.
    runner.run(
        [
            *bq,
            "update",
            "--transfer_config",
            f"--params={params}",
            existing_name,
        ]
    )
    echo("  scheduled query already exists — SQL refreshed")
  else:
    runner.run(
        [
            *bq,
            "mk",
            "--transfer_config",
            "--data_source=scheduled_query",
            # Dataset-specific so several datasets in one project/location
            # each keep their own projection job.
            f"--display_name={MERGE_DISPLAY_NAME}_{s.dataset}",
            "--schedule=every 15 minutes",
            f"--params={params}",
        ]
    )

  receiver_url = runner.run(
      [
          "gcloud",
          "run",
          "services",
          "describe",
          RECEIVER_SVC,
          *proj,
          "--region",
          s.region,
          "--format=value(status.url)",
      ]
  )

  echo("==> Generating telemetry-source config artifacts")
  artifacts = config_artifacts.render_artifacts(
      s._spec(receiver_url), sources=s.sources
  )
  paths = []
  for filename, content in artifacts.items():
    path = s.out_dir / filename
    write_file(path, content)
    paths.append(path)
    echo(f"  wrote {path}")

  echo("")
  echo(f"==> Done. Receiver: {receiver_url}")
  echo(f"    Endpoints: {receiver_url}/v1/logs , {receiver_url}/v1/metrics")
  echo(
      "    Bearer token (fill it into the artifacts; never committed):"
      f" gcloud secrets versions access latest --secret={SECRET}"
      f" --project {s.project}"
  )
  echo("")
  echo("Next: distribute the generated config artifacts, then smoke-test:")
  echo(
      f"    BQAA_OTLP_ENDPOINT={receiver_url} BQAA_OTLP_TOKEN=<token>"
      f" BQAA_PROJECT={s.project} BQAA_DATASET={s.dataset}"
      " python -m pytest producers/tests/test_otlp_e2e.py -v"
  )
  return BootstrapResult(
      receiver_url=receiver_url,
      consumer_url=consumer_url,
      artifact_paths=tuple(paths),
  )


def render_plan(settings: BootstrapSettings) -> str:
  """The commands ``--execute`` would run, without running anything."""
  planner = _PlanRunner()
  run_bootstrap(
      settings,
      planner,
      echo=lambda *_: None,
      write_file=lambda *_: None,
  )
  lines = [
      "bqaa-otel bootstrap plan (nothing has been executed).",
      "Values captured at run time are shown as <placeholders>.",
      "",
  ]
  for argv, input_text in planner.commands:
    lines.append("  $ " + " ".join(_display_arg(a) for a in argv))
    if input_text is not None:
      summary = f"{len(input_text.splitlines())} lines"
      lines.append(f"      [stdin: {summary}]")
  lines += [
      "",
      "Re-run with --execute to apply.",
  ]
  return "\n".join(lines)


def _display_arg(arg: str) -> str:
  if len(arg) > 100:
    return arg[:97] + "..."
  return arg
