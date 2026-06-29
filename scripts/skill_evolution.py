#!/usr/bin/env python3
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

"""Skill evolution: turn a scored quality report into a better SKILL.md.

Consumes a quality report (e.g. from quality_report.py) and the agent's
current ``SKILL.md`` and produces an improved skill via a fleet of parallel,
independent LLM "analysts" whose patches are merged by an inductive
consolidator. Implements the core of Trace2Skill (arXiv:2603.25158, parallel
analysts + inductive consolidation) and AutoSkill (arXiv:2603.01145, the
accumulative ``P_merge`` that preserves capability identity).

Design: the engine has no agent/traffic/registry dependencies. Candidate
selection (best-of-N) is delegated to a caller-supplied ``score_fn`` so the
same engine serves any agent. Import it like quality_report:

    evolve = import_sdk_module("skill_evolution")
    new_skill = evolve.evolve_skill(report, current_skill, score_fn=my_scorer)

Or run as a CLI:

    python skill_evolution.py --report report.json --skill SKILL.md -o V1.md

Auth: uses Vertex AI via the google-genai client. Set GOOGLE_CLOUD_PROJECT and
GOOGLE_CLOUD_LOCATION (or pass project/location), and authenticate with ADC
(gcloud auth application-default login).
"""

import argparse
from collections import Counter
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
from typing import Callable, Optional

logger = logging.getLogger("skill_evolution")

# ---------------------------------------------------------------------------
# Analyst + consolidator system prompts (Trace2Skill / AutoSkill)
# ---------------------------------------------------------------------------

ERROR_ANALYST_PROMPT = """\
You are an Error Analyst in a skill evolution system. You examine a single
FAILED agent trajectory to identify what went wrong and propose a specific,
GENERALIZABLE improvement to the agent's skill document.

You receive the current skill document and a failed trajectory (which may be
multi-turn and may include an execution trace of tool calls and routing).

Analysis process:
1. Read the trajectory. If a [CORRECTION] turn is present, extract BOTH what the
   agent claimed (the wrong fact) AND what the user corrected it to (the right
   fact) -- that is direct evidence of a skill gap.
2. If an execution trace is present, use it as ground truth: did the agent skip a
   tool call (HALLUCINATION), call the wrong tool (KEYWORD_GAP), ignore a tool
   result (MISSING_RULE), get routed wrong (SCOPE_GAP), or merely echo the user's
   correction without re-verifying via a tool (PARROTING)?
3. Identify the ROOT CAUSE -- why the skill document did not prevent this -- and
   categorize it: KEYWORD_GAP, MISSING_RULE, AMBIGUITY, SCOPE_GAP, HALLUCINATION,
   PARROTING, or CORRECTION_IGNORE.
4. Propose a concrete patch that generalizes beyond this one question.

Output format (use exactly this structure):

## Root Cause
[CATEGORY]: [one-line description]

## Analysis
[2-3 sentences. Cite the specific wrong claim + user correction, or specific tool
calls (or their absence), as evidence.]

## Proposed Patch
Section: [which section of the skill to modify or create]
Action: add_rule | add_mapping | add_edge_case | add_anti_pattern
Content:
[The exact text to add. Must generalize beyond this single trajectory.]

RULES:
- Patches must GENERALIZE; be specific and actionable, not vague.
- User corrections are FACTUAL EVIDENCE -- use them to write precise rules.
- If the failure has no generalizable fix, output "NO_PATCH: [reason]".
"""

