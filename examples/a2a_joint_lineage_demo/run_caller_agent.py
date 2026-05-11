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

"""Run the caller-side supervisor against every campaign brief.

For each campaign:

  1. Spin one ADK session on ``InMemoryRunner`` with the caller BQ AA
     Plugin attached.
  2. Stream the brief through; the supervisor commits four local
     decisions, then delegates audience-risk review to the receiver
     via ``RemoteA2aAgent`` — that delegation produces an
     ``A2A_INTERACTION`` row in the caller's ``agent_events``.
  3. Record ``caller_campaign_runs`` (session_id ↔ campaign mapping)
     so the auditor projection can resolve campaign metadata.

After all sessions finish, runs three acceptance gates:

  G1. caller has ≥1 ``A2A_INTERACTION`` row per campaign session;
  G2. receiver dataset has ≥1 row;
  G3. ≥1 caller ``a2a_context_id`` matches a receiver ``session_id``.

G2 and G3 poll with backoff because the receiver-side
``BigQueryAgentAnalyticsPlugin`` writes asynchronously. The caller
flush completes before this script returns, but receiver-side rows
can lag. Hard-fails fast if any gate fails — partial demos are
worse than no demo.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from caller_agent import APP_NAME
from caller_agent import bq_logging_plugin
from caller_agent import root_agent
from caller_agent.agent import CALLER_DATASET_ID
from caller_agent.agent import CALLER_TABLE_ID
from caller_agent.agent import DATASET_LOCATION
from caller_agent.agent import PROJECT_ID
from campaigns import CAMPAIGN_BRIEFS
from google.adk.runners import InMemoryRunner
from google.api_core import exceptions as gax_exceptions
from google.cloud import bigquery
from google.genai.types import Content
from google.genai.types import Part

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_HERE, ".env")

USER_ID = os.getenv("DEMO_USER_ID", "u-a2a-demo-mediabuyer")
PER_SESSION_TIMEOUT_S = int(os.getenv("DEMO_SESSION_TIMEOUT_S", "420"))
RECEIVER_DATASET_ID = os.getenv("RECEIVER_DATASET_ID", "a2a_receiver_demo")
RECEIVER_TABLE_ID = os.getenv("RECEIVER_TABLE_ID", "agent_events")
GATE_POLL_TIMEOUT_S = int(os.getenv("DEMO_GATE_POLL_TIMEOUT_S", "120"))
GATE_POLL_INTERVAL_S = float(os.getenv("DEMO_GATE_POLL_INTERVAL_S", "3.0"))

_CREATE_CAMPAIGN_RUNS_TABLE = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.campaign_runs` (
  session_id STRING,
  campaign STRING,
  brand STRING,
  brief STRING,
  run_order INT64,
  event_count INT64
)
"""


async def _run_one(
    runner: InMemoryRunner,
    campaign: str,
    brief: str,
    idx: int,
    total: int,
) -> tuple[str, int, str | None]:
  """Run one campaign brief through the caller end-to-end."""
  session = await runner.session_service.create_session(
      app_name=runner.app_name,
      user_id=USER_ID,
  )
  session_id = session.id
  print(f"  [{idx}/{total}] caller_session={session_id} campaign={campaign!r}")

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
        f"          ! caller errored after {event_count} events: {exc}",
        file=sys.stderr,
    )

  elapsed = time.monotonic() - start
  if exception_msg is not None:
    error_reason: str | None = f"caller raised an exception ({exception_msg})"
    status = "errored"
  elif event_count == 0:
    error_reason = "caller streamed zero events"
    status = "no-events"
  else:
    error_reason = None
    status = "ok"
  print(
      f"          {status} — {event_count} events streamed, "
      f"{elapsed:.1f}s wall."
  )
  return session_id, event_count, error_reason


