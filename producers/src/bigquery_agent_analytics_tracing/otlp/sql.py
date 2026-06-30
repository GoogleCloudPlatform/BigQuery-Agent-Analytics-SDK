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

"""BQAA projection SQL artifacts (issue #316, PR 4).

The read-time dedup views (``projection.dedup_view_sql``, PR 1) feed two
projection surfaces:

- ``agent_events_otlp`` — a curated table built by a scheduled ``MERGE`` that
  crosswalks deduped ``otel_logs`` rows into the ``agent_events`` shape. The
  ``*_dedup`` view already applies newest-write-wins (``ORDER BY ingest_time
  DESC``), so a replayed/repaired row supersedes the older one before it reaches
  the projection; the ``MERGE`` upserts on ``idempotency_key``.
- ``bqaa_metrics`` — a typed view over the five deduped ``otel_metric_*`` tables.

The curated ``agent_events_otlp`` table schema is pinned by
``projection.agent_events_otlp_columns`` (PR 1).
"""

from __future__ import annotations

from bigquery_agent_analytics_tracing.otlp import schema as otel_schema

CROSSWALK_VERSION = "1"

# (agent_events column, SQL expression over a deduped otel_logs row).
# content_parts is intentionally deferred to the raw-body fast-follow.
_LOG_CROSSWALK: tuple[tuple[str, str], ...] = (
    ("timestamp", "timestamp"),
    ("event_type", "COALESCE(event_name, 'otlp.unknown')"),
    ("agent", "JSON_VALUE(log_attributes, '$.\"agent.name\"')"),
    ("session_id", "JSON_VALUE(log_attributes, '$.\"session.id\"')"),
    ("invocation_id", "JSON_VALUE(log_attributes, '$.\"prompt.id\"')"),
    ("user_id", "JSON_VALUE(resource_attributes, '$.\"user.id\"')"),
    ("trace_id", "trace_id"),
    ("span_id", "span_id"),
    ("parent_span_id", "CAST(NULL AS STRING)"),
    ("content", "body"),
    ("attributes", "log_attributes"),
    ("latency_ms", "CAST(NULL AS JSON)"),
    ("status", "CAST(NULL AS STRING)"),
    ("error_message", "CAST(NULL AS STRING)"),
    ("is_truncated", "FALSE"),
    ("source_product", "source_product"),
    ("source_signal", "source_signal"),
    ("source_event_name", "event_name"),
    ("crosswalk_version", f"'{CROSSWALK_VERSION}'"),
    ("idempotency_key", "idempotency_key"),
)


def agent_events_otlp_merge_sql(dataset: str = "${dataset}") -> str:
  """Scheduled ``MERGE`` that builds ``agent_events_otlp`` from deduped logs.

  Upserts on ``idempotency_key`` so a re-run (or a replayed/repaired row that the
  dedup view now surfaces as newest) updates the existing projection row rather
  than duplicating it.
  """
  cols = [c for c, _ in _LOG_CROSSWALK]
  select = ",\n    ".join(f"{expr} AS {col}" for col, expr in _LOG_CROSSWALK)
  insert_cols = ", ".join(cols)
  insert_vals = ", ".join(f"S.{c}" for c in cols)
  update_set = ",\n    ".join(
      f"{c} = S.{c}" for c in cols if c != "idempotency_key"
  )
  return (
      f"MERGE `{dataset}.agent_events_otlp` T\n"
      f"USING (\n  SELECT\n    {select}\n"
      f"  FROM `{dataset}.otel_logs_dedup`\n) S\n"
      f"ON T.idempotency_key = S.idempotency_key\n"
      f"WHEN MATCHED THEN UPDATE SET\n    {update_set}\n"
      f"WHEN NOT MATCHED THEN INSERT ({insert_cols})\n"
      f"  VALUES ({insert_vals});"
  )


# Per-metric-type projection into the unified bqaa_metrics column shape.
# (column -> expression); sum/gauge carry a scalar value, the aggregate types
# carry count + sum.
_METRIC_PROJECTIONS: dict[str, dict[str, str]] = {
    "otel_metric_sum": {
        "temporality": "aggregation_temporality",
        "value": "value",
        "count": "CAST(NULL AS INT64)",
        "sum_value": "CAST(NULL AS FLOAT64)",
    },
    "otel_metric_gauge": {
        "temporality": "CAST(NULL AS INT64)",
        "value": "value",
        "count": "CAST(NULL AS INT64)",
        "sum_value": "CAST(NULL AS FLOAT64)",
    },
    "otel_metric_histogram": {
        "temporality": "aggregation_temporality",
        "value": "CAST(NULL AS FLOAT64)",
        "count": "count",
        "sum_value": "sum",
    },
    "otel_metric_exponential_histogram": {
        "temporality": "aggregation_temporality",
        "value": "CAST(NULL AS FLOAT64)",
        "count": "count",
        "sum_value": "sum",
    },
    "otel_metric_summary": {
        "temporality": "CAST(NULL AS INT64)",
        "value": "CAST(NULL AS FLOAT64)",
        "count": "count",
        "sum_value": "sum",
    },
}


def bqaa_metrics_view_sql(dataset: str = "${dataset}") -> str:
  """Typed ``bqaa_metrics`` view UNION-ing the five deduped metric tables."""
  blocks = []
  for table in otel_schema.METRIC_TABLES:
    proj = _METRIC_PROJECTIONS[table]
    point_kind = table[len("otel_metric_") :]
    blocks.append(
        "SELECT\n"
        "  metric_name, service_name, time_timestamp, unit,\n"
        f"  '{point_kind}' AS point_kind,\n"
        f"  {proj['temporality']} AS temporality,\n"
        f"  {proj['value']} AS value,\n"
        f"  {proj['count']} AS count,\n"
        f"  {proj['sum_value']} AS sum_value,\n"
        "  attributes, source_product, idempotency_key\n"
        f"FROM `{dataset}.{table}_dedup`"
    )
  union = "\nUNION ALL\n".join(blocks)
  return f"CREATE OR REPLACE VIEW `{dataset}.bqaa_metrics` AS\n{union};"