SUCCESS_ANALYST_PROMPT = """\
You are a Success Analyst in a skill evolution system. You examine a single
SUCCESSFUL agent trajectory to identify transferable patterns worth reinforcing
in the agent's skill document.

You receive the current skill document and a successful trajectory (question,
response, judge verdict; possibly multi-turn).

IMPORTANT -- PARROTED RECOVERIES ARE NOT SUCCESS: if the agent merely repeated the
user's correction without re-querying a tool (a [~] parroted segment), output
"NO_PATCH: parroted recovery, not a transferable success pattern."

Analysis process:
1. Identify what the agent did RIGHT that is NOT already explicit in the skill.
2. Focus on transferable patterns: KEYWORD_MAPPING, RESPONSE_PATTERN,
   DISAMBIGUATION, TOOL_USAGE, or CORRECTION_RECOVERY.

Output format (use exactly this structure):

## Pattern
[CATEGORY]: [one-line description]

## Analysis
[2-3 sentences on what worked and why it is worth reinforcing.]

## Proposed Patch
Section: [which section of the skill to modify or create]
Action: reinforce_pattern | add_mapping | add_example
Content:
[The exact text to add. Must generalize beyond this one question.]

RULES:
- Only propose patches for patterns NOT already in the skill.
- If the skill already covers it, output "NO_PATCH: skill already covers this".
"""

CONSOLIDATOR_PROMPT = """\
You are a Skill Merger. You receive a BASE skill document and patches from
analyst agents who independently examined execution trajectories.

Your job is an ACCUMULATIVE MERGE, not a rewrite. Produce the SEMANTIC UNION of
the base skill and the patches (AutoSkill's P_merge -- versioned evolution that
preserves capability identity).

Merge rules (follow ALL):
1. Preserve identity: keep the same name, purpose, and overall structure.
2. Never drop existing content. Every section, rule, and table row in the BASE
   MUST appear in your output unless a patch explicitly corrects it (then update
   that rule in place). Dropping an existing section is a failure.
3. Semantic union, not concatenation: integrate each patch into the right section.
4. Prevalence: insights from many independent analysts are systematic -- integrate
   confidently; 1-2 analyst one-offs only if clearly general.
5. Deduplicate: state a repeated insight once, in the clearest wording.
6. Import only reusable, non-conflicting additions; strip case-specific entities
   and analyst scratch notes ("NO_PATCH", "Root Cause:").
7. Do not invent figures or policies absent from the base or a patch.
8. A skill is BEHAVIORAL: do NOT bake specific data values (numbers, dates,
   dollar amounts, limits) into it -- those must come from tools at runtime, and
   copies go stale or wrong. Keep rules and tool-usage guidance; never paste
   tool-result facts pulled from a trajectory. (Preserve any data already in the
   base verbatim, but add no new specific values.)
9. On conflict, keep the better-evidenced patch.

Output the COMPLETE merged SKILL.md (frontmatter + full body):
- YAML frontmatter between --- delimiters: keep name/description; set
  metadata.version = base version + 1 ("0"->"1"); metadata.author =
  skill-evolution; metadata.evolved_from = base version.
- The full body = every base section (verbatim or refined in place) plus new
  sections motivated by patches (e.g. Keyword Mappings, Edge Cases, Anti-Patterns,
  Out-of-Scope Handling).

Self-check before output: does every "## " heading from the base still appear? If
not, add it back.
"""

COMPACTION_PROMPT = """\
You are a Skill Compactor. Distill an evolved skill that grew too large to under
{max_chars} characters while preserving effectiveness.

Keep all mandatory tool-use rules and anti-hallucination directives verbatim.
Merge redundant rules (keep the most specific), compress obvious keyword mappings,
remove filler, and preserve section headings and numbered lists.

Output the COMPLETE compacted SKILL.md including YAML frontmatter. Keep the same
version number and metadata.
"""

# ---------------------------------------------------------------------------
# Trajectory partitioning + formatting
# ---------------------------------------------------------------------------


def _has_parroted_recovery(session: dict) -> bool:
  """True if the session has a parroted sub-trajectory outcome."""
  for st in session.get("sub_trajectories", []) or []:
    if st.get("outcome") == "parroted":
      return True
  return False


