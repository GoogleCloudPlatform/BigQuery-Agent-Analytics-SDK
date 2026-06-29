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

"""Pub/Sub envelope v1 construction + per-signal idempotency (issue #316, PR 2).

Pure functions over the **decoded** OTLP JSON structure (camelCase, as produced
by ``protobuf -> dict`` at the receiver edge) — no network, no BigQuery, no
protobuf dependency — so the whole library is unit-testable with synthetic
fixtures. See ``docs/otlp_receiver_design.md`` §4 for the envelope and key
contract.

Idempotency keys are ``SHA256`` hex over a canonicalized payload. They include
``source_position`` (the stable, replay-invariant position of a record within
its OTLP request) so two legitimately identical records/points in the same
request do not collapse, while a retried/replayed request reproduces the key
exactly. ``ingest_time`` is deliberately **not** part of any key.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from bigquery_agent_analytics_tracing.otlp import schema as otel_schema

ENVELOPE_VERSION = "1"

# Point payload keys that are NOT part of a metric point's "value" (they are
# hashed separately, or are not identity-bearing).
_NON_VALUE_POINT_KEYS = frozenset(
    {"attributes", "startTimeUnixNano", "timeUnixNano"}
)


def canonical_json(value: Any) -> str:
  """Stable JSON encoding for hashing (sorted keys, no insignificant space)."""
  return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def request_hash(raw: bytes) -> str:
  """SHA256 hex of the original OTLP request bytes (feeds ``source_position``)."""
  return hashlib.sha256(raw).hexdigest()


def _anyvalue(value: Any) -> Any:
  """Extract a Python value from an OTLP ``AnyValue`` JSON object."""
  if not isinstance(value, dict):
    return value
  if "stringValue" in value:
    return value["stringValue"]
  if "boolValue" in value:
    return value["boolValue"]
  if "intValue" in value:
    return int(value["intValue"])  # OTLP JSON encodes int64 as a string
  if "doubleValue" in value:
    return value["doubleValue"]
  if "bytesValue" in value:
    return value["bytesValue"]
  if "arrayValue" in value:
    return [_anyvalue(v) for v in value["arrayValue"].get("values", [])]
  if "kvlistValue" in value:
    return otlp_attrs_to_dict(value["kvlistValue"].get("values", []))
  return None


def otlp_attrs_to_dict(attributes: Any) -> dict[str, Any]:
  """OTLP ``KeyValue`` list -> ``{key: value}`` dict."""
  result: dict[str, Any] = {}
  for kv in attributes or []:
    result[kv["key"]] = _anyvalue(kv.get("value", {}))
  return result


@dataclass(frozen=True)
class SourcePosition:
  """Stable, replay-invariant position of a record within its OTLP request."""

  raw_otlp_request_hash: str
  resource_index: int
  scope_index: int
  record_index: int | None = None  # log record index (logs)
  metric_index: int | None = None  # metric index (metrics)
  data_point_index: int | None = None  # data point index (metrics)

  def as_dict(self) -> dict[str, Any]:
    return {
        "raw_otlp_request_hash": self.raw_otlp_request_hash,
        "resource_index": self.resource_index,
        "scope_index": self.scope_index,
        "record_index": self.record_index,
        "metric_index": self.metric_index,
        "data_point_index": self.data_point_index,
    }

  def canonical(self) -> str:
    return canonical_json(self.as_dict())


def _sha(parts: list[str]) -> str:
  return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _scope_id(scope: dict[str, Any] | None) -> str:
  scope = scope or {}
  return f"{scope.get('name', '')}@{scope.get('version', '')}"


def log_idempotency_key(
    record: dict[str, Any],
    resource_attributes: dict[str, Any],
    scope: dict[str, Any] | None,
    source_position: SourcePosition,
) -> str:
  """SHA256 key for a log record (design §4.1)."""
  return _sha(
      [
          canonical_json(resource_attributes),
          _scope_id(scope),
          str(
              record.get("observedTimeUnixNano")
              or record.get("timeUnixNano")
              or ""
          ),
          str(record.get("severityNumber", "")),
          canonical_json(record.get("body")),
          canonical_json(otlp_attrs_to_dict(record.get("attributes"))),
          source_position.canonical(),
      ]
  )


def metric_idempotency_key(
    point: dict[str, Any],
    metric_name: str,
    temporality: Any,
    resource_attributes: dict[str, Any],
    scope: dict[str, Any] | None,
    source_position: SourcePosition,
) -> str:
  """SHA256 key for a metric data point of any of the five types (design §4.1).

  The "value" component is the whole point payload minus the separately-hashed
  attributes/timestamps, so it works uniformly for Sum/Gauge (``asDouble`` /
  ``asInt``), Histogram/ExponentialHistogram (buckets), and Summary (quantiles).
  """
  value = {k: v for k, v in point.items() if k not in _NON_VALUE_POINT_KEYS}
  return _sha(
      [
          canonical_json(resource_attributes),
          _scope_id(scope),
          metric_name,
          canonical_json(otlp_attrs_to_dict(point.get("attributes"))),
          str(point.get("startTimeUnixNano", "")),
          str(point.get("timeUnixNano", "")),
          str(temporality or ""),
          canonical_json(value),
          source_position.canonical(),
      ]
  )


def span_idempotency_key(trace_id: str, span_id: str) -> str:
  """Spans have a natural key — no hash needed."""
  return f"{trace_id}{span_id}"


def raw_preservation(
    policy: str = "decoded_only", raw: bytes | None = None
) -> dict[str, Any]:
  """Build the ``raw_preservation`` block.

  ``decoded_only`` keeps no raw bytes; ``decoded_plus_raw`` base64-encodes the
  original record bytes for replay/debug.
  """
  if policy == "decoded_only":
    return {"policy": "decoded_only", "raw_b64": None}
  if policy == "decoded_plus_raw":
    encoded = base64.b64encode(raw).decode("ascii") if raw is not None else None
    return {"policy": "decoded_plus_raw", "raw_b64": encoded}
  raise ValueError(f"unknown raw-preservation policy {policy!r}")


def make_envelope(
    *,
    source_product: str,
    source_signal: str,
    record: dict[str, Any],
    resource_attributes: dict[str, Any],
    scope: dict[str, Any] | None,
    source_position: SourcePosition,
    idempotency_key: str,
    ingest_time: str,
    raw_preservation_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Assemble a successful envelope v1 message for one record/point."""
  return {
      "envelope_version": ENVELOPE_VERSION,
      "otel_schema_version": otel_schema.OTEL_SCHEMA_VERSION,
      "idempotency_key": idempotency_key,
      "ingest_time": ingest_time,
      "source": {"product": source_product, "signal": source_signal},
      "source_position": source_position.as_dict(),
      "otlp": {
          "resource_attributes": resource_attributes,
          "scope": scope or {},
      },
      "record": record,
      "raw_preservation": (
          raw_preservation_block
          if raw_preservation_block is not None
          else {"policy": "decoded_only", "raw_b64": None}
      ),
      "parse_error": None,
      "delivery": {"attempt": 1, "dlq": False},
  }


