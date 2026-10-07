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

"""Agent memory from BigQuery Agent Analytics traces.

Prints the three memory layers (short-term, long-term, reasoning) for one
user, then the combined context block an agent would put in its next prompt.

  python examples/agent_memory/agent_memory_demo.py
      Offline: replays the committed SYNTHETIC fixture through the real
      Client.list_traces code path. No credentials or network needed.

  python examples/agent_memory/agent_memory_demo.py \\
      --project-id my-project --dataset-id agent_analytics \\
      --user-id USER --session-id CURRENT_SESSION
      Live: reads the agent_events table that the ADK
      BigQueryAgentAnalyticsPlugin writes (Application Default Credentials).
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
import sys
from typing import Any, Optional

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import make_bq_client

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import memory_layers  # noqa: E402
import offline_bigquery  # noqa: E402

# Which tool arguments name an entity, and its type. These match the trip
# planner tools in the fixture; pass --entity-arg for your own tools.
ENTITY_ARGUMENTS = {
    "origin": "LOCATION",
    "destination": "LOCATION",
    "city": "LOCATION",
    "near": "LOCATION",
}
OFFLINE_USER = "u-ana"
OFFLINE_SESSION = "s-104"


def _utc(value: datetime) -> str:
  return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_now(value: str) -> datetime:
  parsed = offline_bigquery.parse_timestamp(value)
  return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _shorten(text: Optional[str], limit: int = 72) -> str:
  text = text or ""
  return text if len(text) <= limit else text[: limit - 3] + "..."


def _plural(count: int, noun: str) -> str:
  return f"{count} {noun}{'' if count == 1 else 's'}"


def _display_path(path: Path) -> str:
  try:
    return str(path.resolve().relative_to(Path.cwd()))
  except ValueError:
    return str(path)


def _entity_args(values: Optional[list[str]]) -> dict[str, str]:
  if not values:
    return dict(ENTITY_ARGUMENTS)
  mapping = {}
  for value in values:
    name, _, entity_type = value.partition("=")
    if not name or not entity_type:
      raise argparse.ArgumentTypeError(f"--entity-arg {value!r}: use NAME=TYPE")
    mapping[name] = entity_type
  return mapping


def _inspected_trace(
    memory: memory_layers.UserMemory, session_id: str, trace_id: Optional[str]
) -> Optional[memory_layers.ReasoningTrace]:
  """The trace to print in full: the requested one, else the most recent
  past trace that recorded a tool error, else the most recent past trace."""
  if trace_id:
    return memory.reasoning.get_trace_with_steps(trace_id)
  past = [
      t for t in memory.reasoning.list_traces() if t.session_id != session_id
  ]
  with_errors = [t for t in past if t.metrics["tool_errors"]]
  return (with_errors or past or [None])[0]


def _print_report(
    memory: memory_layers.UserMemory,
    *,
    source: str,
    session_id: str,
    now: datetime,
    since: datetime,
    query: Optional[str],
    trace_id: Optional[str],
    entity_args: dict[str, str],
) -> None:
  reasoning_traces = memory.reasoning.list_traces()
  print("Agent memory from BigQuery Agent Analytics traces")
  print(f"source : {source}")
  print(
      f"user   : {memory.user_id}   current session: {session_id}   now:"
      f" {_utc(now)}"
  )
  print(
      f"read   : {_plural(len(memory.traces), 'session')},"
      f" {_plural(len(reasoning_traces), 'trace')} from one"
      " Client.list_traces() call"
  )
  print(
      f"         TraceFilter(user_id={memory.user_id!r},"
      f" start_time={_utc(since)})"
  )

  print("\n== 1. Short-term memory ==")
  print("Sessions (ShortTermMemory.list_sessions):")
  for info in memory.short_term.list_sessions():
    print(
        f"  {info.session_id}  {_utc(info.created_at)} "
        f" {_plural(info.message_count, 'message'):<10}"
        f"  {_shorten(info.first_message_preview)}"
    )
  print(f"Conversation {session_id} (ShortTermMemory.get_conversation):")
  try:
    conversation = memory.short_term.get_conversation(session_id)
  except KeyError:
    conversation = []
  for message in conversation:
    print(f"  [{message.role}] {message.content}")
  if not conversation:
    print("  (no messages)")

  print("\n== 2. Long-term memory ==")
  print("Preference history (ADK user: state from STATE_DELTA rows):")
  history = memory.long_term.get_preference_history()
  for pref in history:
    until = _utc(pref.valid_until) if pref.valid_until else "current"
    label = f"{pref.category} = {pref.preference}"
    print(
        f"  {label:<20} {_utc(pref.valid_from)} -> {until:<20}"
        f"  {pref.session_id}"
    )
  if not history:
    print("  (no user: state writes)")
  current = memory.long_term.get_preferences()
  print(
      "Current preferences: "
      + (
          ", ".join(f"{k} = {current[k].preference}" for k in sorted(current))
          or "(none)"
      )
  )
  arguments = ", ".join(sorted(entity_args))
  print(f"Entities the agent acted on (tool arguments: {arguments}):")
  entities = memory.long_term.get_entities()
  for entity in entities:
    print(
        f"  {entity.name:<14} {entity.entity_type:<9}"
        f" {_plural(len(entity.mentions), 'tool call'):<12}"
        f"  {', '.join(entity.sessions)}"
    )
  if not entities:
    print("  (none)")

  print("\n== 3. Reasoning memory ==")
  print("Traces (ReasoningMemory.list_traces):")
  for rt in reasoning_traces:
    print(
        f"  {rt.trace_id}  {rt.session_id}  {rt.outcome_status:<20}"
        f"  {_plural(len(rt.tool_calls), 'tool call'):<12}"
        f"  {_shorten(rt.task, 48)}"
    )
  inspected = _inspected_trace(memory, session_id, trace_id)
  if inspected is None:
    print("No past trace to inspect.")
  else:
    print(
        f"Trace {inspected.trace_id} (session {inspected.session_id}):"
        f" {inspected.outcome_status}"
    )
    print(f"  task   : {inspected.task}")
    for step in inspected.steps:
      print(f"  step {step.step_number} : {step.action}")
      if step.thought:
        print(f"           thought: {step.thought}")
      for call in step.tool_calls:
        duration = (
            f"{call.duration_ms:.0f} ms"
            if call.duration_ms is not None
            else "-"
        )
        detail = f"  {call.error}" if call.error else ""
        print(f"           {call.tool_name}  {call.status}  {duration}{detail}")
    print(f"  outcome: {inspected.outcome}")
    metrics = inspected.metrics
    print(
        "  metrics: "
        + " ".join(
            f"{k}={v:.0f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in metrics.items()
        )
    )
  print("Tool stats (ReasoningMemory.get_tool_stats):")
  print("  tool              calls  ok  failed  success   avg_ms")
  for stats in memory.reasoning.get_tool_stats():
    avg = f"{stats.avg_duration_ms:.1f}" if stats.avg_duration_ms else "-"
    print(
        f"  {stats.name:<17} {stats.total_calls:>5} {stats.successful_calls:>3}"
        f" {stats.failed_calls:>7} {stats.success_rate:>8.0%} {avg:>8}"
    )
  if query:
    print(f"Similar past tasks for {query!r} (lexical, successful only):")
    similar = memory.reasoning.get_similar_traces(
        query, exclude_session_id=session_id
    )
    for match in similar:
      print(
          f"  {match.similarity:.2f}  {match.trace.trace_id} "
          f" {_shorten(match.trace.task)}"
      )
    if not similar:
      print("  (none above the threshold)")

  print("\n== 4. get_context() for the next model call ==")
  print(memory.get_context(query or "", session_id=session_id))


def main(argv: Optional[list[str]] = None, *, bq_client: Any = None) -> int:
  parser = argparse.ArgumentParser(
      description=__doc__,
      formatter_class=argparse.RawDescriptionHelpFormatter,
  )
  parser.add_argument("--project-id", help="read a live table in this project")
  parser.add_argument("--dataset-id", help="dataset of the agent_events table")
  parser.add_argument("--table-id", default="agent_events")
  parser.add_argument("--location", help="BigQuery location of the dataset")
  parser.add_argument(
      "--user-id", help=f"user to load (offline default: {OFFLINE_USER})"
  )
  parser.add_argument(
      "--session-id",
      help=f"the current session (offline default: {OFFLINE_SESSION})",
  )
  parser.add_argument(
      "--query",
      help="task to find similar past traces for (default: the current"
      " session's first user message)",
  )
  parser.add_argument("--trace-id", help="invocation id of a trace to print")
  parser.add_argument(
      "--lookback-days",
      type=int,
      default=30,
      help="read sessions with rows in the last N days (default: 30)",
  )
  parser.add_argument(
      "--now",
      help="reference time for the lookback window (default: the fixture's"
      " time offline, the current time live)",
  )
  parser.add_argument(
      "--entity-arg",
      action="append",
      metavar="NAME=TYPE",
      help="a tool argument that names an entity (repeatable)",
  )
  parser.add_argument(
      "--fixture",
      type=Path,
      default=offline_bigquery.DEFAULT_FIXTURE,
      help="offline fixture to replay",
  )
  args = parser.parse_args(argv)
  if bool(args.project_id) != bool(args.dataset_id):
    parser.error("--project-id and --dataset-id go together")
  try:
    entity_args = _entity_args(args.entity_arg)
  except argparse.ArgumentTypeError as e:
    parser.error(str(e))

  if args.project_id:
    if not (args.user_id and args.session_id):
      parser.error("live mode needs --user-id and --session-id")
    now = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    client = Client(
        project_id=args.project_id,
        dataset_id=args.dataset_id,
        table_id=args.table_id,
        location=args.location,
        verify_schema=False,
        bq_client=bq_client
        or make_bq_client(args.project_id, location=args.location),
    )
    source = f"BigQuery {args.project_id}.{args.dataset_id}.{args.table_id}"
    user_id, session_id = args.user_id, args.session_id
  else:
    fixture = offline_bigquery.load_fixture(args.fixture)
    now = _parse_now(args.now) if args.now else fixture.now
    client = Client(
        project_id="offline-demo",
        dataset_id="agent_analytics",
        table_id="agent_events",
        verify_schema=False,
        bq_client=offline_bigquery.OfflineBigQueryClient(fixture.rows),
    )
    source = f"offline fixture (synthetic rows) {_display_path(args.fixture)}"
    user_id = args.user_id or OFFLINE_USER
    session_id = args.session_id or OFFLINE_SESSION

  since = now - timedelta(days=args.lookback_days)
  memory = memory_layers.load_user_memory(
      client, user_id, since=since, entity_args=entity_args
  )
  query = args.query
  if query is None:
    try:
      conversation = memory.short_term.get_conversation(session_id)
    except KeyError:
      conversation = []
    query = next((m.content for m in conversation if m.role == "user"), None)
  _print_report(
      memory,
      source=source,
      session_id=session_id,
      now=now,
      since=since,
      query=query,
      trace_id=args.trace_id,
      entity_args=entity_args,
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