def partition_trajectories(report: dict) -> tuple[list, list]:
  """Split sessions into successes (T+) and failures (T-).

  Sessions scored "meaningful"/"declined" are successes, EXCEPT parroted
  recoveries (the user did the agent's work) which are reclassified as failures.
  """
  successes, failures = [], []
  for s in report.get("sessions", []):
    usefulness = (
        s.get("metrics", {}).get("response_usefulness", {}).get("category", "")
    )
    if usefulness in ("meaningful", "declined"):
      (failures if _has_parroted_recovery(s) else successes).append(s)
    elif usefulness in ("unhelpful", "partial"):
      failures.append(s)
  return successes, failures


def _format_conversation(conversation) -> str:
  """Format a conversation (list of turn dicts, or a string) into text."""
  if isinstance(conversation, str):
    return conversation
  if not isinstance(conversation, list) or not conversation:
    return ""
  parts = []
  for turn in conversation:
    role = (turn.get("role") or "?").capitalize()
    tag = turn.get("tag") or turn.get("inferred_tag") or ""
    tag_str = f" [{tag}]" if tag else ""
    parts.append(f"{role}{tag_str}: {turn.get('text', '')}")
  return "\n".join(parts)


def format_trajectory(session: dict) -> str:
  """Format a session for analyst consumption (single- or multi-turn)."""
  metrics = session.get("metrics", {})
  usefulness = metrics.get("response_usefulness", {})
  grounding = metrics.get("task_grounding", {})

  conversation = _format_conversation(session.get("conversation", []))
  if conversation:
    quality = session.get("quality_scores", {})
    result = f"=== Conversation ===\n{conversation}\n\n"
    result += f"Agent: {session.get('answered_by', '')}\n"
    result += f"Verdict: {usefulness.get('category', '')}\n"
    result += f"Justification: {usefulness.get('justification', '')}\n"
    result += f"Grounding: {grounding.get('category', '')}\n"
    if session.get("corrections"):
      result += f"User corrections: {session['corrections']}\n"
    for dim in (
        "correctness",
        "tool_usage",
        "specificity",
        "scope_compliance",
        "first_time_right",
    ):
      score_data = quality.get(dim, {})
      if score_data:
        result += (
            f"{dim}: {score_data.get('score', '?')}/2 -"
            f" {score_data.get('reason', '')}\n"
        )
    # Surface the per-segment correction outcomes the turn tagger emits
    # (quality_report writes these as ``sub_trajectories``). This is the
    # parrot/recover evidence the Error Analyst is told to use.
    subtraj = session.get("sub_trajectories", []) or []
    if subtraj:
      result += "\n=== Correction sub-trajectories ===\n"
      for seg in subtraj:
        outcome = seg.get("outcome", "")
        icon = {"recovered": "+", "parroted": "~"}.get(outcome, "-")
        span = ""
        if (
            seg.get("start_turn") is not None
            and seg.get("end_turn") is not None
        ):
          span = f" (turns {seg['start_turn']}-{seg['end_turn']})"
        result += f"[{icon}] {seg.get('label', '')}{span} -> {outcome}\n"
    return result

  return (
      f"Question: {session.get('question', '')}\n"
      f"Response: {session.get('response', '')}\n"
      f"Agent: {session.get('answered_by', '')}\n"
      f"Verdict: {usefulness.get('category', '')}\n"
      f"Justification: {usefulness.get('justification', '')}\n"
      f"Grounding: {grounding.get('category', '')}"
  )


# ---------------------------------------------------------------------------
# Analysts
# ---------------------------------------------------------------------------

ROOT_CAUSE_CATEGORIES = frozenset(
    {
        "KEYWORD_GAP",
        "MISSING_RULE",
        "AMBIGUITY",
        "SCOPE_GAP",
        "HALLUCINATION",
        "PARROTING",
        "CORRECTION_IGNORE",
        "KEYWORD_MAPPING",
        "RESPONSE_PATTERN",
        "DISAMBIGUATION",
        "TOOL_USAGE",
        "CORRECTION_RECOVERY",
    }
)