async def _run_all() -> tuple[list[dict[str, object]], list[tuple[str, str]]]:
  """Run every campaign brief through the caller."""
  briefs = CAMPAIGN_BRIEFS
  print(f"Running {len(briefs)} campaign briefs through the caller agent...")
  print(
      "Each brief is one caller ADK session. The supervisor delegates "
      "audience-risk review to the receiver via RemoteA2aAgent, which "
      "produces the A2A_INTERACTION row the auditor projection joins on."
  )
  print()

  runner = InMemoryRunner(
      agent=root_agent,
      app_name=APP_NAME,
      plugins=[bq_logging_plugin],
  )

  succeeded: list[dict[str, object]] = []
  failures: list[tuple[str, str]] = []
  for idx, brief in enumerate(briefs, start=1):
    try:
      session_id, event_count, error_reason = await asyncio.wait_for(
          _run_one(runner, brief.campaign, brief.brief, idx, len(briefs)),
          timeout=PER_SESSION_TIMEOUT_S,
      )
      if error_reason is None:
        succeeded.append(
            {
                "session_id": session_id,
                "campaign": brief.campaign,
                "brand": brief.brand,
                "brief": brief.brief,
                "run_order": idx,
                "event_count": event_count,
            }
        )
      else:
        failures.append((brief.campaign, error_reason))
    except asyncio.TimeoutError:
      msg = f"timeout after {PER_SESSION_TIMEOUT_S}s"
      print(
          f"  [{idx}/{len(briefs)}] TIMEOUT for {brief.campaign!r}",
          file=sys.stderr,
      )
      failures.append((brief.campaign, msg))

  print()
  print("Flushing caller BQ AA Plugin...")
  try:
    await bq_logging_plugin.flush()
  except Exception as exc:  # pylint: disable=broad-except
    print(f"  flush() warning: {exc}", file=sys.stderr)
  try:
    await bq_logging_plugin.shutdown()
  except Exception as exc:  # pylint: disable=broad-except
    print(f"  shutdown() warning: {exc}", file=sys.stderr)

  return succeeded, failures


def _write_campaign_runs(runs: list[dict[str, object]]) -> None:
  """Write the caller's session_id ↔ campaign mapping table."""
  if not runs:
    return
  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)
  client.query(
      _CREATE_CAMPAIGN_RUNS_TABLE.format(
          project=PROJECT_ID,
          dataset=CALLER_DATASET_ID,
      )
  ).result()
  table_ref = f"{PROJECT_ID}.{CALLER_DATASET_ID}.campaign_runs"
  job_config = bigquery.LoadJobConfig(
      write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
      source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
  )
  client.load_table_from_json(runs, table_ref, job_config=job_config).result()
  print(f"  Wrote {len(runs)} campaign_runs rows to {table_ref}")


