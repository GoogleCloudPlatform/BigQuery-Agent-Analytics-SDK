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

"""LoopAgent-based prompt improver.

Creates a LoopAgent wrapping a single LlmAgent that has tools to:
1. Read the quality report and current prompt
2. Generate a candidate improved prompt via Gemini
3. Test the candidate against the golden eval set
4. Write the validated prompt and exit the loop

The loop exits when all golden cases pass, or after ``max_iterations``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("google.genai").setLevel(logging.ERROR)

from agent_improvement.config import ImprovementConfig
from agent_improvement.eval_runner import EvalRunner
from agent_improvement.prompts import IMPROVER_PROMPT
from agent_improvement.tool_introspection import format_tool_signatures
from google import genai
from google.adk.agents import Agent
from google.adk.agents import LoopAgent
from google.adk.models import Gemini
from google.adk.runners import InMemoryRunner
from google.adk.tools import exit_loop
from google.genai import types
from google.genai.types import Content
from google.genai.types import GenerateContentConfig
from google.genai.types import HttpOptions
from google.genai.types import HttpRetryOptions
from google.genai.types import Part

# ---------------------------------------------------------------------------
# Shared state — set by run_improvement(), read by tool functions.
#
# This module-level dict assumes a single improvement cycle runs per
# process. Concurrent cycles in the same process would clobber each
# other's state. This is fine for CLI usage and the LoopAgent runner,
# which are inherently single-cycle-at-a-time.
# ---------------------------------------------------------------------------

_state: dict = {}


# ---------------------------------------------------------------------------
# Tool functions for the inner LlmAgent
# ---------------------------------------------------------------------------


def read_quality_report() -> str:
  """Read the quality report that triggered this improvement cycle.

  Returns a JSON summary of problem sessions including questions,
  responses, and usefulness scores.
  """
  report = _state.get("report", {})
  summary = report.get("summary", {})

  problem_sessions = []
  for session in report.get("sessions", []):
    metrics = session.get("metrics", {})
    usefulness = metrics.get("response_usefulness", {})
    cat = usefulness.get("category", "unknown")
    if cat not in ("unhelpful", "partial"):
      continue
    problem_sessions.append(
        {
            "question": session.get("question", "?"),
            "response": session.get("response", "")[:300],
            "usefulness": cat,
            "justification": usefulness.get("justification", ""),
        }
    )

  return json.dumps(
      {
          "summary": summary,
          "problem_sessions": problem_sessions,
      },
      indent=2,
  )


def read_current_prompt() -> str:
  """Read the current agent prompt and version.

  Returns JSON with the prompt text, version number, and available
  tool signatures.
  """
  config: ImprovementConfig = _state["config"]
  prompt_text, version = config.prompt_adapter.read_prompt()
  tool_sigs = format_tool_signatures(config.agent_tools)
  return json.dumps(
      {
          "version": version,
          "prompt": prompt_text,
          "tool_signatures": tool_sigs,
      },
      indent=2,
  )


def _generate_via_gemini(
    config: ImprovementConfig, current_prompt: str, current_version: int
) -> str:
  """Generate an improved prompt using raw Gemini generation."""
  report = _state.get("report", {})
  summary = report.get("summary", {})

  lines = []
  for session in report.get("sessions", []):
    metrics = session.get("metrics", {})
    usefulness = metrics.get("response_usefulness", {})
    cat = usefulness.get("category", "unknown")
    if cat not in ("unhelpful", "partial"):
      continue
    lines.append(f"- Question: {session.get('question', '?')}")
    resp = session.get("response", "")[:300]
    lines.append(f"  Response: {resp}")
    lines.append(f"  Usefulness: {cat}")
    lines.append("")

  problem_text = "\n".join(lines) or "No problem sessions found."
  tool_sigs = format_tool_signatures(config.agent_tools)

  prompt = IMPROVER_PROMPT.format(
      current_version=current_version,
      current_prompt=current_prompt,
      total_sessions=summary.get("total_sessions", 0),
      meaningful=summary.get("meaningful", 0),
      meaningful_rate=summary.get("meaningful_rate", 0),
      partial=summary.get("partial", 0),
      unhelpful=summary.get("unhelpful", 0),
      unhelpful_rate=summary.get("unhelpful_rate", 0),
      problem_sessions=problem_text,
      tool_signatures=tool_sigs,
  )

  client = genai.Client(
      http_options=HttpOptions(
          retry_options=HttpRetryOptions(
              attempts=3,
              initial_delay=10.0,
              http_status_codes=[429],
          )
      )
  )
  response = client.models.generate_content(
      model=config.model_id,
      contents=prompt,
      config=GenerateContentConfig(
          temperature=0.2,
          response_mime_type="application/json",
      ),
  )
  return response.text


async def _generate_ground_truth(
    config: ImprovementConfig, failed_sessions: list[dict]
) -> list[dict]:
  """Generate synthetic ground truth for failed sessions.

  Uses a "teacher agent" -- the same agent factory with a prompt
  that explicitly instructs tool usage -- to produce reference
  answers for each failed question.  The teacher runs with the
  same tools as the target agent, so its answers are grounded in
  real tool output.

  Returns a list of dicts with ``question``, ``bad_response``, and
  ``ground_truth`` keys.
  """
  teacher_prompt = (
      "You are an expert assistant. For EVERY question, you MUST call "
      "the available tools to look up the answer. NEVER say 'I don't "
      "know' or defer the user elsewhere. ALWAYS use the tools first, "
      "then answer based on the tool results. Be specific and thorough."
  )
  if config.teacher_model_id:
    teacher_agent = config.agent_factory(
        teacher_prompt, model_id=config.teacher_model_id
    )
  else:
    teacher_agent = config.agent_factory(teacher_prompt)
  runner = InMemoryRunner(agent=teacher_agent, app_name="teacher_agent")

  async def _get_answer(session: dict) -> dict:
    question = session.get("question", "")
    sess = await runner.session_service.create_session(
        app_name="teacher_agent", user_id="teacher"
    )
    msg = Content(role="user", parts=[Part(text=question)])
    answer = ""
    async for event in runner.run_async(
        user_id="teacher", session_id=sess.id, new_message=msg
    ):
      if event.content and event.content.parts:
        for part in event.content.parts:
          if part.text:
            answer += part.text
    return {
        "question": question,
        "bad_response": session.get("response", "")[:500],
        "ground_truth": answer,
    }

  results = list(
      await asyncio.gather(*[_get_answer(s) for s in failed_sessions])
  )
  return [r for r in results if r["ground_truth"].strip()]


async def _generate_via_vertex_optimizer(
    config: ImprovementConfig, current_prompt: str
) -> str:
  """Generate an improved prompt using Vertex AI Prompt Optimizer.

  Workflow:

  1. Extract failed sessions from the quality report.
  2. Run a **teacher agent** (same tools, better prompt) on each
     failed question to produce synthetic ground truth.
  3. Feed the original prompt + (question, bad_response,
     ground_truth) triples to the Vertex AI Prompt Optimizer in
     ``target_response`` mode.
  4. Return the optimizer's improved system instructions.
  """
  import pandas as pd
  from vertexai import Client
  from vertexai._genai.types.common import OptimizeConfig
  from vertexai._genai.types.common import OptimizeTarget

  report = _state.get("report", {})

  # Collect failed sessions
  failed = []
  for session in report.get("sessions", []):
    metrics = session.get("metrics", {})
    usefulness = metrics.get("response_usefulness", {})
    cat = usefulness.get("category", "unknown")
    if cat in ("unhelpful", "partial"):
      failed.append(session)

  if not failed:
    return json.dumps(
        {
            "improved_prompt": current_prompt,
            "changes_summary": "No problem sessions to optimize against.",
        }
    )

  # Generate ground truth via teacher agent
  print("  Generating synthetic ground truth via teacher agent...")
  gt_results = await _generate_ground_truth(config, failed)
  print(f"  Generated {len(gt_results)} ground truth answers.")

  # Save ground truth to reports/ for inspection
  gt_dir = os.path.join(
      os.path.dirname(config.eval_cases_path), "..", "reports"
  )
  os.makedirs(gt_dir, exist_ok=True)
  gt_path = os.path.join(gt_dir, "ground_truth_latest.json")
  with open(gt_path, "w") as f:
    json.dump(gt_results, f, indent=2)
  print(f"  Ground truth saved to {gt_path}")
  print("")
  print("  Agent (bad) vs Teacher (ground truth) comparison:")
  print("  " + "─" * 68)
  for i, r in enumerate(gt_results, 1):
    q = r["question"]
    bad = r["bad_response"][:200].replace("\n", " ").strip()
    good = r["ground_truth"][:200].replace("\n", " ").strip()
    print(f"  Q{i}: {q}")
    print(f"    Agent:   {bad}")
    print(f"    Teacher: {good}")
    print("")
  print("  " + "─" * 68)

  if not gt_results:
    return json.dumps(
        {
            "improved_prompt": current_prompt,
            "changes_summary": "Teacher agent could not generate ground truth.",
        }
    )

  # Build DataFrame for target_response mode
  rows = []
  for r in gt_results:
    rows.append(
        {
            "prompt": r["question"],
            "model_response": r["bad_response"],
            "target_response": r["ground_truth"],
        }
    )

  df = pd.DataFrame(rows)

  # Build an augmented prompt for the optimizer. Two critical additions:
  # 1. A tool-use directive so the optimizer doesn't just inline data
  # 2. Tool signatures so it knows what's available
  tool_sigs = format_tool_signatures(config.agent_tools)
  tool_use_directive = (
      "\n\nIMPORTANT: You have access to tools that contain complete, "
      "up-to-date information. For EVERY question, you MUST call the "
      "appropriate tool to look up the answer. Do NOT answer from "
      "memory or from the information listed above. The tools are the "
      "authoritative source. NEVER say 'I don't have that information' "
      "or defer the user elsewhere without first calling a tool."
      "\n\nAVAILABLE TOOLS:\n" + tool_sigs
  )
  prompt_with_tools = current_prompt + tool_use_directive

  print(
      f"  Calling Vertex AI Prompt Optimizer with {len(gt_results)} "
      "ground truth examples..."
  )
  print("  (The optimizer is a server-side job — typically 2-4 minutes.)")

  from google.genai.types import HttpOptions
  from google.genai.types import HttpRetryOptions

  client = Client(
      location=config.vertex_location,
      http_options=HttpOptions(
          retry_options=HttpRetryOptions(
              attempts=6,
              initial_delay=2.0,
              max_delay=60.0,
              exp_base=2.0,
              http_status_codes=[429, 503],
          ),
      ),
  )

  # Run the blocking optimizer call in a thread so we can print
  # progress while it works. Without this the event loop is blocked
  # and nothing prints until the call returns.
  async def _progress_indicator():
    elapsed = 0
    while True:
      await asyncio.sleep(15)
      elapsed += 15
      print(f"  ... still optimizing ({elapsed}s elapsed)", flush=True)

  progress_task = asyncio.create_task(_progress_indicator())
  try:
    result = await asyncio.to_thread(
        client.prompts.optimize,
        prompt=prompt_with_tools,
        config=OptimizeConfig(
            optimization_target=(
                OptimizeTarget.OPTIMIZATION_TARGET_FEW_SHOT_TARGET_RESPONSE
            ),
            examples_dataframe=df,
        ),
    )
  finally:
    progress_task.cancel()

  print("  Optimizer returned a candidate prompt.")

  parsed = result.parsed_response
  if (
      hasattr(parsed, "new_system_instructions")
      and parsed.new_system_instructions
  ):
    improved = parsed.new_system_instructions
    changes = "Vertex AI Prompt Optimizer (target_response mode)"
  elif hasattr(parsed, "suggested_prompt") and parsed.suggested_prompt:
    improved = parsed.suggested_prompt
    changes = "Vertex AI Prompt Optimizer (guideline mode)"
  else:
    improved = current_prompt
    changes = "Optimizer returned no changes"

  # The optimizer rewrites the entire prompt and strips tool
  # instructions. Re-append them so the agent actually uses tools
  # instead of relying on data inlined by the optimizer.
  tool_names = [getattr(t, "__name__", "") for t in config.agent_tools]
  has_tool_ref = any(name and name in improved for name in tool_names)
  if not has_tool_ref and tool_sigs:
    improved += (
        "\n\nIMPORTANT: You have access to tools that contain complete, "
        "up-to-date information. For EVERY question, you MUST call the "
        "appropriate tool to look up the answer. Do NOT rely solely on "
        "the information above -- the tools may have more details. "
        "NEVER say 'I don't have that information' without first "
        "calling a tool."
        "\n\nAVAILABLE TOOLS:\n" + tool_sigs
    )
    changes += " + tool-use directive re-appended"

  return json.dumps(
      {
          "improved_prompt": improved,
          "changes_summary": changes,
      }
  )


async def generate_candidate(current_prompt: str, current_version: int) -> str:
  """Generate an improved prompt candidate.

  Uses the Vertex AI Prompt Optimizer when ``use_vertex_optimizer``
  is enabled in config, otherwise falls back to raw Gemini generation.

  Args:
      current_prompt: The current agent prompt text.
      current_version: The current prompt version number.

  Returns:
      JSON with ``improved_prompt`` and ``changes_summary`` fields.
  """
  config: ImprovementConfig = _state["config"]

  if config.use_vertex_optimizer:
    return await _generate_via_vertex_optimizer(config, current_prompt)
  return _generate_via_gemini(config, current_prompt, current_version)


async def test_candidate(candidate_prompt: str) -> str:
  """Test a candidate prompt against the golden eval set.

  Args:
      candidate_prompt: The full text of the candidate prompt.

  Returns:
      JSON with ``all_passed``, ``passed``, ``total``, and per-case
      results.
  """
  config: ImprovementConfig = _state["config"]

  cases_count = 0
  try:
    with open(config.eval_cases_path) as _f:
      cases_count = len(json.load(_f).get("eval_cases", []))
  except Exception:
    pass
  print(
      f"\n  Testing candidate prompt against {cases_count} golden eval "
      "cases (regression gate)..."
  )

  eval_runner = EvalRunner(
      agent_factory=config.agent_factory,
      model_id=config.model_id,
      judge_prompt=config.judge_prompt,
  )
  all_passed, passed, total, results = await eval_runner.run_golden_eval(
      candidate_prompt, config.eval_cases_path
  )
  return json.dumps(
      {
          "all_passed": all_passed,
          "passed": passed,
          "total": total,
          "results": [
              {
                  "case_id": r["case_id"],
                  "pass": r.get("pass", False),
                  "reason": r.get("reason", ""),
              }
              for r in results
          ],
      },
      indent=2,
  )


def write_prompt(candidate_prompt: str, changes_summary: str) -> str:
  """Write a validated candidate prompt to storage.

  Only call this AFTER test_candidate returns all_passed=true.

  Args:
      candidate_prompt: The full text of the improved prompt.
      changes_summary: Brief description of what changed.

  Returns:
      Confirmation message with the new version number.
  """
  config: ImprovementConfig = _state["config"]
  _, current_version = config.prompt_adapter.read_prompt()
  new_version = config.prompt_adapter.write_prompt(
      candidate_prompt, current_version, changes_summary
  )
  return f"Written PROMPT_V{new_version}. Changes: {changes_summary}"


# ---------------------------------------------------------------------------
# Failure extraction helpers
# ---------------------------------------------------------------------------

_REQUIRED_CASE_KEYS = {"id", "question", "category", "expected_tool"}

# Common words that cause false-positive keyword matches.
_CLASSIFIER_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "get",
        "set",
        "not",
        "one",
        "use",
        "per",
        "any",
        "all",
        "can",
        "may",
        "has",
        "had",
        "its",
        "our",
        "out",
        "who",
        "did",
        "now",
        "her",
        "his",
        "with",
        "from",
        "this",
        "that",
        "have",
        "will",
        "been",
        "into",
        "over",
        "also",
        "than",
        "them",
        "then",
        "they",
        "what",
        "when",
        "each",
        "some",
        "only",
        "such",
        "more",
        "most",
        "many",
        "much",
        "must",
        "here",
        "well",
        "very",
        "does",
        "done",
        "just",
        "like",
        "make",
        "take",
        "give",
        "come",
        "back",
        "after",
        "about",
        "could",
        "would",
        "should",
        "args",
        "string",
        "returns",
        "dictionary",
        "error",
        "message",
        "found",
        "available",
        "requested",
    }
)


def _word_forms(word: str) -> list[str]:
  """Return the word plus common de-suffixed forms (plural, -ing, -ly)."""
  forms = [word]
  if len(word) > 4:
    if word.endswith("ing"):
      forms.append(word[:-3])  # working -> work
    elif word.endswith("ly"):
      forms.append(word[:-2])  # remotely -> remote
    elif word.endswith("ed"):
      forms.append(word[:-2])  # requested -> request
  if len(word) > 3 and word.endswith("s"):
    forms.append(word[:-1])  # expenses -> expense
  return forms


def _classify_question(question: str, tools: list) -> tuple[str, str]:
  """Infer category and expected_tool from available tools.

  Matches question words against tool names and full docstrings
  (including parameter descriptions) to determine which tool should
  handle the question. Uses word-boundary splitting, basic plural
  normalization, and scoring to pick the best match.

  Returns ``("unknown", "unknown")`` if no tool matches.
  """
  if not tools:
    return "unknown", "unknown"

  # Single tool: always the expected tool
  if len(tools) == 1:
    name = getattr(tools[0], "__name__", "unknown")
    return name, name

  # Build a set of normalized question words.
  q_words: set[str] = set()
  for w in re.split(r"\W+", question.lower()):
    if w:
      q_words.update(_word_forms(w))

  best_tool = None
  best_score = 0

  for tool in tools:
    name = getattr(tool, "__name__", "")
    doc = (getattr(tool, "__doc__", "") or "").lower()

    # Extract keywords from tool name (> 3 chars to skip "get", "set").
    name_keywords = [
        w
        for w in name.lower().replace("_", " ").split()
        if len(w) > 3 and w not in _CLASSIFIER_STOP
    ]

    # Extract from full docstring (>= 3 chars, filtered by stop words).
    doc_words: list[str] = []
    for token in doc.replace(",", " ").replace(".", " ").split():
      token = token.strip("()[]:'\"")
      if "_" in token:
        doc_words.extend(
            w
            for w in token.split("_")
            if len(w) > 2 and w not in _CLASSIFIER_STOP
        )
      elif len(token) > 2 and token not in _CLASSIFIER_STOP:
        doc_words.append(token)

    # Build normalized keyword set (original + de-suffixed forms).
    keywords: set[str] = set()
    for kw in name_keywords + doc_words:
      keywords.update(_word_forms(kw))

    # Score by counting keyword/question word overlaps.
    score = len(keywords & q_words)
    if score > best_score:
      best_score = score
      best_tool = name

  if best_tool:
    return best_tool, best_tool
  return "unknown", "unknown"


def extract_failed_cases(report: dict, tools: list | None = None) -> list[dict]:
  """Extract failed sessions as new golden eval cases.

  Classifies each question against available tools to infer
  category and expected_tool. Questions that don't match any
  tool topic are still extracted with category="unknown".
  """
  new_cases = []
  for session in report.get("sessions", []):
    cat = (
        session.get("metrics", {})
        .get("response_usefulness", {})
        .get("category", "")
    )
    if cat not in ("unhelpful", "partial"):
      continue

    question = session.get("question", "")
    if not question:
      continue

    category, expected_tool = _classify_question(question, tools or [])

    case_id = re.sub(r"[^a-z0-9]+", "_", question.lower().strip())[:40]
    case_id = f"extracted_{case_id.strip('_')}"

    new_cases.append(
        {
            "id": case_id,
            "question": question,
            "category": category,
            "expected_tool": expected_tool,
            "notes": f"Extracted from failed synthetic traffic ({cat})",
        }
    )
  return new_cases


def add_eval_cases(eval_cases_path: str, new_cases: list[dict]) -> int:
  """Append new eval cases to the golden eval set."""
  with open(eval_cases_path) as f:
    data = json.load(f)

  existing_ids = {c["id"] for c in data["eval_cases"]}
  existing_questions = {c["question"] for c in data["eval_cases"]}

  added = 0
  for case in new_cases:
    missing = _REQUIRED_CASE_KEYS - set(case.keys())
    if missing:
      print(f"  Skipping invalid eval case (missing {missing}): {case}")
      continue
    if case["id"] in existing_ids or case["question"] in existing_questions:
      continue
    data["eval_cases"].append(case)
    existing_ids.add(case["id"])
    existing_questions.add(case["question"])
    added += 1

  with open(eval_cases_path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

  return added


def build_report_from_eval_results(eval_results_path: str) -> dict:
  """Build a synthetic quality report from golden eval results JSON.

  Bridges the gap between ``run_eval.py --golden`` output format and
  the quality report format the improver expects.
  """
  with open(eval_results_path) as f:
    results = json.load(f)

  total = len(results)
  passed = sum(1 for r in results if r.get("pass", False))
  failed = total - passed

  sessions = []
  for r in results:
    is_pass = r.get("pass", False)
    sessions.append(
        {
            "session_id": r.get("session_id", r.get("case_id", "?")),
            "question": r.get("question", ""),
            "response": r.get("response", ""),
            "metrics": {
                "response_usefulness": {
                    "category": ("meaningful" if is_pass else "unhelpful"),
                    "justification": r.get("reason", ""),
                },
                "task_grounding": {
                    "category": ("grounded" if is_pass else "ungrounded"),
                    "justification": "",
                },
            },
        }
    )

  return {
      "summary": {
          "total_sessions": total,
          "meaningful": passed,
          "meaningful_rate": (round(100 * passed / total) if total else 0),
          "partial": 0,
          "unhelpful": failed,
          "unhelpful_rate": (round(100 * failed / total) if total else 0),
      },
      "sessions": sessions,
  }


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

_PROMPT_ENGINEER_INSTRUCTION = """You are a prompt engineer improving an ADK agent's system prompt.

