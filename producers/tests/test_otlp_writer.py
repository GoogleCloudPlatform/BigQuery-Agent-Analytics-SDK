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

"""Tests for the OTLP BigQuery writer + projection SQL (issue #316, PR 4)."""

from __future__ import annotations

import base64
import io
import json

import pytest

from bigquery_agent_analytics_tracing.otlp import consumer
from bigquery_agent_analytics_tracing.otlp import decode
from bigquery_agent_analytics_tracing.otlp import projection
from bigquery_agent_analytics_tracing.otlp import sql
from bigquery_agent_analytics_tracing.otlp import writer

INGEST = "2026-06-30T00:00:00Z"


class FakeWriter:

  def __init__(self):
    self.rows: list[tuple[str, dict]] = []

  def append(self, table: str, row: dict) -> None:
    self.rows.append((table, row))


def _raw(request: dict) -> bytes:
  return json.dumps(request).encode("utf-8")


def _log_envelope():
  request = {
      "resourceLogs": [
          {
              "resource": {
                  "attributes": [
                      {
                          "key": "service.name",
                          "value": {"stringValue": "claude-code"},
                      },
                      {"key": "user.id", "value": {"stringValue": "u-1"}},
                  ]
              },
              "scopeLogs": [
                  {
                      "scope": {"name": "scope", "version": "1.0"},
                      "logRecords": [
                          {
                              "timeUnixNano": "1700000000000000000",
                              "severityNumber": 9,
                              "body": {"stringValue": "hello"},
                              "eventName": "claude_code.user_prompt",
                              "traceId": "t1",
                              "spanId": "s1",
                              "attributes": [
                                  {
                                      "key": "session.id",
                                      "value": {"stringValue": "sess-1"},
                                  }
                              ],
                          }
                      ],
                  }
              ],
          }
      ]
  }
  return decode.decode_logs_request(
      request,
      source_product="claude_code",
      raw_request=_raw(request),
      ingest_time=INGEST,
  )[0]


_METRIC_BODIES = {
    "sum": {
        "aggregationTemporality": 1,
        "isMonotonic": True,
        "dataPoints": [{"asDouble": 1.5, "timeUnixNano": "100"}],
    },
    "gauge": {"dataPoints": [{"asInt": "7", "timeUnixNano": "100"}]},
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
                "count": "3",
                "sum": 6.0,
                "scale": 2,
                "zeroCount": "0",
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


def _metric_envelope(otlp_kind):
  request = {
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
                      "scope": {"name": "scope"},
                      "metrics": [
                          {
                              "name": "m",
                              "unit": "1",
                              otlp_kind: _METRIC_BODIES[otlp_kind],
                          }
                      ],
                  }
              ],
          }
      ]
  }
  return decode.decode_metrics_request(
      request,
      source_product="claude_code",
      raw_request=_raw(request),
      ingest_time=INGEST,
  )[0]


def _dead_letter_envelope():
  request = {
      "resourceMetrics": [
          {
              "resource": {"attributes": []},
              "scopeMetrics": [{"scope": {}, "metrics": [{"name": "bad"}]}],
          }
      ]
  }
  return decode.decode_metrics_request(
      request,
      source_product="claude_code",
      raw_request=_raw(request),
      ingest_time=INGEST,
  )[0]


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_log_row_maps_fields_and_converts_timestamp():
  row = writer.log_row(_log_envelope())
  assert row["timestamp"] == "2023-11-14T22:13:20Z"  # nanos -> RFC3339
  assert row["service_name"] == "claude-code"
  assert row["severity_number"] == 9
  assert row["trace_id"] == "t1"
  assert row["event_name"] == "claude_code.user_prompt"
  assert json.loads(row["body"]) == {"stringValue": "hello"}
  assert json.loads(row["log_attributes"]) == {"session.id": "sess-1"}


# --------------------------------------------------------------------------
# Contract fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "envelope_fn", [_log_envelope, lambda: _metric_envelope("sum")]
)
def test_rows_preserve_required_contract_fields(envelope_fn):
  envelope = envelope_fn()
  row = writer.envelope_to_row(envelope)
  for field in (
      "idempotency_key",
      "source_position",
      "otel_schema_version",
      "ingest_time",
      "source_product",
      "source_signal",
  ):
    assert row[field] is not None, field
  assert row["idempotency_key"] == envelope["idempotency_key"]
  assert row["source_position"]["raw_otlp_request_hash"]  # threaded through


