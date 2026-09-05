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
from skill_evolution import _validate_incumbent_score
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


def test_public_host_contract_exports_stable_surface():
  import skill_evolution

  assert skill_evolution.__all__ == [
      "ErrorAnalystFn",
      "collect_patches",
      "evolve_skill",
      "format_trajectory",
      "partition_trajectories",
      "select_candidate",
  ]
  assert "unverified user hypothesis" in " ".join(
      collect_patches.__doc__.split()
  )


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


def test_collect_patches_requires_client_outside_hosted_error_only():
  # client=None is legitimate ONLY with a host analyst in error-only mode;
  # anywhere else every built-in analyst future would fail and be swallowed.
  import pytest

  report = {"sessions": [_session("unhelpful", question="q1")]}
  with pytest.raises(ValueError, match="client is required"):
    collect_patches(report, "BASE", client=None, model="unused")
  with pytest.raises(ValueError, match="client is required"):
    collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="both",
        error_analyst_fn=lambda *a: None,
    )


def test_select_candidate_rejects_non_finite_incumbent_score():
  # NaN/inf make every margin comparison False -- the exact "never ship a
  # worse skill" property this function enforces, silently defeated.
  import pytest

  for bad in (float("nan"), float("-inf"), float("inf")):
    with pytest.raises(ValueError, match="finite"):
      select_candidate(
          ["CAND"], "BASE", score_fn=lambda _s: 1.0, incumbent_score=bad
      )


def test_select_candidate_warns_when_incumbent_score_unused(caplog):
  # incumbent_score without score_fn: median-size selection, no gate -- the
  # host must be told the guard is NOT active.
  import logging

  with caplog.at_level(logging.WARNING):
    out = select_candidate(["A", "BB", "CCC"], "BASE", incumbent_score=0.9)
  assert out == "BB"
  assert any("UNGATED" in r.message for r in caplog.records)


def test_format_renders_both_segment_and_full_session_traces():
  # A session carrying BOTH per-segment traces and a full-session trace
  # renders both: segments suppress only the brief sub_trajectories outcome
  # list (redundant), never the full-session evidence.
  s = _session(
      "unhelpful",
      conversation=[{"role": "user", "text": "q"}],
      sub_trajectories=[
          {
              "label": "corr",
              "outcome": "parroted",
              "start_turn": 1,
              "end_turn": 2,
          }
      ],
      execution_sub_trajectories=[
          {
              "label": "corr",
              "outcome": "parroted",
              "start_turn": 1,
              "end_turn": 2,
              "trace": "SEGMENT-TRACE",
          }
      ],
      execution_trace="FULL-SESSION-TRACE",
  )
  out = format_trajectory(s)
  assert "SEGMENT-TRACE" in out
  assert "FULL-SESSION-TRACE" in out
  assert "=== Execution sub-trajectories ===" in out
  assert "=== Execution trace ===" in out
  assert "=== Correction sub-trajectories ===" not in out


def test_format_renders_execution_trace_for_single_turn_sessions():
  # question/response sessions (no conversation list) are a supported
  # quality_report shape and must not silently lose their trace evidence.
  s = _session(
      "unhelpful",
      question="What is the meal limit?",
      response="Ask HR.",
      execution_trace="invoke supervisor -> NO tool call",
  )
  s.pop("conversation", None)
  out = format_trajectory(s)
  assert "Question: What is the meal limit?" in out
  assert "=== Execution trace ===" in out
  assert "invoke supervisor -> NO tool call" in out


def test_error_analyst_fn_in_both_mode_keeps_builtin_for_successes():
  # Docstring contract: the host analyst replaces FAILURE analysts only;
  # success trajectories always use the built-in single-pass analyst.
  calls = {"host": [], "builtin": []}

  def fake_analyst(client, model, session, current_skill, tools):
    calls["host"].append(session["question"])
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool.\n"
        "## Proposed Patch\nContent: call the tool first."
    )

  class _FakeModels:

    def generate_content(self, **_kw):
      raise AssertionError("built-in analyst should be stubbed")

  class _FakeClient:
    models = _FakeModels()

  import skill_evolution as _se

  original = _se.run_analyst

  def spy_run_analyst(client, model, prompt, session, current_skill, *a, **kw):
    calls["builtin"].append(session["question"])
    return (
        "## Pattern\nRESPONSE_PATTERN: kept the derived rate.\n"
        "## Proposed Patch\nContent: keep deriving rates."
    )

  _se.run_analyst = spy_run_analyst
  try:
    report = {
        "sessions": [
            _session("unhelpful", question="fail-1"),
            _session("meaningful", question="win-1"),
        ]
    }
    patches = collect_patches(
        report,
        "BASE",
        client=_FakeClient(),
        model="unused",
        analyst_mode="both",
        error_analyst_fn=fake_analyst,
    )
  finally:
    _se.run_analyst = original
  assert calls["host"] == ["fail-1"]
  assert calls["builtin"] == ["win-1"]
  assert len(patches) == 2


def test_format_renders_uncovered_brief_outcomes_alongside_segments():
  # execution_sub_trajectories can be PARTIAL (the producer skips segments it
  # cannot align to trace spans) -- a parroted outcome with no traced
  # counterpart must still render, matched on start/end turns.
  s = _session(
      "unhelpful",
      conversation=[{"role": "user", "text": "q"}],
      sub_trajectories=[
          {
              "label": "covered",
              "outcome": "recovered",
              "start_turn": 1,
              "end_turn": 2,
          },
          {
              "label": "uncovered",
              "outcome": "parroted",
              "start_turn": 3,
              "end_turn": 4,
          },
      ],
      execution_sub_trajectories=[
          {
              "label": "covered",
              "outcome": "recovered",
              "start_turn": 1,
              "end_turn": 2,
              "trace": "SEGMENT-TRACE",
          }
      ],
  )
  out = format_trajectory(s)
  assert "SEGMENT-TRACE" in out
  assert "uncovered" in out and "parroted" in out
  assert "=== Correction sub-trajectories ===" in out
  # The covered segment's brief entry stays suppressed (redundant); the
  # traced segment header ("--- [+] covered ...") is the only occurrence.
  assert "\n[+] covered" not in out
  assert "--- [+] covered" in out


