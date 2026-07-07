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

"""Live e2e smoke test for the deployed OTLP receiver (issue #316, PR 5).

Proves the whole loop outside unit tests: send OTLP -> Cloud Run receiver ->
Pub/Sub -> consumer -> BigQuery, then verify native tables, dedup views,
agent_events_otlp, bqaa_metrics, and the malformed -> otlp_dead_letter path.

Skipped unless a deployment is configured::

    BQAA_OTLP_ENDPOINT=<url> BQAA_OTLP_TOKEN=<token> \
      BQAA_PROJECT=<proj> BQAA_DATASET=<dataset> \
      python -m pytest producers/tests/test_otlp_e2e.py -v
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid

import pytest

from bigquery_agent_analytics_tracing.otlp import sql as otel_sql
from bigquery_agent_analytics_tracing.otlp import verify as otel_verify

ENDPOINT = os.environ.get("BQAA_OTLP_ENDPOINT")
TOKEN = os.environ.get("BQAA_OTLP_TOKEN")
PROJECT = os.environ.get("BQAA_PROJECT")
DATASET = os.environ.get("BQAA_DATASET")

pytestmark = pytest.mark.skipif(
    not all([ENDPOINT, TOKEN, PROJECT, DATASET]),
    reason="live e2e needs BQAA_OTLP_ENDPOINT/TOKEN/PROJECT/DATASET",
)

requests = pytest.importorskip("requests")
bigquery = pytest.importorskip("google.cloud.bigquery")

_QUALIFIED = f"{PROJECT}.{DATASET}" if PROJECT and DATASET else "ds"


def _post(path: str, body, content_type="application/json"):
  data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body)
  return requests.post(
      ENDPOINT.rstrip("/") + path,
      data=data,
      headers={
          "Authorization": f"Bearer {TOKEN}",
          "Content-Type": content_type,
      },
      timeout=30,
  )


def _rows(query: str):
  return list(bigquery.Client(project=PROJECT).query(query).result())


def _wait_count(query: str, timeout=150) -> int:
  deadline = time.time() + timeout
  while time.time() < deadline:
    rows = _rows(query)
    if rows and rows[0][0]:
      return rows[0][0]
    time.sleep(5)
  return 0


def test_logs_and_metrics_land_and_project():
  run_id = uuid.uuid4().hex

  # Payload builders are shared with `bqaa-otel verify --smoke` (#324 PR3)
  # so the CLI smoke and this live e2e exercise the identical shapes.
  now_nanos = int(time.time() * 1e9)
  logs = otel_verify.synthetic_logs_payload(run_id, now_nanos)
  metric_name = f"bqaa_e2e_{run_id}"
  metrics = otel_verify.synthetic_gauge_payload(run_id, now_nanos)
  assert _post("/v1/logs", logs).status_code == 200
  assert _post("/v1/metrics", metrics).status_code == 200

  run = f"JSON_VALUE(log_attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
  assert (
      _wait_count(f"SELECT COUNT(*) FROM `{_QUALIFIED}.otel_logs` WHERE {run}")
      >= 1
  )
  # read-time dedup view returns it too
  assert (
      _rows(f"SELECT COUNT(*) FROM `{_QUALIFIED}.otel_logs_dedup` WHERE {run}")[
          0
      ][0]
      >= 1
  )
  # metric landed in its per-type table + the bqaa_metrics view
  assert (
      _wait_count(
          f"SELECT COUNT(*) FROM `{_QUALIFIED}.otel_metric_gauge`"
          f" WHERE metric_name = '{metric_name}'"
      )
      >= 1
  )
  assert (
      _rows(
          f"SELECT COUNT(*) FROM `{_QUALIFIED}.bqaa_metrics`"
          f" WHERE metric_name = '{metric_name}'"
      )[0][0]
      >= 1
  )

  # run the projection MERGE now (it is scheduled in prod) and verify the row.
  bigquery.Client(project=PROJECT).query(
      otel_sql.agent_events_otlp_merge_sql(_QUALIFIED)
  ).result()
  assert (
      _rows(
          f"SELECT event_type FROM `{_QUALIFIED}.agent_events_otlp`"
          f" WHERE JSON_VALUE(attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
      )[0][0]
      == "claude_code.user_prompt"
  )


def test_protobuf_logs_land_via_recommended_http_protobuf_path():
  # Exercise the documented enterprise default (OTLP/HTTP protobuf), so the
  # deployed image's opentelemetry-proto decode path is actually covered.
  logs_service_pb2 = pytest.importorskip(
      "opentelemetry.proto.collector.logs.v1.logs_service_pb2"
  )
  from opentelemetry.proto.common.v1 import common_pb2
  from opentelemetry.proto.logs.v1 import logs_pb2
  from opentelemetry.proto.resource.v1 import resource_pb2

  run_id = uuid.uuid4().hex

  def _kv(key, val):
    return common_pb2.KeyValue(
        key=key, value=common_pb2.AnyValue(string_value=val)
    )

  record = logs_pb2.LogRecord(
      time_unix_nano=int(time.time() * 1e9),
      body=common_pb2.AnyValue(string_value="e2e-proto"),
      event_name="claude_code.user_prompt",
      attributes=[_kv("bqaa.run_id", run_id)],
  )
  request = logs_service_pb2.ExportLogsServiceRequest(
      resource_logs=[
          logs_pb2.ResourceLogs(
              resource=resource_pb2.Resource(
                  attributes=[_kv("service.name", "claude-code")]
              ),
              scope_logs=[logs_pb2.ScopeLogs(log_records=[record])],
          )
      ]
  )
  resp = _post(
      "/v1/logs",
      request.SerializeToString(),
      content_type="application/x-protobuf",
  )
  assert resp.status_code == 200
  assert (
      _wait_count(
          f"SELECT COUNT(*) FROM `{_QUALIFIED}.otel_logs`"
          f" WHERE JSON_VALUE(log_attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
      )
      >= 1
  )


def test_malformed_request_lands_in_dead_letter_with_replayable_body():
  run_id = uuid.uuid4().hex
  body = f"not-otlp-{run_id}".encode("utf-8")
  assert _post("/v1/logs", body).status_code == 400

  deadline = time.time() + 150
  while time.time() < deadline:
    rows = _rows(
        f"SELECT raw_b64 FROM `{_QUALIFIED}.otlp_dead_letter`"
        " WHERE stage = 'otlp_decode'"
        " ORDER BY received_at DESC LIMIT 100"
    )
    if any(base64.b64decode(r[0]) == body for r in rows if r[0]):
      return  # replayable original payload present
    time.sleep(5)
  pytest.fail("malformed request did not reach otlp_dead_letter with raw_b64")


TRACES_ENABLED = os.environ.get("BQAA_OTLP_ENABLE_TRACES") == "1"


@pytest.mark.skipif(
    not TRACES_ENABLED,
    reason="traces e2e needs a deployment with BQAA_OTLP_ENABLE_TRACES=1",
)
def test_spans_land_in_otel_spans():
  # Traces signal tier (#324 PR4): synthetic span -> receiver -> otel_spans.
  run_id = uuid.uuid4().hex
  payload = otel_verify.synthetic_span_payload(run_id, int(time.time() * 1e9))
  assert _post("/v1/traces", payload).status_code == 200
  assert (
      _wait_count(
          f"SELECT COUNT(*) FROM `{_QUALIFIED}.otel_spans` WHERE"
          f" JSON_VALUE(span_attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
      )
      >= 1
  )
  # Read-time dedup view surfaces it too.
  assert (
      _rows(
          f"SELECT COUNT(*) FROM `{_QUALIFIED}.otel_spans_dedup` WHERE"
          f" JSON_VALUE(span_attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
      )[0][0]
      >= 1
  )
