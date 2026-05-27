"""Synthetic agent_events generator for the BQAA codelab.

Writes a small corpus of TOOL_COMPLETED + AGENT_COMPLETED events
to the configured agent_events table. Each "session" is a
3-step decision flow: submit_request -> evaluate_option (x3) ->
commit_outcome. The session is closed by an AGENT_COMPLETED row,
which is what the materializer keys on for terminal-event
detection.

Run:

    python seed_events.py \\
        --project-id "$PROJECT_ID" \\
        --dataset-id "$DATASET" \\
        --sessions 5
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import random
import uuid

from google.cloud import bigquery

_EVENT_SCHEMA = [
    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("agent", "STRING"),
    bigquery.SchemaField("session_id", "STRING"),
    bigquery.SchemaField("invocation_id", "STRING"),
    bigquery.SchemaField("user_id", "STRING"),
    bigquery.SchemaField("trace_id", "STRING"),
    bigquery.SchemaField("span_id", "STRING"),
    bigquery.SchemaField("parent_span_id", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("error_message", "STRING"),
    bigquery.SchemaField("is_truncated", "BOOLEAN"),
    bigquery.SchemaField("content", "JSON"),
    bigquery.SchemaField("attributes", "JSON"),
    bigquery.SchemaField("latency_ms", "JSON"),
]


def _row(event_type: str, session_id: str, content: dict, ts: datetime) -> dict:
  return {
      "timestamp": ts.isoformat(),
      "event_type": event_type,
      "agent": "demo-agent",
      "session_id": session_id,
      "invocation_id": str(uuid.uuid4()),
      "user_id": "demo-user",
      "trace_id": session_id[:16],
      "span_id": str(uuid.uuid4())[:16],
      "parent_span_id": None,
      "status": "ok",
      "error_message": None,
      "is_truncated": False,
      "content": json.dumps(content),
      "attributes": "{}",
      "latency_ms": "{}",
  }


def _decision_session(now: datetime) -> list[dict]:
  session_id = f"sess-{uuid.uuid4().hex[:8]}"
  request_id = f"req-{uuid.uuid4().hex[:6]}"
  topics = [
      "approve loan",
      "schedule maintenance",
      "grant access",
      "release budget",
  ]
  topic = random.choice(topics)
  rows: list[dict] = []

  rows.append(
      _row(
          "TOOL_COMPLETED",
          session_id,
          {
              "tool": "submit_request",
              "result": {
                  "request_id": request_id,
                  "request_text": f"Should we {topic}?",
              },
          },
          now,
      )
  )

  options = [
      {
          "option_id": f"opt-{uuid.uuid4().hex[:5]}",
          "option_label": label,
          "confidence": round(random.uniform(0.1, 0.95), 2),
      }
      for label in ("yes", "no", "defer")
  ]
  for i, opt in enumerate(options):
    rows.append(
        _row(
            "TOOL_COMPLETED",
            session_id,
            {
                "tool": "evaluate_option",
                "result": {"request_id": request_id, **opt},
            },
            now + timedelta(seconds=i + 1),
        )
    )

  selected = max(options, key=lambda o: o["confidence"])
  outcome_id = f"out-{uuid.uuid4().hex[:6]}"
  rationale = (
      f"Picked '{selected['option_label']}' "
      f"(confidence {selected['confidence']:.2f}) over "
      f"the {len(options)-1} alternatives."
  )
  rows.append(
      _row(
          "TOOL_COMPLETED",
          session_id,
          {
              "tool": "commit_outcome",
              "result": {
                  "request_id": request_id,
                  "outcome_id": outcome_id,
                  "status": "committed",
                  "rationale": rationale,
              },
          },
          now + timedelta(seconds=5),
      )
  )

  rows.append(
      _row(
          "AGENT_COMPLETED",
          session_id,
          {"final": True},
          now + timedelta(seconds=6),
      )
  )
  return rows


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--project-id", required=True)
  parser.add_argument("--dataset-id", required=True)
  parser.add_argument("--sessions", type=int, default=5)
  args = parser.parse_args()

  client = bigquery.Client(project=args.project_id)
  table_ref = f"{args.project_id}.{args.dataset_id}.agent_events"
  table = bigquery.Table(table_ref, schema=_EVENT_SCHEMA)
  table.time_partitioning = bigquery.TimePartitioning(field="timestamp")
  client.create_table(table, exists_ok=True)

  rows: list[dict] = []
  now = datetime.now(timezone.utc) - timedelta(minutes=10)
  for _ in range(args.sessions):
    rows.extend(_decision_session(now))
    now += timedelta(seconds=30)

  errors = client.insert_rows_json(table_ref, rows)
  if errors:
    raise RuntimeError(f"Insert errors: {errors}")
  print(
      f"Inserted {len(rows)} events across {args.sessions} sessions "
      f"into {table_ref}"
  )


if __name__ == "__main__":
  main()