def run_analyst(client, model, system_prompt, session, current_skill):
  """Run one analyst on one trajectory. Returns patch text or None."""
  from google.genai import types

  trajectory = format_trajectory(session)
  prompt = (
      f"<current_skill>\n{current_skill}\n</current_skill>\n\n"
      f"<trajectory>\n{trajectory}\n</trajectory>\n\n"
      "Analyze this trajectory and propose your patch."
  )
  response = client.models.generate_content(
      model=model,
      contents=prompt,
      config=types.GenerateContentConfig(
          system_instruction=system_prompt, temperature=0.3
      ),
  )
  text = (response.text or "").strip()
  if "NO_PATCH" in text and len(text) < 200:
    return None
  return text or None


def passes_quality_gate(patch: str) -> bool:
  """Reject patches lacking structure or a root-cause category."""
  if len(patch.strip()) < 50:
    return False
  if not any(cat in patch for cat in ROOT_CAUSE_CATEGORIES):
    return False
  has_analysis = "## Root Cause" in patch or "## Pattern" in patch
  has_patch = "## Proposed Patch" in patch or "Content:" in patch
  return has_analysis and has_patch


# ---------------------------------------------------------------------------
# Consolidation + guardrails
# ---------------------------------------------------------------------------


def strip_code_fences(text: str) -> str:
  """Strip a wrapping markdown code fence if the model added one."""
  if text.startswith("```"):
    lines = text.split("\n")
    lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
    stripped = "\n".join(lines).strip()
    return stripped or text
  return text


_ADK_VAR_RE = re.compile(r"\{(\w+)\}")


def sanitize_adk_vars(skill: str) -> str:
  """Escape {identifier} so ADK does not treat it as a session-state variable.

  ADK resolves any {valid_identifier} in an instruction as a context variable and
  crashes when it is missing. Evolved skills often pick up template-like tokens
  (e.g. {requested_topic}) from tool error messages; rewrite {word} as <word>.
  """
  return _ADK_VAR_RE.sub(lambda m: f"<{m.group(1)}>", skill)


def compute_prevalence_summary(patches: list[str]) -> str:
  """Count root-cause categories across patches (systematic vs idiosyncratic)."""
  counts = Counter()
  for patch in patches:
    match = re.search(r"## Root Cause\s*\n\s*\[?(\w+)\]?", patch) or re.search(
        r"## Pattern\s*\n\s*\[?(\w+)\]?", patch
    )
    if match:
      counts[match.group(1)] += 1
  if not counts:
    return ""
  total = len(patches)
  lines = [f"Prevalence across {total} independent analyst patches:"]
  for cat, count in counts.most_common():
    strength = "STRONG" if count >= 3 else "moderate" if count >= 2 else "weak"
    lines.append(
        f"  {cat}: {count}/{total} ({round(count / total * 100)}%) -- {strength}"
    )
  return "\n".join(lines)


def validate_evolved_skill(evolved: str, current_skill: str) -> list[str]:
  """Structural guardrails (Trace2Skill). Empty list = valid."""
  issues = []
  if "---" not in evolved:
    issues.append("Missing YAML frontmatter")
  else:
    fm = re.match(r"---\n(.*?)\n---", evolved, re.DOTALL)
    if fm:
      try:
        import yaml

        yaml.safe_load(fm.group(1))
      except Exception as e:  # noqa: BLE001
        issues.append(f"Invalid YAML frontmatter: {e}")
  if "NO_PATCH:" in evolved and "NO_PATCH:" not in current_skill:
    issues.append("Analyst leak detected: 'NO_PATCH:'")
  if _ADK_VAR_RE.findall(evolved):
    issues.append("ADK context-variable collision: unescaped {identifier}")
  if len(evolved) < len(current_skill):
    issues.append(
        f"Smaller than input ({len(evolved)} < {len(current_skill)}); likely"
        " truncated."
    )
  headers = [ln for ln in evolved.split("\n") if ln.startswith("## ")]
  if len(headers) < 2:
    issues.append(f"Too few sections ({len(headers)} '##' headers).")

  def _headings(text: str) -> set:
    return {
        ln.strip().lstrip("#").strip().lower()
        for ln in text.split("\n")
        if ln.startswith("## ") or ln.startswith("### ")
    }

  dropped = sorted(_headings(current_skill) - _headings(evolved))
  if dropped:
    preview = ", ".join(dropped[:3])
    more = f" (and {len(dropped) - 3} more)" if len(dropped) > 3 else ""
    issues.append(
        f"Dropped {len(dropped)} base section(s): {preview}{more}. Accumulative"
        " merge must preserve every existing section."
    )
  return issues