# --------------------------------------------------------------------------
# Metrics — all five types route + carry the right payload
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "otlp_kind,table",
    [
        ("sum", "otel_metric_sum"),
        ("gauge", "otel_metric_gauge"),
        ("histogram", "otel_metric_histogram"),
        ("exponentialHistogram", "otel_metric_exponential_histogram"),
        ("summary", "otel_metric_summary"),
    ],
)
def test_metric_routes_to_its_table(otlp_kind, table):
  assert writer.target_table(_metric_envelope(otlp_kind)) == table


def test_sum_row_has_value_temporality_monotonic():
  row = writer.metric_row(_metric_envelope("sum"))
  assert row["value"] == 1.5
  assert row["aggregation_temporality"] == 1
  assert row["is_monotonic"] is True


def test_gauge_int_value_is_coerced():
  assert writer.metric_row(_metric_envelope("gauge"))["value"] == 7


def test_histogram_row_has_struct_of_array_buckets():
  row = writer.metric_row(_metric_envelope("histogram"))
  assert row["count"] == 3
  assert row["bucket_counts"] == [1, 2]
  assert row["explicit_bounds"] == [1.0]


def test_exponential_histogram_row_has_pos_buckets():
  row = writer.metric_row(_metric_envelope("exponentialHistogram"))
  assert row["scale"] == 2
  assert row["positive"] == {"offset": 0, "bucket_counts": [1, 2]}


def test_summary_row_has_quantiles_and_no_exemplars():
  row = writer.metric_row(_metric_envelope("summary"))
  assert row["quantile_values"] == [{"quantile": 0.5, "value": 2.0}]
  assert "exemplars" not in row


# --------------------------------------------------------------------------
# Dead-letter landing
# --------------------------------------------------------------------------


def test_dead_letter_lands_in_dead_letter_table():
  envelope = _dead_letter_envelope()
  assert writer.target_table(envelope) == "otlp_dead_letter"
  row = writer.dead_letter_row(envelope)
  assert row["stage"] == "otlp_decode"
  assert row["raw_b64"] is not None
  assert row["idempotency_key"] is not None
  assert row["source_position"]["metric_index"] == 0


# --------------------------------------------------------------------------
# append_envelope / handle_message / span gating
# --------------------------------------------------------------------------


def test_append_envelope_routes_through_writer():
  w = FakeWriter()
  table = writer.append_envelope(_metric_envelope("gauge"), w)
  assert table == "otel_metric_gauge"
  assert w.rows[0][0] == "otel_metric_gauge"


def test_handle_message_decodes_and_appends():
  w = FakeWriter()
  data = json.dumps(_log_envelope()).encode("utf-8")
  assert writer.handle_message(data, w) == "otel_logs"
  assert len(w.rows) == 1


def test_span_envelope_is_dropped_unless_enabled():
  span_env = dict(_log_envelope())
  span_env["source"] = {"product": "claude_code", "signal": "span"}
  w = FakeWriter()
  assert writer.append_envelope(span_env, w) == ""
  assert w.rows == []


# --------------------------------------------------------------------------
# Replay / idempotency
# --------------------------------------------------------------------------


def test_replay_produces_identical_idempotency_key_in_row():
  # Same envelope content -> identical idempotency_key in the row, so the dedup
  # view collapses the replay.
  a = writer.envelope_to_row(_log_envelope())
  b = writer.envelope_to_row(_log_envelope())
  assert a["idempotency_key"] == b["idempotency_key"]


# --------------------------------------------------------------------------
# Projection / dedup SQL
# --------------------------------------------------------------------------


