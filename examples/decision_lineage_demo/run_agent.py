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

"""Run the media-planner agent against every campaign brief.

Each brief becomes one ADK session; the BQ AA Plugin attached to the
``InMemoryRunner`` writes every span (INVOCATION, AGENT, LLM, TOOL,
HITL) into the configured ``agent_events`` table. After all sessions
finish, the driver flushes + shuts down the plugin so all rows are in
BigQuery before downstream extraction starts.

Records the first session id back into ``.env`` (as ``DEMO_SESSION_ID``)
so ``render_queries.sh`` can wire it into the per-session GQL block in
``bq_studio_queries.gql``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from agent import APP_NAME
from agent import bq_logging_plugin
from agent import root_agent
from campaigns import CAMPAIGN_BRIEFS
from google.adk.runners import InMemoryRunner
from google.genai.types import Content
from google.genai.types import Part

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_HERE, ".env")

USER_ID = os.getenv("DEMO_USER_ID", "u-demo-mediabuyer")
PER_SESSION_TIMEOUT_S = int(os.getenv("DEMO_SESSION_TIMEOUT_S", "300"))


async def _run_one(
    runner: InMemoryRunner, campaign: str, brief: str, idx: int, total: int
) -> tuple[str, int, str | None]:
  """Run one campaign brief end-to-end.

  Returns:
      ``(session_id, event_count, error_reason)``. ``error_reason``
      is ``None`` on success and a short string explaining the
      failure cause (exception or zero events) otherwise. Callers
      must drop unsuccessful sessions; their plugin output (if any)
      cannot be relied on.
  """
  session = await runner.session_service.create_session(
      app_name=runner.app_name,
      user_id=USER_ID,
  )
  session_id = session.id
  print(f"  [{idx}/{total}] session={session_id} campaign={campaign!r}")

  message = Content(role="user", parts=[Part(text=brief)])

  start = time.monotonic()
  event_count = 0
  exception_msg: str | None = None
  try:
    async for _event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
      event_count += 1
  except Exception as exc:  # pylint: disable=broad-except
    exception_msg = f"{type(exc).__name__}: {exc}"
    print(
        f"          ! agent run errored after {event_count} events: {exc}",
        file=sys.stderr,
    )
  elapsed = time.monotonic() - start
  if exception_msg is not None:
    error_reason: str | None = (
        f"agent run raised an exception ({exception_msg})"
    )
    status = "errored"
  elif event_count == 0:
    error_reason = "runner streamed zero events"
    status = "no-events"
  else:
    error_reason = None
    status = "ok"
  print(
      f"          {status} — {event_count} runner events streamed, "
      f"{elapsed:.1f}s wall."
  )
  return session_id, event_count, error_reason


async def _run_all() -> tuple[list[str], list[tuple[str, str]]]:
  """Run every campaign brief.

  Returns:
      ``(succeeded_session_ids, failures)`` where ``failures`` is a
      list of ``(campaign, reason)`` for diagnosis.
  """
  briefs = CAMPAIGN_BRIEFS
  print(f"Running {len(briefs)} campaign briefs through the agent...")
  print(
      "Each brief is one ADK session; the BQ AA Plugin writes every "
      "span (INVOCATION/AGENT/LLM/TOOL/HITL) to agent_events."
  )
  print()

  runner = InMemoryRunner(
      agent=root_agent,
      app_name=APP_NAME,
      plugins=[bq_logging_plugin],
  )

  succeeded: list[str] = []
  failures: list[tuple[str, str]] = []
  for idx, brief in enumerate(briefs, start=1):
    try:
      session_id, _event_count, error_reason = await asyncio.wait_for(
          _run_one(runner, brief.campaign, brief.brief, idx, len(briefs)),
          timeout=PER_SESSION_TIMEOUT_S,
      )
      if error_reason is None:
        succeeded.append(session_id)
      else:
        failures.append((brief.campaign, error_reason))
    except asyncio.TimeoutError:
      msg = f"timeout after {PER_SESSION_TIMEOUT_S}s"
      print(
          f"  [{idx}/{len(briefs)}] TIMEOUT after "
          f"{PER_SESSION_TIMEOUT_S}s for {brief.campaign!r}",
          file=sys.stderr,
      )
      failures.append((brief.campaign, msg))

  print()
  print("Flushing BQ AA Plugin so all spans land in BigQuery...")
  # Both flush() and shutdown() are async on the plugin — must await.
  try:
    await bq_logging_plugin.flush()
  except Exception as exc:  # pylint: disable=broad-except
    print(f"  flush() warning: {exc}", file=sys.stderr)
  try:
    await bq_logging_plugin.shutdown()
  except Exception as exc:  # pylint: disable=broad-except
    print(f"  shutdown() warning: {exc}", file=sys.stderr)

  return succeeded, failures


def _record_first_session_id(session_ids: list[str]) -> None:
  """Append DEMO_SESSION_ID to .env if a first session is available."""
  if not session_ids:
    return
  first = session_ids[0]
  lines: list[str] = []
  if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, encoding="utf-8") as f:
      lines = [
          ln
          for ln in f.read().splitlines()
          if not ln.startswith("DEMO_SESSION_ID=")
      ]
  lines.append(f"DEMO_SESSION_ID={first}")
  with open(_ENV_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
  print(f"  Wrote DEMO_SESSION_ID={first} to {_ENV_PATH}")


def main() -> int:
  succeeded, failures = asyncio.run(_run_all())
  print()
  print(f"Sessions: {len(succeeded)} succeeded, {len(failures)} failed.")
  for sid in succeeded:
    print(f"  ok  - {sid}")
  for campaign, reason in failures:
    print(f"  FAIL- {campaign}: {reason}")
  print()
  _record_first_session_id(succeeded)
  if failures:
    print(
        f"ERROR: {len(failures)} of {len(succeeded) + len(failures)} "
        "campaign runs failed. Setup will not proceed with a partial "
        "demo. Re-run ./setup.sh after addressing the failures above "
        "(typical causes: Vertex AI quota / permissions, model "
        "endpoint outages, or network).",
        file=sys.stderr,
    )
    return 1
  if not succeeded:
    print("ERROR: zero sessions produced traces.", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