def _record_first_caller_session_id(runs: list[dict[str, object]]) -> None:
  """Persist the first successful caller session_id to .env.

  ``render_queries.sh`` reads ``DEMO_CALLER_SESSION_ID`` to bind
  Block 4's @caller_session parameter. Without this, the rendered
  query carries an empty literal and returns zero rows.
  """
  if not runs:
    return
  first = str(runs[0]["session_id"])
  lines: list[str] = []
  if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, encoding="utf-8") as f:
      lines = [
          ln
          for ln in f.read().splitlines()
          if not ln.startswith("DEMO_CALLER_SESSION_ID=")
      ]
  lines.append(f"DEMO_CALLER_SESSION_ID={first}")
  with open(_ENV_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
  print(f"  Wrote DEMO_CALLER_SESSION_ID={first} to {_ENV_PATH}")


def _poll_until(
    label: str,
    fn,
    timeout_s: float,
    interval_s: float,
):
  """Poll ``fn()`` until it returns truthy or timeout. Returns final value."""
  deadline = time.monotonic() + timeout_s
  attempt = 0
  result = None
  while time.monotonic() < deadline:
    attempt += 1
    result = fn()
    if result:
      print(f"  {label}: observed after {attempt} poll(s).")
      return result
    time.sleep(interval_s)
  print(
      f"  {label}: still empty after {timeout_s:.0f}s "
      f"({attempt} attempt(s))."
  )
  return result


def _check_acceptance_gates(succeeded: list[dict[str, object]]) -> int:
  """Run the three caller-side acceptance gates. Returns 0 if all pass."""
  if not succeeded:
    return 1
  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)
  caller_table = f"{PROJECT_ID}.{CALLER_DATASET_ID}.{CALLER_TABLE_ID}"
  receiver_table = f"{PROJECT_ID}.{RECEIVER_DATASET_ID}.{RECEIVER_TABLE_ID}"
  caller_sessions = [str(r["session_id"]) for r in succeeded]
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ArrayQueryParameter("sessions", "STRING", caller_sessions),
      ],
  )

  print()
  print("Running acceptance gates...")

  # G1: caller has ≥1 A2A_INTERACTION per campaign session. Caller
  # plugin already flushed before this point so a single read is
  # fine. Catch NotFound — if the caller table is missing the plugin
  # never wrote anything and we want a clean diagnostic, not a raw
  # BigQuery exception.
  q_g1 = f"""
    SELECT
      session_id,
      COUNTIF(event_type = 'A2A_INTERACTION') AS a2a_calls
    FROM `{caller_table}`
    WHERE session_id IN UNNEST(@sessions)
    GROUP BY session_id
  """
  try:
    rows = list(client.query(q_g1, job_config=job_config).result())
  except gax_exceptions.NotFound:
    print(
        f"  G1 FAIL: caller agent_events table `{caller_table}` not "
        "found after caller flush. Verify the caller BQ AA Plugin "
        "wrote successfully (check run_caller_agent.py logs for "
        "flush() / shutdown() warnings).",
        file=sys.stderr,
    )
    return 1
  missing = [r["session_id"] for r in rows if int(r["a2a_calls"]) == 0]
  no_row = set(caller_sessions) - {r["session_id"] for r in rows}
  if missing or no_row:
    print(
        "  G1 FAIL: A2A_INTERACTION missing for "
        f"{sorted(set(missing) | no_row)}",
        file=sys.stderr,
    )
    return 1
  print("  G1 OK — every caller session has ≥1 A2A_INTERACTION row.")

  # G2: receiver dataset has ≥1 row. Receiver plugin runs in the
  # other process and flushes asynchronously w.r.t. the caller's HTTP
  # round-trips, so we poll. Treat NotFound as 0 — on a fresh dataset
  # the receiver table doesn't exist until the plugin's first write
  # creates it, and we want the poll loop to time out cleanly with
  # the intended diagnostic instead of stack-tracing.
  def _g2_check():
    q = f"SELECT COUNT(*) AS n FROM `{receiver_table}`"
    try:
      return int(list(client.query(q).result())[0]["n"])
    except gax_exceptions.NotFound:
      return 0

  receiver_rows = _poll_until(
      "G2 receiver row poll",
      _g2_check,
      timeout_s=GATE_POLL_TIMEOUT_S,
      interval_s=GATE_POLL_INTERVAL_S,
  )
  if not receiver_rows:
    print(
        "  G2 FAIL: receiver agent_events is empty after polling. "
        "Confirm run_receiver_server.py is running with the explicit "
        "Runner(plugins=[...]) path; ./.venv/bin/python3 "
        "smoke_receiver.py reproduces the gap.",
        file=sys.stderr,
    )
    return 1
  print(f"  G2 OK — receiver agent_events has {receiver_rows} rows.")

  # G3: ≥1 caller a2a_context_id matches a receiver session_id. Same
  # async-write race as G2 — poll.
  q_g3 = f"""
    WITH caller_a2a AS (
      SELECT DISTINCT
        JSON_VALUE(attributes, '$.a2a_metadata."a2a:context_id"')
          AS a2a_context_id
      FROM `{caller_table}`
      WHERE event_type = 'A2A_INTERACTION'
        AND session_id IN UNNEST(@sessions)
    ),
    receiver_sessions AS (
      SELECT DISTINCT session_id FROM `{receiver_table}`
      WHERE session_id IS NOT NULL
    )
    SELECT COUNT(*) AS matched
    FROM caller_a2a
    JOIN receiver_sessions
      ON caller_a2a.a2a_context_id = receiver_sessions.session_id
  """

  def _g3_check():
    try:
      rows = list(client.query(q_g3, job_config=job_config).result())
    except gax_exceptions.NotFound:
      return 0
    return int(rows[0]["matched"]) if rows else 0

  matched = _poll_until(
      "G3 caller↔receiver match poll",
      _g3_check,
      timeout_s=GATE_POLL_TIMEOUT_S,
      interval_s=GATE_POLL_INTERVAL_S,
  )
  if not matched:
    print(
        "  G3 FAIL: zero caller a2a_context_id values matched a "
        "receiver session_id after polling. Check that the receiver "
        "server is using InMemorySessionService (or another service "
        "that honors explicit session ids).",
        file=sys.stderr,
    )
    return 1
  print(f"  G3 OK — {matched} caller↔receiver session match(es).")
  return 0


def main() -> int:
  succeeded, failures = asyncio.run(_run_all())
  print()
  print(f"Sessions: {len(succeeded)} succeeded, {len(failures)} failed.")
  for run in succeeded:
    print(f"  ok  - {run['session_id']}  ({run['campaign']})")
  for campaign, reason in failures:
    print(f"  FAIL- {campaign}: {reason}")
  if failures:
    print(
        f"\nERROR: {len(failures)} campaign(s) failed; aborting before "
        "acceptance gates. Re-run after addressing the failures above.",
        file=sys.stderr,
    )
    return 1
  if not succeeded:
    print("ERROR: zero caller sessions produced traces.", file=sys.stderr)
    return 1
  _write_campaign_runs(succeeded)
  _record_first_caller_session_id(succeeded)
  return _check_acceptance_gates(succeeded)


if __name__ == "__main__":
  sys.exit(main())
