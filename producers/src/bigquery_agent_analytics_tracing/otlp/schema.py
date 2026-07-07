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

"""OTel-native BigQuery table schemas for the OTLP receiver (issue #316).

The receiver lands raw OpenTelemetry logs / metric points / (gated) spans into
these tables as the **source of truth**; BQAA ``agent_events`` is delivered as a
projection on top (see ``projection.py``). The layout follows the ClickHouse
OpenTelemetry exporter — the only OTLP-faithful named reference — adapted to
BigQuery: open attribute bags are native ``JSON``, low-cardinality identity is
promoted to top-level columns for partition/cluster keys, and metrics are split
into five per-type tables (Sum / Gauge / Histogram / ExponentialHistogram /
Summary) matching the OTLP proto ``oneof``.

Like ``schema.py``, every schema is a function that takes the caller's
``google.cloud.bigquery`` module, so dry-run callers and unit tests can pass a
fake and never need the dependency installed.

See ``docs/otlp_receiver_design.md`` for the full contract.
"""

from __future__ import annotations

from typing import Any

# Bumps when the physical table layout changes. Stamped as a table label and
# carried in the Pub/Sub envelope; #317/#318 bind a minimum version.
OTEL_SCHEMA_VERSION = "1"


def _source_position(bigquery: Any, mode: str = "NULLABLE") -> Any:
  """Stable, replay-invariant position of a record within its OTLP request.

  Required input to the log/metric idempotency keys (§4.1 of the design doc):
  it distinguishes two legitimately identical records/points in the same
  request, and survives DLQ/replay unchanged. The nested index fields stay
  nullable (a log has no ``data_point_index``; a whole-request dead-letter may
  have none), but ``mode`` controls whether the top-level RECORD is required —
  it is on native analytics tables, nullable on the dead-letter table.
  """
  return bigquery.SchemaField(
      "source_position",
      "RECORD",
      mode=mode,
      fields=[
          bigquery.SchemaField("raw_otlp_request_hash", "STRING"),
          bigquery.SchemaField("resource_index", "INTEGER"),
          bigquery.SchemaField("scope_index", "INTEGER"),
          bigquery.SchemaField("record_index", "INTEGER"),
          bigquery.SchemaField("metric_index", "INTEGER"),
          bigquery.SchemaField("data_point_index", "INTEGER"),
      ],
  )


def _receiver_metadata(bigquery: Any) -> list[Any]:
  """Receiver-added columns on every native **analytics** table.

  The dedup-identity fields are ``REQUIRED``: the ``*_dedup`` views partition on
  ``idempotency_key``, so a null key would collapse unrelated rows into one.
  The dead-letter table (``otlp_dead_letter_schema``) deliberately keeps these
  nullable, since a whole-request failure may lack a full ``source_position``.
  """
  return [
      bigquery.SchemaField(
          "source_product", "STRING", mode="REQUIRED"
      ),  # claude_code | codex
      bigquery.SchemaField(
          "source_signal", "STRING", mode="REQUIRED"
      ),  # log | metric | span
      bigquery.SchemaField("idempotency_key", "STRING", mode="REQUIRED"),
      _source_position(bigquery, mode="REQUIRED"),
      bigquery.SchemaField("ingest_time", "TIMESTAMP", mode="REQUIRED"),
      bigquery.SchemaField(
          "raw_preservation",
          "RECORD",
          fields=[
              bigquery.SchemaField("policy", "STRING"),
              bigquery.SchemaField("raw_b64", "STRING"),
          ],
      ),
      bigquery.SchemaField("otel_schema_version", "STRING", mode="REQUIRED"),
  ]


def _exemplars(bigquery: Any) -> Any:
  """Metric-point exemplars — the native metric->trace correlation key."""
  return bigquery.SchemaField(
      "exemplars",
      "RECORD",
      mode="REPEATED",
      fields=[
          bigquery.SchemaField("time_timestamp", "TIMESTAMP"),
          bigquery.SchemaField("value", "FLOAT"),
          bigquery.SchemaField("span_id", "STRING"),
          bigquery.SchemaField("trace_id", "STRING"),
          bigquery.SchemaField("filtered_attributes", "JSON"),
      ],
  )


def _metric_common(bigquery: Any) -> list[Any]:
  """Columns shared by all five metric tables (before the per-type tail)."""
  return [
      bigquery.SchemaField("service_name", "STRING"),  # promoted, clustered
      bigquery.SchemaField("metric_name", "STRING"),  # promoted, clustered
      bigquery.SchemaField("metric_description", "STRING"),
      bigquery.SchemaField("unit", "STRING"),
      bigquery.SchemaField("start_timestamp", "TIMESTAMP"),
      bigquery.SchemaField("time_timestamp", "TIMESTAMP", mode="REQUIRED"),
      bigquery.SchemaField("flags", "INTEGER"),
      bigquery.SchemaField("resource_attributes", "JSON"),
      bigquery.SchemaField("scope_name", "STRING"),
      bigquery.SchemaField("scope_version", "STRING"),
      bigquery.SchemaField("scope_attributes", "JSON"),
      bigquery.SchemaField("attributes", "JSON"),  # data point attributes
  ]


