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

Host contract: integrations should import only the names listed in ``__all__``.
``evolve_skill`` and ``collect_patches`` accept the quality-report session
mapping described in their docstrings; new optional session fields are additive,
and any incompatible schema change must be called out in ``CHANGELOG.md``.
``error_analyst_fn`` is the supported extension seam for failure analysis and
must return patch text or ``None``. The public return value of
``collect_patches`` remains ``list[str]``.
"""

import argparse
from collections import Counter
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import json
import logging
import math
import os
import re
import threading
from typing import Any, Callable, Optional

# Host analyst signature: fn(client, model, session, current_skill, tools)
# -> patch text or None (see collect_patches).
ErrorAnalystFn = Callable[[Any, str, dict, str, Optional[str]], Optional[str]]

__all__ = [
    "ErrorAnalystFn",
    "collect_patches",
    "evolve_skill",
    "format_trajectory",
    "partition_trajectories",
    "select_candidate",
]

# ``collect_patches`` is an established replacement seam, so its signature and
# exact ``list[str]`` result must stay unchanged. During ``evolve_skill`` only,
# this context-local identity map carries provenance alongside the returned
# string objects. Wrappers that reorder, filter, or deduplicate those objects
# therefore retain correct provenance without receiving a new private keyword.
_PATCH_PROVENANCE: ContextVar[Optional[dict[int, tuple[str, str]]]] = (
    ContextVar("skill_evolution_patch_provenance", default=None)
)

# Segment outcome icons shared by both sub-trajectory renderers.
_SEGMENT_OUTCOME_ICONS = {"recovered": "+", "parroted": "~"}

logger = logging.getLogger("skill_evolution")

# ---------------------------------------------------------------------------
# Analyst + consolidator system prompts (Trace2Skill / AutoSkill)
# ---------------------------------------------------------------------------

ERROR_ANALYST_PROMPT = """\
You are an Error Analyst in a skill evolution system. You examine a single
FAILED agent trajectory to identify what went wrong and propose a specific,
GENERALIZABLE improvement to the agent's skill document.

You receive the current skill document, the list of TOOLS the agent has, and a
failed trajectory (which may be multi-turn and may include an execution trace of
tool calls and routing).

Analysis process:
1. Read the trajectory. If a [CORRECTION] turn is present, extract BOTH what the
   agent claimed (the wrong fact) AND what the user corrected it to (the right
   fact) -- that is direct evidence of a skill gap.
2. Check the TOOLS. If the agent deflected, declined, or said it lacked the
   information (e.g. "contact HR") on a topic one of its tools can answer, the
   root cause is TOOL_USAGE: it failed to use a tool it already has. The fix is a
   rule to CALL THE TOOL first -- never to bake the missing fact into the skill.
3. If an execution trace is present, use it as ground truth: did the agent skip a
   tool call (TOOL_USAGE), call the wrong tool (WRONG_TOOL), ignore a tool
   result (MISSING_RULE), get routed wrong (SCOPE_GAP), invent a fact
   (HALLUCINATION), or echo the user's correction without re-verifying via a tool
   (PARROTING)?
4. Identify the ROOT CAUSE -- why the skill did not prevent this -- and categorize
   it: TOOL_USAGE, WRONG_TOOL, MISSING_RULE, AMBIGUITY, SCOPE_GAP, HALLUCINATION,
   PARROTING, or CORRECTION_IGNORE.
5. Propose a concrete, BEHAVIORAL patch that generalizes beyond this one question.

Output format (use exactly this structure):

## Root Cause
[CATEGORY]: [one-line description]

## Analysis
[2-3 sentences citing the evidence: the wrong claim + user correction, or the
missing/wrong tool call.]

## Proposed Patch
Section: [which section of the skill to modify or create]
Action: add_rule | add_edge_case | add_anti_pattern
Content:
[The exact text to add. A behavioral rule, not a fact. Must generalize.]

RULES:
- Patches must GENERALIZE and be BEHAVIORAL (how to act), never baked facts.
- A user correction is a HYPOTHESIS, never a fact: it proves a gap exists at
  that point in the conversation, but only a TOOL can prove what is true.
  Never copy a user-asserted value into a patch — the fix is always a rule to
  verify via the tool (and to hold the tool's value even when the user
  disputes it).
- A missing fact that a tool could fetch is a TOOL_USAGE fix (a rule to call the
  tool), NOT a reason to return NO_PATCH.
- Only output "NO_PATCH: [reason]" if there is genuinely no behavioral fix and no
  tool could have helped (e.g. a truly out-of-scope request).
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
2. Focus on transferable patterns: RESPONSE_PATTERN, DISAMBIGUATION,
   TOOL_USAGE, or CORRECTION_RECOVERY. Do NOT propose keyword or synonym
   mappings -- the lookup tool resolves the user's wording itself, so a synonym
   table in the skill is redundant.

Output format (use exactly this structure):

## Pattern
[CATEGORY]: [one-line description]

## Analysis
[2-3 sentences on what worked and why it is worth reinforcing.]

## Proposed Patch
Section: [which section of the skill to modify or create]
Action: reinforce_pattern | add_rule | add_example
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
   MUST appear in your output unless a patch corrects it (then update it in
   place). Exception: if the BASE tells the agent to refuse, decline, or deflect
   (e.g. "contact HR") on a topic its tools CAN answer, that is the defect to
   fix -- rewrite it into a tool-first rule. Overriding such a deflection is
   required, not a failure.
3. Semantic union, not concatenation: integrate each patch into the right section.
4. Prevalence: insights from many independent analysts are systematic -- integrate
   confidently; 1-2 analyst one-offs only if clearly general.
5. Deduplicate: state a repeated insight once, in the clearest wording.
6. Import only reusable, non-conflicting additions; strip case-specific entities
   and analyst scratch notes ("NO_PATCH", "Root Cause:").