def test_format_coerces_structured_execution_trace():
  # A host supplying a structured (non-str) trace must get a readable dump,
  # not a per-session TypeError swallowed inside the analyst future.
  s = _session(
      "unhelpful",
      conversation=[{"role": "user", "text": "q"}],
      execution_trace=[{"event": "TOOL_STARTING", "tool": "lookup"}],
  )
  out = format_trajectory(s)
  assert "=== Execution trace ===" in out
  assert "TOOL_STARTING" in out


def test_partition_survives_malformed_segment_entries():
  # A non-dict entry in a host-supplied segment list must not kill
  # partitioning for the whole report.
  report = {
      "sessions": [
          _session(
              "meaningful",
              execution_sub_trajectories=["not-a-dict", None],
          )
      ]
  }
  successes, failures = partition_trajectories(report)
  assert len(successes) == 1 and not failures


def test_select_candidate_rejects_non_finite_computed_incumbent():
  # The guard covers the score_fn(current_skill) fallback too, not just the
  # incumbent_score parameter.
  import pytest

  def scorer(text):
    return float("nan") if text == "BASE" else 1.0

  with pytest.raises(ValueError, match="non-finite incumbent"):
    select_candidate(["CAND"], "BASE", score_fn=scorer)


def test_host_patch_envelope_enforced_with_reason_logged(caplog):
  # A well-typed host patch that misses the envelope is dropped with a
  # logged reason; a valid one passes the gate.
  import logging

  results = iter(
      [
          "too short",
          (
              "## Root Cause\nTOOL_USAGE: skipped the tool despite having"
              " it available.\n## Proposed Patch\nContent: call the tool"
              " first."
          ),
      ]
  )

  def fake_analyst(client, model, session, current_skill, tools):
    return next(results)

  report = {
      "sessions": [
          _session("unhelpful", question="q1"),
          _session("partial", question="q2"),
      ]
  }
  with caplog.at_level(logging.WARNING):
    patches = collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=fake_analyst,
    )
  assert len(patches) == 1
  assert any("Quality gate rejected" in r.message for r in caplog.records)


def test_all_type_violating_host_raises_like_all_failures():
  # A host returning a non-string for EVERY session is the same failure
  # class as raising for every session: zero usable host patches.
  import pytest

  def dict_analyst(client, model, session, current_skill, tools):
    return {"patch": "structured"}

  report = {
      "sessions": [
          _session("unhelpful", question="q1"),
          _session("partial", question="q2"),
      ]
  }
  with pytest.raises(RuntimeError, match="unusable"):
    collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=dict_analyst,
    )


def test_spanless_traced_segment_does_not_suppress_spanless_brief_entries():
  # A traced segment with no turn keys must not blanket-suppress brief
  # entries that also lack spans ((None, None) collision).
  s = _session(
      "unhelpful",
      conversation=[{"role": "user", "text": "q"}],
      sub_trajectories=[{"label": "brief-spanless", "outcome": "parroted"}],
      execution_sub_trajectories=[
          {
              "label": "traced-spanless",
              "outcome": "recovered",
              "trace": "SEGMENT-TRACE",
          }
      ],
  )
  out = format_trajectory(s)
  assert "SEGMENT-TRACE" in out
  assert "brief-spanless" in out and "parroted" in out


def test_ungated_warning_logs_once_through_evolve_flow(caplog):
  # evolve_skill validates early with warn_ungated=False; the warning fires
  # exactly once, inside select_candidate.
  import logging

  with caplog.at_level(logging.WARNING):
    _validate_incumbent_score(0.9, None, warn_ungated=False)
    select_candidate(["A", "BB", "CCC"], "BASE", incumbent_score=0.9)
  ungated = [r for r in caplog.records if "UNGATED" in r.message]
  assert len(ungated) == 1


def test_non_string_host_patch_dropped_not_crashed(caplog):
  # A truthy non-string return must be dropped with a warning BEFORE the
  # quality gate, never crash after the full fleet spend.
  import logging

  results = iter(
      [
          {"patch": "structured"},
          (
              "## Root Cause\nTOOL_USAGE: skipped the tool despite having"
              " it available.\n## Proposed Patch\nContent: call the tool"
              " first."
          ),
      ]
  )

  def fake_analyst(client, model, session, current_skill, tools):
    return next(results)

  report = {
      "sessions": [
          _session("unhelpful", question="q1"),
          _session("partial", question="q2"),
      ]
  }
  with caplog.at_level(logging.WARNING):
    patches = collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=fake_analyst,
    )
  assert len(patches) == 1
  assert any("instead of patch text" in r.message for r in caplog.records)


