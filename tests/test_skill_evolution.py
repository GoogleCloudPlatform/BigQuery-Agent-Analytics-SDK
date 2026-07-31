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

"""Unit tests for the pure helpers in scripts/skill_evolution.py.

These cover trajectory partitioning, formatting, the patch quality gate, the
consolidation guardrails, and fence/var sanitization. They do not make any
network calls (the google-genai import is lazy, inside the API functions).
"""

import json
import os
import sys

# Make scripts/ importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from skill_evolution import _has_parroted_recovery  # noqa: E402
from skill_evolution import _write_evolution_artifacts
from skill_evolution import collect_patches
from skill_evolution import compute_prevalence_summary
from skill_evolution import format_trajectory
from skill_evolution import partition_trajectories
from skill_evolution import passes_quality_gate
from skill_evolution import sanitize_adk_vars
from skill_evolution import select_candidate
from skill_evolution import strip_code_fences
from skill_evolution import validate_evolved_skill


def _session(category, **extra):
  s = {"metrics": {"response_usefulness": {"category": category}}}
  s.update(extra)
  return s


# --- partition_trajectories -------------------------------------------------


def test_partition_splits_success_and_failure():
  report = {
      "sessions": [
          _session("meaningful", question="a"),
          _session("declined", question="b"),
          _session("unhelpful", question="c"),
          _session("partial", question="d"),
      ]
  }
  successes, failures = partition_trajectories(report)
  assert [s["question"] for s in successes] == ["a", "b"]
  assert [s["question"] for s in failures] == ["c", "d"]


def test_partition_reclassifies_parroted_recovery_as_failure():
  s = _session(
      "meaningful",
      question="p",
      sub_trajectories=[{"outcome": "parroted"}],
  )
  successes, failures = partition_trajectories({"sessions": [s]})
  assert not successes
  assert failures and failures[0]["question"] == "p"


def test_partition_ignores_unknown_categories():
  report = {"sessions": [_session("unknown"), _session("")]}
  successes, failures = partition_trajectories(report)
  assert not successes and not failures


def test_has_parroted_recovery():
  assert _has_parroted_recovery({"sub_trajectories": [{"outcome": "parroted"}]})
  assert not _has_parroted_recovery(
      {"sub_trajectories": [{"outcome": "recovered"}]}
  )
  assert not _has_parroted_recovery({})


def test_has_parroted_recovery_in_execution_sub_trajectories():
  # Hosts that segment execution traces per correction mark the outcome on
  # execution_sub_trajectories; a parroted segment there reclassifies too.
  assert _has_parroted_recovery(
      {"execution_sub_trajectories": [{"outcome": "parroted"}]}
  )
  assert not _has_parroted_recovery(
      {"execution_sub_trajectories": [{"outcome": "recovered"}]}
  )


# --- format_trajectory ------------------------------------------------------


def test_format_single_turn():
  s = _session(
      "unhelpful", question="How many PTO days?", response="contact HR"
  )
  out = format_trajectory(s)
  assert "How many PTO days?" in out
  assert "contact HR" in out
  assert "Verdict: unhelpful" in out


def test_format_multi_turn_with_tags():
  s = _session(
      "unhelpful",
      conversation=[
          {"role": "user", "text": "is it 25 days?", "tag": "CORRECTION"},
          {"role": "assistant", "text": "yes"},
      ],
  )
  out = format_trajectory(s)
  assert "=== Conversation ===" in out
  assert "[CORRECTION]" in out
  assert "is it 25 days?" in out


def test_format_renders_subtrajectory_outcomes():
  # Real sub_trajectories shape from quality_report (label/outcome/start_turn/
  # end_turn, no `trace` field). The per-segment parrot/recover outcome must
  # reach the analyst text -- this is the PARROTING evidence the prompt uses.
  s = _session(
      "meaningful",
      conversation=[
          {"role": "user", "text": "is it 25 days?", "tag": "CORRECTION"},
          {"role": "assistant", "text": "yes, 25"},
      ],
      sub_trajectories=[
          {
              "label": "post_correction_1",
              "outcome": "parroted",
              "start_turn": 1,
              "end_turn": 2,
          }
      ],
  )
  out = format_trajectory(s)
  assert "parroted" in out
  assert "post_correction_1" in out
  assert "[~]" in out  # parroted icon


