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

"""Eval runner: send cases to an agent, score responses with LLM judge."""

from __future__ import annotations

import asyncio
import json
from typing import Callable, TYPE_CHECKING

from agent_improvement.prompts import JUDGE_PROMPT
from google import genai
from google.adk.runners import InMemoryRunner
from google.genai.types import Content
from google.genai.types import GenerateContentConfig
from google.genai.types import HttpOptions
from google.genai.types import HttpRetryOptions
from google.genai.types import Part

if TYPE_CHECKING:
  from google.adk.agents import Agent


class EvalRunner:
  """Runs eval cases against an agent and scores with an LLM judge.

  Args:
      agent_factory: Callable that takes a prompt string and returns
          an ADK Agent.
      model_id: Gemini model for the LLM judge.
      judge_prompt: Template for the judge prompt. Must contain
          ``{question}`` and ``{response}`` placeholders.
  """

  def __init__(
      self,
      agent_factory: Callable[[str], Agent],
      model_id: str = "gemini-2.5-flash",
      judge_prompt: str | None = None,
  ) -> None:
    self._agent_factory = agent_factory
    self._model_id = model_id
    self._judge_prompt = judge_prompt or JUDGE_PROMPT
    self._client = genai.Client(
        http_options=HttpOptions(
            retry_options=HttpRetryOptions(
                attempts=3,
                initial_delay=10.0,
                http_status_codes=[429],
            )
        )
    )

  def load_eval_cases(self, path: str) -> list[dict]:
    """Load evaluation cases from a JSON file."""
    with open(path) as f:
      data = json.load(f)
    return data.get("eval_cases", [])

  async def run_single_case(
      self,
      runner: InMemoryRunner,
      case: dict,
      user_id: str = "eval_user",
  ) -> dict:
    """Run a single eval case and return the response.

    Captures both text output and tool call events so the judge can
    verify tool usage with objective data rather than guessing from
    response text.
    """
    session = await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id=user_id,
    )
    user_message = Content(
        role="user",
        parts=[Part(text=case["question"])],
    )
    response_text = ""
    tools_called: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=user_message,
    ):
      if event.content and event.content.parts:
        for part in event.content.parts:
          if part.text:
            response_text += part.text
          if hasattr(part, "function_call") and part.function_call:
            tools_called.append(part.function_call.name)

    return {
        "case_id": case["id"],
        "question": case["question"],
        "category": case.get("category", ""),
        "response": response_text,
        "tools_called": tools_called,
        "session_id": session.id,
    }

  async def judge_case(self, case: dict, result: dict) -> dict:
    """Score a single case response with the LLM judge.

    Uses objective tool-call data captured by ``run_single_case``
    rather than asking the judge to infer tool usage from response
    text.
    """
    expected_tool = case.get("expected_tool", "")
    tools_called = result.get("tools_called", [])

    if expected_tool and expected_tool != "unknown":
      if expected_tool in tools_called:
        tool_check = (
            f"Expected tool '{expected_tool}' was called. "
            f"Tools called: {', '.join(tools_called)}"
        )
        tool_fail_rule = ""
      else:
        tool_check = (
            f"Expected tool '{expected_tool}' was NOT called. "
            f"Tools called: {', '.join(tools_called) or 'none'}"
        )
        tool_fail_rule = (
            f"\nA response FAILS if the expected tool "
            f"'{expected_tool}' was not called, as the answer "
            f"may be hallucinated without tool grounding."
        )
    else:
      tool_check = ""
      tool_fail_rule = ""

    judge_prompt = self._judge_prompt.format(
        question=case["question"],
        response=result["response"][:500],
        tool_check=tool_check,
        tool_fail_rule=tool_fail_rule,
    )
    judge_response = self._client.models.generate_content(
        model=self._model_id,
        contents=judge_prompt,
        config=GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    verdict = json.loads(judge_response.text)
    result["pass"] = verdict.get("pass", False)
    result["reason"] = verdict.get("reason", "")
    return result

  async def run_golden_eval(
      self,
      prompt: str,
      eval_cases_path: str,
  ) -> tuple[bool, int, int, list[dict]]:
    """Run the golden eval set against a candidate prompt.

    Args:
        prompt: The candidate prompt to evaluate.
        eval_cases_path: Path to the golden eval set JSON.

    Returns:
        ``(all_passed, passed_count, total, results)``
    """
    cases = self.load_eval_cases(eval_cases_path)
    agent = self._agent_factory(prompt)
    runner = InMemoryRunner(agent=agent, app_name="eval_agent")

    # Limit concurrent LLM calls to avoid 429 rate-limit errors.
    semaphore = asyncio.Semaphore(5)

    async def _eval_one(case: dict) -> dict:
      async with semaphore:
        result = await self.run_single_case(runner, case, user_id="eval")
        result = await self.judge_case(case, result)
      tag = "PASS" if result["pass"] else "FAIL"
      tools_called = ", ".join(result.get("tools_called", [])) or "none"
      expected_tool = case.get("expected_tool", "unknown")
      answer = result["response"].replace("\n", " ").strip()
      if len(answer) > 120:
        answer = answer[:120] + "..."
      print(f"    {tag}: {case['id']}")
      print(f"         Question: {case['question']}")
      print(f"         Answer: {answer}")
      print(
          f"         Tools called: {tools_called} | Expected: {expected_tool}"
      )
      if not result["pass"]:
        print(f"         Reason: {result['reason']}")
      return result

    results = list(await asyncio.gather(*[_eval_one(c) for c in cases]))
    passed = sum(1 for r in results if r.get("pass", False))
    return passed == len(cases), passed, len(cases), results
