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

"""BQAA projection contract + read-time dedup views over the native tables.

``agent_events_otlp`` re-presents selected ``otel_logs`` / ``otel_spans``
records in BQAA's ``agent_events`` shape so existing SDK workflows keep working
without migrating ``agent_events``. Because the projection's crosswalk + dedup
exceed BigQuery materialized-view limits (window functions are unsupported), the
projection is built by a scheduled ``MERGE`` into a curated table (design doc
§3); this module pins its **output column contract** and the schema-parity check
that guarantees ``agent_events`` drift fails loudly.

The native tables are append-only (at-least-once Storage Write); ``*_dedup``
plain views give exactly-once-at-read on ``idempotency_key``.
"""

from __future__ import annotations

from typing import Any

from bigquery_agent_analytics_tracing.otlp import schema as otel_schema
from bigquery_agent_analytics_tracing.schema import bq_schema


def _content_parts(bigquery: Any) -> Any:
  """``content_parts`` RECORD — mirrors the ``agent_events`` shape exactly."""
  return bigquery.SchemaField(
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
  )


def agent_events_otlp_columns(bigquery: Any) -> list[Any]:
  """Output column contract of the ``agent_events_otlp`` projection.

  Declared **independently** of ``schema.bq_schema`` (not derived from it) so
  that adding a column to canonical ``agent_events`` fails the schema-parity
  test until the projection is updated too. It is a superset: the
  ``agent_events`` columns plus derived provenance.
  """
  return [
      # --- agent_events parity columns (name/type/mode must match) ---
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
      _content_parts(bigquery),
      bigquery.SchemaField("attributes", "JSON"),
      bigquery.SchemaField("latency_ms", "JSON"),
      bigquery.SchemaField("status", "STRING"),
      bigquery.SchemaField("error_message", "STRING"),
      bigquery.SchemaField("is_truncated", "BOOLEAN"),
      # --- derived provenance (query-surface; not physical agent_events cols) ---
      bigquery.SchemaField("source_product", "STRING"),
      bigquery.SchemaField("source_signal", "STRING"),
      bigquery.SchemaField("source_event_name", "STRING"),
      bigquery.SchemaField("crosswalk_version", "STRING"),
      bigquery.SchemaField("idempotency_key", "STRING"),
  ]


def _flatten(fields: list[Any], prefix: str = "") -> list[str]:
  """``path:type:mode`` signatures for every field, recursing into RECORDs.

  Recursion is what lets the parity check catch drift *inside* nested records
  (e.g. ``content_parts.object_ref.details``), not only at the top level.
  """
  out: list[str] = []
  for f in fields:
    path = f"{prefix}{f.name}"
    out.append(f"{path}:{f.field_type}:{f.mode}")
    if f.fields:
      out.extend(_flatten(list(f.fields), prefix=f"{path}."))
  return out


def missing_agent_events_columns(bigquery: Any) -> list[str]:
  """``agent_events`` field signatures not covered by the projection.

  Empty list == the projection is a faithful superset of ``agent_events``,
  recursively (nested ``content_parts`` / ``object_ref`` fields included), so a
  change inside a nested record fails the parity test too. Drives the
  schema-parity contract test.
  """
  projected = set(_flatten(agent_events_otlp_columns(bigquery)))
  return [sig for sig in _flatten(bq_schema(bigquery)) if sig not in projected]


# Native tables that get a read-time dedup view (every otel_* analytics table;
# the dead-letter table is excluded — it is not deduped/queried as analytics).
DEDUP_TABLES = tuple(
    name for name in otel_schema.NATIVE_TABLES if name != "otlp_dead_letter"
)


def dedup_view_sql(table_name: str, dataset: str = "${dataset}") -> str:
  """SQL for a plain read-time dedup view over an append-only native table.

  Exactly-once-at-read on ``idempotency_key`` (keep the latest ``ingest_time``,
  i.e. newest-write-wins, so a replayed/repaired row supersedes the older one).
  A plain logical view, not a materialized view: BigQuery MVs forbid window
  functions, so ``QUALIFY ROW_NUMBER()`` cannot live in an incremental MV.
  ``dataset`` defaults to a deploy-time placeholder.
  """
  if table_name not in DEDUP_TABLES:
    raise ValueError(f"no dedup view defined for table {table_name!r}")
  return (
      f"CREATE OR REPLACE VIEW `{dataset}.{table_name}_dedup` AS\n"
      f"SELECT * FROM `{dataset}.{table_name}`\n"
      f"QUALIFY ROW_NUMBER() OVER (\n"
      f"  PARTITION BY idempotency_key ORDER BY ingest_time DESC\n"
      f") = 1;"
  )