def test_format_renders_execution_sub_trajectories_with_traces():
  # When per-segment execution traces exist, the analyst must see WHAT the
  # agent executed in each segment (preferred over the brief outcome list).
  s = _session(
      "unhelpful",
      conversation=[
          {"role": "user", "text": "is it 25 days?", "tag": "CORRECTION"},
          {"role": "assistant", "text": "yes, 25"},
      ],
      execution_sub_trajectories=[
          {
              "label": "post_correction_1",
              "outcome": "parroted",
              "start_turn": 1,
              "end_turn": 2,
              "trace": "agent->policy_agent (no tool call)",
          }
      ],
  )
  out = format_trajectory(s)
  assert "=== Execution sub-trajectories ===" in out
  assert "[~]" in out
  assert "agent->policy_agent (no tool call)" in out


def test_format_renders_correction_evidence_and_verifications():
  s = _session(
      "unhelpful",
      conversation=[
          {"role": "user", "text": "PTO is 25 days", "tag": "CORRECTION"},
          {"role": "assistant", "text": "you are right"},
      ],
      corrections=1,
      verifications=2,
      correction_boundaries=[
          {
              "turn_index": 1,
              "wrong_claim": "PTO is 20 days",
              "correct_fact": "PTO is 25 days",
              "agent_recovered": False,
          }
      ],
  )
  out = format_trajectory(s)
  assert "User verification requests: 2" in out
  assert "=== Correction Evidence ===" in out
  assert "PTO is 20 days" in out
  assert "Agent recovered: False" in out


def test_format_renders_full_session_execution_trace():
  # A single undivided trace renders when no per-segment traces exist.
  s = _session(
      "unhelpful",
      conversation=[{"role": "user", "text": "q"}],
      execution_trace="invoke supervisor -> transfer policy_agent",
  )
  out = format_trajectory(s)
  assert "=== Execution trace ===" in out
  assert "invoke supervisor -> transfer policy_agent" in out


def test_format_renders_tool_calls_single_turn():
  # A deflection that named the wrong tool must be visible to the analyst so it
  # can propose a TOOL_USAGE/WRONG_TOOL rule from observed behavior.
  s = _session(
      "unhelpful",
      question="what's my STD payout?",
      response="contact HR",
      tool_calls_detail=[
          {"name": "lookup_company_policy", "args": {"topic": "std"}}
      ],
  )
  out = format_trajectory(s)
  assert "Tool calls:" in out
  assert "lookup_company_policy" in out
  assert "std" in out


def test_format_renders_no_tool_calls_when_empty():
  s = _session("unhelpful", question="q", response="r", tool_calls_detail=[])
  assert "Tool calls: (none)" in format_trajectory(s)


def test_format_omits_tool_calls_for_legacy_sessions():
  # Sessions scored before tool_calls_detail existed carry no such key.
  s = _session("unhelpful", question="q", response="r")
  assert "Tool calls" not in format_trajectory(s)


def test_format_says_nothing_when_calls_happened_but_detail_missing():
  # BQ-path: tool_calls counted from trace spans, no structured detail. Must NOT
  # render a false "(none)" -- say nothing instead.
  s = _session(
      "unhelpful",
      question="q",
      response="r",
      tool_calls_detail=[],
      tool_calls=3,
  )
  assert "Tool calls" not in format_trajectory(s)


# --- passes_quality_gate ----------------------------------------------------


def test_quality_gate_accepts_structured_patch():
  patch = (
      "## Root Cause\n[HALLUCINATION]: answered from memory\n\n"
      "## Proposed Patch\nContent:\nAlways call the tool before answering."
  )
  assert passes_quality_gate(patch)


def test_quality_gate_rejects_unstructured_or_short():
  assert not passes_quality_gate("too short")
  assert not passes_quality_gate(
      "## Root Cause\n[HALLUCINATION]: x\n" + "filler " * 20
  )  # missing Proposed Patch / Content
  assert not passes_quality_gate(
      "## Random\nno recognized category here at all, just prose " * 3
  )


# --- strip_code_fences ------------------------------------------------------


def test_strip_code_fences_removes_wrapper():
  assert strip_code_fences("```markdown\nhello\n```") == "hello"
  assert strip_code_fences("```\nhello\nworld\n```") == "hello\nworld"


def test_strip_code_fences_noop_without_fence():
  assert strip_code_fences("plain text") == "plain text"


def test_strip_code_fences_keeps_original_if_empty():
  assert strip_code_fences("```\n```") == "```\n```"


def test_strip_code_fences_removes_orphan_fence_after_frontmatter():
  text = '---\nname: x\nmetadata:\n  version: "1"\n---\n```\n\nBody line.\n'
  assert (
      strip_code_fences(text)
      == '---\nname: x\nmetadata:\n  version: "1"\n---\n\nBody line.\n'
  )


