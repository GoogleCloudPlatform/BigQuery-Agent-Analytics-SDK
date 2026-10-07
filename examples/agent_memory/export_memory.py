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

"""Exports agent memory to the JSON file the web view reads.

For each user, writes the three memory layers computed by
``memory_layers.py``: sessions with their conversations and session state,
preference versions, extracted facts, entities, reasoning traces with a
per-invocation timeline and the memory each ``recall_memory`` call
returned, tool stats, and the ``get_context()`` block. Each user is read
with one ``Client.list_traces`` call.

With ``--run-record`` (the file ``analyst_agent.py`` writes), the export
also gets the run's days, analysts and totals, and one before/after entry
per question that was also asked without memory. An entry is flagged when
the run without memory read the memory tables anyway, through SQL that
names their dataset.

  python examples/agent_memory/export_memory.py            # offline fixture
  python examples/agent_memory/export_memory.py \\
      --project-id my-project --dataset-id bqaa_agent_memory_demo \\
      --table-id analyst_events --memory-tables analyst_ \\
      --run-record examples/agent_memory/recorded_run/live_run.json

Then serve ``examples/agent_memory/viz/`` (``python -m http.server``) and
open ``index.html``.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Optional

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import make_bq_client
from bigquery_agent_analytics import Span

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_memory_demo  # noqa: E402
import memory_consolidation  # noqa: E402
import memory_layers  # noqa: E402
import offline_bigquery  # noqa: E402

SCHEMA = "bqaa-agent-memory-viz/2"
RECALL_TOOL = "recall_memory"
SQL_TOOL = "run_sql"
# A source tag in recalled memory: "[<session id>/<span id>]".
_SOURCE_SESSION = re.compile(r"[\[ ]([^\s\[\]/,]+)/[^\s\],]+")
DEFAULT_OUT = HERE / "viz" / "data" / "memory_export.json"
OFFLINE_USERS = ("u-ana", "u-ben")
RESULT_PREVIEW_CHARS = 240


def _iso(value: Optional[datetime]) -> Optional[str]:
  if value is None:
    return None
  utc = value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
  return utc.replace("+00:00", "Z")


def _short(value: Any, limit: int = RESULT_PREVIEW_CHARS) -> Optional[str]:
  if value is None:
    return None
  text = (
      value
      if isinstance(value, str)
      else json.dumps(value, ensure_ascii=False, default=str)
  )
  return text if len(text) <= limit else text[: limit - 3] + "..."


def _timeline(spans: list[Span]) -> list[dict[str, Any]]:
  """Model calls and tool calls of one invocation, in ms from its start.

  A streamed model call is one row, from its request to its last response
  row. Its status is ``success`` when a terminal response was recorded,
  ``incomplete`` when only fragments were, and ``error`` when it failed.
  """
  start = spans[0].timestamp

  def offset(at: datetime) -> float:
    return (at - start) / timedelta(milliseconds=1)

  # (row index of the event that places the entry, entry)
  entries: list[tuple[int, dict[str, Any]]] = []
  for call in memory_layers.model_calls(spans):
    if call.calls:
      label = "call: " + ", ".join(call.calls)
    elif call.texts:
      # Text from a stream that never finished is not an answer.
      label = "answer" if call.complete else "reply"
    else:
      label = "model turn"
    if call.failed:
      status = "error"
    elif call.complete:
      status = "success"
    else:
      status = "incomplete"
    entries.append(
        (
            call.last_index,
            {
                "kind": "model",
                "label": label,
                "start_ms": offset(call.started_at),
                "end_ms": offset(call.ended_at),
                "status": status,
                "span_id": call.span_id,
                "detail": call.error
                if call.failed
                else (" ".join(call.texts) or None),
            },
        )
    )
  open_tools: dict[Any, dict[str, Any]] = {}
  for index, span in enumerate(spans):
    if span.event_type == "TOOL_STARTING":
      row = {
          "kind": "tool",
          "label": span.content.get("tool") or "unknown",
          "start_ms": offset(span.timestamp),
          "end_ms": None,
          "status": "pending",
          "span_id": span.span_id,
          "detail": None,
      }
      open_tools[span.span_id or row["label"]] = row
      entries.append((index, row))
    elif span.event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
      key = span.span_id or span.content.get("tool")
      row = open_tools.pop(key, None)
      if row is None:
        row = {
            "kind": "tool",
            "label": span.content.get("tool") or "unknown",
            "start_ms": offset(span.timestamp),
            "span_id": span.span_id,
        }
        entries.append((index, row))
      failed = span.event_type == "TOOL_ERROR" or span.is_error
      row["end_ms"] = offset(span.timestamp)
      row["status"] = "error" if failed else "success"
      row["detail"] = (
          span.error_message if failed else _short(span.content.get("result"))
      )
  entries.sort(key=lambda entry: entry[0])
  return [row for _, row in entries]


def _tool_call(call: memory_layers.ToolCall) -> dict[str, Any]:
  return {
      "tool_name": call.tool_name,
      "arguments": call.arguments,
      "result": _short(call.result),
      "status": call.status,
      "duration_ms": call.duration_ms,
      "error": call.error,
      "span_id": call.span_id,
      "started_at": _iso(call.started_at),
  }


def recalled_memory(rt: memory_layers.ReasoningTrace) -> Optional[str]:
  """The memory text the trace's ``recall_memory`` call returned, if any."""
  for call in rt.tool_calls:
    if call.tool_name == RECALL_TOOL and isinstance(call.result, dict):
      text = call.result.get("memory")
      if isinstance(text, str):
        return text
  return None


