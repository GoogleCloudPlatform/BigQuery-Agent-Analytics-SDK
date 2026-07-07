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

"""Tests for the OTLP decode + envelope library (issue #316, PR 2)."""

from __future__ import annotations

import base64
import json

import pytest

from bigquery_agent_analytics_tracing.otlp import decode
from bigquery_agent_analytics_tracing.otlp import envelope as env

INGEST = "2026-06-29T00:00:00Z"


def _raw(request: dict) -> bytes:
  """Original wire payload for a request (OTLP/HTTP JSON bytes here)."""
  return json.dumps(request).encode("utf-8")


def _decode_logs(request, ingest_time=INGEST):
  return decode.decode_logs_request(
      request,
      source_product="claude_code",
      raw_request=_raw(request),
      ingest_time=ingest_time,
  )


def _decode_metrics(request, ingest_time=INGEST):
  return decode.decode_metrics_request(
      request,
      source_product="claude_code",
      raw_request=_raw(request),
      ingest_time=ingest_time,
  )


def _logs_request(log_records):
  return {
      "resourceLogs": [
          {
              "resource": {
                  "attributes": [
                      {
                          "key": "service.name",
                          "value": {"stringValue": "claude-code"},
                      }
                  ]
              },
              "scopeLogs": [
                  {
                      "scope": {
                          "name": "com.anthropic.claude_code",
                          "version": "1.0",
                      },
                      "logRecords": log_records,
                  }
              ],
          }
      ]
  }


_A_LOG = {
    "timeUnixNano": "1700000000000000000",
    "observedTimeUnixNano": "1700000000000000001",
    "severityNumber": 9,
    "severityText": "INFO",
    "body": {"stringValue": "hello"},
    "attributes": [
        {
            "key": "event.name",
            "value": {"stringValue": "claude_code.user_prompt"},
        }
    ],
}


def _metrics_request(kind, body, name="m"):
  return {
      "resourceMetrics": [
          {
              "resource": {
                  "attributes": [
                      {
                          "key": "service.name",
                          "value": {"stringValue": "claude-code"},
                      }
                  ]
              },
              "scopeMetrics": [
                  {
                      "scope": {
                          "name": "com.anthropic.claude_code",
                          "version": "1.0",
                      },
                      "metrics": [{"name": name, "unit": "1", kind: body}],
                  }
              ],
          }
      ]
  }


_METRIC_BODIES = {
    "sum": {
        "aggregationTemporality": 1,
        "isMonotonic": True,
        "dataPoints": [{"asDouble": 1.5, "timeUnixNano": "100"}],
    },
    "gauge": {"dataPoints": [{"asDouble": 2.0, "timeUnixNano": "100"}]},
    "histogram": {
        "aggregationTemporality": 2,
        "dataPoints": [
            {
                "count": "3",
                "sum": 6.0,
                "bucketCounts": ["1", "2"],
                "explicitBounds": [1.0],
                "timeUnixNano": "100",
            }
        ],
    },
    "exponentialHistogram": {
        "aggregationTemporality": 1,
        "dataPoints": [
            {
                "scale": 2,
                "zeroCount": "0",
                "count": "3",
                "sum": 6.0,
                "positive": {"offset": 0, "bucketCounts": ["1", "2"]},
                "timeUnixNano": "100",
            }
        ],
    },
    "summary": {
        "dataPoints": [
            {
                "count": "3",
                "sum": 6.0,
                "quantileValues": [{"quantile": 0.5, "value": 2.0}],
                "timeUnixNano": "100",
            }
        ]
    },
}


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_decode_one_log_record_into_one_envelope():
  req = _logs_request([_A_LOG])
  envs = _decode_logs(req)
  assert len(envs) == 1
  e = envs[0]
  assert e["envelope_version"] == "1"
  assert e["otel_schema_version"] == env.otel_schema.OTEL_SCHEMA_VERSION
  assert e["source"] == {"product": "claude_code", "signal": "log"}
  assert e["source_position"]["raw_otlp_request_hash"] == env.request_hash(
      _raw(req)
  )
  assert e["source_position"]["record_index"] == 0
  assert e["otlp"]["resource_attributes"] == {"service.name": "claude-code"}
  assert len(e["idempotency_key"]) == 64  # sha256 hex
  assert e["parse_error"] is None


# --------------------------------------------------------------------------
# Metrics — all five point types
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,expected_point_kind",
    [
        ("sum", "sum"),
        ("gauge", "gauge"),
        ("histogram", "histogram"),
        ("exponentialHistogram", "exponential_histogram"),
        ("summary", "summary"),
    ],
)
def test_decode_each_metric_point_type(kind, expected_point_kind):
  envs = _decode_metrics(_metrics_request(kind, _METRIC_BODIES[kind]))
  assert len(envs) == 1
  e = envs[0]
  assert e["source"]["signal"] == "metric"
  assert e["record"]["point_kind"] == expected_point_kind
  assert e["source_position"]["metric_index"] == 0
  assert e["source_position"]["data_point_index"] == 0
  assert len(e["idempotency_key"]) == 64


