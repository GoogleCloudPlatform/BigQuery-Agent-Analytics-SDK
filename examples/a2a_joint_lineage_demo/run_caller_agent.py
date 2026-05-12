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
     **ADK 1.33 caveat:** ``RemoteA2aAgent`` spawns its own
     InvocationContext with a fresh caller-side ``session_id``, so
     the ``A2A_INTERACTION`` row lands under that sub-session
     (``agent='audience_risk_reviewer'``), NOT under the supervisor
     session. The mapping projection below pairs them back together.
  3. Record ``campaign_runs`` (supervisor session_id ↔ campaign
     mapping) so the auditor projection can resolve campaign
     metadata.
  4. Materialize ``supervisor_a2a_invocations`` — a deterministic
     pairing of each supervisor's ``TOOL_STARTING`` for
     ``audience_risk_reviewer`` with the corresponding
     ``A2A_INTERACTION`` row on the RemoteA2aAgent sub-session, via
     chronological rank within the same (app_name, user_id). This is
     the bridge ``build_joint_graph.py``'s ``remote_agent_invocations``
     uses to keep the ``CallerCampaignRun -> RemoteAgentInvocation``
     edge valid under ADK 1.33's split-session telemetry shape.

After all sessions finish, runs four acceptance gates:

  G1. caller dataset has ≥ N ``A2A_INTERACTION`` rows where
      ``agent='audience_risk_reviewer'`` (N == campaign count);
  G1.5. ``supervisor_a2a_invocations`` row count == campaign count
        and every row has a non-NULL ``a2a_context_id``;
  G2. receiver dataset has ≥1 row;
  G3. ≥1 mapped ``a2a_context_id`` matches a receiver ``session_id``.

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