def recalled_sessions(
    text: str, known: set[str], current: Optional[str] = None
) -> list[str]:
  """Earlier sessions that recalled memory cites in its source tags."""
  found: list[str] = []
  for match in _SOURCE_SESSION.finditer(text):
    session_id = match.group(1)
    if (
        session_id in known
        and session_id != current
        and session_id not in found
    ):
      found.append(session_id)
  return found


def build_user_export(
    memory: memory_layers.UserMemory,
    *,
    current_session_id: Optional[str] = None,
    query: Optional[str] = None,
) -> dict[str, Any]:
  """One user's memory layers as JSON-ready dicts (times in UTC ISO)."""
  infos = memory.short_term.list_sessions()
  current = current_session_id or (infos[0].session_id if infos else None)
  known = {info.session_id for info in infos}
  traces_by_session: dict[str, list[memory_layers.ReasoningTrace]] = {}
  for rt in memory.reasoning.list_traces():
    traces_by_session.setdefault(rt.session_id, []).append(rt)
  sessions = []
  for info in sorted(infos, key=lambda i: i.created_at):
    recalled: list[str] = []
    for rt in traces_by_session.get(info.session_id, []):
      text = recalled_memory(rt)
      for session_id in recalled_sessions(text or "", known, info.session_id):
        if session_id not in recalled:
          recalled.append(session_id)
    sessions.append(
        {
            "session_id": info.session_id,
            "created_at": _iso(info.created_at),
            "updated_at": _iso(info.updated_at),
            "state": dict(info.state),
            "recalled_sessions": recalled,
            "row_count": sum(
                len(t.spans)
                for t in memory.traces
                if t.session_id == info.session_id
            ),
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "timestamp": _iso(m.timestamp),
                    "span_id": m.span_id,
                    "complete": m.complete,
                }
                for m in memory.short_term.get_conversation(info.session_id)
            ],
        }
    )

  spans_by_trace: dict[tuple[str, str], list[Span]] = {}
  for trace in memory.traces:
    for trace_id, spans in memory_layers.spans_by_invocation(trace).items():
      spans_by_trace[(trace.session_id, trace_id)] = spans
  traces = []
  for rt in sorted(memory.reasoning.list_traces(), key=lambda t: t.started_at):
    recall = recalled_memory(rt)
    sql_calls = [c for c in rt.tool_calls if c.tool_name == SQL_TOOL]
    traces.append(
        {
            "trace_id": rt.trace_id,
            "session_id": rt.session_id,
            "task": rt.task,
            "recall": (
                None
                if recall is None
                else {
                    "text": recall,
                    "sessions": recalled_sessions(recall, known, rt.session_id),
                }
            ),
            "tool_call_count": len(rt.tool_calls),
            "sql_queries": len(sql_calls),
            "sql_errors": sum(c.status == "error" for c in sql_calls),
            "outcome": rt.outcome,
            "outcome_status": rt.outcome_status,
            "started_at": _iso(rt.started_at),
            "completed_at": _iso(rt.completed_at),
            "latency_ms": rt.latency_ms,
            "llm_calls": rt.llm_calls,
            "total_tokens": rt.total_tokens,
            "errors": list(dict.fromkeys(rt.errors)),
            "outcome_span_id": rt.outcome_span_id,
            "steps": [
                {
                    "step_number": step.step_number,
                    "thought": step.thought,
                    "action": step.action,
                    "observation": _short(step.observation),
                    "tool_calls": [_tool_call(c) for c in step.tool_calls],
                }
                for step in rt.steps
            ],
            "timeline": _timeline(spans_by_trace[(rt.session_id, rt.trace_id)]),
        }
    )

  if query is None and current is not None:
    opening = [
        m.content
        for m in memory.short_term.get_conversation(current)
        if m.role == "user"
    ]
    query = opening[0] if opening else ""
  names = [i.state.get("analyst_name") for i in infos]
  return {
      "user_id": memory.user_id,
      "name": next((n for n in names if isinstance(n, str)), None),
      "current_session_id": current,
      "sessions": sessions,
      "preferences": [
          {
              "category": p.category,
              "preference": p.preference,
              "valid_from": _iso(p.valid_from),
              "valid_until": _iso(p.valid_until),
              "session_id": p.session_id,
              "span_id": p.span_id,
          }
          for p in memory.long_term.get_preference_history()
      ],
      "facts": [
          {
              "subject": f.subject,
              "subject_type": f.subject_type,
              "predicate": f.predicate,
              "object": f.object,
              "object_type": f.object_type,
              "statement": f.statement,
              "session_id": f.session_id,
              "span_id": f.span_id,
              "observed_at": _iso(f.observed_at),
          }
          for f in memory.long_term.get_facts()
      ],
      "entities": [
          {
              "name": e.name,
              "entity_type": e.entity_type,
              "mentions": [
                  {
                      "tool_name": m.tool_name,
                      "argument": m.argument,
                      "session_id": m.session_id,
                      "span_id": m.span_id,
                      "timestamp": _iso(m.timestamp),
                      "source": m.source,
                  }
                  for m in e.mentions
              ],
          }
          for e in memory.long_term.get_entities()
      ],
      "traces": traces,
      "tool_stats": [
          {
              "name": s.name,
              "total_calls": s.total_calls,
              "successful_calls": s.successful_calls,
              "failed_calls": s.failed_calls,
              "success_rate": s.success_rate,
              "avg_duration_ms": s.avg_duration_ms,
              "last_used_at": _iso(s.last_used_at),
              "last_failure": (
                  None
                  if s.last_failure is None
                  else {
                      "error": s.last_failure.error,
                      "session_id": s.last_failure.session_id,
                      "span_id": s.last_failure.span_id,
                  }
              ),
          }
          for s in memory.reasoning.get_tool_stats()
      ],
      "context": (
          memory.get_context(query or "", session_id=current)
          if current is not None
          else ""
      ),
  }