def test_collect_patches_tracks_sources_after_quality_gate(monkeypatch):
  import skill_evolution as _se

  host_results = iter(
      [
          "too short",
          (
              "## Root Cause\nTOOL_USAGE: skipped the available lookup.\n"
              "## Proposed Patch\nContent: call the lookup before answering."
          ),
      ]
  )

  def host_analyst(client, model, session, current_skill, tools):
    return next(host_results)

  def builtin_analyst(client, model, prompt, session, current_skill, tools):
    return (
        "## Pattern\nRESPONSE_PATTERN: verified the result before replying.\n"
        "## Proposed Patch\nContent: keep verifying tool results."
    )

  monkeypatch.setattr(_se, "run_analyst", builtin_analyst)
  report = {
      "sessions": [
          _session("unhelpful", question="rejected host"),
          _session("partial", question="kept host"),
          _session("meaningful", question="kept builtin"),
      ]
  }
  provenance = {}
  token = _se._PATCH_PROVENANCE.set(provenance)
  try:
    patches = collect_patches(
        report,
        "BASE",
        client=object(),
        model="unused",
        analyst_mode="both",
        error_analyst_fn=host_analyst,
    )
  finally:
    _se._PATCH_PROVENANCE.reset(token)

  assert len(patches) == 2
  assert all(type(patch) is str for patch in patches)
  assert all(provenance[id(patch)][0] is patch for patch in patches)
  assert [provenance[id(patch)][1] for patch in patches] == ["host", "builtin"]


def test_raising_host_analyst_partial_failure_tolerated(caplog):
  # One raising host analyst degrades to a warning; the surviving patch is
  # still collected and no exception propagates.
  import logging

  def fake_analyst(client, model, session, current_skill, tools):
    if session["question"] == "boom":
      raise RuntimeError("host exploded")
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool despite having it"
        " available.\n## Proposed Patch\nContent: call the tool first."
    )

  report = {
      "sessions": [
          _session("unhelpful", question="boom"),
          _session("partial", question="ok"),
      ]
  }
  with caplog.at_level(logging.WARNING):
    patches = collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=fake_analyst,
    )
  assert len(patches) == 1
  assert any("failed: host exploded" in r.message for r in caplog.records)


def test_all_host_analysts_failing_raises():
  # A systematically broken host analyst must not degrade into a
  # clean-looking zero-patch run.
  import pytest

  def broken_analyst(client, model, session, current_skill, tools):
    raise RuntimeError("bad credentials")

  report = {
      "sessions": [
          _session("unhelpful", question="q1"),
          _session("partial", question="q2"),
      ]
  }
  with pytest.raises(RuntimeError, match="every host error_analyst_fn"):
    collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=broken_analyst,
    )


def test_analyst_timeout_bounds_a_hung_host_analyst(caplog):
  # A blocking host analyst must not hang collect_patches; the timed-out
  # future degrades to a warning like any other analyst failure.
  import logging
  import threading

  release = threading.Event()

  def hung_analyst(client, model, session, current_skill, tools):
    if session["question"] == "hang":
      release.wait(5)
      return None
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool despite having it"
        " available.\n## Proposed Patch\nContent: call the tool first."
    )

  report = {
      "sessions": [
          _session("unhelpful", question="hang"),
          _session("partial", question="ok"),
      ]
  }
  try:
    with caplog.at_level(logging.WARNING):
      patches = collect_patches(
          report,
          "BASE",
          client=None,
          model="unused",
          analyst_mode="error-only",
          error_analyst_fn=hung_analyst,
          analyst_timeout_s=0.3,
      )
  finally:
    release.set()
  assert len(patches) == 1
  assert any("failed" in r.message for r in caplog.records)


def test_hung_analyst_does_not_starve_queued_analyst_one_worker(caplog):
  # With max_workers=1, a hung host analyst must lose its slot on timeout so
  # the queued healthy analyst still runs. Previously the queued future
  # never started, both timed out, and collect_patches raised a FALSE
  # all-host-failures RuntimeError.
  import logging
  import threading

  release = threading.Event()

  def hung_then_ok(client, model, session, current_skill, tools):
    if session["question"] == "hang":
      release.wait(5)
      return None
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool despite having it"
        " available.\n## Proposed Patch\nContent: call the tool first."
    )

  report = {
      "sessions": [
          _session("unhelpful", question="hang"),
          _session("unhelpful", question="ok"),
      ]
  }
  try:
    with caplog.at_level(logging.WARNING):
      patches = collect_patches(
          report,
          "BASE",
          client=None,
          model="unused",
          analyst_mode="error-only",
          error_analyst_fn=hung_then_ok,
          analyst_timeout_s=0.3,
          max_workers=1,
      )
  finally:
    release.set()
  assert len(patches) == 1
  assert any("timed out" in r.message for r in caplog.records)


def test_hung_host_does_not_drop_queued_builtin_success_analyst(
    monkeypatch, caplog
):
  # analyst_mode="both" at max_workers=1: the queued built-in success
  # analyst must still run after the hung host analyst is quarantined,
  # not be silently omitted from the run.
  import logging
  import threading

  import skill_evolution as _se

  release = threading.Event()

  def host_analyst(client, model, session, current_skill, tools):
    if session["question"] == "hang":
      release.wait(5)
      return None
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool despite having it"
        " available.\n## Proposed Patch\nContent: call the tool first."
    )

  builtin_calls = []

  def fake_run_analyst(client, model, prompt, session, current_skill, *a, **k):
    builtin_calls.append(session["question"])
    return (
        "## Pattern\nRESPONSE_PATTERN: kept the derived rate.\n"
        "## Proposed Patch\nContent: keep deriving rates."
    )

  monkeypatch.setattr(_se, "run_analyst", fake_run_analyst)
  report = {
      "sessions": [
          _session("unhelpful", question="hang"),
          _session("unhelpful", question="fail-ok"),
          _session("meaningful", question="win-1"),
      ]
  }
  try:
    with caplog.at_level(logging.WARNING):
      patches = collect_patches(
          report,
          "BASE",
          client=object(),
          model="unused",
          analyst_mode="both",
          error_analyst_fn=host_analyst,
          analyst_timeout_s=0.3,
          max_workers=1,
      )
  finally:
    release.set()
  assert builtin_calls == ["win-1"]
  assert len(patches) == 2  # healthy host patch + built-in success patch