def otel_logs_schema(bigquery: Any) -> list[Any]:
  """``otel_logs`` — one row per OTLP log record."""
  return [
      bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
      bigquery.SchemaField("observed_timestamp", "TIMESTAMP"),
      bigquery.SchemaField("trace_id", "STRING"),
      bigquery.SchemaField("span_id", "STRING"),
      bigquery.SchemaField("trace_flags", "INTEGER"),
      bigquery.SchemaField("severity_text", "STRING"),
      bigquery.SchemaField("severity_number", "INTEGER"),  # promoted, clustered
      bigquery.SchemaField("service_name", "STRING"),  # promoted, clustered
      bigquery.SchemaField("body", "JSON"),
      bigquery.SchemaField("resource_attributes", "JSON"),
      bigquery.SchemaField("scope_name", "STRING"),
      bigquery.SchemaField("scope_version", "STRING"),
      bigquery.SchemaField("scope_attributes", "JSON"),
      bigquery.SchemaField("log_attributes", "JSON"),
      bigquery.SchemaField("event_name", "STRING"),
  ] + _receiver_metadata(bigquery)


def otel_metric_sum_schema(bigquery: Any) -> list[Any]:
  """``otel_metric_sum`` — monotonic/non-monotonic sums (NumberDataPoint)."""
  return (
      _metric_common(bigquery)
      + [
          bigquery.SchemaField("value", "FLOAT"),
          bigquery.SchemaField("aggregation_temporality", "INTEGER"),
          bigquery.SchemaField("is_monotonic", "BOOLEAN"),
          _exemplars(bigquery),
      ]
      + _receiver_metadata(bigquery)
  )


def otel_metric_gauge_schema(bigquery: Any) -> list[Any]:
  """``otel_metric_gauge`` — gauges (NumberDataPoint)."""
  return (
      _metric_common(bigquery)
      + [
          bigquery.SchemaField("value", "FLOAT"),
          _exemplars(bigquery),
      ]
      + _receiver_metadata(bigquery)
  )


def otel_metric_histogram_schema(bigquery: Any) -> list[Any]:
  """``otel_metric_histogram`` — explicit-bucket histograms."""
  return (
      _metric_common(bigquery)
      + [
          bigquery.SchemaField("count", "INTEGER"),
          bigquery.SchemaField("sum", "FLOAT"),
          bigquery.SchemaField("min", "FLOAT"),
          bigquery.SchemaField("max", "FLOAT"),
          bigquery.SchemaField("bucket_counts", "INTEGER", mode="REPEATED"),
          bigquery.SchemaField("explicit_bounds", "FLOAT", mode="REPEATED"),
          bigquery.SchemaField("aggregation_temporality", "INTEGER"),
          _exemplars(bigquery),
      ]
      + _receiver_metadata(bigquery)
  )


def otel_metric_exponential_histogram_schema(bigquery: Any) -> list[Any]:
  """``otel_metric_exponential_histogram`` — base-2 exponential histograms."""
  buckets = lambda name: bigquery.SchemaField(  # noqa: E731
      name,
      "RECORD",
      fields=[
          bigquery.SchemaField("offset", "INTEGER"),
          bigquery.SchemaField("bucket_counts", "INTEGER", mode="REPEATED"),
      ],
  )
  return (
      _metric_common(bigquery)
      + [
          bigquery.SchemaField("count", "INTEGER"),
          bigquery.SchemaField("sum", "FLOAT"),
          bigquery.SchemaField("min", "FLOAT"),
          bigquery.SchemaField("max", "FLOAT"),
          bigquery.SchemaField("scale", "INTEGER"),
          bigquery.SchemaField("zero_count", "INTEGER"),
          bigquery.SchemaField("zero_threshold", "FLOAT"),
          buckets("positive"),
          buckets("negative"),
          bigquery.SchemaField("aggregation_temporality", "INTEGER"),
          _exemplars(bigquery),
      ]
      + _receiver_metadata(bigquery)
  )


def otel_metric_summary_schema(bigquery: Any) -> list[Any]:
  """``otel_metric_summary`` — legacy summaries (no exemplars/temporality)."""
  return (
      _metric_common(bigquery)
      + [
          bigquery.SchemaField("count", "INTEGER"),
          bigquery.SchemaField("sum", "FLOAT"),
          bigquery.SchemaField(
              "quantile_values",
              "RECORD",
              mode="REPEATED",
              fields=[
                  bigquery.SchemaField("quantile", "FLOAT"),
                  bigquery.SchemaField("value", "FLOAT"),
              ],
          ),
      ]
      + _receiver_metadata(bigquery)
  )