def memory_table_reads(
    calls: list[memory_layers.ToolCall], dataset: Optional[str]
) -> list[dict[str, Any]]:
  """The ``run_sql`` calls whose SQL names ``dataset``.

  ``dataset`` is the one that holds the events and memory tables. A run
  without memory tools that queries it has read memory anyway, so it is
  not a memory-free answer.
  """
  if not dataset:
    return []
  named = re.compile(rf"\b{re.escape(dataset)}\b")
  return [
      {
          "purpose": c.arguments.get("purpose"),
          "sql": _short(c.arguments.get("sql")),
          "span_id": c.span_id,
      }
      for c in calls
      if c.tool_name == SQL_TOOL
      and isinstance(c.arguments.get("sql"), str)
      and named.search(c.arguments["sql"])
  ]


def _side(
    memory: memory_layers.UserMemory,
    session_id: str,
    memory_dataset: Optional[str] = None,
) -> Optional[dict[str, Any]]:
  """The first turn of a session, summarized for a before/after view."""
  traces = sorted(
      (t for t in memory.reasoning.list_traces() if t.session_id == session_id),
      key=lambda t: t.started_at,
  )
  if not traces:
    return None
  rt = traces[0]
  sql_calls = [c for c in rt.tool_calls if c.tool_name == SQL_TOOL]
  recall = recalled_memory(rt)
  known = {i.session_id for i in memory.short_term.list_sessions()}
  return {
      "session_id": session_id,
      "trace_id": rt.trace_id,
      "answer": rt.outcome,
      "outcome_status": rt.outcome_status,
      "tool_calls": [c.tool_name for c in rt.tool_calls],
      "sql_queries": len(sql_calls),
      "sql_errors": sum(c.status == "error" for c in sql_calls),
      "memory_table_reads": memory_table_reads(rt.tool_calls, memory_dataset),
      "llm_calls": rt.llm_calls,
      "latency_ms": rt.latency_ms,
      "total_tokens": rt.total_tokens,
      "recalled_sessions": (
          recalled_sessions(recall, known, session_id) if recall else []
      ),
  }


