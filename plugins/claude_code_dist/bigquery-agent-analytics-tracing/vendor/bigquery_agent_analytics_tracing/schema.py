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

"""BigQuery ``agent_events`` schema matching the canonical BQAA shape.

Kept as a function so callers pass in their own ``google.cloud.bigquery``
module — the producer package does not import BigQuery at import time so
dry-run callers (and unit tests) never need the dependency.
"""

from __future__ import annotations

from typing import Any


def bq_schema(bigquery: Any) -> list[Any]:
  """Return the BigQuery schema for the ``agent_events`` table.

  Used by both the direct writer's ``auto_create_table`` path and the
  drainer's ``insert_rows_json`` fallback. The Storage Write API path
  declares the same shape as an Arrow schema in ``drain.py``.
  """
  return [
      bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
      bigquery.SchemaField("event_type", "STRING"),
      bigquery.SchemaField("agent", "STRING"),
      bigquery.SchemaField("session_id", "STRING"),
      bigquery.SchemaField("invocation_id", "STRING"),
      bigquery.SchemaField("user_id", "STRING"),
      bigquery.SchemaField("trace_id", "STRING"),
      bigquery.SchemaField("span_id", "STRING"),
      bigquery.SchemaField("parent_span_id", "STRING"),
      bigquery.SchemaField("content", "JSON"),
      bigquery.SchemaField(
          "content_parts",
          "RECORD",
          mode="REPEATED",
          fields=[
              bigquery.SchemaField("mime_type", "STRING"),
              bigquery.SchemaField("uri", "STRING"),
              bigquery.SchemaField(
                  "object_ref",
                  "RECORD",
                  fields=[
                      bigquery.SchemaField("uri", "STRING"),
                      bigquery.SchemaField("version", "STRING"),
                      bigquery.SchemaField("authorizer", "STRING"),
                      bigquery.SchemaField("details", "JSON"),
                  ],
              ),
              bigquery.SchemaField("text", "STRING"),
              bigquery.SchemaField("part_index", "INTEGER"),
              bigquery.SchemaField("part_attributes", "STRING"),
              bigquery.SchemaField("storage_mode", "STRING"),
          ],
      ),
      bigquery.SchemaField("attributes", "JSON"),
      bigquery.SchemaField("latency_ms", "JSON"),
      bigquery.SchemaField("status", "STRING"),
      bigquery.SchemaField("error_message", "STRING"),
      bigquery.SchemaField("is_truncated", "BOOLEAN"),
  ]
