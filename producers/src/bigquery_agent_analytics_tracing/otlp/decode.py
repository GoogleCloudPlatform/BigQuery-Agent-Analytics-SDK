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

"""Decode OTLP logs/metrics requests into envelope-v1 messages (#316, PR 2).

Input is the **decoded** OTLP JSON structure (``ExportLogsServiceRequest`` /
``ExportMetricsServiceRequest`` as camelCase dicts — what ``protobuf -> dict``
yields at the receiver edge). One envelope is emitted per log record / metric
data point. A record that cannot be decoded becomes a dead-letter envelope
(``parse_error`` populated) rather than aborting the batch, so one bad record
never drops a whole request.

Spans (``ExportTraceServiceRequest``) joined in #324 PR 4; landing stays
gated behind the traces signal tier (``BQAA_OTLP_ENABLE_TRACES``).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from bigquery_agent_analytics_tracing.otlp import envelope as env

# OTLP metric ``oneof data`` member names, mapped to the snake_case point kind
# used by the native ``otel_metric_*`` tables.
_METRIC_KINDS = {
    "sum": "sum",
    "gauge": "gauge",
    "histogram": "histogram",
    "exponentialHistogram": "exponential_histogram",
    "summary": "summary",
}


def _metric_kind(metric: dict[str, Any]) -> str | None:
  """The OTLP ``oneof`` member present on a metric, or ``None`` if unrecognized."""
  for member in _METRIC_KINDS:
    if member in metric:
      return member
  return None


def decode_logs_request(
    request: dict[str, Any],
    *,
    source_product: str,
    raw_request: bytes,
    ingest_time: str,
) -> list[dict[str, Any]]:
  """Decode an ``ExportLogsServiceRequest`` into log envelopes.

  ``request`` is the decoded dict; ``raw_request`` is the original wire payload
  (protobuf or OTLP/HTTP JSON bytes), which is the source of both
  ``raw_otlp_request_hash`` and the **replayable** dead-letter ``raw_b64``.
  """
  raw_hash = env.request_hash(raw_request)
  raw_b64 = base64.b64encode(raw_request).decode("ascii")
  envelopes: list[dict[str, Any]] = []
  for i, resource_logs in enumerate(request.get("resourceLogs", [])):
    resource_attrs = env.otlp_attrs_to_dict(
        resource_logs.get("resource", {}).get("attributes")
    )
    for j, scope_logs in enumerate(resource_logs.get("scopeLogs", [])):
      scope = scope_logs.get("scope", {})
      for k, record in enumerate(scope_logs.get("logRecords", [])):
        position = env.SourcePosition(raw_hash, i, j, record_index=k)
        try:
          key = env.log_idempotency_key(record, resource_attrs, scope, position)
          envelopes.append(
              env.make_envelope(
                  source_product=source_product,
                  source_signal="log",
                  record=record,
                  resource_attributes=resource_attrs,
                  scope=scope,
                  source_position=position,
                  idempotency_key=key,
                  ingest_time=ingest_time,
              )
          )
        except Exception as exc:  # noqa: BLE001 - malformed -> dead letter
          envelopes.append(
              env.dead_letter_envelope(
                  source_product=source_product,
                  source_signal="log",
                  stage="otlp_decode",
                  reason=repr(exc),
                  raw_b64=raw_b64,
                  received_at=ingest_time,
                  source_position=position,
                  raw_otlp_request_hash=raw_hash,
              )
          )
  return envelopes


def _canonical_hex_id(value: Any, nbytes: int) -> Any:
  """Normalize a trace/span id to canonical lowercase hex.

  OTLP/JSON carries ids as hex, but ``MessageToDict`` on the protobuf path
  base64-encodes ``bytes`` fields — without normalization the same span
  gets two different idempotency keys and the ``trace_id`` cluster column
  mixes encodings across transports. Unrecognized shapes pass through
  untouched (identity validation happens downstream).
  """
  if not value or not isinstance(value, str):
    return value
  if len(value) == nbytes * 2:
    try:
      bytes.fromhex(value)
      return value.lower()
    except ValueError:
      pass
  try:
    raw = base64.b64decode(value, validate=True)
    if len(raw) == nbytes:
      return raw.hex()
  except (ValueError, binascii.Error):
    pass
  return value


def _normalize_span_ids(span: dict[str, Any]) -> dict[str, Any]:
  span = dict(span)
  for key, nbytes in (("traceId", 16), ("spanId", 8), ("parentSpanId", 8)):
    if key in span:
      span[key] = _canonical_hex_id(span[key], nbytes)
  if span.get("links"):
    span["links"] = [
        {
            **link,
            "traceId": _canonical_hex_id(link.get("traceId"), 16),
            "spanId": _canonical_hex_id(link.get("spanId"), 8),
        }
        for link in span["links"]
    ]
  return span


def decode_traces_request(
    request: dict[str, Any],
    *,
    source_product: str,
    raw_request: bytes,
    ingest_time: str,
) -> list[dict[str, Any]]:
  """Decode an ``ExportTraceServiceRequest`` into span envelopes.

  Spans carry a natural idempotency key (``trace_id + span_id``, normalized
  to canonical lowercase hex — see ``_canonical_hex_id``); a span missing
  that identity becomes a dead letter. ``raw_request`` is the original wire
  payload (see ``decode_logs_request``).
  """
  raw_hash = env.request_hash(raw_request)
  raw_b64 = base64.b64encode(raw_request).decode("ascii")
  envelopes: list[dict[str, Any]] = []
  for i, resource_spans in enumerate(request.get("resourceSpans", [])):
    resource_attrs = env.otlp_attrs_to_dict(
        resource_spans.get("resource", {}).get("attributes")
    )
    for j, scope_spans in enumerate(resource_spans.get("scopeSpans", [])):
      scope = scope_spans.get("scope", {})
      for k, span in enumerate(scope_spans.get("spans", [])):
        position = env.SourcePosition(raw_hash, i, j, record_index=k)
        try:
          span = _normalize_span_ids(span)
          trace_id = span.get("traceId") or ""
          span_id = span.get("spanId") or ""
          if not trace_id or not span_id:
            raise ValueError("span missing traceId/spanId identity")
          envelopes.append(
              env.make_envelope(
                  source_product=source_product,
                  source_signal="span",
                  record=span,
                  resource_attributes=resource_attrs,
                  scope=scope,
                  source_position=position,
                  idempotency_key=env.span_idempotency_key(trace_id, span_id),
                  ingest_time=ingest_time,
              )
          )
        except Exception as exc:  # noqa: BLE001 - malformed -> dead letter
          envelopes.append(
              env.dead_letter_envelope(
                  source_product=source_product,
                  source_signal="span",
                  stage="otlp_decode",
                  reason=repr(exc),
                  raw_b64=raw_b64,
                  received_at=ingest_time,
                  source_position=position,
                  raw_otlp_request_hash=raw_hash,
              )
          )
  return envelopes


def _metric_record(
    metric: dict[str, Any],
    kind: str,
    body: dict[str, Any],
    point: dict[str, Any],
) -> dict[str, Any]:
  """Decoded record payload for one metric data point."""
  return {
      "metric_name": metric.get("name"),
      "description": metric.get("description"),
      "unit": metric.get("unit"),
      "point_kind": _METRIC_KINDS[kind],
      "aggregation_temporality": body.get("aggregationTemporality"),
      "is_monotonic": body.get("isMonotonic"),
      "point": point,
  }


def decode_metrics_request(
    request: dict[str, Any],
    *,
    source_product: str,
    raw_request: bytes,
    ingest_time: str,
) -> list[dict[str, Any]]:
  """Decode an ``ExportMetricsServiceRequest`` into metric envelopes.

  Handles all five OTLP point types (Sum/Gauge/Histogram/ExponentialHistogram/
  Summary). A metric with no recognized type yields a single dead-letter.
  ``raw_request`` is the original wire payload (see ``decode_logs_request``).
  """
  raw_hash = env.request_hash(raw_request)
  raw_b64 = base64.b64encode(raw_request).decode("ascii")
  envelopes: list[dict[str, Any]] = []
  for i, resource_metrics in enumerate(request.get("resourceMetrics", [])):
    resource_attrs = env.otlp_attrs_to_dict(
        resource_metrics.get("resource", {}).get("attributes")
    )
    for j, scope_metrics in enumerate(resource_metrics.get("scopeMetrics", [])):
      scope = scope_metrics.get("scope", {})
      for m, metric in enumerate(scope_metrics.get("metrics", [])):
        kind = _metric_kind(metric)
        if kind is None:
          envelopes.append(
              env.dead_letter_envelope(
                  source_product=source_product,
                  source_signal="metric",
                  stage="otlp_decode",
                  reason=(
                      f"metric {metric.get('name')!r} has no recognized point"
                      " type"
                  ),
                  raw_b64=raw_b64,
                  received_at=ingest_time,
                  source_position=env.SourcePosition(
                      raw_hash, i, j, metric_index=m
                  ),
                  raw_otlp_request_hash=raw_hash,
              )
          )
          continue
        body = metric.get(kind, {})
        temporality = body.get("aggregationTemporality")
        for p, point in enumerate(body.get("dataPoints", [])):
          position = env.SourcePosition(
              raw_hash, i, j, metric_index=m, data_point_index=p
          )
          try:
            key = env.metric_idempotency_key(
                point,
                metric.get("name", ""),
                temporality,
                resource_attrs,
                scope,
                position,
            )
            envelopes.append(
                env.make_envelope(
                    source_product=source_product,
                    source_signal="metric",
                    record=_metric_record(metric, kind, body, point),
                    resource_attributes=resource_attrs,
                    scope=scope,
                    source_position=position,
                    idempotency_key=key,
                    ingest_time=ingest_time,
                )
            )
          except Exception as exc:  # noqa: BLE001 - malformed -> dead letter
            envelopes.append(
                env.dead_letter_envelope(
                    source_product=source_product,
                    source_signal="metric",
                    stage="otlp_decode",
                    reason=repr(exc),
                    raw_b64=raw_b64,
                    received_at=ingest_time,
                    source_position=position,
                    raw_otlp_request_hash=raw_hash,
                )
            )
  return envelopes