7. Do not invent figures or policies absent from the base or a patch.
8. A skill is BEHAVIORAL, not a knowledge base. Do NOT add facts of any kind --
   not numbers/dates/limits, and not qualitative facts ("PTO is paid out on
   resignation", "a doctor's note is required"). The skill says HOW to behave;
   the TOOL supplies WHAT. Never paste tool-result facts from a trajectory into
   the skill. Preserve facts already in the base verbatim, but add no new ones --
   if the agent needs a fact, the rule is "look it up".
9. On conflict, keep the better-evidenced patch.
10. Do NOT add a Keyword Mapping or Terminology Mapping section. The lookup tool
   resolves the user's own wording itself; capture behavior as rules, never as
   synonym tables.
11. Be tool-first: the merged skill must tell the agent to use its tools for any
   in-scope question BEFORE answering, and to say it lacks the information only
   after a tool search comes up empty -- never to deflect to HR first.
12. Generalize, do not enumerate: prefer ONE rule ("look up any company-policy
   question with your tools") over a long list of specific topics. Collapse
   per-topic lists from the patches into that general rule.

Output the COMPLETE merged SKILL.md (frontmatter + full body):
- YAML frontmatter between --- delimiters: keep name/description; set
  metadata.version = base version + 1 ("0"->"1"); metadata.author =
  skill-evolution; metadata.evolved_from = base version.
- The full body = every base section (refined in place) plus new sections
  motivated by patches (e.g. Tool Usage, Anti-Patterns, Response Rules). Do NOT
  add a Keyword/Terminology Mapping section.

Self-check before output: does every "## " heading from the base still appear? If
not, add it back.
"""

COMPACTION_PROMPT = """\
You are a Skill Compactor. Distill an evolved skill that grew too large to under
{max_chars} characters while preserving effectiveness.

Keep all mandatory tool-use rules and anti-hallucination directives verbatim.
Merge redundant rules (keep the most specific), drop baked facts the tool can
supply, remove filler, and preserve section headings and numbered lists.

Output the COMPLETE compacted SKILL.md including YAML frontmatter. Keep the same
version number and metadata.
"""

# ---------------------------------------------------------------------------
# Trajectory partitioning + formatting
# ---------------------------------------------------------------------------


def _has_parroted_recovery(session: dict) -> bool:
  """True if the session has a parroted sub-trajectory outcome.

  Checks both ``sub_trajectories`` (turn-tagger output) and
  ``execution_sub_trajectories`` (hosts that segment execution traces per
  correction). Either marking a segment ``parroted`` reclassifies the session.
  """
  for key in ("sub_trajectories", "execution_sub_trajectories"):
    for st in session.get(key, []) or []:
      # Host-supplied lists may carry malformed entries; a non-dict must not
      # kill partitioning for the whole report.
      if isinstance(st, dict) and st.get("outcome") == "parroted":
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


def _format_tool_calls(session: dict) -> str:
  """Render the actual tool calls (name + args) for the analyst.

  This makes *tool selection* visible -- e.g. a deflection that never called a
  tool, versus one that called the wrong tool -- so the analyst can propose a
  ``TOOL_USAGE`` / ``WRONG_TOOL`` rule from observed behavior, not just the
  judge's grounding flag. Sessions scored before this field existed carry no
  ``tool_calls_detail`` key and render nothing (backward compatible).
  """
  if "tool_calls_detail" not in session:
    return ""
  detail = session.get("tool_calls_detail") or []
  if not detail:
    # Empty detail but a nonzero count means calls happened yet weren't captured
    # (e.g. a BigQuery-path session) -- say nothing rather than a false "(none)".
    if session.get("tool_calls"):
      return ""
    return "Tool calls: (none)\n"
  lines = ["Tool calls:"]
  for call in detail:
    name = call.get("name", "?") or "?"
    args = call.get("args", {}) or {}
    lines.append(f"  - {name}({json.dumps(args, sort_keys=True)})")
  return "\n".join(lines) + "\n"


def _coerce_trace_text(trace) -> str:
  """Degrade a structured (list/dict) trace value to a readable dump.

  Trajectory rendering runs inside analyst futures; a host-supplied non-str
  trace must not raise a per-session TypeError that gets swallowed as a
  warning (which zeroes out the whole run).
  """
  if isinstance(trace, str):
    return trace
  try:
    return json.dumps(trace, indent=1, default=str)
  except (TypeError, ValueError):
    return str(trace)


def _format_correction_evidence(session) -> str:
  """Render turn-boundary correction evidence, or '' when absent."""
  # Malformed (non-dict) boundary entries are filtered, not fatal: optional
  # evidence must never discard an otherwise valid trajectory.
  boundaries = [
      b
      for b in (session.get("correction_boundaries", []) or [])
      if isinstance(b, dict)
  ]
  if not boundaries:
    return ""
  result = "\n=== Correction Evidence ===\n"
  for b in boundaries:
    result += (
        f"Turn {b.get('turn_index')}: Agent claimed:"
        f" \"{b.get('wrong_claim', '')}\"\n"
        f"  User corrected: \"{b.get('correct_fact', '')}\"\n"
        f"  Agent recovered: {b.get('agent_recovered', False)}\n"
    )
  return result


def _format_execution_subtrajectories(exec_subtraj) -> str:
  """Render per-segment execution traces, or '' when absent."""
  if not exec_subtraj:
    return ""
  result = "\n=== Execution sub-trajectories ===\n"
  result += (
      "Each segment shows agent routing/tool calls for that part of the"
      " conversation. Compare [-] (wrong) vs [+] (recovered) vs [~]"
      " (parroted) segments.\n\n"
  )
  for seg in exec_subtraj:
    outcome = seg.get("outcome", "")
    icon = _SEGMENT_OUTCOME_ICONS.get(outcome, "-")
    result += (
        f"--- [{icon}] {seg.get('label', '')} (turns"
        f" {seg.get('start_turn')}-{seg.get('end_turn')}) ->"
        f" {outcome} ---\n"
    )
    result += _coerce_trace_text(seg.get("trace", "") or "") + "\n\n"
  return result


def _format_execution_trace(session) -> str:
  """Render the full-session execution trace block, or '' when absent."""
  exec_trace = session.get("execution_trace", "")
  if not exec_trace:
    return ""
  exec_trace = _coerce_trace_text(exec_trace)
  return (
      "\n=== Execution trace ===\n"
      "Shows agent routing, tool calls, and LLM requests. Look for:"
      " missing tool calls, wrong routing, tool errors.\n\n" + exec_trace + "\n"
  )


def _format_session_enrichments(session) -> str:
  """Render host-captured evidence shared by BOTH session shapes.

  One renderer for the conversation and question/response branches of
  ``format_trajectory``: corrections, verifications, turn-boundary
  correction evidence, per-segment execution traces, brief segment
  outcomes, and the full-session execution trace. Single-turn sessions
  must not silently lose evidence the multi-turn branch renders.
  """
  result = ""
  if session.get("corrections"):
    result += f"User corrections: {session['corrections']}\n"
  if session.get("verifications"):
    result += f"User verification requests: {session['verifications']}\n"

  # Turn-boundary correction evidence, when the host's tagger extracts it:
  # the wrong claim, the user's correction, and whether the agent recovered.
  result += _format_correction_evidence(session)

  # Per-segment execution traces (hosts that capture routing/tool calls per
  # correction segment). Preferred over the brief sub-trajectory outcome
  # list because the analyst sees WHAT the agent executed in each segment.
  exec_subtraj = [
      seg
      for seg in (session.get("execution_sub_trajectories", []) or [])
      if isinstance(seg, dict)
  ]
  result += _format_execution_subtrajectories(exec_subtraj)

  # Surface the per-segment correction outcomes the turn tagger emits
  # (quality_report writes these as ``sub_trajectories``). A traced segment
  # makes its brief entry redundant (same labels plus executed evidence),
  # but ``execution_sub_trajectories`` can be PARTIAL -- the reference
  # producer's ``_segment_trace_by_turns`` skips segments it cannot align
  # to trace spans -- so brief entries with no traced counterpart (matched
  # on start/end turns) still render: a parroted outcome must never
  # disappear just because its segment lacked a trace.
  covered_spans = {
      (seg.get("start_turn"), seg.get("end_turn"))
      for seg in exec_subtraj
      if seg.get("start_turn") is not None or seg.get("end_turn") is not None
  }
  uncovered = [
      seg
      for seg in (session.get("sub_trajectories", []) or [])
      if isinstance(seg, dict)
      and (seg.get("start_turn"), seg.get("end_turn")) not in covered_spans
  ]
  if uncovered:
    result += "\n=== Correction sub-trajectories ===\n"
    for seg in uncovered:
      outcome = seg.get("outcome", "")
      icon = _SEGMENT_OUTCOME_ICONS.get(outcome, "-")
      span = ""
      if seg.get("start_turn") is not None and seg.get("end_turn") is not None:
        span = f" (turns {seg['start_turn']}-{seg['end_turn']})"
      result += f"[{icon}] {seg.get('label', '')}{span} -> {outcome}\n"

  # Full-session execution trace (single undivided trace), when captured.
  result += _format_execution_trace(session)
  return result


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
    result += _format_tool_calls(session)
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
    result += _format_session_enrichments(session)
    return result

  # Single-turn session shape (question/response, no conversation list). The
  # enrichment sections render here too: quality_report emits supported
  # sessions with question + response + evidence keys, and the analyst needs
  # the correction/routing/tool evidence regardless of session shape.
  base = (
      f"Question: {session.get('question', '')}\n"
      f"Response: {session.get('response', '')}\n"
      f"Agent: {session.get('answered_by', '')}\n"
      f"Verdict: {usefulness.get('category', '')}\n"
      f"Justification: {usefulness.get('justification', '')}\n"
      f"Grounding: {grounding.get('category', '')}\n"
      f"{_format_tool_calls(session)}"
  )
  enrichments = _format_session_enrichments(session)
  if enrichments:
    base = base.rstrip("\n") + "\n" + enrichments
  return base.rstrip("\n")


# ---------------------------------------------------------------------------
# Analysts
# ---------------------------------------------------------------------------

ROOT_CAUSE_CATEGORIES = frozenset(
    {
        "WRONG_TOOL",
        "MISSING_RULE",
        "AMBIGUITY",
        "SCOPE_GAP",
        "HALLUCINATION",
        "PARROTING",
        "CORRECTION_IGNORE",
        "RESPONSE_PATTERN",
        "DISAMBIGUATION",
        "TOOL_USAGE",
        "CORRECTION_RECOVERY",
    }
)


def run_analyst(
    client, model, system_prompt, session, current_skill, tools=None
):
  """Run one analyst on one trajectory. Returns patch text or None."""
  from google.genai import types

  trajectory = format_trajectory(session)
  tools_block = (
      f"<available_tools>\n{tools}\n</available_tools>\n\n" if tools else ""
  )
  prompt = (
      f"<current_skill>\n{current_skill}\n</current_skill>\n\n"
      f"{tools_block}"
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


def _quality_gate_reason(patch: str) -> Optional[str]:
  """Why a patch fails the quality gate, or None when it passes."""
  if len(patch.strip()) < 50:
    return "shorter than 50 chars"
  if not any(cat in patch for cat in ROOT_CAUSE_CATEGORIES):
    return "no recognized root-cause category token"
  if not ("## Root Cause" in patch or "## Pattern" in patch):
    return "missing '## Root Cause' / '## Pattern' marker"
  if not ("## Proposed Patch" in patch or "Content:" in patch):
    return "missing '## Proposed Patch' / 'Content:' marker"
  return None


def passes_quality_gate(patch: str) -> bool:
  """Reject patches lacking structure or a root-cause category."""
  return _quality_gate_reason(patch) is None


# ---------------------------------------------------------------------------
# Consolidation + guardrails
# ---------------------------------------------------------------------------


def strip_code_fences(text: str) -> str:
  """Strip a wrapping markdown code fence if the model added one.

  Handles two shapes:
  1. A fence wrapping the whole response (```markdown\\n...\\n```).
  2. An orphan opening fence the model inserts right after YAML frontmatter
     (``---\\n...\\n---\\n```\\n<body>``). The matching close is usually lost, so
     the unbalanced fence would otherwise render verbatim in the deployed skill.
  """
  if text.startswith("```"):
    lines = text.split("\n")
    lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
    stripped = "\n".join(lines).strip()
    return stripped or text
  # Orphan fence right after a YAML frontmatter block, with no matching close.
  match = re.match(r"(---\n.*?\n---\n)(.*)", text, re.DOTALL)
  if match:
    body = match.group(2)
    if body.count("```") % 2 == 1:
      new_body = re.sub(r"^\n*```[^\n]*\n", "", body, count=1)
      if new_body != body:
        return match.group(1) + new_body
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
    # Strict majority (> half): an exact 50/50 split is consensus for
    # NEITHER side, so a tie stays STRONG instead of VERY STRONG.
    if count >= 3 and count / total > 0.5:
      strength = "VERY STRONG"
    elif count >= 3:
      strength = "STRONG"
    elif count == 2:
      strength = "moderate"
    else:
      strength = "weak"
    lines.append(
        f"  {cat}: {count}/{total} ({round(count / total * 100)}%) -- {strength}"
    )
  lines.append(
      "Strength is a consensus flag: weak = 1 analyst, moderate = 2, STRONG ="
      " >=3 independently converged, VERY STRONG = >=3 and a strict majority"
      " (more than half) of all patches."
  )
  return "\n".join(lines)


def _write_evolution_artifacts(
    artifacts_dir,
    patches,
    candidates,
    selected,
    current_skill,
    version_label="v1",
    selection_note=None,
    patch_sources=None,
):
  """Persist the engine's intermediate reasoning for inspection/audit.

  When ``evolve_skill`` is given ``artifacts_dir`` it writes, under that dir
  (``<label>`` is ``version_label``, so a V1->V2 round writes ``v2_*``):
    - ``<label>_patches.json``  -- every analyst patch (source, root-cause
                                   category, and text)
    - ``<label>_candidates/``   -- each best-of-N consolidation candidate (the
                                   chosen one tagged ``_SELECTED``)
    - ``<label>_prevalence.txt``-- root-cause category tally across the patches
    - ``<label>_selection.txt`` -- one-line record of WHY this outcome (which
                                   candidate won, or why the incumbent was kept)
  """
  os.makedirs(artifacts_dir, exist_ok=True)

  if patch_sources is None:
    patch_sources = ["builtin"] * len(patches)
  if len(patch_sources) != len(patches):
    raise ValueError("patch_sources must align one-to-one with patches")
  unexpected_sources = set(patch_sources) - {"builtin", "host"}
  if unexpected_sources:
    raise ValueError(
        "patch_sources entries must be 'builtin' or 'host'; got"
        f" {sorted(unexpected_sources)!r}"
    )

  records = []
  for i, (patch, source) in enumerate(zip(patches, patch_sources)):
    match = re.search(r"## Root Cause\s*\n\s*\[?(\w+)\]?", patch) or re.search(
        r"## Pattern\s*\n\s*\[?(\w+)\]?", patch
    )
    records.append(
        {
            "index": i + 1,
            "source": source,
            "category": match.group(1) if match else None,
            "patch": patch,
        }
    )
  patches_path = os.path.join(artifacts_dir, f"{version_label}_patches.json")
  with open(patches_path, "w") as f:
    json.dump(records, f, indent=2)
    f.write("\n")

  cand_dir = os.path.join(artifacts_dir, f"{version_label}_candidates")
  os.makedirs(cand_dir, exist_ok=True)
  kept = [c for c in candidates if c and c != current_skill]
  for i, cand in enumerate(kept):
    tag = "_SELECTED" if cand == selected else ""
    path = os.path.join(cand_dir, f"candidate_{i + 1}{tag}.md")
    with open(path, "w") as f:
      f.write(cand if cand.endswith("\n") else cand + "\n")

  prevalence = compute_prevalence_summary(patches)
  if prevalence:
    prevalence_path = os.path.join(
        artifacts_dir, f"{version_label}_prevalence.txt"
    )
    with open(prevalence_path, "w") as f:
      f.write(prevalence + "\n")

  # Audit trail of the selection outcome -- especially the incumbent guard
  # firing ("kept incumbent: ..."), which otherwise leaves no artifact at all.
  if selection_note:
    selection_path = os.path.join(
        artifacts_dir, f"{version_label}_selection.txt"
    )
    with open(selection_path, "w") as f:
      f.write(selection_note + "\n")

  logger.info(
      "Wrote evolution artifacts to %s (%d patches, %d candidates).",
      artifacts_dir,
      len(records),
      len(kept),
  )


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


class _FleetSlots:
  """Concurrency accounting for the analyst fleet.

  ``max_workers`` semaphore slots gate thread CREATION (the dispatcher
  acquires a slot before starting a call's thread, so live threads track
  ``max_workers``, not report size). A quarantined call -- timed out but
  still executing, since Python threads cannot be killed -- may DONATE its
  slot to the next queued analyst so one hung analyst does not starve the
  fleet, but donations are budgeted at ``max_workers``: once that many
  quarantined callables are running on donated slots, further timeouts keep
  their slot until the callable actually returns. Total live analyst
  callables therefore never exceed ``2 * max_workers``, regardless of how
  many analysts time out.
  """

  def __init__(self, max_workers: int):
    self._sem = threading.BoundedSemaphore(max_workers)
    self._lock = threading.Lock()
    self._donated = 0
    self._max_donated = max_workers

  def acquire(self):
    self._sem.acquire()

  def release(self):
    self._sem.release()

  def try_donate(self) -> bool:
    """Hand a quarantined RUNNING call's slot to the next queued analyst.

    Refused once ``max_workers`` quarantined callables already run on
    donated slots; the caller then keeps its slot until the callable
    returns, preserving the 2x ``max_workers`` live-callable ceiling.
    """
    with self._lock:
      if self._donated >= self._max_donated:
        return False
      self._donated += 1
    self._sem.release()
    return True

  def finish(self, donated: bool):
    """A callable returned: free its slot (or retire its donation)."""
    if donated:
      with self._lock:
        self._donated -= 1
    else:
      self._sem.release()


class _AnalystCall:
  """One lazily dispatched analyst invocation with quarantine-on-timeout.

  A ThreadPoolExecutor was rejected twice over: a future that times out
  keeps occupying its pool thread (with ``max_workers=1`` a single hung
  analyst starves every queued analyst, and a healthy fleet reads as
  all-host-failures), and its non-daemon threads block process exit on a
  truly hung callable. Here the dispatcher starts each call on its own
  daemon thread only AFTER acquiring a ``_FleetSlots`` slot. ``wait``
  returning False abandons the call: a still-QUEUED call is cancelled
  outright (the dispatcher returns its slot untouched), and a RUNNING call
  is quarantined -- it hands its slot on only within the bounded donation
  budget (see ``_FleetSlots``), so timeouts can never fan the fleet out
  past ``2 * max_workers`` live callables. Quarantined threads are daemons:
  they may run to completion in the background but cannot delay process
  exit, and they free their slot or donation the moment they return.
  """

  def __init__(self, fn, args):
    self._fn = fn
    self._args = args
    self._lock = threading.Lock()
    self._done = threading.Event()
    self._state = "queued"  # queued -> running -> done | queued -> cancelled
    self._donated = False
    self.timeout_reason: Optional[str] = None
    self.result = None
    self.error: Optional[BaseException] = None

  def try_start(self, slots: _FleetSlots) -> bool:
    """Dispatcher only: start the callable on a fresh daemon thread.

    Returns False without starting when the call was cancelled while
    queued; the dispatcher then returns the slot it acquired.
    """
    with self._lock:
      if self._state == "cancelled":
        return False
      self._state = "running"
    threading.Thread(target=self._run, args=(slots,), daemon=True).start()
    return True

  def _run(self, slots: _FleetSlots):
    try:
      self.result = self._fn(*self._args)
    except BaseException as e:  # noqa: BLE001 - reported via self.error
      self.error = e
    with self._lock:
      self._state = "done"
      donated = self._donated
    self._done.set()
    slots.finish(donated)

  def wait(self, timeout: Optional[float], slots: _FleetSlots) -> bool:
    """True when finished; on timeout, cancel or quarantine (see class doc)."""
    if self._done.wait(timeout):
      return True
    with self._lock:
      if self._state == "done":
        return True  # finished in the race window between wait and lock
      if self._state == "queued":
        # Never started: every slot spent its window held by earlier
        # (possibly quarantined) analysts. Cancel outright -- no thread is
        # ever created for this call.
        self._state = "cancelled"
        self.timeout_reason = (
            "cancelled unstarted; all worker slots stayed busy for the"
            " full timeout window"
        )
        return False
      self._donated = slots.try_donate()
      self.timeout_reason = (
          "quarantined; the slot was handed to the next queued analyst"
          if self._donated
          else "quarantined; donation budget exhausted, the slot stays"
          " held until the callable returns"
      )
    return False


def _dispatch_analysts(calls: list["_AnalystCall"], slots: _FleetSlots):
  """Dispatcher loop (own daemon thread): start calls as slots free up.

  Threads are created only after a slot is acquired, so thread allocation
  is bounded by ``max_workers`` (plus the bounded donation budget), never
  by report size.
  """
  for call in calls:
    slots.acquire()
    if not call.try_start(slots):
      slots.release()  # cancelled while queued; slot goes straight back


def _validate_max_workers(max_workers) -> None:
  """Reject unusable worker counts BEFORE any dispatch or model spend.

  ``max_workers=0`` would otherwise deadlock the dispatcher in ``acquire()``
  and hang ``collect_patches`` forever under the default ``wait(None)``.
  """
  if (
      isinstance(max_workers, bool)
      or not isinstance(max_workers, int)
      or max_workers < 1
  ):
    raise ValueError(
        f"max_workers must be a positive integer (got {max_workers!r}):"
        " zero or negative workers would leave every analyst queued"
        " forever."
    )


def collect_patches(
    report,
    current_skill,
    *,
    client,
    model,
    max_workers=10,
    max_success_samples=15,
    analyst_mode="both",
    tools=None,
    error_analyst_fn: Optional[ErrorAnalystFn] = None,
    analyst_timeout_s: Optional[float] = None,
):
  """Run the analyst fleet over the report. Returns the list of kept patches.

  Args:
    error_analyst_fn: Optional replacement analyst for FAILURE trajectories,
      called as ``fn(client, model, session, current_skill, tools)`` and
      returning patch text or None. Lets a host plug in a richer analyst
      (e.g. an agentic investigator with tool access) while keeping the
      fleet dispatch, quality gate, and consolidation from this engine.
      Success trajectories always use the built-in single-pass analyst.
      The required patch ENVELOPE (enforced by the quality gate, rejects
      logged with their reason): a str of >= 50 chars containing one of the
      ``ROOT_CAUSE_CATEGORIES`` tokens, a ``## Root Cause`` (or
      ``## Pattern``) section, and a ``## Proposed Patch`` (or ``Content:``)
      section -- the same shape ``ERROR_ANALYST_PROMPT`` instructs the
      built-in analyst to produce. ``None`` is the ONLY valid no-patch
      sentinel: any non-string return -- truthy or falsy (``False``, ``0``,
      ``[]``, ``{}``) -- and any empty or whitespace-only string is dropped
      with a warning and counts as a host failure. If EVERY host call
      fails, collect_patches raises RuntimeError instead of degrading a
      broken host analyst into a clean-looking zero-patch run (partial
      failures stay tolerated). Host analysts must treat
      ``correction_boundaries[*].correct_fact`` as an unverified user
      hypothesis: verify it with available tools and never copy it into a
      patch as fact.
    analyst_timeout_s: Optional per-analyst timeout in seconds. A call
      exceeding it is treated like any other analyst failure (warning,
      skipped) and QUARANTINED: its daemon thread may linger until the
      callable returns, but it hands its concurrency slot to the next
      queued analyst so one hung analyst cannot starve the rest of the
      fleet even at ``max_workers=1``. Slot hand-offs are BUDGETED (see
      ``_FleetSlots``): threads are created lazily as slots free up, at
      most ``max_workers`` quarantined callables may run on handed-off
      slots at once, and further timeouts keep their slot until the
      callable returns -- live analyst callables never exceed
      ``2 * max_workers`` regardless of report size. The bound is PER
      ANALYST, applied in submission order -- the worst-case total wait for
      a wholly hung fleet is N x analyst_timeout_s, not analyst_timeout_s
      overall. Default None (no timeout; see issue #397).
  """
  _validate_max_workers(max_workers)
  if client is None and not (
      error_analyst_fn is not None and analyst_mode == "error-only"
  ):
    raise ValueError(
        "client is required unless error_analyst_fn is set AND"
        " analyst_mode='error-only': every built-in analyst call would fail"
        " inside its future and be swallowed as warnings, silently dropping"
        " all patches after the fleet has run."
    )
  successes, failures = partition_trajectories(report)
  logger.info(
      "Trajectories: %d successes, %d failures", len(successes), len(failures)
  )
  if analyst_mode == "error-only":
    successes = []
  elif analyst_mode == "success-only":
    failures = []
  successes = successes[:max_success_samples]

  patches: list[tuple[str, str]] = []
  # Dispatch goes through _AnalystCall + a lazy dispatcher thread rather than
  # a ThreadPoolExecutor: a timed-out call is quarantined and its slot handed
  # to the next queued analyst (within the bounded donation budget), so one
  # hung analyst cannot starve the fleet or fake an all-host-failures run,
  # while live threads stay bounded by max_workers, not report size.
  slots = _FleetSlots(max_workers)
  calls: list[tuple[_AnalystCall, str, str]] = []
  for s in failures:
    if error_analyst_fn is not None:
      call = _AnalystCall(
          error_analyst_fn, (client, model, s, current_skill, tools)
      )
    else:
      call = _AnalystCall(
          run_analyst,
          (client, model, ERROR_ANALYST_PROMPT, s, current_skill, tools),
      )
    calls.append((call, "error", (s.get("question", "") or "")[:60]))
  for s in successes:
    call = _AnalystCall(
        run_analyst,
        (client, model, SUCCESS_ANALYST_PROMPT, s, current_skill, tools),
    )
    calls.append((call, "success", (s.get("question", "") or "")[:60]))
  threading.Thread(
      target=_dispatch_analysts,
      args=([c for c, _, _ in calls], slots),
      daemon=True,
  ).start()

  host_total = host_failed = 0
  first_host_error: Optional[BaseException] = None
  # Sequential waits in submission order so the per-analyst timeout bounds
  # OUR wait; each quarantined call frees its slot, so a queued analyst
  # behind a hung one always gets a fresh full timeout window.
  for call, kind, question in calls:
    is_host = error_analyst_fn is not None and kind == "error"
    if is_host:
      host_total += 1
    error: Optional[BaseException] = None
    if not call.wait(analyst_timeout_s, slots):
      error = TimeoutError(
          f"timed out after {analyst_timeout_s}s ({call.timeout_reason})"
      )
    elif call.error is not None:
      error = call.error
    if error is not None:
      if is_host:
        host_failed += 1
        if first_host_error is None:
          first_host_error = error
      logger.warning("analyst [%s] %s failed: %s", kind, question, error)
      continue
    result = call.result
    if result is None:
      # None is the ONLY valid no-patch sentinel.
      continue
    if not isinstance(result, str):
      # Contract violation, not a quality issue -- and falsy non-strings
      # (False, 0, [], {}) are violations too, not no-patch results. Counts
      # toward the all-host-failures guard, so a host returning dicts for
      # every session cannot masquerade as a healthy zero-patch run.
      if is_host:
        host_failed += 1
        if first_host_error is None:
          first_host_error = TypeError(
              f"error_analyst_fn returned {type(result).__name__},"
              " expected patch text or None"
          )
      logger.warning(
          "analyst [%s] %s returned %s instead of patch text; dropping"
          " (the analyst contract is 'patch text or None').",
          kind,
          question,
          type(result).__name__,
      )
      continue
    if not result.strip():
      # Empty/whitespace-only strings are contract violations too: None is
      # the ONLY no-patch sentinel, and blank text is not gate-able patch
      # text. Count them toward the all-host-failures guard so an all-blank
      # host cannot masquerade as a healthy zero-patch run.
      if is_host:
        host_failed += 1
        if first_host_error is None:
          first_host_error = ValueError(
              "error_analyst_fn returned an empty/whitespace-only string;"
              " the only valid no-patch sentinel is None"
          )
        logger.warning(
            "analyst [%s] %s returned an empty/whitespace-only string;"
            " dropping (the no-patch sentinel is None, not '').",
            kind,
            question,
        )
      continue
    patches.append((result, "host" if is_host else "builtin"))

  if error_analyst_fn is not None and host_total and host_failed == host_total:
    raise RuntimeError(
        "every host error_analyst_fn call failed or returned an unusable"
        f" result ({host_failed}/{host_total}); first error:"
        f" {first_host_error!r}. Refusing to degrade a broken host analyst"
        " into a clean-looking zero-patch run (partial failures are"
        " tolerated)."
    )

  kept = []
  provenance = _PATCH_PROVENANCE.get()
  for patch, source in patches:
    reason = _quality_gate_reason(patch)
    if reason is None:
      # Each accepted occurrence needs its own identity: host and builtin
      # analysts may return the same cached string object. Prefix-and-slice
      # creates an equal, exact ``str`` without encoding assumptions.
      kept_patch = (" " + patch)[1:]
      kept.append(kept_patch)
      if provenance is not None:
        # Keep a strong reference for the map's lifetime so a wrapper can
        # discard this patch without letting Python recycle its ID for a later
        # collection. The identity check at lookup closes the other half of
        # that invariant.
        provenance[id(kept_patch)] = (kept_patch, source)
    else:
      logger.warning("Quality gate rejected a patch (%s): %.80r", reason, patch)
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


def _validate_incumbent_score(
    incumbent_score, score_fn, *, warn_ungated: bool = True
) -> None:
  """Reject unusable incumbent baselines BEFORE any model spend.

  Raises ValueError for a non-finite ``incumbent_score`` (NaN/inf make every
  margin comparison False and silently disable the incumbent guard) and warns
  when a baseline is supplied without a ``score_fn`` (the guard only runs
  with one, so selection would be the ungated median-size candidate).
  """
  if incumbent_score is None:
    return
  if not math.isfinite(incumbent_score):
    raise ValueError(
        f"incumbent_score must be finite (got {incumbent_score!r}): a"
        " NaN/inf baseline makes every margin comparison False and would"
        " silently disable the incumbent guard."
    )
  if score_fn is None and warn_ungated:
    logger.warning(
        "incumbent_score=%.3f was provided but score_fn is None -- the"
        " incumbent guard only runs with a score_fn, so selection falls"
        " back to the UNGATED median-size candidate.",
        incumbent_score,
    )


def select_candidate(
    viable: list,
    current_skill: str,
    score_fn: Optional[Callable[[str], float]] = None,
    min_improvement: float = 0.5,
    incumbent_score: Optional[float] = None,
) -> str:
  """Pick which evolved candidate to ship (pure; no model calls).

  - No viable candidate -> keep the base skill.
  - No ``score_fn`` -> return the median-size viable candidate.
  - With ``score_fn`` -> return the highest-scoring candidate ONLY if it beats
    the incumbent by at least ``min_improvement``; otherwise keep the base skill.

  ``incumbent_score``, when given, is used as the incumbent's score instead of
  calling ``score_fn(current_skill)``. Hosts that already measured the base
  skill (e.g. the quality report the evolution run consumed) pass it to avoid
  re-scoring the incumbent on fresh, noisy traffic. It has effect ONLY when
  ``score_fn`` is also provided -- without one, selection is the ungated
  median-size candidate and a warning is logged. Raises ``ValueError`` for a
  non-finite ``incumbent_score`` AND for a non-finite candidate score
  (NaN/inf on either side would silently defeat the gate).

  The last rule is the restraint property of a self-modifying system: when
  nothing clearly improves, leave the already-good skill alone.
  """
  _validate_incumbent_score(incumbent_score, score_fn)

  if not viable:
    logger.warning("No viable candidate passed guardrails; keeping base skill.")
    return current_skill

  if score_fn is None:
    ordered = sorted(viable, key=len)
    selected = ordered[len(ordered) // 2]
    logger.info("Selected median-size candidate (%d chars).", len(selected))
    return selected

  incumbent = (
      incumbent_score
      if incumbent_score is not None
      else score_fn(current_skill)
  )
  if not math.isfinite(incumbent):
    raise ValueError(
        f"score_fn(current_skill) returned a non-finite incumbent"
        f" ({incumbent!r}); the margin gate cannot run against it."
    )
  best, best_score = None, float("-inf")
  for cand in viable:
    score = score_fn(cand)
    if not math.isfinite(score):
      raise ValueError(
          f"score_fn returned a non-finite candidate score ({score!r}); an"
          " inf/NaN candidate would defeat the same improvement gate that"
          " already rejects non-finite incumbent scores."
      )
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
    incumbent_score: Optional[float] = None,
    tools: Optional[str] = None,
    error_analyst_fn: Optional[ErrorAnalystFn] = None,
    analyst_timeout_s: Optional[float] = None,
    artifacts_dir: Optional[str] = None,
    version_label: str = "v1",
    client=None,
) -> str:
  """Evolve a SKILL.md from a scored quality report.

  Args:
    report: A quality report dict (or a path to its JSON). Must contain
      ``sessions`` with ``metrics.response_usefulness.category`` and either a
      ``conversation`` or ``question``/``response`` per session. Sessions may
      carry optional enrichment keys the analysts render when present (see
      ``scripts/quality_report.py`` for the reference producer):
      ``verifications`` (int count of user verification requests),
      ``correction_boundaries`` (list of {turn_index, wrong_claim,
      correct_fact, agent_recovered}), ``sub_trajectories`` (turn-tagger
      segments: {label, outcome, start_turn, end_turn}),
      ``execution_sub_trajectories`` (same shape plus a per-segment
      ``trace`` string of executed routing/tool calls), and
      ``execution_trace`` (one full-session trace string).
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
    incumbent_score: Pre-measured score of ``current_skill`` (in score_fn
      units). When given, the incumbent guard uses it instead of re-scoring
      the base skill via ``score_fn(current_skill)``.
    error_analyst_fn: Optional replacement analyst for failure trajectories;
      see ``collect_patches`` for the full contract, including the required
      patch envelope and the all-host-failures RuntimeError.
    analyst_timeout_s: Optional per-analyst timeout in seconds (see
      ``collect_patches``).
    artifacts_dir: If set, write the analyst patches, the best-of-N candidates,
      a prevalence summary, and a one-line selection record here (for
      inspection/audit) before returning.
    version_label: Prefix for the artifact filenames (default ``"v1"``). Pass
      the round being PRODUCED -- e.g. ``"v2"`` for a V1->V2 run -- so
      successive rounds don't overwrite each other's artifacts.
    client: Optional pre-built google-genai Client (else one is created).

  Returns:
    The evolved SKILL.md content, or the unchanged ``current_skill`` if no
    improvement was found.
  """
  # Raise on an unusable baseline or worker count BEFORE any spend; the
  # UNGATED warning is left to select_candidate so it logs exactly once per
  # run.
  _validate_incumbent_score(incumbent_score, score_fn, warn_ungated=False)
  _validate_max_workers(max_workers)
  if isinstance(report, str):
    with open(report) as f:
      report = json.load(f)
  summary = report.get("summary", {})
  # A client is ALWAYS required here: even in hosted error-only analyst mode
  # the consolidator uses it. The client-free path collect_patches supports
  # applies to standalone collect_patches usage only.
  client = client or _make_client(project, location)

  patch_provenance: dict[int, tuple[str, str]] = {}
  provenance_token = _PATCH_PROVENANCE.set(patch_provenance)
  try:
    patches = collect_patches(
        report,
        current_skill,
        client=client,
        model=model,
        max_workers=max_workers,
        max_success_samples=max_success_samples,
        analyst_mode=analyst_mode,
        tools=tools,
        error_analyst_fn=error_analyst_fn,
        analyst_timeout_s=analyst_timeout_s,
    )
  finally:
    _PATCH_PROVENANCE.reset(provenance_token)
  if not patches:
    logger.warning("No patches to consolidate; returning the current skill.")
    return current_skill
  # Identity-retaining wrappers resolve each occurrence exactly, even when they
  # reorder or filter equal patch strings. For wrappers that reconstruct the
  # strings (for example, via serialization), fall back to text only when every
  # recorded occurrence of that text has the same source. Plain strings from a
  # legacy replacement and ambiguous equal-text copies remain conservative.
  recorded_sources_by_patch: dict[str, set[str]] = {}
  for recorded_patch, recorded_source in patch_provenance.values():
    recorded_sources_by_patch.setdefault(recorded_patch, set()).add(
        recorded_source
    )
  patch_sources = []
  for patch in patches:
    provenance_entry = patch_provenance.get(id(patch))
    if provenance_entry is not None and provenance_entry[0] is patch:
      source = provenance_entry[1]
    else:
      matching_sources = recorded_sources_by_patch.get(patch, set())
      source = (
          next(iter(matching_sources))
          if len(matching_sources) == 1
          else "builtin"
      )
    patch_sources.append(source)

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
  selected = select_candidate(
      viable, current_skill, score_fn, min_improvement, incumbent_score
  )
  if artifacts_dir:
    # Reconstruct WHY this outcome, so the incumbent guard leaves an audit
    # trail (selection.txt) instead of firing silently.
    if selected == current_skill:
      if not viable:
        selection_note = (
            "kept incumbent: no viable candidate passed the structural"
            " guardrails"
        )
      else:
        selection_note = (
            f"kept incumbent: none of the {len(viable)} viable candidate(s)"
            f" beat the incumbent score by the {min_improvement} margin"
        )
    else:
      picked = viable.index(selected) + 1
      selection_note = (
          f"selected candidate {picked} of {len(viable)} viable"
          f" ({len(selected)} chars,"
          f" {'scored best' if score_fn else 'median size'})"
      )
    # Write the *sanitized* viable candidates (the same pool `selected` came
    # from), so the `_SELECTED` tag attaches correctly and the saved candidate
    # is byte-for-byte the returned skill -- raw `cands` could differ after
    # sanitize_adk_vars().
    _write_evolution_artifacts(
        artifacts_dir,
        patches,
        viable,
        selected,
        current_skill,
        version_label=version_label,
        selection_note=selection_note,
        patch_sources=patch_sources,
    )
  return selected


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
  ap.add_argument(
      "--tools",
      default=None,
      help=(
          "Freeform description of the agent's tools, shown to the analysts so"
          " they can propose 'use the tool' rules instead of NO_PATCH on"
          " deflections. Overridden by --eval-spec's `tools` field if both given."
      ),
  )
  ap.add_argument(
      "--eval-spec",
      default=None,
      help="eval_spec.json to read the `tools` field from (see --tools)",
  )
  ap.add_argument(
      "--artifacts-dir",
      default=None,
      help=(
          "Directory to write analyst patches, best-of-N candidates, the"
          " prevalence tally, and the selection record into (for audit)"
      ),
  )
  args = ap.parse_args()

  tools = args.tools
  if args.eval_spec:
    if not os.path.exists(args.eval_spec):
      ap.error(f"--eval-spec not found: {args.eval_spec}")
    with open(args.eval_spec) as f:
      tools = (json.load(f) or {}).get("tools") or tools

  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s [%(levelname)s] %(message)s",
      datefmt="%H:%M:%S",
  )
  for noisy in ("google.genai", "google_genai", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

  if args.eval_spec and not tools:
    # Library CLI stays lenient (runs without tool awareness) but warns; the
    # lab wrapper (analyze_and_evolve.py) fail-fasts on the same condition.
    # Emitted after basicConfig: a module-level logging call before it would
    # install an implicit WARNING-level root handler and mute every INFO log.
    logging.warning(
        "--eval-spec %s has no `tools` field; analysts will run WITHOUT tool"
        " awareness.",
        args.eval_spec,
    )

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
      tools=tools,
      artifacts_dir=args.artifacts_dir,
  )
  with open(args.output, "w") as f:
    f.write(evolved)
  changed = "unchanged" if evolved == current_skill else "evolved"
  print(f"{changed}: wrote {len(evolved)} chars to {args.output}")


if __name__ == "__main__":
  _main()
