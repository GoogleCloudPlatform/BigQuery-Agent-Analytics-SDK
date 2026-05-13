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

"""Driver script: run the MAKO demo agent for N sessions and
let the BQ AA plugin populate ``agent_events`` in BigQuery.

Each session sends one prompt that asks the agent to walk
through a randomly-flavored MAKO decision flow. The agent's
trace + tool calls are captured into the configured
``(project, dataset, agent_events)`` table by
``bq_logging_plugin``.

Usage (uses defaults from ``mako_demo_agent`` module-level
env-var lookups; pass flags to override):

    PYTHONPATH=src python examples/migration_v5/run_agent.py \\
        --sessions 50 \\
        --project test-project-0728-467323 \\
        --dataset migration_v5_demo \\
        --location US

Requires:
* Vertex AI access for the configured model (default
  ``gemini-2.5-flash``).
* BigQuery write access on the target ``(project, dataset)``.

The notebook calls this in Beat 0 once per fresh dataset.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from typing import Any

_DECISION_PROMPTS = (
    "We need an audience segment decision for the new "
    "Premium Coffee campaign. Audience size ~50000, budget "
    "$2500 remaining.",
    "Bid value decision for the lapsed-buyer retargeting "
    "flight. Audience size ~12000, budget $800 remaining.",
    "Creative variant decision for the holiday push. "
    "Audience size ~80000, budget $3200 remaining.",
    "Frequency cap decision for the always-on awareness "
    "buy. Audience size ~150000, budget $4500 remaining.",
    "Audience segment decision for B2B SaaS demo "
    "campaign. Audience size ~5000, budget $1200 remaining.",
)


async def _run_one_session(
    runner, app_name: str, prompt: str, session_idx: int
) -> None:
  from google.genai import types as genai_types

  user_id = f"demo-user-{session_idx}"
  session = await runner.session_service.create_session(
      app_name=app_name, user_id=user_id
  )
  message = genai_types.Content(
      role="user",
      parts=[genai_types.Part.from_text(text=prompt)],
  )
  async for _event in runner.run_async(
      user_id=user_id,
      session_id=session.id,
      new_message=message,
  ):
    # The plugin captures every event into BQ as a side
    # effect; the driver doesn't need to do anything with
    # the stream here.
    pass


async def _main(args) -> None:
  # Late imports so ``--help`` works without ADK installed.
  from google.adk.runners import InMemoryRunner

  # Configure env vars BEFORE importing the agent module so
  # its module-level ``PROJECT_ID`` / ``DATASET_ID`` /
  # ``DATASET_LOCATION`` reads pick them up.
  if args.project:
    os.environ["PROJECT_ID"] = args.project
  if args.dataset:
    os.environ["DATASET_ID"] = args.dataset
  if args.location:
    os.environ["DATASET_LOCATION"] = args.location

  from mako_demo_agent import APP_NAME
  from mako_demo_agent import bq_logging_plugin
  from mako_demo_agent import root_agent

  runner = InMemoryRunner(
      app_name=APP_NAME,
      agent=root_agent,
      plugins=[bq_logging_plugin],
  )

  rng = random.Random(args.seed)
  prompts = [rng.choice(_DECISION_PROMPTS) for _ in range(args.sessions)]

  print(
      f"Running {args.sessions} sessions against "
      f"{os.environ.get('PROJECT_ID')}."
      f"{os.environ.get('DATASET_ID')}.agent_events",
      file=sys.stderr,
  )
  for idx, prompt in enumerate(prompts):
    if idx % 10 == 0:
      print(f"  session {idx}/{args.sessions}", file=sys.stderr)
    await _run_one_session(runner, APP_NAME, prompt, idx)
  print("done.", file=sys.stderr)


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(
      description=(
          "Run the MAKO demo agent for N sessions; events "
          "land in BigQuery via the BQ AA plugin."
      ),
  )
  parser.add_argument("--sessions", type=int, default=50)
  parser.add_argument("--project", default=None)
  parser.add_argument("--dataset", default=None)
  parser.add_argument("--location", default=None)
  parser.add_argument("--seed", type=int, default=20260512)
  args = parser.parse_args(argv)
  # The agent module is imported inside ``_main`` (after env
  # vars are set), so we can't import it at the top of this
  # file. Defer the script's main to an async runner.
  sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
  asyncio.run(_main(args))
  return 0


if __name__ == "__main__":  # pragma: no cover
  sys.exit(main())
