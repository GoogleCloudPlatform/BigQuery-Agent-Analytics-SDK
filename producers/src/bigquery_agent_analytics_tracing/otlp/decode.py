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

Spans are intentionally out of scope for PR 2 (trace-gated; see the design doc).
"""

from __future__ import annotations

import base64
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
