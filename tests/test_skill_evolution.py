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

import os
import sys

# Make scripts/ importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from skill_evolution import _has_parroted_recovery  # noqa: E402
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
