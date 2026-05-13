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

The selected columns are the subset of the BQ AA plugin's
event schema that the notebook's revalidation tests need
(see ``google/adk/plugins/bigquery_agent_analytics_plugin.py::
_get_events_schema``). Top-level scalar fields ----
``timestamp``, ``event_type``, ``agent``, ``session_id``,
``invocation_id``, ``user_id``, ``trace_id``, ``span_id``,
``parent_span_id``, ``status``, ``error_message``,
``is_truncated`` ---- plus JSON ``content`` / ``attributes``
/ ``latency_ms``. The plugin's full schema also includes
``content_parts`` (a REPEATED RECORD for multimodal
parts); this exporter omits it because the MAKO decision
flow is text-only. Add it back with
``TO_JSON_STRING(content_parts) AS content_parts_json`` if
a future demo needs multimodal trace replay. There is
**no** ``event_id``, ``payload``, ``agent_name``, or
``partition_date`` column on the plugin's table; those
names were from an earlier draft schema and would fail at
query time.

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
import re
import sys

# BigQuery identifier discipline. ``project.dataset.table``
# segments cannot be passed as query parameters, so the
# table reference is interpolated directly into the SQL.
# Validate each segment against the BQ-permitted character
# set before interpolation so a hostile or malformed
# ``--project / --dataset / --table`` argument cannot smuggle
# whitespace, backticks, semicolons, or comment markers into
# the query. Mirrors
# ``bq_bundle_mirror._TABLE_ID_PATTERN`` (which uses the
# same character class for the same reason).
_BQ_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


_SELECT_SQL = """
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated,
  TO_JSON_STRING(content)    AS content_json,
  TO_JSON_STRING(attributes) AS attributes_json,
  TO_JSON_STRING(latency_ms) AS latency_ms_json
FROM `{table}`
ORDER BY timestamp, span_id
LIMIT @row_limit
"""


def _validated_table_id(project: str, dataset: str, table: str) -> str:
  for label, value in (
      ("project", project),
      ("dataset", dataset),
      ("table", table),
  ):
    if not isinstance(value, str) or not _BQ_SEGMENT_PATTERN.fullmatch(value):
      raise ValueError(
          f"--{label} {value!r} is not a well-formed BigQuery "
          f"identifier segment (allowed: ASCII letters, digits, "
          f"'_', '-'; no whitespace, backticks, semicolons, or "
          f"comment markers)"
      )
  return f"{project}.{dataset}.{table}"


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

  table_id = _validated_table_id(args.project, args.dataset, args.table)
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
  see querying BQ directly. JSON columns come back as
  TO_JSON_STRING-encoded strings; decode them here so the
  JSONL nest is a single dict.
  """
  return json.dumps(
      {
          "timestamp": str(row["timestamp"]),
          "event_type": row["event_type"],
          "agent": row["agent"],
          "session_id": row["session_id"],
          "invocation_id": row["invocation_id"],
          "user_id": row["user_id"],
          "trace_id": row["trace_id"],
          "span_id": row["span_id"],
          "parent_span_id": row["parent_span_id"],
          "status": row["status"],
          "error_message": row["error_message"],
          "is_truncated": row["is_truncated"],
          "content": _decode_json(row["content_json"]),
          "attributes": _decode_json(row["attributes_json"]),
          "latency_ms": _decode_json(row["latency_ms_json"]),
      },
      sort_keys=True,
  )


def _decode_json(raw):
  return json.loads(raw) if raw else None


if __name__ == "__main__":  # pragma: no cover
  sys.exit(main())
