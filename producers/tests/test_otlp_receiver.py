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

"""Tests for the OTLP receiver request handling (issue #316, PR 3)."""

from __future__ import annotations

import base64
import io
import json

import pytest

from bigquery_agent_analytics_tracing.otlp import app as otlp_app
from bigquery_agent_analytics_tracing.otlp import envelope as env
from bigquery_agent_analytics_tracing.otlp import receiver

INGEST = "2026-06-29T00:00:00Z"
TOKEN = "secret-token"


class FakePublisher:

  def __init__(self):
    self.calls: list[tuple[str, dict]] = []

  def publish(self, topic: str, message: bytes) -> None:
    self.calls.append((topic, json.loads(message)))

  def to(self, topic: str) -> list[dict]:
    return [msg for t, msg in self.calls if t == topic]


def _config(enable_traces=False):
  return receiver.ReceiverConfig(
      expected_token=TOKEN,
      main_topic="proj/main",
      enable_traces=enable_traces,
  )


def _logs_body():
  request = {
      "resourceLogs": [
          {
              "resource": {"attributes": []},
              "scopeLogs": [
                  {
                      "scope": {"name": "s", "version": "1"},
                      "logRecords": [
                          {"timeUnixNano": "100", "body": {"stringValue": "hi"}}
                      ],
                  }
              ],
          }
      ]
  }
  return json.dumps(request).encode("utf-8")


def _metrics_body(metrics):
  request = {
      "resourceMetrics": [
          {
              "resource": {"attributes": []},
              "scopeMetrics": [{"scope": {"name": "s"}, "metrics": metrics}],
          }
      ]
  }
  return json.dumps(request).encode("utf-8")


_GOOD_GAUGE = {
    "name": "g",
    "gauge": {"dataPoints": [{"asDouble": 1.0, "timeUnixNano": "100"}]},
}


def _call(
    path,
    body,
    *,
    token=TOKEN,
    content_type="application/json",
    config=None,
    publisher=None,
):
  config = config or _config()
  publisher = publisher or FakePublisher()
  auth = f"Bearer {token}" if token is not None else None
  result = receiver.handle_export(
      path=path,
      body=body,
      content_type=content_type,
      auth_header=auth,
      ingest_time=INGEST,
      config=config,
      publisher=publisher,
  )
  return result, publisher


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def test_authenticate_accepts_correct_bearer_only():
  assert receiver.authenticate(f"Bearer {TOKEN}", TOKEN) is True
  assert receiver.authenticate("Bearer wrong", TOKEN) is False
  assert receiver.authenticate(TOKEN, TOKEN) is False  # missing "Bearer "
  assert receiver.authenticate(None, TOKEN) is False


def test_unauthenticated_request_is_rejected_without_publishing():
  result, pub = _call("/v1/logs", _logs_body(), token="wrong")
  assert result.status == 401
  assert pub.calls == []


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def test_valid_logs_export_publishes_to_main_topic():
  result, pub = _call("/v1/logs", _logs_body())
  assert result.status == 200
  assert result.published == 1
  assert result.dead_lettered == 0
  assert len(pub.to("proj/main")) == 1
  assert pub.to("proj/main")[0]["source"]["signal"] == "log"


def test_valid_metrics_export_publishes_to_main_topic():
  result, pub = _call("/v1/metrics", _metrics_body([_GOOD_GAUGE]))
  assert result.status == 200
  assert result.published == 1
  assert pub.to("proj/main")[0]["record"]["point_kind"] == "gauge"


def test_unknown_path_is_404():
  result, pub = _call("/v1/nope", _logs_body())
  assert result.status == 404
  assert pub.calls == []


def test_bad_metric_and_good_one_both_go_to_main_topic():
  # Both success + dead-letter envelopes flow through the main topic; the
  # consumer routes delivery.dlq=true to otlp_dead_letter.
  body = _metrics_body([{"name": "bad"}, _GOOD_GAUGE])
  result, pub = _call("/v1/metrics", body)
  assert result.status == 200
  assert result.published == 1
  assert result.dead_lettered == 1
  main = pub.to("proj/main")
  assert len(main) == 2
  assert sum(1 for e in main if e.get("delivery", {}).get("dlq")) == 1
  assert pub.to("proj/dlq") == []  # receiver never publishes to a DLQ topic


# --------------------------------------------------------------------------
# Whole-request failure -> DLQ, keyed + replayable
# --------------------------------------------------------------------------


