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

"""Run the policy agent over a set of questions and emit conversations JSON.

Each question may be single-turn (``{"id", "question"}``) or multi-turn
(``{"id", "turns": [...]}``). Multi-turn cases drive the anti-parroting
scenario: the user asks a question, then pushes a *wrong* "correction"; a good
agent re-verifies with its tool and holds the right figure instead of parroting
the user's number.

Output is ``{"conversations": [...]}`` in the schema consumed by the SDK's
``quality_report.py --conversations-file`` (session_id, question,
final_response, conversation[], tool_calls), so scoring is identical whether
the traces come from here or from BigQuery.

Usage:
  python run_agent.py --skill skills/SKILL.md \
      --questions eval/questions_test.json --questions eval/questions_corrections.json \
      --model gemini-3.5-flash -o run/v0_test.json
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
  sys.path.insert(0, _SCRIPT_DIR)

from agent.agent import build_config  # noqa: E402
from agent.agent import make_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("run_agent")


def _load_questions(paths):
  """Load and concatenate question files into a flat list of dicts."""
  items = []
  for path in paths:
    with open(path) as f:
      data = json.load(f)
    qs = data.get("questions", data) if isinstance(data, dict) else data
    for q in qs:
      items.append(q)
  return items


def _turns_of(question: dict):
  """Normalize a question dict to a list of user turn strings."""
  if question.get("turns"):
    return list(question["turns"])
  return [question.get("question", "")]


def _response_text(resp) -> str:
  """Concatenate text parts of a response (resp.text can be None)."""
  try:
    if resp.text:
      return resp.text
  except (ValueError, AttributeError):
    pass
  parts = []
  for cand in getattr(resp, "candidates", None) or []:
    content = getattr(cand, "content", None)
    for part in getattr(content, "parts", None) or []:
      if getattr(part, "text", None):
        parts.append(part.text)
  return "\n".join(parts)


def _count_tool_calls(chat) -> int:
  """Count function_call parts across the full (uncurated) chat history."""
  total = 0
  try:
    history = chat.get_history(curated=False)
  except TypeError:
    history = chat.get_history()
  for content in history or []:
    for part in getattr(content, "parts", None) or []:
      if getattr(part, "function_call", None):
        total += 1
  return total


def _run_one(client, model, skill_text, question):
  """Run a single (possibly multi-turn) question through the agent."""
  turns = _turns_of(question)
  config = build_config(skill_text)
  chat = client.chats.create(model=model, config=config)
  conversation = []
  final = ""
  t0 = time.monotonic()
  for turn in turns:
    resp = chat.send_message(turn)
    text = _response_text(resp)
    conversation.append({"role": "user", "text": turn})
    conversation.append({"role": "agent", "text": text})
    final = text
  latency = round(time.monotonic() - t0, 2)
  return {
      "session_id": question.get("id", turns[0][:40]),
      "question": turns[0],
      "final_response": final,
      "conversation": conversation,
      "tool_calls": _count_tool_calls(chat),
      "answered_by": "policy_agent",
      "latency_s": latency,
      "category": question.get("category", ""),
  }


def run(skill_path, question_paths, model, out_path, concurrency=8):
  """Run all questions concurrently and write a conversations file."""
  with open(skill_path) as f:
    skill_text = f.read()
  questions = _load_questions(question_paths)
  client = make_client(model)
  logger.info(
      "Running %d question(s) on %s (skill=%s)...",
      len(questions),
      model,
      os.path.basename(skill_path),
  )

  results = [None] * len(questions)
  with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
    future_to_idx = {
        pool.submit(_run_one, client, model, skill_text, q): i
        for i, q in enumerate(questions)
    }
    for future in cf.as_completed(future_to_idx):
      idx = future_to_idx[future]
      try:
        results[idx] = future.result()
      except Exception as exc:  # pylint: disable=broad-except
        q = questions[idx]
        logger.warning("Question %s failed: %s", q.get("id", idx), exc)
        results[idx] = {
            "session_id": q.get("id", str(idx)),
            "question": _turns_of(q)[0],
            "final_response": f"[ERROR: {exc}]",
            "conversation": [],
            "tool_calls": 0,
            "answered_by": "policy_agent",
            "latency_s": None,
            "category": q.get("category", ""),
            "error": True,
        }

  with open(out_path, "w") as f:
    json.dump({"conversations": results}, f, indent=2)
  ok = sum(1 for r in results if not r.get("error"))
  logger.info(
      "Wrote %d conversation(s) (%d ok) -> %s", len(results), ok, out_path
  )
  return out_path


def _main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--skill", required=True, help="Path to SKILL.md")
  parser.add_argument(
      "--questions",
      action="append",
      required=True,
      help="Question JSON file (repeatable; files are concatenated)",
  )
  parser.add_argument(
      "--model", default="gemini-3.5-flash", help="Gemini model"
  )
  parser.add_argument("-o", "--out", required=True, help="Output JSON path")
  parser.add_argument("--concurrency", type=int, default=8)
  args = parser.parse_args()
  run(args.skill, args.questions, args.model, args.out, args.concurrency)


if __name__ == "__main__":
  _main()
