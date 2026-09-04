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

"""Cross-agent co-evolution orchestrator.

Uses bottleneck detection to identify which agent(s) to evolve, then
runs targeted evolution on the right agent(s). Prevents wasted cycles
evolving the wrong agent.

Library module — invoked via tools.py (ADK agent) or main.py (CLI).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import json
import logging
import os
import time
from types import SimpleNamespace

from . import evolve as evolve_module
from . import hooks
from . import registry
from .bottleneck import detect_bottleneck
from .evolve import evolve
from .evolve import validate_evolved_skill

logger = logging.getLogger(__name__)


def _default_agent_configs() -> dict:
  """Agent name -> ``{skill_dir, label}`` from the agent registry.

  Ordered by the registry's evolution order. Lazy: a missing or
  malformed registry yields an empty mapping here (the caller logs and
  evolves nothing) rather than failing at import time.
  """
  try:
    return registry.agents_summary()
  except Exception as e:  # noqa: BLE001
    logger.warning("Could not load the agent registry: %s", e)
    return {}


def _excluded_count(report_path: str) -> int:
  """Error-shaped records a scorer's preflight dropped from this report.

  A non-zero count means the report's rates use a shrunken denominator
  and cannot be compared against other reports by raw rate. Delegates to
  tools.py, imported lazily because tools.py imports this module.
  """
  from . import tools

  return tools._excluded_count(report_path)


@dataclass
class CoevolutionResult:
  """Result of a co-evolution run."""

  bottleneck_recommendation: str = ""
  bottleneck_summary: str = ""
  evolved_agents: dict = field(default_factory=dict)
  elapsed_seconds: float = 0.0

  @property
  def summary(self) -> str:
    agents = ", ".join(self.evolved_agents.keys()) or "none"
    return (
        f"Bottleneck: {self.bottleneck_recommendation}. "
        f"Evolved: {agents}. "
        f"Time: {self.elapsed_seconds:.1f}s"
    )


def _failure_count(v) -> int:
  """Failure tally that accepts both bottleneck shapes.

  Live ``detect_bottleneck()`` returns failure LISTS; the target-bound
  and precomputed paths carry plain COUNTS. ``len()`` on the int paths
  crashed the summary AFTER skills were already deployed, making the
  tool report failure while the evolved skills sat live.
  """
  return v if isinstance(v, int) else len(v)


def _evolution_order(agent_names: list[str]) -> list[str]:
  """Sort agents into the registry's evolution order.

  The routing agent goes first so the agents it delegates to are scored
  against the already-improved routing (sequential co-evolution, as the
  design intends). That order is the registry's ``order`` field, which
  ``EVOLUTION_ORDER`` overrides. Agents absent from the registry keep
  their insertion order after the registered ones.
  """
  try:
    ranked = registry.get_registry().ordered_names()
  except Exception as e:  # noqa: BLE001
    logger.warning("Cannot read the evolution order from the registry: %s", e)
    return agent_names
  rank = {name: index for index, name in enumerate(ranked)}
  return sorted(agent_names, key=lambda n: rank.get(n, len(rank)))


def _refresh_incumbent(
    output_dir: str | None, current: float | None
) -> float | None:
  """Return the latest deployed score from ``<output_dir>/evolved_score.json``.

  ``evolve()`` records the deployed outcome's score there ("last writer
  wins" by design). Falls back to ``current`` when the file is absent,
  unreadable, or carries no rate — the bar never silently drops to None.
  """
  if not output_dir:
    return current
  path = os.path.join(output_dir, "evolved_score.json")
  try:
    with open(path) as f:
      score = json.load(f).get("meaningful_rate")
    if score is not None:
      return float(score)
  except FileNotFoundError:
    pass
  except Exception as e:  # noqa: BLE001
    logger.warning("Could not refresh incumbent from %s: %s", path, e)
  return current


def coevolve(
    report_path: str,
    agent_configs: dict | None = None,
    output_dir: str | None = None,
    model_id: str = os.getenv("EVOLUTION_MODEL_ID", "gemini-2.5-pro"),
    max_workers: int = 10,
    candidates: int | None = None,
    max_chars: int | None = None,
    agentic: bool = True,
    select_by_score: bool = True,
) -> CoevolutionResult:
  """Run co-evolution: detect bottleneck, evolve the right agent(s).

  Args:
      report_path: Path to quality report JSON.
      agent_configs: Dict mapping agent name -> {skill_dir, label}.
          Defaults to agents from agent_registry.json.
      output_dir: Directory to save evolved skills and logs.
      model_id: Gemini model for all LLM calls.
      max_workers: Max parallel threads.
      candidates: Number of consolidation candidates (best-of-N).
          None = auto-decide based on meaningful_rate.
      max_chars: Max chars for evolved skill.
      agentic: Use agentic error analysts.
      select_by_score: Select candidates by measured score. Needs both
          an ``output_dir`` and a configured ``score`` hook; without them
          the engine's size-based selection is used instead.

  Returns:
      CoevolutionResult with bottleneck analysis and evolved skills.
  """
  t0 = time.time()
  configs = agent_configs or _default_agent_configs()
  result = CoevolutionResult()

  # Step 1: Load report and detect bottleneck
  with open(report_path) as f:
    report = json.load(f)

  client = evolve_module._vertex_client()

  logger.info("Step 1: Detecting bottleneck...")
  _bound_targets = os.getenv("EVOLUTION_TARGET_AGENTS", "").strip()
  _precomputed = None
  try:
    from . import tools

    _precomputed = tools._bottleneck_cache.get(os.path.abspath(report_path))
  except Exception:  # noqa: BLE001
    pass
  if _bound_targets:
    # Target bound (--mode <agent>): skip the ~5-min LLM
    # classification; agents_to_evolve is filtered to the bound
    # targets below anyway.
    logger.info(
        "Skipping bottleneck classification: EVOLUTION_TARGET_AGENTS=%s",
        _bound_targets,
    )
    bottleneck = SimpleNamespace(
        recommendation="both",
        summary="skipped (target bound)",
        confidence="high",
        routing_failures=0,
        skill_failures=0,
        tool_failures=0,
        architecture_failures=0,
    )
  elif _precomputed:
    logger.info(
        "Reusing bottleneck classification from the orchestrator's "
        "earlier detect_bottleneck call: %s",
        _precomputed.get("recommendation"),
    )
    bottleneck = SimpleNamespace(
        recommendation=_precomputed.get("recommendation", "both"),
        summary=_precomputed.get("summary", "precomputed"),
        confidence=_precomputed.get("confidence", "high"),
        routing_failures=_precomputed.get("routing_failures", 0),
        skill_failures=_precomputed.get("skill_failures", 0),
        tool_failures=_precomputed.get("tool_failures", 0),
        architecture_failures=_precomputed.get("architecture_failures", 0),
    )
  else:
    bottleneck = detect_bottleneck(report, client, model_id)
  result.bottleneck_recommendation = bottleneck.recommendation
  result.bottleneck_summary = bottleneck.summary
  logger.info("Bottleneck: %s", bottleneck.summary)

  if bottleneck.recommendation == "none":
    logger.info("No failures to address. Skipping evolution.")
    result.elapsed_seconds = time.time() - t0
    return result

  # Step 2: Determine which agents to evolve
  rec = bottleneck.recommendation
  if rec == "both":
    agents_to_evolve = list(configs.keys())
  elif rec in configs:
    agents_to_evolve = [rec]
  elif rec == "none":
    agents_to_evolve = []
  else:
    fallback = list(configs.keys())[0] if configs else "unknown"
    logger.warning(
        "Unknown recommendation: %s. Defaulting to %s.",
        rec,
        fallback,
    )
    agents_to_evolve = [fallback]

  # EVOLUTION_TARGET_AGENTS (set by main.py from --mode <agent>) is
  # BINDING: it restricts evolution to the named agents regardless of the
  # bottleneck recommendation, so a scoped run stays scoped.
  target_env = os.getenv("EVOLUTION_TARGET_AGENTS", "").strip()
  if target_env:
    targets = [t.strip() for t in target_env.split(",") if t.strip()]
    bound = [a for a in agents_to_evolve if a in targets]
    dropped = [a for a in agents_to_evolve if a not in targets]
    if not bound:
      bound = [t for t in targets if t in configs]
    if dropped or bound != agents_to_evolve:
      logger.info(
          "EVOLUTION_TARGET_AGENTS=%s binds scope: evolving %s "
          "(bottleneck wanted %s)",
          target_env,
          bound,
          agents_to_evolve,
      )
    agents_to_evolve = bound

  agents_to_evolve = _evolution_order(agents_to_evolve)

  logger.info(
      "Step 2: Evolving %d agent(s) sequentially: %s",
      len(agents_to_evolve),
      agents_to_evolve,
  )

  if _excluded_count(report_path) > 0:
    logger.warning(
        "Incumbent quality report has preflight exclusions; "
        "disabling incumbent_score guard for co-evolution"
    )
    incumbent_score = None
  else:
    incumbent_score = report.get("summary", {}).get("meaningful_rate")

  def _make_score_fn(skill_dir):
    """Score a candidate skill on the eval set; returns meaningful_rate."""
    if not (select_by_score and output_dir):
      return None

    score_hook, reason = hooks.get_hook("score")
    if score_hook is None:
      # None (not a 0.0 "skipped" score): a constant 0.0 would lose to
      # the incumbent guard on every candidate, turning evolution into a
      # permanent no-op. None restores the engine's size-based selection.
      logger.info(
          "Scoring candidates by size, not by measured rate: %s",
          reason,
      )
      return None

    _counter = {"n": 0}

    def _score(skill_content):
      # Distinct name per candidate: the score hook derives its
      # report filename from this, so every candidate report
      # survives for regression extraction and PR metrics (a
      # shared tmp name left only the LAST report on disk).
      _counter["n"] += 1
      tmp = os.path.join(
          output_dir,
          f"_score_candidate_{_counter['n']}.md",
      )
      with open(tmp, "w") as fh:
        fh.write(skill_content)
      res = score_hook(tmp, skill_dir, output_dir)
      scored_report = res.get("report_path")
      if scored_report and _excluded_count(scored_report) > 0:
        # None = unmeasurable: evolve() floors it for selection but
        # never records it as the deployed score, so a flaked run
        # cannot lower the bar.
        logger.warning(
            "Candidate scored report %s has preflight exclusions; "
            "score unmeasurable",
            scored_report,
        )
        return None
      return float(res.get("meaningful_rate", 0.0))

    return _score

  # Step 3: Evolve agents (with validation + retry)
  MAX_ATTEMPTS = 5

  def _evolve_agent(agent_name):
    config = configs.get(agent_name)
    if not config:
      logger.warning("No config for agent %s, skipping", agent_name)
      return agent_name, {"error": "no config"}

    skill_dir = config["skill_dir"]
    skill_path = os.path.join(skill_dir, "SKILL.md")
    label = config.get("label", agent_name)
    current_skill = ""
    if os.path.isfile(skill_path):
      with open(skill_path) as f:
        current_skill = f.read()

    for attempt in range(1, MAX_ATTEMPTS + 1):
      logger.info(
          "Evolving %s (attempt %d/%d, skill_dir=%s)...",
          label,
          attempt,
          MAX_ATTEMPTS,
          skill_dir,
      )

      cand_dir = None
      if output_dir:
        suffix = f"_retry{attempt}" if attempt > 1 else ""
        cand_dir = os.path.join(
            output_dir,
            f"{agent_name}_candidates{suffix}",
        )
        os.makedirs(cand_dir, exist_ok=True)

      try:
        evolved = evolve(
            report_path=report_path,
            skill_dir=skill_dir,
            model_id=model_id,
            max_workers=max_workers,
            candidates=candidates,
            candidates_dir=cand_dir,
            max_chars=max_chars,
            agentic=agentic,
            score_fn=_make_score_fn(skill_dir),
            incumbent_score=incumbent_score,
        )

        issues = validate_evolved_skill(evolved, current_skill)
        if issues and attempt < MAX_ATTEMPTS:
          logger.warning(
              "Evolved %s failed validation (attempt %d): %s. Retrying...",
              label,
              attempt,
              "; ".join(issues),
          )
          continue
        elif issues:
          logger.warning(
              "Evolved %s has issues after %d attempts: %s. Using best"
              " effort.",
              label,
              attempt,
              "; ".join(issues),
          )

        agent_result = {
            "skill_dir": skill_dir,
            "evolved_chars": len(evolved),
            "candidates_dir": cand_dir,
            "validation_issues": issues,
            "attempt": attempt,
        }

        # Deploy the SELECTED winner to SKILL.md. The score hook (used
        # during score-based selection) leaves the last-scored candidate
        # there, so we must write the actual winner back.
        with open(skill_path, "w") as f:
          f.write(evolved)
        logger.info(
            "Deployed selected %s skill to %s (%d chars)",
            label,
            skill_path,
            len(evolved),
        )

        if output_dir:
          out_path = os.path.join(
              output_dir,
              f"{agent_name}_evolved_skill.md",
          )
          with open(out_path, "w") as f:
            f.write(evolved)
          logger.info(
              "Saved evolved %s skill to %s (%d chars)",
              label,
              out_path,
              len(evolved),
          )

        return agent_name, agent_result

      except Exception as e:  # noqa: BLE001
        logger.error("Failed to evolve %s (attempt %d): %s", label, attempt, e)
        if attempt == MAX_ATTEMPTS:
          return agent_name, {"error": str(e)}

    return agent_name, {"error": "exhausted retries"}

  # Sequential: each agent's candidate scoring runs full traffic on the
  # shared SKILL.md files, so parallel evolution would corrupt each other.
  for name in agents_to_evolve:
    agent_name, agent_result = _evolve_agent(name)
    result.evolved_agents[agent_name] = agent_result
    # The bar the next agent must clear is the system as it now runs,
    # including this agent's deployed winner (or the kept incumbent).
    incumbent_score = _refresh_incumbent(output_dir, incumbent_score)

  # Save co-evolution summary
  result.elapsed_seconds = time.time() - t0
  if output_dir:
    summary_path = os.path.join(output_dir, "coevolution_summary.json")
    summary = {
        "bottleneck": {
            "recommendation": bottleneck.recommendation,
            "confidence": bottleneck.confidence,
            "summary": bottleneck.summary,
            "routing_failures": _failure_count(bottleneck.routing_failures),
            "skill_failures": _failure_count(bottleneck.skill_failures),
            "tool_failures": _failure_count(bottleneck.tool_failures),
            "architecture_failures": _failure_count(
                bottleneck.architecture_failures
            ),
        },
        "evolved_agents": result.evolved_agents,
        "elapsed_seconds": result.elapsed_seconds,
    }
    with open(summary_path, "w") as f:
      json.dump(summary, f, indent=2)
    logger.info("Co-evolution summary saved to %s", summary_path)

  logger.info("Co-evolution complete: %s", result.summary)
  return result