def test_falsy_non_string_host_results_trip_the_guard():
  # False/0/[]/{} are NOT the no-patch sentinel: a host returning falsy
  # non-strings for every session must raise like any all-failures host,
  # not read as a healthy zero-patch run.
  import pytest

  falsy_by_question = {"q0": False, "q1": 0, "q2": [], "q3": {}}

  def falsy_host(client, model, session, current_skill, tools):
    return falsy_by_question[session["question"]]

  report = {
      "sessions": [_session("unhelpful", question=f"q{i}") for i in range(4)]
  }
  with pytest.raises(RuntimeError, match="every host error_analyst_fn"):
    collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=falsy_host,
    )


def test_all_none_host_results_stay_a_healthy_zero_patch_run():
  # None remains the one valid empty sentinel: an all-None host is a
  # healthy zero-patch run, not an all-host-failures error.
  def quiet_host(client, model, session, current_skill, tools):
    return None

  report = {
      "sessions": [
          _session("unhelpful", question="q1"),
          _session("unhelpful", question="q2"),
      ]
  }
  patches = collect_patches(
      report,
      "BASE",
      client=None,
      model="unused",
      analyst_mode="error-only",
      error_analyst_fn=quiet_host,
  )
  assert patches == []


def test_quarantine_keeps_live_callables_bounded():
  # Timeouts must not fan the fleet out: with max_workers=1 and a report of
  # slow analysts, live callables are capped at 2 * max_workers (one running
  # slot + one budgeted quarantine donation), NOT one per timed-out call.
  import threading

  import pytest

  release = threading.Event()
  lock = threading.Lock()
  active = 0
  peak = 0

  def slow_host(client, model, session, current_skill, tools):
    nonlocal active, peak
    with lock:
      active += 1
      peak = max(peak, active)
    try:
      release.wait(10)  # slower than every timeout window in this test
      return None
    finally:
      with lock:
        active -= 1

  report = {
      "sessions": [_session("unhelpful", question=f"q{i}") for i in range(12)]
  }
  try:
    # Every call times out or is cancelled unstarted -> all-host-failures.
    with pytest.raises(RuntimeError, match="every host error_analyst_fn"):
      collect_patches(
          report,
          "BASE",
          client=None,
          model="unused",
          analyst_mode="error-only",
          error_analyst_fn=slow_host,
          analyst_timeout_s=0.05,
          max_workers=1,
      )
  finally:
    release.set()
  assert peak <= 2, f"expected <= 2 concurrent callables, saw {peak}"


def test_thread_allocation_bounded_by_workers_not_report_size():
  # Threads are created lazily as slots free up: a large report must not
  # allocate one thread per trajectory up front.
  import threading

  lock = threading.Lock()
  baseline = threading.active_count()
  peak = 0

  def counting_host(client, model, session, current_skill, tools):
    nonlocal peak
    with lock:
      peak = max(peak, threading.active_count())
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool despite having it"
        " available.\n## Proposed Patch\nContent: call the tool first."
    )

  report = {
      "sessions": [_session("unhelpful", question=f"q{i}") for i in range(60)]
  }
  patches = collect_patches(
      report,
      "BASE",
      client=None,
      model="unused",
      analyst_mode="error-only",
      error_analyst_fn=counting_host,
      max_workers=2,
  )
  assert len(patches) == 60
  # 2 workers + dispatcher + slack for unrelated/lingering daemons; the old
  # dispatch allocated all 60 threads up front and busted any such bound.
  assert peak - baseline <= 6, (
      f"expected thread growth bounded by max_workers, saw"
      f" {peak - baseline} extra threads"
  )


def test_non_positive_max_workers_rejected_before_dispatch():
  # BoundedSemaphore(0) used to be accepted and every analyst blocked in
  # acquire() forever under the default wait(None). Reject bad worker
  # counts up front, before any analyst runs.
  import pytest

  calls = []

  def host(client, model, session, current_skill, tools):
    calls.append(session["question"])
    return None

  report = {"sessions": [_session("unhelpful", question="q1")]}
  for bad in (0, -3, 2.5, True):
    with pytest.raises(ValueError, match="max_workers must be a positive"):
      collect_patches(
          report,
          "BASE",
          client=None,
          model="unused",
          analyst_mode="error-only",
          error_analyst_fn=host,
          max_workers=bad,
      )
  assert calls == []


def test_evolve_skill_rejects_bad_max_workers_before_any_spend(monkeypatch):
  # The hoisted check must fire before client creation, so a bad worker
  # count cannot burn the analyst fleet + consolidation first.
  import pytest
  import skill_evolution as _se

  def boom(project, location):
    raise AssertionError("client must not be created for bad max_workers")

  monkeypatch.setattr(_se, "_make_client", boom)
  report = {"sessions": [_session("unhelpful", question="q1")]}
  with pytest.raises(ValueError, match="max_workers must be a positive"):
    _se.evolve_skill(report, "BASE", max_workers=0)


def test_all_blank_string_host_results_trip_the_guard():
  # "" and whitespace-only strings are NOT the no-patch sentinel (None is):
  # an all-blank host must raise like any all-failures host, not read as a
  # healthy zero-patch run (or as mere quality-gate rejects).
  import pytest

  blank_by_question = {"q0": "", "q1": "   ", "q2": "\n\t", "q3": ""}

  def blank_host(client, model, session, current_skill, tools):
    return blank_by_question[session["question"]]

  report = {
      "sessions": [_session("unhelpful", question=f"q{i}") for i in range(4)]
  }
  with pytest.raises(RuntimeError, match="every host error_analyst_fn"):
    collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=blank_host,
    )