def run_consolidator(
    client, model, current_skill, patches, summary, temperature=0.2
):
  """Merge all patches into one evolved skill (accumulative semantic union)."""
  from google.genai import types

  patches_text = "\n\n---\n\n".join(
      f"### Patch {i + 1}\n{p}" for i, p in enumerate(patches)
  )
  prevalence = compute_prevalence_summary(patches)
  prompt = (
      f"<base_skill>\n{current_skill}\n</base_skill>\n\n"
      "The base_skill is your STARTING POINT. Merge the analyst patches INTO it"
      " as a semantic union. Keep every existing section unless a patch corrects"
      " it. Never drop a section.\n\n"
      f"<quality_summary>\nMeaningful rate:"
      f" {summary.get('meaningful_rate', 0)}%\nUnhelpful:"
      f" {summary.get('unhelpful', 0)}\nPartial:"
      f" {summary.get('partial', 0)}\n</quality_summary>\n\n"
  )
  if prevalence:
    prompt += f"<prevalence_summary>\n{prevalence}\n</prevalence_summary>\n\n"
  prompt += (
      f"<analyst_patches>\n{patches_text}\n</analyst_patches>\n\n"
      "Produce the complete MERGED SKILL.md (base + patches, semantic union, no"
      " section dropped). Output ONLY the file content."
  )
  for temp in (temperature, min(temperature + 0.3, 1.0)):
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=CONSOLIDATOR_PROMPT,
            temperature=temp,
            max_output_tokens=16384,
        ),
    )
    result = strip_code_fences((response.text or "").strip())
    if len(result) >= 50:
      return result
    logger.warning("Consolidator returned %d chars; retrying.", len(result))
  return result


def run_compaction(client, model, skill, max_chars):
  """Compact an evolved skill that exceeds max_chars."""
  from google.genai import types

  if len(skill) <= max_chars:
    return skill
  logger.info("Compacting %d chars to under %d...", len(skill), max_chars)
  response = client.models.generate_content(
      model=model,
      contents=(
          f"<skill>\n{skill}\n</skill>\n\nCompact this skill to under"
          f" {max_chars} characters. Output ONLY the compacted SKILL.md."
      ),
      config=types.GenerateContentConfig(
          system_instruction=COMPACTION_PROMPT.format(max_chars=max_chars),
          temperature=0.1,
      ),
  )
  return strip_code_fences((response.text or "").strip())


def _consolidate_once(
    client, model, current_skill, patches, summary, max_chars
):
  """One consolidation with a guardrail retry; falls back to the base skill."""
  evolved = run_consolidator(client, model, current_skill, patches, summary)
  if validate_evolved_skill(evolved, current_skill):
    evolved = run_consolidator(
        client, model, current_skill, patches, summary, temperature=0.6
    )
    if validate_evolved_skill(evolved, current_skill):
      logger.error("Guardrail issues persist after retry; keeping base skill.")
      return current_skill
  if max_chars and len(evolved) > max_chars:
    evolved = run_compaction(client, model, evolved, max_chars)
  return evolved