Your goal: make ALL golden eval cases pass by rewriting the prompt.

## Workflow (repeat until all cases pass or you run out of attempts)

1. Call `read_quality_report` to understand what went wrong.
2. Call `read_current_prompt` to see the current prompt and available tools.
3. Call `generate_candidate` with the current prompt and version.
   Parse the JSON result to extract `improved_prompt` and `changes_summary`.
4. Call `test_candidate` with the `improved_prompt` text.
5. If `all_passed` is true:
   a. Call `write_prompt` with the candidate and summary.
   b. Call `exit_loop` to finish.
6. If `all_passed` is false:
   Review which cases failed and try again from step 3 with
   adjustments.

IMPORTANT:
- Always test before writing. Never write an untested candidate.
- If a candidate fails, analyze WHY before generating the next one.
- When calling `generate_candidate`, pass the current prompt TEXT
  and version NUMBER as separate arguments.
"""


def create_improver_agent(
    config: ImprovementConfig,
) -> LoopAgent:
  """Create the LoopAgent that improves the target agent's prompt.

  Args:
      config: Improvement configuration including agent factory,
          tools, prompt adapter, and eval cases path.

  Returns:
      A configured LoopAgent ready to run.
  """
  inner_agent = Agent(
      name="prompt_engineer",
      model=Gemini(
          model=config.model_id,
          retry_options=types.HttpRetryOptions(attempts=3),
      ),
      description="Improves an agent's prompt to pass all eval cases.",
      instruction=_PROMPT_ENGINEER_INSTRUCTION,
      tools=[
          read_quality_report,
          read_current_prompt,
          generate_candidate,
          test_candidate,
          write_prompt,
          exit_loop,
      ],
      output_key="improvement_result",
  )

  return LoopAgent(
      name="prompt_improver",
      sub_agents=[inner_agent],
      max_iterations=config.max_attempts,
  )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_improvement(
    config: ImprovementConfig,
    report: dict | None = None,
    report_path: str | None = None,
    from_eval_results: bool = False,
) -> dict:
  """Run the improvement cycle.

  Args:
      config: Improvement configuration.
      report: Pre-loaded quality report dict. Mutually exclusive with
          ``report_path``.
      report_path: Path to quality report JSON (or eval results JSON
          if ``from_eval_results`` is True).
      from_eval_results: If True, treat ``report_path`` as golden eval
          results and build a synthetic quality report.

  Returns:
      Dict with ``old_version``, ``new_version``, ``golden_cases``,
      and ``improvement_result`` keys.
  """
  # Load report
  if report is None:
    if report_path is None:
      raise ValueError("Either report or report_path is required")
    if from_eval_results:
      report = build_report_from_eval_results(report_path)
    else:
      with open(report_path) as f:
        report = json.load(f)

  _, old_version = config.prompt_adapter.read_prompt()
  rate = report["summary"]["meaningful_rate"]
  print("")
  print("  ┌──────────────────────────────────────┐")
  print("  │          PROMPT IMPROVER             │")
  print("  ├──────────────────────────────────────┤")
  print(f"  │  Prompt version:  v{old_version:<17} │")
  print(
      f"  │  Quality score:   {rate}% meaningful{' ' * (7 - len(str(rate)))}│"
  )
  print("  └──────────────────────────────────────┘")
  print("")

  if report["summary"].get("total_sessions", 0) == 0:
    print("  ERROR: Report has 0 sessions. Cannot improve.")
    return {
        "old_version": old_version,
        "new_version": old_version,
        "golden_cases": 0,
        "improvement_result": "no_data",
    }

  if report["summary"]["meaningful_rate"] >= 95:
    print("  Quality is already high (>=95%). No improvement needed.")
    return {
        "old_version": old_version,
        "new_version": old_version,
        "golden_cases": 0,
        "improvement_result": "already_good",
    }

  # Extract failed cases into golden set FIRST
  failed_cases = extract_failed_cases(report, tools=config.agent_tools)
  if failed_cases:
    added = add_eval_cases(config.eval_cases_path, failed_cases)
    with open(config.eval_cases_path) as f:
      golden_count = len(json.load(f).get("eval_cases", []))
    print(
        f"  Extracted {len(failed_cases)} failed cases, added"
        f" {added} new to golden set ({golden_count} total)."
    )
  else:
    print("  No failed cases to extract.")

  # Set shared state for tool functions
  _state["config"] = config
  _state["report"] = report

  # Run the LoopAgent
  improver = create_improver_agent(config)
  runner = InMemoryRunner(agent=improver, app_name="prompt_improver")
  session = await runner.session_service.create_session(
      app_name="prompt_improver",
      user_id="improver",
  )

  user_message = Content(
      role="user",
      parts=[
          Part(
              text=(
                  "Improve the agent's prompt so all golden eval cases"
                  " pass. Start by reading the quality report."
              )
          )
      ],
  )

  result_text = ""
  async for event in runner.run_async(
      user_id="improver",
      session_id=session.id,
      new_message=user_message,
  ):
    if event.content and event.content.parts:
      for part in event.content.parts:
        if part.text:
          result_text += part.text

  # Read final state
  _, new_version = config.prompt_adapter.read_prompt()
  with open(config.eval_cases_path) as f:
    golden_count = len(json.load(f).get("eval_cases", []))

  if new_version > old_version:
    print(f"\n  v{old_version} -> v{new_version} complete.")
  else:
    print(
        "\n  WARNING: No improvement was written. All candidates"
        " may have failed regression tests."
    )

  return {
      "old_version": old_version,
      "new_version": new_version,
      "golden_cases": golden_count,
      "improvement_result": result_text,
  }