def build_comparisons(
    record: dict[str, Any],
    memories: dict[str, memory_layers.UserMemory],
    names: dict[str, str],
    memory_dataset: Optional[str] = None,
) -> list[dict[str, Any]]:
  """One entry per question the run also asked without memory.

  ``memories`` maps user ids (the analysts' and their control users') to
  loaded memory. ``control_read_memory`` marks a pair whose run without
  memory queried ``memory_dataset``.
  """
  out = []
  sessions = {s["session_id"]: s for s in record.get("sessions", [])}
  for pair in record.get("comparisons", []):
    control = sessions.get(pair["without_memory"], {})
    with_memory = memories.get(pair["user_id"])
    without = memories.get(control.get("user_id", ""))
    if with_memory is None or without is None:
      continue
    without_side = _side(without, pair["without_memory"], memory_dataset)
    out.append(
        {
            "user_id": pair["user_id"],
            "name": names.get(pair["user_id"], pair["user_id"]),
            "day": pair["day"],
            "question": pair["question"],
            "with_memory": _side(
                with_memory, pair["with_memory"], memory_dataset
            ),
            "without_memory": without_side,
            "control_read_memory": bool(
                without_side and without_side["memory_table_reads"]
            ),
        }
    )
  return out


def hide_project(comparisons: list[dict[str, Any]], project_id: str) -> None:
  """Shows the project as ``<project>`` in the SQL of memory table reads."""
  named = re.compile(rf"(?<![\w-]){re.escape(project_id)}(?![\w-])")
  for pair in comparisons:
    for side in (pair["with_memory"], pair["without_memory"]):
      for read in (side or {}).get("memory_table_reads", []):
        if read["sql"]:
          read["sql"] = named.sub("<project>", read["sql"])


def build_run(
    record: dict[str, Any], users: list[dict[str, Any]]
) -> dict[str, Any]:
  """Days, analysts and totals of a recorded run, for the page header."""
  sessions = record.get("sessions", [])
  memory_sessions = [s for s in sessions if s.get("memory") == "on"]
  traces = [t for u in users for t in u["traces"]]
  usage = record.get("usage", [])
  return {
      "model": record.get("model"),
      "run_tag": record.get("run_tag"),
      "started_at": record.get("started_at"),
      "finished_at": record.get("finished_at"),
      "days": record.get("days", []),
      "analysts": record.get("analysts", []),
      "consolidation": [
          {"day": night["day"], **night.get("extraction", {})}
          for night in record.get("consolidation", [])
      ],
      "totals": {
          "analysts": len(record.get("analysts", [])),
          "sessions": len(memory_sessions),
          "control_sessions": len(sessions) - len(memory_sessions),
          "turns": sum(len(s.get("turns", [])) for s in memory_sessions),
          "rows": sum(
              r.get("row_count", 0) for r in record.get("row_counts", [])
          ),
          "model_calls": sum(u.get("model_calls") or 0 for u in usage),
          "tool_calls": sum(t["tool_call_count"] for t in traces),
          "sql_queries": sum(t["sql_queries"] for t in traces),
          "sql_errors": sum(t["sql_errors"] for t in traces),
          "recalls": sum(t["recall"] is not None for t in traces),
          "facts": sum(len(u["facts"]) for u in users),
          "entities": sum(len(u["entities"]) for u in users),
          "preference_versions": sum(len(u["preferences"]) for u in users),
      },
  }