def test_strip_code_fences_keeps_balanced_fence_after_frontmatter():
  # A real, balanced code block in the body must be preserved.
  text = "---\nname: x\n---\nBody.\n\n```bash\nrun it\n```\n"
  assert strip_code_fences(text) == text


# --- sanitize_adk_vars ------------------------------------------------------


def test_sanitize_escapes_braces():
  assert (
      sanitize_adk_vars("use {requested_topic} here")
      == "use <requested_topic> here"
  )


def test_sanitize_noop_without_braces():
  assert sanitize_adk_vars("no braces here") == "no braces here"


# --- compute_prevalence_summary --------------------------------------------


def test_prevalence_counts_categories():
  patches = [
      "## Root Cause\n[HALLUCINATION]: a",
      "## Root Cause\n[HALLUCINATION]: b",
      "## Pattern\n[TOOL_USAGE]: c",
  ]
  out = compute_prevalence_summary(patches)
  assert "HALLUCINATION: 2/3" in out
  assert "STRONG" in out or "moderate" in out
  assert "consensus flag" in out


def test_prevalence_very_strong_needs_consensus_and_majority():
  majority = ["## Root Cause\n[TOOL_USAGE]: x"] * 3 + [
      "## Root Cause\n[PARROTING]: y"
  ]
  out = compute_prevalence_summary(majority)
  assert "TOOL_USAGE: 3/4 (75%) -- VERY STRONG" in out
  assert "PARROTING: 1/4 (25%) -- weak" in out

  minority = ["## Root Cause\n[TOOL_USAGE]: x"] * 3 + [
      "## Root Cause\n[PARROTING]: y"
  ] * 7
  out = compute_prevalence_summary(minority)
  assert "TOOL_USAGE: 3/10 (30%) -- STRONG" in out
  assert "PARROTING: 7/10 (70%) -- VERY STRONG" in out


def test_prevalence_empty_when_no_categories():
  assert compute_prevalence_summary(["just prose", "more prose"]) == ""


# --- validate_evolved_skill -------------------------------------------------

_BASE = (
    '---\nname: x\ndescription: y\nmetadata:\n  version: "0"\n---\n\n'
    "## A\nrule a\n\n## B\nrule b\n"
)


def test_validate_accepts_superset_with_sections_preserved():
  evolved = _BASE + "\n## C\nnew rule c\n"
  assert validate_evolved_skill(evolved, _BASE) == []


def test_validate_flags_dropped_section():
  evolved = (
      '---\nname: x\ndescription: y\nmetadata:\n  version: "1"\n---\n\n'
      "## A\nrule a kept and expanded with extra words to exceed base size.....\n"
  )  # dropped '## B'
  issues = validate_evolved_skill(evolved, _BASE)
  assert any("Dropped" in i for i in issues)


def test_validate_flags_truncation_and_missing_frontmatter():
  assert any(
      "truncated" in i.lower() for i in validate_evolved_skill("## A\nx", _BASE)
  )
  assert any(
      "frontmatter" in i.lower()
      for i in validate_evolved_skill("no frontmatter here", _BASE)
  )


def test_validate_flags_unescaped_adk_var():
  evolved = _BASE.replace("rule b", "use {missing_var} now") + "\n## C\nmore\n"
  assert any(
      "context-variable" in i for i in validate_evolved_skill(evolved, _BASE)
  )


# --- select_candidate (best-of-N + incumbent gate) --------------------------


def test_select_candidate_empty_keeps_base():
  assert select_candidate([], "BASE") == "BASE"


def test_select_candidate_median_without_score_fn():
  # No score_fn -> the median-size viable candidate.
  assert select_candidate(["a", "abc", "abcde"], "BASE") == "abc"


def test_select_candidate_keeps_base_when_no_improvement():
  # Negative control: the best candidate does NOT beat incumbent + margin, so
  # the engine must leave the already-good base skill unchanged (restraint).
  scores = {"BASE": 0.90, "cand1": 0.91, "cand2": 0.88}
  out = select_candidate(
      ["cand1", "cand2"], "BASE", score_fn=scores.get, min_improvement=0.5
  )
  assert out == "BASE"


def test_select_candidate_picks_better_when_it_clears_margin():
  scores = {"BASE": 0.40, "cand1": 0.95, "cand2": 0.60}
  out = select_candidate(
      ["cand1", "cand2"], "BASE", score_fn=scores.get, min_improvement=0.5
  )
  assert out == "cand1"