def test_blank_host_result_is_partial_failure_not_fatal(caplog):
  # One blank return + one valid patch: the blank is warned and dropped,
  # the valid patch survives, and nothing raises.
  import logging

  def mixed_host(client, model, session, current_skill, tools):
    if session["question"] == "blank":
      return "   "
    return (
        "## Root Cause\nTOOL_USAGE: skipped the tool despite having it"
        " available.\n## Proposed Patch\nContent: call the tool first."
    )

  report = {
      "sessions": [
          _session("unhelpful", question="blank"),
          _session("unhelpful", question="ok"),
      ]
  }
  with caplog.at_level(logging.WARNING):
    patches = collect_patches(
        report,
        "BASE",
        client=None,
        model="unused",
        analyst_mode="error-only",
        error_analyst_fn=mixed_host,
    )
  assert len(patches) == 1
  assert any(
      "empty/whitespace-only string" in r.message for r in caplog.records
  )


def test_select_candidate_rejects_non_finite_candidate_score():
  # An inf/NaN CANDIDATE score must not defeat the improvement gate that
  # already rejects non-finite incumbent scores (inf would always win
  # selection AND pass the margin check).
  import pytest

  for bad in (float("inf"), float("-inf"), float("nan")):

    def scorer(text, _bad=bad):
      return 1.0 if text == "BASE" else _bad

    with pytest.raises(ValueError, match="non-finite candidate"):
      select_candidate(["CAND"], "BASE", score_fn=scorer)


def test_format_coerces_structured_segment_trace():
  # A dict/list per-segment trace degrades to a readable dump instead of a
  # TypeError inside the analyst future (parity with the full-session
  # trace coercion).
  s = _session(
      "unhelpful",
      conversation=[{"role": "user", "text": "q"}],
      execution_sub_trajectories=[
          {
              "label": "post_correction_1",
              "outcome": "parroted",
              "start_turn": 1,
              "end_turn": 2,
              "trace": [{"event": "TOOL_STARTING", "tool": "lookup"}],
          }
      ],
  )
  out = format_trajectory(s)
  assert "=== Execution sub-trajectories ===" in out
  assert "TOOL_STARTING" in out


def test_format_filters_malformed_correction_boundaries():
  # Non-dict boundary entries are filtered; optional malformed evidence
  # must not discard an otherwise valid trajectory with AttributeError.
  s = _session(
      "unhelpful",
      conversation=[{"role": "user", "text": "q"}],
      correction_boundaries=[
          None,
          "not-a-dict",
          {
              "turn_index": 1,
              "wrong_claim": "PTO is 20 days",
              "correct_fact": "PTO is 25 days",
              "agent_recovered": True,
          },
      ],
  )
  out = format_trajectory(s)
  assert "=== Correction Evidence ===" in out
  assert "PTO is 25 days" in out


def test_single_turn_sessions_render_all_enrichment_sections():
  # The question/response shape shares the enrichment renderer with the
  # conversation shape -- it must not drop verifications, correction
  # evidence, segment outcomes, or per-segment traces.
  s = _session(
      "unhelpful",
      question="What is the meal limit?",
      response="Ask HR.",
      verifications=2,
      correction_boundaries=[
          {
              "turn_index": 1,
              "wrong_claim": "no limit",
              "correct_fact": "$50 per day",
              "agent_recovered": False,
          }
      ],
      sub_trajectories=[
          {
              "label": "post_correction_1",
              "outcome": "parroted",
              "start_turn": 1,
              "end_turn": 2,
          }
      ],
      execution_sub_trajectories=[
          {
              "label": "post_correction_2",
              "outcome": "recovered",
              "start_turn": 3,
              "end_turn": 4,
              "trace": "agent->policy_agent (tool call)",
          }
      ],
      execution_trace="invoke supervisor -> transfer policy_agent",
  )
  out = format_trajectory(s)
  assert "User verification requests: 2" in out
  assert "=== Correction Evidence ===" in out
  assert "$50 per day" in out
  assert "=== Execution sub-trajectories ===" in out
  assert "agent->policy_agent (tool call)" in out
  # The brief segment has no traced counterpart, so it still renders.
  assert "post_correction_1" in out
  assert "=== Execution trace ===" in out


def test_legacy_sessions_render_without_new_sections():
  # Backward-compat parity: sessions without any of the new keys must not
  # gain new section headings, in either session shape.
  conv = _session("unhelpful", conversation=[{"role": "user", "text": "q"}])
  single = _session("unhelpful", question="q", response="a")
  single.pop("conversation", None)
  for sess in (conv, single):
    out = format_trajectory(sess)
    assert "=== Execution" not in out
    assert "=== Correction" not in out


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
  _write_evolution_artifacts(
      out,
      patches,
      candidates,
      selected,
      base,
      patch_sources=["host", "builtin", "builtin"],
  )

  # Patches: one record per patch, with parsed category.
  records = json.load(open(os.path.join(out, "v1_patches.json")))
  assert len(records) == 3
  assert records[0]["category"] == "TOOL_USAGE"
  assert records[2]["category"] == "RESPONSE_PATTERN"
  assert records[0]["patch"] == patches[0]
  assert [record["source"] for record in records] == [
      "host",
      "builtin",
      "builtin",
  ]

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


def test_write_evolution_artifacts_rejects_misaligned_sources(tmp_path):
  import pytest

  with pytest.raises(ValueError, match="align one-to-one"):
    _write_evolution_artifacts(
        str(tmp_path),
        ["patch one", "patch two"],
        [],
        "BASE",
        "BASE",
        patch_sources=["host"],
    )


