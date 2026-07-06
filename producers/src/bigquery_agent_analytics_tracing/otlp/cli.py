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

"""``bqaa-otel`` — enterprise admin CLI for OTel -> BigQuery (issue #324).

``config`` (PR 1) generates deployable telemetry-source artifacts from an
already-deployed receiver's coordinates; ``bootstrap`` (PR 2) deploys the
full pipeline (plan mode by default, ``--execute`` applies). ``verify`` /
smoke (PR 3) lands next in the stack.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

from . import bootstrap as bootstrap_mod
from . import config_artifacts


def _parse_kv(pairs: str) -> dict[str, str]:
  attrs: dict[str, str] = {}
  for pair in pairs.split(","):
    if not pair:
      continue
    key, sep, value = pair.partition("=")
    if not sep or not key:
      raise argparse.ArgumentTypeError(
          f"expected key=value[,key=value...], got {pair!r}"
      )
    attrs[key] = value
  return attrs


def _build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      prog="bqaa-otel",
      description=(
          "Enterprise admin tooling for Claude Code / Codex OpenTelemetry"
          " export into BigQuery Agent Analytics."
      ),
  )
  sub = parser.add_subparsers(dest="command", required=True)

  config = sub.add_parser(
      "config",
      help=(
          "Generate deployable telemetry-source config artifacts"
          " (Claude Code managed-settings JSON, Codex config.toml, MDM"
          " guidance) for an existing receiver endpoint."
      ),
  )
  config.add_argument(
      "--endpoint",
      required=True,
      help="Receiver base URL, e.g. https://bqaa-otlp-receiver-....run.app",
  )
  config.add_argument(
      "--source",
      default="claude-code",
      help=(
          "Comma-separated telemetry sources:"
          f" {','.join(config_artifacts.SOURCES)}"
      ),
  )
  config.add_argument(
      "--signals",
      default="logs,metrics",
      help="Signal tier: 'logs,metrics' (default) or 'logs,metrics,traces'.",
  )
  config.add_argument(
      "--privacy",
      default="baseline",
      choices=config_artifacts.PRIVACY_TIERS,
      help=(
          "Privacy tier. 'baseline' (default) captures no prompt/tool"
          " content; 'replay' requires --i-understand-content-logging."
      ),
  )
  config.add_argument(
      "--token",
      default=None,
      help=(
          "Bearer token to embed. Default: a <token> placeholder the admin"
          " fills in (keeps secrets out of generated files)."
      ),
  )
  config.add_argument(
      "--resource-attributes",
      type=_parse_kv,
      default=None,
      metavar="K=V[,K=V...]",
      help="OTEL_RESOURCE_ATTRIBUTES to stamp, e.g. department=engineering.",
  )
  config.add_argument(
      "--i-understand-content-logging",
      action="store_true",
      dest="ack_content_logging",
      help=(
          "Required with --privacy replay: acknowledges prompt text (which"
          " can contain full conversation history) will be exported."
      ),
  )
  config.add_argument(
      "--out",
      type=pathlib.Path,
      default=pathlib.Path("."),
      help="Directory to write artifacts into (default: current directory).",
  )

  boot = sub.add_parser(
      "bootstrap",
      help=(
          "Deploy the full OTel->BigQuery pipeline: schema + views, Pub/Sub"
          " + DLQ, bearer-token secret, Cloud Run receiver + consumer,"
          " scheduled MERGE, and telemetry-source config artifacts. Default"
          " is plan mode; pass --execute to apply."
      ),
  )
  boot.add_argument("--project", required=True, help="GCP project id.")
  boot.add_argument(
      "--dataset", default="agent_analytics", help="BigQuery dataset id."
  )
  boot.add_argument(
      "--region",
      default="us-central1",
      help="Cloud Run / Artifact Registry region.",
  )
  boot.add_argument(
      "--bq-location", default="US", help="BigQuery dataset location."
  )
  boot.add_argument(
      "--source",
      default="claude-code",
      help=(
          "Comma-separated telemetry sources:"
          f" {','.join(config_artifacts.SOURCES)}"
      ),
  )
  boot.add_argument(
      "--signals",
      default="logs,metrics",
      help="Signal tier: 'logs,metrics' (default) or 'logs,metrics,traces'.",
  )
  boot.add_argument(
      "--privacy",
      default="baseline",
      choices=config_artifacts.PRIVACY_TIERS,
      help=(
          "Privacy tier. 'baseline' (default) captures no prompt/tool"
          " content; 'replay' requires --i-understand-content-logging."
      ),
  )
  boot.add_argument(
      "--source-product",
      default="claude_code",
      help=(
          "source_product stamped on ingested rows by the receiver"
          " (BQAA_OTLP_SOURCE_PRODUCT)."
      ),
  )
  boot.add_argument(
      "--resource-attributes",
      type=_parse_kv,
      default=None,
      metavar="K=V[,K=V...]",
      help="OTEL_RESOURCE_ATTRIBUTES to stamp, e.g. department=engineering.",
  )
  boot.add_argument(
      "--i-understand-content-logging",
      action="store_true",
      dest="ack_content_logging",
      help=(
          "Required with --privacy replay: acknowledges prompt text (which"
          " can contain full conversation history) will be exported."
      ),
  )
  boot.add_argument(
      "--out",
      type=pathlib.Path,
      default=pathlib.Path("."),
      help="Directory to write config artifacts into after the deploy.",
  )
  boot.add_argument(
      "--execute",
      action="store_true",
      help="Apply the plan (default: print the commands and exit).",
  )
  return parser


def _report_settings_error(exc: ValueError) -> int:
  """Print a tier/settings ValueError (shared by config and bootstrap)."""
  print(f"bqaa-otel: error: {exc}", file=sys.stderr)
  if "acknowledge_content_logging" in str(exc):
    print(
        "bqaa-otel: --privacy replay exports prompt text; pass"
        " --i-understand-content-logging only if that is acceptable"
        " in your environment.",
        file=sys.stderr,
    )
  return 2


def _cmd_config(args: argparse.Namespace) -> int:
  try:
    spec = config_artifacts.BootstrapSpec(
        endpoint=args.endpoint,
        signals=tuple(s for s in args.signals.split(",") if s),
        privacy=args.privacy,
        token=args.token,
        resource_attributes=args.resource_attributes,
        acknowledge_content_logging=args.ack_content_logging,
    )
    artifacts = config_artifacts.render_artifacts(
        spec, sources=tuple(s for s in args.source.split(",") if s)
    )
  except ValueError as exc:
    return _report_settings_error(exc)

  if spec.privacy == "replay":
    print(
        "bqaa-otel: WARNING: replay tier enables content logging — prompt"
        " text (which can contain full conversation history) will be"
        " exported to your telemetry endpoint.",
        file=sys.stderr,
    )

  args.out.mkdir(parents=True, exist_ok=True)
  for filename, content in artifacts.items():
    (args.out / filename).write_text(content)
    print(f"wrote {args.out / filename}")

  print()
  print("Next admin action:")
  if "claude-code.managed-settings.json" in artifacts:
    print(
        "  * Claude Code: paste claude-code.managed-settings.json into the"
        " Claude admin console managed settings (Owner/Primary Owner; no"
        " admin API), or deploy it endpoint-managed via MDM — see"
        " claude-code.endpoint-managed.md."
    )
  if "codex.config.toml" in artifacts:
    print(
        "  * Codex: merge codex.config.toml into each user's"
        " ~/.codex/config.toml (user-level; [otel] is ignored in"
        " project-local config) and set BQAA_OTLP_TOKEN."
    )
  return 0


def _cmd_bootstrap(args: argparse.Namespace) -> int:
  try:
    settings = bootstrap_mod.BootstrapSettings(
        project=args.project,
        dataset=args.dataset,
        region=args.region,
        bq_location=args.bq_location,
        signals=tuple(s for s in args.signals.split(",") if s),
        privacy=args.privacy,
        sources=tuple(s for s in args.source.split(",") if s),
        source_product=args.source_product,
        resource_attributes=args.resource_attributes,
        acknowledge_content_logging=args.ack_content_logging,
        out_dir=args.out,
    )
  except ValueError as exc:
    return _report_settings_error(exc)

  if not args.execute:
    print(bootstrap_mod.render_plan(settings))
    return 0

  # The Cloud Build step uploads the *current directory* as the build
  # context; refuse to mutate anything unless we are at the repo root.
  if not pathlib.Path("deploy/otlp_receiver/Dockerfile").is_file():
    print(
        "bqaa-otel: error: deploy/otlp_receiver/Dockerfile not found —"
        " run --execute from the repository root (the Cloud Build step"
        " uploads the current directory as the build context).",
        file=sys.stderr,
    )
    return 2

  try:
    bootstrap_mod.run_bootstrap(settings, bootstrap_mod.SubprocessRunner())
  except subprocess.CalledProcessError as exc:
    cmd = exc.cmd if isinstance(exc.cmd, str) else " ".join(exc.cmd)
    print(f"bqaa-otel: deploy step failed: {cmd}", file=sys.stderr)
    for stream in (exc.stderr, exc.stdout):
      if stream and stream.strip():
        print(stream.strip(), file=sys.stderr)
    print(
        "bqaa-otel: fix the underlying error and re-run — every step is"
        " idempotent/convergent, so a re-run resumes safely.",
        file=sys.stderr,
    )
    return 1
  return 0


def main(argv: list[str] | None = None) -> int:
  args = _build_parser().parse_args(argv)
  if args.command == "config":
    return _cmd_config(args)
  if args.command == "bootstrap":
    return _cmd_bootstrap(args)
  raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
  sys.exit(main())
