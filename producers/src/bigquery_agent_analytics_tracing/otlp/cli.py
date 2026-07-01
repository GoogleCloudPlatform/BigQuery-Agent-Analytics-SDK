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

PR 1 ships ``config`` (generate deployable telemetry-source artifacts from
an already-deployed receiver's coordinates). ``bootstrap`` (infra
orchestration, PR 2) and ``verify`` / smoke (PR 3) land next in the stack.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

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
  return parser


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
    print(f"bqaa-otel: error: {exc}", file=sys.stderr)
    if "acknowledge_content_logging" in str(exc):
      print(
          "bqaa-otel: --privacy replay exports prompt text; pass"
          " --i-understand-content-logging only if that is acceptable"
          " in your environment.",
          file=sys.stderr,
      )
    return 2

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


def main(argv: list[str] | None = None) -> int:
  args = _build_parser().parse_args(argv)
  if args.command == "config":
    return _cmd_config(args)
  raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
  sys.exit(main())
