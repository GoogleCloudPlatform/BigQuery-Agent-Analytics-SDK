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
import re
import secrets as _secrets
import shlex
import subprocess
from typing import Callable, Protocol

from . import config_artifacts
from . import ddl
from . import sql

AR_REPO = "bqaa"
MAIN_TOPIC = "bqaa-otlp"
DLQ_TOPIC = "bqaa-otlp-dlq"
SUBSCRIPTION = "bqaa-otlp-sub"
# Retention subscription on the DLQ topic: Pub/Sub only retains messages for
# subscriptions that exist at publish time, so without this every dead letter
# forwarded after max delivery attempts would be permanently discarded.
DLQ_SUBSCRIPTION = "bqaa-otlp-dlq-sub"
DLQ_RETENTION = "7d"
SECRET = "bqaa-otlp-token"
RECEIVER_SVC = "bqaa-otlp-receiver"
CONSUMER_SVC = "bqaa-otlp-consumer"
MERGE_DISPLAY_NAME = "bqaa_agent_events_otlp_merge"
MAX_DELIVERY_ATTEMPTS = "5"
ACK_DEADLINE_SECONDS = "60"

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

# gunicorn calls app factories via the 'module:callable()' syntax;
# '--factory' is a uvicorn flag and makes the container exit 2 on startup
# (hit live during the #324 e2e — first real Cloud Run deploy).
_CONSUMER_GUNICORN_ARGS = (
    "--bind,0.0.0.0:8080,--workers,2,--threads,8,"
    "bigquery_agent_analytics_tracing.otlp.consumer:make_push_app_from_env()"
)


class Runner(Protocol):
  """Executes one external command; see :class:`SubprocessRunner`."""

  def run(self, argv: list[str], input_text: str | None = None) -> str:
    """Run to completion; return stdout. Raises on failure."""

  def try_run(
      self, argv: list[str], input_text: str | None = None
  ) -> str | None:
    """Run; return stdout, or ``None`` on failure (probe / idempotent)."""


def _hardened_argv(argv: list[str]) -> list[str]:
  """Make gcloud/bq non-interactive.

  Output is captured, so an interactive prompt would be invisible while the
  child blocks on the tty — the deploy would hang forever mid-sequence.
  ``--quiet`` (gcloud) and ``--headless`` (bq, global flag: goes before the
  command) turn prompts into fast visible failures instead.
  """
  if argv[0] == "gcloud" and "--quiet" not in argv:
    return [*argv, "--quiet"]
  if argv[0] == "bq" and "--headless" not in argv:
    return [argv[0], "--headless", *argv[1:]]
  return argv


