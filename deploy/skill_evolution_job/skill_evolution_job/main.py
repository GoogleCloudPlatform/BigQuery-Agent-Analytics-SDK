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

"""Runner for the skill-evolution agent.

Runs as a CLI, a Cloud Run Job, or a tool self-test.

Usage:
    # Full loop: quality report from BigQuery -> evolve -> PR
    python main.py --full-loop

    # From an existing quality report:
    python main.py --report path/to/quality_report.json
    python main.py --report path/to/quality_report.json --mode coevolve

    # Self-test (deploy.sh --smoke greps for "SELF-TEST PASS"):
    python main.py --test

As a Cloud Run Job with no argv, behavior is driven by env vars:
ISSUE_NUMBER (issue-triggered), FULL_LOOP=true (weekly scheduled run),
or QUALITY_REPORT_PATH (pre-produced report).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import uuid

from . import config
from . import hooks
from . import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Suppress noisy libraries.
for _noisy in (
    "google.genai",
    "google_genai",
    "google.adk",
    "google_adk",
    "google.auth",
    "google_auth",
    "httpx",
    "httpcore",
):
  logging.getLogger(_noisy).setLevel(logging.ERROR)

logger = logging.getLogger("skill_evolution_job.run")


def _default_run_dir(suffix: str) -> str:
  ts = time.strftime("%Y-%m-%d_%H%M%S")
  return os.path.join(
      tempfile.gettempdir(), "skill_evolution_runs", f"{ts}_{suffix}"
  )


def _parse_labels(raw: str | None) -> dict[str, str]:
  labels: dict[str, str] = {}
  for pair in (raw or "").split(","):
    if "=" in pair:
      key, value = pair.split("=", 1)
      if key.strip():
        labels[key.strip()] = value.strip()
  return labels


def _bigquery_quality_report(run_dir: str) -> tuple[str | None, int]:
  """Pre-flight from real traces: score BigQuery sessions.

  Writes ``trace_selector.json`` (the exact slice this run evolves
  against) into the run dir, then produces ``v0_quality_report.json``
  via the SDK's ``scripts/quality_report.py``.

  Returns ``(report_path, total_sessions)``; ``report_path`` is None
  when the report failed or holds fewer than MIN_SESSIONS sessions.
  """
  from . import tools

  cfg = config.get_config()
  labels = _parse_labels(cfg.evolution_trace_labels)
  try:
    app_name = registry.get_registry().app_name_for()
  except registry.RegistryError:
    app_name = None
  selector = {
      "time_period": cfg.eval_time_period,
      "agent_version": cfg.agent_version,
      "labels": labels,
      "app_name": cfg.quality_app_name or app_name,
  }
  os.makedirs(run_dir, exist_ok=True)
  with open(os.path.join(run_dir, "trace_selector.json"), "w") as f:
    json.dump(selector, f, indent=2)
  logger.info("Trace selector for this run: %s", selector)

  result = tools.run_quality_report(
      output_path=os.path.join(run_dir, "quality_report.json"),
      run_dir=run_dir,
  )
  if result.get("error"):
    logger.warning("BigQuery quality report failed: %s", result["error"])
    return None, 0
  total = int(
      result.get("total_sessions")
      or result.get("summary", {}).get("total_sessions", 0)
  )
  if total < cfg.min_sessions:
    return None, total

  saved = result.get("report_path") or os.path.join(
      run_dir, "quality_report.json"
  )
  report_path = os.path.join(run_dir, "v0_quality_report.json")
  if os.path.abspath(saved) != os.path.abspath(report_path):
    os.replace(saved, report_path)
  logger.info(
      "Pre-flight quality report from BigQuery: %d sessions -> %s",
      total,
      report_path,
  )
  return report_path, total


def run_test() -> None:
  """Self-test: registry, engine, tools — no agent run, no evolution.

  deploy.sh --smoke greps this output for the exact sentinel
  ``SELF-TEST PASS``; any earlier hard failure raises and the job exits
  non-zero instead.
  """
  from . import engine
  from . import tools

  print("--- Engine ---")
  path = engine.engine_path()
  engine.load_engine()
  supported = sorted(engine.supported_kwargs())
  print(f"  engine: {path}")
  print(f"  evolve_skill kwargs: {supported or ['**kwargs']}")

  print("--- Registry ---")
  if config.get_config().agent_registry:
    reg = registry.get_registry()
    for name in reg.ordered_names():
      spec = reg.agent(name)
      exists = os.path.isfile(os.path.join(spec.skill_dir, "SKILL.md"))
      print(
          f"  {name}: {spec.skill_dir}"
          f" (SKILL.md {'found' if exists else 'MISSING'})"
      )
  else:
    print("  AGENT_REGISTRY not set — skipping (required for evolution runs)")

  print("--- Tools ---")
  from .agent import root_agent

  tool_names = sorted(
      getattr(t, "__name__", type(t).__name__) for t in root_agent.tools
  )
  print(f"  {len(tool_names)} tools registered: {', '.join(tool_names)}")

  print("--- Hooks ---")
  for name in hooks.HOOK_NAMES:
    hook, source = hooks.get_hook(name)
    print(f"  {name}: {source if hook else 'skip (' + source + ')'}")

  assert callable(tools.run_quality_report)
  print("\nSELF-TEST PASS")


async def run_evolution_agent(
    report_path: str | None = None,
    mode: str = "auto",
    run_dir: str | None = None,
    rounds: int | None = None,
    candidates: int | None = None,
    min_failures: int | None = None,
    from_issue: int | None = None,
) -> str:
  """Run the skill-evolution agent and return its response."""
  # Set env vars for tools to pick up (only when overridden).
  if min_failures is not None:
    os.environ["MIN_FAILURES"] = str(min_failures)

  # Build override notes — only mention params that were explicitly set.
  overrides = []
  if rounds is not None:
    overrides.append(f"Maximum rounds: {rounds}")
  if candidates is not None:
    overrides.append(f"Candidates: {candidates} (best-of-N selection)")
  if min_failures is not None:
    overrides.append(f"Min failures threshold: {min_failures}")
  overrides_note = "\n".join(overrides) + "\n" if overrides else ""

  if from_issue is not None:
    # Issue-triggered mode: parse the quality issue and run evolution.
    if run_dir is None:
      run_dir = _default_run_dir(f"issue_{from_issue}")

    prompt = (
        f"A quality issue has been filed: #{from_issue}.\n"
        f"Run directory: {run_dir}\n"
        f"{overrides_note}"
        "\nFollow the issue-triggered evolution workflow:\n\n"
        f"1. parse_quality_issue({from_issue}) — read the issue details\n"
        "2. Identify the agent to evolve from the issue metadata\n"
        "3. Extract the 'Quality report' URI from the issue metadata"
        " table.\n"
        "   - If it starts with gs://, call download_from_gcs to get it"
        " locally\n"
        "   - If it's a local path, use it directly\n"
        "4. snapshot_skills('initial', run_dir)\n"
        "5. count_failures on the quality report\n"
        "6. If enough failures: detect_bottleneck_tool, run_evolution\n"
        '7. score_candidate on the winner — a {"skipped": ...} result'
        " means no host scoring hook is configured: rely on the engine's"
        " internal candidate selection instead\n"
        "8. snapshot_skills for the evolved version\n"
        "9. compare_versions(run_dir)\n"
        "10. upload_run_to_gcs if configured\n"
        f"11. create_evolution_pr(issue_number={from_issue}) — "
        f"PR with Fixes #{from_issue}\n\n"
        "IMPORTANT: Use the quality report from the issue — it contains"
        " real production data.\n\n"
        "Do NOT restore skills — the evolved skill stays deployed.\n"
        "Report the results."
    )
    logger.info(
        "Starting issue-triggered evolution: issue=#%s, run_dir=%s",
        from_issue,
        run_dir,
    )

  elif report_path is None:
    # Full loop mode: produce the baseline quality report BEFORE the agent.
    if run_dir is None:
      run_dir = _default_run_dir("evolution")
    os.makedirs(run_dir, exist_ok=True)
    cfg = config.get_config()

    report_path = None
    total = 0
    if cfg.quality_source.lower() == "bigquery":
      report_path, total = _bigquery_quality_report(run_dir)

    if report_path is None:
      # Not enough real sessions (or QUALITY_SOURCE=synthetic): the only
      # other baseline source is host-generated traffic.
      traffic_hook, reason = hooks.get_hook("traffic")
      if traffic_hook is None:
        msg = (
            f"insufficient sessions ({total} <"
            f" {cfg.min_sessions} MIN_SESSIONS) and no traffic hook"
            f" configured ({reason}) — nothing to do."
        )
        logger.info(msg)
        return f"NOTHING TO DO: {msg}"
      logger.info("Running traffic hook to generate baseline sessions")
      traffic_result = traffic_hook(run_dir)
      if isinstance(traffic_result, dict) and traffic_result.get(
          "returncode", 0
      ):
        logger.error("Traffic hook failed: %s", traffic_result)
        return f"ERROR: Traffic generation failed: {traffic_result}"
      report_path, total = _bigquery_quality_report(run_dir)
      if report_path is None:
        return (
            "ERROR: still fewer than MIN_SESSIONS sessions"
            f" ({total}) after the traffic hook ran — check that the"
            " generated traffic logs to the configured dataset and that"
            " the trace selector (labels/app_name/time period) matches."
        )

    logger.info(
        "Pre-flight complete. Starting agent with report: %s", report_path
    )

    # Quality gate: when the baseline already meets the threshold there
    # is nothing to evolve — stop instead of burning an evolution round.
    threshold = (cfg.quality_threshold or 0.95) * 100
    try:
      with open(report_path) as f:
        v0_rate = float(
            json.load(f).get("summary", {}).get("meaningful_rate", 0)
        )
    except Exception:  # noqa: BLE001 - malformed report -> evolve anyway
      v0_rate = 0.0
    if v0_rate >= threshold:
      msg = (
          f"QUALITY GATE: V0 meaningful rate {v0_rate:.1f}% meets the"
          f" threshold ({threshold:.0f}%) — nothing to evolve, stopping."
      )
      logger.info(msg)
      return msg

    prompt = (
        f"Quality report is ready at {report_path}.\n"
        f"Run directory: {run_dir}\n"
        f"{overrides_note}\n"
        "The baseline quality report has been generated.\n"
        "Follow the evolution algorithm from your skill. Decide rounds,\n"
        "candidates, and failure thresholds based on the quality data —\n"
        "unless overrides are listed above.\n\n"
        "1. snapshot_skills('initial', run_dir) — save current skills\n"
        "2. For each round:\n"
        "   a. count_failures on the quality report\n"
        "   b. If enough failures: detect_bottleneck_tool, then"
        " run_evolution (or run_coevolution when both agents are"
        " bottlenecks)\n"
        '   c. score_candidate on the winner — a {"skipped": ...}'
        " result means no host scoring hook is configured: rely on the"
        " engine's internal candidate selection and continue\n"
        "   d. snapshot_skills for the evolved version\n"
        "   e. Report the delta from the previous version where"
        " measurable\n"
        "   f. If failures drop below threshold, stop — no more rounds\n"
        "3. compare_versions(run_dir) for the final table\n"
        "4. upload_run_to_gcs if configured\n"
        "5. If the evolution produced a winner: create_evolution_pr\n"
        "Do NOT restore skills — the evolved skill stays deployed.\n"
        "Report the results."
    )
    logger.info(
        "Starting full evolution loop: rounds=%s, candidates=%s,"
        " min_failures=%s, run_dir=%s",
        rounds or "agent-decided",
        candidates or "agent-decided",
        min_failures or "agent-decided",
        run_dir,
    )
  elif mode == "auto":
    if run_dir:
      prompt = (
          f"Quality report is ready at {report_path}.\n"
          f"Run directory: {run_dir}\n"
          f"{overrides_note}\n"
          "Initial skills and snapshots are already in the run"
          " directory.\n"
          "Follow the evolution algorithm from your skill. Decide"
          " rounds,\n"
          "candidates, and failure thresholds based on the quality data"
          " —\n"
          "unless overrides are listed above.\n\n"
          "Start from the gate check:\n"
          "1. count_failures on the quality report\n"
          "2. If enough failures: detect_bottleneck_tool, then"
          " run_evolution\n"
          '3. score_candidate on the winner — a {"skipped": ...}'
          " result means no host scoring hook: rely on the engine's"
          " internal selection\n"
          "4. snapshot_skills for the evolved version\n"
          "5. Report the delta from the previous version where"
          " measurable\n"
          "6. count_failures on the new report if one was produced. If"
          " below threshold, STOP — do not snapshot a duplicate"
          " version. Otherwise repeat evolution for the next round.\n"
          "7. compare_versions(run_dir) for the final table\n"
          "Do NOT restore skills — the evolved skill stays deployed.\n"
          "Only report versions that were actually evolved."
      )
    else:
      prompt = (
          f"Analyze the quality report at {report_path}. "
          f"{overrides_note}"
          "Detect the bottleneck, evolve the appropriate agent(s), and"
          " report your findings."
      )
  elif mode == "coevolve":
    prompt = (
        f"Run co-evolution on the quality report at {report_path}. "
        "Use run_coevolution which handles bottleneck detection and "
        "multi-agent evolution automatically. Then compare_versions and "
        "create_evolution_pr if a winner emerged."
    )
  else:
    run_dir_note = (
        f' and run_dir="{run_dir}" (pass this exact run_dir to every'
        " tool that accepts one)"
        if run_dir
        else ""
    )
    prompt = (
        f"Run skill evolution on the {mode} agent using the quality "
        f"report at {report_path}. Call run_evolution with "
        f'skill_dir="{mode}"{run_dir_note}. Then compare_versions and'
        " report the outcome."
    )

  if report_path:
    logger.info("Starting skill evolution agent: %s mode", mode)
    logger.info("Quality report: %s", report_path)

  # ADK imports are deferred past the pre-flight so the short-circuit
  # paths (insufficient sessions, quality gate) never need the agent.
  from google.adk.runners import Runner
  from google.adk.sessions.in_memory_session_service import InMemorySessionService
  from google.genai import types

  from .agent import app

  runner = Runner(
      app=app,
      session_service=InMemorySessionService(),
      auto_create_session=True,
  )
  user_id = f"skill_evolution_{uuid.uuid4().hex[:8]}"
  session_id = f"evolution_{uuid.uuid4().hex[:8]}"

  response_parts = []
  async for event in runner.run_async(
      user_id=user_id,
      session_id=session_id,
      new_message=types.Content(
          role="user",
          parts=[types.Part.from_text(text=prompt)],
      ),
  ):
    if event.author != "user" and event.content and not event.partial:
      for part in event.content.parts:
        if part.text:
          response_parts.append(part.text)

  return "\n".join(response_parts)


def _run_batch_mode(args, common_kwargs) -> str:
  """Batch mode: run evolution once enough quality issues accumulate."""
  import re
  import subprocess

  from . import gcs_utils
  from . import tools

  min_issues = int(os.getenv("EVOLUTION_MIN_OPEN_ISSUES", "10"))

  cmd = [
      "gh",
      "issue",
      "list",
      "--state",
      "open",
      "--label",
      "quality",
      "--json",
      "number,title,createdAt,body",
      "--limit",
      "200",
  ] + tools._gh_repo_args()
  if not config.get_config().github_repo:
    logger.error("--batch requires GITHUB_REPO (gh needs a target repo)")
    sys.exit(1)
  try:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
      logger.error("gh issue list failed: %s", config.mask_tokens(r.stderr))
      sys.exit(1)
    issues = json.loads(r.stdout)
  except FileNotFoundError:
    logger.error("gh CLI not available — required for --batch mode")
    sys.exit(1)

  logger.info(
      "Open quality issues: %d (threshold: %d)", len(issues), min_issues
  )
  if len(issues) < min_issues:
    msg = (
        f"Not enough accumulated issues: {len(issues)}/{min_issues}. "
        "Skipping evolution."
    )
    logger.info(msg)
    print(msg)
    return msg

  # Uses the most recent quality report only. When issues span multiple
  # quality runs, the full picture would need all distinct report URIs
  # merged — left to the issue-triggered path for now.
  report_uri = None
  for issue in sorted(issues, key=lambda i: i["createdAt"], reverse=True):
    body = issue.get("body", "")
    match = re.search(r"\|\s*Quality report\s*\|\s*`([^`]+)`\s*\|", body)
    if match:
      report_uri = match.group(1)
      break

  if not report_uri:
    logger.warning("No quality report URI found in any open issue")
    report_path = None
  elif report_uri.startswith("gs://"):
    run_dir = args.run_dir or _default_run_dir("batch")
    os.makedirs(run_dir, exist_ok=True)
    local_report = os.path.join(run_dir, "quality_report.json")
    dl = gcs_utils.download_from_gcs(report_uri, local_report)
    if dl.get("status") != "success":
      logger.error("GCS download failed: %s", dl)
      sys.exit(1)
    report_path = local_report
  else:
    report_path = report_uri if os.path.isfile(report_uri) else None

  issue_numbers = [i["number"] for i in issues]
  logger.info(
      "Running batch evolution for %d issues: %s",
      len(issue_numbers),
      issue_numbers[:10],
  )

  return asyncio.run(
      run_evolution_agent(
          report_path=report_path,
          mode=getattr(args, "mode", "auto"),
          run_dir=args.run_dir,
          **common_kwargs,
      )
  )


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Run the skill-evolution agent",
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog="""
Examples:
  %(prog)s --full-loop                           # agent decides everything
  %(prog)s --full-loop --rounds 1                # override: single round
  %(prog)s --full-loop --candidates 5            # override: best-of-5
  %(prog)s --report runs/.../quality_report.json # from an existing report
  %(prog)s --report report.json --mode coevolve
  %(prog)s --test
        """,
  )
  # Set AGENT_REGISTRY early so registry resolution sees it.
  for i, arg in enumerate(sys.argv):
    if arg == "--agent-registry" and i + 1 < len(sys.argv):
      os.environ["AGENT_REGISTRY"] = sys.argv[i + 1]
      break

  # Agent-name mode choices need the registry; it may live inside the
  # host-repo clone, so failure here only narrows --mode choices.
  try:
    agent_names = list(registry.get_registry().agents)
  except Exception:  # noqa: BLE001 - registry resolved again at run time
    agent_names = []

  parser.add_argument(
      "--agent-registry",
      metavar="PATH",
      help=(
          "Path to agent_registry.json (absolute, or relative to the host"
          " repo workdir). Overrides the AGENT_REGISTRY env var."
      ),
  )
  parser.add_argument(
      "--from-issue",
      type=int,
      metavar="N",
      help=(
          "GitHub issue number to process (parse issue, evolve, create PR"
          " with Fixes #N)"
      ),
  )
  parser.add_argument("--report", help="Path to quality report JSON file")
  parser.add_argument(
      "--full-loop",
      action="store_true",
      help="Run the full pipeline: quality report -> evolve -> PR",
  )
  parser.add_argument(
      "--run-dir",
      help="Directory for run artifacts (default: timestamped tempdir)",
  )
  parser.add_argument(
      "--mode",
      default="auto",
      choices=["auto", "coevolve"] + agent_names,
      help=(
          "Evolution mode: auto (detect bottleneck), coevolve"
          " (multi-agent), or a specific agent name (default: auto)."
          + (
              f" Available agents: {', '.join(agent_names)}"
              if agent_names
              else ""
          )
      ),
  )
  parser.add_argument(
      "--rounds",
      type=int,
      default=None,
      help="Maximum evolution rounds (default: agent decides)",
  )
  parser.add_argument(
      "--candidates",
      type=int,
      default=None,
      help="Consolidation candidates for best-of-N (default: agent decides)",
  )
  parser.add_argument(
      "--min-failures",
      type=int,
      default=None,
      help="Minimum failures required to evolve (default: agent decides)",
  )
  parser.add_argument(
      "--trace-labels",
      help=(
          "Evolve only traces matching these custom_tags labels,"
          " comma-separated k=v. Binding: exported as"
          " EVOLUTION_TRACE_LABELS for the BigQuery pre-flight."
      ),
  )
  parser.add_argument(
      "--quality-source",
      type=str,
      choices=["bigquery", "synthetic"],
      default=None,
      help=(
          "Baseline source. FORCES QUALITY_SOURCE past the container's"
          " env default: 'bigquery' scores real sessions; 'synthetic'"
          " goes straight to the host traffic hook."
      ),
  )
  parser.add_argument(
      "--batch",
      action="store_true",
      help=(
          "Batch mode: run evolution only once enough open quality issues"
          " have accumulated (EVOLUTION_MIN_OPEN_ISSUES, default 10)"
      ),
  )
  parser.add_argument(
      "--test",
      action="store_true",
      help="Self-test tools/engine/registry only, don't run the agent",
  )
  args = parser.parse_args()

  if args.quality_source:
    os.environ["QUALITY_SOURCE"] = args.quality_source

  # Make the scoping flags BINDING at the tool layer (the orchestrating
  # agent treats prompt overrides as hints; these env vars are enforced
  # by evolve.py / coevolve.py regardless of what the agent decides).
  if args.candidates:
    os.environ["EVOLUTION_CANDIDATES"] = str(args.candidates)
  if args.rounds:
    os.environ["EVOLUTION_MAX_ROUNDS"] = str(args.rounds)
  if args.mode not in ("auto", "coevolve"):
    os.environ["EVOLUTION_TARGET_AGENTS"] = args.mode
  if getattr(args, "trace_labels", None):
    os.environ["EVOLUTION_TRACE_LABELS"] = args.trace_labels
  bound = {
      k: os.environ[k]
      for k in (
          "EVOLUTION_CANDIDATES",
          "EVOLUTION_MAX_ROUNDS",
          "EVOLUTION_TARGET_AGENTS",
          "EVOLUTION_TRACE_LABELS",
      )
      if k in os.environ
  }
  if bound:
    logger.info("Binding overrides (enforced in tools): %s", bound)

  if args.test:
    run_test()
    return

  common_kwargs = dict(
      rounds=args.rounds,
      candidates=args.candidates,
      min_failures=args.min_failures,
  )

  if args.batch:
    result = _run_batch_mode(args, common_kwargs)
  elif args.from_issue:
    result = asyncio.run(
        run_evolution_agent(
            from_issue=args.from_issue,
            run_dir=args.run_dir,
            **common_kwargs,
        )
    )
  elif args.full_loop:
    result = asyncio.run(
        run_evolution_agent(
            report_path=None, run_dir=args.run_dir, **common_kwargs
        )
    )
  elif args.report:
    report_path = args.report
    if report_path.startswith("gs://"):
      from . import gcs_utils

      local_dir = args.run_dir or _default_run_dir("evolution")
      os.makedirs(local_dir, exist_ok=True)
      local_report = os.path.join(local_dir, "quality_report.json")
      logger.info("Downloading report from GCS: %s", report_path)
      dl = gcs_utils.download_from_gcs(report_path, local_report)
      if dl.get("status") != "success":
        logger.error("GCS download failed: %s", dl)
        sys.exit(1)
      report_path = local_report
    elif not os.path.isfile(report_path):
      logger.error("Quality report not found: %s", report_path)
      sys.exit(1)
    result = asyncio.run(
        run_evolution_agent(
            report_path=report_path,
            mode=args.mode,
            run_dir=args.run_dir,
            **common_kwargs,
        )
    )
  else:
    # Cloud Run Job / CI mode: behavior from env vars.
    cfg = config.get_config()
    if cfg.issue_number:
      result = asyncio.run(
          run_evolution_agent(
              from_issue=int(cfg.issue_number),
              run_dir=args.run_dir,
              **common_kwargs,
          )
      )
    elif cfg.full_loop:
      result = asyncio.run(
          run_evolution_agent(
              report_path=None, run_dir=args.run_dir, **common_kwargs
          )
      )
    elif cfg.quality_report_path:
      if not os.path.isfile(cfg.quality_report_path):
        logger.error("Quality report not found: %s", cfg.quality_report_path)
        sys.exit(1)
      result = asyncio.run(
          run_evolution_agent(
              report_path=cfg.quality_report_path,
              mode=args.mode,
              **common_kwargs,
          )
      )
    else:
      parser.error(
          "--from-issue, --report, or --full-loop is required "
          "(or set ISSUE_NUMBER / QUALITY_REPORT_PATH / FULL_LOOP env vars)"
      )
      return  # unreachable, but makes type checkers happy

  print("\n" + "=" * 60)
  print("SKILL EVOLUTION REPORT")
  print("=" * 60)
  print(result)
  print("=" * 60)

  # A pre-flight failure comes back as an "ERROR: ..." result string.
  # Exit non-zero so schedulers and callers see the failure.
  if isinstance(result, str) and result.startswith("ERROR:"):
    sys.exit(1)


if __name__ == "__main__":
  main()
