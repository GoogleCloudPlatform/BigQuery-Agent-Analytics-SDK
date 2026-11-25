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
"""Tests for combining rule labels and the LLM judge (parent #435)."""

from __future__ import annotations

import json

import pytest

from bigquery_agent_analytics.failure_diagnosis import agent_models
from bigquery_agent_analytics.failure_diagnosis import combine_diagnosis
from bigquery_agent_analytics.failure_diagnosis import diagnose_failed_session
from bigquery_agent_analytics.failure_diagnosis import model_family
from bigquery_agent_analytics.failure_diagnosis import OUTCOMES
from bigquery_agent_analytics.failure_judge import CallableJudgeBackend
from bigquery_agent_analytics.failure_judge import JudgeFailureLabel
from bigquery_agent_analytics.failure_judge import JudgeResult
from bigquery_agent_analytics.rule_taxonomy import collect_session_facts
from bigquery_agent_analytics.rule_taxonomy import RuleFailureLabel

from .test_session_dossier import PROFILE
from .test_session_dossier import Session


def _rule(
    category="finalization",
    span="span-9",
    *,
    primary=True,
    kind="gap_after_span",
    sid="s",
):
  return RuleFailureLabel(
      session_id=sid,
      trace_id="t",
      span_id=span,
      failure_category=category,
      evidence="e",
      confidence=0.9,
      target_kind=kind,
      rule_id="R",
      primary=primary,
  )


def _judge(
    status="labeled",
    category="finalization",
    span="span-9",
    kind="gap_after_span",
    sid="s",
):
  label = None
  if status == "labeled":
    label = JudgeFailureLabel(
        session_id=sid,
        trace_id="t",
        span_id=span,
        failure_category=category,
        evidence="e",
        confidence=0.8,
        target_kind=kind,
        certainty="clear",
        step_index=None,
        evidence_quote="12345678",
        quote_location="final_response",
        runner_up=None,
        rationale="r",
        judge_model="m",
        judge_id="m:h",
    )
  return JudgeResult(
      session_id=sid,
      status=status,
      label=label,
      excluded_reason="gold_wrong" if status == "excluded" else None,
      reason=None,
      attempts=1,
      judge_model="m",
      judge_id="m:h",
      usage={},
  )


class TestCombine:

  def test_agreed_takes_judge_anchor(self):
    d = combine_diagnosis(
        [_rule("wrong source", "span-3", kind="span")],
        _judge(category="wrong source", span="span-4", kind="span"),
    )
    assert d.outcome == "agreed" and d.accepted
    assert d.failure_category == "wrong source"
    assert (d.span_id, d.trace_id, d.target_kind) == ("span-4", "t", "span")
    assert d.anchors_agree is False
    assert d.engine.span_id == "span-3"
    assert d.taxonomy_version == "0.1.0"

  def test_anchors_agree_same_span(self):
    d = combine_diagnosis(
        [_rule("wrong source", "span-3", kind="span")],
        _judge(category="wrong source", span="span-3", kind="span"),
    )
    assert d.anchors_agree is True

  def test_two_gaps_agree_on_anchor(self):
    d = combine_diagnosis([_rule(span="span-5")], _judge(span="span-9"))
    assert d.anchors_agree is True

  def test_disagreed(self):
    d = combine_diagnosis(
        [_rule("turn-waste")],
        _judge(category="execution/computation", kind="span"),
    )
    assert d.outcome == "disagreed" and not d.accepted
    assert d.failure_category is None and d.span_id is None
    assert d.engine.failure_category == "turn-waste"
    assert d.judge.label.failure_category == "execution/computation"
    assert d.anchors_agree is not None

  def test_engine_only(self):
    for status in ("unknown", "invalid", "error"):
      d = combine_diagnosis([_rule()], _judge(status=status))
      assert d.outcome == "engine_only" and not d.accepted
    d = combine_diagnosis([_rule()], None)
    assert d.outcome == "engine_only" and d.judge is None

  def test_judge_only(self):
    d = combine_diagnosis([], _judge())
    assert d.outcome == "judge_only" and not d.accepted
    assert d.anchors_agree is None

  def test_excluded_wins(self):
    d = combine_diagnosis([_rule()], _judge(status="excluded"))
    assert d.outcome == "excluded" and not d.accepted
    assert d.judge.excluded_reason == "gold_wrong"

  def test_none(self):
    d = combine_diagnosis([], _judge(status="invalid"))
    assert d.outcome == "none"
    d = combine_diagnosis([], None, session_id="s")
    assert d.outcome == "none" and d.session_id == "s"

  def test_only_primary_label_compared(self):
    labels = [
        _rule("turn-waste", primary=False),
        _rule("finalization"),
    ]
    d = combine_diagnosis(labels, _judge(category="turn-waste", kind="span"))
    assert d.outcome == "disagreed"
    assert d.engine.failure_category == "finalization"
    assert d.engine_secondary == ("turn-waste",)

  def test_only_secondary_rule_labels(self):
    d = combine_diagnosis([_rule("turn-waste", primary=False)], _judge())
    assert d.outcome == "judge_only"
    assert d.engine is None and d.engine_secondary == ("turn-waste",)

  def test_session_mismatch(self):
    with pytest.raises(ValueError, match="one session"):
      combine_diagnosis([_rule(sid="a")], _judge(sid="b"))
    with pytest.raises(ValueError, match="one session"):
      combine_diagnosis([], None)
    with pytest.raises(ValueError, match="one session"):
      combine_diagnosis([_rule(sid="a")], None, session_id="b")

  def test_every_outcome_is_documented(self):
    assert set(OUTCOMES) == {
        "excluded",
        "agreed",
        "disagreed",
        "engine_only",
        "judge_only",
        "none",
    }


