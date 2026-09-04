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

"""ADK tools for the skill-evolution agent.

Wraps the evolution pipeline modules (:mod:`evolve`, :mod:`bottleneck`,
:mod:`coevolve`) as tool functions, plus the surrounding loop the agent
drives: quality reporting from BigQuery, candidate scoring, run
archival to GCS, and issue/PR creation against the host agent repo.

Everything host-specific goes through :mod:`skill_evolution_job.hooks`
(scoring, publish gate) so the default path needs no host plugins.
Registry access is lazy — importing this module never reads
``agent_registry.json``, so the container starts even when the registry
is missing or malformed.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import time

from . import config
from . import engine
from . import hooks
from . import registry
from .config import mask_tokens as _mask_tokens

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry helpers (lazy — never touched at import time)
# ---------------------------------------------------------------------------


def _resolve_skill_dir(skill_dir: str) -> str:
  """Resolve agent name shortcut to absolute skill directory path."""
  try:
    return registry.get_registry().resolve_skill_dir(skill_dir)
  except registry.RegistryError:
    return skill_dir


def _resolve_agent(agent: str | None) -> tuple[str | None, dict | None]:
  """Default/validate an agent name against the registry.

  Returns ``(agent_name, None)`` on success and ``(None, error_dict)``
  when the registry is unavailable or the name is unknown, so tools can
  return the error instead of raising at the agent.
  """
  try:
    reg = registry.get_registry()
  except registry.RegistryError as exc:
    return None, {"status": "error", "error": str(exc)}
  if agent is None:
    return reg.default_agent, None
  if agent not in reg.agents:
    return None, {
        "status": "error",
        "error": (
            f"Agent {agent!r} not found in agent registry. Available:"
            f" {', '.join(sorted(reg.agents))}"
        ),
    }
  return agent, None


def _gh_repo_args() -> list:
  """--repo flag for gh when GITHUB_REPO is set.

  Deployed containers are not git checkouts, so gh cannot infer the
  repository from cwd (observed live: the job's issue creation failed
  with 'not a git repository' and the run fell back to a dry-run PR).
  Locally, without the env var, gh keeps inferring from the checkout.
  """
  repo = os.getenv("GITHUB_REPO", "").strip()
  return ["--repo", repo] if repo else []


def _safe_copy2(src: str, dst: str) -> None:
  """copy2 that no-ops when src and dst are the same file.

  A candidate skill can already be the deployed SKILL.md (e.g. evolution
  wrote it in place), in which case shutil.copy2 raises SameFileError.
  """
  if os.path.abspath(src) == os.path.abspath(dst):
    return
  shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Tool: run_evolution
# ---------------------------------------------------------------------------


def run_evolution(
    quality_report_path: str,
    skill_dir: str,
    run_dir: str | None = None,
    model_id: str | None = None,
    max_workers: int = 10,
    agentic: bool = True,
    candidates: int | None = None,
    max_chars: int = 25000,
) -> dict:
  """Run skill evolution on a single agent.

  Analyzes execution trajectories from a quality report and evolves
  the agent's SKILL.md through a parallel analyst fleet followed by
  patch consolidation.

  When candidates > 1, generates N candidate skills and saves them
  to run_dir as candidate_1.md, candidate_2.md, etc. The first
  candidate is deployed to SKILL.md. Call score_candidate on each
  to find the best, then restore_skills + deploy the winner.

  Args:
      quality_report_path: Path to quality report JSON file.
      skill_dir: Path to agent's skill directory containing SKILL.md.
          Agent name shortcuts from agent_registry.json are accepted.
      run_dir: Run directory for saving candidate files and artifacts.
          Required when candidates > 1.
      model_id: Gemini model for analysts and consolidator. Defaults to
          EVOLUTION_MODEL_ID.
      max_workers: Max parallel analyst threads.
      agentic: Use agentic error analysts with tool access.
      candidates: Number of consolidation candidates (best-of-N).
          None = auto-decide based on meaningful_rate. EVOLUTION_CANDIDATES
          (from --candidates) overrides whatever is passed here.
      max_chars: Max character count for evolved skill (default: 25000).
          Triggers compaction if exceeded to prevent skill bloat.

  Returns:
      Dict with evolved skill content, path, candidate paths, and
      summary statistics.
  """
  from . import evolve as evolve_mod

  model_id = model_id or config.get_config().evolution_model_id
  skill_dir = _resolve_skill_dir(skill_dir)

  if not os.path.isfile(quality_report_path):
    return {"error": f"Quality report not found: {quality_report_path}"}

  skill_path = os.path.join(skill_dir, "SKILL.md")
  if not os.path.isfile(skill_path):
    return {"error": f"SKILL.md not found in {skill_dir}"}

  # EVOLUTION_MAX_ROUNDS (set by main.py from --rounds) is BINDING here
  # too, per agent: the orchestrator cannot re-run evolution on the same
  # skill beyond the cap (a post-round quality report that re-measures
  # the pre-evolution sessions used to trigger a spurious second round).
  refused = _round_guard(f"run_evolution[{skill_dir}]")
  if refused:
    return refused

  # EVOLUTION_CANDIDATES is BINDING over the caller's value; evolve()
  # applies the same rule, this keeps the candidates_dir decision aligned.
  bound = evolve_mod.bound_candidates()
  if bound is not None and candidates != bound:
    if candidates is not None:
      logger.warning(
          "run_evolution asked for candidates=%d; EVOLUTION_CANDIDATES=%d"
          " is binding and wins",
          candidates,
          bound,
      )
    candidates = bound

  candidates_dir = None
  if candidates is not None and candidates > 1:
    if not run_dir:
      return {"error": "run_dir is required when candidates > 1"}
    candidates_dir = os.path.join(run_dir, "candidates")
  elif candidates is None and run_dir:
    candidates_dir = os.path.join(run_dir, "candidates")

  try:
    # Read current skill for comparison
    with open(skill_path) as f:
      original_content = f.read()
    original_size = len(original_content)

    # Always snapshot the pre-evolution skill in run_dir
    if run_dir:
      os.makedirs(run_dir, exist_ok=True)
      agent_name = os.path.basename(os.path.dirname(skill_dir))
      pre_path = os.path.join(run_dir, f"pre_evolution_{agent_name}_skill.md")
      if not os.path.exists(pre_path):
        with open(pre_path, "w") as f:
          f.write(original_content)
        logger.info("Saved pre-evolution snapshot: %s", pre_path)

    # Deterministic, incumbent-guarded candidate selection: score each
    # candidate on the host's eval set and keep the best only if it beats
    # the V0 baseline. Without a score hook there is nothing to measure
    # with, so the engine's size-based selection stays in charge — never
    # a stub scorer, which would score every candidate 0.0 and make the
    # incumbent guard reject everything.
    _score_fn = None
    _incumbent = None
    _score_dir = run_dir or candidates_dir
    score_hook, score_reason = hooks.get_hook("score")
    if score_hook is None:
      logger.info(
          "score hook not configured (%s); engine falls back to size-based"
          " candidate selection",
          score_reason,
      )
    elif _score_dir:
      try:
        with open(quality_report_path) as _rf:
          _incumbent = json.load(_rf).get("summary", {}).get("meaningful_rate")
        if _excluded_count(quality_report_path) > 0:
          logger.warning(
              "Incumbent quality report %s has preflight exclusions;"
              " disabling incumbent_score guard for evolution",
              quality_report_path,
          )
          _incumbent = None
      except Exception:  # noqa: BLE001
        _incumbent = None

      def _score_fn(skill_content, _sd=skill_dir, _rd=_score_dir):
        tmp = os.path.join(_rd, "_score_candidate_tmp.md")
        with open(tmp, "w") as fh:
          fh.write(skill_content)
        res = score_hook(tmp, _sd, _rd)
        if not isinstance(res, dict):
          logger.warning(
              "Score hook returned non-dict %r; candidate unmeasurable", res
          )
          return None
        if res.get("status") == "error" or res.get("error"):
          logger.warning(
              "Score hook reported an error; candidate unmeasurable: %s",
              _mask_tokens(str(res)),
          )
          return None
        if res.get("skipped") or res.get("unmeasurable"):
          return None
        report_path = res.get("report_path")
        if report_path and _excluded_count(report_path) > 0:
          # None = unmeasurable: evolve() floors it for selection but
          # never records it as the deployed score, so a flaked run
          # cannot lower the bar.
          logger.warning(
              "Candidate scored report %s has preflight exclusions; score"
              " unmeasurable",
              report_path,
          )
          return None
        rate = res.get("meaningful_rate")
        if rate is None:
          return None
        return float(rate)

    evolved_content = evolve_mod.evolve(
        report_path=quality_report_path,
        skill_dir=skill_dir,
        model_id=model_id,
        max_workers=max_workers,
        agentic=agentic,
        candidates=candidates,
        candidates_dir=candidates_dir,
        max_chars=max_chars,
        score_fn=_score_fn,
        incumbent_score=_incumbent,
    )

    # Write evolved skill (first candidate or single result)
    with open(skill_path, "w") as f:
      f.write(evolved_content)

    result = {
        "status": "success",
        "skill_path": skill_path,
        "original_size": original_size,
        "evolved_size": len(evolved_content),
        "growth": f"{len(evolved_content) - original_size:+d} chars",
    }

    # List candidate paths if best-of-N was used
    if candidates_dir and os.path.isdir(candidates_dir):
      cand_files = sorted(
          [
              os.path.join(candidates_dir, f)
              for f in os.listdir(candidates_dir)
              if f.startswith("candidate_") and f.endswith(".md")
          ]
      )
      result["candidate_paths"] = cand_files
      result["candidates_dir"] = candidates_dir
      if _score_fn is not None:
        # Selection already happened inside evolve(): every candidate
        # was scored on the eval set and the winner (incumbent-guarded)
        # is deployed to SKILL.md. Telling the agent to score again
        # sent deployed runs into per-candidate replay loops.
        result["note"] = (
            f"Generated {len(cand_files)} candidates; each was already"
            " scored on the eval set and the best (incumbent-guarded) is"
            " deployed to SKILL.md. Do NOT re-score candidates with"
            " score_candidate — proceed to snapshot/validation of the"
            " deployed winner."
        )
      else:
        result["note"] = (
            f"Generated {len(cand_files)} candidates. First candidate"
            " deployed to SKILL.md. Score each with score_candidate and"
            " pick the best."
        )

    return result

  except Exception as e:
    logger.error("Evolution failed: %s", e, exc_info=True)
    return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: detect_bottleneck_tool
# ---------------------------------------------------------------------------


def detect_bottleneck_tool(quality_report_path: str) -> dict:
  """Detect which agent is the primary quality bottleneck.

  Classifies failures as routing, skill, tool, or architecture issues.
  Recommends which agent to evolve based on the agent registry.

  Args:
      quality_report_path: Path to quality report JSON file.

  Returns:
      Dict with recommendation (agent name, both, or none),
      failure counts, and confidence score.
  """
  from google import genai

  from . import bottleneck as bottleneck_mod

  cfg = config.get_config()
  target_env = (cfg.evolution_target_agents or "").strip()
  if target_env:
    # Target bound (--mode <agent>): classification would spend
    # ~5 min of LLM calls concluding what was already decided.
    return {
        "recommendation": target_env,
        "note": (
            f"Skipped classification: EVOLUTION_TARGET_AGENTS={target_env}"
            " binds the evolution target. Proceed directly to evolution."
        ),
    }

  if not os.path.isfile(quality_report_path):
    return {"error": f"Quality report not found: {quality_report_path}"}

  cached = _bottleneck_cache.get(os.path.abspath(quality_report_path))
  if cached is not None:
    return {**cached, "note": "cached (already classified this report)"}

  with open(quality_report_path) as f:
    report = json.load(f)

  client = genai.Client(
      vertexai=True,
      project=cfg.project_id,
      # Model endpoint, not the infra region: gemini-3.x is global-only.
      location=cfg.model_location
      or os.getenv("GOOGLE_CLOUD_LOCATION")
      or "global",
  )

  try:
    result = bottleneck_mod.detect_bottleneck(report, client)
    out = {
        "recommendation": result.recommendation,
        "confidence": result.confidence,
        "total_failures": result.total_failures,
        "routing_failures": len(result.routing_failures),
        "skill_failures": len(result.skill_failures),
        "tool_failures": len(result.tool_failures),
        "architecture_failures": len(result.architecture_failures),
        "summary": result.summary,
    }
    # Cache so coevolve (and repeat tool calls) reuse this instead of
    # re-classifying the same report (~5 min of LLM calls, observed
    # running twice per loop before this).
    _bottleneck_cache[os.path.abspath(quality_report_path)] = out
    return out
  except Exception as e:
    logger.error("Bottleneck detection failed: %s", e, exc_info=True)
    return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool: run_coevolution
# ---------------------------------------------------------------------------


_rounds_run: dict[str, int] = {}  # EVOLUTION_MAX_ROUNDS guard (per process)


def _round_guard(key: str) -> dict | None:
  """Enforce EVOLUTION_MAX_ROUNDS (set by main.py from --rounds).

  The cap is BINDING: the orchestrating agent cannot add evolution
  rounds beyond it. Counted per ``key`` so one round over several agents
  is still one round each (``run_evolution[<skill_dir>]``,
  ``run_coevolution``). Returns the refusal dict for the tool to hand
  back, or None (and counts the round) when the call may proceed.
  """
  max_rounds = os.getenv("EVOLUTION_MAX_ROUNDS")
  done = _rounds_run.get(key, 0)
  if max_rounds and done >= int(max_rounds):
    logger.warning(
        "%s refused: EVOLUTION_MAX_ROUNDS=%s reached (%d round(s) already"
        " run)",
        key,
        max_rounds,
        done,
    )
    return {
        "status": "refused",
        "reason": (
            f"EVOLUTION_MAX_ROUNDS={max_rounds} reached"
            f" ({done} round(s) already run). Do NOT evolve further —"
            " publish the best result so far and open the PR."
        ),
    }
  _rounds_run[key] = done + 1
  return None


_bottleneck_cache: dict = {}  # report path -> classification (once per report)


def run_coevolution(
    quality_report_path: str,
    output_dir: str | None = None,
    model_id: str | None = None,
    max_workers: int = 10,
    agentic: bool = True,
) -> dict:
  """Run cross-agent co-evolution with automatic bottleneck detection.

  Detects which agent(s) need evolution and runs targeted evolution
  on the right agent(s). Agents are discovered from agent_registry.json.

  Args:
      quality_report_path: Path to quality report JSON file.
      output_dir: Directory to save evolved skills and logs.
      model_id: Gemini model for all LLM calls. Defaults to
          EVOLUTION_MODEL_ID.
      max_workers: Max parallel threads.
      agentic: Use agentic error analysts with tool access.

  Returns:
      Dict with bottleneck recommendation, evolved agents, and timing.
  """
  from . import coevolve as coevolve_mod

  model_id = model_id or config.get_config().evolution_model_id

  refused = _round_guard("run_coevolution")
  if refused:
    return refused

  if not os.path.isfile(quality_report_path):
    return {"error": f"Quality report not found: {quality_report_path}"}

  try:
    reg = registry.get_registry()
  except registry.RegistryError as exc:
    return {"status": "error", "error": str(exc)}

  try:
    result = coevolve_mod.coevolve(
        report_path=quality_report_path,
        output_dir=output_dir,
        model_id=model_id,
        max_workers=max_workers,
        agentic=agentic,
        select_by_score=True,
    )

    deployed = {}
    for agent_name, agent_result in result.evolved_agents.items():
      if "error" in agent_result:
        continue
      evolved_path = None
      if output_dir:
        evolved_path = os.path.join(
            output_dir, f"{agent_name}_evolved_skill.md"
        )
      if evolved_path and os.path.isfile(evolved_path):
        spec = reg.agents.get(agent_name)
        if spec:
          dst = os.path.join(spec.skill_dir, "SKILL.md")
          _safe_copy2(evolved_path, dst)
          deployed[agent_name] = dst
          logger.info("Deployed evolved %s skill to %s", agent_name, dst)

    return {
        "status": "success",
        "bottleneck_recommendation": result.bottleneck_recommendation,
        "bottleneck_summary": result.bottleneck_summary,
        "evolved_agents": result.evolved_agents,
        "deployed": deployed,
        "elapsed_seconds": result.elapsed_seconds,
        "summary": result.summary,
    }
  except Exception as e:
    logger.error("Co-evolution failed: %s", e, exc_info=True)
    return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: run_quality_report
# ---------------------------------------------------------------------------


def run_quality_report(
    output_path: str,
    time_period: str | None = None,
    app_name: str | None = None,
    run_dir: str | None = None,
) -> dict:
  """Build a quality report from the agent's BigQuery session data.

  Runs the SDK's ``scripts/quality_report.py`` (shipped next to the
  evolution engine) over the sessions the analytics plugin wrote to
  BigQuery, and saves the structured report the evolution pipeline
  consumes. This is the default, zero-plugin source of trajectories:
  no traffic generation is involved.

  Args:
      output_path: Where to write the report JSON. A relative path is
          placed inside ``run_dir`` when one is given.
      time_period: Session window (e.g. '24h', '7d', 'all'). Defaults
          to EVAL_TIME_PERIOD.
      app_name: Filter to one agent app name. Defaults to
          QUALITY_APP_NAME, then the registry's default_app_name.
      run_dir: Run directory for artifacts.

  Returns:
      Dict with report_path and a small summary (total sessions,
      meaningful rate), or {"error": ...} when the report could not be
      produced. Never raises.
  """
  cfg = config.get_config()

  try:
    scripts_dir = os.path.dirname(engine.engine_path())
  except FileNotFoundError as exc:
    return {"error": str(exc)}
  script = os.path.join(scripts_dir, "quality_report.py")
  if not os.path.isfile(script):
    return {
        "error": (
            f"quality_report.py not found next to the evolution engine in"
            f" {scripts_dir}. Ship the SDK's scripts/ directory with the"
            " job image (SDK_SCRIPTS_DIR)."
        )
    }

  if run_dir and not os.path.isabs(output_path):
    output_path = os.path.join(run_dir, output_path)
  output_path = os.path.abspath(output_path)
  os.makedirs(os.path.dirname(output_path), exist_ok=True)

  if app_name is None:
    app_name = cfg.quality_app_name
  if app_name is None:
    try:
      app_name = registry.get_registry().app_name_for()
    except registry.RegistryError:
      app_name = None

  cmd = [
      sys.executable,
      script,
      "--output-json",
      output_path,
      "--time-period",
      time_period or cfg.eval_time_period,
  ]
  if app_name:
    cmd.extend(["--app-name", app_name])
  else:
    logger.warning(
        "No app name resolved (QUALITY_APP_NAME unset and no"
        " default_app_name in the registry); the report covers every app"
        " in the dataset."
    )
  for label in (cfg.evolution_trace_labels or "").split(","):
    label = label.strip()
    if label:
      cmd.extend(["--label", label])

  logger.info("Quality report: %s", " ".join(cmd))
  t0 = time.time()
  try:
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,
        env={**os.environ},
    )
  except subprocess.TimeoutExpired:
    return {"error": "Quality report timed out (1h limit)"}
  except Exception as exc:  # noqa: BLE001 - a tool must never raise
    return {"error": _mask_tokens(str(exc))}

  if r.returncode != 0:
    stderr_tail = "\n".join(
        _mask_tokens(r.stderr or "").strip().split("\n")[-20:]
    )
    return {
        "error": (
            f"quality_report.py failed (exit {r.returncode}): {stderr_tail}"
        ),
        "elapsed_seconds": round(time.time() - t0, 1),
    }

  if not os.path.isfile(output_path):
    return {"error": "quality_report.py did not produce an output file"}

  result = {
      "report_path": output_path,
      "elapsed_seconds": round(time.time() - t0, 1),
  }
  try:
    with open(output_path) as f:
      summary = json.load(f).get("summary") or {}
  except (OSError, ValueError) as exc:
    result["warning"] = f"Report is not readable JSON: {exc}"
    return result
  result["total_sessions"] = summary.get("total_sessions")
  if summary.get("meaningful_rate") is not None:
    result["meaningful_rate"] = summary.get("meaningful_rate")
  twin = _identical_prior_report(output_path, summary)
  if twin:
    # Same session count and same meaningful count as an earlier report
    # in this run: the window returned the same sessions again. That
    # happens whenever the evolved skill is not yet deployed (it ships
    # via the PR), so this is not a measurement of the new version.
    result["warning"] = (
        f"Summary is identical to {os.path.basename(twin)} (same"
        " total_sessions and meaningful count): the time window returned"
        " the same sessions, so this report does NOT measure the evolved"
        " skill. Do not start another round on it; the winner's score is"
        " in evolved_score.json / compare_versions."
    )
    result["stale"] = True
  return result


def _identical_prior_report(output_path: str, summary: dict) -> str | None:
  """Earlier report in the same directory with an identical summary."""
  total = summary.get("total_sessions")
  meaningful = summary.get("meaningful")
  if total is None or meaningful is None:
    return None
  for path in sorted(
      glob.glob(
          os.path.join(os.path.dirname(output_path), "*quality_report.json")
      )
  ):
    if os.path.abspath(path) == os.path.abspath(output_path):
      continue
    if os.path.getmtime(path) > os.path.getmtime(output_path):
      continue
    try:
      with open(path) as f:
        prior = json.load(f).get("summary") or {}
    except (OSError, ValueError):
      continue
    if (
        prior.get("total_sessions") == total
        and prior.get("meaningful") == meaningful
    ):
      return path
  return None


# ---------------------------------------------------------------------------
# Tool: score_candidate
# ---------------------------------------------------------------------------


def score_candidate(
    candidate_path: str,
    skill_dir: str,
    run_dir: str,
) -> dict:
  """Score one evolution candidate through the host's score hook.

  Scoring a candidate means exercising a not-yet-published SKILL.md,
  which only the host can do (its agent, its eval set). The job
  therefore dispatches to the ``score`` hook (EVOLUTION_HOOKS module or
  SCORE_CMD). With no hook configured this returns ``{"skipped":
  reason}`` and candidate selection falls back to the engine's
  size-based heuristic.

  Args:
      candidate_path: Path to the candidate SKILL.md file.
      skill_dir: Agent skill directory (agent name shortcuts from
          agent_registry.json accepted).
      run_dir: Run directory for the hook's output files.

  Returns:
      The hook's result dict — must carry ``meaningful_rate`` — or
      {"skipped": reason} when no score hook is configured.
  """
  hook, reason = hooks.get_hook("score")
  if hook is None:
    logger.info("score_candidate skipped: %s", reason)
    return {"skipped": reason}

  skill_dir = _resolve_skill_dir(skill_dir)
  if not os.path.isfile(candidate_path):
    return {"error": f"Candidate not found: {candidate_path}"}
  if not os.path.isdir(skill_dir):
    return {"error": f"Skill directory not found: {skill_dir}"}
  os.makedirs(run_dir, exist_ok=True)

  try:
    result = hook(candidate_path, skill_dir, run_dir)
  except Exception as exc:  # noqa: BLE001 - a tool must never raise
    return {"status": "error", "error": _mask_tokens(str(exc))}

  if not isinstance(result, dict):
    return {"status": "error", "error": f"score hook returned {type(result)}"}
  for key in ("error", "stderr_tail", "output_tail"):
    if isinstance(result.get(key), str):
      result[key] = _mask_tokens(result[key])
  return result


# ---------------------------------------------------------------------------
# Tools: GCS archival
# ---------------------------------------------------------------------------


def upload_run_to_gcs(
    run_dir: str,
    bucket_name: str | None = None,
    prefix: str = "skill-evolution-runs",
) -> dict:
  """Upload a run directory to Google Cloud Storage.

  Uploads all files in the run directory to GCS for archival and
  sharing. Uses the run directory basename as the GCS subfolder.

  Controlled by the GCS_UPLOAD env var (default: false). When false,
  upload is skipped. EVOLUTION_GCS_BUCKET must be configured.

  Args:
      run_dir: Local path to the run directory.
      bucket_name: GCS bucket name. Defaults to the configured bucket.
      prefix: GCS path prefix (default: 'skill-evolution-runs').

  Returns:
      Dict with status, GCS URI, and number of files uploaded.
      Returns status="skipped" if GCS_UPLOAD is not enabled.
  """
  from . import gcs_utils

  return gcs_utils.upload_dir_to_gcs(
      run_dir, bucket_name=bucket_name, prefix=prefix
  )


def download_from_gcs(gcs_uri: str, local_path: str) -> dict:
  """Download a file from GCS to the local filesystem.

  Args:
      gcs_uri: GCS URI (gs://bucket/path/to/file).
      local_path: Local path to save the file.

  Returns:
      Dict with status and local_path.
  """
  from . import gcs_utils

  return gcs_utils.download_from_gcs(gcs_uri, local_path)


# ---------------------------------------------------------------------------
# Issue / PR body helpers
# ---------------------------------------------------------------------------


def _default_pr_body(
    agent: str,
    version: str,
    metrics: dict,
    evolved_size: int,
    run_dir: str,
    session_ids: list[str] | None = None,
) -> str:
  """Deterministic PR body built from the run's measured metrics."""
  m = metrics
  baseline_excl = m.get("baseline_excl", 0)
  evolved_excl = m.get("evolved_excl", 0)

  body = (
      f"## Skill Evolution: {agent} {version}\n\n"
      f"### Quality Before Evolution\n\n"
      f"| Metric | Value |\n"
      f"|--------|-------|\n"
      f"| Meaningful rate | {m['baseline_meaningful']}% |\n"
      f"| Unhelpful rate | {m['baseline_unhelpful']}% |\n\n"
      f"### Candidate Eval Scores\n\n"
      f"| Metric | Baseline ({m['baseline_label']}) "
      f"| Evolved ({version}) |\n"
      f"|--------|:------------:|:-------------------:|\n"
      f"| Meaningful rate | {m['baseline_meaningful']}% "
      f"| {m['evolved_meaningful']}% |\n"
      f"| Unhelpful rate | {m['baseline_unhelpful']}% "
      f"| {m['evolved_unhelpful']}% |\n"
      f"| Excluded error-shaped (infra) | {baseline_excl} |"
      f" {evolved_excl} |\n"
      f"| Skill size | | {evolved_size} chars |\n\n"
  )
  if baseline_excl > 0 or evolved_excl > 0:
    body += (
        f"**DENOMINATORS DIFFER**: the preflight excluded {baseline_excl}"
        f" baseline vs {evolved_excl} evolved error-shaped record(s), so"
        " the two rates cover different question subsets. Deltas"
        " suppressed.\n\n"
    )
  selector_path = os.path.join(run_dir, "trace_selector.json")
  if os.path.isfile(selector_path):
    try:
      with open(selector_path) as f:
        sel = json.load(f)
      labels = (
          ", ".join(f"{k}={v}" for k, v in (sel.get("labels") or {}).items())
          or "(none)"
      )
      body += (
          f"### Trace Selector (reproducibility)\n\n"
          f"Evolved from BigQuery traces where:"
          f" app=`{sel.get('app_name')}`,"
          f" agent_version=`{sel.get('agent_version') or 'any'}`,"
          f" labels: {labels}, window: {sel.get('time_period')}\n\n"
      )
    except Exception:  # noqa: BLE001
      pass
  if session_ids:
    body += f"### Failing Sessions ({len(session_ids)})\n\n"
    for sid in session_ids[:10]:
      body += f"- `{sid}`\n"
    if len(session_ids) > 10:
      body += f"- ... and {len(session_ids) - 10} more\n"
    body += "\n"
  body += f"Run: `{os.path.basename(run_dir)}`\n"
  return body


def _extract_rate(report_path: str, field: str) -> str:
  if not report_path or not os.path.isfile(report_path):
    return "?"
  try:
    with open(report_path) as f:
      data = json.load(f)
    val = data["summary"][field]
    return f"{round(val, 1)}"
  except Exception:  # noqa: BLE001
    return "?"


def _excluded_count(report_path: str) -> int:
  """Error-shaped records the scorer's preflight dropped from this report.

  A non-zero count means the report's rates use a shrunken denominator
  and cannot be compared against other reports by raw rate.
  """
  try:
    with open(report_path) as f:
      summary = json.load(f).get("summary") or {}
    return int((summary.get("excluded_error_shaped") or {}).get("count", 0))
  except Exception:  # noqa: BLE001
    return 0


def _find_evolved_skill(run_dir: str, version: str, agent: str) -> str | None:
  """Find the evolved skill MD file in the run directory."""
  candidates = [
      f"best_{version}_skill.md",
      f"{version}_{agent}_skill.md",
      f"{version}_skill.md",
  ]
  for name in candidates:
    path = os.path.join(run_dir, name)
    if os.path.isfile(path):
      return path
  return None


def _resolve_repo_skill_path(agent: str) -> str | None:
  """In-repo path of an agent's SKILL.md, relative to the repo root.

  Relative to the registry's ``repo_root`` inside the host-repo workdir
  — the directory ``create_evolution_pr`` commits in — so joining it
  back onto the workdir root can never escape the clone.
  """
  reg = registry.get_registry()
  spec = reg.agents.get(agent)
  if spec is None:
    return None
  return os.path.relpath(
      os.path.join(spec.skill_dir, "SKILL.md"), reg.repo_root
  )


def _authoritative_evolved_score(run_dir: str) -> float | None:
  """The deployed winner's selection score from ``evolved_score.json``.

  Written by ``evolve()`` on the same eval set the incumbent was
  measured on. None when the file is absent, marked unmeasurable, or
  carries no rate.
  """
  path = os.path.join(run_dir, "evolved_score.json")
  if not os.path.isfile(path):
    return None
  try:
    with open(path) as f:
      payload = json.load(f)
  except (OSError, ValueError):
    return None
  if payload.get("unmeasurable") or payload.get("meaningful_rate") is None:
    return None
  return float(payload["meaningful_rate"])


def _collect_quality_metrics(run_dir: str, version: str) -> dict:
  """Collect baseline and evolved quality metrics from report files.

  The evolved figure comes, in order of preference, from: the winner's
  own scored report (the candidate report whose rate matches
  ``evolved_score.json``); ``evolved_score.json`` alone; a
  ``{version}_quality_report.json`` / ``best_{version}_...`` re-report;
  the best candidate report. A post-round ``run_quality_report`` is
  ranked below the selection score on purpose: until the PR merges the
  evolved skill is not deployed, so that re-report usually re-measures
  the pre-evolution sessions (in the lab this produced a PR titled
  "23.1% -> 26.9%" for a winner that scored 100 on the eval set).
  """
  evolved_report = None
  evolved_source = "report"
  authoritative = _authoritative_evolved_score(run_dir)
  if authoritative is not None:
    evolved_source = "evolved_score.json"
    for pattern in (
        "candidate_*_report.json",
        "_score_candidate_*_report.json",
    ):
      for path in sorted(
          glob.glob(os.path.join(run_dir, "**", pattern), recursive=True)
      ):
        try:
          rate = float(_extract_rate(path, "meaningful_rate"))
        except ValueError:
          continue
        if abs(rate - authoritative) < 0.05 and not _excluded_count(path):
          evolved_report = path
          break
      if evolved_report:
        break

  if evolved_report is None and authoritative is None:
    for name in [
        f"{version}_quality_report.json",
        f"best_{version}_quality_report.json",
    ]:
      path = os.path.join(run_dir, name)
      if os.path.isfile(path):
        evolved_report = path
        break

  if evolved_report is None and authoritative is None:
    # Co-evolution runs score best-of-N candidates without writing a
    # {version}_quality_report.json — fall back to the best candidate
    # report so PR titles carry the real evolved rate instead of "?%".
    best_rate = -1.0
    best_any_rate, best_any = -1.0, None
    for pattern in (
        "candidate_*_report.json",
        "_score_candidate_*_report.json",
    ):
      for path in glob.glob(
          os.path.join(run_dir, "**", pattern), recursive=True
      ):
        try:
          rate = float(_extract_rate(path, "meaningful_rate"))
        except ValueError:
          continue
        if rate > best_any_rate:
          best_any_rate, best_any = rate, path
        if _excluded_count(path):
          logger.warning(
              "Skipping %s for the PR-title rate: preflight excluded"
              " records, denominator not comparable",
              path,
          )
          continue
        if rate > best_rate:
          best_rate, evolved_report = rate, path
    if evolved_report is None and best_any is not None:
      # Every candidate report lost records: use the best shrunken one
      # so its non-zero exclusion count reaches the metrics dict and the
      # denominators-differ machinery fires, instead of reporting 0
      # exclusions against a "?%" rate.
      logger.warning(
          "All candidate reports have preflight exclusions; using %s with"
          " its exclusion count surfaced",
          best_any,
      )
      evolved_report = best_any

  baseline_report = None
  baseline_label = "initial"
  baseline_candidates = sorted(
      glob.glob(os.path.join(run_dir, "*_quality_report.json"))
  )
  if baseline_candidates:
    baseline_report = baseline_candidates[0]
    baseline_label = os.path.basename(baseline_report).split("_quality_report")[
        0
    ]

  baseline_excl = _excluded_count(baseline_report) if baseline_report else 0
  evolved_excl = _excluded_count(evolved_report) if evolved_report else 0

  if evolved_report is not None:
    evolved_meaningful = _extract_rate(evolved_report, "meaningful_rate")
    evolved_unhelpful = _extract_rate(evolved_report, "unhelpful_rate")
  elif authoritative is not None:
    # Selection score without a matching report file (e.g. the score
    # hook keeps its reports elsewhere): rate is authoritative, the
    # unhelpful split is unknown.
    evolved_meaningful = f"{authoritative:.1f}"
    evolved_unhelpful = "?"
  else:
    evolved_meaningful = evolved_unhelpful = "?"

  return {
      "evolved_meaningful": evolved_meaningful,
      "evolved_unhelpful": evolved_unhelpful,
      "evolved_source": evolved_source,
      "baseline_meaningful": _extract_rate(baseline_report, "meaningful_rate"),
      "baseline_unhelpful": _extract_rate(baseline_report, "unhelpful_rate"),
      "baseline_label": baseline_label,
      "baseline_excl": baseline_excl,
      "evolved_excl": evolved_excl,
  }


def _gather_run_context(run_dir: str, version: str, agent: str) -> str:
  """Gather all available context from a run directory for issue bodies."""
  parts = []

  # Summary.json — version comparison table
  summary_path = os.path.join(run_dir, "summary.json")
  if os.path.isfile(summary_path):
    with open(summary_path) as f:
      summary = json.load(f)
    parts.append("## Version Comparison\n")
    parts.append(
        "| Version | Sessions | Meaningful | Unhelpful | Tool Usage |"
        " Specificity | First-Time-Right |"
    )
    parts.append(
        "|---------|----------|------------|-----------|------------|"
        "-------------|------------------|"
    )
    for v_label, v_data in summary.items():
      dims = v_data.get("dimension_averages", {})
      parts.append(
          f"| {v_label} | {v_data.get('total_sessions', '?')} "
          f"| {v_data.get('meaningful_rate', '?')}% "
          f"| {v_data.get('unhelpful_rate', '?')}% "
          f"| {dims.get('tool_usage', '?')} "
          f"| {dims.get('specificity', '?')} "
          f"| {dims.get('first_time_right', '?')} |"
      )
    parts.append("")

  # Pre-evolution skill (what we started from)
  pre_skill = os.path.join(run_dir, f"pre_evolution_{agent}_skill.md")
  if not os.path.isfile(pre_skill):
    pre_skill = os.path.join(run_dir, f"v0_{agent.split('_')[0]}_skill.md")
  if os.path.isfile(pre_skill):
    with open(pre_skill) as f:
      content = f.read()
    parts.append(f"## Pre-Evolution Skill ({len(content)} chars)\n")
    parts.append(f"```markdown\n{content[:2000]}\n```\n")

  # Evolved skill
  evolved_path = _find_evolved_skill(run_dir, version, agent)
  if evolved_path:
    with open(evolved_path) as f:
      content = f.read()
    parts.append(f"## Evolved Skill {version} ({len(content)} chars)\n")
    parts.append(f"```markdown\n{content[:3000]}\n```\n")

  # Candidate scores (if best-of-N was used)
  candidate_reports = sorted(
      glob.glob(os.path.join(run_dir, "candidate_*_report.json"))
  )
  if candidate_reports:
    parts.append("## Candidate Scores (Best-of-N)\n")
    parts.append("| Candidate | Meaningful | Unhelpful | Sessions |")
    parts.append("|-----------|------------|-----------|----------|")
    for rpath in candidate_reports:
      cname = os.path.basename(rpath).split("_report")[0]
      try:
        with open(rpath) as f:
          cdata = json.load(f)
        cs = cdata.get("summary", {})
        parts.append(
            f"| {cname} | {cs.get('meaningful_rate', '?')}% "
            f"| {cs.get('unhelpful_rate', '?')}% "
            f"| {cs.get('total_sessions', '?')} |"
        )
      except Exception:  # noqa: BLE001
        pass
    parts.append("")

  # Sample failures from baseline report
  baseline_reports = sorted(
      glob.glob(os.path.join(run_dir, "*_quality_report.json"))
  )
  if baseline_reports:
    try:
      with open(baseline_reports[0]) as f:
        report = json.load(f)
      sessions = report.get("sessions", [])
      failures = [
          s
          for s in sessions
          if s.get("metrics", {}).get("response_usefulness", {}).get("category")
          == "unhelpful"
      ][:5]
      if failures:
        parts.append("## Sample Failures (from baseline)\n")
        for s in failures:
          q = s.get("question", "?")
          reason = (
              s.get("metrics", {})
              .get("response_usefulness", {})
              .get("justification", "")[:200]
          )
          parts.append(f"- **Q:** {q}")
          parts.append(f"  **Why unhelpful:** {reason}\n")
    except Exception:  # noqa: BLE001
      pass

  return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool: parse_quality_issue
# ---------------------------------------------------------------------------


def parse_quality_issue(issue_number: int) -> dict:
  """Read a quality issue from GitHub and extract structured context.

  Parses the structured markdown body created by a quality agent,
  extracting the metadata table, root cause, failure patterns,
  affected sessions, and recommended fix.

  Args:
      issue_number: GitHub issue number to read.

  Returns:
      Dict with category, agent_name, topic, severity, root_cause,
      failure_patterns, recommendation, affected_questions,
      quality_summary, and the full issue body.
  """
  import re

  r = subprocess.run(
      [
          "gh",
          "issue",
          "view",
          str(issue_number),
          *_gh_repo_args(),
          "--json",
          "title,body,labels,state",
      ],
      cwd=config.workdir_or_none(),
      capture_output=True,
      text=True,
  )
  if r.returncode != 0:
    return {
        "error": (
            f"Failed to read issue #{issue_number}: {_mask_tokens(r.stderr)}"
        )
    }

  data = json.loads(r.stdout)
  body = data.get("body", "")
  labels = [l["name"] for l in data.get("labels", [])]
  title = data.get("title", "")

  result = {
      "issue_number": issue_number,
      "title": title,
      "state": data.get("state", ""),
      "labels": labels,
      "body": body,
  }

  # Parse metadata table: | Field | Value |
  metadata = {}
  for m in re.finditer(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|$", body, re.MULTILINE):
    key = m.group(1).strip().strip("*").lower()
    val = m.group(2).strip().strip("`")
    if key in ("field", "value") or key.startswith("-") or key.startswith(":"):
      continue
    metadata[key] = val

  result["category"] = metadata.get("category", "")
  result["agent_name"] = metadata.get("agent", "")
  result["topic"] = metadata.get("topic", "")
  result["severity"] = metadata.get("severity", "")
  result["action_needed"] = metadata.get("action needed", "")
  result["sessions_affected"] = metadata.get("sessions affected", "")
  result["meaningful_rate"] = metadata.get("meaningful rate", "")

  # Extract root cause section
  root_match = re.search(
      r"## Root Cause\s*\n+(.+?)(?=\n## |\Z)", body, re.DOTALL
  )
  result["root_cause"] = root_match.group(1).strip() if root_match else ""

  # Extract failure patterns table
  patterns = []
  pat_section = re.search(
      r"## Failure Patterns\s*\n+(.+?)(?=\n## |\Z)", body, re.DOTALL
  )
  if pat_section:
    for row in re.finditer(
        r"^\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*\|$",
        pat_section.group(1),
        re.MULTILINE,
    ):
      pat = row.group(1).strip()
      if pat.lower() in ("pattern", "---"):
        continue
      patterns.append(
          {
              "pattern": pat,
              "count": int(row.group(2)),
              "verdict": row.group(3).strip(),
              "example_question": row.group(4).strip(),
          }
      )
  result["failure_patterns"] = patterns

  # Extract recommended fix
  rec_match = re.search(
      r"## Recommended Fix\s*\n+(.+?)(?=\n## |\n---|\Z)", body, re.DOTALL
  )
  result["recommendation"] = rec_match.group(1).strip() if rec_match else ""

  # Extract affected session IDs and questions
  questions = []
  session_ids = []
  sessions_section = re.search(r"<details>.*?</details>", body, re.DOTALL)
  if sessions_section:
    for sid_match in re.finditer(
        r"### Session \d+: `(.+?)`", sessions_section.group(0)
    ):
      session_ids.append(sid_match.group(1))
    for q_match in re.finditer(
        r"\*\*Question:\*\*\s*(.+)", sessions_section.group(0)
    ):
      questions.append(q_match.group(1).strip())
  result["session_ids"] = session_ids
  result["affected_questions"] = questions

  # Extract reproduce commands
  reproduce_match = re.search(
      r"## Reproduce\s*\n+```(?:bash)?\s*\n(.+?)```", body, re.DOTALL
  )
  if reproduce_match:
    cmds = reproduce_match.group(1).strip().split("\n")
    smoke_questions = []
    for cmd in cmds:
      q_in_cmd = re.search(r'"(.+?)"', cmd)
      if q_in_cmd:
        smoke_questions.append(q_in_cmd.group(1))
    if smoke_questions:
      result["affected_questions"] = list(
          set(result["affected_questions"] + smoke_questions)
      )

  # Quality report summary
  summary = {}
  sum_section = re.search(
      r"## Quality Report Summary\s*\n+(.+?)(?=\n---|\n\*|\Z)",
      body,
      re.DOTALL,
  )
  if sum_section:
    for row in re.finditer(
        r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|$", sum_section.group(1), re.MULTILINE
    ):
      k = row.group(1).strip().lower()
      v = row.group(2).strip().strip("`")
      if k not in ("metric", "value") and not k.startswith("-"):
        summary[k] = v
  result["quality_summary"] = summary

  return result


# ---------------------------------------------------------------------------
# Tool: create_evolution_issue
# ---------------------------------------------------------------------------


def create_evolution_issue(
    run_dir: str,
    version: str = "v1",
    agent: str | None = None,
    dry_run: bool = False,
) -> dict:
  """Create a GitHub issue documenting the skill evolution run.

  Gathers quality metrics, version comparisons, candidate scores, and
  sample failures from the run directory into a structured issue body.

  Publishing is gated on EVOLUTION_PUBLISH: when it is false (the
  default) the issue is written to the run directory instead of being
  created on GitHub.

  Args:
      run_dir: Path to the run directory with evolution artifacts.
      version: Evolved skill version label (e.g. 'v1', 'v2').
      agent: Agent that was evolved (name from agent_registry.json).
          Defaults to the registry's first agent.
      dry_run: If True, write issue to local file instead of GitHub.

  Returns:
      Dict with status, issue URL/number, or dry_run file path.
  """
  cfg = config.get_config()
  publish_disabled = False
  if not cfg.evolution_publish and not dry_run:
    logger.info(
        "EVOLUTION_PUBLISH is false — writing a local issue preview"
        " instead of creating it on GitHub."
    )
    dry_run = True
    publish_disabled = True

  agent, error = _resolve_agent(agent)
  if error:
    return error
  run_dir = os.path.abspath(run_dir)
  if not os.path.isdir(run_dir):
    return {"error": f"Run directory not found: {run_dir}"}

  metrics = _collect_quality_metrics(run_dir, version)
  run_context = _gather_run_context(run_dir, version, agent)

  evolved_path = _find_evolved_skill(run_dir, version, agent)
  evolved_size = 0
  if evolved_path:
    evolved_size = os.path.getsize(evolved_path)

  meaningful = metrics["evolved_meaningful"]
  baseline = metrics["baseline_meaningful"]
  baseline_excl = metrics.get("baseline_excl", 0)
  evolved_excl = metrics.get("evolved_excl", 0)

  if baseline_excl > 0 or evolved_excl > 0:
    title = (
        f"[Evolution] {agent} skill {version} — "
        f"meaningful {baseline}% → {meaningful}% [denominators differ]"
    )
  else:
    title = (
        f"[Evolution] {agent} skill {version} — "
        f"meaningful {baseline}% → {meaningful}%"
    )

  m = metrics
  issue_body = (
      f"## Metadata\n\n"
      f"| Field | Value |\n"
      f"|-------|-------|\n"
      f"| Agent | `{agent}` |\n"
      f"| Version | `{version}` |\n"
      f"| Meaningful rate | {m['baseline_meaningful']}% → "
      f"{m['evolved_meaningful']}% |\n"
      f"| Unhelpful rate | {m['baseline_unhelpful']}% → "
      f"{m['evolved_unhelpful']}% |\n"
      f"| Baseline Exclusions | {baseline_excl} |\n"
      f"| Evolved Exclusions | {evolved_excl} |\n"
      f"| Skill size | {evolved_size} chars |\n"
      f"| Run | `{os.path.basename(run_dir)}` |\n\n"
  )
  if baseline_excl > 0 or evolved_excl > 0:
    issue_body += (
        f"**DENOMINATORS DIFFER**: the preflight excluded {baseline_excl}"
        f" baseline vs {evolved_excl} evolved error-shaped record(s), so"
        " the two rates cover different question subsets. Deltas"
        " suppressed.\n\n"
    )
  issue_body += f"## Run Context\n\n{run_context}\n"

  labels = ["evolution", agent]

  if dry_run:
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"issue_evolution_{agent}_{version}_{ts}.md"
    filepath = os.path.join(run_dir, filename)
    with open(filepath, "w") as f:
      f.write(f"# {title}\n\n")
      f.write(f"**Labels:** {', '.join(labels)}\n\n")
      f.write(issue_body + "\n")
    result = {"status": "dry_run", "file": filepath, "title": title}
    if publish_disabled:
      result["reason"] = (
          "EVOLUTION_PUBLISH is false — local preview only; nothing was"
          " sent to GitHub."
      )
    return result

  # Create issue via gh CLI
  try:
    label_args = []
    for label in labels:
      label_args.extend(["--label", label])

    r = subprocess.run(
        [
            "gh",
            "issue",
            "create",
            *_gh_repo_args(),
            "--title",
            title,
            "--body",
            issue_body,
            *label_args,
        ],
        cwd=config.workdir_or_none(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
      # Labels may not exist yet — retry without labels
      logger.warning(
          "gh issue create failed with labels: %s", _mask_tokens(r.stderr)
      )
      r = subprocess.run(
          [
              "gh",
              "issue",
              "create",
              *_gh_repo_args(),
              "--title",
              title,
              "--body",
              issue_body,
          ],
          cwd=config.workdir_or_none(),
          capture_output=True,
          text=True,
      )
    if r.returncode != 0:
      return {"status": "error", "error": _mask_tokens(r.stderr)}

    issue_url = r.stdout.strip()
    # Extract issue number from URL
    issue_number = issue_url.rstrip("/").split("/")[-1]
    logger.info("Created issue #%s: %s", issue_number, issue_url)

    # Save issue as .md in run directory
    md_path = os.path.join(run_dir, f"issue_{issue_number}_evolution.md")
    with open(md_path, "w") as f:
      f.write(f"# {title}\n\n")
      f.write(f"**Issue:** {issue_url}\n")
      f.write(f"**Labels:** {', '.join(labels)}\n\n")
      f.write(issue_body + "\n")
    logger.info("Saved issue .md to %s", md_path)

    return {
        "status": "created",
        "url": issue_url,
        "number": int(issue_number),
        "title": title,
        "md_path": md_path,
    }

  except Exception as e:
    logger.error("Failed to create issue: %s", e)
    return {"status": "error", "error": _mask_tokens(str(e))}


# ---------------------------------------------------------------------------
# Git workdir
# ---------------------------------------------------------------------------


def _ensure_git_workdir() -> str:
  """Return the git workdir used for branching, committing, and PRs.

  Exactly one clone of the host agent repo exists per process and
  :mod:`skill_evolution_job.config` owns it (including the GH_TOKEN-in-URL
  masking discipline at the clone site). Evolution edits skills inside
  that clone, so the evolved SKILL.md is always in the directory that
  gets committed.
  """
  return config.workdir()


# ---------------------------------------------------------------------------
# Tool: create_evolution_pr
# ---------------------------------------------------------------------------


def create_evolution_pr(
    run_dir: str,
    version: str = "v1",
    agent: str | None = None,
    base_branch: str | None = None,
    dry_run: bool = False,
    output_file: str | None = None,
    issue_number: int | None = None,
    session_ids: list[str] | None = None,
) -> dict:
  """Create a GitHub PR with the evolved skill.

  Uses deterministic git commands for branch/commit/push (only the
  SKILL.md file), then ``gh pr create`` for the PR. The PR body is a
  metrics table built from the run's reports.

  Before opening a real PR the ``gate`` hook decides whether the winner
  may be published. With no gate hook configured the behavior follows
  GATE_POLICY: ``skip`` (default) continues, ``require`` fails.

  When ``output_file`` is set, writes the evolved skill to that path
  instead of creating a PR.

  Args:
      run_dir: Path to the run directory containing evolved skills
          and quality reports.
      version: Skill version label (e.g. 'v1', 'v2').
      agent: Agent to create PR for (name from agent_registry.json).
      base_branch: Base branch for the PR. Env: GITHUB_BASE_BRANCH.
      dry_run: If True, preview branch/title/body without creating.
      output_file: If set, write the evolved skill to this path and
          skip PR creation entirely.
      issue_number: If set, append 'Fixes #N' to the PR body to
          auto-close the linked issue on merge.
      session_ids: Failing session IDs from the quality issue, included
          in the PR body for traceability.

  Returns:
      Dict with status, PR URL, and metrics.
  """
  cfg = config.get_config()
  base_branch = base_branch or cfg.github_base_branch

  publish_disabled = False
  if not cfg.evolution_publish and not dry_run and not output_file:
    logger.info(
        "EVOLUTION_PUBLISH is false — producing a local PR preview"
        " instead of pushing a branch and opening a PR."
    )
    dry_run = True
    publish_disabled = True

  if not dry_run and not output_file:
    gate_agent, error = _resolve_agent(agent)
    if error:
      return error
    gate_hook, gate_reason = hooks.get_hook("gate")
    if gate_hook is not None:
      passed, detail = gate_hook(run_dir, version, gate_agent)
      if passed is False:
        return {
            "status": "refused_by_gate",
            "reason": (
                "Winner failed the publish gate — PR NOT opened"
                f" ({detail}). Fix the skill or run another round; the"
                " refusal stays in this log."
            ),
        }
    elif cfg.gate_policy == "require":
      return {
          "status": "error",
          "error": (
              "ERROR: GATE_POLICY=require but no gate hook is configured"
              " (set EVOLUTION_HOOKS or GATE_CMD)"
          ),
      }
    else:
      logger.info(
          "Publish gate skipped (%s); GATE_POLICY=%s — continuing to PR"
          " creation.",
          gate_reason,
          cfg.gate_policy,
      )

  agent, error = _resolve_agent(agent)
  if error:
    return error
  run_dir = os.path.abspath(run_dir)
  if not os.path.isdir(run_dir):
    return {"error": f"Run directory not found: {run_dir}"}

  # --- Locate evolved skill ---
  evolved_skill_path = _find_evolved_skill(run_dir, version, agent)
  if not evolved_skill_path:
    return {"error": f"No evolved skill found in {run_dir} for {version}"}

  with open(evolved_skill_path) as f:
    evolved_content = f.read()

  # --- Resolve skill path in the repo ---
  try:
    repo_skill_path = _resolve_repo_skill_path(agent)
  except registry.RegistryError as exc:
    return {"status": "error", "error": str(exc)}
  if not repo_skill_path:
    return {"error": f"Agent {agent!r} not found in agent registry."}

  # --- Output-file mode: write locally and return ---
  if output_file:
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w") as f:
      f.write(evolved_content)
    return {
        "status": "written",
        "output_file": output_file,
        "size": len(evolved_content),
    }

  # --- Collect quality metrics ---
  metrics = _collect_quality_metrics(run_dir, version)
  evolved_size = len(evolved_content)
  timestamp = time.strftime("%Y%m%d-%H%M%S")
  branch_name = f"skill-evolution/{agent}-{version}-{timestamp}"

  baseline_excl = metrics.get("baseline_excl", 0)
  evolved_excl = metrics.get("evolved_excl", 0)

  if baseline_excl > 0 or evolved_excl > 0:
    title = (
        f"Evolve {agent} skill to {version} "
        f"({metrics['baseline_meaningful']}% "
        f"-> {metrics['evolved_meaningful']}%) [denominators differ]"
    )
  else:
    title = (
        f"Evolve {agent} skill to {version} "
        f"({metrics['baseline_meaningful']}% "
        f"-> {metrics['evolved_meaningful']}%)"
    )

  if dry_run:
    body = _default_pr_body(
        agent, version, metrics, evolved_size, run_dir, session_ids
    )
    result = {
        "status": "dry_run",
        "title": title,
        "branch": branch_name,
        "base_branch": base_branch,
        "repo_skill_path": repo_skill_path,
        "evolved_skill_size": evolved_size,
        "metrics": metrics,
        "body_preview": body,
    }
    preview_path = os.path.join(run_dir, "pr_preview.md")
    with open(preview_path, "w") as f:
      f.write(f"# {title}\n\n{body}\n")
    result["preview_path"] = preview_path
    if publish_disabled:
      result["reason"] = (
          "EVOLUTION_PUBLISH is false — local preview only; no branch was"
          " pushed and no PR was opened."
      )
    return result

  # --- Resolve the git workdir (the single clone of the host repo) ---
  try:
    git_root = _ensure_git_workdir()
  except RuntimeError as e:
    return {"status": "error", "error": _mask_tokens(str(e))}

  # The registry's repo_root lives inside this clone, so the relative
  # skill path must stay inside it — reject anything that climbs out
  # via '..' rather than writing outside the checkout.
  git_root = os.path.abspath(git_root)
  abs_skill_path = os.path.abspath(os.path.join(git_root, repo_skill_path))
  if os.path.commonpath([git_root, abs_skill_path]) != git_root:
    return {
        "status": "error",
        "error": (
            f"Refusing to commit {repo_skill_path}: it resolves outside the"
            f" git workdir {git_root}. Check 'repo_root' and 'skill_dir' in"
            " the agent registry."
        ),
    }

  # --- Remember current branch to restore later ---
  try:
    original_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=git_root,
        capture_output=True,
        text=True,
    ).stdout.strip()
  except Exception:  # noqa: BLE001
    original_branch = base_branch

  # --- Stash uncommitted changes ---
  stashed = False
  dirty = (
      subprocess.run(["git", "diff", "--quiet"], cwd=git_root).returncode != 0
  )
  cached = (
      subprocess.run(
          ["git", "diff", "--cached", "--quiet"], cwd=git_root
      ).returncode
      != 0
  )
  if dirty or cached:
    subprocess.run(
        ["git", "stash", "push", "-m", "create_evolution_pr: temp stash"],
        cwd=git_root,
        capture_output=True,
    )
    stashed = True

  try:
    # --- Create branch from base ---
    r = subprocess.run(
        ["git", "checkout", "-b", branch_name, f"origin/{base_branch}"],
        cwd=git_root,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
      return {
          "status": "error",
          "error": f"Branch creation failed: {_mask_tokens(r.stderr)}",
      }

    # --- Copy evolved skill and commit ---
    os.makedirs(os.path.dirname(abs_skill_path), exist_ok=True)
    with open(abs_skill_path, "w") as f:
      f.write(evolved_content)

    commit_msg = (
        f"Evolve {agent} skill to {version}\n\n"
        f"Meaningful rate: {metrics['baseline_meaningful']}% "
        f"-> {metrics['evolved_meaningful']}%"
    )
    if baseline_excl > 0 or evolved_excl > 0:
      commit_msg += " [denominators differ]"
    commit_msg += f"\nRun: {os.path.basename(run_dir)}"

    for step, cmd in (
        ("git add", ["git", "add", repo_skill_path]),
        ("git commit", ["git", "commit", "-m", commit_msg]),
    ):
      r = subprocess.run(cmd, cwd=git_root, capture_output=True, text=True)
      if r.returncode != 0:
        # An unchecked failure here pushed an unchanged branch and opened
        # an empty PR.
        return {
            "status": "error",
            "error": (
                f"{step} failed (exit {r.returncode}):"
                f" {_mask_tokens(r.stderr or r.stdout)}"
            ),
        }

    # --- Push ---
    r = subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        cwd=git_root,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
      return {
          "status": "error",
          "error": f"Push failed: {_mask_tokens(r.stderr)}",
      }

    body = _default_pr_body(
        agent, version, metrics, evolved_size, run_dir, session_ids
    )

    # --- Link to issue if provided ---
    if issue_number:
      body += f"\n\nFixes #{issue_number}"

    # --- Create PR via gh CLI ---
    r = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            *_gh_repo_args(),
            "--base",
            base_branch,
            "--head",
            branch_name,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=git_root,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
      return {
          "status": "error",
          "error": f"gh pr create failed: {_mask_tokens(r.stderr)}",
      }

    pr_url = r.stdout.strip()
    pr_number = pr_url.rstrip("/").split("/")[-1]
    logger.info("Created PR #%s: %s", pr_number, pr_url)

    # Save PR as .md in run directory
    md_path = os.path.join(run_dir, f"pr_{pr_number}_evolution.md")
    with open(md_path, "w") as f:
      f.write(f"# {title}\n\n")
      f.write(f"**PR:** {pr_url}\n")
      f.write(f"**Branch:** `{branch_name}`\n\n")
      f.write(body + "\n")
    logger.info("Saved PR .md to %s", md_path)

    result = {
        "status": "success",
        "pr_url": pr_url,
        "branch": branch_name,
        "meaningful_rate": metrics["evolved_meaningful"],
        "md_path": md_path,
    }

    # --- Publish hook: push the winning skill to a host registry ---
    # Best-effort: the PR already exists, so a hook failure is recorded
    # in the result rather than turning the call into an error. Runs
    # while the evolution branch is still checked out, so skill_dir
    # holds the evolved SKILL.md.
    publish_hook, publish_reason = hooks.get_hook("publish")
    if publish_hook is not None:
      try:
        result["publish_hook"] = publish_hook(
            os.path.dirname(abs_skill_path), run_dir
        )
      except Exception as exc:  # noqa: BLE001 - PR stands either way
        logger.error("Publish hook failed: %s", exc)
        result["publish_hook"] = {
            "status": "error",
            "error": _mask_tokens(str(exc)),
        }
    else:
      logger.info("Publish hook skipped (%s).", publish_reason)

    return result

  except Exception as e:
    logger.error("Failed to create PR: %s", e)
    return {"status": "error", "error": _mask_tokens(str(e))}

  finally:
    # --- Restore original branch and stash ---
    subprocess.run(
        ["git", "checkout", original_branch],
        cwd=git_root,
        capture_output=True,
    )
    if stashed:
      subprocess.run(["git", "stash", "pop"], cwd=git_root, capture_output=True)


# ---------------------------------------------------------------------------
# Tool: snapshot_skills
# ---------------------------------------------------------------------------


def snapshot_skills(label: str, run_dir: str) -> dict:
  """Save current agent skills to a run directory for later comparison.

  Copies the current SKILL.md files for all registered agents
  into the run directory with the given version label.

  Args:
      label: Version label (e.g. 'v0', 'v1', 'v2').
      run_dir: Path to the run directory.

  Returns:
      Dict with status and paths of saved snapshots.
  """
  try:
    agents = registry.get_registry().agents
  except registry.RegistryError as exc:
    return {"status": "error", "error": str(exc)}

  os.makedirs(run_dir, exist_ok=True)
  saved = {}
  for name, spec in agents.items():
    src = os.path.join(spec.skill_dir, "SKILL.md")
    if os.path.isfile(src):
      dst = os.path.join(run_dir, f"{label}_{name}_skill.md")
      shutil.copy2(src, dst)
      saved[name] = dst

  return {"status": "success", "label": label, "saved": saved}


# ---------------------------------------------------------------------------
# Tool: restore_skills
# ---------------------------------------------------------------------------


def restore_skills(label: str, run_dir: str) -> dict:
  """Restore agent skills from a previous snapshot in the run directory.

  Copies saved SKILL.md files from the run directory back to the
  agent skill directories.

  Args:
      label: Version label to restore (e.g. 'v0', 'v1').
      run_dir: Path to the run directory containing snapshots.

  Returns:
      Dict with status and paths of restored skills.
  """
  try:
    agents = registry.get_registry().agents
  except registry.RegistryError as exc:
    return {"status": "error", "error": str(exc)}

  restored = {}
  for name, spec in agents.items():
    src = os.path.join(run_dir, f"{label}_{name}_skill.md")
    dst = os.path.join(spec.skill_dir, "SKILL.md")
    if os.path.isfile(src):
      _safe_copy2(src, dst)
      restored[name] = dst

  if not restored:
    return {
        "status": "error",
        "error": f"No snapshots found for label '{label}' in {run_dir}",
    }

  return {"status": "success", "label": label, "restored": restored}


# ---------------------------------------------------------------------------
# Tool: count_failures
# ---------------------------------------------------------------------------


def count_failures(quality_report_path: str) -> dict:
  """Count failures in a quality report for the evolution gate.

  Returns the number of non-meaningful sessions (total - meaningful).
  Use this to decide whether there is enough failure signal to
  justify running evolution. Default threshold: 30 failures.

  Args:
      quality_report_path: Path to quality report JSON file.

  Returns:
      Dict with total sessions, meaningful count, failure count,
      and whether the threshold is met.
  """
  if not os.path.isfile(quality_report_path):
    return {"error": f"Quality report not found: {quality_report_path}"}

  with open(quality_report_path) as f:
    report = json.load(f)

  summary = report.get("summary", {})
  total = summary.get("total_sessions", 0)
  meaningful = summary.get("meaningful", 0)
  failures = total - meaningful
  min_failures = int(os.getenv("MIN_FAILURES", "30"))

  return {
      "total_sessions": total,
      "meaningful": meaningful,
      "failures": failures,
      "min_failures_threshold": min_failures,
      "should_evolve": failures >= min_failures,
      "meaningful_rate": summary.get("meaningful_rate"),
  }


# ---------------------------------------------------------------------------
# Tool: read_skill
# ---------------------------------------------------------------------------


def read_skill(agent_name: str) -> dict:
  """Read the current SKILL.md content for an agent.

  Use this to review an evolved skill before running traffic.
  Check for: appropriate size (8-15KB), keyword mappings table,
  anti-patterns section, no excessive repetition.

  Args:
      agent_name: Agent name from agent_registry.json.

  Returns:
      Dict with skill content, size, and version metadata.
  """
  try:
    reg = registry.get_registry()
  except registry.RegistryError as exc:
    return {"error": str(exc)}
  if agent_name not in reg.agents:
    available = ", ".join(sorted(reg.agents))
    return {"error": f"Unknown agent: {agent_name}. Available: {available}"}
  skill_dir = reg.agents[agent_name].skill_dir

  skill_path = os.path.join(skill_dir, "SKILL.md")
  if not os.path.isfile(skill_path):
    return {"error": f"SKILL.md not found at {skill_path}"}

  with open(skill_path) as f:
    content = f.read()

  # Extract version from YAML frontmatter, prefix with 'v' if needed
  version = "unknown"
  if content.startswith("---"):
    import re

    m = re.search(r'version:\s*["\']?(\S+)', content)
    if m:
      version = m.group(1).strip("\"'")
      if not version.startswith("v"):
        version = f"v{version}"

  # Count sections
  sections = [line for line in content.split("\n") if line.startswith("## ")]

  return {
      "agent": agent_name,
      "version": version,
      "size_chars": len(content),
      "size_kb": round(len(content) / 1024, 1),
      "sections": [s.strip("# ").strip() for s in sections],
      "content": content,
  }


# ---------------------------------------------------------------------------
# Tool: list_agents
# ---------------------------------------------------------------------------


def list_agents() -> dict:
  """List all registered agents available for evolution.

  Returns agent names, skill directories, and labels from the
  agent registry. Use this to discover which agents can be evolved.

  Returns:
      Dict mapping agent name to skill_dir and label.
  """
  try:
    reg = registry.get_registry()
  except registry.RegistryError as exc:
    return {"error": str(exc)}
  return {
      name: {"skill_dir": spec.skill_dir, "label": spec.label}
      for name, spec in reg.agents.items()
  }


# ---------------------------------------------------------------------------
# Tool: compare_versions
# ---------------------------------------------------------------------------


def compare_versions(run_dir: str) -> dict:
  """Generate a comparison table of all scored versions in a run.

  Scans the run directory for quality reports (*_quality_report.json,
  v1_quality_report.json, etc.) and produces a summary table.

  Args:
      run_dir: Path to the run directory.

  Returns:
      Dict with comparison table and per-version metrics.
  """
  if not os.path.isdir(run_dir):
    return {"error": f"Run directory not found: {run_dir}"}

  reports = sorted(glob.glob(os.path.join(run_dir, "*_quality_report.json")))
  if not reports:
    return {"error": "No quality reports found in run directory"}

  versions = []
  for report_path in reports:
    fname = os.path.basename(report_path)
    label = fname.split("_quality_report")[0]
    with open(report_path) as f:
      report = json.load(f)
    summary = report.get("summary", {})
    versions.append(
        {
            "version": label,
            "total_sessions": summary.get("total_sessions", 0),
            "meaningful": summary.get("meaningful", 0),
            "meaningful_rate": summary.get("meaningful_rate", 0),
            "unhelpful": summary.get("unhelpful", 0),
            "unhelpful_rate": summary.get("unhelpful_rate", 0),
            # Post-preflight denominator marker: rates from shrunken reports
            # must not feed deltas or the best-version pick as if they
            # shared a question set.
            "excluded": (summary.get("excluded_error_shaped") or {}).get(
                "count", 0
            ),
        }
    )

  # Authoritative evolved-skill score recorded by the incumbent-guarded
  # selection (scored on the SAME eval set as v0). Prefer this over noisy
  # re-scored *_quality_report.json files, which compare_versions cannot
  # even see for the deployed skill (it is saved as *_report.json, not
  # *_quality_report.json).
  evolved_score_path = os.path.join(run_dir, "evolved_score.json")
  if os.path.isfile(evolved_score_path):
    try:
      with open(evolved_score_path) as f:
        es = json.load(f)
      if not any(v["version"] == "evolved" for v in versions):
        # An unmeasurable winner records a null score; publish it as
        # unmeasured — never as 0% on a session count it never had.
        unmeasurable = bool(es.get("unmeasurable"))
        versions.append(
            {
                "version": "evolved",
                "total_sessions": (
                    0
                    if unmeasurable
                    else (versions[0]["total_sessions"] if versions else 0)
                ),
                "meaningful": 0,
                "meaningful_rate": (
                    None if unmeasurable else es.get("meaningful_rate", 0)
                ),
                "unhelpful": 0,
                "unhelpful_rate": 0,
                "excluded": 0,
                "unmeasurable": unmeasurable,
            }
        )
    except Exception:  # noqa: BLE001
      pass

  # Compute deltas — only between measured reports with full
  # denominators; a delta across a preflight-shrunken or unmeasurable
  # report is not a measurement.
  def _uncomparable(v):
    return (
        v.get("excluded")
        or v.get("unmeasurable")
        or v["meaningful_rate"] is None
    )

  for i, v in enumerate(versions):
    if i == 0 or _uncomparable(v) or _uncomparable(versions[i - 1]):
      v["delta"] = None
    else:
      prev_rate = versions[i - 1]["meaningful_rate"] or 0
      curr_rate = v["meaningful_rate"] or 0
      v["delta"] = round(curr_rate - prev_rate, 1)

  # Find peak version (excluding v0 baseline); clean measured
  # denominators first, shrunken reports only as a marked fallback,
  # never an unmeasurable row.
  evolved = [v for v in versions if v["version"] != "v0"]
  measured = [
      v
      for v in evolved
      if v["meaningful_rate"] is not None and not v.get("unmeasurable")
  ]
  clean_evolved = [v for v in measured if not v.get("excluded")]
  if clean_evolved:
    best = max(clean_evolved, key=lambda v: v["meaningful_rate"])
  elif measured:
    best = max(measured, key=lambda v: v["meaningful_rate"])
    logger.warning(
        "compare_versions: every evolved report has preflight exclusions;"
        " best_version %s is on a shrunken denominator",
        best["version"],
    )
  else:
    best = versions[0]

  # Build text table
  header = "| Version | Sessions | Meaningful Rate | Delta | Best |"
  sep = "|---------|----------|-----------------|-------|------|"
  rows = [header, sep]
  for v in versions:
    # A measured 0.0pp must stay distinguishable from a suppressed delta.
    if v["delta"] is None:
      delta = "-"
    elif v["delta"] > 0:
      delta = f"+{v['delta']}pp"
    else:
      delta = f"{v['delta']}pp"
    marker = " <-- " if v["version"] == best["version"] else ""
    if v.get("unmeasurable"):
      sessions, rate = "-", "n/a (unmeasurable)"
    else:
      sessions = str(v["total_sessions"])
      if v.get("excluded"):
        sessions += f" ({v['excluded']} excluded)"
      rate = f"{v['meaningful_rate']}%"
    rows.append(
        f"| {v['version']:<7} | {sessions:>8} | "
        f"{rate:>13} | {delta:>5} | {marker:>4} |"
    )

  return {
      "status": "success",
      "versions": versions,
      "best_version": best["version"],
      "best_meaningful_rate": best["meaningful_rate"],
      "table": "\n".join(rows),
  }
