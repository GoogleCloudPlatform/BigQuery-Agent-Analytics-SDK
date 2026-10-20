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

"""Tests for canonical rubrics and skill_evolution reuse in agent_improvement_cycle."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from bigquery_agent_analytics.evaluation_rubrics import build_metrics

_DEMO_DIR = (
    Path(__file__).resolve().parents[1] / "examples" / "agent_improvement_cycle"
)
if str(_DEMO_DIR) not in sys.path:
  sys.path.insert(0, str(_DEMO_DIR))

from agent_improvement import eval_runner  # noqa: E402
from agent_improvement import improver_agent  # noqa: E402
from agent_improvement import prompts  # noqa: E402


def test_judge_prompt_embeds_canonical_usefulness_and_tool_usage_rubrics():
  metrics = {m.name: m for m in build_metrics()}
  usefulness = metrics["response_usefulness"]
  tool_usage = metrics["tool_usage"]

  assert usefulness.definition in prompts.JUDGE_PROMPT
  assert tool_usage.definition in prompts.JUDGE_PROMPT
  for cat in usefulness.categories:
    assert f"* {cat.name}: {cat.definition}" in prompts.JUDGE_PROMPT
  for cat in tool_usage.categories:
    assert f"* {cat.name}: {cat.definition}" in prompts.JUDGE_PROMPT

  expected_usefulness_choices = " | ".join(
      f'"{c.name}"' for c in usefulness.categories
  )
  expected_tool_choices = " | ".join(
      f'"{c.name}"' for c in tool_usage.categories
  )
  expected_pass_cat = usefulness.categories[0].name
  expected_fail_cats = " or ".join(
      f'"{c.name}"' for c in usefulness.categories[1:]
  )
  assert expected_usefulness_choices in prompts.JUDGE_PROMPT
  assert expected_tool_choices in prompts.JUDGE_PROMPT
  assert (
      f'A response PASSES ("pass": true) when response_usefulness is'
      f' "{expected_pass_cat}".' in prompts.JUDGE_PROMPT
  )
  assert (
      f'A response FAILS ("pass": false) when response_usefulness is'
      f" {expected_fail_cats}." in prompts.JUDGE_PROMPT
  )

  rendered = prompts.JUDGE_PROMPT.format(
      question="How many PTO days?",
      response="You get 20 PTO days.",
      tool_check="SENTINEL_TOOL_CHECK",
      tool_fail_rule="\nSENTINEL_TOOL_FAIL_RULE",
  )
  assert "Question: How many PTO days?" in rendered
  assert '"pass": true or false' in rendered
  assert "SENTINEL_TOOL_CHECK" in rendered
  assert "SENTINEL_TOOL_FAIL_RULE" in rendered


@pytest.mark.asyncio
async def test_eval_runner_judge_case_records_canonical_categories(
    monkeypatch: pytest.MonkeyPatch,
):
  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  pass_usefulness, fail_usefulness, partial_usefulness = usefulness_cats[:3]
  pass_tool_usage, partial_tool_usage, fail_tool_usage = tool_usage_cats[:3]

  verdicts = iter(
      [
          {
              "pass": False,
              "response_usefulness": partial_usefulness,
              "tool_usage": partial_tool_usage,
              "reason": "Answered only half the question.",
          },
          {
              "pass": False,
              "response_usefulness": "  Partial ",
              "tool_usage": f"  {partial_tool_usage.upper()}  ",
              "reason": "Capitalized labels from judge.",
          },
          {
              "pass": False,
              "response_usefulness": "off_vocab_label",
              "tool_usage": "invented_tool_label",
              "reason": "Off-vocabulary labels on failure.",
          },
          {
              "pass": True,
              "reason": "Missing category labels on pass.",
          },
          {
              "pass": False,
              "response_usefulness": pass_usefulness,
              "tool_usage": pass_tool_usage,
              "reason": "Failed despite meaningful label.",
          },
          {
              "pass": True,
              "response_usefulness": partial_usefulness,
              "tool_usage": pass_tool_usage,
              "reason": "Only half answered despite pass=true.",
          },
          {
              "pass": True,
              "response_usefulness": fail_usefulness,
              "tool_usage": pass_tool_usage,
              "reason": "Unhelpful despite pass=true.",
          },
      ]
  )

  class FakeModels:

    def generate_content(self, **kwargs):
      return SimpleNamespace(text=json.dumps(next(verdicts)))

  class FakeClient:

    def __init__(self, *args, **kwargs):
      self.models = FakeModels()

  monkeypatch.setattr("agent_improvement.eval_runner.genai.Client", FakeClient)

  runner = eval_runner.EvalRunner(agent_factory=lambda p: None)
  case = {
      "id": "case_1",
      "question": "What is the PTO and sick leave policy?",
      "expected_tool": "lookup_policy",
  }

  # 1. Exact canonical labels
  scored = await runner.judge_case(
      case,
      {
          "case_id": "case_1",
          "question": case["question"],
          "response": "PTO is 20 days.",
          "tools_called": ["lookup_policy"],
      },
  )
  assert scored["pass"] is False
  assert scored["response_usefulness"] == partial_usefulness
  assert scored["tool_usage"] == partial_tool_usage
  assert scored["reason"] == "Answered only half the question."

  # 2. Capitalized / whitespace-padded labels normalized to canonical lowercase
  scored_caps = await runner.judge_case(
      case,
      {
          "case_id": "case_1",
          "question": case["question"],
          "response": "PTO is 20 days.",
          "tools_called": ["lookup_policy"],
      },
  )
  assert scored_caps["response_usefulness"] == partial_usefulness
  assert scored_caps["tool_usage"] == partial_tool_usage

  # 3. Off-vocabulary labels fall back based on pass and tool_ok
  scored_off = await runner.judge_case(
      case,
      {
          "case_id": "case_1",
          "question": case["question"],
          "response": "I think it is 20 days.",
          "tools_called": [],
      },
  )
  assert scored_off["pass"] is False
  assert scored_off["response_usefulness"] == fail_usefulness
  assert scored_off["tool_usage"] == fail_tool_usage

  # 4. Missing labels on pass with expected tool called fall back to pass categories
  scored_pass = await runner.judge_case(
      case,
      {
          "case_id": "case_1",
          "question": case["question"],
          "response": "PTO is 20 days and sick leave is 10 days.",
          "tools_called": ["lookup_policy"],
      },
  )
  assert scored_pass["pass"] is True
  assert scored_pass["response_usefulness"] == pass_usefulness
  assert scored_pass["tool_usage"] == pass_tool_usage

  # 5. Contradictory pass=False + pass_usefulness overrides usefulness to fail_usefulness
  scored_contradiction = await runner.judge_case(
      case,
      {
          "case_id": "case_1",
          "question": case["question"],
          "response": "PTO is 20 days.",
          "tools_called": ["lookup_policy"],
      },
  )
  assert scored_contradiction["pass"] is False
  assert scored_contradiction["response_usefulness"] == fail_usefulness
  assert scored_contradiction["tool_usage"] == pass_tool_usage

  # 6. Contradictory pass=True + partial_usefulness overrides pass to False
  scored_pass_partial = await runner.judge_case(
      case,
      {
          "case_id": "case_1",
          "question": case["question"],
          "response": "PTO is 20 days.",
          "tools_called": ["lookup_policy"],
      },
  )
  assert scored_pass_partial["pass"] is False
  assert scored_pass_partial["response_usefulness"] == partial_usefulness
  assert scored_pass_partial["tool_usage"] == pass_tool_usage

  # 7. Contradictory pass=True + fail_usefulness overrides pass to False
  scored_pass_unhelpful = await runner.judge_case(
      case,
      {
          "case_id": "case_1",
          "question": case["question"],
          "response": "Contact HR.",
          "tools_called": ["lookup_policy"],
      },
  )
  assert scored_pass_unhelpful["pass"] is False
  assert scored_pass_unhelpful["response_usefulness"] == fail_usefulness
  assert scored_pass_unhelpful["tool_usage"] == pass_tool_usage


def test_build_report_from_eval_results_preserves_three_category_usefulness(
    tmp_path: Path,
):
  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  pass_usefulness, fail_usefulness, partial_usefulness = usefulness_cats[:3]
  proper_tool, partial_tool, missing_tool = tool_usage_cats[:3]

  eval_path = tmp_path / "eval_results.json"
  eval_path.write_text(
      json.dumps(
          [
              {
                  "case_id": "c1",
                  "question": "Q1",
                  "response": "R1",
                  "pass": True,
                  "response_usefulness": pass_usefulness,
                  "tool_usage": proper_tool,
                  "tools_called": ["get_pto"],
                  "reason": "Complete answer.",
              },
              {
                  "case_id": "c2",
                  "question": "Q2",
                  "response": "R2",
                  "pass": False,
                  "response_usefulness": partial_usefulness,
                  "tool_usage": partial_tool,
                  "tools_called": ["get_pto"],
                  "reason": "Missed carryover rule.",
              },
              {
                  "case_id": "c3",
                  "question": "Q3",
                  "response": "Contact HR.",
                  "pass": False,
                  "response_usefulness": fail_usefulness,
                  "tool_usage": missing_tool,
                  "tools_called": [],
                  "reason": "Deflected to HR.",
              },
          ]
      )
  )

  report = improver_agent.build_report_from_eval_results(str(eval_path))

  assert report["summary"] == {
      "total_sessions": 3,
      pass_usefulness: 1,
      "meaningful_rate": 33,
      partial_usefulness: 1,
      fail_usefulness: 1,
      "unhelpful_rate": 33,
  }
  categories = [
      s["metrics"]["response_usefulness"]["category"]
      for s in report["sessions"]
  ]
  assert categories == [pass_usefulness, partial_usefulness, fail_usefulness]
  tool_categories = [
      s["metrics"]["tool_usage"]["category"] for s in report["sessions"]
  ]
  assert tool_categories == [proper_tool, partial_tool, missing_tool]
  grounding_categories = [
      s["metrics"]["task_grounding"]["category"] for s in report["sessions"]
  ]
  assert grounding_categories == ["grounded", "ungrounded", "ungrounded"]
  assert report["sessions"][0]["tool_calls_detail"] == [
      {"name": "get_pto", "args": {}}
  ]


def test_read_quality_report_uses_partition_and_format_trajectory(
    monkeypatch: pytest.MonkeyPatch,
):
  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  pass_usefulness, fail_usefulness = usefulness_cats[0], usefulness_cats[1]
  missing_tool = tool_usage_cats[2]

  report = {
      "summary": {"total_sessions": 3},
      "sessions": [
          {
              "question": "Good question",
              "response": "Good answer",
              "metrics": {"response_usefulness": {"category": pass_usefulness}},
          },
          {
              "question": "Parroted question",
              "response": "Parroted answer",
              "metrics": {"response_usefulness": {"category": pass_usefulness}},
              "sub_trajectories": [{"outcome": "parroted"}],
          },
          {
              "question": "Deflected question",
              "response": "Ask HR",
              "metrics": {
                  "response_usefulness": {
                      "category": fail_usefulness,
                      "justification": "Deflected without tool call.",
                  },
                  "tool_usage": {"category": missing_tool},
              },
          },
      ],
  }
  monkeypatch.setitem(improver_agent._state, "report", report)

  payload = json.loads(improver_agent.read_quality_report())
  problems = payload["problem_sessions"]

  # Both the parroted session and the unhelpful session are included via
  # skill_evolution.partition_trajectories().
  assert [p["question"] for p in problems] == [
      "Parroted question",
      "Deflected question",
  ]
  assert problems[1]["tool_usage"] == missing_tool
  assert "Question: Deflected question" in problems[1]["trajectory"]
  assert "Justification: Deflected without tool call." in (
      problems[1]["trajectory"]
  )


def test_build_report_from_eval_results_legacy_and_off_vocab_fallback(
    tmp_path: Path,
):
  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  pass_usefulness, fail_usefulness, partial_usefulness = usefulness_cats[:3]
  proper_tool, partial_tool, missing_tool = tool_usage_cats[:3]

  eval_path = tmp_path / "legacy_and_off_vocab.json"
  eval_path.write_text(
      json.dumps(
          [
              {
                  "case_id": "legacy_pass",
                  "question": "Legacy pass question",
                  "response": "Legacy pass answer",
                  "pass": True,
                  "tools_called": ["get_pto"],
                  "reason": "Legacy pass without category fields.",
              },
              {
                  "case_id": "legacy_fail",
                  "question": "Legacy fail question",
                  "response": "Legacy fail answer",
                  "pass": False,
                  "tools_called": [],
                  "reason": "Legacy fail without category fields.",
              },
              {
                  "case_id": "caps_partial",
                  "question": "Capitalized partial question",
                  "response": "Half answer",
                  "pass": False,
                  "response_usefulness": "  Partial ",
                  "tool_usage": f"  {partial_tool.upper()}  ",
                  "tools_called": ["get_pto"],
                  "reason": "Capitalized labels.",
              },
              {
                  "case_id": "off_vocab_fail",
                  "question": "Off-vocab fail question",
                  "response": "Bad answer",
                  "pass": False,
                  "response_usefulness": "suboptimal",
                  "tool_usage": "bad_tool_call",
                  "tools_called": [],
                  "reason": "Off-vocabulary labels.",
              },
              {
                  "case_id": "contradictory_fail",
                  "question": "Contradictory fail question",
                  "response": "Ungrounded answer",
                  "pass": False,
                  "response_usefulness": pass_usefulness,
                  "tool_usage": missing_tool,
                  "tools_called": [],
                  "reason": "Failed tool rule despite meaningful text.",
              },
              {
                  "case_id": "contradictory_pass_partial",
                  "question": "Contradictory pass partial question",
                  "response": "Only half answered",
                  "pass": True,
                  "response_usefulness": partial_usefulness,
                  "tools_called": ["get_pto"],
                  "reason": "Pass=true with partial category.",
              },
              {
                  "case_id": "contradictory_pass_unhelpful",
                  "question": "Contradictory pass unhelpful question",
                  "response": "Deflected answer",
                  "pass": True,
                  "response_usefulness": fail_usefulness,
                  "tools_called": [],
                  "reason": "Pass=true with unhelpful category.",
              },
          ]
      )
  )

  report = improver_agent.build_report_from_eval_results(str(eval_path))

  assert report["summary"] == {
      "total_sessions": 7,
      pass_usefulness: 1,
      "meaningful_rate": 14,
      partial_usefulness: 2,
      fail_usefulness: 4,
      "unhelpful_rate": 57,
  }
  categories = [
      s["metrics"]["response_usefulness"]["category"]
      for s in report["sessions"]
  ]
  assert categories == [
      pass_usefulness,
      fail_usefulness,
      partial_usefulness,
      fail_usefulness,
      fail_usefulness,
      partial_usefulness,
      fail_usefulness,
  ]
  tool_categories = [
      s["metrics"]["tool_usage"]["category"] for s in report["sessions"]
  ]
  assert tool_categories == [
      proper_tool,
      missing_tool,
      partial_tool,
      missing_tool,
      missing_tool,
      missing_tool,
      missing_tool,
  ]
  grounding_categories = [
      s["metrics"]["task_grounding"]["category"] for s in report["sessions"]
  ]
  assert grounding_categories == [
      "grounded",
      "ungrounded",
      "ungrounded",
      "ungrounded",
      "ungrounded",
      "ungrounded",
      "ungrounded",
  ]

  # Every failed case (including legacy, capitalized, off-vocab, and both
  # directions of contradictory pass/category) must survive extraction.
  extracted = improver_agent.extract_failed_cases(report, tools=[])
  assert [c["question"] for c in extracted] == [
      "Legacy fail question",
      "Capitalized partial question",
      "Off-vocab fail question",
      "Contradictory fail question",
      "Contradictory pass partial question",
      "Contradictory pass unhelpful question",
  ]


@pytest.mark.asyncio
async def test_test_candidate_preserves_canonical_categories_in_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  pass_usefulness, fail_usefulness, partial_usefulness = usefulness_cats[:3]
  proper_tool, partial_tool = tool_usage_cats[:2]

  eval_cases_path = tmp_path / "eval_cases.json"
  eval_cases_path.write_text(
      json.dumps(
          {
              "eval_cases": [
                  {
                      "id": "c1",
                      "question": "Q1",
                      "expected_tool": "lookup_policy",
                  },
                  {
                      "id": "c2",
                      "question": "Q2",
                      "expected_tool": "lookup_policy",
                  },
              ]
          }
      )
  )

  # Drive the real EvalRunner.run_golden_eval and test_candidate when the judge
  # returns pass=True alongside each canonical failure label (partial and
  # unhelpful). Both cases must be reconciled to pass=False so the regression
  # gate rejects the candidate (all_passed=False, passed=0) and CLI exits 1.
  verdicts = iter(
      [
          {
              "pass": True,
              "response_usefulness": partial_usefulness,
              "tool_usage": partial_tool,
              "reason": "Only half answered.",
          },
          {
              "pass": True,
              "response_usefulness": fail_usefulness,
              "tool_usage": proper_tool,
              "reason": "Unhelpful response.",
          },
      ]
  )

  class FakeModels:

    def generate_content(self, **kwargs):
      return SimpleNamespace(text=json.dumps(next(verdicts)))

  class FakeClient:

    def __init__(self, *args, **kwargs):
      self.models = FakeModels()

  class FakeSessionService:

    async def create_session(self, **kwargs):
      return SimpleNamespace(id="sess_1")

  class FakeInMemoryRunner:

    def __init__(self, **kwargs):
      self.app_name = kwargs.get("app_name", "eval_agent")
      self.session_service = FakeSessionService()

    async def run_async(self, **kwargs):
      yield SimpleNamespace(
          content=SimpleNamespace(
              parts=[
                  SimpleNamespace(
                      text="Stubbed agent answer",
                      function_call=SimpleNamespace(name="lookup_policy"),
                  )
              ]
          )
      )

  monkeypatch.setattr("agent_improvement.eval_runner.genai.Client", FakeClient)
  monkeypatch.setattr(
      "agent_improvement.eval_runner.InMemoryRunner", FakeInMemoryRunner
  )
  config = SimpleNamespace(
      eval_cases_path=str(eval_cases_path),
      agent_factory=lambda p: None,
      model_id="gemini-2.5-flash",
      judge_prompt=None,
      prompt_adapter=SimpleNamespace(read_prompt=lambda: ("prompt v1", 1)),
  )
  monkeypatch.setitem(improver_agent._state, "config", config)

  payload = json.loads(await improver_agent.test_candidate("candidate prompt"))
  assert payload["all_passed"] is False
  assert payload["passed"] == 0
  assert payload["total"] == 2
  assert payload["results"] == [
      {
          "case_id": "c1",
          "pass": False,
          "response_usefulness": partial_usefulness,
          "tool_usage": partial_tool,
          "reason": "Only half answered.",
      },
      {
          "case_id": "c2",
          "pass": False,
          "response_usefulness": fail_usefulness,
          "tool_usage": proper_tool,
          "reason": "Unhelpful response.",
      },
  ]


def test_run_eval_golden_cli_exits_nonzero_on_failure_categories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
  import importlib.util

  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  _, fail_usefulness, partial_usefulness = usefulness_cats[:3]
  proper_tool, partial_tool = tool_usage_cats[:2]

  eval_cases_path = tmp_path / "eval_cases.json"
  eval_cases_path.write_text(
      json.dumps(
          {
              "eval_cases": [
                  {
                      "id": "c1",
                      "question": "Q1",
                      "expected_tool": "lookup_policy",
                  },
                  {
                      "id": "c2",
                      "question": "Q2",
                      "expected_tool": "lookup_policy",
                  },
              ]
          }
      )
  )

  verdicts = iter(
      [
          {
              "pass": True,
              "response_usefulness": partial_usefulness,
              "tool_usage": partial_tool,
              "reason": "Only half answered.",
          },
          {
              "pass": True,
              "response_usefulness": fail_usefulness,
              "tool_usage": proper_tool,
              "reason": "Unhelpful response.",
          },
      ]
  )

  class FakeModels:

    def generate_content(self, **kwargs):
      return SimpleNamespace(text=json.dumps(next(verdicts)))

  class FakeClient:

    def __init__(self, *args, **kwargs):
      self.models = FakeModels()

  class FakeSessionService:

    async def create_session(self, **kwargs):
      return SimpleNamespace(id="sess_1")

  class FakeInMemoryRunner:

    def __init__(self, **kwargs):
      self.app_name = kwargs.get("app_name", "eval_agent")
      self.session_service = FakeSessionService()

    async def run_async(self, **kwargs):
      yield SimpleNamespace(
          content=SimpleNamespace(
              parts=[
                  SimpleNamespace(
                      text="Stubbed agent answer",
                      function_call=SimpleNamespace(name="lookup_policy"),
                  )
              ]
          )
      )

  config = SimpleNamespace(
      eval_cases_path=str(eval_cases_path),
      agent_factory=lambda p: None,
      model_id="gemini-2.5-flash",
      judge_prompt=None,
      prompt_adapter=SimpleNamespace(read_prompt=lambda: ("prompt v1", 1)),
  )

  monkeypatch.setattr("agent_improvement.eval_runner.genai.Client", FakeClient)
  monkeypatch.setattr(
      "agent_improvement.eval_runner.InMemoryRunner", FakeInMemoryRunner
  )
  monkeypatch.setattr("agent_improvement.load_config", lambda p=None: config)
  monkeypatch.setattr(
      sys,
      "argv",
      [
          "run_eval.py",
          "--golden",
          "--eval-cases",
          str(eval_cases_path),
          "--output-dir",
          str(tmp_path),
      ],
  )

  run_eval_spec = importlib.util.spec_from_file_location(
      "run_eval_cli", _DEMO_DIR / "eval" / "run_eval.py"
  )
  assert run_eval_spec and run_eval_spec.loader
  run_eval_mod = importlib.util.module_from_spec(run_eval_spec)
  run_eval_spec.loader.exec_module(run_eval_mod)

  with pytest.raises(SystemExit) as exc_info:
    run_eval_mod.main()
  assert exc_info.value.code == 1
  written = json.loads((tmp_path / "latest_eval_results.json").read_text())
  assert [r["pass"] for r in written] == [False, False]
  assert [r["response_usefulness"] for r in written] == [
      partial_usefulness,
      fail_usefulness,
  ]


@pytest.mark.asyncio
async def test_improver_call_sites_use_partition_and_format_trajectory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  pass_usefulness, fail_usefulness, partial_usefulness = usefulness_cats[:3]
  missing_tool = tool_usage_cats[2]

  sessions = [
      {
          "question": "Meaningful question",
          "response": "Meaningful answer",
          "metrics": {"response_usefulness": {"category": pass_usefulness}},
      },
      {
          "question": "Parroted question",
          "response": "Parroted answer",
          "metrics": {"response_usefulness": {"category": pass_usefulness}},
          "sub_trajectories": [{"outcome": "parroted"}],
      },
      {
          "question": "Partial question",
          "response": "Partial answer",
          "metrics": {
              "response_usefulness": {
                  "category": partial_usefulness,
                  "justification": "Missed detail.",
              }
          },
      },
      {
          "question": "Unhelpful question",
          "response": "Unhelpful answer",
          "metrics": {
              "response_usefulness": {
                  "category": fail_usefulness,
                  "justification": "Did not call tool.",
              },
              "tool_usage": {"category": missing_tool},
          },
      },
  ]
  report = {
      "summary": {
          "total_sessions": 4,
          pass_usefulness: 2,
          "meaningful_rate": 50,
          partial_usefulness: 1,
          fail_usefulness: 1,
          "unhelpful_rate": 25,
      },
      "sessions": list(sessions),
  }

  # 1. extract_failed_cases uses partition_trajectories
  extracted = improver_agent.extract_failed_cases(report, tools=[])
  assert [c["question"] for c in extracted] == [
      "Parroted question",
      "Partial question",
      "Unhelpful question",
  ]

  # 2. _generate_via_gemini uses partition_trajectories and format_trajectory
  captured_prompts: list[str] = []

  class FakeModels:

    def generate_content(self, **kwargs):
      captured_prompts.append(kwargs["contents"])
      return SimpleNamespace(
          text=json.dumps(
              {"improved_prompt": "v2 prompt", "changes_summary": "updated"}
          )
      )

  class FakeClient:

    def __init__(self, *args, **kwargs):
      self.models = FakeModels()

  monkeypatch.setattr(
      "agent_improvement.improver_agent.genai.Client", FakeClient
  )
  config = SimpleNamespace(
      agent_tools=[],
      model_id="gemini-2.5-flash",
      eval_cases_path=str(tmp_path / "eval_cases.json"),
      quality_threshold=0.9,
      max_failure_extract="all",
      prompt_adapter=SimpleNamespace(read_prompt=lambda: ("v1", 1)),
  )
  monkeypatch.setitem(improver_agent._state, "report", report)

  improver_agent._generate_via_gemini(config, "v1 prompt", 1)
  assert len(captured_prompts) == 1
  gemini_prompt = captured_prompts[0]
  assert "Question: Meaningful question" not in gemini_prompt
  assert "Question: Parroted question" in gemini_prompt
  assert "Question: Partial question" in gemini_prompt
  assert "Justification: Missed detail." in gemini_prompt
  assert "Question: Unhelpful question" in gemini_prompt
  assert "Justification: Did not call tool." in gemini_prompt

  # When only meaningful non-parroted sessions exist, fallback text is used
  monkeypatch.setitem(
      improver_agent._state,
      "report",
      {"summary": report["summary"], "sessions": [sessions[0]]},
  )
  improver_agent._generate_via_gemini(config, "v1 prompt", 1)
  assert "No problem sessions found." in captured_prompts[-1]

  # 3. _generate_via_vertex_optimizer uses partition_trajectories
  fake_vertexai = SimpleNamespace(Client=object)
  fake_common = SimpleNamespace(OptimizeConfig=object, OptimizeTarget=object)
  monkeypatch.setitem(sys.modules, "pandas", SimpleNamespace(DataFrame=list))
  monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
  monkeypatch.setitem(sys.modules, "vertexai._genai", SimpleNamespace())
  monkeypatch.setitem(sys.modules, "vertexai._genai.types", SimpleNamespace())
  monkeypatch.setitem(sys.modules, "vertexai._genai.types.common", fake_common)

  # All meaningful -> early exit before _generate_ground_truth
  no_problem_res = json.loads(
      await improver_agent._generate_via_vertex_optimizer(config, "v1 prompt")
  )
  assert (
      no_problem_res["changes_summary"]
      == "No problem sessions to optimize against."
  )

  # Mixed sessions -> passes only partitioned failures to _generate_ground_truth
  gt_called_with: list[dict] = []

  async def fake_generate_ground_truth(cfg, failed_list):
    gt_called_with.extend(failed_list)
    return []

  monkeypatch.setattr(
      "agent_improvement.improver_agent._generate_ground_truth",
      fake_generate_ground_truth,
  )
  monkeypatch.setitem(improver_agent._state, "report", report)
  monkeypatch.setitem(improver_agent._state, "output_dir", str(tmp_path))

  await improver_agent._generate_via_vertex_optimizer(config, "v1 prompt")
  assert [s["question"] for s in gt_called_with] == [
      "Parroted question",
      "Partial question",
      "Unhelpful question",
  ]

  # 4. run_improvement narrows report["sessions"] via partition_trajectories
  run_report = {
      "summary": {
          "total_sessions": 4,
          pass_usefulness: 4,
          "meaningful_rate": 95,
          partial_usefulness: 0,
          fail_usefulness: 0,
          "unhelpful_rate": 0,
      },
      "sessions": list(sessions),
  }
  result = await improver_agent.run_improvement(config, report=run_report)
  assert result["improvement_result"] == "already_good"
  assert [s["question"] for s in run_report["sessions"]] == [
      "Parroted question",
      "Partial question",
      "Unhelpful question",
  ]