def build_export(
    users: list[dict[str, Any]],
    *,
    label: str,
    source: str,
    exported_at: str,
    run: Optional[dict[str, Any]] = None,
    comparisons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
  return {
      "schema": SCHEMA,
      "label": label,
      "source": source,
      "exported_at": exported_at,
      "run": run,
      "users": users,
      "comparisons": comparisons or [],
  }


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
      "--user-id",
      action="append",
      help=f"user to export, repeatable (offline default: {OFFLINE_USERS})",
  )
  parser.add_argument("--lookback-days", type=int, default=30)
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
      "--memory-tables",
      metavar="PREFIX",
      help=(
          "live: read extracted facts and entities from the consolidation"
          " tables with this prefix (e.g. analyst_)"
      ),
  )
  parser.add_argument(
      "--run-record",
      type=Path,
      help=(
          "live: the run record analyst_agent.py wrote; adds the run's days,"
          " totals and before/after comparisons, and its analysts as the"
          " default users"
      ),
  )
  parser.add_argument(
      "--fixture", type=Path, default=offline_bigquery.DEFAULT_FIXTURE
  )
  parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
  parser.add_argument("--label", help="run label shown in the page header")
  parser.add_argument(
      "--show-project",
      action="store_true",
      help=(
          "show the real project id in the export (default: <project>, also"
          " in the SQL of memory table reads)"
      ),
  )
  args = parser.parse_args(argv)
  if bool(args.project_id) != bool(args.dataset_id):
    parser.error("--project-id and --dataset-id go together")
  try:
    entity_args = agent_memory_demo.parse_entity_args(args.entity_arg)
  except argparse.ArgumentTypeError as e:
    parser.error(str(e))

  record = None
  tables = None
  bq = None
  if args.project_id:
    if args.run_record:
      record = json.loads(args.run_record.read_text("utf-8"))
    if not args.user_id and not record:
      parser.error("live mode needs at least one --user-id or --run-record")
    now = (
        offline_bigquery.parse_timestamp(args.now)
        if args.now
        else datetime.now(timezone.utc)
    )
    bq = bq_client or make_bq_client(args.project_id, location=args.location)
    client = Client(
        project_id=args.project_id,
        dataset_id=args.dataset_id,
        table_id=args.table_id,
        location=args.location,
        verify_schema=False,
        bq_client=bq,
    )
    if args.memory_tables:
      tables = memory_consolidation.MemoryTables.in_dataset(
          args.project_id,
          args.dataset_id,
          events=args.table_id,
          prefix=args.memory_tables,
      )
    project = args.project_id if args.show_project else "<project>"
    source = f"{project}.{args.dataset_id}.{args.table_id}"
    label = args.label or (record or {}).get("label") or "BigQuery read"
  else:
    if args.memory_tables or args.run_record:
      parser.error("--memory-tables and --run-record need --project-id")
    fixture = offline_bigquery.load_fixture(args.fixture)
    now = (
        offline_bigquery.parse_timestamp(args.now) if args.now else fixture.now
    )
    client = Client(
        project_id="offline-demo",
        dataset_id="agent_analytics",
        table_id="agent_events",
        verify_schema=False,
        bq_client=offline_bigquery.OfflineBigQueryClient(fixture.rows),
    )
    source = f"synthetic fixture {Path(args.fixture).name}"
    label = args.label or "Offline synthetic fixture"

  def load(user_id: str) -> memory_layers.UserMemory:
    facts, entities = (
        memory_consolidation.load_memory_items(bq, tables, user_id)
        if tables is not None
        else ([], [])
    )
    return memory_layers.load_user_memory(
        client,
        user_id,
        since=now - timedelta(days=args.lookback_days),
        entity_args=entity_args,
        facts=facts,
        extracted_entities=entities,
    )

  user_ids = args.user_id or (
      [a["user_id"] for a in record["analysts"]] if record else OFFLINE_USERS
  )
  memories = {user_id: load(user_id) for user_id in user_ids}
  for user_id, memory in memories.items():
    if not memory.traces:
      print(f"{user_id}: no sessions found; left out of the export")
  users = [
      build_user_export(memories[user_id])
      for user_id in user_ids
      if memories[user_id].traces
  ]
  run, comparisons = None, []
  if record is not None:
    controls = {
        s["user_id"]
        for s in record.get("sessions", [])
        if s.get("memory") == "off"
    }
    for user_id in sorted(controls):
      memories[user_id] = load(user_id)
    names = {a["user_id"]: a["name"] for a in record.get("analysts", [])}
    comparisons = build_comparisons(
        record, memories, names, memory_dataset=args.dataset_id
    )
    if not args.show_project:
      hide_project(comparisons, args.project_id)
    run = build_run(record, users)
  export = build_export(
      users,
      label=label,
      source=source,
      exported_at=_iso(now) or "",
      run=run,
      comparisons=comparisons,
  )
  args.out.parent.mkdir(parents=True, exist_ok=True)
  args.out.write_text(
      json.dumps(export, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
  )
  for user in users:
    print(
        f"{user['user_id']}: {len(user['sessions'])} sessions,"
        f" {len(user['traces'])} traces, {len(user['preferences'])} preference"
        f" versions, {len(user['entities'])} entities,"
        f" {len(user['facts'])} facts"
    )
  if comparisons:
    flawed = [c["name"] for c in comparisons if c["control_read_memory"]]
    print(
        f"{len(comparisons)} before/after comparisons"
        + (
            f"; the run without memory read the memory tables for"
            f" {', '.join(flawed)}"
            if flawed
            else ""
        )
    )
  print(f"wrote {args.out}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