def test_undecodable_body_is_dead_lettered_with_keyed_replayable_payload():
  body = b"this is not OTLP json"
  result, pub = _call("/v1/logs", body)
  assert result.status == 400
  assert result.dead_lettered == 1
  dl = pub.to("proj/main")[0]  # dead letter travels the main topic
  assert dl["delivery"]["dlq"] is True
  assert dl["parse_error"]["stage"] == "otlp_decode"
  assert dl["source_position"] is None  # whole-request failure
  assert dl["idempotency_key"] is not None  # keyed from request hash + stage
  # raw_b64 round-trips to the original body (replayable).
  assert base64.b64decode(dl["raw_preservation"]["raw_b64"]) == body


def test_valid_json_with_wrong_otlp_shape_is_dead_lettered_not_500():
  # Valid JSON, malformed OTLP structure: the decode library raises inside the
  # receiver's guard; it must dead-letter (keyed + replayable), never crash.
  body = json.dumps({"resourceLogs": [None]}).encode("utf-8")
  result, pub = _call("/v1/logs", body)
  assert result.status == 400
  assert result.dead_lettered == 1
  dl = pub.to("proj/main")[0]  # dead letter travels the main topic
  assert dl["delivery"]["dlq"] is True
  assert dl["parse_error"]["stage"] == "otlp_decode"
  assert dl["idempotency_key"] is not None
  assert base64.b64decode(dl["raw_preservation"]["raw_b64"]) == body


# --------------------------------------------------------------------------
# Traces signal-tier gate (#324)
# --------------------------------------------------------------------------


def test_traces_path_is_404_when_disabled():
  result, pub = _call("/v1/traces", b"{}")
  assert result.status == 404
  assert pub.calls == []


def test_traces_path_is_501_when_enabled_landing_deferred():
  result, pub = _call("/v1/traces", b"{}", config=_config(enable_traces=True))
  assert result.status == 501
  assert pub.calls == []


def test_traces_requires_auth_before_revealing_gate_state():
  # Unauthenticated callers must get 401 on /v1/traces regardless of the gate,
  # so 404-vs-501 never leaks to them.
  for enabled in (False, True):
    result, pub = _call(
        "/v1/traces",
        b"{}",
        token="wrong",
        config=_config(enable_traces=enabled),
    )
    assert result.status == 401
    assert pub.calls == []


# --------------------------------------------------------------------------
# decode_body + guard + route_envelopes units
# --------------------------------------------------------------------------


def test_decode_body_rejects_unsupported_content_type():
  with pytest.raises(receiver.DecodeError):
    receiver.decode_body("log", "text/csv", b"a,b,c")


def test_dead_letter_key_guard_rejects_stage_only():
  # The receiver always passes a request hash; a stage-only key would collapse
  # unrelated whole-request failures, so it is forbidden.
  with pytest.raises(ValueError):
    env.dead_letter_key("otlp_decode", None, None)


def test_route_envelopes_sends_all_to_main_and_counts_dlq():
  pub = FakePublisher()
  envelopes = [
      {"delivery": {"dlq": False}},
      {"delivery": {"dlq": True}},
      {"delivery": {"dlq": False}},
  ]
  published, dead = receiver.route_envelopes(envelopes, pub, _config())
  assert (published, dead) == (2, 1)
  # everything on the main topic; the consumer routes dlq=true to the table
  assert len(pub.to("proj/main")) == 3
  assert pub.to("proj/dlq") == []


# --------------------------------------------------------------------------
# WSGI entrypoint (app.py) — no pubsub/proto deps needed (injected fake)
# --------------------------------------------------------------------------


def _wsgi_call(
    application, path, body=b"", auth=None, content_type="application/json"
):
  environ = {
      "PATH_INFO": path,
      "REQUEST_METHOD": "POST",
      "CONTENT_LENGTH": str(len(body)),
      "CONTENT_TYPE": content_type,
      "wsgi.input": io.BytesIO(body),
  }
  if auth is not None:
    environ["HTTP_AUTHORIZATION"] = auth
  captured: dict = {}

  def start_response(status, headers):
    captured["status"] = status

  out = b"".join(application(environ, start_response))
  return captured["status"], out


def test_wsgi_healthz_returns_ok():
  application = otlp_app.make_app(config=_config(), publisher=FakePublisher())
  status, out = _wsgi_call(application, "/healthz")
  assert status.startswith("200")
  assert out == b"ok"


def test_wsgi_logs_export_routes_through_the_app():
  pub = FakePublisher()
  application = otlp_app.make_app(config=_config(), publisher=pub)
  status, out = _wsgi_call(
      application, "/v1/logs", _logs_body(), auth=f"Bearer {TOKEN}"
  )
  assert status.startswith("200")
  assert json.loads(out)["published"] == 1
  assert len(pub.to("proj/main")) == 1


def test_wsgi_unauthenticated_is_401():
  application = otlp_app.make_app(config=_config(), publisher=FakePublisher())
  status, _ = _wsgi_call(
      application, "/v1/logs", _logs_body(), auth="Bearer nope"
  )
  assert status.startswith("401")