def otel_spans_schema(bigquery: Any) -> list[Any]:
  """``otel_spans`` — one row per span (trace-gated)."""
  return [
      bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),  # start
      bigquery.SchemaField("end_timestamp", "TIMESTAMP"),
      bigquery.SchemaField("trace_id", "STRING"),
      bigquery.SchemaField("span_id", "STRING"),
      bigquery.SchemaField("parent_span_id", "STRING"),
      bigquery.SchemaField("trace_state", "STRING"),
      bigquery.SchemaField("span_name", "STRING"),  # promoted, clustered
      bigquery.SchemaField("span_kind", "STRING"),
      bigquery.SchemaField("service_name", "STRING"),  # promoted, clustered
      bigquery.SchemaField("status_code", "STRING"),
      bigquery.SchemaField("status_message", "STRING"),
      bigquery.SchemaField("resource_attributes", "JSON"),
      bigquery.SchemaField("scope_name", "STRING"),
      bigquery.SchemaField("scope_version", "STRING"),
      bigquery.SchemaField("scope_attributes", "JSON"),
      bigquery.SchemaField("span_attributes", "JSON"),
      bigquery.SchemaField(
          "events",
          "RECORD",
          mode="REPEATED",
          fields=[
              bigquery.SchemaField("timestamp", "TIMESTAMP"),
              bigquery.SchemaField("name", "STRING"),
              bigquery.SchemaField("attributes", "JSON"),
          ],
      ),
      bigquery.SchemaField(
          "links",
          "RECORD",
          mode="REPEATED",
          fields=[
              bigquery.SchemaField("trace_id", "STRING"),
              bigquery.SchemaField("span_id", "STRING"),
              bigquery.SchemaField("trace_state", "STRING"),
              bigquery.SchemaField("attributes", "JSON"),
          ],
      ),
  ] + _receiver_metadata(bigquery)


def otlp_dead_letter_schema(bigquery: Any) -> list[Any]:
  """``otlp_dead_letter`` — malformed/failed records; never analytics tables."""
  return [
      bigquery.SchemaField("received_at", "TIMESTAMP", mode="REQUIRED"),
      bigquery.SchemaField(
          "stage", "STRING"
      ),  # auth|otlp_decode|crosswalk|write
      bigquery.SchemaField("reason", "STRING"),
      bigquery.SchemaField("source_product", "STRING"),
      bigquery.SchemaField("source_signal", "STRING"),
      bigquery.SchemaField("idempotency_key", "STRING"),
      _source_position(bigquery),
      bigquery.SchemaField("raw_b64", "STRING"),
  ]


# Registry: table name -> (schema fn, partition field, clustering fields).
# Clustering keys are top-level, non-repeated, non-JSON columns (BigQuery
# cannot cluster on JSON), matching the design doc §2.4.
NATIVE_TABLES: dict[str, tuple[Any, str, tuple[str, ...]]] = {
    "otel_logs": (
        otel_logs_schema,
        "timestamp",
        ("service_name", "severity_number", "trace_id"),
    ),
    "otel_metric_sum": (
        otel_metric_sum_schema,
        "time_timestamp",
        ("service_name", "metric_name"),
    ),
    "otel_metric_gauge": (
        otel_metric_gauge_schema,
        "time_timestamp",
        ("service_name", "metric_name"),
    ),
    "otel_metric_histogram": (
        otel_metric_histogram_schema,
        "time_timestamp",
        ("service_name", "metric_name"),
    ),
    "otel_metric_exponential_histogram": (
        otel_metric_exponential_histogram_schema,
        "time_timestamp",
        ("service_name", "metric_name"),
    ),
    "otel_metric_summary": (
        otel_metric_summary_schema,
        "time_timestamp",
        ("service_name", "metric_name"),
    ),
    "otel_spans": (
        otel_spans_schema,
        "timestamp",
        ("service_name", "span_name", "trace_id"),
    ),
    "otlp_dead_letter": (
        otlp_dead_letter_schema,
        "received_at",
        ("source_product", "stage"),
    ),
}

# The five per-type metric tables, in declaration order.
METRIC_TABLES = (
    "otel_metric_sum",
    "otel_metric_gauge",
    "otel_metric_histogram",
    "otel_metric_exponential_histogram",
    "otel_metric_summary",
)


def table_labels(table_name: str) -> dict[str, str]:
  """Labels stamped on every native table."""
  return {
      "otel_schema_version": OTEL_SCHEMA_VERSION,
      "bqaa_table": table_name,
  }