# ---------------------------------------------------------------------------
# Patch collection
# ---------------------------------------------------------------------------


def collect_patches(
    report,
    current_skill,
    *,
    client,
    model,
    max_workers=10,
    max_success_samples=15,
    analyst_mode="both",
):
  """Run the analyst fleet over the report. Returns the list of kept patches."""
  successes, failures = partition_trajectories(report)
  logger.info(
      "Trajectories: %d successes, %d failures", len(successes), len(failures)
  )
  if analyst_mode == "error-only":
    successes = []
  elif analyst_mode == "success-only":
    failures = []
  successes = successes[:max_success_samples]

  patches = []
  with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = {}
    for s in failures:
      fut = executor.submit(
          run_analyst, client, model, ERROR_ANALYST_PROMPT, s, current_skill
      )
      futures[fut] = ("error", (s.get("question", "") or "")[:60])
    for s in successes:
      fut = executor.submit(
          run_analyst, client, model, SUCCESS_ANALYST_PROMPT, s, current_skill
      )
      futures[fut] = ("success", (s.get("question", "") or "")[:60])
    for fut in as_completed(futures):
      kind, question = futures[fut]
      try:
        result = fut.result()
        if result:
          patches.append(result)
      except Exception as e:  # noqa: BLE001
        logger.warning("analyst [%s] %s failed: %s", kind, question, e)

  kept = [p for p in patches if passes_quality_gate(p)]
  logger.info(
      "Collected %d patches (%d passed the quality gate).",
      len(patches),
      len(kept),
  )
  return kept


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _make_client(project, location):
  from google import genai

  return genai.Client(
      vertexai=True,
      project=project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
      location=location or os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
  )