def dead_letter_key(
    stage: str,
    source_position: SourcePosition | None = None,
    raw_otlp_request_hash: str | None = None,
) -> str:
  """Deterministic key for a dead-letter / replay row.

  Decode failed, so there is no canonical record *content* to hash. Key from
  ``source_position`` (which carries the original request hash + indices) where
  decode got that far; otherwise from ``raw_otlp_request_hash + stage`` for
  whole-request failures (auth, undecodable request). This lets DLQ rows be
  deduped and lets a replay worker recognize a re-failed message.
  """
  if source_position is not None:
    return _sha([source_position.canonical(), stage])
  return _sha([raw_otlp_request_hash or "", stage])


def dead_letter_envelope(
    *,
    source_product: str,
    source_signal: str,
    stage: str,
    reason: str,
    raw_b64: str | None,
    received_at: str,
    source_position: SourcePosition | None = None,
    raw_otlp_request_hash: str | None = None,
) -> dict[str, Any]:
  """Envelope for a record that could not be decoded/written.

  Routed to ``otlp_dead_letter`` + the DLQ topic, never to the analytics tables.
  ``raw_b64`` must be the **replayable** original OTLP request payload (so a
  replay worker can republish it to ``/v1/logs`` / ``/v1/metrics``), not a
  re-serialized subrecord. ``source_position`` is carried where decode got far
  enough; a whole-request failure leaves it ``None`` and keys from
  ``raw_otlp_request_hash + stage``.
  """
  return {
      "envelope_version": ENVELOPE_VERSION,
      "idempotency_key": dead_letter_key(
          stage, source_position, raw_otlp_request_hash
      ),
      "ingest_time": received_at,
      "source": {"product": source_product, "signal": source_signal},
      "source_position": (
          source_position.as_dict() if source_position is not None else None
      ),
      "record": None,
      "raw_preservation": {"policy": "raw_only", "raw_b64": raw_b64},
      "parse_error": {"stage": stage, "reason": reason, "raw_b64": raw_b64},
      "delivery": {"attempt": 1, "dlq": True},
  }