def test_merge_sql_upserts_into_agent_events_otlp_on_idempotency_key():
  s = sql.agent_events_otlp_merge_sql(dataset="ds")
  assert "MERGE `ds.agent_events_otlp` T" in s
  assert "FROM `ds.otel_logs_dedup`" in s
  assert "ON T.idempotency_key = S.idempotency_key" in s
  assert "WHEN MATCHED THEN UPDATE SET" in s  # upsert
  assert "WHEN NOT MATCHED THEN INSERT" in s
  for col in (
      "source_product",
      "source_signal",
      "source_event_name",
      "crosswalk_version",
  ):
    assert col in s


def test_merge_columns_are_within_projection_parity_contract():
  # Every column the MERGE writes must be a real agent_events_otlp column.
  import dataclasses

  @dataclasses.dataclass
  class _F:
    name: str
    field_type: str = "STRING"
    mode: str = "NULLABLE"
    fields: tuple = ()

  class _BQ:

    def SchemaField(self, name, field_type, mode="NULLABLE", fields=()):  # noqa: N802
      return _F(name, field_type, mode, tuple(fields))

  projection_cols = {
      f.name for f in projection.agent_events_otlp_columns(_BQ())
  }
  merge_cols = {c for c, _ in sql._LOG_CROSSWALK}
  assert merge_cols <= projection_cols


def test_dedup_view_is_newest_write_wins():
  s = projection.dedup_view_sql("otel_logs", dataset="ds")
  assert "PARTITION BY idempotency_key ORDER BY ingest_time DESC" in s


def test_bqaa_metrics_view_unions_all_five_metric_tables():
  s = sql.bqaa_metrics_view_sql(dataset="ds")
  assert "CREATE OR REPLACE VIEW `ds.bqaa_metrics`" in s
  for table in (
      "otel_metric_sum",
      "otel_metric_gauge",
      "otel_metric_histogram",
      "otel_metric_exponential_histogram",
      "otel_metric_summary",
  ):
    assert f"`ds.{table}_dedup`" in s
  assert s.count("UNION ALL") == 4


# --------------------------------------------------------------------------
# Consumer callback (ack/nack) — no Pub/Sub needed
# --------------------------------------------------------------------------


class _FakeMessage:

  def __init__(self, data):
    self.data = data
    self.acked = False
    self.nacked = False

  def ack(self):
    self.acked = True

  def nack(self):
    self.nacked = True


def test_consumer_callback_acks_and_writes_on_success():
  w = FakeWriter()
  callback = consumer.make_callback(w)
  msg = _FakeMessage(json.dumps(_log_envelope()).encode("utf-8"))
  callback(msg)
  assert msg.acked is True
  assert w.rows[0][0] == "otel_logs"


def test_consumer_callback_nacks_on_failure():
  w = FakeWriter()
  callback = consumer.make_callback(w)
  msg = _FakeMessage(b"not json")
  callback(msg)
  assert msg.nacked is True
  assert w.rows == []


# --------------------------------------------------------------------------
# Push consumer WSGI app (the Cloud Run deploy path) — no Pub/Sub needed
# --------------------------------------------------------------------------


def _push_wsgi(app, body, path="/"):
  environ = {
      "PATH_INFO": path,
      "CONTENT_LENGTH": str(len(body)),
      "wsgi.input": io.BytesIO(body),
  }
  captured: dict = {}

  def start_response(status, headers):
    captured["status"] = status

  out = b"".join(app(environ, start_response))
  return captured["status"], out


def _push_body(envelope):
  data = base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")
  return json.dumps({"message": {"data": data}}).encode("utf-8")


def test_push_app_writes_and_acks_with_204():
  w = FakeWriter()
  app = consumer.make_push_app(w)
  status, _ = _push_wsgi(app, _push_body(_log_envelope()))
  assert status.startswith("204")
  assert w.rows[0][0] == "otel_logs"


def test_push_app_returns_500_on_bad_message():
  w = FakeWriter()
  app = consumer.make_push_app(w)
  status, _ = _push_wsgi(app, b"garbage")
  assert status.startswith("500")  # -> Pub/Sub retry -> DLQ
  assert w.rows == []


def test_push_app_healthz_serves_port():
  # A Cloud Run service must answer on its port; /healthz proves the app binds.
  status, out = _push_wsgi(
      consumer.make_push_app(FakeWriter()), b"", "/healthz"
  )
  assert status.startswith("200")
  assert out == b"ok"