def _sentence_answer_session():
  return (
      Session()
      .start("What is the total rainfall?")
      .tool(
          "run_code",
          {"code": "print(df.rain.sum())", "purpose": "sum"},
          "exit code 0\n41.7",
      )
      .answer("The total rainfall is 41.7 mm over the period.")
  )


def _backend(reply, seen=None):
  def fn(system, prompt, output_schema):
    if seen is not None:
      seen.append(prompt)
    return json.dumps(reply)

  return CallableJudgeBackend(fn, model_id="fake")


_FINALIZATION = {
    "excluded": "",
    "primary_category": "finalization",
    "first_error_step": None,
    "target_kind": "gap_after_span",
    "confidence": "clear",
    "runner_up": "",
    "evidence_quote": "The total rainfall is 41.7 mm",
    "rationale": "Step 1 prints 41.7; the response is a sentence.",
}


class TestDiagnose:

  def test_agreed_end_to_end(self):
    seen = []
    d = diagnose_failed_session(
        _sentence_answer_session().rows,
        _backend(_FINALIZATION, seen),
        profile=PROFILE,
        reference_answer=41.7,
    )
    assert d.engine.rule_id == "FIN-IN-ANSWER"
    assert d.outcome == "agreed" and d.accepted
    assert d.failure_category == "finalization"
    assert d.target_kind == "gap_after_span"
    assert d.anchors_agree is True
    # The judge is blind to the rules and sees the expected answer.
    assert "FIN-IN-ANSWER" not in seen[0]
    assert "## Expected answer\n```\n41.7\n```" in seen[0]

  def test_judge_only_without_reference(self):
    d = diagnose_failed_session(
        _sentence_answer_session().rows,
        _backend(_FINALIZATION),
        profile=PROFILE,
    )
    assert d.engine is None
    assert d.outcome == "judge_only" and not d.accepted

  def test_without_backend(self):
    d = diagnose_failed_session(
        _sentence_answer_session().rows, profile=PROFILE, reference_answer=41.7
    )
    assert d.outcome == "engine_only" and d.judge is None

  def test_task_context_and_rubric_reach_the_judge(self):
    seen_system = []

    def fn(system, prompt, output_schema):
      seen_system.append((system, prompt))
      return json.dumps(_FINALIZATION)

    diagnose_failed_session(
        _sentence_answer_session().rows,
        CallableJudgeBackend(fn, model_id="fake"),
        profile=PROFILE,
        task="Total rainfall, bare number.",
        context={"Data sources": "rain.csv"},
        rubric="Custom rubric text.",
    )
    system, user = seen_system[0]
    assert "Custom rubric text." in system
    assert "Total rainfall, bare number." in user
    assert "### Data sources" in user


class TestModelFamily:

  @pytest.mark.parametrize(
      "model, family",
      [
          ("gemini-2.5-flash", "google"),
          ("publishers/google/models/gemini-2.5-pro", "google"),
          ("claude-opus-4", "anthropic"),
          ("gpt-4o", "openai"),
          ("o3-mini", "openai"),
          ("qwen3-max", "alibaba"),
          ("deepseek-flash", "deepseek"),
          ("llama-3.1-70b", "meta"),
          ("my-project.my_dataset.partner_model", None),
          ("omni", None),
          ("", None),
          (None, None),
      ],
  )
  def test_model_family(self, model, family):
    assert model_family(model) == family

  def _session(self, *models):
    session = Session().start()
    for model in models:
      session.add(
          "LLM_REQUEST",
          {"prompt": "p"},
          attributes=json.dumps({"model": model}),
      )
    return session.tool("t", {}, "printed 41.7").answer(
        "The total is about 42."
    )

  def test_agent_models_in_order_without_repeats(self):
    session = self._session("gemini-2.5-flash", "qwen-plus", "gemini-2.5-flash")
    facts = collect_session_facts(session.rows)
    assert agent_models(facts) == ("gemini-2.5-flash", "qwen-plus")

  @pytest.mark.parametrize(
      "agents, judge, expected",
      [
          (("gemini-2.5-flash",), "gemini-2.5-pro", True),
          (("gemini-2.5-flash",), "deepseek-flash", False),
          (("gemini-2.5-flash", "qwen-plus"), "qwen-max", True),
          ((), "deepseek-flash", None),
          (("p.d.remote",), "deepseek-flash", None),
          (("gemini-2.5-flash",), "p.d.remote", None),
      ],
  )
  def test_same_family_flag(self, agents, judge, expected):
    reply = dict(_FINALIZATION, evidence_quote="printed 41.7")
    d = diagnose_failed_session(
        self._session(*agents).rows,
        CallableJudgeBackend(lambda s, p, o: json.dumps(reply), model_id=judge),
    )
    assert d.agent_models == agents
    assert d.same_family is expected

  def test_no_judge_no_flag(self):
    d = diagnose_failed_session(self._session("gemini-2.5-flash").rows)
    assert d.same_family is None
