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
  assert expected_usefulness_choices in prompts.JUDGE_PROMPT
  assert expected_tool_choices in prompts.JUDGE_PROMPT

  rendered = prompts.JUDGE_PROMPT.format(
      question="How many PTO days?",
      response="You get 20 PTO days.",
      tool_check="Expected tool 'get_pto' was called.",
      tool_fail_rule="",
  )
  assert "Question: How many PTO days?" in rendered
  assert '"pass": true or false' in rendered


@pytest.mark.asyncio
async def test_eval_runner_judge_case_records_canonical_categories(
    monkeypatch: pytest.MonkeyPatch,
):
  usefulness_cats, tool_usage_cats = prompts.get_rubric_categories()
  partial_usefulness = usefulness_cats[-1]
  partial_tool_usage = tool_usage_cats[1]

  class FakeModels:

    def generate_content(self, **kwargs):
      return SimpleNamespace(
          text=json.dumps(
              {
                  "pass": False,
                  "response_usefulness": partial_usefulness,
                  "tool_usage": partial_tool_usage,
                  "reason": "Answered only half the question.",
              }
          )
      )

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
  result = {
      "case_id": "case_1",
      "question": case["question"],
      "response": "PTO is 20 days.",
      "tools_called": ["lookup_policy"],
  }

  scored = await runner.judge_case(case, result)

  assert scored["pass"] is False
  assert scored["response_usefulness"] == partial_usefulness
  assert scored["tool_usage"] == partial_tool_usage
  assert scored["reason"] == "Answered only half the question."


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