def test_evolve_skill_writes_host_patch_provenance(monkeypatch, tmp_path):
  import skill_evolution as _se

  def host_analyst(client, model, session, current_skill, tools):
    return (
        "## Root Cause\nTOOL_USAGE: skipped the available lookup.\n"
        "## Proposed Patch\nContent: call the lookup before answering."
    )

  monkeypatch.setattr(
      _se,
      "_consolidate_once",
      lambda *args, **kwargs: _BASE + "\n## C\nnew rule c\n",
  )
  result = _se.evolve_skill(
      {"sessions": [_session("unhelpful", question="q")]},
      _BASE,
      client=object(),
      candidates=1,
      analyst_mode="error-only",
      error_analyst_fn=host_analyst,
      artifacts_dir=str(tmp_path),
  )

  assert "## C" in result
  records = json.load(open(tmp_path / "v1_patches.json"))
  assert [(record["source"], record["category"]) for record in records] == [
      ("host", "TOOL_USAGE")
  ]


def test_evolve_skill_preserves_legacy_collect_patches_replacements(
    monkeypatch, tmp_path
):
  import skill_evolution as _se

  patch = (
      "## Root Cause\nTOOL_USAGE: skipped the available lookup.\n"
      "## Proposed Patch\nContent: call the lookup before answering."
  )

  def legacy_collector(
      report,
      current_skill,
      *,
      client,
      model,
      max_workers=10,
      max_success_samples=15,
      analyst_mode="both",
      tools=None,
      error_analyst_fn=None,
      analyst_timeout_s=None,
  ):
    return [patch]

  monkeypatch.setattr(_se, "collect_patches", legacy_collector)
  monkeypatch.setattr(
      _se,
      "_consolidate_once",
      lambda *args, **kwargs: _BASE + "\n## C\nnew rule c\n",
  )

  result = _se.evolve_skill(
      {"sessions": [_session("unhelpful", question="q")]},
      _BASE,
      client=object(),
      candidates=1,
      artifacts_dir=str(tmp_path),
  )

  assert "## C" in result
  records = json.load(open(tmp_path / "v1_patches.json"))
  assert records[0]["source"] == "builtin"


