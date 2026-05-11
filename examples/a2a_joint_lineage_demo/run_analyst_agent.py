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

"""Run the audit-analyst agent against the joint context graph.

This is the loop-closing step of the demo:

  1. ``run_caller_agent.py`` ran the supervisor agent through the
     campaign briefs; its A2A delegations landed in the caller
     ``agent_events``.
  2. ``run_receiver_server.py`` served the governance reviewer;
     receiver spans landed in the receiver ``agent_events``.
  3. ``build_org_graphs.py`` materialized per-org context graphs.
  4. ``build_joint_graph.py`` stitched the auditor projections and
     created ``a2a_joint_context_graph``.
  5. **This script** asks the analyst agent a natural-language
     question; the agent picks one of four tools (stitch_health,
     list_campaigns, audit_campaign, find_governance_rejections),
     runs a parameterized query, and answers in plain English.

The analyst's own reasoning trace lands in
``<ANALYST_DATASET>.agent_events`` via the BQ AA Plugin, so
operators can build audit-of-the-audit lineage from it later.

Usage::

    ./.venv/bin/python3 run_analyst_agent.py                # canned question
    ./.venv/bin/python3 run_analyst_agent.py "Is the audit graph healthy?"

Multiple questions can be passed positionally; each runs as its own
ADK session so the analyst's traces stay scoped per-question.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Optional

from analyst_agent import APP_NAME
from analyst_agent import bq_logging_plugin
from analyst_agent import root_agent
from google.adk.runners import InMemoryRunner
from google.genai.types import Content
from google.genai.types import Part

_HERE = os.path.dirname(os.path.abspath(__file__))

USER_ID = os.getenv("DEMO_USER_ID", "u-a2a-demo-analyst")
PER_QUESTION_TIMEOUT_S = int(os.getenv("DEMO_ANALYST_TIMEOUT_S", "300"))

# Default canned questions cover one read against each of the four
# analyst tools (stitch_health, list_campaigns, audit_campaign,
# find_governance_rejections) so a no-argument run exercises every
# tool surface end-to-end.
_DEFAULT_QUESTIONS = [
    (
        "Is the joint audit graph healthy? How many remote A2A calls"
        " did we record, and were they all stitched to a receiver"
        " session?"
    ),
    (
        "What campaigns are in scope for this audit run? List each"
        " campaign and its caller_session_id."
    ),
    (
        "For the first campaign you find, walk me through the remote"
        " governance agent's full audit path — every option it"
        " considered, which one it selected, and the rationale for"
        " each dropped option."
    ),
    (
        "Across the whole portfolio, what are the lowest-scored"
        " options the governance agent dropped? Quote the rejection"
        " rationale verbatim."
    ),
]


def _final_text(events: list) -> Optional[str]:
  """Pull the analyst agent's last user-visible text response.

  Returns ``None`` when the agent produced no terminal text response
  (e.g. all events were tool calls/results with no follow-up
  summary). Callers should treat that case as a failure rather than
  silently exiting success: ``run_e2e_demo.sh`` exits 0 if the
  driver returns 0, and we don't want a question that produced
  zero answer text to slip past the e2e gate.
  """
  for event in reversed(events):
    content = getattr(event, "content", None)
    if not content:
      continue
    parts = getattr(content, "parts", None)
    if not parts:
      continue
    role = getattr(content, "role", None)
    if role == "user":
      continue
    text_parts = [
        getattr(p, "text", None) for p in parts if getattr(p, "text", None)
    ]
    if text_parts:
      return "\n".join(text_parts).strip()
  return None


async def _ask_one(
    runner: InMemoryRunner, question: str, idx: int, total: int
) -> tuple[str, int, str | None]:
  """Run one question through the analyst end-to-end."""
  session = await runner.session_service.create_session(
      app_name=runner.app_name,
      user_id=USER_ID,
  )
  session_id = session.id
  print(f"\n  [{idx}/{total}] analyst_session={session_id}")
  print(f"          Q: {question}")

  message = Content(role="user", parts=[Part(text=question)])
  start = time.monotonic()
  events: list = []
  exception_msg: str | None = None
  try:
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
      events.append(event)
  except Exception as exc:  # pylint: disable=broad-except
    exception_msg = f"{type(exc).__name__}: {exc}"
    print(
        f"          ! analyst errored after {len(events)} events: {exc}",
        file=sys.stderr,
    )

  elapsed = time.monotonic() - start
  if exception_msg is not None:
    return session_id, len(events), exception_msg

  answer = _final_text(events)
  if answer is None:
    msg = "analyst produced no final text response"
    print(
        f"          ! {msg} ({elapsed:.1f}s, {len(events)} events)",
        file=sys.stderr,
    )
    return session_id, len(events), msg

  print(f"          A ({elapsed:.1f}s, {len(events)} events):")
  for line in answer.splitlines():
    print(f"            {line}")
  return session_id, len(events), None


async def _ask_all(questions: list[str]) -> int:
  print(f"Running {len(questions)} analyst question(s)...")
  runner = InMemoryRunner(
      agent=root_agent,
      app_name=APP_NAME,
      plugins=[bq_logging_plugin],
  )

  failures = 0
  for idx, q in enumerate(questions, start=1):
    try:
      _, _, error_msg = await asyncio.wait_for(
          _ask_one(runner, q, idx, len(questions)),
          timeout=PER_QUESTION_TIMEOUT_S,
      )
    except asyncio.TimeoutError:
      error_msg = f"timeout after {PER_QUESTION_TIMEOUT_S}s"
      print(f"  [{idx}/{len(questions)}] TIMEOUT", file=sys.stderr)
    if error_msg:
      failures += 1

  print("\nFlushing analyst BQ AA Plugin...")
  try:
    await bq_logging_plugin.flush()
  except Exception as exc:  # pylint: disable=broad-except
    print(f"  flush() warning: {exc}", file=sys.stderr)
  try:
    await bq_logging_plugin.shutdown()
  except Exception as exc:  # pylint: disable=broad-except
    print(f"  shutdown() warning: {exc}", file=sys.stderr)

  return failures


def main() -> int:
  parser = argparse.ArgumentParser(
      description=(
          "Ask the analyst agent natural-language audit questions"
          " about the joint A2A context graph."
      ),
  )
  parser.add_argument(
      "questions",
      nargs="*",
      help=(
          "One or more questions. Each question runs as its own ADK"
          " session. If omitted, four canned questions exercise"
          " every analyst tool."
      ),
  )
  args = parser.parse_args()

  questions = args.questions if args.questions else _DEFAULT_QUESTIONS

  failures = asyncio.run(_ask_all(questions))
  if failures:
    print(
        f"\nERROR: {failures} of {len(questions)} questions failed.",
        file=sys.stderr,
    )
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
