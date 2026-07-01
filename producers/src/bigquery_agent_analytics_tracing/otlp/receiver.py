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

"""OTLP receiver request handling (issue #316, PR 3).

The transport-agnostic core: authenticate, decode an OTLP export body, run it
through the PR-2 decode library, and publish the resulting envelopes to the main
ingest topic — success rows and dead letters alike (dead letters carry
``delivery.dlq=true``; the consumer routes those to ``otlp_dead_letter``). The
HTTP/WSGI entrypoint (``app.py``) and the real Pub/Sub publisher wrap this;
tests drive it directly with local fixtures and a fake publisher — no network.

The original request bytes (``body``) are threaded into the decode layer so
dead-letter envelopes carry a replayable payload and a reproducible request
hash (the PR-2 contract). Whole-request failures (bad auth aside) are
dead-lettered with ``raw_otlp_request_hash`` always set, so the dead-letter key
never collapses (see ``envelope.dead_letter_key``).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hmac
import json
from typing import Any, Protocol

from bigquery_agent_analytics_tracing.otlp import decode
from bigquery_agent_analytics_tracing.otlp import envelope as env

# OTLP HTTP path -> signal name.
SIGNAL_PATHS = {
    "/v1/logs": "log",
    "/v1/metrics": "metric",
    "/v1/traces": "trace",
}


class Publisher(Protocol):
  """Minimal publish interface (real impl wraps google-cloud-pubsub)."""

  def publish(self, topic: str, message: bytes) -> None:
    ...


class DecodeError(Exception):
  """OTLP body could not be decoded into a request dict."""


@dataclass
class ReceiverConfig:
  expected_token: str
  main_topic: str
  enable_traces: bool = False  # signal-tier gate (#324)
  source_product: str = "claude_code"


@dataclass
class ReceiverResult:
  status: int
  published: int = 0
  dead_lettered: int = 0
  message: str = ""


def authenticate(auth_header: str | None, expected_token: str) -> bool:
  """Constant-time bearer-token check."""
  prefix = "Bearer "
  if not auth_header or not auth_header.startswith(prefix):
    return False
  return hmac.compare_digest(auth_header[len(prefix) :], expected_token)


def decode_body(
    signal: str, content_type: str | None, body: bytes
) -> dict[str, Any]:
  """Decode an OTLP export body into the decoded request dict.

  OTLP/HTTP protobuf (the recommended enterprise default) via
  ``opentelemetry-proto`` (lazy-imported), or OTLP/HTTP JSON via ``json``.
  Raises ``DecodeError`` on failure.
  """
  content = (content_type or "").split(";")[0].strip().lower()
  if content in ("application/json", ""):
    try:
      return json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
      raise DecodeError(f"invalid OTLP/JSON body: {exc!r}") from exc
  if content == "application/x-protobuf":
    return _decode_protobuf(signal, body)
  raise DecodeError(f"unsupported content-type {content_type!r}")


def _decode_protobuf(signal: str, body: bytes) -> dict[str, Any]:
  try:
    from google.protobuf.json_format import MessageToDict

    if signal == "log":
      from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest as Request
    elif signal == "metric":
      from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest as Request
    else:
      raise DecodeError(f"protobuf decode unsupported for signal {signal!r}")
  except ImportError as exc:
    raise DecodeError(
        "OTLP/protobuf needs the 'receiver' extra (opentelemetry-proto)"
    ) from exc
  message = Request()
  try:
    message.ParseFromString(body)
  except Exception as exc:  # noqa: BLE001
    raise DecodeError(f"invalid OTLP/protobuf body: {exc!r}") from exc
  return MessageToDict(message, preserving_proto_field_name=False)


def _encode(envelope: dict[str, Any]) -> bytes:
  return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def route_envelopes(
    envelopes: list[dict[str, Any]],
    publisher: Publisher,
    config: ReceiverConfig,
) -> tuple[int, int]:
  """Publish every envelope to the **main** ingest topic.

  Dead letters travel the same path with ``delivery.dlq=true``; the consumer
  routes those to ``otlp_dead_letter`` — so parse/decode errors actually reach
  the table. (The subscription's Pub/Sub dead-letter policy is a separate
  transport safety net for consumer-side poison messages, not this path.)
  """
  published = dead = 0
  for envelope in envelopes:
    publisher.publish(config.main_topic, _encode(envelope))
    if envelope.get("delivery", {}).get("dlq"):
      dead += 1
    else:
      published += 1
  return published, dead


def handle_export(
    *,
    path: str,
    body: bytes,
    content_type: str | None,
    auth_header: str | None,
    ingest_time: str,
    config: ReceiverConfig,
    publisher: Publisher,
) -> ReceiverResult:
  """Authenticate, decode, and route one OTLP export request."""
  signal = SIGNAL_PATHS.get(path)
  if signal is None:
    return ReceiverResult(404, message=f"unknown path {path!r}")

  # Authenticate every known OTLP endpoint *before* any gate or decode, so auth
  # failures are always 401 and the trace gate state never leaks to
  # unauthenticated callers.
  if not authenticate(auth_header, config.expected_token):
    return ReceiverResult(401, message="unauthenticated")

  if signal == "trace":
    if not config.enable_traces:
      return ReceiverResult(404, message="traces not enabled")
    # Receiver wiring exists behind the flag, but span landing is deferred to
    # the trace work (design doc); accept-but-not-implemented.
    return ReceiverResult(501, message="trace landing not implemented")

  # Decode the body AND walk its structure under one guard: any malformed
  # request — bad bytes (DecodeError) or valid JSON with the wrong OTLP shape
  # (e.g. ``{"resourceLogs":[null]}`` raising inside the decode library) —
  # becomes a keyed, replayable whole-request dead letter, never a 500.
  try:
    request = decode_body(signal, content_type, body)
    if signal == "log":
      envelopes = decode.decode_logs_request(
          request,
          source_product=config.source_product,
          raw_request=body,
          ingest_time=ingest_time,
      )
    else:
      envelopes = decode.decode_metrics_request(
          request,
          source_product=config.source_product,
          raw_request=body,
          ingest_time=ingest_time,
      )
  except Exception as exc:  # noqa: BLE001 - malformed request -> dead letter
    dead_letter = env.dead_letter_envelope(
        source_product=config.source_product,
        source_signal=signal,
        stage="otlp_decode",
        reason=repr(exc),
        raw_b64=base64.b64encode(body).decode("ascii"),
        received_at=ingest_time,
        raw_otlp_request_hash=env.request_hash(body),
    )
    # Same main topic as success rows; the consumer routes delivery.dlq=true to
    # otlp_dead_letter.
    publisher.publish(config.main_topic, _encode(dead_letter))
    return ReceiverResult(
        400, dead_lettered=1, message="malformed otlp request"
    )

  published, dead = route_envelopes(envelopes, publisher, config)
  return ReceiverResult(200, published=published, dead_lettered=dead)
