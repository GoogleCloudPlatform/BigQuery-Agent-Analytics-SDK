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