class SubprocessRunner:
  """Real runner: gcloud/bq via subprocess, echoing each command."""

  def __init__(self, echo: Callable[..., None] = print):
    self._echo = echo

  def run(self, argv: list[str], input_text: str | None = None) -> str:
    argv = _hardened_argv(argv)
    self._echo(f"$ {shlex.join(argv)}")
    # stdin is never the tty: a prompting child must see EOF, not hang.
    stdin_kwargs = (
        {"input": input_text}
        if input_text is not None
        else {"stdin": subprocess.DEVNULL}
    )
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=True,
        **stdin_kwargs,
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
    # Both feed backtick-quoted SQL identifiers (DDL, the DCL GRANT, the
    # scheduled MERGE) run under admin credentials: a backtick-bearing
    # value breaks out of the quoting and appends arbitrary SQL. Same
    # validation as VerifySettings.
    if not re.fullmatch(r"[a-z0-9.:-]+", self.project, re.IGNORECASE):
      raise ValueError(f"invalid GCP project id {self.project!r}")
    if not re.fullmatch(r"\w+", self.dataset, re.ASCII):
      raise ValueError(f"invalid BigQuery dataset id {self.dataset!r}")
    if not self.sources:
      raise ValueError(
          "at least one telemetry source is required; expected one of"
          f" {config_artifacts.SOURCES}"
      )
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

  echo("==> Ensuring the bearer token secret exists")
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

  echo("==> Ensuring service accounts exist")
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

  echo("==> Ensuring Pub/Sub topics + DLQ retention subscription exist")
  for topic in (MAIN_TOPIC, DLQ_TOPIC):
    runner.try_run(["gcloud", "pubsub", "topics", "create", topic, *proj])
  # Retain dead letters so they can be inspected and replayed; a topic with
  # no subscription silently discards everything published to it. Hard
  # `run` on the mutation: a swallowed failure here would report success
  # while dead letters keep evaporating.
  dlq_retention_flags = [
      "--message-retention-duration",
      DLQ_RETENTION,
      "--expiration-period",
      "never",
  ]
  if (
      runner.try_run(
          [
              "gcloud",
              "pubsub",
              "subscriptions",
              "describe",
              DLQ_SUBSCRIPTION,
              *proj,
          ]
      )
      is None
  ):
    runner.run(
        [
            "gcloud",
            "pubsub",
            "subscriptions",
            "create",
            DLQ_SUBSCRIPTION,
            *proj,
            "--topic",
            DLQ_TOPIC,
            *dlq_retention_flags,
        ]
    )
  else:
    # Converge retention/expiration on a pre-existing subscription.
    runner.run(
        [
            "gcloud",
            "pubsub",
            "subscriptions",
            "update",
            DLQ_SUBSCRIPTION,
            *proj,
            *dlq_retention_flags,
        ]
    )

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
  # jobUser needs project scope (query jobs), but write access is scoped to
  # the target dataset: a project-wide grant would let a compromised
  # consumer SA modify every dataset in the project.
  runner.run(
      [
          "gcloud",
          "projects",
          "add-iam-policy-binding",
          s.project,
          "--member",
          f"serviceAccount:{consumer_sa}",
          "--role",
          "roles/bigquery.jobUser",
      ]
  )
  # Dataset-scoped write access via GA BigQuery DCL, piped over the same
  # bq query stdin path the DDL uses. The obvious alternatives both fail
  # live: `bq add-iam-policy-binding --dataset` needs an allowlisted
  # preview API ("This feature requires allowlisting"), and `bq update
  # --source /dev/stdin` rejects stdin ("Source path is not a file" — bq
  # requires a regular file). GRANT is idempotent, so re-runs converge.
  runner.run(
      [*bq, "query", "--use_legacy_sql=false"],
      input_text=(
          "GRANT `roles/bigquery.dataEditor` ON SCHEMA"
          f" `{s.project}.{s.dataset}`"
          f' TO "serviceAccount:{consumer_sa}";'
      ),
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
          # Single-token --args=... form: gcloud argparse treats a
          # space-separated value that begins with '-' as another flag
          # ("--args: expected one argument").
          f"--args={_CONSUMER_GUNICORN_ARGS}",
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

  echo("==> Ensuring the push subscription (OIDC) with DLQ")
  # One shared flag list for create AND the repair/update path, so the
  # settings structurally cannot drift apart.
  push_flags = [
      "--push-endpoint",
      f"{consumer_url}/",
      "--push-auth-service-account",
      push_sa,
      "--dead-letter-topic",
      DLQ_TOPIC,
      "--max-delivery-attempts",
      MAX_DELIVERY_ATTEMPTS,
      "--ack-deadline",
      ACK_DEADLINE_SECONDS,
  ]
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
              *push_flags,
          ]
      )
      is None
  ):
    runner.run(
        [
            "gcloud",
            "pubsub",
            "subscriptions",
            "update",
            SUBSCRIPTION,
            *proj,
            *push_flags,
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
  # The listing is a hard `run`, not `try_run`: "can't list" is NOT "doesn't
  # exist". DTS display names are not unique, so treating a transient listing
  # failure as absence would mint a duplicate scheduled MERGE on every flaky
  # re-run. bq requires --transfer_location (the dataset location) here.
  existing_name = _find_merge_config(
      runner.run(
          [
              *bq,
              "ls",
              "--transfer_config",
              f"--transfer_location={s.bq_location}",
              "--format=json",
          ]
      ),
      s.dataset,
  )
  # --service_account_name pins the transfer config to the consumer SA
  # (which already holds the BigQuery roles). Without it DTS runs the MERGE
  # on the invoking admin's personal OAuth — the projection job silently
  # dies when that admin loses access or leaves. The admin needs
  # iam.serviceAccounts.actAs on the consumer SA for this call.
  dts_sa = f"--service_account_name={consumer_sa}"
  if existing_name:
    # Converge: refresh the SQL so re-runs after crosswalk/MERGE changes
    # never leave a stale scheduled query behind.
    runner.run(
        [
            *bq,
            "update",
            "--transfer_config",
            f"--params={params}",
            # bq forwards service_account_name on update only when
            # update_credentials is set (bq 2.1.28 frontend/command_update);
            # without it the config silently keeps its old credential.
            "--update_credentials",
            dts_sa,
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
            dts_sa,
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
  """Shell-quoted (so a copied plan line runs unchanged), long args elided."""
  if len(arg) > 100:
    return shlex.quote(arg[:97]) + "...[truncated]"
  return shlex.quote(arg)
