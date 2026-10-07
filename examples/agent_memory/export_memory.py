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
``memory_layers.py``: sessions with their conversations, preference versions,
entities, reasoning traces with a per-invocation timeline, tool stats, and
the ``get_context()`` block. Each user is read with one
``Client.list_traces`` call.

  python examples/agent_memory/export_memory.py            # offline fixture
  python examples/agent_memory/export_memory.py \\
      --project-id my-project --dataset-id bqaa_agent_memory_demo \\
      --user-id demo-ana --user-id demo-ben                 # live table

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
import sys
from typing import Any, Optional

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import make_bq_client
from bigquery_agent_analytics import Span

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_memory_demo  # noqa: E402
import memory_layers  # noqa: E402
import offline_bigquery  # noqa: E402

SCHEMA = "bqaa-agent-memory-viz/1"
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
  """Model turns and tool calls of one invocation, in ms from its start."""
  start = spans[0].timestamp

  def offset(span: Span) -> float:
    return (span.timestamp - start) / timedelta(milliseconds=1)

  rows: list[dict[str, Any]] = []
  requests: dict[Any, Span] = {}
  open_tools: dict[Any, dict[str, Any]] = {}
  for span in spans:
    if span.event_type == "LLM_REQUEST":
      requests[span.span_id] = span
    elif span.event_type in ("LLM_RESPONSE", "LLM_ERROR"):
      request = requests.pop(span.span_id, span)
      texts, calls = memory_layers.response_parts(span.content.get("response"))
      if calls:
        label = "call: " + ", ".join(calls)
      elif texts:
        label = "answer"
      else:
        label = "model turn"
      failed = span.event_type == "LLM_ERROR" or span.is_error
      rows.append(
          {
              "kind": "model",
              "label": label,
              "start_ms": offset(request),
              "end_ms": offset(span),
              "status": "error" if failed else "success",
              "span_id": span.span_id,
              "detail": span.error_message
              if failed
              else (" ".join(texts) or None),
          }
      )
    elif span.event_type == "TOOL_STARTING":
      row = {
          "kind": "tool",
          "label": span.content.get("tool") or "unknown",
          "start_ms": offset(span),
          "end_ms": None,
          "status": "pending",
          "span_id": span.span_id,
          "detail": None,
      }
      open_tools[span.span_id or row["label"]] = row
      rows.append(row)
    elif span.event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
      key = span.span_id or span.content.get("tool")
      row = open_tools.pop(key, None)
      if row is None:
        row = {
            "kind": "tool",
            "label": span.content.get("tool") or "unknown",
            "start_ms": offset(span),
            "span_id": span.span_id,
        }
        rows.append(row)
      failed = span.event_type == "TOOL_ERROR" or span.is_error
      row["end_ms"] = offset(span)
      row["status"] = "error" if failed else "success"
      row["detail"] = (
          span.error_message if failed else _short(span.content.get("result"))
      )
  return rows


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


def build_user_export(
    memory: memory_layers.UserMemory,
    *,
    current_session_id: Optional[str] = None,
    query: Optional[str] = None,
) -> dict[str, Any]:
  """One user's memory layers as JSON-ready dicts (times in UTC ISO)."""
  infos = memory.short_term.list_sessions()
  current = current_session_id or (infos[0].session_id if infos else None)
  sessions = []
  for info in sorted(infos, key=lambda i: i.created_at):
    sessions.append(
        {
            "session_id": info.session_id,
            "created_at": _iso(info.created_at),
            "updated_at": _iso(info.updated_at),
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
    traces.append(
        {
            "trace_id": rt.trace_id,
            "session_id": rt.session_id,
            "task": rt.task,
            "outcome": rt.outcome,
            "outcome_status": rt.outcome_status,
            "started_at": _iso(rt.started_at),
            "completed_at": _iso(rt.completed_at),
            "latency_ms": rt.latency_ms,
            "llm_calls": rt.llm_calls,
            "total_tokens": rt.total_tokens,
            "errors": list(dict.fromkeys(rt.errors)),
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
  return {
      "user_id": memory.user_id,
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


def build_export(
    users: list[dict[str, Any]],
    *,
    label: str,
    source: str,
    exported_at: str,
) -> dict[str, Any]:
  return {
      "schema": SCHEMA,
      "label": label,
      "source": source,
      "exported_at": exported_at,
      "users": users,
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
      "--fixture", type=Path, default=offline_bigquery.DEFAULT_FIXTURE
  )
  parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
  parser.add_argument("--label", help="run label shown in the page header")
  parser.add_argument(
      "--show-project",
      action="store_true",
      help="show the real project id in the export (default: <project>)",
  )
  args = parser.parse_args(argv)
  if bool(args.project_id) != bool(args.dataset_id):
    parser.error("--project-id and --dataset-id go together")
  try:
    entity_args = agent_memory_demo.parse_entity_args(args.entity_arg)
  except argparse.ArgumentTypeError as e:
    parser.error(str(e))

  if args.project_id:
    if not args.user_id:
      parser.error("live mode needs at least one --user-id")
    now = (
        offline_bigquery.parse_timestamp(args.now)
        if args.now
        else datetime.now(timezone.utc)
    )
    client = Client(
        project_id=args.project_id,
        dataset_id=args.dataset_id,
        table_id=args.table_id,
        location=args.location,
        verify_schema=False,
        bq_client=bq_client
        or make_bq_client(args.project_id, location=args.location),
    )
    project = args.project_id if args.show_project else "<project>"
    source = f"{project}.{args.dataset_id}.{args.table_id}"
    label = args.label or "BigQuery read"
  else:
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

  users = []
  for user_id in args.user_id or OFFLINE_USERS:
    memory = memory_layers.load_user_memory(
        client,
        user_id,
        since=now - timedelta(days=args.lookback_days),
        entity_args=entity_args,
    )
    users.append(build_user_export(memory))
  export = build_export(
      users, label=label, source=source, exported_at=_iso(now) or ""
  )
  args.out.parent.mkdir(parents=True, exist_ok=True)
  args.out.write_text(
      json.dumps(export, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
  )
  for user in users:
    print(
        f"{user['user_id']}: {len(user['sessions'])} sessions,"
        f" {len(user['traces'])} traces, {len(user['preferences'])} preference"
        f" versions, {len(user['entities'])} entities"
    )
  print(f"wrote {args.out}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