def _legacy_collector_wrapper(original_collector, transform):
  """Wrap a collector without accepting any post-#395 private keywords."""

  def collector(
      report,
      current_skill,
      *,
      client,
      model,
      max_workers=10,
      max_success_samples=15,
      analyst_mode="both",
      tools=None,
      error_analyst_fn=None,
      analyst_timeout_s=None,
  ):
    patches = original_collector(
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
    return transform(patches)

  return collector


def _run_wrapped_provenance_case(
    monkeypatch, tmp_path, transform, *, shared_patch=False
):
  import skill_evolution as _se

  host_patch = (
      "## Root Cause\nTOOL_USAGE: host patch skipped the lookup.\n"
      "## Proposed Patch\nContent: host should call the lookup first."
  )
  builtin_patch = (
      "## Pattern\nRESPONSE_PATTERN: builtin patch verified the result.\n"
      "## Proposed Patch\nContent: builtin should keep verification."
  )
  if shared_patch:
    builtin_patch = host_patch

  def host_analyst(client, model, session, current_skill, tools):
    return host_patch

  def builtin_analyst(client, model, prompt, session, current_skill, tools):
    return builtin_patch

  monkeypatch.setattr(_se, "run_analyst", builtin_analyst)
  monkeypatch.setattr(
      _se,
      "collect_patches",
      _legacy_collector_wrapper(_se.collect_patches, transform),
  )
  monkeypatch.setattr(
      _se,
      "_consolidate_once",
      lambda *args, **kwargs: _BASE + "\n## C\nnew rule c\n",
  )

  _se.evolve_skill(
      {
          "sessions": [
              _session("unhelpful", question="host"),
              _session("meaningful", question="builtin"),
          ]
      },
      _BASE,
      client=object(),
      candidates=1,
      error_analyst_fn=host_analyst,
      artifacts_dir=str(tmp_path),
  )

  return (
      json.load(open(tmp_path / "v1_patches.json")),
      host_patch,
      builtin_patch,
  )


def test_evolve_skill_keeps_provenance_when_wrapper_reorders(
    monkeypatch, tmp_path
):
  records, host_patch, builtin_patch = _run_wrapped_provenance_case(
      monkeypatch, tmp_path, lambda patches: list(reversed(patches))
  )
  assert [(record["patch"], record["source"]) for record in records] == [
      (builtin_patch, "builtin"),
      (host_patch, "host"),
  ]


def test_evolve_skill_keeps_provenance_when_wrapper_filters(
    monkeypatch, tmp_path
):
  records, host_patch, _ = _run_wrapped_provenance_case(
      monkeypatch,
      tmp_path,
      lambda patches: [patch for patch in patches if "host patch" in patch],
  )
  assert [(record["patch"], record["source"]) for record in records] == [
      (host_patch, "host")
  ]


def test_evolve_skill_keeps_provenance_when_wrapper_deduplicates(
    monkeypatch, tmp_path
):
  records, host_patch, builtin_patch = _run_wrapped_provenance_case(
      monkeypatch,
      tmp_path,
      lambda patches: list(dict.fromkeys([patches[0], *patches])),
  )
  assert [(record["patch"], record["source"]) for record in records] == [
      (host_patch, "host"),
      (builtin_patch, "builtin"),
  ]


def test_evolve_skill_distinguishes_shared_patch_occurrences_when_filtered(
    monkeypatch, tmp_path
):
  def keep_second_occurrence(patches):
    assert patches[0] == patches[1]
    assert patches[0] is not patches[1]
    assert all(type(patch) is str for patch in patches)
    return patches[1:]

  records, shared_patch, _ = _run_wrapped_provenance_case(
      monkeypatch,
      tmp_path,
      keep_second_occurrence,
      shared_patch=True,
  )
  assert [(record["patch"], record["source"]) for record in records] == [
      (shared_patch, "builtin")
  ]


def test_evolve_skill_distinguishes_shared_patch_occurrences_when_reordered(
    monkeypatch, tmp_path
):
  records, shared_patch, _ = _run_wrapped_provenance_case(
      monkeypatch,
      tmp_path,
      lambda patches: list(reversed(patches)),
      shared_patch=True,
  )
  assert [(record["patch"], record["source"]) for record in records] == [
      (shared_patch, "builtin"),
      (shared_patch, "host"),
  ]


def test_evolve_skill_keeps_discarded_batch_alive_for_provenance(
    monkeypatch, tmp_path
):
  import skill_evolution as _se

  host_patch = (
      "## Root Cause\nTOOL_USAGE: discarded host patch skipped lookup.\n"
      "## Proposed Patch\nContent: call the host lookup first."
  )
  builtin_patch = (
      "## Pattern\nRESPONSE_PATTERN: returned builtin patch verified data.\n"
      "## Proposed Patch\nContent: keep builtin verification."
  )

  def host_analyst(client, model, session, current_skill, tools):
    return host_patch

  def builtin_analyst(client, model, prompt, session, current_skill, tools):
    return builtin_patch

  original_collector = _se.collect_patches

  def discarding_collector(
      report,
      current_skill,
      *,
      client,
      model,
      max_workers=10,
      max_success_samples=15,
      analyst_mode="both",
      tools=None,
      error_analyst_fn=None,
      analyst_timeout_s=None,
  ):
    discarded = original_collector(
        {"sessions": [_session("unhelpful", question="discarded host")]},
        current_skill,
        client=client,
        model=model,
        max_workers=max_workers,
        max_success_samples=max_success_samples,
        analyst_mode="error-only",
        tools=tools,
        error_analyst_fn=error_analyst_fn,
        analyst_timeout_s=analyst_timeout_s,
    )
    provenance = _se._PATCH_PROVENANCE.get()
    assert provenance[id(discarded[0])][0] is discarded[0]
    del discarded
    return original_collector(
        {"sessions": [_session("meaningful", question="returned builtin")]},
        current_skill,
        client=client,
        model=model,
        max_workers=max_workers,
        max_success_samples=max_success_samples,
        analyst_mode="success-only",
        tools=tools,
        error_analyst_fn=error_analyst_fn,
        analyst_timeout_s=analyst_timeout_s,
    )

  monkeypatch.setattr(_se, "run_analyst", builtin_analyst)
  monkeypatch.setattr(_se, "collect_patches", discarding_collector)
  monkeypatch.setattr(
      _se,
      "_consolidate_once",
      lambda *args, **kwargs: _BASE + "\n## C\nnew rule c\n",
  )

  _se.evolve_skill(
      {"sessions": []},
      _BASE,
      client=object(),
      candidates=1,
      error_analyst_fn=host_analyst,
      artifacts_dir=str(tmp_path),
  )

  records = json.load(open(tmp_path / "v1_patches.json"))
  assert [(record["patch"], record["source"]) for record in records] == [
      (builtin_patch, "builtin")
  ]


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


class TestDemoShellContract:
  """run_e2e_demo.sh honors the final (post-#360) data-path contract."""

  _DEMO = os.path.join(
      os.path.dirname(__file__),
      "..",
      "examples",
      "skill_evolution_lab",
      "run_e2e_demo.sh",
  )

  def _text(self):
    with open(self._DEMO) as f:
      return f.read()

  def test_no_workaround_remnants(self):
    text = self._text()
    assert "--conversations-file" not in text
    assert "TODO(#360)" not in text
    assert "agent_events_${RUN_LABEL}_" not in text
    assert "pending SDK" not in text

  def test_scoring_is_bounded_and_labeled(self):
    text = self._text()
    for required in (
        "--app-name skill-evolution-lab",
        '--label "run=$RUN_LABEL"',
        "--time-period 24h",
        "--limit 500",
    ):
      assert required in text, required


class TestServerSideGate:
  """score() aborts when a pass was not judged server-side (#385 P1)."""

  _LAB = os.path.join(
      os.path.dirname(__file__), "..", "examples", "skill_evolution_lab"
  )

  def test_score_enforces_execution_mode(self):
    with open(os.path.join(self._LAB, "run_e2e_demo.sh")) as f:
      text = f.read()
    assert "--require-execution-mode ai_generate" in text

  def _run_print_rate(self, tmp_path, mode):
    import subprocess

    report = {
        "summary": {
            "golden_eval_summary": {
                "matched_meaningful_rate": 100.0,
                "matched_meaningful": 1,
                "matched": 1,
                "total_sessions": 1,
            }
        },
        "sessions": [{"session_id": "t1", "golden_eval": {"matched": True}}],
        "details": {"execution_mode": mode},
    }
    p = tmp_path / f"report_{mode}.json"
    p.write_text(json.dumps(report))
    return subprocess.run(
        [
            sys.executable,
            os.path.join(self._LAB, "print_rate.py"),
            "--require-execution-mode",
            "ai_generate",
            str(p),
        ],
        capture_output=True,
        text=True,
    )

  def test_complete_fallback_pass_is_fatal(self, tmp_path):
    result = self._run_print_rate(tmp_path, "api_fallback")
    assert result.returncode == 1
    assert "not judged server-side" in result.stderr

  def test_server_side_pass_is_accepted(self, tmp_path):
    result = self._run_print_rate(tmp_path, "ai_generate")
    assert result.returncode == 0


class TestToolSpanPairing:
  """TOOL_STARTING/TOOL_COMPLETED share one span id so args survive (#385 P1)."""

  def _rows_for(self, record):
    import importlib.util
    import unittest.mock as mock

    lab_dir = os.path.join(
        os.path.dirname(__file__), "..", "examples", "skill_evolution_lab"
    )
    path = os.path.join(lab_dir, "run_agent.py")
    spec = importlib.util.spec_from_file_location("skill_lab_run_agent", path)
    run_agent = importlib.util.module_from_spec(spec)
    # run_agent imports the lab-local `_quiet` helper, and `agent.agent`
    # pulls the live Gemini/ADK stack (google.cloud.storage etc.) that CI
    # does not install -- stub it: this test exercises only the BigQuery
    # row writer, which needs neither build_config nor make_client.
    import types

    fake_pkg = types.ModuleType("agent")
    fake_mod = types.ModuleType("agent.agent")
    fake_mod.build_config = lambda *a, **k: None
    fake_mod.make_client = lambda *a, **k: None
    fake_pkg.agent = fake_mod
    saved = {k: sys.modules.get(k) for k in ("agent", "agent.agent")}
    sys.modules["agent"] = fake_pkg
    sys.modules["agent.agent"] = fake_mod
    sys.path.insert(0, lab_dir)
    try:
      spec.loader.exec_module(run_agent)
    finally:
      sys.path.remove(lab_dir)
      for k, v in saved.items():
        if v is None:
          sys.modules.pop(k, None)
        else:
          sys.modules[k] = v

    captured = {}

    class _FakeBQ:

      def __init__(self, **_kwargs):
        pass

      def create_dataset(self, *_a, **_k):
        return object()

      def create_table(self, *_a, **_k):
        return object()

      def insert_rows_json(self, _table, rows):
        captured["rows"] = rows
        return []

    with mock.patch.dict(
        os.environ,
        {"PROJECT_ID": "p", "DATASET_ID": "d", "TABLE_ID": "agent_events"},
    ):
      with mock.patch.object(
          run_agent, "_events_from_record", wraps=run_agent._events_from_record
      ):
        import google.cloud.bigquery as bq_mod

        with mock.patch.object(bq_mod, "Client", _FakeBQ):
          run_agent._write_bigquery([record], "skill-evolution-lab", {})
    return captured["rows"]

  def test_tool_args_pair_by_shared_span_id(self):
    record = {
        "session_id": "s1",
        "error": None,
        "conversation": [
            {"role": "user", "text": "q"},
            {"role": "agent", "text": "a"},
        ],
        "tool_calls_detail": [
            {"name": "lookup_company_policy", "args": {"topic": "pto"}}
        ],
        "_events": [
            ("USER_MESSAGE_RECEIVED", {"text": "q"}, 0),
            (
                "TOOL_STARTING",
                {"tool": "lookup_company_policy", "args": {"topic": "pto"}},
                0,
            ),
            (
                "TOOL_COMPLETED",
                {"tool": "lookup_company_policy", "result": "20 days"},
                0,
            ),
            ("LLM_RESPONSE", {"response": "a"}, 0),
        ],
    }
    rows = self._rows_for(record)
    starts = [r for r in rows if r["event_type"] == "TOOL_STARTING"]
    dones = [r for r in rows if r["event_type"] == "TOOL_COMPLETED"]
    assert len(starts) == 1 and len(dones) == 1
    assert starts[0]["span_id"] == dones[0]["span_id"]
    assert json.loads(starts[0]["content"])["args"] == {"topic": "pto"}
    # USER/LLM spans stay distinct.
    others = {
        r["span_id"]
        for r in rows
        if r["event_type"] in ("USER_MESSAGE_RECEIVED", "LLM_RESPONSE")
    }
    assert starts[0]["span_id"] not in others


class TestDocsMatchRecording:
  """Public docs are pinned to the committed recording's result artifacts.

  Requested in #385 review: another sample_run regeneration must not be able
  to silently leave the repository narrative (README/VERIFICATION/examples
  index/sample README) describing a superseded recording.
  """

  _LAB = os.path.join(
      os.path.dirname(__file__), "..", "examples", "skill_evolution_lab"
  )

  def _read(self, *parts):
    with open(os.path.join(self._LAB, *parts)) as f:
      return f.read()

  def _overall(self, text):
    import re

    m = re.search(
        r"\| Overall \| (\d+\.\d+% \(\d+/80\)) \| (\d+\.\d+% \(\d+/80\)) \|",
        text,
    )
    assert m, "Overall row missing from result artifact"
    return m.group(1), m.group(2)

  def test_headline_rates_and_winner_pinned_everywhere(self):
    v0_rate, v1_rate = self._overall(self._read("sample_run", "RESULT.md"))
    r2 = self._read("sample_run", "RESULT_ROUND2.md")
    v1_again, v2_rate = self._overall(r2)
    assert v1_again == v1_rate, "RESULT vs RESULT_ROUND2 disagree on V1"

    def pct(rate):
      return float(rate.split("%")[0])

    winner = "V2" if pct(v2_rate) > pct(v1_rate) else "V1"

    docs = {
        "VERIFICATION.md": self._read("VERIFICATION.md"),
        "README.md": self._read("README.md"),
        "sample_run/README.md": self._read("sample_run", "README.md"),
        "examples/README.md": self._read("..", "README.md"),
    }
    for name, text in docs.items():
      for rate in (v0_rate.split(" ")[0], v1_rate.split(" ")[0]):
        assert rate in text, f"{name} missing headline rate {rate}"
      assert v2_rate.split(" ")[0] in text, f"{name} missing V2 rate"
    # The kept-version narrative must match the artifacts: when V2 wins, no
    # doc may claim the incumbent was kept for THIS recording (and vice
    # versa the numbers above pin the refusal story).
    if winner == "V2":
      assert (
          "kept V2" in docs["README.md"]
          or "kept **V2**" in docs["README.md"]
          or "**kept V2**" in docs["README.md"]
      ), "lab README does not state that V2 was kept"
      assert (
          "kept" in docs["VERIFICATION.md"]
          and "97.5%" in docs["VERIFICATION.md"]
      )
