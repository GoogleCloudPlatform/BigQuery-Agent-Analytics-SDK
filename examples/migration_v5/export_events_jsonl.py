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

"""Export captured events from BigQuery to a local JSONL
snapshot.

This is **not** an event generator. The event stream's
source of truth is the BQ AA plugin's ``agent_events``
table, populated by running ``mako_demo_agent.py`` via
``run_agent.py``. This helper exports a subset of that
table to a local ``events.jsonl`` file so the notebook's
revalidation tests (Beat 3) have a deterministic offline
corpus to gate against — same input every run regardless
of when the live agent last ran.

Usage:

    PYTHONPATH=src python examples/migration_v5/export_events_jsonl.py \\
        --project test-project-0728-467323 \\
        --dataset migration_v5_demo \\
        --table agent_events \\
        --out examples/migration_v5/events.jsonl \\
        --limit 200

Pin a fixed ``--limit`` so the captured snapshot stays
small and stable. The notebook regenerates this only when
the demo's event semantics change.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

_SELECT_SQL = """
SELECT
  event_id,
  invocation_id,
  session_id,
  user_id,
  agent_name,
  event_type,
  TO_JSON_STRING(payload)         AS payload_json,
  TO_JSON_STRING(content)         AS content_json,
  event_timestamp,
  partition_date
FROM `{table}`
ORDER BY event_timestamp, event_id
LIMIT @row_limit
"""


def main(argv=None) -> int:
  from google.cloud import bigquery

  parser = argparse.ArgumentParser(
      description=(
          "Export a deterministic offline snapshot of "
          "agent_events for the notebook's revalidation "
          "tests."
      ),
  )
  parser.add_argument("--project", required=True)
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--table", default="agent_events")
  parser.add_argument("--location", default="US")
  parser.add_argument(
      "--out",
      type=pathlib.Path,
      default=pathlib.Path(__file__).parent / "events.jsonl",
  )
  parser.add_argument("--limit", type=int, default=200)
  args = parser.parse_args(argv)

  table_id = f"{args.project}.{args.dataset}.{args.table}"
  client = bigquery.Client(project=args.project, location=args.location)
  sql = _SELECT_SQL.format(table=table_id)
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("row_limit", "INT64", args.limit),
      ]
  )
  print(f"Exporting from {table_id} (LIMIT {args.limit})", file=sys.stderr)
  rows = list(client.query(sql, job_config=job_config).result())
  args.out.write_text(
      "\n".join(_row_to_jsonl(r) for r in rows) + "\n",
      encoding="utf-8",
  )
  print(f"Wrote {len(rows)} events to {args.out}", file=sys.stderr)
  return 0


def _row_to_jsonl(row) -> str:
  """One JSON line per row. Keeps the plugin schema verbatim
  so downstream revalidation tests see the same shape they'd
  see querying BQ directly. ``payload`` and ``content`` come
  back as JSON-encoded strings (via ``TO_JSON_STRING`` in
  SQL); decode them here so the JSONL nest is a single dict."""
  payload = json.loads(row["payload_json"]) if row["payload_json"] else None
  content = json.loads(row["content_json"]) if row["content_json"] else None
  return json.dumps(
      {
          "event_id": row["event_id"],
          "invocation_id": row["invocation_id"],
          "session_id": row["session_id"],
          "user_id": row["user_id"],
          "agent_name": row["agent_name"],
          "event_type": row["event_type"],
          "payload": payload,
          "content": content,
          "event_timestamp": str(row["event_timestamp"]),
          "partition_date": str(row["partition_date"]),
      },
      sort_keys=True,
  )


if __name__ == "__main__":  # pragma: no cover
  sys.exit(main())