def test_select_candidate_uses_incumbent_score_without_rescoring_base():
  # A host that already measured the base skill passes incumbent_score;
  # score_fn must then NEVER be called on the base (re-scoring on fresh
  # traffic is noisy and expensive).
  def score_fn(skill):
    assert skill != "BASE", "must not re-score the incumbent"
    return {"cand1": 0.95, "cand2": 0.60}[skill]

  out = select_candidate(
      ["cand1", "cand2"],
      "BASE",
      score_fn=score_fn,
      min_improvement=0.5,
      incumbent_score=0.40,
  )
  assert out == "cand1"


def test_select_candidate_incumbent_score_gates_selection():
  # The provided incumbent score participates in the margin gate.
  scores = {"cand1": 0.95}
  out = select_candidate(
      ["cand1"],
      "BASE",
      score_fn=scores.get,
      min_improvement=0.5,
      incumbent_score=0.90,
  )
  assert out == "BASE"


# --- collect_patches (host analyst hook) ------------------------------------


def test_collect_patches_dispatches_error_analyst_fn():
  # A host-supplied analyst replaces the built-in one for FAILURE
  # trajectories; no client/model calls happen when it handles them all.
  seen = []

  def fake_analyst(client, model, session, current_skill, tools):
    seen.append(session["question"])
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool.\n"
        "## Proposed Patch\nContent: call the tool first."
    )

  report = {
      "sessions": [
          _session("unhelpful", question="q1"),
          _session("partial", question="q2"),
      ]
  }
  patches = collect_patches(
      report,
      "BASE",
      client=None,
      model="unused",
      analyst_mode="error-only",
      error_analyst_fn=fake_analyst,
  )
  assert sorted(seen) == ["q1", "q2"]
  assert len(patches) == 2


# --- _write_evolution_artifacts --------------------------------------------


def test_write_evolution_artifacts(tmp_path):
  patches = [
      "## Root Cause\nTOOL_USAGE: deflected instead of calling the tool.\n",
      "## Root Cause\nTOOL_USAGE: same again.\n",
      "## Pattern\nRESPONSE_PATTERN: bridged the term.\n",
  ]
  base = "BASE SKILL"
  candidates = ["CAND ONE", "CAND TWO", base, ""]  # base + empty are dropped
  selected = "CAND TWO"

  out = str(tmp_path)
  _write_evolution_artifacts(out, patches, candidates, selected, base)

  # Patches: one record per patch, with parsed category.
  records = json.load(open(os.path.join(out, "v1_patches.json")))
  assert len(records) == 3
  assert records[0]["category"] == "TOOL_USAGE"
  assert records[2]["category"] == "RESPONSE_PATTERN"
  assert records[0]["patch"] == patches[0]

  # Candidates: base/empty filtered; selected one tagged.
  cand_dir = os.path.join(out, "v1_candidates")
  files = sorted(os.listdir(cand_dir))
  assert files == ["candidate_1.md", "candidate_2_SELECTED.md"]
  assert (
      open(os.path.join(cand_dir, "candidate_2_SELECTED.md")).read().strip()
      == "CAND TWO"
  )

  # Prevalence summary written.
  prevalence = open(os.path.join(out, "v1_prevalence.txt")).read()
  assert "TOOL_USAGE: 2/3" in prevalence


def test_write_evolution_artifacts_version_label_and_selection(tmp_path):
  # A V1->V2 round writes v2_* names (no clobbering of the v1_* artifacts),
  # and the selection note is persisted as the audit trail of the outcome.
  patches = ["## Root Cause\nTOOL_USAGE: x.\n"]
  base = "BASE SKILL"

  out = str(tmp_path)
  _write_evolution_artifacts(
      out,
      patches,
      [base],  # every candidate == base -> incumbent kept, no candidate files
      base,
      base,
      version_label="v2",
      selection_note="kept incumbent: no viable candidate passed guardrails",
  )

  assert os.path.exists(os.path.join(out, "v2_patches.json"))
  assert os.listdir(os.path.join(out, "v2_candidates")) == []
  note = open(os.path.join(out, "v2_selection.txt")).read()
  assert note.startswith("kept incumbent:")


def test_prevalence_exact_tie_stays_strong():
  # Review P3 #11: an exact 50/50 split is consensus for NEITHER side --
  # VERY STRONG requires a strict majority (> half).
  patch_a = "## Root Cause\nA: x\n## Proposed Patch\nContent: y"
  patch_b = "## Root Cause\nB: x\n## Proposed Patch\nContent: y"
  out = compute_prevalence_summary([patch_a] * 3 + [patch_b] * 3)
  assert "A: 3/6 (50%) -- STRONG" in out
  assert "B: 3/6 (50%) -- STRONG" in out
  out = compute_prevalence_summary([patch_a] * 4 + [patch_b] * 2)
  assert "A: 4/6 (67%) -- VERY STRONG" in out