def test_sum_preserves_temporality_and_monotonicity_in_record():
  e = _decode_metrics(_metrics_request("sum", _METRIC_BODIES["sum"]))[0]
  assert e["record"]["aggregation_temporality"] == 1
  assert e["record"]["is_monotonic"] is True


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_idempotency_key_is_stable_across_decodes():
  req = _logs_request([_A_LOG])
  assert (
      _decode_logs(req)[0]["idempotency_key"]
      == _decode_logs(req)[0]["idempotency_key"]
  )


def test_idempotency_key_ignores_ingest_time_so_replay_collapses():
  req = _logs_request([_A_LOG])
  k1 = _decode_logs(req, ingest_time="2026-06-29T00:00:00Z")[0][
      "idempotency_key"
  ]
  k2 = _decode_logs(req, ingest_time="2026-06-29T11:11:11Z")[0][
      "idempotency_key"
  ]
  assert k1 == k2


def test_two_identical_records_in_one_request_get_distinct_keys():
  # Same content twice -> source_position.record_index distinguishes them, so
  # the dedup view will not collapse two legitimate records.
  envs = _decode_logs(_logs_request([dict(_A_LOG), dict(_A_LOG)]))
  assert len(envs) == 2
  assert envs[0]["idempotency_key"] != envs[1]["idempotency_key"]
  assert envs[0]["source_position"]["record_index"] == 0
  assert envs[1]["source_position"]["record_index"] == 1


def test_distinct_data_points_get_distinct_keys():
  body = {
      "dataPoints": [
          {"asDouble": 1.0, "timeUnixNano": "100"},
          {"asDouble": 1.0, "timeUnixNano": "100"},  # identical value
      ]
  }
  envs = _decode_metrics(_metrics_request("gauge", body))
  assert len(envs) == 2
  assert envs[0]["idempotency_key"] != envs[1]["idempotency_key"]


# --------------------------------------------------------------------------
# Malformed -> dead letter (replayable + keyed, per #316 contract)
# --------------------------------------------------------------------------


def test_metric_with_unrecognized_type_becomes_dead_letter():
  envs = _decode_metrics(
      _metrics_request("notARealType", {"dataPoints": []}, name="bad")
  )
  assert len(envs) == 1
  e = envs[0]
  assert e["record"] is None
  assert e["delivery"]["dlq"] is True
  assert e["parse_error"]["stage"] == "otlp_decode"
  assert e["source_position"]["metric_index"] == 0


def test_dead_letter_raw_b64_is_the_replayable_original_request():
  req = _metrics_request("notARealType", {"dataPoints": []}, name="bad")
  e = _decode_metrics(req)[0]
  # raw_b64 round-trips to the ORIGINAL request bytes (republishable to
  # /v1/metrics), not a re-serialized subrecord.
  assert base64.b64decode(e["raw_preservation"]["raw_b64"]) == _raw(req)
  assert base64.b64decode(e["parse_error"]["raw_b64"]) == _raw(req)
  # and the request hash is reproducible from that payload.
  assert e["source_position"]["raw_otlp_request_hash"] == env.request_hash(
      _raw(req)
  )


def test_dead_letter_is_keyed_from_source_position():
  req = _metrics_request("notARealType", {"dataPoints": []}, name="bad")
  e = _decode_metrics(req)[0]
  raw_hash = env.request_hash(_raw(req))
  expected = env.dead_letter_key(
      "otlp_decode", env.SourcePosition(raw_hash, 0, 0, metric_index=0)
  )
  assert e["idempotency_key"] == expected
  assert e["idempotency_key"] is not None


def test_dead_letter_key_falls_back_to_request_hash_and_stage():
  # Whole-request failure (no source_position): key from request hash + stage.
  k_auth = env.dead_letter_key("auth", None, "req-hash")
  k_decode = env.dead_letter_key("otlp_decode", None, "req-hash")
  assert len(k_auth) == 64
  assert k_auth != k_decode  # stage participates in the key


def test_one_bad_metric_does_not_drop_the_good_one():
  req = {
      "resourceMetrics": [
          {
              "resource": {"attributes": []},
              "scopeMetrics": [
                  {
                      "scope": {},
                      "metrics": [
                          {"name": "bad"},  # no recognized point type
                          {"name": "good", "gauge": _METRIC_BODIES["gauge"]},
                      ],
                  }
              ],
          }
      ]
  }
  envs = _decode_metrics(req)
  assert len(envs) == 2
  assert envs[0]["parse_error"]["stage"] == "otlp_decode"
  assert envs[1]["record"]["metric_name"] == "good"


# --------------------------------------------------------------------------
# Helpers: attributes, raw preservation, keys, request hash
# --------------------------------------------------------------------------


def test_otlp_attrs_to_dict_extracts_typed_anyvalues():
  attrs = [
      {"key": "s", "value": {"stringValue": "x"}},
      {"key": "n", "value": {"intValue": "42"}},
      {"key": "b", "value": {"boolValue": True}},
  ]
  assert env.otlp_attrs_to_dict(attrs) == {"s": "x", "n": 42, "b": True}