def select_candidate(
    viable: list,
    current_skill: str,
    score_fn: Optional[Callable[[str], float]] = None,
    min_improvement: float = 0.5,
) -> str:
  """Pick which evolved candidate to ship (pure; no model calls).

  - No viable candidate -> keep the base skill.
  - No ``score_fn`` -> return the median-size viable candidate.
  - With ``score_fn`` -> return the highest-scoring candidate ONLY if it beats
    the incumbent by at least ``min_improvement``; otherwise keep the base skill.

  The last rule is the restraint property of a self-modifying system: when
  nothing clearly improves, leave the already-good skill alone.
  """
  if not viable:
    logger.warning("No viable candidate passed guardrails; keeping base skill.")
    return current_skill

  if score_fn is None:
    ordered = sorted(viable, key=len)
    selected = ordered[len(ordered) // 2]
    logger.info("Selected median-size candidate (%d chars).", len(selected))
    return selected

  incumbent = score_fn(current_skill)
  best, best_score = None, float("-inf")
  for cand in viable:
    score = score_fn(cand)
    logger.info("Candidate scored %.3f (incumbent %.3f).", score, incumbent)
    if score > best_score:
      best, best_score = cand, score
  if best_score < incumbent + min_improvement:
    logger.info(
        "Best candidate %.3f does not beat incumbent %.3f + %.3f margin;"
        " keeping base skill.",
        best_score,
        incumbent,
        min_improvement,
    )
    return current_skill
  logger.info("Selected best candidate by score: %.3f.", best_score)
  return best


def evolve_skill(
    report,
    current_skill: str,
    *,
    model: str = "gemini-2.5-pro",
    project: Optional[str] = None,
    location: Optional[str] = None,
    max_workers: int = 10,
    max_success_samples: int = 15,
    candidates: int = 3,
    max_chars: Optional[int] = None,
    analyst_mode: str = "both",
    score_fn: Optional[Callable[[str], float]] = None,
    min_improvement: float = 0.5,
    client=None,
) -> str:
  """Evolve a SKILL.md from a scored quality report.

  Args:
    report: A quality report dict (or a path to its JSON). Must contain
      ``sessions`` with ``metrics.response_usefulness.category`` and either a
      ``conversation`` or ``question``/``response`` per session.
    current_skill: The current SKILL.md content (the base to merge into).
    model: Gemini model for analysts + consolidator (Vertex AI).
    project, location: Vertex project/location (default: env GOOGLE_CLOUD_*).
    candidates: Number of consolidation candidates to generate (best-of-N).
    max_chars: If set, compact any candidate that exceeds this size.
    analyst_mode: "both" (default), "error-only", or "success-only".
    score_fn: Optional ``(skill_content) -> float`` used to pick the best
      candidate and to gate against the incumbent. With no ``score_fn`` the
      median-size viable candidate is returned (avoids truncated runts/bloat).
    min_improvement: A candidate must beat the incumbent score by at least this
      margin (in score_fn units) to be selected; otherwise the base is kept.
    client: Optional pre-built google-genai Client (else one is created).

  Returns:
    The evolved SKILL.md content, or the unchanged ``current_skill`` if no
    improvement was found.
  """
  if isinstance(report, str):
    with open(report) as f:
      report = json.load(f)
  summary = report.get("summary", {})
  client = client or _make_client(project, location)

  patches = collect_patches(
      report,
      current_skill,
      client=client,
      model=model,
      max_workers=max_workers,
      max_success_samples=max_success_samples,
      analyst_mode=analyst_mode,
  )
  if not patches:
    logger.warning("No patches to consolidate; returning the current skill.")
    return current_skill

  logger.info("Generating %d candidate(s)...", candidates)
  cands = []
  with ThreadPoolExecutor(max_workers=min(candidates, max_workers)) as executor:
    futures = [
        executor.submit(
            _consolidate_once,
            client,
            model,
            current_skill,
            patches,
            summary,
            max_chars,
        )
        for _ in range(candidates)
    ]
    for fut in as_completed(futures):
      try:
        cands.append(fut.result())
      except Exception as e:  # noqa: BLE001
        logger.warning("Candidate consolidation failed: %s", e)

  # Sanitize first, then validate the sanitized text -- otherwise a candidate
  # whose only flaw is an unescaped {context_var} (exactly what sanitize_adk_vars
  # fixes) gets rejected by validate before it can be cleaned.
  viable = []
  for c in cands:
    if not c or c == current_skill:
      continue
    c = sanitize_adk_vars(c)
    if not validate_evolved_skill(c, current_skill):
      viable.append(c)
  return select_candidate(viable, current_skill, score_fn, min_improvement)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main():
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument(
      "--report",
      required=True,
      help="quality report JSON (failures to learn from)",
  )
  ap.add_argument("--skill", required=True, help="path to the current SKILL.md")
  ap.add_argument(
      "-o",
      "--output",
      required=True,
      help="where to write the evolved SKILL.md",
  )
  ap.add_argument("--model", default="gemini-2.5-pro")
  ap.add_argument("--project", default=None)
  ap.add_argument("--location", default=None)
  ap.add_argument("--candidates", type=int, default=3)
  ap.add_argument("--max-chars", type=int, default=None)
  ap.add_argument(
      "--analyst-mode",
      default="both",
      choices=["both", "error-only", "success-only"],
  )
  args = ap.parse_args()

  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s [%(levelname)s] %(message)s",
      datefmt="%H:%M:%S",
  )
  for noisy in ("google.genai", "google_genai", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

  with open(args.skill) as f:
    current_skill = f.read()
  evolved = evolve_skill(
      args.report,
      current_skill,
      model=args.model,
      project=args.project,
      location=args.location,
      candidates=args.candidates,
      max_chars=args.max_chars,
      analyst_mode=args.analyst_mode,
  )
  with open(args.output, "w") as f:
    f.write(evolved)
  changed = "unchanged" if evolved == current_skill else "evolved"
  print(f"{changed}: wrote {len(evolved)} chars to {args.output}")


if __name__ == "__main__":
  _main()