# Supervisor ↔ RemoteA2aAgent-sub-session mapping projection.
#
# Under ADK 1.33, ``RemoteA2aAgent`` spawns its own InvocationContext
# with a fresh caller-side session_id. The ``A2A_INTERACTION`` row
# therefore lives in a sibling session (``agent='audience_risk_reviewer'``)
# with no foreign key back to the supervisor session that triggered
# the delegation. The two events DO share ``user_id`` and live in the
# same caller dataset, so chronological-rank pairing within the
# current run's session set deterministically reconstructs the link:
#
#   * Supervisor side: ``TOOL_STARTING`` rows scoped to
#     ``session_id IN UNNEST(@sessions)`` with
#     ``content.tool = 'audience_risk_reviewer'`` and
#     ``content.tool_origin = 'A2A'``. Filters out non-current
#     supervisor sessions and the four local tool calls per brief.
#   * A2A side: ``A2A_INTERACTION`` rows with
#     ``agent = 'audience_risk_reviewer'`` and
#     ``timestamp >= MIN(supervisor_ts)`` — the timestamp lower bound
#     keeps stale rows from prior runs out without needing a
#     supervisor-session FK on the A2A row.
#
# Pair each supervisor ``TOOL_STARTING`` to the first A2A sub-session
# event in the interval ``[supervisor_ts, next_supervisor_ts)``. This is
# stricter than global row-number pairing: an extra stale A2A row can no
# longer shift every later campaign's mapping.
_CREATE_SUPERVISOR_A2A_INVOCATIONS = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.supervisor_a2a_invocations` AS
WITH tool_start_base AS (
  SELECT
    session_id AS caller_session_id,
    span_id AS supervisor_span_id,
    timestamp AS supervisor_ts,
    user_id
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'TOOL_STARTING'
    AND JSON_VALUE(content, '$.tool') = 'audience_risk_reviewer'
    AND JSON_VALUE(content, '$.tool_origin') = 'A2A'
    AND session_id IN UNNEST(@sessions)
),
tool_starts AS (
  SELECT
    *,
    LEAD(supervisor_ts) OVER (
      PARTITION BY user_id ORDER BY supervisor_ts
    ) AS next_supervisor_ts
  FROM tool_start_base
),
window_bounds AS (
  SELECT MIN(supervisor_ts) AS min_ts FROM tool_starts
),
a2a_events AS (
  SELECT
    session_id AS a2a_invocation_session_id,
    span_id AS a2a_invocation_span_id,
    timestamp AS a2a_invocation_timestamp,
    user_id,
    JSON_VALUE(attributes, '$.a2a_metadata."a2a:task_id"') AS a2a_task_id,
    JSON_VALUE(attributes, '$.a2a_metadata."a2a:context_id"') AS a2a_context_id,
    COALESCE(
      JSON_VALUE(content, '$.metadata.adk_session_id'),
      JSON_VALUE(
        attributes,
        '$.a2a_metadata."a2a:response".metadata.adk_session_id'
      )
    ) AS receiver_session_id_from_response,
    timestamp
  FROM `{project}.{dataset}.{table}`
  WHERE event_type = 'A2A_INTERACTION'
    AND agent = 'audience_risk_reviewer'
    AND timestamp >= (SELECT min_ts FROM window_bounds)
),
paired AS (
  SELECT
    ts.caller_session_id,
    ts.supervisor_span_id,
    ts.supervisor_ts,
    ae.a2a_invocation_session_id,
    ae.a2a_invocation_span_id,
    ae.a2a_invocation_timestamp,
    ae.a2a_task_id,
    ae.a2a_context_id,
    ae.receiver_session_id_from_response,
    ROW_NUMBER() OVER (
      PARTITION BY ts.caller_session_id, ts.supervisor_span_id
      ORDER BY ae.a2a_invocation_timestamp
    ) AS match_rank
  FROM tool_starts AS ts
  LEFT JOIN a2a_events AS ae
    ON ts.user_id = ae.user_id
    AND ae.a2a_invocation_timestamp >= ts.supervisor_ts
    AND (
      ts.next_supervisor_ts IS NULL
      OR ae.a2a_invocation_timestamp < ts.next_supervisor_ts
    )
)
SELECT
  caller_session_id,
  supervisor_span_id,
  supervisor_ts,
  a2a_invocation_session_id,
  a2a_invocation_span_id,
  a2a_invocation_timestamp,
  a2a_task_id,
  a2a_context_id,
  receiver_session_id_from_response
FROM paired
WHERE match_rank = 1
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


def _write_supervisor_a2a_invocations(
    runs: list[dict[str, object]],
) -> int:
  """Materialize the supervisor↔A2A-sub-session mapping table.

  Returns the row count. Hard-fails (raises) on a BigQuery error so
  the caller surfaces the problem directly rather than producing an
  empty mapping that silently fails downstream gates.
  """
  if not runs:
    return 0
  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)
  sql = _CREATE_SUPERVISOR_A2A_INVOCATIONS.format(
      project=PROJECT_ID,
      dataset=CALLER_DATASET_ID,
      table=CALLER_TABLE_ID,
  )
  caller_sessions = [str(r["session_id"]) for r in runs]
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ArrayQueryParameter("sessions", "STRING", caller_sessions),
      ],
  )
  client.query(sql, job_config=job_config).result()
  table_ref = f"{PROJECT_ID}.{CALLER_DATASET_ID}.supervisor_a2a_invocations"
  count_row = list(
      client.query(f"SELECT COUNT(*) AS n FROM `{table_ref}`").result()
  )[0]
  count = int(count_row["n"])
  print(f"  Wrote {count} supervisor_a2a_invocations row(s) to {table_ref}")
  return count


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


def _check_acceptance_gates(
    succeeded: list[dict[str, object]],
    mapping_rows: int,
) -> int:
  """Run the caller-side acceptance gates. Returns 0 if all pass.

  ``mapping_rows`` is the row count returned by
  ``_write_supervisor_a2a_invocations`` — passing it in (rather than
  re-querying) keeps the gate output aligned with the table we just
  wrote and surfaces the count even when ``mapping_rows == 0`` would
  cause every downstream query to be empty.
  """
  if not succeeded:
    return 1
  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)
  caller_table = f"{PROJECT_ID}.{CALLER_DATASET_ID}.{CALLER_TABLE_ID}"
  receiver_table = f"{PROJECT_ID}.{RECEIVER_DATASET_ID}.{RECEIVER_TABLE_ID}"
  mapping_table = f"{PROJECT_ID}.{CALLER_DATASET_ID}.supervisor_a2a_invocations"
  expected_a2a_calls = len(succeeded)

  print()
  print("Running acceptance gates...")

  # G1: caller dataset has ≥ N A2A_INTERACTION rows where
  # ``agent='audience_risk_reviewer'`` (N == campaign count). Under
  # ADK 1.33 the RemoteA2aAgent spawns a sub-session whose
  # session_id is NOT in @sessions, so a per-supervisor-session
  # count returns zero even when the delegation succeeded. Caller
  # plugin already flushed before this point so a single read is
  # fine. Catch NotFound — if the caller table is missing the plugin
  # never wrote anything and we want a clean diagnostic, not a raw
  # BigQuery exception.
  q_g1 = f"""
    SELECT COUNT(*) AS a2a_calls
    FROM `{caller_table}`
    WHERE event_type = 'A2A_INTERACTION'
      AND agent = 'audience_risk_reviewer'
  """
  try:
    rows = list(client.query(q_g1).result())
  except gax_exceptions.NotFound:
    print(
        f"  G1 FAIL: caller agent_events table `{caller_table}` not "
        "found after caller flush. Verify the caller BQ AA Plugin "
        "wrote successfully (check run_caller_agent.py logs for "
        "flush() / shutdown() warnings).",
        file=sys.stderr,
    )
    return 1
  a2a_calls = int(rows[0]["a2a_calls"])
  if a2a_calls < expected_a2a_calls:
    print(
        f"  G1 FAIL: expected ≥ {expected_a2a_calls} "
        "A2A_INTERACTION rows where agent='audience_risk_reviewer' "
        f"in `{caller_table}`, found {a2a_calls}. Each campaign "
        "should produce one delegation; the supervisor LLM may have "
        "skipped the audience_risk_reviewer tool call.",
        file=sys.stderr,
    )
    return 1
  print(
      f"  G1 OK — caller has {a2a_calls} A2A_INTERACTION row(s) "
      f"(expected ≥ {expected_a2a_calls})."
  )

  # G1.5: supervisor_a2a_invocations row count == campaign count and
  # every row has a non-NULL a2a_context_id. This is the gate that
  # guards the ADK 1.33 supervisor↔A2A-sub-session pairing —
  # without it, a chronologically-misaligned dataset (e.g. a
  # supervisor TOOL_STARTING with no matching A2A_INTERACTION
  # because the receiver timed out) would silently produce an
  # under-counted mapping and a downstream graph missing campaigns.
  if mapping_rows != expected_a2a_calls:
    print(
        f"  G1.5 FAIL: supervisor_a2a_invocations has "
        f"{mapping_rows} row(s), expected exactly "
        f"{expected_a2a_calls} (one per campaign). The chronological"
        " pairing in supervisor_a2a_invocations may have misaligned "
        "TS↔A2A pairs; inspect `{mapping_table}` against the "
        "caller agent_events TOOL_STARTING vs A2A_INTERACTION "
        "timeline.".replace("{mapping_table}", mapping_table),
        file=sys.stderr,
    )
    return 1
  q_g1_5 = f"""
    SELECT COUNT(*) AS missing
    FROM `{mapping_table}`
    WHERE a2a_context_id IS NULL OR a2a_invocation_session_id IS NULL
  """
  missing = int(list(client.query(q_g1_5).result())[0]["missing"])
  if missing:
    print(
        f"  G1.5 FAIL: {missing} mapping row(s) have NULL "
        "a2a_context_id / a2a_invocation_session_id. The pairing "
        "found a TOOL_STARTING row that did not match a valid "
        f"A2A_INTERACTION attribute. Inspect `{mapping_table}`.",
        file=sys.stderr,
    )
    return 1
  print(
      f"  G1.5 OK — supervisor_a2a_invocations has "
      f"{mapping_rows} row(s), all with non-NULL a2a_context_id."
  )

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

  # G3: ≥1 mapped a2a_context_id matches a receiver session_id. The
  # mapping table (G1.5) already constrains a2a_context_id to the
  # current run, so no @sessions filter is needed. Same async-write
  # race as G2 — poll the receiver side.
  q_g3 = f"""
    WITH mapping AS (
      SELECT DISTINCT a2a_context_id
      FROM `{mapping_table}`
      WHERE a2a_context_id IS NOT NULL
    ),
    receiver_sessions AS (
      SELECT DISTINCT session_id FROM `{receiver_table}`
      WHERE session_id IS NOT NULL
    )
    SELECT COUNT(*) AS matched
    FROM mapping
    JOIN receiver_sessions
      ON mapping.a2a_context_id = receiver_sessions.session_id
  """

  def _g3_check():
    try:
      rows = list(client.query(q_g3).result())
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
  mapping_rows = _write_supervisor_a2a_invocations(succeeded)
  return _check_acceptance_gates(succeeded, mapping_rows=mapping_rows)


if __name__ == "__main__":
  sys.exit(main())