def test_raw_preservation_modes():
  assert env.raw_preservation() == {"policy": "decoded_only", "raw_b64": None}
  rp = env.raw_preservation("decoded_plus_raw", b"abc")
  assert rp["policy"] == "decoded_plus_raw"
  assert base64.b64decode(rp["raw_b64"]) == b"abc"
  with pytest.raises(ValueError):
    env.raw_preservation("bogus")


def test_span_idempotency_key_is_natural():
  assert env.span_idempotency_key("trace1", "span1") == "trace1span1"


def test_request_hash_is_deterministic_sha256():
  assert env.request_hash(b"abc") == env.request_hash(b"abc")
  assert len(env.request_hash(b"abc")) == 64


# --------------------------------------------------------------------------
# Traces (#324 PR4 — span landing)
# --------------------------------------------------------------------------


def _decode_traces(request, ingest_time=INGEST):
  return decode.decode_traces_request(
      request,
      source_product="claude_code",
      raw_request=_raw(request),
      ingest_time=ingest_time,
  )


def _traces_request(spans):
  return {
      "resourceSpans": [
          {
              "resource": {
                  "attributes": [
                      {
                          "key": "service.name",
                          "value": {"stringValue": "claude-code"},
                      }
                  ]
              },
              "scopeSpans": [
                  {"scope": {"name": "s", "version": "1"}, "spans": spans}
              ],
          }
      ]
  }


def _span(**overrides):
  span = {
      "traceId": "0123456789abcdef0123456789abcdef",
      "spanId": "0123456789abcdef",
      "name": "tool_use",
      "kind": "SPAN_KIND_INTERNAL",
      "startTimeUnixNano": "1000000000",
      "endTimeUnixNano": "2000000000",
      "status": {"code": "STATUS_CODE_OK"},
      "attributes": [{"key": "tool.name", "value": {"stringValue": "Bash"}}],
  }
  span.update(overrides)
  return span


def test_decode_one_span_into_one_envelope():
  envelopes = _decode_traces(_traces_request([_span()]))
  assert len(envelopes) == 1
  e = envelopes[0]
  assert e["source"]["signal"] == "span"
  assert e["parse_error"] is None
  assert e["record"]["name"] == "tool_use"
  assert e["otlp"]["resource_attributes"]["service.name"] == "claude-code"
  # Spans have a natural idempotency key: trace_id + span_id.
  assert e["idempotency_key"] == env.span_idempotency_key(
      "0123456789abcdef0123456789abcdef", "0123456789abcdef"
  )


def test_span_key_is_replay_invariant():
  request = _traces_request([_span()])
  a = _decode_traces(request, ingest_time="2026-06-29T00:00:00Z")[0]
  b = _decode_traces(request, ingest_time="2026-06-30T09:09:09Z")[0]
  assert a["idempotency_key"] == b["idempotency_key"]


def test_span_missing_identity_becomes_dead_letter():
  envelopes = _decode_traces(_traces_request([_span(spanId="")]))
  assert len(envelopes) == 1
  assert envelopes[0]["delivery"]["dlq"] is True
  assert envelopes[0]["parse_error"]["stage"] == "otlp_decode"


def test_two_spans_get_distinct_keys_and_positions():
  spans = [_span(), _span(spanId="fedcba9876543210")]
  envelopes = _decode_traces(_traces_request(spans))
  assert len(envelopes) == 2
  assert envelopes[0]["idempotency_key"] != envelopes[1]["idempotency_key"]
  assert (
      envelopes[0]["source_position"]["record_index"]
      != envelopes[1]["source_position"]["record_index"]
  )


def test_protobuf_base64_ids_normalize_to_canonical_hex():
  # MessageToDict base64-encodes bytes fields, while OTLP/JSON carries hex:
  # without normalization the same span gets two different idempotency keys
  # and the trace_id cluster column mixes encodings across transports.
  import base64

  trace_hex = "0123456789abcdef0123456789abcdef"
  span_hex = "0123456789abcdef"
  parent_hex = "fedcba9876543210"
  b64 = lambda h: base64.b64encode(bytes.fromhex(h)).decode("ascii")
  span = _span(
      traceId=b64(trace_hex),
      spanId=b64(span_hex),
      parentSpanId=b64(parent_hex),
      links=[
          {
              "traceId": b64(trace_hex),
              "spanId": b64(parent_hex),
          }
      ],
  )
  [envelope] = _decode_traces(_traces_request([span]))
  record = envelope["record"]
  assert record["traceId"] == trace_hex
  assert record["spanId"] == span_hex
  assert record["parentSpanId"] == parent_hex
  assert record["links"][0]["traceId"] == trace_hex
  assert record["links"][0]["spanId"] == parent_hex
  assert envelope["idempotency_key"] == trace_hex + span_hex


def test_hex_ids_are_lowercased_for_stable_keys():
  span = _span(
      traceId="0123456789ABCDEF0123456789ABCDEF", spanId="0123456789ABCDEF"
  )
  [envelope] = _decode_traces(_traces_request([span]))
  assert envelope["record"]["traceId"] == "0123456789abcdef0123456789abcdef"
  assert envelope["idempotency_key"] == (
      "0123456789abcdef0123456789abcdef" + "0123456789abcdef"
  )
