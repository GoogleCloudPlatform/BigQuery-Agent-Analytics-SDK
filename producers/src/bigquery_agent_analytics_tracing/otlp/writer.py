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

"""Envelope v1 -> native BigQuery rows; append-only writer (issue #316, PR 4).

Consumes the Pub/Sub envelope-v1 messages produced by the receiver (PR 3) and
maps each to a row for its native table — ``otel_logs``, ``otel_spans``
(traces signal tier), the five ``otel_metric_*`` tables, or ``otlp_dead_letter``. The
row mapping is pure and table-routing is deterministic, so the whole thing is
unit-testable with a fake writer; the real BigQuery client is lazy-imported.

Every row preserves the contract fields the dedup views and replay path depend
on: ``idempotency_key``, ``source_position`` (incl. ``raw_otlp_request_hash``),
``otel_schema_version``, ``ingest_time``, ``source_product``, ``source_signal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
import json
from typing import Any, Protocol

from bigquery_agent_analytics_tracing.otlp import envelope as env

_METRIC_TABLE = {
    "sum": "otel_metric_sum",
    "gauge": "otel_metric_gauge",
    "histogram": "otel_metric_histogram",
    "exponential_histogram": "otel_metric_exponential_histogram",
    "summary": "otel_metric_summary",
}


class TableWriteError(Exception):
  """Envelope could not be mapped/routed to a native table."""


class BigQueryWriter(Protocol):
  """Append rows to a native table (real impl wraps google-cloud-bigquery)."""

  def append(self, table: str, row: dict[str, Any]) -> None:
    ...


def _json(value: Any) -> str | None:
  """Serialize a JSON column value (matches the package convention)."""
  if value is None:
    return None
  return json.dumps(value, sort_keys=True)


def _nanos_to_iso(nanos: Any) -> str | None:
  """OTLP ``*UnixNano`` (string/int) -> RFC3339 UTC timestamp string."""
  if nanos in (None, ""):
    return None
  seconds = int(nanos) / 1_000_000_000
  return (
      datetime.fromtimestamp(seconds, tz=timezone.utc)
      .isoformat()
      .replace("+00:00", "Z")
  )


def _num(point: dict[str, Any]) -> float | int | None:
  if "asDouble" in point:
    return point["asDouble"]
  if "asInt" in point:
    return int(point["asInt"])
  return None


def _int(value: Any) -> int | None:
  return None if value in (None, "") else int(value)


def _int_list(values: Any) -> list[int]:
  return [int(v) for v in (values or [])]


def _str(value: Any) -> str | None:
  return None if value is None else str(value)


# OTLP/JSON encodes enums as ints while protobuf MessageToDict emits names;
# both must land the same canonical STRING or filters silently miss rows.
_SPAN_KIND_NAMES = {
    0: "SPAN_KIND_UNSPECIFIED",
    1: "SPAN_KIND_INTERNAL",
    2: "SPAN_KIND_SERVER",
    3: "SPAN_KIND_CLIENT",
    4: "SPAN_KIND_PRODUCER",
    5: "SPAN_KIND_CONSUMER",
}
_STATUS_CODE_NAMES = {
    0: "STATUS_CODE_UNSET",
    1: "STATUS_CODE_OK",
    2: "STATUS_CODE_ERROR",
}


def _enum_name(value: Any, names: dict[int, str]) -> str | None:
  if value is None:
    return None
  if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
    return names.get(int(value), str(value))
  return str(value)


# The reverse direction, for INTEGER columns: protobuf MessageToDict emits
# enum NAMES ("SEVERITY_NUMBER_INFO", "AGGREGATION_TEMPORALITY_DELTA") where
# OTLP/JSON carries ints. Found live in the #317 e2e: every real metric
# export and every Codex log envelope dead-lettered on these two fields.
_SEVERITY_NUMBER_VALUES = {"SEVERITY_NUMBER_UNSPECIFIED": 0} | {
    f"SEVERITY_NUMBER_{level}{n if n > 1 else ''}": base + n - 1
    for base, level in (
        (1, "TRACE"),
        (5, "DEBUG"),
        (9, "INFO"),
        (13, "WARN"),
        (17, "ERROR"),
        (21, "FATAL"),
    )
    for n in (1, 2, 3, 4)
}
_AGGREGATION_TEMPORALITY_VALUES = {
    "AGGREGATION_TEMPORALITY_UNSPECIFIED": 0,
    "AGGREGATION_TEMPORALITY_DELTA": 1,
    "AGGREGATION_TEMPORALITY_CUMULATIVE": 2,
}


def _enum_int(value: Any, values: dict[str, int]) -> int | None:
  """Int for an INTEGER column from either enum encoding; never raises."""
  if value in (None, ""):
    return None
  if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
    return int(value)
  return values.get(str(value))


def _receiver_metadata_row(envelope: dict[str, Any]) -> dict[str, Any]:
  return {
      "source_product": envelope["source"]["product"],
      "source_signal": envelope["source"]["signal"],
      "idempotency_key": envelope["idempotency_key"],
      "source_position": envelope["source_position"],
      "ingest_time": envelope["ingest_time"],
      "raw_preservation": envelope.get("raw_preservation"),
      "otel_schema_version": envelope.get("otel_schema_version"),
  }


def _exemplars_rows(point: dict[str, Any]) -> list[dict[str, Any]]:
  rows = []
  for ex in point.get("exemplars", []) or []:
    rows.append(
        {
            "time_timestamp": _nanos_to_iso(ex.get("timeUnixNano")),
            "value": _num(ex),
            "span_id": ex.get("spanId"),
            "trace_id": ex.get("traceId"),
            "filtered_attributes": _json(
                env.otlp_attrs_to_dict(ex.get("filteredAttributes"))
            ),
        }
    )
  return rows


def log_row(envelope: dict[str, Any]) -> dict[str, Any]:
  record = envelope["record"]
  resource = envelope["otlp"]["resource_attributes"]
  scope = envelope["otlp"].get("scope") or {}
  row = {
      "timestamp": (
          _nanos_to_iso(record.get("timeUnixNano"))
          or _nanos_to_iso(record.get("observedTimeUnixNano"))
          or envelope["ingest_time"]
      ),
      "observed_timestamp": _nanos_to_iso(record.get("observedTimeUnixNano")),
      "trace_id": record.get("traceId"),
      "span_id": record.get("spanId"),
      "trace_flags": _int(record.get("flags")),
      "severity_text": record.get("severityText"),
      "severity_number": _enum_int(
          record.get("severityNumber"), _SEVERITY_NUMBER_VALUES
      ),
      "service_name": resource.get("service.name"),
      "body": _json(record.get("body")),
      "resource_attributes": _json(resource),
      "scope_name": scope.get("name"),
      "scope_version": scope.get("version"),
      "scope_attributes": _json(
          env.otlp_attrs_to_dict(scope.get("attributes"))
      ),
      "log_attributes": _json(env.otlp_attrs_to_dict(record.get("attributes"))),
      "event_name": record.get("eventName"),
  }
  row.update(_receiver_metadata_row(envelope))
  return row


def span_row(envelope: dict[str, Any]) -> dict[str, Any]:
  record = envelope["record"]
  resource = envelope["otlp"]["resource_attributes"]
  scope = envelope["otlp"].get("scope") or {}
  status = record.get("status") or {}
  row = {
      "timestamp": (
          _nanos_to_iso(record.get("startTimeUnixNano"))
          or envelope["ingest_time"]
      ),
      "end_timestamp": _nanos_to_iso(record.get("endTimeUnixNano")),
      "trace_id": record.get("traceId"),
      "span_id": record.get("spanId"),
      "parent_span_id": record.get("parentSpanId"),
      "trace_state": record.get("traceState"),
      "span_name": record.get("name"),
      "span_kind": _enum_name(record.get("kind"), _SPAN_KIND_NAMES),
      "service_name": resource.get("service.name"),
      "status_code": _enum_name(status.get("code"), _STATUS_CODE_NAMES),
      "status_message": status.get("message"),
      "resource_attributes": _json(resource),
      "scope_name": scope.get("name"),
      "scope_version": scope.get("version"),
      "scope_attributes": _json(
          env.otlp_attrs_to_dict(scope.get("attributes"))
      ),
      "span_attributes": _json(
          env.otlp_attrs_to_dict(record.get("attributes"))
      ),
      "events": [
          {
              "timestamp": _nanos_to_iso(event.get("timeUnixNano")),
              "name": event.get("name"),
              "attributes": _json(
                  env.otlp_attrs_to_dict(event.get("attributes"))
              ),
          }
          for event in record.get("events", []) or []
      ],
      "links": [
          {
              "trace_id": link.get("traceId"),
              "span_id": link.get("spanId"),
              "trace_state": link.get("traceState"),
              "attributes": _json(
                  env.otlp_attrs_to_dict(link.get("attributes"))
              ),
          }
          for link in record.get("links", []) or []
      ],
  }
  row.update(_receiver_metadata_row(envelope))
  return row


def _metric_common_row(envelope: dict[str, Any]) -> dict[str, Any]:
  record = envelope["record"]
  point = record["point"]
  resource = envelope["otlp"]["resource_attributes"]
  scope = envelope["otlp"].get("scope") or {}
  row = {
      "service_name": resource.get("service.name"),
      "metric_name": record.get("metric_name"),
      "metric_description": record.get("description"),
      "unit": record.get("unit"),
      "start_timestamp": _nanos_to_iso(point.get("startTimeUnixNano")),
      "time_timestamp": (
          _nanos_to_iso(point.get("timeUnixNano")) or envelope["ingest_time"]
      ),
      "flags": _int(point.get("flags")),
      "resource_attributes": _json(resource),
      "scope_name": scope.get("name"),
      "scope_version": scope.get("version"),
      "scope_attributes": _json(
          env.otlp_attrs_to_dict(scope.get("attributes"))
      ),
      "attributes": _json(env.otlp_attrs_to_dict(point.get("attributes"))),
  }
  row.update(_receiver_metadata_row(envelope))
  return row


def _buckets(buckets: dict[str, Any] | None) -> dict[str, Any]:
  buckets = buckets or {}
  return {
      "offset": _int(buckets.get("offset")),
      "bucket_counts": _int_list(buckets.get("bucketCounts")),
  }


def metric_row(envelope: dict[str, Any]) -> dict[str, Any]:
  record = envelope["record"]
  point = record["point"]
  kind = record["point_kind"]
  row = _metric_common_row(envelope)
  temporality = _enum_int(
      record.get("aggregation_temporality"), _AGGREGATION_TEMPORALITY_VALUES
  )
  if kind in ("sum", "gauge"):
    row["value"] = _num(point)
    row["exemplars"] = _exemplars_rows(point)
    if kind == "sum":
      row["aggregation_temporality"] = temporality
      row["is_monotonic"] = record.get("is_monotonic")
  elif kind == "histogram":
    row.update(
        {
            "count": _int(point.get("count")),
            "sum": point.get("sum"),
            "min": point.get("min"),
            "max": point.get("max"),
            "bucket_counts": _int_list(point.get("bucketCounts")),
            "explicit_bounds": list(point.get("explicitBounds") or []),
            "aggregation_temporality": temporality,
            "exemplars": _exemplars_rows(point),
        }
    )
  elif kind == "exponential_histogram":
    row.update(
        {
            "count": _int(point.get("count")),
            "sum": point.get("sum"),
            "min": point.get("min"),
            "max": point.get("max"),
            "scale": _int(point.get("scale")),
            "zero_count": _int(point.get("zeroCount")),
            "zero_threshold": point.get("zeroThreshold"),
            "positive": _buckets(point.get("positive")),
            "negative": _buckets(point.get("negative")),
            "aggregation_temporality": temporality,
            "exemplars": _exemplars_rows(point),
        }
    )
  elif kind == "summary":
    row.update(
        {
            "count": _int(point.get("count")),
            "sum": point.get("sum"),
            "quantile_values": [
                {"quantile": q.get("quantile"), "value": q.get("value")}
                for q in point.get("quantileValues", []) or []
            ],
        }
    )
  else:
    raise TableWriteError(f"unknown metric point_kind {kind!r}")
  return row


def dead_letter_row(envelope: dict[str, Any]) -> dict[str, Any]:
  parse_error = envelope.get("parse_error") or {}
  return {
      "received_at": envelope["ingest_time"],
      "stage": parse_error.get("stage"),
      "reason": parse_error.get("reason"),
      "source_product": envelope["source"]["product"],
      "source_signal": envelope["source"]["signal"],
      "idempotency_key": envelope.get("idempotency_key"),
      "source_position": envelope.get("source_position"),
      "raw_b64": (envelope.get("raw_preservation") or {}).get("raw_b64"),
  }


def target_table(envelope: dict[str, Any]) -> str:
  """The native table an envelope lands in."""
  if envelope.get("delivery", {}).get("dlq"):
    return "otlp_dead_letter"
  signal = envelope["source"]["signal"]
  if signal == "log":
    return "otel_logs"
  if signal == "metric":
    kind = envelope["record"]["point_kind"]
    if kind not in _METRIC_TABLE:
      raise TableWriteError(f"unknown metric point_kind {kind!r}")
    return _METRIC_TABLE[kind]
  if signal == "span":
    return "otel_spans"
  raise TableWriteError(f"no native table for signal {signal!r}")


def envelope_to_row(envelope: dict[str, Any]) -> dict[str, Any]:
  """Map an envelope to its native BigQuery row."""
  if envelope.get("delivery", {}).get("dlq"):
    return dead_letter_row(envelope)
  signal = envelope["source"]["signal"]
  if signal == "log":
    return log_row(envelope)
  if signal == "metric":
    return metric_row(envelope)
  if signal == "span":
    return span_row(envelope)
  raise TableWriteError(f"no row mapping for signal {signal!r}")


def append_envelope(
    envelope: dict[str, Any],
    writer: BigQueryWriter,
    *,
    enable_spans: bool = False,
) -> str:
  """Map an envelope to a row and append it to the right native table.

  Returns the target table name. Span envelopes are dropped unless
  ``enable_spans`` (the traces signal tier, BQAA_OTLP_ENABLE_TRACES).
  """
  if envelope["source"]["signal"] == "span" and not enable_spans:
    return ""  # traces tier disabled on this consumer
  table = target_table(envelope)
  writer.append(table, envelope_to_row(envelope))
  return table


def handle_message(
    data: bytes,
    writer: BigQueryWriter,
    *,
    enable_spans: bool = False,
) -> str:
  """Decode one Pub/Sub envelope-v1 message and append it. Returns the table."""
  envelope = json.loads(data.decode("utf-8"))
  return append_envelope(envelope, writer, enable_spans=enable_spans)
